# MockingBot Codex Handoff

Last updated: 2026-07-31

## Resume Here

Workspace:

`C:\Users\user\Desktop\MockingBot_Revamp_CodeX`

Repository:

`https://github.com/OllieBabaSan/MockingBot.git`

Branch: `main`

Latest code commit before this handoff: see `git log -1` (rolling live breaker update).

The source working tree was clean before this documentation update.

The main Paper and Live bots currently run together. Paper is the canonical
signal/roster source; Live imports Paper's completed canonical events in source
order and applies its own capital, slot, execution, and exchange constraints.
This is a diagnostic training arrangement. Live must eventually be able to
select and score independently, but do not remove the current dependency until
parity confidence is materially stronger.

## Current Runtime State

Last dashboard snapshot on 2026-07-29:

### Paper

- Dashboard: `http://127.0.0.1:8765`
- Latest verified value after recovery: about `$11,098.34`
- Fixed starting equity: `$10,000`
- Persistent equity high-water: `$12,119.35`
- Drawdown from high-water: about `8.42%`
- Open tokens: `5`

### Live

- Dashboard: `http://127.0.0.1:8766`
- Hyperliquid account: `0x2f7Fd044C323152488105dD01B93107b84f92C98`
- API/agent wallet: `0x243954546255a5e18b19d9f7e281131a97fce44a`
- Contributed starting equity: `$802.20`
- Breaker high-water: about `$823.47`
- Current equity: about `$640.70`
- The persistent high-water drawdown remains diagnostic only.
- Rolling 24-hour warning/breaker: `15%` / `25%`; a trip blocks new entries
  for 24 hours and does not force liquidation.
- Rolling 7-day warning/breaker: `35%` / `50%`; a trip blocks new entries for
  seven days and does not force liquidation.
- Live equity samples are persisted in `live_equity_history`. An expired marker
  clears automatically when its rolling breach is gone; a continuing breach
  retrips the applicable window.
- Open tokens: `4`
- Quarantined coins: `0`
- Unresolved execution intents: `0`

Current Live positions at handoff:

| Coin | Side | Margin | Approx. open PnL |
|---|---:|---:|---:|
| BTC | Short | about `$131` | near flat |
| HYPE | Long | about `$136` | losing |
| NEAR | Long | about `$136` | profitable |
| XRP | Short | about `$68` | profitable |

The all-time high-water drawdown does not itself stop Live. Do not reset risk
history or force-close positions without explicit user authorization.

Always verify current processes and dashboards rather than trusting these
snapshot values.

## Credentials and Security

The active main credentials file is:

`MockingBot_Main_Live_Test.Hyper.txt`

It contains secrets. Never print, quote, commit, or place its API key in logs or
handoffs. Credential files, runtime databases, logs, backups, and caches must
remain uncommitted.

The main account uses Hyperliquid unified-account trading. The account wallet
and API/agent wallet are different by design.

Elite credentials are separate and must never be substituted into the main
Live bot.

## Architecture and Safety Rules

- Paper start: `$10,000`
- Paper slots: `10`
- Live slots: `6`
- Both Paper and Live use an explicit `20%` per-token margin cap based on total
  local book allocation basis, not remaining cash.
- The old two-slice/slot-derived cap was removed because it produced a `33.3%`
  Live token cap with six slots.
- Existing over-cap positions were grandfathered during rollout but could not
  receive additions.
- Per-wallet margin cap: `35%` of total book basis.
- Leverage: tier-controlled, currently capped at `5x`; normal active positions
  have generally used `3x`.
- Live drawdown warning: `15%`.
- Live hard breaker: `25%` from persistent high-water. Breaker causes wind-down
  and blocks new risk; it is not a reason to delete state or abandon the bot.
- Opposite-side replacement is allowed only when the incoming wallet has a
  strictly superior tier. A ranked reversal closes the incumbent side first.
- Confirmed exchange fills are recorded even below normal minimum allocation.
  Minimum allocation governs submission, never recognition of a real fill.
