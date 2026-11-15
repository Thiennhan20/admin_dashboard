# ENTN Admin Dashboard (CineArena Command Center)

Bảng điều khiển quản trị **Django** cho hệ thống nền tảng xem phim & giải trí (phim/TV shows, phòng xem chung Watch Party, thông báo đẩy Expo Push, hệ thống game 3D Voxel Arena & Daily Challenge). Ứng dụng kết nối đồng thời **PostgreSQL**, **MongoDB**, **Upstash Redis** và các dịch vụ Node.js API bên ngoài.

---

## 1. Công nghệ sử dụng (Tech Stack)

* **Backend:** Django `6.0.5` (yêu cầu **Python `>= 3.12`**), Gunicorn (`gthread`), WhiteNoise (`CompressedStaticFilesStorage`).
* **Thư viện Python** ([`requirements.txt`](requirements.txt)): `Django==6.0.5`, `requests`, `psycopg[binary]`, `pymongo`, `gunicorn`, `whitenoise`, `qrcode`.
* **Cơ sở dữ liệu & Hạ tầng:**
  * **PostgreSQL** (`DATABASE_URL`, dùng custom backend retry `admin_pannel.db.backends.postgresql_retry` hoặc fallback SQLite cục bộ `db.sqlite3`): lưu tài khoản quản trị (`auth_user`), hồ sơ phân quyền/2FA (`AdminProfile`), `AuditLog`, `ErrorLog`, `SystemSetting`, `BackupLog`, `FeaturedContent`, `MaintenanceNotice`, `UserReport`, `ChangelogEntry`, `EmailLog`, `ScheduledTask`, `TaskRunLog`, `WebhookEndpoint`, `WebhookLog`.
  * **MongoDB** (`MONGODB_URI`, `MONGODB_NAME`): quản lý trực tiếp 14 collections vận hành (`users`, `sessions`, `comments`, `watchprogresses`, `streamhistories`, `searchhistories`, `notifications`, `passwordresettokens`, `blacklistedtokens`, `gameprofiles`, `gamehistories`, `dailychallenges`, `dailysubmissions`, `systemsettings`).
  * **Upstash Redis REST (Cache & Security):** quản lý cache `tmdb:*`, đếm Rate Limit đăng nhập/2FA (`admin_sec:rl:*`) và khóa cron phân tán (`admin_sec:cron_lock:*`).
  * **Upstash Redis REST (Watch Rooms):** theo dõi và cưỡng chế đóng phòng xem chung (`room:*`).
* **Frontend:** Bootstrap `5.3.2`, Font Awesome `6.5.1`, Chart.js `4.4.4`, Google Fonts (`Plus Jakarta Sans`, `Space Grotesk`, `JetBrains Mono`), Dark/Light Mode và chuyển đổi ngôn ngữ VI/EN ([`static/admin_dashboard/js/admin_i18n.js`](static/admin_dashboard/js/admin_i18n.js), [`static/admin_dashboard/js/admin_ui.js`](static/admin_dashboard/js/admin_ui.js)).

---

## 2. Chức năng & Phân hệ chính

### Vận hành & Hạ tầng
* **Operations Dashboard (`/dashboard/`):** Tổng quan trạng thái Node.js API, 3D Arena server, MongoDB metrics, hoạt động 7 ngày gần nhất và phòng xem chung đang mở.
* **Analytics (`/dashboard/analytics/`):** Biểu đồ Chart.js theo dải thời gian (người dùng mới, xác thực email/Google, top watchlist, tiến độ xem, bình luận theo giờ/ngày, stream audio, thông báo).
* **Monitoring (`/dashboard/monitoring/`):** Live health check & độ trễ của Node.js API, MongoDB (`dbStats`), Upstash Redis Cache (`INFO`), Upstash Redis Rooms và PostgreSQL.
* **Streaming Servers (`/dashboard/servers/`):** Kiểm tra sức khỏe cụm KKPhim, VidSrc và NguonC/OPhim; hỗ trợ quét domain VidSrc (tự động mỗi 20 phút qua `VidSrcCronThread` hoặc kích hoạt thủ công) và đồng bộ lên `db.systemsettings`.
* **Upstash Cache (`/dashboard/cache/`):** Quét khóa `tmdb:*`, xem dữ liệu/TTL và xóa từng key hoặc flush cache.

