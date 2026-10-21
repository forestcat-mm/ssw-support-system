COMPOSE := docker compose

# どこで動かすか。docker（既定。手元）: web・e2e のコンテナの中。host: クラウドの VM（Docker なし）の
# backend/ で、.env を読んで uv の venv で（tools/cloud/host_run.sh。クラウドでは SessionStart フックが
# RUNNER=host を立てる。docs/cloud-session.md）
RUNNER ?= docker
export RUNNER
ifeq ($(RUNNER),host)
WEB := tools/cloud/host_run.sh
# e2e: 単体テストと別の DB（docker-compose の e2e と同じ）。$(1) は足す環境変数（GATE2=1 など）
E2E = $(WEB) env TEST_DB_SUFFIX=e2e DJANGO_ALLOW_ASYNC_UNSAFE=true $(1)
NO_HOST = @echo "RUNNER=host では $@ を使わない（$(1)）" >&2; exit 1
else
WEB := $(COMPOSE) exec web
E2E = $(COMPOSE) --profile e2e run --rm $(foreach v,$(1),-e $(v)) e2e
endif
MANAGE := $(WEB) python manage.py

.PHONY: checks check-quick test-lf test-db e2e gate2 gate3 gate4 design-shots blank-pages seed seed-reset seed-scenario filings env up down build logs migrate admin shell psql lock test lint format messages

env:        ## .env を作る・足りないキーを補う・鍵を生成する（既存の値は変えない）
	python3 scripts/init_env.py

up:         ## コンテナを起動する
ifeq ($(RUNNER),host)
	$(call NO_HOST,PostgreSQL・Redis は SessionStart フックが起こす)
else
	$(COMPOSE) up -d
endif

down:       ## コンテナを停止する（DB のデータは残る）
ifeq ($(RUNNER),host)
	$(call NO_HOST,コンテナが無い)
else
	$(COMPOSE) down
endif

build:      ## web イメージを作り直す（依存を変えたとき）
ifeq ($(RUNNER),host)
	$(call NO_HOST,依存は SessionStart フックの uv sync が入れる)
else
	$(COMPOSE) build web
endif

logs:       ## ログを追う
ifeq ($(RUNNER),host)
	$(call NO_HOST,コンテナが無い)
else
	$(COMPOSE) logs -f
endif

migrate:    ## マイグレーションを適用する
	$(MANAGE) migrate

admin:      ## 管理者ロールのユーザーを対話で作る（superuser は使わない）
	$(MANAGE) create_admin

shell:      ## Django shell
	$(MANAGE) shell

psql:       ## DB に psql で入る
ifeq ($(RUNNER),host)
	$(WEB) sh -c 'psql "$$DATABASE_URL"'
else
	$(COMPOSE) exec db sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'
endif

lock:       ## pyproject.toml の変更を uv.lock に反映する（その後 make build）
ifeq ($(RUNNER),host)
	cd backend && uv lock
else
	$(COMPOSE) run --rm --no-deps web uv lock
endif

# 検査の段（修正 1 回目の後。README の「検査」と CLAUDE.md の作業の進め方）:
#   make check-quick T=…  直すたびに: lint と、指定したファイル・アプリのテスト（slow を除く）
#   make test             タスクのコミットのたびに: 単体テストの全部を並列で（slow を含む）
#   make test-lf          落ちたテストだけを流し直す（直したとき）
#   make checks           push の前に 1 回: lint・test・e2e・gate2（既定は 1 つの基準日）・gate3
# テスト用の DB は使い回す（--reuse-db。新しいマイグレーションは流れる）。マイグレーションを直した・
# 消したときや DB がおかしいときは make test-db で作り直す。
# -rs: 飛ばしたテストの件数と理由を出す（tools/run_checks.sh がログに写す）
PYTEST := $(WEB) pytest -n auto --reuse-db -rs
T ?=

test:       ## 単体テストの全部を並列で（slow を含む。先に make up）
	$(PYTEST) $(T)

check-quick: ## lint と、T で指定したファイル・アプリのテスト（slow を除く。例: make check-quick T=tests/core）
	$(MAKE) lint
	$(PYTEST) -m "not slow" $(T)

test-lf:    ## 前回落ちたテストだけを流し直す
	$(PYTEST) --lf

test-db:    ## テスト用の DB を作り直して全部を流す（マイグレーションを直した・消したとき）
	$(WEB) pytest -n auto --create-db -rs

E2E_WORKERS ?= 4
e2e:        ## 通しのテスト（Playwright、Chromium。並列。E2E_WORKERS で数を変える）。初回はイメージのビルドに時間がかかる
	$(call E2E,) pytest e2e -n $(E2E_WORKERS)

