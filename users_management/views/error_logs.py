"""Error logs views."""
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from users_management.decorators import require_permission
from users_management.models import ErrorLog
from users_management.services import invalidate_swr
from users_management.views_legacy import check_admin_access

__all__ = ['error_logs_list', 'api_error_logs_clear']


def _parse_day(raw_value):
    if not raw_value:
        return None
    try:
        return datetime.strptime(raw_value.strip(), '%Y-%m-%d')
    except ValueError:
        return None


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('error_logs.view')
def error_logs_list(request):
    try:
        page = max(1, int(request.GET.get('page', 1) or 1))
    except (TypeError, ValueError):
        page = 1
    limit = 20
    level = request.GET.get('level', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()

    use_tz = getattr(settings, 'USE_TZ', False)
    tz = timezone.get_current_timezone() if use_tz else None

    base_qs = ErrorLog.objects.all()
    start_dt = _parse_day(date_from)
    if start_dt is not None:
        if tz and timezone.is_naive(start_dt):
            start_dt = timezone.make_aware(start_dt, tz)
        base_qs = base_qs.filter(timestamp__gte=start_dt)

    end_dt = _parse_day(date_to)
    if end_dt is not None:
        end_exclusive = end_dt + timedelta(days=1)
        if tz and timezone.is_naive(end_exclusive):
            end_exclusive = timezone.make_aware(end_exclusive, tz)
        base_qs = base_qs.filter(timestamp__lt=end_exclusive)

    qs = base_qs.filter(level=level) if level else base_qs

    agg = base_qs.aggregate(
        error=Count('id', filter=Q(level='error')),
        warning=Count('id', filter=Q(level='warning')),
        critical=Count('id', filter=Q(level='critical')),
        total_all=Count('id'),
    )
    total = qs.count() if level else agg['total_all']
    total_pages = max(1, -(-total // limit))
    logs = qs[(page - 1) * limit: page * limit]

    now_local = timezone.localtime(timezone.now()) if use_tz else datetime.now()
    today_date = now_local.date()
    start_7d_naive = datetime.combine(today_date - timedelta(days=6), datetime.min.time())
    start_7d = timezone.make_aware(start_7d_naive, tz) if tz else start_7d_naive
    buckets = {}
    for offset in range(7):
        d = today_date - timedelta(days=6 - offset)
        buckets[d] = {
            'label': d.strftime('%m-%d'),
            'iso': d.isoformat(),
            'error': 0,
            'warning': 0,
            'critical': 0,
            'total': 0,
        }
    for ts, lvl in ErrorLog.objects.filter(timestamp__gte=start_7d).values_list('timestamp', 'level'):
        if not ts:
            continue
        dt = timezone.localtime(ts) if (use_tz and timezone.is_aware(ts)) else ts
        d_key = dt.date()
        if d_key in buckets:
            bucket_key = lvl if lvl in ('error', 'warning', 'critical') else 'error'
            buckets[d_key][bucket_key] += 1
            buckets[d_key]['total'] += 1
    trend_days = list(buckets.values())
    max_trend = max((b['total'] for b in trend_days), default=1) or 1
    for b in trend_days:
        b['pct'] = round((b['total'] / max_trend) * 100) if b['total'] else 0

    context = {
        'logs': logs, 'page': page, 'total': total,
        'total_pages': total_pages, 'level': level,
        'date_from': date_from, 'date_to': date_to,
        'trend_days': trend_days,
        'trend_7d_total': sum(b['total'] for b in trend_days),
        'stats': {
            'error': agg['error'],
            'warning': agg['warning'],
            'critical': agg['critical'],
        },
    }
    return render(request, 'admin_dashboard/error_logs/error_logs.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('error_logs.delete')
def api_error_logs_clear(request):
    if request.method != 'POST':
        return JsonResponse({'success': False}, status=405)
    from users_management.models import AuditLog
    count, _ = ErrorLog.objects.all().delete()
    invalidate_swr('admin_sidebar_counts')
    invalidate_swr('admin_bell_notifs_v1')
    AuditLog.log(request, 'clear_error_logs', 'error_log', '', {'count': count})
    return JsonResponse({'success': True, 'deleted': count})
