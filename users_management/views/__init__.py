"""Views package – re-exports every view so urls.py keeps working unchanged."""

# ── Legacy views (all existing functionality) ───────────────────────
from users_management.views_legacy import (           # noqa: F401
    admin_login, admin_logout, admin_register,
    operations_dashboard, api_dashboard_overview,
    api_mongo_collection, api_mongo_action,
    cleanup_watchlist,
    admin_list, admin_create, admin_detail, admin_edit, admin_delete,
    movies_list, tv_shows_list, comments_list,
    rooms_list, notifications_list,
    cache_management, api_cache_list, api_cache_action,
    api_sync_from_api,
)

from users_management.views.users import *             # noqa: F401,F403
from users_management.views.watchlist import *         # noqa: F401,F403

# ── New views ───────────────────────────────────────────────────────
from users_management.views.watch_progress import *    # noqa: F401,F403
from users_management.views.search_history import *    # noqa: F401,F403
from users_management.views.token_mgmt import *        # noqa: F401,F403
from users_management.views.profile import *           # noqa: F401,F403
from users_management.views.media_detail import *      # noqa: F401,F403
from users_management.views.analytics import *         # noqa: F401,F403
from users_management.views.export_views import *      # noqa: F401,F403
from users_management.views.global_search import *     # noqa: F401,F403
from users_management.views.monitoring import *        # noqa: F401,F403
from users_management.views.settings_view import *     # noqa: F401,F403
from users_management.views.backup import *            # noqa: F401,F403
from users_management.views.error_logs import *        # noqa: F401,F403
from users_management.views.content_mgmt import *      # noqa: F401,F403
from users_management.views.reports import *           # noqa: F401,F403
from users_management.views.changelog import *         # noqa: F401,F403
from users_management.views.email_mgmt import *        # noqa: F401,F403
from users_management.views.scheduled_tasks import *   # noqa: F401,F403
from users_management.views.webhooks import *          # noqa: F401,F403
from users_management.views.api import *               # noqa: F401,F403
from users_management.views.audit_logs import *        # noqa: F401,F403
from users_management.views.notifications_api import * # noqa: F401,F403
from users_management.views.servers import *          # noqa: F401,F403
from users_management.views.games import *            # noqa: F401,F403
