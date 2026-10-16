"""Tests for the grokbot -> Mycelium bridge.

No network, no live substrate: the store is faked so these can never write a
trace into the real DB. Run directly (`python3 tests/test_bridge.py`) or under
pytest.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gmbridge import bridge  # noqa: E402


class FakeStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def emit(self, **kwargs):
        self.rows.append(kwargs)
        return kwargs


def _write(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- mapping

def test_close_profit_is_success():
    trace = bridge.trace_for(
        {"type": "close", "mint": "M1", "pnl_sol": 0.42, "pnl_pct": 31.0, "score": 0.81},
        session="paper", agent="a")
    assert trace["outcome"] == "success"
    assert trace["target"] == "mint:M1"
    assert trace["action"] == bridge.ACTION_CLOSE
    assert trace["payload"]["score"] == 0.81


def test_close_loss_is_failure():
    trace = bridge.trace_for(
        {"type": "close", "mint": "M2", "pnl_sol": -0.10, "score": 0.61},
        session="paper", agent="a")
    assert trace["outcome"] == "failure"


def test_break_even_counts_as_failure():
    """A trade that returns nothing after fees did not win. Rounding this to
    the favourable side is how a win rate drifts upward."""
    trace = bridge.trace_for(
        {"type": "close", "mint": "M3", "pnl_sol": 0.0, "score": 0.77},
        session="paper", agent="a")
    assert trace["outcome"] == "failure"


def test_buy_and_skip_are_info_not_results():
    for record in ({"type": "buy", "mint": "M4", "scores": {"total": 0.7}},
                   {"type": "skip", "mint": "M5", "stage": "scoring", "score": 0.2}):
        trace = bridge.trace_for(record, session="paper", agent="a")
        assert trace["outcome"] == "info", trace
        assert trace["kind"] == "observation"


def test_intent_is_a_decision_kind():
    trace = bridge.trace_for({"type": "intent", "mint": "M6", "score": 0.9, "size_sol": 0.05},
                             session="paper", agent="a")
    assert trace["kind"] == "decision"


def test_missing_score_is_none_not_zero():
    """Absent must not become 0.0: that would file every unscored record into
    the bottom calibration bucket and manufacture losers."""
    assert bridge.entry_score({"type": "close", "mint": "M"}) is None
    trace = bridge.trace_for({"type": "close", "mint": "M", "pnl_sol": 1.0},
                             session="paper", agent="a")
    assert "score" not in trace["payload"]


def test_record_without_mint_is_unmappable():
    assert bridge.trace_for({"type": "close", "pnl_sol": 1.0}, session="s", agent="a") is None


def test_unknown_type_is_unmappable():
    assert bridge.trace_for({"type": "heartbeat", "mint": "M"}, session="s", agent="a") is None


# ------------------------------------------------------------------ tailing

def test_sync_emits_then_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        log, cursor = tmp / "trades.jsonl", tmp / "cursor.json"
        _write(log, [
            {"type": "buy", "mint": "A", "scores": {"total": 0.8}, "ts": 1.0},
            {"type": "close", "mint": "A", "pnl_sol": 0.5, "score": 0.8, "ts": 2.0},
            {"type": "skip", "mint": "B", "stage": "analyzer", "score": 0.1, "ts": 3.0},
        ])
        store = FakeStore()
        first = bridge.sync(log, store=store, cursor_path=cursor)
        assert first["emitted"] == 3, first
        assert len(store.rows) == 3

        second = bridge.sync(log, store=store, cursor_path=cursor)
        assert second["emitted"] == 0, "re-running must not duplicate traces"
        assert len(store.rows) == 3


def test_sync_appends_only_new_records():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        log, cursor = tmp / "trades.jsonl", tmp / "cursor.json"
        _write(log, [{"type": "buy", "mint": "A", "scores": {"total": 0.8}, "ts": 1.0}])
        store = FakeStore()
        bridge.sync(log, store=store, cursor_path=cursor)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "close", "mint": "A", "pnl_sol": 0.2,
                                 "score": 0.8, "ts": 9.0}) + "\n")
        again = bridge.sync(log, store=store, cursor_path=cursor)
        assert again["emitted"] == 1
        assert len(store.rows) == 2


def test_half_written_line_is_not_consumed():
    """A live trader can be mid-write. The partial record must wait, not be
    parsed as garbage and dropped."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        log, cursor = tmp / "trades.jsonl", tmp / "cursor.json"
        _write(log, [{"type": "buy", "mint": "A", "scores": {"total": 0.8}, "ts": 1.0}])
        with log.open("a", encoding="utf-8") as fh:
            fh.write('{"type": "close", "mint": "A", "pnl_sol"')  # no newline
        store = FakeStore()
        result = bridge.sync(log, store=store, cursor_path=cursor)
        assert result["emitted"] == 1
        assert result["bad_lines"] == 0

        with log.open("a", encoding="utf-8") as fh:
            fh.write(': 0.3, "score": 0.8, "ts": 5.0}\n')
        result = bridge.sync(log, store=store, cursor_path=cursor)
        assert result["emitted"] == 1, "the completed line must be picked up"


def test_malformed_line_is_counted_not_fatal():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        log, cursor = tmp / "trades.jsonl", tmp / "cursor.json"
        _write(log, [{"type": "buy", "mint": "A", "scores": {"total": 0.8}, "ts": 1.0}])
        with log.open("a", encoding="utf-8") as fh:
            fh.write("{not json at all\n")
            fh.write(json.dumps({"type": "close", "mint": "A", "pnl_sol": 0.1,
                                 "score": 0.8, "ts": 6.0}) + "\n")
        store = FakeStore()
        result = bridge.sync(log, store=store, cursor_path=cursor)
        assert result["bad_lines"] == 1
        assert result["emitted"] == 2


def test_truncated_log_restarts_rather_than_going_silent():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        log, cursor = tmp / "trades.jsonl", tmp / "cursor.json"
        _write(log, [{"type": "buy", "mint": "A", "scores": {"total": 0.8}, "ts": 1.0}])
        store = FakeStore()
        bridge.sync(log, store=store, cursor_path=cursor)
        _write(log, [{"type": "close", "mint": "B", "pnl_sol": 0.3, "score": 0.9, "ts": 7.0}])
        result = bridge.sync(log, store=store, cursor_path=cursor)
        assert result["emitted"] == 1, result
        assert store.rows[-1]["target"] == "mint:B"


def test_dry_run_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        log, cursor = tmp / "trades.jsonl", tmp / "cursor.json"
        _write(log, [{"type": "buy", "mint": "A", "scores": {"total": 0.8}, "ts": 1.0}])
        store = FakeStore()
        result = bridge.sync(log, store=store, cursor_path=cursor, dry_run=True)
        assert result["emitted"] == 1
        assert store.rows == []
        assert not cursor.exists(), "a dry run must not advance the cursor"


def test_missing_log_is_not_an_error():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        result = bridge.sync(tmp / "nope.jsonl", store=FakeStore(),
                             cursor_path=tmp / "cursor.json")
        assert result["emitted"] == 0


def _main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    print(f"bridge: {'OK' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
