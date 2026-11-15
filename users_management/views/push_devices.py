"""Expo Push notification dispatch (with auto-prune) and Push Devices management views."""
import json
import re

import requests
from bson import ObjectId
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST
from pymongo import UpdateOne

from users_management.decorators import require_permission
from users_management.models import AuditLog
from users_management.services import (
    build_pagination_context,
    cached_swr,
    clean_value,
    invalidate_swr,
    mongo_admin,
    parallel_run,
)
from users_management.views_legacy import check_admin_access

__all__ = [
    'send_expo_push_batch',
    'push_devices_list',
    'api_push_devices_test',
    'api_push_devices_remove',
]

EXPO_PUSH_URL = 'https://exp.host/--/api/v2/push/send'
_EXPO_TOKEN_RE = re.compile(r'^(ExponentPushToken|ExpoPushToken)\[[^\]]+\]$')


def _prune_dead_push_tokens(dead_tokens):
    """Remove dead/unregistered Expo push tokens from all MongoDB user documents."""
    cleaned = [t for t in dict.fromkeys(dead_tokens or []) if isinstance(t, str) and t]
    if not cleaned:
        return 0
    db = mongo_admin.db()
    res = db.users.update_many(
        {'pushTokens': {'$in': cleaned}},
        {'$pull': {'pushTokens': {'$in': cleaned}}},
    )
    invalidate_swr('push_devices_stats_v1', 'notifications_stats_v1', 'admin_sidebar_counts')
    return int(res.modified_count or 0)


