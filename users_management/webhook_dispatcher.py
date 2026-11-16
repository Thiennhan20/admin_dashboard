"""Outbound webhook dispatcher supporting HMAC-signed JSON, Slack, and Discord payloads."""
from datetime import datetime
import hashlib
import hmac
import json
import sys
import threading

import requests

from users_management.models import WebhookEndpoint, WebhookLog
from users_management.services import validate_public_http_url


def _format_webhook_payload(url, event_name, data, timestamp):
    lower_url = (url or '').lower()
    summary_text = f'[ENTN Admin] Event `{event_name}` at {timestamp}\n```json\n{json.dumps(data or {}, ensure_ascii=False, default=str)[:1200]}\n```'
    if 'discord.com/api/webhooks' in lower_url or 'discordapp.com/api/webhooks' in lower_url:
        return {'content': summary_text}
    if 'hooks.slack.com/' in lower_url:
        return {'text': summary_text}
    return {
        'event': event_name,
        'timestamp': timestamp,
        'data': data or {},
    }


def _deliver_single(endpoint, event_name, data, timestamp):
    ok_url, reason = validate_public_http_url(endpoint.url)
    payload = _format_webhook_payload(endpoint.url, event_name, data, timestamp)
    if not ok_url:
        WebhookLog.objects.create(
            endpoint=endpoint,
            event=event_name,
            payload=payload,
            response_status=0,
            response_body=f'Blocked unsafe URL: {reason}',
        )
        return

    headers = {'Content-Type': 'application/json'}
    if endpoint.secret:
        encoded = json.dumps(payload, separators=(',', ':'), default=str).encode('utf-8')
        sig = hmac.new(endpoint.secret.encode('utf-8'), encoded, hashlib.sha256).hexdigest()
        headers['X-Webhook-Signature'] = sig
    if isinstance(endpoint.headers, dict):
        headers.update({str(k): str(v) for k, v in endpoint.headers.items()})

    try:
        resp = requests.post(endpoint.url, json=payload, headers=headers, timeout=5)
        WebhookLog.objects.create(
            endpoint=endpoint,
            event=event_name,
            payload=payload,
            response_status=resp.status_code,
            response_body=(resp.text or '')[:1000],
        )
    except Exception as exc:
        WebhookLog.objects.create(
            endpoint=endpoint,
            event=event_name,
            payload=payload,
            response_status=0,
            response_body=str(exc)[:1000],
        )


def dispatch_webhook_event(event_name, data=None, sync=False):
    """Find active webhooks subscribed to `event_name` and deliver asynchronously by default."""
    try:
        endpoints = list(WebhookEndpoint.objects.filter(is_active=True))
    except Exception:
        return 0

    matched = [
        ep for ep in endpoints
        if isinstance(ep.events, list) and (event_name in ep.events or '*' in ep.events)
    ]
    if not matched:
        return 0

    timestamp = datetime.utcnow().isoformat() + 'Z'

    def _run():
        from django.db import close_old_connections
        try:
            for ep in matched:
                _deliver_single(ep, event_name, data, timestamp)
        finally:
            close_old_connections()

    if sync or 'test' in sys.argv:
        if sync:
            _run()
        return len(matched)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return len(matched)

