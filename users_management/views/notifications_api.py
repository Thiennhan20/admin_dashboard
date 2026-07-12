"""Admin notifications API endpoint (bell icon polling)."""
from datetime import datetime, timedelta

from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse

from users_management.services import mongo_admin, clean_value
from users_management.views_legacy import check_admin_access

__all__ = ['api_admin_notifications']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def api_admin_notifications(request):
    """Returns recent system events for the notification bell."""
    try:
        db = mongo_admin.db()
        cutoff = datetime.utcnow() - timedelta(hours=24)
        events = []

        # New users in last 24h
        new_users = clean_value(list(
            db.users.find({'createdAt': {'$gte': cutoff}}, {'name': 1, 'email': 1, 'createdAt': 1})
            .sort('createdAt', -1).limit(5)
        ))
        for u in new_users:
            events.append({
                'type': 'user', 'icon': 'fa-user-plus',
                'title': f"New user: {u.get('name', u.get('email', 'Unknown'))}",
                'time': u.get('createdAt', ''),
            })

        # New comments in last 24h
        new_comments = clean_value(list(
            db.comments.find({'createdAt': {'$gte': cutoff}}, {'content': 1, 'createdAt': 1})
            .sort('createdAt', -1).limit(5)
        ))
        for c in new_comments:
            events.append({
                'type': 'comment', 'icon': 'fa-comment',
                'title': f"New comment: {c.get('content', '')[:50]}",
                'time': c.get('createdAt', ''),
            })

        # Sort all events by time
        events.sort(key=lambda x: x.get('time', ''), reverse=True)

        return JsonResponse({
            'success': True,
            'events': events[:10],
            'count': len(new_users) + len(new_comments),
        })
    except Exception as e:
        return JsonResponse({'success': False, 'events': [], 'count': 0, 'error': str(e)})
