"""Content management views – Featured Content & Maintenance Notices."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.shortcuts import render, redirect, get_object_or_404

from users_management.models import FeaturedContent, MaintenanceNotice, AuditLog
from users_management.views_legacy import check_admin_access

__all__ = ['content_management', 'content_action']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def content_management(request):
    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'add_featured':
            FeaturedContent.objects.create(
                content_type=request.POST.get('content_type', 'movie'),
                tmdb_id=request.POST.get('tmdb_id', ''),
                title=request.POST.get('title', ''),
                poster_path=request.POST.get('poster_path', ''),
                backdrop_path=request.POST.get('backdrop_path', ''),
                description=request.POST.get('description', ''),
                position=int(request.POST.get('position', 0)),
                is_active=request.POST.get('is_active') == 'on',
                created_by=request.user,
            )
            AuditLog.log(request, 'add_featured_content', 'featured_content')
            messages.success(request, 'Featured content added!')

        elif action == 'add_notice':
            MaintenanceNotice.objects.create(
                title=request.POST.get('title', ''),
                message=request.POST.get('message', ''),
                is_active=request.POST.get('is_active') == 'on',
                created_by=request.user,
            )
            AuditLog.log(request, 'add_maintenance_notice', 'maintenance_notice')
            messages.success(request, 'Maintenance notice created!')

        return redirect('admin_dashboard:content_management')

    context = {
        'featured': FeaturedContent.objects.all(),
        'notices': MaintenanceNotice.objects.all(),
    }
    return render(request, 'admin_dashboard/content/content_mgmt.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def content_action(request, content_type, content_id):
    if request.method != 'POST':
        return redirect('admin_dashboard:content_management')
    action = request.POST.get('action', '')

    if content_type == 'featured':
        obj = get_object_or_404(FeaturedContent, id=content_id)
        if action == 'delete':
            obj.delete()
            messages.success(request, 'Featured content deleted!')
        elif action == 'toggle':
            obj.is_active = not obj.is_active
            obj.save()
            messages.success(request, f'Featured content {"activated" if obj.is_active else "deactivated"}!')
    elif content_type == 'notice':
        obj = get_object_or_404(MaintenanceNotice, id=content_id)
        if action == 'delete':
            obj.delete()
            messages.success(request, 'Notice deleted!')
        elif action == 'toggle':
            obj.is_active = not obj.is_active
            obj.save()
            messages.success(request, f'Notice {"activated" if obj.is_active else "deactivated"}!')

    AuditLog.log(request, f'{action}_{content_type}', content_type, str(content_id))
    return redirect('admin_dashboard:content_management')
