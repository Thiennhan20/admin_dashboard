"""Admin notifications API endpoint (bell icon polling)."""
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.utils import timezone

from users_management import services
from users_management.models import ErrorLog
from users_management.services import mongo_admin, clean_value, parallel_run, cached_swr
from users_management.views_legacy import check_admin_access

__all__ = ['api_admin_notifications']


def _safe_recent_errors(cutoff_sql):
    try:
        rows = list(
            ErrorLog.objects.filter(
                timestamp__gte=cutoff_sql,
                level__in=['error', 'critical'],
            ).order_by('-timestamp').values('id', 'level', 'message', 'request_path', 'timestamp')[:5]
        )
        total = ErrorLog.objects.filter(
            timestamp__gte=cutoff_sql,
            level__in=['error', 'critical'],
        ).count()
        return rows, total
    except Exception:
        return [], 0


def _safe_live_rooms():
    try:
        rooms, _err = services.public_rooms()
        return list(rooms or [])
    except Exception:
        return []


def _fetch_admin_notifications():
    db = mongo_admin.db()
    cutoff = datetime.utcnow() - timedelta(hours=24)
    use_tz = getattr(settings, 'USE_TZ', False)
    cutoff_sql = (timezone.now() - timedelta(hours=24)) if use_tz else cutoff

    res = parallel_run({
        'new_users': lambda: clean_value(list(
            db.users.find({'createdAt': {'$gte': cutoff}}, {'name': 1, 'email': 1, 'createdAt': 1})
            .sort('createdAt', -1).limit(5)
        )),
        'new_comments': lambda: clean_value(list(
            db.comments.find({'createdAt': {'$gte': cutoff}}, {'content': 1, 'createdAt': 1})
            .sort('createdAt', -1).limit(5)
        )),
        'pending_reports': lambda: clean_value(list(
            db.userreports.find(
                {'status': {'$in': ['pending', 'reviewing']}},
                {'reporterName': 1, 'reasonCategory': 1, 'reason': 1, 'targetDetail': 1, 'status': 1, 'createdAt': 1},
            ).sort('createdAt', -1).limit(5)
        )),
        'pending_reports_count': lambda: db.userreports.count_documents({'status': 'pending'}),
        'live_rooms': _safe_live_rooms,
    }, max_workers=5)

    # Query relational ErrorLog on request thread to reuse Django's managed connection cleanly
    recent_errors, recent_errors_count = _safe_recent_errors(cutoff_sql)

    events = []
    new_users = res['new_users']
    new_comments = res['new_comments']
    pending_reports = res['pending_reports']
    live_rooms = res.get('live_rooms') or []

    for r in pending_reports:
        snippet = (r.get('targetDetail') or r.get('reason') or '')[:55]
        reason_tag = r.get('reasonCategory') or r.get('reason') or ''
        detail = f"[{reason_tag}] {snippet}".strip() if reason_tag else snippet
        events.append({
            'type': 'report', 'icon': 'fa-flag',
            'detail': detail,
            'title': f"Reported comment: {detail}",
            'url': '/dashboard/reports/?status=pending',
            'time': r.get('createdAt', ''),
        })
    for err in recent_errors:
        detail = (err.get('message') or err.get('request_path') or err.get('level') or '')[:55]
        lvl = (err.get('level') or 'error').lower()
        events.append({
            'type': 'error', 'icon': 'fa-bug',
            'level': lvl,
            'detail': detail,
            'title': f"System {lvl}: {detail}",
            'url': f"/dashboard/error-logs/?level={lvl}",
            'time': clean_value(err.get('timestamp')) or '',
        })
    if live_rooms:
        first = live_rooms[0] if isinstance(live_rooms[0], dict) else {}
        first_title = (first.get('movieTitle') or first.get('title') or first.get('roomName') or first.get('name') or '')[:35]
        detail = f"{len(live_rooms)} ({first_title})" if first_title else f"{len(live_rooms)}"
        room_ts = clean_value(first.get('createdAt') or first.get('updatedAt')) or cutoff.isoformat()
        events.append({
            'type': 'room', 'icon': 'fa-tv',
            'detail': detail,
            'title': f"Live watch rooms: {detail}",
            'url': '/dashboard/rooms/',
            'time': room_ts,
        })
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

    events.sort(key=lambda x: str(x.get('time') or ''), reverse=True)
    return {
        'success': True,
        'events': events[:10],
        'count': (
            len(new_users)
            + len(new_comments)
            + int(res.get('pending_reports_count') or 0)
            + int(recent_errors_count or 0)
            + (1 if live_rooms else 0)
        ),
        'pending_reports': int(res.get('pending_reports_count') or 0),
        'recent_errors': int(recent_errors_count or 0),
        'live_rooms': len(live_rooms),
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
