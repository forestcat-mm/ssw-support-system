"""seed_dummy の申請用の写真（U4。ADR-0047、U4-D6）。

人の顔ではない、色の面と丸の図（ダミーと分かる絵）を、外国人 1〜3 番に入れる。
3 番は撮影日を 7 か月前にして、申請の書類の一覧・一括申請に「写真が古い」が出る場面に
する。ほかの外国人は写真なし（頭文字の丸）。
"""

from __future__ import annotations

import io

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image, ImageDraw

from apps.seed.context import SeedContext
from services import worker_photos

# 外国人の番号 → 撮影日（基準日から何日前）と色
PHOTOS = {1: (30, (205, 214, 236)), 2: (60, (214, 232, 220)), 3: (215, (238, 226, 205))}


def dummy_image(color: tuple[int, int, int]) -> SimpleUploadedFile:
    image = Image.new("RGB", (450, 600), color)
    draw = ImageDraw.Draw(image)
    draw.ellipse((135, 120, 315, 300), fill=(255, 255, 255))
    draw.rectangle((90, 360, 360, 600), fill=(255, 255, 255))
    draw.text((20, 20), "DUMMY", fill=(90, 96, 112))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return SimpleUploadedFile("dummy.jpg", buffer.getvalue(), content_type="image/jpeg")


def seed_photos(ctx: SeedContext) -> None:
    for number, (days_ago, color) in PHOTOS.items():
        photo = worker_photos.register(
            ctx.worker(number),
            dummy_image(color),
            taken_on=ctx.days(-days_ago),
            box=None,
            by=None,
        )
        ctx.keep(photo)
