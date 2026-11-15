"""Template tags and filters for RBAC permission checks and display formatting."""
from django import template

from ..services import get_admin_profile

register = template.Library()


@register.simple_tag(takes_context=True)
def has_perm(context, perm_code):
    """Check if the current admin user has a permission.

    Usage in templates::

        {% load admin_tags %}
        {% has_perm 'users.delete' as can_delete %}
        {% if can_delete %}<button>Delete</button>{% endif %}
    """
    request = context.get('request')
    if not request or not request.user.is_authenticated:
        return False
    if request.user.is_superuser:
        return True
    profile = get_admin_profile(request.user, request)
    if profile:
        return profile.has_perm(perm_code)
    return False


@register.simple_tag(takes_context=True)
def is_super_admin(context):
    """Return True if the current user is a Django superuser or has role == 'super_admin'."""
    request = context.get('request')
    if not request or not request.user.is_authenticated:
        return False
    if request.user.is_superuser:
        return True
    profile = get_admin_profile(request.user, request)
    return bool(profile and profile.role == 'super_admin')


@register.filter
def format_interval(minutes):
    """Format an integer number of minutes into a readable interval string."""
    try:
        mins = max(1, int(minutes or 1))
    except (TypeError, ValueError):
        return str(minutes)
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
