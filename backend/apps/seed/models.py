from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TrackedModel


class SeedObject(TrackedModel):
    """seed_dummy が作った行の台帳。--reset はここに載っている行だけを消す。"""

    content_type = models.ForeignKey(
        ContentType, verbose_name=_("target type"), on_delete=models.PROTECT
    )
    object_id = models.PositiveBigIntegerField(_("target ID"))

    class Meta:
        verbose_name = _("dummy data record")
        verbose_name_plural = _("dummy data records")
        constraints = [
            models.UniqueConstraint(fields=["content_type", "object_id"], name="seed_object_unique")
        ]

    def __str__(self) -> str:
        return f"{self.content_type.model}:{self.object_id}"
