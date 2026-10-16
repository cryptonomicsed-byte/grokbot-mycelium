"""Tests for the conviction ceiling.

The last two tests read Vantage's own source. They are the reason this file
exists: the ceiling is a fact about another codebase, and a fact about another
codebase that nothing checks is a comment. If Vantage moves its
auto-execution line, these fail here instead of quietly re-arming the hazard.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gmbridge import ceiling  # noqa: E402

VANTAGE_TRADING = Path.home() / "Vantage" / "backend" / "routers" / "trading.py"
VANTAGE_CONFIG = Path.home() / "Vantage" / "backend" / "config.py"


# --------------------------------------------------------------- the clamp

def test_nothing_maps_to_or_above_the_line():
    for score in (0, 19, 35, 55, 64.5, 69.5, 100, 1000, -5, 1e9):
        value = ceiling.conviction_for(score)
        assert value < ceiling.AUTO_EXECUTE_THRESHOLD, (score, value)
        assert 0.0 <= value <= 1.0, (score, value)


def test_max_score_hits_the_ceiling_exactly():
    assert ceiling.conviction_for(100) == ceiling.MAX_PUSH_CONVICTION
    assert ceiling.conviction_for(1000) == ceiling.MAX_PUSH_CONVICTION, "clamped, not extrapolated"


def test_negative_is_the_floor_not_negative():
    assert ceiling.conviction_for(-5) == 0.0


def test_non_finite_is_rejected_not_coerced():
    for bad in (float("nan"), float("inf"), float("-inf")):
        try:
            ceiling.conviction_for(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} should raise")


def test_zero_scale_is_rejected():
    try:
        ceiling.conviction_for(50, scale_max=0)
    except ValueError:
        return
    raise AssertionError("scale_max=0 must raise")


def test_crossing_the_line_requires_the_named_opt_in():
    assert ceiling.conviction_for(100, allow_auto_execute=True) == 1.0
    assert ceiling.conviction_for(100) < ceiling.AUTO_EXECUTE_THRESHOLD


def test_ordering_is_preserved_below_the_ceiling():
    """The clamp must not flatten the ranking -- a research score still has to
    sort."""
    low, mid, high = (ceiling.conviction_for(s) for s in (10, 50, 90))
    assert low < mid < high


# ------------------------------------------------------- destination guard

def test_trading_ingest_is_refused_by_name():
    try:
        ceiling.assert_not_execution_target("https://v/ api/trading/signals/ingest".replace(" ", ""))
    except ceiling.ExecutionBoundaryRefused as exc:
        assert "auto-creates real orders" in str(exc)
        return
    raise AssertionError("trading ingest must be refused")


def test_intel_ingest_is_allowed():
    ceiling.assert_not_execution_target("https://vantage/api/intel/signals/ingest")


# --------------------------------------------------- assertions about Vantage

def test_vantage_auto_execution_threshold_is_still_0_7():
    if not VANTAGE_TRADING.exists():
        print("      (skipped: Vantage tree not present on this host)")
        return
    source = VANTAGE_TRADING.read_text(encoding="utf-8")
    match = re.search(r"conviction\s*>\s*([0-9.]+)", source)
    assert match, "could not find the auto-execution comparison in trading.py"
    assert float(match.group(1)) == ceiling.AUTO_EXECUTE_THRESHOLD, (
        f"Vantage moved the line to {match.group(1)}; MAX_PUSH_CONVICTION "
        f"({ceiling.MAX_PUSH_CONVICTION}) must be re-derived"
    )


def test_vantage_order_insert_still_follows_the_threshold():
    """The guard only matters because the high-conviction branch inserts a real
    order. If that ever stops being true, this module is dead weight and should
    be deleted rather than kept as decoration."""
    if not VANTAGE_TRADING.exists():
        print("      (skipped: Vantage tree not present on this host)")
        return
    source = VANTAGE_TRADING.read_text(encoding="utf-8")
    assert "INSERT INTO trading_orders" in source, (
        "high-conviction branch no longer inserts an order -- re-evaluate whether "
        "this ceiling is still needed"
    )


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
    print(f"ceiling: {'OK' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
