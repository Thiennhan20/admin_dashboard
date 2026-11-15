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

            res = parallel_run({
                'reg': lambda: clean_value(list(db.users.aggregate(reg_pipeline))),
                'wl_facet': lambda: list(db.users.aggregate(wl_facet_pipeline)),
                'user_ver': lambda: list(db.users.aggregate(user_ver_pipeline)),
                'comments_facet': lambda: list(db.comments.aggregate(comments_facet_pipeline)),
                'wp_stats': lambda: list(db.watchprogresses.aggregate(wp_stats_pipeline)),
                'notif_stats': lambda: clean_value(list(db.notifications.aggregate(notif_pipeline))),
                'stream_stats': lambda: clean_value(list(db.streamhistories.aggregate(stream_pipeline))),
            }, max_workers=6)

            wl_doc = res['wl_facet'][0] if res['wl_facet'] else {}
            uv_doc = res['user_ver'][0] if res['user_ver'] else {}
            cm_doc = res['comments_facet'][0] if res['comments_facet'] else {}
            wp_doc = res['wp_stats'][0] if res['wp_stats'] else {}

            total_users = uv_doc.get('total', 0) or 0
            verified = uv_doc.get('verified', 0) or 0
            google_cnt = uv_doc.get('google', 0) or 0

            return {
                'success': True,
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
            f'analytics_data_v2:{days_limit}',
            fresh_ttl=60,
            stale_ttl=300,
            fetcher=_compute_analytics,
            force=force,
        )
        return JsonResponse(payload)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
