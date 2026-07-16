# Deferred Adjustments

These are intentionally deferred while the paper test is running. Do not interrupt
the bot for these unless a critical issue appears.

## Minor Cleanup / Clarity

- Make the cash reserve behavior explicit with a setting such as
  `MIN_CASH_RESERVE_PCT=0.10`.
- Rename `paper rejected` skip reasons into more specific reasons where possible:
  `cash reserve protected`, `position cap`, `coin already held`, or
  `minimum order notional`.
- Keep preserving roughly 10% cash reserve before opening additional paper
  positions. Current observed cash was about 12% of paper value, so rejecting
  another full slot was risk-rational.
- Ensure source wallets for open paper positions remain monitored even if they
  rotate out of the active roster. The effective scan list should include
  `roster + source_wallets(open paper positions)` so exits cannot be orphaned.
  Direct check on 2026-07-01 showed current source wallets still held their
  tracked positions, but this should be fixed before Scoring Engine/allocation changes.

## API Pacing

- Current API failures are within acceptable range for paper testing, mostly
  isolated `429` wallet-metrics rate limits during roster refresh.
- Likely cause is too many sequential wallet-metrics calls close together while
  evaluating leaderboard candidates, not necessarily true parallel calls.
- In a future maintenance pass, make roster refresh gentler:
  increase `WALLET_POLL_DELAY`, add retry/backoff for `429`, cache wallet
  metrics during refresh, reduce candidate evaluation depth once enough strong
  wallets are found, and avoid refreshing the roster more often than needed.
- Continue watching for clustered `429`s, repeated refresh failures, stale price
  data, missed scans, or log gaps. Isolated `429`s are not currently critical.
- Future scan improvement: consider staggered wallet scanning, where each cycle
  checks a slice of the followed roster instead of scanning every wallet at
  once. This could support faster effective polling, such as 15-second cadence,
  without hammering the API as the roster grows.

## Wallet Pause Decision

- Wallet pausing was retired before live launch. Scoring Engine demotion and
  reduced Candidate allocation are the deliberate response to deteriorating
  wallet performance.
- Do not reintroduce wallet pausing or stop-loss behavior without new evidence
  and a separately reviewed design.

## Allocation Follow-Up

- Simulate a live account as closely as practical. Do not preserve a hard 10%
  cash-reserve rule as a design requirement unless future live-style testing
  proves it useful. Treat idle cash as dry powder/risk buffer, not as cash
  needed to "cover" losses in the paper ledger.

## Scoring Engine Design

- Working name for wallet quality mechanism: `Scoring Engine`.
- Scoring Engine should use a simple two-layer model:
  long-term trust tier plus current/recent form.
- Replace earlier highest-performer language of `Core` with `Elite`.
- Proposed Scoring Engine categories:
  `Elite`, `Core`, `Candidate`, and `Bench`.
- `Elite`: top allocation class, based on highest combined historic PnL,
  recent PnL/form, win quality, and loss avoidance. Award the top 5 wallet
  spots to Elite when data supports it.
- `Core`: next 10 strongest wallets that do not quite earn Elite status.
- `Candidate`: solid wallets with acceptable PnL and loss prevention, but
  currently outperformed by Elite and Core.
- `Bench`: wallets demoted from Candidate/Core/Elite due to underperformance,
  repeated losses, or poor copied results. Bench should normally mean no new
  allocation until recovery evidence appears, not permanent removal.
- Scoring Engine's primary goal is to optimize for winners, not merely punish losers.
  Reward weighting should be the first design priority.
- A high-performing wallet that trades infrequently can send the strongest
  signal. If it only takes high-conviction trades and wins most of the time,
  its signals may deserve the highest allocation when they appear.
- A frequent trader with a high win rate and controlled losses is preferable to
  a more frequent trader whose PnL comes from many wins outweighing a higher
  number of losses.
- For a smaller account, prioritize selectivity and loss avoidance over chasing
  the largest possible upside.
