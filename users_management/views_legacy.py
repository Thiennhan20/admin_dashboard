from django.conf import settings
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib import messages
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.decorators import user_passes_test
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from urllib.parse import urlencode
from bson import ObjectId
from .models import (
    AdminProfile, ROLE_DEFAULTS, MODULES, ACTIONS, MODULE_LABELS,
    AuditLog, get_rbac_matrix,
)
from .decorators import require_permission, require_super_admin
from .rate_limit import (
    check_rate_limit, consume_action_rate_limit, record_failure, clear_failures,
    format_retry_wait, get_client_ip,
)
from .totp import verify_totp_code, verify_and_consume_backup_code
from .services import (
    MONGO_COLLECTIONS, mongo_admin, public_rooms, server_health,
    arena_health, runtime_system_info, clean_value,
    check_api_status as _service_check_api_status, parallel_run, cached_swr,
    build_pagination_context, invalidate_swr,
)
import requests
import json
from datetime import datetime, timedelta
from django.db import models

# API configuration
API_BASE_URL = settings.API_AUTH_BASE_URL

_2FA_SESSION_KEYS = ('_2fa_user_id', '_2fa_username', '_2fa_backend', '_2fa_expires_at', '_2fa_next')
_2FA_SESSION_TTL_SECONDS = 600


def _clear_pending_2fa_session(request):
    for key in _2FA_SESSION_KEYS:
        request.session.pop(key, None)