### Dữ liệu Người dùng, Media & Game
* **Users (`/dashboard/users/`):** Tìm kiếm, lọc, xem chi tiết 360° (watchlist, watch progress, search history, comments, sessions, push tokens, social graph), sửa hồ sơ, khóa/mở khóa (`api_user_ban`), thu hồi phiên và xóa cascade.
* **Movies / TV Shows / Watchlist:** Duyệt danh mục phim/series tổng hợp từ dữ liệu người dùng, xem chi tiết, dọn dẹp watchlist không hợp lệ và gỡ bỏ nội dung cascade.
* **Comments / Watch Progress / Stream History / Search History:** Kiểm duyệt ẩn/hiện/xóa bình luận, theo dõi tiến độ xem, kiểm tra link stream (có chống SSRF) và quản lý lịch sử tìm kiếm.
* **Watch Rooms (`/dashboard/rooms/`):** Theo dõi phòng Watch Party thời gian thực từ Redis và đóng phòng cưỡng chế.
* **Notifications & Push Devices (`/dashboard/notifications/`, `/dashboard/push-devices/`):** Lịch sử thông báo, gửi broadcast kèm Expo Push (batch 100 token) và tự động dọn `DeviceNotRegistered` token chết.
* **Games — 3D Voxel Arena & Daily Challenge (`/dashboard/games/...`):** Quản lý `gameprofiles`, `gamehistories`, điều khiển phòng Arena trực tiếp qua `GAME_SERVER_URL` và tạo/chỉnh sửa thử thách số hàng ngày (`dailychallenges` / `dailysubmissions`).

### Bảo mật, Hệ thống & Mở rộng
* **Admins & RBAC (`/dashboard/admins/`):** Quản lý tài khoản quản trị viên, reset 2FA khẩn cấp (chỉ Super Admin) và ma trận phân quyền tùy biến.
* **Active Sessions & Tokens (`/dashboard/sessions/`, `/dashboard/password-resets/`, `/dashboard/blacklisted-tokens/`):** Theo dõi phiên đăng nhập, cưỡng chế đăng xuất người dùng và dọn token hết hạn.
* **Audit Logs & Error Logs (`/dashboard/audit-logs/`, `/dashboard/error-logs/`):** Truy vết thao tác quản trị (IP, User-Agent, đối tượng) và lưu stack-trace lỗi chưa xử lý qua `ErrorLoggingMiddleware`.
* **Platform Settings (`/dashboard/settings/`):** Cấu hình toàn hệ thống (bật/tắt đăng ký, phòng xem, chế độ bảo trì, TTL cache, giới hạn bình luận...) đồng bộ SQL và MongoDB.
* **Backup & Safe Dry-Run Restore (`/dashboard/backup/`):** Xuất snapshot MongoDB Extended JSON v2 (mặc định **loại bỏ mật khẩu & token nhạy cảm**) và quy trình khôi phục 2 bước (Dry-Run Preview → xác nhận token phiên + kiểm tra checksum SHA-256; chế độ `replace` yêu cầu nhập cụm `RESTORE` và quyền `backup.delete` / Super Admin).
* **Tiện ích khác:** Featured Content & Maintenance Notices (`/dashboard/content/`), Moderation Reports (`/dashboard/reports/`), Changelog (`/dashboard/changelog/`), Email Dispatch (`/dashboard/email/`), Scheduled Tasks (`/dashboard/scheduled-tasks/`), Webhooks (`/dashboard/webhooks/`), Global Search (`/dashboard/api/global-search/`) và Export CSV/Excel/PDF (`/dashboard/api/export/<collection>/`).

---

## 3. Kiến trúc Bảo mật & Phân quyền (Security Architecture)

### 3.1. Kiểm soát truy cập, Rate Limiting & 2FA
* **Khóa đường vào mặc định (`AdminAccessMiddleware`):**
  * Chuyển hướng toàn bộ `/admin/login/` về `/dashboard/login/` nhằm tránh kẻ tấn công đi vòng qua lớp Rate Limit và 2FA của Dashboard.
  * Yêu cầu `is_authenticated` và `is_staff` hoặc `is_superuser` cho toàn bộ `/admin/` và `/dashboard/`.
  * Đường dẫn `/dashboard/register/` **chỉ mở một lần duy nhất để bootstrap** khi hệ thống chưa có tài khoản `is_staff=True` nào; sau đó tự động khóa và chỉ cho phép `superuser` / `super_admin` truy cập.
