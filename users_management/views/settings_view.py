"""System settings management views — synchronized with MongoDB (`db.systemsettings`)."""
from datetime import datetime

from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.shortcuts import render, redirect

from users_management.decorators import require_permission
from users_management.models import SystemSetting, AuditLog
from users_management.services import mongo_admin
from users_management.views_legacy import check_admin_access

__all__ = ['system_settings']

# Default settings with descriptions
DEFAULT_SETTINGS = [
    ('registration_enabled', True, 'Allow new user registration on app/web'),
    ('watch_rooms_enabled', True, 'Enable Watch Room (Watch Party) feature'),
    ('max_rooms', 50, 'Maximum number of simultaneous watch rooms'),
    ('cache_ttl_seconds', 3600, 'Default TMDB cache TTL in seconds'),
    ('broadcast_template', 'System Announcement', 'Default broadcast notification title'),
    ('maintenance_mode', False, 'Enable maintenance mode (blocks app/web)'),
    ('max_comments_per_user', 100, 'Max comments per user per day'),
]


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('settings.view')
def system_settings(request):
    if request.method == 'POST':
        profile = getattr(request.user, 'admin_profile', None)
        if not (request.user.is_superuser or (profile and profile.has_perm('settings.edit'))):
            messages.error(request, 'Bạn không có quyền chỉnh sửa cấu hình hệ thống.')
            return redirect('admin_dashboard:system_settings')
        synced_dict = {}
        for key, default, desc in DEFAULT_SETTINGS:
            val = request.POST.get(key, '')
            if isinstance(default, bool):
                val = val == 'on'
            elif isinstance(default, int):
                try:
                    val = int(val) if val else default
                except ValueError:
                    val = default
            SystemSetting.set(key, val, desc, request.user)
            synced_dict[key] = val

        # Synchronize directly to MongoDB so Node.js server-en-website reads live settings
        try:
            if mongo_admin.enabled:
                mongo_admin.db().systemsettings.update_one(
                    {'key': 'platform_settings'},
                    {'$set': {
                        'key': 'platform_settings',
                        'settings': synced_dict,
                        'updated_by': request.user.username,
                        'updated_at': datetime.utcnow(),
                    }},
                    upsert=True,
                )
        except Exception:
            pass

        AuditLog.log(request, 'update_settings', 'system', details=synced_dict)
        messages.success(request, 'Đã lưu cấu hình hệ thống và đồng bộ lên cụm MongoDB!')
        return redirect('admin_dashboard:system_settings')

    # Load current settings (prefer MongoDB if available, fallback to SQL)
    mongo_settings = {}
    try:
        if mongo_admin.enabled:
            doc = mongo_admin.db().systemsettings.find_one({'key': 'platform_settings'})
            if doc and isinstance(doc.get('settings'), dict):
                mongo_settings = doc['settings']
    except Exception:
        pass

    settings_data = []
    for key, default, desc in DEFAULT_SETTINGS:
        current = mongo_settings.get(key, SystemSetting.get(key, default))
        settings_data.append({
            'key': key,
            'value': current,
            'default': default,
            'description': desc,
            'type': 'bool' if isinstance(default, bool) else 'int' if isinstance(default, int) else 'str',
        })

    context = {'settings_data': settings_data}
    return render(request, 'admin_dashboard/settings/settings.html', context)
