# Bot bán Up/Down theo giá trị USD gộp bằng nhau

Chương trình này nằm trong fork `haithanhkc/poly-market-maker`. Giấy phép MIT gốc được giữ nguyên trong `LICENSE.txt`.

Bot chỉ **bán** số share Up và Down đang có. Sau khi một lệnh SELL **thực sự khớp** (kể cả khớp một phần), bot tính số tiền gộp đã bán và có thể treo một lệnh SELL phía còn lại sao cho **cùng số USD gộp**, không phải cùng số share.

Ví dụ: 10 Up khớp tại 0,60 là 6 USD gộp. Nếu giá bán Down được cấu hình là 0,40 thì kích thước là 15 share, vì 15 × 0,40 = 6 USD. Lệnh đối ứng không được làm tròn số share lên.

## Điều bot không làm

- Không đảm bảo có lãi. Không đảm bảo lệnh thứ hai sẽ khớp.
- Giá trị đang chờ và giá trị đã bán là **gộp**: `giá × số share`. Phí không bị trừ. Thị trường BTC 5 phút được đọc ngày 2026-11-07 có phí cơ sở maker/taker 1000 và `fd.r` 0,07; các số đó không được dùng để tính lời.
- Bán cùng số USD **không** làm tồn kho trung lập. Ví dụ trên bán 10 Up và 15 Down.
- Không sao chép chiến lược bubblebust và không hứa tổng hai giá lớn hơn 1. Giá 0,60 và 0,40 trong ví dụ chỉ là giá cấu hình.
- Không mua, không split, không mint, không tự duyệt allowance, không ký giao dịch ví, không hủy mọi lệnh của cả tài khoản.
- Chế độ paper/replay không cần khóa và không mở mạng.

Chi tiết tiếng Anh về khác biệt với Bands và các API đã lỗi thời: `docs/sell_equal_limitations.md`.

## Cách tránh bán qua bán lại không dừng

Bot không phản ứng theo từng lệnh bằng một lệnh con mới. Nó giữ hai tổng:

- `gross_sold.up`
- `gross_sold.down`

Chênh lệch dương nghĩa là Up đã bán nhiều USD gộp hơn. Bot chỉ treo thêm Down cho phần còn thiếu sau khi trừ các lệnh hedge Down đang mở. Khi Down khớp, kể cả lệnh seed ban đầu, phần thiếu giảm và **không** sinh ra hedge Up mới cho chính khớp đó. Nếu hedge đang treo lớn hơn phần thiếu, bot hủy bớt hedge của chính nó thay vì để khớp tiếp rồi đảo chiều.

Khớp một phần dùng số share đã khớp, không dùng số share lúc treo. Mỗi `fill_id` chỉ được tính một lần. Khoảng trống `size_matched` chỉ được cộng khi lớn hơn số đã tính từ trade, nên trade đến sau không bị cộng lần nữa.

Share bị giữ bởi mọi lệnh còn hiệu lực, kể cả lệnh gửi hoặc hủy mà chưa rõ kết quả. Bot không gửi lệnh mới khi đang halt vì gửi/hủy không chắc, sổ lệnh biến mất mà không có trade giải thích, feed thị trường hoặc feed user quá cũ, hoặc thị trường đã kết thúc.

## Chạy trên Windows

Cài Python 3.10 trở lên từ python.org và chọn “Add python.exe to PATH”. Mở Command Prompt trong thư mục repo:

```bat
py -3 -m unittest tests.test_sell_equal
py -3 -m poly_market_maker.sell_equal demo
py -3 -m poly_market_maker.sell_equal replay --script config\replay_equal_usd.json
```

Paper và replay chỉ dùng thư viện chuẩn. Không cần `pip install` và không cần file `.env`. `demo` in ra:

```text
value_basis=gross
gross_up=6.00
gross_down=0
open_hedge outcome=down price=0.40 size=15 gross=6.00
```

Trên Linux hoặc macOS, đổi `py -3` thành `python3`.

Cấu hình mẫu `config/sell_equal_value.example.json` treo thang giá bán cả hai phía từ 0,51 đến 0,60, bước 0,01, mỗi mức 10 share. Đó là mẫu, đổi được. Mọi số tiền và kích thước trong JSON phải là chuỗi thập phân, không phải số JSON, để tránh sai số float.

