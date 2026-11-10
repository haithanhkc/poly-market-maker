# Chạy

Hướng dẫn chạy nằm trong [README.md](../README.md). Một lệnh, hai file cấu hình: `config.paper.json` và `config.live.json`.

PowerShell, repo `C:\Users\danny\Desktop\poly-market-maker`, venv `.venv`:

```powershell
cd C:\Users\danny\Desktop\poly-market-maker
.\.venv\Scripts\Activate.ps1
cd paper_split_maker
python -m paper_split_maker --config config.live.json --out ..\runs\live --max-markets 3 --live --yes-i-understand-live-risk
```

File `config.live.run14.json` chỉ có trên máy chủ. Cách chuyển các khóa cũ sang `config.live.json` nằm ở mục "Migrate" trong README. Dùng thư mục `--out` mới sau khi đổi vốn hoặc trần lỗ. Tạo file `kill.switch` trong thư mục đang chạy để dừng.
