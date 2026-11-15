"""Daily Challenge & Submissions management views (`dailychallenges`, `dailysubmissions`)."""
import json
import random
from datetime import datetime, timedelta

from bson import ObjectId
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render

from users_management.decorators import require_permission
from users_management.models import AuditLog
from users_management.services import (
    mongo_admin, clean_value, parallel_run, cached_swr,
    build_pagination_context, invalidate_swr,
)
from users_management.views_legacy import check_admin_access, check_api_status

__all__ = [
    'daily_challenge_list',
    'api_daily_challenge_action',
]


def _vn_today_str():
    """Return current YYYY-MM-DD in UTC+7 matching game-en-website DailyChallenge key."""
    return (datetime.utcnow() + timedelta(hours=7)).strftime('%Y-%m-%d')


def validate_daily_secret(secret):
    """Validate Daily Challenge secret: exactly 4 unique ASCII digits."""
    s = str(secret or '').strip()
    if len(s) != 4 or not s.isdigit() or len(set(s)) != 4:
        return False, 'Secret must be exactly 4 unique digits (e.g. 4829).'
    return True, None


def _invalidate_daily_cache():
    invalidate_swr('daily_challenge_stats_v1', 'admin_sidebar_counts')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('daily_challenge.view')
def daily_challenge_list(request):
    """Manage Daily Number Challenges and inspect/reset player submissions."""
    page = max(1, int(request.GET.get('page', 1) or 1))
    limit = max(1, min(100, int(request.GET.get('limit', 20) or 20)))
    date_filter = request.GET.get('date', '').strip()
    status_filter = request.GET.get('status', '').strip()
    search = request.GET.get('search', '').strip()
    force = request.GET.get('refresh') == '1'
    today_vn = _vn_today_str()

    try:
        db = mongo_admin.db()
        sub_query = {}
        if date_filter:
            sub_query['date'] = date_filter
        if status_filter == 'solved':
            sub_query['isSolved'] = True
        elif status_filter == 'failed':
            sub_query['isSolved'] = False
            sub_query['isFinished'] = True
        elif status_filter == 'active':
            sub_query['isFinished'] = {'$ne': True}
        if search:
            or_clause = [{'username': {'$regex': search, '$options': 'i'}}, {'date': {'$regex': search, '$options': 'i'}}]
            if ObjectId.is_valid(search):
                or_clause.append({'userId': ObjectId(search)})
            sub_query['$or'] = or_clause

        sub_tail = [
            {'$sort': {'updatedAt': -1, 'createdAt': -1}},
            {'$skip': (page - 1) * limit},
            {'$limit': limit},
        ]
        if sub_query:
            sub_pipeline = [
                {'$match': sub_query},
                {'$facet': {'meta': [{'$count': 'total'}], 'items': sub_tail}},
            ]
        else:
            sub_pipeline = sub_tail

        def _fetch_daily_stats():
            ch_agg = list(db.dailychallenges.aggregate([
                {'$group': {
                    '_id': None,
                    'total_challenges': {'$sum': 1},
                    'sum_participants': {'$sum': '$totalParticipants'},
                    'sum_solved': {'$sum': '$totalSolved'},
                }}
            ]))
            sub_agg = list(db.dailysubmissions.aggregate([
                {'$group': {
                    '_id': None,
                    'total_submissions': {'$sum': 1},
                    'solved_count': {'$sum': {'$cond': [{'$eq': ['$isSolved', True]}, 1, 0]}},
                    'avg_guesses': {'$avg': '$guessCount'},
                }}
            ]))
            today_doc = db.dailychallenges.find_one({'date': today_vn}, {'_id': 1, 'totalParticipants': 1, 'totalSolved': 1})
            c = ch_agg[0] if ch_agg else {}
            s = sub_agg[0] if sub_agg else {}
            tot_sub = s.get('total_submissions', 0) or 0
            solved_cnt = s.get('solved_count', 0) or 0
            return {
                'total_challenges': c.get('total_challenges', 0) or 0,
                'total_submissions': tot_sub,
                'solved_count': solved_cnt,
                'solve_rate': round((solved_cnt / tot_sub) * 100, 1) if tot_sub > 0 else 0,
                'avg_guesses': round(s.get('avg_guesses', 0) or 0, 2),
                'today_date': today_vn,
                'today_configured': bool(today_doc),
            }

        results = parallel_run({
            'challenges': lambda: clean_value(list(db.dailychallenges.find({}).sort('date', -1).limit(60))),
            'submissions': lambda: list(db.dailysubmissions.aggregate(sub_pipeline)),
            'stats': lambda: cached_swr(
                'daily_challenge_stats_v1',
                fresh_ttl=30,
                stale_ttl=120,
                fetcher=_fetch_daily_stats,
                force=force,
            ),
        }, max_workers=3)

        challenges = results['challenges']
        for ch in challenges:
            ch['id'] = ch.get('_id')
            parts = ch.get('totalParticipants') or 0
            solved = ch.get('totalSolved') or 0
            ch['solve_rate'] = round((solved / parts) * 100, 1) if parts > 0 else 0

        stats = results['stats']
        if sub_query:
            facet = results['submissions'][0] if results['submissions'] else {}
            meta = facet.get('meta', [])
            total = meta[0]['total'] if meta else 0
            raw_subs = facet.get('items', [])
        else:
            total = stats['total_submissions']
            raw_subs = results['submissions']

        submissions = clean_value(raw_subs)
        for sub in submissions:
            sub['id'] = sub.get('_id')
            sub['user_id_str'] = str(sub.get('userId') or '')
            sub['guesses_json'] = json.dumps(sub.get('guesses') or [], ensure_ascii=False)

        pag = build_pagination_context(page, total, limit, request.GET)
        available_dates = [ch.get('date') for ch in challenges if ch.get('date')]

        context = {
            'challenges': challenges,
            'submissions': submissions,
            'available_dates': available_dates,
            'stats': stats,
            'total': total,
            'limit': limit,
            'date_filter': date_filter,
            'status_filter': status_filter,
            'search': search,
            'today_vn': today_vn,
            'api_status': check_api_status(),
            **pag,
        }
    except Exception as exc:
        context = {
            'challenges': [], 'submissions': [], 'available_dates': [],
            'stats': {}, 'total': 0, 'page': 1, 'total_pages': 1, 'page_range': [1],
            'date_filter': date_filter, 'status_filter': status_filter, 'search': search,
            'today_vn': today_vn, 'error': str(exc), 'api_status': {'status': 'offline'},
        }
    return render(request, 'admin_dashboard/games/daily_challenge.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('daily_challenge.edit')
def api_daily_challenge_action(request):
    """Create/update daily challenge secret or delete/reset submissions & challenges."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)

    try:
        body = json.loads(request.body)
        action = body.get('action', '')
        if action in ('delete_challenge', 'delete_submission'):
            profile = getattr(request.user, 'admin_profile', None)
            if not (request.user.is_superuser or (profile and profile.has_perm('daily_challenge.delete'))):
                return JsonResponse({'success': False, 'error': 'Permission daily_challenge.delete required'}, status=403)
        db = mongo_admin.db()
        now = datetime.utcnow()

        if action == 'upsert_challenge':
            date_str = str(body.get('date') or _vn_today_str()).strip()
            try:
                datetime.strptime(date_str, '%Y-%m-%d')
            except ValueError:
                return JsonResponse({'success': False, 'error': 'Date must follow YYYY-MM-DD format.'}, status=400)

            secret = str(body.get('secret') or '').strip()
            if not secret and body.get('auto_generate'):
                digits = list('0123456789')
                random.shuffle(digits)
                secret = ''.join(digits[:4])

            valid, err = validate_daily_secret(secret)
            if not valid:
                return JsonResponse({'success': False, 'error': err}, status=400)

            db.dailychallenges.update_one(
                {'date': date_str},
                {
                    '$set': {'secret': secret, 'updatedAt': now},
                    '$setOnInsert': {
                        'date': date_str,
                        'totalParticipants': 0,
                        'totalSolved': 0,
                        'createdAt': now,
                    },
                },
                upsert=True,
            )
            _invalidate_daily_cache()
            AuditLog.log(request, 'upsert_daily_challenge', 'dailychallenge', date_str, {'date': date_str})
            return JsonResponse({'success': True, 'date': date_str, 'secret': secret})

        elif action == 'delete_challenge':
            cid = str(body.get('id') or '').strip()
            if not ObjectId.is_valid(cid):
                return JsonResponse({'success': False, 'error': 'Invalid challenge ID'}, status=400)
            res = db.dailychallenges.delete_one({'_id': ObjectId(cid)})
            _invalidate_daily_cache()
            AuditLog.log(request, 'delete_daily_challenge', 'dailychallenge', cid)
            return JsonResponse({'success': True, 'deleted': res.deleted_count})

        elif action == 'delete_submission':
            ids = body.get('ids') or ([body.get('id')] if body.get('id') else [])
            oids = [ObjectId(i) for i in ids if ObjectId.is_valid(str(i))]
            if not oids:
                return JsonResponse({'success': False, 'error': 'No valid submission IDs provided'}, status=400)

            docs = list(db.dailysubmissions.find({'_id': {'$in': oids}}, {'date': 1, 'isSolved': 1}))
            res = db.dailysubmissions.delete_many({'_id': {'$in': oids}})

            # Keep challenge participant/solved counters consistent without going negative
            decrements = {}
            for doc in docs:
                dkey = doc.get('date')
                if not dkey:
                    continue
                entry = decrements.setdefault(dkey, {'parts': 0, 'solved': 0})
                entry['parts'] += 1
                if doc.get('isSolved'):
                    entry['solved'] += 1

            for dkey, counts in decrements.items():
                ch = db.dailychallenges.find_one({'date': dkey}, {'totalParticipants': 1, 'totalSolved': 1})
                if ch:
                    new_parts = max(0, int(ch.get('totalParticipants') or 0) - counts['parts'])
                    new_solved = max(0, int(ch.get('totalSolved') or 0) - counts['solved'])
                    db.dailychallenges.update_one(
                        {'_id': ch['_id']},
                        {'$set': {'totalParticipants': new_parts, 'totalSolved': new_solved, 'updatedAt': now}},
                    )

            _invalidate_daily_cache()
            AuditLog.log(request, 'delete_daily_submissions', 'dailysubmission', '', {'count': res.deleted_count})
            return JsonResponse({'success': True, 'deleted': res.deleted_count})

        return JsonResponse({'success': False, 'error': 'Unknown action'}, status=400)
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=500)

