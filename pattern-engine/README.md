# pattern-engine

Raschke-hinted pattern trading system on Google ADK. Design + hint
traceability: [DESIGN.md](DESIGN.md). Sibling projects: `day-orchestrator`,
`deep-research`.

Two planes (the interview's rule: LLMs are components, never the predictor):

- **Trading plane** — `PatternMonitor` (ADK `BaseAgent`), zero LLM calls:
  registry of code-defined strategies → signals → `RiskManager` (even 1% risk,
  ATR units, process-vs-expectancy breakers) → `PaperGateway` → journal +
  MAE/MFE excursion labels.
- **Research plane** (Phase 3, approved next): LLM components via
  ds41/LiteLLM propose → verified engine backtests → rule-graded gate →
  registry promote/demote. `app/core/models.py` carries the proven
  `resolve_model` wiring.

## Run

```bash
uv sync
uv run python -m app.runtime once     # ingest EOD + quality gate + process newest bar (ADK Runner + sqlite+aiosqlite sessions)
uv run python -m app.runtime replay   # full-history paper replay + stats + excursion report
uv run python -m app.runtime research # regeneration loop: ds41 proposes, code sweeps + grades
uv run python -m app.runtime serve    # APScheduler daily EOD loop (no jobstore; re-derived at boot)
uv run pytest                         # 24 tests: engine lock, parity, gate, research loop
uv run lint-imports                   # layers: workflows > exec > strategies > core
```

## Verified (Phase 1+2)

- Engine port cross-checked **trade-for-trade** against the original
  `raschke-strategy` engine on the locked fixture (1-ATR variants, both).
- Parity lock: live monitor path (gateway state machine) produces
  byte-identical trades to the engine — the anti-divergence contract.
- Locked reference stats (fixture: SPY 1993-01…2026-11, repaired OHLC):
  B 3-ATR/10-bar: 71 trades PF 1.40 avgR +0.27; C 2.5-ATR trail: 58 trades
  PF 1.45 avgR +0.30. Combined book (both live): 73 trades PF 1.43.
- Excursion labels reproduce her diagnostic: winners' max MAE 0.97R ("should
  not go beyond this point"), losers' median MAE 1.53R, winners' median MFE
  3.0R (the 3-ATR target doing its job).
- DataQualityGate caught and repaired 22 real 1-ulp OHLC artifacts in the
  fresh yfinance pull; calendar check flags gross holes, not holidays.

## Verified (Phase 3: research loop, live on ds41)

- Round-trip on the LAN model (192.168.0.206:8000, `ds41`): reads registry
  stats, proposes one bounded JSON variant with rationale — e.g.
  "Tightening ADX and pullback depth slightly while raising the fixed
  target…" — code sweeps 33y and grades. The LLM never grades.
- Gate (fixed ex-ante): PF ≥ 1.25, avgR ≥ 0.10, maxDD ≤ 15R, n ≥ 30,
  ≥3 positive folds, best fold ≤ 80% of gross positive R. Recalibrated from
  the first median-fold rule after measuring the incumbent itself
  (fold avgR [0.11, 0.16, 0.09, −0.05, 1.04] — epoch-lumpy by nature); the
  policy targets one-epoch wonders, not uniform epochs.
- Promotion additionally requires non-domination: a gated variant with lower
  avg_r than a live sibling stays `candidate` (a worse variant must not
  steal trades — the monitor is first-signal-wins). Demonstrated live:
  ds41's variant passed the absolute gate (n=51, PF 1.27, avgR 0.194) and
  was held back as dominated by v1 (0.268).


## Phase 4: autonomy — the daemon

`serve` is the autonomous app: flock single-instance lock (a second daemon
parks, never double-trades), APScheduler with **no jobstore** (schedule
re-derived at boot; registry/journal DBs are the truth), jobs:

| job | schedule (UTC) | does |
|-----|----------------|-------|
| `eod_run` | Mon–Fri 22:15 | ingest + quality gate + process newest bar |
| `decay_run` | daily 23:30 | trailing-5y re-grade of live specs; 2 consecutive negative windows → retire |
| `research_run` | daily 12:00 | SPY loop + full HL basket, dual-window gate |
| `report_run` | daily 23:35 | markdown report → `var/reports/YYYY-MM-DD.md` |

Install as a launchd agent (paper only):

```bash
mkdir -p ~/Library/"Application Support"/pattern-engine/logs
cp ops/launchd/com.agp.pattern-engine.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.agp.pattern-engine.plist
# logs: ~/Library/Application Support/pattern-engine/logs/agent.log
```

The journal persists across restarts (idempotent append on
strategy_id+entry_ts), so the book survives daemon restarts and upgrades.
Verified end-to-end via `cycle`: EOD bar processed → decay (both incumbents
healthy: avgR +1.17/+0.97 trailing 5y) → live ds41 round (proposal n=53,
PF 1.24 — rejected 0.01 under the gate) → report written.

## Phase 5: Hyperliquid basket (in progress)

Execution target is Hyperliquid (mainnet read-only first, then gated live).
The market layer is exchange-native:

- `app/core/hyperliquid.py` — public `info` endpoints; universe (main dex +
  `xyz` builder dex); 8h-candle pagination (API caps 5000 candles/request;
  8h reaches ~3 years, 1h only ~208 days) aggregated to UTC days.
  Wishlist: BTC, ETH, SPX, `xyz:XYZ100`, `xyz:GOLD`, `xyz:SILVER`,
  `xyz:BRENTOIL` (held out until ≥ 300 days).
- `app/core/yahoo.py` — long **research** histories via Yahoo proxies
  (BTC-USD, ETH-USD, ^GSPC, GC=F, SI=F, CL=F-for-Brent; XYZ100 has no
  proxy and stays HL-only).
- **Dual-window gate** (`app/research/gate.py`): the full walk-forward gate
  runs on the long research window; promotion additionally requires the
  HL-native confirmation window (same params, n ≥ 10, avg_r > 0). Research
  evidence + instrument realism; neither alone promotes.
- Registry keys on `(market, timeframe, id)` — same variant id across
  markets is a different row, never an overwrite.
- `hl-ingest` / `hl-research` runtime modes; proposals may set
  `"timeframe": "8h"` to sweep HL-native intraday bars.
- `hl-shadow [address]` — shadow mode: paper-runs every live HL strategy on
  fresh bars, mirrors the real account via public read-only endpoints
  (`clearinghouseState`/`userFills`/`metaAndAssetCtxs` — address only, no
  key, no signing, no order path), reconciles the two books, and writes
  `var/shadow_last.json`. Runs daily at 00:05 UTC from the daemon (HL is
  24/7; the mark is the UTC day close) and appends each reconciliation to
  `var/shadow_history.jsonl`.

### Live order path (Step 2.2 — built, awaiting credentials)

`LiveGateway` subclasses the parity-locked `PaperGateway`; only the fill
boundary changes — every entry/exit routes through `guarded_send`:

1. **Kill switch** — `var/KILL` file wins over everything, survives state
   corruption, manual re-arm only (`runtime kill` trips + disarms; `arm`
   refuses while tripped).
2. **Armed flag** — durable in `var/live_state.json` (`runtime arm`).
3. **Confirmation gate** — the first N orders (config `confirm_first_n`,
   default 5) enqueue as inspectable intents instead of sending; `runtime
   approve <id>` executes exactly what was shown. Corrupt state fails safe
   (unarmed). Missing `HL_API_SECRET` is a hard stop — no paper fallback.

Orders go via the official SDK, signed with the API-wallet key acting on
the main account address; testnet selectable via config. `runtime preflight`
prints the full human checklist; `runtime live-once` is the gated
counterpart of `hl-shadow` (refuses unless armed and un-killed; a halt
stops every market). No real order has been sent yet — the key is not set.


## Layout

```
app/core      config, models(ds41/LiteLLM), market+quality, indicators,
              backtest engine, excursion labels, registry store
app/strategies  pullback_resumption (seed), seeds + signal-fn resolution
app/exec      PaperGateway (paper fills), HyperliquidGateway (read-only
              account mirror), LiveGateway (gated orders), guards (kill +
              confirmation), RiskManager (sizing, breakers)
app/workflows PatternMonitor (BaseAgent, shared live/replay path)
app/research VariantProposer (ds41 LlmAgent), SweepAndGrade (code),
              walk-forward gate, fence-tolerant JSON extractor
```

Live money: the gated order path is built (Step 2.2) but has never sent an
order — `HL_API_SECRET` is not set. Certification order: testnet first,
then mainnet tiny size behind the confirmation gate.
