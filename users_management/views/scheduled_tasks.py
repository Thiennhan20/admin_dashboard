"""Scheduled tasks management views."""
from datetime import datetime, timedelta

from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

from users_management.models import ScheduledTask, TaskRunLog, AuditLog
from users_management.services import mongo_admin
from users_management.views_legacy import check_admin_access

__all__ = ['scheduled_tasks_list', 'scheduled_task_run']

# Built-in task runners
TASK_RUNNERS = {}


def _run_cleanup_tokens():
    db = mongo_admin.db()
    cutoff = datetime.utcnow() - timedelta(hours=24)
    r1 = db.passwordresettokens.delete_many({'expiresAt': {'$lt': cutoff}})
    return f'Deleted {r1.deleted_count} expired password reset tokens'


def _run_cleanup_blacklist():
    db = mongo_admin.db()
    cutoff = datetime.utcnow() - timedelta(hours=24)
    r1 = db.blacklistedtokens.delete_many({'expiresAt': {'$lt': cutoff}})
    return f'Deleted {r1.deleted_count} expired blacklisted tokens'


def _run_cleanup_progress():
    db = mongo_admin.db()
    cutoff = datetime.utcnow() - timedelta(days=90)
    r1 = db.watchprogresses.delete_many({'lastWatched': {'$lt': cutoff}})
    return f'Deleted {r1.deleted_count} watch progress entries older than 90 days'


TASK_RUNNERS = {
    'cleanup_tokens': _run_cleanup_tokens,
    'cleanup_blacklist': _run_cleanup_blacklist,
    'cleanup_progress': _run_cleanup_progress,
}


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def scheduled_tasks_list(request):
    if not request.user.is_superuser:
        messages.error(request, 'Only Super Admin can manage scheduled tasks!')
        return redirect('admin_dashboard:dashboard')

    if request.method == 'POST':
        action = request.POST.get('action', '')
        task_id = request.POST.get('task_id')

        if action == 'create':
            ScheduledTask.objects.create(
                name=request.POST.get('name', ''),
                description=request.POST.get('description', ''),
                task_type=request.POST.get('task_type', 'custom'),
                interval_minutes=int(request.POST.get('interval_minutes', 1440)),
                is_active=request.POST.get('is_active') == 'on',
            )
            messages.success(request, 'Task created!')
        elif action == 'toggle' and task_id:
            task = get_object_or_404(ScheduledTask, id=task_id)
            task.is_active = not task.is_active
            task.save()
            messages.success(request, f'Task {"activated" if task.is_active else "deactivated"}!')
        elif action == 'delete' and task_id:
            get_object_or_404(ScheduledTask, id=task_id).delete()
            messages.success(request, 'Task deleted!')

        return redirect('admin_dashboard:scheduled_tasks')

    tasks = ScheduledTask.objects.all()
    context = {'tasks': tasks}
    return render(request, 'admin_dashboard/scheduled_tasks/scheduled_tasks.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def scheduled_task_run(request, task_id):
    if request.method != 'POST':
        return JsonResponse({'success': False}, status=405)
    task = get_object_or_404(ScheduledTask, id=task_id)
    runner = TASK_RUNNERS.get(task.task_type)
    if not runner:
        return JsonResponse({'success': False, 'error': f'No runner for type: {task.task_type}'})

    run_log = TaskRunLog.objects.create(task=task, status='running')
    try:
        output = runner()
        run_log.status = 'success'
        run_log.output = output or ''
        run_log.finished_at = timezone.now()
        run_log.save()

        task.last_run = timezone.now()
        task.last_status = 'success'
        task.last_output = output or ''
        task.save()

        AuditLog.log(request, 'run_scheduled_task', 'scheduled_task', str(task_id), {'output': output})
        return JsonResponse({'success': True, 'output': output})
    except Exception as e:
        run_log.status = 'failed'
        run_log.output = str(e)
        run_log.finished_at = timezone.now()
        run_log.save()
        task.last_run = timezone.now()
        task.last_status = 'failed'
        task.last_output = str(e)
        task.save()
        return JsonResponse({'success': False, 'error': str(e)})
