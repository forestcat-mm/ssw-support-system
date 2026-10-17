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
├── README.md                  # 起動・テスト・ダミー投入・環境変数
├── docs/
│   ├── requirements.md        # 仕様書（Claude Docs から Markdown エクスポート）
│   ├── tasks-phase1.md        # 段階1のタスク一覧と完了確認・本番準備
│   ├── tasks-phase2.md        # 段階2のタスク一覧と完了確認・価値検証（B）
│   ├── tasks-phase3.md        # 段階3のタスク一覧（確定済み）
│   ├── handoff-phase3.md      # 段階3への引継ぎ（到達点・ADR の一覧・持ち越し・注意）
│   ├── operations.md          # 管理者・担当者向けの運用手順
│   ├── dummy-data.md          # seed_dummy の内容（外国人の番号ごとの場面）
│   ├── design/                # 業務画面のデザイン
│   └── decisions/             # ADR（設計判断の記録。1 判断 1 ファイル）
├── backend/
│   ├── config/                # settings / urls / wsgi
│   ├── apps/
│   │   ├── core/              # organization, offices, staff, users, audit, attachments
│   │   ├── workers/           # foreign_workers, residence_records, addresses
│   │   ├── hosts/             # host_organizations, host_sites, support_contracts
│   │   ├── contracts/         # employment_contracts
│   │   ├── support/           # support_plans, support_records, 定期面談・相談・ガイダンス・交付記録
│   │   ├── filings/           # filings（届出・申請タスク、期限計算）
│   │   ├── seed/              # seed_dummy（ダミーデータ）
│   │   └── forms_engine/      # form_templates, form_mappings, generated_documents（段階3の T29 で作る）
│   ├── services/              # ドメインロジック（deadlines, filings, support_*, periodic_report など）
│   ├── tests/                 # pytest（単体・画面）
│   └── e2e/                   # Playwright の通しのテスト（make e2e・make gate2）
├── templates/forms/           # 入管配布の Word/Excel テンプレート（版ごとにフォルダ。段階3で作る）
├── mappings/                  # 様式ごとのマッピング定義 JSON（段階3で作る）
├── scripts/                   # 手元用の補助スクリプト（init_env.py など。標準ライブラリのみ）
├── docker-compose.yml
├── docker-compose.prod.yml    # 本番準備で作る（docs/tasks-phase1.md）
└── .env.example
```

## コーディング規約
- 型ヒント必須。ruff + mypy を CI 相当として `make lint` で実行。
- モデル名は仕様書第 7 章のテーブル名に合わせる（snake_case の複数形→ Django モデルは単数形 PascalCase）。
- 日付は `date`、日時は timezone-aware `datetime`（Asia/Tokyo）。今日は `timezone.localdate()` で取る（`date.today()` は使わない）。
- 期限ルールは `services/deadlines.py` に関数として定義し、テストで固定日付を使って検証する。
- テストは pytest + pytest-django。新しいモデル・サービスには必ずテストを付ける。画面を足したら e2e（PC 幅 1366px と 390px）も付ける。
- 監査対象モデル（`services/audit.py` の `AUDITED_MODELS`）では `bulk_create` / `QuerySet.update()` を使わない（シグナルを通らず監査ログに残らない）。必要な場合は `audit.event()` で囲み、イベントとして手動で記録する。
- UI 文言は英語の msgid と日本語の訳で持つ（`make messages`。未訳を残さない。`tests/test_i18n.py` が検査する）。
- コミットは 1 タスク 1 コミット。メッセージは `feat: ` / `fix: ` / `chore: ` / `docs: ` の接頭辞。

## 作業の進め方（段階1・2で定着したもの）

### タスクの流れ
- 各タスクは「計画 → 確認 → 実装 → 報告」で進める。確認を得るまで実装しない。1 回の依頼は 1 機能。
- 計画には次を入れる。
  - **タスクの照合**: タスク表（`docs/tasks-phase*.md`）の記述と、これまでの実装・ADR とのずれ。
  - **出典**: 制度・様式の根拠の URL・版（改正日）・取得日・SHA-256。頁の番号も入れる。
  - **様式・要領の項目と保存項目の対応**: 公式の様式・運用要領の欄から逆算した、保存する項目の表。
  - **決めてほしい点**: 選択肢ごとにおすすめを先頭に置き、理由を添える。
- 仕様書の章番号を明示して参照する。仕様書と矛盾する判断が要るときや、方針を決めたときは、`docs/decisions/` に ADR を書く。

### 制度の根拠
- 制度（入管法令・運用要領・参考様式）の根拠は推測で書かない。公式の資料を取得して読み、頁つきで引く。
- 確かめられなかった点は、ADR の「未確認の点」に明記する。版は URL・取得日・SHA-256 で特定する。

### コミットと push
- 1 タスク 1 コミット。`feat:` と `docs:`（ADR・タスク表・運用手順）は分ける。直しは `fix:`。push 前の直しは、指示があれば該当のコミットに squash する。
- コミットの末尾に `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` を付ける。
- **push は指示があるまでしない。**

### 報告
報告には次を書く。
- `make test` / `make lint` / `make e2e` の結果（件数）
- コミットのハッシュ
- push の状況
- 実装中に決めたこと・見つけたこと
- 画面での確かめ方（操作と期待結果）

## 業務画面とデータの作法
- **業務画面で完結させる。** 日常の操作で admin を使わない（admin は開発者とマスタ用）。e2e では `/admin/` へのリクエストが 0 件であることを確かめる。
- **記録は上書きしない。** 誤りは取消（理由は必須）＋新しい行。版を持つもの（在留情報・雇用契約・支援計画）は新しい版。相談の経過は追記だけ。添付も取消で残す。
- **監査ログ**: 個人情報の閲覧・作成・更新・取消・ダウンロード・生成を残す（制約6）。閲覧は画面ごとに 1 回 1 件。
- **要配慮添付**（健康診断の書類など。ADR-0011）:
  - ダウンロードはその外国人の担当者と管理者だけで、`services.attachments.can_download()` を通す。
  - ファイルの URL を作らない。
  - ファイル名を監査ログ・通知・admin に出さない。
  - ほかの種別で添付させない。
- **権限**は操作（`Action`）で判定し、モデルの権限は `services/authorization.DJANGO_PERMISSION_ACTIONS` で操作に読み替える。添付できるモデルは、必ずこの表に載せる。
- **無効化した外国人**には記録を足させない（見るだけ）。

## 段階のゲート
- 各段階のゲートは、ダミーデータでの確認（A）とする。seed\_dummy のデータを使い、自動の通し（例: `make gate2`）と人による確かめで判定する。
- 実データでの検証（B）は MVP 完了（段階4）後の価値検証で行う（`docs/tasks-phase2.md` の末尾）。

## よく使うコマンド
```
make env       # .env を作る・足りないキーを補う・鍵を生成する（既存の値は変えない）
make up        # docker compose up -d
make migrate   # python manage.py migrate
make admin     # 管理者ロールのユーザーを作る（superuser は使わない）
make seed      # python manage.py seed_dummy  （外国人30人・受入れ機関3社。作成済みなら何もしない）
make seed-reset  # ダミーデータを消して作り直す（DATE=YYYY-MM-DD で基準日）。ダミーの外国人に手で足した記録も消える
make test      # pytest
make lint      # ruff check . && ruff format --check . && mypy .
make format    # ruff format . && ruff check --fix .
make e2e       # 通しのテスト（Playwright。gate2・design-shots は飛ばす）
make gate2     # 段階2の受け入れ確認（DATE=YYYY-MM-DD で基準日。画面の「今日」も変わる）
make messages  # UI 文言の抽出とコンパイル（.po と .mo の両方をコミット）
make filings   # 日次の届出タスクの起票を手で実行（DATE=YYYY-MM-DD で基準日を指定）
```
