"""日中併記の旧い版に付ける補足の頁（自作の日中併記）の supplement.docx を作る（開発用。ADR-0027 の追記）。

入管の日中併記の版（001339744.docx）の第1-6号は旧い版で、今の様式（参考様式第１－６号）にある
「Ⅸ４ 待遇の相違（内容・理由）等の説明を求めることができる窓口」が無く、Ⅸ の番号・別紙１ の見出し・
「時間給」の語が違う（`.checks/zh-official-diff.md`）。足りない事項を補足の頁に日本語と中国語で書き、
生成のときに雇用条件書の末尾に綴じる（services/form_docx.append_body）。

- 部品（スタイル・用紙・余白）は取り出した版（extracted.docx）をそのまま使い、本文だけをこの補足にする。
- 日本語の文は今の様式の文。中国語の訳は T43 の訳の表（tools/forms/translations/1-6.zh-hans.json）と
  同じ訳を使い、注の訳は新しく書いた。**訳はすべて人の確認待ち。**
- 欄（部署名・担当者職氏名・連絡先）は差込タグ（{{ 名前 or "元の空白" }}）。日本語と中国語の行の両方に置く。
出力は手で直さない（テストで、出力が登録済みのファイルと同じことを確かめる）。標準ライブラリだけで動く。

使い方（リポジトリの直下で）:
    python3 tools/forms/make_zh_supplement.py 1-6 templates/forms/1-6/2026-10-18.ja-zh
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))

import docx_tags  # noqa: E402

SP = "　"
JA_FONT = "ＭＳ 明朝"
ZH_FONT = "SimSun"
BODY = 18  # 9pt（併記の版の本文と同じ）
TITLE = 24  # 12pt

DEPARTMENT = docx_tags.default_tag("treatment_department", SP * 7)
PERSON = docx_tags.default_tag("treatment_person", SP * 8)
PHONE = docx_tags.default_tag("treatment_phone", SP * 9)

# (言語, 大きさ, 太字, 文)。言語は ja・zh、文の中の {tag} は差込タグの場所
SUPPLEMENT_1_6: list[tuple[str, int, bool, str]] = [
    ("ja", TITLE, True, "雇用条件書の補足（参考様式第１－６号の現行の様式に合わせて補う事項）"),
    ("zh", TITLE, True, "雇佣条件书的补充（按照参考式样第1-6号现行式样补充的事项）"),
    ("ja", BODY, False, ""),
    ("ja", BODY, False,
     "この補足は、上の雇用条件書（出入国在留管理庁の日中併記の様式）に無い事項を、現行の参考様式第１－６号に"
     "合わせて補うものであり、上の雇用条件書と一体のものとする。"),
    ("zh", BODY, False,
     "本补充按照现行的参考式样第1-6号，补充上述雇佣条件书（出入国在留管理厅的中日对照式样）中没有的事项，"
     "与上述雇佣条件书构成一个整体。"),
    ("ja", BODY, False, ""),
    ("ja", BODY, False, "Ⅸ．その他（現行の様式のⅨ４）"),
    ("zh", BODY, False, "Ⅸ. 其他（现行式样的Ⅸ4）"),
    ("ja", BODY, False,
     "＿４．次の窓口に対して通常の労働者との間の待遇の相違（内容・理由）等について説明を求めることができる。"),
    ("zh", BODY, False, "＿4．可以向以下窗口要求说明与普通劳动者之间的待遇差异（内容、理由）等。"),
    ("ja", BODY, False, "＿＿＿部署名{department}担当者職氏名{person}（連絡先{phone}）"),
    ("zh", BODY, False, "＿＿＿部门名称{department}负责人职务、姓名{person}（联系方式{phone}）"),
    ("ja", BODY, False, ""),
    ("ja", BODY, False, "（注）"),
    ("ja", BODY, False,
     "１．上の雇用条件書のⅨ「４．雇用管理の改善等に関する事項に係る相談窓口」と「５．本契約終了後に乙が"
     "帰国するに当たり…」は、現行の様式ではそれぞれⅨ５とⅨ６である。"),
    ("zh", BODY, False,
     "1．上述雇佣条件书Ⅸ中的“4．关于改善雇佣管理等事项的咨询窗口”和“5．乙方在本合同终止后回国之际……”，"
     "在现行式样中分别为Ⅸ5和Ⅸ6。"),
    ("ja", BODY, False,
     "２．上の雇用条件書の「参考様式第１－６号（別紙１）」は、現行の様式の「参考様式第１－６号＿別紙１」で"
     "ある。"),
    ("zh", BODY, False,
     "2．上述雇佣条件书的“参考式样第1-6号（附件1）”，即现行式样的“参考式样第1-6号＿附件1”。"),
    ("ja", BODY, False,
     "３．別紙１の「※日給・時給の場合の１か月当たりの金額」の「時給」は、現行の様式の「時間給」と同じで"
     "ある。"),
    ("zh", BODY, False,
     "3．附件1中“※采用日薪/时薪制时，折算月薪为”的“时薪”，与现行式样中的“时间工资（時間給）”相同。"),
    ("ja", BODY, False, ""),
    ("ja", BODY, False, ""),
    ("ja", BODY, False, "受取人（署名）／领取人（签字）{line}"),
]


LINE = SP * 20  # 署名の欄（下線）


def run(text: str, language: str, size: int, bold: bool, underline: bool = False) -> str:
    font = JA_FONT if language == "ja" else ZH_FONT
    lang = "ja-JP" if language == "ja" else "zh-CN"
    properties = (
        f'<w:rFonts w:ascii="{font}" w:eastAsia="{font}" w:hAnsi="{font}"/>'
        + ("<w:b/>" if bold else "")
        + ('<w:u w:val="single"/>' if underline else "")
        + f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/><w:lang w:eastAsia="{lang}"/>'
    )
    return f'<w:r><w:rPr>{properties}</w:rPr><w:t xml:space="preserve">{text}</w:t></w:r>'


def paragraph(language: str, size: int, bold: bool, text: str) -> str:
    # 差込タグは escape しない（docxtpl が読む）。ほかの文字は XML の文字にする
    parts = re.split(r"(\{department\}|\{person\}|\{phone\})", text.replace("＿", SP))
    tags = {"{department}": DEPARTMENT, "{person}": PERSON, "{phone}": PHONE}
    body = "".join(tags[part] if part in tags else escape(part) for part in parts)
    spacing = '<w:spacing w:before="0" w:after="0" w:line="320" w:lineRule="atLeast"/>'
    runs = ""
    if body.endswith("{line}"):  # 「／」の後ろの中国語を中国語の字体にし、署名の欄に下線を引く
        ja, zh = body[: -len("{line}")].split("／")
        runs = (
            run(ja + "／", "ja", size, bold)
            + run(zh, "zh", size, bold)
            + run(LINE, "ja", size, bold, underline=True)
        )
    elif body:
        runs = run(body, language, size, bold)
    return f"<w:p><w:pPr>{spacing}<w:jc w:val=\"left\"/></w:pPr>{runs}</w:p>"


CONTENT = {"1-6": SUPPLEMENT_1_6}


def main(form_id: str, folder: str) -> None:
    source = Path(folder) / "extracted.docx"
    with zipfile.ZipFile(source) as original:
        xml = original.read("word/document.xml").decode("utf-8")
        head = xml[: xml.index("<w:body>") + len("<w:body>")]
        section = re.findall(r"<w:sectPr\b.*?</w:sectPr>", xml, re.S)[-1]
        body = "".join(paragraph(*item) for item in CONTENT[form_id])
        document = head + body + section + "</w:body></w:document>"
        with zipfile.ZipFile(Path(folder) / "supplement.docx", "w") as out:
            for item in original.infolist():
                if item.filename == "word/document.xml":
                    out.writestr(item, document.encode("utf-8"))
                else:
                    out.writestr(item, original.read(item.filename))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in CONTENT:
        raise SystemExit("usage: make_zh_supplement.py <1-6> <folder>")
    main(sys.argv[1], sys.argv[2])
