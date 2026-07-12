"""Users management views with server-side pagination, sorting, filters, and ban/unban."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.contrib import messages
from bson import ObjectId

from users_management.models import AuditLog
from users_management.services import mongo_admin, clean_value
from users_management.views_legacy import check_admin_access, check_api_status

__all__ = ['users_list', 'user_detail', 'user_edit', 'user_delete']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def users_list(request):
    """Trang danh sách users có phân trang phía server, tìm kiếm, lọc và sắp xếp."""
    page = int(request.GET.get('page', 1))
    limit = int(request.GET.get('limit', 20))
    search = request.GET.get('search', '').strip()
    verified_filter = request.GET.get('verified', '')
    banned_filter = request.GET.get('banned', '')
    sort_by = request.GET.get('sort', 'created_at')

    try:
        db = mongo_admin.db()
        query = {}

        # Search filter
        if search:
            query['$or'] = [
                {'name': {'$regex': search, '$options': 'i'}},
                {'email': {'$regex': search, '$options': 'i'}},
            ]

        # Verified filter
        if verified_filter == 'true':
            query['isEmailVerified'] = True
        elif verified_filter == 'false':
            query['isEmailVerified'] = {'$ne': True}

        # Banned filter
        if banned_filter == 'true':
            query['isBanned'] = True
        elif banned_filter == 'false':
            query['isBanned'] = {'$ne': True}

        # Count total
        total_users = db.users.count_documents(query)
        total_pages = max(1, -(-total_users // limit))

        # Sorting
        sort_field = 'createdAt'
        sort_direction = -1
        if sort_by == 'name':
            sort_field = 'name'
            sort_direction = 1
        elif sort_by == 'email':
            sort_field = 'email'
            sort_direction = 1

        # Fetch users
        users = list(
            db.users.find(query)
            .sort(sort_field, sort_direction)
            .skip((page - 1) * limit)
            .limit(limit)
        )
        users = clean_value(users)

        # Add 'id' field for compatibility
        for user in users:
            user['id'] = user.get('_id')

        # Additional quick stats
        stats = {
            'total': db.users.count_documents({}),
            'verified': db.users.count_documents({'isEmailVerified': True}),
            'banned': db.users.count_documents({'isBanned': True}),
            'with_watchlist': db.users.count_documents({'watchlist.0': {'$exists': True}}),
        }

        context = {
            'users': users,
            'total_users': total_users,
            'stats': stats,
            'page': page,
            'limit': limit,
            'total_pages': total_pages,
            'search': search,
            'verified_filter': verified_filter,
            'banned_filter': banned_filter,
            'sort': sort_by,
            'api_status': check_api_status(),
        }
    except Exception as e:
        context = {
            'users': [],
            'total_users': 0,
            'stats': {'total': 0, 'verified': 0, 'banned': 0, 'with_watchlist': 0},
            'page': 1,
            'limit': limit,
            'total_pages': 1,
            'search': search,
            'verified_filter': verified_filter,
            'banned_filter': banned_filter,
            'sort': sort_by,
            'error': f'Lỗi: {e}',
            'api_status': {'status': 'offline'},
        }

    return render(request, 'admin_dashboard/users/users_list.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def user_detail(request, user_id):
    """Chi tiết user từ MongoDB kèm các thông tin liên quan."""
    try:
        db = mongo_admin.db()
        user = db.users.find_one({'_id': ObjectId(user_id)})
        if not user:
            messages.error(request, 'Không tìm thấy user!')
            return redirect('admin_dashboard:users_list')

        user = clean_value(user)
        user['id'] = user.get('_id')

        # Lấy lịch sử xem của user này
        watch_progress = list(db.watchprogresses.find({'userId': ObjectId(user_id)}).sort('lastWatched', -1))
        watch_progress = clean_value(watch_progress)

        # Lấy lịch sử tìm kiếm
        search_history = db.searchhistories.find_one({'userId': ObjectId(user_id)})
        search_queries = clean_value(search_history.get('queries', [])) if search_history else []

        # Lấy bình luận của user
        comments = list(db.comments.find({'userId': ObjectId(user_id)}).sort('createdAt', -1).limit(50))
        comments = clean_value(comments)

        context = {
            'user': user,
            'watch_progress': watch_progress,
            'search_queries': search_queries,
            'comments': comments,
            'api_status': check_api_status(),
        }
        return render(request, 'admin_dashboard/users/user_detail.html', context)
    except Exception as e:
        messages.error(request, f'Lỗi: {e}')
        return redirect('admin_dashboard:users_list')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def user_edit(request, user_id):
    """Chỉnh sửa thông tin user trực tiếp trong MongoDB."""
    try:
        db = mongo_admin.db()
        oid = ObjectId(user_id)

        if request.method == 'POST':
            name = request.POST.get('name', '').strip()
            email = request.POST.get('email', '').strip()
            avatar = request.POST.get('avatar', '').strip()
            is_email_verified = request.POST.get('is_email_verified') == 'on'

            update_doc = {
                'name': name,
                'email': email,
                'isEmailVerified': is_email_verified,
            }
            if avatar:
                update_doc['avatar'] = avatar

            db.users.update_one({'_id': oid}, {'$set': update_doc})
            AuditLog.log(request, 'edit_user', 'user', user_id, update_doc)
            messages.success(request, 'Cập nhật user thành công!')
            return redirect('admin_dashboard:user_detail', user_id=user_id)

        user = db.users.find_one({'_id': oid})
        if not user:
            messages.error(request, 'Không tìm thấy user!')
            return redirect('admin_dashboard:users_list')

        user = clean_value(user)
        user['id'] = user.get('_id')
        return render(request, 'admin_dashboard/users/user_edit.html', {'user': user})
    except Exception as e:
        messages.error(request, f'Lỗi: {e}')
        return redirect('admin_dashboard:users_list')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def user_delete(request, user_id):
    """Xóa user trực tiếp trong MongoDB."""
    try:
        db = mongo_admin.db()
        oid = ObjectId(user_id)

        # Xóa các watchlist/comments/history liên quan
        db.comments.delete_many({'userId': oid})
        db.searchhistories.delete_many({'userId': oid})
        db.watchprogresses.delete_many({'userId': oid})
        
        # Xóa user
        result = db.users.delete_one({'_id': oid})
        AuditLog.log(request, 'delete_user', 'user', user_id)
        messages.success(request, f'Xóa user thành công! ({result.deleted_count} record)')
    except Exception as e:
        messages.error(request, f'Lỗi: {e}')
    return redirect('admin_dashboard:users_list')
