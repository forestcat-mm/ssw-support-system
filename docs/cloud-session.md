# クラウドセッション（Claude Code on the web）の環境の設定

2026-10-21 に、手元の Mac のセッションからクラウドのセッション（claude.ai/code）へ移すために書き、同じ日にクラウドのセッションで確かめて直した。進め方の決まりは CLAUDE.md の「クラウドセッションでの進め方」。

**要点**: クラウドの VM では Docker のイメージを取れない（下の 8）。Docker を使わずに VM の上で動かす（`RUNNER=host`）。OS の道具は環境の設定画面の setup script（`tools/cloud/setup.sh`）が入れ、セッションごとの準備は SessionStart フック（`tools/cloud/session_start.sh`）が行う。検査は手元と同じ `make` で流せる（`RUNNER=host` はフックが立てる）。

## 1. VM（2026-10-21 に確かめた）

| 項目 | 値 |
|---|---|
| OS | Ubuntu 24.04.5 LTS・x86_64・4 CPU・メモリ 15GB |
| 利用者 | root（`sudo` は要らない） |
| 初めから入っているもの | python3 3.13.16（`/usr/bin/python3.12` 3.12.3 もある）・uv 0.11.32・PostgreSQL 16.15・Redis 7.0.15・LibreOffice 24.2.7.2（GUI つき）・IPA ゴシック・Carlito・docker 29.9.1（daemon は止まっている）・Chromium（playwright の chromium-1194） |
| 足りないもの | gettext・GUI なしの LibreOffice・IPA 明朝・Noto CJK・uv 0.13.1・playwright 1.63 の Chromium（chromium-1243） → setup script が入れる |

- venv の Python は 3.12（`backend/.python-version`。web のイメージの `python:3.12-slim-bookworm` と同じ 3.12。細かい版は VM の 3.12.3）。
- LibreOffice は手元の web のイメージ（Debian bookworm の 7.4.7.2）と違い 24.2.7.2。帳票の頁数の検査はクラウドでも通るが、字体は違う（下の 7）。
- `uv.lock` は uv 0.11.32 でも 0.13.1 でもそのまま使える（`uv lock --check` が通る）。setup script は Dockerfile と同じ 0.13.1 を入れる。

## 2. 環境変数

値そのものはここに書かない。開発用はすべてダミーでよい（実データ・本物の鍵は使わない。CLAUDE.md の制約 4・10）。`.env` は SessionStart フックが `scripts/init_env.py`（`make env` と同じ）で作り、接続先を VM の構成に合わせて書き換える。環境の設定画面の「Environment variables」には何も足さなくてよい。

| 名前 | 用途 | クラウドでの値 |
|---|---|---|
| `DJANGO_SECRET_KEY`・`FIELD_ENCRYPTION_KEYS`・`BLIND_INDEX_KEY` | Django の秘密鍵・列の暗号化の鍵・検索用の鍵 | `init_env.py` が生成（下の 5 の「.env と暗号化の鍵」） |
| `DATABASE_URL` | DB の接続先 | ホストを `localhost` に（`@db:5432` → `@localhost:5432`） |
| `REDIS_URL` | Django-Q のジョブ | `redis://localhost:6379/0` |
| `EMAIL_HOST` | メール（手元は Mailpit） | `localhost`（Mailpit は無い。単体テストはメールを外へ送らない） |
| `DJANGO_STORAGE_OPTIONS` | 添付の保存先 | `'{"location": "<リポジトリ>/media"}'`。JSON の値は `'…'` で囲む（`.env.example` の決まり。bash の `. .env` と uv の `--env-file` が囲まない JSON で崩れたため） |
| そのほか（`DJANGO_DEBUG`・`POSTGRES_*`・`AUTH_*`・`SUBMISSION_*`・`TRUST_CF_CONNECTING_IP`・`ALLOW_DUMMY_DATA`） | | `.env.example` のまま |
| `RUNNER` | make の動かし方（下の 6） | `host`（フックが `CLAUDE_ENV_FILE` に書く） |
| `PLAYWRIGHT_BROWSERS_PATH` | Playwright のブラウザの場所 | `/opt/pw-browsers`（フックが書く。setup script が chromium-1243 を足す） |
| `UV_SYSTEM_CERTS` | uv がプロキシの CA を使う | `1`（フックが書く。VM の既定の `UV_NATIVE_TLS` は非推奨の警告が出るので外す） |
| `TEST_DB_SUFFIX`・`DJANGO_ALLOW_ASYNC_UNSAFE` | e2e のテスト DB の名前・Playwright と Django の同居 | `make e2e`・`make gate2` などが付ける |

