# BTC up/down split maker (predict.fun)

Splits USDT into Up and Down on predict.fun (BNB Chain), posts post-only sells, and merges what does not fill. `config.live.json` uses `strategy: ladder`. The series is `btc-updown-5m` by default. `btc-updown-15m` and `btc-updown-1h` use the same bot; the 1-hour market resolves on Binance candles at the venue, not in this process.

1. Split before the session opens. `.env` sets the lead to 300 seconds unless `BOT_SPLIT_LEAD` is blanked. A window that has already opened is skipped. One open market, so the previous pair is merged in the last 5 seconds rather than held across the next split.
2. Post a 3-level post-only ladder on each side, 0.01 apart, one 5-share clip per rung, around that side's own mid.
3. Reprice after `min_order_rest_seconds` (5) when the mid has moved by more than half a spacing.
4. The cheaper side (mid at least `skew_mid_gap` below the other) joins the touch and keeps all three rungs. The richer side steps one cent back and drops a rung.
5. In the last 60 seconds a side at or above 0.90 is offered only at or above 0.90. A side at or below 0.10 joins the bid. Still post-only.
6. If the other leg already filled at P and the bid pays `1 - P`, take that bid. Otherwise keep quoting. Merge leftovers at the stop, then redeem.

The book is one Yes axis. A Down price of `p` is the complement `1 - p` on that book. Maker orders are sells of the outcome token with `isPostOnly`. Maker fee is 0. The smallest exchange order is 1 share; this ladder still posts at least 5.

A skipped quote is one line: `quote_skipped <market> <side> <reason> ...` with the price, budget, minimum size, and headroom. Reasons include `loss_budget`, `pair_floor`, and `stale_feed`.

## Dry run (Windows PowerShell)

Repo: `C:\Users\danny\Desktop\poly-market-maker`. Paper mode uses the standard library only and does not send orders.

```powershell
cd C:\Users\danny\Desktop\poly-market-maker
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r paper_split_maker\requirements.txt
copy paper_split_maker\.env.example paper_split_maker\.env
cd paper_split_maker
python -m paper_split_maker --config config.paper.json --out ..\runs\paper --max-markets 1
```

That command does not send orders. Paper reads `PREDICT_NETWORK` and `PREDICT_API_KEY` from `.env` (or the process environment) and uses them for the public book. `mainnet` calls `https://api.predict.fun` with the `x-api-key` header. Leaving the network unset stays on testnet, which has no `btc-updown-5m` series. Discovery errors, the round count, and `opened` are printed on stderr and stored in `summary.json`. A window that has already started is skipped; the next one is loaded during `split_lead_seconds` (300 unless `BOT_SPLIT_LEAD` is set).

`strategy=dual_bid` is a separate paper mode. It does not split or sell. After the round opens it posts a post-only bid ladder on Up and on Down, refreshes it with the book, and holds the shares until resolution. New bids start at `dual_bid_start_seconds` (8) and are pulled at `dual_bid_stop_seconds` (275). A bid is placed only when the other side's average price plus that bid is at most `target_pair_cost` (0.97). The heavier side stops when the share balance `min/max` is below 0.85 or the matched pair cost reaches 1. Prices under 0.10 are skipped. Each round is capped by `round_budget` (25 USDT) and by the existing account loss floor. The ladder session-loss number is not applied here; it is sized for the sell strategy and is smaller than one bid.

Fills are modeled from the public trade prints the paper loop polls (`GET /v1/orders/matches?marketId=` and the order book), not from a private user channel and not from `wss://ws.predict.fun/ws`. `/v1/markets/{id}/matches` is not a predict.fun route. The order book is fetched again on every poll. A book is fresh when that snapshot arrived within `stale_after_ms`, not when predict's `updateTimestampMs` (the last time the book changed) is recent. A resting dual-bid ladder is not booked as a loss. The account floor sees filled unmatched shares, plus open bids on the side we already hold more of. The round budget still caps spend at 25 USDT. Our bid is last in the queue that was displayed at that price when we posted. A SELL print at our price consumes that queue and then fills us at our bid. A SELL print below our price treats the queue as already swept and can fill us at our bid. BUY prints do not fill bids. Maker fee is 0 in this model, and a cancel we have requested does not fill. `config.paper.json` polls every 2 seconds, and `dual_bid_requote_ms` is 1500, so this command reprices on that 2-second poll. That is not the venue matching engine. Each poll prints `dual_bid <slug> books_ok=… bids_posted=… trades_ok=… trades_failed=… trades_http=… trades_seen=… skipped <reason>=<count>` on stderr. The same counters are on each `rounds` row in `summary.json`.

