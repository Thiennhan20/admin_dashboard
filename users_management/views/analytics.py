"""Analytics & Business metrics views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render
from datetime import datetime, timedelta

from users_management.services import mongo_admin, clean_value
from users_management.views_legacy import check_admin_access

__all__ = ['analytics_dashboard', 'api_analytics_data']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def analytics_dashboard(request):
    return render(request, 'admin_dashboard/analytics/analytics.html')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
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

        # 1. User registrations per day
        reg_pipeline = [
            {'$match': {'createdAt': {'$gte': start_date}}},
            {'$group': {
                '_id': {'$dateToString': {'format': '%Y-%m-%d', 'date': '$createdAt'}},
                'count': {'$sum': 1},
            }},
            {'$sort': {'_id': 1}},
        ]
        registrations = clean_value(list(db.users.aggregate(reg_pipeline)))

        # 2. Top 10 most watchlisted content
        watchlist_pipeline = [
            {'$unwind': '$watchlist'},
            {'$group': {
                '_id': '$watchlist.id',
                'title': {'$first': '$watchlist.title'},
                'count': {'$sum': 1},
            }},
            {'$sort': {'count': -1}},
            {'$limit': 10},
        ]
        top_watchlisted = clean_value(list(db.users.aggregate(watchlist_pipeline)))

        # 3. Verified vs unverified
        total_users = db.users.count_documents({})
        verified = db.users.count_documents({'isEmailVerified': True})

        # 4. Activity by hour (comments in selected range)
        hour_pipeline = [
            {'$match': {'createdAt': {'$gte': start_date}}},
            {'$group': {
                '_id': {'$hour': '$createdAt'},
                'count': {'$sum': 1},
            }},
            {'$sort': {'_id': 1}},
        ]
        activity_by_hour = clean_value(list(db.comments.aggregate(hour_pipeline)))

        # 5. Comments per day
        comments_pipeline = [
            {'$match': {'createdAt': {'$gte': start_date}}},
            {'$group': {
                '_id': {'$dateToString': {'format': '%Y-%m-%d', 'date': '$createdAt'}},
                'count': {'$sum': 1},
            }},
            {'$sort': {'_id': 1}},
        ]
        comments_per_day = clean_value(list(db.comments.aggregate(comments_pipeline)))

        # 6. Watch progress stats
        wp_total = db.watchprogresses.count_documents({})
        wp_movie = db.watchprogresses.count_documents({'isTVShow': {'$ne': True}})
        wp_tv = db.watchprogresses.count_documents({'isTVShow': True})

        # 7. Content type distribution in watchlists
        type_pipeline = [
            {'$unwind': '$watchlist'},
            {'$group': {
                '_id': '$watchlist.type',
                'count': {'$sum': 1},
            }},
        ]
        content_types = clean_value(list(db.users.aggregate(type_pipeline)))

        return JsonResponse({
            'success': True,
            'registrations': registrations,
            'top_watchlisted': top_watchlisted,
            'user_verification': {'verified': verified, 'unverified': total_users - verified, 'total': total_users},
            'activity_by_hour': activity_by_hour,
            'comments_per_day': comments_per_day,
            'watch_progress': {'total': wp_total, 'movies': wp_movie, 'tv_shows': wp_tv},
            'content_types': content_types,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
