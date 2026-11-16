from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
import ipaddress
import platform
import socket
import sys
import threading
from urllib.parse import urlencode, urlparse

import django
import requests
from bson import ObjectId
from bson.errors import InvalidId
from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections, connections
from pymongo import MongoClient
from pymongo.errors import PyMongoError


API_TIMEOUT = 8
_CACHE_LOCK = threading.Lock()
_INFLIGHT_REFRESHES = set()


def parallel_run(tasks, max_workers=6):
    """Execute a mapping of {name: zero-arg callable} concurrently and collect results.

    Returns a dict mapping each name to its return value (exceptions propagate if not
    handled inside individual callables).
    """
    if not tasks:
        return {}
    workers = min(max_workers, len(tasks))
    if workers <= 1:
        return {name: fn() for name, fn in tasks.items()}

    def _run_worker(fn):
        try:
            return fn()
        finally:
            connections.close_all()

    results = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(_run_worker, fn): name for name, fn in tasks.items()}
        for future in as_completed(future_map):
            name = future_map[future]
            results[name] = future.result()
    return results


def _run_refresh(key, fresh_ttl, stale_ttl, fetcher):
    try:
        value = fetcher()
        cache.set(f'{key}:fresh', value, fresh_ttl)
        cache.set(f'{key}:stale', value, stale_ttl)
        return value
    except Exception:
        return None
    finally:
        close_old_connections()
        with _CACHE_LOCK:
            _INFLIGHT_REFRESHES.discard(key)


def cached_swr(key, fresh_ttl, stale_ttl, fetcher, force=False, non_blocking_default=None):
    """Cache helper implementing Stale-While-Revalidate (SWR) with single-flight refresh.

    - Fresh hit: returns immediately from cache.
    - Stale hit (not forced): returns stale value immediately and triggers one
      background daemon refresh if none is in flight.
    - Cold miss: returns `non_blocking_default` (and warms in background) if provided,
      otherwise fetches synchronously and caches both fresh and stale tiers.
    """
    if not force:
        fresh = cache.get(f'{key}:fresh')
        if fresh is not None:
            return fresh

        stale = cache.get(f'{key}:stale')
        if stale is not None:
            _trigger_bg_refresh(key, fresh_ttl, stale_ttl, fetcher)
            return stale

        if non_blocking_default is not None:
            _trigger_bg_refresh(key, fresh_ttl, stale_ttl, fetcher)
            return non_blocking_default

    value = fetcher()
    cache.set(f'{key}:fresh', value, fresh_ttl)
    cache.set(f'{key}:stale', value, stale_ttl)
    return value


def _trigger_bg_refresh(key, fresh_ttl, stale_ttl, fetcher):
    with _CACHE_LOCK:
        if key in _INFLIGHT_REFRESHES:
            return
        _INFLIGHT_REFRESHES.add(key)
    t = threading.Thread(
        target=_run_refresh,
        args=(key, fresh_ttl, stale_ttl, fetcher),
        daemon=True,
        name=f'swr-{key}',
    )
    t.start()


def invalidate_swr(*keys):
    """Invalidate both fresh and stale cache keys for one or more SWR entries."""
    to_delete = []
    for k in keys:
        if not k:
            continue
        to_delete.append(f'{k}:fresh')
        to_delete.append(f'{k}:stale')
    if to_delete:
        cache.delete_many(to_delete)


_MISSING_PROFILE = object()


def get_admin_profile(user, request=None):
    """Fetch a user's AdminProfile at most once per HTTP request."""
    if not user or not getattr(user, 'is_authenticated', False):
        return None
    if request is not None:
        cached = getattr(request, '_cached_admin_profile', _MISSING_PROFILE)
        if cached is not _MISSING_PROFILE:
            return cached
    try:
        profile = user.admin_profile
    except Exception:
        profile = None
    if request is not None:
        try:
            request._cached_admin_profile = profile
        except Exception:
            pass
    return profile

