"""第1-6号・第1-17号の中国語（簡体字）の訳文の版を作る（自作の訳文。開発用。T43、ADR-0035）。

日本語の版の写しの固定の文だけを、訳の表（tools/forms/translations/<様式>.zh-hans.json）の中国語に置き換える。
欄の位置・差込タグ・セル・結合・罫線・入力規則は日本語の版と同じ。
- 第1-6号（Word）: template.docx の段落ごとに、差込タグを {1}・{2}… にした文で表を引き、最初の文字の要素の
  書式で文を入れ、差込タグだけの文字の要素（下線つきの記入欄など）は元の書式のまま残す（T43-D9 の直し）。
  字体は中国語の字体（Noto Serif CJK SC）にそろえる（styles.xml の既定も。T43-D7 の直し。頁が増えてよい）。
  差込タグの空のときの文（or の後）も表で訳す。
- 第1-17号（Excel）: 共有の文字列（xl/sharedStrings.xml）を訳し（ふりがなを外す）、字体を中国語の字体にする
  （styles.xml の字体の名前・文字セットだけ。明朝は Noto Serif CJK SC、ほかは Noto Sans CJK SC）。
  シート・結合・罫線・数式は変えない。
- マッピングは日本語の版の写しで、印刷済みの文に差し込む template の文だけを訳す。規則は日本語の版の写しに
  「訳の無い日本語の値」の警告を足す。manifest は自作の様式（source.self_made）で、訳の元の版を書く。
- 対照表（docs/translations/<様式>.zh-hans.md・values.zh-hans.md）を訳の表から作る。

訳の無い日本語の文が残れば止まる。出力は手で直さない（テストで、出力が登録済みのファイルと同じことを
確かめる）。標準ライブラリだけで動く。

使い方（リポジトリの直下で）:
    python3 tools/forms/make_translation_templates.py [出力の根（既定はリポジトリの直下）]
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
LANGUAGE = "zh-hans"
TOOL = "tools/forms/make_translation_templates.py"
VALUES = "backend/services/data/translations/zh-hans.json"
FORMS = {"1-6": "2026-10-18", "1-17": "2026-10-17"}
JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
KANA = re.compile(r"[\u3041-\u3096\u30a1-\u30fa]")  # 中国語に無い字（「・」「ー」は除く）
TAG = re.compile(r'\{\{\s*(\w+)\s+or\s+"([^"]*)"\s*\}\}')
PLACEHOLDER = re.compile(r"\{(\d+)\}")
PARAGRAPH = re.compile(r"<w:p[ >].*?</w:p>", re.S)
TEXT = re.compile(r"(<w:t(?:\s[^>]*)?>)([^<]*)(</w:t>)")
SI = re.compile(r"<si>(.*?)</si>", re.S)


class TranslationError(Exception):
    pass


def unescape(text: str) -> str:
    return html.unescape(text)


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def table(form_id: str) -> dict[str, Any]:
    path = REPO / "tools" / "forms" / "translations" / f"{form_id}.{LANGUAGE}.json"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def copy_zip(source: Path, target: Path, replaced: dict[str, bytes]) -> None:
    """zip の中身をそのまま写し、replaced の名前だけを差し替える（日時・圧縮も元のまま）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w") as archive:
        for info in original.infolist():
            data = replaced.get(info.filename, original.read(info.filename))
            copy = zipfile.ZipInfo(info.filename, info.date_time)
            copy.compress_type = info.compress_type
            copy.external_attr = info.external_attr
            archive.writestr(copy, data)


# --- Word（第1-6号）---------------------------------------------------------------------


def _key(text: str) -> tuple[str, list[tuple[str, str]]]:
    tags: list[tuple[str, str]] = []

    def placeholder(match: re.Match[str]) -> str:
        tags.append((match.group(1), match.group(2)))
        return "{%d}" % len(tags)

    return TAG.sub(placeholder, text), tags