- Unowned/mismatched coins are quarantined individually; the rest of the book
  continues.
- Duplicate main instances are blocked by the instance lock.
- Dormant wallet pause functionality was deliberately removed. Wallet quality
  is controlled through scoring, demotion, and tier allocation.

## Recent Important Incident

Live accumulated `$239.26` of PUMP short margin under the old slot-derived
token cap. PUMP moved sharply upward and caused most of the account's drawdown.
The user chose to wait rather than manually trim it.

The short later closed because of wallet action, not the breaker:

- Elite wallet `0x17c3c8...a868` exited its short.
- The same wallet opened a PUMP long with Elite score about `79.6`.
- Its superior-ranked reversal closed the remaining short allocations.
- The three short slices realized roughly `-$110.85`.
- A smaller PUMP long was then opened within the corrected 20% cap.

The wallet's Elite status was based on a relatively small completed sample.
After the active-risk scoring rollout it dynamically demoted to Core `64.4`.

## Scoring and Parity

- Paper wallet history was imported into Live so Live did not start scoring at
  zero.
- Scoring uses normalized return rather than raw copied dollars, preventing
  Paper's larger allocations from dominating Live outcomes.
- Live consumes completed Paper events in canonical source order with Paper's
  observed signal prices.
- The parity monitor compares shared inputs and decisions. Expected differences
  include slot/cash constraints, exchange minimums, fill prices, and Live-only
  execution safety.
- A Live trade does not have to appear as an open Paper position. The goal is
  shared rules and explainable approximation, not identical books.
- Wallet realized performance now uses the most recent 20 completed exits with
  a 10-trade half-life. Individual returns are clipped to `-5%/+5%`, and the
  realized component is capped at `+15/-20` so old gains and single outliers do
  not dominate indefinitely.
- Scoring now includes a dynamic active-risk overlay based on the canonical
  source wallet's current Hyperliquid margin and unrealized PnL. Material open
  drawdown and the breadth of materially losing positions reduce the score.
  An active penalty of `15` or more caps the wallet at Candidate. Recovery
  automatically reduces or removes the cap.
- Current wallet scores refresh after every canonical scan. An audit row is
  appended only when the tier changes or the score moves by at least 0.5,
  keeping dashboard tiers current without excessive database growth.
- Wallet `0x17c3c8...a868` changed from Elite `80.5` to Core `64.4` after the
  rollout. Its existing allocations correctly retain their historical Elite
  entry tier while the dashboard's Current tier shows Core.
- Paper midpoint versus Live exchange-fill price differences are expected
  environment variance and should not be treated as decision defects.
- After engine code changes, restart both Paper and Live so their code
  fingerprints and comparison epoch remain aligned. The user explicitly
  authorized this as routine maintenance.

## Post-Restart CASHCAT Intent Incident

During canonical catch-up after the Windows restart, Live prepared a
`CASHCAT SHORT` entry intent. Hyperliquid rejected the leverage update because
cross margin is not allowed for that asset. No order was submitted and no fill
occurred, but the early rejection path incorrectly left the durable intent in
`PREPARED`, causing the Live loop to restart repeatedly.

Commit `2d74b8c` now persists deterministic zero-fill leverage rejections as
`FAILED` and consumes them as skipped signals during recovery. It does not
retry with isolated margin, fabricate a fill, or alter existing positions.
Targeted `47` execution tests and the full `116`-test suite passed.

## Dashboard State

- Paper and Live dashboards are separate and mobile-oriented.
- Header shows only the last updated local date/time.
- Timezone is selectable and times display in a 12-hour clock.
- Open Positions is the persistent first data panel.
- Recent Closes and Execution Confirmations contain compact trade information.
- Wallet tier is shown with positions and execution confirmations.
- Token Risk Alerts:
  - show only the last 24 hours,
  - are below actionable warning panels,
  - are collapsed by default.
