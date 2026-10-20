# IPAM portable Docker — 2026.10.20.1

Bộ cài giữ nguyên mã nguồn và giao diện của IPAM Production tại thời điểm ghi trong `release.json`, gồm IP Address, VLAN/Subnet, Scanner, SNMP Discovery, MAC/Port History, Topology, Physical Port Mapping, Server Connection, HA Management, Diagrams, Export Center, HTML offline và PDF.

27 file source/static gốc được đối chiếu SHA-256 với container `ip-address-trunk-modes-20261020-r3`. Khi build, image nhận thêm một bản vá một dòng: xử lý literal `%` cả khi danh sách tham số SQL rỗng, để Endpoint History chạy được trên database mới. `distribution_patches` trong `release.json` ghi hash trước/sau; source snapshot và server Production không bị sửa. Dependency Python được khóa theo runtime. Database mới có đầy đủ 46 bảng nhưng không chứa bản ghi Production. Không đóng gói `.env`, tài khoản, SNMP community, session, inventory mạng, bản đồ đã lưu hoặc database backup của hệ thống đang chạy.

## Build và cài mới

Yêu cầu Docker Engine/Desktop, Docker Compose v2 và Python 3. Source public này không yêu cầu quyền tải image IPAM private trên GHCR.

```sh
cd /Users/hoangan
git clone https://github.com/hoangankg797521/Docker-Source.git
cd Docker-Source/ipam
python3 scripts/configure.py
```

Nếu repository đã tồn tại, vào repository đó thay vì clone đè. File `.env` chỉ được tạo khi chưa có, quyền `600`; mật khẩu admin tại `IPAM_ADMIN_PASSWORD`. Không chia sẻ hoặc commit file này.

Trên Mac/Docker Desktop:

```sh
docker compose -f compose.bridge.yaml up -d --build --wait
docker compose -f compose.bridge.yaml ps
```

Trên Linux cần host networking cho ARP/scanner:

```sh
docker compose up -d --build --wait
```

Mở http://localhost:8088 trên Mac hoặc `http://IP-MAY-CHU:8088` trên Linux. Đăng nhập `admin` với mật khẩu đã tạo riêng trong `.env`. Database ban đầu trống; khai báo thiết bị/SNMP/subnet tại site mới. Bridge trên Docker Desktop giới hạn ARP cùng lớp 2.

Image mặc định `ipam:local` được build từ source thư mục này. PostgreSQL đi kèm Compose. Volume `postgres-data` và `app-data` giữ dữ liệu; không dùng `down -v` nếu cần bảo toàn dữ liệu.

Để build sạch riêng image:

```sh
docker build --pull --no-cache -t ipam:local .
```

Gói GHCR cũ `ghcr.io/hoangankg797521/ipam:2026.10.20.1` vẫn là kênh phát hành riêng; việc công khai source tại đây không tự đổi quyền của package đó.

## Cấu hình và truy cập

- `.env`: image/version, cổng, thông tin PostgreSQL, mật khẩu admin ban đầu, `DHCP_SYNC_TOKEN`, cookie và session.
- `IPAM_ADMIN_PASSWORD` chỉ tạo admin khi chưa có tài khoản; restart/upgrade không reset mật khẩu đã đổi trong UI.
- SNMP credentials và DHCP agent phải khai báo riêng tại site mới. DHCP sync token trong agent phải trùng giá trị site này.
- Qua HTTPS reverse proxy, đặt `AUTH_SECURE_COOKIE=1`. Với truy cập HTTP trực tiếp dùng `0`.
- UI/logic thời gian giữ hành vi bản Production hiện tại; `TZ` cấu hình timezone của container.
- App dùng `NET_RAW`/`NET_ADMIN` cho scanner; không cần `privileged: true`.

## Backup và restore

```sh
sh scripts/backup.sh
# Hoặc chỉ định thư mục mới:
sh scripts/backup.sh backups/site-a-before-upgrade
```

Backup gồm PostgreSQL custom dump, dữ liệu `/data`, image ID và bản `.env` của site. Thư mục tạo với quyền riêng tư, cần lưu ở nơi được bảo vệ. Nếu dùng bridge:

```sh
IPAM_COMPOSE_FILE=compose.bridge.yaml sh scripts/backup.sh
```

Restore sẽ thay nội dung database bằng backup và giải nén file `/data` lên volume hiện tại. Chỉ chạy khi đã chọn đúng site/backup:

```sh
sh scripts/restore.sh backups/site-a-before-upgrade --replace-database
```

Script dừng app trước restore và giữ nguyên `.env` đang sử dụng. Khi khôi phục sang máy mới, dùng bản `installation.env` trong backup làm `.env` trước khi dựng PostgreSQL. Backup dữ liệu và việc vận chuyển credential là thao tác riêng của quản trị viên; chúng không nằm trong image phát hành.

## Upgrade và rollback

1. Backup.
2. Đổi `IPAM_IMAGE` trong `.env` sang version mới hoặc digest được cung cấp trong `image-digest.txt`.
3. `docker compose pull app && docker compose up -d --no-deps --wait app`.
4. Kiểm tra health, đăng nhập và các module cần dùng.

Rollback code: đặt lại image version/digest trước đó rồi chạy lại bước 3. Nếu một release yêu cầu thay schema, làm theo migration/restore của release đó. Bootstrap chỉ khởi tạo database hoàn toàn trống; database có sẵn nhưng thiếu bảng sẽ dừng và yêu cầu migration được duyệt, không tự xóa hoặc tạo lại dữ liệu.

## Build và xác minh

```sh
python3 scripts/verify_package.py
docker build -t ipam:local .
python3 tests/run.py --image ipam:local
```

Test tạo Compose project và volume tạm có tên ngẫu nhiên, kiểm tra cài mới, 20 API/module, UI assets, PDF tiếng Việt, từ chối default password, restart, backup và restore. Test không dùng database Production và không kích hoạt network scan. Volume test được dọn khi xong. Kết quả ở `tests/output/`, không đưa vào image.

Bản GHCR trước đây được build/test đa kiến trúc bằng workflow tại repository Private; repository Public này cung cấp source để tự build local. Chưa kiểm thử chạy native ARM64 trên phần cứng thật.

Version release bất biến. Khi đổi code/schema, cập nhật snapshot/hash/version trong `release.json` trước khi publish phiên bản tiếp theo. Không dùng các image tag cục bộ của server Production làm base phát hành.
