from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
import json

# ── Admin Profile ───────────────────────────────────────────────────
class AdminProfile(models.Model):
    """Extended profile for admin users with RBAC support."""
    ROLE_CHOICES = [
        ('super_admin', 'Super Admin'),
        ('admin', 'Admin'),
        ('moderator', 'Moderator'),
        ('viewer', 'Viewer'),
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='admin_profile')
    role = models.CharField(max_length=50, default='admin', choices=ROLE_CHOICES)
    avatar = models.TextField(blank=True, default='')  # base64 or URL
    permissions = models.JSONField(default=dict, blank=True, help_text='Custom permission overrides as {module.action: bool}')
    totp_secret = models.CharField(max_length=64, blank=True, default='')
    totp_enabled = models.BooleanField(default=False)
    totp_backup_codes = models.JSONField(default=list, blank=True, help_text='Hashed one-time 2FA recovery codes')
    last_api_sync = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Admin Profile"
        verbose_name_plural = "Admin Profiles"

    def __str__(self):
        return f"Admin: {self.user.username} ({self.role})"

    @property
    def is_super_admin(self):
        return bool(self.user.is_superuser or self.role == 'super_admin')

    def has_perm(self, perm_code):
        """Check if this admin has a specific permission (e.g. 'users.delete')."""
        if self.role == 'super_admin' or self.user.is_superuser:
            return True
        # Check explicit overrides first
        if isinstance(self.permissions, dict) and perm_code in self.permissions:
            return bool(self.permissions[perm_code])
        # Fall back to role defaults
        return perm_code in ROLE_DEFAULTS.get(self.role, set())


# ── RBAC Default Permissions ────────────────────────────────────────
MODULES = [
    'dashboard', 'users', 'comments', 'movies', 'tv_shows', 'watchlist',
    'watch_progress', 'search_history', 'stream_history', 'rooms',
    'notifications', 'push_devices', 'sessions',
    'games', 'daily_challenge', 'servers', 'cache',
    'admins', 'monitoring', 'settings', 'reports', 'content', 'changelog',
    'email', 'scheduled_tasks', 'webhooks', 'backup', 'error_logs',
    'audit_logs', 'analytics', 'tokens',
]
ACTIONS = ['view', 'edit', 'delete', 'export']

MODULE_LABELS = {
    'dashboard': 'Dashboard',
    'users': 'Users',
    'comments': 'Comments',
    'movies': 'Movies',
    'tv_shows': 'TV Shows',
    'watchlist': 'Watchlist',
    'watch_progress': 'Watch Progress',
    'search_history': 'Search History',
    'stream_history': 'Stream History',
    'rooms': 'Watch Rooms',
    'notifications': 'Notifications',
    'push_devices': 'Push Devices',
    'sessions': 'Active Sessions',
    'games': '3D Arena / Games',
    'daily_challenge': 'Daily Challenge',
    'servers': 'Stream Servers',
    'cache': 'Upstash Cache',
    'admins': 'Admin Accounts',
    'monitoring': 'Monitoring',
    'settings': 'Platform Settings',
    'reports': 'Moderation Reports',
    'content': 'Featured Content',
    'changelog': 'Changelog',
    'email': 'Email Dispatch',
    'scheduled_tasks': 'Scheduled Tasks',
    'webhooks': 'Webhooks',
    'backup': 'Backup & Restore',
    'error_logs': 'Error Logs',
    'audit_logs': 'Audit Logs',
    'analytics': 'Analytics',
    'tokens': 'Security Tokens',
}

ROLE_DEFAULTS = {
    'super_admin': {f'{m}.{a}' for m in MODULES for a in ACTIONS},
    'admin': {f'{m}.{a}' for m in MODULES for a in ACTIONS} - {
        'admins.edit', 'admins.delete',
        'settings.view', 'settings.edit', 'settings.delete', 'settings.export',
        'backup.view', 'backup.edit', 'backup.delete', 'backup.export',
        'scheduled_tasks.edit', 'scheduled_tasks.delete',
        'webhooks.edit', 'webhooks.delete',
    },
    'moderator': {
        'dashboard.view',
        'users.view', 'users.export',
        'comments.view', 'comments.edit', 'comments.delete', 'comments.export',
        'movies.view', 'tv_shows.view',
        'watchlist.view',
        'watch_progress.view',
        'search_history.view',
        'stream_history.view',
        'rooms.view', 'rooms.delete',
        'notifications.view', 'notifications.edit',
        'push_devices.view',
        'sessions.view',
        'games.view', 'daily_challenge.view',
        'reports.view', 'reports.edit',
        'content.view',
        'analytics.view',
    },
    'viewer': {f'{m}.view' for m in MODULES} - {
        'settings.view', 'backup.view', 'webhooks.view',
        'scheduled_tasks.view', 'admins.view', 'error_logs.view',
    },
}


