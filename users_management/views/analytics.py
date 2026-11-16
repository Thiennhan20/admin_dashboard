"""Analytics & Business metrics views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render
from datetime import datetime, timedelta

from users_management.decorators import require_permission
from users_management.services import mongo_admin, clean_value, parallel_run, cached_swr
from users_management.views_legacy import check_admin_access

__all__ = ['analytics_dashboard', 'api_analytics_data']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('analytics.view')
def analytics_dashboard(request):
    return render(request, 'admin_dashboard/analytics/analytics.html')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('analytics.view')
def api_analytics_data(request):
    """API endpoint returning chart data for the analytics dashboard."""
    try:
        db = mongo_admin.db()
        now = datetime.utcnow()
        days_param = request.GET.get('days', '30')
        if days_param == '7':
            days_limit = 7
        elif days_param == 'this_month':
            days_limit = max(1, (now - now.replace(day=1)).days + 1)
        else:
            try:
                days_limit = int(days_param)
            except ValueError:
                days_limit = 30
        
        start_date = now - timedelta(days=days_limit)
        force = request.GET.get('refresh') == '1'

        def _compute_analytics():
            reg_pipeline = [
                {'$match': {'createdAt': {'$gte': start_date}}},
                {'$group': {
                    '_id': {'$dateToString': {'format': '%Y-%m-%d', 'date': '$createdAt'}},
                    'count': {'$sum': 1},
                }},
                {'$sort': {'_id': 1}},
            ]
            wl_facet_pipeline = [
                {'$match': {'watchlist.0': {'$exists': True}}},
                {'$project': {'watchlist': 1}},
                {'$unwind': '$watchlist'},
                {'$facet': {
                    'top_watchlisted': [
                        {'$group': {
                            '_id': '$watchlist.id',
                            'title': {'$first': '$watchlist.title'},
                            'count': {'$sum': 1},
                        }},
                        {'$sort': {'count': -1}},
                        {'$limit': 10},
                    ],
                    'content_types': [
                        {'$group': {
                            '_id': '$watchlist.type',
                            'count': {'$sum': 1},
                        }},
                    ],
                }},
            ]
            user_ver_pipeline = [
                {'$group': {
                    '_id': None,
                    'total': {'$sum': 1},
                    'verified': {'$sum': {'$cond': [{'$eq': ['$isEmailVerified', True]}, 1, 0]}},
                    'google': {'$sum': {'$cond': [{'$eq': ['$authType', 'google']}, 1, 0]}},
                    'banned': {'$sum': {'$cond': [{'$eq': ['$isBanned', True]}, 1, 0]}},
                }},
            ]
            comments_facet_pipeline = [
                {'$match': {'createdAt': {'$gte': start_date}}},
                {'$facet': {
                    'by_hour': [
                        {'$group': {
                            '_id': {'$hour': '$createdAt'},
                            'count': {'$sum': 1},
                        }},
                        {'$sort': {'_id': 1}},
                    ],
                    'per_day': [
                        {'$group': {
                            '_id': {'$dateToString': {'format': '%Y-%m-%d', 'date': '$createdAt'}},
                            'count': {'$sum': 1},
                        }},
                        {'$sort': {'_id': 1}},
                    ],
                    'heatmap': [
                        {'$group': {
                            '_id': {
                                'dow': {'$dayOfWeek': '$createdAt'},
                                'hour': {'$hour': '$createdAt'},
                            },
                            'count': {'$sum': 1},
                        }},
                    ],
                }},
            ]
            wp_pct_expr = {
                '$cond': [
                    {'$gt': [{'$ifNull': ['$duration', 0]}, 0]},
                    {'$divide': [{'$ifNull': ['$currentTime', 0]}, '$duration']},
                    0,
                ]
            }
            wp_stats_pipeline = [
                {'$project': {
                    'isTVShow': 1,
                    'audio': 1,
                    'pct': wp_pct_expr,
                }},
                {'$group': {
                    '_id': None,
                    'total': {'$sum': 1},
                    'tv_shows': {'$sum': {'$cond': [{'$eq': ['$isTVShow', True]}, 1, 0]}},
                    'movies': {'$sum': {'$cond': [{'$ne': ['$isTVShow', True]}, 1, 0]}},
                    'vietsub': {'$sum': {'$cond': [{'$eq': ['$audio', 'vietsub']}, 1, 0]}},
                    'dubbed': {'$sum': {'$cond': [{'$eq': ['$audio', 'dubbed']}, 1, 0]}},
                    'comp_low': {'$sum': {'$cond': [{'$lt': ['$pct', 0.25]}, 1, 0]}},
                    'comp_mid': {'$sum': {'$cond': [{'$and': [{'$gte': ['$pct', 0.25]}, {'$lt': ['$pct', 0.75]}]}, 1, 0]}},
                    'comp_high': {'$sum': {'$cond': [{'$gte': ['$pct', 0.75]}, 1, 0]}},
                }},
            ]
            notif_pipeline = [
                {'$group': {
                    '_id': '$type',
                    'total': {'$sum': 1},
                    'unread': {'$sum': {'$cond': [{'$eq': ['$read', False]}, 1, 0]}},
                }},
                {'$sort': {'total': -1}},
            ]
            stream_pipeline = [
                {'$group': {
                    '_id': {'$ifNull': ['$audio', 'vietsub']},
                    'links': {'$sum': 1},
                    'plays': {'$sum': {'$ifNull': ['$createdCount', 1]}},
                }},
            ]

            d1_cut = now - timedelta(days=1)
            d7_cut = now - timedelta(days=7)
            d14_cut = now - timedelta(days=14)
            d30_cut = now - timedelta(days=30)

            def _distinct_active(since_dt):
                ids = set(db.watchprogresses.distinct('userId', {'lastWatched': {'$gte': since_dt}}))
                ids.update(db.comments.distinct('userId', {'createdAt': {'$gte': since_dt}}))
                ids.update(db.sessions.distinct('userId', {'lastActive': {'$gte': since_dt}}))
                return len(ids)

            res = parallel_run({
                'reg': lambda: clean_value(list(db.users.aggregate(reg_pipeline))),
                'wl_facet': lambda: list(db.users.aggregate(wl_facet_pipeline)),
                'user_ver': lambda: list(db.users.aggregate(user_ver_pipeline)),
                'comments_facet': lambda: list(db.comments.aggregate(comments_facet_pipeline)),
                'wp_stats': lambda: list(db.watchprogresses.aggregate(wp_stats_pipeline)),
                'notif_stats': lambda: clean_value(list(db.notifications.aggregate(notif_pipeline))),
                'stream_stats': lambda: clean_value(list(db.streamhistories.aggregate(stream_pipeline))),
                'dau': lambda: _distinct_active(d1_cut),
                'wau': lambda: _distinct_active(d7_cut),
                'mau': lambda: _distinct_active(d30_cut),
                'active_sessions': lambda: db.sessions.count_documents({'$or': [{'expiresAt': {'$gt': now}}, {'expiresAt': None}]}),
                'reg_curr_7d': lambda: db.users.count_documents({'createdAt': {'$gte': d7_cut}}),
                'reg_prev_7d': lambda: db.users.count_documents({'createdAt': {'$gte': d14_cut, '$lt': d7_cut}}),
                'cm_curr_7d': lambda: db.comments.count_documents({'createdAt': {'$gte': d7_cut}}),
                'cm_prev_7d': lambda: db.comments.count_documents({'createdAt': {'$gte': d14_cut, '$lt': d7_cut}}),
            }, max_workers=6)

            wl_doc = res['wl_facet'][0] if res['wl_facet'] else {}
            uv_doc = res['user_ver'][0] if res['user_ver'] else {}
            cm_doc = res['comments_facet'][0] if res['comments_facet'] else {}
            wp_doc = res['wp_stats'][0] if res['wp_stats'] else {}

            total_users = uv_doc.get('total', 0) or 0
            verified = uv_doc.get('verified', 0) or 0
            google_cnt = uv_doc.get('google', 0) or 0

            # Build 7x24 matrix (Mon..Sun x 0..23h); MongoDB $dayOfWeek is 1=Sun..7=Sat
            heatmap_matrix = [[0] * 24 for _ in range(7)]
            for cell in (cm_doc.get('heatmap') or []):
                cell_id = cell.get('_id') or {}
                dow = int(cell_id.get('dow') or 1)
                hour = int(cell_id.get('hour') or 0)
                if 1 <= dow <= 7 and 0 <= hour < 24:
                    mon_idx = (dow - 2) % 7
                    heatmap_matrix[mon_idx][hour] = int(cell.get('count') or 0)

            reg_curr = res['reg_curr_7d'] or 0
            reg_prev = res['reg_prev_7d'] or 0
            reg_wow_pct = round(((reg_curr - reg_prev) / reg_prev) * 100, 1) if reg_prev > 0 else (100.0 if reg_curr > 0 else 0.0)
            cm_curr = res['cm_curr_7d'] or 0
            cm_prev = res['cm_prev_7d'] or 0
            cm_wow_pct = round(((cm_curr - cm_prev) / cm_prev) * 100, 1) if cm_prev > 0 else (100.0 if cm_curr > 0 else 0.0)

            return {
                'success': True,
                'kpis': {
                    'dau': res['dau'],
                    'wau': res['wau'],
                    'mau': res['mau'],
                    'active_sessions': res['active_sessions'],
                    'reg_curr_7d': reg_curr,
                    'reg_prev_7d': reg_prev,
                    'reg_wow_pct': reg_wow_pct,
                    'cm_curr_7d': cm_curr,
                    'cm_prev_7d': cm_prev,
                    'cm_wow_pct': cm_wow_pct,
                },
                'heatmap': heatmap_matrix,
                'registrations': res['reg'],
                'top_watchlisted': clean_value(wl_doc.get('top_watchlisted', [])),
                'user_verification': {
                    'verified': verified,
                    'unverified': total_users - verified,
                    'total': total_users,
                },
                'auth_types': {
                    'google': google_cnt,
                    'email': max(0, total_users - google_cnt),
                    'banned': uv_doc.get('banned', 0) or 0,
                },
                'activity_by_hour': clean_value(cm_doc.get('by_hour', [])),
                'comments_per_day': clean_value(cm_doc.get('per_day', [])),
                'watch_progress': {
                    'total': wp_doc.get('total', 0) or 0,
                    'movies': wp_doc.get('movies', 0) or 0,
                    'tv_shows': wp_doc.get('tv_shows', 0) or 0,
                },
                'watch_audio': {
                    'vietsub': wp_doc.get('vietsub', 0) or 0,
                    'dubbed': wp_doc.get('dubbed', 0) or 0,
                },
                'watch_completion': {
                    'low': wp_doc.get('comp_low', 0) or 0,
                    'mid': wp_doc.get('comp_mid', 0) or 0,
                    'high': wp_doc.get('comp_high', 0) or 0,
                },
                'notif_stats': res['notif_stats'],
                'stream_summary': res['stream_stats'],
                'content_types': clean_value(wl_doc.get('content_types', [])),
            }

        payload = cached_swr(
            f'analytics_data_v3:{days_limit}',
            fresh_ttl=60,
            stale_ttl=300,
            fetcher=_compute_analytics,
            force=force,
        )
        return JsonResponse(payload)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
