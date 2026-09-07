"""Custom context processors for the admin dashboard."""
from django.core.cache import cache


def admin_context(request):
    """Inject admin-related context into every template.

    Provides:
    - ``admin_profile``: the logged-in admin's profile (or None)
    - ``admin_notif_count``: approximate count of recent system events
    - ``admin_role``: current role string
    - ``sidebar_counts``: dictionary of live item counts for sidebar badges
    """
    if not request.user.is_authenticated:
        return {}

    profile = getattr(request.user, 'admin_profile', None)
    role = profile.role if profile else ('super_admin' if request.user.is_superuser else 'admin')

    # Cached counts for sidebar badges and notification bell (TTL 20 seconds)
    cache_key = 'admin_sidebar_counts'
    data = cache.get(cache_key)

    if not data:
        notif_count = 0
        sidebar_counts = {
            'users': 0,
            'comments': 0,
            'watch_progress': 0,
            'rooms': 0,
            'game_profiles': 0,
            'error_logs': 0,
        }
        try:
            from .services import mongo_admin, public_rooms
            from .models import ErrorLog
            from datetime import datetime, timedelta

            if mongo_admin.enabled:
                db = mongo_admin.db()
                cutoff = datetime.utcnow() - timedelta(hours=24)
                try:
                    notif_count = (
                        db.users.count_documents({'createdAt': {'$gte': cutoff}})
                        + db.comments.count_documents({'createdAt': {'$gte': cutoff}})
                    )
                except Exception:
                    pass

                try:
                    sidebar_counts['users'] = db.users.estimated_document_count()
                except Exception:
                    pass
                try:
                    sidebar_counts['comments'] = db.comments.estimated_document_count()
                except Exception:
                    pass
                try:
                    sidebar_counts['watch_progress'] = db.watchprogresses.estimated_document_count()
                except Exception:
                    pass
                try:
                    sidebar_counts['game_profiles'] = db.gameprofiles.estimated_document_count()
                except Exception:
                    pass

            try:
                rooms, _ = public_rooms()
                sidebar_counts['rooms'] = len(rooms) if rooms else 0
            except Exception:
                pass

            try:
                sidebar_counts['error_logs'] = ErrorLog.objects.filter(level='error').count()
            except Exception:
                pass

        except Exception:
            pass

        data = {
            'admin_notif_count': min(notif_count, 99),
            'sidebar_counts': sidebar_counts,
        }
        cache.set(cache_key, data, 20)  # 20 seconds TTL

    return {
        'admin_profile': profile,
        'admin_role': role,
        'admin_notif_count': data.get('admin_notif_count', 0),
        'sidebar_counts': data.get('sidebar_counts', {}),
    }

