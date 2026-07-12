"""Error logs views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render

from users_management.models import ErrorLog
from users_management.views_legacy import check_admin_access

__all__ = ['error_logs_list', 'api_error_logs_clear']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def error_logs_list(request):
    page = int(request.GET.get('page', 1))
    limit = 20
    level = request.GET.get('level', '')

    qs = ErrorLog.objects.all()
    if level:
        qs = qs.filter(level=level)

    total = qs.count()
    total_pages = max(1, -(-total // limit))
    logs = qs[(page - 1) * limit: page * limit]

    context = {
        'logs': logs, 'page': page, 'total': total,
        'total_pages': total_pages, 'level': level,
        'stats': {
            'error': ErrorLog.objects.filter(level='error').count(),
            'warning': ErrorLog.objects.filter(level='warning').count(),
            'critical': ErrorLog.objects.filter(level='critical').count(),
        },
    }
    return render(request, 'admin_dashboard/error_logs/error_logs.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def api_error_logs_clear(request):
    if request.method != 'POST':
        return JsonResponse({'success': False}, status=405)
    from users_management.models import AuditLog
    count = ErrorLog.objects.count()
    ErrorLog.objects.all().delete()
    AuditLog.log(request, 'clear_error_logs', 'error_log', '', {'count': count})
    return JsonResponse({'success': True, 'deleted': count})
