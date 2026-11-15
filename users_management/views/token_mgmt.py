"""Active Sessions, Password Reset Tokens & Blacklisted Tokens management views."""
import csv
from datetime import datetime, timedelta
import json
import re

from bson import ObjectId
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render

from users_management.decorators import require_permission
from users_management.models import AuditLog
from users_management.services import (
    build_pagination_context,
    cached_swr,
    clean_value,
    format_device_display,
    invalidate_swr,
    mongo_admin,
    parallel_run,
)
from users_management.views_legacy import check_admin_access, check_api_status

__all__ = [
    'sessions_list',
    'password_resets_list',
    'blacklisted_tokens_list',
    'api_tokens_cleanup',
    'api_tokens_delete',
]


def _has_collection_perm(user, collection, action='delete'):
    if user.is_superuser:
        return True
    profile = getattr(user, 'admin_profile', None)
    if not profile:
        return False
    mod = 'sessions' if collection == 'sessions' else 'tokens'
    return profile.has_perm(f'{mod}.{action}')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('sessions.view')
def sessions_list(request):
    """Dedicated Active Login Sessions view backed by MongoDB db.sessions."""
    page = max(1, int(request.GET.get('page', 1) or 1))
    limit = min(100, max(10, int(request.GET.get('limit', 25) or 25)))
    q = (request.GET.get('q') or '').strip()
    status_filter = (request.GET.get('status') or '').strip()
    sort_by = (request.GET.get('sort') or 'lastActive_desc').strip()
    export_fmt = (request.GET.get('export') or '').strip().lower()

    sort_map = {
        'lastActive_desc': {'lastActive': -1, 'createdAt': -1},
        'createdAt_desc': {'createdAt': -1},
        'expiresAt_asc': {'expiresAt': 1},
    }
    sort_spec = sort_map.get(sort_by, sort_map['lastActive_desc'])

    try:
        db = mongo_admin.db()
        now = datetime.utcnow()

        base_match = {}
        if status_filter == 'active':
            base_match['$or'] = [{'expiresAt': {'$gt': now}}, {'expiresAt': None}, {'expiresAt': {'$exists': False}}]
        elif status_filter == 'expired':
            base_match['expiresAt'] = {'$lte': now}

        match_query = dict(base_match)
        if q:
            safe_q = re.escape(q)
            matched_user_ids = [
                u['_id']
                for u in db.users.find(
                    {'$or': [
                        {'name': {'$regex': safe_q, '$options': 'i'}},
                        {'email': {'$regex': safe_q, '$options': 'i'}},
                        {'username': {'$regex': safe_q, '$options': 'i'}},
                    ]},
                    {'_id': 1},
                ).limit(500)
            ]
            or_filters = [
                {'ip': {'$regex': safe_q, '$options': 'i'}},
                {'location': {'$regex': safe_q, '$options': 'i'}},
                {'device': {'$regex': safe_q, '$options': 'i'}},
            ]
            if matched_user_ids:
                or_filters.append({'userId': {'$in': matched_user_ids}})
            if ObjectId.is_valid(q):
                or_filters.append({'userId': ObjectId(q)})
            match_query = {'$and': [match_query, {'$or': or_filters}]} if match_query else {'$or': or_filters}

        user_lookup = {
            '$lookup': {
                'from': 'users',
                'let': {'uid': '$userId'},
                'pipeline': [
                    {'$match': {'$expr': {'$eq': ['$_id', '$$uid']}}},
                    {'$project': {'name': 1, 'email': 1, 'username': 1}},
                ],
                'as': 'user',
            }
        }
        base_pipeline = ([{'$match': match_query}] if match_query else []) + [{'$sort': sort_spec}]

        if export_fmt == 'csv':
            if not _has_collection_perm(request.user, 'sessions', 'export'):
                return redirect('admin_dashboard:sessions_list')
            export_pipeline = base_pipeline + [
                {'$limit': 2000},
                user_lookup,
                {'$unwind': {'path': '$user', 'preserveNullAndEmptyArrays': True}},
                {'$project': {'refreshToken': 0}},
            ]
            export_docs = clean_value(list(db.sessions.aggregate(export_pipeline)))
            resp = HttpResponse(content_type='text/csv; charset=utf-8')
            resp['Content-Disposition'] = f'attachment; filename="sessions_{now.strftime("%Y%m%d_%H%M%S")}.csv"'
            resp.write('\ufeff')
            writer = csv.writer(resp)
            writer.writerow(['Session ID', 'User Name', 'Email', 'Device', 'IP', 'Location', 'Last Active', 'Expires At'])
            for s in export_docs:
                u = s.get('user') if isinstance(s.get('user'), dict) else {}
                dev = format_device_display(s)
                writer.writerow([
                    s.get('_id', ''),
                    u.get('name') or u.get('username') or '',
                    u.get('email') or '',
                    f"{dev['primary']} {dev['secondary']}".strip(),
                    s.get('ip') or '',
                    s.get('location') or '',
                    s.get('lastActive') or s.get('createdAt') or '',
                    s.get('expiresAt') or '',
                ])
            AuditLog.log(request, 'export_sessions', 'sessions', 'csv', {'rows': len(export_docs)})
            return resp

        items_pipeline = base_pipeline + [
            {'$skip': (page - 1) * limit},
            {'$limit': limit},
            user_lookup,
            {'$unwind': {'path': '$user', 'preserveNullAndEmptyArrays': True}},
            {'$project': {'refreshToken': 0}},
        ]

        def _fetch_session_global_stats():
            kpi = parallel_run({
                'total_all': lambda: db.sessions.count_documents({}),
                'expired_all': lambda: db.sessions.count_documents({'expiresAt': {'$lte': datetime.utcnow()}}),
                'unique_users': lambda: len(db.sessions.distinct('userId')),
            }, max_workers=3)
            return kpi

        res = parallel_run({
            'total': lambda: db.sessions.count_documents(match_query),
            'items': lambda: clean_value(list(db.sessions.aggregate(items_pipeline))),
            'global_stats': lambda: cached_swr(
                'sessions_global_stats_v1',
                fresh_ttl=30,
                stale_ttl=120,
                fetcher=_fetch_session_global_stats,
                force=request.GET.get('refresh') == '1',
            ),
            'api_status': check_api_status,
        }, max_workers=4)

        total = res['total']
        sessions = res['items']
        gstats = res['global_stats']
        pag = build_pagination_context(page, total, limit, request.GET)

        for s in sessions:
            s['id'] = s.get('_id')
            user = s.get('user') if isinstance(s.get('user'), dict) else {}
            s['user_name'] = (
                user.get('name')
                or user.get('username')
                or (str(user.get('email')).split('@')[0] if user.get('email') else 'User')
            )
            s['user_email'] = user.get('email', '')
            s['user_id_str'] = str(s.get('userId') or '')
            dev = format_device_display(s)
            s['device_primary'] = dev['primary']
            s['device_secondary'] = dev['secondary']

        total_all = gstats.get('total_all', 0)
        expired_all = gstats.get('expired_all', 0)
        context = {
            'sessions': sessions,
            'total': total,
            'q': q,
            'status_filter': status_filter,
            'sort_by': sort_by,
            'stats': {
                'total_all': total_all,
                'active_all': max(0, total_all - expired_all),
                'expired_all': expired_all,
                'unique_users': gstats.get('unique_users', 0),
            },
            'api_status': res['api_status'],
            **pag,
        }
    except Exception as e:
        context = {
            'sessions': [], 'total': 0, 'q': q, 'status_filter': status_filter,
            'sort_by': sort_by,
            'stats': {'total_all': 0, 'active_all': 0, 'expired_all': 0, 'unique_users': 0},
            'page': 1, 'total_pages': 1, 'page_range': [1], 'preserved_params': '',
            'error': str(e), 'api_status': {'status': 'offline'},
        }
    return render(request, 'admin_dashboard/tokens/sessions_list.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('tokens.view')
def password_resets_list(request):
    """Password Reset Tokens view (db.passwordresettokens)."""
    page = max(1, int(request.GET.get('page', 1) or 1))
    q = (request.GET.get('q') or '').strip()
    limit = 20
    try:
        db = mongo_admin.db()
        query = {}
        if q and ObjectId.is_valid(q):
            oid = ObjectId(q)
            query = {'$or': [{'_id': oid}, {'userId': oid}]}

        res = parallel_run({
            'total': lambda: db.passwordresettokens.count_documents(query),
            'items': lambda: clean_value(list(
                db.passwordresettokens.find(query, {'tokenHash': 0})
                .sort('createdAt', -1)
                .skip((page - 1) * limit)
                .limit(limit)
            )),
            'total_sessions': lambda: db.sessions.count_documents({}),
        }, max_workers=3)

        total = res['total']
        pag = build_pagination_context(page, total, limit, request.GET)
        items = res['items']
        for item in items:
            item['id'] = item.get('_id')

        context = {
            'items': items,
            'total': total,
            'total_sessions': res['total_sessions'],
            'q': q,
            'api_status': check_api_status(),
            **pag,
        }
    except Exception as e:
        context = {
            'items': [], 'total_sessions': 0, 'q': q,
            'page': 1, 'total': 0, 'total_pages': 1, 'page_range': [1], 'preserved_params': '',
            'error': str(e), 'api_status': {'status': 'offline'},
        }
    return render(request, 'admin_dashboard/tokens/password_resets.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('tokens.view')
def blacklisted_tokens_list(request):
    page = max(1, int(request.GET.get('page', 1) or 1))
    limit = 20
    try:
        db = mongo_admin.db()
        res = parallel_run({
            'total': lambda: db.blacklistedtokens.count_documents({}),
            'items': lambda: clean_value(list(
                db.blacklistedtokens.find({}, {'token': 0})
                .sort('createdAt', -1)
                .skip((page - 1) * limit)
                .limit(limit)
            )),
        }, max_workers=2)
        total = res['total']
        pag = build_pagination_context(page, total, limit, request.GET)
        items = res['items']
        for item in items:
            item['id'] = item.get('_id')
        context = {
            'items': items, 'total': total,
            'api_status': check_api_status(),
            **pag,
        }
    except Exception as e:
        context = {
            'items': [], 'page': 1, 'total': 0, 'total_pages': 1, 'page_range': [1],
            'error': str(e), 'api_status': {'status': 'offline'},
        }
    return render(request, 'admin_dashboard/tokens/blacklisted_tokens.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def api_tokens_cleanup(request):
    if request.method != 'POST':
        return JsonResponse({'success': False}, status=405)
    body = json.loads(request.body or '{}')
    collection = body.get('collection', '')
    if collection not in ('passwordresettokens', 'blacklistedtokens', 'sessions'):
        return JsonResponse({'success': False, 'error': 'Invalid collection'}, status=400)
    if not _has_collection_perm(request.user, collection, 'delete'):
        return JsonResponse({'success': False, 'error': 'Permission denied'}, status=403)
    try:
        db = mongo_admin.db()
        cutoff = datetime.utcnow() - timedelta(hours=24)
        if collection == 'passwordresettokens':
            result = db.passwordresettokens.delete_many({'expiresAt': {'$lt': cutoff}})
        elif collection == 'blacklistedtokens':
            result = db.blacklistedtokens.delete_many({'expiresAt': {'$lt': cutoff}})
        else:
            result = db.sessions.delete_many({'expiresAt': {'$lt': datetime.utcnow()}})
            invalidate_swr('admin_sidebar_counts', 'sessions_global_stats_v1')
        AuditLog.log(request, f'cleanup_{collection}', collection, '', {'deleted': result.deleted_count})
        return JsonResponse({'success': True, 'deleted': result.deleted_count})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def api_tokens_delete(request):
    if request.method != 'POST':
        return JsonResponse({'success': False}, status=405)
    body = json.loads(request.body or '{}')
    collection = body.get('collection', '')
    ids = body.get('ids', [])
    user_id = str(body.get('user_id') or '').strip()
    if collection not in ('passwordresettokens', 'blacklistedtokens', 'sessions'):
        return JsonResponse({'success': False, 'error': 'Invalid collection'}, status=400)
    if not _has_collection_perm(request.user, collection, 'delete'):
        return JsonResponse({'success': False, 'error': 'Permission denied'}, status=403)
    try:
        db = mongo_admin.db()
        coll = db[collection]
        if collection == 'sessions' and user_id:
            if not ObjectId.is_valid(user_id):
                return JsonResponse({'success': False, 'error': 'Invalid user ID'})
            result = coll.delete_many({'userId': ObjectId(user_id)})
            invalidate_swr('admin_sidebar_counts', 'sessions_global_stats_v1')
            AuditLog.log(request, 'revoke_all_user_sessions', 'sessions', user_id, {'deleted': result.deleted_count})
            return JsonResponse({'success': True, 'deleted': result.deleted_count})
        oids = [ObjectId(i) for i in ids if ObjectId.is_valid(i)]
        result = coll.delete_many({'_id': {'$in': oids}})
        if collection == 'sessions':
            invalidate_swr('admin_sidebar_counts', 'sessions_global_stats_v1')
        AuditLog.log(request, f'delete_{collection}', collection, '', {'count': result.deleted_count})
        return JsonResponse({'success': True, 'deleted': result.deleted_count})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
