from django.conf import settings
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib import messages
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.decorators import user_passes_test
from .models import AdminProfile
from .services import MONGO_COLLECTIONS, mongo_admin, public_rooms, server_health
import requests
import json
from datetime import datetime
from django.db import models

# API configuration
API_BASE_URL = settings.API_AUTH_BASE_URL

def is_admin_user(user):
    """Kiểm tra user có phải là admin không"""
    return user.is_authenticated and (user.is_staff or user.is_superuser)

def admin_login(request):
    """Trang đăng nhập cho admin dashboard"""
    # Nếu đã đăng nhập và là admin, chuyển đến dashboard
    if request.user.is_authenticated and is_admin_user(request.user):
        messages.info(request, f'Bạn đã đăng nhập với tài khoản {request.user.username}')
        return redirect('admin_dashboard:dashboard')
    
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        
        # Kiểm tra thông tin đăng nhập
        if not username or not password:
            messages.error(request, 'Vui lòng nhập đầy đủ thông tin!')
            return render(request, 'admin_dashboard/auth/login.html')
        
        user = authenticate(request, username=username, password=password)
        
        if user is not None:
            if is_admin_user(user):
                login(request, user)
                messages.success(request, f'Chào mừng {user.username}! Đăng nhập thành công.')
                return redirect('admin_dashboard:dashboard')
            else:
                messages.error(request, 'Tài khoản này không có quyền truy cập admin dashboard!')
        else:
            messages.error(request, 'Tên đăng nhập hoặc mật khẩu không đúng!')
    
    return render(request, 'admin_dashboard/auth/login.html')

def admin_logout(request):
    """Đăng xuất khỏi admin dashboard"""
    logout(request)
    messages.success(request, 'Đã đăng xuất thành công!')
    return redirect('admin_dashboard:login')

