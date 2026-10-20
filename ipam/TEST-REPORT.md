# IPAM Docker — báo cáo xác minh 20/10/2026

Release: [2026.10.20.1](https://github.com/hoangankg797521/Codex-Project/releases/tag/ipam-v2026.10.20.1)

Image: `ghcr.io/hoangankg797521/ipam:2026.10.20.1`

Digest: `sha256:69eb74c38bba3af3318e197dd432e5ee70f575ff4c91466109bf7f556639e032`

Source release commit: `a58db3f0fd8149824166ea1d291c4b7a3d401aaa`

## Kết quả thực tế

- PASS: SHA-256 của 27 file source/static từ Production; schema đủ 46 bảng và không có bản ghi site cũ.
- PASS: build sạch tại Docker Desktop amd64 và GitHub Actions.
- PASS: cài PostgreSQL mới, mật khẩu admin riêng; từ chối admin/admin.
- PASS: 20 API của các module chính; 7 asset UI; tạo server/diagram thử nghiệm; xuất PDF.
- PASS: restart giữ dữ liệu và tài khoản.
- PASS: backup/restore giữ server và diagram thử nghiệm.
- PASS: Chrome headless 1440×1000, đăng nhập, Topology canvas tải xong, Server Connection hiển thị; không có pageerror JavaScript.
- PASS: publish manifest Linux amd64/arm64, kéo lại image từ GHCR theo digest và chạy lại bộ integration trên amd64.
- PASS: GitHub Release có bộ cài, image-digest.txt và SHA256SUMS; GHCR từ chối truy cập ẩn danh (HTTP 401).

Workflow và log kiểm thử: [GitHub Actions](https://github.com/hoangankg797521/Codex-Project/actions/runs/39439164000).

## Phạm vi chưa xác minh

Chưa chạy runtime trên máy ARM64 thật; chưa chạy scan/SNMP/DHCP với thiết bị mạng tại site mới. Không thực hiện regression toàn bộ thao tác nghiệp vụ; mã nguồn/giao diện được giữ từ snapshot Production, trừ bản vá tương thích SQL một dòng đã ghi trong release.json. Không sao chép dữ liệu hoặc credential Production. Production không được restart hoặc triển khai lại trong nhiệm vụ đóng gói này.

Hướng dẫn cài đặt, backup và rollback nằm trong README.md. Test chỉ tạo/xóa volume của Compose project thử nghiệm có tên ngẫu nhiên.

## Công khai source trong Docker-Source

Source ứng dụng/schema giữ nguyên hash; Dockerfile đổi OCI source label sang Docker-Source. Compose bổ sung build local và image mặc định ipam:local. Validate cả host và bridge Compose: PASS. Không chứa .env/runtime hoặc secret khớp Inventory IPAM. Integration test tại bước công khai chưa chạy lại: Docker Desktop socket không tồn tại tại thời điểm thử. Các kết quả ở trên thuộc bản snapshot/release trước đó, không phải lần chạy mới trong repository Public.
