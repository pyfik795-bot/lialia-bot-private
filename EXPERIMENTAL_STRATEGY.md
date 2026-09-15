# Experimental profit strategy v1

This branch is intentionally separate from `main`. It must be tested in shadow
or demo mode before any live deployment.

## Rules

1. Skip a signal when the market is outside its entry zone.
2. Skip a signal when TP1 or any later target has already been passed.
3. Size each position so the initial stop risks 1% of current account equity.
   Channel leverage remains 20x for GG Shot and 10x for Fat Pig, but leverage
   no longer defines the dollar risk. Margin is capped at 25% of equity, and a
   signal is rejected when the exchange minimum quantity would exceed the risk
   budget by more than the rounding allowance.
4. After TP1, a proposed 6% buffer is applied only if it improves the existing
   stop. The bot never widens risk after entry.
5. After TP2, move the remainder to exact break-even. TP3+ do not tighten the
   stop further yet.
6. Keep the existing take-profit splits so this experiment changes one family
   of variables at a time.

## What “best” means

No strategy can guarantee maximum profit. Choose a winner only after at least
30-50 clean signals per channel on the same code version. Compare net PnL after
fees, maximum drawdown, profit factor, average loss, and results with the best
and worst trade removed.
