"""Email management views."""
from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import render, redirect

from users_management.decorators import require_permission
from users_management.models import EmailLog, AuditLog
from users_management.rate_limit import consume_action_rate_limit, format_retry_wait
from users_management.services import mongo_admin, clean_value
from users_management.views_legacy import check_admin_access

__all__ = ['email_management']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('email.view')
def email_management(request):
    if request.method == 'POST' and request.POST.get('action') == 'send':
        profile = getattr(request.user, 'admin_profile', None)
        if not (request.user.is_superuser or (profile and profile.has_perm('email.edit'))):
            raise PermissionDenied('Permission email.edit required')

        allowed, retry_after, _ = consume_action_rate_limit('admin_sensitive', request)
        if not allowed:
            messages.error(request, f'Thao tác quá nhanh. Vui lòng thử lại sau {format_retry_wait(retry_after)}.')
            return redirect('admin_dashboard:email')

        recipient_type = request.POST.get('recipient_type', 'single')
        subject = request.POST.get('subject', '').strip()
        body = request.POST.get('body', '').strip()
        recipients = []

        if not subject or not body:
            messages.error(request, 'Vui lòng nhập tiêu đề và nội dung Email!')
            return redirect('admin_dashboard:email')

        try:
            if recipient_type == 'broadcast':
                db = mongo_admin.db()
                users = list(db.users.find({}, {'email': 1}))
                recipients = [u.get('email') for u in users if u.get('email')]
            else:
                email = request.POST.get('recipient_email', '').strip()
                if email:
                    recipients = [email]

            if not recipients:
                messages.error(request, 'Không tìm thấy người nhận phù hợp!')
                return redirect('admin_dashboard:email')

            # Try to send via Django mail
            status = 'pending'
            error_msg = ''
            try:
                from django.core.mail import send_mail
                if getattr(settings, 'EMAIL_HOST', ''):
                    for email_addr in recipients[:100]:  # Limit batch
                        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [email_addr], fail_silently=False)
                    status = 'sent'
                else:
                    status = 'pending'
                    error_msg = 'Chưa cấu hình SMTP. Email đã được lưu nhưng chưa gửi đi.'
            except Exception as e:
                status = 'failed'
                error_msg = str(e)

            EmailLog.objects.create(
                sender=request.user,
                recipient_type=recipient_type,
                recipients=recipients[:100],
                subject=subject,
                body=body,
                status=status,
                error_message=error_msg,
            )
            AuditLog.log(request, 'send_email', 'email', '', {
                'type': recipient_type, 'count': len(recipients), 'status': status
            })

            if status == 'sent':
                messages.success(request, f'Đã gửi Email tới {len(recipients)} người nhận!')
            elif status == 'pending':
                messages.warning(request, f'Đã lưu Email ({len(recipients)} người nhận) nhưng máy chủ SMTP chưa được cấu hình.')
            else:
                messages.error(request, f'Gửi Email thất bại: {error_msg}')

        except Exception as e:
            messages.error(request, f'Lỗi: {e}')

        return redirect('admin_dashboard:email')

    logs = EmailLog.objects.all()[:50]
    smtp_host = getattr(settings, 'EMAIL_HOST', '') or ''
    context = {
        'logs': logs,
        'smtp_configured': bool(smtp_host),
        'smtp_host': smtp_host,
    }
    return render(request, 'admin_dashboard/email/email.html', context)
