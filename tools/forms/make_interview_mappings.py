"""定期面談報告書（第5-5号 本人用・第5-6号 監督者用）のマッピング定義の JSON を作る（開発用。T35、ADR-0024）。

欄は original.xlsx の文字で探す（第5-6号は ４ の欄が第5-5号より 2 行下にある）。印刷済みの文を持つ欄
（方式・役職・問題の有無・⑥・４③ の対応結果）は、元の文の □ を値の差込にした template の書式にし、
値が無いときの文が元の文と同じことを確かめる（違えば止まる）。出力は手で直さない。
標準ライブラリだけで動く。

使い方（リポジトリの直下で）:
    python3 tools/forms/make_interview_mappings.py 5-5 > mappings/5-5/2026-10-18.json
    python3 tools/forms/make_interview_mappings.py 5-6 > mappings/5-6/2026-10-18.json
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import unescape

ROOT = Path(__file__).resolve().parents[2]
VALID_FROM = "2026-10-18"
SHEETS = {"5-5": "参考様式第５－５号", "5-6": "参考様式第５－６号"}
QUESTIONS = ROOT / "backend" / "services" / "data" / "interview_questions.json"
SP = "　"


def cell_texts(path: Path) -> dict[str, str]:
    """シートのセルの文字（共有文字列。ふりがな〔rPh〕は除く）。"""
    with zipfile.ZipFile(path) as archive:
        shared = archive.read("xl/sharedStrings.xml").decode("utf-8")
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    strings = []
    for item in re.findall(r"<si>(.*?)</si>", shared, re.S):
        item = re.sub(r"<rPh\b.*?</rPh>", "", item, flags=re.S)
        strings.append(unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", item, re.S))))
    texts = {}
    for match in re.finditer(r'<c r="([A-Z]+\d+)"((?:[^>/]|/(?!>))*)>(.*?)</c>', sheet, re.S):
        value = re.search(r"<v>(\d+)</v>", match.group(3))
        if value and 't="s"' in match.group(2):
            texts[match.group(1)] = strings[int(value.group(1))]
    return texts


def find(texts: dict[str, str], prefix: str, column: str | None = None) -> str:
    refs = [
        ref
        for ref, text in texts.items()
        if text.startswith(prefix) and (column is None or re.match(rf"{column}\d+$", ref))
    ]
    if len(refs) != 1:
        raise SystemExit(f"cell starting with {prefix!r}: {refs}")
    return refs[0]


def below(ref: str, rows: int = 1) -> str:
    letters, number = re.match(r"([A-Z]+)(\d+)", ref).groups()  # type: ignore[union-attr]
    return f"{letters}{int(number) + rows}"


def right_of(ref: str, letters: str) -> str:
    return letters + re.match(r"[A-Z]+(\d+)", ref).group(1)  # type: ignore[union-attr]


class Builder:
    def __init__(self, form_id: str) -> None:
        self.form_id = form_id
        self.sheet = SHEETS[form_id]
        self.texts = cell_texts(ROOT / "templates" / "forms" / form_id / VALID_FROM / "original.xlsx")
        self.fields: list[dict] = []

    def target(self, ref: str) -> str:
        return f"{self.sheet}!{ref}"

    def value(self, ref: str, key: str, label: str, fmt: str = "text", **extra: object) -> None:
        self.fields.append(
            {"key": f"i.{key}", "target": self.target(ref), "format": fmt, "label": label, **extra}
        )

    def template(
        self, ref: str, label: str, replacements: list[tuple[str, str]], keys: dict, **extra: object
    ) -> None:
        """元の文の部分（old）を差込（new。{名前}）にした template。値が無いときの文は元の文。"""
        original = self.texts[ref]
        pattern = original.replace("{", "{{").replace("}", "}}")
        for old, new in replacements:
            if old not in pattern:
                raise SystemExit(f"{ref}: {old!r} not in {original!r}")
            pattern = pattern.replace(old, new, 1)
        empty = {name: spec.get("empty", "") for name, spec in keys.items()}
        if pattern.format(**empty) != original:
            raise SystemExit(f"{ref}: the empty template differs from the original")
        self.fields.append(
            {
                "target": self.target(ref),
                "format": "template",
                "template": pattern,
                "keys": {
                    name: {"key": f"i.{spec['key']}", **{k: v for k, v in spec.items() if k != "key"}}
                    for name, spec in keys.items()
                },
                "label": label,
                **extra,
            }
        )


def check(key: str) -> dict:
    return {"key": key, "format": "check", "empty": "□"}


def build(form_id: str) -> dict:
    b = Builder(form_id)
    t = b.texts
    supervisor = form_id == "5-6"
    # １ 面談対象者
    first = find(t, "①監督者の氏名" if supervisor else "①特定技能外国人の氏名", "A")
    second = below(first)
    if supervisor:
        b.value(right_of(first, "L"), "supervisor_name", "１① 監督者の氏名及び役職", fit=True)
        b.value(right_of(second, "L"), "supervisor_department", "１② 監督者の所属部署", fit=True)
    else:
        b.value(right_of(first, "L"), "worker_name", "１① 特定技能外国人の氏名", fit=True)
        b.value(right_of(second, "L"), "host_name", "１② 特定技能所属機関の氏名又は名称", fit=True)
    day = find(t, "③面", "A")
    b.value(right_of(day, "L"), "interviewed_on", "１③ 面談日", "date")
    b.template(
        find(t, "□　対面"),
        "１④ 方式",
        [("□", "{in_person}"), ("□", "{online}")],
        {"in_person": check("method_in_person"), "online": check("method_online")},
    )
    # ２ 面談対応者
    staff = find(t, "①対応者の氏名", "A")
    b.value(right_of(staff, "L"), "staff_name", "２① 対応者の氏名", fit=True)
    b.template(
        find(t, "□　支援責任者"),
        "２② 対応者の役職",
        [("□", "{manager}"), ("□", "{staff}")],
        {"manager": check("staff_manager"), "staff": check("staff_support")},
    )
    b.value(right_of(find(t, "役職名"), "AB"), "staff_position", "２② 役職名", fit=True)
    # ３ 面談結果
    questions = [
        q
        for section in json.loads(QUESTIONS.read_text(encoding="utf-8"))["versions"][-1]["sections"]
        for q in section["questions"]
    ]
    first_text = "①" + questions[0]["supervisor" if supervisor else "self"]
    first_row = int(re.match(r"F(\d+)", find(t, first_text, "F")).group(1))  # type: ignore[union-attr]
    for index, question in enumerate(questions):
        row = first_row + index
        text = question["supervisor" if supervisor else "self"]
        assert t[f"W{row}"] == "□有  □無", row
        name = question["code"].replace(".", "_")
        label = f"３ {question['code']} {text}"
        b.template(
            f"W{row}",
            f"{label}（問題の有無）",
            [("□", "{yes}"), ("□", "{no}")],
            {"yes": check(f"q{name}_yes"), "no": check(f"q{name}_no")},
        )
        b.value(f"Z{row}", f"q{name}_detail", f"{label}（問題の内容）", wrap=True)
        if question.get("other"):
            other = right_of(f"W{row}", "I")
            if not t[other].startswith("（"):
                raise SystemExit(f"{other} is not the other matter")
            b.template(
                other,
                "３ 5.2 その他（事項）",
                [(t[other][1:-1], "{subject}")],
                {"subject": {"key": "other_subject", "empty": t[other][1:-1]}},
                fit=True,
            )
    b.template(
        find(t, "□　有り"),
        "３⑥ 基準不適合等の有無",
        [("□", "{yes}"), ("□", "{no}")],
        {"yes": check("nc_yes"), "no": check("nc_no")},
    )
    notable = right_of(find(t, "⑦その他特筆", "A"), "F")
    b.value(notable, "notable", "３⑦ その他特筆すべき事項", wrap=True)
    # ４ 基準不適合等への対応
    occurred = right_of(find(t, "①基準不適合等の発生年月日", "A"), "L")
    b.value(occurred, "nc_occurred_on", "４① 基準不適合等の発生年月日", "date")
    b.value(right_of(find(t, "②基準不適合等の内容", "A"), "L"), "nc_content", "４② 基準不適合等の内容", wrap=True)
    guidance = find(t, "□　労働基準監督署等")
    b.template(
        guidance,
        "４③ア 1号特定技能外国人への対応",
        [("□", "{guided}"), ("案内した機関：", "案内した機関：{guided_to}"), ("□", "{none}")],
        {"guided": check("nc_guided"), "guided_to": {"key": "nc_guided_to"}, "none": check("nc_no_action")},
    )
    reason = "理由：\r\n"
    b.template(
        below(guidance),
        "４③ア 理由",
        [(reason, reason + "{reason}")],
        {"reason": {"key": "nc_no_guidance_reason"}},
    )
    notice = find(t, "（ア）責任者への")
    b.template(
        notice,
        "４③イ（ア） 責任者への通知",
        [
            ("□", "{notified}"),
            ("通知日：", "通知日：{notified_on}"),
            ("通知の相手方", "通知の相手方{notified_to}"),
            ("□", "{not_notified}"),
        ],
        {
            "notified": check("nc_notified"),
            "notified_on": {"key": "nc_notified_on", "format": "date"},
            "notified_to": {"key": "nc_notified_to", "pattern": "：{}"},
            "not_notified": check("nc_not_notified"),
        },
    )
    b.template(
        below(notice),
        "４③イ（ア） 理由",
        [(reason, reason + "{reason}")],
        {"reason": {"key": "nc_not_notified_reason"}},
    )
    b.template(
        find(t, "（イ）基準不適合等の出入国"),
        "４③イ（イ） 出入国在留管理庁への案内",
        [("□", "{guided}"), ("□", "{not_yet}")],
        {"guided": check("nc_immigration_guided"), "not_yet": check("nc_immigration_not_yet")},
    )
    report = find(t, "□　関係行政機関へ通報済み")
    b.template(
        report,
        "４③ウ 関係行政機関への対応",
        [
            ("□", "{reported}"),
            ("通報日：", "通報日：{reported_on}"),
            ("通報先機関：", "通報先機関：{report_to}"),
            ("□", "{unreported}"),
        ],
        {
            "reported": check("nc_reported"),
            "reported_on": {"key": "nc_reported_on", "format": "date"},
            "report_to": {"key": "nc_report_to"},
            "unreported": check("nc_unreported"),
        },
    )
    b.template(
        below(report),
        "４③ウ 理由",
        [(reason, reason + "{reason}")],
        {"reason": {"key": "nc_unreported_reason"}},
    )
    made = find(t, "作成年月日", "Q")
    b.value(right_of(made, "V"), "made_on", "作成年月日", "date")
    b.value(right_of(find(t, "面談実施者の氏名", "Q"), "X"), "staff_name", "面談実施者の氏名", fit=True)
    return {
        "form_id": form_id,
        "valid_from": VALID_FROM,
        "note": (
            "T35 の全欄（ADR-0024）。値は services/form_data_interview.py が作る。回答の無い記録"
            "（T35 より前）は質問の欄を書かない（印刷済みの □ のまま）。"
        ),
        "fit_min_size": 6,
        "fields": b.fields,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in SHEETS:
        raise SystemExit("usage: make_interview_mappings.py <5-5|5-6>")
    print(json.dumps(build(sys.argv[1]), ensure_ascii=False, indent=1))
