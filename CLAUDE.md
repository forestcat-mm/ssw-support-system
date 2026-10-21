# 特定技能 登録支援機関 業務管理システム（MVP）

> **決まり: 報告・計画・途中の連絡・記録・PR の説明文は、すべて日本語で書く。最後のまとめを送る前に、日本語で書けているかを自分で確かめる。** ターンの最後のまとめ、`.checks/` のファイル、決めた点の記録、コミットのメッセージも同じ（ファイル名・関数名・URL の名前などの識別子はそのまま）。
> これまで 4 回、長い作業の最後のまとめが英語になった（2026-10-18〜21）。報告は、先に `.checks/` の報告（クラウドでは PR の説明文）を日本語で書き、それをもとに最後のまとめを日本語で書く。

仕様の正本は `docs/requirements.md`。章番号で参照する（例: 「第7章 データモデル」）。
迷ったら仕様書を読み、仕様書に無いことは実装前に確認を求める。

## 目的
登録支援機関（自社）の内部業務を一元化する自社専用 Web システム。
価値検証 MVP の範囲は 4 つ: ①台帳＋期限ダッシュボード ②支援実績記録 ③PDF 帳票 ④入管向け一括 Excel 生成。
規模: 担当者数名 / 受入れ機関 5 社以下 / 外国人 50 人以下。対象分野は工業製品製造業分野・飲食料品製造業分野。支援言語は日本語＋中国語（簡体字）。

## 技術スタック（確定）
- Backend: Python 3.12 / Django 5 / PostgreSQL 16
- Frontend: Django テンプレート + 最小限の JS（React は本人ポータル追加時に検討）
- Excel 生成: 入管配布のブックの中のシートの XML の、指定したセルの値だけを書き換える（openpyxl で開いて保存すると図形などが落ちるため。openpyxl は読み戻しの確かめだけに使う。ADR-0018）
- Word 生成: docxtpl（original の写しに差込タグだけを置いた template.docx に差し込む）
- PDF 変換: LibreOffice headless（Docker で同居。`convert(path) -> pdf` の 1 関数に閉じ、docx と xlsx を受ける。フォントの置き換えは `backend/config/fonts/`。ADR-0018）
- ファイル保存: django-storages（手元はローカル、本番は S3 互換）
- ジョブ: Django-Q（Redis）
- 開発用メール: Mailpit
- 実行: Docker Compose（手元と本番で同じ compose、差分は `.env` と `docker-compose.prod.yml`）

## 絶対に守る制約
1. 外部 API（入管システム、翻訳、認証 SaaS、通知 SaaS 等）に接続しない。例外は自社 SMTP のみ。
2. 個人情報の列（在留カード番号、パスポート番号、口座情報）は列暗号化する。マイナンバーは保存しない。
3. 入管配布の Excel テンプレートは「既存ブックを開いて指定セルに値を書く」のみ。シート構造・入力規則・保護・マクロを変更しない。
   - 例外: PDF に変換する直前の一時の写しでだけ、ADR-0032 の例外の一覧のものを変えてよい（第1-17号の manifest の `pdf_merges` の範囲の結合、提出書類一覧表の提出確認欄の〇の図形・横 1 頁に収める設定・Excel と同じ行の改頁・表紙の氏名と名称の縦の配置）。保存するブックは変えない。Word の様式を詰める組み方（manifest の `pdf_layout`。第1-5号の日中併記）も同じ一覧に載せ、保存する文書は変えない。
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
│   ├── handoff-mvp.md         # MVP 完成時の引継ぎ（段階4で作ったもの・人の確認・後で直す・未確認の点・本番準備の前提）
│   ├── operations.md          # 管理者・担当者向けの運用手順
│   ├── dummy-data.md          # seed_dummy の内容（外国人の番号ごとの場面）
│   ├── translations/          # 自作の訳文の対照表（日本語と中国語。道具が作る。T43）
│   ├── design/                # 業務画面のデザイン（design-2026-10/ に仕様・見本・design-shots・細かい決定）
│   ├── sources.md             # 一次資料の一覧（資料名・URL・取得日・SHA-256・使う ADR）
│   ├── cloud-session.md       # クラウドセッションの環境の設定
│   └── decisions/             # ADR（設計判断の記録。1 判断 1 ファイル）。log/ にブロックの細かい決定の記録
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
├── mappings/                  # 様式ごとのマッピング定義 JSON と生成前の検査の規則 *.rules.json（ADR-0019）
├── tools/                     # 開発用の道具（run_checks.sh〔make checks〕、forms/ の template.docx・訳文の版の作成）
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
- **計画を出して止まる**のは、次のときだけ。確認を得るまで実装しない。
  - 制度の判断が要る
  - 既存のデータを壊す変更
  - 大きな機能（T41〜T43 など）
