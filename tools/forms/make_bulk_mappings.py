"""一括登録用ツールの欄の対応（データのキー → 列）を作る（T48。ADR-0039）。

列の表（mappings/bulk-*/<版>.columns.json）の項目名を正規化した名前（空白・括弧の番号を除く）と、
同じ名前の何番目かで、データのキー（services/bulk_application_data.py が作る値）を当てる。3 つの手続
（認定・更新・変更）で列の位置や項目名の番号が違っても、同じ表で当てられる。
出力は手で直さない。必須（特定技能の CSV 列があり、条件の無いもの）なのに当てていない欄は、
NOT_MAPPED に理由が無ければ止める（T46 の完了条件「当たらないものは理由つきで一覧」）。

使い方（リポジトリの直下で）:
  python tools/forms/make_bulk_mappings.py bulk-coe 2026-06-04 > mappings/bulk-coe/2026-06-04.json
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECTION_V = "申請情報入力(区分V)"
SIMULTANEOUS = "申請情報入力(同時申請)"


FULL_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def norm(name: str) -> str:
    name = name.translate(FULL_DIGITS).replace("（", "(").replace("）", ")")
    name = re.sub(r"\(\d+\)", "", name)
    return re.sub(r"[\s　]", "", name)


def _relatives(prefix: str) -> list[tuple[str, int, str]]:
    rows: list[tuple[str, int, str]] = []
    for n in range(1, 7):
        rows.append((f"在日親族及び同居者{n}在日親族及び同居者続柄", 0, f"rel{n}.relationship"))
        k = n - 1
        rows += [
            ("在日親族及び同居者氏名", k, f"rel{n}.name"),
            ("在日親族及び同居者生年月日", k, f"rel{n}.birth_date"),
            ("在日親族及び同居者国籍・地域", k, f"rel{n}.nationality"),
            ("在日親族及び同居者同居予定の有無|在日親族及び同居者同居の有無", k, f"rel{n}.together"),
            ("在日親族及び同居者勤務先名称・通学先名称", k, f"rel{n}.workplace"),
            ("在日親族及び同居者在留カード番号特別永住者証明書番号", k, f"rel{n}.card"),
        ]
    return rows


COMMON: list[tuple[str, int, str]] = [
    ("国籍・地域", 0, "p.nationality"),
    ("生年月日", 0, "p.birth_date"),
    ("氏名", 0, "p.name"),
    ("性別", 0, "p.sex"),
    ("出生地", 0, "p.birthplace"),
    ("配偶者の有無", 0, "a.spouse"),
    ("職業", 0, "a.occupation"),
    ("本国における居住地", 0, "p.home_address"),
    ("日本における連絡先郵便番号", 0, "p.postal"),
    ("日本における連絡先居住地|日本における連絡先住居地", 0, "p.pref"),
    ("日本における連絡先居住地|日本における連絡先住居地", 1, "p.city"),
    ("日本における連絡先居住地|日本における連絡先住居地", 2, "p.address_rest"),
    ("日本における連絡先電話番号", 0, "p.phone"),
    ("日本における連絡先携帯電話番号", 0, "p.mobile"),
    ("メールアドレス", 0, "p.email"),
    ("旅券番号", 0, "p.passport"),
    ("旅券有効期限", 0, "p.passport_expiry"),
    # 認定
    ("入国目的", 0, "a.entry_purpose"),
    ("入国予定年月", 0, "a.entry_month"),
    ("上陸予定港", 0, "a.port"),
    ("上陸予定港その他", 0, "a.port_other"),
    ("滞在予定期間(年数)", 0, "a.stay_years"),
    ("滞在予定期間(月数)", 0, "a.stay_months"),
    ("同伴者の有無", 0, "a.companion"),
    ("査証申請予定地", 0, "a.visa_place"),
    ("過去の出入国歴有無", 0, "d.entry_history"),
    ("過去の出入国歴回数", 0, "d.entry_count"),
    ("直近の出入国歴(始期)", 0, "d.last_entry_from"),
    ("直近の出入国歴(終期)", 0, "d.last_entry_to"),
    ("過去の在留資格認定証明書交付申請歴有無", 0, "d.coe_history"),
    ("過去の在留資格認定証明書交付申請歴回数", 0, "d.coe_count"),
    ("過去の在留資格認定証明書交付申請歴うち不交付となった回数", 0, "d.coe_refused"),
    ("犯罪を理由とする処分を受けたことの有無", 0, "d.crime"),
    ("犯罪を理由とする処分を受けたことの有無有内容入力欄", 0, "d.crime_detail"),
    ("退去強制又は出国命令による出国の有無", 0, "d.deport"),
    ("退去強制又は出国命令による出国の有無回数", 0, "d.deport_count"),
    ("退去強制又は出国命令による出国の有無直近の送還歴", 0, "d.deport_last"),
    ("在日親族及び同居者有無", 0, "p.relatives"),
    *_relatives(""),
    # 更新・変更
    ("現に有する在留資格", 0, "p.status"),
    ("在留期間", 0, "p.period"),
    ("在留期間の満了日", 0, "p.expiry"),
    ("在留カードの有無", 0, "p.has_card"),
    ("在留カード番号", 0, "p.card"),
    ("ED番号(英字)", 0, "a.ed_alpha"),
    ("ED番号(数字)", 0, "a.ed_number"),
    ("希望する在留資格", 0, "a.desired_status"),
    ("希望する在留期間", 0, "a.desired_period"),
    ("更新の理由|変更の理由", 0, "a.reason"),
    # 受領・通知・意思の確認
    ("在留資格認定証明書の受領方法|在留カードの受領方法", 0, "a.receipt"),
    ("申請対象者の住居地", 0, "a.receipt_residence"),
    ("受領官署", 0, "a.receipt_office"),
    ("通知送信用メールアドレス", 0, "a.notice_email"),
    ("申請に先立ち、申請者本人に申請の意思を確認してください。", 0, "a.intention"),
    ("フリー欄", 0, "a.free"),
]

PLAN = [
    ("特定技能雇用契約の内容等を外国人が十分に理解できる言語による情報提供の実施の有無", "plan.guidance"),
    ("上記について対面又はテレビ電話装置その他の方法により行うこととしていることの有無", "plan.guidance_method"),
    ("出入国時に港又は飛行場への送迎をすることとしていることの有無", "plan.transfer"),
    ("1号特定技能外国人支援計画適切な住居の確保に係る支援をすることとしていることの有無", "plan.housing"),
    ("金融機関における預金口座等の生活に必要な契約に係る支援をすることとしていることの有無", "plan.contracts"),
    ("本邦での生活一般に関する事項等を外国人が十分に理解できる言語により実施することの有無", "plan.orientation"),
    ("関係機関への同行その他の必要な措置を講ずることとしていることの有無", "plan.procedures"),
    ("1号特定技能外国人支援計画日本語を学習する機会を提供することとしていることの有無", "plan.japanese"),
    ("外国人が理解できる言語により相談又は苦情の申出に対して必要な措置を講ずることの有無", "plan.consultation"),
    ("外国人と日本人の交流の促進に係る支援をすることとしていることの有無", "plan.exchange"),
    ("外国人が責めに帰すべき事由によらず契約を解除される場合は、転職支援をすることの有無", "plan.job_change"),
    ("支援責任者等が定期面談を実施し、問題発生時は関係行政機関に通報することの有無", "plan.interview"),
    ("支援計画を作成し、当該外国人にその写しを交付することとしていることの有無", "plan.copy"),
    ("支援の内容が外国人の適正な在留に資するものであって、かつ適切に実施できることの有無", "plan.appropriate"),
]

SECTION: list[tuple[str, int, str]] = [
    ("技能水準評価区分", 0, "q.skill_eval"),
    *[("技能水準合格した試験名", k, f"q.skill_exam{k + 1}") for k in range(3)],
    *[("技能水準受験地", k, f"q.skill_place{k + 1}") for k in range(3)],
    ("技能水準その他の評価方法による証明", 0, "q.skill_other"),
    ("日本語能力評価区分", 0, "q.ja_eval"),
    *[("日本語能力合格した試験名", k, f"q.ja_exam{k + 1}") for k in range(3)],
    *[("日本語能力受験地", k, f"q.ja_place{k + 1}") for k in range(3)],
    ("日本語能力その他の評価方法による証明", 0, "q.ja_other"),
    ("良好に修了した技能実習2号１良好に修了した技能実習2号職種・作業職種", 0, "q.intern1_occupation"),
    ("良好に修了した技能実習2号2良好に修了した技能実習2号職種・作業職種", 0, "q.intern2_occupation"),
    *[("良好に修了した技能実習2号職種・作業作業", k, f"q.intern{k + 1}_work") for k in range(2)],
    *[
        ("良好に修了した技能実習2号職種・作業良好に修了したことの証明", k, f"q.intern{k + 1}_proof")
        for k in range(2)
    ],
    ("申請時における特定技能1号での通算在留期間(年数)", 0, "q.ssw_years"),
    ("申請時における特定技能1号での通算在留期間(月数)", 0, "q.ssw_months"),
    ("特定技能雇用契約に係る保証金の徴収その他財産管理又は違約金等の支払契約の有無", 0, "f.deposit"),
    ("徴収・管理機関名", 0, "f.deposit_collector"),
    ("徴収金額・管理財産", 0, "f.deposit_amount"),
    ("取次ぎ又は活動準備に関する外国の機関への費用支払について、合意していることの有無", 0, "f.foreign_fee"),
    ("外国の機関名", 0, "f.foreign_fee_org"),
    ("支払額", 0, "f.foreign_fee_amount"),
    ("職歴の有無", 0, "w.has"),
    *[
        (f"職歴{label}職歴入社年月", 0, f"w{n}.joined")
        for n, label in enumerate(["１", "2", "3", "4", "5", "6", "7", "8", "9", "１0"], start=1)
    ],
    *[("職歴退社年月", k, f"w{k + 1}.left") for k in range(10)],
    *[("職歴勤務先名称", k, f"w{k + 1}.employer") for k in range(10)],
    ("代理人氏名", 0, "ag.name"),
    ("代理人本人との関係", 0, "ag.relationship"),
    ("代理人郵便番号", 0, "ag.postal"),
    ("代理人住所", 0, "ag.pref"),
    ("代理人住所", 1, "ag.city"),
    ("代理人住所", 2, "ag.address_rest"),
    ("代理人電話番号", 0, "ag.phone"),
    ("代理人携帯電話番号", 0, "ag.mobile"),
    ("取次者(オンラインシステム利用者)氏名", 0, "it.name"),
    ("取次者郵便番号", 0, "it.postal"),
    ("取次者住所", 0, "it.pref"),
    ("取次者住所", 1, "it.city"),
    ("取次者住所", 2, "it.address_rest"),
    ("取次者所属機関等", 0, "it.organization"),
    ("取次者電話番号", 0, "it.phone"),
    ("特定技能雇用契約雇用契約期間(始期)", 0, "c.starts_on"),
    ("特定技能雇用契約雇用契約期間(終期)", 0, "c.ends_on"),
    ("特定産業分野１特定技能雇用契約従事すべき業務の内容特定産業分野", 0, "c.field1"),
    ("特定産業分野2特定技能雇用契約従事すべき業務の内容特定産業分野", 0, "c.field2"),
    ("特定産業分野3特定技能雇用契約従事すべき業務の内容特定産業分野", 0, "c.field3"),
    *[("特定技能雇用契約従事すべき業務の内容業務区分", k, f"c.category{k + 1}") for k in range(3)],
    ("特定技能雇用契約従事すべき業務の内容職種主たる職種", 0, "c.main_occupation"),
    *[
        (f"他職種{label}特定技能雇用契約従事すべき業務の内容職種他職種", 0, f"c.other_occupation{n}")
        for n, label in enumerate(["１", "2", "3"], start=1)
    ],
    ("特定技能雇用契約所定労働時間(週平均)", 0, "c.weekly_hours"),
    ("特定技能雇用契約所定労働時間(月平均)", 0, "c.monthly_hours"),
    ("特定技能雇用契約所定労働時間が通常の労働者の所定労働時間と同等であることの有無", 0, "c.hours_equal"),
    ("特定技能雇用契約月額報酬", 0, "c.monthly_pay"),
    ("特定技能雇用契約基本給の時間換算額", 0, "c.hourly_pay"),
    ("特定技能雇用契約同等の業務に従事する日本人の月額報酬", 0, "c.japanese_pay"),
    ("特定技能雇用契約報酬の額が日本人が従事する場合の報酬の額と同等以上であることの有無", 0, "c.pay_equal"),
    ("特定技能雇用契約報酬の支払方法", 0, "c.payment_method"),
    ("特定技能雇用契約外国人であることを理由に日本人と異なった待遇としている事項の有無", 0, "c.discrimination"),
    (
        "特定技能雇用契約外国人であることを理由に日本人と異なった待遇としている事項の有無有内容入力欄",
        0,
        "c.discrimination_detail",
    ),
    ("一時帰国を希望した場合に必要な有給休暇を取得させるものとしていることの有無", 0, "c.home_leave"),
    ("外国人が帰国旅費を負担できないとき、旅費を負担し、その他必要な措置をすることの有無", 0, "c.return_travel"),
    ("外国人の健康の状況その他の生活の状況を把握するために必要な措置を講ずることの有無", 0, "c.health"),
    ("特定技能所属機関氏名又は名称", 0, "h.name"),
    ("特定技能所属機関法人番号の有無", 0, "h.has_corporate_number"),
    ("特定技能所属機関法人番号", 0, "h.corporate_number"),
    ("特定技能所属機関雇用保険適用事業所番号", 0, "h.employment_insurance_number"),
    ("特定技能所属機関業種主たる業種", 0, "h.industry1"),
    ("特定技能所属機関業種主たる業種その他", 0, "h.industry1_other"),
    ("他業種１特定技能所属機関業種他業種", 0, "h.industry2"),
    ("他業種2特定技能所属機関業種他業種", 0, "h.industry3"),
    ("特定技能所属機関業種他業種その他", 0, "h.industry2_other"),
    ("特定技能所属機関業種他業種その他", 1, "h.industry3_other"),
    ("特定技能所属機関住所(所在地)郵便番号", 0, "h.postal"),
    ("特定技能所属機関住所(所在地)", 0, "h.pref"),
    ("特定技能所属機関住所(所在地)", 1, "h.city"),
    ("特定技能所属機関住所(所在地)", 2, "h.address_rest"),
    ("特定技能所属機関電話番号", 0, "h.phone"),
    ("特定技能所属機関資本金", 0, "h.capital"),
    ("特定技能所属機関年間売上金額", 0, "h.sales"),
    ("特定技能所属機関常勤職員数", 0, "h.employees"),
    ("特定技能所属機関代表者の氏名", 0, "h.representative"),
    ("特定技能所属機関勤務させる事業所名", 0, "s.name"),
    ("特定技能所属機関勤務させる事業所所在地郵便番号", 0, "s.postal"),
    ("特定技能所属機関勤務させる事業所所在地", 0, "s.pref"),
    ("特定技能所属機関勤務させる事業所所在地", 1, "s.city"),
    ("特定技能所属機関勤務させる事業所所在地", 2, "s.address_rest"),
    ("勤務させる事業所が健康保険及び厚生年金保険の適用事業所であることの有無", 0, "s.health_pension"),
    ("勤務させる事業所が労災保険及び雇用保険の適用事業所であることの有無", 0, "s.labour_insurance"),
    ("特定技能所属機関労働保険番号", 0, "s.labour_insurance_number"),
    (
        "特定技能1号での入国を希望し登録支援機関に1号特定技能外国人支援計画の全部の実施を委託することの有無"
        "|特定技能1号での在留を希望し登録支援機関に1号特定技能外国人支援計画の全部の実施を委託することの有無",
        0,
        "h.full_support",
    ),
    *[(name, 0, key) for name, key in PLAN],
    ("登録支援機関氏名又は名称", 0, "o.name"),
    ("登録支援機関法人番号の有無", 0, "o.has_corporate_number"),
    ("登録支援機関法人番号", 0, "o.corporate_number"),
    ("登録支援機関雇用保険適用事業所番号", 0, "o.employment_insurance_number"),
    ("登録支援機関郵便番号", 0, "o.postal"),
    ("登録支援機関所在地", 0, "o.pref"),
    ("登録支援機関所在地", 1, "o.city"),
    ("登録支援機関所在地", 2, "o.address_rest"),
    ("登録支援機関電話番号", 0, "o.phone"),
    ("登録支援機関代表者の氏名", 0, "o.representative"),
    ("登録支援機関登録番号", 0, "o.registration_number"),
    ("登録支援機関登録年月日", 0, "o.registered_on"),
    ("登録支援機関支援を行う事業所の名称", 0, "of.name"),
    ("登録支援機関郵便番号", 1, "of.postal"),
    ("登録支援機関所在地", 3, "of.pref"),
    ("登録支援機関所在地", 4, "of.city"),
    ("登録支援機関所在地", 5, "of.address_rest"),
    ("登録支援機関支援責任者名", 0, "of.manager"),
    ("登録支援機関支援担当者名", 0, "of.staff"),
    ("登録支援機関対応可能言語", 0, "of.languages"),
    ("登録支援機関支援委託手数料(月額/人)", 0, "o.monthly_fee"),
]

SIMULTANEOUS_SPEC: list[tuple[str, int, str]] = [
    ("同時申請種別選択", 0, "sim.reentry"),
    ("同時申請種別選択", 1, "sim.extra_activity"),
    ("同時申請種別選択", 2, "sim.work_certificate"),
]

# 申告の (11)〜(32)（services/data/host_declarations.json の項目。項目名の終わりで当てる）
DECLARATION_DATA = ROOT / "backend" / "services" / "data" / "host_declarations.json"

# 必須（条件なし）なのに当てない欄と理由
NOT_MAPPED: dict[str, str] = {}

# リスト連動の子（キー → 親のキー）
LINKED = {
    "p.city": "p.pref",
    "ag.city": "ag.pref",
    "it.city": "it.pref",
    "h.city": "h.pref",
    "s.city": "s.pref",
    "o.city": "o.pref",
    "of.city": "of.pref",
    "q.intern1_work": "q.intern1_occupation",
    "q.intern2_work": "q.intern2_occupation",
    "c.category1": "c.field1",
    "c.category2": "c.field2",
    "c.category3": "c.field3",
}


def _declaration_specs() -> list[tuple[str, str]]:
    """(項目名の終わり, キー)。内容の欄は「有内容入力欄」を付けた名前。"""
    data = json.loads(DECLARATION_DATA.read_text(encoding="utf-8"))
    specs = []
    for item in data["items"]:
        if item["key"] in ("27", "28"):
            continue  # 労働者派遣（直接雇用だけなので当てない）
        tail = norm(re.sub(r"^\(\d+\)\s*", "", item["label"]))
        specs.append((tail, f"decl.{item['key']}"))
        if item["detail"]:
            specs.append((tail + "有内容入力欄", f"decl.{item['key']}_detail"))
    return specs


def _cells(table: dict, sheet: str) -> list[dict]:
    seen = set()
    cells = []
    for column in table["columns"]:
        if column["sheet"] != sheet or column["column"] in seen:
            continue
        seen.add(column["column"])
        cells.append(column)
    return cells


def _find(cells: list[dict], names: str, occurrence: int) -> dict | None:
    wanted = [norm(name) for name in names.split("|")]
    matches = [c for c in cells if norm(c["name"]) in wanted]
    return matches[occurrence] if occurrence < len(matches) else None


def make(form_id: str, valid_from: str) -> dict:
    folder = ROOT / "mappings" / form_id
    table = json.loads((folder / f"{valid_from}.columns.json").read_text(encoding="utf-8"))
    fields = []
    used: set[tuple[str, str]] = set()
    by_key: dict[str, dict] = {}

    def add(column: dict, key: str) -> None:
        cell = (column["sheet"], column["column"])
        if cell in used:
            raise SystemExit(f"{form_id}: {cell} mapped twice ({key})")
        used.add(cell)
        item = {
            "key": key,
            "target": f"{column['sheet']}!{column['column']}",
            "format": "code" if column["code"] else "text",
            "label": column["name"],
        }
        if column["code"]:
            item["code"] = column["code"]
        if key in LINKED:
            item["parent_key"] = LINKED[key]
        fields.append(item)
        by_key[key] = item

    plans = [
        (_cells(table, table["common_sheet"]), COMMON),
        (_cells(table, SECTION_V), SECTION),
        (_cells(table, SIMULTANEOUS) if form_id != "bulk-coe" else [], SIMULTANEOUS_SPEC),
    ]
    for cells, spec in plans:
        for names, occurrence, key in spec:
            column = _find(cells, names, occurrence)
            if column is not None:
                add(column, key)
    section = _cells(table, SECTION_V)
    for tail, key in _declaration_specs():
        matches = [c for c in section if norm(c["name"]).endswith(tail)]
        if len(matches) != 1:
            raise SystemExit(f"{form_id}: declaration {key} matched {len(matches)} columns")
        add(matches[0], key)
    # 必須（条件なし・特定技能の CSV 列あり）なのに当てていない欄
    missing = []
    for sheet in (table["common_sheet"], SECTION_V):
        for column in _cells(table, sheet):
            cell = (column["sheet"], column["column"])
            if cell in used or not column["required"] or not column["csv_column"]:
                continue
            if column["check_parent_column"]:
                continue
            reason = NOT_MAPPED.get(norm(column["name"]))
            if reason is None:
                missing.append(f"{column['column']} {column['name']}")
    if missing:
        raise SystemExit(f"{form_id}: required columns without data:\n" + "\n".join(missing))
    not_mapped = [
        {"target": f"{c['sheet']}!{c['column']}", "label": c["name"], "reason": NOT_MAPPED[norm(c["name"])]}
        for sheet in (table["common_sheet"], SECTION_V)
        for c in _cells(table, sheet)
        if (c["sheet"], c["column"]) not in used and norm(c["name"]) in NOT_MAPPED
    ]
    return {
        "form_id": form_id,
        "valid_from": valid_from,
        "note": "一括申請の欄の対応（T48、ADR-0039）。tools/forms/make_bulk_mappings.py が作る。"
        "値は services/bulk_application_data.py が作る。format=code の欄は、コードをツールの表示名に"
        "して書く（parent_key があれば親の値の連動の表）。",
        "fields": fields,
        "not_mapped": not_mapped,
    }


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    print(json.dumps(make(sys.argv[1], sys.argv[2]), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
