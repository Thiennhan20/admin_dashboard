"""Shared API endpoints – bulk actions, ban/unban, admin notifications."""
import json

from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from users_management.models import AuditLog
from users_management.services import mongo_admin, clean_value
from users_management.views_legacy import check_admin_access

__all__ = ['api_bulk_action', 'api_user_ban']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_http_methods(["POST"])
def api_bulk_action(request):
    """Generic bulk action endpoint for MongoDB collections."""
    body = json.loads(request.body)
    collection = body.get('collection', '')
    action = body.get('action', '')
    ids = body.get('ids', [])

    if not ids:
        return JsonResponse({'success': False, 'error': 'No IDs provided'})

    allowed = {
        'users': ['delete', 'delete_watchlist_items'],
        'comments': ['delete', 'hide', 'restore'],
        'notifications': ['delete', 'read', 'unread'],
        'watchprogresses': ['delete'],
        'searchhistories': ['delete'],
    }

    if collection not in allowed or action not in allowed[collection]:
        return JsonResponse({'success': False, 'error': 'Invalid collection/action'})

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
                # Cascading delete of user relations
                db.comments.delete_many({'userId': {'$in': oids}})
                db.searchhistories.delete_many({'userId': {'$in': oids}})
                db.watchprogresses.delete_many({'userId': {'$in': oids}})
            elif collection == 'comments':
                # Also delete notifications related to comments
                db.notifications.delete_many({
                    '$or': [
                        {'metadata.commentId': {'$in': oids}},
                        {'metadata.parentCommentId': {'$in': oids}},
                    ]
                })
                # Delete replies
                db.comments.delete_many({'parentId': {'$in': oids}})
            result = db[collection].delete_many({'_id': {'$in': oids}})
            count = result.deleted_count

        elif action in ('hide', 'restore'):
            is_deleted = action == 'hide'
            result = db.comments.update_many(
                {'_id': {'$in': oids}},
                {'$set': {'isDeleted': is_deleted}},
            )
            count = result.modified_count

        elif action in ('read', 'unread'):
            from datetime import datetime
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
@require_http_methods(["POST"])
def api_user_ban(request):
    """Ban or unban a user in MongoDB."""
    body = json.loads(request.body)
    user_id = body.get('user_id', '')
    action = body.get('action', '')  # 'ban' or 'unban'
    reason = body.get('reason', '')

    if not user_id or action not in ('ban', 'unban'):
        return JsonResponse({'success': False, 'error': 'Invalid parameters'})

    try:
        from bson import ObjectId
        from datetime import datetime
        db = mongo_admin.db()
        oid = ObjectId(user_id)

        if action == 'ban':
            update = {
                'isBanned': True,
                'banReason': reason,
                'bannedAt': datetime.utcnow(),
                'bannedBy': request.user.username,
            }
        else:
            update = {
                'isBanned': False,
                'banReason': '',
                'bannedAt': None,
                'bannedBy': '',
            }

        result = db.users.update_one({'_id': oid}, {'$set': update})
        AuditLog.log(request, f'{action}_user', 'user', user_id, {'reason': reason})
        return JsonResponse({'success': True, 'modified': result.modified_count})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
