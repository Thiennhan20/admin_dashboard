"""Webhook management views."""
import json
import hashlib
import hmac

from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
import requests

from users_management.decorators import require_permission
from users_management.models import WebhookEndpoint, WebhookLog, AuditLog
from users_management.services import validate_public_http_url
from users_management.views_legacy import check_admin_access

__all__ = ['webhooks_list', 'webhook_action', 'webhook_test']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('webhooks.view')
def webhooks_list(request):
    if request.method == 'POST' and request.POST.get('action') == 'create':
        profile = getattr(request.user, 'admin_profile', None)
        if not (request.user.is_superuser or (profile and profile.has_perm('webhooks.edit'))):
            raise PermissionDenied('Permission webhooks.edit required')
        url = (request.POST.get('url', '') or '').strip()
        ok_url, reason = validate_public_http_url(url)
        if not ok_url:
            messages.error(request, f'Lỗi: Webhook URL không hợp lệ ({reason})')
            return redirect('admin_dashboard:webhooks')
        events = request.POST.getlist('events')
        WebhookEndpoint.objects.create(
            name=request.POST.get('name', ''),
            url=url,
            events=events,
            is_active=request.POST.get('is_active') == 'on',
            secret=request.POST.get('secret', ''),
            created_by=request.user,
        )
        AuditLog.log(request, 'create_webhook', 'webhook')
        messages.success(request, 'Đã tạo Webhook thành công!')
        return redirect('admin_dashboard:webhooks')

    endpoints = WebhookEndpoint.objects.all()
    recent_logs = WebhookLog.objects.all()[:20]
    available_events = [
        'user.created', 'user.deleted', 'user.banned',
        'comment.created', 'comment.deleted', 'comment.hidden',
        'report.resolved',
        'room.created', 'room.closed',
        'notification.broadcast',
        'backup.completed', 'system.error',
    ]
    context = {
        'endpoints': endpoints,
        'recent_logs': recent_logs,
        'available_events': available_events,
    }
    return render(request, 'admin_dashboard/webhooks/webhooks.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('webhooks.edit')
def webhook_action(request, webhook_id):
    if request.method != 'POST':
        return redirect('admin_dashboard:webhooks')
    endpoint = get_object_or_404(WebhookEndpoint, id=webhook_id)
    action = request.POST.get('action', '')
    if action == 'delete':
        profile = getattr(request.user, 'admin_profile', None)
        if not (request.user.is_superuser or (profile and profile.has_perm('webhooks.delete'))):
            raise PermissionDenied('Permission webhooks.delete required')

    if action == 'delete':
        endpoint.delete()
        messages.success(request, 'Đã xóa Webhook!')
    elif action == 'toggle':
        endpoint.is_active = not endpoint.is_active
        endpoint.save()
        messages.success(request, f'Đã {"bật" if endpoint.is_active else "tắt"} Webhook!')

    AuditLog.log(request, f'webhook_{action}', 'webhook', str(webhook_id))
    return redirect('admin_dashboard:webhooks')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('webhooks.edit')
def webhook_test(request, webhook_id):
    """Send a test payload to a webhook endpoint."""
    if request.method != 'POST':
        return JsonResponse({'success': False}, status=405)

    endpoint = get_object_or_404(WebhookEndpoint, id=webhook_id)
    ok_url, reason = validate_public_http_url(endpoint.url)
    if not ok_url:
        return JsonResponse({'success': False, 'error': f'Unsafe webhook URL: {reason}'}, status=400)
    payload = {
        'event': 'test',
        'data': {'message': 'This is a test webhook from Admin Panel'},
    }

    headers = {'Content-Type': 'application/json'}
    if endpoint.secret:
        sig = hmac.new(endpoint.secret.encode(), json.dumps(payload).encode(), hashlib.sha256).hexdigest()
        headers['X-Webhook-Signature'] = sig
    if endpoint.headers:
        headers.update(endpoint.headers)

    try:
        resp = requests.post(endpoint.url, json=payload, headers=headers, timeout=10)
        WebhookLog.objects.create(
            endpoint=endpoint, event='test', payload=payload,
            response_status=resp.status_code, response_body=resp.text[:1000],
        )
        return JsonResponse({'success': True, 'status_code': resp.status_code, 'body': resp.text[:500]})
    except Exception as e:
        WebhookLog.objects.create(
            endpoint=endpoint, event='test', payload=payload,
            response_status=0, response_body=str(e),
        )
        return JsonResponse({'success': False, 'error': str(e)})
