# Experimental strategy: release gate

This branch is NOT approved for unattended real-money deployment.

## Verified offline (2026-09-17)

- 158 unit tests pass with placeholder configuration, without exchange orders.
- Initial stop is included in the entry request, then set explicitly after entry.
- Planned price-to-stop risk has no former 25% overshoot allowance.
- Invalid signals do not change leverage or trigger failure liquidation.
- Existing exchange positions block new entries in the same symbol.
- The updater refuses a checkout whose branch differs from its update target.

## Remaining release blockers

- Planned stop loss is not a maximum loss guarantee: fees, funding, slippage,
  gaps and liquidation must be included in sizing and deployment checks.
- Confirm actual fill size, partial IOC fills and liquidation distance before
  declaring an entry fully protected. Leverage-setting failures need verification.
- Failure recovery must confirm closure and retain unresolved positions rather
  than dropping them from tracking. Emergency closure also needs confirmation.
- Stop-movement failures need durable retries, including after restart.
- Reconcile closed PnL using paginated exchange executions, commissions and
  funding; eliminate duplicate close records before strategy comparison.
- Compare strategies on chronological held-out signals per channel, accounting
  for entry delay, executable prices and ambiguous within-candle stop/TP order.
- Perform demo fault-injection tests (disconnect, restart, API timeout, partial
  fill) and verify the actual second-PC runtime/configuration before release.

Do not select a strategy by win rate alone. Compare net expectancy, drawdown,
tail losses and sensitivity to execution costs. No strategy guarantees profit.
Production configuration, credentials and private trading history stay outside Git.
