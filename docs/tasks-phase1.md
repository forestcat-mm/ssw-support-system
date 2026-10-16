# 段階1（基盤）タスク一覧

各タスクは 1 セッション・1 コミットで完結させる。順序どおりに進める。
ゲート: 「担当者が日常利用を開始できる」＝ T01〜T14 完了、ダミーデータで台帳とダッシュボードが回る。

| # | タスク | 仕様書 | 完了条件 |
|---|---|---|---|
| T01 | Django プロジェクト + Docker Compose（web / db / redis / mailpit）+ `.env.example` + Makefile | 第10章 | `make up` → admin にログインできる |
| T02 | ruff / mypy / pytest の設定と空テスト、`make lint` `make test` | 第10章 | 両方グリーン |
| T03 | `core`: カスタム User（AbstractUser のみ）, 共通列（作成/更新日時・者）, Organization（1行。自社名・登録番号・ロゴ・食品協議会加入日）, Office, Staff, SupportLanguage（支援言語選択肢）, Attachment（django-storages 経由、種別ごとの拡張子・サイズ制限）、UI 文言の ja リソース | 第6章, 第7章, 第9章 | admin で登録できる。Organization は1行制約 |
| T04 | `core`: User（T03 で作成済み）の拡張、UserRole（管理者・支援担当者。行で追加できる構造）、TOTP 多要素認証、招待・無効化（ユーザーは削除せず `is_active` で無効化。作成者・更新者の参照が PROTECT のため）。T04a 権限判定・無効化／T04b TOTP・回復用コード・ロック・セッション／T04c 招待の 3 コミット | 第4章, 第8章 | 2 ロールでログイン・権限差を確認 |
| T05 | `core`: AuditLog（閲覧・作成・更新・削除・エクスポートを記録するミドルウェア＋ヘルパ）、添付ファイルのダウンロードビュー（認可・監査付き。公開 URL は作らない）。T04 の `TODO(T05)` を解消: MFA リセット（管理者のアクション・管理コマンド `reset_mfa` の両方）、ユーザー無効化・再有効化、招待を必ず記録 | 第8章 | 外国人詳細の閲覧・添付のダウンロードがログに残る。MFA リセット（画面・コマンド）が記録される。無効化したユーザーの監査ログが残る |
| T06 | 暗号化フィールド型（`EncryptedCharField`・検索用の `BlindIndexField`。実際の列は T07 以降）。鍵は環境変数（`FIELD_ENCRYPTION_KEYS`・`BLIND_INDEX_KEY`、`make env` で生成）。鍵のローテーション（`rotate_encryption_keys`）とブラインドインデックスの作り直し（`rebuild_blind_indexes`）。TOTP 秘密鍵は平文のまま（ADR-0003）、漏洩時の `reset_mfa --all` | 第8章 | DB 直接参照で平文が見えない |
| T07 | `workers`: ForeignWorker, ResidenceRecord（履歴）, WorkerAddress / Contact（履歴）、「最新行」ビュー。各モデルを `services/audit.py` の `AUDITED_MODELS`（個人情報を含むものは `VIEW_AUDITED_MODELS` にも）に登録し、詳細画面から `audit.record_view()` を呼ぶ。**パスポートは foreign_workers の単一列ではなく、在留情報と同じ履歴テーブル `passport_records`**（番号〔`EncryptedCharField`〕・発行国・発行日・有効期限・番号の `BlindIndexField`）。在留カード番号・パスポート番号は更新のたびに変わり、同一人物に複数の履歴行が残る前提で、二重登録防止の一意性は「同じ番号（ブラインドインデックス）が別の人に紐づかない」ことを意味する（同一人物の複数行は許す。単純な unique 制約にはしない）。実装: PostgreSQL の排他制約（`btree_gist`）＋保存前のフォーム検査。取り消し済みの行は対象外（誤登録を取り消して正しい人に付け直せる）。申請種別に「在留カード再交付」「記載事項変更（新カード交付）」（番号が変わり在留期限は同じ） | 第7章, 第8章 | 在留期限を更新すると新行が増え、最新が一覧に出る。外国人詳細の閲覧が監査ログに残る |
| T08 | `hosts`: HostOrganization, HostSite（産業分類コード・分野・登録法人/協議会加入日・証明書添付）, SupportContract。分野・業務区分はマスタ（区分は適用開始日・終了日つき、飲食料品は 2027-04-01 に 2 区分へ）。登録法人・協議会への加入は受入れ機関×分野の `HostMembership`（ADR-0004）。協力確認書は提出先の市区町村ごとの `HostCooperationConfirmation`。支援委託契約は同じ受入れ機関で期間の重なりを禁止 | 第6章「上乗せ要件」, 第7章 | 2社・各1事業所を登録できる |
| T09 | `contracts`: EmploymentContract（版管理：変更は新版、`version` と `change_reason`）。見出し（外国人×受入れ機関、終了日・終了理由）と版（作成後は変更不可）と版の明細（交代制・諸手当・控除）。版の項目は参考様式第1-6号「雇用条件書」と別紙1「賃金の支払」から逆算（段階3の生成元）。状態（予定／有効／終了）は列で持たず日付と終了の登録から判定。同じ外国人の期間の重なりは排他制約と事前検査。終了の取り消しは管理者のみ | 第7章 | 報酬変更で版が増え、差分が取れる |
| T10 | `services/deadlines.py`: 在留期限（3か月前）・定期面談（3か月）・随時届出（14日）・定期届出（4/1–5/31）・登録更新（6か月前）の期限計算＋テスト。純粋関数が日付だけの `Deadline`（due_on・unadjusted_due_on・opens_on・final_on・priority）を返し、`assess(deadline, today)` が警告レベルを返す。随時届出は事由の日＋14日（民法140条・初日不算入）、行政機関の休日なら翌開庁日（行政機関の休日に関する法律2条）。定期面談は民法143条で3か月。登録更新は満了日の6か月前の月初〜4か月前の月末（最終は3か月前の月末）。祝日は jpholiday | 第5章 F06/F07 | 固定日付テストがグリーン |
| T11 | `filings`: Filing（種別・対象・トリガー事由・期限・提出日・受理番号・状態）と、契約変更/終了・在留期限からの自動起票。契約の終了は `services/contracts.terminate()`（コメントの位置）から起票し、起算日は終了日。契約の変更は新しい版の `changed_on` を起算日とし、届出の根拠に `services/contracts.diff()` の差分を残す。起票日は `Deadline.opens_on`、期限は `Deadline.due_on`（法令上の期限）を保存する。種別ごとに区別（入管の届出／申請／内部）と届出主体（所属機関／登録支援機関／内部）を持つ。変更届出の要否は運用要領（令和8年8月版）第7章の別表で判定（`services/filing_rules.py`。判断できない変更は要否の確認の印を付けて起票）。受入れ困難届出は終了理由が会社都合・その他のとき（自己都合退職・契約満了・転職は除く）。日次の起票は Django-Q2 の実行予定（ADR-0005） | 第5章 F07 | 契約終了を登録すると 14 日期限の Filing が自動生成 |
| T12 | 管理コマンド `seed_dummy`: 受入れ機関2社（工業製品製造業・飲食料品製造業）、外国人30人（中国籍中心、在留期限・入国日をばらす）、契約、面談予定。在留カード番号・旅券番号・法人番号は `services/residence_card.generate_dummy` / `services/passport.generate_dummy` / `services/corporate_number.generate_dummy`（基礎番号の先頭 `99`）に固定の種の `random.Random` を渡して作る（形式検査と同じ形）。README にダミーである旨を書く。内容と使い方は `docs/dummy-data.md`。台帳 `seed.SeedObject` に載せた行だけを `--reset` で消す。DEBUG かつ `ALLOW_DUMMY_DATA=true` のときだけ動く | 第11章 | 何度実行しても同じ件数で再現 |
| T13 | 画面: 外国人一覧（検索・絞込）／詳細／編集、受入れ機関一覧／詳細。T13a〜f に分ける: a=デザイントークン・共通レイアウト・部品の見本 `/ui/`（管理者のみ）・権限の共通部品・Playwright の土台（`make e2e`）、b=外国人一覧、c=外国人詳細、d=外国人の登録・編集、e=履歴の追加・取消、f=外国人の無効化（管理者のみ・理由必須。有効な在留情報・旅券は取消、未完了の届出タスクは取消）と受入れ機関一覧／詳細。a の後に `/ui/` でデザインを確認してから b に進む。外国人一覧の絞り込みの欄は、T13c 以降で「検索・受入れ機関・担当者・状態」を 1 行目に、「分野・警告レベル・並び順」は折りたたみに整理する（今は 2 段に折り返している） | 第5章 F01/F02、第9章 | ダミー30人を一覧から辿れる |
| T14 | 画面: 期限ダッシュボード（担当者別、警告レベルの色分け: 超過＝Blush、7日以内＝Butter（太字＋左の印）、14日以内＝Mint Foam、対応期間中＝枠線のみ（塗りなし）、受付前＝日付の文字だけ。部品は T13a の `level_badge`。種別フィルタ）。在留期限は最新行のビュー（`LatestResidenceRecord`）から取る。色分けは `assess()` が返すレベルを使い、基準は繰り延べ前の `unadjusted_due_on`（安全側）。法令上の期限 `due_on` は画面に併記する（閉庁日で繰り延べたときは「法令上の期限: ○/○」）。届出主体（所属機関／登録支援機関）でも絞り込めるようにする（段階4の電子届出システムが所属機関用と登録支援機関用で分かれるため）。残り日数は `services/filings.deadline_of(filing)` を `assess()` に渡して求める。外国人一覧（T13b）は既定で入国前・在留中だけを出すため、離職直後で雇用契約終了届出などが未提出の人は一覧から消える。その人はダッシュボードの届出タスクで拾う（状態ではなく未完了の届出タスクから出す） | 第5章 F06 | ダミーデータで超過（Blush）・7日以内（Butter）・14日以内（Mint Foam）・対応期間中（枠線）が出る |
| T15 | README（起動・テスト・ダミー投入の手順。起動は `make env` → `make up` → `make migrate` → `make admin`）と ADR テンプレート。運用手順も書く: 退職時は「ユーザー無効化」と「Staff の is_active オフ」の両方を行う（連動しない）／管理者は 2 人以上置く／端末紛失時（回復用コード → 管理者の MFA リセット → 最後の手段 `reset_mfa`）／ロック解除（admin のアクセス試行の削除）／年 1 回（年末まで）jpholiday を更新し、翌年の祝日（法改正・特例を含む）が正しいかを確認する／誤って無効化した外国人は、管理者が admin の外国人の画面で無効化を外して戻す（無効化のときに取り消された在留情報・旅券は新しい行として登録し直す。日付で起票される届出タスクは翌朝の日次の起票か `make filings` で起票し直され、それ以外は手で起票する）／ダミーデータの説明は `docs/dummy-data.md` にリンクする | — | 新しい PC で README どおりに起動できる |

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
- [ ] 接続元 IP: 本番は `TRUST_CF_CONNECTING_IP=true`（監査ログと django-axes が `services/client_ip.py` を共有。T05 で実装済み）。サーバーのポートを開けず Cloudflare Tunnel 経由でしか届かないことを確認してから true にする。そのうえで、ユーザー名単位のロックに IP 単位の条件（パスワードスプレー対策）を加えるか判断し、`SILENCED_SYSTEM_CHECKS` の `axes.W006` を見直す
- [ ] マイグレーション用ユーザーが PostgreSQL 拡張 `btree_gist`（外国人台帳の排他制約で使用。信頼済み拡張なので DB の所有者・CREATE 権限で作成可）を作れることを確認する。マネージド DB（RDS 等）へ移るときは拡張の対応を確認する
- [ ] 監査ログを DB ユーザーの権限でも追記専用にする: マイグレーション用ユーザー（テーブル所有者）とアプリ用ユーザーを分け、アプリ用ユーザーから `core_auditlog` の UPDATE / DELETE / TRUNCATE を REVOKE する（INSERT・SELECT のみ）。トリガー（`core_auditlog_append_only`）とあわせて二重に禁止する
- [ ] 暗号化鍵（`FIELD_ENCRYPTION_KEYS`）・ブラインドインデックス鍵（`BLIND_INDEX_KEY`）の保管場所と復旧手順を決めて文書にする。鍵を失うと暗号化した全データが読めなくなるため、DB バックアップとは別の場所に保管し、2 人以上がアクセスできるようにする。ローテーション手順（新しい鍵を先頭に足す → `rotate_encryption_keys` → `--check` が 0 件 → 旧鍵を外す）も同じ文書に置く
- [ ] DB バックアップを必ず暗号化し、その鍵もバックアップとは別の場所に保管する（ADR-0003: TOTP 秘密鍵とパスワードのハッシュが同時に漏れるのを防ぐ）
- [ ] 漏洩が疑われたときの手順を文書にする（`reset_mfa --all --yes` で全員の MFA をリセット、パスワードの再設定、暗号化鍵のローテーション）
- [ ] 本番でも `qcluster`（Django-Q2）を動かし、止まったら気づけるよう監視する（日次の届出タスクの起票が止まるため。ADR-0005）
- [ ] 監査ログを改変不可の保存先へ日次で書き出す（第8章・第10章。例: S3 Object Lock のコンプライアンスモード、保持 5 年以上）。保存先の決定と同時に対応し、Django-Q のジョブにする
- [ ] ファイル保存を S3 互換ストレージにする（`DJANGO_STORAGE_BACKEND=storages.backends.s3.S3Storage` と `DJANGO_STORAGE_OPTIONS`。非公開バケット・署名付き URL・上書き禁止を実ストレージで確認）
- [ ] 静的ファイル（業務画面の CSS、admin）を本番で配信する: `STATIC_ROOT` を決めてイメージのビルド時に `collectstatic` を実行し、gunicorn の前段（または WhiteNoise）で配信する。手元は `runserver` が配信している
- [ ] 本番の DB はアプリ専用ユーザー（スーパーユーザー権限・CREATEDB なし）で接続する。テスト用 DB の作成権限は開発環境だけにする
- 完了条件: 本番用の環境変数で `check --deploy` の警告が 0 件。本番イメージに開発用依存が無く、アプリの DB ユーザーが SUPERUSER / CREATEDB を持たない。添付のアップロード・ダウンロードが S3 互換ストレージで動く。アプリの DB ユーザーで `core_auditlog` を UPDATE / DELETE / TRUNCATE すると権限エラーになる

