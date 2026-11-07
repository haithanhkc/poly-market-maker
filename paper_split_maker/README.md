# paper-split-maker

Sổ lệnh **giấy** cho thị trường nhị phân BTC Up/Down 5 phút trên Polymarket. Bot bán hai phía sau khi `SPLIT` tạo kho Up và Down bằng nhau, giữ phần lệch trong một ngân sách lỗ đã khóa, `MERGE` cặp token chưa bị lệnh treo giữ chỗ, và chỉ `REDEEM` khi có kết quả từ nguồn chính thức hoặc từ fixture được gắn nhãn.

Đây là chương trình mới, nằm trọn trong thư mục này. Nó không sửa `poly_market_maker` và không gửi lệnh lên CLOB.

## Cảnh báo

Bot không có khóa riêng, không ký, không có endpoint xác thực, và không có nhánh lệnh thật. Không có cờ bật live.

Sàn rủi ro dưới đây giới hạn **lỗ cuối kỳ trong sổ mô phỏng**. Nó không giới hạn lỗ mark-to-market khi đang giữ kho. Nó không bao gồm phí gas, lỗi hợp đồng, oracle, hay việc khớp thật ngoài mô hình. Các bài kiểm thử của repo **không** phải bằng chứng rằng lỗ live bị chặn ở 3%.

Lịch sử một ví quan sát được là mô tả hành vi, không phải dữ liệu khớp phản chứng cho lệnh của bot này. Bot không sao chép một quy tắc fair-value ẩn và không khẳng định lợi nhuận.

## Diễn giải “lỗ tối đa 3%”

Vốn giấy mặc định là **1000 USDC**. Mức 3% là **ngân sách lỗ cuối kỳ của cả sổ**, tính một lần trên vốn ban đầu:

- ngân sách lỗ = `floor(1000 × 0.03)` = **30 USDC**
- sàn vốn = `1000 − 30` = **970 USDC**

Bất biến của mô hình: vốn cuối kỳ trong tình huống xấu nhất ≥ 970.

Diễn giải cố định, không nới:

- Không phải mức lỗ của từng lệnh.
- Không làm mới sau mỗi thị trường thua.
- Không làm mới khi khởi động lại cùng file `state.json`.
- Không phải hạn mức live, không phải lệnh dừng trên sàn, không phải cam kết hoàn lỗ.

Phần dự phòng (phí bất lợi, dự phòng làm tròn, dự phòng mỗi thị trường đang mở) **nằm trong** 30 USDC đó. Vì vậy chỗ còn lại để chịu lỗ giao dịch nhỏ hơn 30. Xóa file trạng thái là mở một sổ mới; chương trình không tự xóa sổ, nhưng cũng không thể ngăn người vận hành xóa file.

Khi worst-case chạm sàn, bot đóng băng mở thêm rủi ro. Việc còn được phép là hủy lệnh, `MERGE` phần token chưa bị giữ chỗ nếu không làm worst-case giảm, và bán phía đang dư nếu lệnh đó giảm lệch kho, không làm worst-case giảm, vẫn có edge, và không vượt bid. Bot không bán bất chấp giá để ép đóng.

## Kiến trúc

```text
config.example.json        chiến thuật (edge, clip, cooldown, cửa sổ hết hạn)
state.json                 sổ cái + khóa rủi ro tại lúc tạo sổ
paper_split_maker/
  config.py                đọc cấu hình, từ chối khóa lạ hoặc khóa nhạy cảm
  models.py                thị trường, lệnh, sổ
  risk.py                  worst-case / best-case
  quotes.py                giá bán từng phía từ chính sổ lệnh của phía đó
  engine.py                SPLIT, MERGE, REDEEM, giữ chỗ, hủy, khớp giấy
  normalize.py             Gamma, CLOB, Data API, khung websocket công khai
  public_client.py         GET tới đúng ba host chính thức
  session.py / cli.py      demo, replay, paper có hạn
```

Mỗi nhịp quyết định, theo thứ tự:

