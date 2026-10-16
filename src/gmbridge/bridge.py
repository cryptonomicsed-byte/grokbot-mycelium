"""grokbot-pumpfun -> Mycelium bridge.

WHAT THIS IS
------------
grokbot-pumpfun (github zostaff/grokbot-pumpfun, MIT) is a pump.fun new-launch
funnel whose live executor is a stub by design -- src/executor.py:246/251 both
raise NotImplementedError. What it *does* produce, run entirely in dry-run, is
a JSONL tape of labelled decisions:

    logs/trades.jsonl
      {type: intent, mint, symbol, score, size_sol, ts, mode}
      {type: buy,    mint, scores{audit,narrative,timing,metrics,total},
                      metrics{...}, token{...}, ts, mode}
      {type: skip,   mint, stage, reason, score, ts, mode}
      {type: close,  mint, creator, entry_price, exit_price,
                      pnl_sol, pnl_pct, hold_seconds, reason, score, ts, mode}

The `close` record is the part nothing else in this ecosystem has: it carries
`score` -- the entry score -- *next to the realised outcome*. That is a
labelled pair, and it is the missing input underneath Mycelium's miners.

WHY THAT MATTERS
----------------
Mycelium's miners currently assert confidence from invented arithmetic:

    min(0.95, 0.5 + 0.08 * count)            # recurring_workflow
    min(0.95, 0.4 + rate)                    # anomaly
    min(0.9,  0.5 + 0.15 * len(agents))      # cross_agent
    min(0.97, 0.55 + 0.05 * count)           # opportunity
    min(0.9,  0.5 + 0.08 * distinct_wallets) # wallet_activity
    min(0.92, 0.5 + 0.2 * len(shared))       # wallet_correlation

Those fields land in a column and get ordered by. A fabricated number decides
which findings surface and in what order. Nothing in the substrate could
falsify them, because the substrate held no outcomes -- only actions.

This bridge supplies outcomes. `calibrate.py` then measures the entry score
against them and emits a finding whose confidence is a MEASURED rate, or
emits nothing at all when the sample is too small to measure. The invented
formulas are not touched; this is a second, falsifiable source laid alongside
them.

DESIGN PROPERTIES
-----------------
- Cursor by byte offset, so re-running is idempotent and a crash mid-file
  re-reads only what it lost. Guarded against truncation/rotation.
- No opinions about the host. Reads a path, writes to the substrate's
  configured store. Runs wherever python3 does.
- A malformed line is counted and skipped, never fatal -- same contract as
  grokbot's own read_log.
- The store is injected, so tests never touch the live substrate.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

DEFAULT_CURSOR = "~/.grokbot-mycelium/cursor.json"

# Actions are namespaced so they read as one family in the trace log and so
# Mycelium's sequence miners group them without colliding with other agents'.
ACTION_INTENT = "pumpfun.intent"
ACTION_BUY = "pumpfun.buy"
ACTION_SKIP = "pumpfun.skip"
ACTION_CLOSE = "pumpfun.close"

TARGET_PREFIX = "mint:"


def mint_target(mint: str) -> str:
    """Everything about one token is reachable by this one URI.

    A skipped token, a bought token and a closed token share a target, so the
    decision and its outcome are adjacent in the log -- which is what makes
    calibration a join the substrate can already do.
    """
    return f"{TARGET_PREFIX}{mint}"


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _scores_block(record: Dict[str, Any]) -> Dict[str, float]:
    raw = record.get("scores") or {}
    out: Dict[str, float] = {}
    for key in ("audit", "narrative", "timing", "metrics", "total"):
        number = _finite(raw.get(key))
        if number is not None:
            out[key] = round(number, 6)
    return out


def entry_score(record: Dict[str, Any]) -> Optional[float]:
    """The score a decision was taken on, from whichever record carries it.

    `buy` nests it under scores.total; `close` and `skip` carry it flat under
    `score`; `intent` carries it flat too. Returns None rather than 0.0 when
    absent -- an unmeasured score is not a score of zero, and defaulting to
    0.0 would file every unlabelled record into the bottom calibration bucket.
    """
    nested = _finite((record.get("scores") or {}).get("total"))
    if nested is not None:
        return round(nested, 6)
    return _finite(record.get("score"))


def trace_for(record: Dict[str, Any], *, session: str, agent: str) -> Optional[Dict[str, Any]]:
    """Map one grokbot record onto one substrate trace, or None if unmappable.

    Outcome vocabulary is the substrate's, not grokbot's:
      a closed position is success when pnl_sol > 0, else failure. Break-even
      (pnl_sol == 0) is failure, because a trade that returns nothing after
      fees and slippage did not win -- and rounding it to the favourable side
      is how a win rate drifts.
      Everything pre-outcome is `info`: an intent, a buy and a skip are all
      assertions, not results. Only money makes a result.
    """
    kind_of = str(record.get("type") or "")
    mint = str(record.get("mint") or "").strip()
    if not mint:
        return None

    ts = _finite(record.get("ts"))
    payload: Dict[str, Any] = {
        "symbol": record.get("symbol"),
        "mode": record.get("mode"),
        "source_type": kind_of,
        # Provenance: which record this trace came from, so a finding can always
        # be argued back to a line in grokbot's own log.
        "log_ts": ts,
    }

    if kind_of == "intent":
        action, outcome, kind = ACTION_INTENT, "info", "decision"
        payload["size_sol"] = _finite(record.get("size_sol"))
        score = entry_score(record)
        if score is not None:
            payload["score"] = score

    elif kind_of == "buy":
        action, outcome, kind = ACTION_BUY, "info", "observation"
        payload["size_sol"] = _finite(record.get("size_sol"))
        payload["entry_price"] = _finite(record.get("entry_price"))
        payload["scores"] = _scores_block(record)
        score = entry_score(record)
        if score is not None:
            payload["score"] = score
        metrics = record.get("metrics") or {}
        payload["metrics"] = {
            k: metrics.get(k)
            for k in ("risk_score", "creator_share", "sniper_count", "wallet_diversity",
                      "curve_health", "unique_wallets", "round_trip_cost_pct",
                      "curve_liquidity_sol")
            if metrics.get(k) is not None
        }
        token = record.get("token") or {}
        payload["token"] = {k: token.get(k) for k in
                            ("age_seconds", "unique_buyers", "curve_progress",
                             "market_cap_sol", "creator") if token.get(k) is not None}
        payload["checker_approved"] = bool((record.get("checker") or {}).get("approve"))

    elif kind_of == "skip":
        action, outcome, kind = ACTION_SKIP, "info", "observation"
        payload["stage"] = record.get("stage")
        payload["reason"] = record.get("reason")
        payload["detail"] = record.get("detail")
        score = entry_score(record)
        if score is not None:
            payload["score"] = score

    elif kind_of == "close":
        action, kind = ACTION_CLOSE, "observation"
        pnl_sol = _finite(record.get("pnl_sol"))
        pnl_pct = _finite(record.get("pnl_pct"))
        # Break-even counts as failure. See docstring.
        outcome = "success" if (pnl_sol or 0.0) > 0 else "failure"
        payload["pnl_sol"] = pnl_sol
        payload["pnl_pct"] = pnl_pct
        payload["hold_seconds"] = _finite(record.get("hold_seconds"))
        payload["reason"] = record.get("reason")
        payload["creator"] = record.get("creator")
        payload["final"] = record.get("final")
        score = entry_score(record)
        if score is not None:
            payload["score"] = score

    else:
        return None

    return {
        "agent": agent,
        "session": session,
        "kind": kind,
        "action": action,
        "target": mint_target(mint),
        "outcome": outcome,
        "payload": payload,
    }


def _load_cursor(path: Path) -> Dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {}


_VERIFY_BYTES = 64


def _tail_probe(path: Path, offset: int) -> Optional[str]:
    """Hash of the last few bytes already consumed, for verifying the cursor.

    An offset alone is not enough. If the log is rewritten in place -- rotated,
    truncated, or recreated by another process -- the same offset points into
    different bytes, and a tailer comparing only numbers will read a fragment,
    count it as a malformed line, and go quietly silent about everything after
    it. Hashing the bytes *ending* at the cursor is stable under appends (which
    do not touch them) and detects every replacement, so the failure becomes a
    restart instead of a stall.
    """
    if offset <= 0:
        return None
    try:
        size = path.stat().st_size
        if size < offset:
            return "size-below-offset"
        with path.open("rb") as fh:
            fh.seek(max(0, offset - _VERIFY_BYTES))
            window = fh.read(offset - max(0, offset - _VERIFY_BYTES))
    except OSError:
        return None
    return hashlib.sha256(window).hexdigest()[:16]


def _replaced(previous: Optional[str], current: Optional[str]) -> bool:
    """True when the bytes we already read are no longer the bytes at that offset.

    NOTE the consequence, because it is a real trade-off and not a detail: a
    detected replacement re-reads from the beginning of the file, so records
    already ingested can be ingested a second time. At-least-once, not
    exactly-once. Traces are append-only observations, so a duplicate is
    survivable; silently skipping the rest of the log is not.
    """
    if previous is None or current is None:
        return False
    return previous != current


def _save_cursor(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    tmp.replace(path)


def read_from(path: Path, offset: int) -> Tuple[List[Dict[str, Any]], int, int]:
    """Read complete JSONL lines from `offset`.

    Returns (records, new_offset, bad_lines). new_offset is only advanced past
    the last complete line, so a record half-written by a live trader is read
    on the next pass rather than parsed as garbage.
    """
    records: List[Dict[str, Any]] = []
    bad = 0
    try:
        size = path.stat().st_size
    except OSError:
        return records, offset, bad

    if offset > size:
        offset = 0  # truncated or rotated: start over rather than sit silent

    with path.open("rb") as fh:
        fh.seek(offset)
        start = offset
        buffer = b""
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            buffer += chunk
        # Consume only up to the final newline; keep the remainder for next time.
        last_newline = buffer.rfind(b"\n")
        if last_newline < 0:
            return records, start, bad
        complete, remainder = buffer[: last_newline + 1], buffer[last_newline + 1:]

    consumed = len(complete)
    for raw in complete.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            bad += 1
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
        else:
            bad += 1
    return records, start + consumed, bad


def sync(
    log_path: str | Path,
    *,
    store: Any,
    agent: str = "grokbot-pumpfun",
    session: str = "paper",
    cursor_path: str | Path = DEFAULT_CURSOR,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Tail grokbot's JSONL tape into the substrate. Idempotent per byte.

    `store` is the substrate module (mycelium_substrate.core) or anything
    exposing .emit() with the same signature. Injected so tests and dry runs
    cannot write to a live substrate by accident.
    """
    log = Path(os.path.expanduser(str(log_path)))
    cursor_file = Path(os.path.expanduser(str(cursor_path)))
    key = f"{agent}:{log}"
    state = _load_cursor(cursor_file)
    entry = state.get(key) or {}
    offset = int(entry.get("offset") or 0)
    emitted_before = int(entry.get("emitted") or 0)

    probe = _tail_probe(log, offset)
    restarted = False
    if offset > 0 and _replaced(entry.get("probe"), probe):
        offset = 0  # the bytes we already read are gone; re-read from the top
        restarted = True
        probe = None

    records, new_offset, bad = read_from(log, offset)

    summary = {
        "log": str(log),
        "read": len(records),
        "emitted": 0,
        "skipped_unmappable": 0,
        "bad_lines": bad,
        "dry_run": dry_run,
        "restarted": restarted,
        "offset_before": offset,
        "offset_after": new_offset,
        "counts": {},
    }

    for record in records:
        trace = trace_for(record, session=session, agent=agent)
        if trace is None:
            summary["skipped_unmappable"] += 1
            continue
        summary["counts"][trace["action"]] = summary["counts"].get(trace["action"], 0) + 1
        if dry_run:
            summary["emitted"] += 1
            continue
        store.emit(**trace)
        summary["emitted"] += 1

    if not dry_run:
        state[key] = {
            "offset": new_offset,
            "emitted": emitted_before + summary["emitted"],
            "log": str(log),
            "probe": _tail_probe(log, new_offset),
            "updated": time.time(),
        }
        _save_cursor(cursor_file, state)

    return summary
