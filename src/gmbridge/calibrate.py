"""Measure grokbot's entry score against realised outcomes.

THE QUESTION THIS ANSWERS
-------------------------
Not "does the score look right" -- "of the tokens it scored high, what share
actually made money, and does that hold up on data the fit never saw?"

grokbot's replay.py draws the score distribution and the money separately. It
never joins them, so the number that would justify ranking on the score -- the
realised hit rate per score bucket -- does not exist in either project. This
module produces it.

TWO QUESTIONS, KEPT APART
-------------------------
  does it SORT?          top half vs bottom half hit rate, tested against the
                         standard error of the difference
  does it GENERALISE?    bucket rates fitted on the first half of the tape,
                         scored against the second half

They have separate answers and the report keeps them separate. Collapsing them
is how a ranking device gets quoted as a probability.

WHAT IT REFUSES TO DO
---------------------
It will not emit a number it has not measured. Below MIN_SAMPLES resolved
closes it emits nothing and says so, rather than reporting 0.5 -- a
mid-ranking value that reads as a mild positive. `undefined != zero`, and
`unmeasured != neutral`.

It also will not claim the score is informative. "No measurable
discrimination" is a finding about the score, not a failure of the comparison.

A FIELD THIS MODULE DELIBERATELY DOES NOT HAVE
----------------------------------------------
A first cut reported `brier_binned` -- the Brier score using each bucket's own
measured hit rate -- against the base rate. It looked like a calibration test
and it was decorative: in-sample bucket rates minimise squared error within
each bucket, so binning on ANY partition cannot score worse than the global
constant. The comparison was guaranteed true before the data arrived, which
makes it the same defect as `min(0.95, 0.5 + 0.08 * count)` wearing different
clothes. It was removed after the first end-to-end run, and the held-out split
below replaced it because a held-out split can actually fail.

THE HONEST LIMIT, STATED IN THE OUTPUT
--------------------------------------
Only *buys* get outcomes. A skipped token's later price is never observed by
grokbot, so the funnel's own precision is unmeasurable from its own log. The
count of unverifiable skips is printed alongside every result, because a
calibration over the surviving buys answers a narrower question than "is the
funnel good" -- and quoting it as a funnel number would be wrong.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
from typing import Any, Dict, List, Optional, Tuple

# Buckets match scripts/replay.py's own BUCKETS, so this report and grokbot's
# can be read side by side without re-binning.
BUCKETS: List[Tuple[float, float]] = [
    (0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01),
]

# Below this there is no measurement, only a small sample that happens to have
# a rate. Chosen as the point where a bucket's hit rate stops moving by more
# than ~10 points on one more observation.
MIN_SAMPLES = 30
MIN_BUCKET = 10
# Each half of a held-out split needs enough closes to estimate rates at all.
MIN_SPLIT = 20

MINER_NAME = "calibration"


def bucket_label(score: float) -> str:
    for low, high in BUCKETS:
        if low <= score < high:
            return f"{low:.1f}-{min(high, 1.0):.1f}"
    return "?"


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _row_to_dict(row: Any) -> Dict[str, Any]:
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {}


def closes_from(rows: List[Any], *, action: str = "pumpfun.close") -> List[Dict[str, Any]]:
    """Extract labelled (score, outcome) pairs from substrate traces.

    A close without a score, or without a finite pnl, is DROPPED and therefore
    counted in neither the numerator nor the denominator -- not defaulted.
    Filing an unscored close into the bottom bucket would manufacture a losing
    sample for a score that was never recorded.
    """
    closes: List[Dict[str, Any]] = []
    for row in rows:
        record = row if isinstance(row, dict) else _row_to_dict(row)
        if record.get("action") != action:
            continue
        payload = record.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                payload = {}
        payload = payload or {}
        score = _finite(payload.get("score"))
        pnl_sol = _finite(payload.get("pnl_sol"))
        if score is None or pnl_sol is None:
            continue
        closes.append({
            "mint": str(record.get("target") or "").replace("mint:", ""),
            "score": score,
            "pnl_sol": pnl_sol,
            "pnl_pct": _finite(payload.get("pnl_pct")),
            "reason": payload.get("reason"),
            "creator": payload.get("creator"),
            "ts": _finite(record.get("ts")) or _finite(payload.get("log_ts")) or 0.0,
            "win": 1.0 if pnl_sol > 0 else 0.0,
        })
    return closes


def bucket_table(closes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    table: List[Dict[str, Any]] = []
    for low, high in BUCKETS:
        group = [c for c in closes if low <= c["score"] < high]
        label = f"{low:.1f}-{min(high, 1.0):.1f}"
        if not group:
            table.append({"bucket": label, "n": 0, "wins": 0,
                          "hit_rate": None, "mean_pnl_sol": None})
            continue
        wins = sum(1 for c in group if c["win"] > 0)
        table.append({
            "bucket": label,
            "n": len(group),
            "wins": wins,
            "hit_rate": round(wins / len(group), 4),
            "mean_pnl_sol": round(statistics.fmean(c["pnl_sol"] for c in group), 6),
        })
    return table


def brier(pairs: List[Tuple[float, float]]) -> Optional[float]:
    """Mean squared error of a probability against a binary outcome."""
    if not pairs:
        return None
    return round(sum((p - y) ** 2 for p, y in pairs) / len(pairs), 6)


def _two_proportion_se(p1: float, n1: int, p2: float, n2: int) -> float:
    """Standard error of the difference in two proportions."""
    return ((p1 * (1 - p1) / n1) + (p2 * (1 - p2) / n2)) ** 0.5


def held_out_check(closes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Fit bucket rates on the earlier half of the tape, score the later half.

    This is the only comparison in this module that can FAIL for a real reason.
    In-sample bucket rates cannot lose to a constant (they minimise squared
    error within each bucket by construction), so any in-sample version of this
    test is guaranteed true and measures nothing.

    Splitting by time rather than at random, because the tape is chronological:
    a random split lets later outcomes inform an "earlier" fit, which is the
    leak the check exists to prevent.
    """
    ordered = sorted(closes, key=lambda c: c["ts"])
    half = len(ordered) // 2
    fit, evaluate = ordered[:half], ordered[half:]
    if len(fit) < MIN_SPLIT or len(evaluate) < MIN_SPLIT:
        return {"available": False,
                "reason": f"needs >= {MIN_SPLIT} closes per half "
                          f"(have {len(fit)}/{len(evaluate)})"}

    rates: Dict[str, float] = {}
    for row in bucket_table(fit):
        if row["n"] > 0:
            rates[row["bucket"]] = row["hit_rate"]

    # A bucket with no fit-set examples has no rate. Those closes are excluded
    # rather than assigned the base rate, which would silently credit the model
    # for the prediction it did not make.
    model_pairs: List[Tuple[float, float]] = []
    for close in evaluate:
        rate = rates.get(bucket_label(close["score"]))
        if rate is not None:
            model_pairs.append((rate, close["win"]))

    fit_base = sum(c["win"] for c in fit) / len(fit)
    if not model_pairs:
        return {"available": False, "reason": "no fit-set bucket covers the evaluation half"}

    base_pairs = [(fit_base, c["win"]) for c in evaluate]
    model_brier = brier(model_pairs)
    base_brier = brier(base_pairs)
    return {
        "available": True,
        "fit_n": len(fit),
        "eval_n": len(evaluate),
        "eval_covered": len(model_pairs),
        "fit_base_rate": round(fit_base, 4),
        "eval_base_rate": round(sum(c["win"] for c in evaluate) / len(evaluate), 4),
        "heldout_brier_model": model_brier,
        "heldout_brier_base": base_brier,
        "heldout_generalises": bool(model_brier is not None
                                   and base_brier is not None
                                   and model_brier < base_brier),
    }


