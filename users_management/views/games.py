"""Game management views for Voxel Feast Brawl 3D Arena (`game-3D-realtime`) + MongoDB profiles/histories."""
import json
import re
from datetime import datetime
import requests
from bson import ObjectId
from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render

from users_management.decorators import require_permission
from users_management.models import AuditLog
from users_management.services import (
    mongo_admin, arena_health, clean_value, parallel_run, cached_swr,
    build_pagination_context, invalidate_swr,
)
from users_management.views_legacy import check_admin_access, check_api_status

_USER_PUBLIC_PROJECT = {
    'name': 1,
    'username': 1,
    'displayName': 1,
    'email': 1,
    'avatar': 1,
    'profilePicture': 1,
    'originalAvatar': 1,
}

# Exact achievement/title catalog mirrored from game-en-website/backend/services/achievements.js
_TITLE_LABELS = {
    'ai_first_win': 'Training Initiate',
    'ai_easy_5': 'AI Apprentice',
    'ai_easy_15': 'Easy Bot Specialist',
    'ai_easy_30': 'Easy Bot Overlord',
    'ai_medium_3': 'Tactical Learner',
    'ai_medium_10': 'Skynet Hunter',
    'ai_medium_25': 'Medium AI Tamer',
    'ai_hard_1': 'Hellfire Trial',
    'ai_hard_5': 'Algorithm Master',
    'ai_hard_20': 'Superintelligence',
    'ai_all_rounder_3': 'Triple Threat Conqueror',
    'ai_total_50': 'Machine Destroyer',
    'pvp_first_match': 'Arena Debut',
    'pvp_first_win': 'First Blood',
    'pvp_wins_5': 'Rising Contender',
    'pvp_wins_10': 'PvP Warrior',
    'pvp_wins_25': 'Elite Gladiator',
    'pvp_wins_50': 'Arena Warlord',
    'pvp_wins_100': 'Supreme Conqueror',
    'rank_1050': 'Ranked Climber',
    'rank_silver': 'Silver Challenger',
    'rank_1200': 'Silver Veteran',
    'rank_gold': 'Rank Master',
    'rank_1400': 'Golden Vanguard',
    'rank_platinum': 'Platinum Legend',
    'rank_diamond': 'Diamond Superstar',
    'rank_master': 'Grandmaster Sovereign',
    'codebreaker_5': 'Codebreaker',
    'pro_guesser': 'Pro Guesser',
    'sniper_guesser': 'Code Sniper',
    'mind_reader_2': 'Mind Reader',
    'fast_solver_5_times': 'Master Lockpicker',
    'quick_reflex_90s': 'Quick Reflexes',
    'speed_demon': 'Speed Demon',
    'lightning_35s': 'Lightning Strike',
    'iron_will_8_guesses': 'Iron Will',
    'patience_master_180s': 'Grandmaster Patience',
    'rps_king_5': 'RPS Champion',
    'comeback_second_turn_5': 'Second-Turn Comeback',
    'streak_2': 'Double Strike',
    'streak_3': 'Unstoppable',
    'streak_5': 'Rampage Streak',
    'streak_7': 'Godlike Streak',
    'streak_10': 'Immortal Streak',
    'level_2': 'Level 2 Rookie',
    'veteran_level_5': 'Arena Veteran',
    'level_10': 'Battle Hardened',
    'level_15': 'Living Monument',
    'xp_200': 'XP Collector',
    'xp_1000': 'XP Hoarder',
    'total_matches_50': 'Dedicated Competitor',
}

