"""Game management views for gameprofiles and gamehistories."""
import json
from datetime import datetime
from bson import ObjectId
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render

from users_management.models import AuditLog
from users_management.services import mongo_admin, clean_value
from users_management.views_legacy import check_admin_access, check_api_status

__all__ = [
    'game_profiles_list',
    'game_histories_list',
    'api_game_profile_delete',
    'api_game_history_delete',
]


def format_game_dt(val):
    if not val:
        return '-'
    val_str = str(val).strip()
    if 'T' in val_str:
        parts = val_str.split('T')
        date_part = parts[0]
        time_part = parts[1].split('.')[0][:5] if len(parts) > 1 else ''
        return f"{date_part} {time_part}".strip()
    elif ' ' in val_str:
        parts = val_str.split(' ')
        date_part = parts[0]
        time_part = parts[1].split('.')[0][:5] if len(parts) > 1 else ''
        return f"{date_part} {time_part}".strip()
    return val_str[:16]


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def game_profiles_list(request):
    """Management page for player game profiles (gameprofiles)."""
    page = int(request.GET.get('page', 1))
    limit = int(request.GET.get('limit', 20))
    search = request.GET.get('search', '').strip()

    try:
        db = mongo_admin.db()
        match_query = {}

        pipeline = []
        pipeline.append({
            '$lookup': {
                'from': 'users',
                'localField': 'userId',
                'foreignField': '_id',
                'as': 'user',
            }
        })
        pipeline.append({'$unwind': {'path': '$user', 'preserveNullAndEmptyArrays': True}})

        if search:
            match_query['$or'] = [
                {'user.name': {'$regex': search, '$options': 'i'}},
                {'user.username': {'$regex': search, '$options': 'i'}},
                {'user.email': {'$regex': search, '$options': 'i'}},
                {'userId': ObjectId(search) if len(search) == 24 else search},
            ]

        if match_query:
            pipeline.append({'$match': match_query})

        # Count total matching
        count_pipeline = pipeline + [{'$count': 'total'}]
        count_res = list(db.gameprofiles.aggregate(count_pipeline))
        total = count_res[0]['total'] if count_res else 0
        total_pages = max(1, -(-total // limit))

        # Paginate & sort
        pipeline.extend([
            {'$sort': {'rating': -1, 'updatedAt': -1}},
            {'$skip': (page - 1) * limit},
            {'$limit': limit},
        ])

        items = clean_value(list(db.gameprofiles.aggregate(pipeline)))
        for item in items:
            item['id'] = item.get('_id')
            user = item.get('user')
            if isinstance(user, dict):
                d_name = user.get('name') or user.get('username') or user.get('displayName')
                if not d_name and user.get('email'):
                    d_name = str(user.get('email')).split('@')[0]
                item['display_name'] = d_name or 'Người dùng'
                item['user_email'] = user.get('email', '')
                item['user_avatar'] = user.get('avatar') or user.get('profilePicture') or user.get('originalAvatar')
            else:
                item['display_name'] = 'Người dùng ẩn'
                item['user_email'] = str(item.get('userId', ''))
                item['user_avatar'] = None

            item['formatted_date'] = format_game_dt(item.get('updatedAt') or item.get('createdAt'))

        # Overall Stats
        total_profiles = db.gameprofiles.count_documents({})
        stats_agg = list(db.gameprofiles.aggregate([
            {
                '$group': {
                    '_id': None,
                    'max_rating': {'$max': '$rating'},
                    'total_wins': {'$sum': '$wins'},
                    'total_xp': {'$sum': '$totalXp'},
                }
            }
        ]))
        stats_data = stats_agg[0] if stats_agg else {}
        stats = {
            'total': total_profiles,
            'max_rating': stats_data.get('max_rating', 0) or 0,
            'total_wins': stats_data.get('total_wins', 0) or 0,
            'total_xp': stats_data.get('total_xp', 0) or 0,
        }

        context = {
            'items': items,
            'stats': stats,
            'page': page,
            'limit': limit,
            'total': total,
            'total_pages': total_pages,
            'search': search,
            'api_status': check_api_status(),
        }
    except Exception as e:
        context = {
            'items': [], 'stats': {}, 'page': 1, 'total': 0, 'total_pages': 1,
            'error': f'Error: {e}', 'api_status': {'status': 'offline'},
            'search': search, 'limit': limit,
        }

    return render(request, 'admin_dashboard/games/game_profiles.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def game_histories_list(request):
    """Management page for game match histories (gamehistories)."""
    page = int(request.GET.get('page', 1))
    limit = int(request.GET.get('limit', 20))
    search = request.GET.get('search', '').strip()

    try:
        db = mongo_admin.db()
        match_query = {}

        if search:
            match_query['$or'] = [
                {'roomId': {'$regex': search, '$options': 'i'}},
                {'winnerId': ObjectId(search) if len(search) == 24 else search},
            ]

        total = db.gamehistories.count_documents(match_query)
        total_pages = max(1, -(-total // limit))

        pipeline = []
        if match_query:
            pipeline.append({'$match': match_query})

        pipeline.extend([
            {'$sort': {'createdAt': -1, 'finishedAt': -1}},
            {'$skip': (page - 1) * limit},
            {'$limit': limit},
            {
                '$lookup': {
                    'from': 'users',
                    'localField': 'winnerId',
                    'foreignField': '_id',
                    'as': 'winner',
                }
            },
            {'$unwind': {'path': '$winner', 'preserveNullAndEmptyArrays': True}},
        ])

        items = clean_value(list(db.gamehistories.aggregate(pipeline)))
        for item in items:
            item['id'] = item.get('_id')
            winner = item.get('winner')
            if isinstance(winner, dict):
                w_name = winner.get('name') or winner.get('username') or winner.get('displayName')
                if not w_name and winner.get('email'):
                    w_name = str(winner.get('email')).split('@')[0]
                item['winner_name'] = w_name or 'Người chơi'
                item['winner_avatar'] = winner.get('avatar') or winner.get('profilePicture') or winner.get('originalAvatar')
            else:
                item['winner_name'] = 'Người chơi'
                item['winner_avatar'] = None

            item['formatted_date'] = format_game_dt(item.get('finishedAt') or item.get('createdAt'))

        # Overall Stats
        total_matches = db.gamehistories.count_documents({})
        stats_agg = list(db.gamehistories.aggregate([
            {
                '$group': {
                    '_id': None,
                    'avg_guesses': {'$avg': '$totalGuesses'},
                    'avg_duration': {'$avg': '$duration'},
                }
            }
        ]))
        stats_data = stats_agg[0] if stats_agg else {}
        stats = {
            'total': total_matches,
            'avg_guesses': round(stats_data.get('avg_guesses', 0) or 0, 1),
            'avg_duration': round(stats_data.get('avg_duration', 0) or 0, 1),
        }

        context = {
            'items': items,
            'stats': stats,
            'page': page,
            'limit': limit,
            'total': total,
            'total_pages': total_pages,
            'search': search,
            'api_status': check_api_status(),
        }
    except Exception as e:
        context = {
            'items': [], 'stats': {}, 'page': 1, 'total': 0, 'total_pages': 1,
            'error': f'Error: {e}', 'api_status': {'status': 'offline'},
            'search': search, 'limit': limit,
        }

    return render(request, 'admin_dashboard/games/game_histories.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def api_game_profile_delete(request):
    """API view to delete single/multiple game profiles."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)
    try:
        body = json.loads(request.body)
        ids = body.get('ids', [])
        if not ids:
            return JsonResponse({'success': False, 'error': 'No IDs provided'})

        db = mongo_admin.db()
        oids = [ObjectId(i) for i in ids if len(i) == 24]
        result = db.gameprofiles.delete_many({'_id': {'$in': oids}})
        AuditLog.log(request, 'bulk_delete_game_profiles', 'gameprofile', '', {'count': result.deleted_count})
        return JsonResponse({'success': True, 'deleted': result.deleted_count})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def api_game_history_delete(request):
    """API view to delete single/multiple game match histories."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)
    try:
        body = json.loads(request.body)
        ids = body.get('ids', [])
        if not ids:
            return JsonResponse({'success': False, 'error': 'No IDs provided'})

        db = mongo_admin.db()
        oids = [ObjectId(i) for i in ids if len(i) == 24]
        result = db.gamehistories.delete_many({'_id': {'$in': oids}})
        AuditLog.log(request, 'bulk_delete_game_histories', 'gamehistory', '', {'count': result.deleted_count})
        return JsonResponse({'success': True, 'deleted': result.deleted_count})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
