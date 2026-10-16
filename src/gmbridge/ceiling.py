"""Conviction-ceiling guard for any push out of this bridge into Vantage.

WHY THIS MODULE EXISTS
----------------------
Vantage's trading ingest is not a research endpoint. Verified live in this
tree, Vantage/backend/routers/trading.py:

    2145:  conviction: 0-1 confidence (>0.7 auto-executes if wallet exists)
    2171:  if conviction > 0.7 and direction_upper in ("BUY","LONG","BULLISH"):
    2233:      INSERT INTO trading_orders (...)   <- a REAL order

and Vantage/backend/config.py:

    138:   PUMPFUN_SCAN_CONVICTION: float = 0.72   # >0.7 -> auto-order in ingest

grokbot's scoring matrix emits a 0-1 `scores.total`. The obvious wiring --
put the score in a field named conviction -- turns a research score into an
order-placing bot, silently, because the number already sits in the right
domain. Nothing about the value looks wrong; only its destination is.

So this module is the only sanctioned path for a score to approach that
field, and it clamps strictly below the line by construction. Crossing the
line requires a separately-named opt-in that defaults off.

TWO NAMED THINGS, TWO DIFFERENT JOBS
------------------------------------
  conviction_for(category_score)  -> a 0-1 number for a category score
  A category score is a ranking device. It is not the probability of
  anything. It must never be normalised into a field a consumer reads as a
  likelihood, and it must never be handed to order placement.
"""

from __future__ import annotations

# The line Vantage auto-executes at. Copied from trading.py:2171, asserted in
# tests so a Vantage change that moves it fails loudly here instead of
# quietly re-enabling the hazard.
AUTO_EXECUTE_THRESHOLD = 0.7

# Strictly under the line. 0.69 leaves the ordering legible while making
# "crossed the threshold" impossible rather than merely unlikely.
MAX_PUSH_CONVICTION = 0.69


class ExecutionBoundaryRefused(RuntimeError):
    """Raised when a caller tries to aim research output at order placement."""


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def conviction_for(
    category_score: float,
    *,
    scale_max: float = 100.0,
    allow_auto_execute: bool = False,
) -> float:
    """Map a category score onto Vantage's 0-1 conviction field, under the ceiling.

    `allow_auto_execute` is the separately-named opt-in. It defaults False and
    is never derived from the input -- a score cannot argue its way past the
    ceiling.

    Negative and non-finite inputs are clamped, not extrapolated: a caller
    passing -5 or 1e9 has a scale bug, and the honest response is the ceiling
    value or the floor, never a value outside the field's domain.
    """
    if category_score != category_score:  # NaN
        raise ValueError("category_score must be a finite number, got NaN")
    if category_score in (float("inf"), float("-inf")):
        raise ValueError("category_score must be a finite number, got inf")
    if scale_max <= 0:
        raise ValueError(f"scale_max must be positive, got {scale_max}")

    ceiling = 1.0 if allow_auto_execute else MAX_PUSH_CONVICTION
    normalised = _clamp(category_score, 0.0, scale_max) / scale_max
    return round(normalised * ceiling, 4)


def assert_not_execution_target(url: str) -> None:
    """Refuse the trading ingest path outright, by name.

    grokbot's own log keys decisions on `mint`. Vantage's trading ingest keys
    the resulting order on `symbol` (trading.py:2237). A pump.fun ticker is
    not resolvable, so a signal routed there can only produce an order for a
    string nobody can fill -- and it would be created by a real order-insert
    for any conviction above the line. The intel ingest carries `mint` and is
    the only correct destination for research output.
    """
    if "/api/trading/signals/ingest" in url:
        raise ExecutionBoundaryRefused(
            "refused: /api/trading/signals/ingest auto-creates real orders above "
            f"conviction {AUTO_EXECUTE_THRESHOLD}. Research output goes to "
            "/api/intel/signals/ingest, which carries `mint`. Route by name, "
            "never as an arithmetic side effect."
        )
