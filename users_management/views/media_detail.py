"""Movie & TV Show detail views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render

from users_management.decorators import require_permission
from users_management.services import mongo_admin, clean_value, parallel_run
from users_management.views_legacy import check_admin_access, check_api_status

__all__ = ['movie_detail', 'tv_detail']


def _media_detail(request, media_id, is_tv=False):
    """Shared logic for movie/tv detail page."""
    try:
        db = mongo_admin.db()

        try:
            media_id_int = int(media_id)
        except ValueError:
            media_id_int = media_id

        progress_query = {'contentId': str(media_id)}
        if is_tv:
            progress_query['isTVShow'] = True
        else:
            progress_query['isTVShow'] = {'$ne': True}

        user_lookup = {
            '$lookup': {
                'from': 'users',
                'let': {'uid': '$userId'},
                'pipeline': [
                    {'$match': {'$expr': {'$eq': ['$_id', '$$uid']}}},
                    {'$project': {'name': 1, 'email': 1, 'username': 1, 'avatar': 1}},
                ],
                'as': 'user',
            }
        }

        progress_pipeline = [
            {'$match': progress_query},
            {'$sort': {'lastWatched': -1}},
            {'$limit': 50},
            user_lookup,
            {'$unwind': {'path': '$user', 'preserveNullAndEmptyArrays': True}},
        ]

        comment_pipeline = [
            {'$match': {'movieId': str(media_id)}},
            {'$sort': {'createdAt': -1}},
            {'$limit': 50},
            user_lookup,
            {'$unwind': {'path': '$user', 'preserveNullAndEmptyArrays': True}},
        ]

        res = parallel_run({
            'users_with_watchlist': lambda: clean_value(list(db.users.find(
                {'watchlist.id': media_id_int},
                {'name': 1, 'email': 1, 'avatar': 1, 'watchlist.$': 1}
            ).limit(200))),
            'watchlist_count': lambda: db.users.count_documents({'watchlist.id': media_id_int}),
            'progress_items': lambda: clean_value(list(db.watchprogresses.aggregate(progress_pipeline))),
            'progress_count': lambda: db.watchprogresses.count_documents(progress_query),
            'comments': lambda: clean_value(list(db.comments.aggregate(comment_pipeline))),
            'comment_count': lambda: db.comments.count_documents({'movieId': str(media_id)}),
            'api_status': check_api_status,
        }, max_workers=6)

        users_with_watchlist = res['users_with_watchlist']
        progress_items = res['progress_items']
        comments = res['comments']

        media_info = {'id': media_id, 'title': f'Content #{media_id}', 'poster': ''}
        for u in users_with_watchlist:
            wl = u.get('watchlist', [])
            if wl:
                media_info['title'] = wl[0].get('title', media_info['title'])
                media_info['poster'] = wl[0].get('poster_path', '')
                break

        context = {
            'media': media_info,
            'is_tv': is_tv,
            'watchlist_users': users_with_watchlist,
            'progress_items': progress_items,
            'comments': comments,
            'stats': {
                'watchlist_count': res['watchlist_count'],
                'progress_count': res['progress_count'],
                'comment_count': res['comment_count'],
            },
            'api_status': res['api_status'],
        }
    except Exception as e:
        context = {
            'media': {'id': media_id, 'title': 'Error', 'poster': ''},
            'is_tv': is_tv,
            'error': str(e),
            'watchlist_users': [], 'progress_items': [], 'comments': [],
            'stats': {'watchlist_count': 0, 'progress_count': 0, 'comment_count': 0},
            'api_status': {'status': 'offline'},
        }
    template = 'admin_dashboard/tv_shows/tv_detail.html' if is_tv else 'admin_dashboard/movies/movie_detail.html'
    return render(request, template, context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('movies.view')
def movie_detail(request, media_id):
    return _media_detail(request, media_id, is_tv=False)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('tv_shows.view')
def tv_detail(request, media_id):
    return _media_detail(request, media_id, is_tv=True)
