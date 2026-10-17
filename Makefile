COMPOSE := docker compose
MANAGE := $(COMPOSE) exec web python manage.py

.PHONY: e2e gate2 design-shots seed seed-reset filings env up down build logs migrate admin shell psql lock test lint format messages

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

design-shots: ## 見本画面（/ui/）のスクリーンショットを docs/design/ に保存する
	$(COMPOSE) --profile e2e run --rm -e DESIGN_SHOTS=1 e2e pytest e2e/test_design_shots.py

lint:       ## 静的検査（ruff check / ruff format --check / mypy）
	$(COMPOSE) exec web sh -c 'ruff check . && ruff format --check . && mypy .'

format:     ## 整形と自動修正
	$(COMPOSE) exec web sh -c 'ruff format . && ruff check --fix .'

seed:       ## ダミーデータを作る（外国人30人・受入れ機関3社。作成済みなら何もしない）
	$(MANAGE) seed_dummy $(if $(DATE),--date $(DATE),)

seed-reset: ## ダミーデータだけを消して作り直す（DATE=YYYY-MM-DD で基準日を指定できる）
	$(MANAGE) seed_dummy --reset $(if $(DATE),--date $(DATE),)

filings:    ## 日次の届出タスクの起票を手で実行する（DATE=YYYY-MM-DD で基準日を指定できる）
	$(MANAGE) create_scheduled_filings $(if $(DATE),--date $(DATE),)

messages:   ## UI 文言を .po に抽出し .mo にコンパイルする（.po と .mo の両方をコミットする）
	$(COMPOSE) exec web sh -c 'python manage.py makemessages -l ja --no-location --no-obsolete && python manage.py compilemessages -l ja'