## 3. ネットワークの接続先（2026-10-21 の状態コード）

アプリそのものは外部 API に接続しない（制約 1。例外は自社 SMTP）。次は開発の作業で使う。接続先の許可（環境の設定画面の「Network access」）は変えない。下の「使わない」は、許可が無くても困らないように道具を組んだもの。

| 用途 | 接続先 | 状態 |
|---|---|---|
| GitHub（clone・push・PR） | `github.com` | git は通る（Web の画面は 400・403 だが使わない） |
| GitHub のリリース | `github.com/.../releases/download/...` | 200 |
| Python のパッケージ（uv・uv そのもの） | `pypi.org`・`files.pythonhosted.org` | 200・404（届いている。中身の取得は通る） |
| OS のパッケージ | `archive.ubuntu.com`・`security.ubuntu.com`（VM）、`deb.debian.org`・`security.debian.org` | すべて 200 |
| Playwright のブラウザ | `cdn.playwright.dev` | ブラウザを取れる（根は 400）。予備の `playwright.download.prss.microsoft.com` は 502（使わなくてよい） |
| 一次資料 | `www.moj.go.jp`・`laws.e-gov.go.jp`・`nvlpubs.nist.gov` | すべて 200 |
| 字体の配布元（ADR-0046。作り直すときだけ） | `raw.githubusercontent.com` | 200 |
| Docker のイメージ | `mcr.microsoft.com` | 200（Playwright のイメージを取れた） |
| Docker のイメージ | `registry-1.docker.io`・`auth.docker.io`・`production.cloudflare.docker.com` | 401・200・403。manifest の取得は **429**（取得の回数の上限）で、イメージを取れない |
| uv の取得（使わない） | `astral.sh` → `releases.astral.sh` | **403**。uv は PyPI から入れる |
| ghcr のイメージの中身（使わない） | `ghcr.io`（401）→ `pkg-containers.githubusercontent.com` | **403**。`ghcr.io/astral-sh/uv` を取れないので、Dockerfile をクラウドで組めない |

一次資料の一覧と SHA-256 は `docs/sources.md`。取得は User-Agent を付け、間をあけて 1 件ずつ行う。

## 4. setup script（`tools/cloud/setup.sh`）

環境の設定画面の「Setup script」に、`tools/cloud/setup.sh` の全文をそのまま貼る。setup script はセッションを始める前に root で 1 回流れ、終わった後のファイルが環境の cache になる（約 5 分に収まれば。次のセッションは cache から始まり、setup script は流れない）。

- 中身: OS の道具だけ。apt（PostgreSQL 16・Redis・gettext・GUI なしの LibreOffice・字体・python3.12）、uv 0.13.1（PyPI から）、playwright 1.63.0 の Chromium（`/opt/pw-browsers` に足す。`--with-deps`）。リポジトリのファイルは読まない。
- 失敗しても続け、必ず exit 0 で終わる（失敗するとセッションが始まらない）。段ごとの成否と時間を `/var/log/ssw-setup.log`（出力は `/var/log/ssw-setup.log.full`）に書く。SessionStart フックがこのログを読み、失敗した段を知らせる。
- 時間（2026-10-21 にこの VM で流した）: apt-update 7 秒・apt-install 3〜11 秒・uv 3 秒以内・Chromium 44 秒。合わせて約 1 分で、5 分に収まる（分けなくてよい）。
- **貼り直しの決まり**: リポジトリの `tools/cloud/setup.sh` が正本。版（uv・playwright・Python）を変えたら setup.sh も変え（`tests/test_cloud_tools.py` が Dockerfile・pyproject・`.python-version` と照らす）、PR の説明文に「設定画面に貼り直す」と書く。マージの後に人が貼り直す。setup script を変えると cache が作り直される（設定画面の setup script が変わったときだけ。リポジトリの setup.sh を変えただけでは変わらない）。

## 5. SessionStart フック（`.claude/settings.json` → `tools/cloud/session_start.sh`）

セッションを始めるたび（再開・compact の後も）に流れる。`CLAUDE_CODE_REMOTE=true` のときだけ動き、手元では何もしない。何度動いても同じ結果になる。失敗しても止めずに続け、exit 0 で終わる。