def _translated_text(text: str, data: dict[str, Any]) -> str:
    key, tags = _key(text)
    if not JAPANESE.search(key):
        found = key
    elif key in data["paragraphs"]:
        found = data["paragraphs"][key]
    else:
        raise TranslationError(f"no translation for the paragraph: {key!r}")

    def tag(match: re.Match[str]) -> str:
        name, default = tags[int(match.group(1)) - 1]
        if JAPANESE.search(default):
            if default not in data["defaults"]:
                raise TranslationError(f"no translation for the tag default: {default!r}")
            default = data["defaults"][default]
        return '{{ %s or "%s" }}' % (name, default)

    result = PLACEHOLDER.sub(tag, found)
    if sorted(n for n, _ in TAG.findall(result)) != sorted(n for n, _ in TAG.findall(text)):
        raise TranslationError(f"tags differ after translation: {key!r}")
    return result


FONT = "Noto Serif CJK SC"  # 訳文の字体（中国語の字体にそろえる。T43-D7 の直し）
SANS = "Noto Sans CJK SC"
RUN = re.compile(r"<w:r(?:\s[^>]*)?>.*?</w:r>", re.S)
RFONTS = re.compile(r"<w:rFonts\b[^>]*/>")
SC_FONTS = f'<w:rFonts w:ascii="{FONT}" w:hAnsi="{FONT}" w:eastAsia="{FONT}" w:cs="{FONT}"/>'


def _sc_fonts(run: str) -> str:
    """文字の要素の字体を中国語の字体にする。"""
    if RFONTS.search(run):
        return RFONTS.sub(SC_FONTS, run, count=1)
    if "<w:rPr>" in run:
        return run.replace("<w:rPr>", "<w:rPr>" + SC_FONTS, 1)
    head = re.match(r"<w:r(?:\s[^>]*)?>", run)
    assert head is not None
    return run[: head.end()] + "<w:rPr>" + SC_FONTS + "</w:rPr>" + run[head.end() :]


def _run_text(run: str) -> str:
    return "".join(unescape(body) for _, body, _ in TEXT.findall(run))


def _text_run(properties: str, text: str) -> str:
    return f'<w:r>{properties}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _underlined_tail(runs: list[re.Match[str]]) -> tuple[int, str, str] | None:
    """段落の終わりの、下線の付いた文字の要素の続き（例「約｛タグ｝円（合計）」）。

    (元の続きの中で最初のタグの前の字数, 続きの書式, 続きの最初のタグ) を返す。無ければ None。
    """
    texts = [(_run_text(run.group(0)), run.group(0)) for run in runs]
    index = len(texts)
    while index > 0 and (not texts[index - 1][0] or "<w:u " in texts[index - 1][1]):
        index -= 1
    tail = [item for item in texts[index:] if item[0]]
    joined = "".join(text for text, _ in tail)
    tag = TAG.search(joined)
    if not tail or tag is None or TAG.fullmatch(joined):
        return None
    found = re.search(r"<w:rPr>.*?</w:rPr>", tail[0][1], re.S)
    return tag.start(), found.group(0) if found else "", tag.group(0)


def _pieces(new: str, tag_runs: dict[str, str], properties: str) -> list[str]:
    pieces: list[str] = []
    literal = ""
    position = 0
    for match in TAG.finditer(new):
        literal += new[position : match.start()]
        position = match.end()
        kept = tag_runs.pop(match.group(0), None)
        if kept is None:
            literal += match.group(0)
            continue
        if literal:
            pieces.append(_text_run(properties, literal))
            literal = ""
        pieces.append(kept)
    literal += new[position:]
    if literal:
        pieces.append(_text_run(properties, literal))
    return pieces


