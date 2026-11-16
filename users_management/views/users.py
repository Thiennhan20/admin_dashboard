"""Users management views with server-side pagination, sorting, filters, and ban/unban."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.contrib import messages
from bson import ObjectId

from users_management.decorators import require_permission
from users_management.models import AuditLog
from users_management.services import (
    mongo_admin, clean_value, parallel_run, cached_swr,
    build_pagination_context, format_device_display, invalidate_swr,
)
from users_management.views_legacy import check_admin_access, check_api_status

__all__ = ['users_list', 'user_detail', 'user_edit', 'user_delete']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('users.view')
def users_list(request):
    """Trang danh sách users có phân trang phía server, tìm kiếm, lọc và sắp xếp."""
    page = int(request.GET.get('page', 1))
    limit = int(request.GET.get('limit', 20))
    search = request.GET.get('search', '').strip()
    verified_filter = request.GET.get('verified', '')
    banned_filter = request.GET.get('banned', '')
    auth_type_filter = request.GET.get('auth_type', '').strip()
    sort_by = request.GET.get('sort', 'created_at')

    try:
        db = mongo_admin.db()
        query = {}

        # Search filter (supports name, email, username)
        if search:
            query['$or'] = [
                {'name': {'$regex': search, '$options': 'i'}},
                {'email': {'$regex': search, '$options': 'i'}},
                {'username': {'$regex': search, '$options': 'i'}},
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

        # Auth type filter (google vs email)
        if auth_type_filter == 'google':
            query['authType'] = 'google'
        elif auth_type_filter == 'email':
            query['authType'] = {'$ne': 'google'}

        # Sorting
        sort_field = 'createdAt'
        sort_direction = -1
        if sort_by == 'name':
            sort_field = 'name'
            sort_direction = 1
        elif sort_by == 'email':
            sort_field = 'email'
            sort_direction = 1

        users_pipeline = []
        if query:
            users_pipeline.append({'$match': query})
        users_pipeline.extend([
            {'$sort': {sort_field: sort_direction}},
            {'$skip': (page - 1) * limit},
            {'$limit': limit},
            {'$addFields': {
                'watchlist_count': {'$size': {'$ifNull': ['$watchlist', []]}},
            }},
            {'$project': {
                'password': 0,
                'emailVerificationToken': 0,
                'friends': 0,
                'friendRequests': 0,
                'sentFriendRequests': 0,
                'watchlist': 0,
            }},
        ])

        def _fetch_user_stats():
            res = list(db.users.aggregate([
                {'$group': {
                    '_id': None,
                    'total': {'$sum': 1},
                    'verified': {'$sum': {'$cond': [{'$eq': ['$isEmailVerified', True]}, 1, 0]}},
                    'banned': {'$sum': {'$cond': [{'$eq': ['$isBanned', True]}, 1, 0]}},
                    'google_users': {'$sum': {'$cond': [{'$eq': ['$authType', 'google']}, 1, 0]}},
                    'with_watchlist': {'$sum': {'$cond': [
                        {'$gt': [{'$size': {'$ifNull': ['$watchlist', []]}}, 0]}, 1, 0
                    ]}},
                }}
            ]))
            if not res:
                return {'total': 0, 'verified': 0, 'banned': 0, 'google_users': 0, 'email_users': 0, 'with_watchlist': 0}
            doc = res[0]
            total_c = doc.get('total', 0) or 0
            google_c = doc.get('google_users', 0) or 0
            return {
                'total': total_c,
                'verified': doc.get('verified', 0) or 0,
                'banned': doc.get('banned', 0) or 0,
                'google_users': google_c,
                'email_users': max(0, total_c - google_c),
                'with_watchlist': doc.get('with_watchlist', 0) or 0,
            }

        force_stats = request.GET.get('refresh') == '1'
        tasks = {
            'users': lambda: list(db.users.aggregate(users_pipeline)),
            'stats': lambda: cached_swr(
                'user_list_stats_v2',
                fresh_ttl=30,
                stale_ttl=120,
                fetcher=_fetch_user_stats,
                force=force_stats,
            ),
        }
        if query:
            tasks['total_users'] = lambda: db.users.count_documents(query)

        results = parallel_run(tasks, max_workers=3)
        stats = results['stats']
        total_users = results['total_users'] if query else stats['total']
        pag = build_pagination_context(page, total_users, limit, request.GET)

        users = clean_value(results['users'])
        for user in users:
            user['id'] = user.get('_id')

        context = {
            'users': users,
            'total_users': total_users,
            'stats': stats,
            'limit': limit,
            'search': search,
            'verified_filter': verified_filter,
            'banned_filter': banned_filter,
            'auth_type_filter': auth_type_filter,
            'sort': sort_by,
            'api_status': check_api_status(),
            **pag,
        }
    except Exception as e:
        context = {
            'users': [],
            'total_users': 0,
            'stats': {'total': 0, 'verified': 0, 'banned': 0, 'google_users': 0, 'email_users': 0, 'with_watchlist': 0},
            'page': 1,
            'limit': limit,
            'total_pages': 1,
            'page_range': [1],
            'search': search,
            'verified_filter': verified_filter,
            'banned_filter': banned_filter,
            'auth_type_filter': auth_type_filter,
            'sort': sort_by,
            'error': f'Lỗi: {e}',
            'api_status': {'status': 'offline'},
        }

    return render(request, 'admin_dashboard/users/users_list.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('users.view')
def user_detail(request, user_id):
    """Chi tiết user 360° từ MongoDB kèm Sessions, Watchlist, Progress, Search History, Comments."""
    try:
        from datetime import datetime
        db = mongo_admin.db()
        oid = ObjectId(user_id)

        # Handle inline quick actions from user_detail page
        if request.method == 'POST':
            action = request.POST.get('action', '')
            if action == 'toggle_verify':
                curr = db.users.find_one({'_id': oid}, {'isEmailVerified': 1})
                new_val = not bool(curr and curr.get('isEmailVerified'))
                db.users.update_one({'_id': oid}, {'$set': {'isEmailVerified': new_val, 'updatedAt': datetime.utcnow()}})
                AuditLog.log(request, 'toggle_verify_user', 'user', user_id, {'isEmailVerified': new_val})
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({'ok': True, 'isEmailVerified': new_val})
                messages.success(request, 'Đã cập nhật trạng thái xác minh Email!')
                return redirect('admin_dashboard:user_detail', user_id=user_id)

            elif action == 'revoke_session':
                session_id = request.POST.get('session_id', '')
                if session_id:
                    db.sessions.delete_one({'_id': ObjectId(session_id), 'userId': oid})
                    AuditLog.log(request, 'revoke_user_session', 'session', session_id, {'user_id': user_id})
                    messages.success(request, 'Đã thu hồi phiên đăng nhập thiết bị!')
                return redirect('admin_dashboard:user_detail', user_id=user_id)

            elif action == 'revoke_all_sessions':
                res = db.sessions.delete_many({'userId': oid})
                AuditLog.log(request, 'revoke_all_user_sessions', 'user', user_id, {'deleted': res.deleted_count})
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({'ok': True, 'deleted': res.deleted_count})
                messages.success(request, f'Đã đăng xuất bắt buộc {res.deleted_count} thiết bị của người dùng!')
                return redirect('admin_dashboard:user_detail', user_id=user_id)

            elif action == 'clear_search_history':
                db.searchhistories.delete_many({'$or': [{'user_id': oid}, {'userId': oid}]})
                AuditLog.log(request, 'clear_user_search_history', 'user', user_id)
                messages.success(request, 'Đã xóa lịch sử tìm kiếm của người dùng!')
                return redirect('admin_dashboard:user_detail', user_id=user_id)

            elif action == 'remove_push_token':
                token = request.POST.get('push_token', '').strip()
                if token:
                    db.users.update_one({'_id': oid}, {'$pull': {'pushTokens': token}})
                    AuditLog.log(request, 'remove_user_push_token', 'user', user_id, {'token_prefix': token[:24]})
                    messages.success(request, 'Đã gỡ bỏ thiết bị Push Notification khỏi tài khoản!')
                return redirect('admin_dashboard:user_detail', user_id=user_id)

        raw_user = db.users.find_one({'_id': oid}, {'password': 0, 'emailVerificationToken': 0})
        if not raw_user:
            messages.error(request, 'Không tìm thấy người dùng!')
            return redirect('admin_dashboard:users_list')

        # Auto-expire temporary ban if banExpiresAt has passed
        ban_exp = raw_user.get('banExpiresAt')
        if raw_user.get('isBanned') and isinstance(ban_exp, datetime) and ban_exp <= datetime.utcnow():
            db.users.update_one(
                {'_id': oid},
                {'$set': {'isBanned': False, 'banReason': '', 'banExpiresAt': None, 'bannedAt': None, 'bannedBy': ''}},
            )
            raw_user['isBanned'] = False
            raw_user['banReason'] = ''
            raw_user['banExpiresAt'] = None

        created_dt = raw_user.get('createdAt')
        updated_dt = raw_user.get('updatedAt') or created_dt
        user = clean_value(raw_user)
        user['id'] = user.get('_id')
        # Provide snake_case aliases expected by templates alongside camelCase
        user['is_email_verified'] = bool(raw_user.get('isEmailVerified'))
        user['created_at'] = created_dt
        user['updated_at'] = updated_dt
        friend_oids = [x for x in (raw_user.get('friends') or []) if isinstance(x, ObjectId)]
        req_oids = [x for x in (raw_user.get('friendRequests') or []) if isinstance(x, ObjectId)]
        push_tokens = [str(t) for t in (raw_user.get('pushTokens') or []) if t]
        user['friends_count'] = len(friend_oids)
        user['friend_requests_count'] = len(req_oids)
        watchlist = user.get('watchlist') or []

        def _fetch_social(oid_list):
            if not oid_list:
                return []
            docs = list(db.users.find(
                {'_id': {'$in': oid_list}},
                {'name': 1, 'username': 1, 'email': 1, 'avatar': 1},
            ))
            cleaned = clean_value(docs)
            for d in cleaned:
                d['id'] = d.get('_id')
            return cleaned

        detail_data = parallel_run({
            'sessions': lambda: list(db.sessions.find({'userId': oid}, {'refreshToken': 0}).sort('lastActive', -1)),
            'watch_progress': lambda: list(db.watchprogresses.find({'userId': oid}).sort('lastWatched', -1).limit(30)),
            'search_doc': lambda: db.searchhistories.find_one({'$or': [{'user_id': oid}, {'userId': oid}]}),
            'comments': lambda: list(db.comments.find({'userId': oid}).sort('createdAt', -1).limit(50)),
            'stream_histories': lambda: list(db.streamhistories.find({'userId': oid}).sort('lastCreatedAt', -1).limit(20)),
            'friends_list': lambda: _fetch_social(friend_oids),
            'friend_requests_list': lambda: _fetch_social(req_oids),
        }, max_workers=6)

        active_sessions = clean_value(detail_data['sessions'])
        for s in active_sessions:
            s['id'] = s.get('_id')
            dev = format_device_display(s)
            s['device_primary'] = dev['primary']
            s['device_secondary'] = dev['secondary']

        watch_progress = clean_value(detail_data['watch_progress'])
        for wp in watch_progress:
            dur = float(wp.get('duration') or 0)
            cur = float(wp.get('currentTime') or 0)
            wp['pct'] = min(100, round((cur / dur) * 100)) if dur > 0 else 0
            wp['media_id'] = wp.get('media_id') or wp.get('contentId') or ''
            wp['media_type'] = wp.get('media_type') or ('tv' if wp.get('isTVShow') else 'movie')
            wp['season_number'] = wp.get('season_number') or wp.get('season')
            wp['episode_number'] = wp.get('episode_number') or wp.get('episode')

        search_doc = detail_data['search_doc']
        search_queries = []
        if search_doc:
            if 'history' in search_doc and isinstance(search_doc['history'], list):
                for entry in reversed(search_doc['history']):
                    if isinstance(entry, dict) and entry.get('query'):
                        search_queries.append({
                            'query': entry.get('query'),
                            'searched_at': clean_value(entry.get('searched_at')),
                        })
                    elif isinstance(entry, str):
                        search_queries.append({'query': entry, 'searched_at': None})
            elif 'queries' in search_doc and isinstance(search_doc['queries'], list):
                for q in search_doc['queries']:
                    search_queries.append({'query': str(q), 'searched_at': None})

        comments = clean_value(detail_data['comments'])
        for c in comments:
            c['media_id'] = c.get('media_id') or c.get('movieId') or ''
            c['media_type'] = c.get('media_type') or c.get('type') or 'comment'
        stream_histories = clean_value(detail_data['stream_histories'])

        context = {
            'user': user,
            'watchlist_count': len(watchlist),
            'watchlist_overflow': [
                {
                    'id': m.get('id'),
                    'title': m.get('title') or m.get('name') or 'Untitled',
                    'poster_path': m.get('poster_path') or '',
                    'release_date': m.get('release_date') or '',
                }
                for m in watchlist[24:]
                if isinstance(m, dict)
            ],
            'friends_count': len(friend_oids),
            'friends_list': detail_data['friends_list'],
            'friend_requests_list': detail_data['friend_requests_list'],
            'push_tokens': push_tokens,
            'active_sessions': active_sessions,
            'sessions_count': len(active_sessions),
            'watch_progress': watch_progress,
            'watch_progress_count': len(watch_progress),
            'stream_histories': stream_histories,
            'search_queries': search_queries,
            'search_count': len(search_queries),
            'comments': comments,
            'comments_count': len(comments),
            'api_status': check_api_status(),
        }
        return render(request, 'admin_dashboard/users/user_detail.html', context)
    except Exception as e:
        messages.error(request, f'Lỗi: {e}')
        return redirect('admin_dashboard:users_list')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('users.edit')
def user_edit(request, user_id):
    """Chỉnh sửa thông tin user trực tiếp trong MongoDB."""
    try:
        from datetime import datetime
        db = mongo_admin.db()
        oid = ObjectId(user_id)

        if request.method == 'POST':
            name = request.POST.get('name', '').strip()
            username = request.POST.get('username', '').strip().lower()
            email = request.POST.get('email', '').strip().lower()
            avatar = request.POST.get('avatar', '').strip()
            is_email_verified = request.POST.get('is_email_verified') == 'on'
            is_banned = request.POST.get('is_banned') == 'on'
            ban_reason = request.POST.get('ban_reason', '').strip()

            update_doc = {
                'name': name,
                'email': email,
                'isEmailVerified': is_email_verified,
                'isBanned': is_banned,
                'banReason': ban_reason if is_banned else '',
                'updatedAt': datetime.utcnow(),
            }
            if username:
                update_doc['username'] = username
            if avatar:
                update_doc['avatar'] = avatar
            if is_banned:
                update_doc['bannedAt'] = datetime.utcnow()
                update_doc['bannedBy'] = request.user.username
                db.sessions.delete_many({'userId': oid})

            db.users.update_one({'_id': oid}, {'$set': update_doc})
            AuditLog.log(request, 'edit_user', 'user', user_id, clean_value(update_doc))
            messages.success(request, 'Cập nhật hồ sơ người dùng thành công!')
            return redirect('admin_dashboard:user_detail', user_id=user_id)

        raw_user = db.users.find_one({'_id': oid}, {'password': 0, 'emailVerificationToken': 0})
        if not raw_user:
            messages.error(request, 'Không tìm thấy người dùng!')
            return redirect('admin_dashboard:users_list')

        # Auto-expire temporary ban if banExpiresAt has passed
        ban_exp = raw_user.get('banExpiresAt')
        if raw_user.get('isBanned') and isinstance(ban_exp, datetime) and ban_exp <= datetime.utcnow():
            db.users.update_one(
                {'_id': oid},
                {'$set': {'isBanned': False, 'banReason': '', 'banExpiresAt': None, 'bannedAt': None, 'bannedBy': ''}},
            )
            raw_user['isBanned'] = False
            raw_user['banReason'] = ''
            raw_user['banExpiresAt'] = None

        user = clean_value(raw_user)
        user['id'] = user.get('_id')
        user['is_email_verified'] = bool(raw_user.get('isEmailVerified'))
        user['created_at'] = raw_user.get('createdAt')
        user['updated_at'] = raw_user.get('updatedAt')
        return render(request, 'admin_dashboard/users/user_edit.html', {'user': user})
    except Exception as e:
        messages.error(request, f'Lỗi: {e}')
        return redirect('admin_dashboard:users_list')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('users.delete')
def user_delete(request, user_id):
    """Xóa user trực tiếp trong MongoDB kèm toàn bộ dữ liệu liên kết (Cascade Delete)."""
    from users_management.rate_limit import consume_action_rate_limit, format_retry_wait
    from users_management.webhook_dispatcher import dispatch_webhook_event
    allowed_rl, retry_after, _ = consume_action_rate_limit('admin_sensitive', request)
    if not allowed_rl:
        messages.error(request, f'Thao tác quá nhanh. Vui lòng thử lại sau {format_retry_wait(retry_after)}.')
        return redirect('admin_dashboard:users_list')
    try:
        db = mongo_admin.db()
        oid = ObjectId(user_id)

        # Cascade xóa toàn bộ dữ liệu liên quan của user song song
        parallel_run({
            'sessions': lambda: db.sessions.delete_many({'userId': oid}),
            'comments': lambda: db.comments.delete_many({'userId': oid}),
            'searchhistories': lambda: db.searchhistories.delete_many({'$or': [{'user_id': oid}, {'userId': oid}]}),
            'watchprogresses': lambda: db.watchprogresses.delete_many({'userId': oid}),
            'notifications': lambda: db.notifications.delete_many({'$or': [{'recipient': oid}, {'sender': oid}]}),
            'passwordresettokens': lambda: db.passwordresettokens.delete_many({'user': oid}),
            'friend_refs': lambda: db.users.update_many(
                {'$or': [{'friends': oid}, {'friendRequests': oid}, {'sentFriendRequests': oid}]},
                {'$pull': {'friends': oid, 'friendRequests': oid, 'sentFriendRequests': oid}},
            ),
        }, max_workers=7)

        # Xóa user & invalidate caches
        result = db.users.delete_one({'_id': oid})
        invalidate_swr('admin_sidebar_counts', 'user_list_stats_v2', 'mongo_overview_cache', 'sessions_global_stats_v1')
        AuditLog.log(request, 'delete_user', 'user', user_id)
        if result.deleted_count:
            dispatch_webhook_event('user.deleted', {'user_id': user_id, 'admin': request.user.username})
        messages.success(request, f'Đã xóa người dùng và dọn sạch toàn bộ dữ liệu liên quan! ({result.deleted_count} bản ghi)')
    except Exception as e:
        messages.error(request, f'Lỗi: {e}')
    return redirect('admin_dashboard:users_list')