1. Xác nhận lệnh đã sống sau `ack_latency` và hủy đã xong sau `cancel_latency` (chỉ khi feed còn nối).
2. Nếu đã quá giờ kết thúc mà chưa có kết quả chính thức: đánh dấu `expired_unresolved`, không bịa phe thắng.
3. Feed đứt, sổ lệnh cũ, hoặc còn dưới `stop_quoting_before_expiry`: không chào giá mới; xin hủy; giữ chỗ cho đến khi hủy được xác nhận.
4. Nếu còn edge trên sổ của từng phía và chưa chạm sàn: `SPLIT` thêm cho đủ kho cặp, trong hạn mức từng thị trường.
5. Chào tối đa một lệnh bán đang hiệu lực cho mỗi phía. Muốn đổi giá thì phải hủy và đợi xác nhận, để không giữ chỗ hai lần.
6. Khi không còn gì để chào: `MERGE` cặp token tự do.

Up và Down được định giá **tách nhau**. Bot không đặt giá Down bằng `1 − Up` và không giả định hai chân khớp cùng lúc hay tổng giá > 1 còn đúng sau này.

### Giá và độ lệch kho

Fair của một phía là midpoint `(best bid + best ask) / 2` của **chính** token đó. Sổ một phía, spread rộng hơn `max_spread`, hoặc hai chiều giao nhau thì bỏ qua.

Edge yêu cầu = `min_edge + skew_edge_per_share × max(0, kho_phía_kia − kho_phía_này)`.

Giá chào = làm tròn lên theo tick của `max(best ask, fair + edge)`, và phải nằm trong `[min_price, max_price]`, lớn hơn best bid (không biến thành lệnh taker).

Phía đang làm lệch nặng hơn bị giảm size: `clip − skew_size_per_share × phần lệch`. Nhỏ hơn `min_order_size` thì không chào phía đó. Phía đang dư hàng được chào đủ clip nếu còn hàng và còn sàn rủi ro. Không có edge thì không bán, kể cả khi đang lệch.

## Chính sách rủi ro

Với mỗi thị trường chưa tất toán, nhánh Up giả định mọi lệnh bán Up đang treo khớp hết và không lệnh bán Down nào khớp. Nhánh Down làm ngược lại. Đây là kiểu khớp một phía, một phần, hoặc đồng thời theo hướng bất lợi — không phải kịch bản cả hai phía cùng khớp (kịch bản đó thường đỡ hơn).

```text
đóng góp Up = kho Up + Σ (tiền bán đã làm tròn xuống − phí dự phòng làm tròn lên − size)
worst-case  = cash − dự phòng làm tròn toàn sổ + Σ min(nhánh Up, nhánh Down) − dự phòng thị trường đang mở
```

Phí dự phòng dùng công thức taker công bố cho crypto, `size × risk_fee_rate × p × (1 − p)`, làm tròn lên 0.00001. Tài liệu Polymarket hiện nói maker không bị tính phí; sổ PnL gốc vì vậy dùng `maker_fee_rate = 0`. Khoản 0.07 vẫn bị trừ trong worst-case vì mô hình giấy không chứng minh lệnh sẽ là maker, và vì biểu phí có thể đổi. Sau khi khớp giấy với phí maker bằng 0, phần dự phòng đó được nhả, nên worst-case không tệ hơn lúc vừa treo lệnh.

Nếu đã có phe thắng tin cậy, worst-case chỉ còn nhánh của phe đó, vẫn gồm các lệnh bán phe thắng chưa hủy xong. `REDEEM` chỉ chạy khi không còn lệnh treo.

Lệnh ở trạng thái queued, live, hoặc cancel_requested đều bị tính. Hủy không xong ngay. Feed đứt thì mốc xác nhận hủy bị xóa và chỉ đặt lại sau khi nối lại cộng `cancel_latency`.

Khóa sau được ghi vào `state.json` lúc tạo sổ. Lần chạy sau có thể siết chiến thuật (edge, cooldown) nhưng **không** hạ sàn, không tăng vốn, không nới ngân sách từng thị trường, không rút ngắn trễ hủy, và không đổi `fill_mode` đã khóa:

| Khóa | Mặc định |
| --- | --- |
| `initial_capital` | 1000 |
| `max_loss_fraction` | 0.03 |
| sàn / ngân sách lỗ | 970 / 30 USDC |
| `per_market_budget` | 40 USDC gộp đã `SPLIT` mỗi thị trường; `MERGE` không hoàn hạn mức này |
| `max_open_markets` | 2 |
| `operational_reserve` | 0.05 USDC mỗi thị trường đang mở, trừ trong worst-case, nhả khi redeem |
| `global_rounding_reserve` | 0.01 USDC, luôn trừ trong worst-case |
| `risk_fee_rate` | 0.07 |
| `maker_fee_rate` | 0 |
| `ack_latency_ms` | 250 |
| `cancel_latency_ms` | 500 |
| `fill_mode` | `queue` |

