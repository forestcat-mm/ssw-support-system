# 特定技能 登録支援機関 業務管理システム（MVP）

登録支援機関（自社）の内部業務を一元化する自社専用の Web システム。段階1では、外国人台帳・受入れ機関台帳・雇用契約・届出タスクと期限ダッシュボード・管理者向けの設定画面までを提供する。

- 仕様の正本: [`docs/requirements.md`](docs/requirements.md)（章番号で参照する）
- 作業の約束事: [`CLAUDE.md`](CLAUDE.md)
- 段階1のタスクと完了確認: [`docs/tasks-phase1.md`](docs/tasks-phase1.md)
- 設計判断（ADR）: [`docs/decisions/`](docs/decisions/)
- 管理者向けの運用手順: [`docs/operations.md`](docs/operations.md)
- ダミーデータの内容: [`docs/dummy-data.md`](docs/dummy-data.md)
- 業務画面のデザイン: [`docs/design/README.md`](docs/design/README.md)

## 必要なもの

- Docker Desktop（Docker Compose v2）
- make
- Python 3（`make env` が標準ライブラリだけで `.env` を作る）

アプリの Python・PostgreSQL・Redis・Mailpit はすべてコンテナの中で動くので、手元に入れる必要はない。

## 起動する

```
make env        # .env を作る（鍵を生成する。既存の .env の値は変えない）
make up         # コンテナを起動する（web / qcluster / db / redis / mailpit）
make migrate    # DB を作る
make admin      # 最初の管理者を対話で作る（superuser は使わない）
```

- 業務画面: http://localhost:8000/ （ログイン後は期限ダッシュボード）
- 初回ログインでは二要素認証（認証アプリ）の設定から始まる。回復用コードは必ず控える。
- 開発用のメール（招待メールなど）: http://localhost:8025/ （Mailpit）
- 管理画面: http://localhost:8000/admin/ （受入れ機関・雇用契約の登録と、マスタ。日常の設定は業務画面の「設定」）

止めるときは `make down`（DB のデータは残る）。依存を変えたときは `make build`。

## ダミーデータを入れる

本物の外国人情報は開発環境に入れない（CLAUDE.md の制約10）。確認はダミーデータで行う。

```
make seed                     # 外国人30人・受入れ機関2社など（作成済みなら何もしない）
make seed-reset               # ダミーだけを消して作り直す
make seed DATE=2026-10-16     # 基準日を指定する（同じ基準日なら同じ内容）
```

ダミーの担当者 `dummy_tanto` でログインするには、`docker compose exec web python manage.py changepassword dummy_tanto` でパスワードを設定する。内容の詳細は [`docs/dummy-data.md`](docs/dummy-data.md)。

ダミーデータの氏名・番号・住所はすべて架空。法人番号は検査用数字の合った `99` 始まり、電話は `000-` 始まり、メールは `example.invalid`。

## テスト・検査

```
make test           # pytest（先に make up）
make lint           # ruff check / ruff format --check / mypy（strict）
make format         # 整形と自動修正
make e2e            # 通しのテスト（Playwright・Chromium。e2e コンテナ。初回はイメージの作成に時間がかかる）
make design-shots   # 見本画面（/ui/）のスクリーンショットを docs/design/ に保存する
make messages       # UI 文言を .po に抽出し .mo にコンパイルする（両方をコミットする）
```

コミットの前に `make test`・`make lint`・`make e2e` を通す。コミットは 1 タスク 1 コミットで、接頭辞は `feat: ` / `fix: ` / `chore: ` / `docs: `。

## よく使うそのほかのコマンド

```
make logs           # ログを追う
make shell          # Django shell
make psql           # DB に psql で入る
make filings        # 日次の届出タスクの起票を手で実行する（DATE=YYYY-MM-DD で基準日を指定）
make lock           # pyproject.toml の変更を uv.lock に反映する（その後 make build）
```

日次の起票は `qcluster`（Django-Q2）が毎日 6:00 に実行する（ADR-0005）。