* **Chống Brute-Force (`users_management/rate_limit.py`):**
  * Giới hạn riêng cho `admin_login` và `admin_2fa`: mặc định tối đa **5 lần thất bại / tài khoản** và **10 lần thất bại / IP** trong cửa sổ **900 giây (15 phút)**, trả mã HTTP `429`.
  * Bucket lưu phân tán trên Upstash Redis (`admin_sec:rl:*`), fallback an toàn về bộ nhớ cục bộ nếu chưa cấu hình Redis.
* **Xác thực 2 bước 2FA TOTP (`users_management/totp.py`, `views/profile.py`):**
  * Chuẩn RFC 6238 (6 chữ số, chu kỳ 30s, sai số ±1 bước, so sánh hằng thời gian `hmac.compare_digest`).
  * Sinh mã QR SVG hoàn toàn **offline** (`data:image/svg+xml;base64,...`), không gửi `totp_secret` ra bên ngoài.
  * Cấp **8 mã dự phòng dùng 1 lần**, băm bằng **SHA-256 + random salt 16 byte** trước khi lưu vào DB.
  * Phiên chờ xác thực 2FA hết hạn sau **600 giây (10 phút)** và kiểm tra `next` bằng `url_has_allowed_host_and_scheme` để chống Open Redirect.

### 3.2. Phân quyền RBAC (`users_management/models.py`, `decorators.py`)
* Hỗ trợ **4 vai trò** trên **31 module × 4 quyền (`view`, `edit`, `delete`, `export`)** kèm bảng override chi tiết trên từng `AdminProfile.permissions`:
  * **`super_admin`:** Toàn quyền trên mọi module, quản lý admin khác, reset 2FA, backup/restore destructive và settings.
  * **`admin`:** Quyền vận hành hầu hết các module nghiệp vụ; bị giới hạn mặc định với `admins.edit/delete`, `settings.*`, `backup.*`, `scheduled_tasks.edit/delete`, `webhooks.edit/delete`.
  * **`moderator`:** Tập trung kiểm duyệt nội dung, user, comment, phòng xem, báo cáo và phân tích dữ liệu.
  * **`viewer`:** Chỉ xem; bị `AdminAccessMiddleware` chặn toàn bộ HTTP method ghi (`POST/PUT/PATCH/DELETE`), ngoại trừ đăng xuất và cập nhật hồ sơ cá nhân.
* Cơ chế **Fail-closed**: tài khoản staff không có `AdminProfile` sẽ bị từ chối truy cập mọi phân hệ (trừ `/dashboard/profile/`).

### 3.3. Bảo vệ dữ liệu & Chống tấn công phổ biến
* **CSRF:** Bảo vệ CSRF của Django đang bật trên toàn bộ form và AJAX API (không có `@csrf_exempt` trên bất kỳ endpoint nào).
* **Chống SSRF khi kiểm tra Stream:** `validate_public_http_url()` ([`services.py`](users_management/services.py)) chặn scheme ngoài `http/https`, chặn hostname nội bộ, dải IP private/loopback/link-local/reserved và DNS phân giải ra IP nội bộ; tắt tự động chuyển hướng (`allow_redirects=False`).
* **Ẩn trường nhạy cảm:** Các query danh sách người dùng và token sử dụng MongoDB projection để loại trừ `password`, `emailVerificationToken`, `refreshToken`, `tokenHash`, `token`.
* **Chống XSS nhẹ ở tầng xuất JSON:** Kiểm tra lại toàn bộ template dùng `|safe` (xem mục *Lưu ý bảo mật khi vận hành & phát triển* bên dưới).

---

## 4. Lưu ý Bảo mật Quan trọng (Checklist Production & Hardening)

> **Cảnh báo cấu hình mặc định trong [`admin_pannel/settings.py`](admin_pannel/settings.py):** Khi triển khai Production/Railway, **bắt buộc** thiết lập đầy đủ các biến môi trường bên dưới để ghi đè fallback phát triển local.