def send_expo_push_batch(tokens, title, body, data=None, noti_type='version_updated'):
    """Dispatch Expo Push notifications in chunks of 100 and auto-prune DeviceNotRegistered tokens.

    Returns a dict: {'attempted', 'sent', 'pruned', 'pruned_tokens', 'users_updated', 'error'}.
    """
    valid_tokens = list(dict.fromkeys(
        t.strip() for t in (tokens or [])
        if isinstance(t, str) and t.strip().startswith(('ExponentPushToken', 'ExpoPushToken'))
    ))
    if not valid_tokens:
        return {
            'attempted': 0,
            'sent': 0,
            'pruned': 0,
            'pruned_tokens': [],
            'users_updated': 0,
            'error': None,
        }

    headers = {
        'Accept': 'application/json',
        'Accept-Encoding': 'gzip, deflate',
        'Content-Type': 'application/json',
    }
    payload_data = dict(data) if isinstance(data, dict) else {'type': noti_type, 'source': 'admin_broadcast'}

    sent = 0
    dead_tokens = []
    errors = []

    for idx in range(0, len(valid_tokens), 100):
        batch_tokens = valid_tokens[idx:idx + 100]
        chunk = [
            {
                'to': token,
                'sound': 'default',
                'title': title,
                'body': body,
                'data': payload_data,
            }
            for token in batch_tokens
        ]
        try:
            resp = requests.post(
                EXPO_PUSH_URL,
                json=chunk,
                headers=headers,
                timeout=(3, 8),
            )
            if not resp.ok:
                errors.append(f'HTTP {resp.status_code}')
                continue

            try:
                body_json = resp.json()
            except ValueError:
                sent += len(chunk)
                continue

            tickets = body_json.get('data') if isinstance(body_json, dict) else None
            if not isinstance(tickets, list):
                sent += len(chunk)
                continue

            for pos, ticket in enumerate(tickets):
                tok = batch_tokens[pos] if pos < len(batch_tokens) else ''
                if not isinstance(ticket, dict):
                    continue
                if ticket.get('status') == 'ok':
                    sent += 1
                else:
                    details = ticket.get('details') if isinstance(ticket.get('details'), dict) else {}
                    err_code = details.get('error') or ticket.get('message') or 'PushError'
                    if err_code == 'DeviceNotRegistered' and tok:
                        dead_tokens.append(tok)
                    else:
                        errors.append(str(err_code)[:60])
        except requests.RequestException as exc:
            errors.append(str(exc)[:100])

    dead_tokens = list(dict.fromkeys(dead_tokens))
    users_updated = _prune_dead_push_tokens(dead_tokens) if dead_tokens else 0

    return {
        'attempted': len(valid_tokens),
        'sent': sent,
        'pruned': len(dead_tokens),
        'pruned_tokens': dead_tokens,
        'users_updated': users_updated,
        'error': '; '.join(dict.fromkeys(errors)) if errors else None,
    }


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('push_devices.view')
def push_devices_list(request):
    """Dedicated view listing every user Expo Push token in MongoDB db.users.pushTokens."""
    search = (request.GET.get('search') or '').strip()
    format_filter = (request.GET.get('format') or '').strip()
    user_status = (request.GET.get('user_status') or '').strip()
    page = max(1, int(request.GET.get('page', 1) or 1))
    limit = min(100, max(10, int(request.GET.get('limit', 25) or 25)))

    try:
        db = mongo_admin.db()
        user_match = {'pushTokens.0': {'$exists': True}}
        if user_status == 'locked':
            user_match['isLocked'] = True
        elif user_status == 'active':
            user_match['isLocked'] = {'$ne': True}

        pipeline = [
            {'$match': user_match},
            {'$project': {
                '_id': 1,
                'name': 1,
                'email': 1,
                'isLocked': 1,
                'updatedAt': 1,
                'pushTokens': 1,
            }},
            {'$unwind': {'path': '$pushTokens', 'includeArrayIndex': 'token_index'}},
            {'$project': {
                'user_id': {'$toString': '$_id'},
                'user_name': {'$ifNull': ['$name', 'Unknown']},
                'user_email': {'$ifNull': ['$email', '']},
                'is_locked': {'$ifNull': ['$isLocked', False]},
                'updatedAt': '$updatedAt',
                'token': '$pushTokens',
                'token_index': '$token_index',
                'is_valid_format': {
                    '$regexMatch': {
                        'input': {'$ifNull': ['$pushTokens', '']},
                        'regex': r'^ExponentPushToken\[.+\]$|^ExpoPushToken\[.+\]$',
                    }
                },
            }},
        ]

        post_match = {}
        if search:
            post_match['$or'] = [
                {'user_name': {'$regex': re.escape(search), '$options': 'i'}},
                {'user_email': {'$regex': re.escape(search), '$options': 'i'}},
                {'token': {'$regex': re.escape(search), '$options': 'i'}},
                {'user_id': {'$regex': re.escape(search), '$options': 'i'}},
            ]
        if format_filter == 'valid':
            post_match['is_valid_format'] = True
        elif format_filter == 'invalid':
            post_match['is_valid_format'] = False
        if post_match:
            pipeline.append({'$match': post_match})

        pipeline.append({'$sort': {'updatedAt': -1, 'user_email': 1, 'token_index': 1}})
        pipeline.append({
            '$facet': {
                'meta': [{'$count': 'total'}],
                'items': [{'$skip': (page - 1) * limit}, {'$limit': limit}],
            }
        })

        def _fetch_push_stats():
            stat_res = parallel_run({
                'kpi_agg': lambda: list(db.users.aggregate([
                    {'$match': {'pushTokens.0': {'$exists': True}}},
                    {'$project': {
                        'pushTokens': 1,
                        'isLocked': {'$ifNull': ['$isLocked', False]},
                    }},
                    {'$group': {
                        '_id': None,
                        'users_count': {'$sum': 1},
                        'tokens_count': {'$sum': {'$size': '$pushTokens'}},
                        'locked_users': {'$sum': {'$cond': ['$isLocked', 1, 0]}},
                    }},
                ])),
                'malformed_agg': lambda: list(db.users.aggregate([
                    {'$match': {'pushTokens.0': {'$exists': True}}},
                    {'$unwind': '$pushTokens'},
                    {'$match': {
                        'pushTokens': {'$not': {'$regex': r'^ExponentPushToken\[.+\]$|^ExpoPushToken\[.+\]$'}},
                    }},
                    {'$count': 'malformed'},
                ])),
            }, max_workers=2)
            kpi = stat_res['kpi_agg'][0] if stat_res['kpi_agg'] else {}
            malformed_agg = stat_res['malformed_agg']
            return {
                'users_count': kpi.get('users_count', 0) or 0,
                'tokens_count': kpi.get('tokens_count', 0) or 0,
                'locked_users': kpi.get('locked_users', 0) or 0,
                'malformed_count': malformed_agg[0].get('malformed', 0) if malformed_agg else 0,
            }

        results = parallel_run({
            'agg': lambda: list(db.users.aggregate(pipeline)),
            'stats': lambda: cached_swr(
                'push_devices_stats_v1',
                fresh_ttl=30,
                stale_ttl=120,
                fetcher=_fetch_push_stats,
                force=request.GET.get('refresh') == '1',
            ),
        }, max_workers=2)

        agg = results['agg']
        facet = agg[0] if agg else {}
        total = ((facet.get('meta') or [{}])[0].get('total', 0)) if facet else 0
        items = clean_value(facet.get('items', []))
        stats = results['stats']

        pag = build_pagination_context(page, total, limit, request.GET)
        context = {
            'devices': items,
            'total': total,
            'limit': limit,
            'search': search,
            'format_filter': format_filter,
            'user_status': user_status,
            **pag,
            'stats': stats,
        }
    except Exception as exc:
        context = {
            'devices': [],
            'total': 0,
            'page': 1,
            'total_pages': 1,
            'page_range': [1],
            'preserved_params': '',
            'limit': limit,
            'search': search,
            'format_filter': format_filter,
            'user_status': user_status,
            'stats': {'users_count': 0, 'tokens_count': 0, 'locked_users': 0, 'malformed_count': 0},
            'error': str(exc),
        }
    return render(request, 'admin_dashboard/notifications/push_devices.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_POST
@require_permission('push_devices.edit')
def api_push_devices_test(request):
    """Send a test Expo Push notification to one or more push tokens (auto-pruning dead ones)."""
    try:
        data = json.loads(request.body or '{}') if request.body else request.POST.dict()
    except (ValueError, TypeError):
        data = request.POST.dict()

    tokens = data.get('tokens')
    if isinstance(tokens, str):
        tokens = [t.strip() for t in tokens.split(',') if t.strip()]
    single_token = (data.get('token') or '').strip()
    if single_token:
        tokens = (tokens or []) + [single_token]

    title = (data.get('title') or 'ENTN Admin Test Push').strip()
    body = (data.get('body') or 'Thử nghiệm kết nối Expo Push từ bảng quản trị ENTN.').strip()

    if not tokens:
        return JsonResponse({'success': False, 'ok': False, 'error': 'Vui lòng chọn ít nhất 1 push token.'}, status=400)

    res = send_expo_push_batch(
        tokens,
        title=title,
        body=body,
        data={'type': 'admin_test_push', 'source': 'push_devices_panel'},
    )
    AuditLog.log(
        request,
        'test_push_device',
        'push_device',
        tokens[0] if len(tokens) == 1 else f'{len(tokens)} devices',
        {'attempted': res['attempted'], 'sent': res['sent'], 'pruned': res['pruned']},
    )
    return JsonResponse({'success': True, 'ok': True, **res})


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_POST
@require_permission('push_devices.delete')
def api_push_devices_remove(request):
    """Remove single, selected, or all malformed push tokens from MongoDB users."""
    try:
        data = json.loads(request.body or '{}') if request.body else request.POST.dict()
    except (ValueError, TypeError):
        data = request.POST.dict()

    try:
        db = mongo_admin.db()
        removed_count = 0

        if data.get('remove_malformed'):
            malformed = list(db.users.aggregate([
                {'$match': {'pushTokens.0': {'$exists': True}}},
                {'$unwind': '$pushTokens'},
                {'$match': {
                    'pushTokens': {'$not': {'$regex': r'^ExponentPushToken\[.+\]$|^ExpoPushToken\[.+\]$'}},
                }},
                {'$group': {'_id': None, 'tokens': {'$addToSet': '$pushTokens'}}},
            ]))
            bad_tokens = malformed[0].get('tokens', []) if malformed else []
            if bad_tokens:
                res = db.users.update_many(
                    {'pushTokens': {'$in': bad_tokens}},
                    {'$pull': {'pushTokens': {'$in': bad_tokens}}},
                )
                removed_count = int(res.modified_count or 0)
            AuditLog.log(request, 'prune_malformed_push', 'push_device', 'malformed', {'tokens': len(bad_tokens), 'users_modified': removed_count})
        else:
            items = data.get('items')
            if not isinstance(items, list):
                uid = (data.get('user_id') or '').strip()
                tok = (data.get('token') or '').strip()
                items = [{'user_id': uid, 'token': tok}] if (uid and tok) else []

            if not items:
                return JsonResponse({'success': False, 'ok': False, 'error': 'Thiếu user_id hoặc token cần xóa.'}, status=400)

            by_uid = {}
            orphan_tokens = []
            for item in items:
                uid = (item.get('user_id') or '').strip()
                tok = (item.get('token') or '').strip()
                if not tok:
                    continue
                if ObjectId.is_valid(uid):
                    by_uid.setdefault(ObjectId(uid), set()).add(tok)
                else:
                    orphan_tokens.append(tok)

            bulk_ops = [
                UpdateOne({'_id': oid}, {'$pull': {'pushTokens': {'$in': list(toks)}}})
                for oid, toks in by_uid.items()
            ]
            if bulk_ops:
                bw_res = db.users.bulk_write(bulk_ops, ordered=False)
                removed_count += int(getattr(bw_res, 'modified_count', 0) or 0)
            if orphan_tokens:
                unique_orphans = list(dict.fromkeys(orphan_tokens))
                res = db.users.update_many(
                    {'pushTokens': {'$in': unique_orphans}},
                    {'$pull': {'pushTokens': {'$in': unique_orphans}}},
                )
                removed_count += int(res.modified_count or 0)

            AuditLog.log(request, 'remove_push_device', 'push_device', str(len(items)), {'removed_count': removed_count})

        invalidate_swr('push_devices_stats_v1', 'notifications_stats_v1', 'admin_sidebar_counts')
        return JsonResponse({'success': True, 'ok': True, 'removed_count': removed_count})
    except Exception as exc:
        return JsonResponse({'success': False, 'ok': False, 'error': str(exc)}, status=500)
