# Uptime Kuma Operations

Gói Docker Compose build cho Uptime Kuma **2.5.6**, gồm giao diện Operations/Spectrum hiện dùng và các tính năng custom đang chạy trên production.

## Chạy nhanh

```sh
cp .env.example .env
docker compose -f compose.yaml build --pull
docker compose -f compose.yaml up -d
docker compose -f compose.yaml ps
```

Mở `http://<IP-máy-chạy-Docker>:3001` và tạo tài khoản quản trị khi được hỏi. Xem log bằng `docker compose -f compose.yaml logs -f uptime-kuma`.

Đổi cổng hoặc đường dẫn dữ liệu trong `.env`. Mặc định dữ liệu nằm tại `./data`, được mount vào `/app/data` và bị loại khỏi Git. Sao lưu thư mục này riêng; không đưa database, cấu hình monitor hay thông tin Telegram lên repo.

## Thành phần đã đóng gói

- Theme Operations, Spectrum và dashboard refinement; responsive desktop/mobile và hỗ trợ light/dark mode.
- Giao diện chọn monitor dạng thẻ, checkbox riêng và thao tác hàng loạt hiện có.
- Tối đa 5 check phụ Ping, TCP Port, HTTP(S) cho monitor chủ động. Monitor lên Up khi có ít nhất một check thành công; chỉ xuống Down khi mọi check lỗi theo retry đã cấu hình. Trang chi tiết hiển thị sức khỏe từng protocol.
- Telegram “ALL SYSTEMS ONLINE” sau khi có sự cố Down thực sự và toàn bộ monitor đang bật phục hồi. Dùng cấu hình Telegram sẵn có trong database, chống gửi trùng và thử lại khi gửi lỗi.
- Knex migration thêm `monitor.additional_checks` cho database mới hoặc database chưa có cột này.
- Mẫu Telegram Minimal NOC tại `templates/minimal-noc-template.html` để cấu hình trong WebUI. Template không tự nhập token/chat ID và không thay cấu hình notification đã lưu.

Các file runtime custom, migration và bundle giao diện nằm trong `overlay/app/`. Source CSS và Telegram được lưu thêm trong `custom-source/` để dễ bảo trì. Gói không chứa database, `node_modules`, file backup, token hoặc Chat ID.

## Cập nhật phiên bản upstream

Mặc định image nền được ghim ở Uptime Kuma `2.5.6` để tương thích với các patch custom. Chỉ đổi `UPTIME_KUMA_VERSION` sau khi rebase các source custom lên phiên bản mới, kiểm tra migration và build lại giao diện. Tùy chỉnh local có thể cần cập nhật khi nâng cấp upstream.

## Dùng database hiện có

Dừng instance cũ trước khi sao chép dữ liệu SQLite. Sao lưu toàn bộ thư mục dữ liệu, chép bản sao vào đường dẫn `UPTIME_KUMA_DATA_DIR`, rồi chạy container. Không chạy đồng thời hai instance cùng ghi vào một database. Khi đổi nơi triển khai, kiểm tra database backup trước khi chuyển traffic.

## Ghi chú

Image nền được lấy từ image chính thức `louislam/uptime-kuma`. Thành phần upstream giữ giấy phép MIT của tác giả upstream; xem `UPSTREAM-LICENSE-MIT.txt`. Tùy chỉnh trong gói này nhắm riêng tới Uptime Kuma 2.5.6.
