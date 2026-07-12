"""Search History management views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render
import json

from users_management.models import AuditLog
from users_management.services import mongo_admin, clean_value
from users_management.views_legacy import check_admin_access, check_api_status

__all__ = ['search_history_list', 'api_search_history_delete']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def search_history_list(request):
    page = int(request.GET.get('page', 1))
    limit = int(request.GET.get('limit', 20))
    search = request.GET.get('search', '').strip()

    try:
        db = mongo_admin.db()
        query = {}
        if search:
            query['queries'] = {'$regex': search, '$options': 'i'}

        total = db.searchhistories.count_documents(query)
        total_pages = max(1, -(-total // limit))

        pipeline = []
        if query:
            pipeline.append({'$match': query})
        pipeline.extend([
            {'$sort': {'updated_at': -1}},
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
        items = clean_value(list(db.searchhistories.aggregate(pipeline)))
        for item in items:
            item['id'] = item.get('_id')

        # Top search terms
        top_pipeline = [
            {'$unwind': '$queries'},
            {'$group': {'_id': '$queries', 'count': {'$sum': 1}}},
            {'$sort': {'count': -1}},
            {'$limit': 20},
        ]
        top_terms = clean_value(list(db.searchhistories.aggregate(top_pipeline)))
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
