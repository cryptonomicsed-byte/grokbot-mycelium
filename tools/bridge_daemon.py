#!/usr/bin/env python3
"""Bridge daemon: tail grokbot's tape into the substrate, then re-measure.

Designed to run under ares-control as `ares-grokbot-paper.service` (category
`paper` -- see ares-control/service/daemons.py). One loop, two jobs, in this
order and for a reason:

  1. sync      grokbot's JSONL -> substrate traces
  2. measure   joined traces -> calibration report (and a finding, if the
               sample supports one)

Sync first because a measurement over a stale trace set would be reported as a
fresh one, and the report has no field for "when the underlying tape ended".

SILENCE IS NOT SUCCESS HERE. The convergence-signal watchdog pattern in this
ecosystem runs quiet and reports only on change, because a heartbeat that says
"fine" every five minutes trains you to ignore it. That works for a monitor
whose job is to notice. This process' job is to keep producing evidence, so it
logs every pass -- a bridge that has silently stopped is indistinguishable
from a tape with no new closes, and only the log separates them.

Exit codes are reserved for "the loop cannot continue". A single failed pass
is logged and retried; a permanently missing log directory is an error, because
that is a misconfiguration someone must fix.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _log(path: Path | None, event: dict) -> None:
    event["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = json.dumps(event, ensure_ascii=False)
    print(line, flush=True)
    if path:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            print(f"log write failed: {exc}", file=sys.stderr, flush=True)


def _load_substrate(substrate_path: str | None):
    path = substrate_path or os.environ.get("MYCELIUM_SUBSTRATE_PATH")
    if path:
        sys.path.insert(0, path)
    import mycelium_substrate.core as core  # noqa: E402
    return core


def one_pass(*, log_path: Path, cursor: Path, agent: str,
             substrate_path: str | None, emit: bool, dry_run: bool) -> dict:
    core = _load_substrate(substrate_path)
    if os.environ.get("MYCELIUM_DB"):
        core.init_db(os.environ["MYCELIUM_DB"])
    from gmbridge import bridge, calibrate  # noqa: E402

    if not log_path.exists():
        return {"event": "no_tape", "log": str(log_path),
                "detail": "grokbot has not written its tape yet"}

    synced = bridge.sync(log_path, store=core, agent=agent, cursor_path=cursor,
                         dry_run=dry_run)
    rows = core.query_traces(agent=agent, limit=100000)
    closes = calibrate.closes_from(rows)
    skips = [r for r in rows if (dict(r).get("action") == "pumpfun.skip")]
    report = calibrate.build_report(closes, unverifiable_skips=len(skips))

    result = {
        "event": "pass",
        "synced": synced["emitted"],
        "bad_lines": synced["bad_lines"],
        "restarted": synced.get("restarted", False),
        "resolved_closes": report["resolved_closes"],
        "unverifiable_skips": report["unverifiable_skips"],
        "measured": report["measured"],
        "verdict": report["verdict"],
        "discrimination": report.get("discrimination"),
        "discrimination_significant": report.get("discrimination_significant"),
        "held_out_generalises": (report.get("held_out") or {}).get("heldout_generalises"),
        "top_bucket": (report.get("top_measured_bucket") or {}).get("bucket"),
        "top_bucket_hit_rate": (report.get("top_measured_bucket") or {}).get("hit_rate"),
    }
    if emit and report["measured"] and not dry_run:
        finding = calibrate.emit_finding(report, store=core)
        result["finding"] = finding.get("id") if finding else None
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="grokbot tape -> Mycelium bridge daemon")
    parser.add_argument("--tape", default=os.environ.get("GROKBOT_TAPE",
                                                         "~/grokbot-pumpfun/logs/trades.jsonl"))
    parser.add_argument("--cursor", default=os.environ.get(
        "GROKBOT_CURSOR", "~/.grokbot-mycelium/cursor.json"))
    parser.add_argument("--state-log", default=os.environ.get(
        "GROKBOT_STATE_LOG", "~/.grokbot-mycelium/pass.jsonl"))
    parser.add_argument("--agent", default="grokbot-pumpfun")
    parser.add_argument("--substrate-path", default=os.environ.get("MYCELIUM_SUBSTRATE_PATH"))
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument("--no-emit", action="store_true", help="measure but never persist")
    parser.add_argument("--dry-run", action="store_true", help="read, write nothing")
    args = parser.parse_args(argv)

    log_path = Path(os.path.expanduser(args.tape))
    cursor = Path(os.path.expanduser(args.cursor))
    state_log = Path(os.path.expanduser(args.state_log))

    while True:
        try:
            event = one_pass(log_path=log_path, cursor=cursor, agent=args.agent,
                             substrate_path=args.substrate_path,
                             emit=not args.no_emit, dry_run=args.dry_run)
        except Exception as exc:  # a failed pass must not kill the loop
            event = {"event": "pass_failed", "error": f"{type(exc).__name__}: {exc}"}
        _log(state_log, event)
        if args.once:
            return 0
        time.sleep(max(15, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
