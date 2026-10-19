"""第1-6号・第1-17号の中国語（簡体字）の訳文の版を作る（自作の訳文。開発用。T43、ADR-0035）。

日本語の版の写しの固定の文だけを、訳の表（tools/forms/translations/<様式>.zh-hans.json）の中国語に置き換える。
欄の位置・差込タグ・セル・結合・罫線・入力規則は日本語の版と同じ。
- 第1-6号（Word）: template.docx の段落ごとに、差込タグを {1}・{2}… にした文で表を引き、段落の最初の文字の
  要素に訳を入れ、ほかの文字の要素は空にする（書式は最初の文字の要素のもの。段落の途中の下線は残らない）。
  字体は日本語の版のまま（簡体字だけの字は LibreOffice が代わりの字体で出す。SimSun → Noto Serif CJK SC
  にすると行の高さが変わり頁が増えたため。T43-D7）。差込タグの空のときの文（or の後）も表で訳す。
- 第1-17号（Excel）: 共有の文字列（xl/sharedStrings.xml）だけを訳す（ふりがなを外す）。シート・スタイル・
  数式は変えない（日本語の版と同じフォント。T43-D7）。
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


def _translate_paragraph(paragraph: str, data: dict[str, Any]) -> str:
    texts = TEXT.findall(paragraph)
    if not texts:
        return paragraph
    text = "".join(unescape(body) for _, body, _ in texts)
    if not JAPANESE.search(text):
        return paragraph
    new = _translated_text(text, data)
    first = [True]

    def put(match: re.Match[str]) -> str:
        if first[0]:
            first[0] = False
            return '<w:t xml:space="preserve">' + escape(new) + match.group(3)
        return match.group(1) + match.group(3)

    return TEXT.sub(put, paragraph)


def translate_docx(source: Path, target: Path, data: dict[str, Any]) -> None:
    with zipfile.ZipFile(source) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    result = PARAGRAPH.sub(lambda m: _translate_paragraph(m.group(0), data), xml)
    left = [unescape(body) for _, body, _ in TEXT.findall(result) if KANA.search(unescape(body))]
    if left:
        raise TranslationError(f"Japanese text is left: {left[:3]!r}")
    copy_zip(source, target, {"word/document.xml": result.encode("utf-8")})


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
    copy_zip(source, target, {"xl/sharedStrings.xml": result.encode("utf-8")})


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
