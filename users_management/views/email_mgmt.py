"""Email management views."""
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.shortcuts import render, redirect

from users_management.models import EmailLog, AuditLog
from users_management.services import mongo_admin, clean_value
from users_management.views_legacy import check_admin_access

__all__ = ['email_management']


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def email_management(request):
    if request.method == 'POST' and request.POST.get('action') == 'send':
        recipient_type = request.POST.get('recipient_type', 'single')
        subject = request.POST.get('subject', '').strip()
        body = request.POST.get('body', '').strip()
        recipients = []

        if not subject or not body:
            messages.error(request, 'Subject and body are required!')
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
                messages.error(request, 'No recipients found!')
                return redirect('admin_dashboard:email')

            # Try to send via Django mail
            status = 'pending'
            error_msg = ''
            try:
                from django.core.mail import send_mail
                from django.conf import settings as dj_settings
                if getattr(dj_settings, 'EMAIL_HOST', ''):
                    for email_addr in recipients[:100]:  # Limit batch
                        send_mail(subject, body, dj_settings.DEFAULT_FROM_EMAIL, [email_addr], fail_silently=True)
                    status = 'sent'
                else:
                    status = 'pending'
                    error_msg = 'SMTP not configured. Email saved but not sent.'
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
                messages.success(request, f'Email sent to {len(recipients)} recipient(s)!')
            elif status == 'pending':
                messages.warning(request, f'Email saved ({len(recipients)} recipients) but SMTP not configured.')
            else:
                messages.error(request, f'Email failed: {error_msg}')

        except Exception as e:
            messages.error(request, f'Error: {e}')

        return redirect('admin_dashboard:email')

    logs = EmailLog.objects.all()[:50]
    context = {'logs': logs}
    return render(request, 'admin_dashboard/email/email.html', context)
