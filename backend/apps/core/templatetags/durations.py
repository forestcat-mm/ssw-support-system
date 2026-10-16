from datetime import timedelta

from django import template

register = template.Library()


@register.filter
def minutes(value: timedelta | None) -> int:
    """timedelta を分（切り上げ）にする。ロック画面の解除までの時間表示用。"""
    if value is None:
        return 0
    return -(-int(value.total_seconds()) // 60)
