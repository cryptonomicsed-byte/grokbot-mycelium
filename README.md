# grokbot-mycelium

Two integrations for [grokbot-pumpfun](https://github.com/zostaff/grokbot-pumpfun)
(MIT, 61★), both chosen because grokbot produces something the rest of this
ecosystem does not.

    grokbot tape  ->  Mycelium traces  ->  calibration  ->  measured finding
          |
          +------->  ares-control (`paper` category)  ->  Vantage can toggle it

## The one property that makes this worth wiring

grokbot's `close` record carries **the entry score next to the realised
outcome**:

```json
{"type":"close","mint":"...","score":0.81,"pnl_sol":0.42,"pnl_pct":31.0,
 "hold_seconds":812.4,"reason":"take_profit","creator":"...","ts":...,"mode":"dry_run"}
```

Nothing else in this stack has that. Mycelium's trace log records actions;
it holds no outcomes, which is *why* its miners assert confidence from
invented arithmetic:

```
min(0.95, 0.5 + 0.08 * count)             # recurring_workflow
min(0.95, 0.4 + rate)                     # anomaly
min(0.9,  0.5 + 0.15 * len(agents))       # cross_agent
min(0.97, 0.55 + 0.05 * count)            # opportunity
min(0.9,  0.5 + 0.08 * distinct_wallets)  # wallet_activity
min(0.92, 0.5 + 0.2 * len(shared))        # wallet_correlation
```

Those land in a column and get ordered by, so a fabricated number decides which
findings surface. Nothing in the substrate could falsify them. A labelled
outcome can.

This bridge does not touch those formulas. It lays a second, falsifiable
source alongside them: a miner that returns a **rate it measured**, with the
`n` attached, or returns nothing.

## Path 1 — feed Mycelium a labelled tape

```
tools/bridge_daemon.py
  1  tail logs/trades.jsonl by byte offset (idempotent, restart-safe)
  2  emit substrate traces: pumpfun.{intent,buy,skip,close}
  3  join close.score against close.outcome
  4  emit a finding whose confidence IS the measured win rate
```

Mapping, per record type:

| grokbot | substrate kind | action | outcome |
|---|---|---|---|
| `intent` | `decision` | `pumpfun.intent` | `info` |
| `buy` | `observation` | `pumpfun.buy` | `info` |
| `skip` | `observation` | `pumpfun.skip` | `info` |
| `close` | `observation` | `pumpfun.close` | `success` if `pnl_sol > 0` else `failure` |

Three deliberate choices:

- **Break-even is failure.** A trade that returns nothing after fees did not
  win; rounding it to the favourable side is how a win rate drifts.
- **Everything pre-outcome is `info`.** An intent, a buy and a skip are
  assertions. Only money is a result.
- **A missing score is `None`, never `0.0`.** Defaulting would file every
  unscored close into the bottom bucket and manufacture losers.

## Path 2 — ares-control / Vantage

The paper daemon is registered in `~/ares-control/service/daemons.py` under a
new `paper` category:

```
"ares-grokbot-paper.service": Category.PAPER,
```

`PAPER` is togglable without `approve_execution=true`, because grokbot's live
executor raises `NotImplementedError` (`src/executor.py:246,251`) — no key it
holds can reach a venue. It is *not* labelled `intel`: it is trade-shaped
research whose output is decisions, and calling it intel would hide that from
the next reader. **If grokbot's executor is ever implemented, that unit
becomes `EXECUTION` in the same commit** — otherwise the fleet's approval gate
is bypassed by a change in a different repository.

`deploy/ares-grokbot-paper.service` is the unit: no network, no wallet, no
trading key, `ProtectSystem=strict`, `CapabilityBoundingSet=` empty.

## The refusal — do not point this at Vantage's trading ingest

Verified in this tree, `Vantage/backend/routers/trading.py`:

```
2145:  conviction: 0-1 confidence (>0.7 auto-executes if wallet exists)
2171:  if conviction > 0.7 and direction_upper in ("BUY","LONG","BULLISH"):
2233:      INSERT INTO trading_orders (...)      <- a REAL order
```

and `Vantage/backend/config.py:138`:

```
PUMPFUN_SCAN_CONVICTION: float = 0.72   # >0.7 -> auto-order in ingest
```

grokbot scores 0–1. The obvious wiring — put the score in a field named
`conviction` — turns a research score into an order-placing bot, silently,
because the number already sits in the right domain. Nothing about the value
looks wrong; only its destination is.

Two refusals are therefore enforced in code, not in comments:

- `ceiling.conviction_for()` clamps strictly below the line by construction.
  Crossing it needs a separately-named opt-in that defaults off.
- `ceiling.assert_not_execution_target()` rejects the trading ingest path by
  name. grokbot keys on `mint`; that endpoint keys the resulting order on
  `symbol` (trading.py:2237), so a pump.fun ticker would create an order for a
  string nobody can fill.

An opt-in for the *correct* destination is available when grokbot has a real
tape: `/api/intel/signals/ingest` carries `mint`. Default off.

`tests/test_ceiling.py` reads Vantage's own source and fails if the 0.7 line
moves. A fact about another codebase that nothing checks is a comment.

## Commands

```bash
cd ~/grokbot-mycelium

python3 tests/test_bridge.py       # 15 tests, no network
python3 tests/test_calibrate.py    # 25 tests
python3 tests/test_ceiling.py      # 11 tests, 2 read Vantage's source

python3 tools/demo_end_to_end.py                  # plumbing check, SYNTHETIC tape
python3 tools/demo_end_to_end.py --tape ~/grokbot-pumpfun/logs/trades.jsonl

python3 tools/bridge_daemon.py --once             # one pass
python3 tools/bridge_daemon.py                    # under systemd

python3 -m gmbridge.calibrate --emit              # measure + persist
```

## What was removed, and why it matters

The first cut reported `brier_binned` — the Brier score using each bucket's
own measured hit rate — against the base rate. It looked like a calibration
test. It was decoration: **in-sample bucket rates minimise squared error within
each bucket, so binning on *any* partition cannot score worse than the global
constant.** The comparison was guaranteed true before the data arrived — the
same defect as `min(0.95, 0.5 + 0.08 * count)` in different clothes. It was
found by running the demo, not by a unit test, and replaced with a
chronological held-out split, which can actually fail.

`tests/test_calibrate.py::test_in_sample_brier_comparison_is_gone` guards the
removal so it cannot come back as decoration.

## What is measured, and what is not

Measured, from a real tape:

- win rate per score bucket, with `n`
- top-half vs bottom-half discrimination, tested against the standard error
  of the difference
- a chronological held-out check: bucket rates fitted on the earlier half,
  scored on the later half

**Not** measured, and stated in every report:

- **The funnel's own precision.** A skipped token's later price is never
  observed by grokbot, so only *buys* get outcomes. Every report prints the
  count of unverifiable skips. A calibration over surviving buys answers a
  narrower question than "is this funnel good", and quoting it as a funnel
  number would be wrong.
- **Anything about pump.fun.** The shipping test fixtures are synthetic and
  labelled as such. They prove the plumbing; only a real `logs/trades.jsonl`
  produces a market number. A uniform result from a fixture is evidence of
  nothing.

## A naming hazard this module creates

The substrate's `findings.confidence` is a `REAL` column every other miner
fills with an invented heuristic and consumers order by. This miner puts a
**measured win rate** in it — a different kind of object in the same column.
Within this miner the column is comparable (every row is a rate with an `n`).
Across miners it is not: ordering a measured rate against
`min(0.95, 0.5 + 0.08*count)` is meaningless in both directions. Fixing that
means a separate column or a miner-scoped ordering — a substrate change, not
something to do quietly from inside a bridge. Flagged, not silently patched.

## Running it for real

Requires a grokbot dry-run with an x.ai key for its three Grok stages
(`api.x.ai` — xAI's Grok, **not** Groq; different vendor, different key). No
other credential is needed: the bridge reads a file and writes traces.

```bash
cd ~/grokbot-pumpfun
pip install -r requirements.txt
cp config.example.yaml config.yaml     # set your x.ai key; dry-run is the default
python -m src.cli run                  # writes logs/trades.jsonl

cd ~/grokbot-mycelium
python3 tools/bridge_daemon.py --once
```

Nothing here trades. grokbot's live path raises `NotImplementedError` on
purpose, and this bridge never asks it to.
