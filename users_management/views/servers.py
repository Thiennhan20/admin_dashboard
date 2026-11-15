import os
import re
import sys
import time
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import requests
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt
from users_management.decorators import require_permission
from users_management.services import mongo_admin, parallel_run, cached_swr
from users_management.views_legacy import check_admin_access

# Default VidSrc settings state
DEFAULT_VIDSRC_SETTINGS = {
    'active_domain': '',
    'backup_domains': [],
    'auto_update': True,
    'last_scraped': None,
    'scraped_domains': []
}

def get_vidsrc_settings():
    if not mongo_admin.enabled:
        return DEFAULT_VIDSRC_SETTINGS
    try:
        db = mongo_admin.db()
        settings_doc = db.systemsettings.find_one({'key': 'vidsrc_config'})
        if settings_doc:
            return {
                'active_domain': settings_doc.get('active_domain', ''),
                'backup_domains': settings_doc.get('backup_domains', []),
                'auto_update': settings_doc.get('auto_update', True),
                'last_scraped': settings_doc.get('last_scraped'),
                'scraped_domains': settings_doc.get('scraped_domains', [])
            }
    except Exception as e:
        print("Error reading vidsrc settings from MongoDB:", e)
    return DEFAULT_VIDSRC_SETTINGS

def save_vidsrc_settings(data):
    if not mongo_admin.enabled:
        return
    try:
        db = mongo_admin.db()
        db.systemsettings.update_one(
            {'key': 'vidsrc_config'},
            {'$set': {**data, 'updated_at': time.time()}},
            upsert=True
        )
    except Exception as e:
        print("Error saving vidsrc settings to MongoDB:", e)

def fetch_and_scan_vidsrc_domains():
    url = 'https://vidsrc.domains/'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        html = res.text
    except Exception as e:
        return {'ok': False, 'error': f"Failed to fetch vidsrc.domains: {str(e)}"}

    active_match = re.search(r'<section[^>]*id=["\']domains["\'][^>]*>([\s\S]*?)<\/section>', html, re.I)
    old_match = re.search(r'<section[^>]*id=["\']old["\'][^>]*>([\s\S]*?)<\/section>', html, re.I)

    active_html = active_match.group(1) if active_match else ''
    old_html = old_match.group(1) if old_match else ''

    def extract_domains(source_html, status_class):
        pattern = rf'<span\s+class=["\'](?:domain-name|domain-plain)["\']>([^<]+)<\/span>\s*<span\s+class=["\']{status_class}["\']'
        matches = re.findall(pattern, source_html, re.I)
        results = []
        for d in matches:
            d_clean = d.strip().lower()
            if not d_clean.startswith('http'):
                d_clean = f'https://{d_clean}'
            results.append(d_clean)
        return list(dict.fromkeys(results))

    group_active = extract_domains(active_html or html, 'status-live')
    group_old = extract_domains(old_html or html, 'status-old')
    group_dead = extract_domains(old_html or html, 'status-dead')

    def check_health(domain_url):
        clean_domain = domain_url.rstrip('/')
        start_time = time.time()
        try:
            # Step 1: Request gate token from vs_src.php
            token_url = f"{clean_domain}/vs_src.php?type=movie&id=550"
            token_headers = {
                'User-Agent': headers['User-Agent'],
                'Referer': f"{clean_domain}/embed/movie?tmdb=550",
            }
            r_token = requests.get(token_url, headers=token_headers, timeout=3.5)
            if r_token.status_code != 200:
                return {
                    'url': domain_url,
                    'ok': False,
                    'status': r_token.status_code,
                    'error': f'vs_src.php returned {r_token.status_code}',
                    'latency': int((time.time() - start_time) * 1000)
                }

            token_data = r_token.json()
            cloud_url = token_data.get('src')
            if not cloud_url:
                return {
                    'url': domain_url,
                    'ok': False,
                    'status': 400,
                    'error': 'Missing stream src in vs_src.php',
                    'latency': int((time.time() - start_time) * 1000)
                }

            # Step 2: Validate token on CDN (cloudorchestranova.com)
            cloud_headers = {
                'User-Agent': headers['User-Agent'],
                'Referer': f"{clean_domain}/",
            }
            r_cloud = requests.get(cloud_url, headers=cloud_headers, timeout=3.5)
            latency = int((time.time() - start_time) * 1000)

            is_expired = (r_cloud.status_code == 403) or ('Session expired' in r_cloud.text) or ('<title>Expired</title>' in r_cloud.text)
            if r_cloud.status_code == 200 and not is_expired:
                return {
                    'url': domain_url,
                    'ok': True,
                    'status': 200,
                    'latency': latency
                }
            else:
                return {
                    'url': domain_url,
                    'ok': False,
                    'status': r_cloud.status_code,
                    'error': 'Session expired / Blocked by CDN' if is_expired else f'CDN HTTP {r_cloud.status_code}',
                    'latency': latency
                }
        except Exception as err:
            latency = int((time.time() - start_time) * 1000)
            return {
                'url': domain_url,
                'ok': False,
                'status': 'Error',
                'error': str(err),
                'latency': latency
            }

    # Probe active and historical candidates concurrently instead of sequentially
    candidates = list(group_active) + list(group_old)
    if candidates:
        with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
            probed = list(pool.map(check_health, candidates))
    else:
        probed = []
    split_at = len(group_active)
    active_results = probed[:split_at]
    old_results = probed[split_at:]
    dead_results = [{'url': d, 'ok': False, 'status': 'Dead', 'error': 'Reported dead by vidsrc.domains', 'latency': 0} for d in group_dead]

    # Find best active domain and backup domains sorted by latency
    all_online = [r for r in (active_results + old_results) if r['ok']]
    all_online.sort(key=lambda x: x['latency'])
    best_domain = all_online[0]['url'] if all_online else ''
    backup_domains = [r['url'] for r in all_online[1:5]] if len(all_online) > 1 else []

    scraped_data = {
        'active_group': active_results,
        'old_group': old_results,
        'dead_group': dead_results,
        'best_domain': best_domain,
        'backup_domains': backup_domains,
        'scraped_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'scraped_timestamp': time.time()
    }

    return {'ok': True, 'data': scraped_data}