- **文言・見た目・小さな使い勝手の直し**は、計画を出さずに実装し、報告にまとめる。決めてほしい点はおすすめで進め、報告に書く。
- 1 回の依頼は 1 機能。
- 計画には次を入れる。
  - **タスクの照合**: タスク表（`docs/tasks-phase*.md`）の記述と、これまでの実装・ADR とのずれ。
  - **出典**: 制度・様式の根拠の URL・版（改正日）・取得日・SHA-256。頁の番号も入れる。
  - **様式・要領の項目と保存項目の対応**: 公式の様式・運用要領の欄から逆算した、保存する項目の表。
  - **決めてほしい点**: 選択肢ごとにおすすめを先頭に置き、理由を添える。
- 仕様書の章番号を明示して参照する。仕様書と矛盾する判断が要るときや、方針を決めたときは、`docs/decisions/` に ADR を書く。

### ブロックでの進め方（段階3の T31〜T40。詳しくは `docs/tasks-phase3.md` の「T31〜T40 の進め方」）
- T31〜T40 は 3 つのブロックで進める。A: T31・T32・T33、B: T34・T35・T36・T37、C: T38・T39・T40。
- **計画はブロックで 1 回出す**。中身はタスクごとの節（照合・出典・項目の対応・決めてほしい点）。決めてほしい点の番号は、タスクの番号を付けた通し番号にする（例: T31-D1）。ブロックで使う様式の取得（URL・取得日・SHA-256）は計画の段階で済ませる。取得は User-Agent を付け、間をあけて 1 件ずつ行う。
- **実装はタスクの順**。コミットはタスクごとに `feat:` と `docs:` を分ける。
- **検査**: 下の「検査の段」のとおり。
- **報告はブロックの終わりに 1 回**。タスクごとの実装・決めたこと・画面での確かめ方、満たしたゲート A の項目、人が見比べる（A の③）様式の一覧を書く。push は報告を確かめてから指示を受ける。
- **途中で止まる条件**: 次のどれかに当たったら、ブロックの途中でも push せずに報告して止まる。決めた点と違う実装が要る、制度の判断が要る、既存のデータを壊すモデルの変更が要る、直せない検査の失敗がある。

### 検査の段（README の「テスト・検査」）
- **直すたびに**: `make check-quick T=…`。lint と、指定したファイル・アプリのテスト（slow を除く）。
- **タスクのコミットのたびに**: `make test`。単体テストの全部を並列で（slow を含む）。
- **push の前に 1 回だけ**: `make checks`。lint・test・e2e・gate2・gate3。
  - gate2 は既定で 1 つの基準日。期限の計算・届出の起票にさわったときだけ、3 つの基準日にする（`GATE2_DATES="2026-10-17 2026-11-30 2026-12-28"`）。
- **テストが落ちて直したとき**: 落ちたテストだけを `make test-lf`（`--lf`）で流し直す。全部の流し直しは、最後の `make checks` の 1 回にまとめる。
- テスト用の DB は使い回す。マイグレーションを直した・消したときは `make test-db`。

