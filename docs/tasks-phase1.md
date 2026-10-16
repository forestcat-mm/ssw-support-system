# 段階1（基盤）タスク一覧

各タスクは 1 セッション・1 コミットで完結させる。順序どおりに進める。
ゲート: 「担当者が日常利用を開始できる」＝ T01〜T14 完了、ダミーデータで台帳とダッシュボードが回る。

| # | タスク | 仕様書 | 完了条件 |
|---|---|---|---|
| T01 | Django プロジェクト + Docker Compose（web / db / redis / mailpit）+ `.env.example` + Makefile | 第10章 | `make up` → admin にログインできる |
| T02 | ruff / mypy / pytest の設定と空テスト、`make lint` `make test` | 第10章 | 両方グリーン |
| T03 | `core`: Organization（1行）, Office, Staff, 設定テーブル（自社名・登録番号・支援言語選択肢） | 第7章 | admin で登録できる。Organization は1行制約 |
| T04 | `core`: User / UserRole（管理者・支援担当者）、TOTP 多要素認証、招待・無効化 | 第4章, 第8章 | 2 ロールでログイン・権限差を確認 |
| T05 | `core`: AuditLog（閲覧・作成・更新・削除・エクスポートを記録するミドルウェア＋ヘルパ） | 第8章 | 外国人詳細の閲覧がログに残る |
| T06 | 暗号化フィールド（在留カード番号・パスポート番号・口座）。鍵は環境変数 | 第8章 | DB 直接参照で平文が見えない |
| T07 | `workers`: ForeignWorker, ResidenceRecord（履歴）, WorkerAddress / Contact（履歴）、「最新行」ビュー | 第7章 | 在留期限を更新すると新行が増え、最新が一覧に出る |
| T08 | `hosts`: HostOrganization, HostSite（産業分類コード・分野・登録法人/協議会加入日・証明書添付）, SupportContract | 第6章「上乗せ要件」, 第7章 | 2社・各1事業所を登録できる |
| T09 | `contracts`: EmploymentContract（版管理：変更は新版、`version` と `change_reason`） | 第7章 | 報酬変更で版が増え、差分が取れる |
| T10 | `services/deadlines.py`: 在留期限（3か月前）・定期面談（3か月）・随時届出（14日）・定期届出（4/1–5/31）・登録更新（6か月前）の期限計算＋テスト | 第5章 F06/F07 | 固定日付テストがグリーン |
| T11 | `filings`: Filing（種別・対象・トリガー事由・期限・提出日・受理番号・状態）と、契約変更/終了・在留期限からの自動起票 | 第5章 F07 | 契約終了を登録すると 14 日期限の Filing が自動生成 |
| T12 | 管理コマンド `seed_dummy`: 受入れ機関2社（工業製品製造業・飲食料品製造業）、外国人30人（中国籍中心、在留期限・入国日をばらす）、契約、面談予定 | 第11章 | 何度実行しても同じ件数で再現 |
| T13 | 画面: 外国人一覧（検索・絞込）／詳細／編集、受入れ機関一覧／詳細 | 第5章 F01/F02 | ダミー30人を一覧から辿れる |
| T14 | 画面: 期限ダッシュボード（担当者別、7日/14日/超過の3色、種別フィルタ） | 第5章 F06 | ダミーデータで赤・黄・緑が出る |
| T15 | README（起動・テスト・ダミー投入の手順）と ADR テンプレート | — | 新しい PC で README どおりに起動できる |

## 本番準備（本番サーバー決定時、`docker-compose.prod.yml` 作成と同時に対応）
- [ ] `docker-compose.prod.yml` を作成する（`./:/app` マウントの解除、`runserver` → gunicorn）
- [ ] `manage.py check --deploy` の警告を解消する。手元（http）はオフのまま、本番は環境変数でオンにする形にする
  - W004 `SECURE_HSTS_SECONDS`
  - W008 `SECURE_SSL_REDIRECT`（Cloudflare Tunnel 経由のため `SECURE_PROXY_SSL_HEADER` と合わせて検討）
  - W012 `SESSION_COOKIE_SECURE`
  - W016 `CSRF_COOKIE_SECURE`
  - W018 `DJANGO_DEBUG=false`
  - W009 本番の `DJANGO_SECRET_KEY` を生成値にする（手元でダミー値のまま起動すると出る）
- [ ] 本番イメージは `uv sync --frozen --no-dev` でビルドし、ruff / mypy / pytest などの開発用依存を入れない
- [ ] 本番の DB はアプリ専用ユーザー（スーパーユーザー権限・CREATEDB なし）で接続する。テスト用 DB の作成権限は開発環境だけにする
- 完了条件: 本番用の環境変数で `check --deploy` の警告が 0 件。本番イメージに開発用依存が無く、アプリの DB ユーザーが SUPERUSER / CREATEDB を持たない

## 段階2 以降の見出し（詳細は着手時に分解）
- 段階2: SupportPlan / SupportPlanItem / SupportRecord（面談は本人用・上司用で別行）、相談記録の分類、証跡添付、キャリアアッププラン・実務経験証明書の交付記録
- 段階3: forms_engine（FormTemplate / FormMapping / GeneratedDocument）、Word→PDF 変換サービス、支援計画書・事前ガイダンス確認書（日中2言語）、生成前バリデーション
- 段階4: 一括申請用 Excel / 一括届出用 Excel のマッピング定義と生成、分野別添付セット（分野参考様式第3-1号 / 第13-1号 / 第13-2号）
