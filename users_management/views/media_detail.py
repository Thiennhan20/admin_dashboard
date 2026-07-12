"""Movie & TV Show detail views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render

from users_management.services import mongo_admin, clean_value
from users_management.views_legacy import check_admin_access, check_api_status

__all__ = ['movie_detail', 'tv_detail']


def _media_detail(request, media_id, is_tv=False):
    """Shared logic for movie/tv detail page."""
    try:
        db = mongo_admin.db()
        id_field = 'id'

        # Find in watchlists
        try:
            media_id_int = int(media_id)
        except ValueError:
            media_id_int = media_id

        users_with_watchlist = clean_value(list(db.users.find(
            {'watchlist.id': media_id_int},
            {'name': 1, 'email': 1, 'avatar': 1, 'watchlist.$': 1}
        )))

        # Get media info from first watchlist entry
        media_info = {'id': media_id, 'title': f'Content #{media_id}', 'poster': ''}
        for u in users_with_watchlist:
            wl = u.get('watchlist', [])
            if wl:
                media_info['title'] = wl[0].get('title', media_info['title'])
                media_info['poster'] = wl[0].get('poster_path', '')
                break

        # Watch progress
        progress_query = {'contentId': str(media_id)}
        if is_tv:
            progress_query['isTVShow'] = True
        else:
            progress_query['isTVShow'] = {'$ne': True}

        progress_pipeline = [
            {'$match': progress_query},
            {'$sort': {'lastWatched': -1}},
            {'$limit': 50},
            {'$lookup': {
                'from': 'users',
                'localField': 'userId',
                'foreignField': '_id',
                'as': 'user',
            }},
            {'$unwind': {'path': '$user', 'preserveNullAndEmptyArrays': True}},
        ]
        progress_items = clean_value(list(db.watchprogresses.aggregate(progress_pipeline)))

        # Related comments
        comment_pipeline = [
            {'$match': {'movieId': str(media_id)}},
            {'$sort': {'createdAt': -1}},
            {'$limit': 50},
            {'$lookup': {
                'from': 'users',
                'localField': 'userId',
                'foreignField': '_id',
                'as': 'user',
            }},
            {'$unwind': {'path': '$user', 'preserveNullAndEmptyArrays': True}},
        ]
        comments = clean_value(list(db.comments.aggregate(comment_pipeline)))

        context = {
            'media': media_info,
            'is_tv': is_tv,
            'watchlist_users': users_with_watchlist,
            'progress_items': progress_items,
            'comments': comments,
            'stats': {
                'watchlist_count': len(users_with_watchlist),
                'progress_count': len(progress_items),
                'comment_count': len(comments),
            },
            'api_status': check_api_status(),
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
def movie_detail(request, media_id):
    return _media_detail(request, media_id, is_tv=False)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def tv_detail(request, media_id):
    return _media_detail(request, media_id, is_tv=True)