def _safe_next_url(request, raw_next):
    if raw_next and url_has_allowed_host_and_scheme(
        raw_next,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return raw_next
    return ''


def is_admin_user(user):
    """Kiểm tra user có phải là admin không"""
    return user.is_authenticated and (user.is_staff or user.is_superuser)


def admin_login(request):
    """Trang đăng nhập cho admin dashboard (kèm Rate Limit & 2FA TOTP challenge)."""
    next_url = _safe_next_url(request, request.POST.get('next') or request.GET.get('next') or '')
    if request.user.is_authenticated and is_admin_user(request.user):
        messages.info(request, f'Bạn đã đăng nhập với tài khoản {request.user.username}')
        return redirect(next_url or 'admin_dashboard:dashboard')

    allow_bootstrap_register = not User.objects.filter(is_staff=True).exists()
    entered_username = ''

    if request.method == 'POST':
        _clear_pending_2fa_session(request)
        username = (request.POST.get('username') or '').strip()
        password = request.POST.get('password') or ''
        entered_username = username

        allowed, retry_after, reason = check_rate_limit('admin_login', request, username)
        if not allowed:
            wait_str = format_retry_wait(retry_after)
            messages.error(
                request,
                f'Quá nhiều lần đăng nhập thất bại ({reason}). Vui lòng thử lại sau {wait_str}.',
            )
            return render(
                request,
                'admin_dashboard/auth/login.html',
                {
                    'allow_bootstrap_register': allow_bootstrap_register,
                    'username': entered_username,
                    'next': next_url,
                    'rate_limited_seconds': retry_after,
                },
                status=429,
            )

        if not username or not password:
            messages.error(request, 'Vui lòng nhập đầy đủ thông tin!')
            return render(
                request,
                'admin_dashboard/auth/login.html',
                {
                    'allow_bootstrap_register': allow_bootstrap_register,
                    'username': entered_username,
                    'next': next_url,
                },
            )

        user = authenticate(request, username=username, password=password)

        if user is not None:
            if is_admin_user(user):
                clear_failures('admin_login', request, username)
                profile = AdminProfile.objects.filter(user=user).first()
                if profile and profile.totp_enabled and profile.totp_secret:
                    request.session['_2fa_user_id'] = user.pk
                    request.session['_2fa_username'] = user.username
                    request.session['_2fa_backend'] = getattr(
                        user, 'backend', 'django.contrib.auth.backends.ModelBackend'
                    )
                    request.session['_2fa_expires_at'] = int(timezone.now().timestamp()) + _2FA_SESSION_TTL_SECONDS
                    request.session['_2fa_next'] = next_url
                    request.session.modified = True
                    return redirect('admin_dashboard:login_2fa')

                login(request, user)
                try:
                    AuditLog.log(request, 'login', 'auth', str(user.pk), {'username': user.username, 'method': 'password'})
                except Exception:
                    pass
                is_super = bool(user.is_superuser or (profile and profile.role == 'super_admin'))
                if is_super and getattr(settings, 'ENFORCE_SUPERADMIN_2FA', True):
                    request.session['_require_2fa_setup'] = True
                    request.session.modified = True
                    messages.warning(request, 'Tài khoản Super Admin bắt buộc phải kích hoạt 2FA TOTP trước khi truy cập hệ thống!')
                    return redirect('admin_dashboard:admin_profile')
                messages.success(request, f'Chào mừng {user.username}! Đăng nhập thành công.')
                return redirect(next_url or 'admin_dashboard:dashboard')
            else:
                record_failure('admin_login', request, username)
                messages.error(request, 'Tài khoản này không có quyền truy cập trang quản trị!')
        else:
            remaining, locked_now, lock_wait = record_failure('admin_login', request, username)
            if locked_now:
                wait_str = format_retry_wait(lock_wait)
                messages.error(
                    request,
                    f'Tên đăng nhập hoặc mật khẩu không đúng! Tài khoản/IP đã bị tạm khóa {wait_str}.',
                )
                return render(
                    request,
                    'admin_dashboard/auth/login.html',
                    {
                        'allow_bootstrap_register': allow_bootstrap_register,
                        'username': entered_username,
                        'next': next_url,
                        'rate_limited_seconds': lock_wait,
                    },
                    status=429,
                )
            extra = f' (Còn {remaining} lần thử)' if remaining <= 2 else ''
            messages.error(request, f'Tên đăng nhập hoặc mật khẩu không đúng!{extra}')

    return render(
        request,
        'admin_dashboard/auth/login.html',
        {
            'allow_bootstrap_register': allow_bootstrap_register,
            'username': entered_username,
            'next': next_url,
        },
    )


def admin_login_2fa(request):
    """Bước xác thực OTP 2FA TOTP / Backup Code sau khi kiểm tra mật khẩu thành công."""
    if request.user.is_authenticated and is_admin_user(request.user):
        return redirect('admin_dashboard:dashboard')

    uid = request.session.get('_2fa_user_id')
    expires_at = request.session.get('_2fa_expires_at') or 0
    if not uid or int(timezone.now().timestamp()) > int(expires_at):
        _clear_pending_2fa_session(request)
        messages.error(request, 'Phiên xác thực 2FA không tồn tại hoặc đã hết hạn. Vui lòng đăng nhập lại.')
        return redirect('admin_dashboard:login')

    user = User.objects.filter(pk=uid, is_active=True).first()
    profile = AdminProfile.objects.filter(user=user).first() if user else None
    if not user or not is_admin_user(user) or not (profile and profile.totp_enabled and profile.totp_secret):
        _clear_pending_2fa_session(request)
        messages.error(request, 'Trạng thái 2FA không hợp lệ. Vui lòng đăng nhập lại.')
        return redirect('admin_dashboard:login')

    remaining_backup_codes = len(profile.totp_backup_codes or [])

    if request.method == 'POST':
        allowed, retry_after, reason = check_rate_limit('admin_2fa', request, user.username)
        if not allowed:
            wait_str = format_retry_wait(retry_after)
            messages.error(
                request,
                f'Quá nhiều lần nhập sai mã 2FA ({reason}). Vui lòng thử lại sau {wait_str}.',
            )
            return render(
                request,
                'admin_dashboard/auth/login_2fa.html',
                {
                    'pending_username': user.username,
                    'remaining_backup_codes': remaining_backup_codes,
                    'rate_limited_seconds': retry_after,
                },
                status=429,
            )

        code = (request.POST.get('code') or '').strip()
        used_backup = False
        valid = verify_totp_code(profile.totp_secret, code)
        if not valid:
            used_backup = verify_and_consume_backup_code(profile, code)
            valid = used_backup

        if valid:
            clear_failures('admin_2fa', request, user.username)
            next_url = _safe_next_url(request, request.session.get('_2fa_next') or '')
            backend = request.session.get('_2fa_backend') or 'django.contrib.auth.backends.ModelBackend'
            _clear_pending_2fa_session(request)
            login(request, user, backend=backend)
            try:
                AuditLog.log(
                    request,
                    'login',
                    'auth',
                    str(user.pk),
                    {'username': user.username, 'method': 'totp_backup' if used_backup else 'totp'},
                )
            except Exception:
                pass
            if used_backup:
                profile.refresh_from_db(fields=['totp_backup_codes'])
                left = len(profile.totp_backup_codes or [])
                messages.warning(
                    request,
                    f'Đăng nhập bằng mã dự phòng thành công! Bạn còn {left} mã dự phòng chưa dùng.',
                )
            else:
                messages.success(request, f'Chào mừng {user.username}! Xác thực 2FA thành công.')
            return redirect(next_url or 'admin_dashboard:dashboard')

        remaining, locked_now, lock_wait = record_failure('admin_2fa', request, user.username)
        if locked_now:
            wait_str = format_retry_wait(lock_wait)
            messages.error(
                request,
                f'Mã OTP / dự phòng không đúng! Đã tạm khóa xác thực trong {wait_str}.',
            )
            return render(
                request,
                'admin_dashboard/auth/login_2fa.html',
                {
                    'pending_username': user.username,
                    'remaining_backup_codes': remaining_backup_codes,
                    'rate_limited_seconds': lock_wait,
                },
                status=429,
            )
        messages.error(request, f'Mã OTP hoặc mã dự phòng không đúng! Còn {remaining} lần thử.')

    return render(
        request,
        'admin_dashboard/auth/login_2fa.html',
        {
            'pending_username': user.username,
            'remaining_backup_codes': remaining_backup_codes,
        },
    )


@require_POST
def admin_login_2fa_cancel(request):
    """Hủy bước 2FA đang chờ và quay lại trang đăng nhập."""
    _clear_pending_2fa_session(request)
    messages.info(request, 'Đã hủy phiên xác thực 2FA.')
    return redirect('admin_dashboard:login')


def admin_logout(request):
    """Đăng xuất khỏi admin dashboard"""
    if request.user.is_authenticated:
        try:
            AuditLog.log(request, 'logout', 'auth', str(request.user.pk), {'username': request.user.username})
        except Exception:
            pass
    _clear_pending_2fa_session(request)
    logout(request)
    messages.success(request, 'Đã đăng xuất thành công!')
    return redirect('admin_dashboard:login')


def admin_register(request):
    """Trang đăng ký admin mới - Chỉ mở khi hệ thống chưa có Admin (Bootstrap) hoặc được tạo bởi Superuser"""
    has_any_admin = User.objects.filter(is_staff=True).exists()
    if has_any_admin and not (request.user.is_authenticated and request.user.is_superuser):
        messages.error(request, 'Tính năng tự đăng ký đã đóng để bảo mật. Vui lòng liên hệ Super Admin!')
        return redirect('admin_dashboard:login')

    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_staff = True
            user.is_superuser = not has_any_admin
            user.save()

            role = 'super_admin' if not has_any_admin else 'admin'
            AdminProfile.objects.update_or_create(
                user=user,
                defaults={'role': role, 'permissions': {}},
            )

            messages.success(request, f'Đã khởi tạo tài khoản quản trị: {user.username}.')
            return redirect('admin_dashboard:login')
        else:
            for error in form.errors.values():
                messages.error(request, error)
    else:
        form = UserCreationForm()

    return render(request, 'admin_dashboard/auth/register.html', {'form': form})

def check_admin_access(user):
    """Check if user has admin access"""
    return user.is_staff or user.is_superuser


_MONGO_COLLECTION_MODULE = {
    'users': 'users',
    'sessions': 'sessions',
    'comments': 'comments',
    'userreports': 'reports',
    'watchprogresses': 'watch_progress',
    'streamhistories': 'stream_history',
    'searchhistories': 'search_history',
    'notifications': 'notifications',
    'passwordresettokens': 'tokens',
    'blacklistedtokens': 'tokens',
    'gameprofiles': 'games',
    'gamehistories': 'games',
    'dailychallenges': 'daily_challenge',
    'dailysubmissions': 'daily_challenge',
    'systemsettings': 'settings',
}


def _has_perm(request, perm_code):
    if not request.user.is_authenticated:
        return False
    if request.user.is_superuser:
        return True
    profile = getattr(request.user, 'admin_profile', None)
    return bool(profile and profile.has_perm(perm_code))


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('dashboard.view')
def operations_dashboard(request):
    """Operations dashboard. Data loads asynchronously for a smoother UI."""
    context = {
        'client_url': settings.CLIENT_URL,
        'server_url': settings.SERVER_URL,
        'game_server_url': getattr(settings, 'GAME_SERVER_URL', 'http://localhost:3006'),
        'game_client_url': getattr(settings, 'GAME_CLIENT_URL', 'http://localhost:5173'),
        'api_base_url': settings.API_BASE_URL,
        'api_auth_base_url': settings.API_AUTH_BASE_URL,
        'mongo_enabled': mongo_admin.enabled,
        'mongo_collections': MONGO_COLLECTIONS,
        'system_info': runtime_system_info(),
    }
    return render(request, 'admin_dashboard/dashboard/dashboard.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('dashboard.view')
def api_dashboard_overview(request):
    force = request.GET.get('refresh') == '1'

    def _safe_mongo():
        try:
            return mongo_admin.overview(force=force)
        except Exception as exc:
            return {'enabled': mongo_admin.enabled, 'error': str(exc)}

    results = parallel_run({
        'mongo': _safe_mongo,
        'rooms': lambda: public_rooms(force=force),
        'health': lambda: server_health(force=force),
        'arena': lambda: arena_health(force=force),
    }, max_workers=4)

    mongo_data = results['mongo']
    rooms, rooms_error = results['rooms']
    health = results['health']
    arena = results['arena']

    return JsonResponse({
        'success': True,
        'client_url': settings.CLIENT_URL,
        'server_url': settings.SERVER_URL,
        'game_server_url': getattr(settings, 'GAME_SERVER_URL', 'http://localhost:3006'),
        'api_base_url': settings.API_BASE_URL,
        'api_auth_base_url': settings.API_AUTH_BASE_URL,
        'server_health': health,
        'arena_health': arena,
        'system_info': runtime_system_info(),
        'rooms': rooms,
        'rooms_error': rooms_error,
        'mongo': mongo_data,
        'collections': {
            key: {'label': meta['label']}
            for key, meta in MONGO_COLLECTIONS.items()
        },
    })


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def api_mongo_collection(request, collection):
    mod = _MONGO_COLLECTION_MODULE.get(collection, 'dashboard')
    req_perm = f'{mod}.view'
    if not _has_perm(request, req_perm):
        return JsonResponse({'success': False, 'error': f'Permission {req_perm} required'}, status=403)
    try:
        data = mongo_admin.list_collection(
            collection,
            page=request.GET.get('page', 1),
            limit=request.GET.get('limit', 20),
        )
        data['success'] = True
        data['collection'] = collection
        data['label'] = MONGO_COLLECTIONS[collection]['label']
        return JsonResponse(data)
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_http_methods(["POST"])
def api_mongo_action(request, collection, document_id, action):
    mod = _MONGO_COLLECTION_MODULE.get(collection, 'dashboard')
    req_action = 'delete' if action == 'delete' else 'edit'
    req_perm = f'{mod}.{req_action}'
    if not _has_perm(request, req_perm):
        return JsonResponse({'success': False, 'error': f'Permission {req_perm} required'}, status=403)
    try:
        if action == 'delete':
            changed = mongo_admin.delete_document(collection, document_id)
            message = f'Deleted {changed} document(s).'
            if collection == 'comments' and changed:
                from .webhook_dispatcher import dispatch_webhook_event
                dispatch_webhook_event('comment.deleted', {'comment_id': document_id, 'admin': request.user.username})
        elif collection == 'comments' and action in ('hide', 'restore'):
            changed = mongo_admin.set_comment_deleted(document_id, action == 'hide')
            message = f'Updated {changed} comment(s).'
            if action == 'hide' and changed:
                from .webhook_dispatcher import dispatch_webhook_event
                dispatch_webhook_event('comment.hidden', {'comment_id': document_id, 'admin': request.user.username})
        elif collection == 'notifications' and action in ('read', 'unread'):
            changed = mongo_admin.set_notification_read(document_id, action == 'read')
            message = f'Updated {changed} notification(s).'
        else:
            return JsonResponse({'success': False, 'error': 'Unsupported action'}, status=400)
        return JsonResponse({'success': True, 'message': message})
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def admin_dashboard(request):
    """Dashboard chính cho admin - Real-time từ API"""
    try:
        # Gọi API để lấy dữ liệu real-time
        response = requests.get(f'{API_BASE_URL}/users', timeout=10)
        if response.status_code == 200:
            users_data = response.json()
            
            # Thêm field 'id' cho mỗi user từ '_id'
            for user in users_data:
                user['id'] = user.get('_id')
            
            # Tính toán thống kê từ API data
            total_users = len(users_data)
            verified_users = len([u for u in users_data if u.get('isEmailVerified', False)])
            unverified_users = total_users - verified_users
            
            # Lấy users gần đây (5 users đầu tiên)
            recent_users = users_data[:5]
            
            # Tính toán watchlist stats
            total_watchlist_items = 0
            unique_movies = set()
            top_watchlist_users = []
            
            for user in users_data:
                watchlist = user.get('watchlist', [])
                total_watchlist_items += len(watchlist)
                for movie in watchlist:
                    unique_movies.add(movie.get('id'))
            
            # Sắp xếp users theo số lượng watchlist
            top_watchlist_users = sorted(
                users_data,
                key=lambda x: len(x.get('watchlist', [])),
                reverse=True
            )[:5]
            
            context = {
                'total_users': total_users,
                'verified_users': verified_users,
                'unverified_users': unverified_users,
                'recent_users': recent_users,
                'top_watchlist_users': top_watchlist_users,
                'total_watchlist_items': total_watchlist_items,
                'unique_movies': len(unique_movies),
                'api_status': check_api_status(),
            }
        else:
            context = {
                'error': f'Không thể lấy dữ liệu từ API: {response.status_code}',
                'api_status': {'status': 'error', 'message': f'HTTP {response.status_code}'}
            }
            
    except requests.exceptions.RequestException as e:
        context = {
            'error': f'Không thể kết nối đến API: {str(e)}',
            'api_status': {'status': 'offline', 'message': str(e)}
        }
    except Exception as e:
        context = {
            'error': f'Lỗi: {str(e)}',
            'api_status': {'status': 'error', 'message': str(e)}
        }
    
    return render(request, 'admin_dashboard/dashboard/dashboard.html', context)

@login_required
def users_list(request):
    """Trang danh sách users - Real-time từ API"""
    try:
        response = requests.get(f'{API_BASE_URL}/users', timeout=10)
        if response.status_code == 200:
            users_data = response.json()
            
            # Thêm field 'id' cho mỗi user từ '_id'
            for user in users_data:
                user['id'] = user.get('_id')
            
            # Tính toán thống kê
            total_users = len(users_data)
            verified_users = len([u for u in users_data if u.get('isEmailVerified', False)])
            unverified_users = total_users - verified_users
            
            context = {
                'users': users_data,
                'total_users': total_users,
                'verified_users': verified_users,
                'unverified_users': unverified_users,
                'api_status': check_api_status(),
            }
        else:
            context = {
                'error': f'Không thể lấy dữ liệu từ API: {response.status_code}',
                'users': [],
                'api_status': {'status': 'error'}
            }
            
    except Exception as e:
        context = {
            'error': f'Lỗi: {str(e)}',
            'users': [],
            'api_status': {'status': 'offline'}
        }
    
    return render(request, 'admin_dashboard/users/users_list.html', context)

@login_required
def user_detail(request, user_id):
    """Trang chi tiết user - Real-time từ API"""
    try:
        response = requests.get(f'{API_BASE_URL}/users/{user_id}', timeout=10)
        if response.status_code == 200:
            user_data = response.json()
            # Thêm field 'id' cho user từ '_id'
            user_data['id'] = user_data.get('_id')
            context = {
                'user': user_data,
                'api_status': check_api_status(),
            }
            return render(request, 'admin_dashboard/users/user_detail.html', context)
        else:
            messages.error(request, f'Không tìm thấy người dùng với ID: {user_id}')
            return redirect('admin_dashboard:users_list')
            
    except Exception as e:
        messages.error(request, f'Lỗi: {str(e)}')
        return redirect('admin_dashboard:users_list')

@login_required
def user_edit(request, user_id):
    """Trang chỉnh sửa user - Real-time với API"""
    try:
        if request.method == 'POST':
            # Lấy dữ liệu từ form
            user_data = {
                'name': request.POST.get('name'),
                'email': request.POST.get('email'),
                'avatar': request.POST.get('avatar', ''),
                'isEmailVerified': request.POST.get('is_email_verified') == 'on'
            }
            
            # Cập nhật user qua API
            response = requests.put(
                f'{API_BASE_URL}/users/{user_id}',
                json=user_data,
                timeout=10
            )
            
            if response.status_code == 200:
                messages.success(request, 'Cập nhật người dùng thành công!')
                return redirect('admin_dashboard:user_detail', user_id=user_id)
            else:
                messages.error(request, f'Lỗi khi cập nhật: {response.status_code}')
        
        # Lấy thông tin user hiện tại
        response = requests.get(f'{API_BASE_URL}/users/{user_id}', timeout=10)
        if response.status_code == 200:
            user_data = response.json()
            # Thêm field 'id' cho user từ '_id'
            user_data['id'] = user_data.get('_id')
            context = {
                'user': user_data,
            }
            return render(request, 'admin_dashboard/users/user_edit.html', context)
        else:
            messages.error(request, f'Không tìm thấy người dùng với ID: {user_id}')
            return redirect('admin_dashboard:users_list')
            
    except Exception as e:
        messages.error(request, f'Lỗi: {str(e)}')
        return redirect('admin_dashboard:users_list')

@login_required
def user_delete(request, user_id):
    """Xóa user - Real-time với API"""
    try:
        response = requests.delete(f'{API_BASE_URL}/users/{user_id}', timeout=10)
        if response.status_code == 200:
            messages.success(request, 'Xóa người dùng thành công!')
        else:
            messages.error(request, f'Lỗi khi xóa người dùng: {response.status_code}')
            
    except Exception as e:
        messages.error(request, f'Lỗi: {str(e)}')
    
    return redirect('admin_dashboard:users_list')

@login_required
def watchlist_list(request):
    """Trang danh sách watchlist - Real-time từ API"""
    try:
        response = requests.get(f'{API_BASE_URL}/users', timeout=10)
        if response.status_code == 200:
            users_data = response.json()
            
            # Thêm field 'id' cho mỗi user từ '_id'
            for user in users_data:
                user['id'] = user.get('_id')
            
            # Tạo danh sách watchlist items từ tất cả users
            watchlist_items = []
            for user in users_data:
                user_watchlist = user.get('watchlist', [])
                for movie in user_watchlist:
                    watchlist_items.append({
                        'id': f"{user['id']}_{movie.get('id')}",
                        'user': user,
                        'movie_id': movie.get('id'),
                        'title': movie.get('title'),
                        'poster_path': movie.get('poster_path'),
                        'added_at': user.get('createdAt')  # Sử dụng user creation date
                    })
            
            # Tính toán thống kê
            total_items = len(watchlist_items)
            unique_movies = len(set(item['movie_id'] for item in watchlist_items))
            unique_users = len(set(item['user']['id'] for item in watchlist_items))
            
            context = {
                'watchlist_items': watchlist_items,
                'total_items': total_items,
                'unique_movies': unique_movies,
                'unique_users': unique_users,
                'api_status': check_api_status(),
            }
        else:
            context = {
                'error': f'Không thể lấy dữ liệu từ API: {response.status_code}',
                'watchlist_items': [],
                'api_status': {'status': 'error'}
            }
            
    except Exception as e:
        context = {
            'error': f'Lỗi: {str(e)}',
            'watchlist_items': [],
            'api_status': {'status': 'offline'}
        }
    
    return render(request, 'admin_dashboard/watchlist/watchlist_list.html', context)

@login_required
def watchlist_detail(request, item_id):
    """Trang chi tiết watchlist item - Real-time từ API"""
    try:
        # Parse item_id để lấy user_id và movie_id
        if '_' in item_id:
            user_id, movie_id = item_id.split('_', 1)
            
            response = requests.get(f'{API_BASE_URL}/users/{user_id}', timeout=10)
            if response.status_code == 200:
                user_data = response.json()
                # Thêm field 'id' cho user từ '_id'
                user_data['id'] = user_data.get('_id')
                watchlist = user_data.get('watchlist', [])
                
                # Tìm movie trong watchlist
                movie_item = None
                for movie in watchlist:
                    if str(movie.get('id')) == movie_id:
                        movie_item = {
                            'id': item_id,
                            'user': user_data,
                            'movie_id': movie.get('id'),
                            'title': movie.get('title'),
                            'poster_path': movie.get('poster_path')
                        }
                        break
                
                if movie_item:
                    context = {
                        'item': movie_item,
                        'api_status': check_api_status(),
                    }
                    return render(request, 'admin_dashboard/watchlist/watchlist_detail.html', context)
        
        messages.error(request, 'Không tìm thấy mục danh sách xem!')
        return redirect('admin_dashboard:watchlist_list')
        
    except Exception as e:
        messages.error(request, f'Lỗi: {str(e)}')
        return redirect('admin_dashboard:watchlist_list')

@login_required
def watchlist_edit(request, item_id):
    """Trang chỉnh sửa watchlist item - Real-time với API"""
    try:
        if request.method == 'POST':
            # Parse item_id
            if '_' in item_id:
                user_id, movie_id = item_id.split('_', 1)
                
                # Lấy dữ liệu user hiện tại
                response = requests.get(f'{API_BASE_URL}/users/{user_id}', timeout=10)
                if response.status_code == 200:
                    user_data = response.json()
                    watchlist = user_data.get('watchlist', [])
                    
                    # Cập nhật movie trong watchlist
                    updated_watchlist = []
                    for movie in watchlist:
                        if str(movie.get('id')) == movie_id:
                            updated_movie = {
                                'id': int(movie_id),
                                'title': request.POST.get('title', movie.get('title')),
                                'poster_path': request.POST.get('poster_path', movie.get('poster_path', ''))
                            }
                            updated_watchlist.append(updated_movie)
                        else:
                            updated_watchlist.append(movie)
                    
                    # Cập nhật user với watchlist mới
                    update_response = requests.put(
                        f'{API_BASE_URL}/users/{user_id}',
                        json={'watchlist': updated_watchlist},
                        timeout=10
                    )
                    
                    if update_response.status_code == 200:
                        messages.success(request, 'Cập nhật mục danh sách xem thành công!')
                        return redirect('admin_dashboard:watchlist_detail', item_id=item_id)
                    else:
                        messages.error(request, f'Lỗi khi cập nhật: {update_response.status_code}')
        
        # Hiển thị form edit
        if '_' in item_id:
            user_id, movie_id = item_id.split('_', 1)
            
            response = requests.get(f'{API_BASE_URL}/users/{user_id}', timeout=10)
            if response.status_code == 200:
                user_data = response.json()
                # Thêm field 'id' cho user từ '_id'
                user_data['id'] = user_data.get('_id')
                watchlist = user_data.get('watchlist', [])
                
                # Tìm movie item
                movie_item = None
                for movie in watchlist:
                    if str(movie.get('id')) == movie_id:
                        movie_item = {
                            'id': item_id,
                            'user': user_data,
                            'movie_id': movie.get('id'),
                            'title': movie.get('title'),
                            'poster_path': movie.get('poster_path')
                        }
                        break
                
                if movie_item:
                    # Lấy danh sách tất cả users để hiển thị trong dropdown
                    users_response = requests.get(f'{API_BASE_URL}/users', timeout=10)
                    if users_response.status_code == 200:
                        users_data = users_response.json()
                        # Thêm field 'id' cho mỗi user từ '_id'
                        for user in users_data:
                            user['id'] = user.get('_id')
                        
                        context = {
                            'item': movie_item,
                            'users': users_data,
                        }
                    else:
                        context = {
                            'item': movie_item,
                            'users': [],
                        }
                    return render(request, 'admin_dashboard/watchlist/watchlist_edit.html', context)
        
        messages.error(request, 'Không tìm thấy mục danh sách xem!')
        return redirect('admin_dashboard:watchlist_list')
        
    except Exception as e:
        messages.error(request, f'Lỗi: {str(e)}')
        return redirect('admin_dashboard:watchlist_list')

@login_required
def watchlist_delete(request, item_id):
    """Xóa watchlist item - Real-time với API"""
    try:
        if '_' in item_id:
            user_id, movie_id = item_id.split('_', 1)
            
            # Lấy dữ liệu user hiện tại
            response = requests.get(f'{API_BASE_URL}/users/{user_id}', timeout=10)
            if response.status_code == 200:
                user_data = response.json()
                watchlist = user_data.get('watchlist', [])
                
                # Lọc bỏ movie cần xóa
                updated_watchlist = [
                    movie for movie in watchlist 
                    if str(movie.get('id')) != movie_id
                ]
                
                # Cập nhật user với watchlist mới
                update_response = requests.put(
                    f'{API_BASE_URL}/users/{user_id}',
                    json={'watchlist': updated_watchlist},
                    timeout=10
                )
                
                if update_response.status_code == 200:
                    messages.success(request, 'Xóa mục danh sách xem thành công!')
                else:
                    messages.error(request, f'Lỗi khi xóa: {update_response.status_code}')
                    
    except Exception as e:
        messages.error(request, f'Lỗi: {str(e)}')
    
    return redirect('admin_dashboard:watchlist_list')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('watchlist.delete')
def cleanup_watchlist(request):
    """Dọn dẹp watchlist không hợp lệ (ưu tiên MongoDB bulk_write, fallback API song song)."""
    try:
        if mongo_admin.enabled:
            from pymongo import UpdateOne
            db = mongo_admin.db()
            bad_item_filter = {
                'watchlist': {
                    '$elemMatch': {
                        '$or': [
                            {'id': None}, {'id': ''}, {'id': {'$exists': False}},
                            {'title': None}, {'title': ''}, {'title': {'$exists': False}},
                        ]
                    }
                }
            }
            dirty_users = list(db.users.find(bad_item_filter, {'watchlist': 1}))
            ops = []
            for user in dirty_users:
                wl = user.get('watchlist') or []
                cleaned_wl = [m for m in wl if isinstance(m, dict) and m.get('id') and m.get('title')]
                if len(cleaned_wl) != len(wl):
                    ops.append(UpdateOne({'_id': user['_id']}, {'$set': {'watchlist': cleaned_wl}}))
            cleaned_count = 0
            if ops:
                res = db.users.bulk_write(ops, ordered=False)
                cleaned_count = int(getattr(res, 'modified_count', 0) or len(ops))
            invalidate_swr('media_catalog:movie', 'media_catalog:tv')
            messages.success(request, f'Đã dọn dẹp danh sách xem cho {cleaned_count} người dùng!')
        else:
            response = requests.get(f'{API_BASE_URL}/users', timeout=10)
            if response.status_code == 200:
                from concurrent.futures import ThreadPoolExecutor
                users_data = response.json()
                updates = []
                for user in users_data:
                    watchlist = user.get('watchlist', [])
                    cleaned_watchlist = [
                        movie for movie in watchlist
                        if movie.get('id') and movie.get('title')
                    ]
                    if len(cleaned_watchlist) != len(watchlist):
                        updates.append((user['_id'], cleaned_watchlist))

                def _push_clean(item):
                    uid, wl = item
                    resp = requests.put(f'{API_BASE_URL}/users/{uid}', json={'watchlist': wl}, timeout=10)
                    return resp.status_code == 200

                cleaned_count = 0
                if updates:
                    with ThreadPoolExecutor(max_workers=5) as pool:
                        cleaned_count = sum(1 for ok in pool.map(_push_clean, updates) if ok)
                invalidate_swr('media_catalog:movie', 'media_catalog:tv')
                messages.success(request, f'Đã dọn dẹp danh sách xem cho {cleaned_count} người dùng!')
            else:
                messages.error(request, f'Lỗi khi lấy dữ liệu: {response.status_code}')

    except Exception as e:
        messages.error(request, f'Lỗi: {str(e)}')

    return redirect('admin_dashboard:watchlist_list')

# API endpoints cho AJAX calls
@require_http_methods(["POST"])
@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('dashboard.edit')
def api_sync_from_api(request):
    """API endpoint để refresh dữ liệu từ API (cho AJAX)"""
    try:
        response = requests.get(f'{API_BASE_URL}/users', timeout=10)
        if response.status_code == 200:
            users_data = response.json()
            
            # Tính toán thống kê
            total_users = len(users_data)
            verified_users = len([u for u in users_data if u.get('isEmailVerified', False)])
            
            return JsonResponse({
                'success': True,
                'message': f'Đã refresh dữ liệu: {total_users} users',
                'total_users': total_users,
                'verified_users': verified_users
            })
        else:
            return JsonResponse({
                'success': False,
                'error': f'Lỗi khi gọi API: {response.status_code}'
            })
            
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        })

