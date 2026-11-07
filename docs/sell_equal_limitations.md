# Sell-equal bot: limitations and upstream drift

This note is the English review summary. The operator guide is `README.vi.md`.

## What the example does

A real SELL fill of 10 Up shares at 0.60 has gross proceeds of 6 USD. The bot
may post one Down SELL at the configured 0.40, size 15, because 15 * 0.40 is
also 6 USD. That is equal gross sale value, not equal shares.

Pending hedge value and cumulative sale value are **gross** (`price * shares`).
`fee_rate_bps` is ignored. The verified BTC 5-minute market reported maker and
taker base fees of 1000 and `fd.r` of 0.07 on 2026-11-07. Those numbers are not
applied. Nothing here is net of fees, and nothing is a profit.

Equal gross value is not inventory-neutral: the example sells 10 Up and 15 Down.
The second order is post-only and may never fill. There is no guarantee that
the two prices sum to more than 1, and this is not a bubblebust replica.

## Difference from upstream Bands

Bands, still in this repository, refreshes both bids and asks around a midpoint,
treats a buy of token A as the liquidity equivalent of a sell of token B at the
complement price, and sizes bands in shares. Its shutdown path calls
`cancel_all`, which the legacy client documents as cancelling every open order
on the account. The legacy app also max-approves collateral and conditional
tokens on startup, and if the midpoint request fails it substitutes a random
price.

The sell-equal bot does none of that. It posts SELL orders only, reacts to
actual filled shares, sizes the opposite SELL from the remaining gross-USD
deficit, and cancels only order ids it stored. A fill on the lagging side
reduces the deficit. It does not start a reverse hedge chain. Excess hedge
orders are cancelled instead of being left to overshoot.

## API assumptions that are out of date

Checked on 2026-11-07 against public docs and the `polymarket-client` 0.17.0 wheel:

- The current official Python SDK is `polymarket-client` (`SecureClient` /
  `AsyncSecureClient`). The keeper's `py-clob-client>=0.13.3` pin is the legacy
  stack. This bot does not call it.
- `SecureClient.create_limit_order` plus `post_order` posts a signed order and
  does not run the allowance-recovery helper. The one-shot place helper can
  approve ERC-1155 after a rejection. This bot does not call that helper, does
  not call allowance refresh, and does not approve.
- `SecureClient.create` can attempt a deposit-wallet deployment when a builder
  or relayer key is configured. This bot never passes that key. It also refuses
  a client whose signer address and funder address are the same.
- Authenticated user events are documented at
  `wss://ws-subscriptions-clob.polymarket.com/ws/user` with `markets` set to
  condition ids. Trade `side` is the taker's side. Maker SELLs are read from
  `maker_orders`. REST `list_open_orders` and `list_account_trades` are the
  reconciliation fallback. If either feed is stale or a read fails, the engine
  stops placing orders.
- Cancel-by-id is `cancel_orders`. Account-wide and whole-market cancels exist
  on the SDK and are not referenced by this adapter.
- Public `GET /clob-markets/{condition_id}` for the sampled BTC Up/Down market
  returned `v: "v1"`, two tokens (Up and Down), tick `0.01`, minimum size `5`.
  `GET /book` had no protocol `version` field. Gamma `endDate` was
  `2026-11-07T04:20:00Z`. Legacy `end_date_iso` on `GET /markets/{id}` was
  midnight the same day and is ignored.
- Token ids are not derived locally from the conditional-token helper. The
  configured ids must match both Gamma and the CLOB payload.
- Share balances from `get_balance_allowance` are treated as 6-decimal base
  units because current order amounts are 6-decimal integers. That scaling was
  not checked against a wallet. No balance was read for this change.

The sampled market was already a short window. Live verification must be
repeated and refuses a passed `endDate` even if `acceptingOrders` is still
true. `market_end_ts` in the paper configs is only a simulation bound.

## Not tested live

No credential was loaded, no client was constructed against production, and no
order, cancel, approval, or wallet deployment was sent. Paper, replay, and the
fake-client cycle are the tests that ran. A live cycle can still fail on
signature type, allowance spender, protocol v2 books, geoblock, or a tick
change. Unknown submit or cancel responses halt and keep share reservations.
