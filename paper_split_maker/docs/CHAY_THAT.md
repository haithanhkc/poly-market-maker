# Chạy thật trên máy của bạn

Logic đang dùng là logic cũ: mỗi phía một lệnh 5 shares, giá `max(ask, fair + edge)`, lệch kho nhẹ, không sàn giá cặp, không trần lỗ từng phiên. Vốn trong `config.live.old200.json` là 200 USDC. Sàn 3% là 194 USDC, khóa trong sổ, không nới sau lỗ hay sau khi mở lại. Chỉ một thị trường mở. Lệnh bán là post-only. File này không chứa khóa.

Chạy thử khô một lần ngắn là nên làm, không bắt buộc.

## 1. Cài đặt

Cần Python 3.10 trở lên. Mở terminal trong thư mục `paper_split_maker`.

Windows:

```bat
py -3 -m pip install -r requirements-dev.txt
py -3 -m pip install -r requirements-live.txt
```

macOS hoặc Linux:

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pip install -r requirements-live.txt
```

`requirements-live.txt` có client CLOB chính thức, `web3`, `websockets`, và gói `polymarket`.

## 2. Tạo `.env`

```bat
copy .env.example .env
```

```bash
cp .env.example .env
```

Điền trên máy bạn, không gửi file này cho ai và không commit:

- `POLY_PRIVATE_KEY`: khóa của địa chỉ ký.
- `POLY_FUNDER`: địa chỉ đang giữ pUSD. Trên polymarket.com, mở menu hồ sơ và chép địa chỉ ví.
- `POLY_SIGNATURE_TYPE`: xem mục dưới.
- `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`: để trống thì client tự suy ra.
- `RELAYER_API_KEY` và `RELAYER_API_KEY_ADDRESS`: bắt buộc nếu ví không phải EOA. Tạo ở Settings → API Keys → Relayer API Keys. Dòng địa chỉ hiện ra ở đó là địa chỉ ký.

`.env` đã nằm trong `.gitignore`.

## 3. Biết ví của mình là loại nào

So sánh hai địa chỉ:

- Địa chỉ ký: địa chỉ của `POLY_PRIVATE_KEY`. Sau khi tạo Relayer API key, Polymarket hiện địa chỉ này. Không dán khóa vào trang web lạ để “xem địa chỉ”.
- Địa chỉ quỹ: địa chỉ trong menu hồ sơ / ô nạp tiền. Đó là `POLY_FUNDER`.

Nếu hai địa chỉ giống nhau, ví là EOA. Đặt `POLY_SIGNATURE_TYPE=0`.

Nếu khác nhau, tiền nằm trong ví thông minh. Đặt:

| Bạn tạo tài khoản thế nào | `POLY_SIGNATURE_TYPE` |
| --- | --- |
| Email hoặc Google (Magic), ví tạo trước 4 tháng 5 năm 2026 | 1 POLY_PROXY |
| MetaMask, Rabby hoặc ví ngoài, tạo trước 4 tháng 5 năm 2026 | 2 GNOSIS_SAFE |
| Ví Polymarket tạo từ 4 tháng 5 năm 2026 trở đi | 3 POLY_1271 (deposit wallet) |

Banner lúc chạy in `POLY_FUNDER` và loại chữ ký. Đối chiếu với địa chỉ hồ sơ trước khi để lệnh chạy.

Việc nào chạy được:

- Đặt lệnh bán post-only và hủy lệnh: cả bốn loại, qua client CLOB, dùng `POLY_FUNDER` làm ví giữ tiền.
- Tách, gộp, redeem của EOA: giao dịch Polygon từ khóa của bạn. Bạn trả gas.
- Tách, gộp, redeem của proxy, Safe và deposit wallet: gói `polymarket` (`SecureClient.split_position`, `merge_positions`, `redeem_positions`) ký và gửi relayer. Cần hai biến `RELAYER_API_KEY` và `RELAYER_API_KEY_ADDRESS`. Không có hai biến đó thì bot không gửi yêu cầu chưa ký, và không có hàng để báo giá.

Collateral là pUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`. Bot chỉ `approve` khi allowance đang thiếu. Thị trường v1 dùng adapter `0xAdA100Db00Ca00073811820692005400218FcE1f`. Thị trường v2 dùng router `0x12121212006e4CD160D18e3f00711DA5c3372600`.

## 4. Kiểm tra số dư

Trên Polymarket, xem pUSD của đúng địa chỉ `POLY_FUNDER`. Bắt đầu bằng số nhỏ hơn 200 nếu bạn chưa chạy thật lần nào. Sàn trong file là 3% của 200, tức 194. Nếu bạn nạp ít hơn 200, sửa `initial_capital` cho đúng số bạn chịu mất 3%, rồi tạo thư mục `--out` mới. Sửa file sau khi sổ đã tạo không hạ được sàn đã khóa.

## 5. Chạy

Dry-run, tùy chọn, không ký và không gửi lệnh:

```bash
python3 -m paper_split_maker live \
  --config config.live.old200.json \
  --out /tmp/psm-dry \
  --duration-seconds 60 \
  --max-markets 1 \
  --live --dry-run --yes-i-understand-live-risk
```

Windows, đổi `python3` thành `py -3` và đường dẫn `--out` thành một thư mục bạn ghi được, ví dụ `%TEMP%\psm-dry`.

Chạy thật:

```bash
python3 -m paper_split_maker live \
  --config config.live.old200.json \
  --out /tmp/psm-live \
  --duration-seconds 300 \
  --max-markets 1 \
  --live --yes-i-understand-live-risk
```

Banner in địa chỉ quỹ, loại ví, sàn 194, và `taker_repair` tắt. Kho của phiên sau được tách khoảng 45 giây trước giờ mở. Chưa xác nhận thì không báo giá phiên đó.

## 6. Dừng

Ctrl+C: tiến trình gọi hủy mọi lệnh rồi thoát.

Hoặc tạo file kill switch đúng tên trong cấu hình, `kill.switch`, trong thư mục bạn đang đứng:

```bash
echo stop > kill.switch
```

```bat
echo stop> kill.switch
```

Xóa file này trước lần chạy sau, nếu không bot dừng ngay.

## 7. Log

Trong thư mục `--out`:

- `state.json`: sổ, gồm sàn đã khóa.
- `logs/events.jsonl` và `logs/events.csv`: từng lệnh, hủy, tách, khớp.
- `summary.json`: kết quả lúc thoát.

Không có khóa trong các file này.

## 8. Trước khi bật

- Tài khoản không có lệnh mở của bot khác. Lệnh lạ bị coi là lệch sổ và bị hủy hết.
- `POLY_FUNDER` trên banner trùng địa chỉ hồ sơ.
- `taker_repair` trong file là false.
- Bạn biết Ctrl+C và file `kill.switch`.
- Vốn nhỏ cho lần đầu.
- Dry-run một lần nếu bạn muốn xem dòng “would” trước. Bỏ qua cũng được.