- Quarantined Coins and other warning panels are below the primary trading data.
- Paper Starting Equity remains fixed at `$10,000`.
- Paper Drawdown now uses a persistent high-water stored in
  `paper_equity_high_water`; it does not incorrectly show 0% merely because the
  account remains above `$10,000`.
- The dashboard latest-wallet-score query was optimized from more than 60
  seconds to roughly 0.5 seconds.

## Recent Commits

- `2d74b8c Resolve terminal leverage rejection intents`
- `9877f96 Refresh dynamic wallet scores after scans`
- `a40c1d5 Make wallet tiers respond to active losses`
- `74767eb Track paper high water and optimize dashboard`
- `31edc14 Deemphasize token risk alerts`
- `a03a5b0 Simplify dashboard status header`
- `d9c2e8c Cap token exposure at twenty percent`
- `c7c7ea0 Keep recovery events out of wallet scoring`
- `31e8b5a Track parity price-source classification fix`
- `353967d Show contributed live starting equity`
- `51b1105 Handle unified account capital credits`

Full test status at the latest change: `116` tests passed.

## Windows Restart and Hummingbot Test Handoff

The Windows restart completed. WSL 2 is installed, and Docker Desktop `4.84.0`
was verified healthy with Docker Engine `29.6.2`. Only Docker's managed
`docker-desktop` WSL distribution is present; no separate Ubuntu distribution
is currently installed.

Hummingbot/Hyperliquid testing is a separate evaluation:

- Docker Desktop `4.84.0` is installed.
- Hummingbot source and the initial paper setup are under:
  `C:\Users\user\Documents\Codex\2026-07-29\i-d\outputs`
- Hyperliquid's public API was verified without credentials. `HYPE-USDC` market
  data is working.
- Do not continue with the spot-only `_paper_trade` arrangement as the final
  evaluation.
- The target is realistic perpetual testing covering leverage, hourly funding,
  margin, and liquidation.
- Use Hummingbot's unmodified `perpetual_market_making` strategy with the
  `hyperliquid_perpetual_testnet` connector.
- Begin conservatively at `2x` leverage with a dedicated disposable testnet
  wallet and faucet funds.
- Never request, print, transmit, or store the wallet private key in chat. The
  user must enter it locally into Hummingbot.
- Compare testnet results against mainnet public prices, order books, and
  funding because testnet liquidity may not represent mainnet.
- WSL installation attempts from the non-administrator session failed.

Post-restart Hummingbot preparation is complete:

- Docker Compose validates, and `hummingbot/hummingbot:latest` is downloaded.
- Static inspection of that exact image confirms
  `hyperliquid_perpetual_testnet` and `perpetual_market_making` are present.
- The old spot `_paper_trade` strategy was removed.
- The replacement strategy is
  `hyperliquid_hype_usd_perpetual_testnet.yml`: `HYPE-USD`, one-way mode,
  `2x` leverage, one level per side, `0.25 HYPE`, `0.50%` spreads, `0.75%`
  profit-taking, and a `3%` stop loss.
- `start-testnet.ps1` refuses `_paper_trade`, refuses a mainnet connector file,
  and requires the testnet derivative.
- No connector credential file exists and no private key was requested,
  printed, or stored.
- Public comparison at validation time:
  mainnet mark about `$53.65`, bid/ask about `$53.642/$53.645`, funding
  `0.0000125`; testnet mark about `$73.98`, bid/ask about
  `$73.983/$75.556`, funding `0.04`. Testnet is materially distorted.
- No Hummingbot container is running. The user must connect the disposable
  faucet-funded testnet wallet interactively before validation or start.

Required user action before restart:

```powershell
wsl --install
```

Run that command from PowerShell as Administrator, then restart Windows.

Post-restart checklist:

1. Completed: verify `wsl --status`.
2. Completed: start Docker Desktop and verify `docker info`.
3. Completed: replace spot paper with perpetual testnet configuration.
4. Completed: confirm no mainnet connector credentials are present.
5. Required user action: run `start-testnet.ps1`.
6. In Hummingbot, run `connect hyperliquid_perpetual_testnet` and enter the
   disposable testnet wallet details locally.
