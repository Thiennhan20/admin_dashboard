"""Shared API endpoints – bulk actions, ban/unban, admin notifications."""
import json

from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from users_management.decorators import require_permission
from users_management.models import AuditLog
from users_management.rate_limit import consume_action_rate_limit
from users_management.services import mongo_admin, clean_value, invalidate_comment_cache
from users_management.views_legacy import check_admin_access
from users_management.webhook_dispatcher import dispatch_webhook_event

__all__ = ['api_bulk_action', 'api_user_ban']

_BULK_PERM_MAP = {
    ('users', 'delete'): 'users.delete',
    ('users', 'delete_watchlist_items'): 'watchlist.delete',
    ('sessions', 'delete'): 'sessions.delete',
    ('comments', 'delete'): 'comments.delete',
    ('comments', 'hide'): 'comments.edit',
    ('comments', 'restore'): 'comments.edit',
    ('notifications', 'delete'): 'notifications.delete',
    ('notifications', 'read'): 'notifications.edit',
    ('notifications', 'unread'): 'notifications.edit',
    ('watchprogresses', 'delete'): 'watch_progress.delete',
    ('searchhistories', 'delete'): 'search_history.delete',
}


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_http_methods(["POST"])
def api_bulk_action(request):
    """Generic bulk action endpoint for MongoDB collections."""
    body = json.loads(request.body or '{}')
    collection = body.get('collection', '')
    action = body.get('action', '')
    ids = body.get('ids', [])

    if not ids:
        return JsonResponse({'success': False, 'error': 'No IDs provided'})

    allowed = {
        'users': ['delete', 'delete_watchlist_items'],
        'sessions': ['delete'],
        'comments': ['delete', 'hide', 'restore'],
        'notifications': ['delete', 'read', 'unread'],
        'watchprogresses': ['delete'],
        'searchhistories': ['delete'],
    }

    if collection not in allowed or action not in allowed[collection]:
        return JsonResponse({'success': False, 'error': 'Invalid collection/action'})

    req_perm = _BULK_PERM_MAP.get((collection, action))
    if not request.user.is_superuser:
        profile = getattr(request.user, 'admin_profile', None)
        if not (profile and req_perm and profile.has_perm(req_perm)):
            return JsonResponse({'success': False, 'error': f'Permission denied ({req_perm})'}, status=403)

    allowed_rl, retry_after, _ = consume_action_rate_limit('admin_sensitive', request)
    if not allowed_rl:
        return JsonResponse(
            {'success': False, 'error': f'Rate limit exceeded. Retry in {retry_after}s', 'retryAfter': retry_after},
            status=429,
        )

    try:
        from bson import ObjectId
        db = mongo_admin.db()
        count = 0

        if action == 'delete_watchlist_items':
            for item_id in ids:
                try:
                    u_id, m_id = item_id.split('_', 1)
                    try:
                        movie_id = int(m_id)
                    except ValueError:
                        movie_id = m_id
                    db.users.update_one(
                        {'_id': ObjectId(u_id)},
                        {'$pull': {'watchlist': {'id': movie_id}}}
                    )
                    if isinstance(movie_id, int):
                        db.users.update_one(
                            {'_id': ObjectId(u_id)},
                            {'$pull': {'watchlist': {'id': str(movie_id)}}}
                        )
                except Exception:
                    pass
            count = len(ids)

        elif action == 'delete':
            oids = [ObjectId(i) for i in ids]
            if collection == 'users':
                # Complete cascading delete of user relations
                db.sessions.delete_many({'userId': {'$in': oids}})
                db.comments.delete_many({'userId': {'$in': oids}})
                db.searchhistories.delete_many({'$or': [{'user_id': {'$in': oids}}, {'userId': {'$in': oids}}]})
                db.watchprogresses.delete_many({'userId': {'$in': oids}})
                db.notifications.delete_many({'$or': [{'recipient': {'$in': oids}}, {'sender': {'$in': oids}}]})
                db.passwordresettokens.delete_many({'user': {'$in': oids}})
                db.users.update_many(
                    {},
                    {'$pull': {'friends': {'$in': oids}, 'friendRequests': {'$in': oids}, 'sentFriendRequests': {'$in': oids}}}
                )
            elif collection == 'comments':
                # Collect affected parent threads and delete replies/notifications cleanly
                parent_oids = set()
                delete_oids = set(oids)
                for c in db.comments.find({'_id': {'$in': oids}}, {'_id': 1, 'parentId': 1}):
                    if c.get('parentId'):
                        parent_oids.add(c['parentId'])
                for r in db.comments.find({'parentId': {'$in': oids}}, {'_id': 1}):
                    delete_oids.add(r['_id'])
                all_delete_oids = list(delete_oids)
                db.notifications.delete_many({
                    '$or': [
                        {'metadata.commentId': {'$in': all_delete_oids}},
                        {'metadata.parentCommentId': {'$in': all_delete_oids}},
                    ]
                })
                result = db.comments.delete_many({'_id': {'$in': all_delete_oids}})
                for p_oid in parent_oids:
                    if p_oid not in delete_oids:
                        mongo_admin.sync_comment_parent_reply_count(db, p_oid, pulled_ids=all_delete_oids)
                invalidate_comment_cache()
                count = result.deleted_count
                AuditLog.log(request, f'bulk_{action}', collection, '', {'ids': ids, 'count': count})
                dispatch_webhook_event('comment.deleted', {'count': count, 'ids': ids[:20], 'admin': request.user.username})
                return JsonResponse({'success': True, 'count': count})
            result = db[collection].delete_many({'_id': {'$in': oids}})
            count = result.deleted_count
            if collection == 'users' and count:
                dispatch_webhook_event('user.deleted', {'count': count, 'ids': ids[:20], 'admin': request.user.username})

        elif action in ('hide', 'restore'):
            oids = [ObjectId(i) for i in ids]
            is_deleted = action == 'hide'
            parent_oids = {
                c['parentId']
                for c in db.comments.find({'_id': {'$in': oids}}, {'parentId': 1})
                if c.get('parentId')
            }
            result = db.comments.update_many(
                {'_id': {'$in': oids}},
                {'$set': {'isDeleted': is_deleted}},
            )
            for p_oid in parent_oids:
                mongo_admin.sync_comment_parent_reply_count(db, p_oid)
            invalidate_comment_cache()
            count = result.modified_count
            if is_deleted and count:
                dispatch_webhook_event('comment.hidden', {'count': count, 'ids': ids[:20], 'admin': request.user.username})

        elif action in ('read', 'unread'):
            from datetime import datetime
            oids = [ObjectId(i) for i in ids]
            is_read = action == 'read'
            update = {'read': is_read}
            if is_read:
                update['readAt'] = datetime.utcnow()
            else:
                update['readAt'] = None
            result = db.notifications.update_many(
                {'_id': {'$in': oids}},
                {'$set': update},
            )
            count = result.modified_count

        AuditLog.log(request, f'bulk_{action}', collection, '', {'ids': ids, 'count': count})
        return JsonResponse({'success': True, 'count': count})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('users.edit')