```powershell
python -m paper_split_maker --config config.paper.json --strategy dual_bid --out runs\x --max-markets 5 --duration-seconds 1800
```

`summary.json` then has one `rounds` row per session (fills per side, average prices, pair cost, matched shares, balance, excess, PnL, winner) and a `round_totals` object. PnL is filled in when the round redeems. The command does not send orders. `--live` with `dual_bid` is refused.

`strategy=small_pair` is a second paper-only mode. It does not replace `dual_bid`. It quotes BTC 5-minute and 15-minute up/down rounds from 2 seconds after the open. The top Up bid and the top Down bid sum to 0.96 and sit half of the remaining cent under the Up mid. Four levels step down by 0.01. Each level is one lot, `ceil(1 / price)` shares and at most 8. When one side is ahead by 4 shares or more, that side's bids are cancelled until it is back under the cap. From 270s (5m) or 840s (15m), a flat position places nothing and a skewed position keeps only the light side. The round stops at 25 USDT. Shares are held to resolution and redeemed. Fills use the same queue model and `GET /v1/orders/matches` feed as `dual_bid`. This is not a claim that the shape is profitable.

`prepare_run` still derives the ladder bankroll from the split (`split_clip * 3`, and the worst-day fraction `182.3864 / 2250`). With `split_clip` 25 that is capital 75 and a loss floor of about 68.92, which is why a JSON `initial_capital` of 5000 used to be ignored. For `dual_bid` and `small_pair` the JSON `initial_capital` and `max_loss_fraction` are put back after that derive, unless `BOT_CAPITAL` or `BOT_MAX_LOSS_FRACTION` is set. `--capital` and `--max-loss-fraction` override both. `--max-markets` accepts up to 500 and, when you pass it, wins over `BOT_MAX_MARKETS`. Use a fresh `--out` directory; a resume will not change the capital locked in `state.json`.

```powershell
cd C:\Users\danny\Desktop\poly-market-maker
.\.venv\Scripts\Activate.ps1
cd paper_split_maker
python -m paper_split_maker --config config.paper.json --strategy small_pair --out runs\smallpair --max-markets 300 --duration-seconds 90000 --capital 5000 --max-loss-fraction 0.20
```

That is about 25 hours, long enough for 300 sessions when both series are on. Both the 5-minute and the 15-minute rounds count toward `--max-markets`. `small_pair_series` in the config, or `--series`, chooses which books to load. Allowed values are `btc-updown-5m`, `btc-updown-15m`, and `btc-updown-1h`. The default is 5m and 15m.

A 5-minute-only run opens about 12 rounds an hour, so 300 sessions is 25 hours plus one extra hour for the last round to finish:

```powershell
python -m paper_split_maker --config config.paper.json --strategy small_pair --series btc-updown-5m --out runs\smallpair5m --max-markets 300 --duration-seconds 93600 --capital 5000 --max-loss-fraction 0.20
```

The process sends no orders. Mainnet reads still need `PREDICT_NETWORK=mainnet` and `PREDICT_API_KEY` in `.env`. `--live` with `small_pair` is refused. Each `rounds` row has the winner, fills and shares per side, average prices, pair cost, matched shares, the excess side and its price, and PnL split into matched and excess.

`--binance-lean` (config `small_pair_binance`, default off) reads the public Binance BTCUSDT trade stream and uses that fair value as a filter. It does not send orders and does not use an API key. The quote stays on the book mid. A bid more than 0.02 above its fair value is not posted (0.03 when the Binance print is older than 2 seconds). From 30 seconds, a side below 0.45 is pulled. From 240 seconds, a side below 0.50 is pulled. A side at or above 0.55 may lead by 12 shares; a side below 0.50 may lead by 4. When the Up mid is within 0.10 of 0.50 the pair sum is 0.94 instead of 0.96. Quoting starts at 10 seconds. Sigma is the 15-minute realized volatility, multiplied by at most 1. Each simulated fill logs fair value, price, fair minus price, seconds into the round, and the age of the Binance print. The round row counts bids removed by each rule and the average fair-minus-price of fills. Without the flag the old 2-second, 0.96, cap-of-4 quote is unchanged. A 10-session wrapper is `run_smallpair_bn10.ps1`:

```powershell
cd C:\Users\danny\Desktop\poly-market-maker\paper_split_maker
.\run_smallpair_bn10.ps1
```

An open-ended $100 run is `run_smallpair_bn100.ps1`. It keeps the machine awake, writes `runs\smallpair_bn100`, and uses `--duration-seconds 0 --max-markets 0` so sessions roll until cash cannot fund another one, you press Ctrl+C, or you create `runs\smallpair_bn100\STOP`. `summary.json` is rewritten after every redeem (`stop_reason`, equity, sessions, wins, losses, cumulative PnL) and `sessions.csv` has one row per session. The per-session budget is `min(25, cash * 0.25)`. `--max-loss-fraction 0.99` keeps the account floor from stopping the run first. Run the same script again after a reboot: it resumes `state.json` when the capital and loss fraction match. If they do not, the process prints the locked bankroll and keeps it. Do not delete `state.json` to force a new bankroll; use a new `--out` directory instead.

```powershell
cd C:\Users\danny\Desktop\poly-market-maker\paper_split_maker
.\run_smallpair_bn100.ps1
```

Check the key, the login, and the USDT balance without placing an order. The JSON it prints does not contain the key or the JWT:

```powershell
python -m paper_split_maker --config config.paper.json --check-connection
```

`requirements.txt` is the paper install. Live signing needs a second install, and still does not run unless every live gate passes:

```powershell
python -m pip install -r requirements-live.txt
```

Live needs all three gates. `config.live.json` already has `"live": true`. The process also refuses to start unless both flags are present. Create a file named `kill.switch` in the working directory to stop. New splits stop once realized loss reaches `-(initial_capital * max_loss_fraction)`.

```powershell
python -m paper_split_maker --config config.live.json --out ..\runs\live --max-markets 3 --live --yes-i-understand-live-risk
```

Use a new `--out` directory after changing capital or the loss fraction. Those values are locked into `state.json` and a resume will not loosen them. Do not put keys in the config. The process reads `PREDICT_PRIVATE_KEY`, `PREDICT_API_KEY`, `PREDICT_NETWORK` (`testnet` or `mainnet`), and the optional `PREDICT_ACCOUNT` from `.env` or the environment only when `--live` is set. Testnet does not need an API key. Mainnet also needs `PREDICT_ALLOW_MAINNET=yes`. This copy stays on testnet until a later, explicit live approval.

`paper_split_maker/.env` overrides the JSON. `BOT_SPLIT_SIZE` is the split for that run. Clip size, rung count, and the loss stops are derived from it unless an advanced line is set. See `.env.example`.

The derived ladder is scaled from bubblebust (3,488 splits of exactly 750 pUSD on the old tape). The same ratios are applied in USDT. Each side may sell their mean heavy-side fill, 9.32% of the split, and never less than 5 shares. The clip is their modal 10-share print scaled by split/750, on a 5-share grid, and at least 5. Rungs are how many clips fit in that budget, at most 3 (their cheap side keeps a rung the rich side drops; loser shares average 1.48 times winner shares). Capital is 3 splits, because a position opened 293s before the session and merged 84s after the close overlaps the next two sessions. The session stop is their p95 session loss (8.828) times split/750. The daily stop is their worst day (182.39) divided by 3×750. The last 120s uses a 0.90 winner floor; their last-60s medians are 0.90 and 0.11. The next market is split 300 seconds before it opens (their median is 293). That lead does not pull quotes on the current market; those stop in the last 5s. Cash from the 3-split bankroll funds the overlap, so the current session stays quoted while the next one is already resting. A one-split bankroll still merges in the lead, because it cannot pay for both. New sells stop after both sides together have sold 35% of the split, the 90th percentile of their share volume on the older days. A matched sell cannot lock a pair below 1. A resting rung is replaced only when the target moves by at least one tick, or the bid has reached the order. Split 25 and 50 both become clip 5, 1 rung, 5 shares per side, because 9.32% is under 5 shares. Split 25 → capital 75, session stop 0.294, daily budget about 6.08. Split 50 → capital 150, session stop 0.589, daily budget about 12.16.

