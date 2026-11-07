# Hai chế độ bán, không phải một

Chương trình nằm trong fork `haithanhkc/poly-market-maker`. Giấy phép MIT gốc giữ nguyên trong `LICENSE.txt`.

Có hai chế độ tách biệt:

- `share_balanced_session`: chế độ mới. Cân số share Up/Down còn lại trong một phiên. Mọi lệnh bán là maker, `post_only=true`. Không tắt được.
- `legacy_equal_gross_usd`: chế độ cũ trong `poly_market_maker.sell_equal`. Cân USD gộp, không cân share. Không bị đổi thầm thành chế độ mới.

Không chế độ nào tự split, tự mua, tự chuyển vốn sang thị trường khác, hay tự chạy nền trên máy bạn.

## Chế độ share-balanced, paper

Bạn tự split trước. Bot chỉ bán số share đã khai trong snapshot. Snapshot phải có `cost_basis_source=explicit_allocation` và `cost_basis_usd` của đúng phần được cấp. Không có cơ sở giá thì bot từ chối. Nó không lấy cả ví làm vốn và không coi khoản nạp là cost.

Ví dụ an toàn: vốn và cost 100 USD, 100 Up và 100 Down, ngưỡng +2 USD và -2 USD. Ngưỡng -2 là chặn rủi ro trước khi treo lệnh, không phải lệnh cắt lỗ đã khớp. Ngưỡng +2 dùng sàn thanh toán thận trọng: tiền bán gộp + min(Up còn, Down còn) - cost - phí. Rebate chưa trả không được cộng. Maker crypto theo tài liệu hiện tại có phí 0; phí taker `C * 0.07 * p * (1-p)` chỉ để ước tính, không để gửi lệnh taker.

Một lệnh tối thiểu 5 share tại giá 0,50 làm sàn thanh toán giảm 2,50 USD nếu chỉ một phía khớp. Ngân sách 2 USD không chứa nổi lệnh đó, nên bot ghi `min_size_exceeds_risk_budget` và bỏ qua. Khối lượng thấp hoặc bằng không là đúng, không phải lỗi cần nới rủi ro.

Mọi lệnh submit đi qua một chỗ và luôn `post_only=true`. Sổ lệnh đổi trước lúc gửi khiến lệnh vượt bid thì bị từ chối. Không gửi lại dạng taker, market, IOC hay FOK. Không cân bằng, không chốt +2/-2, không xử lý feed cũ bằng cách lấy thanh khoản. Nếu không treo được giá maker trong tick, min size và ngân sách, bot chờ hoặc dừng rủi ro mới và giữ phần còn lại đến lúc resolve. Hủy lệnh không có nghĩa là đã đóng vị thế. Không đảm bảo hai chân đều khớp, không đảm bảo lỗ không quá 2 USD, không đảm bảo có lãi, không phải bubblebust.

Trên Windows, trong thư mục repo:

```bat
py -3 -m unittest tests.test_share_balanced_session
py -3 -m poly_market_maker.share_balanced_session demo
py -3 -m poly_market_maker.share_balanced_session replay --script config\replay_share_balanced_session.json
```

`demo` không cần secret và không gửi lệnh thật. Nó mô phỏng một phiên: lần đầu bỏ qua vì 5 share tại 0,50 vượt ngân sách 2 USD, sau đó mới treo maker khi ask là 0,62, một chân Up khớp 5 share, tới gần hết phiên thì hủy lệnh của bot và in phần còn lại. `py -3 -m poly_market_maker.share_balanced_session live` luôn từ chối, kể cả khi có `--send`.

File mẫu: `config/share_balanced_session.example.json` và `config/share_balanced_baseline.json`. Đồng hồ trong file là mô phỏng, không có nghĩa cửa sổ BTC 5 phút thật còn mở.

## Chế độ cũ: USD gộp

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