Chiến thuật mặc định thêm: clip 5, min size 5, tick 0.01, `min_edge` 0.01, `skew_size_per_share` 1, `skew_edge_per_share` 0.002, spread tối đa 0.10, sổ lệnh cũ sau 3000 ms, lệch đồng hồ chấp nhận 5000 ms (`clock_skew_ms`, vì timestamp sàn có thể đi trước máy local), cooldown 500 ms, ngừng chào 20000 ms trước giờ kết thúc, `SPLIT` mỗi lần tối đa 20 khi kho cặp tự do dưới 10. Vòng paper lấy đồng hồ phiên làm mốc sổ cái; timestamp sàn chỉ dùng để biết sổ lệnh và print cũ hay mới.

Rebate maker **không** vào cash, không vào realized PnL, không vào sàn. Ước tính riêng, không được đảm bảo: `rebate_share × size × published_taker_fee_rate × p × (1 − p)` với 0.20 và 0.07. Chương trình rebate thật chia một pool, và kỳ nghỉ phí có thể trả 0. Con số này chỉ để tách biệt, không phải tiền sẽ nhận.

Best-case trong summary là cận trên trong mô hình (khớp phía thua, giữ phía thắng, phí maker cấu hình, không cộng rebate). Nó không phải định giá thị trường.

## Mô hình khớp giấy

Một lần giá chạm mức không phải là một lần khớp maker.

`fill_mode = queue` (mặc định):

- Lệnh chỉ có thể khớp khi đã `live` hoặc đang `cancel_requested`.
- Chỉ tính print có `taker_side = BUY` (có người nâng ask). Print `SELL` bị bỏ.
- Giá print thấp hơn giá bán của mình thì không khớp. Giá print cao hơn cũng không khớp, trừ khi bật `trade_through_fills` (mặc định tắt). Không tự gán phần khớp đẹp hơn giá limit.
- Khối lượng print trừ `queue_ahead` trước. Khi tham gia đúng mức ask, hàng chờ bằng khối đang treo ở mức đó cộng `extra_queue_shares`. Khi cải thiện giá trong spread, hàng chờ bằng `improve_queue_shares` (mặc định 5) cộng `extra_queue_shares`. Khi đứng sau best ask vì cần thêm edge, hàng chờ chỉ bằng `extra_queue_shares`.
- Phần còn lại mới khớp, theo thứ tự thời gian gửi, không vượt size còn lại và không bán khống.

`fill_mode = none`: bỏ mọi print. Sự kiện `adverse_fill` chỉ dùng trong fixture, bắt buộc `label = synthetic`, và bị từ chối nếu thị trường không phải synthetic.

Print cũ hơn lúc lệnh `live` không khớp. `trade_id` trùng thì bỏ. Sổ lệnh có timestamp cũ hơn snapshot đã nhận thì bỏ. Hash khác ở cùng timestamp thì đánh dấu không chắc và ngừng chào.

## Dữ liệu công khai

Paper mode chỉ GET, không body, không header xác thực, và chỉ tới:

- `https://gamma-api.polymarket.com/markets/slug/{slug}` để tìm thị trường. Slug mặc định `btc-updown-5m-{unix đầu cửa sổ 5 phút}`.
- `https://clob.polymarket.com/book?token_id=` cho sổ lệnh.
- `https://data-api.polymarket.com/v2/trades?condition=` cho print.
- `https://data-api.polymarket.com/v2/resolutions?condition=` cho kết quả.

Phiên bản Gamma `v1` dùng `clobTokenIds`, `v2` dùng `positionIds`. Phiên bản khác bị bỏ. Chỉ nhận đúng hai nhãn Up và Down; Yes/No bị bỏ. Token map theo nhãn, không theo “phần tử đầu là Up”.

Kết quả được nhận chỉ khi một trong các điều sau đúng:

- Data API: `status = resolved`, loại binary nếu có ghi, payout đúng `[1000000, 0]` hoặc `[0, 1000000]` micro-USDC, rồi map theo index outcome.
- Khung `market_resolved`: `winning_asset_id` và `winning_outcome` cùng trỏ một token đã biết.
- Fixture: `source = fixture`, `label = synthetic-fixture`, và thị trường được đánh dấu synthetic.

Giá 0.99 không phải kết quả. Payout lạ, hai nguồn mâu thuẫn, hoặc fixture gắn vào thị trường public thì không `REDEEM`.

Vòng paper dùng REST có `duration-seconds` và `max-markets`. Không có daemon vô hạn trong CLI. Khung websocket công khai (`book`, `last_trade_price`, `market_resolved`) có bộ chuẩn hóa để replay offline; vòng paper không mở websocket.

## Sổ sách và log

`SPLIT` trừ USDC, cộng cùng lượng Up và Down. `MERGE` chỉ được trên phần tự do (kho trừ phần lệnh đang treo) và cộng lại USDC. `REDEEM` cộng kho của phe thắng và đốt phe thua. Realized PnL là tổng `net_cash_flow` của thị trường đã redeem. Worst/best trong summary là vốn cuối kỳ mô phỏng, gồm cả lỗ đã realized.

Mỗi sự kiện ghi `logs/events.jsonl` và `logs/events.csv`. Cuối phiên có `summary.json`. `state.json` được ghi bằng cách thay file. Chạy lại cùng thư mục `--out` sẽ nạp sổ cũ; `event_id` và `trade_id` đã thấy sẽ không áp dụng lần nữa.

## Cách chạy

Từ thư mục `paper_split_maker` (Python 3.10+):

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
python3 -m paper_split_maker demo --config config.example.json --out /tmp/psm-demo
python3 -m paper_split_maker replay \
  --config config.example.json \
  --scenario paper_split_maker/scenarios/symmetric_demo.json \
  --out /tmp/psm-replay
python3 -m paper_split_maker paper \
  --config config.example.json \
  --duration-seconds 20 \
  --max-markets 1 \
  --out /tmp/psm-paper
```

Chạy pytest trong thư mục này. `conftest.py` bảo suite gốc của repo (lệnh `pytest` ở thư mục cha) bỏ qua cây này, để package `tests` của keeper không bị đè.

`demo` và `replay` chạy offline. File kịch bản phải có `label` chứa `synthetic` hoặc `fixture`. `symmetric_demo.json` là sổ đối xứng giả: ask 0.52 hai phía, hàng chờ 10, sau đó print mua vượt hàng chờ. Kỳ vọng của fixture này là cash 1000.20 và realized 0.20 trước rebate ước tính 0.034944. Đó là số của fixture, không phải kết quả live.

`paper` thoát khi hết thời lượng. Lần chạy đầu tạo `state.json`. Muốn sổ mới thì dùng thư mục `--out` khác. Không có cờ reset.

### Replay lịch sử (phản sự thật, không phải kết quả live)

`historical-replay` chạy sổ giấy trên 10 thị trường BTC Up/Down 5 phút lấy từ dữ liệu public. Snapshot đã lưu nằm ở `data/bkk_2026-11-07.snapshot.json`, nhãn `reconstructed-from-public-prints`. Cửa sổ là ngày 7 tháng 11 năm 2026 theo giờ Bangkok (UTC+7), tức 2026-11-06T17:00:00Z đến 2026-11-07T17:00:00Z.

Không có sổ lệnh lịch sử public. `GET /book` chỉ là sổ đang mở và trả 404 sau khi thị trường đóng. Replay dựng lại top-of-book từ print Data API `/v2/trades`, đối chiếu giá 1 phút `/v2/prices-history`, và chỉ tất toán bằng payout `/v2/resolutions`. Giả định nằm trong snapshot: ask là print BUY cuối làm tròn lên tick, bid là print SELL cuối làm tròn xuống; nếu spread quan sát nhỏ hơn 0.02 thì bid bị hạ để spread thành 0.02, nhằm đứng vào ask với hàng chờ 5 shares (cùng mức `improve_queue_shares`) thay vì treo phía sau với hàng chờ 0. Print BUY đúng giá, sau khi trừ 5 shares, mới khớp. Print SELL không khớp lệnh bán. Print giá cao hơn không xuyên qua (`trade_through_fills` = false). Giá history nằm ngoài bid/ask quan sát thì sổ bị coi là không chắc và bot không báo giá mới.

Cấu hình `config.historical200.json` dùng vốn 200 USDC. Trần lỗ 3% không đổi: ngân sách 6 USDC, sàn 194, không nới sau lỗ hay sau restart. Giới hạn theo thị trường được thu nhỏ theo tỷ lệ, không nới sàn: `per_market_budget` 8 (cùng tỷ lệ 4% như 40/1000), `split_clip` 5 (20 nhân 200/1000 ra 4, nâng lên đúng size tối thiểu 5), `min_pair_inventory` 5. Mọi thông số rủi ro còn lại giữ mặc định, gồm `max_open_markets` 2. Mười phiên không chồng lên nhau nên sổ chỉ mở một thị trường tại một thời điểm.

```bash
python3 -m paper_split_maker historical-replay \
  --config config.historical200.json \
  --snapshot data/bkk_2026-11-07.snapshot.json \
  --out /tmp/psm-historical
