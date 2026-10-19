from django import template

from dashboard import day_view

register = template.Library()


@register.simple_tag
def friendly_day(dc):
    """The plain-language model of one day_compare payload (see dashboard.day_view)."""
    return day_view.friendly(dc)
