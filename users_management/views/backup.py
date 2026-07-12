"""Backup management views – export MongoDB collections as JSON."""
import json
from datetime import datetime

from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect

from users_management.models import BackupLog, AuditLog
from users_management.services import mongo_admin, clean_value, MONGO_COLLECTIONS
from users_management.views_legacy import check_admin_access

__all__ = ['backup_management', 'backup_download']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def backup_management(request):
    if not request.user.is_superuser:
        messages.error(request, 'Only Super Admin can access Backup!')
        return redirect('admin_dashboard:dashboard')

    logs = BackupLog.objects.all()[:20]
    collections = list(MONGO_COLLECTIONS.keys())
    context = {'logs': logs, 'collections': collections}
    return render(request, 'admin_dashboard/backup/backup.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def backup_download(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    if not request.user.is_superuser:
        return JsonResponse({'error': 'Permission denied'}, status=403)

    selected = request.POST.getlist('collections')
    if not selected:
        selected = list(MONGO_COLLECTIONS.keys())

    log = BackupLog.objects.create(created_by=request.user, collections=selected, status='in_progress')
    try:
        db = mongo_admin.db()
        backup_data = {}
        for coll_name in selected:
            if coll_name in MONGO_COLLECTIONS:
                proj = MONGO_COLLECTIONS[coll_name].get('projection', {})
                docs = list(db[coll_name].find({}, proj))
                backup_data[coll_name] = clean_value(docs)

        content = json.dumps(backup_data, indent=2, ensure_ascii=False, default=str)
        log.file_size = len(content.encode('utf-8'))
        log.status = 'success'
        log.save()

        AuditLog.log(request, 'backup_database', 'backup', str(log.id), {'collections': selected, 'size': log.file_size})

        response = HttpResponse(content, content_type='application/json; charset=utf-8')
        filename = f'backup_{datetime.now():%Y%m%d_%H%M%S}.json'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    except Exception as e:
        log.status = 'failed'
        log.error_message = str(e)
        log.save()
        messages.error(request, f'Backup failed: {e}')
        return redirect('admin_dashboard:backup')