def _translate_paragraph(paragraph: str, data: dict[str, Any]) -> str:
    """段落を訳して組み直す。差込タグだけの文字の要素（下線つきの記入欄など）は元の書式のまま残し、
    段落の終わりの下線の続き（「約｛タグ｝円（合計）」など）は訳でも同じ書式にする（T43-D9 の直し）。
    ほかの文は最初の文字の要素の書式で入れる。字体は中国語の字体にする。"""
    runs = list(RUN.finditer(paragraph))
    if not runs:
        return paragraph
    text = "".join(_run_text(run.group(0)) for run in runs)
    if not JAPANESE.search(text):
        return RUN.sub(lambda m: _sc_fonts(m.group(0)), paragraph)
    new = _translated_text(text, data)
    tag_runs: dict[str, str] = {}
    base = ""
    for run in runs:
        body = _run_text(run.group(0))
        if TAG.fullmatch(body):
            tag_runs.setdefault(body, run.group(0))
        elif body and not base:
            base = run.group(0)
    base = base or runs[0].group(0)
    found = re.search(r"<w:rPr>.*?</w:rPr>", base, re.S)
    properties = found.group(0) if found else ""
    tail = _underlined_tail(runs)
    if tail is not None and tail[2] in new:
        before, tail_properties, tag = tail
        start = max(0, new.index(tag) - before)
        pieces = _pieces(new[:start], tag_runs, properties)
        pieces.append(_text_run(tail_properties, new[start:]))
    else:
        pieces = _pieces(new, tag_runs, properties)
    between = "".join(
        paragraph[runs[index].end() : runs[index + 1].start()] for index in range(len(runs) - 1)
    )
    rebuilt = "".join(_sc_fonts(piece) for piece in pieces)
    return paragraph[: runs[0].start()] + rebuilt + between + paragraph[runs[-1].end() :]


def _sc_styles(styles: str) -> str:
    """styles.xml の字体を中国語の字体にし（既定の字体も）、東アジアの言語を中国語にする。"""
    result = RFONTS.sub(SC_FONTS, styles)
    result = re.sub(r'(<w:lang\b[^>]*w:eastAsia=")[^"]*(")', r"\1zh-CN\2", result)
    if "<w:rPrDefault>" in result and SC_FONTS not in result.split("</w:rPrDefault>")[0]:
        result = result.replace("<w:rPrDefault><w:rPr>", "<w:rPrDefault><w:rPr>" + SC_FONTS, 1)
    return result


def translate_docx(source: Path, target: Path, data: dict[str, Any]) -> None:
    with zipfile.ZipFile(source) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    result = PARAGRAPH.sub(lambda m: _translate_paragraph(m.group(0), data), xml)
    # 段落の印などに残る字体の指定も中国語の字体に、東アジアの言語を中国語にする
    result = RFONTS.sub(SC_FONTS, result)
    result = re.sub(r'(<w:lang\b[^>]*w:eastAsia=")[^"]*(")', r"\1zh-CN\2", result)
    left = [unescape(body) for _, body, _ in TEXT.findall(result) if KANA.search(unescape(body))]
    if left:
        raise TranslationError(f"Japanese text is left: {left[:3]!r}")
    with zipfile.ZipFile(source) as archive:
        styles = archive.read("word/styles.xml").decode("utf-8")
    copy_zip(
        source,
        target,
        {
            "word/document.xml": result.encode("utf-8"),
            "word/styles.xml": _sc_styles(styles).encode("utf-8"),
        },
    )


def blank_docx(template: Path, target: Path) -> None:
    """差込タグを空のときの文にした docx（訳文の版の original。テンプレートを空で差し込んだもの）。"""
    with zipfile.ZipFile(template) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")

    def replace(match: re.Match[str]) -> str:
        body = unescape(match.group(2))
        if not TAG.search(body):
            return match.group(0)
        return match.group(1) + escape(TAG.sub(lambda tag: tag.group(2), body)) + match.group(3)

    if "{{" in TAG.sub("", "".join(unescape(m[1]) for m in TEXT.findall(xml))):
        raise TranslationError("a tag is split across text elements")
    copy_zip(template, target, {"word/document.xml": TEXT.sub(replace, xml).encode("utf-8")})


# --- Excel（第1-17号）--------------------------------------------------------------------


def _si_text(body: str) -> str:
    plain = re.sub(r"<rPh\b.*?</rPh>", "", body, flags=re.S)
    return "".join(unescape(t) for t in re.findall(r"<t(?:\s[^>]*)?>([^<]*)</t>", plain))