def get_rbac_matrix(profile=None):
    """Build module x action permission matrix metadata for admin create/edit templates."""
    overrides = (profile.permissions if profile and isinstance(profile.permissions, dict) else {})
    role = profile.role if profile else 'admin'
    role_defaults = ROLE_DEFAULTS.get(role, set())
    rows = []
    for mod in MODULES:
        perms = []
        for act in ACTIONS:
            code = f'{mod}.{act}'
            default_val = code in role_defaults
            effective_val = bool(overrides[code]) if code in overrides else default_val
            perms.append({
                'action': act,
                'code': code,
                'default': default_val,
                'effective': effective_val,
                'is_overridden': code in overrides,
            })
        rows.append({
            'module': mod,
            'label': MODULE_LABELS.get(mod, mod.replace('_', ' ').title()),
            'actions': perms,
        })
    return rows


# ── Audit Log ───────────────────────────────────────────────────────
class AuditLog(models.Model):
    admin_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='audit_logs')
    admin_username = models.CharField(max_length=150, default='')
    action = models.CharField(max_length=100, db_index=True)
    target_type = models.CharField(max_length=100, blank=True, default='')
    target_id = models.CharField(max_length=255, blank=True, default='')
    details = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default='')
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-timestamp']
        verbose_name = "Audit Log"
        verbose_name_plural = "Audit Logs"

    def __str__(self):
        return f"[{self.timestamp:%Y-%m-%d %H:%M}] {self.admin_username}: {self.action}"

    @classmethod
    def log(cls, request, action, target_type='', target_id='', details=None):
        ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', ''))
        if ',' in ip:
            ip = ip.split(',')[0].strip()
        return cls.objects.create(
            admin_user=request.user if request.user.is_authenticated else None,
            admin_username=request.user.username if request.user.is_authenticated else 'anonymous',
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            details=details or {},
            ip_address=ip or None,
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
        )


# ── Error Log ───────────────────────────────────────────────────────
class ErrorLog(models.Model):
    LEVEL_CHOICES = [
        ('error', 'Error'),
        ('warning', 'Warning'),
        ('critical', 'Critical'),
    ]
    level = models.CharField(max_length=20, choices=LEVEL_CHOICES, default='error', db_index=True)
    message = models.TextField()
    traceback_text = models.TextField(blank=True, default='')
    request_path = models.CharField(max_length=500, blank=True, default='')
    request_method = models.CharField(max_length=10, blank=True, default='')
    request_body = models.TextField(blank=True, default='')
    user_agent = models.TextField(blank=True, default='')
    admin_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-timestamp']
        verbose_name = "Error Log"

    def __str__(self):
        return f"[{self.level.upper()}] {self.message[:80]}"


# ── System Settings ────────────────────────────────────────────────
class SystemSetting(models.Model):
    key = models.CharField(max_length=100, unique=True, db_index=True)
    value = models.JSONField(default=dict)
    description = models.TextField(blank=True, default='')
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['key']

    def __str__(self):
        return f"{self.key} = {self.value}"

    @classmethod
    def get(cls, key, default=None):
        try:
            return cls.objects.get(key=key).value
        except cls.DoesNotExist:
            return default

    @classmethod
    def set(cls, key, value, description='', user=None):
        obj, created = cls.objects.update_or_create(
            key=key,
            defaults={'value': value, 'description': description, 'updated_by': user},
        )
        return obj


# ── Backup Log ──────────────────────────────────────────────────────
class BackupLog(models.Model):
    STATUS_CHOICES = [
        ('success', 'Success'),
        ('failed', 'Failed'),
        ('in_progress', 'In Progress'),
    ]
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    collections = models.JSONField(default=list, help_text='List of collection names backed up')
    file_size = models.BigIntegerField(default=0, help_text='Size in bytes')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='in_progress')
    error_message = models.TextField(blank=True, default='')
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"Backup {self.timestamp:%Y-%m-%d %H:%M} ({self.status})"


# ── Featured Content ────────────────────────────────────────────────
class FeaturedContent(models.Model):
    TYPE_CHOICES = [('movie', 'Movie'), ('tv', 'TV Show')]
    content_type = models.CharField(max_length=10, choices=TYPE_CHOICES)
    tmdb_id = models.CharField(max_length=50)
    title = models.CharField(max_length=500)
    poster_path = models.CharField(max_length=500, blank=True, default='')
    backdrop_path = models.CharField(max_length=500, blank=True, default='')
    description = models.TextField(blank=True, default='')
    position = models.IntegerField(default=0, help_text='Display order, lower = first')
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['position', '-created_at']

    def __str__(self):
        return f"[{self.content_type}] {self.title}"


# ── Maintenance Notice ──────────────────────────────────────────────
class MaintenanceNotice(models.Model):
    title = models.CharField(max_length=300)
    message = models.TextField()
    is_active = models.BooleanField(default=False)
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title


