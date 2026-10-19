"""PDF の読み込みの確かめと、画像（JPEG・PNG）の 1 頁の PDF（提出用の統合 PDF。T42、ADR-0033）。

- 画像は圧縮し直さない（T42-D7）。JPEG はそのまま（DCTDecode）、PNG は IDAT をそのまま
  （FlateDecode と PNG の予測子）埋め込む。透過（アルファ）・インターレースの PNG、CMYK の JPEG は
  扱わない（UnreadableFile。PDF か JPEG にしてから綴じてもらう）。
- 頁は A4（横長の画像は A4 横）。画像の縦横比のまま、頁いっぱいに中央に置く。
- PDF は pypdf で読めるか（壊れていない・パスワードが無い・頁がある）を確かめる。在留申請オンライン
  システムは、セキュリティの設定がある PDF をエラーファイルとして扱う（Q&A Q4-2）。
"""

from __future__ import annotations

import io
import struct
import zlib
from dataclasses import dataclass

from pypdf import PdfReader

A4 = (595.276, 841.89)  # pt
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
# JPEG の SOF（フレームの始まり）のマーカー。C4・C8・CC は SOF ではない
SOF_MARKERS = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE})


class UnreadableFile(Exception):
    """綴じられないファイル（理由は code。個人情報・ファイル名を含めない）。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def kind_of(data: bytes) -> str:
    """ファイルの先頭のバイトで種類を決める（pdf・jpeg・png。ほかは空）。"""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(JPEG_SIGNATURE):
        return "jpeg"
    if data.startswith(PNG_SIGNATURE):
        return "png"
    return ""


def pdf_reader(data: bytes) -> PdfReader:
    """読める PDF の PdfReader（壊れている・パスワード付き・頁が無いときは UnreadableFile）。"""
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise UnreadableFile("encrypted")
        if len(reader.pages) == 0:
            raise UnreadableFile("no_pages")
    except UnreadableFile:
        raise
    except Exception as error:  # noqa: BLE001  pypdf の例外は種類が多い。読めないとして扱う
        raise UnreadableFile("broken") from error
    return reader


def as_pdf(data: bytes) -> bytes:
    """PDF ならそのまま（読めるかを確かめる）、画像なら 1 頁の PDF にする。"""
    kind = kind_of(data)
    if kind == "pdf":
        pdf_reader(data)
        return data
    if kind == "jpeg":
        return image_pdf(jpeg_image(data))
    if kind == "png":
        return image_pdf(png_image(data))
    raise UnreadableFile("unsupported")


@dataclass(frozen=True)
class Image:
    width: int
    height: int
    entries: dict[str, str]  # 画像の XObject の辞書（Width・Height を除く）
    stream: bytes


def jpeg_image(data: bytes) -> Image:
    position = 2
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            raise UnreadableFile("broken")
        marker = data[position + 1]
        if marker == 0xFF:  # 詰め物
            position += 1
            continue
        if marker in (0x01, *range(0xD0, 0xD9)):  # 長さを持たないマーカー
            position += 2
            continue
        (length,) = struct.unpack(">H", data[position + 2 : position + 4])
        if marker in SOF_MARKERS:
            if position + 10 > len(data):
                break
            height, width = struct.unpack(">HH", data[position + 5 : position + 9])
            components = data[position + 9]
            spaces = {1: "/DeviceGray", 3: "/DeviceRGB"}
            if components not in spaces or not width or not height:
                raise UnreadableFile("unsupported_image")
            entries = {
                "ColorSpace": spaces[components],
                "BitsPerComponent": "8",
                "Filter": "/DCTDecode",
            }
            return Image(width, height, entries, data)
        position += 2 + length
    raise UnreadableFile("broken")


def png_image(data: bytes) -> Image:
    position = len(PNG_SIGNATURE)
    header: tuple[int, ...] | None = None
    palette = b""
    chunks: list[bytes] = []
    while position + 8 <= len(data):
        length, name = struct.unpack(">I4s", data[position : position + 8])
        body = data[position + 8 : position + 8 + length]
        if name == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif name == b"PLTE":
            palette = body
        elif name == b"IDAT":
            chunks.append(body)
        elif name == b"IEND":
            break
        position += 12 + length
    if header is None or not chunks:
        raise UnreadableFile("broken")
    width, height, depth, color_type, _compression, _filter, interlace = header
    colors = {0: 1, 2: 3, 3: 1}.get(color_type)
    if colors is None or interlace != 0 or depth not in (1, 2, 4, 8, 16):
        raise UnreadableFile("unsupported_image")  # 透過・インターレース
    if color_type == 0:
        space = "/DeviceGray"
    elif color_type == 2:
        space = "/DeviceRGB"
    else:
        if not palette or depth == 16:
            raise UnreadableFile("broken")
        space = f"[/Indexed /DeviceRGB {len(palette) // 3 - 1} <{palette.hex()}>]"
    stream = b"".join(chunks)
    try:
        zlib.decompressobj().decompress(stream, 64)  # 壊れていないかの確かめ（先頭だけ）
    except zlib.error as error:
        raise UnreadableFile("broken") from error
    entries = {
        "ColorSpace": space,
        "BitsPerComponent": str(depth),
        "Filter": "/FlateDecode",
        "DecodeParms": (
            f"<< /Predictor 15 /Colors {colors} /BitsPerComponent {depth} /Columns {width} >>"
        ),
    }
    return Image(width, height, entries, stream)


def _place(image: Image) -> tuple[float, float, float, float, float, float]:
    """頁の大きさと、画像を置く位置・大きさ（縦横比のまま頁いっぱいに中央）。"""
    page_w, page_h = A4 if image.height >= image.width else (A4[1], A4[0])
    scale = min(page_w / image.width, page_h / image.height)
    width, height = image.width * scale, image.height * scale
    return page_w, page_h, (page_w - width) / 2, (page_h - height) / 2, width, height


def image_pdf(image: Image) -> bytes:
    """画像 1 枚の 1 頁の PDF（PDF 1.4。暗号化・制限なし）。"""
    page_w, page_h, x, y, width, height = _place(image)
    content = f"q {width:.3f} 0 0 {height:.3f} {x:.3f} {y:.3f} cm /Im0 Do Q".encode()
    entries = " ".join(f"/{key} {value}" for key, value in image.entries.items())
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w:.3f} {page_h:.3f}] "
            "/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>"
        ).encode(),
        (
            f"<< /Type /XObject /Subtype /Image /Width {image.width} /Height {image.height} "
            f"{entries} /Length {len(image.stream)} >>\nstream\n"
        ).encode()
        + image.stream
        + b"\nendstream",
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()
