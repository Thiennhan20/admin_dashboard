"""Custom context processors for the admin dashboard."""


def admin_context(request):
    """Inject admin-related context into every template.

    Provides:
    - ``admin_profile``: the logged-in admin's profile (or None)
    - ``admin_notif_count``: approximate count of recent system events
    - ``admin_role``: current role string
    """
    if not request.user.is_authenticated:
        return {}

    profile = getattr(request.user, 'admin_profile', None)
    role = profile.role if profile else ('super_admin' if request.user.is_superuser else 'admin')

    # Quick notification count — lightweight query
    notif_count = 0
    try:
        from .services import mongo_admin
        if mongo_admin.enabled:
            from datetime import datetime, timedelta
            cutoff = datetime.utcnow() - timedelta(hours=24)
            db = mongo_admin.db()
            notif_count = (
                db.users.count_documents({'createdAt': {'$gte': cutoff}})
                + db.comments.count_documents({'createdAt': {'$gte': cutoff}})
            )
    except Exception:
        pass

    return {
        'admin_profile': profile,
        'admin_role': role,
        'admin_notif_count': min(notif_count, 99),
    }