1. setup script のログに失敗した段があれば知らせる。ログが無ければ「setup script を貼っていない」と知らせる。
2. PostgreSQL・Redis を起こす（cache には動いている状態が残らない）。
3. 字体の置き換えの規則（`backend/config/fonts/99-form-fonts.conf`）を `/etc/fonts/conf.d/` に写す（違うときだけ。`fc-cache`）。
4. `.env` を作る・補う（既存の値は変えない）。接続先を `localhost` に、添付の保存先をリポジトリの `media/` にし、JSON の値を `'…'` で囲む。
5. DB の利用者（CREATEDB）と DB を、無いときだけ作る。
6. `uv sync --frozen --group e2e`・`migrate`・`load_form_templates`・`compilemessages -l ja`。
7. 道具がそろっているかを確かめる: soffice の版、字体の規則の写し、Chromium（`PLAYWRIGHT_BROWSERS_PATH` に、いまの playwright が使う版があるか）、uv の版（Dockerfile と同じか）、venv の Python（`.python-version` と同じ 3.12 か）。
8. `CLAUDE_ENV_FILE` に `RUNNER=host`・`PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`・`UV_SYSTEM_CERTS=1` と `.env` の読み込みを書く（以後の Bash で使われる）。
9. 知らせを出す。足りないもの・失敗を先頭に「【クラウドの環境: 足りないもの・失敗が N 件】」として並べ、その後に知らせておくこと（`.env` を写しから戻した・DB を作り直した）と、そろった版・かかった時間。人には systemMessage、Claude には additionalContext で届く。

時間: 準備が済んだ VM で約 8〜25 秒、`.env` も DB も無い VM で約 50 秒（migrate が 45 秒）。

### .env と暗号化の鍵

`.env` の鍵（`FIELD_ENCRYPTION_KEYS`・`BLIND_INDEX_KEY`）が変わると、DB に残っている暗号化した列（在留カード番号など。ダミー）が読めなくなる。

- フックは `.env` の写しを `/root/.config/ssw-support-system/env`（git の外。フォルダは 700、ファイルは 600）に置き、`.env` が消えたときは写しから戻す（鍵は前と同じ）。
- `.env` も写しも無いのに DB が残っているときは、新しい鍵になるので、開発用の DB を作り直し、そのことを知らせに出す（ダミーだけ。ダミーは `make seed` で作り直す）。テスト用の DB（`test_ssw*`）は流すたびに作るか空にするので、そのまま。
- 新しいセッションは cache から始まり、`.env` も DB も無い状態から作るので、ずれは起きない。

## 6. 検査（`RUNNER=host`）

`Makefile` は `RUNNER`（既定 `docker`。手元の動きは変わらない）で切り替える。`RUNNER=host` では、コマンドを `tools/cloud/host_run.sh`（`.env` を読み、`backend/` の uv の venv で流す）で流す。CLAUDE.md の「検査の段」はクラウドでもそのまま使う。

| make | `RUNNER=host` で流すもの | 2026-10-21 の結果 |
|---|---|---|
| `make lint` | `ruff check`・`ruff format --check`・`mypy`（e2e の依存も入った venv で） | 約 30 秒。e2e の型の誤りを直して通る |
| `make check-quick T=…`・`make test`・`make test-lf`・`make test-db` | `pytest -n auto --reuse-db -rs` | 全部で約 19 分（4 CPU）。1620 件が通り、`.doc` を変換し直すテスト 2 件は LibreOffice の版が違うので飛ばす（下の 6 の「手元でしか流せない検査」） |
| `make e2e` | `pytest e2e -n 4`（`TEST_DB_SUFFIX=e2e`。make のコマンド行の `GATE4=1` などのゲートの変数は外す） | 約 5 分 40 秒〜6 分 30 秒。131 件が通り、5 件を飛ばす（ゲートと design-shots） |
| `make gate2`・`make gate3`・`make gate4` | 同じテストを同じ環境変数で | 各 1 件が通る（約 2 分・2 分・2 分 20 秒） |
| `make checks` | `tools/run_checks.sh`。ログの先頭に `=== RUNNER=host`、検査ごとに飛ばしたテストの件数と理由（`SKIPPED [n] …`）。1 件の上限 `CHECK_LIMIT` は 1800 秒（既定の 900 秒では単体テストが時間切れになった） | 約 33 分（gate4 を含む。2026-10-21 は 32 分 36 秒） |
| `make messages`・`make seed` など | `$(MANAGE)` を backend の venv で | |
| `make design-shots` | 流さない（知らせて止める） | 字体と画面の見え方が手元の e2e のイメージと同じかを確かめていない |
| `make up`・`down`・`build`・`logs` | 流さない（知らせて止める） | PostgreSQL・Redis はフックが起こす。依存は `uv sync` |
| `make blank-pages` | 空の様式の頁数・字体・代わりの字体（下の 7） | 約 2 分半 |