# ── User Report ─────────────────────────────────────────────────────
class UserReport(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('reviewing', 'Reviewing'),
        ('resolved', 'Resolved'),
        ('dismissed', 'Dismissed'),
    ]
    TARGET_CHOICES = [
        ('user', 'User'),
        ('comment', 'Comment'),
        ('content', 'Content'),
    ]
    reporter_id = models.CharField(max_length=255, help_text='MongoDB user _id')
    reporter_name = models.CharField(max_length=255, blank=True, default='')
    target_type = models.CharField(max_length=20, choices=TARGET_CHOICES)
    target_id = models.CharField(max_length=255)
    target_detail = models.TextField(blank=True, default='', help_text='Snapshot of reported content')
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='resolved_reports')
    resolution_note = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Report [{self.status}] {self.target_type}:{self.target_id}"


# ── Changelog ───────────────────────────────────────────────────────
class ChangelogEntry(models.Model):
    CATEGORY_CHOICES = [
        ('feature', 'New Feature'),
        ('fix', 'Bug Fix'),
        ('improvement', 'Improvement'),
        ('security', 'Security'),
        ('breaking', 'Breaking Change'),
    ]
    version = models.CharField(max_length=50, db_index=True)
    title = models.CharField(max_length=300)
    content = models.TextField(help_text='Markdown supported')
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='feature')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    published_at = models.DateTimeField(default=timezone.now)
    is_published = models.BooleanField(default=True)

    class Meta:
        ordering = ['-published_at']

    def __str__(self):
        return f"v{self.version} - {self.title}"


# ── Email Log ───────────────────────────────────────────────────────
class EmailLog(models.Model):
    STATUS_CHOICES = [
        ('sent', 'Sent'),
        ('failed', 'Failed'),
        ('pending', 'Pending'),
    ]
    sender = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    recipient_type = models.CharField(max_length=20, default='single', help_text='single or broadcast')
    recipients = models.JSONField(default=list, help_text='List of email addresses')
    subject = models.CharField(max_length=500)
    body = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    error_message = models.TextField(blank=True, default='')
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-sent_at']

    def __str__(self):
        return f"[{self.status}] {self.subject}"


# ── Scheduled Tasks ─────────────────────────────────────────────────
class ScheduledTask(models.Model):
    TASK_TYPES = [
        ('cleanup_tokens', 'Cleanup Expired Tokens'),
        ('cleanup_blacklist', 'Cleanup Blacklisted Tokens'),
        ('cleanup_progress', 'Cleanup Old Watch Progress'),
        ('backup_db', 'Backup Database'),
        ('weekly_report', 'Weekly Activity Report Snapshot'),
        ('health_check', 'Health Check All Services'),
        ('custom', 'Custom Task'),
    ]
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    task_type = models.CharField(max_length=50, choices=TASK_TYPES)
    is_active = models.BooleanField(default=True)
    interval_minutes = models.IntegerField(default=1440, help_text='Run every N minutes (1440 = daily)')
    last_run = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=20, blank=True, default='')
    last_output = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def next_run(self):
        if not self.last_run:
            return None
        from datetime import timedelta
        return self.last_run + timedelta(minutes=self.interval_minutes)

    @property
    def human_interval(self):
        mins = max(1, int(self.interval_minutes or 1))
        if mins >= 1440 and mins % 1440 == 0:
            days = mins // 1440
            return f'{days} day' if days == 1 else f'{days} days'
        if mins >= 60 and mins % 60 == 0:
            hours = mins // 60
            return f'{hours} hour' if hours == 1 else f'{hours} hours'
        if mins >= 1440:
            days = mins // 1440
            rem_h = (mins % 1440) // 60
            return f'{days}d {rem_h}h' if rem_h else f'{days}d'
        if mins >= 60:
            return f'{mins // 60}h {mins % 60}m'
        return f'{mins} min'


class TaskRunLog(models.Model):
    task = models.ForeignKey(ScheduledTask, on_delete=models.CASCADE, related_name='run_logs')
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default='running')
    output = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f"{self.task.name} @ {self.started_at:%Y-%m-%d %H:%M}"


# ── Webhook ─────────────────────────────────────────────────────────
class WebhookEndpoint(models.Model):
    name = models.CharField(max_length=200)
    url = models.URLField(max_length=500)
    events = models.JSONField(default=list, help_text='List of event names to trigger on')
    is_active = models.BooleanField(default=True)
    secret = models.CharField(max_length=200, blank=True, default='', help_text='Shared secret for HMAC')
    headers = models.JSONField(default=dict, blank=True, help_text='Extra headers to send')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.url})"


class WebhookLog(models.Model):
    endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE, related_name='logs')
    event = models.CharField(max_length=100)
    payload = models.JSONField(default=dict)
    response_status = models.IntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True, default='')
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-sent_at']

    def __str__(self):
        return f"{self.event} → {self.endpoint.name} ({self.response_status})"
