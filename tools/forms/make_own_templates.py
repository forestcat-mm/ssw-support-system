"""自機関のテンプレート（キャリアアッププラン・実務経験証明書）を作る（開発用。T36-D3、ADR-0025）。

どちらも公式の参考様式が無い（キャリアアッププランは「任意様式」、実務経験証明書は様式なし）。
同じ定義から、空の様式（original.docx。欄は空白）と、差込タグを置いた template.docx を作り、
マッピング定義の JSON を標準出力に出す。タグは {{ 名前 or "" }}（値が無いと空）。出力は手で直さない
（テストで、出力が登録済みのファイルと同じことを確かめる）。zip の日時は固定し、同じ入力から同じ
バイトを作る。標準ライブラリだけで動く。

使い方（リポジトリの直下で）:
    python3 tools/forms/make_own_templates.py career-plan templates/forms/career-plan/2026-10-18 \\
        > mappings/career-plan/2026-10-18.json
    python3 tools/forms/make_own_templates.py experience-certificate \\
        templates/forms/experience-certificate/2026-10-18 \\
        > mappings/experience-certificate/2026-10-18.json
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

VALID_FROM = "2026-10-18"
STAMP = (2026, 10, 18, 0, 0, 0)
LEVEL_ROWS = 5
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.wordprocessingml.styles+xml"/>'
    "</Types>"
)
ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/></Relationships>'
)
DOCUMENT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/styles" Target="styles.xml"/></Relationships>'
)
STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f"<w:styles {W}><w:docDefaults><w:rPrDefault><w:rPr>"
    '<w:rFonts w:ascii="ＭＳ 明朝" w:eastAsia="ＭＳ 明朝" w:hAnsi="ＭＳ 明朝"/>'
    '<w:sz w:val="21"/><w:lang w:val="en-US" w:eastAsia="ja-JP"/></w:rPr></w:rPrDefault>'
    '<w:pPrDefault><w:pPr><w:spacing w:after="60" w:line="300" w:lineRule="auto"/></w:pPr>'
    "</w:pPrDefault></w:docDefaults></w:styles>"
)


class Builder:
    """本文の XML を作る（tags=True ならタグ、False なら空の様式）。"""

    def __init__(self, tags: bool) -> None:
        self.tags = tags
        self.parts: list[str] = []
        self.fields: list[dict] = []

    def slot(self, name: str, label: str, fmt: str = "text", blank: str = "") -> str:
        """欄（空の様式では blank〔空白など〕、template ではタグ。値が無いと blank に戻る）。"""
        if not any(item["target"] == name for item in self.fields):
            self.fields.append(
                {"key": f"{self.prefix}.{name}", "target": name, "format": fmt, "label": label}
            )
        return "{{ " + name + ' or "' + blank + '" }}' if self.tags else blank

    prefix = ""

    @staticmethod
    def run(text: str, *, bold: bool = False, size: int | None = None) -> str:
        props = ("<w:b/>" if bold else "") + (f'<w:sz w:val="{size}"/>' if size else "")
        rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
        return f'<w:r>{rpr}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'

    def paragraph(
        self, *runs: str, align: str = "", indent: int = 0, keep: bool = False
    ) -> str:
        ppr = ""
        if align or indent or keep:
            ppr = (
                "<w:pPr>"
                + ("<w:keepNext/>" if keep else "")
                + (f'<w:ind w:left="{indent}"/>' if indent else "")
                + (f'<w:jc w:val="{align}"/>' if align else "")
                + "</w:pPr>"
            )
        return f"<w:p>{ppr}{''.join(runs)}</w:p>"

    def add(self, *runs: str, **kwargs: object) -> None:
        self.parts.append(self.paragraph(*runs, **kwargs))  # type: ignore[arg-type]

    def table(self, widths: list[int], rows: list[list[str]], header: bool = True) -> None:
        grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
        border = "".join(
            f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="000000"/>'
            for side in ("top", "left", "bottom", "right", "insideH", "insideV")
        )
        body = []
        for index, cells in enumerate(rows):
            tr = "<w:tr>" + ("<w:trPr><w:tblHeader/></w:trPr>" if header and index == 0 else "")
            for width, content in zip(widths, cells):
                tr += f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>{content}</w:tc>'
            body.append(tr + "</w:tr>")
        self.parts.append(
            f'<w:tbl><w:tblPr><w:tblW w:w="{sum(widths)}" w:type="dxa"/><w:tblBorders>{border}'
            f'</w:tblBorders><w:tblLayout w:type="fixed"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid>'
            + "".join(body)
            + "</w:tbl>"
        )

    def xml(self) -> str:
        section = (
            '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" '
            'w:bottom="1134" w:left="1134" w:header="567" w:footer="567" w:gutter="0"/></w:sectPr>'
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f"<w:document {W}><w:body>{''.join(self.parts)}{section}</w:body></w:document>"
        )


def date_line(b: Builder, name: str, label: str) -> str:
    year = b.slot(f"{name}_y", f"{label}（年）", "zen", "　　　　")
    month = b.slot(f"{name}_m", f"{label}（月）", "zen", "　　")
    day = b.slot(f"{name}_d", f"{label}（日）", "zen", "　　")
    return b.run(f"{year}年{month}月{day}日")


def career_plan(b: Builder) -> None:
    b.prefix = "p"
    r = b.run
    b.add(r("キャリアアッププラン", bold=True, size=32), align="center")
    b.add(date_line(b, "made", "作成日"), align="right")
    b.add(r(b.slot("worker_name", "特定技能外国人の氏名")), r("　殿"))
    b.add(r("特定技能所属機関　"), r(b.slot("host_name", "特定技能所属機関名")), align="right")
    b.add(r("代表者　"), r(b.slot("host_representative", "代表者 役職・氏名")), align="right")
    b.add(
        r("事業所　"),
        r(b.slot("site_name", "事業所")),
        r("（分野："),
        r(b.slot("field", "分野")),
        r("　業務区分："),
        r(b.slot("categories", "業務区分")),
        r("）"),
    )
    b.add(r("１　想定されるキャリアルート", bold=True), keep=True)
    b.add(r(b.slot("career_route", "想定されるキャリアルート")), indent=210)
    b.add(r("２　各レベルの業務内容・習熟の目安となる年数・レベルアップに必要な経験・実績、資格・検定・職務上の地位・処遇", bold=True), keep=True)
    header = ["レベル", "目安の年数", "業務内容", "必要な経験・実績、資格・検定", "職務上の地位・処遇"]
    rows = [[b.paragraph(r(text, bold=True)) for text in header]]
    for row in range(1, LEVEL_ROWS + 1):
        cells = []
        for name, label in (
            ("name", "レベル"),
            ("years", "目安の年数"),
            ("duties", "業務内容"),
            ("requirements", "必要な経験・実績、資格・検定"),
            ("treatment", "職務上の地位・処遇"),
        ):
            cells.append(b.paragraph(r(b.slot(f"l{row}_{name}", f"２ {row} 行目 {label}"))))
        rows.append(cells)
    b.table([1200, 1300, 2700, 2300, 2138], rows)
    b.add(r("３　転職できる範囲（水産加工業の区分）", bold=True), keep=True)
    b.add(r(b.slot("transfer_scope", "転職できる範囲")), indent=210)
    b.add(
        r("（第"),
        r(b.slot("plan_version", "プランの版", "text")),
        r("版　適用開始日 "),
        r(b.slot("applies_from", "プランの適用開始日", "date")),
        r("）"),
        align="right",
    )
    b.add(r("説明・受領", bold=True), keep=True)
    b.add(r("上記の内容について説明を受け、書面を受け取りました。"))
    b.add(r("受領日　　　　年　　　月　　　日　　　氏名（署名）＿＿＿＿＿＿＿＿＿＿＿＿＿＿"), align="right")


def experience_certificate(b: Builder) -> None:
    b.prefix = "e"
    r = b.run
    b.add(r("実務経験証明書", bold=True, size=32), align="center")
    b.add(date_line(b, "issued", "証明日"), align="right")
    b.add(r("下記の者が、当社において下記のとおり業務に従事したことを証明します。"))
    b.add(r("記"), align="center")
    for number, (name, label, suffix) in enumerate(
        (
            ("worker_name", "氏名", ""),
            ("birth_date", "生年月日", ""),
            ("nationality", "国籍・地域", ""),
            ("period", "従事した期間", "（休業等の期間を含む）"),
            ("field", "分野", ""),
            ("categories", "業務区分", ""),
            ("site", "事業所", ""),
            ("duties", "従事した業務の内容", ""),
        ),
        start=1,
    ):
        fmt = "date" if name == "birth_date" else "text"
        runs = [r(f"{number}　{label}　"), r(b.slot(name, label, fmt))]
        if suffix:
            runs.append(r(suffix))
        b.add(*runs)
    b.add(r("以上"), align="right")
    b.add(r("特定技能所属機関　"), r(b.slot("host_name", "特定技能所属機関名")), align="right")
    b.add(r("所在地　"), r(b.slot("host_address", "所在地")), align="right")
    b.add(r("代表者　"), r(b.slot("host_representative", "代表者 役職・氏名")), align="right")


FORMS = {"career-plan": career_plan, "experience-certificate": experience_certificate}
NOTES = {
    "career-plan": "T36 の全欄（ADR-0025）。自機関のテンプレート（任意様式）。値は services/form_data_career.py が作る。",
    "experience-certificate": "T36 の全欄（ADR-0025）。自機関のテンプレート（様式なし）。値は services/form_data_career.py が作る。",
}


def write(path: Path, document_xml: str) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in (
            ("[Content_Types].xml", CONTENT_TYPES),
            ("_rels/.rels", ROOT_RELS),
            ("word/_rels/document.xml.rels", DOCUMENT_RELS),
            ("word/styles.xml", STYLES),
            ("word/document.xml", document_xml),
        ):
            info = zipfile.ZipInfo(name, STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data.encode("utf-8"))


def main(form_id: str, folder: str) -> None:
    make = FORMS[form_id]
    blank, tagged = Builder(tags=False), Builder(tags=True)
    make(blank)
    make(tagged)
    write(Path(folder) / "original.docx", blank.xml())
    write(Path(folder) / "template.docx", tagged.xml())
    mapping = {
        "form_id": form_id,
        "valid_from": VALID_FROM,
        "note": NOTES[form_id],
        "fields": tagged.fields,
        "rules": [],
    }
    print(json.dumps(mapping, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in FORMS:
        raise SystemExit("usage: make_own_templates.py <career-plan|experience-certificate> <folder>")
    main(sys.argv[1], sys.argv[2])