## 将来運用
- [ ] 監査ログの保持期間（最低 5 年。入管の記録保存義務期間を下回らない）を過ぎた行の削除手順を作る。管理コマンドで、DB 所有者の権限で対象期間を外部保存先への書き出し済みと確認 → トリガー `core_auditlog_append_only` を一時的に外す → 期間指定で削除 → トリガーを戻す、を 1 トランザクションで行い、実施自体を監査ログに記録する

## 段階2 以降の見出し（詳細は着手時に分解）
- 段階2: SupportPlan / SupportPlanItem / SupportRecord（面談は本人用・上司用で別行）、相談記録の分類、証跡添付、キャリアアッププラン・実務経験証明書の交付記録
  - 外国人詳細の履歴タブに、行ごとの「全桁を表示」を足す（過去の在留カード番号・旅券番号を入管照会や過去書類との突き合わせで使うため。押したときだけ復号し、監査ログに 1 件残す。置き場は `workers/components/history_row.html` の `data-number-actions`）
- 段階3: forms_engine（FormTemplate / FormMapping / GeneratedDocument）、Word→PDF 変換サービス、支援計画書・事前ガイダンス確認書（日中2言語）、生成前バリデーション
- 段階4: 一括申請用 Excel / 一括届出用 Excel のマッピング定義と生成、分野別添付セット（分野参考様式第3-1号 / 第13-1号 / 第13-2号）