MONGO_COLLECTIONS = {
    'users': {
        'label': 'Users',
        'sort': [('createdAt', -1)],
        'projection': {'password': 0, 'emailVerificationToken': 0},
    },
    'sessions': {
        'label': 'Active Sessions',
        'sort': [('lastActive', -1)],
        'projection': {'refreshToken': 0},
    },
    'comments': {
        'label': 'Comments',
        'sort': [('createdAt', -1)],
    },
    'userreports': {
        'label': 'User Reports',
        'sort': [('createdAt', -1)],
    },
    'watchprogresses': {
        'label': 'Watch Progress',
        'sort': [('lastWatched', -1)],
    },
    'streamhistories': {
        'label': 'Stream History',
        'sort': [('lastCreatedAt', -1)],
    },
    'searchhistories': {
        'label': 'Search History',
        'sort': [('updated_at', -1)],
    },
    'notifications': {
        'label': 'Notifications',
        'sort': [('createdAt', -1)],
    },
    'passwordresettokens': {
        'label': 'Password Reset Tokens',
        'sort': [('createdAt', -1)],
        'projection': {'tokenHash': 0},
    },
    'blacklistedtokens': {
        'label': 'Blacklisted Tokens',
        'sort': [('createdAt', -1)],
        'projection': {'token': 0},
    },
    'gameprofiles': {
        'label': 'Game Profiles',
        'sort': [('rating', -1)],
    },
    'gamehistories': {
        'label': 'Game Match Histories',
        'sort': [('createdAt', -1)],
    },
    'dailychallenges': {
        'label': 'Daily Challenges',
        'sort': [('date', -1)],
    },
    'dailysubmissions': {
        'label': 'Daily Submissions',
        'sort': [('updatedAt', -1)],
    },
    'systemsettings': {
        'label': 'System Settings',
        'sort': [('updatedAt', -1)],
    },
}


