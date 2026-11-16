"""Admin profile & 2FA TOTP management views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth import update_session_auth_hash
from django.contrib import messages
from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST

from users_management.models import AdminProfile, AuditLog
from users_management.totp import (
    generate_totp_secret,
    build_otpauth_uri,
    render_qr_svg_data_uri,
    verify_totp_code,
    generate_backup_codes,
)
from users_management.views_legacy import check_admin_access

__all__ = [
    'admin_profile',
    'setup_2fa',
    'enable_2fa',
    'disable_2fa',
    'regenerate_backup_codes',
]

_2FA_SETUP_SESSION_KEY = '_2fa_setup_secret'
_2FA_BACKUP_FLASH_KEY = '_2fa_new_backup_codes'


def _get_or_create_profile(user):
    profile, _ = AdminProfile.objects.get_or_create(
        user=user,
        defaults={'role': 'super_admin' if user.is_superuser else 'admin'},
    )
    return profile


def _is_valid_admin_avatar(value):
    if not value:
        return True
    if len(value) > 400000:
        return False
    lower = value.lower()
    if lower.startswith(('https://', 'http://')):
        return True
    return lower.startswith((
        'data:image/png;base64,',
        'data:image/jpeg;base64,',
        'data:image/jpg;base64,',
        'data:image/webp;base64,',
        'data:image/gif;base64,',
    ))


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def admin_profile(request):
    """View/edit the logged-in admin's own profile & 2FA TOTP settings."""
    profile = _get_or_create_profile(request.user)

    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'update_profile':
            if 'avatar' in request.POST:
                avatar = (request.POST.get('avatar') or '').strip()
                if not _is_valid_admin_avatar(avatar):
                    messages.error(request, 'Định dạng hoặc kích thước Avatar không hợp lệ!')
                    return redirect('admin_dashboard:admin_profile')
                profile.avatar = avatar
                profile.save(update_fields=['avatar', 'updated_at'])
            request.user.first_name = request.POST.get('first_name', '')
            request.user.last_name = request.POST.get('last_name', '')
            request.user.email = request.POST.get('email', request.user.email)
            request.user.save()
            AuditLog.log(request, 'update_profile', 'admin', str(request.user.id))
            messages.success(request, 'Đã cập nhật hồ sơ cá nhân thành công!')
            return redirect('admin_dashboard:admin_profile')

        elif action == 'change_password':
            current = request.POST.get('current_password', '')
            new_pw = request.POST.get('new_password', '')
            confirm = request.POST.get('confirm_password', '')
            if not request.user.check_password(current):
                messages.error(request, 'Mật khẩu hiện tại không đúng!')
            elif new_pw != confirm:
                messages.error(request, 'Mật khẩu xác nhận không khớp!')
            elif len(new_pw) < 8:
                messages.error(request, 'Mật khẩu mới phải có ít nhất 8 ký tự!')
            else:
                request.user.set_password(new_pw)
                request.user.save()
                update_session_auth_hash(request, request.user)
                AuditLog.log(request, 'change_password', 'admin', str(request.user.id))
                messages.success(request, 'Đã đổi mật khẩu thành công!')
            return redirect('admin_dashboard:admin_profile')

    pending_secret = request.session.get(_2FA_SETUP_SESSION_KEY, '') if not profile.totp_enabled else ''
    tfa_otpauth_uri = ''
    tfa_qr_data_uri = ''
    if pending_secret:
        tfa_otpauth_uri = build_otpauth_uri(pending_secret, request.user.username)
        tfa_qr_data_uri = render_qr_svg_data_uri(tfa_otpauth_uri)

    tfa_new_backup_codes = request.session.pop(_2FA_BACKUP_FLASH_KEY, None)
    recent_logs = AuditLog.objects.filter(admin_user=request.user).order_by('-timestamp')[:20]

    context = {
        'profile': profile,
        'recent_logs': recent_logs,
        'tfa_pending_secret': pending_secret,
        'tfa_otpauth_uri': tfa_otpauth_uri,
        'tfa_qr_data_uri': tfa_qr_data_uri,
        'tfa_new_backup_codes': tfa_new_backup_codes,
        'tfa_backup_remaining': len(profile.totp_backup_codes or []),
    }
    return render(request, 'admin_dashboard/profile/profile.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_POST
def setup_2fa(request):
    """Generate a temporary TOTP secret in session for QR scanning."""
    if request.POST.get('cancel') == '1':
        request.session.pop(_2FA_SETUP_SESSION_KEY, None)
        messages.info(request, 'Đã hủy thiết lập 2FA.')
        return redirect('admin_dashboard:admin_profile')

    profile = _get_or_create_profile(request.user)
    if profile.totp_enabled:
        messages.info(request, '2FA đã được kích hoạt trên tài khoản này.')
        return redirect('admin_dashboard:admin_profile')

    secret = generate_totp_secret()
    request.session[_2FA_SETUP_SESSION_KEY] = secret
    request.session.modified = True
    return redirect('admin_dashboard:admin_profile')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_POST
def enable_2fa(request):
    """Verify the first 6-digit OTP against session secret, enable 2FA & issue backup codes."""
    profile = _get_or_create_profile(request.user)
    secret = request.session.get(_2FA_SETUP_SESSION_KEY, '')
    otp_code = (request.POST.get('otp_code') or request.POST.get('code') or '').strip()

    if not secret:
        messages.error(request, 'Chưa khởi tạo mã bí mật 2FA. Vui lòng bấm Thiết lập 2FA lại.')
        return redirect('admin_dashboard:admin_profile')

    if not verify_totp_code(secret, otp_code):
        messages.error(request, 'Mã OTP 6 số không đúng hoặc đã hết hạn. Vui lòng thử lại!')
        return redirect('admin_dashboard:admin_profile')

    plain_codes, hashed_codes = generate_backup_codes(count=8)
    profile.totp_secret = secret
    profile.totp_enabled = True
    profile.totp_backup_codes = hashed_codes
    profile.save(update_fields=['totp_secret', 'totp_enabled', 'totp_backup_codes', 'updated_at'])

    request.session.pop(_2FA_SETUP_SESSION_KEY, None)
    request.session.pop('_require_2fa_setup', None)
    request.session[_2FA_BACKUP_FLASH_KEY] = plain_codes
    request.session.modified = True

    AuditLog.log(request, 'enable_2fa', 'admin', str(request.user.id))
    messages.success(request, 'Đã bật xác thực 2 bước (2FA TOTP) thành công! Hãy lưu ngay các mã dự phòng bên dưới.')
    return redirect('admin_dashboard:admin_profile')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_POST
def disable_2fa(request):
    """Verify current password and disable 2FA TOTP on own account."""
    profile = _get_or_create_profile(request.user)
    if request.user.is_superuser or profile.role == 'super_admin':
        messages.error(request, 'Tài khoản Super Admin bắt buộc phải duy trì xác thực 2FA TOTP, không thể tắt!')
        return redirect('admin_dashboard:admin_profile')
    current_password = request.POST.get('current_password') or ''
    if not request.user.check_password(current_password):
        messages.error(request, 'Mật khẩu hiện tại không đúng, không thể tắt 2FA!')
        return redirect('admin_dashboard:admin_profile')

    profile.totp_enabled = False
    profile.totp_secret = ''
    profile.totp_backup_codes = []
    profile.save(update_fields=['totp_enabled', 'totp_secret', 'totp_backup_codes', 'updated_at'])
    request.session.pop(_2FA_SETUP_SESSION_KEY, None)
    request.session.pop(_2FA_BACKUP_FLASH_KEY, None)

    AuditLog.log(request, 'disable_2fa', 'admin', str(request.user.id))
    messages.success(request, 'Đã tắt xác thực 2 bước (2FA) cho tài khoản của bạn.')
    return redirect('admin_dashboard:admin_profile')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_POST
def regenerate_backup_codes(request):
    """Replace existing 2FA recovery codes after password confirmation."""
    profile = _get_or_create_profile(request.user)
    if not profile.totp_enabled:
        messages.error(request, 'Bạn cần bật 2FA trước khi tạo lại mã dự phòng.')
        return redirect('admin_dashboard:admin_profile')

    current_password = request.POST.get('current_password') or ''
    if not request.user.check_password(current_password):
        messages.error(request, 'Mật khẩu hiện tại không đúng!')
        return redirect('admin_dashboard:admin_profile')

    plain_codes, hashed_codes = generate_backup_codes(count=8)
    profile.totp_backup_codes = hashed_codes
    profile.save(update_fields=['totp_backup_codes', 'updated_at'])
    request.session[_2FA_BACKUP_FLASH_KEY] = plain_codes
    request.session.modified = True

    AuditLog.log(request, 'regenerate_backup_codes', 'admin', str(request.user.id))
    messages.success(request, 'Đã tạo 8 mã dự phòng mới (các mã cũ đã bị vô hiệu).')
    return redirect('admin_dashboard:admin_profile')
