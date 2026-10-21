"""申請用の写真（U4。ADR-0047）: 保存（向き・EXIF・3:4）・履歴・6 か月・権限・監査ログ。"""

import io
from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone
from PIL import Image

from apps.core.models import AuditAction, AuditLog
from apps.workers.models import ForeignWorker, WorkerPhoto
from services import worker_photos, workers
from tests.core.factories import make_user, verified_client
from tests.workers.factories import make_worker

pytestmark = pytest.mark.django_db

GPS_TAG = 0x8825
MAKE_TAG = 0x010F
ORIENTATION_TAG = 0x0112


def jpeg(
    width: int = 400, height: int = 300, *, orientation: int | None = None
) -> SimpleUploadedFile:
    """位置（GPS）・端末の EXIF と、向き（Orientation）を付けた JPEG。"""
    image = Image.new("RGB", (width, height), (200, 120, 80))
    exif = Image.Exif()
    exif[MAKE_TAG] = "TestPhone"
    exif[GPS_TAG] = {1: "N", 2: (35.0, 41.0, 0.0)}
    if orientation:
        exif[ORIENTATION_TAG] = orientation
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return SimpleUploadedFile("photo.jpg", buffer.getvalue(), content_type="image/jpeg")


def png(width: int = 300, height: int = 400) -> SimpleUploadedFile:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buffer, format="PNG")
    return SimpleUploadedFile("photo.png", buffer.getvalue(), content_type="image/png")


def opened(field: object) -> Image.Image:
    with field.open("rb") as file:  # type: ignore[attr-defined]
        image = Image.open(io.BytesIO(file.read()))
        image.load()
        return image


def today() -> date:
    return timezone.localdate()


def test_orientation_is_applied_and_exif_is_removed() -> None:
    worker = make_worker()
    photo = worker_photos.register(
        worker, jpeg(400, 300, orientation=6), taken_on=today(), box=None, by=None
    )
    original = opened(photo.original)
    assert original.size == (300, 400)  # 向き 6（90 度）を画像に反映した
    for image in (original, opened(photo.cropped)):
        exif = image.getexif()
        assert MAKE_TAG not in exif and GPS_TAG not in exif and ORIENTATION_TAG not in exif
    cropped = opened(photo.cropped)
    assert cropped.format == "JPEG"
    assert cropped.size[1] * 3 == cropped.size[0] * 4  # 縦 4：横 3
    assert (photo.crop_width, photo.crop_height) == cropped.size


def test_box_is_fitted_to_3_4_inside_the_image() -> None:
    fitted = worker_photos.fit_box(400, 300, worker_photos.Box(350, 250, 300, 300))
    assert fitted.height * 3 == fitted.width * 4
    assert fitted.left + fitted.width <= 400 and fitted.top + fitted.height <= 300
    centre = worker_photos.fit_box(400, 300, None)
    assert centre == worker_photos.Box(87, 0, 225, 300)  # (400 - 225) // 2


def test_png_is_kept_as_png_and_the_photo_is_jpeg() -> None:
    photo = worker_photos.register(make_worker(), png(), taken_on=today(), box=None, by=None)
    assert photo.original_format == "png" and opened(photo.original).format == "PNG"
    assert opened(photo.cropped).format == "JPEG"


def test_other_files_and_future_dates_are_rejected() -> None:
    worker = make_worker()
    with pytest.raises(ValidationError):
        worker_photos.register(
            worker,
            SimpleUploadedFile("a.pdf", b"%PDF-1.4\n"),
            taken_on=today(),
            box=None,
            by=None,
        )
    with pytest.raises(ValidationError):
        worker_photos.register(
            worker, jpeg(), taken_on=today() + timedelta(days=1), box=None, by=None
        )
    assert not WorkerPhoto.objects.exists()


def test_new_photo_keeps_the_old_one_in_the_history_and_void_falls_back() -> None:
    worker = make_worker()
    first = worker_photos.register(worker, jpeg(), taken_on=date(2026, 1, 10), box=None, by=None)
    second = worker_photos.register(worker, jpeg(), taken_on=date(2026, 6, 10), box=None, by=None)
    assert worker_photos.latest(worker) == second
    assert worker_photos.history(worker) == [second, first]
    worker_photos.void(second, reason="別の人の写真", by=None)
    assert worker_photos.latest(worker) == first
    with pytest.raises(ValidationError):
        worker_photos.void(first, reason=" ", by=None)


