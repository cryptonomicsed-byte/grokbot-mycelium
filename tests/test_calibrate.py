"""Tests for the calibration measurement.

The synthetic datasets here exist to prove the *arithmetic, the refusals and
the branch coverage*, not the market. A fixture that encodes the same
assumption as the code proves nothing about wiring -- so the verdict branches
that cannot be reached honestly from a natural dataset are tested directly
against `_verdict`, which is why that function exists.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gmbridge import calibrate  # noqa: E402


def _row(score, pnl_sol, mint="M", ts=0.0):
    return {
        "action": "pumpfun.close",
        "target": f"mint:{mint}",
        "ts": ts,
        "payload": {"score": score, "pnl_sol": pnl_sol, "pnl_pct": pnl_sol * 100},
    }


def _calibrated(n=400, seed=7):
    """score IS the win probability, iid through time -- should generalise."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        score = rng.random()
        win = rng.random() < score
        rows.append(_row(round(score, 4), 0.5 if win else -0.3, mint=f"T{i}", ts=float(i)))
    return rows


def _independent(n=400, seed=11):
    """score unrelated to outcome -- must report no measurable discrimination."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        score = rng.random()
        win = rng.random() < 0.5
        rows.append(_row(round(score, 4), 0.5 if win else -0.3, mint=f"R{i}", ts=float(i)))
    return rows


class FakeStore:
    def __init__(self):
        self.findings = []

    def add_finding(self, **kwargs):
        kwargs["id"] = f"f{len(self.findings)}"
        self.findings.append(kwargs)
        return kwargs


# ------------------------------------------------------------- extraction

def test_closes_from_parses_payload_json_string():
    rows = [{"action": "pumpfun.close", "target": "mint:X",
             "payload": '{"score": 0.7, "pnl_sol": 0.1}'}]
    closes = calibrate.closes_from(rows)
    assert len(closes) == 1 and closes[0]["win"] == 1.0


def test_close_without_score_is_dropped_not_defaulted():
    """Dropping is the point. Filing it in the 0.0-0.5 bucket would invent a
    loser for a score that was never recorded."""
    rows = [_row(None, 0.4, mint="A"), _row(0.6, 0.4, mint="B")]
    closes = calibrate.closes_from(rows)
    assert len(closes) == 1
    assert closes[0]["mint"] == "B"


def test_close_without_pnl_is_dropped():
    rows = [{"action": "pumpfun.close", "target": "mint:A", "payload": {"score": 0.6}}]
    assert calibrate.closes_from(rows) == []


def test_non_close_actions_are_ignored():
    rows = [{"action": "pumpfun.buy", "target": "mint:A",
             "payload": {"score": 0.6, "pnl_sol": 1}}]
    assert calibrate.closes_from(rows) == []


def test_zero_pnl_is_a_loss():
    assert calibrate.closes_from([_row(0.9, 0.0)])[0]["win"] == 0.0


def test_timestamp_is_carried_for_the_chronological_split():
    closes = calibrate.closes_from([_row(0.5, 1.0, ts=123.0)])
    assert closes[0]["ts"] == 123.0


# -------------------------------------------------------------- bucketing

def test_bucket_labels_match_replay_py_buckets():
    assert calibrate.bucket_label(0.0) == "0.0-0.5"
    assert calibrate.bucket_label(0.55) == "0.5-0.6"
    assert calibrate.bucket_label(0.95) == "0.9-1.0"
    # replay.py's last bucket is (0.9, 1.01), so a perfect 1.0 belongs there
    # rather than falling off the end of the table.
    assert calibrate.bucket_label(1.0) == "0.9-1.0"
    assert calibrate.bucket_label(1.05) == "?"


def test_bucket_table_counts():
    closes = calibrate.closes_from([_row(0.65, 1.0), _row(0.66, -1.0), _row(0.95, -1.0)])
    table = {r["bucket"]: r for r in calibrate.bucket_table(closes)}
    assert table["0.6-0.7"]["n"] == 2 and table["0.6-0.7"]["wins"] == 1
    assert table["0.6-0.7"]["hit_rate"] == 0.5
    assert table["0.5-0.6"]["n"] == 0 and table["0.5-0.6"]["hit_rate"] is None


# --------------------------------------------------------------- refusals

def test_empty_input_measures_nothing():
    report = calibrate.build_report([])
    assert report["measured"] is False
    assert "no resolved outcomes" in report["verdict"]
    assert "base_rate" not in report
    assert report["top_measured_bucket"] is None
    assert report["emittable"] is False


def test_small_sample_is_refused_not_defaulted():
    """Below the floor there is no number -- specifically not 0.5, which reads
    as a mild positive and sits mid-ranking."""
    report = calibrate.build_report(calibrate.closes_from(_calibrated(n=10)))
    assert report["measured"] is False
    assert "unmeasured" in report["verdict"]
    assert report["top_measured_bucket"] is None


def test_unmeasured_report_emits_no_finding():
    store = FakeStore()
    report = calibrate.build_report(calibrate.closes_from(_calibrated(n=10)))
    assert calibrate.emit_finding(report, store=store) is None
    assert store.findings == []


def test_thin_top_bucket_emits_nothing():
    """Enough closes overall, but the top bucket is too thin to quote."""
    rows = calibrate.closes_from(_calibrated(n=400))
    rows = [c for c in rows if c["score"] < 0.8]
    rows.append({"mint": "L1", "score": 0.95, "pnl_sol": 1.0, "pnl_pct": 100.0,
                 "reason": "tp", "creator": "C", "ts": 999.0, "win": 1.0})
    report = calibrate.build_report(rows)
    assert report["measured"] is True
    assert report["top_measured_bucket"]["bucket"] != "0.9-1.0"


def test_held_out_split_needs_enough_closes_per_half():
    report = calibrate.build_report(calibrate.closes_from(_calibrated(n=32)))
    assert report["measured"] is True, "32 >= MIN_SAMPLES"
    assert report["held_out"]["available"] is False, "16 per half < MIN_SPLIT"
    assert "per half" in report["held_out"]["reason"]


# ------------------------------------------------------------ measurement

def test_calibrated_score_discriminates_and_generalises():
    report = calibrate.build_report(calibrate.closes_from(_calibrated()))
    assert report["measured"] is True
    assert report["discrimination_significant"] is True
    assert report["discrimination"] > 0
    assert report["held_out"]["available"] is True
    assert report["held_out"]["heldout_generalises"] is True
    assert report["verdict"] == "score discriminates and generalises out of sample"


def test_independent_score_is_reported_as_uninformative():
    """This is the finding that matters: a ranking device with no measured
    relationship to results."""
    report = calibrate.build_report(calibrate.closes_from(_independent()))
    assert report["measured"] is True
    assert report["discrimination_significant"] is False
    assert report["verdict"] == "no measurable discrimination"


def test_discrimination_is_checked_against_its_own_standard_error():
    report = calibrate.build_report(calibrate.closes_from(_independent()))
    assert report["discrimination_se"] > 0
    assert abs(report["discrimination"]) < 2 * report["discrimination_se"]


def test_split_is_chronological_not_random():
    """A random split lets later outcomes inform an 'earlier' fit, which is
    exactly the leak the held-out check exists to prevent."""
    rows = []
    ts = 0.0
    # Early half: the score is INVERTED (high score loses).
    for k in range(25):
        rows.append(_row(0.9, -1.0, mint=f"a{k}", ts=ts)); ts += 1
    for k in range(25):
        rows.append(_row(0.1, 1.0, mint=f"b{k}", ts=ts)); ts += 1
    # Late half: normal (high score wins). A chronological fit learned the
    # wrong rule and cannot profit here; a random fit could.
    for k in range(25):
        rows.append(_row(0.9, 1.0, mint=f"c{k}", ts=ts)); ts += 1
    for k in range(25):
        rows.append(_row(0.1, -1.0, mint=f"d{k}", ts=ts)); ts += 1

    held = calibrate.held_out_check(calibrate.closes_from(rows))
    assert held["available"] is True
    assert held["fit_n"] == 50 and held["eval_n"] == 50
    # The fit learned "low score wins", which is wrong for the evaluation half:
    # so it must NOT report generalisation. This is the check earning its keep.
    assert held["heldout_generalises"] is False


# --------------------------------------------------- verdict branch coverage

def test_every_verdict_branch_is_reachable():
    """The branches that cannot be produced naturally are tested directly --
    otherwise a dead branch looks like coverage."""
    cases = [
        (False, {"available": False, "reason": "x"}, "no measurable discrimination"),
        (True, {"available": False, "reason": "too few closes"},
         "score discriminates (held-out check unavailable)"),
        (True, {"available": True, "heldout_generalises": True},
         "score discriminates and generalises out of sample"),
        (True, {"available": True, "heldout_generalises": False},
         "score discriminates in-sample but not out of sample"),
    ]
    for discriminates, held_out, expected in cases:
        verdict, _ = calibrate._verdict(discriminates, held_out)
        assert verdict == expected, (discriminates, held_out, verdict)


def test_no_measurable_discrimination_beats_a_good_held_out_result():
    """Ordering matters: discrimination is the gate. A passing held-out check
    on a non-discriminating score must not be reported as success."""
    verdict, _ = calibrate._verdict(False, {"available": True, "heldout_generalises": True})
    assert verdict == "no measurable discrimination"


def test_in_sample_brier_comparison_is_gone():
    """It was guaranteed true by construction -- in-sample bucket rates cannot
    lose to a constant -- so it measured nothing and was removed. Guard the
    removal so it cannot come back as decoration."""
    report = calibrate.build_report(calibrate.closes_from(_calibrated()))
    assert "brier_binned" not in report
    assert "binning_beats_base_rate" not in report


# ---------------------------------------------------------------- findings

def test_finding_confidence_is_a_measured_rate_with_its_n():
    store = FakeStore()
    report = calibrate.build_report(calibrate.closes_from(_calibrated()),
                                    unverifiable_skips=123)
    finding = calibrate.emit_finding(report, store=store)
    assert finding is not None
    best = report["top_measured_bucket"]
    assert finding["confidence"] == best["hit_rate"], "confidence must BE the measurement"
    assert finding["payload"]["measured_not_asserted"] is True
    assert finding["payload"]["unverifiable_skips"] == 123
    assert f"n={best['n']}" in finding["title"]
    assert "123 skips" in finding["evidence"]


def test_overfitted_finding_says_do_not_gate_capital():
    store = FakeStore()
    report = calibrate.build_report(calibrate.closes_from(_classified_as_overfit()))
    assert report["verdict"] == "score discriminates in-sample but not out of sample"
    finding = calibrate.emit_finding(report, store=store)
    assert "overfitted" in finding["suggestion"]

def _classified_as_overfit():
    """The signal is real overall but the bucket rates do not transfer.

    Early half: the score is strongly informative (0.9 -> 23/25 wins,
    0.1 -> 2/25). Late half: the same buckets are flat (~50%), so rates fitted
    on the early half are actively wrong for the late half.

    Note what this construction had to do to exist: a FULL sign flip between
    periods cancels out in the pooled halves test and reports "no measurable
    discrimination". So this branch needs the score to keep a genuine overall
    ordering while its per-bucket rates stop transferring -- which is precisely
    the shape of a signal that worked and then stopped, and exactly the case a
    single in-sample number would have reported as a success.

    Built as TRACE rows and pushed through closes_from, so the test exercises
    the same path production uses -- a fixture shaped like the *output* would
    have hidden a mapping bug behind a passing assertion.
    """
    rows = []
    ts = 0.0
    for score, wins in ((0.9, 23), (0.1, 2)):          # early half: informative
        for k in range(25):
            rows.append(_row(score, 1.0 if k < wins else -1.0, mint=f"E{score}{k}", ts=ts))
            ts += 1
    for score, wins in ((0.9, 12), (0.1, 13)):         # late half: flat
        for k in range(25):
            rows.append(_row(score, 1.0 if k < wins else -1.0, mint=f"L{score}{k}", ts=ts))
            ts += 1
    return rows


def test_uninformative_finding_tells_you_to_stop_ranking_on_it():
    store = FakeStore()
    report = calibrate.build_report(calibrate.closes_from(_independent()))
    finding = calibrate.emit_finding(report, store=store)
    assert "Do NOT rank or gate" in finding["suggestion"]


def test_scope_is_stated_in_the_payload():
    store = FakeStore()
    report = calibrate.build_report(calibrate.closes_from(_calibrated()))
    finding = calibrate.emit_finding(report, store=store)
    assert "surviving buys only" in finding["payload"]["scope"]


def test_report_renders_without_error():
    text = calibrate.format_report(
        calibrate.build_report(calibrate.closes_from(_calibrated())))
    assert "VERDICT" in text and "0.9-1.0" in text


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
    print(f"calibrate: {'OK' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