def build_pagination_context(page, total, limit, query_params=None):
    """Build bounded pagination range and preserved query string for includes/pagination.html."""
    total_pages = max(1, -(-int(total) // int(limit)))
    page = max(1, min(int(page), total_pages))
    start_p = max(1, page - 2)
    end_p = min(total_pages, page + 2)
    page_range = list(range(start_p, end_p + 1))

    preserved = ''
    if query_params:
        cleaned = {k: v for k, v in query_params.items() if k != 'page' and v not in (None, '')}
        preserved = urlencode(cleaned)

    return {
        'page': page,
        'total_pages': total_pages,
        'page_range': page_range,
        'preserved_params': preserved,
    }


def format_device_display(session_doc):
    """Normalize session device fields (`device`, `deviceInfo`, `userAgent`) for UI display."""
    if not isinstance(session_doc, dict):
        return {'primary': 'Unknown Device', 'secondary': ''}
    device_info = session_doc.get('deviceInfo')
    if isinstance(device_info, dict):
        browser = device_info.get('browser') or ''
        os_name = device_info.get('os') or ''
        hw = device_info.get('device') or ''
        if browser and os_name:
            primary = f'{browser} on {os_name}'
        else:
            primary = browser or os_name or str(session_doc.get('device') or 'Browser Session')
        return {'primary': primary, 'secondary': hw}

    raw_device = str(session_doc.get('device') or '').strip()
    ua = str(session_doc.get('userAgent') or '').strip()
    if raw_device:
        secondary = ua[:70] + ('...' if len(ua) > 70 else '') if ua else ''
        return {'primary': raw_device, 'secondary': secondary}
    if ua:
        return {'primary': ua[:60] + ('...' if len(ua) > 60 else ''), 'secondary': ''}
    return {'primary': 'Unknown Device', 'secondary': ''}


def validate_public_http_url(url):
    """Validate an external HTTP/HTTPS URL and reject loopback/private/link-local targets (SSRF guard)."""
    try:
        parsed = urlparse(str(url or '').strip())
    except Exception:
        return False, 'Malformed URL'
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return False, 'URL must use http or https scheme with a valid host'

    host = parsed.hostname.lower()
    if host in ('localhost', '127.0.0.1', '::1', '0.0.0.0') or host.endswith(('.local', '.internal', '.localhost')):
        return False, 'Refusing to probe local/internal hostname'

    def _is_disallowed_ip(ip_str):
        ip_obj = ipaddress.ip_address(ip_str)
        return (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        )

    try:
        if _is_disallowed_ip(host):
            return False, 'Refusing to probe private or reserved IP address'
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
            for info in infos:
                addr = info[4][0]
                if _is_disallowed_ip(addr):
                    return False, f'Hostname resolves to disallowed IP ({addr})'
        except socket.gaierror:
            return False, 'DNS resolution failed for host'

    return True, None


def clean_value(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return [clean_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): clean_value(item) for key, item in value.items()}
    return value


def api_get(path, params=None, timeout=API_TIMEOUT):
    url = f'{settings.API_BASE_URL}{path}'
    headers = {}
    if getattr(settings, 'ADMIN_API_KEY', None):
        headers['x-admin-api-key'] = settings.ADMIN_API_KEY
    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _fetch_server_health():
    try:
        response = requests.get(f'{settings.SERVER_URL}/health', timeout=(2, 4))
        return {
            'ok': response.ok,
            'status_code': response.status_code,
            'data': response.json() if response.headers.get('content-type', '').startswith('application/json') else {},
        }
    except requests.RequestException as exc:
        return {'ok': False, 'error': str(exc)}


def server_health(force=False):
    return cached_swr('srv_health_cache', fresh_ttl=45, stale_ttl=300, fetcher=_fetch_server_health, force=force)


def check_api_status():
    """Non-blocking API health summary using cached server_health telemetry."""
    data = cached_swr(
        'srv_health_cache',
        fresh_ttl=45,
        stale_ttl=300,
        fetcher=_fetch_server_health,
        non_blocking_default={'ok': None},
    )
    ok = data.get('ok') if isinstance(data, dict) else None
    if ok is True:
        return {'status': 'online'}
    if ok is False:
        err = data.get('error') or f"HTTP {data.get('status_code', 'error')}"
        return {'status': 'offline', 'message': err}
    return {'status': 'checking'}


def _fetch_public_rooms():
    try:
        data = api_get('/rooms/public', timeout=(2, 4))
        return data.get('rooms', []), None
    except requests.RequestException as exc:
        return [], str(exc)


def public_rooms(force=False):
    return cached_swr('srv_public_rooms_cache', fresh_ttl=30, stale_ttl=300, fetcher=_fetch_public_rooms, force=force)


def _fetch_arena_health():
    game_url = getattr(settings, 'GAME_SERVER_URL', 'http://localhost:3006')
    try:
        resp = requests.get(f'{game_url}/api/admin/overview', timeout=(0.8, 2.0))
        if resp.ok:
            return resp.json()
        resp_health = requests.get(f'{game_url}/api/health', timeout=(0.8, 2.0))
        if resp_health.ok:
            return resp_health.json()
        return {'ok': False, 'error': f'HTTP {resp.status_code}'}
    except requests.RequestException as exc:
        return {'ok': False, 'error': str(exc)}


def arena_health(force=False):
    return cached_swr('srv_arena_health_cache', fresh_ttl=30, stale_ttl=300, fetcher=_fetch_arena_health, force=force)


def runtime_system_info():
    return {
        'django_version': django.get_version(),
        'python_version': f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}',
        'platform': f'{platform.system()} {platform.release()}',
        'database_mode': 'MongoDB + Redis + SQL',
    }


class MongoAdminService:
    def __init__(self):
        self.uri = settings.MONGODB_URI
        self.db_name = settings.MONGODB_NAME
        self._client = None
        self._db = None
        self._init_lock = threading.Lock()

    @property
    def enabled(self):
        return bool(self.uri)

    def db(self):
        if not self.enabled:
            raise RuntimeError('MONGODB_URI is not configured')
        if self._db is None:
            with self._init_lock:
                if self._db is None:
                    self._client = MongoClient(
                        self.uri,
                        serverSelectionTimeoutMS=8000,
                        connectTimeoutMS=5000,
                        socketTimeoutMS=15000,
                        maxPoolSize=20,
                    )
                    if self.db_name:
                        self._db = self._client[self.db_name]
                    else:
                        self._db = self._client.get_default_database()
        return self._db

    def ping(self):
        if not self.enabled:
            return {'ok': False, 'error': 'MONGODB_URI is not configured'}
        try:
            self.db().command('ping')
            return {'ok': True}
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}

    def count(self, collection, query=None):
        return self.db()[collection].count_documents(query or {})

    def list_collection(self, collection, page=1, limit=20, query=None):
        meta = MONGO_COLLECTIONS.get(collection)
        if not meta:
            raise ValueError('Unsupported collection')

        page = max(int(page or 1), 1)
        limit = min(max(int(limit or 20), 1), 100)
        skip = (page - 1) * limit
        query = query or {}
        coll = self.db()[collection]

        def fetch_rows():
            cursor = (
                coll.find(query, meta.get('projection'))
                .sort(meta.get('sort', [('_id', -1)]))
                .skip(skip)
                .limit(limit)
            )
            return [clean_value(row) for row in cursor]

        def fetch_total():
            return coll.count_documents(query) if query else coll.estimated_document_count()

        res = parallel_run({'rows': fetch_rows, 'total': fetch_total}, max_workers=2)
        rows = res['rows']
        total = res['total']
        return {
            'rows': rows,
            'total': total,
            'page': page,
            'limit': limit,
            'has_more': skip + len(rows) < total,
        }

    def overview(self, force=False):
        if not self.enabled:
            return {'enabled': False, 'error': 'MONGODB_URI is not configured'}

        return cached_swr(
            'mongo_overview_cache',
            fresh_ttl=45,
            stale_ttl=300,
            fetcher=self._compute_overview,
            force=force,
        )

    def _compute_overview(self):
        db = self.db()
        now_utc = datetime.utcnow()
        today_start = datetime(now_utc.year, now_utc.month, now_utc.day)
        tomorrow_start = today_start + timedelta(days=1)
        prev_week_start = today_start - timedelta(days=13)

        def count_coll(coll_name):
            try:
                return db[coll_name].estimated_document_count()
            except PyMongoError:
                return 0

        def daily_counts(coll_name, date_field):
            pipeline = [
                {'$match': {date_field: {'$gte': prev_week_start, '$lt': tomorrow_start}}},
                {'$group': {
                    '_id': {'$dateToString': {'format': '%Y-%m-%d', 'date': f'${date_field}'}},
                    'count': {'$sum': 1},
                }},
            ]
            try:
                return {row['_id']: row['count'] for row in db[coll_name].aggregate(pipeline) if row.get('_id')}
            except PyMongoError:
                return {}

        def fetch_user_stats():
            res = list(db.users.aggregate([
                {'$group': {
                    '_id': None,
                    'verified': {'$sum': {'$cond': [{'$eq': ['$isEmailVerified', True]}, 1, 0]}},
                    'banned': {'$sum': {'$cond': [{'$eq': ['$isBanned', True]}, 1, 0]}},
                }}
            ]))
            return res[0] if res else {'verified': 0, 'banned': 0}

        def fetch_trending():
            items = list(db.watchprogresses.aggregate([
                {'$group': {
                    '_id': '$contentId',
                    'title': {'$first': '$title'},
                    'poster_path': {'$first': '$poster_path'},
                    'isTVShow': {'$first': '$isTVShow'},
                    'views': {'$sum': 1},
                    'lastWatched': {'$max': '$lastWatched'},
                }},
                {'$sort': {'views': -1, 'lastWatched': -1}},
                {'$limit': 5},
            ]))
            if not items:
                items = list(db.users.aggregate([
                    {'$unwind': '$watchlist'},
                    {'$group': {
                        '_id': {'$toString': '$watchlist.id'},
                        'title': {'$first': '$watchlist.title'},
                        'poster_path': {'$first': '$watchlist.poster_path'},
                        'isTVShow': {'$first': {'$eq': ['$watchlist.type', 'tv']}},
                        'views': {'$sum': 1},
                    }},
                    {'$sort': {'views': -1}},
                    {'$limit': 5},
                ]))
            return items

        tasks = {f'cnt:{c}': (lambda name=c: count_coll(name)) for c in MONGO_COLLECTIONS}
        tasks.update({
            'recent_users': lambda: list(
                db.users.find({}, {'password': 0, 'emailVerificationToken': 0, 'friends': 0, 'friendRequests': 0})
                .sort('createdAt', -1)
                .limit(5)
            ),
            'user_stats': fetch_user_stats,
            'daily_users': lambda: daily_counts('users', 'createdAt'),
            'daily_comments': lambda: daily_counts('comments', 'createdAt'),
            'daily_watches': lambda: daily_counts('watchprogresses', 'lastWatched'),
            'trending': fetch_trending,
            'recent_comments': lambda: list(db.comments.aggregate([
                {'$sort': {'createdAt': -1}},
                {'$limit': 5},
                {'$lookup': {
                    'from': 'users',
                    'localField': 'userId',
                    'foreignField': '_id',
                    'as': 'user',
                }},
                {'$unwind': {'path': '$user', 'preserveNullAndEmptyArrays': True}},
                {'$project': {
                    'movieId': 1,
                    'type': 1,
                    'content': 1,
                    'likes': 1,
                    'isDeleted': 1,
                    'createdAt': 1,
                    'user.name': 1,
                    'user.email': 1,
                }},
            ])),
            'watch_progress': lambda: list(db.watchprogresses.find({}).sort('lastWatched', -1).limit(5)),
            'search_docs': lambda: list(db.searchhistories.find({}).sort('updated_at', -1).limit(5)),
            'notifications': lambda: list(db.notifications.find({}).sort('createdAt', -1).limit(5)),
        })

        results = parallel_run(tasks, max_workers=8)
        counts = {c: results[f'cnt:{c}'] for c in MONGO_COLLECTIONS}
        users = results['recent_users']
        u_stats = results['user_stats']
        verified_users = u_stats.get('verified', 0)
        banned_users = u_stats.get('banned', 0)
        unverified_users = max(counts.get('users', 0) - verified_users, 0)
        total_watchlist_items = sum(len(user.get('watchlist', [])) for user in users)

        daily_u = results['daily_users']
        daily_c = results['daily_comments']
        daily_w = results['daily_watches']

        day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        weekly_days = []
        current_week_total = 0
        for offset in range(6, -1, -1):
            d_start = today_start - timedelta(days=offset)
            d_str = d_start.strftime('%Y-%m-%d')
            u_cnt = daily_u.get(d_str, 0)
            c_cnt = daily_c.get(d_str, 0)
            w_cnt = daily_w.get(d_str, 0)
            day_total = u_cnt + c_cnt + w_cnt
            current_week_total += day_total
            weekly_days.append({
                'date': d_str,
                'label': day_names[d_start.weekday()],
                'new_users': u_cnt,
                'comments': c_cnt,
                'watches': w_cnt,
                'total': day_total,
            })

        prev_week_total = 0
        for offset in range(13, 6, -1):
            d_str = (today_start - timedelta(days=offset)).strftime('%Y-%m-%d')
            prev_week_total += daily_u.get(d_str, 0) + daily_c.get(d_str, 0) + daily_w.get(d_str, 0)

        if prev_week_total > 0:
            growth_pct = round(((current_week_total - prev_week_total) / prev_week_total) * 100, 1)
        else:
            growth_pct = 100.0 if current_week_total > 0 else 0.0

        return {
            'enabled': True,
            'counts': counts,
            'stats': {
                'verified_users': verified_users,
                'unverified_users': unverified_users,
                'banned_users': banned_users,
                'total_watchlist_items_recent_users': total_watchlist_items,
                'weekly_growth_pct': growth_pct,
                'weekly_total_events': current_week_total,
            },
            'weekly_activity': weekly_days,
            'trending_media': clean_value(results['trending']),
            'recent_users': clean_value(users),
            'recent_comments': clean_value(results['recent_comments']),
            'watch_progress': clean_value(results['watch_progress']),
            'search_histories': clean_value(results['search_docs']),
            'notifications': clean_value(results['notifications']),
        }

    def object_id(self, value):
        try:
            return ObjectId(value)
        except (InvalidId, TypeError):
            raise ValueError('Invalid object id')

    def _collect_comment_branch_ids(self, db, root_comment):
        root_oid = root_comment['_id']
        if not root_comment.get('parentId'):
            replies = list(db.comments.find({'parentId': root_oid}, {'_id': 1}))
            return [root_oid] + [r['_id'] for r in replies]

        ids = [root_oid]
        seen = {root_oid}
        frontier = [root_oid]
        while frontier:
            children = list(db.comments.find(
                {'parentId': root_comment.get('parentId'), 'replyToComment': {'$in': frontier}},
                {'_id': 1},
            ))
            next_frontier = []
            for child in children:
                cid = child['_id']
                if cid not in seen:
                    seen.add(cid)
                    ids.append(cid)
                    next_frontier.append(cid)
            frontier = next_frontier
        return ids

    @staticmethod
    def sync_comment_parent_reply_count(db, parent_oid, pulled_ids=None):
        if not parent_oid:
            return
        update_doc = {
            '$set': {
                'replyCount': db.comments.count_documents({'parentId': parent_oid, 'isDeleted': {'$ne': True}}),
            }
        }
        if pulled_ids:
            update_doc['$pull'] = {'replies': {'$in': list(pulled_ids)}}
        db.comments.update_one({'_id': parent_oid}, update_doc)

    def delete_document(self, collection, document_id):
        if collection not in MONGO_COLLECTIONS or collection == 'users':
            raise ValueError('Unsupported delete target')

        oid = self.object_id(document_id)
        db = self.db()

        if collection == 'comments':
            comment = db.comments.find_one({'_id': oid})
            if not comment:
                return 0

            ids = self._collect_comment_branch_ids(db, comment)
            parent_oid = comment.get('parentId')
            db.notifications.delete_many({
                '$or': [
                    {'metadata.commentId': {'$in': ids}},
                    {'metadata.parentCommentId': {'$in': ids}},
                ]
            })
            result = db.comments.delete_many({'_id': {'$in': ids}})
            if parent_oid:
                self.sync_comment_parent_reply_count(db, parent_oid, pulled_ids=ids)
            invalidate_comment_cache()
            return result.deleted_count

        result = db[collection].delete_one({'_id': oid})
        if collection == 'userreports':
            invalidate_admin_report_cache()
        return result.deleted_count

    def set_comment_deleted(self, comment_id, is_deleted=True):
        oid = self.object_id(comment_id)
        db = self.db()
        comment = db.comments.find_one({'_id': oid}, {'_id': 1, 'parentId': 1})
        if not comment:
            return 0
        result = db.comments.update_one(
            {'_id': oid},
            {'$set': {'isDeleted': bool(is_deleted)}},
        )
        if comment.get('parentId'):
            self.sync_comment_parent_reply_count(db, comment.get('parentId'))
        invalidate_comment_cache()
        return result.modified_count

    def set_notification_read(self, notification_id, read=True):
        oid = self.object_id(notification_id)
        update = {'read': bool(read), 'readAt': datetime.utcnow() if read else None}
        result = self.db().notifications.update_one({'_id': oid}, {'$set': update})
        return result.modified_count