```

Lệnh `historical-fetch` tải lại snapshot từ API public (GET, không thân, không khóa). Kết quả là phản sự thật trên sổ dựng lại. Nó không chứng minh khớp lệnh thật và không chứng minh lợi nhuận live.

```bash
python3 -m paper_split_maker historical-fetch \
  --snapshot data/bkk_2026-11-07.snapshot.json
```

Mã thoát 2 là lỗi cấu hình hoặc lỗi đọc file. Phiên paper kết thúc đúng hạn kể cả khi API public lỗi; `summary.json` có `public_data_ok`.

## Bối cảnh ví đã được kể lại

Các số sau là quan sát người dùng cung cấp về một ví khác, không phải kết quả của bot này và không phải dữ liệu khớp cho lệnh của chúng ta: 570 lệnh SELL, 27 vòng đã đóng, dòng tiền ròng +3.42914214, 14 vòng dương và 13 vòng âm, tốt nhất +15.12, tệ nhất −14.55, trước chi phí và rebate bên ngoài. Đó là dòng tiền theo vòng, không phải suất sinh lời tài khoản. Trong cửa sổ 7 ngày ví đó chủ yếu maker nhưng có 43 lệnh taker, nên không phải lúc nào cũng maker. Hành vi được dùng làm gợi ý cấu trúc (SPLIT cân, bán clip, MERGE phần chưa đụng, để phần dư tất toán), không phải để suy một công thức fair-value.

## Hạn chế đã biết

- Không mô phỏng drawdown mark-to-market, trượt giá ngoài print, gas, reorg, hay oracle sai.
- Hàng chờ giấy là giả định (khối đang thấy tại mức giá, cộng một lượng cấu hình). Tape công khai không cho biết vị trí thật của một lệnh chưa từng được gửi.
- Chỉ một lệnh bán hiệu lực mỗi phía. Không có lưới nhiều mức.
- Sổ 5 phút thường rất rộng; khi spread vượt 0.10 bot đứng ngoài. Đó là cố ý.
- `MERGE` và `SPLIT` là bút toán. Không có giao dịch on-chain.
- Làm tròn size và cash về 0.000001. Size lệnh mặc định về bước 0.01.
- Rebate là ước tính thô, có thể cao hoặc thấp hơn tiền thật, và có thể bằng không.
- Sửa tay `state.json` làm hỏng sổ. Chương trình từ chối chạy nếu worst-case đã dưới sàn.
- Kiểm thử ngẫu nhiên có hạt số là kiểm tra bất biến của mô hình, không phải bằng chứng về lỗ live.

## Kiểm thử

`python3 -m pytest -q` trong thư mục này. Bộ test offline gồm thị trường đối xứng, sổ 0.99/0.01 rồi đảo chiều, khớp một phía, khớp treo đồng thời, trễ hủy, lỗ realized cộng dồn, trần nhiều thị trường, phí và dự phòng, MERGE đụng hàng đang giữ chỗ, feed cũ, nối lại kèm sự kiện trùng, khởi động lại, hết hạn chưa có kết quả, và một vòng hành động ngẫu nhiên có hạt. Không bài nào trong số đó chứng minh lỗ live ≤ 3%.
