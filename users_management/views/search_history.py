"""Search History management views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render
import json

from users_management.decorators import require_permission
from users_management.models import AuditLog
from users_management.services import mongo_admin, clean_value, parallel_run, cached_swr
from users_management.views_legacy import check_admin_access, check_api_status

__all__ = ['search_history_list', 'api_search_history_delete']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('search_history.view')
def search_history_list(request):
    page = int(request.GET.get('page', 1))
    limit = int(request.GET.get('limit', 20))
    search = request.GET.get('search', '').strip()

    try:
        db = mongo_admin.db()
        query = {}
        if search:
            query['$or'] = [
                {'history.query': {'$regex': search, '$options': 'i'}},
                {'queries': {'$regex': search, '$options': 'i'}},
            ]

        pipeline = []
        if query:
            pipeline.append({'$match': query})
        pipeline.extend([
            {'$addFields': {
                'resolvedUserId': {'$ifNull': ['$user_id', '$userId']},
                'resolvedUpdatedAt': {'$ifNull': ['$updated_at', '$updatedAt']},
            }},
            {'$sort': {'resolvedUpdatedAt': -1, '_id': -1}},
            {'$skip': (page - 1) * limit},
            {'$limit': limit},
            {'$lookup': {
                'from': 'users',
                'localField': 'resolvedUserId',
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

        top_pipeline = [
            {'$project': {
                'terms': {
                    '$cond': [
                        {'$isArray': '$history'},
                        {'$map': {'input': '$history', 'as': 'h', 'in': '$$h.query'}},
                        {'$ifNull': ['$queries', []]},
                    ]
                }
            }},
            {'$unwind': '$terms'},
            {'$match': {'terms': {'$ne': None, '$ne': ''}}},
            {'$group': {'_id': {'$toLower': '$terms'}, 'count': {'$sum': 1}}},
            {'$sort': {'count': -1}},
            {'$limit': 20},
        ]

        force_cache = request.GET.get('refresh') == '1'
        results = parallel_run({
            'total': lambda: db.searchhistories.count_documents(query),
            'items': lambda: list(db.searchhistories.aggregate(pipeline)),
            'top_terms': lambda: cached_swr(
                'search_history_top_terms_v1',
                fresh_ttl=60,
                stale_ttl=300,
                fetcher=lambda: list(db.searchhistories.aggregate(top_pipeline)),
                force=force_cache,
            ),
        }, max_workers=3)

        total = results['total']
        total_pages = max(1, -(-total // limit))

        items = clean_value(results['items'])
        for item in items:
            item['id'] = item.get('_id')
            item['userId'] = item.get('user_id') or item.get('userId') or item.get('resolvedUserId')
            item['updatedAt'] = item.get('updated_at') or item.get('updatedAt') or item.get('created_at')
            # Normalize history entries into `queries` list of strings + `history_items`
            raw_hist = item.get('history') or []
            if raw_hist and isinstance(raw_hist, list):
                extracted = [
                    (h.get('query', '') if isinstance(h, dict) else str(h))
                    for h in reversed(raw_hist)
                ]
                item['queries'] = [q for q in extracted if q]
            elif not item.get('queries'):
                item['queries'] = []

        top_terms = clean_value(results['top_terms'])
        for t in top_terms:
            t['term'] = t.get('_id', '')

        context = {
            'items': items,
            'top_terms': top_terms,
            'page': page, 'limit': limit, 'total': total,
            'total_pages': total_pages, 'search': search,
            'api_status': check_api_status(),
        }
    except Exception as e:
        context = {
            'items': [], 'top_terms': [], 'page': 1, 'total': 0,
            'total_pages': 1, 'error': str(e), 'search': search,
            'limit': limit, 'api_status': {'status': 'offline'},
        }
    return render(request, 'admin_dashboard/search_history/search_history_list.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('search_history.delete')
def api_search_history_delete(request):
    if request.method != 'POST':
        return JsonResponse({'success': False}, status=405)
    body = json.loads(request.body)
    ids = body.get('ids', [])
    try:
        from bson import ObjectId
        db = mongo_admin.db()
        oids = [ObjectId(i) for i in ids]
        result = db.searchhistories.delete_many({'_id': {'$in': oids}})
        AuditLog.log(request, 'bulk_delete_search_history', 'searchhistory', '', {'count': result.deleted_count})
        return JsonResponse({'success': True, 'deleted': result.deleted_count})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
