# Share-balanced session (maker only)

This mode is not the legacy equal-gross-USD helper.

It sells an explicit Up/Down allocation toward equal remaining shares after fills. Every sell is post-only. There is no taker, market, IOC, or crossing retry, including when the +/-2 USD band is reached, the session is ending, or the book is stale. If a maker quote does not fit the tick, minimum size, and risk cap, the bot skips or halts new risk and keeps the residual inventory. That inventory can remain until resolution.

The settlement floor is gross proceeds plus the smaller remaining share balance, minus the declared cost basis and fees. Unpaid rebates are excluded. A bid-walk liquidation number and a midpoint mark are printed separately and are not orders. The -2 USD figure is a pre-trade stress guard, not a filled stop. The +2 USD figure uses that floor, not a second copy of the inventory and not a rebate.

Official crypto fee text used here: makers pay 0; taker fee is `C * 0.07 * p * (1-p)`; maker rebates are a daily pool share, not fill-time cash. Official order constraint used here is `min_order_size` in shares plus the tick, from the book payload. Five shares at 0.50 can move the floor by 2.50 USD, so a 2 USD budget correctly places nothing.

No live sender is implemented for this mode. Paper replay does not assume queue position: a print at our price does not fill us; a print strictly through our price can.
