import re
import time
import json
import threading
import requests
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt
from users_management.services import mongo_admin

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

    active_results = [check_health(d) for d in group_active]
    old_results = [check_health(d) for d in group_old]
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


@login_required
def server_management(request):
    vidsrc_config = get_vidsrc_settings()
    scraped_data = vidsrc_config.get('scraped_domains', {})
    scraped_ts = scraped_data.get('scraped_timestamp', 0) if isinstance(scraped_data, dict) else 0
    SCAN_INTERVAL = 20 * 60  # 20 minutes

    # Auto-scan if no data or if 20 minutes passed since last scan
    if not scraped_data or (time.time() - scraped_ts > SCAN_INTERVAL):
        scan_res = fetch_and_scan_vidsrc_domains()
        if scan_res.get('ok'):
            data = scan_res['data']
            if vidsrc_config.get('auto_update', True):
                vidsrc_config['active_domain'] = data['best_domain']
                vidsrc_config['backup_domains'] = data.get('backup_domains', [])
            vidsrc_config['last_scraped'] = data['scraped_at']
            vidsrc_config['scraped_domains'] = data
            save_vidsrc_settings(vidsrc_config)

    context = {
        'vidsrc_config': vidsrc_config,
        'vidsrc_config_json': json.dumps(vidsrc_config.get('scraped_domains', {})),
        'active_tab': 'servers'
    }
    return render(request, 'admin_dashboard/servers/servers.html', context)


@login_required
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
                res = fetch_and_scan_vidsrc_domains()
                if res.get('ok'):
                    data = res['data']
                    if config.get('auto_update', True):
                        config['active_domain'] = data['best_domain']
                        config['backup_domains'] = data.get('backup_domains', [])
                    config['last_scraped'] = data['scraped_at']
                    config['scraped_domains'] = data
                    save_vidsrc_settings(config)
        except Exception as e:
            print("VidSrc initial cron scan error:", e)

        # 2. Continuous 20-minute scan loop
        while True:
            time.sleep(SCAN_INTERVAL)
            try:
                config = get_vidsrc_settings()
                res = fetch_and_scan_vidsrc_domains()
                if res.get('ok'):
                    data = res['data']
                    if config.get('auto_update', True):
                        config['active_domain'] = data['best_domain']
                        config['backup_domains'] = data.get('backup_domains', [])
                    config['last_scraped'] = data['scraped_at']
                    config['scraped_domains'] = data
                    save_vidsrc_settings(config)
            except Exception as e:
                print("VidSrc recurring cron scan error:", e)

    t = threading.Thread(target=loop, daemon=True, name="VidSrcCronThread")
    t.start()

start_vidsrc_cron_thread()
