"""RBAC permission decorators and request helpers for admin views."""
from functools import wraps

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect

from .models import AuditLog
from .services import get_admin_profile


def is_json_request(request):
    """Detect AJAX / fetch / API requests that expect JSON instead of HTML redirects."""
    accept = request.headers.get('Accept', '')
    content_type = request.content_type or ''
    return (
        request.path.startswith('/dashboard/api/')
        or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or 'application/json' in accept
        or 'application/json' in content_type
    )


def require_permission(perm_codes):
    """Decorator verifying that the logged-in admin holds the required RBAC permission(s).

    Accepts a single permission string (e.g. ``'users.delete'``) or an iterable of
    permission strings where holding any one grants access.
    Superusers and ``super_admin`` profiles bypass all checks.
    """
    codes = (perm_codes,) if isinstance(perm_codes, str) else tuple(perm_codes)

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                if is_json_request(request):
                    return JsonResponse({'success': False, 'ok': False, 'error': 'Authentication required'}, status=401)
                return redirect('admin_dashboard:login')

            if not (user.is_staff or user.is_superuser):
                if is_json_request(request):
                    return JsonResponse({'success': False, 'ok': False, 'error': 'Admin access required'}, status=403)
                messages.error(request, 'Bạn không có quyền truy cập bảng điều khiển quản trị!')
                return redirect('admin_dashboard:login')

            if user.is_superuser:
                return view_func(request, *args, **kwargs)

            profile = get_admin_profile(user, request)
            if profile and any(profile.has_perm(code) for code in codes):
                return view_func(request, *args, **kwargs)

            perm_label = ', '.join(codes)
            if is_json_request(request):
                return JsonResponse(
                    {'success': False, 'ok': False, 'error': f'Permission denied ({perm_label})'},
                    status=403,
                )
            messages.error(request, 'Bạn không có quyền thực hiện thao tác này.')
            return redirect('admin_dashboard:dashboard')

        return _wrapped

    return decorator


def require_super_admin(view_func):
    """Restrict access strictly to Django superusers or profiles with role == 'super_admin'."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            if is_json_request(request):
                return JsonResponse({'success': False, 'ok': False, 'error': 'Authentication required'}, status=401)
            return redirect('admin_dashboard:login')

        profile = get_admin_profile(user, request)
        if user.is_superuser or (profile and profile.role == 'super_admin'):
            return view_func(request, *args, **kwargs)

        if is_json_request(request):
            return JsonResponse({'success': False, 'ok': False, 'error': 'Super Admin access required'}, status=403)
        messages.error(request, 'Chỉ Super Admin mới có quyền thực hiện thao tác này.')
        return redirect('admin_dashboard:dashboard')

    return _wrapped


def log_action(action, target_type='', get_target_id=None, get_details=None):
    """Decorator that automatically logs an admin action after a successful response."""
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            response = view_func(request, *args, **kwargs)
            if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                tid = ''
                details = {}
                if get_target_id:
                    try:
                        tid = get_target_id(request, *args, **kwargs)
                    except Exception:
                        pass
                if get_details:
                    try:
                        details = get_details(request, *args, **kwargs)
                    except Exception:
                        pass
                try:
                    AuditLog.log(request, action, target_type, tid, details)
                except Exception:
                    pass
            return response

        return _wrapped

    return decorator