def _probe_single_clusters():
    """Probe KKPhim, VidSrc, and NguonC streaming clusters concurrently."""
    ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36'

    def _probe_s1():
        t0 = time.time()
        try:
            r1 = requests.get('https://phimapi.com/danh-sach/phim-moi-cap-nhat?page=1', headers={'User-Agent': ua}, timeout=4)
            lat1 = int((time.time() - t0) * 1000)
            items1 = len((r1.json() or {}).get('items', [])) if r1.ok else 0
            return {
                'name': 'KKPhim / PhimAPI',
                'endpoint': 'https://phimapi.com',
                'protocol': 'HLS Stream (M3U8)',
                'quality': 'Full HD 1080p • Vietsub/Thuyết Minh',
                'ok': r1.ok,
                'status': r1.status_code,
                'latency': lat1,
                'items_sample': items1,
            }
        except Exception as exc:
            return {
                'name': 'KKPhim / PhimAPI',
                'endpoint': 'https://phimapi.com',
                'protocol': 'HLS Stream (M3U8)',
                'quality': 'Full HD 1080p',
                'ok': False,
                'status': 'ERR',
                'latency': int((time.time() - t0) * 1000),
                'error': str(exc),
            }

    def _probe_s2():
        vidsrc_cfg = get_vidsrc_settings()
        active_dom = (vidsrc_cfg.get('active_domain') or 'https://vidsrc.xyz').rstrip('/')
        t1 = time.time()
        try:
            r2 = requests.get(active_dom, headers={'User-Agent': ua}, timeout=4)
            lat2 = int((time.time() - t1) * 1000)
            return {
                'name': 'VidSrc Global CDN',
                'endpoint': active_dom,
                'protocol': 'HLS / Auto-Domain Failover',
                'quality': '1080p Multi-Audio / Subtitles',
                'ok': r2.status_code < 500,
                'status': r2.status_code,
                'latency': lat2,
                'backup_count': len(vidsrc_cfg.get('backup_domains', [])),
            }
        except Exception as exc:
            return {
                'name': 'VidSrc Global CDN',
                'endpoint': active_dom,
                'protocol': 'HLS / Auto-Domain Failover',
                'quality': '1080p Multi-Audio',
                'ok': False,
                'status': 'ERR',
                'latency': int((time.time() - t1) * 1000),
                'error': str(exc),
            }

    def _probe_s3():
        t2 = time.time()
        try:
            r3 = requests.get('https://ophim17.cc/v1/api/home', headers={'User-Agent': ua}, timeout=4)
            lat3 = int((time.time() - t2) * 1000)
            return {
                'name': 'NguonC / OPhim Backup',
                'endpoint': 'https://ophim17.cc',
                'protocol': 'HLS / Embed Hybrid',
                'quality': 'HD 720p / 1080p',
                'ok': r3.ok,
                'status': r3.status_code,
                'latency': lat3,
            }
        except Exception as exc:
            return {
                'name': 'NguonC / OPhim Backup',
                'endpoint': 'https://ophim17.cc',
                'protocol': 'HLS / Embed Hybrid',
                'quality': 'HD 720p / 1080p',
                'ok': False,
                'status': 'ERR',
                'latency': int((time.time() - t2) * 1000),
                'error': str(exc),
            }

    return parallel_run({
        'server1': _probe_s1,
        'server2': _probe_s2,
        'server3': _probe_s3,
    }, max_workers=3)


def probe_streaming_clusters(force=False):
    """Live health & latency probe for Server 1 (KKPhim), Server 2 (VidSrc), and Server 3 (NguonC/OPhim)."""
    return cached_swr(
        'streaming_clusters_probe_v1',
        fresh_ttl=45,
        stale_ttl=300,
        fetcher=_probe_single_clusters,
        force=force,
    )


_vidsrc_bg_lock = threading.Lock()


