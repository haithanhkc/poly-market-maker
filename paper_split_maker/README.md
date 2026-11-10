# BTC 5-minute split maker

Splits pUSD into Up and Down, posts post-only sells, and merges what does not fill. `config.live.json` uses `strategy: ladder`.

1. Split a few seconds before the session opens (`split_lead_seconds` 8). A window that has already opened is skipped. One open market, so the previous pair is merged in the last 5 seconds rather than held across the next split.
2. Post a 3-level post-only ladder on each side, 0.01 apart, one 5-share clip per rung, around that side's own mid.
3. Reprice after `min_order_rest_seconds` (5) when the mid has moved by more than half a spacing.
4. The cheaper side (mid at least `skew_mid_gap` below the other) joins the touch and keeps all three rungs. The richer side steps one cent back and drops a rung.
5. In the last 60 seconds a side at or above 0.90 is offered only at or above 0.90. A side at or below 0.10 joins the bid. Still post-only.
6. If the other leg already filled at P and the bid pays `1 - P`, take that bid. Otherwise keep quoting. Merge leftovers at the stop, then redeem.

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

`paper_split_maker/.env` overrides the JSON. `BOT_SPLIT_SIZE` is the split for that run. Clip size, rung count, and the loss stops are derived from it unless an advanced line is set. See `.env.example`.

The derived ladder is scaled from bubblebust (3,488 splits of exactly 750). Each side may sell their mean heavy-side fill, 9.32% of the split, and never less than 5 shares. The clip is their modal 10-share print scaled by split/750, on a 5-share grid, and at least 5. Rungs are how many clips fit in that budget, at most 3 (their cheap side keeps a rung the rich side drops; loser shares average 1.48 times winner shares). Capital is 3 splits, because a position opened 293s before the session and merged 84s after the close overlaps the next two sessions. The session stop is their p95 session loss (8.828 pUSD) times split/750. The daily stop is their worst day (182.39 pUSD) divided by 3×750. The last 120s uses a 0.90 winner floor; their last-60s medians are 0.90 and 0.11. Quotes are not pulled 293s early: that lead is funded by the extra splits, and this engine still flattens in the last 5s. Split 25 and 50 both become clip 5, 1 rung, 5 shares per side, because 9.32% is under 5 shares. Split 25 → capital 75, session stop 0.294, daily budget about 6.08. Split 50 → capital 150, session stop 0.589, daily budget about 12.16.

On a live run, when `BOT_CAPITAL` is not set and the pUSD balance can be read, that balance becomes the capital before `state.json` is created. A balance below the split refuses the run. The dashboard never reads the chain. `BOT_CAPITAL`, `BOT_CLIP_SIZE`, `BOT_RUNGS`, `BOT_MAX_LOSS_FRACTION`, and `BOT_SESSION_STOP` still override the formula when present in `.env`. They are not on the dashboard.

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
- Split 25 và 50: clip 5, 1 bậc, 5 cổ mỗi phía. Vốn 75 và 150. Dừng phiên khoảng 0,294 và 0,589.

Live, nếu không có dòng `BOT_CAPITAL` và đọc được số dư pUSD, bot dùng số dư đó làm vốn (trước khi tạo `state.json`) khi số dư đủ cho khối split. Số dư thấp hơn khối split thì lần chạy bị từ chối. Trang không gọi chain. Muốn ghi đè, thêm trong `.env` (không có ô trên trang): `BOT_CAPITAL`, `BOT_CLIP_SIZE`, `BOT_RUNGS`, `BOT_MAX_LOSS_FRACTION`, `BOT_SESSION_STOP`.

Double-click `paper_split_maker\start_dashboard.bat`. File này tạo hoặc dùng `.venv`, cài dependency nếu thiếu, rồi mở trình duyệt tại `http://127.0.0.1:8765/`. Server chỉ bind `127.0.0.1`. Request có Host hoặc Origin không phải localhost bị từ chối. Mọi thao tác ghi phải là POST.

- **Lưu**: chỉ ghi split và số phiên. Không đụng khóa.
- **Ví**: khóa riêng, funder, kiểu chữ ký (0 EOA, 1 POLY_PROXY, 2 GNOSIS_SAFE, 3 POLY_1271), CLOB API key / secret / passphrase, relayer API key và địa chỉ relayer. Ô khóa là kiểu password và để trống. Lưu ô trống thì giữ giá trị cũ. **Xóa khóa** xóa các khóa, funder và API key. Trang chỉ hiện "đã lưu" (và 4 ký tự cuối nếu chuỗi đủ dài), cộng địa chỉ ký suy ra từ khóa riêng và địa chỉ funder. Giá trị khóa không quay lại trang, log, JSON trạng thái hay câu trả lời API. Không đổi khóa khi bot đang chạy.
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
| `strategy` | ladder | `single` is the old one-order quote. Both shipped configs use `ladder` |
| `initial_capital` | 59 | Bankroll locked into the ledger |
| `max_loss_fraction` | 0.20 | Daily loss stop at 11.80 (floor 47.20). Wide enough that a 5-share sell near mid is allowed |
| `session_max_loss` | 6 | Stop one session after 6 pUSD of loss, then merge |
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
| `risk_fee_rate` | 0.07 | Fee used in the loss-floor check. Maker fee stays 0 |

`config.paper.json` uses the same ladder, the same 59 pUSD bankroll, the same 25 split, and the same loss stops. It sets `"live": false`. The paper engine splits inside the ledger (`auto_split` true) because there is no chain. It does not send orders. The public paper window is 20 seconds.

## Migrate `config.live.run14.json`

That file lives only on the operator PC. It is not in this repo. Copy the values you still want into `config.live.json`. Then start with a new `--out` directory.

| Old key | What to do |
| --- | --- |
| `pre_split_lead_ms` | Delete it. Set `split_lead_seconds` (8 in the shipped file) |
| `stale_after_ms` | Paper book age. Live uses `stale_feed_seconds` |
| `market_slugs` | Delete it. The bot discovers `btc-updown-5m-<open>` |
| `entry_mode`, `buy_pair_arb`, `slices_per_side` | Delete them. The live bot is `strategy: ladder` |
| `close_improve_seconds`, `vol_pause_*`, `btc_pause_move`, `taker_repair`, `max_pair_loss`, `allow_loss_unwind`, `leg_fee_buffer` | Delete them. The loader rejects unknown keys |
| `session_max_loss` | Keep it. The shipped file stops one session at 6 pUSD |
| `max_loss_fraction` `0.034` | That 0.90–1.02 budget blocks a 5-share sell below about 0.80. The shipped file uses `0.20` on 59 pUSD (budget 11.80, floor 47.20) |
| `config.live.run5.json`, `run13`, `run15`, `old30`, `old200` | Removed from the repo. Do not point `--config` at them |

A 3% budget on a 30 pUSD account (0.90) only fits a 5-share sell near 0.82 or higher. The ladder mode needs the wider daily budget above, or the mid quotes never post. Use a new `--out` directory so the old 29.10 floor is not locked in.