`market_end_ts` trong file paper chỉ là mốc mô phỏng. Nó không có nghĩa cửa sổ 5 phút thật vẫn còn mở.

## Thị trường đã đối chiếu

Ngày 2026-11-07, bằng các GET công khai, không tài khoản:

- Điều kiện `0xe4966d642938113525dfb125060bda90b9f8d8c25945ae352de1b75c160bc383`
- Slug `btc-updown-5m-1794024900`
- Up `44857089090901365835706371888611204979538894171442730872794600452065181330944`
- Down `17424551638207318915606858171665237870653355896978276914963148187077601073259`
- Tick 0,01, tối thiểu 5 share, `negRisk` false, phiên bản CLOB `v1`
- `endDate` của Gamma là `2026-11-07T04:20:00Z`

`GET /book` không có trường protocol `version`. `end_date_iso` của endpoint `/markets` cũ là nửa đêm và bị bỏ qua. Bản ghi nằm ở `config/verified_btc_updown_snapshot.json`. Cửa sổ này đã hoặc sẽ kết thúc rất nhanh. Live phải đọc lại và sẽ từ chối nếu `endDate` đã qua, dù `acceptingOrders` vẫn là true.

## Live, chỉ khi tự bật

Lệnh `live` không gửi gì nếu thiếu `POLY_SELL_BOT_ENABLE_LIVE=1` hoặc thiếu `--send`. Đường này **chưa được chạy** với tài khoản thật trong lúc viết code.

Cần Python 3.11+ và gói đã ghim:

```bat
py -3.11 -m pip install -r requirements-sell-bot-live.txt
```

Biến môi trường chỉ đặt trên máy của bạn, không ghi vào repo, không dán vào chat:

- `POLY_SELL_BOT_ENABLE_LIVE=1`
- `POLYMARKET_PRIVATE_KEY` là signer
- `POLYMARKET_WALLET_ADDRESS` là funder, phải khác địa chỉ signer
- `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE` là credential CLOB có sẵn

Không truyền khóa builder/relayer. Adapter đọc số dư và allowance của đúng exchange đã biết, rồi lấy min của số dư, allowance và inventory trong config. Nếu allowance thấp hơn kích thước tối thiểu, bot dừng và không tự duyệt. Đơn chỉ là SELL, `post_only`, trong đúng condition đã cấu hình. Khi tắt, bot chỉ hủy các id do chính nó lưu.

Feed user đã được kiểm tra trong tài liệu và trong `polymarket-client` 0.17.0: `wss://ws-subscriptions-clob.polymarket.com/ws/user`, lọc theo condition id. Bật thêm bằng `POLY_SELL_BOT_USER_WS=1`. Nếu không bật hoặc stream lỗi, vòng live dùng `list_open_orders` và `list_account_trades`. Feed cũ hoặc đọc lỗi thì dừng đặt lệnh.

```bat
py -3.11 -m poly_market_maker.sell_equal live --send --config config\sell_equal_value.example.json
```

`--send` có thể treo lệnh SELL thật nếu kiểm tra thị trường và số dư đều qua. File mẫu trỏ vào cửa sổ 5 phút đã được ghi nhận; kiểm tra live phải từ chối khi cửa sổ đã hết. Hãy đổi condition, token và inventory cho số share bạn đang nắm trước khi cân nhắc `--send`.

## Ràng buộc rủi ro

- Không bán dưới `price_floor`, không vượt `price_ceiling`, không đổi giá hedge quá `max_reprice_ticks`. Mặc định là 0: không đuổi giá để đủ USD.
- Không vượt tồn kho còn lại sau phần đã giữ cho lệnh mở.
- Không vượt `max_outstanding_notional`.
- Seed mới dừng khi lệch USD gộp vượt `max_gross_skew`. Hedge chỉ làm phần thiếu nhỏ lại.
- `max_session_loss` so mark cấu hình: `mark ban đầu - (mark phần còn + tiền gộp đã bán)`. Chạm ngưỡng thì dừng và hủy lệnh của bot.
- Phần dư nhỏ hơn min size hoặc nhỏ hơn `value_dust` được để lại, không bán thêm.

Trạng thái ghi ở `state_path` (thư mục `state/` không được commit). Khởi động lại không tính lại fill cũ và không bỏ phần share đang giữ.
