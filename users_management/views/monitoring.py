"""System monitoring views with persistent MongoDB health logs."""
from datetime import datetime
import time

from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render
import requests

from users_management.decorators import require_permission
from users_management.services import clean_value, mongo_admin, parallel_run
from users_management.views_legacy import check_admin_access
from users_management.webhook_dispatcher import dispatch_webhook_event

__all__ = ['monitoring_dashboard', 'api_health_check']


def _check_nodejs_health():
    try:
        start = time.time()
        resp = requests.get(f'{settings.SERVER_URL}/health', timeout=5)
        ms = round((time.time() - start) * 1000)
        return {'status': 'online' if resp.ok else 'error', 'response_ms': ms, 'status_code': resp.status_code}
    except Exception as e:
        return {'status': 'offline', 'error': str(e)}


def _check_mongodb_health():
    try:
        start = time.time()
        ping = mongo_admin.ping()
        ms = round((time.time() - start) * 1000)
        entry = {'status': 'online' if ping.get('ok') else 'error', 'response_ms': ms}
        if mongo_admin.enabled:
            stats = mongo_admin.db().command('dbStats')
            entry['collections'] = stats.get('collections', 0)
            entry['data_size'] = stats.get('dataSize', 0)
            entry['storage_size'] = stats.get('storageSize', 0)
        return entry
    except Exception as e:
        return {'status': 'offline', 'error': str(e)}


def _check_redis_cache_health():
    try:
        url = (settings.UPSTASH_REDIS_URL or '').rstrip('/')
        token = settings.UPSTASH_REDIS_TOKEN
        if not (url and token):
            return {'status': 'not_configured'}
        headers = {'Authorization': f'Bearer {token}'}
        start = time.time()
        resp = requests.post(f'{url}/pipeline', headers=headers, json=[['PING'], ['INFO']], timeout=5)
        ms = round((time.time() - start) * 1000)
        info_text = ''
        if resp.ok and isinstance(resp.json(), list):
            parts = resp.json()
            entry = {'status': 'online', 'response_ms': ms}
            if len(parts) > 1 and isinstance(parts[1], dict):
                info_text = parts[1].get('result', '') or ''
        else:
            ping_resp = requests.post(url, headers=headers, json=['PING'], timeout=5)
            ms = round((time.time() - start) * 1000)
            entry = {'status': 'online' if ping_resp.ok else 'error', 'response_ms': ms}
            if ping_resp.ok:
                info_resp = requests.post(url, headers=headers, json=['INFO'], timeout=5)
                if info_resp.ok:
                    info_text = info_resp.json().get('result', '') or ''
        for line in info_text.splitlines():
            if ':' in line:
                k, v = line.split(':', 1)
                if k in ('total_commands_processed', 'total_data_size_human', 'max_data_size_human'):
                    entry[k] = v
        return entry
    except Exception as e:
        return {'status': 'offline', 'error': str(e)}


def _check_redis_rooms_health():
    try:
        url = settings.UPSTASH_REDIS_ROOMS_URL
        token = settings.UPSTASH_REDIS_ROOMS_TOKEN
        if not (url and token):
            return {'status': 'not_configured'}
        start = time.time()
        resp = requests.post(url, headers={'Authorization': f'Bearer {token}'}, json=['PING'], timeout=5)
        ms = round((time.time() - start) * 1000)
        return {'status': 'online' if resp.ok else 'error', 'response_ms': ms}
    except Exception as e:
        return {'status': 'offline', 'error': str(e)}


def _check_postgresql_health():
    try:
        from django.db import connection
        start = time.time()
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        ms = round((time.time() - start) * 1000)
        return {'status': 'online', 'response_ms': ms}
    except Exception as e:
        return {'status': 'offline', 'error': str(e)}


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('monitoring.view')
def monitoring_dashboard(request):
    history_logs = []
    if mongo_admin.enabled:
        try:
            history_logs = clean_value(list(
                mongo_admin.db().admin_health_logs.find({})
                .sort('checkedAt', -1)
                .limit(15)
            ))
        except Exception:
            history_logs = []
    return render(request, 'admin_dashboard/monitoring/monitoring.html', {'initial_history': history_logs})


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('monitoring.view')
def api_health_check(request):
    """Check health of all connected services concurrently and persist snapshot."""
    results = parallel_run({
        'nodejs': _check_nodejs_health,
        'mongodb': _check_mongodb_health,
        'redis_cache': _check_redis_cache_health,
        'redis_rooms': _check_redis_rooms_health,
        'postgresql': _check_postgresql_health,
    }, max_workers=5)

    offline_services = [name for name, info in results.items() if info.get('status') in ('offline', 'error')]
    if offline_services:
        dispatch_webhook_event('system.error', {
            'source': 'monitoring',
            'offline_services': offline_services,
        })

    history_logs = []
    if mongo_admin.enabled:
        try:
            db = mongo_admin.db()
            now = datetime.utcnow()
            db.admin_health_logs.insert_one({
                'checkedAt': now,
                'services': {
                    k: {
                        'status': v.get('status', 'unknown'),
                        'response_ms': v.get('response_ms'),
                        'error': (v.get('error') or '')[:200],
                    }
                    for k, v in results.items()
                },
                'offlineCount': len(offline_services),
            })
            # Retain at most 100 latest health check snapshots
            stale = list(db.admin_health_logs.find({}, {'_id': 1}).sort('checkedAt', -1).skip(100))
            if stale:
                db.admin_health_logs.delete_many({'_id': {'$in': [s['_id'] for s in stale]}})
            history_logs = clean_value(list(
                db.admin_health_logs.find({}).sort('checkedAt', -1).limit(15)
            ))
        except Exception:
            history_logs = []

    return JsonResponse({'success': True, 'services': results, 'history': history_logs})
