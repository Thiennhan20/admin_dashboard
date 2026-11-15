"""Watch Progress management views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render
from datetime import datetime

from users_management.decorators import require_permission
from users_management.models import AuditLog
from users_management.services import mongo_admin, clean_value, parallel_run, cached_swr
from users_management.views_legacy import check_admin_access, check_api_status

__all__ = ['watch_progress_list', 'api_watch_progress_delete']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('watch_progress.view')
def watch_progress_list(request):
    """Full watch-progress management page with stats, filters, pagination."""
    page = int(request.GET.get('page', 1))
    limit = int(request.GET.get('limit', 20))
    search = request.GET.get('search', '').strip()
    content_type = request.GET.get('type', '')

    try:
        db = mongo_admin.db()
        query = {}
        if search:
            query['$or'] = [
                {'title': {'$regex': search, '$options': 'i'}},
                {'contentId': search},
            ]
        if content_type == 'movie':
            query['isTVShow'] = {'$ne': True}
        elif content_type == 'tv':
            query['isTVShow'] = True

        pipeline = []
        if query:
            pipeline.append({'$match': query})
        pipeline.extend([
            {'$sort': {'lastWatched': -1}},
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
                'user.password': 0,
                'user.emailVerificationToken': 0,
                'user.watchlist': 0,
                'user.friends': 0,
                'user.friendRequests': 0,
                'user.sentFriendRequests': 0,
            }},
        ])

        def _fetch_wp_stats():
            type_agg = list(db.watchprogresses.aggregate([
                {'$group': {
                    '_id': None,
                    'total_all': {'$sum': 1},
                    'tv_shows': {'$sum': {'$cond': [{'$eq': ['$isTVShow', True]}, 1, 0]}},
                    'movies': {'$sum': {'$cond': [{'$ne': ['$isTVShow', True]}, 1, 0]}},
                }}
            ]))
            top_list = clean_value(list(db.watchprogresses.aggregate([
                {'$group': {'_id': '$contentId', 'count': {'$sum': 1}, 'title': {'$first': '$title'}}},
                {'$sort': {'count': -1}},
                {'$limit': 5},
            ])))
            for top_item in top_list:
                top_item['id'] = top_item.get('_id', '')
            s = type_agg[0] if type_agg else {}
            return {
                'movies': s.get('movies', 0),
                'tv_shows': s.get('tv_shows', 0),
                'total_all': s.get('total_all', 0),
                'top_content': top_list,
            }

        force_stats = request.GET.get('refresh') == '1'
        tasks = {
            'items': lambda: list(db.watchprogresses.aggregate(pipeline)),
            'wp_stats': lambda: cached_swr(
                'watch_progress_stats_v1',
                fresh_ttl=30,
                stale_ttl=120,
                fetcher=_fetch_wp_stats,
                force=force_stats,
            ),
        }
        if query:
            tasks['total'] = lambda: db.watchprogresses.count_documents(query)

        results = parallel_run(tasks, max_workers=3)
        wp_stats = results['wp_stats']
        total = results['total'] if query else wp_stats['total_all']
        total_pages = max(1, -(-total // limit))

        items = clean_value(results['items'])
        for item in items:
            item['id'] = item.get('_id')

        stats = {
            'total': total,
            'movies': wp_stats['movies'],
            'tv_shows': wp_stats['tv_shows'],
            'top_content': wp_stats['top_content'],
        }

        context = {
            'items': items,
            'stats': stats,
            'page': page,
            'limit': limit,
            'total': total,
            'total_pages': total_pages,
            'search': search,
            'content_type': content_type,
            'api_status': check_api_status(),
        }
    except Exception as e:
        context = {
            'items': [], 'stats': {}, 'page': 1, 'total': 0, 'total_pages': 1,
            'error': f'Error: {e}', 'api_status': {'status': 'offline'},
            'search': search, 'content_type': content_type, 'limit': limit,
        }
    return render(request, 'admin_dashboard/watch_progress/watch_progress_list.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('watch_progress.delete')
def api_watch_progress_delete(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)
    import json
    body = json.loads(request.body)
    ids = body.get('ids', [])
    if not ids:
        return JsonResponse({'success': False, 'error': 'No IDs provided'})
    try:
        from bson import ObjectId
        db = mongo_admin.db()
        oids = [ObjectId(i) for i in ids]
        result = db.watchprogresses.delete_many({'_id': {'$in': oids}})
        AuditLog.log(request, 'bulk_delete_watch_progress', 'watchprogress', '', {'count': result.deleted_count})
        return JsonResponse({'success': True, 'deleted': result.deleted_count})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
