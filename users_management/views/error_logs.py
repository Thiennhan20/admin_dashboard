"""Error logs views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import render

from users_management.decorators import require_permission
from users_management.models import ErrorLog
from users_management.services import invalidate_swr
from users_management.views_legacy import check_admin_access

__all__ = ['error_logs_list', 'api_error_logs_clear']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('error_logs.view')
def error_logs_list(request):
    page = max(1, int(request.GET.get('page', 1) or 1))
    limit = 20
    level = request.GET.get('level', '')

    qs = ErrorLog.objects.all()
    if level:
        qs = qs.filter(level=level)

    agg = ErrorLog.objects.aggregate(
        error=Count('id', filter=Q(level='error')),
        warning=Count('id', filter=Q(level='warning')),
        critical=Count('id', filter=Q(level='critical')),
        total_all=Count('id'),
    )
    total = qs.count() if level else agg['total_all']
    total_pages = max(1, -(-total // limit))
    logs = qs[(page - 1) * limit: page * limit]

    context = {
        'logs': logs, 'page': page, 'total': total,
        'total_pages': total_pages, 'level': level,
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
    AuditLog.log(request, 'clear_error_logs', 'error_log', '', {'count': count})
    return JsonResponse({'success': True, 'deleted': count})
