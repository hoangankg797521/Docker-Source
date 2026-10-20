# Homepage Portable — giao diện và Visual Editor

Bộ source độc lập để lưu trên GitHub và build Docker tại máy khác. Giữ giao diện sáng/tối, nút bút Edit, kéo thả thẻ/nhóm, chỉnh số cột và đổi hai tiêu đề. Dịch vụ và bookmark ban đầu trống; không kèm cấu hình, tài khoản, token, log, backup hoặc dữ liệu từ các máy cũ.

Dùng hai container: [Homepage v1.13.2](https://github.com/gethomepage/homepage/releases/tag/v1.13.2) và editor Node 22. Chỉ editor xuất cổng ra ngoài; Homepage kết nối qua mạng Compose. Cần Docker Engine/Desktop với Docker Compose v2 và Internet để tải base image lần đầu. Có thể chạy CPU amd64/arm64 nếu base image hỗ trợ nền tảng đó.

## Build và chạy lần đầu

Mở terminal trong thư mục này:

```sh
cp .env.example .env
# Sửa .env theo địa chỉ thực tế trước khi chạy.
docker compose build
docker compose run --rm setup
docker compose up -d
docker compose ps
```

Lệnh setup yêu cầu nhập mật khẩu mới hai lần, không hiển thị ký tự. Tài khoản Edit là `admin`; chỉ hash scrypt được lưu vào volume riêng. Setup chỉ thêm file cấu hình còn thiếu và từ chối chạy lại nếu đã có tài khoản, tránh ghi đè dữ liệu.

Với cấu hình mặc định, mở http://localhost:3000. Nhấn biểu tượng bút, đăng nhập, tạo nhóm và thêm dịch vụ. Dashboard xem được không cần đăng nhập; các thao tác sửa yêu cầu đăng nhập.

## Đổi IP, domain hoặc cổng

Ví dụ minh họa khi truy cập qua LAN và domain:

```dotenv
PORT=3000
PUBLIC_ORIGINS=http://192.168.1.50:3000,https://dashboard.example.com
HOMEPAGE_ALLOWED_HOSTS=192.168.1.50:3000,dashboard.example.com,localhost:3000
```

Thay các địa chỉ ví dụ bằng địa chỉ của bạn. `PUBLIC_ORIGINS` phải khớp chính xác URL trình duyệt (protocol + host + port, không có đường dẫn hoặc dấu `/` cuối). `HOMEPAGE_ALLOWED_HOSTS` chỉ chứa host và port, ngăn bằng dấu phẩy. Khi đổi `PORT`, cập nhật cả hai danh sách. Sau khi đổi `.env`, chạy `docker compose up -d`.

Nếu dùng reverse proxy, trỏ proxy vào cổng editor, mặc định 3000; cho phép truy cập cùng origin vào `/editor/*` và `/api/revalidate`. TLS do reverse proxy cung cấp. Không mở riêng cổng Homepage.

## Lưu trên GitHub và triển khai ở nơi khác

Chỉ đưa nội dung thư mục source này vào repository. `.env` và các thư mục chứa dữ liệu/secret đã được ignore. Dockerfile dùng COPY theo danh sách cụ thể và `.dockerignore` giới hạn build context. Không đưa file `auth.json`, export cấu hình từ máy cũ hoặc bản backup vào repo.

Máy mới: clone repository, làm các bước build/setup/up phía trên. Mỗi máy có volume riêng và đặt mật khẩu riêng. Không cần sao chép dữ liệu từ máy đang chạy.

## Dữ liệu và vận hành

- Volume `homepage-portable_config`: YAML, custom CSS/JS và dữ liệu Homepage tạo khi chạy.
- Volume `homepage-portable_editor-state`: hash tài khoản, audit, backup khi lưu cấu hình.
- `docker compose logs --tail=100`: xem tình trạng container.
- `docker compose down`: dừng và bỏ container, vẫn giữ volume.
- Build rồi `docker compose up -d` để cập nhật mã editor. File mặc định chỉ được seed khi setup, không ghi đè tùy chỉnh đã lưu.

Đây là bản khởi tạo sạch, không phải công cụ di chuyển dữ liệu. Widget liên kết hệ thống khác phải được cấu hình lại bằng thông tin của nơi mới. Không mount Docker socket mặc định. Muốn quay lại hệ thống hiện tại chỉ cần tiếp tục dùng hệ thống đó; gói này không thay đổi các máy đang chạy.

## Kiểm thử source

```sh
node --test editor/test.js
```

Test dùng fixture giả và thư mục tạm; không truy cập server hiện tại. Build/start Docker phải được xác minh trên máy có Docker. Bản đóng gói này đã kiểm tra source editor và YAML; chưa chạy container thực tế trên máy tạo gói vì không có Docker CLI.
