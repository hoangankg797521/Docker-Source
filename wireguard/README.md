# WireGuard custom: wg-easy + NOC Dashboard + Unified ACL

Bộ triển khai mới, giữ giao diện và tính năng custom của wg-easy 15.4.0 cùng NOC Dashboard. Không chứa tài khoản, khóa VPN, peer, lịch sử hoặc database của máy đang chạy.

| Image | Thành phần |
|---|---|
| `ghcr.io/hoangankg797521/wg-easy:1.0.0` | wg-easy 15.4.0, UI và API Unified ACL |
| `ghcr.io/hoangankg797521/wg-noc:1.0.0` | NOC Dashboard tiếng Việt, quản lý client, QR/config, trạng thái, lịch sử, cấu hình server, ACL |
| `ghcr.io/hoangankg797521/wg-acl:1.0.0` | Policy database/audit, nftables enforcement; cũng giữ network namespace |

## Cài mới trên Linux x86_64

Yêu cầu: Docker Engine + Compose v2, Python 3, Linux có kernel WireGuard/nftables. Linux VM phù hợp; LXC cần quyền/kernel từ host. Chưa xác nhận ARM64 hay VPN thực qua Docker Desktop macOS/Windows. Dự trù 2 CPU/2GB RAM; source build wg-easy cần nhiều RAM hơn.

```sh
git clone https://github.com/hoangankg797521/Docker-Source.git
cd Docker-Source/wireguard
python3 scripts/configure.py --endpoint vpn.example.com --scope 192.168.50.0/24 --dns 1.1.1.1
docker compose pull
docker compose up -d --wait
```

Thay endpoint bằng DNS/IP thực; scope bằng các mạng được phép truy cập, phân cách bằng dấu phẩy. Script từ chối ghi đè `.env`/`secrets` hiện có. Tài khoản mới: `wgadmin`; xem mật khẩu trực tiếp trong `secrets/bootstrap.env` trên máy cài, không đưa file này lên Git. Cả hai UI dùng cùng tài khoản wg-easy. NOC hiện dùng Basic Auth của wg-easy, không hỗ trợ tài khoản đã bật 2FA; đây là giới hạn của bản custom hiện tại. Đổi mật khẩu trong wg-easy sau khi đăng nhập; bootstrap chỉ có tác dụng với database trống.

Mặc định hai Web chỉ bind `127.0.0.1`. Từ máy quản trị:

```sh
ssh -N -L 51821:127.0.0.1:51821 -L 51822:127.0.0.1:51822 USER@SERVER
```

Mở wg-easy tại http://localhost:51821, NOC tại http://localhost:51822. Muốn dùng IP quản trị riêng, sửa `WEB_BIND_IP` và `WG_EASY_UI_URL` trong `.env` rồi chạy lại Compose; chỉ dùng HTTP trên mạng quản trị tin cậy hoặc sau reverse proxy HTTPS. `INSECURE=true` là yêu cầu cho HTTP nội bộ của wg-easy, không thay thế bảo vệ truy cập mạng.

Nếu GHCR package yêu cầu xác thực, dùng `docker login ghcr.io` với token `read:packages`, hoặc build từ source bằng `docker compose build && docker compose up -d --wait`. Repo public không đảm bảo package tự động public; owner có thể chỉnh visibility từng package trên GitHub Packages.

## Hoạt động VPN và ACL

- Forward/NAT UDP `VPN_PORT` trên router/firewall tới cùng cổng trên máy Docker. Không cần công khai cổng Web hoặc ACL API8089.
- wg0 dùng `10.255.66.0/24` ở bản này; không đổi subnet trong database hoặc bootstrap riêng lẻ vì adapter và engine kiểm tra subnet. Docker bridge tự chọn subnet; kiểm tra không trùng các LAN/VPN cần dùng.
- Peer mới mặc định **Deny All**. Tạo client, mở Access Control, chọn Custom ACL hoặc Allow All trong phạm vi `ACL_SCOPE`, lưu và kiểm tra Apply Status.
- Custom ACL có IP/subnet, TCP/UDP/ICMP/Any, port/range, Enable, audit và revision conflict. Thu hồi policy áp dụng cả phiên đang tồn tại. Allow All chỉ cho các mạng trong scope.
- AllowedIPs là route trên client, ACL là quyền server: cần đủ cả hai. Có thể cấp DNS TCP/UDP53 trong scope nếu dùng DNS nội bộ; DNS Internet ngoài scope sẽ bị chặn khi được định tuyến vào VPN.
- IPv6 forwarding tắt. Routed/site-to-site peers với serverAllowedIps không được hỗ trợ trong chế độ Unified ACL.
- Muốn đổi scope: sửa `ACL_SCOPE`, kiểm tra policy hiện hữu, rồi recreate `acl`. Đổi scope không tự thêm route trên thiết bị, không tự sửa AllowedIPs toàn cục. Policy custom hiện có cần được quản trị viên rà soát lại khi thu hẹp scope.
- Không dùng legacy import trên cài mới: `config/legacy-ids.json` để `[]`.
- CPU/RAM/disk dashboard lấy từ mount hệ thống Linux; khi chạy trong VM là thông số VM.

## Build, lưu file image và cài offline

```sh
docker compose build
# File tar chứa đủ ba image (namespace dùng lại image ACL).
docker save -o wireguard-custom-1.0.0-amd64.tar \
  ghcr.io/hoangankg797521/wg-easy:1.0.0 \
  ghcr.io/hoangankg797521/wg-noc:1.0.0 \
  ghcr.io/hoangankg797521/wg-acl:1.0.0
```

Ở nơi mới, copy thư mục `wireguard` và tar, chạy configure với thông tin nơi mới, `docker load -i wireguard-custom-1.0.0-amd64.tar`, rồi `docker compose up -d --pull never --wait`. Không copy secrets hoặc volumes của nơi cũ nếu muốn cài mới. Image chứa mã ứng dụng; compose và secrets được cấu hình lúc chạy. Không lưu image tar lớn trực tiếp trong Git; dùng GHCR hoặc GitHub Release assets.

Repository không còn workflow tự động build/publish image. Các image `1.0.0` đã xuất bản trên GHCR vẫn có thể dùng; khi thay đổi source, build thủ công theo hướng dẫn phía trên và chọn tag mới. Ưu tiên pin digest cho production.

## Dữ liệu, nâng cấp và rollback

Ba named volumes độc lập: `wg-config` (keys/database), `acl-data` (policy/audit), `noc-data` (history). Dừng stack bằng `docker compose down` giữ volumes; **không dùng `down -v`** nếu muốn giữ dữ liệu. Lưu cả `.env`, `secrets/` và bản sao ba volumes trong backup riêng có bảo vệ. Không publish backup.

Bộ này dành cho cài mới. Không trỏ vào volumes production hiện tại hoặc thay stack hiện tại mà chưa có kế hoạch migration/backup. Trước upgrade: dừng stack trong cửa sổ bảo trì, backup nhất quán volumes, ghi lại image digests, thay image rồi kiểm tra. Rollback về digest trước; nếu schema đổi, dùng bản backup phù hợp khi stack đã dừng.

## Nguồn và giấy phép

wg-easy upstream v15.4.0, commit `2f9751e302acf78fd560417fd2dfbf224b2174c8`, runtime base pinned digest trong Dockerfile. Source custom đầy đủ nằm ở `wg-easy/src`; NOC và ACL ở hai thư mục tương ứng. Bản phân phối này theo AGPL-3.0, giữ nguyên LICENSE và thông báo upstream. Cần giữ source tương ứng với image khi phân phối/sửa đổi.
