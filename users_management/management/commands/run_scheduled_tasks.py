"""Execute due ScheduledTasks from CLI / external system cron / Render Cron Jobs."""
import json

from django.core.management.base import BaseCommand

from users_management.views.scheduled_tasks import run_due_scheduled_tasks


class Command(BaseCommand):
    help = 'Execute due ScheduledTask jobs (backup_db, cleanup_tokens, weekly_report, health_check, etc.).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--all',
            action='store_true',
            help='Force run all active tasks even if interval has not elapsed.',
        )
        parser.add_argument(
            '--task-id',
            type=int,
            default=None,
            help='Run a specific ScheduledTask ID immediately.',
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='Output machine-readable JSON summary.',
        )

    def handle(self, *args, **options):
        results = run_due_scheduled_tasks(
            force_all=options['all'],
            task_id=options['task_id'],
        )
        if options['json']:
            self.stdout.write(json.dumps(results, indent=2, default=str))
            return

        if not results:
            self.stdout.write('No scheduled tasks due.')
            return

        for item in results:
            status = 'OK' if item.get('success') else 'FAIL'
            detail = item.get('output') or item.get('error') or ''
            self.stdout.write(f"[{status}] #{item['task_id']} {item['name']} ({item['task_type']}): {detail}")