__all__ = [
    'game_profiles_list',
    'game_histories_list',
    'api_game_profile_update',
    'api_game_profile_delete',
    'api_game_history_delete',
    'api_arena_control',
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
@require_permission('games.view')
def game_profiles_list(request):
    """Management page for player game profiles (gameprofiles)."""
    page = max(1, int(request.GET.get('page', 1) or 1))
    limit = max(1, min(100, int(request.GET.get('limit', 20) or 20)))
    search = request.GET.get('search', '').strip()
    force = request.GET.get('refresh') == '1'

    arena_data = {}
    try:
        db = mongo_admin.db()
        user_lookup = {
            '$lookup': {
                'from': 'users',
                'let': {'uid': '$userId'},
                'pipeline': [
                    {'$match': {'$expr': {'$eq': ['$_id', '$$uid']}}},
                    {'$project': _USER_PUBLIC_PROJECT},
                ],
                'as': 'user',
            }
        }

        search_match = {}
        if search:
            safe_q = re.escape(search)
            matched_user_ids = [
                u['_id']
                for u in db.users.find(
                    {'$or': [
                        {'name': {'$regex': safe_q, '$options': 'i'}},
                        {'username': {'$regex': safe_q, '$options': 'i'}},
                        {'email': {'$regex': safe_q, '$options': 'i'}},
                    ]},
                    {'_id': 1},
                ).limit(500)
            ]
            or_filters = [{'playerName': {'$regex': safe_q, '$options': 'i'}}]
            if matched_user_ids:
                or_filters.append({'userId': {'$in': matched_user_ids}})
            if ObjectId.is_valid(search):
                or_filters.append({'userId': ObjectId(search)})
            else:
                or_filters.append({'userId': search})
            search_match = {'$or': or_filters}

        # Always paginate before joining users so at most `limit` docs trigger $lookup
        data_pipeline = (
            ([{'$match': search_match}] if search_match else [])
            + [
                {'$sort': {'rating': -1, 'updatedAt': -1}},
                {'$skip': (page - 1) * limit},
                {'$limit': limit},
                user_lookup,
                {'$unwind': {'path': '$user', 'preserveNullAndEmptyArrays': True}},
            ]
        )

        def _fetch_profile_stats():
            res = list(db.gameprofiles.aggregate([
                {'$group': {
                    '_id': None,
                    'total': {'$sum': 1},
                    'max_rating': {'$max': '$rating'},
                    'total_wins': {'$sum': '$wins'},
                    'total_xp': {'$sum': '$totalXp'},
                }}
            ]))
            s = res[0] if res else {}
            return {
                'total': s.get('total', 0) or 0,
                'max_rating': s.get('max_rating', 0) or 0,
                'total_wins': s.get('total_wins', 0) or 0,
                'total_xp': s.get('total_xp', 0) or 0,
            }

        tasks = {
            'data': lambda: list(db.gameprofiles.aggregate(data_pipeline)),
            'stats': lambda: cached_swr(
                'game_profiles_stats_v1',
                fresh_ttl=30,
                stale_ttl=120,
                fetcher=_fetch_profile_stats,
                force=force,
            ),
            'arena': lambda: arena_health(force=force),
            'api_status': check_api_status,
        }
        if search:
            tasks['total'] = lambda: db.gameprofiles.count_documents(search_match)

        results = parallel_run(tasks, max_workers=5)

        stats = results['stats']
        arena_data = results['arena']
        total = results['total'] if search else stats['total']
        raw_items = results['data']
        total_pages = max(1, -(-total // limit))

        items = clean_value(raw_items)
        for item in items:
            item['id'] = item.get('_id')
            user = item.get('user')
            if isinstance(user, dict):
                d_name = user.get('name') or user.get('username') or user.get('displayName') or item.get('playerName')
                if not d_name and user.get('email'):
                    d_name = str(user.get('email')).split('@')[0]
                item['display_name'] = d_name or 'Người dùng'
                item['user_email'] = user.get('email', '')
                item['user_avatar'] = user.get('avatar') or user.get('profilePicture') or user.get('originalAvatar')
            else:
                item['display_name'] = item.get('playerName') or 'Chiến Binh 3D'
                item['user_email'] = str(item.get('userId') or item.get('skin') or 'arena.3d')
                item['user_avatar'] = None

            ai_stats = item.get('aiStats') if isinstance(item.get('aiStats'), dict) else {}
            item['ai_wins'] = sum(int((ai_stats.get(d) or {}).get('wins') or 0) for d in ('easy', 'medium', 'hard'))
            item['ai_losses'] = sum(int((ai_stats.get(d) or {}).get('losses') or 0) for d in ('easy', 'medium', 'hard'))
            unlocked = item.get('unlockedAchievementIds')
            item['unlocked_count'] = len(unlocked) if isinstance(unlocked, list) else 0
            title_id = item.get('equippedTitleId') or ''
            item['equipped_title_label'] = _TITLE_LABELS.get(title_id, title_id)
            item['edit_json'] = json.dumps({
                'id': item['id'],
                'displayName': item['display_name'],
                'rating': int(item.get('rating') or 1000),
                'highestRating': int(item.get('highestRating') or item.get('rating') or 1000),
                'totalXp': int(item.get('totalXp') or 0),
                'wins': int(item.get('wins') or 0),
                'losses': int(item.get('losses') or 0),
                'currentWinStreak': int(item.get('currentWinStreak') or 0),
                'bestWinStreak': int(item.get('bestWinStreak') or 0),
                'dailyAiXpEarned': int(item.get('dailyAiXpEarned') or 0),
                'equippedTitleId': title_id,
            }, ensure_ascii=False)
            item['formatted_date'] = format_game_dt(item.get('updatedAt') or item.get('createdAt'))

        pag = build_pagination_context(page, total, limit, request.GET)
        context = {
            'items': items,
            'stats': stats,
            'arena': arena_data,
            'arena_rooms': arena_data.get('rooms', []) if isinstance(arena_data, dict) else [],
            'title_choices': list(_TITLE_LABELS.items()),
            'game_server_url': getattr(settings, 'GAME_SERVER_URL', 'http://localhost:3006'),
            'game_client_url': getattr(settings, 'GAME_CLIENT_URL', 'http://localhost:5173'),
            'limit': limit,
            'total': total,
            'search': search,
            'api_status': results['api_status'],
            **pag,
        }
    except Exception as e:
        arena_data = arena_data or arena_health()
        context = {
            'items': [], 'stats': {}, 'page': 1, 'total': 0, 'total_pages': 1,
            'arena': arena_data,
            'arena_rooms': arena_data.get('rooms', []) if isinstance(arena_data, dict) else [],
            'game_server_url': getattr(settings, 'GAME_SERVER_URL', 'http://localhost:3006'),
            'game_client_url': getattr(settings, 'GAME_CLIENT_URL', 'http://localhost:5173'),
            'error': f'Error: {e}', 'api_status': {'status': 'offline'},
            'search': search, 'limit': limit,
        }

    return render(request, 'admin_dashboard/games/game_profiles.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('games.view')
def game_histories_list(request):
    """Management page for game match histories (gamehistories)."""
    page = max(1, int(request.GET.get('page', 1) or 1))
    limit = max(1, min(100, int(request.GET.get('limit', 20) or 20)))
    search = request.GET.get('search', '').strip()
    mode_filter = request.GET.get('mode', '').strip()
    force = request.GET.get('refresh') == '1'

    arena_data = {}
    try:
        db = mongo_admin.db()
        match_query = {}
        if mode_filter == 'ai':
            match_query['isAiRoom'] = True
        elif mode_filter == 'pvp':
            match_query['isAiRoom'] = {'$ne': True}
        elif mode_filter == 'forfeited':
            match_query['endReason'] = 'forfeit'
        if search:
            or_filters = [
                {'roomId': {'$regex': search, '$options': 'i'}},
                {'winnerName': {'$regex': search, '$options': 'i'}},
                {'players.name': {'$regex': search, '$options': 'i'}},
            ]
            if ObjectId.is_valid(search):
                or_filters.append({'winnerId': ObjectId(search)})
            match_query['$or'] = or_filters

        history_project = {
            'roomId': 1,
            'roomTitle': 1,
            'winnerId': 1,
            'winnerName': 1,
            'winnerMass': 1,
            'winnerKills': 1,
            'playersCount': 1,
            'humanCount': 1,
            'botCount': 1,
            'totalGuesses': 1,
            'winnerGuessCount': 1,
            'loserGuessCount': 1,
            'duration': 1,
            'isAiRoom': 1,
            'aiDifficulty': 1,
            'status': 1,
            'endReason': 1,
            'xpEligible': 1,
            'xpEligibilityReason': 1,
            'ratingReason': 1,
            'players': 1,
            'finishedAt': 1,
            'createdAt': 1,
        }
        tail_stages = [
            {'$sort': {'createdAt': -1, 'finishedAt': -1}},
            {'$skip': (page - 1) * limit},
            {'$limit': limit},
            {'$project': history_project},
            {
                '$lookup': {
                    'from': 'users',
                    'let': {'wid': '$winnerId'},
                    'pipeline': [
                        {'$match': {'$expr': {'$eq': ['$_id', '$$wid']}}},
                        {'$project': _USER_PUBLIC_PROJECT},
                    ],
                    'as': 'winner',
                }
            },
            {'$unwind': {'path': '$winner', 'preserveNullAndEmptyArrays': True}},
        ]

        data_pipeline = []
        if match_query:
            data_pipeline.append({'$match': match_query})
            data_pipeline.append({
                '$facet': {
                    'meta': [{'$count': 'total'}],
                    'items': tail_stages,
                }
            })
        else:
            data_pipeline.extend(tail_stages)

        def _fetch_history_stats():
            res = list(db.gamehistories.aggregate([
                {
                    '$group': {
                        '_id': None,
                        'total': {'$sum': 1},
                        'avg_guesses': {'$avg': '$totalGuesses'},
                        'avg_duration': {'$avg': '$duration'},
                    }
                }
            ]))
            s = res[0] if res else {}
            return {
                'total': s.get('total', 0) or 0,
                'avg_guesses': round(s.get('avg_guesses', 0) or 0, 1),
                'avg_duration': round(s.get('avg_duration', 0) or 0, 1),
            }

        results = parallel_run({
            'data': lambda: list(db.gamehistories.aggregate(data_pipeline)),
            'stats': lambda: cached_swr(
                'game_histories_stats_v1',
                fresh_ttl=30,
                stale_ttl=120,
                fetcher=_fetch_history_stats,
                force=force,
            ),
            'arena': lambda: arena_health(force=force),
            'api_status': check_api_status,
        }, max_workers=4)

        stats = results['stats']
        arena_data = results['arena']
        if match_query:
            facet_doc = results['data'][0] if results['data'] else {}
            meta_list = facet_doc.get('meta', [])
            total = meta_list[0]['total'] if meta_list else 0
            raw_items = facet_doc.get('items', [])
        else:
            total = stats['total']
            raw_items = results['data']
        total_pages = max(1, -(-total // limit))

        items = clean_value(raw_items)
        for item in items:
            item['id'] = item.get('_id')
            winner = item.get('winner')
            if isinstance(winner, dict):
                w_name = winner.get('name') or winner.get('username') or winner.get('displayName') or item.get('winnerName')
                if not w_name and winner.get('email'):
                    w_name = str(winner.get('email')).split('@')[0]
                item['winner_name'] = w_name or 'Người chơi'
                item['winner_avatar'] = winner.get('avatar') or winner.get('profilePicture') or winner.get('originalAvatar')
            else:
                item['winner_name'] = item.get('winnerName') or 'Chiến Binh 3D'
                item['winner_avatar'] = None

            item['detail_json'] = json.dumps({
                'roomId': item.get('roomId'),
                'status': item.get('status') or 'completed',
                'isAiRoom': bool(item.get('isAiRoom')),
                'aiDifficulty': item.get('aiDifficulty'),
                'endReason': item.get('endReason'),
                'ratingReason': item.get('ratingReason'),
                'xpEligible': item.get('xpEligible', True),
                'xpEligibilityReason': item.get('xpEligibilityReason'),
                'duration': item.get('duration') or 0,
                'players': item.get('players') or [],
            }, ensure_ascii=False)
            item['formatted_date'] = format_game_dt(item.get('finishedAt') or item.get('createdAt'))

        pag = build_pagination_context(page, total, limit, request.GET)
        context = {
            'items': items,
            'stats': stats,
            'arena': arena_data,
            'arena_rooms': arena_data.get('rooms', []) if isinstance(arena_data, dict) else [],
            'game_server_url': getattr(settings, 'GAME_SERVER_URL', 'http://localhost:3006'),
            'game_client_url': getattr(settings, 'GAME_CLIENT_URL', 'http://localhost:5173'),
            'limit': limit,
            'total': total,
            'search': search,
            'mode_filter': mode_filter,
            'api_status': results['api_status'],
            **pag,
        }
    except Exception as e:
        arena_data = arena_data or arena_health()
        context = {
            'items': [], 'stats': {}, 'page': 1, 'total': 0, 'total_pages': 1, 'page_range': [1],
            'arena': arena_data,
            'arena_rooms': arena_data.get('rooms', []) if isinstance(arena_data, dict) else [],
            'game_server_url': getattr(settings, 'GAME_SERVER_URL', 'http://localhost:3006'),
            'game_client_url': getattr(settings, 'GAME_CLIENT_URL', 'http://localhost:5173'),
            'error': f'Error: {e}', 'api_status': {'status': 'offline'},
            'search': search, 'mode_filter': mode_filter, 'limit': limit,
        }

    return render(request, 'admin_dashboard/games/game_histories.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('games.edit')
def api_game_profile_update(request):
    """Update numeric progression stats & equipped title on a gameprofile."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)
    try:
        body = json.loads(request.body)
        pid = str(body.get('id') or '').strip()
        if not ObjectId.is_valid(pid):
            return JsonResponse({'success': False, 'error': 'Invalid gameprofile ID'}, status=400)

        int_fields = (
            'rating', 'highestRating', 'totalXp', 'wins', 'losses',
            'currentWinStreak', 'bestWinStreak', 'dailyAiXpEarned',
        )
        updates = {'updatedAt': datetime.utcnow()}
        for field in int_fields:
            if field in body and body[field] is not None and str(body[field]).strip() != '':
                val = int(body[field])
                if val < 0:
                    return JsonResponse({'success': False, 'error': f'{field} cannot be negative'}, status=400)
                updates[field] = val

        if 'rating' in updates and 'highestRating' in updates:
            updates['highestRating'] = max(updates['rating'], updates['highestRating'])

        if 'equippedTitleId' in body:
            title_id = str(body.get('equippedTitleId') or '').strip()
            updates['equippedTitleId'] = title_id or None

        db = mongo_admin.db()
        res = db.gameprofiles.update_one({'_id': ObjectId(pid)}, {'$set': updates})
        invalidate_swr('game_profiles_stats_v1')
        AuditLog.log(request, 'update_game_profile', 'gameprofile', pid, clean_value(updates))
        return JsonResponse({'success': True, 'modified': res.modified_count})
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Invalid numeric input'}, status=400)
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=500)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('games.delete')
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
        invalidate_swr('game_profiles_stats_v1', 'admin_sidebar_counts')
        AuditLog.log(request, 'bulk_delete_game_profiles', 'gameprofile', '', {'count': result.deleted_count})
        return JsonResponse({'success': True, 'deleted': result.deleted_count})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('games.delete')
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
        invalidate_swr('game_histories_stats_v1')
        AuditLog.log(request, 'bulk_delete_game_histories', 'gamehistory', '', {'count': result.deleted_count})
        return JsonResponse({'success': True, 'deleted': result.deleted_count})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('games.view')
def api_arena_control(request):
    """Live control & telemetry bridge to game-3D-realtime (Voxel Feast Brawl 3D)."""
    game_url = getattr(settings, 'GAME_SERVER_URL', 'http://localhost:3006')
    if request.method == 'GET':
        return JsonResponse(arena_health())

    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'Method not allowed'}, status=405)

    profile = getattr(request.user, 'admin_profile', None)
    if not (request.user.is_superuser or (profile and profile.has_perm('games.edit'))):
        return JsonResponse({'ok': False, 'error': 'Permission games.edit required'}, status=403)

    try:
        body = json.loads(request.body or '{}')
        action = body.get('action', '')

        if action == 'update_room':
            room_id = body.get('room_id', 'MEGA-1')
            payload = {
                'botsEnabled': bool(body.get('botsEnabled', True)),
                'targetBotPopulation': int(body.get('targetBotPopulation', 12)),
                'maxPlayers': int(body.get('maxPlayers', 26)),
            }
            if body.get('title'):
                payload['title'] = body['title']
            resp = requests.post(f'{game_url}/api/admin/rooms/{room_id}/config', json=payload, timeout=5)
            AuditLog.log(request, 'arena_update_room', 'arena_room', room_id, payload)
            return JsonResponse(resp.json(), status=resp.status_code)

        elif action == 'create_room':
            payload = {
                'id': body.get('id', ''),
                'title': body.get('title', 'Đấu Trường Sự Kiện 3D'),
                'isPublic': bool(body.get('isPublic', True)),
                'maxPlayers': int(body.get('maxPlayers', 24)),
                'botsEnabled': bool(body.get('botsEnabled', True)),
                'targetBotPopulation': int(body.get('targetBotPopulation', 10)),
            }
            resp = requests.post(f'{game_url}/api/admin/rooms', json=payload, timeout=5)
            AuditLog.log(request, 'arena_create_room', 'arena_room', payload.get('id', ''), payload)
            return JsonResponse(resp.json(), status=resp.status_code)

        elif action == 'delete_room':
            room_id = body.get('room_id', '')
            resp = requests.delete(f'{game_url}/api/admin/rooms/{room_id}', timeout=5)
            AuditLog.log(request, 'arena_delete_room', 'arena_room', room_id)
            return JsonResponse(resp.json(), status=resp.status_code)

        elif action == 'sync_snapshot':
            # Sync live 3D Arena leaderboard fighters into MongoDB gameprofiles & gamehistories
            overview = arena_health()
            if not overview.get('ok'):
                return JsonResponse({'ok': False, 'error': '3D Arena server is offline'}, status=400)
            db = mongo_admin.db()
            now = datetime.utcnow()
            synced_profiles = 0
            synced_matches = 0
            for room in overview.get('rooms', []):
                lb = room.get('leaderboard', [])
                if lb:
                    top = lb[0]
                    db.gamehistories.insert_one({
                        'roomId': room.get('id', 'MEGA-1'),
                        'roomTitle': room.get('title', 'Voxel Feast Brawl 3D'),
                        'gameMode': 'Feast Brawl 3D',
                        'winnerName': top.get('name', 'Fighter'),
                        'winnerMass': top.get('mass', 0),
                        'winnerKills': top.get('kills', 0),
                        'playersCount': room.get('totalFighters', len(lb)),
                        'humansCount': room.get('humans', 0),
                        'botsCount': room.get('bots', 0),
                        'totalGuesses': top.get('kills', 0),
                        'duration': overview.get('uptimeSec', 180),
                        'createdAt': now,
                        'finishedAt': now,
                    })
                    synced_matches += 1
                for fighter in lb:
                    fname = fighter.get('name', 'Fighter')
                    mass = int(fighter.get('mass', 0))
                    kills = int(fighter.get('kills', 0))
                    rating_est = 1000 + mass * 2 + kills * 35
                    db.gameprofiles.update_one(
                        {'playerName': fname},
                        {
                            '$set': {
                                'playerName': fname,
                                'user': {'name': fname, 'email': f"{ fighter.get('skin', 'voxel') }@arena.3d" if fighter.get('isBot') else 'human@arena.3d'},
                                'skin': fighter.get('skin', 'voxel'),
                                'tierName': fighter.get('tierName', 'Tân Binh'),
                                'isBot': bool(fighter.get('isBot')),
                                'updatedAt': now,
                            },
                            '$max': {
                                'rating': rating_est,
                                'highestRating': rating_est,
                                'bestMass': int(fighter.get('bestMass') or mass),
                                'bestWinStreak': kills,
                            },
                            '$inc': {
                                'wins': 1 if kills > 0 else 0,
                                'totalXp': mass + kills * 25,
                                'currentWinStreak': 1 if kills > 0 else 0,
                            },
                            '$setOnInsert': {
                                'losses': 0,
                                'createdAt': now,
                            },
                        },
                        upsert=True,
                    )
                    synced_profiles += 1
            AuditLog.log(request, 'arena_sync_snapshot', 'arena', '', {'profiles': synced_profiles, 'matches': synced_matches})
            return JsonResponse({'ok': True, 'synced_profiles': synced_profiles, 'synced_matches': synced_matches})

        return JsonResponse({'ok': False, 'error': 'Unknown arena action'}, status=400)
    except Exception as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)
