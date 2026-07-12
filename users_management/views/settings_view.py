"""System settings management views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.shortcuts import render, redirect

from users_management.models import SystemSetting, AuditLog
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
def system_settings(request):
    if not request.user.is_superuser:
        messages.error(request, 'Only Super Admin can access System Settings!')
        return redirect('admin_dashboard:dashboard')

    if request.method == 'POST':
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
        AuditLog.log(request, 'update_settings', 'system')
        messages.success(request, 'Settings saved successfully!')
        return redirect('admin_dashboard:system_settings')

    # Load current settings
    settings_data = []
    for key, default, desc in DEFAULT_SETTINGS:
        current = SystemSetting.get(key, default)
        settings_data.append({
            'key': key,
            'value': current,
            'default': default,
            'description': desc,
            'type': 'bool' if isinstance(default, bool) else 'int' if isinstance(default, int) else 'str',
        })

    context = {'settings_data': settings_data}
    return render(request, 'admin_dashboard/settings/settings.html', context)