- Score weighting should favor historic PnL and recent form, but avoid relying
  too heavily on recent PnL alone. Weight win quality, win count, loss count,
  average loss size, and loss prevention heavily, both historically and recently.
- Scoring Engine v1 copied-trade weighting priority, based on live paper data so far:
  1. sample-adjusted realized copied-trade PnL;
  2. win rate and average exit quality;
  3. loss control, including worst loss, average loss, and repeated losses;
  4. recent trend/form, especially repeated negative exits;
  5. churn/slot-consumption penalty only when expectancy is weak;
  6. raw total PnL as useful but not dominant, because one large win can distort
     early samples;
  7. open/unrealized PnL as low-authority context until positions close.
- Do not crown a wallet `Elite` from copied-trade data on raw PnL alone. Apply
  a sample-size discount so small but swingy samples remain Candidate/Core until
  they prove consistency.
- High-frequency wallets should not be punished merely for activity. Penalize
  churn only when it consumes slots without producing clean positive expectancy.
- Parameter goal for the mature system: only Elite and Core wallets should
  normally generate follow-worthy signals. There should ideally be more Core
  wallets than available capital, so Scoring Engine is choosing among good options.
- Candidate wallets are primarily watch-and-see wallets. They should earn their
  way into Core by improved performance rather than receiving meaningful
  allocation by default.
- Principle: long-term tier sets maximum trust and allocation ceiling; recent
  form adjusts allocation within that trust band.
- Scoring Engine v1 should begin in shadow mode:
  calculate scores/states, keep a ledger of recommendations and outcomes, but
  do not control trading until the signal proves useful.
- Shadow mode should write its own SQLite ledger, not just log text. Suggested
  tables: `legacy marshal_wallet_scores`, `legacy marshal_signal_journal`, and
  `legacy marshal_shadow_positions`.
- `legacy marshal_wallet_scores` should record timestamp, wallet, tier
  (`Elite`/`Core`/`Candidate`/`Bench`), total score, realized PnL component,
  win-rate component, recent-form component, churn penalty, drawdown/loss
  penalty, and a short explanation string.
- `legacy marshal_signal_journal` should record timestamp, linked signal id, wallet,
  coin, side, actual bot action, actual skip reason, Scoring Engine tier/score at
  signal time, Scoring Engine recommendation, would-execute yes/no, and reason.
- `legacy marshal_shadow_positions` should track hypothetical entries Scoring Engine liked
  but the bot skipped, then close them when the source wallet exits so health
  checks can compare actual PnL against Scoring Engine hypothetical PnL.
- Scoring Engine health checks should compare actual bot PnL vs Scoring Engine-preferred
  PnL, trades Scoring Engine would have avoided, PnL by tier, skipped signals Scoring Engine
  liked, and whether Scoring Engine is reducing losses or merely reducing activity.
- Preserve nuance for tuning: slower high-conviction wallets vs rapid churn,
  recent-form weight vs noise chasing, high win rate vs total PnL, tier signal
  quality, and whether slot-cap skips are blocking high-quality trades.
- Shadow reports should also attribute open/unrealized drag by source wallet,
  coin, position age, and side. Keep unrealized PnL low-authority for scoring
  until closed, but expose persistent open drawdown because realized gains can
  hide a weak current book.
- Consider Scoring Engine design-change implementation once the bot reaches about 20
  executed exits, assuming no critical issue requires action sooner.
- The first implementation should be enough to test whether Scoring Engine improves on
  a simple "follow the top wallets" design. Favor explainable allocation/ranking
  and reward weighting over complex enforcement.
- Keep the Scoring Engine explainable. Each flag should be reducible to a short reason:
  recent realized PnL, loss count in last N exits, average loss size, largest
  recent loss, same-direction loss cluster, copied latency, or open drawdown.
- Avoid destabilizing complexity. No external market-regime API unless later
  evidence shows it is necessary and reliable.

## Testing Priority

