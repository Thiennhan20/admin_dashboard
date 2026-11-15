# Cấu trúc Template — Admin Dashboard (`templates/admin_dashboard/`)

Thư mục này chứa toàn bộ **51 template HTML** của giao diện quản trị ENTN Admin, kế thừa từ khung chung `base.html` (trừ nhóm trang xác thực `auth/` kế thừa từ `auth/base_auth.html`).

## Cấu trúc thư mục hiện tại

```text
templates/admin_dashboard/
├── base.html                         # Layout shell chung (Sidebar, Topbar, Dark/Light, i18n, Toast)
├── includes/
│   ├── confirm_modal.html            # Modal xác nhận thao tác nguy hiểm dùng chung
│   └── pagination.html               # Thanh phân trang server-side dùng chung
├── auth/
│   ├── base_auth.html                # Khung giao diện cho trang đăng nhập / đăng ký / 2FA
│   ├── login.html                    # Đăng nhập (kèm thông báo rate-limit 429)
│   ├── login_2fa.html                # Xác thực 6 số TOTP hoặc Backup Code
│   └── register.html                 # Đăng ký (chỉ mở khi bootstrap admin đầu tiên hoặc Super Admin)
├── dashboard/
│   └── dashboard.html                # Operations Dashboard (tải telemetry bất đồng bộ)
├── analytics/
│   └── analytics.html                # Biểu đồ phân tích Chart.js 4.4.4
├── monitoring/
│   └── monitoring.html               # Giám sát sức khỏe Node.js, MongoDB, Redis, PostgreSQL
├── servers/
│   └── servers.html                  # Giám sát cụm KKPhim / VidSrc / NguonC & auto-scan VidSrc
├── cache/
│   └── cache.html                    # Quản lý khóa cache tmdb:* trên Upstash Redis
├── users/
│   ├── users_list.html               # Danh sách người dùng MongoDB
│   ├── user_detail.html              # Hồ sơ chi tiết 360° của người dùng
│   └── user_edit.html                # Chỉnh sửa / xác minh / khóa người dùng
├── watchlist/
│   ├── watchlist_list.html
│   ├── watchlist_detail.html
│   └── watchlist_edit.html
├── movies/
│   ├── movies_list.html
│   └── movie_detail.html
├── tv_shows/
│   ├── tv_shows_list.html
│   └── tv_detail.html
├── comments/
│   └── comments_list.html            # Kiểm duyệt ẩn/hiện/xóa bình luận
├── rooms/
│   └── rooms_list.html               # Phòng xem chung Watch Party (Upstash Redis)
├── notifications/
│   ├── notifications_list.html       # Thông báo hệ thống & Broadcast
│   └── push_devices.html             # Quản lý thiết bị Expo Push & test push
├── watch_progress/
│   └── watch_progress_list.html
├── stream_history/
│   └── stream_history_list.html      # Lịch sử link phát & probe kiểm tra link sống (SSRF-safe)
├── search_history/
│   └── search_history_list.html
├── games/
│   ├── game_profiles.html            # Hồ sơ người chơi 3D Arena / Game
│   ├── game_histories.html           # Lịch sử trận đấu 3D Arena
│   └── daily_challenge.html          # Thử thách hàng ngày & bài nộp
├── admin/
│   ├── _rbac_matrix_card.html        # Partial bảng ma trận phân quyền RBAC
│   ├── admin_list.html               # Danh sách tài khoản quản trị
│   ├── admin_create.html             # Tạo tài khoản quản trị kèm ma trận quyền
│   ├── admin_detail.html             # Chi tiết admin, quyền hiệu lực & nút reset 2FA
│   └── admin_edit.html               # Sửa tài khoản admin & override quyền
├── profile/
│   └── profile.html                  # Hồ sơ cá nhân, đổi mật khẩu, bật/tắt 2FA & mã dự phòng
├── tokens/
│   ├── sessions_list.html            # Phiên đăng nhập MongoDB
│   ├── password_resets.html          # Token đặt lại mật khẩu
│   └── blacklisted_tokens.html       # JWT/token bị thu hồi
├── content/
│   └── content_mgmt.html             # Nội dung nổi bật & thông báo bảo trì
├── reports/
│   └── reports.html                  # Báo cáo vi phạm từ người dùng
├── changelog/
│   └── changelog.html                # Nhật ký phiên bản (đồng bộ MongoDB)
├── email/
│   └── email.html                    # Gửi Email & xem log gửi
├── scheduled_tasks/
│   └── scheduled_tasks.html          # Quản lý & chạy tác vụ định kỳ
├── webhooks/
│   └── webhooks.html                 # Cấu hình Webhook endpoints & log phản hồi
├── backup/
│   └── backup.html                   # Backup MongoDB JSON v2 & Dry-Run Restore 2 bước
├── settings/
│   └── settings.html                 # Cấu hình nền tảng (SQL + MongoDB)
├── audit_logs/
│   └── audit_logs.html               # Nhật ký kiểm toán hành động Admin
└── error_logs/
    └── error_logs.html               # Nhật ký lỗi hệ thống (/dashboard/)
```

## Quy ước UI & Lưu ý Bảo mật Template

* **UI System:** Dùng CSS variables 3 tầng tại `base.html`, Bootstrap `5.3.2`, Font Awesome `6.5.1`, Google Fonts (`Plus Jakarta Sans`, `Space Grotesk`, `JetBrains Mono`) và từ điển đa ngôn ngữ VI/EN tại `static/admin_dashboard/js/admin_i18n.js`.
* **CSRF Protection:** Mọi `<form method="post">` bắt buộc có `{% csrf_token %}`; các yêu cầu `fetch()`/`XMLHttpRequest` thay đổi dữ liệu phải gửi kèm header `X-CSRFToken`.
* **Auto-escaping & `|safe`:** Mặc định Django auto-escape toàn bộ biến template để chống XSS. Không dùng `|safe` cho dữ liệu do người dùng nhập hoặc từ nguồn ngoài; nếu cần truyền JSON vào `<script>`, ưu tiên dùng template tag `{{ value|json_script:"element-id" }}` thay cho `|safe`.
* **Routing & Views:** URL được định nghĩa tại [`users_management/urls.py`](../../users_management/urls.py) và xử lý qua các module trong [`users_management/views/`](../../users_management/views/) cùng [`users_management/views_legacy.py`](../../users_management/views_legacy.py).
