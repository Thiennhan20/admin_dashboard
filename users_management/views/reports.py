"""User reports management views (MongoDB-backed manual moderation queue)."""
from datetime import datetime, timezone
import re
from urllib.parse import urlencode

from bson import ObjectId
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render

from users_management.decorators import require_permission
from users_management.models import AuditLog
from users_management.services import (
    build_pagination_context,
    clean_value,
    invalidate_admin_report_cache,
    mongo_admin,
    parallel_run,
)
from users_management.views_legacy import _has_perm, check_admin_access
from users_management.webhook_dispatcher import dispatch_webhook_event

__all__ = ['reports_list', 'report_action']

VALID_STATUSES = ('pending', 'reviewing', 'resolved', 'dismissed')
VALID_REASONS = ('spam', 'harassment', 'hate_speech', 'spoiler', 'inappropriate', 'other')


def _parse_datetime(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            iso = value.strip()
            if iso.endswith('Z'):
                iso = iso[:-1] + '+00:00'
            dt = datetime.fromisoformat(iso)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _build_client_comment_url(movie_id, content_type, comment_id):
    if not movie_id or not comment_id:
        return ''
    base = getattr(settings, 'CLIENT_URL', 'http://localhost:3000').rstrip('/')
    segment = 'movies' if content_type == 'movie' else 'tvshows'
    return f'{base}/{segment}/{movie_id}#comment-{comment_id}'


def _redirect_reports(request):
    params = {}
    for key in ('status', 'reason', 'search', 'page'):
        val = (request.POST.get(f'next_{key}') or request.GET.get(key) or '').strip()
        if val:
            params[key] = val
    base_url = redirect('admin_dashboard:reports').url
    if params:
        return redirect(f'{base_url}?{urlencode(params)}')
    return redirect('admin_dashboard:reports')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('reports.view')
def reports_list(request):
    page = max(1, int(request.GET.get('page', 1) or 1))
    limit = 20
    status_filter = request.GET.get('status', '').strip()
    reason_filter = request.GET.get('reason', '').strip()
    search = request.GET.get('search', '').strip()

    if status_filter and status_filter not in VALID_STATUSES:
        status_filter = ''
    if reason_filter and reason_filter not in VALID_REASONS:
        reason_filter = ''

    query = {}
    if status_filter:
        query['status'] = status_filter
    if reason_filter:
        query['reasonCategory'] = reason_filter
    if search:
        regex = {'$regex': re.escape(search), '$options': 'i'}
        query['$or'] = [
            {'targetDetail': regex},
            {'reason': regex},
            {'details': regex},
            {'reporterName': regex},
            {'reportedUserName': regex},
        ]

    try:
        db = mongo_admin.db()
        results = parallel_run({
            'reports': lambda: list(
                db.userreports.find(query)
                .sort([('createdAt', -1)])
                .skip((page - 1) * limit)
                .limit(limit)
            ),
            'total': lambda: db.userreports.count_documents(query),
            'pending': lambda: db.userreports.count_documents({'status': 'pending'}),
            'reviewing': lambda: db.userreports.count_documents({'status': 'reviewing'}),
            'resolved': lambda: db.userreports.count_documents({'status': 'resolved'}),
            'dismissed': lambda: db.userreports.count_documents({'status': 'dismissed'}),
        }, max_workers=4)

        raw_reports = results['reports']
        total = results['total']

        comment_oids = []
        author_oids = []
        target_oids = []
        for doc in raw_reports:
            t_id = doc.get('targetId')
            if isinstance(t_id, ObjectId):
                target_oids.append(t_id)
                if doc.get('targetType', 'comment') == 'comment':
                    comment_oids.append(t_id)
            elif isinstance(t_id, str) and ObjectId.is_valid(t_id):
                oid = ObjectId(t_id)
                target_oids.append(oid)
                if doc.get('targetType', 'comment') == 'comment':
                    comment_oids.append(oid)

            a_id = doc.get('reportedUserId')
            if isinstance(a_id, ObjectId):
                author_oids.append(a_id)
            elif isinstance(a_id, str) and ObjectId.is_valid(a_id):
                author_oids.append(ObjectId(a_id))

        hydrate = parallel_run({
            'comments': lambda: (
                {
                    str(c['_id']): c
                    for c in db.comments.find(
                        {'_id': {'$in': comment_oids}},
                        {'content': 1, 'isDeleted': 1, 'likes': 1, 'movieId': 1, 'type': 1, 'parentId': 1, 'userId': 1},
                    )
                }
                if comment_oids else {}
            ),
            'authors': lambda: (
                {
                    str(u['_id']): u
                    for u in db.users.find(
                        {'_id': {'$in': author_oids}},
                        {'name': 1, 'username': 1, 'email': 1, 'isBanned': 1, 'banReason': 1},
                    )
                }
                if author_oids else {}
            ),
            'siblings': lambda: (
                list(db.userreports.find(
                    {'targetId': {'$in': target_oids}},
                    {'targetId': 1, 'status': 1},
                ))
                if target_oids else []
            ),
        }, max_workers=3)

        comments_map = hydrate['comments']
        authors_map = hydrate['authors']
        target_counts = {}
        for sib in hydrate['siblings']:
            tid_str = str(sib.get('targetId') or '')
            if not tid_str:
                continue
            bucket = target_counts.setdefault(tid_str, {'total': 0, 'open': 0})
            bucket['total'] += 1
            if sib.get('status') in ('pending', 'reviewing'):
                bucket['open'] += 1

        reports = []
        for doc in raw_reports:
            cleaned = clean_value(doc)
            report_id = cleaned.get('_id', '')
            target_id = str(cleaned.get('targetId') or '')
            author_id = str(cleaned.get('reportedUserId') or '')
            live_comment = comments_map.get(target_id)
            live_author = authors_map.get(author_id)

            if live_comment is None:
                comment_state = 'deleted'
            elif live_comment.get('isDeleted'):
                comment_state = 'hidden'
            else:
                comment_state = 'active'

            snapshot = cleaned.get('commentSnapshot') or {}
            snapshot_content = (
                cleaned.get('targetDetail')
                or snapshot.get('content')
                or ''
            )
            live_content = live_comment.get('content') if live_comment else ''
            movie_id = cleaned.get('movieId') or (live_comment.get('movieId') if live_comment else None)
            content_type = cleaned.get('contentType') or (live_comment.get('type') if live_comment else None)
            parent_comment_id = str(
                cleaned.get('parentCommentId')
                or (live_comment.get('parentId') if live_comment else '')
                or ''
            )

            counts_for_target = target_counts.get(target_id, {'total': 1, 'open': 0})
            reports.append({
                'id': report_id,
                'reporter_id': str(cleaned.get('reporterId') or ''),
                'reporter_name': cleaned.get('reporterName') or '',
                'target_type': cleaned.get('targetType') or 'comment',
                'target_id': target_id,
                'parent_comment_id': parent_comment_id,
                'is_reply': bool(parent_comment_id),
                'author_id': author_id,
                'author_name': (
                    (live_author.get('name') if live_author else '')
                    or cleaned.get('reportedUserName')
                    or snapshot.get('authorName')
                    or ''
                ),
                'author_email': live_author.get('email', '') if live_author else '',
                'author_is_banned': bool(live_author.get('isBanned')) if live_author else False,
                'author_exists': live_author is not None,
                'movie_id': movie_id,
                'content_type': content_type,
                'client_comment_url': _build_client_comment_url(movie_id, content_type, target_id),
                'comment_content': snapshot_content,
                'live_content': live_content,
                'content_changed': bool(live_content and snapshot_content and live_content != snapshot_content),
                'comment_state': comment_state,
                'reason_category': cleaned.get('reasonCategory') or 'other',
                'details': cleaned.get('details') or '',
                'reason': cleaned.get('reason') or '',
                'status': cleaned.get('status') or 'pending',
                'action_taken': cleaned.get('actionTaken') or 'none',
                'resolved_by': cleaned.get('resolvedBy') or '',
                'resolution_note': cleaned.get('resolutionNote') or '',
                'created_at': _parse_datetime(doc.get('createdAt')),
                'resolved_at': _parse_datetime(doc.get('resolvedAt')),
                'same_target_total': counts_for_target['total'],
                'same_target_open': counts_for_target['open'],
            })

        pagination = build_pagination_context(page, total, limit, request.GET)
        context = {
            'reports': reports,
            'total': total,
            'status_filter': status_filter,
            'reason_filter': reason_filter,
            'search': search,
            'stats': {
                'pending': results['pending'],
                'reviewing': results['reviewing'],
                'resolved': results['resolved'],
                'dismissed': results['dismissed'],
            },
            'can_edit_reports': _has_perm(request, 'reports.edit'),
            'can_edit_comments': _has_perm(request, 'comments.edit'),
            'can_delete_comments': _has_perm(request, 'comments.delete'),
            'can_edit_users': _has_perm(request, 'users.edit'),
            **pagination,
        }
    except Exception as exc:
        context = {
            'reports': [],
            'page': 1,
            'total': 0,
            'total_pages': 1,
            'page_range': [1],
            'preserved_params': '',
            'status_filter': status_filter,
            'reason_filter': reason_filter,
            'search': search,
            'stats': {'pending': 0, 'reviewing': 0, 'resolved': 0, 'dismissed': 0},
            'error': f'MongoDB error: {exc}',
            'can_edit_reports': _has_perm(request, 'reports.edit'),
            'can_edit_comments': _has_perm(request, 'comments.edit'),
            'can_delete_comments': _has_perm(request, 'comments.delete'),
            'can_edit_users': _has_perm(request, 'users.edit'),
        }

    return render(request, 'admin_dashboard/reports/reports.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('reports.edit')
def report_action(request, report_id):
    if request.method != 'POST':
        return _redirect_reports(request)

    if not ObjectId.is_valid(report_id):
        raise Http404('Report not found')

    db = mongo_admin.db()
    oid = ObjectId(report_id)
    report = db.userreports.find_one({'_id': oid})
    if not report:
        raise Http404('Report not found')

    action = request.POST.get('action', '').strip()
    note = request.POST.get('note', '').strip()[:500]
    apply_siblings = request.POST.get('apply_siblings', '1') == '1'
    ban_reason = request.POST.get('ban_reason', '').strip()[:300]
    now = datetime.utcnow()

    target_id = report.get('targetId')
    target_id_str = str(target_id) if target_id else ''

    def _mark_resolved(action_taken='none', status_value='resolved'):
        update_fields = {
            'status': status_value,
            'actionTaken': action_taken,
            'resolvedBy': request.user.username,
            'resolutionNote': note,
            'resolvedAt': now,
            'updatedAt': now,
        }
        db.userreports.update_one({'_id': oid}, {'$set': update_fields})
        if apply_siblings and target_id is not None:
            db.userreports.update_many(
                {
                    '_id': {'$ne': oid},
                    'targetId': target_id,
                    'status': {'$in': ['pending', 'reviewing']},
                },
                {'$set': update_fields},
            )

    if action == 'reviewing':
        db.userreports.update_one(
            {'_id': oid},
            {'$set': {'status': 'reviewing', 'updatedAt': now}},
        )
        AuditLog.log(request, 'report_reviewing', 'user_report', report_id)
        messages.success(request, 'Đã chuyển trạng thái báo cáo sang đang xem xét!')

    elif action in ('resolved', 'dismissed'):
        _mark_resolved(action_taken='none', status_value=action)
        AuditLog.log(request, f'report_{action}', 'user_report', report_id, {'note': note})
        dispatch_webhook_event('report.resolved', {'report_id': report_id, 'status': action, 'admin': request.user.username})
        label = 'đã xử lý' if action == 'resolved' else 'đã bỏ qua'
        messages.success(request, f'Đã chuyển trạng thái báo cáo sang {label}!')

    elif action == 'hide_comment':
        if not _has_perm(request, 'comments.edit'):
            raise PermissionDenied('Permission comments.edit required')
        if target_id_str and ObjectId.is_valid(target_id_str):
            mongo_admin.set_comment_deleted(target_id_str, True)
        _mark_resolved(action_taken='comment_hidden', status_value='resolved')
        AuditLog.log(
            request, 'report_hide_comment', 'comment', target_id_str,
            {'report_id': report_id, 'note': note},
        )
        dispatch_webhook_event('comment.hidden', {'comment_id': target_id_str, 'report_id': report_id, 'admin': request.user.username})
        messages.success(request, 'Đã ẩn bình luận và đánh dấu báo cáo đã xử lý!')

    elif action == 'restore_comment':
        if not _has_perm(request, 'comments.edit'):
            raise PermissionDenied('Permission comments.edit required')
        if target_id_str and ObjectId.is_valid(target_id_str):
            mongo_admin.set_comment_deleted(target_id_str, False)
        update_fields = {'updatedAt': now}
        if note:
            update_fields['resolutionNote'] = note
        db.userreports.update_one({'_id': oid}, {'$set': update_fields})
        AuditLog.log(
            request, 'report_restore_comment', 'comment', target_id_str,
            {'report_id': report_id},
        )
        messages.success(request, 'Đã khôi phục hiển thị bình luận!')

    elif action == 'delete_comment':
        if not _has_perm(request, 'comments.delete'):
            raise PermissionDenied('Permission comments.delete required')
        if target_id_str and ObjectId.is_valid(target_id_str):
            mongo_admin.delete_document('comments', target_id_str)
        _mark_resolved(action_taken='comment_deleted', status_value='resolved')
        AuditLog.log(
            request, 'report_delete_comment', 'comment', target_id_str,
            {'report_id': report_id, 'note': note},
        )
        dispatch_webhook_event('comment.deleted', {'comment_id': target_id_str, 'report_id': report_id, 'admin': request.user.username})
        messages.success(request, 'Đã xóa vĩnh viễn bình luận và đánh dấu báo cáo đã xử lý!')

    elif action == 'ban_author':
        if not _has_perm(request, 'users.edit'):
            raise PermissionDenied('Permission users.edit required')
        also_hide = request.POST.get('hide_comment', '1') == '1'
        if also_hide:
            if not _has_perm(request, 'comments.edit'):
                raise PermissionDenied('Permission comments.edit required')
            if target_id_str and ObjectId.is_valid(target_id_str):
                mongo_admin.set_comment_deleted(target_id_str, True)

        from datetime import timedelta
        try:
            ban_duration_hours = int(request.POST.get('ban_duration_hours') or 0)
        except (TypeError, ValueError):
            ban_duration_hours = 0

        author_id = report.get('reportedUserId')
        author_id_str = str(author_id) if author_id else ''
        if author_id_str and ObjectId.is_valid(author_id_str):
            author_oid = ObjectId(author_id_str)
            effective_reason = ban_reason or note or f"Comment violation ({report.get('reasonCategory', 'other')})"
            ban_expires = (now + timedelta(hours=ban_duration_hours)) if ban_duration_hours > 0 else None
            db.users.update_one(
                {'_id': author_oid},
                {'$set': {
                    'isBanned': True,
                    'banReason': effective_reason,
                    'bannedAt': now,
                    'bannedBy': request.user.username,
                    'banExpiresAt': ban_expires,
                }},
            )
            db.sessions.delete_many({'userId': author_oid})

        _mark_resolved(action_taken='user_banned', status_value='resolved')
        AuditLog.log(
            request, 'report_ban_author', 'user', author_id_str,
            {'report_id': report_id, 'comment_id': target_id_str, 'note': note, 'duration_hours': ban_duration_hours or None},
        )
        dispatch_webhook_event('user.banned', {
            'user_id': author_id_str,
            'report_id': report_id,
            'duration_hours': ban_duration_hours or None,
            'admin': request.user.username,
        })
        messages.success(request, 'Đã khóa tài khoản tác giả và xử lý báo cáo vi phạm!')

    invalidate_admin_report_cache()
    return _redirect_reports(request)
