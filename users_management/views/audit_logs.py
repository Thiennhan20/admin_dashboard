"""Audit logs views."""
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render
from django.utils import timezone

from users_management.decorators import require_permission
from users_management.models import AuditLog
from users_management.views_legacy import check_admin_access

__all__ = ['audit_logs_list']

_ALLOWED_SORTS = {
    '-timestamp', 'timestamp', 'action', '-action', 'admin_username', '-admin_username'
}


def _parse_day(raw_value):
    if not raw_value:
        return None
    try:
        return datetime.strptime(raw_value.strip(), '%Y-%m-%d')
    except ValueError:
        return None


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('audit_logs.view')
def audit_logs_list(request):
    try:
        page = max(1, int(request.GET.get('page', 1) or 1))
    except (TypeError, ValueError):
        page = 1
    limit = 30
    action_filter = request.GET.get('action', '').strip()
    admin_filter = request.GET.get('admin', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    sort = request.GET.get('sort', '-timestamp').strip()
    if sort not in _ALLOWED_SORTS:
        sort = '-timestamp'

    qs = AuditLog.objects.all()
    if action_filter:
        qs = qs.filter(action__icontains=action_filter)
    if admin_filter:
        qs = qs.filter(admin_username__icontains=admin_filter)

    tz = timezone.get_current_timezone() if getattr(settings, 'USE_TZ', False) else None
    start_dt = _parse_day(date_from)
    if start_dt is not None:
        if tz and timezone.is_naive(start_dt):
            start_dt = timezone.make_aware(start_dt, tz)
        qs = qs.filter(timestamp__gte=start_dt)

    end_dt = _parse_day(date_to)
    if end_dt is not None:
        end_exclusive = end_dt + timedelta(days=1)
        if tz and timezone.is_naive(end_exclusive):
            end_exclusive = timezone.make_aware(end_exclusive, tz)
        qs = qs.filter(timestamp__lt=end_exclusive)

    qs = qs.order_by(sort)

    total = qs.count()
    total_pages = max(1, -(-total // limit))
    logs = qs[(page - 1) * limit: page * limit]

    # Get distinct actions for filter dropdown
    distinct_actions = AuditLog.objects.values_list('action', flat=True).distinct()[:50]

    context = {
        'logs': logs, 'page': page, 'total': total,
        'total_pages': total_pages, 'action_filter': action_filter,
        'admin_filter': admin_filter, 'date_from': date_from,
        'date_to': date_to, 'sort': sort,
        'distinct_actions': distinct_actions,
    }
    return render(request, 'admin_dashboard/audit_logs/audit_logs.html', context)
