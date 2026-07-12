# Movie Platform - Admin Dashboard

## 🎯 Overview
A modern, feature-rich Django-based Admin Dashboard designed for orchestrating a movie/TV-show streaming platform. It interfaces directly with a MongoDB database, a Node.js API, Redis caches, and PostgreSQL to provide real-time metrics, platform control, data management, and operational security—all packaged in a premium, responsive glassmorphic user interface.

---

## 🚀 Key Features

### 1. Operations Panel & Data Manager
- **Centralized Data Hub**: View, search, filter, and paginate through key MongoDB collections in real-time.
  - **Users & Watchlists**: Manage accounts, view detailed activity logs, and edit watchlist items.
  - **Content (Movies / TV Shows)**: List and delete TMDB media index records.
  - **Social Interactions**: Audit comments, hide/delete posts, and supervise active Watch Rooms (Watch Parties) hosted in Redis.
- **Bulk Actions**: Perform mass deletes (e.g., bulk banning users, purging invalid watchlists) with cascade cleanup.

### 2. Advanced Analytics Dashboard
- **Interactive Chart.js Visualizations**: Line, doughnut, and bar charts detailing user registration trend, email verification ratio, top watchlisted content, comment frequency, and hourly activity.
- **Date Range Picker**: Filter metrics dynamically (7 days, 30 days, current month, 90 days) with responsive chart animations.
- **Theme Adaptability**: Charts dynamically re-draw their gridlines and text labels when switching between Light and Dark mode.

### 3. Comprehensive Security & Audit
- **Role-Based Access Control (RBAC)**: Fine-grained admin roles (`super_admin`, `admin`, `moderator`) mapping to specific permissions.
- **Admin Activity Logs**: Automatic SQL-persisted audit log recording all mutating actions made by admin users (Action, Target Type, Target ID, Admin IP, Timestamp).
- **Session & Token Management**: Monitor and search active MongoDB `blacklistedtokens` and `passwordresettokens`.
- **CSRF Protection**: Comprehensive CSRF tokens enforced across all forms and AJAX endpoints.

### 4. Utilities & Extensions
- **Multi-Format Data Export**: Standardized dropdown menus to export lists (Users, Watchlist, Watch Progress, Search History, Notifications, and Audit Logs) to **CSV**, **Excel (.xls)**, and **PDF / Print Layout**.
- **System Health Monitor**: Live latency tracking (ping check) and status details for the Node.js API, MongoDB database storage, Redis Cache memory, Redis Rooms, and PostgreSQL.
- **Automated Tasks (Cron)**: CRUD operations on task schedules; runs MongoDB cleanup handlers (token expiry and watch progress purging).
- **System-Wide Settings**: Control flags for maintenance mode, registration toggle, maximum watch rooms, and daily comment caps.
- **Email Broadcast Tool**: Draft and send bulk HTML emails to target user groups or single addresses with SMTP delivery logging.
- **Webhook Integrations**: Configure, test, and log HTTP POST webhook payloads to external API endpoints.
- **Changelog Manager**: Publish platform feature updates and logs.

### 5. Premium UI/UX Design
- **Responsive Dark/Light Mode**: Elegant HSL-curated color scheme, Outfit and Inter typography, and clean glassmorphism styling.
- **Micro-interactions**:
  - **Toast Notifications**: Auto-dismissing floating notifications replacing intrusive modal alerts.
  - **Skeleton Shimmer Loaders**: Shimmering loaders mimicking content structures during AJAX list updates.
  - **Sticky Headers**: Fixed table column headers on scroll for ease of navigation.
  - **Table Gridlines**: Clear vertical border dividers and balanced action button spacing to prevent misclicks.

---

## 🏗️ Architecture & URL Structure

### Data Flow
```
                     ┌──────────────────┐
                     │  Django Admin    │ (Dashboard UI, RBAC,
                     │  (PostgreSQL)    │  Audit Logs, Error Logs)
                     └────────┬─────────┘
                              │ (Direct Query / API Fetch)
       ┌──────────────────────┼──────────────────────┐
       ▼                      ▼                      ▼
┌─────────────┐        ┌─────────────┐        ┌─────────────┐
│   MongoDB   │        │  Redis Cache│        │  Node.js API│
│ (User Data) │        │   & Rooms   │        │  (Service)  │
└─────────────┘        └─────────────┘        └─────────────┘
```

### URL Map
- **`/admin/`** - Django native superuser configuration portal.
- **`/dashboard/`** - Custom operations dashboard home.
- **`/dashboard/users/`** - User accounts management and detail cards.
- **`/dashboard/watchlist/`** - User favorites & watchlist sync.
- **`/dashboard/admins/`** - Admin accounts list, detail, and creation forms.
- **`/dashboard/analytics/`** - Interactive charts dashboard.
- **`/dashboard/monitoring/`** - System health checks.
- **`/dashboard/settings/`** - Global system settings config.
- **`/dashboard/backup/`** - MongoDB collections JSON backup tools.
- **`/dashboard/error-logs/`** - Middleware captured unhandled crash stack-traces.
- **`/dashboard/webhooks/`** - Outgoing events hook manager.
- **`/dashboard/scheduled-tasks/`** - Automatic task runners.
- **`/dashboard/email/`** - Email composer and SMTP logs.
- **`/dashboard/changelog/`** - Feature log entries publisher.