def admin_register(request):
    """Trang đăng ký admin mới - Chỉ cho phép tạo staff user"""
    # Kiểm tra nếu đã đăng nhập và là admin
    if request.user.is_authenticated and is_admin_user(request.user):
        messages.info(request, f'Bạn đã đăng nhập với tài khoản {request.user.username}')
        return redirect('admin_dashboard:dashboard')
    
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_staff = True  # Cho phép truy cập Django admin
            user.is_superuser = False  # Không phải superuser
            user.save()
            
            # Tạo admin profile
            AdminProfile.objects.create(user=user, role='admin')
            
            messages.success(request, f'Đã tạo admin mới: {user.username}. Có thể đăng nhập tại /admin/ hoặc /dashboard/')
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


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def operations_dashboard(request):
    """Operations dashboard. Data loads asynchronously for a smoother UI."""
    context = {
        'client_url': settings.CLIENT_URL,
        'server_url': settings.SERVER_URL,
        'api_base_url': settings.API_BASE_URL,
        'api_auth_base_url': settings.API_AUTH_BASE_URL,
        'mongo_enabled': mongo_admin.enabled,
        'mongo_collections': MONGO_COLLECTIONS,
    }
    return render(request, 'admin_dashboard/dashboard/dashboard.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def api_dashboard_overview(request):
    try:
        mongo_data = mongo_admin.overview()
    except Exception as exc:
        mongo_data = {'enabled': mongo_admin.enabled, 'error': str(exc)}

    rooms, rooms_error = public_rooms()
    health = server_health()

    return JsonResponse({
        'success': True,
        'client_url': settings.CLIENT_URL,
        'server_url': settings.SERVER_URL,
        'api_base_url': settings.API_BASE_URL,
        'api_auth_base_url': settings.API_AUTH_BASE_URL,
        'server_health': health,
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
    try:
        if action == 'delete':
            changed = mongo_admin.delete_document(collection, document_id)
            message = f'Deleted {changed} document(s).'
        elif collection == 'comments' and action in ('hide', 'restore'):
            changed = mongo_admin.set_comment_deleted(document_id, action == 'hide')
            message = f'Updated {changed} comment(s).'
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
            messages.error(request, f'Không tìm thấy user với ID: {user_id}')
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
                messages.success(request, 'Cập nhật user thành công!')
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
            messages.error(request, f'Không tìm thấy user với ID: {user_id}')
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
            messages.success(request, 'Xóa user thành công!')
        else:
            messages.error(request, f'Lỗi khi xóa user: {response.status_code}')
            
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
        
        messages.error(request, 'Không tìm thấy watchlist item!')
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
                        messages.success(request, 'Cập nhật watchlist item thành công!')
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
        
        messages.error(request, 'Không tìm thấy watchlist item!')
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
                    messages.success(request, 'Xóa watchlist item thành công!')
                else:
                    messages.error(request, f'Lỗi khi xóa: {update_response.status_code}')
                    
    except Exception as e:
        messages.error(request, f'Lỗi: {str(e)}')
    
    return redirect('admin_dashboard:watchlist_list')


@login_required
def cleanup_watchlist(request):
    """Dọn dẹp watchlist - Real-time với API"""
    try:
        response = requests.get(f'{API_BASE_URL}/users', timeout=10)
        if response.status_code == 200:
            users_data = response.json()
            cleaned_count = 0
            
            for user in users_data:
                watchlist = user.get('watchlist', [])
                # Lọc bỏ các items không hợp lệ
                cleaned_watchlist = [
                    movie for movie in watchlist 
                    if movie.get('id') and movie.get('title')
                ]
                
                if len(cleaned_watchlist) != len(watchlist):
                    # Cập nhật user với watchlist đã dọn dẹp (sử dụng _id thực tế từ API)
                    update_response = requests.put(
                        f'{API_BASE_URL}/users/{user["_id"]}',
                        json={'watchlist': cleaned_watchlist},
                        timeout=10
                    )
                    
                    if update_response.status_code == 200:
                        cleaned_count += 1
            
            messages.success(request, f'Đã dọn dẹp watchlist cho {cleaned_count} users!')
        else:
            messages.error(request, f'Lỗi khi lấy dữ liệu: {response.status_code}')
            
    except Exception as e:
        messages.error(request, f'Lỗi: {str(e)}')
    
    return redirect('admin_dashboard:watchlist_list')

# API endpoints cho AJAX calls
@csrf_exempt
@require_http_methods(["POST"])
@login_required
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

def check_api_status():
    """Kiểm tra trạng thái API"""
    try:
        response = requests.get(f'{API_BASE_URL}/users', timeout=5)
        if response.status_code == 200:
            return {
                'status': 'online',
                'last_check': datetime.now().strftime('%H:%M:%S'),
                'response_time': response.elapsed.total_seconds()
            }
        else:
            return {
                'status': 'error',
                'last_check': datetime.now().strftime('%H:%M:%S'),
                'message': f'HTTP {response.status_code}'
            }
    except requests.exceptions.RequestException as e:
        return {
            'status': 'offline',
            'last_check': datetime.now().strftime('%H:%M:%S'),
            'message': str(e)
        }

@login_required


# Admin Management Views
@login_required
def admin_list(request):
    """Danh sách tài khoản admin"""
    try:
        # Lấy tất cả admin users với profile
        admin_users = User.objects.filter(
            admin_profile__isnull=False
        ).select_related('admin_profile').order_by('-date_joined')
        
        context = {
            'admin_users': admin_users,
            'total_admins': admin_users.count(),
        }
        return render(request, 'admin_dashboard/admin/admin_list.html', context)
        
    except Exception as e:
        messages.error(request, f'Lỗi khi lấy danh sách admin: {str(e)}')
        return redirect('admin_dashboard:dashboard')

@login_required
def admin_create(request):
    """Tạo tài khoản admin mới - Sử dụng Django admin chuẩn"""
    # Chỉ superuser mới có thể tạo admin mới
    if not request.user.is_superuser:
        messages.error(request, 'Chỉ có Superuser mới có thể tạo admin mới!')
        return redirect('admin_dashboard:admin_list')
    
    if request.method == 'POST':
        try:
            username = request.POST.get('username')
            email = request.POST.get('email')
            password = request.POST.get('password')
            confirm_password = request.POST.get('confirm_password')
            first_name = request.POST.get('first_name', '')
            last_name = request.POST.get('last_name', '')
            role = request.POST.get('role', 'admin')
            is_superuser = request.POST.get('is_superuser') == 'on'
            
            # Validation
            if not all([username, email, password]):
                messages.error(request, 'Vui lòng điền đầy đủ thông tin bắt buộc!')
                return render(request, 'admin_dashboard/admin/admin_create.html')
                
            if password != confirm_password:
                messages.error(request, 'Mật khẩu xác nhận không khớp!')
                return render(request, 'admin_dashboard/admin/admin_create.html')
                
            if User.objects.filter(username=username).exists():
                messages.error(request, 'Tên đăng nhập đã tồn tại!')
                return render(request, 'admin_dashboard/admin/admin_create.html')
                
            if User.objects.filter(email=email).exists():
                messages.error(request, 'Email đã được sử dụng!')
                return render(request, 'admin_dashboard/admin/admin_create.html')
            
            # Tạo user với quyền admin đúng cách
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
                first_name=first_name,
                last_name=last_name,
                is_staff=True,  # Cho phép truy cập Django admin
                is_superuser=is_superuser,  # Quyền superuser nếu được chọn
                is_active=True
            )
            
            # Tạo admin profile
            AdminProfile.objects.create(
                user=user,
                role=role
            )
            
            # Thêm thông báo hướng dẫn
            admin_type = "Superuser" if is_superuser else "Staff"
            messages.success(request, f'Đã tạo thành công tài khoản {admin_type}: {username}. Có thể đăng nhập tại /admin/')
            return redirect('admin_dashboard:admin_list')
            
        except Exception as e:
            messages.error(request, f'Lỗi khi tạo admin: {str(e)}')
    
    context = {
        'roles': ['admin', 'super_admin', 'moderator']
    }
    return render(request, 'admin_dashboard/admin/admin_create.html', context)

@login_required
def admin_detail(request, admin_id):
    """Chi tiết tài khoản admin"""
    try:
        admin_user = User.objects.select_related('admin_profile').get(
            id=admin_id,
            admin_profile__isnull=False
        )
        
        context = {
            'admin_user': admin_user,
        }
        return render(request, 'admin_dashboard/admin/admin_detail.html', context)
        
    except User.DoesNotExist:
        messages.error(request, 'Không tìm thấy tài khoản admin!')
        return redirect('admin_dashboard:admin_list')
    except Exception as e:
        messages.error(request, f'Lỗi: {str(e)}')
        return redirect('admin_dashboard:admin_list')

@login_required
def admin_edit(request, admin_id):
    """Chỉnh sửa tài khoản admin"""
    try:
        admin_user = User.objects.select_related('admin_profile').get(
            id=admin_id,
            admin_profile__isnull=False
        )
        
        if request.method == 'POST':
            username = request.POST.get('username')
            email = request.POST.get('email')
            first_name = request.POST.get('first_name', '')
            last_name = request.POST.get('last_name', '')
            role = request.POST.get('role', 'admin')
            is_active = request.POST.get('is_active') == 'on'
            new_password = request.POST.get('new_password', '')
            confirm_password = request.POST.get('confirm_password', '')
            
            # Validation
            if not all([username, email]):
                messages.error(request, 'Username và Email là bắt buộc!')
                return render(request, 'admin_dashboard/admin/admin_edit.html', {'admin_user': admin_user})
            
            # Check username conflict (exclude current user)
            if User.objects.filter(username=username).exclude(id=admin_id).exists():
                messages.error(request, 'Tên đăng nhập đã tồn tại!')
                return render(request, 'admin_dashboard/admin/admin_edit.html', {'admin_user': admin_user})
                
            # Check email conflict (exclude current user)
            if User.objects.filter(email=email).exclude(id=admin_id).exists():
                messages.error(request, 'Email đã được sử dụng!')
                return render(request, 'admin_dashboard/admin/admin_edit.html', {'admin_user': admin_user})
            
            # Check password confirmation if new password provided
            if new_password and new_password != confirm_password:
                messages.error(request, 'Mật khẩu xác nhận không khớp!')
                return render(request, 'admin_dashboard/admin/admin_edit.html', {'admin_user': admin_user})
            
            # Update user
            admin_user.username = username
            admin_user.email = email
            admin_user.first_name = first_name
            admin_user.last_name = last_name
            admin_user.is_active = is_active
            
            # Chỉ superuser mới có thể thay đổi quyền superuser
            if request.user.is_superuser:
                is_superuser = request.POST.get('is_superuser') == 'on'
                admin_user.is_superuser = is_superuser
            
            if new_password:
                admin_user.set_password(new_password)
                
            admin_user.save()
            
            # Update profile
            admin_user.admin_profile.role = role
            admin_user.admin_profile.save()
            
            messages.success(request, 'Đã cập nhật thông tin admin thành công!')
            return redirect('admin_dashboard:admin_detail', admin_id=admin_id)
        
        context = {
            'admin_user': admin_user,
            'roles': ['admin', 'super_admin', 'moderator']
        }
        return render(request, 'admin_dashboard/admin/admin_edit.html', context)
        
    except User.DoesNotExist:
        messages.error(request, 'Không tìm thấy tài khoản admin!')
        return redirect('admin_dashboard:admin_list')
    except Exception as e:
        messages.error(request, f'Lỗi: {str(e)}')
        return redirect('admin_dashboard:admin_list')

@login_required
def admin_delete(request, admin_id):
    """Xóa tài khoản admin"""
    try:
        admin_user = User.objects.select_related('admin_profile').get(
            id=admin_id,
            admin_profile__isnull=False
        )
        
        # Không cho phép xóa chính mình
        if admin_user.id == request.user.id:
            messages.error(request, 'Không thể xóa chính tài khoản của bạn!')
            return redirect('admin_dashboard:admin_list')
        
        # Không cho phép xóa nếu chỉ còn 1 admin
        total_admins = User.objects.filter(admin_profile__isnull=False).count()
        if total_admins <= 1:
            messages.error(request, 'Không thể xóa admin cuối cùng!')
            return redirect('admin_dashboard:admin_list')
        
        username = admin_user.username
        admin_user.delete()  # Cascade delete sẽ xóa AdminProfile
        
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
def placeholder_movies(request):
    context = {
        'title': 'Movies',
        'key': 'movies',
        'columns': ['ID', 'Title', 'Release Date', 'Rating', 'Status'],
        'data': [
            ['MV-001', 'Dune: Part Two', '2024-03-01', '8.8', 'Active'],
            ['MV-002', 'Oppenheimer', '2023-07-21', '8.4', 'Active'],
            ['MV-003', 'The Batman', '2022-03-04', '7.9', 'Active'],
            ['MV-004', 'Spider-Man: Across the Spider-Verse', '2023-06-02', '8.7', 'Active'],
            ['MV-005', 'Interstellar', '2014-11-07', '8.6', 'Active'],
        ]
    }
    return render(request, 'admin_dashboard/placeholder.html', context)

@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def placeholder_tv(request):
    context = {
        'title': 'TV Shows',
        'key': 'tvShows',
        'columns': ['ID', 'Title', 'Seasons', 'Rating', 'Status'],
        'data': [
            ['TV-001', 'Breaking Bad', '5', '9.5', 'Active'],
            ['TV-002', 'Game of Thrones', '8', '9.2', 'Active'],
            ['TV-003', 'Stranger Things', '4', '8.7', 'Active'],
            ['TV-004', 'The Office', '9', '8.9', 'Active'],
            ['TV-005', 'Better Call Saul', '6', '8.9', 'Active'],
        ]
    }
    return render(request, 'admin_dashboard/placeholder.html', context)

@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def placeholder_comments(request):
    context = {
        'title': 'Comments',
        'key': 'comments',
        'columns': ['ID', 'User', 'Content', 'Target', 'Date'],
        'data': [
            ['CM-001', 'john_doe', 'Great movie!', 'MV-001', '2024-05-18'],
            ['CM-002', 'jane_smith', 'I loved the ending.', 'TV-002', '2024-05-18'],
            ['CM-003', 'movie_buff', 'Not bad, but could be better.', 'MV-003', '2024-05-17'],
            ['CM-004', 'critic99', 'A masterpiece of cinema.', 'MV-002', '2024-05-17'],
            ['CM-005', 'casual_watcher', 'Too long for my taste.', 'MV-005', '2024-05-16'],
        ]
    }
    return render(request, 'admin_dashboard/placeholder.html', context)

@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def placeholder_rooms(request):
    context = {
        'title': 'Watch Rooms',
        'key': 'rooms',
        'columns': ['ID', 'Host', 'Movie/TV', 'Viewers', 'Status'],
        'data': [
            ['RM-001', 'john_doe', 'Dune: Part Two', '12', 'Live'],
            ['RM-002', 'jane_smith', 'Stranger Things', '5', 'Live'],
            ['RM-003', 'movie_buff', 'The Batman', '8', 'Live'],
        ]
    }
    return render(request, 'admin_dashboard/placeholder.html', context)

@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def placeholder_notifications(request):
    context = {
        'title': 'Notifications',
        'key': 'notifications',
        'columns': ['ID', 'Type', 'Title', 'Sent', 'Status'],
        'data': [
            ['NT-001', 'System', 'Welcome to Movie Admin', '2024-05-18', 'Sent'],
            ['NT-002', 'Alert', 'Server Maintenance', '2024-05-17', 'Sent'],
            ['NT-003', 'Promo', 'New Features Added', '2024-05-15', 'Sent'],
        ]
    }
    return render(request, 'admin_dashboard/placeholder.html', context)


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
def cache_management(request):
    """Upstash Redis TMDB cache viewer — shell page, data loads via AJAX."""
    return render(request, 'admin_dashboard/cache/cache.html')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def api_cache_list(request):
    """AJAX: paginated cache key listing. Only fetches TTL for visible page."""
    page = max(int(request.GET.get('page', 1)), 1)
    limit = 10
    cat_filter = request.GET.get('category', '')
    search = request.GET.get('q', '').lower()

    try:
        all_keys = _scan_all_tmdb_keys()
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

        # Fetch Upstash INFO for stats
        info_raw = _upstash(['INFO'])
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
@require_http_methods(["POST"])
def api_cache_action(request):
    """Delete a single key or flush all tmdb:* cache."""
    try:
        body = json.loads(request.body)
        action = body.get('action')

        if action == 'delete':
            key = body.get('key', '')
            if not key.startswith('tmdb:'):
                return JsonResponse({'success': False, 'error': 'Invalid key'}, status=400)
            _upstash(['DEL', key])
            return JsonResponse({'success': True, 'message': f'Deleted {key}'})

        elif action == 'flush':
            cursor = '0'
            deleted = 0
            while True:
                result = _upstash(['SCAN', cursor, 'MATCH', 'tmdb:*', 'COUNT', '200'])
                if result is None:
                    break
                cursor = str(result[0])
                for k in result[1]:
                    _upstash(['DEL', k])
                    deleted += 1
                if cursor == '0':
                    break
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

