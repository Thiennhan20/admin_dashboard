import traceback
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from .decorators import is_json_request
from .rate_limit import get_client_ip
from .services import get_admin_profile


def _safe_next_query(request):
    next_url = request.GET.get('next', '').strip()
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return next_url
    return ''


def _deny_response(request, message, redirect_to='admin_dashboard:dashboard', status=403):
    if is_json_request(request):
        return JsonResponse({'error': message, 'ok': False, 'success': False}, status=status)
    messages.error(request, message)
    return redirect(redirect_to)


class AdminAccessMiddleware:
    """Middleware to check admin dashboard access, lock /admin/login/, and enforce RBAC permissions."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path

        # Lock Django's built-in /admin/login/ so attackers cannot bypass Rate Limit or 2FA
        if path.startswith('/admin/login'):
            login_url = reverse('admin_dashboard:login')
            next_url = _safe_next_query(request)
            if next_url:
                return redirect(f'{login_url}?{urlencode({"next": next_url})}')
            return redirect(login_url)

        if path.startswith('/admin/') and not path.startswith('/admin/logout'):
            if not request.user.is_authenticated or not (request.user.is_staff or request.user.is_superuser):
                login_url = reverse('admin_dashboard:login')
                return redirect(f'{login_url}?{urlencode({"next": request.get_full_path()})}')

        public_urls = (
            '/dashboard/login/',
            '/dashboard/logout/',
        )

        if path.startswith('/dashboard/'):
            # Only allow /dashboard/register/ during initial bootstrap (0 staff users)
            # or when authenticated as superuser / super_admin
            if path.startswith('/dashboard/register/'):
                has_any_admin = User.objects.filter(is_staff=True).exists()
                if not has_any_admin:
                    return self.get_response(request)
                profile = get_admin_profile(request.user, request) if request.user.is_authenticated else None
                if request.user.is_authenticated and (
                    request.user.is_superuser
                    or getattr(profile, 'role', '') in ('super_admin', 'superadmin')
                ):
                    return self.get_response(request)
                messages.error(request, 'Đăng ký công khai đã bị khóa. Chỉ Super Admin mới có thể cấp tài khoản quản trị mới!')
                return redirect('admin_dashboard:login')

            if any(path.startswith(url) for url in public_urls):
                return self.get_response(request)

            if not request.user.is_authenticated:
                if is_json_request(request):
                    return JsonResponse({'error': 'Authentication required', 'ok': False, 'success': False}, status=401)
                messages.error(request, 'Bạn cần đăng nhập để truy cập trang quản trị ENTN!')
                return redirect('admin_dashboard:login')

            if not (request.user.is_staff or request.user.is_superuser):
                if is_json_request(request):
                    return JsonResponse({'error': 'Forbidden: Admin access required', 'ok': False, 'success': False}, status=403)
                messages.error(request, 'Bạn không có quyền truy cập bảng điều khiển quản trị!')
                return redirect('admin_dashboard:login')

            # Enforce RBAC for non-superusers
            if not request.user.is_superuser:
                profile = get_admin_profile(request.user, request)
                role = getattr(profile, 'role', '') if profile else ''

                # Viewer cannot execute state-mutating requests (except logout & own profile)
                if role == 'viewer' and request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
                    allowed_viewer_posts = ('/dashboard/logout/', '/dashboard/profile/')
                    if not any(path.startswith(p) for p in allowed_viewer_posts):
                        return _deny_response(
                            request,
                            'Tài khoản Viewer chỉ có quyền xem, không được phép thực hiện thao tác thay đổi dữ liệu.',
                        )

                # Fail closed if non-superuser staff somehow lacks an AdminProfile
                if not profile:
                    if not path.startswith('/dashboard/profile/'):
                        return _deny_response(
                            request,
                            'Tài khoản quản trị chưa được cấp hồ sơ phân quyền.',
                        )
                elif role != 'super_admin':
                    if path.startswith('/dashboard/settings/'):
                        req_perm = 'settings.edit' if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') else 'settings.view'
                        if not profile.has_perm(req_perm):
                            return _deny_response(request, 'Bạn không có quyền truy cập Cấu hình hệ thống.')

                    elif path.startswith('/dashboard/backup/restore/'):
                        if not profile.has_perm('backup.edit'):
                            return _deny_response(request, 'Bạn không có quyền khôi phục bản sao lưu dữ liệu.')

                    elif path.startswith('/dashboard/backup/') or path.startswith('/dashboard/api/backup/'):
                        req_perm = 'backup.export' if request.method == 'POST' else 'backup.view'
                        if not (profile.has_perm(req_perm) or profile.has_perm('backup.edit')):
                            return _deny_response(request, 'Bạn không có quyền truy cập Sao lưu dữ liệu.')

                    elif path.startswith('/dashboard/admins/'):
                        if path.endswith('/delete/'):
                            req_perm = 'admins.delete'
                        elif path.endswith('/edit/') or path.startswith('/dashboard/admins/create/'):
                            req_perm = 'admins.edit'
                        else:
                            req_perm = 'admins.view'
                        if not profile.has_perm(req_perm):
                            redir = 'admin_dashboard:admin_list' if profile.has_perm('admins.view') else 'admin_dashboard:dashboard'
                            return _deny_response(request, 'Bạn không có quyền thao tác trên Quản trị viên.', redirect_to=redir)

                    elif path.startswith('/dashboard/scheduled-tasks/'):
                        req_perm = 'scheduled_tasks.edit' if request.method in ('POST', 'DELETE') else 'scheduled_tasks.view'
                        if not profile.has_perm(req_perm):
                            return _deny_response(request, 'Bạn không có quyền truy cập Tác vụ định kỳ.')

                    elif path.startswith('/dashboard/webhooks/'):
                        req_perm = 'webhooks.edit' if request.method in ('POST', 'DELETE') else 'webhooks.view'
                        if not profile.has_perm(req_perm):
                            return _deny_response(request, 'Bạn không có quyền truy cập Webhooks.')

        return self.get_response(request)


class ErrorLoggingMiddleware:
    """Catch unhandled exceptions in /dashboard/ and persist them to the ErrorLog model."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if not request.path.startswith('/dashboard/'):
            return None
        try:
            from .models import ErrorLog
            ip = get_client_ip(request)
            ErrorLog.objects.create(
                level='error',
                message=str(exception)[:2000],
                traceback_text=traceback.format_exc()[:5000],
                request_path=request.path[:500],
                request_method=request.method,
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
                admin_user=request.user if request.user.is_authenticated else None,
                ip_address=None if ip == 'unknown' else ip,
            )
        except Exception:
            pass  # Never break the error flow
        return None
