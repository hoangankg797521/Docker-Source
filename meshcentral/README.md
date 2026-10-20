# MeshCentral Custom Docker

Bộ source để build lại MeshCentral với giao diện và các tính năng custom đang chạy ổn định trên hệ thống hiện tại. Image dựa trên MeshCentral chính thức phiên bản `1.2.6` và giữ toàn bộ file tùy chỉnh trong thư mục `custom/`.

## Thành phần được đóng gói

- Giao diện quản trị và đăng nhập đã tùy biến.
- Nhập liệu bàn phím từ xa hỗ trợ IME/Telex trên macOS, xử lý phím tắt và Unicode.
- Quay màn hình desktop thành MP4/H.264, xem trước và tải video.
- Phần mở rộng MeshCentral nằm trong `custom/meshuser.js`.

Trang đăng nhập nhận tên tổ chức từ `MESH_BRAND_NAME`; tên này không bị đóng cứng theo bệnh viện hoặc máy chủ đang chạy.

## Cài ở máy chủ mới

Yêu cầu Docker Engine/Desktop, Docker Compose v2, kết nối Internet để tải image gốc và một tên DNS hoặc địa chỉ IP mà trình duyệt và các agent truy cập được.

```sh
git clone https://github.com/hoangankg797521/Docker-Source.git
cd Docker-Source/meshcentral
cp .env.example .env
```

Sửa `.env`:

```dotenv
HOSTNAME=meshcentral.example.com
MESH_BRAND_NAME=Your Organization
```

`HOSTNAME` phải là tên DNS hoặc địa chỉ IP thực tế dùng để truy cập MeshCentral, không gồm giao thức, cổng hay đường dẫn. Có thể đổi `HTTP_PORT` hoặc `HTTPS_PORT` nếu cổng host 80/443 đang được dùng.

```sh
docker build --build-arg MESHCENTRAL_VERSION=1.2.6 -t meshcentral-custom:1.2.6 .
docker compose up -d
docker compose ps
docker compose logs -f meshcentral
```

Mở `https://<HOSTNAME>/`, chọn **Tạo tài khoản** và tự đặt tên đăng nhập, mật khẩu. Không có mật khẩu mặc định. Tài khoản đầu tiên tự động trở thành quản trị viên. Nút tạo tài khoản chỉ hiện khi server cho phép, gồm lúc hệ thống chưa có user. Đăng ký tài khoản mới sau đó bị tắt theo mặc định. Chỉ đặt `ALLOW_NEW_ACCOUNTS=true` nếu muốn cho phép người dùng tự đăng ký.

HTTPS trong container dùng cổng **443**, HTTP dùng **80**. Cổng **4433** trong log là Intel AMT, không phải cổng đăng nhập web. Compose mặc định ánh xạ `443:443` và `80:80`.

## Dữ liệu và cập nhật

Image không chứa database, tài khoản, chứng thư TLS, device records, file upload, backup hoặc `config.json` của production. Docker Compose tạo volume riêng cho dữ liệu, file người dùng, web assets và backup.

- `docker compose down` dừng dịch vụ nhưng giữ volume.
- `docker compose down -v` xóa toàn bộ volume và dữ liệu của máy đó.
- Đổi `.env` sau khi MeshCentral đã tạo `config.json` không ghi đè cấu hình đang lưu. Hãy sửa cấu hình đang dùng qua MeshCentral hoặc sao lưu rồi chỉnh `meshcentral-data/config.json`.
- Nếu mang theo một `config.json` có CSP riêng, hãy cho phép `blob:` trong `media-src` và hostname của máy mới trong `connect-src`; giữ lại các directive còn lại.

Wrapper trong image chép các template và `meshuser.js` vào đúng vị trí trước khi gọi entrypoint chính thức. Với bộ dữ liệu mới, nó tạo hostname, chính sách CSP cho kết nối WebSocket và phát video MP4, tên tổ chức trên trang đăng nhập, đồng thời tắt đăng ký công khai. Khi restart, volume dữ liệu hiện tại vẫn được giữ nguyên.

Để đổi phiên bản, cập nhật `MESHCENTRAL_VERSION` và `IMAGE_TAG` trong `.env`, xem thay đổi upstream trước, rồi chạy `docker compose up -d --build`. Các file custom hiện dựa trên MeshCentral 1.2.6 nên cần rà soát và ghép lại khi nâng major/minor version.

## Kiểm tra source

Các kiểm tra cục bộ dùng Node.js tích hợp, không cần thư viện ngoài:

```sh
node tests/test-ime-keyboard.cjs
node tests/test-desktop-video-mp4.cjs
node tests/test-prepare-runtime.cjs
```

Bản gói đã được build thành công trên Docker Desktop Intel (`linux/amd64`) và đã xác minh HTTPS cùng luồng tạo admin đầu tiên. Chưa xác minh build trên ARM64.

Build image trước khi triển khai:

```sh
docker compose build
```

## License

MeshCentral và các file source dẫn xuất từ upstream tuân theo Apache License 2.0; xem [LICENSE](LICENSE). Những file có thay đổi đã được đánh dấu. Upstream: [Ylianst/MeshCentral](https://github.com/Ylianst/MeshCentral).
