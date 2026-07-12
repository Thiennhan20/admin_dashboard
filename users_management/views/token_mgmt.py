"""Password Reset Tokens & Blacklisted Tokens management views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import render
import json

from users_management.models import AuditLog
from users_management.services import mongo_admin, clean_value
from users_management.views_legacy import check_admin_access, check_api_status

__all__ = [
    'password_resets_list', 'blacklisted_tokens_list',
    'api_tokens_cleanup', 'api_tokens_delete',
]


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def password_resets_list(request):
    page = int(request.GET.get('page', 1))
    limit = 20
    try:
        db = mongo_admin.db()
        total = db.passwordresettokens.count_documents({})
        total_pages = max(1, -(-total // limit))
        items = clean_value(list(
            db.passwordresettokens.find({}, {'tokenHash': 0})
            .sort('createdAt', -1)
            .skip((page - 1) * limit)
            .limit(limit)
        ))
        for item in items:
            item['id'] = item.get('_id')
        context = {
            'items': items, 'page': page, 'total': total,
            'total_pages': total_pages, 'api_status': check_api_status(),
        }
    except Exception as e:
        context = {
            'items': [], 'page': 1, 'total': 0, 'total_pages': 1,
            'error': str(e), 'api_status': {'status': 'offline'},
        }
    return render(request, 'admin_dashboard/tokens/password_resets.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def blacklisted_tokens_list(request):
    page = int(request.GET.get('page', 1))
    limit = 20
    try:
        db = mongo_admin.db()
        total = db.blacklistedtokens.count_documents({})
        total_pages = max(1, -(-total // limit))
        items = clean_value(list(
            db.blacklistedtokens.find({}, {'token': 0})
            .sort('createdAt', -1)
            .skip((page - 1) * limit)
            .limit(limit)
        ))
        for item in items:
            item['id'] = item.get('_id')
        context = {
            'items': items, 'page': page, 'total': total,
            'total_pages': total_pages, 'api_status': check_api_status(),
        }
    except Exception as e:
        context = {
            'items': [], 'page': 1, 'total': 0, 'total_pages': 1,
            'error': str(e), 'api_status': {'status': 'offline'},
        }
    return render(request, 'admin_dashboard/tokens/blacklisted_tokens.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def api_tokens_cleanup(request):
    if request.method != 'POST':
        return JsonResponse({'success': False}, status=405)
    body = json.loads(request.body)
    collection = body.get('collection', '')
    try:
        from datetime import datetime, timedelta
        db = mongo_admin.db()
        cutoff = datetime.utcnow() - timedelta(hours=24)
        if collection == 'passwordresettokens':
            result = db.passwordresettokens.delete_many({'expiresAt': {'$lt': cutoff}})
        elif collection == 'blacklistedtokens':
            result = db.blacklistedtokens.delete_many({'expiresAt': {'$lt': cutoff}})
        else:
            return JsonResponse({'success': False, 'error': 'Invalid collection'})
        AuditLog.log(request, f'cleanup_{collection}', collection, '', {'deleted': result.deleted_count})
        return JsonResponse({'success': True, 'deleted': result.deleted_count})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def api_tokens_delete(request):
    if request.method != 'POST':
        return JsonResponse({'success': False}, status=405)
    body = json.loads(request.body)
    collection = body.get('collection', '')
    ids = body.get('ids', [])
    try:
        from bson import ObjectId
        db = mongo_admin.db()
        coll = db[collection] if collection in ('passwordresettokens', 'blacklistedtokens') else None
        if not coll:
            return JsonResponse({'success': False, 'error': 'Invalid collection'})
        oids = [ObjectId(i) for i in ids]
        result = coll.delete_many({'_id': {'$in': oids}})
        AuditLog.log(request, f'delete_{collection}', collection, '', {'count': result.deleted_count})
        return JsonResponse({'success': True, 'deleted': result.deleted_count})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
