"""Changelog management views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.shortcuts import render, redirect, get_object_or_404

from users_management.models import ChangelogEntry, AuditLog
from users_management.views_legacy import check_admin_access

__all__ = ['changelog_list', 'changelog_action']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def changelog_list(request):
    if request.method == 'POST' and request.POST.get('action') == 'create':
        ChangelogEntry.objects.create(
            version=request.POST.get('version', ''),
            title=request.POST.get('title', ''),
            content=request.POST.get('content', ''),
            category=request.POST.get('category', 'feature'),
            created_by=request.user,
        )
        AuditLog.log(request, 'create_changelog', 'changelog')
        messages.success(request, 'Changelog entry created!')
        return redirect('admin_dashboard:changelog')

    entries = ChangelogEntry.objects.all()
    context = {'entries': entries}
    return render(request, 'admin_dashboard/changelog/changelog.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def changelog_action(request, entry_id):
    if request.method != 'POST':
        return redirect('admin_dashboard:changelog')
    entry = get_object_or_404(ChangelogEntry, id=entry_id)
    action = request.POST.get('action', '')
    if action == 'delete':
        entry.delete()
        messages.success(request, 'Entry deleted!')
    elif action == 'toggle':
        entry.is_published = not entry.is_published
        entry.save()
        messages.success(request, f'Entry {"published" if entry.is_published else "unpublished"}!')
    AuditLog.log(request, f'changelog_{action}', 'changelog', str(entry_id))
    return redirect('admin_dashboard:changelog')