- Prefer uninterrupted test data over minor cleanup.
- Batch non-critical changes into a later maintenance pass.
- Interrupt only for critical issues: crashes, data corruption, repeated API
  failure loops, live-trading risk, or clearly wrong PnL accounting.

## Future Dashboard

- Create a mobile-optimized dashboard for operating a profitable copy-trading bot.
- Include paper equity curve, open positions, realized and unrealized PnL,
  recent signals, skipped signal reasons, wallet roster health,
  API failures, cash reserve, and exposure.
- Regular checkups should include the operating-risk cluster:
  cash reserve percentage, open position count, open cost basis, notional
  exposure, drawdown percentage, unrealized PnL, and skipped entries due to
  reserve/cap constraints.
- Regular analysis should also include:
  signal execution rate, entry/exit counts, skip reasons by count and trend,
  per-wallet contribution, per-wallet skipped signals, per-coin exposure,
  concentration by wallet and coin, realized win/loss count, average realized
  exit PnL, largest realized loss, largest open unrealized loss, API failure
  rate, time since last signal, time since last successful scan, and current
  roster size.
- Every fifth checkup should include a direct activity verification pass:
  inspect the latest monitor log lines, confirm the Python process is alive,
  query the SQLite database directly, compare latest log paper value against
  computed database value, and verify recent timestamps are advancing.
- Keep the dashboard focused on this bot's performance and operational safety.
  Omit side-by-side comparison fields unless explicitly requested later.

## Naming Cleanup

- Project name is `MockingBot`.
- Next maintenance/update pass should remove references to `legacy project names`.
- Remove suffixes and transitional names such as `` and `` from
  user-facing project/file references where practical.
- Discontinue the name `Scoring Engine`.
- Rename user-facing `Scoring Engine` references to `Scoring Engine`.
- Treat this as a cleanup/refactor pass, not an emergency live-run change.

## Later-Stage Position Replacement

- Position replacement means closing or reducing a weaker existing paper position
  to free capital/slot space for a stronger new Scoring Engine signal.
- The design theory is sound: it could prevent weak/default Candidate positions
  from occupying scarce slots when a much stronger Core/Elite signal appears.
- Risk is real: this touches exit/accounting logic, can increase churn, and can
  close positions just before they recover.
- Do not prioritize this in the near-term Scoring Engine build.
- First observe how solid/profitable the bot is with scoring, Bench blocking,
  tier sizing, Candidate moderation, and slot priority.
- Revisit only after the simpler Scoring Engine structure has proven stable and
  profitable over enough sessions.

## Future Position Slice Refinements

- Current opposite-side behavior should remain conservative for now: if the bot
  holds a coin in one direction, a new opposite-side signal is skipped.
- Possible later improvement: close or reduce the existing position only when the
  opposite-side signal comes from an equal-or-higher-ranked wallet/signal. Do
  not let a Candidate nullify a Core/Elite trade.
- If two strong traders disagree on direction, the safest response may be to
  close or reduce exposure rather than reverse. Uncertainty can be more
  important than chasing potential gains.
- Current same-wallet same-coin/same-side duplicate behavior is blocked with
  `wallet slice already held`.
- Revisit this later. A trader may open a small test position, then add more
  after confirmation. The bot should not permanently punish that strategy.
- Future improvement: allow same-wallet add-to-position as an additional
  confirmation slice when the wallet remains Scoring Engine eligible.
- Keep caps strict: still limit total per-coin exposure and total slices, but
  consider an exception for same-wallet confirmation adds if data shows it is
  useful.

## Paper Reset Procedure

- On the next reset/update pass, reset paper account to `$10,000` cash and
  `$0` realized PnL.
- Also reset the stored session baseline/memory to `$10,000` so the drawdown
  (`dd`) stat starts from the fresh baseline instead of a stale pre-reset value.
- This should prevent false drawdown readings such as positive paper value above
  `$10,000` still showing `dd`.