### 4.1. Bắt buộc khi Deploy Production
1. **Đặt `DEBUG=False`:** Trong `settings.py`, `DEBUG` mặc định là `True` nếu không có biến môi trường `DEBUG`. Chạy `DEBUG=True` trên production sẽ làm **rò rỉ stack trace/biến cấu hình khi lỗi** và làm `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_HSTS_SECONDS` mặc định tắt.
2. **Đặt `SECRET_KEY` mạnh và duy nhất:** Không bao giờ dùng chuỗi fallback `django-insecure-...` trong `settings.py`. Sinh khóa ngẫu nhiên tối thiểu 50 ký tự từ secret manager / Railway Variables.
3. **Cấu hình chặt `ALLOWED_HOSTS` và `CSRF_TRUSTED_ORIGINS`:** Liệt kê đúng domain thật (VD: `CSRF_TRUSTED_ORIGINS=https://admin.example.com`). App đã cấu hình `SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')` phù hợp reverse proxy HTTPS.
4. **Tuyệt đối không đưa bí mật vào Git hay Docker image:**
   * Không commit `.env`, `.env.local`, `db.sqlite3`, `auto_backups/`, `ADMIN_SETUP.md`, `RAILWAY_DEPLOY.md`.
   * Lưu ý: `settings.py` có hàm `load_local_env()` ghi đè trực tiếp `os.environ[key] = value` từ `.env` (và thử tìm thêm `../../server-en-website/.env` nếu tồn tại). **Không được đặt file `.env` bên trong môi trường build/deploy container** để tránh ghi đè biến môi trường của Railway hoặc làm lộ file bí mật trong image.
   * Khuyến nghị thêm `auto_backups/` vào [`.gitignore`](.gitignore) để file snapshot tạo bởi cron `backup_db` không bao giờ bị `git add` nhầm.
5. **Xoay vòng khóa (Key Rotation):** Nếu nghi ngờ rò rỉ `.env` hoặc log chứa chuỗi kết nối, cần rotate ngay: `SECRET_KEY`, mật khẩu PostgreSQL (`DATABASE_URL`), mật khẩu MongoDB Atlas (`MONGODB_URI`), token Upstash Redis và `ADMIN_API_KEY`.
6. **Bắt buộc bật 2FA (TOTP)** cho mọi tài khoản `super_admin` / `admin` ngay sau khi khởi tạo.

### 4.2. Các điểm cần lưu ý / cải thiện thêm trong mã nguồn hiện tại
* **Webhook SSRF (`views/webhooks.py`):** Hiện `webhooks_list` và `webhook_test` chưa chạy `validate_public_http_url()` cho `endpoint.url` và cho phép gộp `endpoint.headers`. Chỉ cấp quyền `webhooks.*` cho tài khoản thực sự tin cậy (mặc định đã tắt với `admin`/`moderator`/`viewer`) và nên bổ sung kiểm tra SSRF tương tự `stream_history.py`.
* **Tin cậy header proxy (`rate_limit.py`):** `get_client_ip()` lấy IP đầu tiên trong `HTTP_X_FORWARDED_FOR`. Cần đảm bảo app **luôn đứng sau reverse proxy đáng tin cậy** (như Railway edge) và không mở cổng Gunicorn trực tiếp ra Internet để tránh spoofing IP vượt rate-limit theo IP.
* **Kiểm tra độ mạnh mật khẩu admin (`views_legacy.py`):** `admin_create` và `admin_edit` gọi `create_user`/`set_password` trực tiếp trên server mà chưa chạy `django.contrib.auth.password_validation.validate_password()` (khác với `admin_register` dùng `UserCreationForm`). Cần tuân thủ mật khẩu mạnh (≥12 ký tự) khi tạo/sửa tài khoản quản trị.
* **Bảo vệ `totp_secret` trong DB:** `AdminProfile.totp_secret` hiện lưu dạng plain Base32 trong bảng SQL (trong khi `totp_backup_codes` đã hash salted SHA-256). Cần bật SSL cho `DATABASE_URL` và giới hạn quyền truy cập DB PostgreSQL cũng như Django `/admin/`.
* **Projection & XSS phòng ngừa (`views/users.py`, `templates/`):** `users_list` đã ẩn `password` và `emailVerificationToken`, nhưng `user_detail` / `user_edit` đang gọi `db.users.find_one({'_id': oid})` không kèm projection; ngoài ra `servers.html` đang nhúng `{{ vidsrc_config_json|safe }}` trong `<script type="application/json">` — nên chuyển dần sang filter chuẩn `json_script` của Django để loại trừ rủi ro breakout thẻ `</script>`.
* **Quản lý RAM & Tài nguyên worker (`nixpacks.toml`):** Server mặc định chạy `--workers 1 --threads 8 --timeout 60`, kèm 2 cron thread tự động (`ScheduledTasksCronThread`, `VidSrcCronThread`) và cache Django đang dùng `LocMemCache` mặc định (lưu trực tiếp trong RAM worker). Khi dữ liệu MongoDB lớn, tác vụ `backup_db` / restore 50MB có thể gây **tiêu tốn RAM cao hoặc `WORKER TIMEOUT` → `SIGKILL (OOM)`**. Cần theo dõi Metrics RAM, cân nhắc streaming backup/export hoặc chạy cron qua CLI `manage.py run_scheduled_tasks` tách riêng.
* **Lỗi đã ghi nhận khi audit (`views/api.py`):** Trong `api_bulk_action`, các hành động `comments` (`hide`/`restore`) và `notifications` (`read`/`unread`) đang tham chiếu biến `oids` chưa được khởi tạo bên ngoài nhánh `action == 'delete'`, cần fix nếu sử dụng API bulk cho các hành động này.

