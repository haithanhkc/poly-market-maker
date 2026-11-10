# BTC 5-minute split maker

Splits pUSD into Up and Down, posts post-only sells on both legs, reprices as the book moves, and flattens before the window ends.

1. Split in the lead before the session opens (`split_lead_seconds`, default 45). Windows that have already opened are skipped.
2. Post a maker sell on each leg. `slices_per_side` is 1. Set it to 2 or 3 later for a small ladder; the quote path already takes that count.
3. Reprice only after `min_order_rest_seconds` and only when the target moved by more than `max(2 ticks, requote_vol_k * short-window stdev)`.
4. After one leg fills at P, the other leg's floor is `1 - P` (plus the taker fee when `leg_fee_buffer` is true). If the best bid covers that floor, sell as a taker. Otherwise rest post-only at `max(floor, best ask - 1 tick)` and reprice. A post-only reject that says the order crosses the book is retried as a taker when the bid covers the floor. Partial fills use the filled size.
5. Merge leftover pairs. Redeem after the market settles. A pair that cannot meet the loss budget is held until the stop window so a later legal price can still be posted. A book older than `stale_feed_seconds` is logged as `quote_skipped` and is not merged.

A skipped quote is one line: `quote_skipped <market> <side> <reason> ...` with the price, budget, minimum size, and headroom. Reasons include `loss_budget`, `pair_floor`, and `stale_feed`.

## Run (Windows PowerShell)

Repo: `C:\Users\danny\Desktop\poly-market-maker`. Virtualenv: `.venv`.

```powershell
cd C:\Users\danny\Desktop\poly-market-maker
.\.venv\Scripts\Activate.ps1
cd paper_split_maker
python -m paper_split_maker --config config.paper.json --out ..\runs\paper --max-markets 1
```

Live needs all three gates. `config.live.json` already has `"live": true`. The process also refuses to start unless both flags are present. Create a file named `kill.switch` in the working directory to stop. New splits stop once realized loss reaches `-(initial_capital * max_loss_fraction)`.

```powershell
python -m paper_split_maker --config config.live.json --out ..\runs\live --max-markets 3 --live --yes-i-understand-live-risk
```

Use a new `--out` directory after changing capital or the loss fraction. Those values are locked into `state.json` and a resume will not loosen them. Do not put keys in the config. The process reads `POLY_PRIVATE_KEY`, `POLY_FUNDER`, and `POLY_SIGNATURE_TYPE` from the environment only when `--live` is set.

Retry a deferred redeem with the same program. This path loads keys and does not start a session:

```powershell
python -m paper_split_maker --redeem-pending ..\runs\live\redeem-pending.json
```

## Config

Two files. Anything omitted uses the default in `paper_split_maker/config.py`.

| Param | Live value | Meaning |
| --- | --- | --- |
| `initial_capital` | 30 | Bankroll locked into the ledger |
| `max_loss_fraction` | 0.03 | Loss budget. Floor is 29.10. A new split or a new sell that would break the floor is skipped |
| `split_clip` / `clip_size` | 5 | Shares split, and the size of each sell |
| `max_open_markets` | 1 | One session at a time |
| `pair_min_edge` | 0.01 | A matched pair of sells must clear `1 + edge` |
| `max_unpaired_fraction` | 0.5 | Cap on size that is not matched by the other leg |
| `split_lead_seconds` | 45 | Split this many seconds before the window opens |
| `stop_quoting_before_expiry_ms` | 20000 | Stop quoting and flatten in the last 20s |
| `close_improve_seconds` | 90 | Step the ask toward the bid, never through the pair floor |
| `min_order_rest_seconds` | 20 | Minimum time before a replace |
| `requote_vol_k` / `requote_vol_window_ms` | 1 / 30000 | Replace only if the move beats this volatility band |
| `stale_feed_seconds` | 10 | Skip quoting when the book is older than this |
| `sell_ready_timeout_seconds` | 30 | If posts keep erroring after the open, cancel and merge |
| `leg_unwind_seconds` | 5 | Repeat the other-leg bid check |
| `leg_fee_buffer` | true | Lift the other-leg floor by the taker fee |
| `allow_loss_unwind` | false | Do not sell through the floor |
| `max_unwind_loss` | 0.50 | Cap if loss unwind is turned on, and only in the last `unwind_loss_before_expiry_seconds` |
| `max_runtime_seconds` | 2400 | Hard stop. Does not cut a session off early |
| `kill_switch_file` | kill.switch | Stop file, created in the working directory |
| `slices_per_side` | 1 | Ladder depth. Raise later for more price levels |
| `risk_fee_rate` | 0.07 | Fee used in the loss-floor check. Maker fee stays 0 |

`config.paper.json` is the same engine with `"live"` omitted, capital 1000, and a 20 second paper window. It does not send orders.

## Migrate `config.live.run14.json`

That file lives only on the operator PC. It is not in this repo. Copy the values you still want into `config.live.json`. Then start with a new `--out` directory.

| Old key | What to do |
| --- | --- |
| `pre_split_lead_ms` | Divide by 1000 and set `split_lead_seconds`. If the seconds field is missing, the loader does this itself. When both are set, seconds win |
| `stale_after_ms` | Paper book age. Live uses `stale_feed_seconds` |
| `market_slugs` | Delete it. A placeholder such as `no-initial-discovery-placeholder` is ignored; the bot discovers `btc-updown-5m-<open>` |
| `entry_mode` | Keep `split_sell` |
| `max_loss_fraction` `0.034` | Still valid. The shipped file uses `0.03` (budget 0.90, floor 29.10). 3.4% of 30 is about 1.02, which is what a 5-share sell near 0.80 needs |
| `slices_per_side` | Leave at 1 until you want a ladder |
| `vol_pause_move`, `btc_pause_move`, `taker_repair`, `session_max_loss`, `max_pair_loss` | Still accepted. Leave them out. They default off. The loss budget, the pair edge, and the other-leg unwind replace them |
| `config.live.run5.json`, `run13`, `run15`, `old30`, `old200` | Removed from the repo. Do not point `--config` at them |

A 5-share sell fits the 0.90 budget only when the price is about 0.82 or higher before fees, and about 0.85 to 0.86 once the 7% risk fee and the reserves are included. Below that the log says `quote_skipped ... loss_budget` and the pair is held.
