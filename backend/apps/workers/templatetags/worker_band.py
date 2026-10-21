"""外国人に関わる画面の上に留める細い帯（仕様 5.3・見本 07。U6）。"""

from typing import Any

from django import template

from apps.workers.models import ForeignWorker

register = template.Library()


@register.simple_tag(takes_context=True)
def person_band_for(context: template.Context) -> dict[str, Any] | None:
    """context の worker が外国人なら細い帯の中身。詳細の画面は自分の帯を出すので使わない。"""
    from apps.workers.views import person_band

    worker = context.get("worker")
    request = context.get("request")
    if not isinstance(worker, ForeignWorker) or request is None:
        return None
    return person_band(request, worker)