gate2:      ## 段階2の受け入れ確認（seed → 担当者 2 人が定期面談と相談を画面で記録。DATE=YYYY-MM-DD で基準日）
	$(call E2E,GATE2=1 $(if $(DATE),GATE_DATE=$(DATE),)) pytest -s e2e/test_phase2_gate.py

gate3:      ## 段階3の受け入れ確認（seed → seed_documents → 署名・生成・併記・警告・生成不可を画面で。DATE=YYYY-MM-DD で基準日）
	$(call E2E,GATE3=1 $(if $(DATE),GATE_DATE=$(DATE),)) pytest -s e2e/test_phase3_gate.py

gate4:      ## 段階4の受け入れ確認（seed → 一括申請 3 種・一括届出 7 種の xlsm を画面で作り、写した入力チェックと構造を確かめる）
	$(call E2E,GATE4=1) pytest -s e2e/test_phase4_gate.py

# ログは .checks/logs/<日時>_<名前>_checks.log（CHECKS_NAME=phase4-blockD のように名前を付ける）。
# いちばん新しいログは .checks/logs/latest.log でも開ける。
CHECKS_NAME ?=
CHECKS_LOG ?= .checks/logs/$(shell date +%Y-%m-%d_%H%M)_$(if $(CHECKS_NAME),$(CHECKS_NAME)_,)checks.log
CHECK_LIMIT ?= 900
# gate2 は既定で 1 つの基準日。期限の計算・届出の起票にさわったときは 3 つの基準日にする:
#   make checks GATE2_DATES="2026-10-17 2026-11-30 2026-12-28"
GATE2_DATES ?= 2026-10-17
GATE3 ?= 1
checks:     ## push の前に 1 回: lint・test・e2e・gate2（GATE2_DATES）・gate3（GATE3= で外す）・gate4（GATE4=1）を順に流し、終了コードと === DONE を CHECKS_LOG に書く
	tools/run_checks.sh $(CHECKS_LOG) $(CHECK_LIMIT) "LINT::make lint" "TEST::make test" "E2E::make e2e" $(foreach d,$(GATE2_DATES),"GATE2 $(d)::make gate2 DATE=$(d)") $(if $(GATE3),"GATE3::make gate3",) $(if $(GATE4),"GATE4::make gate4",)

design-shots: ## 見本画面（/ui/）のスクリーンショットを docs/design/ に保存する
ifeq ($(RUNNER),host)
	$(call NO_HOST,字体と画面の見え方が手元の e2e のイメージと同じかを確かめていない。手元で make design-shots を流す)
else
	$(call E2E,DESIGN_SHOTS=1) pytest e2e/test_design_shots.py
endif

lint:       ## 静的検査（ruff check / ruff format --check / mypy）
	$(WEB) sh -c 'ruff check . && ruff format --check . && mypy .'

format:     ## 整形と自動修正
	$(WEB) sh -c 'ruff format . && ruff check --fix .'

seed:       ## ダミーデータを作る（外国人31人・受入れ機関4社。作成済みなら何もしない）
	$(MANAGE) seed_dummy $(if $(DATE),--date $(DATE),)

seed-reset: ## ダミーデータだけを消して作り直す（DATE=YYYY-MM-DD で基準日を指定できる）
	$(MANAGE) seed_dummy --reset $(if $(DATE),--date $(DATE),)

seed-scenario: ## 手動のテストシナリオの本線（王 麗娜さん）の記録。UNTIL=N で節 1〜N、DOCS=1 で生成物も、RESET=1 で作り直す、REPLACE=1 で画面で作った同じ記録を消してから作る（make seed の後に）
	$(MANAGE) seed_scenario $(if $(UNTIL),--until $(UNTIL),) $(if $(DOCS),--with-docs,) $(if $(RESET),--reset,) $(if $(REPLACE),--replace,)

seed-docs:  ## ダミーの生成物の場面（署名待ち・署名済み・回収のタスク・警告つき。seed の後に。T39）
	$(MANAGE) seed_documents

filings:    ## 日次の届出タスクの起票を手で実行する（DATE=YYYY-MM-DD で基準日を指定できる）
	$(MANAGE) create_scheduled_filings $(if $(DATE),--date $(DATE),)

forms:      ## 様式マスタ（templates/forms・mappings）を読み込む。何度実行しても同じ結果
	$(MANAGE) load_form_templates

blank-pages: ## 公式の様式を空のまま PDF にし、頁数・埋め込みの字体・代わりの字体を出す（FORMS="1-23 1-5" で絞る、MD=1 で Markdown。手元とクラウドの見比べ）
	$(WEB) python ../tools/forms/blank_pages.py $(if $(MD),--markdown,) $(FORMS)

messages:   ## UI 文言を .po に抽出し .mo にコンパイルする（.po と .mo の両方をコミットする）
	$(WEB) sh -c 'python manage.py makemessages -l ja --no-location --no-obsolete && python manage.py compilemessages -l ja'