## 環境変数

環境ごとに変わる値はすべて環境変数で渡す（CLAUDE.md の制約4）。手元は `.env`（git に入れない）、本番はサーバーの環境変数。`.env.example` と同じ並び。

| 変数 | 用途 | 手元の値（.env.example） |
|---|---|---|
| `DJANGO_SECRET_KEY` | Django の秘密鍵 | `make env` が生成する |
| `DJANGO_DEBUG` | デバッグ表示 | `true`（本番は `false`） |
| `DJANGO_ALLOWED_HOSTS` | 受け付けるホスト名（カンマ区切り） | `localhost,127.0.0.1` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | CSRF で信頼するオリジン | `http://localhost:8000,...` |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | db コンテナの初期化 | 開発用のダミー値 |
| `DATABASE_URL` | web からの DB の接続先 | `postgres://ssw:...@db:5432/ssw` |
| `REDIS_URL` | Django-Q2 のブローカー | `redis://redis:6379/0` |
| `EMAIL_HOST` / `EMAIL_PORT` / `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` / `EMAIL_USE_TLS` | メール送信（手元は Mailpit、本番は自社 SMTP のみ） | `mailpit` / `1025` |
| `DEFAULT_FROM_EMAIL` | 送信元のメールアドレス | `noreply@example.invalid` |
| `DJANGO_STORAGE_BACKEND` / `DJANGO_STORAGE_OPTIONS` | 添付ファイルの保存先（手元はローカル、本番は S3 互換） | `FileSystemStorage` / `{"location": "/app/media"}` |
| `AUTH_LOCKOUT_FAILURE_LIMIT` / `AUTH_LOCKOUT_MINUTES` | ログイン失敗のロック（回数・分） | `5` / `30` |
| `SESSION_IDLE_TIMEOUT_MINUTES` | 無操作でログアウトするまでの分 | `30` |
| `TRUST_CF_CONNECTING_IP` | `CF-Connecting-IP` を接続元とみなす（Cloudflare Tunnel 経由でしか届かない構成だけ `true`） | `false` |
| `FIELD_ENCRYPTION_KEYS` | 列暗号化の鍵（カンマ区切り。先頭が現在の鍵） | `make env` が生成する |
| `BLIND_INDEX_KEY` | 番号の検索用インデックスの鍵 | `make env` が生成する |
| `ALLOW_DUMMY_DATA` | `make seed` を許す（手元だけ。本番には入れない） | `true` |

暗号化の鍵を失うと、暗号化した在留カード番号・旅券番号は復号できない。本番の鍵は DB のバックアップとは別の場所に保管する（`docs/tasks-phase1.md` の本番準備）。

## 開発者向けの管理画面

管理者のメニューには出さないが、権限のある人は URL で直接開ける（T15a）。

| 画面 | URL |
|---|---|
| ログイン失敗の記録（django-axes） | `/admin/axes/accessattempt/` |
| 日次の処理の予定・結果（Django-Q2） | `/admin/django_q/schedule/` ・ `/admin/django_q/success/` ・ `/admin/django_q/failure/` |
| ロールと権限（ロールの変更） | `/admin/core/role/` ・ `/admin/core/user/` |
| 監査ログ（admin 版） | `/admin/core/auditlog/` |

ユーザー・担当者・事務所・自機関・監査ログの日常の操作は、業務画面の「設定」で行う（T15b）。受入れ機関と雇用契約の登録・編集は、段階2で業務画面ができるまで管理画面で行う。

## 構成

```
backend/
  config/       settings・urls（環境変数は django-environ）、管理画面のテンプレートの上書き
  apps/         core（自機関・ユーザー・監査）/ workers / hosts / contracts / filings / seed
  services/     業務ロジック（期限計算・届出の起票・検査・監査など。画面から切り離す）
  tests/        pytest（単体・画面）
  e2e/          Playwright の通しのテスト
docs/           仕様書・タスク・ADR・運用手順・デザイン
```
