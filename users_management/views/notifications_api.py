"""Admin notifications API endpoint (bell icon polling)."""
from datetime import datetime, timedelta

from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse

from users_management.services import mongo_admin, clean_value, parallel_run, cached_swr
from users_management.views_legacy import check_admin_access

__all__ = ['api_admin_notifications']


def _fetch_admin_notifications():
    db = mongo_admin.db()
    cutoff = datetime.utcnow() - timedelta(hours=24)

    res = parallel_run({
        'new_users': lambda: clean_value(list(
            db.users.find({'createdAt': {'$gte': cutoff}}, {'name': 1, 'email': 1, 'createdAt': 1})
            .sort('createdAt', -1).limit(5)
        )),
        'new_comments': lambda: clean_value(list(
            db.comments.find({'createdAt': {'$gte': cutoff}}, {'content': 1, 'createdAt': 1})
            .sort('createdAt', -1).limit(5)
        )),
    }, max_workers=2)

    events = []
    new_users = res['new_users']
    new_comments = res['new_comments']
    for u in new_users:
        detail = u.get('name') or u.get('email') or ''
        events.append({
            'type': 'user', 'icon': 'fa-user-plus',
            'detail': detail,
            'title': f"New user: {detail or 'Unknown'}",
            'time': u.get('createdAt', ''),
        })
    for c in new_comments:
        detail = (c.get('content') or '')[:50]
        events.append({
            'type': 'comment', 'icon': 'fa-comment',
            'detail': detail,
            'title': f"New comment: {detail}",
            'time': c.get('createdAt', ''),
        })

    events.sort(key=lambda x: x.get('time', ''), reverse=True)
    return {
        'success': True,
        'events': events[:10],
        'count': len(new_users) + len(new_comments),
    }


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def api_admin_notifications(request):
    """Returns recent system events for the notification bell."""
    try:
        force = request.GET.get('refresh') == '1'
        data = cached_swr(
            'admin_bell_notifs_v1',
            fresh_ttl=30,
            stale_ttl=120,
            fetcher=_fetch_admin_notifications,
            force=force,
        )
        return JsonResponse(data)
    except Exception as e:
        return JsonResponse({'success': False, 'events': [], 'count': 0, 'error': str(e)})
