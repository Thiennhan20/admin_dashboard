"""User reports management views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.shortcuts import render, redirect, get_object_or_404

from users_management.models import UserReport, AuditLog
from users_management.views_legacy import check_admin_access

__all__ = ['reports_list', 'report_action']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def reports_list(request):
    page = int(request.GET.get('page', 1))
    limit = 20
    status_filter = request.GET.get('status', '')

    qs = UserReport.objects.all()
    if status_filter:
        qs = qs.filter(status=status_filter)

    total = qs.count()
    total_pages = max(1, -(-total // limit))
    reports = qs[(page - 1) * limit: page * limit]

    context = {
        'reports': reports, 'page': page, 'total': total,
        'total_pages': total_pages, 'status_filter': status_filter,
        'stats': {
            'pending': UserReport.objects.filter(status='pending').count(),
            'reviewing': UserReport.objects.filter(status='reviewing').count(),
            'resolved': UserReport.objects.filter(status='resolved').count(),
            'dismissed': UserReport.objects.filter(status='dismissed').count(),
        },
    }
    return render(request, 'admin_dashboard/reports/reports.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
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
        AuditLog.log(request, f'report_{action}', 'user_report', str(report_id))
        messages.success(request, f'Report marked as {action}!')

    return redirect('admin_dashboard:reports')