### 手元でしか流せない検査

PR の説明文に「手元で流すこと」として、コマンドと期待する結果を書く。

| 検査 | 手元のコマンド | 理由 |
|---|---|---|
| `.doc` を変換し直すテスト（第1-4号・第1-25号） | `make test T="tests/forms_engine/test_form_contract.py::test_converting_1_4_again_gives_the_same_xml tests/forms_engine/test_form_application.py::test_converting_1_25_again_gives_the_same_xml"` | `converted.docx` は LibreOffice 7.4.7.2 で作った。クラウドは 24.2 で、理由つきで飛ばす |
| `converted.docx` を作り直す（`tools/forms/convert_doc.py`） | README の様式の道具の節 | 同上。クラウドで作り直さない |
| 帳票の PDF を人が見比べる（A の③） | `make blank-pages`・生成の画面 | クラウドは字体が違う（下の 7） |
| `make design-shots` | `make design-shots` | 上の表 |
| Docker のイメージ・compose（`make build`・e2e のイメージ・`make up` の qcluster と Mailpit） | `make build` など | クラウドでイメージを取れない（下の 8） |
| `.env.example` の `'…'` が compose で外れるか | 手元の `.env` の `DJANGO_STORAGE_OPTIONS` も `'…'` で囲んでから（`make env` は既存の値を変えない）、`docker compose run --rm --no-deps web python -c "import os; print(os.environ['DJANGO_STORAGE_OPTIONS'])"` | `{"location": "/app/media"}` と出れば外れている（引用符が残れば添付の保存先の設定が読めない） |

## 7. 帳票の字体（クラウドと手元の違い）

クラウドの VM には、手元の web のイメージに無い字体（Inter・WenQuanYi Zen Hei・Liberation・DejaVu など。VM の既定と Playwright の依存で入る）がある。置き換えの規則（`99-form-fonts.conf`）に無い字体名や、置き換え先に無い字は、fontconfig と LibreOffice が別の字体を選ぶので、クラウドでは手元と違う字体になる。頁数は、2026-10-21 の時点で単体テスト・gate3 の頁数の検査が通っている。

`make blank-pages`（`tools/forms/blank_pages.py`）で、入っている字体の一覧（`fc-list : family`）、空の様式ごとの頁数・埋め込みの字体と、代わりの字体（元の字体名・置き換え先・選ばれた字体・字の数・字の例・理由・当てはめ）を出す。手元（web のコンテナ）とクラウドで同じ形の結果になるので、見比べて対策を決める（対策は別の PR。2026-10-21 の案は PR #1 のコメント）。

- 代わりの字体は、文書の字の並びと PDF の字の並びを位置で突き合わせて、run ごとの指定と比べる（同じ字が日本語と中国語の run に出ても取り違えない）。位置で当たらない字（頁ごとのヘッダーなど）だけは字で推定し、当てはめの列が「字」になる。
- 理由: 規則が無い／置き換え先に字が無い（⑴・㊞ など）／欧文の字体で描かれた（`w:hint="eastAsia"` の □ ※ などを LibreOffice が欧文の字体で描く）／置き換え先で描かれない／文書の文字に無い。

対策の案は次の 3 つで、まだ決めていない。

- (a) LibreOffice の変換のときだけ、web のイメージに無い字体を使わせない（fontconfig の rejectfont など）
- (b) 足りない字体名（Century・Times New Roman・SimHei・DengXian・Microsoft YaHei など）の置き換えの規則を足す
- (c) (a) と (b) の両方

## 8. Docker を使う案（使えない）

2026-10-21 に確かめた。`dockerd &` で daemon は約 2 秒で起きるが、`docker compose build web` は 2 秒で失敗する。

- `ghcr.io/astral-sh/uv:0.13.1`（Dockerfile の `COPY --from`）の中身の取得先 `pkg-containers.githubusercontent.com` が 403。
- Docker Hub（`python:3.12-slim-bookworm`・`postgres:16`・`redis:7`・`axllent/mailpit`）は 429（取得の回数の上限）。
- `mcr.microsoft.com` の Playwright のイメージだけは取れる（1 分 16 秒）。ほかが取れないので使わない。

ghcr の中身の取得先は接続先の許可で通せるかもしれないが、Docker Hub の 429（取得の回数の上限）は許可では直らない。接続先の許可は変えず、Docker を使わない形（`RUNNER=host`）にした。
