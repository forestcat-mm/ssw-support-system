"""業務画面の字体（Noto Sans JP・Noto Sans SC）のサブセットの woff2 を作る（開発用の道具。U1）。

入力は Google Fonts の公式の配布（github.com/google/fonts の ofl/notosansjp・ofl/notosanssc）の
可変の TTF。取得した URL・日付・SHA-256 は docs/decisions/0046-ui-redesign-2026-10.md に書く。
出力は backend/apps/core/static/ui/fonts/ の静的な woff2（本番は作った woff2 を配るだけで、外から読まない）。

- Noto Sans JP: 400・500・700。JIS X 0208 の字（第1・第2水準の漢字・かな・記号）と、ASCII・Latin-1・
  よく使う記号の区画、業務画面の文言（locale の .po とテンプレート）に出る字。
- Noto Sans SC: 400・700。GB 2312 の漢字（母国語の氏名〔簡体字〕が JP の字体に無いときに使う）。
  CSS の unicode-range を漢字の区画に絞るので、使わない画面では読み込まれない。

使い方（fonttools と brotli が要る。手元の venv などで）:
    pip install fonttools brotli
    python tools/fonts/make_web_fonts.py --jp NotoSansJP[wght].ttf --sc NotoSansSC[wght].ttf
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "backend" / "apps" / "core" / "static" / "ui" / "fonts"

BLOCKS = [
    (0x0020, 0x007E),  # ASCII
    (0x00A0, 0x00FF),  # Latin-1
    (0x0100, 0x017F),  # Latin Extended-A（ローマ字の氏名の補助記号）
    (0x2000, 0x206F),  # 一般の句読点
    (0x2100, 0x21FF),  # 文字のような記号・矢印
    (0x2200, 0x22FF),  # 数学の記号
    (0x2460, 0x24FF),  # 囲み英数字（①など）
    (0x2500, 0x25FF),  # 罫線・幾何学の図形（◆・■・□・〇など）
    (0x2600, 0x26FF),  # その他の記号
    (0x3000, 0x303F),  # CJK の記号と句読点
    (0x3040, 0x309F),  # ひらがな
    (0x30A0, 0x30FF),  # カタカナ
    (0x31F0, 0x31FF),  # カタカナの拡張
    (0xFF00, 0xFFEF),  # 全角・半角の形
]


def codec_chars(codec: str, lead: range, trail: range) -> set[str]:
    """2 バイトの文字コードの表（EUC-JP の JIS X 0208・GB 2312）にある字。"""
    out: set[str] = set()
    for a in lead:
        for b in trail:
            try:
                out.add(bytes([a, b]).decode(codec))
            except UnicodeDecodeError:
                continue
    return out


def ui_text_chars() -> set[str]:
    """業務画面の文言（.po の訳・テンプレート）に出る、ASCII 以外の字。"""
    chars: set[str] = set()
    files = [ROOT / "backend" / "locale" / "ja" / "LC_MESSAGES" / "django.po"]
    files += list((ROOT / "backend").glob("apps/*/templates/**/*.html"))
    for path in files:
        chars |= {c for c in path.read_text(encoding="utf-8") if ord(c) > 0x7F}
    return chars


def jp_chars() -> set[str]:
    chars = codec_chars("euc_jp", range(0xA1, 0xFF), range(0xA1, 0xFF))
    for start, end in BLOCKS:
        chars |= {chr(c) for c in range(start, end + 1)}
    return chars | ui_text_chars()


def sc_chars() -> set[str]:
    hanzi = codec_chars("gb2312", range(0xB0, 0xF8), range(0xA1, 0xFF))
    return {c for c in hanzi if re.match(r"[㐀-鿿]", c)}


def build(source: Path, weight: int, chars: set[str], target: Path) -> None:
    font = TTFont(source)
    static = instancer.instantiateVariableFont(font, {"wght": weight})
    options = subset.Options()
    options.flavor = "woff2"
    options.layout_features = ["*"]
    options.hinting = False
    options.name_IDs = ["*"]
    options.notdef_outline = True
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes=sorted(ord(c) for c in chars))
    subsetter.subset(static)
    target.parent.mkdir(parents=True, exist_ok=True)
    static.flavor = "woff2"
    static.save(target)
    print(f"{target.relative_to(ROOT)}: {target.stat().st_size // 1024} KB, {len(chars)} 字")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jp", type=Path, required=True, help="NotoSansJP[wght].ttf")
    parser.add_argument("--sc", type=Path, required=True, help="NotoSansSC[wght].ttf")
    args = parser.parse_args()
    jp = jp_chars()
    for weight in (400, 500, 700):
        build(args.jp, weight, jp, OUT / f"NotoSansJP-{weight}.woff2")
    sc = sc_chars()
    for weight in (400, 700):
        build(args.sc, weight, sc, OUT / f"NotoSansSC-{weight}.woff2")


if __name__ == "__main__":
    main()