---

## 5. Biến Môi Trường (Environment Variables)

Tạo file `.env` trong `admin_pannel/` khi phát triển local (chỉ dùng placeholder, **không commit `.env`**):

```ini
# ── Django Core & Security (BẮT BUỘC cấu hình đúng trên Production) ──
SECRET_KEY=<chuoi-ngau-nhien-it-nhat-50-ky-tu>
DEBUG=False
ALLOWED_HOSTS=localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=https://your-admin-domain.example.com

# Tùy chọn HTTPS / Cookie / HSTS (mặc định tự kích hoạt khi DEBUG=False)
SECURE_SSL_REDIRECT=True
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
SECURE_HSTS_SECONDS=3600
SECURE_HSTS_INCLUDE_SUBDOMAINS=False
SECURE_HSTS_PRELOAD=False

# ── PostgreSQL (Admin Users, RBAC, Audit/Error Logs, Settings) ───────
# Bỏ trống hoặc đặt USE_SQLITE=True nếu muốn dùng db.sqlite3 khi dev local
DATABASE_URL=postgresql://<user>:<password>@<host>:<port>/<dbname>?sslmode=require
USE_SQLITE=False
DB_CONN_MAX_AGE=300
DB_WAIT_TIMEOUT=120
DB_WAIT_INTERVAL=3

# ── MongoDB Atlas (Dữ liệu người dùng & vận hành) ────────────────────
MONGODB_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority
MONGODB_NAME=<database_name>

# ── Upstash Redis REST (Cache TMDB, Rate Limit Login/2FA, Cron Lock) ─
UPSTASH_REDIS_REST_CACHE_URL=https://<cache-endpoint>.upstash.io
UPSTASH_REDIS_REST_CACHE_TOKEN=<upstash_cache_token>

# ── Upstash Redis REST (Phòng Watch Party thời gian thực) ───────────
UPSTASH_REDIS_REST_URL=https://<rooms-endpoint>.upstash.io
UPSTASH_REDIS_REST_TOKEN=<upstash_rooms_token>

# ── Dịch vụ Web / Node.js API / 3D Voxel Arena ───────────────────────
CLIENT_URL=http://localhost:3000
SERVER_URL=http://localhost:3001
API_BASE_URL=http://localhost:3001/api
API_AUTH_BASE_URL=http://localhost:3001/api/auth
ADMIN_API_KEY=<shared_admin_api_key>
GAME_SERVER_URL=http://localhost:3006
GAME_CLIENT_URL=http://localhost:5173
```

---

## 6. Cấu trúc Thư mục chính

