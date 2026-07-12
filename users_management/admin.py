from django.contrib import admin
from .models import (
    AdminProfile, AuditLog, ErrorLog, SystemSetting, BackupLog,
    FeaturedContent, MaintenanceNotice, UserReport, ChangelogEntry,
    EmailLog, ScheduledTask, TaskRunLog, WebhookEndpoint, WebhookLog,
)


@admin.register(AdminProfile)
class AdminProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'created_at')
    list_filter = ('role',)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('admin_username', 'action', 'target_type', 'target_id', 'timestamp')
    list_filter = ('action', 'target_type')
    search_fields = ('admin_username', 'action', 'target_id')
    readonly_fields = ('admin_user', 'admin_username', 'action', 'target_type', 'target_id', 'details', 'ip_address', 'user_agent', 'timestamp')


@admin.register(ErrorLog)
class ErrorLogAdmin(admin.ModelAdmin):
    list_display = ('level', 'message', 'request_path', 'timestamp')
    list_filter = ('level',)


@admin.register(SystemSetting)
class SystemSettingAdmin(admin.ModelAdmin):
    list_display = ('key', 'value', 'updated_at')


@admin.register(BackupLog)
class BackupLogAdmin(admin.ModelAdmin):
    list_display = ('status', 'created_by', 'file_size', 'timestamp')


@admin.register(FeaturedContent)
class FeaturedContentAdmin(admin.ModelAdmin):
    list_display = ('title', 'content_type', 'is_active', 'position')


@admin.register(MaintenanceNotice)
class MaintenanceNoticeAdmin(admin.ModelAdmin):
    list_display = ('title', 'is_active', 'created_at')


@admin.register(UserReport)
class UserReportAdmin(admin.ModelAdmin):
    list_display = ('target_type', 'target_id', 'status', 'created_at')
    list_filter = ('status', 'target_type')


@admin.register(ChangelogEntry)
class ChangelogEntryAdmin(admin.ModelAdmin):
    list_display = ('version', 'title', 'category', 'is_published', 'published_at')


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = ('subject', 'recipient_type', 'status', 'sent_at')


@admin.register(ScheduledTask)
class ScheduledTaskAdmin(admin.ModelAdmin):
    list_display = ('name', 'task_type', 'is_active', 'last_run', 'last_status')


@admin.register(TaskRunLog)
class TaskRunLogAdmin(admin.ModelAdmin):
    list_display = ('task', 'status', 'started_at', 'finished_at')


@admin.register(WebhookEndpoint)
class WebhookEndpointAdmin(admin.ModelAdmin):
    list_display = ('name', 'url', 'is_active')


@admin.register(WebhookLog)
class WebhookLogAdmin(admin.ModelAdmin):
    list_display = ('endpoint', 'event', 'response_status', 'sent_at')
