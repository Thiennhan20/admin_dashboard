"""Rate limiting for Admin Panel login & 2FA verification (Upstash Redis + in-memory fallback)."""
import hashlib
import ipaddress
import threading
import time

import requests
from django.conf import settings


_MEM_LOCK = threading.Lock()
_MEM_BUCKETS = {}  # {key: (count, expires_at_monotonic)}

DEFAULT_RULES = {
    'admin_login': {
        'ip_max': 10,
        'account_max': 5,
        'window_seconds': 900,
    },
    'admin_2fa': {
        'ip_max': 10,
        'account_max': 5,
        'window_seconds': 900,
    },
}


def reset_memory_rate_limits():
    """Clear in-memory rate limit buckets (intended for automated tests)."""
    with _MEM_LOCK:
        _MEM_BUCKETS.clear()


def _normalize_ip(raw_value):
    candidate = str(raw_value or '').strip()
    if not candidate:
        return None
    # Strip port on IPv4:port if present
    if ':' in candidate and '.' in candidate:
        candidate = candidate.split(':', 1)[0].strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def get_client_ip(request):
    """Safely extract the client IP address from reverse-proxy headers or REMOTE_ADDR."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        for part in xff.split(','):
            parsed = _normalize_ip(part)
            if parsed:
                return parsed
    xri = _normalize_ip(request.META.get('HTTP_X_REAL_IP', ''))
    if xri:
        return xri
    remote = _normalize_ip(request.META.get('REMOTE_ADDR', ''))
    return remote or 'unknown'


def _hash_part(value):
    return hashlib.sha256(str(value or '').strip().lower().encode('utf-8')).hexdigest()[:32]


def _get_rule(scope):
    base = dict(DEFAULT_RULES.get(scope, {'ip_max': 10, 'account_max': 5, 'window_seconds': 900}))
    custom = getattr(settings, 'ADMIN_RATE_LIMITS', None)
    if isinstance(custom, dict) and isinstance(custom.get(scope), dict):
        base.update(custom[scope])
    return base


def _bucket_keys(scope, ip, account_identifier):
    keys = [('ip', f'admin_sec:rl:{scope}:ip:{_hash_part(ip)}')]
    ident = str(account_identifier or '').strip().lower()
    if ident:
        keys.append(('account', f'admin_sec:rl:{scope}:acct:{_hash_part(ident)}'))
    return keys


def _redis_enabled():
    return bool(getattr(settings, 'UPSTASH_REDIS_URL', '') and getattr(settings, 'UPSTASH_REDIS_TOKEN', ''))


def _redis_pipeline(commands):
    url = settings.UPSTASH_REDIS_URL
    token = settings.UPSTASH_REDIS_TOKEN
    resp = requests.post(
        f'{url}/pipeline',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json=commands,
        timeout=(1.5, 2.5),
    )
    resp.raise_for_status()
    return [item.get('result') for item in resp.json()]


def _mem_get_pair(key):
    now = time.monotonic()
    with _MEM_LOCK:
        entry = _MEM_BUCKETS.get(key)
        if not entry:
            return 0, 0
        count, exp = entry
        if now >= exp:
            _MEM_BUCKETS.pop(key, None)
            return 0, 0
        return count, max(1, int(exp - now))


def _mem_incr(key, window_seconds):
    now = time.monotonic()
    with _MEM_LOCK:
        entry = _MEM_BUCKETS.get(key)
        if not entry or now >= entry[1]:
            exp = now + window_seconds
            _MEM_BUCKETS[key] = (1, exp)
            return 1, int(window_seconds)
        count = entry[0] + 1
        _MEM_BUCKETS[key] = (count, entry[1])
        return count, max(1, int(entry[1] - now))


def _mem_delete(*keys):
    with _MEM_LOCK:
        for k in keys:
            _MEM_BUCKETS.pop(k, None)


def check_rate_limit(scope, request, account_identifier=''):
    """Return `(allowed, retry_after_seconds, reason_kind)` for `(ip, account_identifier)`."""
    rule = _get_rule(scope)
    ip = get_client_ip(request)
    entries = _bucket_keys(scope, ip, account_identifier)
    limits = {'ip': int(rule['ip_max']), 'account': int(rule['account_max'])}

    if _redis_enabled():
        try:
            cmds = []
            for _, key in entries:
                cmds.append(['GET', key])
                cmds.append(['TTL', key])
            res = _redis_pipeline(cmds)
            max_retry = 0
            blocked_kind = ''
            for idx, (kind, _) in enumerate(entries):
                raw_cnt = res[idx * 2]
                ttl_val = res[idx * 2 + 1]
                cnt = int(raw_cnt or 0)
                if cnt >= limits[kind]:
                    ttl_sec = int(ttl_val) if ttl_val and int(ttl_val) > 0 else int(rule['window_seconds'])
                    if ttl_sec > max_retry:
                        max_retry = ttl_sec
                        blocked_kind = kind
            if max_retry > 0:
                return False, max_retry, blocked_kind
            return True, 0, ''
        except Exception:
            pass  # Transparently fall back to in-memory limiter

    max_retry = 0
    blocked_kind = ''
    for kind, key in entries:
        cnt, ttl_sec = _mem_get_pair(key)
        if cnt >= limits[kind] and ttl_sec > max_retry:
            max_retry = ttl_sec
            blocked_kind = kind
    if max_retry > 0:
        return False, max_retry, blocked_kind
    return True, 0, ''


def record_failure(scope, request, account_identifier=''):
    """Increment failure counters and return `(remaining_attempts, locked_now, retry_after_seconds)`."""
    rule = _get_rule(scope)
    window = int(rule['window_seconds'])
    ip = get_client_ip(request)
    entries = _bucket_keys(scope, ip, account_identifier)
    limits = {'ip': int(rule['ip_max']), 'account': int(rule['account_max'])}

    if _redis_enabled():
        try:
            incr_cmds = [['INCR', key] for _, key in entries]
            counts = _redis_pipeline(incr_cmds)
            exp_cmds = []
            for idx, (_, key) in enumerate(entries):
                if int(counts[idx] or 0) == 1:
                    exp_cmds.append(['EXPIRE', key, str(window)])
            if exp_cmds:
                _redis_pipeline(exp_cmds)

            remaining = min(max(0, limits[kind] - int(counts[idx] or 0)) for idx, (kind, _) in enumerate(entries))
            locked_now = any(int(counts[idx] or 0) >= limits[kind] for idx, (kind, _) in enumerate(entries))
            return remaining, locked_now, window if locked_now else 0
        except Exception:
            pass

    remaining_list = []
    locked_now = False
    max_retry = 0
    for kind, key in entries:
        cnt, ttl_sec = _mem_incr(key, window)
        rem = max(0, limits[kind] - cnt)
        remaining_list.append(rem)
        if cnt >= limits[kind]:
            locked_now = True
            max_retry = max(max_retry, ttl_sec)
    return (min(remaining_list) if remaining_list else 0), locked_now, max_retry


def clear_failures(scope, request, account_identifier=''):
    """Clear account failure bucket (and if no other errors on account) upon successful auth."""
    ip = get_client_ip(request)
    entries = _bucket_keys(scope, ip, account_identifier)
    # Clear account bucket + ip bucket on clean authentication
    keys = [key for _, key in entries]
    if _redis_enabled():
        try:
            _redis_pipeline([['DEL', k] for k in keys])
        except Exception:
            pass
    _mem_delete(*keys)


def format_retry_wait(seconds):
    """Format seconds nicely in Vietnamese for lockout messages."""
    secs = max(1, int(seconds or 1))
    mins, rem = divmod(secs, 60)
    if mins > 0 and rem > 0:
        return f'{mins} phút {rem} giây'
    if mins > 0:
        return f'{mins} phút'
    return f'{rem} giây'