def translate_xlsx(source: Path, target: Path, data: dict[str, Any]) -> None:
    with zipfile.ZipFile(source) as archive:
        xml = archive.read("xl/sharedStrings.xml").decode("utf-8")
        sheets = {
            name: archive.read(name).decode("utf-8")
            for name in archive.namelist()
            if name.startswith("xl/worksheets/sheet")
        }
    for name, sheet in sheets.items():  # 共有の文字列のほかに日本語の文が無いこと
        inline = re.findall(r'<c [^>]*t="(?:inlineStr|str)"[^>]*>.*?</c>', sheet, re.S)
        if any(JAPANESE.search(unescape(cell)) for cell in inline):
            raise TranslationError(f"{name}: an inline string has Japanese text")

    def put(match: re.Match[str]) -> str:
        text = _si_text(match.group(1))
        if not JAPANESE.search(text):
            return match.group(0)
        if text not in data["strings"]:
            raise TranslationError(f"no translation for the string: {text!r}")
        return '<si><t xml:space="preserve">' + escape(data["strings"][text]) + "</t></si>"

    result = SI.sub(put, xml)
    with zipfile.ZipFile(source) as archive:
        styles = archive.read("xl/styles.xml").decode("utf-8")
    copy_zip(
        source,
        target,
        {
            "xl/sharedStrings.xml": _sc_rich_fonts(result).encode("utf-8"),
            "xl/styles.xml": _sc_xlsx_styles(styles).encode("utf-8"),
        },
    )


def _sc_font_name(name: str) -> str:
    """明朝は Noto Serif CJK SC、ほか（ゴシックなど）は Noto Sans CJK SC。"""
    return FONT if ("明朝" in name or "Mincho" in name) else SANS


def _sc_xlsx_styles(styles: str) -> str:
    """Excel の字体を中国語の字体にする（文字セットを中国語にし、テーマの字体の指定を外す）。"""
    result = re.sub(
        r'<name val="([^"]*)"/>', lambda m: f'<name val="{_sc_font_name(m.group(1))}"/>', styles
    )
    result = re.sub(r'<charset val="\d+"/>', '<charset val="134"/>', result)
    return re.sub(r'<scheme val="[^"]*"/>', "", result)


def _sc_rich_fonts(shared: str) -> str:
    result = re.sub(
        r'<rFont val="([^"]*)"/>', lambda m: f'<rFont val="{_sc_font_name(m.group(1))}"/>', shared
    )
    return re.sub(r'<charset val="\d+"/>', '<charset val="134"/>', result)


# --- マッピング・規則・manifest -----------------------------------------------------------


