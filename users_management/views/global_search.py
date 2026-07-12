"""Global search API endpoint."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse

from users_management.services import mongo_admin, clean_value
from users_management.views_legacy import check_admin_access

__all__ = ['api_global_search']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def api_global_search(request):
    q = request.GET.get('q', '').strip()
    if len(q) < 2:
        return JsonResponse({'success': True, 'results': {}})

    results = {}
    try:
        db = mongo_admin.db()
        regex = {'$regex': q, '$options': 'i'}

        # Users
        users = clean_value(list(
            db.users.find(
                {'$or': [{'name': regex}, {'email': regex}]},
                {'name': 1, 'email': 1, 'avatar': 1}
            ).limit(5)
        ))
        if users:
            results['users'] = [{'id': u['_id'], 'name': u.get('name', ''), 'email': u.get('email', '')} for u in users]

        # Comments
        comments = clean_value(list(
            db.comments.find(
                {'content': regex},
                {'content': 1, 'movieId': 1, 'createdAt': 1}
            ).limit(5)
        ))
        if comments:
            results['comments'] = [{'id': c['_id'], 'content': c.get('content', '')[:100], 'movieId': c.get('movieId', '')} for c in comments]

        # Movies in watchlists
        movie_pipeline = [
            {'$unwind': '$watchlist'},
            {'$match': {'watchlist.title': regex}},
            {'$group': {
                '_id': '$watchlist.id',
                'title': {'$first': '$watchlist.title'},
                'type': {'$first': '$watchlist.type'},
                'poster': {'$first': '$watchlist.poster_path'},
            }},
            {'$limit': 5},
        ]
        movies = clean_value(list(db.users.aggregate(movie_pipeline)))
        if movies:
            results['movies'] = [{'id': m['_id'], 'title': m.get('title', ''), 'type': m.get('type', 'movie'), 'poster': m.get('poster', '')} for m in movies]

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

    return JsonResponse({'success': True, 'results': results, 'query': q})
