"""Template tags for RBAC permission checks."""
from django import template

from users_management.models import ROLE_DEFAULTS

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
    profile = getattr(request.user, 'admin_profile', None)
    if profile:
        return profile.has_perm(perm_code)
    return False
