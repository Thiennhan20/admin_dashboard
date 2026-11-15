"""Watchlist management views with server-side aggregation, pagination, search, and delete."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render, redirect
from django.http import JsonResponse, Http404
from django.contrib import messages
from bson import ObjectId

from users_management.decorators import require_permission
from users_management.models import AuditLog
from users_management.services import mongo_admin, clean_value, parallel_run, cached_swr
from users_management.views_legacy import check_admin_access, check_api_status

__all__ = ['watchlist_list', 'watchlist_detail', 'watchlist_edit', 'watchlist_delete']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('watchlist.view')
def watchlist_list(request):
    """Trang danh sách watchlist dùng MongoDB Aggregation để phân trang và tìm kiếm."""
    page = int(request.GET.get('page', 1))
    limit = int(request.GET.get('limit', 20))
    search = request.GET.get('search', '').strip()
    media_type = request.GET.get('type', '')

    try:
        db = mongo_admin.db()
        
        # Combined $facet pipeline: project only needed fields before $unwind
        match_query = {}
        if search:
            match_query['watchlist.title'] = {'$regex': search, '$options': 'i'}
        if media_type:
            match_query['watchlist.type'] = media_type

        facet_pipeline = [
            {'$match': {'watchlist.0': {'$exists': True}}},
            {'$project': {'name': 1, 'email': 1, 'createdAt': 1, 'watchlist': 1}},
            {'$unwind': '$watchlist'},
        ]
        if match_query:
            facet_pipeline.append({'$match': match_query})
        facet_pipeline.append({
            '$facet': {
                'meta': [{'$count': 'total'}],
                'items': [
                    {'$sort': {'watchlist.title': 1}},
                    {'$skip': (page - 1) * limit},
                    {'$limit': limit},
                ],
            }
        })

        def _fetch_watchlist_stats():
            unique_movies_pipeline = [
                {'$match': {'watchlist.0': {'$exists': True}}},
                {'$project': {'watchlist.id': 1}},
                {'$unwind': '$watchlist'},
                {'$group': {'_id': '$watchlist.id'}},
                {'$count': 'total'},
            ]
            sub = parallel_run({
                'unique_movies': lambda: list(db.users.aggregate(unique_movies_pipeline)),
                'unique_users': lambda: db.users.count_documents({'watchlist.0': {'$exists': True}}),
            }, max_workers=2)
            um_res = sub['unique_movies']
            return {
                'unique_movies': um_res[0]['total'] if um_res else 0,
                'unique_users': sub['unique_users'],
            }

        force_stats = request.GET.get('refresh') == '1'
        results = parallel_run({
            'facet': lambda: list(db.users.aggregate(facet_pipeline)),
            'stats': lambda: cached_swr(
                'watchlist_global_stats_v1',
                fresh_ttl=30,
                stale_ttl=120,
                fetcher=_fetch_watchlist_stats,
                force=force_stats,
            ),
        }, max_workers=2)

        facet_doc = results['facet'][0] if results['facet'] else {}
        meta_list = facet_doc.get('meta', [])
        total_items = meta_list[0]['total'] if meta_list else 0
        total_pages = max(1, -(-total_items // limit))

        items = facet_doc.get('items', [])
        watchlist_items = []
        for doc in items:
            wl = doc.get('watchlist', {})
            user_id = str(doc.get('_id'))
            movie_id = str(wl.get('id'))
            watchlist_items.append({
                'id': f"{user_id}_{movie_id}",
                'user': {
                    'id': user_id,
                    'name': doc.get('name', 'Unknown'),
                    'email': doc.get('email', ''),
                },
                'movie_id': movie_id,
                'title': wl.get('title', 'Unknown'),
                'type': wl.get('type', 'movie'),
                'poster_path': wl.get('poster_path', ''),
                'added_at': doc.get('createdAt'),
            })

        watchlist_items = clean_value(watchlist_items)
        unique_movies = results['stats']['unique_movies']
        unique_users = results['stats']['unique_users']

        context = {
            'watchlist_items': watchlist_items,
            'total_items': total_items,
            'unique_movies': unique_movies,
            'unique_users': unique_users,
            'page': page,
            'limit': limit,
            'total_pages': total_pages,
            'search': search,
            'media_type': media_type,
            'api_status': check_api_status(),
        }
    except Exception as e:
        context = {
            'watchlist_items': [],
            'total_items': 0,
            'unique_movies': 0,
            'unique_users': 0,
            'page': 1,
            'limit': limit,
            'total_pages': 1,
            'search': search,
            'media_type': media_type,
            'error': f'Lỗi: {e}',
            'api_status': {'status': 'offline'},
        }

    return render(request, 'admin_dashboard/watchlist/watchlist_list.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('watchlist.view')
def watchlist_detail(request, item_id):
    """Xem chi tiết một item trong watchlist."""
    try:
        user_id, movie_id = item_id.split('_', 1)
    except ValueError:
        raise Http404("Invalid watchlist item format.")

    try:
        db = mongo_admin.db()
        
        # Get user details
        user = db.users.find_one({'_id': ObjectId(user_id)})
        if not user:
            messages.error(request, 'Không tìm thấy người dùng!')
            return redirect('admin_dashboard:watchlist_list')

        # Find movie inside user's watchlist
        movie_item = None
        watchlist = user.get('watchlist', [])
        for item in watchlist:
            if str(item.get('id')) == movie_id:
                movie_item = item
                break

        if not movie_item:
            messages.error(request, 'Không tìm thấy phim trong danh sách xem của người dùng!')
            return redirect('admin_dashboard:watchlist_list')

        movie_item = clean_value(movie_item)
        user = clean_value(user)

        context = {
            'item': {
                'id': item_id,
                'user': user,
                'movie': movie_item
            },
            'api_status': check_api_status(),
        }
        return render(request, 'admin_dashboard/watchlist/watchlist_detail.html', context)
    except Exception as e:
        messages.error(request, f'Lỗi: {e}')
        return redirect('admin_dashboard:watchlist_list')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('watchlist.edit')
def watchlist_edit(request, item_id):
    """Edit watchlist item is usually not required or is simple redirect, but we provide it for compatibility."""
    messages.info(request, 'Mục danh sách xem do người dùng trực tiếp quản lý.')
    return redirect('admin_dashboard:watchlist_detail', item_id=item_id)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('watchlist.delete')
def watchlist_delete(request, item_id):
    """Xóa một item khỏi watchlist của user."""
    try:
        user_id, movie_id = item_id.split('_', 1)
    except ValueError:
        messages.error(request, 'Định dạng ID không hợp lệ!')
        return redirect('admin_dashboard:watchlist_list')

    try:
        db = mongo_admin.db()
        # Try numeric ID first, then string
        try:
            m_id = int(movie_id)
        except ValueError:
            m_id = movie_id

        result = db.users.update_one(
            {'_id': ObjectId(user_id)},
            {'$pull': {'watchlist': {'id': m_id}}}
        )
        if result.modified_count == 0 and isinstance(m_id, int):
            # Try as string if numeric failed
            result = db.users.update_one(
                {'_id': ObjectId(user_id)},
                {'$pull': {'watchlist': {'id': str(m_id)}}}
            )

        AuditLog.log(request, 'delete_watchlist_item', 'watchlist', item_id)
        messages.success(request, 'Đã xóa phim khỏi danh sách xem của người dùng!')
    except Exception as e:
        messages.error(request, f'Lỗi: {e}')

    return redirect('admin_dashboard:watchlist_list')
