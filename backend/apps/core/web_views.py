"""業務画面の共通の画面（トップ・部品の見本）。"""

from datetime import timedelta
from typing import Any

from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.generic import TemplateView

from apps.core.models import Action
from apps.core.permissions import ActionRequiredMixin
from apps.hosts.models import SpecifiedSkillField
from apps.workers.models import WorkerStatus
from services import deadlines


class HomeView(ActionRequiredMixin, TemplateView):
    """ログイン後のトップ（T14 で期限ダッシュボードに差し替える）。"""

    template_name = "ui/home.html"


class UiShowcaseView(ActionRequiredMixin, TemplateView):
    """部品の見本（管理者のみ）。デザインの確認と、部品を足すときの見本に使う。"""

    template_name = "ui/showcase.html"
    required_action = Action.SYSTEM_MANAGE

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()
        overdue = deadlines.residence_expiry(today - timedelta(days=5))
        within_7 = deadlines.residence_expiry(today + timedelta(days=3))
        within_14 = deadlines.residence_expiry(today + timedelta(days=12))
        open_ = deadlines.residence_expiry(today + timedelta(days=60))
        not_yet = deadlines.residence_expiry(today + timedelta(days=200))
        context["deadlines"] = [
            (_("overdue"), overdue, "Blush"),
            (_("within 7 days"), within_7, _("Butter, bold, with a dot")),
            (_("within 14 days"), within_14, "Mint Foam"),
            (_("open"), open_, _("outline only")),
            (_("not yet open"), not_yet, _("date only")),
        ]
        context["colors"] = [
            ("Canvas", "--color-canvas", "#fafafa", _("page background")),
            ("Pure White", "--color-surface", "#ffffff", _("cards and tables")),
            ("Ink", "--color-ink", "#262626", _("text")),
            ("Graphite", "--color-graphite", "#757577", _("secondary text")),
            ("Ash", "--color-ash", "#aeaeaf", _("borders of inputs, disabled")),
            (
                "Signal Blue",
                "--color-signal",
                "#008ae8",
                _("primary actions and active state only"),
            ),
            ("Sky Tint", "--color-sky", "#e0f2fe", _("active menu, before entry")),
            ("Blush", "--color-blush", "#fbcfe8", _("overdue, errors, danger")),
            ("Butter", "--color-butter", "#ffe77a", _("within 7 days")),
            ("Mint Foam", "--color-mint", "#b6edee", _("within 14 days, success")),
            ("Powder Blue", "--color-powder", "#afe4ff", _("industrial products field")),
            ("Lavender Mist", "--color-lavender", "#e6dafd", _("food manufacturing field")),
        ]
        context["fields"] = list(SpecifiedSkillField.objects.all())
        context["statuses"] = list(WorkerStatus.choices)
        context["deactivated_label"] = _("deactivated")
        context["rows"] = [
            {
                "kanji": "王芳",
                "romaji": "WANG FANG",
                "nationality": "中国",
                "host": "（サンプル）精機工業",
                "deadline": overdue,
                "staff": "佐藤",
                "status": "residing",
                "status_label": WorkerStatus.RESIDING.label,
            },
            {
                "kanji": "張偉",
                "romaji": "ZHANG WEI",
                "nationality": "中国",
                "host": "（サンプル）精機工業",
                "deadline": within_7,
                "staff": "鈴木",
                "status": "residing",
                "status_label": WorkerStatus.RESIDING.label,
            },
            {
                "kanji": "劉静",
                "romaji": "LIU JING",
                "nationality": "中国",
                "host": "（サンプル）食品",
                "deadline": within_14,
                "staff": "佐藤",
                "status": "left_job",
                "status_label": WorkerStatus.LEFT_JOB.label,
            },
            {
                "kanji": "",
                "romaji": "NGUYEN VAN AN",
                "nationality": "ベトナム",
                "host": "（サンプル）食品",
                "deadline": not_yet,
                "staff": "鈴木",
                "status": "pre_entry",
                "status_label": WorkerStatus.PRE_ENTRY.label,
            },
        ]
        return context
