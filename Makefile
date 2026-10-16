COMPOSE := docker compose
MANAGE := $(COMPOSE) exec web python manage.py

.PHONY: up down build logs migrate admin shell psql lock test lint format messages

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

lint:       ## 静的検査（ruff check / ruff format --check / mypy）
	$(COMPOSE) exec web sh -c 'ruff check . && ruff format --check . && mypy .'

format:     ## 整形と自動修正
	$(COMPOSE) exec web sh -c 'ruff format . && ruff check --fix .'

messages:   ## UI 文言を .po に抽出し .mo にコンパイルする（.po と .mo の両方をコミットする）
	$(COMPOSE) exec web sh -c 'python manage.py makemessages -l ja --no-location --no-obsolete && python manage.py compilemessages -l ja'
