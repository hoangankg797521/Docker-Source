# Docker Source

Các gói Docker độc lập; mỗi thư mục có hướng dẫn, cấu hình và dữ liệu riêng.

| Thư mục | Image build local |
|---|---|
| [npm](npm/) | `local/npm:2.15.1-ddns.2` (Nginx Proxy Manager + DDNS) |
| [homepage](homepage/) | `homepage:latest`, `homepage:editor` |
| [uptime-kuma](uptime-kuma/) | `uptime-kuma:2.5.6` |
| [meshcentral](meshcentral/) | `meshcentral:1.2.6` |
| [ipam](ipam/) | `ipam:local` |
| [wireguard](wireguard/) | `ghcr.io/hoangankg797521/wg-easy:1.0.0`, `wg-noc:1.0.0`, `wg-acl:1.0.0` |

Homepage có hai target dashboard/editor nên dùng hai tag của cùng tên image. Tên Compose project Homepage giữ nguyên `homepage-portable` để không thay đổi định danh volume của bản cài hiện có.

## IPAM trên Mac

```sh
cd /Users/hoangan
git clone https://github.com/hoangankg797521/Docker-Source.git
cd Docker-Source/ipam
python3 scripts/configure.py
docker compose -f compose.bridge.yaml up -d --build --wait
```

Nếu đã clone, cập nhật bằng `git pull --ff-only` khi working tree sạch rồi vào thư mục `ipam`. Script configure từ chối ghi đè `.env` có sẵn. Mở http://localhost:8088; tài khoản `admin`, mật khẩu riêng trong `.env`.

Không đưa `.env`, database hoặc backup runtime lên Git. Gỡ file khỏi phiên bản hiện tại không xoá file khỏi lịch sử Git cũ.
