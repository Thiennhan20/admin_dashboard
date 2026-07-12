"""System monitoring views."""
import time

from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render
import requests

from users_management.services import mongo_admin
from users_management.views_legacy import check_admin_access

__all__ = ['monitoring_dashboard', 'api_health_check']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def monitoring_dashboard(request):
    return render(request, 'admin_dashboard/monitoring/monitoring.html')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def api_health_check(request):
    """Check health of all connected services."""
    results = {}

    # 1. Node.js Server
    try:
        start = time.time()
        resp = requests.get(f'{settings.SERVER_URL}/health', timeout=5)
        ms = round((time.time() - start) * 1000)
        results['nodejs'] = {'status': 'online' if resp.ok else 'error', 'response_ms': ms, 'status_code': resp.status_code}
    except Exception as e:
        results['nodejs'] = {'status': 'offline', 'error': str(e)}

    # 2. MongoDB
    try:
        start = time.time()
        ping = mongo_admin.ping()
        ms = round((time.time() - start) * 1000)
        results['mongodb'] = {'status': 'online' if ping.get('ok') else 'error', 'response_ms': ms}
        if mongo_admin.enabled:
            db = mongo_admin.db()
            stats = db.command('dbStats')
            results['mongodb']['collections'] = stats.get('collections', 0)
            results['mongodb']['data_size'] = stats.get('dataSize', 0)
            results['mongodb']['storage_size'] = stats.get('storageSize', 0)
    except Exception as e:
        results['mongodb'] = {'status': 'offline', 'error': str(e)}

    # 3. Upstash Redis (Cache)
    try:
        url = settings.UPSTASH_REDIS_URL
        token = settings.UPSTASH_REDIS_TOKEN
        if url and token:
            start = time.time()
            resp = requests.post(url, headers={'Authorization': f'Bearer {token}'}, json=['PING'], timeout=5)
            ms = round((time.time() - start) * 1000)
            results['redis_cache'] = {'status': 'online' if resp.ok else 'error', 'response_ms': ms}
            # Get info
            info_resp = requests.post(url, headers={'Authorization': f'Bearer {token}'}, json=['INFO'], timeout=5)
            if info_resp.ok:
                info_text = info_resp.json().get('result', '')
                for line in info_text.splitlines():
                    if ':' in line:
                        k, v = line.split(':', 1)
                        if k in ('total_commands_processed', 'total_data_size_human', 'max_data_size_human'):
                            results['redis_cache'][k] = v
        else:
            results['redis_cache'] = {'status': 'not_configured'}
    except Exception as e:
        results['redis_cache'] = {'status': 'offline', 'error': str(e)}

    # 4. Upstash Redis (Rooms)
    try:
        url = settings.UPSTASH_REDIS_ROOMS_URL
        token = settings.UPSTASH_REDIS_ROOMS_TOKEN
        if url and token:
            start = time.time()
            resp = requests.post(url, headers={'Authorization': f'Bearer {token}'}, json=['PING'], timeout=5)
            ms = round((time.time() - start) * 1000)
            results['redis_rooms'] = {'status': 'online' if resp.ok else 'error', 'response_ms': ms}
        else:
            results['redis_rooms'] = {'status': 'not_configured'}
    except Exception as e:
        results['redis_rooms'] = {'status': 'offline', 'error': str(e)}

    # 5. PostgreSQL
    try:
        from django.db import connection
        start = time.time()
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        ms = round((time.time() - start) * 1000)
        results['postgresql'] = {'status': 'online', 'response_ms': ms}
    except Exception as e:
        results['postgresql'] = {'status': 'offline', 'error': str(e)}

    return JsonResponse({'success': True, 'services': results})
