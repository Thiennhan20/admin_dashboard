"""RBAC permission decorators for admin views."""
from functools import wraps

from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.contrib import messages

from .models import AuditLog


def require_permission(perm_code):
    """Decorator that checks if the logged-in admin has a specific permission.

    Usage::

        @require_permission('users.delete')
        def user_delete(request, user_id):
            ...

    Super admins and superusers bypass all checks.
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return redirect('admin_dashboard:login')
            # Superusers always pass
            if user.is_superuser:
                return view_func(request, *args, **kwargs)
            profile = getattr(user, 'admin_profile', None)
            if profile and profile.has_perm(perm_code):
                return view_func(request, *args, **kwargs)
            # Permission denied
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': 'Permission denied'}, status=403)
            messages.error(request, f'Bạn không có quyền thực hiện hành động này ({perm_code}).')
            return redirect('admin_dashboard:dashboard')
        return _wrapped
    return decorator


def log_action(action, target_type='', get_target_id=None, get_details=None):
    """Decorator that automatically logs an admin action after a successful response.

    ``get_target_id`` and ``get_details`` are optional callables receiving
    ``(request, *args, **kwargs)`` and returning the target id / details dict.
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            response = view_func(request, *args, **kwargs)
            # Only log on mutating methods
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
                    pass  # Never break the view because of logging
            return response
        return _wrapped
    return decorator
