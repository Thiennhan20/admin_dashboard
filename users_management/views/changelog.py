"""Changelog management views — synced to MongoDB & integrated with user broadcast notifications."""
from datetime import datetime

from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import render, redirect, get_object_or_404

from users_management.decorators import require_permission
from users_management.models import ChangelogEntry, AuditLog
from users_management.services import mongo_admin
from users_management.views_legacy import check_admin_access

__all__ = ['changelog_list', 'changelog_action']


def _sync_changelogs_to_mongo(broadcast_entry=None):
    try:
        if not mongo_admin.enabled:
            return
        db = mongo_admin.db()
        published = [
            {
                'id': e.id,
                'version': e.version,
                'title': e.title,
                'content': e.content,
                'category': e.category,
                'created_at': e.created_at.isoformat() if e.created_at else None,
            }
            for e in ChangelogEntry.objects.filter(is_published=True).order_by('-created_at')
        ]
        db.systemsettings.update_one(
            {'key': 'changelogs'},
            {'$set': {'key': 'changelogs', 'entries': published, 'updated_at': datetime.utcnow()}},
            upsert=True,
        )
        # If broadcast_entry is provided, send a version_updated notification to all users
        if broadcast_entry:
            now = datetime.utcnow()
            user_ids = [u['_id'] for u in db.users.find({}, {'_id': 1})]
            if user_ids:
                docs = [
                    {
                        'recipient': uid,
                        'type': 'version_updated',
                        'metadata': {
                            'version': broadcast_entry.version,
                            'title': broadcast_entry.title,
                            'message': broadcast_entry.content[:300],
                            'category': broadcast_entry.category,
                        },
                        'read': False,
                        'createdAt': now,
                    }
                    for uid in user_ids
                ]
                db.notifications.insert_many(docs)
    except Exception:
        pass


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('changelog.view')
def changelog_list(request):
    if request.method == 'POST' and request.POST.get('action') == 'create':
        profile = getattr(request.user, 'admin_profile', None)
        if not (request.user.is_superuser or (profile and profile.has_perm('changelog.edit'))):
            raise PermissionDenied('Permission changelog.edit required')
        entry = ChangelogEntry.objects.create(
            version=request.POST.get('version', ''),
            title=request.POST.get('title', ''),
            content=request.POST.get('content', ''),
            category=request.POST.get('category', 'feature'),
            created_by=request.user,
        )
        broadcast = request.POST.get('broadcast_users') != 'off'
        _sync_changelogs_to_mongo(broadcast_entry=entry if broadcast else None)
        AuditLog.log(request, 'create_changelog', 'changelog', str(entry.id), {'version': entry.version})
        messages.success(request, f'Đã xuất bản nhật ký {entry.version} và gửi thông báo tới người dùng!')
        return redirect('admin_dashboard:changelog')

    entries = ChangelogEntry.objects.all()
    context = {'entries': entries}
    return render(request, 'admin_dashboard/changelog/changelog.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('changelog.edit')
def changelog_action(request, entry_id):
    if request.method != 'POST':
        return redirect('admin_dashboard:changelog')
    entry = get_object_or_404(ChangelogEntry, id=entry_id)
    action = request.POST.get('action', '')
    if action == 'delete':
        profile = getattr(request.user, 'admin_profile', None)
        if not (request.user.is_superuser or (profile and profile.has_perm('changelog.delete'))):
            raise PermissionDenied('Permission changelog.delete required')
    if action == 'delete':
        entry.delete()
        messages.success(request, 'Đã xóa mục nhật ký thay đổi!')
    elif action == 'toggle':
        entry.is_published = not entry.is_published
        entry.save()
        messages.success(request, f'Đã {"xuất bản" if entry.is_published else "gỡ xuất bản"} mục nhật ký!')
    _sync_changelogs_to_mongo()
    AuditLog.log(request, f'changelog_{action}', 'changelog', str(entry_id))
    return redirect('admin_dashboard:changelog')
