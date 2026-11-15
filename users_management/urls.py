from django.urls import path
from . import views

app_name = 'admin_dashboard'

urlpatterns = [
    # ── Authentication ──────────────────────────────────────────────
    path('login/', views.admin_login, name='login'),
    path('login/2fa/', views.admin_login_2fa, name='login_2fa'),
    path('login/2fa/cancel/', views.admin_login_2fa_cancel, name='login_2fa_cancel'),
    path('logout/', views.admin_logout, name='logout'),
    path('register/', views.admin_register, name='register'),

    # ── Dashboard ───────────────────────────────────────────────────
    path('', views.operations_dashboard, name='dashboard'),

    # ── Users ───────────────────────────────────────────────────────
    path('users/', views.users_list, name='users_list'),
    path('users/<str:user_id>/', views.user_detail, name='user_detail'),
    path('users/<str:user_id>/edit/', views.user_edit, name='user_edit'),
    path('users/<str:user_id>/delete/', views.user_delete, name='user_delete'),

    # ── Watchlist ───────────────────────────────────────────────────
    path('watchlist/', views.watchlist_list, name='watchlist_list'),
    path('watchlist/<str:item_id>/', views.watchlist_detail, name='watchlist_detail'),
    path('watchlist/<str:item_id>/edit/', views.watchlist_edit, name='watchlist_edit'),
    path('watchlist/<str:item_id>/delete/', views.watchlist_delete, name='watchlist_delete'),
    path('watchlist/cleanup/', views.cleanup_watchlist, name='cleanup_watchlist'),

    # ── Admin Accounts ──────────────────────────────────────────────
    path('admins/', views.admin_list, name='admin_list'),
    path('admins/create/', views.admin_create, name='admin_create'),
    path('admins/<int:admin_id>/', views.admin_detail, name='admin_detail'),
    path('admins/<int:admin_id>/edit/', views.admin_edit, name='admin_edit'),
    path('admins/<int:admin_id>/delete/', views.admin_delete, name='admin_delete'),
    path('admins/<int:admin_id>/reset-2fa/', views.admin_reset_2fa, name='admin_reset_2fa'),

    # ── Media ───────────────────────────────────────────────────────
    path('movies/', views.movies_list, name='movies'),
    path('movies/<str:media_id>/', views.movie_detail, name='movie_detail'),
    path('tv-shows/', views.tv_shows_list, name='tv_shows'),
    path('tv-shows/<str:media_id>/', views.tv_detail, name='tv_detail'),
    path('comments/', views.comments_list, name='comments'),
    path('rooms/', views.rooms_list, name='rooms'),
    path('notifications/', views.notifications_list, name='notifications'),
    path('push-devices/', views.push_devices_list, name='push_devices'),
    path('api/push-devices/test/', views.api_push_devices_test, name='api_push_devices_test'),
    path('api/push-devices/remove/', views.api_push_devices_remove, name='api_push_devices_remove'),
    path('cache/', views.cache_management, name='cache'),
    path('servers/', views.server_management, name='servers'),
    path('api/servers/probe/', views.api_servers_probe, name='api_servers_probe'),
    path('api/servers/vidsrc/scan/', views.api_vidsrc_scan, name='api_vidsrc_scan'),
    path('api/servers/vidsrc/select/', views.api_vidsrc_select, name='api_vidsrc_select'),
    path('api/servers/vidsrc/toggle-auto/', views.api_vidsrc_toggle_auto, name='api_vidsrc_toggle_auto'),

    # ── NEW: Watch Progress & Stream History ────────────────────────
    path('watch-progress/', views.watch_progress_list, name='watch_progress'),
    path('stream-history/', views.stream_history_list, name='stream_history'),
    path('api/stream-history/probe/', views.api_stream_history_probe, name='api_stream_history_probe'),
    path('api/stream-history/delete/', views.api_stream_history_delete, name='api_stream_history_delete'),

    # ── GAME MANAGEMENT (3D Arena, Profiles & Daily Challenge) ───────
    path('games/profiles/', views.game_profiles_list, name='game_profiles'),
    path('games/histories/', views.game_histories_list, name='game_histories'),
    path('games/daily-challenge/', views.daily_challenge_list, name='daily_challenge'),
    path('api/games/profiles/update/', views.api_game_profile_update, name='api_game_profile_update'),
    path('api/games/profiles/delete/', views.api_game_profile_delete, name='api_game_profile_delete'),
    path('api/games/histories/delete/', views.api_game_history_delete, name='api_game_history_delete'),
    path('api/games/daily-challenge/', views.api_daily_challenge_action, name='api_daily_challenge_action'),
    path('api/games/arena/', views.api_arena_control, name='api_arena_control'),

    # ── NEW: Search History ─────────────────────────────────────────
    path('search-history/', views.search_history_list, name='search_history'),

    # ── NEW: Token Management & Sessions ────────────────────────────
    path('sessions/', views.sessions_list, name='sessions_list'),
    path('password-resets/', views.password_resets_list, name='password_resets'),
    path('blacklisted-tokens/', views.blacklisted_tokens_list, name='blacklisted_tokens'),

    # ── NEW: Admin Profile & 2FA ────────────────────────────────────
    path('profile/', views.admin_profile, name='admin_profile'),
    path('profile/2fa/setup/', views.setup_2fa, name='profile_2fa_setup'),
    path('profile/2fa/enable/', views.enable_2fa, name='profile_2fa_enable'),
    path('profile/2fa/disable/', views.disable_2fa, name='profile_2fa_disable'),
    path('profile/2fa/backup-codes/', views.regenerate_backup_codes, name='profile_2fa_backup_codes'),

    # ── NEW: Analytics ──────────────────────────────────────────────
    path('analytics/', views.analytics_dashboard, name='analytics'),

    # ── NEW: Audit Logs ─────────────────────────────────────────────
    path('audit-logs/', views.audit_logs_list, name='audit_logs'),

    # ── NEW: System ─────────────────────────────────────────────────
    path('monitoring/', views.monitoring_dashboard, name='monitoring'),
    path('settings/', views.system_settings, name='system_settings'),
    path('backup/', views.backup_management, name='backup'),
    path('backup/restore/preview/', views.backup_restore_preview, name='backup_restore_preview'),
    path('backup/restore/confirm/', views.backup_restore_confirm, name='backup_restore_confirm'),
    path('backup/restore/cancel/', views.backup_restore_cancel, name='backup_restore_cancel'),
    path('error-logs/', views.error_logs_list, name='error_logs'),

    # ── NEW: Content & Extensions ───────────────────────────────────
    path('content/', views.content_management, name='content_management'),
    path('content/<str:content_type>/<int:content_id>/', views.content_action, name='content_action'),
    path('reports/', views.reports_list, name='reports'),
    path('reports/<int:report_id>/', views.report_action, name='report_action'),
    path('changelog/', views.changelog_list, name='changelog'),
    path('changelog/<int:entry_id>/', views.changelog_action, name='changelog_action'),
    path('email/', views.email_management, name='email'),
    path('scheduled-tasks/', views.scheduled_tasks_list, name='scheduled_tasks'),
    path('scheduled-tasks/<int:task_id>/run/', views.scheduled_task_run, name='scheduled_task_run'),
    path('webhooks/', views.webhooks_list, name='webhooks'),
    path('webhooks/<int:webhook_id>/', views.webhook_action, name='webhook_action'),
    path('webhooks/<int:webhook_id>/test/', views.webhook_test, name='webhook_test'),

    # ── API Endpoints ───────────────────────────────────────────────
    path('api/refresh/', views.api_sync_from_api, name='api_refresh'),
    path('api/overview/', views.api_dashboard_overview, name='api_overview'),
    path('api/cache/', views.api_cache_action, name='api_cache_action'),
    path('api/cache/list/', views.api_cache_list, name='api_cache_list'),
    path('api/mongo/<str:collection>/', views.api_mongo_collection, name='api_mongo_collection'),
    path('api/mongo/<str:collection>/<str:document_id>/<str:action>/', views.api_mongo_action, name='api_mongo_action'),

    # ── NEW API Endpoints ───────────────────────────────────────────
    path('api/bulk-action/', views.api_bulk_action, name='api_bulk_action'),
    path('api/user-ban/', views.api_user_ban, name='api_user_ban'),
    path('api/global-search/', views.api_global_search, name='api_global_search'),
    path('api/analytics/', views.api_analytics_data, name='api_analytics'),
    path('api/health-check/', views.api_health_check, name='api_health_check'),
    path('api/admin-notifications/', views.api_admin_notifications, name='api_admin_notifications'),
    path('api/watch-progress/delete/', views.api_watch_progress_delete, name='api_watch_progress_delete'),
    path('api/search-history/delete/', views.api_search_history_delete, name='api_search_history_delete'),
    path('api/tokens/cleanup/', views.api_tokens_cleanup, name='api_tokens_cleanup'),
    path('api/tokens/delete/', views.api_tokens_delete, name='api_tokens_delete'),
    path('api/error-logs/clear/', views.api_error_logs_clear, name='api_error_logs_clear'),
    path('api/export/<str:collection_name>/', views.export_collection, name='api_export'),
    path('api/backup/download/', views.backup_download, name='backup_download'),
]
