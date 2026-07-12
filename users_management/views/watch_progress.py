"""Watch Progress management views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render
from datetime import datetime

from users_management.models import AuditLog
from users_management.services import mongo_admin, clean_value
from users_management.views_legacy import check_admin_access, check_api_status

__all__ = ['watch_progress_list', 'api_watch_progress_delete']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
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

        total = db.watchprogresses.count_documents(query)
        total_pages = max(1, -(-total // limit))

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
        ])
        items = clean_value(list(db.watchprogresses.aggregate(pipeline)))
        for item in items:
            item['id'] = item.get('_id')

        # Stats
        stats = {
            'total': total,
            'movies': db.watchprogresses.count_documents({'isTVShow': {'$ne': True}}),
            'tv_shows': db.watchprogresses.count_documents({'isTVShow': True}),
        }
        # Top watched content
        top_pipeline = [
            {'$group': {'_id': '$contentId', 'count': {'$sum': 1}, 'title': {'$first': '$title'}}},
            {'$sort': {'count': -1}},
            {'$limit': 5},
        ]
        stats['top_content'] = clean_value(list(db.watchprogresses.aggregate(top_pipeline)))
        for item in stats['top_content']:
            item['id'] = item.get('_id', '')

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