_HOMEPAGE_COMMENT_LIMITS = (4, 5, 8, 9, 10, 12, 15, 20, 50)
_HOMEPAGE_COMMENT_SORTS = ('likes', 'replies')


def invalidate_admin_report_cache():
    """Clear SWR caches for admin sidebar counts and topbar notification bell."""
    for base in ('admin_sidebar_counts', 'admin_bell_notifs_v1'):
        cache.delete(f'{base}:fresh')
        cache.delete(f'{base}:stale')


def invalidate_comment_cache():
    """Best-effort invalidation of homepage comment caches in Redis / Node server."""
    upstash_url = getattr(settings, 'UPSTASH_REDIS_ROOMS_URL', '') or ''
    upstash_token = getattr(settings, 'UPSTASH_REDIS_ROOMS_TOKEN', '') or ''
    if upstash_url and upstash_token:
        keys = [f'homepage:comments:recent:{lim}' for lim in _HOMEPAGE_COMMENT_LIMITS]
        for sort_key in _HOMEPAGE_COMMENT_SORTS:
            for lim in _HOMEPAGE_COMMENT_LIMITS:
                keys.append(f'homepage:comments:top:{sort_key}:{lim}')
        try:
            requests.post(
                f'{upstash_url.rstrip("/")}/pipeline',
                headers={'Authorization': f'Bearer {upstash_token}', 'Content-Type': 'application/json'},
                json=[['DEL', key] for key in keys],
                timeout=(1, 2),
            )
        except Exception:
            pass

    admin_key = getattr(settings, 'ADMIN_API_KEY', '') or ''
    server_url = getattr(settings, 'SERVER_URL', '') or ''
    if admin_key and server_url and 'test' not in sys.argv:
        try:
            requests.post(
                f'{server_url.rstrip("/")}/api/comments/admin/invalidate-cache',
                headers={'x-admin-api-key': admin_key},
                timeout=(1, 2),
            )
        except Exception:
            pass


mongo_admin = MongoAdminService()
