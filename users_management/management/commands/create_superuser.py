from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from users_management.models import AdminProfile


class Command(BaseCommand):
    help = 'Create the first Django admin superuser and AdminProfile.'

    def add_arguments(self, parser):
        parser.add_argument('--username', type=str, help='Login username')
        parser.add_argument('--email', type=str, help='Email address')
        parser.add_argument('--password', type=str, help='Password')
        parser.add_argument('--first-name', type=str, default='', help='First name')
        parser.add_argument('--last-name', type=str, default='', help='Last name')

    def handle(self, *args, **options):
        username = options['username']
        email = options['email']
        password = options['password']
        first_name = options['first_name']
        last_name = options['last_name']

        if not all([username, email, password]):
            self.stdout.write(
                self.style.ERROR('Missing required args: --username, --email, --password')
            )
            return

        if User.objects.filter(is_superuser=True).exists():
            self.stdout.write(
                self.style.WARNING('A superuser already exists.')
            )
            return

        if User.objects.filter(username=username).exists():
            self.stdout.write(
                self.style.ERROR(f'Username "{username}" already exists.')
            )
            return

        if User.objects.filter(email=email).exists():
            self.stdout.write(
                self.style.ERROR(f'Email "{email}" is already in use.')
            )
            return

        try:
            user = User.objects.create_superuser(
                username=username,
                email=email,
                password=password,
                first_name=first_name,
                last_name=last_name,
            )

            AdminProfile.objects.create(
                user=user,
                role='super_admin',
            )

            self.stdout.write(
                self.style.SUCCESS(
                    f'Created superuser: {username}\n'
                    f'Email: {email}\n'
                    f'Login URLs: /admin/ or /dashboard/'
                )
            )
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f'Failed to create superuser: {str(e)}')
            )
