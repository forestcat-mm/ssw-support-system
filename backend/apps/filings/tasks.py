"""Django-Q2 から呼ぶジョブ（ADR-0005）。中身は services.filings に置く。"""

from django.utils import timezone

from services import filings


def create_scheduled_filings() -> str:
    """日次の起票（毎日 6:00 Asia/Tokyo）。止まっていた日があっても次回で追いつく（冪等）。"""
    result = filings.create_scheduled(timezone.localdate())
    return f"created={len(result.created)} existing={result.existing}"