### 検査の待ち方
- 長い検査は `make checks CHECKS_NAME=<段階>-<ブロック>` で流す（各検査の終了コードと `=== DONE` を `.checks/logs/<日時>_<名前>_checks.log` に必ず書く。いちばん新しいログは `.checks/logs/latest.log`）。待つときは `=== DONE` と上限の時間（30 分まで）の両方で待つ。上限の無い待ち方（`until grep … DONE` だけのループ）はしない。
- 前の実行が終わる前に次を始めない（DB と e2e のコンテナを分け合うので、重なると紛らわしい落ち方をする）。`make checks` の間に、ほかのテストを流さない。
- Python の依存を足したら、e2e のイメージを作り直す（`docker compose --profile e2e build e2e`）。作り直さないと、e2e が全部時間切れで遅く落ちる。
- 報告には件数と終了コードを書く。

### .checks/ の置き方（人が開いて読むフォルダ。git の外。目次は `.checks/README.md`）
- クラウドセッションでは、`.checks/` はセッションの中だけの作業場所（人は開けない）。人に見せる計画・報告は PR の説明文に、残す決定は `docs/` に書く（下の「クラウドセッションでの進め方」）。
- 段階・ブロックごとのフォルダ（`phase<N>/block<X>/`、段階3の残りは `phase3/T41-T43/`、直しは `phase<N>/fix<M>/`、段階をまたぐ直しは `fixes/`）。中は `plan.md`・`report.md`・`decisions.md`・`images/`（人が見る画像。何の画像か分かる英数字の名前。例 `1-6_zh_before.png`）・`work/`（作業の途中のファイル）。ブロックをまたぐ決めた点は段階のフォルダに 1 つ置く（例 `phase4/decisions-blockBCD.md`）。
- 取得した一次資料は `sources/<取得日>_<資料名>.<拡張子>`（取得の記録は同じ名前に `.headers`）。調べの本文は `research/<段階>-<タスク>_<題>.md`（作業のファイルは同じ名前に `_data/`）。検査のログは `logs/<日時>_<名前>.log`。古い・重複したものは消さずに `archive/`。
- 新しいブロックを始めたらフォルダを作り、`.checks/README.md` の目次（ブロックの表・sources の一覧・今いちばん新しい報告）を書き足す。

### 人がいない間の進め方（「止まらずに進める」指示のとき）
- 決めてほしい点はおすすめで決めて進め、決めた点の記録（番号・内容・選んだ案・理由・人の確認が要るか）に書き足す。実装中に自分で決めた点も書く。
- 途中で止まる条件に当たったとき: 制度の判断が要る → 安全な側の仮の決定にして「人の確認待ち」と書く。既存のデータを壊すモデルの変更が要る → 実装せず、設計だけを書く。直せない検査の失敗 → そのタスクを飛ばして報告に書く。
- 開発用の DB を作り直さない（`make seed-reset`・`make seed-scenario RESET=1`・`REPLACE=1` は手順だけを書く）。決めた ADR を変えない。既存の様式の見た目の不具合は直さず、「後で直す一覧」（`docs/handoff-mvp.md`）に書く。
- 時間や上限で止まるときは、タスクの切れ目でコミットし、状況を報告して止まる。push はしない。

### 実装で気をつけること（これまでに分かったこと）
- ruff の行の長さ（E501・100）は全角の字を 2 と数える。日本語のコメント・docstring は短く折り返す。
- UI 文言の msgid が別の所の訳とぶつかるとき（例: "Contents" は「中身」と「目次」）は、文脈（`context`）を付ける。`tests/test_i18n.py` は紛らわしい msgid に文脈があるかを検査する。
- 役割の色の変数は `base.css`・`components.css` だけで使い、`--palette-*` は `tokens.css` だけに書く（`tests/ui/test_layout.py`）。字体は同梱（`static/ui/fonts/`）で、外から読まない。
- `make design-shots` は seed_dummy のデータで撮る（テストの中で DEBUG を立てる）。画面を変えたら撮り直して、見本と見比べる。

### 制度の根拠
- 制度（入管法令・運用要領・参考様式）の根拠は推測で書かない。公式の資料を取得して読み、頁つきで引く。
- 確かめられなかった点は、ADR の「未確認の点」に明記する。版は URL・取得日・SHA-256 で特定する。

