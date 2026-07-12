import traceback

from django.shortcuts import redirect
from django.urls import reverse
from django.contrib import messages


class AdminAccessMiddleware:
    """Middleware to check admin dashboard access permissions."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        public_urls = [
            '/dashboard/login/',
            '/dashboard/register/',
            '/admin/',
            '/admin/login/',
            '/admin/logout/',
        ]

        if request.path.startswith('/dashboard/'):
            if any(request.path.startswith(url) for url in public_urls):
                response = self.get_response(request)
                return response

            if not request.user.is_authenticated:
                messages.error(request, 'Bạn cần đăng nhập để truy cập admin dashboard!')
                return redirect('admin_dashboard:login')

            if not (request.user.is_staff or request.user.is_superuser):
                messages.error(request, 'Bạn không có quyền truy cập admin dashboard!')
                return redirect('admin_dashboard:login')

        response = self.get_response(request)
        return response


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
            ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', ''))
            if ',' in ip:
                ip = ip.split(',')[0].strip()
            ErrorLog.objects.create(
                level='error',
                message=str(exception)[:2000],
                traceback_text=traceback.format_exc()[:5000],
                request_path=request.path[:500],
                request_method=request.method,
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
                admin_user=request.user if request.user.is_authenticated else None,
                ip_address=ip or None,
            )
        except Exception:
            pass  # Never break the error flow
        return None