```text
admin_pannel/
├── admin_pannel/
│   ├── db/backends/postgresql_retry/  # Wrapper PostgreSQL tự retry khi khởi động
│   ├── settings.py                    # Cấu hình bảo mật, DB, Services, Static/WhiteNoise
│   ├── urls.py                        # Root URLs (/, /health/, /admin/, /dashboard/)
│   ├── wsgi.py / asgi.py
├── static/admin_dashboard/js/
│   ├── admin_i18n.js                  # Từ điển VI/EN dùng chung
│   └── admin_ui.js                    # Theme Dark/Light, Sidebar, Search, Toast, Modal
├── templates/admin_dashboard/         # 51 templates HTML theo phân hệ (chi tiết xem README trong thư mục)
├── users_management/
│   ├── management/commands/
│   │   ├── create_superuser.py        # CLI tạo Super Admin đầu tiên
│   │   └── run_scheduled_tasks.py     # CLI chạy tác vụ định kỳ (--all, --task-id, --json)
│   ├── migrations/                    # DB migrations (0001 -> 0003)
│   ├── templatetags/admin_tags.py
│   ├── views/                         # 28 module view theo chức năng
│   ├── context_processors.py          # Đếm badge sidebar (SWR cache)
│   ├── decorators.py                  # @require_permission, @require_super_admin, @log_action
│   ├── middleware.py                  # AdminAccessMiddleware & ErrorLoggingMiddleware
│   ├── models.py                      # 14 SQL models + ma trận RBAC (31 modules x 4 actions)
│   ├── rate_limit.py                  # Rate limit login & 2FA (Upstash Redis + fallback RAM)
│   ├── services.py                    # MongoAdminService, SSRF check, SWR cache, health checks
│   ├── tests.py                       # 7 bộ test (RBAC, 2FA, Rate Limit, Backup, Cron, i18n)
│   ├── totp.py                        # RFC 6238 TOTP & salted SHA-256 recovery codes
│   ├── urls.py                        # Toàn bộ routing /dashboard/...
│   └── views_legacy.py                # Auth, 2FA login, Admin CRUD, Dashboard, Cache, Media, Rooms
├── manage.py
├── nixpacks.toml                      # Lệnh start Production (migrate + collectstatic + gunicorn)
└── requirements.txt
```

---

## 7. Cài đặt, Chạy Local, Deploy & Kiểm thử

### 7.1. Chạy Local
```bash
cd admin_pannel

# 1. Tạo và kích hoạt môi trường ảo (Python >= 3.12)
python -m venv .venv
source .venv/bin/activate      # Linux/macOS
# .venv\Scripts\activate       # Windows PowerShell

# 2. Cài đặt thư viện
pip install -r requirements.txt

# 3. Tạo file .env theo mục 5 (khuyến nghị đặt USE_SQLITE=True hoặc trỏ tới Postgres dev riêng)

# 4. Áp dụng migration
python manage.py migrate

# 5. Tạo Super Admin đầu tiên (khuyên dùng prompt tương tác để không lưu mật khẩu vào shell history)
python manage.py createsuperuser
# Sau đó vào Django shell hoặc tạo profile role='super_admin' (hoặc dùng CLI create_superuser / trang /dashboard/register/ khi chưa có admin nào)

# 6. Khởi động server phát triển
python manage.py runserver
```

> **Lưu ý an toàn:** Đường dẫn `/dashboard/register/` chỉ mở để khởi tạo **tài khoản admin đầu tiên** khi bảng `auth_user` chưa có tài khoản `is_staff=True` nào; sau đó hệ thống tự động khóa đăng ký công khai.

### 7.2. Deploy Production (Railway / Nixpacks)
File [`nixpacks.toml`](nixpacks.toml) thực thi:
```bash
python manage.py migrate && python manage.py collectstatic --noinput && gunicorn admin_pannel.wsgi --bind 0.0.0.0:$PORT --worker-class gthread --workers 1 --threads 8 --timeout 60
```
* Đảm bảo đặt `DEBUG=False`, `SECRET_KEY`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` trên Railway Variables.
* Có thể chạy tác vụ định kỳ qua cron ngoài bằng lệnh:
  ```bash
  python manage.py run_scheduled_tasks
  ```

### 7.3. Kiểm thử (`tests.py`)
Bộ test trong [`users_management/tests.py`](users_management/tests.py) bao gồm 7 nhóm test (`RbacAndMiddlewareTests`, `TotpTwoFactorTests`, `LoginRateLimitTests`, `SessionsAndPushDevicesTests`, `BackupAndDryRunRestoreTests`, `ScheduledTasksCronTests`, `SingleLanguageI18nContractTests`).

> **Cảnh báo an toàn Database trước khi chạy `manage.py test`:**
> Hàm `load_local_env()` trong [`settings.py`](admin_pannel/settings.py) nạp `.env` bằng `os.environ[key] = value` (ghi đè biến môi trường shell). Nếu `.env` đang trỏ tới PostgreSQL thật/staging (`DATABASE_URL`), lệnh `python manage.py test` sẽ cố tạo/xóa database `test_<dbname>` trên máy chủ PostgreSQL đó. Hãy dùng `.env` dev riêng hoặc trỏ `DATABASE_URL` tới DB test cục bộ/SQLite trước khi chạy test!