### Template Organization
```
templates/admin_dashboard/
├── base.html                    # Base layout shell (Navbar, Sidebar, Theme, Translation)
├── auth/                        # Admin authentication templates
│   ├── login.html
│   └── register.html
├── dashboard/                   # Main platform analytics & overview
│   └── dashboard.html
├── users/                       # User profile database explorer
│   ├── users_list.html
│   ├── user_detail.html
│   └── user_edit.html
├── watchlist/                   # User watchlist manager
│   ├── watchlist_list.html
│   ├── watchlist_detail.html
│   └── watchlist_edit.html
├── admin/                       # RBAC administration forms & lists
│   ├── admin_list.html
│   ├── admin_create.html
│   ├── admin_detail.html
│   └── admin_edit.html
├── analytics/                   # Data visualizer
│   └── analytics.html
├── audit_logs/                  # Security actions logger
│   └── audit_logs.html
├── backup/                      # Database collections backupper
│   └── backup.html
├── cache/                       # Platform memory cache manager
│   └── cache.html
├── changelog/                   # Site updates release publisher
│   └── changelog.html
├── comments/                    # Comment moderation queue
│   └── comments_list.html
├── content/                     # TV and Movie master indexer
│   └── content_mgmt.html
├── email/                       # Bulk mail broadcaster tool
│   └── email.html
├── error_logs/                  # Crash dump stack trace inspector
│   └── error_logs.html
├── monitoring/                  # Server health ping visualizer
│   └── monitoring.html
├── movies/                      # Movie metadata viewer
│   ├── movies_list.html
│   └── movie_detail.html
├── notifications/               # Platform broadcast notification lists
│   └── notifications_list.html
├── profile/                     # Personal admin profile settings
│   └── profile.html
├── reports/                     # User report cards list
│   └── reports.html
├── rooms/                       # Watch party rooms (Redis active)
│   └── rooms_list.html
├── scheduled_tasks/             # Automated cron actions manager
│   └── scheduled_tasks.html
├── search_history/              # Search keywords queries list
│   └── search_history_list.html
├── settings/                    # Global system-wide configs
│   └── settings.html
├── tokens/                      # Password reset and blacklisted session tokens
│   ├── blacklisted_tokens.html
│   └── password_resets.html
├── tv_shows/                    # TV Series index explorer
│   ├── tv_shows_list.html
│   └── tv_detail.html
├── watch_progress/              # Media play progress checkpoints list
│   └── watch_progress_list.html
└── webhooks/                    # Live outbound event push hooks
    └── webhooks.html
```

---

## 🛠️ Technical Stack

- **Backend Framework**: Django 6.0.5 (Python 3)
- **Databases**:
  - **PostgreSQL**: Stores relational models for Admin Users, Roles, Audit Logs, Error Logs, Webhooks, Changelogs, System Settings, and Backup Logs.
  - **MongoDB**: External document store managing User credentials, Watchlists, Watch Progresses, Search History, Comments, and Notifications.
  - **Redis**: Handles temporary caches (TMDB results) and active Watch Party Rooms.
- **Frontend Utilities**: Bootstrap 5.3.2, Font Awesome 6.5.1, Chart.js 4.4.4.

---

## 📦 Installation & Setup

### 1. Prerequisites
- Python 3.9+
- MongoDB, PostgreSQL, and Redis instances.
- Active Node.js API instance.

### 2. Environment Configuration
Create a `.env` file in the root directory and configure the following variables:
```ini
# Django Security
SECRET_KEY=your-django-secret-key
DEBUG=True

# Relational Database (PostgreSQL)
DATABASE_URL=postgres://user:password@host:port/database

# Document Database (MongoDB)
MONGO_URI=mongodb+srv://user:password@cluster.mongodb.net
MONGO_DB_NAME=movie-platform

# Cache & Rooms (Redis)
REDIS_CACHE_URL=redis://localhost:6379/0
REDIS_ROOMS_URL=redis://localhost:6379/1

# Node.js Streaming API Base URL
NODEJS_API_URL=http://localhost:3001
```

### 3. Execution
```bash
# Clone the repository
git clone <repository-url>
cd admin_en_website/admin_pannel

# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows use: .venv\Scripts\activate

# Install requirements
pip install -r requirements.txt

# Run migrations
python manage.py migrate

# Create the master admin (Superuser)
python manage.py createsuperuser

# Run local development server
python manage.py runserver
```

---

## 🛡️ Security Best Practices
- **No Sensitive Data Stored Locally**: All movie-platform client user data is queried in real-time or securely piped; raw user passwords are never kept in the admin panel database.
- **Crash Reports**: An `ErrorLoggingMiddleware` automatically catches unhandled dashboard errors, writes the traceback stack to SQL, and displays a graceful notice to prevent exposing runtime system internals to administrative users.