# Admin Management Views
VALID_ADMIN_ROLES = ('super_admin', 'admin', 'moderator', 'viewer')


def _is_actor_super_admin(user):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    profile = getattr(user, 'admin_profile', None)
    return bool(profile and profile.role == 'super_admin')


def _build_role_defaults_json():
    return json.dumps({r: sorted(perms) for r, perms in ROLE_DEFAULTS.items()})


def _extract_permission_overrides_from_post(post_data, role):
    """Parse custom RBAC matrix checkboxes and keep only diffs against ROLE_DEFAULTS[role]."""
    defaults = ROLE_DEFAULTS.get(role, set())
    overrides = {}
    for mod in MODULES:
        for act in ACTIONS:
            code = f'{mod}.{act}'
            checked = post_data.get(f'perm_{code}') == 'on'
            if checked != (code in defaults):
                overrides[code] = checked
    return overrides


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('admins.view')
def admin_list(request):
    """Danh sách tài khoản admin"""
    try:
        admin_users = User.objects.filter(
            admin_profile__isnull=False
        ).select_related('admin_profile').order_by('-date_joined')

        context = {
            'admin_users': admin_users,
            'total_admins': admin_users.count(),
            'active_admins': admin_users.filter(is_active=True).count(),
            'tfa_enabled_count': admin_users.filter(admin_profile__totp_enabled=True).count(),
        }
        return render(request, 'admin_dashboard/admin/admin_list.html', context)

    except Exception as e:
        messages.error(request, f'Lỗi khi lấy danh sách admin: {str(e)}')
        return redirect('admin_dashboard:dashboard')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('admins.edit')
