"""業務画面の共通の画面（トップ・部品の見本）。"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.generic import TemplateView

from apps.core.models import Action
from apps.core.permissions import ActionRequiredMixin
from apps.hosts.models import SpecifiedSkillField
from apps.workers.models import WorkerStatus
from services import deadlines


class HomeView(ActionRequiredMixin, TemplateView):
    """ログイン後のトップ（T14 で期限ダッシュボードに差し替える）。"""

    template_name = "ui/home.html"


@dataclass(frozen=True)
class DesignSet:
    """見本の画面で見比べるデザインのセット（T13a。1 つに決めたら不採用のセットは削除する）。"""

    key: str
    name: str
    source: str
    stylesheet: str | None  # tokens.css の後ろに重ねるファイル（A は tokens.css そのもの）
    summary: Any  # 遅延翻訳の文字列


DESIGN_SETS = [
    DesignSet(
        "a",
        "Aboard",
        "",
        None,
        gettext_lazy("Neutral canvas, Signal Blue for primary actions only, pastel statuses."),
    ),
    DesignSet(
        "b",
        "ElevenLabs",
        "https://styles.refero.design/style/031056ff-7af1-46db-8daa-115f731c5d26",
        "ui/tokens-b.css",
        gettext_lazy("Three warm surfaces, black pill buttons, no colored buttons."),
    ),
    DesignSet(
        "c",
        "Workable",
        "https://styles.refero.design/style/0ab4c544-6147-4998-8365-3a0f6191e54f",
        "ui/tokens-c.css",
        gettext_lazy("White cards on a warm canvas, Forest Teal for primary actions, no shadows."),
    ),
    DesignSet(
        "d",
        "Officevibe",
        "https://styles.refero.design/style/ced1c98f-d489-48f7-a01f-1fa59a07b706",
        "ui/tokens-d.css",
        gettext_lazy(
            "Navy navigation and primary buttons, cobalt for focus and links, cream lines."
        ),
    ),
    DesignSet(
        "e",
        "Glide",
        "https://styles.refero.design/style/a80cb179-2d90-4896-b52d-4e674b421498",
        "ui/tokens-e.css",
        gettext_lazy(
            "Three gray surfaces, black structural lines, teal for focus only, 6px corners."
        ),
    ),
]

# 色見本に出す役割（値はページの CSS 変数から読むので、どのセットでも同じ一覧で済む）
SWATCHES = [
    ("--color-canvas", gettext_lazy("page background")),
    ("--color-surface", gettext_lazy("cards and tables")),
    ("--color-band", gettext_lazy("bands and row hover")),
    ("--color-field", gettext_lazy("input background")),
    ("--color-ink", gettext_lazy("headings and UI text")),
    ("--color-text-body", gettext_lazy("body text")),
    ("--color-ink-2", gettext_lazy("strong secondary text")),
    ("--color-graphite", gettext_lazy("secondary text")),
    ("--color-ash", gettext_lazy("faintest text, disabled")),
    ("--color-line-structure", gettext_lazy("structural lines")),
    ("--color-line", gettext_lazy("light lines")),
    ("--input-border", gettext_lazy("input borders")),
    ("--color-accent", gettext_lazy("focus and active state")),
    ("--button-primary-bg", gettext_lazy("primary button")),
    ("--nav-bg", gettext_lazy("navigation background")),
    ("--level-overdue-bg", gettext_lazy("overdue")),
    ("--level-within-7-bg", gettext_lazy("within 7 days")),
    ("--level-within-14-bg", gettext_lazy("within 14 days")),
    ("--level-open-border", gettext_lazy("open (outline)")),
    ("--field-industrial-bg", gettext_lazy("industrial products field")),
    ("--field-food-bg", gettext_lazy("food manufacturing field")),
]

# 一覧の見本 30 行（架空の氏名。簡体字・ローマ字・日本語が同じ行に並ぶ）
SAMPLE_NAMES = [
    ("王芳", "WANG FANG"),
    ("张伟", "ZHANG WEI"),
    ("刘静", "LIU JING"),
    ("李娜", "LI NA"),
    ("陈杰", "CHEN JIE"),
    ("杨洋", "YANG YANG"),
    ("赵磊", "ZHAO LEI"),
    ("黄敏", "HUANG MIN"),
    ("周涛", "ZHOU TAO"),
    ("吴霞", "WU XIA"),
    ("徐丽", "XU LI"),
    ("孙浩", "SUN HAO"),
    ("马超", "MA CHAO"),
    ("朱婷", "ZHU TING"),
    ("胡斌", "HU BIN"),
    ("郭欣", "GUO XIN"),
    ("何勇", "HE YONG"),
    ("高燕", "GAO YAN"),
    ("林峰", "LIN FENG"),
    ("罗琳", "LUO LIN"),
    ("郑凯", "ZHENG KAI"),
    ("梁雪", "LIANG XUE"),
    ("谢晨", "XIE CHEN"),
    ("宋佳", "SONG JIA"),
    ("唐亮", "TANG LIANG"),
    ("", "NGUYEN VAN AN"),
    ("", "TRAN THI HOA"),
    ("", "LE VAN NAM"),
    ("", "SITI RAHAYU"),
    ("", "BUDI SANTOSO"),
]
# 在留期限（今日からの日数）。超過 1・7日以内 2・14日以内 2・対応期間中 6・受付前 19
SAMPLE_DAYS = [
    -5,
    3,
    6,
    10,
    13,
    25,
    40,
    55,
    70,
    80,
    88,
    120,
    150,
    180,
    200,
    230,
    260,
    300,
    330,
    365,
    400,
    430,
    460,
    500,
    540,
    600,
    640,
    700,
    760,
    800,
]


def sample_rows(today: date) -> list[dict[str, Any]]:
    hosts = ["（サンプル）精機工業株式会社", "（サンプル）食品株式会社"]
    staff = ["佐藤 花子", "鈴木 次郎"]
    nationalities = ["中国"] * 25 + ["ベトナム"] * 3 + ["インドネシア"] * 2
    letters = "ABCDEFGHJKLMNPRSTUVWXYZ"
    rows = []
    for index, ((kanji, romaji), days) in enumerate(zip(SAMPLE_NAMES, SAMPLE_DAYS, strict=True)):
        status = WorkerStatus.RESIDING
        if index in (7, 19):
            status = WorkerStatus.LEFT_JOB
        elif index == 27:
            status = WorkerStatus.PRE_ENTRY
        elif index == 29:
            status = WorkerStatus.RETURNED
        # 在留カード番号の形（英字 2・数字 8・英字 2）のダミー。入国前は未交付
        digits = (index + 1) * 30_791_327 % 100_000_000
        prefix = letters[index % 23] + letters[index * 7 % 23]
        suffix = letters[index * 5 % 23] + letters[index * 3 % 23]
        card = "" if status == WorkerStatus.PRE_ENTRY else f"{prefix}{digits:08d}{suffix}"
        rows.append(
            {
                "kanji": kanji,
                "romaji": romaji,
                "nationality": nationalities[index],
                "host": hosts[index % 2],
                "card": card,
                "wage": f"¥{205_000 + (index * 3_700) % 40_000:,}",
                "deadline": deadlines.residence_expiry(today + timedelta(days=days)),
                "staff": staff[index % 2],
                "status": status.value,
                "status_label": _("deactivated") if index == 28 else status.label,
                "deactivated": index == 28,
            }
        )
    return rows


class UiShowcaseView(ActionRequiredMixin, TemplateView):
    """部品の見本（管理者のみ）。デザインの確認と、部品を足すときの見本に使う。

    ?theme=b〜e で比較セットのトークンを重ねる（T13a。この画面だけに当たり、admin には当てない）。
    """

    template_name = "ui/showcase.html"
    required_action = Action.SYSTEM_MANAGE

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        sets = {design_set.key: design_set for design_set in DESIGN_SETS}
        context["design_sets"] = DESIGN_SETS
        context["design_set"] = sets.get(self.request.GET.get("theme", ""), DESIGN_SETS[0])

        today = timezone.localdate()
        context["deadlines"] = [
            (_("overdue"), deadlines.residence_expiry(today - timedelta(days=5)), _("fill")),
            (
                _("within 7 days"),
                deadlines.residence_expiry(today + timedelta(days=3)),
                _("fill, bold, with a dot"),
            ),
            (
                _("within 14 days"),
                deadlines.residence_expiry(today + timedelta(days=12)),
                _("fill"),
            ),
            (_("open"), deadlines.residence_expiry(today + timedelta(days=60)), _("outline only")),
            (
                _("not yet open"),
                deadlines.residence_expiry(today + timedelta(days=200)),
                _("date only"),
            ),
        ]
        context["swatches"] = SWATCHES
        context["fields"] = list(SpecifiedSkillField.objects.all())
        context["statuses"] = list(WorkerStatus.choices)
        context["deactivated_label"] = _("deactivated")

        rows = sample_rows(today)
        context["rows"] = rows
        levels = [deadlines.assess(row["deadline"], today).level for row in rows]
        context["level_counts"] = [
            (
                "badge--overdue",
                _("Overdue: %(count)s") % {"count": levels.count(deadlines.Level.OVERDUE)},
            ),
            (
                "badge--within-7",
                _("Within 7 days: %(count)s") % {"count": levels.count(deadlines.Level.WITHIN_7)},
            ),
            (
                "badge--within-14",
                _("Within 14 days: %(count)s") % {"count": levels.count(deadlines.Level.WITHIN_14)},
            ),
        ]
        return context
