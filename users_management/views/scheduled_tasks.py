"""Scheduled tasks management views and automated background cron runner."""
from datetime import datetime, timedelta
from pathlib import Path
import sys
import threading
import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import close_old_connections
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
import requests

from users_management.decorators import require_permission
from users_management.models import AuditLog, BackupLog, ScheduledTask, TaskRunLog
from users_management.services import mongo_admin
from users_management.views_legacy import check_admin_access

__all__ = [
    'TASK_RUNNERS',
    'execute_scheduled_task',
    'run_due_scheduled_tasks',
    'start_scheduled_tasks_cron_thread',
    'scheduled_tasks_list',
    'scheduled_task_run',
]

_TASK_EXEC_LOCK = threading.Lock()
_CRON_STARTED = False
_CRON_POLL_SECONDS = 60
_CRON_STATE = {
    'started': False,
    'poll_seconds': _CRON_POLL_SECONDS,
    'last_tick': None,
    'last_executed_count': 0,
}


def _run_cleanup_tokens():
    from users_management.services import invalidate_swr
    db = mongo_admin.db()
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=24)
    r1 = db.passwordresettokens.delete_many({'expiresAt': {'$lt': cutoff}})
    r2 = db.sessions.delete_many({'expiresAt': {'$lt': now}})
    r3 = db.users.update_many(
        {'isBanned': True, 'banExpiresAt': {'$ne': None, '$lte': now}},
        {'$set': {'isBanned': False, 'banReason': '', 'banExpiresAt': None, 'bannedAt': None, 'bannedBy': ''}},
    )
    invalidate_swr('admin_sidebar_counts', 'sessions_global_stats_v1', 'user_list_stats_v2')
    return (
        f'Deleted {r1.deleted_count} expired password reset tokens, '
        f'{r2.deleted_count} expired sessions & auto-unbanned {r3.modified_count} expired bans'
    )


def _run_cleanup_blacklist():
    db = mongo_admin.db()
    cutoff = datetime.utcnow() - timedelta(hours=24)
    r1 = db.blacklistedtokens.delete_many({'expiresAt': {'$lt': cutoff}})
    return f'Deleted {r1.deleted_count} expired blacklisted tokens'


def _run_cleanup_progress():
    from users_management.services import invalidate_swr
    db = mongo_admin.db()
    cutoff = datetime.utcnow() - timedelta(days=90)
    r1 = db.watchprogresses.delete_many({'lastWatched': {'$lt': cutoff}})
    invalidate_swr('admin_sidebar_counts')
    return f'Deleted {r1.deleted_count} watch progress entries older than 90 days'


