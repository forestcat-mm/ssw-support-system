"""申請用の写真（U4。ADR-0047、仕様 6）。

- 受けるのは JPEG・PNG（10MB まで）。向き（EXIF の Orientation）だけを画像に
  反映し、位置（GPS）・端末などの EXIF は元の画像からも切り抜いた画像からも消して
  保存する（U4-D2 と補い）。色・左右反転などの加工はしない（提出写真の規格）。
- 縦 3:4（顔写真の規格: 縦 4・横 3）に切り抜く。枠は画面で決め、無ければ真ん中。
- 撮影日が基準日の 6 か月前より前なら「写真が古い」（申請の日前 6 か月以内。U4-D4）。
- 監査ログ: 登録（作成）・取消（更新）はモデルの保存で残る。ダウンロードは
  download_jpeg が残す。画面に表示するための取り出しは残さない（U4-D5 と補い）。
"""

from __future__ import annotations

import datetime
import io
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files import File
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from PIL import Image, ImageOps, UnidentifiedImageError

from apps.core.models import AuditAction, User
from apps.workers.models import ForeignWorker, WorkerPhoto
from services import audit, workers
from services.files import MB, FilePolicy, validate_file

PHOTO_POLICY = FilePolicy(extensions=frozenset({"jpg", "jpeg", "png"}), max_bytes=10 * MB)
RATIO = (3, 4)  # 横:縦
STALE_MONTHS = 6
JPEG_QUALITY = 92


@dataclass(frozen=True)
class Box:
    left: int
    top: int
    width: int
    height: int


def latest(worker: ForeignWorker) -> WorkerPhoto | None:
    """画面に使う写真（取り消していない最新の 1 枚）。"""
    return worker.photos.filter(voided_at__isnull=True).order_by("-created_at", "-pk").first()


def history(worker: ForeignWorker) -> list[WorkerPhoto]:
    return list(
        worker.photos.select_related("created_by", "voided_by").order_by("-created_at", "-pk")
    )


def months_before(day: datetime.date, months: int) -> datetime.date:
    """暦の months か月前の同じ日（無ければその月の末日）。"""
    month = day.month - months
    year = day.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    for candidate in (31, 30, 29, 28):
        try:
            return day.replace(year=year, month=month, day=min(day.day, candidate))
        except ValueError:
            continue
    raise ValueError(day)


def is_stale(photo: WorkerPhoto, basis_on: datetime.date) -> bool:
    """申請の日前 6 か月より前に撮影した写真か（暦の 6 か月前の同じ日より前）。"""
    return photo.taken_on < months_before(basis_on, STALE_MONTHS)


def notice(worker: ForeignWorker, basis_on: datetime.date) -> str:
    """申請の書類の一覧・一括申請に出す注意（無ければ空）。"""
    photo = latest(worker)
    if photo is None:
        return _("No application photo is registered.")
    if is_stale(photo, basis_on):
        return _(
            "The application photo is old (taken on %(day)s). It must be taken within 6 months "
            "before the application."
        ) % {"day": photo.taken_on.isoformat()}
    return ""


def fit_box(width: int, height: int, box: Box | None) -> Box:
    """切り抜きの枠を、画像の中の縦 3:4 に直す（無ければ真ん中のいちばん大きい 3:4）。"""
    if box is None or box.width <= 0 or box.height <= 0:
        crop_w = min(width, height * RATIO[0] // RATIO[1])
        crop_h = crop_w * RATIO[1] // RATIO[0]
        return Box((width - crop_w) // 2, (height - crop_h) // 2, crop_w, crop_h)
    crop_w = min(box.width, width, height * RATIO[0] // RATIO[1])
    crop_h = crop_w * RATIO[1] // RATIO[0]
    left = min(max(box.left, 0), width - crop_w)
    top = min(max(box.top, 0), height - crop_h)
    return Box(left, top, crop_w, crop_h)


def _encode(image: Image.Image, image_format: str) -> bytes:
    """EXIF を付けずに書き出す（Pillow は exif を渡さなければ書かない）。"""
    buffer = io.BytesIO()
    if image_format == "PNG":
        image.save(buffer, format="PNG", optimize=True)
    else:
        image.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue()


def _open(upload: File[Any]) -> tuple[Image.Image, str]:
    validate_file(upload, PHOTO_POLICY)
    upload.seek(0)
    try:
        image = Image.open(upload)
        image_format = (image.format or "").upper()
        image.load()
    except (UnidentifiedImageError, OSError) as error:
        raise ValidationError(
            {"upload": _("The image could not be read. Use a JPEG or PNG file.")}
        ) from error
    if image_format not in ("JPEG", "PNG"):
        raise ValidationError({"upload": _("Use a JPEG or PNG file.")})
    # 向きだけを反映する（EXIF の Orientation）。ほかの EXIF は書き出すときに付けない
    return ImageOps.exif_transpose(image) or image, image_format


@transaction.atomic
def register(
    worker: ForeignWorker,
    upload: File[Any],
    *,
    taken_on: datetime.date,
    box: Box | None,
    by: User | None,
) -> WorkerPhoto:
    """新しい写真を足す（前の写真は履歴に残る）。"""
    if taken_on > timezone.localdate():
        raise ValidationError({"taken_on": _("The date taken cannot be in the future.")})
    image, image_format = _open(upload)
    fitted = fit_box(image.width, image.height, box)
    cropped = image.crop(
        (fitted.left, fitted.top, fitted.left + fitted.width, fitted.top + fitted.height)
    )
    extension = "png" if image_format == "PNG" else "jpg"
    photo = WorkerPhoto(
        worker=worker,
        original_format=image_format.lower(),
        crop_left=fitted.left,
        crop_top=fitted.top,
        crop_width=fitted.width,
        crop_height=fitted.height,
        taken_on=taken_on,
        created_by=by,
        updated_by=by,
    )
    photo.original.save(
        f"original.{extension}", ContentFile(_encode(image, image_format)), save=False
    )
    photo.cropped.save("photo.jpg", ContentFile(_encode(cropped, "JPEG")), save=False)
    photo.save()
    return photo


def void(photo: WorkerPhoto, *, reason: str, by: User | None) -> None:
    workers.void_record(photo, by=by, reason=reason)


def image_bytes(photo: WorkerPhoto) -> bytes:
    """画面に出す切り抜いた画像（監査ログは残さない。画面の閲覧に含める）。"""
    with photo.cropped.open("rb") as file:
        content: bytes = file.read()
    return content


def jpeg_filename(photo: WorkerPhoto) -> str:
    """ダウンロードの名前「<ローマ字の氏名>_<撮影日>.jpg」。"""
    name = "_".join(photo.worker.name_romaji.split()) or "photo"
    return f"{name}_{photo.taken_on:%Y%m%d}.jpg"


def download_jpeg(photo: WorkerPhoto, *, by: User | None) -> bytes:
    """オンライン申請に出す JPEG（切り抜いた画像）。監査ログに download を残す。"""
    actor: dict[str, Any] = {"actor": by} if by is not None else {}
    with audit.event(
        AuditAction.DOWNLOAD,
        target=photo,
        context={"worker": photo.worker_id, "photo": photo.pk, "taken_on": photo.taken_on},
        **actor,
    ):
        return image_bytes(photo)