def _run_and_persist_vidsrc_scan():
    """Run VidSrc domain scan and save settings while holding _vidsrc_bg_lock."""
    if not _vidsrc_bg_lock.acquire(blocking=False):
        return None
    try:
        config = get_vidsrc_settings()
        scan_res = fetch_and_scan_vidsrc_domains()
        if scan_res.get('ok'):
            data = scan_res['data']
            if config.get('auto_update', True) and data.get('best_domain'):
                config['active_domain'] = data['best_domain']
                config['backup_domains'] = data.get('backup_domains', [])
            config['last_scraped'] = data['scraped_at']
            config['scraped_domains'] = data
            save_vidsrc_settings(config)
        return scan_res
    finally:
        _vidsrc_bg_lock.release()


def _trigger_vidsrc_scan_async():
    """Kick off a non-blocking VidSrc domain scan in a daemon thread if one isn't running."""
    if _vidsrc_bg_lock.locked():
        return

    def _run():
        try:
            _run_and_persist_vidsrc_scan()
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True, name='VidSrcOnDemandScan').start()


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('servers.view')
def server_management(request):
    vidsrc_config = get_vidsrc_settings()
    scraped_data = vidsrc_config.get('scraped_domains', {})
    scraped_ts = scraped_data.get('scraped_timestamp', 0) if isinstance(scraped_data, dict) else 0
    SCAN_INTERVAL = 20 * 60  # 20 minutes

    # Trigger background refresh if stale instead of blocking page load
    if not scraped_data or (time.time() - scraped_ts > SCAN_INTERVAL):
        _trigger_vidsrc_scan_async()

    clusters = probe_streaming_clusters(force=request.GET.get('refresh') == '1')
    context = {
        'vidsrc_config': vidsrc_config,
        'vidsrc_config_json': json.dumps(vidsrc_config.get('scraped_domains', {})),
        'clusters': clusters,
        'active_tab': 'servers'
    }
    return render(request, 'admin_dashboard/servers/servers.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('servers.view')
@require_GET
def api_servers_probe(request):
    """API endpoint to live-ping all 3 streaming servers (KKPhim, VidSrc, NguonC)."""
    return JsonResponse({'ok': True, 'clusters': probe_streaming_clusters(force=True), 'probed_at': time.strftime('%H:%M:%S')})


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('servers.edit')
@require_GET
def api_vidsrc_scan(request):
    res = fetch_and_scan_vidsrc_domains()
    if not res.get('ok'):
        return JsonResponse(res, status=500)
    
    config = get_vidsrc_settings()
    data = res['data']
    
    # If auto update is ON, automatically save best domain and backup domains
    if config.get('auto_update') and data.get('best_domain'):
        config['active_domain'] = data['best_domain']
        config['backup_domains'] = data.get('backup_domains', [])

    config['last_scraped'] = data['scraped_at']
    config['scraped_domains'] = data
    save_vidsrc_settings(config)

    return JsonResponse({'ok': True, 'config': config, 'scan_results': data})


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('servers.edit')
@require_POST
def api_vidsrc_select(request):
    domain = request.POST.get('domain')
    if not domain:
        return JsonResponse({'ok': False, 'error': 'Domain parameter is required'}, status=400)
    
    config = get_vidsrc_settings()
    config['active_domain'] = domain
    save_vidsrc_settings(config)
    return JsonResponse({'ok': True, 'active_domain': domain})


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('servers.edit')
@require_POST
def api_vidsrc_toggle_auto(request):
    auto = request.POST.get('auto') == 'true'
    config = get_vidsrc_settings()
    config['auto_update'] = auto
    save_vidsrc_settings(config)
    return JsonResponse({'ok': True, 'auto_update': auto})


# ── Background 20-Minute Cron Thread ────────────────────────────────────
_vidsrc_cron_started = False

def start_vidsrc_cron_thread():
    global _vidsrc_cron_started
    if _vidsrc_cron_started:
        return
    _vidsrc_cron_started = True

    def loop():
        SCAN_INTERVAL = 20 * 60  # 20 minutes (1,200 seconds)
        # 1. Run immediate check on server boot if scan is missing or older than 20 mins
        try:
            config = get_vidsrc_settings()
            scraped_data = config.get('scraped_domains', {})
            scraped_ts = scraped_data.get('scraped_timestamp', 0) if isinstance(scraped_data, dict) else 0
            if not scraped_data or (time.time() - scraped_ts > SCAN_INTERVAL):
                _run_and_persist_vidsrc_scan()
        except Exception as e:
            print("VidSrc initial cron scan error:", e)

        # 2. Continuous 20-minute scan loop
        while True:
            time.sleep(SCAN_INTERVAL)
            try:
                _run_and_persist_vidsrc_scan()
            except Exception as e:
                print("VidSrc recurring cron scan error:", e)

    t = threading.Thread(target=loop, daemon=True, name="VidSrcCronThread")
    t.start()

# Only start background cron in active server processes, not management commands like migrate/check
if not any(cmd in sys.argv for cmd in ('migrate', 'makemigrations', 'collectstatic', 'check', 'test', 'shell')):
    start_vidsrc_cron_thread()