### コミットと push
- 1 タスク 1 コミット。`feat:` と `docs:`（ADR・タスク表・運用手順）は分ける。直しは `fix:`。push 前の直しは、指示があれば該当のコミットに squash する。
- コミットの末尾に `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` を付ける。
- **push は指示があるまでしない。**

### 報告
- 報告・計画・途中の連絡・PR の説明文は、必ず日本語で書く（ターンの最後のまとめも）。送る前に、最後の段落まで日本語かを確かめる。
- クラウドセッションでは、ブロックの報告を PR の説明文に書き、チャットの最後のまとめはその要約にする。
- 報告は短くする。表は「検査の結果」と「決めたこと」だけにし、詳しくは `.checks/` のファイルに書く。
- 報告には次を書く。
  - 検査の結果（件数・終了コード・ログの最後の `=== DONE`）
  - コミットのハッシュ
  - push の状況
  - 実装中に決めたこと・見つけたこと
  - 画面での確かめ方（操作と期待結果。詳しくは .checks/ のファイル）

## クラウドセッションでの進め方（Claude Code on the web。2026-10-21 に決めた）
- 計画・報告・決めた点は PR の説明文に書く（日本語）。残す決定は ADR（`docs/decisions/`）か `docs/`（細かい決定は `docs/decisions/log/`、画面のデザインは `docs/design/`、一次資料の一覧は `docs/sources.md`）に書く。`.checks/` はセッションの中だけの作業場所として使う（git の外。セッションが終わると消える）。
- 作業はブランチで行い、ブロック（または 1 つの依頼）ごとに PR を作る。PR を作るための作業ブランチへの push は、「push は指示があるまでしない」の例外としてよい。main への push と merge はしない（merge は人が行う）。
- 開発用の秘密の値（`SECRET_KEY`・暗号化の鍵・DB のパスワードなど）はダミー。実データ・本物の鍵は使わない（制約 10）。
- 検査のうち、クラウドで流せないもの（Docker が要るもの: e2e・gate2・gate3・gate4・design-shots・PDF の変換を使うテストなど）は、PR の説明文に「手元で流すこと」として、コマンドと期待する結果を書く。流せたものは件数と終了コードを書く。
- クラウドの環境の設定（環境変数・ネットワークの許可・セットアップ）は `docs/cloud-session.md`。
- 一次資料は `docs/sources.md` の URL から取り直し、SHA-256 で同じ版かを確かめる。

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
make seed      # python manage.py seed_dummy  （外国人31人・受入れ機関4社。作成済みなら何もしない）
make seed-reset  # ダミーデータを消して作り直す（DATE=YYYY-MM-DD で基準日）。ダミーの外国人に手で足した記録・テストシナリオの記録も消える
make seed-scenario  # 手動のテストシナリオの本線（王 麗娜さん）の記録（UNTIL=N で節 1〜N、DOCS=1 で生成物も、RESET=1 で作り直す、REPLACE=1 で画面で作った同じ記録を消してから作る）
make check-quick T=tests/core  # 直すたびに: lint と、指定したテスト（slow を除く）
make test      # 単体テストの全部を並列で（-n auto・--reuse-db。slow を含む）
make test-lf   # 前回落ちたテストだけ
make test-db   # テスト用の DB を作り直して全部（マイグレーションを直したとき）
make lint      # ruff check . && ruff format --check . && mypy .
make format    # ruff format . && ruff check --fix .
make e2e       # 通しのテスト（Playwright。並列。gate2・gate3・design-shots は飛ばす）
make gate2     # 段階2の受け入れ確認（DATE=YYYY-MM-DD で基準日。画面の「今日」も変わる）
make messages  # UI 文言の抽出とコンパイル（.po と .mo の両方をコミット）
make filings   # 日次の届出タスクの起票を手で実行（DATE=YYYY-MM-DD で基準日を指定）
make forms     # 様式マスタ（templates/forms・mappings）を読み込む
make checks    # push の前に: lint・test・e2e・gate2（既定は 1 つの基準日。GATE2_DATES で 3 つ）・gate3。終了コードと === DONE を .checks/logs/<日時>_<CHECKS_NAME>_checks.log（latest.log）に書く
```