def translate_mapping(definition: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    strings = data.get("mapping", {})

    def text(value: str) -> str:
        if not JAPANESE.search(value):
            return value
        if value not in strings:
            raise TranslationError(f"no translation for the mapping text: {value!r}")
        return str(strings[value])

    result = json.loads(json.dumps(definition))
    result["note"] = (
        f"{definition.get('note', '')} 中国語（簡体字）の訳文の版（自作。T43、ADR-0035）: 日本語の版の"
        f"マッピングの写しで、印刷済みの文に差し込む文だけを訳す（{TOOL} が作る）。"
    ).strip()
    for item in result.get("fields", []):
        if "template" in item:
            item["template"] = text(item["template"])
        for spec in (item.get("keys") or {}).values():
            for name in ("pattern", "empty"):
                if name in spec:
                    spec[name] = text(spec[name])
    return result


def translate_rules(rules: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(rules))
    result["rules"] = [
        *result.get("rules", []),
        {
            "id": "untranslated",
            "type": "check",
            "name": "untranslated_values",
            "severity": "warn",
            "note": "訳の表に無い日本語の値は日本語のまま出す（氏名・住所などは訳さない）。T43-D5",
        },
    ]
    return result


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(form_id: str, japanese: dict[str, Any], folder: Path, files: dict[str, str]) -> Any:
    source = japanese["source"]
    data = {
        **{key: value for key, value in japanese.items() if key not in ("source",)},
        "language": LANGUAGE,
        "name": f"{japanese['name']}の訳文（中国語・簡体字。自作）",
        "requires_signed_copy": False,  # 署名は日本語の版に（T43-D4）
        "translation": {
            "of_language": "ja",
            "table": f"tools/forms/translations/{form_id}.{LANGUAGE}.json",
            "values": VALUES,
        },
        **files,
        "source": {
            "self_made": True,
            "tool": TOOL,
            "url": "",
            "page": "",
            "retrieved_on": source["retrieved_on"],
            "revision_date": source.get("revision_date"),
            "revision_date_unknown": bool(source.get("revision_date_unknown")),
            "original_sha256": sha256(folder / files["original"]),
            **(
                {"template_sha256": sha256(folder / files["template"])}
                if "template" in files
                else {}
            ),
            "translation_of": {
                "url": source["url"],
                "original_sha256": source["original_sha256"],
            },
            "adr": "ADR-0035（中国語の訳文。自作。訳は人の確認待ち）",
        },
    }
    data.pop("converted", None)
    return data


# --- 対照表 ----------------------------------------------------------------------------


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def comparison(title: str, lead: str, rows: list[tuple[str, str]]) -> str:
    lines = [
        f"# {title}",
        "",
        lead,
        "",
        "> **訳はすべて人の確認待ち**（自作の訳。入管の中国語の様式〔旧い版、001339744.docx〕の訳語を参考にした）。",
        "> 直すときは tools/forms/translations の訳の表（値は backend/services/data/translations）を直し、",
        f"> `python3 {TOOL}` で作り直す（この表も作り直される）。",
        "",
        "| # | 日本語 | 中国語（簡体字） |",
        "|---|---|---|",
    ]
    for number, (japanese, chinese) in enumerate(rows, start=1):
        lines.append(f"| {number} | {cell(japanese)} | {cell(chinese)} |")
    return "\n".join(lines) + "\n"


def main(root: Path) -> None:
    for form_id, valid_from in FORMS.items():
        data = table(form_id)
        japanese_folder = REPO / "templates" / "forms" / form_id / valid_from
        japanese = json.loads((japanese_folder / "manifest.json").read_text(encoding="utf-8"))
        folder = root / "templates" / "forms" / form_id / f"{valid_from}.{LANGUAGE}"
        if japanese["method"] == "docx-tags":
            translate_docx(japanese_folder / japanese["template"], folder / "template.docx", data)
            blank_docx(folder / "template.docx", folder / "original.docx")
            files = {"original": "original.docx", "template": "template.docx"}
        else:
            translate_xlsx(japanese_folder / japanese["original"], folder / "original.xlsx", data)
            files = {"original": "original.xlsx"}
        written = manifest(form_id, japanese, folder, files)
        (folder / "manifest.json").write_text(
            json.dumps(written, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        mappings = REPO / "mappings" / form_id
        out = root / "mappings" / form_id
        out.mkdir(parents=True, exist_ok=True)
        definition = json.loads((mappings / f"{valid_from}.json").read_text(encoding="utf-8"))
        (out / f"{valid_from}.{LANGUAGE}.json").write_text(
            json.dumps(translate_mapping(definition, data), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        rules = json.loads((mappings / f"{valid_from}.rules.json").read_text(encoding="utf-8"))
        (out / f"{valid_from}.{LANGUAGE}.rules.json").write_text(
            json.dumps(translate_rules(rules), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        rows = list(data.get("paragraphs", {}).items()) + list(data.get("defaults", {}).items())
        rows += list(data.get("strings", {}).items()) + list(data.get("mapping", {}).items())
        docs = root / "docs" / "translations"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / f"{form_id}.{LANGUAGE}.md").write_text(
            comparison(
                f"{japanese['name']}の訳文（中国語・簡体字）の対照表",
                f"日本語の版（{valid_from}）の固定の文と、訳文の版（{valid_from}.{LANGUAGE}）の文。"
                "差込タグは {1}・{2}…（Word）、{名前}（Excel のマッピング）。",
                rows,
            ),
            encoding="utf-8",
        )
    values = json.loads((REPO / VALUES).read_text(encoding="utf-8"))
    rows = list(values["values"].items())
    rows += [(item["pattern"], item["replace"]) for item in values["patterns"]]
    for item in values["token_lists"]:
        if isinstance(item["tokens"], dict):
            rows += [(f"{key}（{item['separator']} で区切った語）", v) for key, v in item["tokens"].items()]
    (root / "docs" / "translations" / f"values.{LANGUAGE}.md").write_text(
        comparison(
            "欄に入れる値の訳（中国語・簡体字）の対照表",
            "訳文の版で生成するとき、欄の値が表の文と同じものだけを中国語にする（標準の支援計画の"
            "実施予定の初期値・様式の言葉・分野と業務区分・国名など。形の決まった文は正規表現）。"
            "ほかの日本語の値（氏名・住所・自由記述）は日本語のまま出す。",
            rows,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else REPO)
