# Nginx Proxy Manager 2.15.1 + DDNS Access Lists

Gói tái triển khai từ container đang chạy, chụp ngày 21/10/2026. Image `local/npm:2.15.1-ddns.2`, SQLite, ports 80/81/443, bind mounts `data/` và `letsencrypt/`. Giữ DDNS resolvers, timezone và Docker log rotation của production.

## Triển khai mới

Cần Docker Engine + Compose v2 trên Linux, ports 80/81/443 còn trống.

```sh
cd npm
docker compose build
docker compose up -d
```

Mở `http://IP-MAY-MOI:81` để tạo tài khoản theo giao diện NPM. Không có tài khoản hay mật khẩu production trong repository.

## Khôi phục đầy đủ hệ thống hiện tại

GitHub public chỉ chứa ứng dụng/cấu hình triển khai. Cần lấy riêng hai file `npm-state-20261021.tar.gz` và `npm-image-2.15.1-ddns.2.tar.gz` từ bản backup riêng. Database chứa users, password hashes, proxy hosts, upstreams, ACL/DDNS, certificates và cấu hình; volumes chứa khóa ký, chứng chỉ/khóa SSL, ACME account, logs và cấu hình Nginx.

Trên máy mới, **trước khi chạy Compose**, dùng thư mục npm chưa có data/letsencrypt. Cần Python 3.12+ cho tar extraction filter:

```sh
sudo python3 restore.py /secure/npm-state-20261021.tar.gz
gzip -dc /secure/npm-image-2.15.1-ddns.2.tar.gz | docker load
docker compose up -d --no-build --pull never
docker exec nginx-proxy-manager nginx -t
docker compose ps
```

Image backup là image chính xác đã chạy. Tài khoản đăng nhập và cấu hình được giữ trong SQLite. Đổi DNS/NAT về máy mới sau khi kiểm tra từng Proxy Host, SSL, Access List và DDNS; các upstream nội bộ phải truy cập được từ mạng mới. Không để hai máy cùng dùng ACME account để gia hạn khi chuyển đổi. Có thể giữ nguyên máy cũ để rollback DNS/NAT, không xóa volumes cũ.

Snapshot còn lưu Compose gốc, docker inspect, image inspect, runtime config `/etc/nginx`, `/app/config`, `/etc/letsencrypt.ini`, checksum và marker VERIFIED. Restore tự động chỉ khôi phục volumes; runtime config dùng để đối chiếu khi chuyển môi trường, không tự ghi đè vào image mới. Nếu có sửa runtime ngoài image, cần đối chiếu trước cutover.

## Thành phần public và cách build

`assets/runtime-overlay.tar.gz` chứa backend DDNS, migration, template, startup hook và frontend đã build, xuất trực tiếp từ container đang chạy; không có volumes, database hoặc khóa SSL. Dockerfile áp overlay lên base image upstream cố định bằng digest. `assets/frontend-source.tar.gz` chứa frontend source và LICENSE từ source build có trên host; đây là source tham chiếu, runtime overlay là bản ứng dụng xuất trực tiếp. Không khẳng định frontend source này tái build bit-for-bit với image hiện tại.

Không chạy `docker compose build` khi muốn dùng nguyên image backup; dùng `--no-build --pull never`. Không tự nâng cấp upstream `latest`, vì DDNS là tùy biến riêng. `SHA256SUMS` xác minh các archive public bằng `sha256sum -c SHA256SUMS`.

## Kiểm chứng bản chụp

Snapshot dùng SQLite Backup API; integrity_check; so sánh data_version; kiểm tra cấu hình/chứng chỉ không thay đổi trong lúc chụp; giải nén và kiểm tra database khôi phục. `nginx -t` production thành công sau snapshot. Chưa diễn tập chạy toàn bộ ở máy đích khác; cần kiểm tra kết nối upstream/DNS, SSL và DDNS sau triển khai.

Giữ các bản backup ở nơi riêng tư (quyền 0600), tuyệt đối không commit lên repository public. Hạn chế truy cập port quản trị 81 theo mạng quản trị phù hợp. Giấy phép upstream nằm trong frontend-source archive.
