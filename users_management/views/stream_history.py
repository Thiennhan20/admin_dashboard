"""Stream History & stream link health management (`streamhistories`)."""
import json
import time

import requests
from bson import ObjectId
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render

from users_management.decorators import require_permission
from users_management.models import AuditLog
from users_management.services import (
    mongo_admin, clean_value, parallel_run, cached_swr,
    build_pagination_context, invalidate_swr, validate_public_http_url,
)
from users_management.views_legacy import check_admin_access, check_api_status

__all__ = [
    'stream_history_list',
    'api_stream_history_probe',
    'api_stream_history_delete',
]

_USER_MINI_PROJECT = {
    'name': 1,
    'username': 1,
    'email': 1,
    'avatar': 1,
}


def _invalidate_stream_cache():
    invalidate_swr('stream_history_stats_v2', 'admin_sidebar_counts')


def _probe_single_url(url):
    """Safely probe an external stream URL with SSRF check and bounded timeout."""
    ok_url, err = validate_public_http_url(url)
    if not ok_url:
        return {'alive': False, 'status': 0, 'latency_ms': 0, 'error': err}

    headers = {'User-Agent': 'CineArena-Admin-HealthCheck/1.0'}
    t0 = time.monotonic()
    try:
        resp = requests.head(url, headers=headers, timeout=(2.5, 4.0), allow_redirects=False)
        if resp.status_code in (403, 405, 501):
            resp.close()
            resp = requests.get(
                url,
                headers={**headers, 'Range': 'bytes=0-511'},
                timeout=(2.5, 4.0),
                stream=True,
                allow_redirects=False,
            )
        status = resp.status_code
        resp.close()
        latency_ms = round((time.monotonic() - t0) * 1000)
        alive = 200 <= status < 400
        return {'alive': alive, 'status': status, 'latency_ms': latency_ms, 'error': None if alive else f'HTTP {status}'}
    except requests.RequestException as exc:
        latency_ms = round((time.monotonic() - t0) * 1000)
        return {'alive': False, 'status': 0, 'latency_ms': latency_ms, 'error': str(exc)[:120]}


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('stream_history.view')
def stream_history_list(request):
    """Manage generated M3U8/stream links (`db.streamhistories`) and check stream availability."""
    page = max(1, int(request.GET.get('page', 1) or 1))
    limit = max(1, min(100, int(request.GET.get('limit', 20) or 20)))
    search = request.GET.get('search', '').strip()
    media_type = request.GET.get('type', '').strip()
    audio_filter = request.GET.get('audio', '').strip()
    force = request.GET.get('refresh') == '1'

    try:
        db = mongo_admin.db()
        query = {}
        if media_type == 'tv':
            query['contentType'] = 'tvshow'
        elif media_type == 'movie':
            query['contentType'] = 'movie'
        if audio_filter in ('vietsub', 'dubbed'):
            query['audio'] = audio_filter
        if search:
            or_filters = [
                {'title': {'$regex': search, '$options': 'i'}},
                {'movieId': {'$regex': search, '$options': 'i'}},
                {'streamUrl': {'$regex': search, '$options': 'i'}},
                {'roomId': {'$regex': search, '$options': 'i'}},
            ]
            if ObjectId.is_valid(search):
                or_filters.append({'userId': ObjectId(search)})
            query['$or'] = or_filters

        tail_stages = [
            {'$sort': {'lastCreatedAt': -1, 'updatedAt': -1}},
            {'$skip': (page - 1) * limit},
            {'$limit': limit},
            {'$addFields': {
                'playlist_count': {'$size': {'$ifNull': ['$episodePlaylist', []]}},
            }},
            {'$project': {'episodePlaylist': 0}},
            {'$lookup': {
                'from': 'users',
                'let': {'uid': '$userId'},
                'pipeline': [
                    {'$match': {'$expr': {'$eq': ['$_id', '$$uid']}}},
                    {'$project': _USER_MINI_PROJECT},
                ],
                'as': 'user',
            }},
            {'$unwind': {'path': '$user', 'preserveNullAndEmptyArrays': True}},
        ]

        if query:
            data_pipeline = [
                {'$match': query},
                {'$facet': {'meta': [{'$count': 'total'}], 'items': tail_stages}},
            ]
        else:
            data_pipeline = tail_stages

        def _fetch_stream_stats():
            agg = list(db.streamhistories.aggregate([
                {'$group': {
                    '_id': None,
                    'total': {'$sum': 1},
                    'total_plays': {'$sum': {'$ifNull': ['$createdCount', 1]}},
                    'tv_shows': {'$sum': {'$cond': [{'$eq': ['$contentType', 'tvshow']}, 1, 0]}},
                    'movies': {'$sum': {'$cond': [{'$eq': ['$contentType', 'movie']}, 1, 0]}},
                    'vietsub': {'$sum': {'$cond': [{'$eq': ['$audio', 'vietsub']}, 1, 0]}},
                    'dubbed': {'$sum': {'$cond': [{'$eq': ['$audio', 'dubbed']}, 1, 0]}},
                }}
            ]))
            top = clean_value(list(db.streamhistories.aggregate([
                {'$group': {
                    '_id': {'$ifNull': ['$movieId', '$title']},
                    'title': {'$first': '$title'},
                    'plays': {'$sum': {'$ifNull': ['$createdCount', 1]}},
                    'links': {'$sum': 1},
                    'contentType': {'$first': '$contentType'},
                }},
                {'$sort': {'plays': -1}},
                {'$limit': 5},
            ])))
            for t_item in top:
                t_item['movie_id'] = t_item.get('_id') or ''
            s = agg[0] if agg else {}
            return {
                'total': s.get('total', 0) or 0,
                'total_plays': s.get('total_plays', 0) or 0,
                'tv_shows': s.get('tv_shows', 0) or 0,
                'movies': s.get('movies', 0) or 0,
                'vietsub': s.get('vietsub', 0) or 0,
                'dubbed': s.get('dubbed', 0) or 0,
                'top_titles': top,
            }

        results = parallel_run({
            'data': lambda: list(db.streamhistories.aggregate(data_pipeline)),
            'stats': lambda: cached_swr(
                'stream_history_stats_v2',
                fresh_ttl=30,
                stale_ttl=120,
                fetcher=_fetch_stream_stats,
                force=force,
            ),
        }, max_workers=2)

        stats = results['stats']
        if query:
            facet = results['data'][0] if results['data'] else {}
            meta = facet.get('meta', [])
            total = meta[0]['total'] if meta else 0
            raw_items = facet.get('items', [])
        else:
            total = stats['total']
            raw_items = results['data']

        items = clean_value(raw_items)
        for item in items:
            item['id'] = item.get('_id')
            user = item.get('user') if isinstance(item.get('user'), dict) else {}
            item['user_name'] = user.get('name') or user.get('username') or 'Anonymous'
            item['user_email'] = user.get('email', '')
            item['user_id_str'] = str(item.get('userId') or '')

        pag = build_pagination_context(page, total, limit, request.GET)
        context = {
            'items': items,
            'stats': stats,
            'total': total,
            'limit': limit,
            'search': search,
            'type_filter': media_type,
            'audio_filter': audio_filter,
            'api_status': check_api_status(),
            **pag,
        }
    except Exception as exc:
        context = {
            'items': [], 'stats': {}, 'total': 0, 'page': 1, 'total_pages': 1, 'page_range': [1],
            'search': search, 'type_filter': media_type, 'audio_filter': audio_filter,
            'error': str(exc), 'api_status': {'status': 'offline'},
        }
    return render(request, 'admin_dashboard/stream_history/stream_history_list.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('stream_history.view')
def api_stream_history_probe(request):
    """Probe one or up to 10 stored stream URLs by MongoDB ID without exposing SSRF vectors."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)
    try:
        body = json.loads(request.body)
        ids = body.get('ids') or ([body.get('id')] if body.get('id') else [])
        oids = [ObjectId(i) for i in ids[:10] if ObjectId.is_valid(str(i))]
        if not oids:
            return JsonResponse({'success': False, 'error': 'No valid IDs provided'}, status=400)

        db = mongo_admin.db()
        docs = list(db.streamhistories.find({'_id': {'$in': oids}}, {'streamUrl': 1, 'title': 1}))
        probe_tasks = {
            str(d['_id']): (lambda url=d.get('streamUrl', ''): _probe_single_url(url))
            for d in docs
        }
        results = parallel_run(probe_tasks, max_workers=5)
        return JsonResponse({'success': True, 'results': results})
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=500)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('stream_history.delete')
def api_stream_history_delete(request):
    """Delete single or multiple stream history records."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)
    try:
        body = json.loads(request.body)
        ids = body.get('ids') or ([body.get('id')] if body.get('id') else [])
        oids = [ObjectId(i) for i in ids if ObjectId.is_valid(str(i))]
        if not oids:
            return JsonResponse({'success': False, 'error': 'No valid IDs provided'}, status=400)

        db = mongo_admin.db()
        res = db.streamhistories.delete_many({'_id': {'$in': oids}})
        _invalidate_stream_cache()
        AuditLog.log(request, 'delete_stream_histories', 'streamhistory', '', {'count': res.deleted_count})
        return JsonResponse({'success': True, 'deleted': res.deleted_count})
    except Exception as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=500)