@require_http_methods(["POST"])
def api_user_ban(request):
    """Ban or unban a user in MongoDB (supports optional temporary ban `duration_hours`)."""
    allowed_rl, retry_after, _ = consume_action_rate_limit('admin_sensitive', request)
    if not allowed_rl:
        return JsonResponse(
            {'success': False, 'error': f'Rate limit exceeded. Retry in {retry_after}s', 'retryAfter': retry_after},
            status=429,
        )

    body = json.loads(request.body or '{}')
    user_id = body.get('user_id', '')
    action = body.get('action', '')  # 'ban' or 'unban'
    reason = body.get('reason', '')
    try:
        duration_hours = int(body.get('duration_hours') or 0)
    except (TypeError, ValueError):
        duration_hours = 0

    if not user_id or action not in ('ban', 'unban'):
        return JsonResponse({'success': False, 'error': 'Invalid parameters'})

    try:
        from bson import ObjectId
        from datetime import datetime, timedelta
        db = mongo_admin.db()
        oid = ObjectId(user_id)
        now = datetime.utcnow()

        if action == 'ban':
            ban_expires = (now + timedelta(hours=duration_hours)) if duration_hours > 0 else None
            update = {
                'isBanned': True,
                'banReason': reason,
                'bannedAt': now,
                'bannedBy': request.user.username,
                'banExpiresAt': ban_expires,
            }
            # Immediately revoke all active sessions of the banned user
            db.sessions.delete_many({'userId': oid})
        else:
            ban_expires = None
            update = {
                'isBanned': False,
                'banReason': '',
                'bannedAt': None,
                'bannedBy': '',
                'banExpiresAt': None,
            }

        result = db.users.update_one({'_id': oid}, {'$set': update})
        AuditLog.log(request, f'{action}_user', 'user', user_id, {
            'reason': reason,
            'duration_hours': duration_hours or None,
        })
        if action == 'ban':
            dispatch_webhook_event('user.banned', {
                'user_id': user_id,
                'reason': reason,
                'duration_hours': duration_hours or None,
                'admin': request.user.username,
            })
        return JsonResponse({
            'success': True,
            'modified': result.modified_count,
            'banExpiresAt': ban_expires.isoformat() + 'Z' if ban_expires else None,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
