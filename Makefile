COMPOSE := docker compose
MANAGE := $(COMPOSE) exec web python manage.py

.PHONY: checks e2e gate2 design-shots seed seed-reset filings env up down build logs migrate admin shell psql lock test lint format messages

env:        ## .env を作る・足りないキーを補う・鍵を生成する（既存の値は変えない）
	python3 scripts/init_env.py

up:         ## コンテナを起動する
	$(COMPOSE) up -d

down:       ## コンテナを停止する（DB のデータは残る）
	$(COMPOSE) down

build:      ## web イメージを作り直す（依存を変えたとき）
	$(COMPOSE) build web

logs:       ## ログを追う
	$(COMPOSE) logs -f

migrate:    ## マイグレーションを適用する
	$(MANAGE) migrate

admin:      ## 管理者ロールのユーザーを対話で作る（superuser は使わない）
	$(MANAGE) create_admin

shell:      ## Django shell
	$(MANAGE) shell

psql:       ## DB に psql で入る
	$(COMPOSE) exec db sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

lock:       ## pyproject.toml の変更を uv.lock に反映する（その後 make build）
	$(COMPOSE) run --rm --no-deps web uv lock

test:       ## テストを実行する（先に make up）
	$(COMPOSE) exec web pytest

e2e:        ## 通しのテスト（Playwright、Chromium）。初回はイメージのビルドに時間がかかる
	$(COMPOSE) --profile e2e run --rm e2e

gate2:      ## 段階2の受け入れ確認（seed → 担当者 2 人が定期面談と相談を画面で記録。DATE=YYYY-MM-DD で基準日）
	$(COMPOSE) --profile e2e run --rm -e GATE2=1 $(if $(DATE),-e GATE_DATE=$(DATE),) e2e pytest -s e2e/test_phase2_gate.py

CHECKS_LOG ?= .checks/checks.log
CHECK_LIMIT ?= 900
GATE2_DATES ?=
checks:     ## lint・test・e2e（GATE2_DATES="2026-10-17 …" で gate2 も）を順に流し、終了コードと === DONE を CHECKS_LOG に書く
	tools/run_checks.sh $(CHECKS_LOG) $(CHECK_LIMIT) "LINT::make lint" "TEST::make test" "E2E::make e2e" $(foreach d,$(GATE2_DATES),"GATE2 $(d)::make gate2 DATE=$(d)")

design-shots: ## 見本画面（/ui/）のスクリーンショットを docs/design/ に保存する
	$(COMPOSE) --profile e2e run --rm -e DESIGN_SHOTS=1 e2e pytest e2e/test_design_shots.py

lint:       ## 静的検査（ruff check / ruff format --check / mypy）
	$(COMPOSE) exec web sh -c 'ruff check . && ruff format --check . && mypy .'

format:     ## 整形と自動修正
	$(COMPOSE) exec web sh -c 'ruff format . && ruff check --fix .'

seed:       ## ダミーデータを作る（外国人31人・受入れ機関4社。作成済みなら何もしない）
	$(MANAGE) seed_dummy $(if $(DATE),--date $(DATE),)

seed-reset: ## ダミーデータだけを消して作り直す（DATE=YYYY-MM-DD で基準日を指定できる）
	$(MANAGE) seed_dummy --reset $(if $(DATE),--date $(DATE),)

seed-docs:  ## ダミーの生成物の場面（署名待ち・署名済み・回収のタスク・警告つき。seed の後に。T39）
	$(MANAGE) seed_documents

filings:    ## 日次の届出タスクの起票を手で実行する（DATE=YYYY-MM-DD で基準日を指定できる）
	$(MANAGE) create_scheduled_filings $(if $(DATE),--date $(DATE),)

forms:      ## 様式マスタ（templates/forms・mappings）を読み込む。何度実行しても同じ結果
	$(MANAGE) load_form_templates

messages:   ## UI 文言を .po に抽出し .mo にコンパイルする（.po と .mo の両方をコミットする）
	$(COMPOSE) exec web sh -c 'python manage.py makemessages -l ja --no-location --no-obsolete && python manage.py compilemessages -l ja'
