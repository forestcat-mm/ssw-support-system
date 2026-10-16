# 特定技能 登録支援機関 業務管理システム（MVP）

仕様の正本は `docs/requirements.md`。章番号で参照する（例: 「第7章 データモデル」）。
迷ったら仕様書を読み、仕様書に無いことは実装前に確認を求める。

## 目的
登録支援機関（自社）の内部業務を一元化する自社専用 Web システム。
価値検証 MVP の範囲は 4 つ: ①台帳＋期限ダッシュボード ②支援実績記録 ③PDF 帳票 ④入管向け一括 Excel 生成。
規模: 担当者数名 / 受入れ機関 5 社以下 / 外国人 50 人以下。対象分野は工業製品製造業分野・飲食料品製造業分野。支援言語は日本語＋中国語（簡体字）。

## 技術スタック（確定）
- Backend: Python 3.12 / Django 5 / PostgreSQL 16
- Frontend: Django テンプレート + 最小限の JS（React は本人ポータル追加時に検討）
- Excel 生成: openpyxl（入管配布テンプレートを開いて値だけ書く）
- Word→PDF: docxtpl + LibreOffice headless（Docker で同居。`convert(docx) -> pdf` の 1 関数に閉じる）
- ファイル保存: django-storages（手元はローカル、本番は S3 互換）
- ジョブ: Django-Q（Redis）
- 開発用メール: Mailpit
- 実行: Docker Compose（手元と本番で同じ compose、差分は `.env` と `docker-compose.prod.yml`）

## 絶対に守る制約
1. 外部 API（入管システム、翻訳、認証 SaaS、通知 SaaS 等）に接続しない。例外は自社 SMTP のみ。
2. 個人情報の列（在留カード番号、パスポート番号、口座情報）は列暗号化する。マイナンバーは保存しない。
3. 入管配布の Excel テンプレートは「既存ブックを開いて指定セルに値を書く」のみ。シート構造・入力規則・保護・マクロを変更しない。
4. 環境依存の値（DB 接続、ストレージ、SMTP、暗号化鍵、許可ドメイン）はすべて環境変数。コードに書かない。`.env` は git に入れない。
5. 様式は `様式ID × 適用開始日 × テンプレート × マッピング定義` で版管理し、マッピング定義はコードではなくデータ（JSON）として持つ。旧版は削除しない。
6. 個人情報の閲覧・作成・更新・削除・帳票生成・エクスポートは監査ログに残す（閲覧も記録）。
7. 履歴が要る項目（在留情報、住所、雇用契約、支援計画）は上書きせず版または履歴行で持つ。
8. 業務ロジック（期限計算、帳票マッピング、バリデーション）は `services/` に置き、ビューやモデルのメソッドに埋め込まない。
9. 自社固有の値（自社名、登録番号、支援言語の選択肢、ロゴ）は設定テーブルまたは設定ファイルに持つ。マルチテナント構造は作らない。
10. 本物の外国人情報を開発環境に入れない。ダミーデータは管理コマンド `seed_dummy` で再現可能にする。

## リポジトリ構成
```
.
├── CLAUDE.md
├── docs/
│   ├── requirements.md        # 仕様書（Claude Docs から Markdown エクスポート）
│   ├── tasks-phase1.md        # 段階1のタスク一覧
│   └── decisions/             # ADR（設計判断の記録。1 判断 1 ファイル）
├── backend/
│   ├── config/                # settings / urls / wsgi
│   ├── apps/
│   │   ├── core/              # organization, offices, staff, users, audit
│   │   ├── workers/           # foreign_workers, residence_records, addresses
│   │   ├── hosts/             # host_organizations, host_sites, support_contracts
│   │   ├── contracts/         # employment_contracts
│   │   ├── support/           # support_plans, support_plan_items, support_records
│   │   ├── filings/           # filings（届出・申請タスク、期限計算）
│   │   └── forms_engine/      # form_templates, form_mappings, generated_documents
│   ├── services/              # ドメインロジック（deadlines, forms, validation, pdf）
│   └── tests/
├── templates/forms/           # 入管配布の Word/Excel テンプレート（版ごとにフォルダ）
├── mappings/                  # 様式ごとのマッピング定義 JSON
├── docker-compose.yml
├── docker-compose.prod.yml
└── .env.example
```

## コーディング規約
- 型ヒント必須。ruff + mypy を CI 相当として `make lint` で実行。
- モデル名は仕様書第 7 章のテーブル名に合わせる（snake_case の複数形→ Django モデルは単数形 PascalCase）。
- 日付は `date`、日時は timezone-aware `datetime`（Asia/Tokyo）。
- 期限ルールは `services/deadlines.py` に関数として定義し、テストで固定日付を使って検証する。
- テストは pytest + pytest-django。新しいモデル・サービスには必ずテストを付ける。
- コミットは 1 タスク 1 コミット。メッセージは `feat: ` / `fix: ` / `chore: ` / `docs: ` の接頭辞。

## 作業の進め方
- 実装前に計画を短く提示し、確認を得てから書く。
- 1 回の依頼は 1 機能。仕様書の章番号を明示して参照する。
- 動作確認手順（コマンドと期待結果）を毎回最後に書く。
- 仕様書と矛盾する判断が必要になったら、`docs/decisions/` に ADR を書いて提案する。

## よく使うコマンド
```
make up        # docker compose up -d
make migrate   # python manage.py migrate
make seed      # python manage.py seed_dummy  （外国人30人・受入れ機関2社）
make test      # pytest
make lint      # ruff check . && mypy .
```