def admin_create(request):
    """Tạo tài khoản admin mới kèm vai trò & ma trận phân quyền RBAC."""
    actor_is_super = _is_actor_super_admin(request.user)
    if not actor_is_super:
        messages.error(request, 'Chỉ Super Admin hoặc Superuser mới có thể tạo tài khoản admin mới!')
        return redirect('admin_dashboard:admin_list')

    available_roles = ['admin', 'super_admin', 'moderator', 'viewer']
    base_context = {
        'roles': available_roles,
        'rbac_matrix': get_rbac_matrix(None),
        'actions': ACTIONS,
        'role_defaults_json': _build_role_defaults_json(),
    }

    if request.method == 'POST':
        try:
            username = (request.POST.get('username') or '').strip()
            email = (request.POST.get('email') or '').strip()
            password = request.POST.get('password') or ''
            confirm_password = request.POST.get('confirm_password') or ''
            first_name = (request.POST.get('first_name') or '').strip()
            last_name = (request.POST.get('last_name') or '').strip()
            role = request.POST.get('role', 'admin')
            is_superuser = request.POST.get('is_superuser') == 'on'

            if role not in VALID_ADMIN_ROLES:
                messages.error(request, 'Vai trò không hợp lệ!')
                return render(request, 'admin_dashboard/admin/admin_create.html', base_context)

            if not all([username, email, password]):
                messages.error(request, 'Vui lòng điền đầy đủ thông tin bắt buộc!')
                return render(request, 'admin_dashboard/admin/admin_create.html', base_context)

            if password != confirm_password:
                messages.error(request, 'Mật khẩu xác nhận không khớp!')
                return render(request, 'admin_dashboard/admin/admin_create.html', base_context)

            if User.objects.filter(username=username).exists():
                messages.error(request, 'Tên đăng nhập đã tồn tại!')
                return render(request, 'admin_dashboard/admin/admin_create.html', base_context)

            if User.objects.filter(email=email).exists():
                messages.error(request, 'Email đã được sử dụng!')
                return render(request, 'admin_dashboard/admin/admin_create.html', base_context)

            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
                first_name=first_name,
                last_name=last_name,
                is_staff=True,
                is_superuser=is_superuser,
                is_active=True,
            )

            overrides = {}
            if request.POST.get('perm_matrix_submitted') == '1':
                overrides = _extract_permission_overrides_from_post(request.POST, role)

            AdminProfile.objects.create(
                user=user,
                role=role,
                permissions=overrides,
            )

            try:
                AuditLog.log(
                    request, 'create_admin', 'admin_user', str(user.pk),
                    {'username': username, 'role': role, 'is_superuser': is_superuser, 'custom_overrides': len(overrides)},
                )
            except Exception:
                pass

            admin_type = "Superuser" if is_superuser else "Staff"
            messages.success(request, f'Đã tạo thành công tài khoản {admin_type}: {username} ({role}).')
            return redirect('admin_dashboard:admin_list')

        except Exception as e:
            messages.error(request, f'Lỗi khi tạo admin: {str(e)}')

    return render(request, 'admin_dashboard/admin/admin_create.html', base_context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('admins.view')
def admin_detail(request, admin_id):
    """Chi tiết tài khoản admin"""
    try:
        admin_user = User.objects.select_related('admin_profile').get(
            id=admin_id,
            admin_profile__isnull=False
        )

        context = {
            'admin_user': admin_user,
            'rbac_matrix': get_rbac_matrix(admin_user.admin_profile),
            'actions': ACTIONS,
            'actor_is_super': _is_actor_super_admin(request.user),
        }
        return render(request, 'admin_dashboard/admin/admin_detail.html', context)

    except User.DoesNotExist:
        messages.error(request, 'Không tìm thấy tài khoản admin!')
        return redirect('admin_dashboard:admin_list')
    except Exception as e:
        messages.error(request, f'Lỗi: {str(e)}')
        return redirect('admin_dashboard:admin_list')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('admins.edit')
def admin_edit(request, admin_id):
    """Chỉnh sửa tài khoản admin & ma trận phân quyền RBAC"""
    try:
        admin_user = User.objects.select_related('admin_profile').get(
            id=admin_id,
            admin_profile__isnull=False
        )
        actor_is_super = _is_actor_super_admin(request.user)
        target_is_super = admin_user.is_superuser or admin_user.admin_profile.role == 'super_admin'

        if target_is_super and not actor_is_super:
            messages.error(request, 'Chỉ Super Admin mới có thể chỉnh sửa tài khoản Super Admin!')
            return redirect('admin_dashboard:admin_detail', admin_id=admin_id)

        available_roles = ['super_admin', 'admin', 'moderator', 'viewer'] if actor_is_super else ['admin', 'moderator', 'viewer']
        edit_context = {
            'admin_user': admin_user,
            'roles': available_roles,
            'rbac_matrix': get_rbac_matrix(admin_user.admin_profile),
            'actions': ACTIONS,
            'role_defaults_json': _build_role_defaults_json(),
            'actor_is_super': actor_is_super,
        }

        if request.method == 'POST':
            username = (request.POST.get('username') or '').strip()
            email = (request.POST.get('email') or '').strip()
            first_name = (request.POST.get('first_name') or '').strip()
            last_name = (request.POST.get('last_name') or '').strip()
            role = request.POST.get('role', admin_user.admin_profile.role)
            is_active = request.POST.get('is_active') == 'on'
            new_password = request.POST.get('new_password', '')
            confirm_password = request.POST.get('confirm_password', '')

            if role not in VALID_ADMIN_ROLES or (role == 'super_admin' and not actor_is_super):
                messages.error(request, 'Bạn không có quyền gán vai trò này!')
                return render(request, 'admin_dashboard/admin/admin_edit.html', edit_context)

            if not all([username, email]):
                messages.error(request, 'Tên đăng nhập và Email là bắt buộc!')
                return render(request, 'admin_dashboard/admin/admin_edit.html', edit_context)

            if User.objects.filter(username=username).exclude(id=admin_id).exists():
                messages.error(request, 'Tên đăng nhập đã tồn tại!')
                return render(request, 'admin_dashboard/admin/admin_edit.html', edit_context)

            if User.objects.filter(email=email).exclude(id=admin_id).exists():
                messages.error(request, 'Email đã được sử dụng!')
                return render(request, 'admin_dashboard/admin/admin_edit.html', edit_context)

            if new_password and new_password != confirm_password:
                messages.error(request, 'Mật khẩu xác nhận không khớp!')
                return render(request, 'admin_dashboard/admin/admin_edit.html', edit_context)

            admin_user.username = username
            admin_user.email = email
            admin_user.first_name = first_name
            admin_user.last_name = last_name
            admin_user.is_active = is_active

            if request.user.is_superuser:
                admin_user.is_superuser = request.POST.get('is_superuser') == 'on'

            if new_password:
                admin_user.set_password(new_password)

            admin_user.save()

            profile = admin_user.admin_profile
            old_role = profile.role
            profile.role = role
            if request.POST.get('perm_matrix_submitted') == '1':
                profile.permissions = _extract_permission_overrides_from_post(request.POST, role)
            elif role != old_role:
                profile.permissions = {}
            profile.save()

            try:
                AuditLog.log(
                    request, 'edit_admin', 'admin_user', str(admin_user.pk),
                    {'username': username, 'role': role, 'is_active': is_active, 'overrides': profile.permissions},
                )
            except Exception:
                pass

            messages.success(request, 'Đã cập nhật thông tin admin thành công!')
            return redirect('admin_dashboard:admin_detail', admin_id=admin_id)

        return render(request, 'admin_dashboard/admin/admin_edit.html', edit_context)

    except User.DoesNotExist:
        messages.error(request, 'Không tìm thấy tài khoản admin!')
        return redirect('admin_dashboard:admin_list')
    except Exception as e:
        messages.error(request, f'Lỗi: {str(e)}')
        return redirect('admin_dashboard:admin_list')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_POST
@require_permission('admins.delete')
def admin_delete(request, admin_id):
    """Xóa tài khoản admin"""
    try:
        admin_user = User.objects.select_related('admin_profile').get(
            id=admin_id,
            admin_profile__isnull=False
        )

        if admin_user.id == request.user.id:
            messages.error(request, 'Không thể xóa chính tài khoản của bạn!')
            return redirect('admin_dashboard:admin_list')

        target_is_super = admin_user.is_superuser or admin_user.admin_profile.role == 'super_admin'
        if target_is_super and not _is_actor_super_admin(request.user):
            messages.error(request, 'Chỉ Super Admin mới có quyền xóa tài khoản Super Admin!')
            return redirect('admin_dashboard:admin_list')

        total_admins = User.objects.filter(admin_profile__isnull=False).count()
        if total_admins <= 1:
            messages.error(request, 'Không thể xóa admin cuối cùng!')
            return redirect('admin_dashboard:admin_list')

        username = admin_user.username
        admin_user.delete()

        try:
            AuditLog.log(request, 'delete_admin', 'admin_user', str(admin_id), {'username': username})
        except Exception:
            pass

        messages.success(request, f'Đã xóa tài khoản admin: {username}')
        return redirect('admin_dashboard:admin_list')

    except User.DoesNotExist:
        messages.error(request, 'Không tìm thấy tài khoản admin!')
        return redirect('admin_dashboard:admin_list')
    except Exception as e:
        messages.error(request, f'Lỗi khi xóa admin: {str(e)}')
        return redirect('admin_dashboard:admin_list')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_POST
@require_super_admin
def admin_reset_2fa(request, admin_id):
    """Super Admin reset khẩn cấp 2FA khi admin mất thiết bị Authenticator."""
    try:
        target_user = User.objects.select_related('admin_profile').get(
            id=admin_id,
            admin_profile__isnull=False,
        )
        profile = target_user.admin_profile
        profile.totp_secret = ''
        profile.totp_enabled = False
        profile.totp_backup_codes = []
        profile.save(update_fields=['totp_secret', 'totp_enabled', 'totp_backup_codes', 'updated_at'])

        try:
            AuditLog.log(
                request, 'reset_2fa', 'admin_user', str(target_user.pk),
                {'username': target_user.username},
            )
        except Exception:
            pass

        messages.success(request, f'Đã reset 2FA cho tài khoản {target_user.username}.')
    except User.DoesNotExist:
        messages.error(request, 'Không tìm thấy tài khoản admin!')
    except Exception as e:
        messages.error(request, f'Lỗi reset 2FA: {str(e)}')
    return redirect('admin_dashboard:admin_detail', admin_id=admin_id)

# Re-export non-blocking cached health status helper so view imports remain compatible
check_api_status = _service_check_api_status

def flat_list_to_dict(flat_list):
    if not flat_list:
        return {}
    res = {}
    for i in range(0, len(flat_list), 2):
        if i + 1 < len(flat_list):
            res[flat_list[i]] = flat_list[i+1]
    return res

def _upstash_rooms(command_args):
    """Execute a single Upstash Redis REST command on the rooms database and return the result."""
    url = settings.UPSTASH_REDIS_ROOMS_URL
    token = settings.UPSTASH_REDIS_ROOMS_TOKEN
    if not url or not token:
        return None
    resp = requests.post(
        url,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json=command_args,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get('result')

def _upstash_rooms_pipeline(commands):
    """Execute multiple Redis commands in a single Upstash pipeline request on the rooms database."""
    url = settings.UPSTASH_REDIS_ROOMS_URL
    token = settings.UPSTASH_REDIS_ROOMS_TOKEN
    if not url or not token:
        return None
    resp = requests.post(
        f'{url}/pipeline',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json=commands,
        timeout=15,
    )
    resp.raise_for_status()
    return [item.get('result') for item in resp.json()]


def _build_media_catalog(is_tv):
    """Aggregate movies or TV shows from user watchlists and watchprogresses in parallel."""
    db = mongo_admin.db()
    target_type = 'tv' if is_tv else 'movie'
    default_prefix = 'Phim bộ ' if is_tv else 'Phim '

    watchlist_match = (
        {'watchlist.type': 'tv'}
        if is_tv
        else {'$or': [
            {'watchlist.type': 'movie'},
            {'watchlist.type': {'$exists': False}},
            {'watchlist.type': None},
        ]}
    )
    watchlist_pipeline = [
        {'$match': {'watchlist.0': {'$exists': True}}},
        {'$project': {'watchlist': 1}},
        {'$unwind': '$watchlist'},
        {'$match': watchlist_match},
        {'$group': {
            '_id': {'$toString': '$watchlist.id'},
            'title': {'$first': '$watchlist.title'},
            'poster': {'$first': '$watchlist.poster_path'},
            'watchlist_count': {'$sum': 1},
        }},
    ]

    wp_match = {'isTVShow': True} if is_tv else {'isTVShow': {'$ne': True}}
    wp_pipeline = [
        {'$match': wp_match},
        {'$group': {
            '_id': {'$toString': '$contentId'},
            'title': {'$first': '$title'},
            'poster': {'$first': '$poster'},
            'progress_count': {'$sum': 1},
            'last_watched': {'$max': '$lastWatched'},
        }},
    ]

    results = parallel_run({
        'watchlist': lambda: list(db.users.aggregate(watchlist_pipeline)),
        'progress': lambda: list(db.watchprogresses.aggregate(wp_pipeline)),
    }, max_workers=2)

    media_map = {}
    total_watchlist_count = 0

    for doc in results['watchlist']:
        media_id = str(doc.get('_id'))
        wl_count = doc.get('watchlist_count', 0)
        total_watchlist_count += wl_count
        media_map[media_id] = {
            'id': media_id,
            'title': doc.get('title'),
            'poster': doc.get('poster'),
            'watchlist_count': wl_count,
            'progress_count': 0,
            'last_watched': None,
        }

    for doc in results['progress']:
        media_id = str(doc.get('_id'))
        p_last = doc.get('last_watched')
        p_last_str = (
            p_last.isoformat() if isinstance(p_last, datetime) else (str(p_last) if p_last else None)
        )
        if media_id not in media_map:
            media_map[media_id] = {
                'id': media_id,
                'title': doc.get('title') or (default_prefix + media_id),
                'poster': doc.get('poster') or '',
                'watchlist_count': 0,
                'progress_count': 0,
                'last_watched': None,
            }
        entry = media_map[media_id]
        entry['progress_count'] += doc.get('progress_count', 0)
        if p_last_str and (not entry['last_watched'] or p_last_str > entry['last_watched']):
            entry['last_watched'] = p_last_str

    items = sorted(media_map.values(), key=lambda x: x['watchlist_count'], reverse=True)
    return {
        'items': items,
        'total_unique': len(items),
        'total_watchlist_count': total_watchlist_count,
    }


def _media_catalog_list(request, *, is_tv):
    """Shared handler for paginated Movies and TV Shows catalog management."""
    db = mongo_admin.db()
    target_type = 'tv' if is_tv else 'movie'
    redirect_name = 'admin_dashboard:tv_shows' if is_tv else 'admin_dashboard:movies'
    template_name = (
        'admin_dashboard/tv_shows/tv_shows_list.html'
        if is_tv
        else 'admin_dashboard/movies/movies_list.html'
    )
    list_key = 'tv_shows' if is_tv else 'movies'
    total_key = 'total_tv' if is_tv else 'total_movies'
    label = 'TV Show' if is_tv else 'phim'

    if request.method == 'POST' and request.POST.get('action') == 'delete_media':
        media_id = request.POST.get('media_id')
        post_is_tv = request.POST.get('is_tv') == 'true'
        if media_id:
            try:
                try:
                    media_id_int = int(media_id)
                except ValueError:
                    media_id_int = media_id

                del_res = parallel_run({
                    'progress': lambda: db.watchprogresses.delete_many({
                        'contentId': media_id,
                        'isTVShow': post_is_tv,
                    }),
                    'watchlist': lambda: db.users.update_many(
                        {'watchlist.id': {'$in': [media_id_int, str(media_id)]}},
                        {'$pull': {'watchlist': {'id': {'$in': [media_id_int, str(media_id)]}}}},
                    ),
                    'comments': lambda: db.comments.delete_many({
                        'movieId': {'$in': [str(media_id), media_id_int]},
                        'type': target_type,
                    }),
                    'streams': lambda: db.streamhistories.delete_many({
                        'movieId': {'$in': [str(media_id), media_id_int]},
                    }),
                }, max_workers=4)
                del_progress = del_res['progress']
                del_watchlist = del_res['watchlist']
                del_comments = del_res['comments']
                invalidate_swr(f'media_catalog:{target_type}', 'admin_sidebar_counts', 'mongo_overview_cache')
                from users_management.services import invalidate_comment_cache
                invalidate_comment_cache()
                AuditLog.log(
                    request, f'delete_{target_type}', target_type, str(media_id),
                    {
                        'watchlist_modified': del_watchlist.modified_count,
                        'progress_deleted': del_progress.deleted_count,
                        'comments_deleted': del_comments.deleted_count,
                    },
                )
                messages.success(
                    request,
                    f'Đã xóa {label} thành công! Gỡ bỏ khỏi {del_watchlist.modified_count} watchlists, '
                    f'{del_progress.deleted_count} watch histories và {del_comments.deleted_count} bình luận.',
                )
            except Exception as e:
                messages.error(request, f'Lỗi khi xóa {label}: {str(e)}')
            return redirect(redirect_name)

    page = max(1, int(request.GET.get('page', 1) or 1))
    limit = max(1, min(100, int(request.GET.get('limit', 25) or 25)))
    search = request.GET.get('search', '').strip()
    force = request.GET.get('refresh') == '1'

    try:
        catalog = cached_swr(
            f'media_catalog:{target_type}',
            fresh_ttl=60,
            stale_ttl=300,
            fetcher=lambda: _build_media_catalog(is_tv),
            force=force,
        )
        items = catalog['items']
        if search:
            q_lower = search.lower()
            items = [
                m for m in items
                if q_lower in str(m.get('title') or '').lower() or q_lower in str(m.get('id') or '').lower()
            ]

        total = len(items)
        total_pages = max(1, -(-total // limit))
        if page > total_pages:
            page = total_pages
        start = (page - 1) * limit
        page_items = items[start:start + limit]

        context = {
            list_key: page_items,
            total_key: catalog['total_unique'],
            'total_watchlist_count': catalog['total_watchlist_count'],
            'page': page,
            'limit': limit,
            'total': total,
            'total_pages': total_pages,
            'page_range': range(max(1, page - 2), min(total_pages, page + 2) + 1),
            'search': search,
            'preserved_params': urlencode({'search': search}) if search else '',
            'api_status': check_api_status(),
        }
    except Exception as e:
        context = {
            list_key: [],
            total_key: 0,
            'total_watchlist_count': 0,
            'page': 1,
            'limit': limit,
            'total': 0,
            'total_pages': 1,
            'page_range': range(1, 2),
            'search': search,
            'preserved_params': '',
            'error': f'Lỗi MongoDB: {str(e)}',
            'api_status': {'status': 'offline'},
        }
    return render(request, template_name, context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('movies.view')
def movies_list(request):
    """Quản lý Movies"""
    return _media_catalog_list(request, is_tv=False)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('tv_shows.view')
def tv_shows_list(request):
    """Quản lý TV Shows"""
    return _media_catalog_list(request, is_tv=True)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('comments.view')
def comments_list(request):
    """Danh sách và kiểm duyệt bình luận"""
    page = int(request.GET.get('page', 1))
    limit = int(request.GET.get('limit', 20))
    search = request.GET.get('search', '').strip()
    status_filter = request.GET.get('status', '')
    
    try:
        query = {}
        if search:
            query['content'] = {'$regex': search, '$options': 'i'}
            
        if status_filter == 'deleted':
            query['isDeleted'] = True
        elif status_filter == 'active':
            query['isDeleted'] = {'$ne': True}
            
        db = mongo_admin.db()
        pipeline = []
        if query:
            pipeline.append({'$match': query})
            
        pipeline.extend([
            {'$sort': {'createdAt': -1}},
            {'$skip': (page - 1) * limit},
            {'$limit': limit},
            {'$lookup': {
                'from': 'users',
                'localField': 'userId',
                'foreignField': '_id',
                'as': 'user',
            }},
            {'$unwind': {'path': '$user', 'preserveNullAndEmptyArrays': True}},
            {'$project': {
                'movieId': 1,
                'type': 1,
                'content': 1,
                'likes': 1,
                'isDeleted': 1,
                'createdAt': 1,
                'user.name': 1,
                'user.email': 1,
            }}
        ])
        
        results = parallel_run({
            'comments': lambda: list(db.comments.aggregate(pipeline)),
            'total': lambda: db.comments.count_documents(query),
        }, max_workers=2)
        comments = clean_value(results['comments'])
        for c in comments:
            c['id'] = c.get('_id')

        total = results['total']
        total_pages = max(1, -(-total // limit))
        
        context = {
            'comments': comments,
            'page': page,
            'limit': limit,
            'total': total,
            'total_pages': total_pages,
            'search': search,
            'status_filter': status_filter,
            'api_status': check_api_status()
        }
    except Exception as e:
        context = {
            'comments': [],
            'page': page,
            'limit': limit,
            'total': 0,
            'total_pages': 1,
            'error': f'Lỗi MongoDB: {str(e)}',
            'api_status': {'status': 'offline'}
        }
    return render(request, 'admin_dashboard/comments/comments_list.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('rooms.view')
def rooms_list(request):
    """Danh sách và quản lý phòng xem chung (Watch Rooms) từ Redis"""
    if request.method == 'POST' and request.POST.get('action') == 'delete':
        if not _has_perm(request, 'rooms.delete'):
            raise PermissionDenied('Permission rooms.delete required')
        room_id = request.POST.get('room_id')
        if room_id:
            try:
                _upstash_rooms(['DEL', f'room:{room_id}'])
                _upstash_rooms(['DEL', f'room:{room_id}:users'])
                _upstash_rooms(['DEL', f'room:{room_id}:grace'])
                AuditLog.log(request, 'close_room', 'room', room_id)
                from .webhook_dispatcher import dispatch_webhook_event
                dispatch_webhook_event('room.closed', {'room_id': room_id, 'admin': request.user.username})
                messages.success(request, f'Đã kết thúc phòng {room_id} thành công!')
            except Exception as e:
                messages.error(request, f'Lỗi khi đóng phòng: {str(e)}')
            return redirect('admin_dashboard:rooms')
            
    try:
        keys = []
        cursor = '0'
        while True:
            res = _upstash_rooms(['SCAN', cursor, 'MATCH', 'room:ROOM-*', 'COUNT', '500'])
            if not res:
                break
            cursor = str(res[0])
            keys.extend(res[1])
            if cursor == '0':
                break
        
        room_keys = [k for k in keys if ':' not in k.replace('room:ROOM-', '')]
        
        rooms_data = []
        if room_keys:
            commands = []
            for k in room_keys:
                commands.append(['HGETALL', k])
                commands.append(['TTL', k])
                room_id = k.split(':')[-1]
                commands.append(['SCARD', f'room:{room_id}:users'])
                
            pipeline_res = _upstash_rooms_pipeline(commands)
            
            for i, k in enumerate(room_keys):
                room_id = k.split(':')[-1]
                raw_hash = pipeline_res[3*i]
                ttl = pipeline_res[3*i + 1]
                member_count = pipeline_res[3*i + 2]
                
                data = flat_list_to_dict(raw_hash)
                if not data or not data.get('host_id'):
                    continue
                
                try:
                    created_at_ms = int(data.get('created_at', 0))
                    created_time = datetime.fromtimestamp(created_at_ms / 1000.0) if created_at_ms else None
                except:
                    created_time = None
                    
                rooms_data.append({
                    'room_id': room_id,
                    'title': data.get('title', 'Phòng xem phim'),
                    'host_id': data.get('host_id'),
                    'host_name': data.get('host_name', 'Host'),
                    'host_avatar': data.get('host_avatar', ''),
                    'content_type': data.get('content_type', 'movie'),
                    'season': data.get('season'),
                    'current_episode': data.get('current_episode'),
                    'status': data.get('status', 'WAITING'),
                    'member_count': member_count,
                    'max_users': data.get('max_users', 2),
                    'created_at': created_time,
                    'ttl': ttl,
                    'stream_url': data.get('stream_url', '')
                })
        
        rooms_data.sort(key=lambda x: x['created_at'] or datetime.min, reverse=True)
        context = {
            'rooms': rooms_data,
            'total_rooms': len(rooms_data),
            'api_status': check_api_status()
        }
    except Exception as e:
        context = {
            'rooms': [],
            'total_rooms': 0,
            'error': f'Lỗi kết nối Redis: {str(e)}',
            'api_status': {'status': 'offline'}
        }
    return render(request, 'admin_dashboard/rooms/rooms_list.html', context)


def _send_expo_push_batch(tokens, title, body, noti_type='version_updated'):
    """Delegate Expo Push batch dispatch with automatic DeviceNotRegistered pruning."""
    from .views.push_devices import send_expo_push_batch
    res = send_expo_push_batch(tokens, title, body, noti_type=noti_type)
    return res['sent'], res['error']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('notifications.view')
def notifications_list(request):
    """Lịch sử thông báo, thống kê thiết bị Expo Push và phát thông báo hệ thống."""
    db = mongo_admin.db()

    if request.method == 'POST' and request.POST.get('action') == 'broadcast':
        profile = getattr(request.user, 'admin_profile', None)
        if not (request.user.is_superuser or (profile and profile.has_perm('notifications.edit'))):
            messages.error(request, 'Bạn không có quyền gửi thông báo hệ thống.')
            return redirect('admin_dashboard:notifications')
        allowed, retry_after, _ = consume_action_rate_limit('admin_sensitive', request)
        if not allowed:
            messages.error(request, f'Thao tác quá nhanh. Vui lòng thử lại sau {format_retry_wait(retry_after)}.')
            return redirect('admin_dashboard:notifications')
        noti_type = request.POST.get('type', 'version_updated').strip() or 'version_updated'
        title = request.POST.get('title', 'Thông báo hệ thống').strip()
        message = request.POST.get('message', '').strip()
        target_mode = request.POST.get('target_mode', 'all').strip()
        target_input = request.POST.get('target_users', '').strip()
        send_expo_push = request.POST.get('send_expo_push') == 'on'

        try:
            user_query = {}
            if target_mode == 'specific':
                raw_tokens = [x.strip() for x in target_input.replace('\n', ',').split(',') if x.strip()]
                oids = [ObjectId(x) for x in raw_tokens if ObjectId.is_valid(x)]
                emails = [x.lower() for x in raw_tokens if not ObjectId.is_valid(x)]
                or_parts = []
                if oids:
                    or_parts.append({'_id': {'$in': oids}})
                if emails:
                    or_parts.append({'email': {'$in': emails}})
                if not or_parts:
                    messages.warning(request, 'Vui lòng nhập ít nhất 1 Email hoặc User ID hợp lệ!')
                    return redirect('admin_dashboard:notifications')
                user_query = {'$or': or_parts}

            users = list(db.users.find(user_query, {'_id': 1, 'pushTokens': 1}))
            if not users:
                messages.warning(request, 'Không tìm thấy người dùng nào khớp điều kiện gửi thông báo.')
                return redirect('admin_dashboard:notifications')

            now = datetime.utcnow()
            meta = {
                'versionHash': 'SYSTEM_' + now.strftime('%Y%m%d%H%M%S'),
                'versionMessage': f"{title}: {message}",
            }

            notifications_to_insert = []
            push_tokens = []
            for u in users:
                notifications_to_insert.append({
                    'recipient': u['_id'],
                    'actor': None,
                    'type': noti_type,
                    'read': False,
                    'readAt': None,
                    'metadata': meta,
                    'createdAt': now,
                    'updatedAt': now,
                })
                if send_expo_push and isinstance(u.get('pushTokens'), list):
                    push_tokens.extend(u['pushTokens'])

            if notifications_to_insert:
                batch_size = 500
                for idx in range(0, len(notifications_to_insert), batch_size):
                    db.notifications.insert_many(notifications_to_insert[idx:idx + batch_size])

            invalidate_swr('notifications_stats_v1', 'admin_sidebar_counts')

            push_note = ''
            if send_expo_push:
                from .views.push_devices import send_expo_push_batch
                push_res = send_expo_push_batch(push_tokens, title, message, noti_type=noti_type)
                push_sent = push_res['sent']
                pruned = push_res['pruned']
                push_err = push_res['error']
                if push_err:
                    push_note = f' (Expo Push: {push_sent} thiết bị, cảnh báo: {push_err})'
                else:
                    push_note = f' + {push_sent} thiết bị Expo Push!'
                if pruned:
                    push_note += f' (Đã tự động dọn {pruned} token chết)'

            AuditLog.log(request, 'broadcast_notification', 'notification', target_mode, {'users': len(users), 'expo_push': send_expo_push})
            from .webhook_dispatcher import dispatch_webhook_event
            dispatch_webhook_event('notification.broadcast', {
                'type': noti_type,
                'title': title,
                'users': len(users),
                'expo_push': send_expo_push,
                'admin': request.user.username,
            })
            messages.success(request, f'Đã phát thông báo thành công tới {len(users)} người dùng{push_note}')
        except Exception as e:
            messages.error(request, f'Lỗi gửi thông báo: {str(e)}')
        return redirect('admin_dashboard:notifications')

    page = max(1, int(request.GET.get('page', 1) or 1))
    limit = 20
    type_filter = request.GET.get('type', '').strip()
    read_filter = request.GET.get('read', '').strip()
    search = request.GET.get('search', '').strip()
    force = request.GET.get('refresh') == '1'

    try:
        query = {}
        if type_filter:
            query['type'] = type_filter
        if read_filter == 'true':
            query['read'] = True
        elif read_filter == 'false':
            query['read'] = False
        if search:
            matched_users = list(db.users.find(
                {'$or': [
                    {'name': {'$regex': search, '$options': 'i'}},
                    {'email': {'$regex': search, '$options': 'i'}},
                ]},
                {'_id': 1},
            ).limit(100))
            matched_ids = [u['_id'] for u in matched_users]
            or_clause = [
                {'metadata.versionMessage': {'$regex': search, '$options': 'i'}},
                {'metadata.commentPreview': {'$regex': search, '$options': 'i'}},
            ]
            if matched_ids:
                or_clause.append({'recipient': {'$in': matched_ids}})
            if ObjectId.is_valid(search):
                or_clause.append({'recipient': ObjectId(search)})
            query['$or'] = or_clause

        tail_stages = [
            {'$sort': {'createdAt': -1}},
            {'$skip': (page - 1) * limit},
            {'$limit': limit},
            {'$lookup': {
                'from': 'users',
                'localField': 'recipient',
                'foreignField': '_id',
                'as': 'recipient_user',
            }},
            {'$unwind': {'path': '$recipient_user', 'preserveNullAndEmptyArrays': True}},
            {'$project': {
                'recipient': 1,
                'actor': 1,
                'type': 1,
                'read': 1,
                'readAt': 1,
                'metadata': 1,
                'createdAt': 1,
                'recipient_name': '$recipient_user.name',
                'recipient_email': '$recipient_user.email',
            }},
        ]

        if query:
            pipeline = [
                {'$match': query},
                {'$facet': {'meta': [{'$count': 'total'}], 'items': tail_stages}},
            ]
        else:
            pipeline = tail_stages

        def _fetch_notif_stats():
            noti_agg = list(db.notifications.aggregate([
                {'$group': {
                    '_id': None,
                    'total': {'$sum': 1},
                    'unread': {'$sum': {'$cond': [{'$eq': ['$read', False]}, 1, 0]}},
                }}
            ]))
            push_agg = list(db.users.aggregate([
                {'$match': {'pushTokens.0': {'$exists': True}}},
                {'$group': {
                    '_id': None,
                    'push_users': {'$sum': 1},
                    'push_tokens': {'$sum': {'$size': '$pushTokens'}},
                }}
            ]))
            na = noti_agg[0] if noti_agg else {}
            pa = push_agg[0] if push_agg else {}
            tot = na.get('total', 0) or 0
            unr = na.get('unread', 0) or 0
            return {
                'total_all': tot,
                'unread': unr,
                'read': max(0, tot - unr),
                'push_users': pa.get('push_users', 0) or 0,
                'push_tokens': pa.get('push_tokens', 0) or 0,
            }

        results = parallel_run({
            'data': lambda: list(db.notifications.aggregate(pipeline)),
            'stats': lambda: cached_swr(
                'notifications_stats_v1',
                fresh_ttl=30,
                stale_ttl=120,
                fetcher=_fetch_notif_stats,
                force=force,
            ),
        }, max_workers=2)

        stats = results['stats']
        if query:
            facet = results['data'][0] if results['data'] else {}
            meta = facet.get('meta', [])
            total = meta[0]['total'] if meta else 0
            raw_items = facet.get('items', [])
        else:
            total = stats['total_all']
            raw_items = results['data']

        notifications = clean_value(raw_items)
        for n in notifications:
            n['id'] = n.get('_id')
            n['recipient_id_str'] = str(n.get('recipient') or '')

        pag = build_pagination_context(page, total, limit, request.GET)
        context = {
            'notifications': notifications,
            'stats': stats,
            'total': total,
            'type_filter': type_filter,
            'read_filter': read_filter,
            'search': search,
            'api_status': check_api_status(),
            **pag,
        }
    except Exception as e:
        context = {
            'notifications': [], 'stats': {}, 'page': page, 'total': 0,
            'total_pages': 1, 'page_range': [1],
            'type_filter': type_filter, 'read_filter': read_filter, 'search': search,
            'error': f'Lỗi MongoDB: {str(e)}',
            'api_status': {'status': 'offline'},
        }
    return render(request, 'admin_dashboard/notifications/notifications_list.html', context)


# ── Upstash Redis cache management ──────────────────────────────────

def _upstash(command_args):
    """Execute a single Upstash Redis REST command and return the result."""
    url = settings.UPSTASH_REDIS_URL
    token = settings.UPSTASH_REDIS_TOKEN
    if not url or not token:
        return None
    resp = requests.post(
        url,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json=command_args,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get('result')

def _upstash_pipeline(commands):
    """Execute multiple Redis commands in a single Upstash pipeline request."""
    url = settings.UPSTASH_REDIS_URL
    token = settings.UPSTASH_REDIS_TOKEN
    if not url or not token:
        return None
    resp = requests.post(
        f'{url}/pipeline',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json=commands,
        timeout=15,
    )
    resp.raise_for_status()
    return [item.get('result') for item in resp.json()]


def _classify_key(key):
    """Parse a tmdb:* key into endpoint, params, and category."""
    parts = key.split(':', 2)
    endpoint = parts[1] if len(parts) > 1 else '—'
    params = parts[2] if len(parts) > 2 else ''
    ep = endpoint.lower()
    if 'trending' in ep:
        category = 'trending'
    elif 'discover' in ep:
        category = 'discover'
    elif 'search' in ep:
        category = 'search'
    elif 'genre' in ep:
        category = 'genre'
    else:
        category = 'detail'
    return endpoint, params, category


def _scan_all_tmdb_keys():
    """SCAN all tmdb:* key names (fast — no data or TTL fetched)."""
    cursor = '0'
    raw_keys = []
    while True:
        result = _upstash(['SCAN', cursor, 'MATCH', 'tmdb:*', 'COUNT', '500'])
        if result is None:
            return None
        cursor = str(result[0])
        raw_keys.extend(result[1])
        if cursor == '0':
            break
    return sorted(set(raw_keys))


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('cache.view')
def cache_management(request):
    """Upstash Redis TMDB cache viewer — shell page, data loads via AJAX."""
    return render(request, 'admin_dashboard/cache/cache.html')


def _fetch_cache_snapshot():
    """Fetch TMDB Redis keys and Redis INFO concurrently."""
    return parallel_run({
        'keys': _scan_all_tmdb_keys,
        'info': lambda: _upstash(['INFO']),
    }, max_workers=2)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('cache.view')
def api_cache_list(request):
    """AJAX: paginated cache key listing. Only fetches TTL for visible page."""
    page = max(int(request.GET.get('page', 1)), 1)
    limit = 10
    cat_filter = request.GET.get('category', '')
    search = request.GET.get('q', '').lower()
    force = request.GET.get('refresh') == '1'

    try:
        snapshot = cached_swr(
            'api_cache_scan_v1',
            fresh_ttl=30,
            stale_ttl=120,
            fetcher=_fetch_cache_snapshot,
            force=force,
        )
        all_keys = snapshot.get('keys')
        if all_keys is None:
            return JsonResponse({'success': False, 'error': 'Upstash Redis not configured'})

        # Classify every key (cheap — no network call)
        classified = []
        categories = set()
        for k in all_keys:
            endpoint, params, category = _classify_key(k)
            categories.add(category)
            # Apply filters
            if cat_filter and category != cat_filter:
                continue
            if search and search not in k.lower():
                continue
            classified.append({
                'key': k,
                'endpoint': endpoint,
                'params': params,
                'category': category,
            })

        total = len(classified)
        total_pages = max(1, -(-total // limit))  # ceil division
        if page > total_pages:
            page = total_pages
        start = (page - 1) * limit
        page_keys = classified[start:start + limit]

        # Pipeline: fetch TTL for only the visible 10 keys in ONE request
        if page_keys:
            ttl_commands = [['TTL', item['key']] for item in page_keys]
            ttls = _upstash_pipeline(ttl_commands)
            if ttls:
                for item, ttl in zip(page_keys, ttls):
                    item['ttl'] = ttl if ttl and ttl > 0 else -1
            else:
                for item in page_keys:
                    item['ttl'] = -1

        # Reuse cached Upstash INFO stats
        info_raw = snapshot.get('info')
        stats = {}
        if info_raw:
            for line in info_raw.splitlines():
                if ':' in line:
                    k, v = line.split(':', 1)
                    stats[k] = v

        return JsonResponse({
            'success': True,
            'keys': page_keys,
            'page': page,
            'total': total,
            'total_pages': total_pages,
            'total_all': len(all_keys),
            'categories': sorted(categories),
            'stats': {
                'commands': stats.get('total_commands_processed', '0'),
                'storage_used': stats.get('total_data_size_human', '0 B'),
                'storage_max': stats.get('max_data_size_human', '256 MB'),
                'storage_used_bytes': stats.get('total_data_size', '0'),
                'storage_max_bytes': stats.get('max_data_size', '268435456'),
            }
        })
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)})


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('cache.view')
@require_http_methods(["POST"])
def api_cache_action(request):
    """Delete a single key or flush all tmdb:* cache."""
    try:
        body = json.loads(request.body)
        action = body.get('action')
        req_perm = 'cache.view' if action == 'get' else ('cache.delete' if action in ('delete', 'flush') else 'cache.edit')
        if not _has_perm(request, req_perm):
            return JsonResponse({'success': False, 'error': f'Permission {req_perm} required'}, status=403)
        if action in ('delete', 'flush'):
            allowed, retry_after, _ = consume_action_rate_limit('admin_sensitive', request)
            if not allowed:
                return JsonResponse(
                    {'success': False, 'error': f'Rate limit exceeded. Retry in {retry_after}s', 'retryAfter': retry_after},
                    status=429,
                )

        if action == 'delete':
            key = body.get('key', '')
            if not key.startswith('tmdb:'):
                return JsonResponse({'success': False, 'error': 'Invalid key'}, status=400)
            _upstash(['DEL', key])
            invalidate_swr('api_cache_scan_v1')
            return JsonResponse({'success': True, 'message': f'Deleted {key}'})

        elif action == 'flush':
            cursor = '0'
            deleted = 0
            while True:
                result = _upstash(['SCAN', cursor, 'MATCH', 'tmdb:*', 'COUNT', '200'])
                if result is None:
                    break
                cursor = str(result[0])
                batch = result[1]
                if batch:
                    _upstash_pipeline([['DEL', k] for k in batch])
                    deleted += len(batch)
                if cursor == '0':
                    break
            invalidate_swr('api_cache_scan_v1')
            return JsonResponse({'success': True, 'message': f'Flushed {deleted} keys'})

        elif action == 'get':
            key = body.get('key', '')
            if not key.startswith('tmdb:'):
                return JsonResponse({'success': False, 'error': 'Invalid key'}, status=400)
            val = _upstash(['GET', key])
            try:
                # Try to parse it as JSON if it's a JSON string
                parsed = json.loads(val) if val else None
            except:
                parsed = val
            return JsonResponse({'success': True, 'data': parsed})

        return JsonResponse({'success': False, 'error': 'Unknown action'}, status=400)
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)