7. Confirm faucet balance, `HYPE-USD`, one-way mode, and `2x` leverage.
8. Only then run `start`.

## Start and Verification Procedure

Check processes:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -match '^python' -and $_.CommandLine -match 'MockingBot' } |
  Select-Object ProcessId, Name, CommandLine
```

Run tests:

```powershell
python -m unittest discover -s tests -q
python -m py_compile .\MockingBot.py .\MockingBot_Dashboard.py .\MockingBot_Elite.py
git diff --check
```

Before every Live start, with the Live engine stopped:

```powershell
python .\MockingBot.py preflight-live
```

Preflight is read-only and must report:

- configuration pass,
- no duplicate Live instance,
- circuit breaker clear,
- correct account/agent relationship,
- viable six-slot sizing,
- synchronized local/exchange positions,
- `RESULT: PASS - no orders submitted`.

Start Paper:

```powershell
python .\MockingBot.py
```

Start Live only through the gated launcher:

```powershell
.\Start-MockingBot_Live.ps1
```

Start dashboards:

```powershell
.\Start-MockingBot_Paper_Dashboard.ps1
.\Start-MockingBot_Live_Dashboard.ps1
```

Check recent logs:

```powershell
Get-Content .\MockingBot_Data\mockingbot_live.log -Tail 80
Get-Content .\MockingBot_Main_Live_Test_Data\mockingbot_live.log -Tail 100
```

Check dashboard APIs:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/status
Invoke-RestMethod http://127.0.0.1:8766/api/status
```

## Normal Restart Discipline

For parity-relevant engine changes:

1. Run targeted and full tests.
2. Commit the change.
3. Stop both Paper and Live engines.
4. Run `preflight-live`.
5. Start Paper and Live.
6. Restart both dashboards if dashboard code changed.
7. Verify both process command lines.
8. Verify both dashboard APIs.
9. Check logs for current cycles, synchronization, quarantine, unresolved
   intents, breaker state, and code/parity noise.

Do not restart Elite merely because main Paper/Live code changed unless the
change affects Elite or it is no longer running as intended.

## Deferred Work and Watch Items

- Live remains dependent on Paper as its canonical event/roster source. Over the
  next month, identify and fix Live scoring/selection defects so it can
  eventually run independently.
- Monitor whether the scoring engine demotes small-sample wallets quickly enough
  after performance deterioration.
- Continue watching the current Live drawdown and PUMP long. Do not infer a
  breaker event from a normal trader exit or ranked reversal; trace execution
  logs and `execution_audit`.
- Continue reviewing Paper/Live parity, distinguishing explainable book-state
  divergence from true logic divergence.
- Paper records midpoint estimates while Live records actual fills. This is
  acceptable, but any remaining false parity alerts around price-source reasons
  should be refined during a maintenance window.
- Cloud deployment/manual intervention design notes are still preliminary.
  The future cloud checklist must cover remote dashboard access, persistent
  storage, secrets, service supervision, alerts, backups, and an authenticated
  remote command path for manual trade rejection/closure.
- TrendBot has a separate handoff:
  `C:\Users\user\Desktop\trend-bot\TRENDBOT_HANDOFF.md`

## User Preferences and Operating Intent

- Keep the bot as hands-off as practical.
- Do not shut down the whole book for a single coin mismatch.
- Do not force-close positions merely because they are temporarily losing.
- Preserve strong-wallet autonomy while constraining concentration risk.
- Treat a 25% loss inside 24 hours as catastrophic and a 50% loss inside seven
  days as a viability-level event. A 25% weekly loss alone must not stop Live.
- Maintain Paper/Live code parity automatically after repairs without asking
  separately each time.
- Prefer monitoring evidence over premature parameter changes; current Live
  constraints should generally run for 1–2 weeks before relaxation.
