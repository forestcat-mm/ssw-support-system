from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TrackedModel


class SeedScope(models.TextChoices):
    """台帳の区分。dummy は seed_dummy、scenario は seed_scenario（手動のテストシナリオの本線）。"""

    DUMMY = "dummy", _("dummy data")
    SCENARIO = "scenario", _("test scenario")


class _ScopeManager(models.Manager["SeedObject"]):
    def __init__(self, scope: str) -> None:
        super().__init__()
        self.scope = scope

    def get_queryset(self) -> models.QuerySet["SeedObject"]:
        return super().get_queryset().filter(scope=self.scope)

    def create(self, **kwargs: object) -> "SeedObject":
        return super().create(**({"scope": self.scope} | kwargs))


class SeedObject(TrackedModel):
    """seed_dummy・seed_scenario が作った行の台帳。--reset はここに載っている行だけを消す。

    objects はダミーの行だけ（seed_dummy・seed_documents のための既定）。シナリオの行は
    scenario_objects で扱う（seed_dummy の「作成済み」の判定や --reset に混ぜない）。
    """

    content_type = models.ForeignKey(
        ContentType, verbose_name=_("target type"), on_delete=models.PROTECT
    )
    object_id = models.PositiveBigIntegerField(_("target ID"))
    scope = models.CharField(
        _("scope"), max_length=16, choices=SeedScope.choices, default=SeedScope.DUMMY
    )

    objects = _ScopeManager(SeedScope.DUMMY)
    scenario_objects = _ScopeManager(SeedScope.SCENARIO)
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("dummy data record")
        verbose_name_plural = _("dummy data records")
        constraints = [
            models.UniqueConstraint(fields=["content_type", "object_id"], name="seed_object_unique")
        ]

    def __str__(self) -> str:
        return f"{self.content_type.model}:{self.object_id}"


class SeedRun(TrackedModel):
    """seed_dummy の基準日（区分ごとに 1 行。開発用。fix2）。

    seed_documents が seed_dummy と同じ基準日で生成物を作るために読む。書くのは seed_dummy
    だけで、DEBUG と ALLOW_DUMMY_DATA が true のときだけ（builders.ensure_allowed）。
    本番の DB では空のまま。
    """

    scope = models.CharField(_("scope"), max_length=16, choices=SeedScope.choices, unique=True)
    base_on = models.DateField(_("base date"))

    class Meta:
        verbose_name = _("dummy data base date")
        verbose_name_plural = _("dummy data base dates")

    def __str__(self) -> str:
        return f"{self.scope}: {self.base_on}"