def _run_backup_db():
    """Stream a real BSON Extended JSON snapshot directly to disk and log BackupLog."""
    from users_management.views.backup import write_mongo_backup_snapshot

    log = BackupLog.objects.create(created_by=None, collections=[], status='in_progress')
    try:
        auto_dir = Path(settings.BASE_DIR) / 'auto_backups'
        auto_dir.mkdir(parents=True, exist_ok=True)
        snap_path = auto_dir / f'auto_backup_{datetime.utcnow():%Y%m%d_%H%M%S}.json'
        with snap_path.open('wb') as fh:
            file_size, counts, valid_collections = write_mongo_backup_snapshot(
                fh,
                selected=None,
                include_secrets=False,
            )

        # Keep at most 10 auto backup files on disk
        existing_files = sorted(auto_dir.glob('auto_backup_*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in existing_files[10:]:
            stale.unlink(missing_ok=True)

        log.collections = valid_collections
        log.file_size = file_size
        log.status = 'success'
        log.save()
        from users_management.webhook_dispatcher import dispatch_webhook_event
        dispatch_webhook_event('backup.completed', {
            'file': snap_path.name,
            'file_size': file_size,
            'collections': len(valid_collections),
        })
        return f'Snapshot saved ({snap_path.name}, {len(valid_collections)} collections, {log.file_size} bytes, counts={counts})'
    except Exception as exc:
        log.status = 'failed'
        log.error_message = str(exc)
        log.save()
        raise


def _run_health_check():
    from users_management.services import arena_health, parallel_run, server_health
    probes = parallel_run({
        's': lambda: server_health().get('ok', False),
        'm': lambda: mongo_admin.ping().get('ok', False),
        'a': lambda: arena_health().get('ok', False),
    }, max_workers=3)
    s_ok, m_ok, a_ok = probes['s'], probes['m'], probes['a']
    return f'Health Check -> API Server: {"OK" if s_ok else "DOWN"}, MongoDB: {"OK" if m_ok else "DOWN"}, 3D Arena: {"OK" if a_ok else "DOWN"}'


def _run_weekly_report():
    """Compute 7-day operations metrics across MongoDB & Django audit/task logs."""
    db = mongo_admin.db()
    now_utc = datetime.utcnow()
    week_ago_utc = now_utc - timedelta(days=7)
    week_ago_aware = timezone.now() - timedelta(days=7)

    total_users = db.users.count_documents({})
    new_users_7d = db.users.count_documents({'createdAt': {'$gte': week_ago_utc}})
    new_comments_7d = db.comments.count_documents({'createdAt': {'$gte': week_ago_utc}})
    streams_7d = db.streamhistories.count_documents({'lastCreatedAt': {'$gte': week_ago_utc}})
    active_sessions = db.sessions.count_documents({'$or': [{'expiresAt': {'$gt': now_utc}}, {'expiresAt': None}]})
    failed_tasks_7d = TaskRunLog.objects.filter(status='failed', started_at__gte=week_ago_aware).count()

    summary = {
        'period': f'{week_ago_utc:%Y-%m-%d}..{now_utc:%Y-%m-%d}',
        'total_users': total_users,
        'new_users_7d': new_users_7d,
        'new_comments_7d': new_comments_7d,
        'streams_7d': streams_7d,
        'active_sessions': active_sessions,
        'failed_tasks_7d': failed_tasks_7d,
    }
    AuditLog.objects.create(
        admin_user=None,
        admin_username='system_cron',
        action='weekly_report',
        target_type='report',
        target_id=summary['period'],
        details=summary,
    )
    email_note = ''
    if getattr(settings, 'EMAIL_HOST', ''):
        from django.contrib.auth.models import User as DjangoUser
        from django.core.mail import send_mail
        raw_targets = (getattr(settings, 'ADMIN_REPORT_EMAIL', '') or '').strip()
        if raw_targets:
            recipients = [x.strip() for x in raw_targets.split(',') if x.strip()]
        else:
            recipients = list(
                DjangoUser.objects.filter(
                    is_active=True,
                    is_staff=True,
                    admin_profile__role='super_admin',
                )
                .exclude(email='')
                .values_list('email', flat=True)
            )
        if recipients:
            body_text = (
                f"ENTN Weekly Operations Report ({summary['period']})\n\n"
                f"- Total Users: {total_users}\n"
                f"- New Users (7d): +{new_users_7d}\n"
                f"- New Comments (7d): +{new_comments_7d}\n"
                f"- Streams (7d): {streams_7d}\n"
                f"- Active Sessions: {active_sessions}\n"
                f"- Failed Tasks (7d): {failed_tasks_7d}\n"
            )
            try:
                send_mail(
                    f"[ENTN Admin] Weekly Report ({summary['period']})",
                    body_text,
                    settings.DEFAULT_FROM_EMAIL,
                    recipients[:20],
                    fail_silently=False,
                )
                email_note = f', email_sent={len(recipients[:20])}'
            except Exception as exc:
                email_note = f', email_error={exc}'

    return (
        f'Weekly Report ({summary["period"]}): total_users={total_users}, '
        f'+{new_users_7d} new users, +{new_comments_7d} comments, '
        f'{streams_7d} streams, {active_sessions} sessions, {failed_tasks_7d} failed tasks{email_note}'
    )


def _run_custom():
    return f'Custom maintenance task executed at {datetime.utcnow().isoformat()}Z'


TASK_RUNNERS = {
    'cleanup_tokens': _run_cleanup_tokens,
    'cleanup_blacklist': _run_cleanup_blacklist,
    'cleanup_progress': _run_cleanup_progress,
    'backup_db': _run_backup_db,
    'weekly_report': _run_weekly_report,
    'health_check': _run_health_check,
    'custom': _run_custom,
}


def _try_acquire_distributed_task_lock(task_id):
    """Best-effort distributed NX lock on Upstash Redis to avoid multi-worker cron overlap."""
    url = (getattr(settings, 'UPSTASH_REDIS_URL', '') or '').strip().rstrip('/')
    token = (getattr(settings, 'UPSTASH_REDIS_TOKEN', '') or '').strip()
    if not url or not token:
        return True  # Fallback cleanly to DB + thread lock
    try:
        resp = requests.post(
            f'{url}/pipeline',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json=[['SET', f'admin_sec:cron_lock:{task_id}', '1', 'NX', 'EX', '300']],
            timeout=2.0,
        )
        if not resp.ok:
            return True
        res = resp.json()
        if isinstance(res, list) and res and res[0].get('result') is None:
            return False
        return True
    except requests.RequestException:
        return True


def _release_distributed_task_lock(task_id):
    url = (getattr(settings, 'UPSTASH_REDIS_URL', '') or '').strip().rstrip('/')
    token = (getattr(settings, 'UPSTASH_REDIS_TOKEN', '') or '').strip()
    if not url or not token:
        return
    try:
        requests.post(
            f'{url}/pipeline',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json=[['DEL', f'admin_sec:cron_lock:{task_id}']],
            timeout=2.0,
        )
    except requests.RequestException:
        pass


def execute_scheduled_task(task, request=None):
    """Execute one ScheduledTask safely, recording TaskRunLog and updating ScheduledTask."""
    runner = TASK_RUNNERS.get(task.task_type)
    if not runner:
        return {'success': False, 'error': f'No runner for type: {task.task_type}'}

    stale_cutoff = timezone.now() - timedelta(minutes=10)
    if TaskRunLog.objects.filter(task=task, status='running', started_at__gte=stale_cutoff).exists():
        return {'success': False, 'skipped': True, 'error': 'Task already running'}

    if not _try_acquire_distributed_task_lock(task.id):
        return {'success': False, 'skipped': True, 'error': 'Task locked by another worker'}

    run_log = TaskRunLog.objects.create(task=task, status='running')
    try:
        output = runner()
        now = timezone.now()
        run_log.status = 'success'
        run_log.output = output or ''
        run_log.finished_at = now
        run_log.save()

        task.last_run = now
        task.last_status = 'success'
        task.last_output = output or ''
        task.save()

        if request is not None:
            AuditLog.log(request, 'run_scheduled_task', 'scheduled_task', str(task.id), {'output': output})
        else:
            AuditLog.objects.create(
                admin_user=None,
                admin_username='system_cron',
                action='auto_run_scheduled_task',
                target_type='scheduled_task',
                target_id=str(task.id),
                details={'task_name': task.name, 'task_type': task.task_type, 'output': output},
            )
        return {'success': True, 'output': output}
    except Exception as exc:
        now = timezone.now()
        run_log.status = 'failed'
        run_log.output = str(exc)
        run_log.finished_at = now
        run_log.save()

        task.last_run = now
        task.last_status = 'failed'
        task.last_output = str(exc)
        task.save()
        return {'success': False, 'error': str(exc)}
    finally:
        _release_distributed_task_lock(task.id)


def run_due_scheduled_tasks(force_all=False, task_id=None, now=None):
    """Run all active ScheduledTasks that are past their interval (or all if force_all=True)."""
    current_time = now or timezone.now()
    if not _TASK_EXEC_LOCK.acquire(blocking=False):
        return []

    results = []
    try:
        qs = ScheduledTask.objects.filter(is_active=True).order_by('id')
        if task_id is not None:
            qs = ScheduledTask.objects.filter(id=task_id)
            force_all = True

        for task in qs:
            interval = max(1, int(task.interval_minutes or 1))
            is_due = force_all or task.last_run is None or (current_time - task.last_run) >= timedelta(minutes=interval)
            if not is_due:
                continue
            res = execute_scheduled_task(task)
            results.append({
                'task_id': task.id,
                'name': task.name,
                'task_type': task.task_type,
                **res,
            })
        return results
    finally:
        _TASK_EXEC_LOCK.release()


def start_scheduled_tasks_cron_thread():
    """Launch background daemon polling every 60s for due ScheduledTasks."""
    global _CRON_STARTED
    if _CRON_STARTED:
        return
    _CRON_STARTED = True
    _CRON_STATE['started'] = True

    def _cron_loop():
        # Brief initial delay so Django finishes boot / migrations before polling
        time.sleep(10)
        while True:
            try:
                close_old_connections()
                executed = run_due_scheduled_tasks()
                _CRON_STATE['last_tick'] = timezone.now()
                _CRON_STATE['last_executed_count'] = len(executed)
            except Exception:
                pass
            finally:
                close_old_connections()
            time.sleep(_CRON_POLL_SECONDS)

    t = threading.Thread(target=_cron_loop, daemon=True, name='ScheduledTasksCronThread')
    t.start()


if not any(cmd in sys.argv for cmd in ('migrate', 'makemigrations', 'collectstatic', 'check', 'test', 'shell', 'run_scheduled_tasks')):
    start_scheduled_tasks_cron_thread()


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('scheduled_tasks.view')
def scheduled_tasks_list(request):
    profile = getattr(request.user, 'admin_profile', None)
    can_edit = request.user.is_superuser or bool(profile and profile.has_perm('scheduled_tasks.edit'))
    can_delete = request.user.is_superuser or bool(profile and profile.has_perm('scheduled_tasks.delete'))

    if request.method == 'POST':
        action = request.POST.get('action', '')
        task_id = request.POST.get('task_id')

        if action == 'delete':
            if not can_delete:
                messages.error(request, 'Bạn không có quyền xóa tác vụ định kỳ.')
                return redirect('admin_dashboard:scheduled_tasks')
            if task_id:
                task_obj = get_object_or_404(ScheduledTask, id=task_id)
                tname = task_obj.name
                task_obj.delete()
                AuditLog.log(request, 'delete_scheduled_task', 'scheduled_task', str(task_id), {'name': tname})
                messages.success(request, 'Đã xóa tác vụ định kỳ!')
        else:
            if not can_edit:
                messages.error(request, 'Bạn không có quyền chỉnh sửa hoặc chạy tác vụ định kỳ.')
                return redirect('admin_dashboard:scheduled_tasks')

            if action == 'create':
                interval_val = max(1, int(request.POST.get('interval_minutes', 1440) or 1440))
                t = ScheduledTask.objects.create(
                    name=(request.POST.get('name') or '').strip() or 'Scheduled Task',
                    description=(request.POST.get('description') or '').strip(),
                    task_type=request.POST.get('task_type', 'custom'),
                    interval_minutes=interval_val,
                    is_active=request.POST.get('is_active') == 'on',
                )
                AuditLog.log(request, 'create_scheduled_task', 'scheduled_task', str(t.id), {'task_type': t.task_type})
                messages.success(request, 'Đã tạo tác vụ định kỳ mới!')
            elif action == 'toggle' and task_id:
                task = get_object_or_404(ScheduledTask, id=task_id)
                task.is_active = not task.is_active
                task.save()
                AuditLog.log(request, 'toggle_scheduled_task', 'scheduled_task', str(task.id), {'is_active': task.is_active})
                messages.success(request, f'Đã {"bật" if task.is_active else "tắt"} tác vụ định kỳ!')
            elif action == 'run_due':
                executed = run_due_scheduled_tasks()
                messages.success(request, f'Đã chạy {len(executed)} tác vụ đến hạn.')

        return redirect('admin_dashboard:scheduled_tasks')

    now = timezone.now()
    tasks = list(ScheduledTask.objects.all())
    for t in tasks:
        if not t.is_active:
            t.next_run_display = 'Paused'
        elif t.last_run is None:
            t.next_run_display = 'Due now'
        else:
            nxt = t.last_run + timedelta(minutes=max(1, int(t.interval_minutes or 1)))
            t.next_run_display = 'Due now' if nxt <= now else nxt

    recent_runs = TaskRunLog.objects.select_related('task').order_by('-started_at')[:25]
    context = {
        'tasks': tasks,
        'recent_runs': recent_runs,
        'cron_state': _CRON_STATE,
    }
    return render(request, 'admin_dashboard/scheduled_tasks/scheduled_tasks.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('scheduled_tasks.edit')
def scheduled_task_run(request, task_id):
    if request.method != 'POST':
        return JsonResponse({'success': False}, status=405)
    task = get_object_or_404(ScheduledTask, id=task_id)
    res = execute_scheduled_task(task, request=request)
    if not res.get('success'):
        return JsonResponse({'success': False, 'error': res.get('error', 'Failed')})
    return JsonResponse({'success': True, 'output': res.get('output', '')})