@pytest.mark.parametrize(
    ("basis", "expected"),
    [(date(2026, 8, 31), date(2026, 2, 28)), (date(2026, 10, 21), date(2026, 4, 21))],
)
def test_six_months_before(basis: date, expected: date) -> None:
    assert worker_photos.months_before(basis, 6) == expected


def test_notice_for_missing_and_old_photos() -> None:
    worker = make_worker()
    assert worker_photos.notice(worker, date(2026, 10, 21)) == "申請用の写真が登録されていません。"
    worker_photos.register(worker, jpeg(), taken_on=date(2026, 4, 20), box=None, by=None)
    assert worker_photos.notice(worker, date(2026, 10, 21)).startswith(
        "写真が古い（2026-04-20 撮影）"
    )
    assert worker_photos.notice(worker, date(2026, 10, 20)) == ""


def test_registering_and_voiding_are_audited(admin_client: Client) -> None:
    worker = make_worker()
    response = admin_client.get(f"/workers/{worker.pk}/photos/new/")
    assert response.status_code == 200
    once = response.context["once_token"]
    posted = admin_client.post(
        f"/workers/{worker.pk}/photos/new/",
        {
            response.context["once_field"]: once,
            "upload": jpeg(),
            "taken_on": today().isoformat(),
            "crop_left": "10",
            "crop_top": "0",
            "crop_width": "150",
            "crop_height": "200",
        },
    )
    assert posted.status_code == 302
    photo = WorkerPhoto.objects.get()
    assert (photo.crop_left, photo.crop_width, photo.crop_height) == (10, 150, 200)
    assert AuditLog.objects.filter(
        action=AuditAction.CREATE, content_type__model="workerphoto", object_id=str(photo.pk)
    ).exists()


def test_image_is_not_audited_and_download_is(admin_client: Client) -> None:
    worker = make_worker(name_romaji="WANG FANG")
    photo = worker_photos.register(worker, jpeg(), taken_on=date(2026, 9, 1), box=None, by=None)
    before = AuditLog.objects.count()
    image = admin_client.get(f"/workers/{worker.pk}/photos/{photo.pk}/image/")
    assert image.status_code == 200 and image["Content-Type"] == "image/jpeg"
    assert AuditLog.objects.count() == before
    download = admin_client.get(f"/workers/{worker.pk}/photos/{photo.pk}/download/")
    assert download["Content-Disposition"] == 'attachment; filename="WANG_FANG_20260901.jpg"'
    assert (
        AuditLog.objects.filter(
            action=AuditAction.DOWNLOAD, content_type__model="workerphoto", object_id=str(photo.pk)
        ).count()
        == 1
    )


def test_only_people_who_can_see_the_worker_get_the_image(
    worker_with_photo: tuple[ForeignWorker, WorkerPhoto],
) -> None:
    worker, photo = worker_with_photo
    nobody = verified_client(make_user("nobody"))
    assert nobody.get(f"/workers/{worker.pk}/photos/{photo.pk}/image/").status_code == 403
    assert nobody.get(f"/workers/{worker.pk}/photos/{photo.pk}/download/").status_code == 403
    staff = verified_client(make_user("viewer3", roles=("support_staff",)))
    assert staff.get(f"/workers/{worker.pk}/photos/{photo.pk}/image/").status_code == 200
    other = make_worker(name_romaji="OTHER")
    assert staff.get(f"/workers/{other.pk}/photos/{photo.pk}/image/").status_code == 404


def test_deactivated_worker_cannot_get_a_new_photo(admin_client: Client) -> None:
    worker = make_worker()
    workers.deactivate(worker, by=None, reason="重複")
    assert admin_client.get(f"/workers/{worker.pk}/photos/new/").status_code == 403


def test_header_uses_the_photo_and_the_initial_without_it(
    support_staff_client: Client, worker_with_photo: tuple[ForeignWorker, WorkerPhoto]
) -> None:
    worker, photo = worker_with_photo
    html = support_staff_client.get(f"/workers/{worker.pk}/").content.decode()
    assert f"/workers/{worker.pk}/photos/{photo.pk}/image/" in html
    assert "写真 2026-09-01 撮影" in html
    plain = make_worker(name_kanji="李娜")
    html = support_staff_client.get(f"/workers/{plain.pk}/").content.decode()
    assert "person-photo__initial" in html and ">李<" in html


@pytest.fixture
def worker_with_photo() -> tuple[ForeignWorker, WorkerPhoto]:
    worker = make_worker()
    photo = worker_photos.register(worker, jpeg(), taken_on=date(2026, 9, 1), box=None, by=None)
    return worker, photo
