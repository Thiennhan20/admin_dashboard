"""Audit logs views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render

from users_management.models import AuditLog
from users_management.views_legacy import check_admin_access

__all__ = ['audit_logs_list']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def audit_logs_list(request):
    page = int(request.GET.get('page', 1))
    limit = 30
    action_filter = request.GET.get('action', '')
    admin_filter = request.GET.get('admin', '')

    qs = AuditLog.objects.all()
    if action_filter:
        qs = qs.filter(action__icontains=action_filter)
    if admin_filter:
        qs = qs.filter(admin_username__icontains=admin_filter)

    total = qs.count()
    total_pages = max(1, -(-total // limit))
    logs = qs[(page - 1) * limit: page * limit]

    # Get distinct actions for filter dropdown
    distinct_actions = AuditLog.objects.values_list('action', flat=True).distinct()[:50]

    context = {
        'logs': logs, 'page': page, 'total': total,
        'total_pages': total_pages, 'action_filter': action_filter,
        'admin_filter': admin_filter, 'distinct_actions': distinct_actions,
    }
    return render(request, 'admin_dashboard/audit_logs/audit_logs.html', context)