def _verdict(discriminates: bool, held_out: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """The report's conclusion, kept in one place so every branch is testable."""
    if not discriminates:
        return ("no measurable discrimination",
                "top-half and bottom-half hit rates differ by less than twice their "
                "standard error; the score does not separate winners from losers on "
                "this sample")
    if not held_out.get("available"):
        return ("score discriminates (held-out check unavailable)",
                f"ranking holds in-sample; generalisation not tested: {held_out.get('reason')}")
    if held_out.get("heldout_generalises"):
        return ("score discriminates and generalises out of sample", None)
    return ("score discriminates in-sample but not out of sample",
            "bucket rates fitted on the earlier half do not predict the later half "
            "better than a constant -- the ranking may be overfitted to the period "
            "it was measured on")


def build_report(closes: List[Dict[str, Any]],
                 *, unverifiable_skips: int = 0) -> Dict[str, Any]:
    n = len(closes)
    report: Dict[str, Any] = {
        "resolved_closes": n,
        "bucket_table": bucket_table(closes),
        "unverifiable_skips": unverifiable_skips,
        "min_samples": MIN_SAMPLES,
        "measured": n >= MIN_SAMPLES,
        # Present-but-None on every path, so a caller can branch on the key
        # without a KeyError -- and so "no number" is representable at all.
        "top_measured_bucket": None,
        "emittable": False,
        "held_out": {"available": False, "reason": "not computed"},
    }
    if n == 0:
        report["verdict"] = "no resolved outcomes: nothing is measurable"
        report["reason"] = ("zero close records carry both a score and a pnl; "
                            "run grokbot in dry-run and let positions close")
        return report
    if n < MIN_SAMPLES:
        report["verdict"] = "unmeasured (sample too small)"
        report["reason"] = (f"{n} resolved closes < MIN_SAMPLES {MIN_SAMPLES}; "
                            "no confidence emitted rather than a mid-ranking default")
        return report

    base = sum(c["win"] for c in closes) / n
    report["base_rate"] = round(base, 4)
    # Diagnostic only: faithful to "what if a consumer read the score as a
    # probability", which nobody should. No verdict is derived from it, because
    # a rank and a likelihood are different objects that share a range.
    report["brier_score_as_probability"] = brier([(c["score"], c["win"]) for c in closes])
    report["brier_base_rate"] = brier([(base, c["win"]) for c in closes])

    ordered = sorted(closes, key=lambda c: c["score"])
    half = n // 2
    low_half, high_half = ordered[:half], ordered[half:]
    low_rate = sum(c["win"] for c in low_half) / len(low_half)
    high_rate = sum(c["win"] for c in high_half) / len(high_half)
    report["bottom_half_hit_rate"] = round(low_rate, 4)
    report["top_half_hit_rate"] = round(high_rate, 4)
    discrimination = high_rate - low_rate
    se = _two_proportion_se(high_rate, len(high_half), low_rate, len(low_half))
    report["discrimination"] = round(discrimination, 4)
    report["discrimination_se"] = round(se, 4)
    # Roughly a 95% interval. A gap inside twice its own standard error is
    # noise, and calling it discrimination would be the invention this module
    # exists to prevent.
    discriminates = abs(discrimination) > 2 * se
    report["discrimination_significant"] = discriminates

    held_out = held_out_check(closes)
    report["held_out"] = held_out
    verdict, reason = _verdict(discriminates, held_out)
    report["verdict"] = verdict
    if reason:
        report["reason"] = reason

    # The emitted number is the realised hit rate of the highest bucket with
    # enough observations -- measured, with its n attached, never a formula.
    for row in reversed(report["bucket_table"]):
        if row["n"] >= MIN_BUCKET:
            report["top_measured_bucket"] = row
            break
    report["emittable"] = report["top_measured_bucket"] is not None
    return report


def format_report(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("=" * 70)
    lines.append("  grokbot entry score vs realised outcome")
    lines.append("=" * 70)
    lines.append(f"  resolved closes: {report['resolved_closes']}")
    lines.append(f"  skips with NO observable outcome: {report['unverifiable_skips']}")
    lines.append("")
    lines.append(f"  {'score bucket':<14} {'n':>5} {'wins':>5} {'hit rate':>9} {'mean pnl SOL':>14}")
    for row in report["bucket_table"]:
        hr = "—" if row["hit_rate"] is None else f"{row['hit_rate'] * 100:5.1f}%"
        mp = "—" if row["mean_pnl_sol"] is None else f"{row['mean_pnl_sol']:+.6f}"
        lines.append(f"  {row['bucket']:<14} {row['n']:>5} {row['wins']:>5} {hr:>9} {mp:>14}")
    lines.append("")
    if "base_rate" in report:
        lines.append(f"  base rate (P(win)):          {report['base_rate'] * 100:5.1f}%")
        lines.append(f"  bottom-half hit rate:        {report['bottom_half_hit_rate'] * 100:5.1f}%")
        lines.append(f"  top-half hit rate:           {report['top_half_hit_rate'] * 100:5.1f}%")
        lines.append(f"  discrimination:              {report['discrimination']:+.4f}"
                     f"  (SE {report['discrimination_se']:.4f}, "
                     f"{'significant' if report['discrimination_significant'] else 'NOT significant'})")
        held = report.get("held_out") or {}
        if held.get("available"):
            lines.append(f"  held-out Brier, model:       {held['heldout_brier_model']}"
                         f"  (fit n={held['fit_n']}, eval n={held['eval_n']})")
            lines.append(f"  held-out Brier, base rate:   {held['heldout_brier_base']}")
            lines.append(f"  fit base {held['fit_base_rate']} vs eval base "
                         f"{held['eval_base_rate']}"
                         f"   {'DRIFTED' if abs(held['fit_base_rate'] - held['eval_base_rate']) > 0.1 else 'stable'}")
        else:
            lines.append(f"  held-out check:              not available -- {held.get('reason')}")
        lines.append(f"  Brier, raw score read as p:  {report['brier_score_as_probability']}"
                     f"  (diagnostic only, no verdict derived)")
    lines.append("")
    lines.append(f"  VERDICT: {report['verdict']}")
    if report.get("reason"):
        lines.append(f"  {report['reason']}")
    lines.append("")
    lines.append("  Scope: buys only. A skipped token's later price is never")
    lines.append("  observed, so this measures the score over the surviving")
    lines.append("  trades, NOT the funnel's precision. Quoting it as a")
    lines.append("  funnel number would be wrong.")
    lines.append("")
    return "\n".join(lines)


def emit_finding(report: Dict[str, Any], *, store: Any) -> Optional[Dict[str, Any]]:
    """Persist a calibration finding, or refuse when nothing was measured.

    NAMING WARNING, because it is a hazard this module creates rather than
    inherits. The substrate's `findings.confidence` is a REAL column that every
    other miner fills with an invented heuristic (`min(0.95, 0.5 + 0.08*count)`)
    and that consumers order by. This miner puts a MEASURED WIN RATE in it --
    a different kind of object in the same column. Concretely:

      * a measured rate and an invented heuristic must never be ORDER BY'd
        against each other; the comparison would be meaningless in both
        directions
      * within this miner the column is comparable, because every row in it is
        a win rate with an `n` attached

    The number is measured, so it belongs in the report. The column is shared,
    so it does not belong in a cross-miner ranking. Fixing that properly means
    a separate column or a miner-scoped ordering -- a substrate change, not
    something to do quietly from inside a bridge.
    """
    if not report.get("measured"):
        return None
    best = report.get("top_measured_bucket")
    if not best:
        return None

    hit = best["hit_rate"]
    title = (f"grokbot entry score measured: {hit * 100:.1f}% win rate in the "
             f"{best['bucket']} bucket (n={best['n']})")

    table_lines = ["score bucket   n    wins   hit rate   mean pnl SOL"]
    for row in report["bucket_table"]:
        hit_cell = "—" if row["hit_rate"] is None else f"{row['hit_rate'] * 100:.1f}%"
        pnl_cell = "—" if row["mean_pnl_sol"] is None else f"{row['mean_pnl_sol']:+.6f}"
        table_lines.append(
            f"{row['bucket']:<14} {row['n']:>4} {row['wins']:>5}   "
            f"{hit_cell:>8}   {pnl_cell:>12}"
        )

    held = report.get("held_out") or {}
    held_line = (f"held-out (fit/eval split)  model {held['heldout_brier_model']} vs "
                 f"base {held['heldout_brier_base']} -> "
                 f"{'generalises' if held.get('heldout_generalises') else 'does NOT generalise'}"
                 if held.get("available") else
                 f"held-out check unavailable: {held.get('reason')}")

    evidence = "\n".join([
        f"Measured over {report['resolved_closes']} resolved closes "
        f"({report['unverifiable_skips']} skips have no observable outcome and are excluded).",
        "",
        *table_lines,
        "",
        f"base rate P(win)            {report['base_rate'] * 100:.1f}%",
        f"bottom/top half hit rate    {report['bottom_half_hit_rate'] * 100:.1f}% / "
        f"{report['top_half_hit_rate'] * 100:.1f}%",
        f"discrimination (top-bottom) {report['discrimination']:+.4f} "
        f"(SE {report['discrimination_se']:.4f}, "
        f"{'significant' if report['discrimination_significant'] else 'NOT significant'})",
        held_line,
        f"raw score read as p         {report['brier_score_as_probability']} (diagnostic)",
        "",
        f"VERDICT: {report['verdict']}",
    ])

    if not report.get("discrimination_significant"):
        suggestion = (
            "No measurable discrimination: the top and bottom halves differ by less "
            "than twice the standard error. Do NOT rank or gate on this score, and do "
            "not restate it as a probability. Re-measure after more closes resolve "
            "before concluding it is worthless."
        )
    elif not held.get("available"):
        suggestion = (
            "Ranking holds in-sample but the sample is too small to test "
            "generalisation. Do not act on the ranking until a held-out split is "
            "available; re-run once more closes have resolved."
        )
    elif held.get("heldout_generalises"):
        suggestion = (
            "The entry score separates winners from losers beyond noise AND its "
            "bucket rates hold on data the fit never saw. Ranking on it survives "
            "contact with outcomes -- keep it, and cite this measured rate rather "
            "than any formula-derived confidence."
        )
    else:
        suggestion = (
            "The score sorts outcomes in-sample but its bucket rates do NOT predict "
            "the later half better than a constant. Treat the ranking as overfitted "
            "to the period it was measured on and do not gate capital on it."
        )

    return store.add_finding(
        miner=MINER_NAME,
        confidence=float(hit),
        title=title,
        evidence=evidence,
        suggestion=suggestion,
        payload={
            "resolved_closes": report["resolved_closes"],
            "unverifiable_skips": report["unverifiable_skips"],
            "base_rate": report.get("base_rate"),
            "brier_base_rate": report.get("brier_base_rate"),
            "brier_score_as_probability": report.get("brier_score_as_probability"),
            "discrimination": report.get("discrimination"),
            "discrimination_se": report.get("discrimination_se"),
            "discrimination_significant": report.get("discrimination_significant"),
            "held_out": held,
            "bucket_table": report["bucket_table"],
            # How far the measured difference sits from noise, as a multiple of
            # its own standard error. Not a probability of anything.
            "evidence_strength": (
                round(abs(report["discrimination"]) / (2 * report["discrimination_se"]), 4)
                if report.get("discrimination_se") else None
            ),
            "confidence_is_a_measured_win_rate_not_a_heuristic": True,
            "measured_not_asserted": True,
            "scope": "surviving buys only; excludes unverifiable skips",
        },
    )


def load_substrate_rows(agent: str = "grokbot-pumpfun") -> List[Any]:
    """Read traces from the configured substrate. Imported here so the pure
    functions above stay importable on a device with no substrate installed."""
    path = os.environ.get("MYCELIUM_SUBSTRATE_PATH")
    if path:
        import sys
        sys.path.insert(0, path)
    import mycelium_substrate.core as core  # noqa: E402
    return core.query_traces(agent=agent, limit=100000)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Calibrate grokbot's entry score against realised outcomes")
    parser.add_argument("--agent", default="grokbot-pumpfun")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument("--emit", action="store_true",
                        help="persist a finding when the sample is sufficient")
    args = parser.parse_args(argv)

    rows = load_substrate_rows(args.agent)
    closes = closes_from(rows)
    skips = [r for r in rows if _row_to_dict(r).get("action") == "pumpfun.skip"]
    report = build_report(closes, unverifiable_skips=len(skips))

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report))

    if args.emit:
        path = os.environ.get("MYCELIUM_SUBSTRATE_PATH")
        if path:
            import sys
            sys.path.insert(0, path)
        import mycelium_substrate.core as core  # noqa: E402
        finding = emit_finding(report, store=core)
        if finding is None:
            print("no finding emitted: sample insufficient or nothing measured")
            return 1
        print(f"finding emitted: {finding.get('id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
