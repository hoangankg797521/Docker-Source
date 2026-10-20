# Homepage Portable — giao diện gốc và Visual Editor

Gói Docker độc lập, lấy CSS/JS và editor từ Homepage gốc v1.13.2. Giữ header, sáng/tối, font/màu/thẻ, tìm dịch vụ, đồng hồ, credit, nút bút, kéo thả, chỉnh 1–8 cột, đổi tiêu đề, backup/restore và thư viện icon Dashboard Icons / MDI / Simple Icons / selfh.st.

Không chứa dịch vụ, IP/domain, API key, tài khoản, mật khẩu, lịch sử hay số liệu của hệ thống gốc. Bốn nhóm mẫu, mỗi nhóm bốn thẻ trỏ `https://example.com/`, chỉ dùng để thể hiện bố cục 4 cột. Xóa/đổi tên các thẻ này bằng Edit. CPU/RAM/Disk đọc dữ liệu của nơi cài mới; không mang collector LXC riêng của máy cũ.

## Cài mới

Yêu cầu Docker Engine/Desktop, Docker Compose v2, Git và Internet để tải base image. Tìm kiếm icon và ảnh icon dùng các thư viện online như bản gốc.

```sh
git clone https://github.com/hoangankg797521/Docker-Source.git
cd Docker-Source/homepage-portable
cp .env.example .env
```

Sửa `.env` theo URL thực tế. Ví dụ IP máy mới `192.168.100.10`:

```dotenv
PORT=3000
PUBLIC_ORIGINS=http://192.168.100.10:3000
HOMEPAGE_ALLOWED_HOSTS=192.168.100.10:3000,localhost:3000
```

Nếu dùng domain thêm `https://dashboard.example.com` vào `PUBLIC_ORIGINS`, thêm `dashboard.example.com` vào `HOMEPAGE_ALLOWED_HOSTS`. Các phần tử ngăn bằng dấu phẩy; origin không có dấu `/` cuối. Reverse proxy phải trỏ vào cổng editor 3000 và chuyển tiếp `/editor/*`, `/api/*` cùng origin.

```sh
docker compose build
docker compose run --rm setup
docker compose up -d
docker compose ps
```

Setup yêu cầu nhập mật khẩu mới hai lần, tối thiểu 8 ký tự, không hiển thị ký tự. Tài khoản là `admin`. Chỉ hash scrypt được lưu trong volume riêng. Nếu setup chưa thành công, editor sẽ hướng dẫn tạo tài khoản thay vì báo lỗi chung. Setup từ chối ghi đè tài khoản đã tồn tại.

Mở `http://IP-MAY-MOI:3000`, bấm bút và đăng nhập. Sửa thẻ, tiêu đề hoặc số cột, bấm Save/Done. Thử `docker compose restart` rồi tải lại để kiểm tra dữ liệu được giữ.

## Cách gói hoạt động

- `Dockerfile`: target Homepage dựa trên `ghcr.io/gethomepage/homepage:v1.13.2`, target editor dựa trên Node 22 Alpine.
- `init`: chạy một lần trước Homepage, seed các file còn thiếu từ `defaults/`. Không ghi đè cấu hình đã có. Volume Homepage bật `nocopy` để tránh image chèn cấu hình mẫu mặc định.
- `setup`: tạo tài khoản mới, seed cấu hình khi cần; chỉ lưu hash, không nhúng mật khẩu trong image.
- `start.js`: xóa cache Homepage dựng sẵn bằng `/api/revalidate` trước khi gateway bắt đầu phục vụ.
- `homepage`: nội bộ Docker; `editor`: gateway duy nhất xuất cổng 3000.
- Volume `homepage-portable_config`: YAML, custom CSS/JS và runtime Homepage.
- Volume `homepage-portable_editor-state`: hash tài khoản, audit và backup của máy mới.

Không mount Docker socket mặc định. CPU/RAM/Disk có thể phản ánh phạm vi host/container mà Docker cung cấp, khác phạm vi LXC của hệ thống gốc. Giá trị được lấy thật từ nơi cài mới.

## Vận hành và bảo toàn dữ liệu

```sh
docker compose logs --tail=100
docker compose down
```

`down` không xóa volume. Khi cập nhật source, build rồi chạy `up -d`; init không ghi đè dữ liệu đang có. Bản này dành cho cài mới sạch. Nếu cài đè bản đóng gói cũ, volume cũ vẫn giữ CSS/JS cũ; cần sao lưu và thực hiện migration có chủ đích, không xóa volume chứa dữ liệu cần giữ.

`.env`, runtime, backup và secret không được đưa lên GitHub. Dockerfile dùng COPY theo danh sách cụ thể; `.dockerignore` giới hạn build context.

## Kiểm thử

```sh
node --test editor/test.js package-test.js
```

Kiểm tra editor: auth, Origin/CSRF, bảo toàn widget/trường nâng cao, conflict, save/restore/rollback, icon catalog và cache. Kiểm tra đóng gói: seed đủ asset gốc, bố cục 4 cột, chỉ URL mẫu, chạy init lại không ghi đè sửa đổi. Test dùng fixture giả, không truy cập hệ thống gốc.
