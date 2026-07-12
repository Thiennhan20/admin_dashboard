"""Admin profile page views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth import update_session_auth_hash
from django.contrib import messages
from django.shortcuts import render, redirect

from users_management.models import AdminProfile, AuditLog
from users_management.views_legacy import check_admin_access

__all__ = ['admin_profile']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def admin_profile(request):
    """View/edit the logged-in admin's own profile."""
    profile, _ = AdminProfile.objects.get_or_create(
        user=request.user,
        defaults={'role': 'super_admin' if request.user.is_superuser else 'admin'},
    )

    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'update_profile':
            request.user.first_name = request.POST.get('first_name', '')
            request.user.last_name = request.POST.get('last_name', '')
            request.user.email = request.POST.get('email', request.user.email)
            request.user.save()
            avatar = request.POST.get('avatar', '')
            if avatar:
                profile.avatar = avatar
                profile.save()
            AuditLog.log(request, 'update_profile', 'admin', str(request.user.id))
            messages.success(request, 'Profile updated successfully!')
            return redirect('admin_dashboard:admin_profile')

        elif action == 'change_password':
            current = request.POST.get('current_password', '')
            new_pw = request.POST.get('new_password', '')
            confirm = request.POST.get('confirm_password', '')
            if not request.user.check_password(current):
                messages.error(request, 'Current password is incorrect!')
            elif new_pw != confirm:
                messages.error(request, 'New passwords do not match!')
            elif len(new_pw) < 8:
                messages.error(request, 'Password must be at least 8 characters!')
            else:
                request.user.set_password(new_pw)
                request.user.save()
                update_session_auth_hash(request, request.user)
                AuditLog.log(request, 'change_password', 'admin', str(request.user.id))
                messages.success(request, 'Password changed successfully!')
            return redirect('admin_dashboard:admin_profile')

    # Recent audit logs for this admin
    recent_logs = AuditLog.objects.filter(admin_user=request.user).order_by('-timestamp')[:20]

    context = {
        'profile': profile,
        'recent_logs': recent_logs,
    }
    return render(request, 'admin_dashboard/profile/profile.html', context)