On a live run, when `BOT_CAPITAL` is not set and the USDT balance can be read, that balance becomes the capital before `state.json` is created. A balance below the split refuses the run. The dashboard never reads the chain. `BOT_CAPITAL`, `BOT_CLIP_SIZE`, `BOT_RUNGS`, `BOT_MAX_LOSS_FRACTION`, and `BOT_SESSION_STOP` still override the formula when present in `.env`. They are not on the dashboard.

## Bảng điều khiển (Windows)

Copy `paper_split_maker\.env.example` thành `.env` nếu chưa có. Trang chỉ sửa hai ô: khối split (`BOT_SPLIT_SIZE`) và số phiên (`BOT_MAX_MARKETS`). Clip, số bậc, dừng một phiên và dừng ngày được tính từ khối split và hiện ở mục "Bot sẽ dùng" (chỉ đọc).

Công thức lấy từ lệnh của bubblebust (3.488 lần split, mỗi lần đúng 750), rồi nhân theo khối split của bạn. Tối thiểu 5 cổ.

- Mỗi phía bán trung bình 9,32% split (phía bán nhiều hơn). Dưới 5 cổ thì nâng lên 5.
- Clip hay gặp là 10 cổ trên 750, tức 10/750 của split, làm tròn xuống bội số của 5, và không dưới 5.
- Số bậc = số clip vừa ngân sách đó, tối đa 3. Phía đắt bỏ một bậc: họ bán phía thua trung bình 1,48 lần phía thắng.
- Vốn = 3 × split. Họ mở lệnh trước 293 giây và gộp 84 giây sau khi đóng, nên thường giữ 2 split, đôi khi 3.
- Dừng một phiên = 8,828 × split/750 (p95 lỗ một phiên của họ).
- Dừng ngày = 182,39 / (3 × 750) của vốn (ngày tệ nhất trong 13 ngày).
- 120 giây cuối: winner không dưới 0,90. Median 60 giây cuối của họ là winner 0,90 và loser 0,11.
- Tách phiên sau trước 300 giây. Phiên đang chạy vẫn rao đến 5 giây cuối. Vốn 3 split trả cho phiên chồng nhau.
- Dừng bán thêm khi cả hai phía đã bán 35% split. Cặp đã khớp không được khóa dưới 1.
- Lệnh treo chỉ bị hủy khi giá mới lệch ít nhất 1 tick, hoặc bid đã chạm lệnh.
- Split 25 và 50: clip 5, 1 bậc, 5 cổ mỗi phía. Vốn 75 và 150. Dừng phiên khoảng 0,294 và 0,589.

Live, nếu không có dòng `BOT_CAPITAL` và đọc được số dư USDT, bot dùng số dư đó làm vốn (trước khi tạo `state.json`) khi số dư đủ cho khối split. Số dư thấp hơn khối split thì lần chạy bị từ chối. Trang không gọi chain. Muốn ghi đè, thêm trong `.env` (không có ô trên trang): `BOT_CAPITAL`, `BOT_CLIP_SIZE`, `BOT_RUNGS`, `BOT_MAX_LOSS_FRACTION`, `BOT_SESSION_STOP`.

Double-click `paper_split_maker\start_dashboard.bat`. File này tạo hoặc dùng `.venv`, cài dependency nếu thiếu, rồi mở trình duyệt tại `http://127.0.0.1:8765/`. Server chỉ bind `127.0.0.1`. Request có Host hoặc Origin không phải localhost bị từ chối. Mọi thao tác ghi phải là POST.

- **Lưu**: chỉ ghi split và số phiên. Không đụng khóa.
- **Ví**: khóa riêng `PREDICT_PRIVATE_KEY`, API key `PREDICT_API_KEY` (mainnet), mạng `testnet` hoặc `mainnet`, và địa chỉ Predict Account nếu USDT nằm ở smart account. Ô khóa là kiểu password và để trống. Lưu ô trống thì giữ giá trị cũ. **Xóa khóa** xóa khóa riêng và API key, rồi đưa mạng về testnet. Trang chỉ hiện "đã lưu" (và 4 ký tự cuối nếu chuỗi đủ dài), cộng địa chỉ ký suy ra từ khóa riêng. Giá trị khóa không quay lại trang, log, JSON trạng thái hay câu trả lời API. Không đổi khóa khi bot đang chạy.
- **Chạy paper**: xóa `kill.switch` cũ, tạo thư mục `runs\paper-<thời gian>`, không gửi lệnh.
- **Chạy live**: phải tick "Tôi hiểu rủi ro live". Lệnh có `--live --yes-i-understand-live-risk`.
- **Dừng**: ghi `kill.switch` và tắt process. Không chạy hai bot cùng lúc.

