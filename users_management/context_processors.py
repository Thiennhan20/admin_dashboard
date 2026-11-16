"""Custom context processors for the admin dashboard."""
from datetime import datetime, timedelta


def _fetch_sidebar_counts():
    from .services import mongo_admin, public_rooms, parallel_run
    from .models import ErrorLog

    notif_count = 0
    sidebar_counts = {
        'users': 0,
        'comments': 0,
        'reports': 0,
        'watch_progress': 0,
        'stream_history': 0,
        'rooms': 0,
        'game_profiles': 0,
        'daily_challenges': 0,
        'error_logs': 0,
        'sessions': 0,
        'push_devices': 0,
    }

    def _count_push_devices():
        res = list(mongo_admin.db().users.aggregate([
            {'$match': {'pushTokens.0': {'$exists': True}}},
            {'$group': {'_id': None, 'total': {'$sum': {'$size': '$pushTokens'}}}},
        ]))
        return res[0].get('total', 0) if res else 0

    def safe(fn, default=0):
        def _runner():
            try:
                return fn()
            except Exception:
                return default
        return _runner

    tasks = {
        'rooms': safe(lambda: len(public_rooms()[0] or []), 0),
        'error_logs': safe(lambda: ErrorLog.objects.filter(level='error').count(), 0),
    }

    if mongo_admin.enabled:
        db = mongo_admin.db()
        cutoff = datetime.utcnow() - timedelta(hours=24)
        tasks.update({
            'notif_users': safe(lambda: db.users.count_documents({'createdAt': {'$gte': cutoff}}), 0),
            'notif_comments': safe(lambda: db.comments.count_documents({'createdAt': {'$gte': cutoff}}), 0),
            'notif_reports': safe(lambda: db.userreports.count_documents({'status': 'pending'}), 0),
            'users': safe(lambda: db.users.estimated_document_count(), 0),
            'comments': safe(lambda: db.comments.estimated_document_count(), 0),
            'reports': safe(lambda: db.userreports.count_documents({'status': {'$in': ['pending', 'reviewing']}}), 0),
            'watch_progress': safe(lambda: db.watchprogresses.estimated_document_count(), 0),
            'stream_history': safe(lambda: db.streamhistories.estimated_document_count(), 0),
            'game_profiles': safe(lambda: db.gameprofiles.estimated_document_count(), 0),
            'daily_challenges': safe(lambda: db.dailychallenges.estimated_document_count(), 0),
            'sessions': safe(lambda: db.sessions.estimated_document_count(), 0),
            'push_devices': safe(_count_push_devices, 0),
        })

    results = parallel_run(tasks, max_workers=6)
    notif_count = results.get('notif_users', 0) + results.get('notif_comments', 0) + results.get('notif_reports', 0)
    for k in sidebar_counts:
        sidebar_counts[k] = results.get(k, 0)

    return {
        'admin_notif_count': min(notif_count, 99),
        'sidebar_counts': sidebar_counts,
    }


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

    from .services import cached_swr, get_admin_profile

    profile = get_admin_profile(request.user, request)
    role = profile.role if profile else ('super_admin' if request.user.is_superuser else 'admin')

    # Stale-While-Revalidate cache: 60s fresh, 10min stale background refresh
    data = cached_swr(
        'admin_sidebar_counts',
        fresh_ttl=60,
        stale_ttl=600,
        fetcher=_fetch_sidebar_counts,
        non_blocking_default={'admin_notif_count': 0, 'sidebar_counts': {}},
    )

    return {
        'admin_profile': profile,
        'admin_role': role,
        'admin_notif_count': data.get('admin_notif_count', 0),
        'sidebar_counts': data.get('sidebar_counts', {}),
    }

