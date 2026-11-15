"""Content management views – Featured Content & Maintenance Notices (synced to MongoDB)."""
from datetime import datetime

from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import render, redirect, get_object_or_404

from users_management.decorators import require_permission
from users_management.models import FeaturedContent, MaintenanceNotice, AuditLog
from users_management.services import mongo_admin
from users_management.views_legacy import check_admin_access

__all__ = ['content_management', 'content_action']


def _sync_content_to_mongo():
    """Synchronize FeaturedContent and MaintenanceNotice records into MongoDB `systemsettings`."""
    try:
        if not mongo_admin.enabled:
            return
        db = mongo_admin.db()
        featured_items = [
            {
                'id': f.id,
                'content_type': f.content_type,
                'tmdb_id': f.tmdb_id,
                'title': f.title,
                'poster_path': f.poster_path,
                'backdrop_path': f.backdrop_path,
                'description': f.description,
                'position': f.position,
                'is_active': f.is_active,
            }
            for f in FeaturedContent.objects.filter(is_active=True).order_by('position', '-created_at')
        ]
        active_notices = [
            {
                'id': n.id,
                'title': n.title,
                'message': n.message,
                'is_active': n.is_active,
                'created_at': n.created_at.isoformat() if n.created_at else None,
            }
            for n in MaintenanceNotice.objects.filter(is_active=True).order_by('-created_at')
        ]
        db.systemsettings.update_one(
            {'key': 'featured_content'},
            {'$set': {'key': 'featured_content', 'items': featured_items, 'updated_at': datetime.utcnow()}},
            upsert=True,
        )
        db.systemsettings.update_one(
            {'key': 'maintenance_notices'},
            {'$set': {'key': 'maintenance_notices', 'notices': active_notices, 'updated_at': datetime.utcnow()}},
            upsert=True,
        )
    except Exception:
        pass


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('content.view')
def content_management(request):
    if request.method == 'POST':
        profile = getattr(request.user, 'admin_profile', None)
        if not (request.user.is_superuser or (profile and profile.has_perm('content.edit'))):
            raise PermissionDenied('Permission content.edit required')
        action = request.POST.get('action', '')

        if action == 'add_featured':
            FeaturedContent.objects.create(
                content_type=request.POST.get('content_type', 'movie'),
                tmdb_id=request.POST.get('tmdb_id', ''),
                title=request.POST.get('title', ''),
                poster_path=request.POST.get('poster_path', ''),
                backdrop_path=request.POST.get('backdrop_path', ''),
                description=request.POST.get('description', ''),
                position=int(request.POST.get('position', 0) or 0),
                is_active=request.POST.get('is_active') == 'on',
                created_by=request.user,
            )
            _sync_content_to_mongo()
            AuditLog.log(request, 'add_featured_content', 'featured_content')
            messages.success(request, 'Đã thêm nội dung nổi bật và đồng bộ lên MongoDB!')

        elif action == 'add_notice':
            MaintenanceNotice.objects.create(
                title=request.POST.get('title', ''),
                message=request.POST.get('message', ''),
                is_active=request.POST.get('is_active') == 'on',
                created_by=request.user,
            )
            _sync_content_to_mongo()
            AuditLog.log(request, 'add_maintenance_notice', 'maintenance_notice')
            messages.success(request, 'Đã tạo thông báo bảo trì và đồng bộ lên MongoDB!')

        return redirect('admin_dashboard:content_management')

    context = {
        'featured': FeaturedContent.objects.all(),
        'notices': MaintenanceNotice.objects.all(),
    }
    return render(request, 'admin_dashboard/content/content_mgmt.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('content.edit')
def content_action(request, content_type, content_id):
    if request.method != 'POST':
        return redirect('admin_dashboard:content_management')
    action = request.POST.get('action', '')
    if action == 'delete':
        profile = getattr(request.user, 'admin_profile', None)
        if not (request.user.is_superuser or (profile and profile.has_perm('content.delete'))):
            raise PermissionDenied('Permission content.delete required')

    if content_type == 'featured':
        obj = get_object_or_404(FeaturedContent, id=content_id)
        if action == 'delete':
            obj.delete()
            messages.success(request, 'Đã xóa nội dung nổi bật!')
        elif action == 'toggle':
            obj.is_active = not obj.is_active
            obj.save()
            messages.success(request, f'Đã {"bật" if obj.is_active else "tắt"} nội dung nổi bật!')
    elif content_type == 'notice':
        obj = get_object_or_404(MaintenanceNotice, id=content_id)
        if action == 'delete':
            obj.delete()
            messages.success(request, 'Đã xóa thông báo bảo trì!')
        elif action == 'toggle':
            obj.is_active = not obj.is_active
            obj.save()
            messages.success(request, f'Đã {"bật" if obj.is_active else "tắt"} thông báo bảo trì!')

    _sync_content_to_mongo()
    AuditLog.log(request, f'{action}_{content_type}', content_type, str(content_id))
    return redirect('admin_dashboard:content_management')