Retry a deferred redeem with the same program. This path loads keys and does not start a session:

```powershell
python -m paper_split_maker --redeem-pending ..\runs\live\redeem-pending.json
```

## Config

Two files. Anything omitted uses the default in `paper_split_maker/config.py`. A run from the dashboard or the CLI then replaces capital, clip, rungs, the per-side budget, the late window, and both loss stops with the split formula above. The table is what the JSON stores before that replacement.

| Param | Live value | Meaning |
| --- | --- | --- |
| `strategy` | ladder | `single` is the old one-order quote. `dual_bid` is the paper buy-both mode. Both shipped configs use `ladder` |
| `initial_capital` | 59 | Bankroll locked into the ledger |
| `max_loss_fraction` | 0.20 | Daily loss stop at 11.80 (floor 47.20). Wide enough that a 5-share sell near mid is allowed |
| `session_max_loss` | 6 | Stop one session after 6 USDT of loss, then merge |
| `split_clip` / `clip_size` | 25 / 5 | One split per session, 5 shares per rung |
| `ladder_levels` / `ladder_spacing` | 3 / 0.01 | Rungs per side, one cent apart |
| `skew_mid_gap` | 0.04 | How far a mid must lag before that side joins the touch |
| `late_window_seconds` / `winner_min_price` | 60 / 0.90 | Last-minute winner floor and loser dump |
| `max_unpaired_fraction` | 0.60 | The cheap side may lead by 15 shares of a 25 split |
| `pair_min_edge` | 0 | Pairs are not required to clear 1.01. The loss caps are the limit |
| `max_open_markets` | 1 | One session at a time |
| `split_lead_seconds` | 8 | Split this many seconds before the open, after the previous merge |
| `stop_quoting_before_expiry_ms` | 5000 | Flatten in the last 5s so the last minute still quotes |
| `min_order_rest_seconds` | 5 | Minimum time before a replace |
| `stale_feed_seconds` | 10 | Skip quoting when the book is older than this |
| `kill_switch_file` | kill.switch | Stop file, created in the working directory |
| `series_slug` | btc-updown-5m | `btc-updown-15m` or `btc-updown-1h` change the window |
| `risk_fee_rate` | 0.07 | Fee used in the loss-floor check. Maker fee stays 0 |

`config.paper.json` uses the same ladder, the same 59 USDT bankroll, the same 25 split, and the same loss stops. It sets `"live": false`. The paper engine splits inside the ledger (`auto_split` true) because there is no chain. It does not send orders. The public paper window is 20 seconds.

## Migrate `config.live.run14.json`

That file lives only on the operator PC. It is not in this repo. Copy the values you still want into `config.live.json`. Then start with a new `--out` directory.

| Old key | What to do |
| --- | --- |
| `pre_split_lead_ms` | Delete it. Set `split_lead_seconds` (8 in the shipped file) |
| `stale_after_ms` | Paper book age. Live uses `stale_feed_seconds` |
| `market_slugs` | Delete it. The bot discovers `btc-updown-5m-<open>` |
| `entry_mode`, `buy_pair_arb`, `slices_per_side` | Delete them. The live bot is `strategy: ladder` |
| `close_improve_seconds`, `vol_pause_*`, `btc_pause_move`, `taker_repair`, `max_pair_loss`, `allow_loss_unwind`, `leg_fee_buffer` | Delete them. The loader rejects unknown keys |
| `session_max_loss` | Keep it. The shipped file stops one session at 6 USDT |
| `max_loss_fraction` `0.034` | That 0.90–1.02 budget blocks a 5-share sell below about 0.80. The shipped file uses `0.20` on 59 USDT (budget 11.80, floor 47.20) |
| `config.live.run5.json`, `run13`, `run15`, `old30`, `old200` | Removed from the repo. Do not point `--config` at them |

A 3% budget on a 30 USDT account (0.90) only fits a 5-share sell near 0.82 or higher. The ladder mode needs the wider daily budget above, or the mid quotes never post. Use a new `--out` directory so the old 29.10 floor is not locked in.
