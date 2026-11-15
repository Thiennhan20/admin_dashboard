"""User reports management views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import render, redirect, get_object_or_404

from users_management.decorators import require_permission
from users_management.models import UserReport, AuditLog
from users_management.views_legacy import check_admin_access

__all__ = ['reports_list', 'report_action']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('reports.view')
def reports_list(request):
    page = max(1, int(request.GET.get('page', 1) or 1))
    limit = 20
    status_filter = request.GET.get('status', '')

    qs = UserReport.objects.all()
    if status_filter:
        qs = qs.filter(status=status_filter)

    agg = UserReport.objects.aggregate(
        pending=Count('id', filter=Q(status='pending')),
        reviewing=Count('id', filter=Q(status='reviewing')),
        resolved=Count('id', filter=Q(status='resolved')),
        dismissed=Count('id', filter=Q(status='dismissed')),
        total_all=Count('id'),
    )
    total = qs.count() if status_filter else agg['total_all']
    total_pages = max(1, -(-total // limit))
    reports = qs[(page - 1) * limit: page * limit]

    context = {
        'reports': reports, 'page': page, 'total': total,
        'total_pages': total_pages, 'status_filter': status_filter,
        'stats': {
            'pending': agg['pending'],
            'reviewing': agg['reviewing'],
            'resolved': agg['resolved'],
            'dismissed': agg['dismissed'],
        },
    }
    return render(request, 'admin_dashboard/reports/reports.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('reports.edit')
def report_action(request, report_id):
    if request.method != 'POST':
        return redirect('admin_dashboard:reports')
    report = get_object_or_404(UserReport, id=report_id)
    action = request.POST.get('action', '')

    if action in ('reviewing', 'resolved', 'dismissed'):
        report.status = action
        if action in ('resolved', 'dismissed'):
            report.resolved_by = request.user
            report.resolution_note = request.POST.get('note', '')
        report.save()
        status_labels = {
            'reviewing': 'đang xem xét',
            'resolved': 'đã xử lý',
            'dismissed': 'đã bỏ qua',
        }
        AuditLog.log(request, f'report_{action}', 'user_report', str(report_id))
        messages.success(request, f'Đã chuyển trạng thái báo cáo sang {status_labels.get(action, action)}!')

    return redirect('admin_dashboard:reports')
