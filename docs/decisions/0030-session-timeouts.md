# ADR-0030: 自動ログアウトは無操作 60 分とログインから 12 時間とし、5 分前に知らせて無操作の分だけ延長できる

- 状態: 採用
- 日付: 2026-10-19
- 関連: 仕様書 第8章（セッションは無操作で失効）、修正 1 回目 J8（計画 `.checks/plan-fix1.md`。J8-D1 は (a)）

## 背景

これまでの設定は次のとおりだった（`config/settings.py`）。
- 操作が無ければ 30 分で失効する（環境変数 `SESSION_IDLE_TIMEOUT_MINUTES`。既定 30）。リクエストのたびに延びる（`SESSION_SAVE_EVERY_REQUEST`）。ブラウザを閉じても失効する。
- ログインからの上限は無かった。
- 失効の前の知らせも無かった。長い入力の画面だけは、入力中に 5 分ごとに延長の通信を送っていた。

入力の途中で知らせなく失効すると、書きかけの記録が消える。一方で、上限が無いと、開いたままの端末のセッションがいつまでも使える。

## 出典（2026-10-19 取得）

| 資料 | 版 | URL | SHA-256 |
|---|---|---|---|
| NIST SP 800-63B-4 Digital Identity Guidelines: Authentication and Authenticator Management | July 2025（Last-Modified 2025-08-01） | https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63B-4.pdf | `d9ab89576b5ae2cfb3ba37781110788dea27a85a5079496dbfc1a79834670afd` |

2.2.3 Reauthentication（印刷7頁）: 「A definite reauthentication overall timeout SHALL be established, which SHOULD be no more than 24 hours at AAL2. The inactivity timeout SHOULD be no more than 1 hour.」

このシステムのログインは、パスワードと TOTP の 2 要素（AAL2 に当たる）。

## 決定

1. **無操作の失効**: 既定を 60 分にする（環境変数 `SESSION_IDLE_TIMEOUT_MINUTES`。運用で短くできる）。今より長くなる。リクエストのたびに延びるのは今のとおり。
2. **ログインからの上限**: 12 時間にする（新しい環境変数 `SESSION_ABSOLUTE_TIMEOUT_HOURS`。既定 12）。
   - ログインの日時をセッションに持つ（`login_at`。ログインのシグナルで入れる）。持っていない古いセッションでは、初めて見たときを日時にする。
   - ミドルウェア（`SessionLimitMiddleware`）で確かめる。過ぎていたらログアウトし、ログインの画面に移して「ログインから 12 時間たったので、ログアウトしました」と出す。
   - 延長の通信とモーダルの中の送信には、401 を返す。上限は延長できない。
3. **知らせ**: 失効の 5 分前（`SESSION_WARNING_SECONDS`）に、画面の下に知らせを出す（forms.js の `initSessionNotice`）。
   - 残りは「最後にサーバーに届いた時から無操作の失効まで」と「ログインからの上限まで」の早いほうで数える。サーバーに届いたことは、画面の読み込み・延長の通信・モーダルの読み込みで数える。
   - 無操作の失効が先なら「延長する」を出し、押すと延長の通信（`accounts:keepalive`）を送る。
   - 上限が先なら、延長できないこと（保存すること）を知らせ、ボタンは出さない。
   - 時間が来たら「ログアウトしました」に変える。画面は移さない（入力の途中の文字を写せるように）。次の操作でログインの画面に移る。
4. **延長の通信の応答**: 失効した後は 401 を返す。これまではログインの画面へのリダイレクトだった。fetch はリダイレクトをたどってログインの画面を受け取るので、延長に成功したと取り違えるため。
5. `[hidden]` の属性を、部品のクラスより強く効かせる（`base.css`）。延長のボタンなど、JS で出し入れする部品を確実に隠すため。

## 影響

- 手元の `.env` に `SESSION_IDLE_TIMEOUT_MINUTES=30` があれば、その値のまま（`make env` は既存の値を変えない）。60 分にするには `.env` を直す。`.env.example` と README は 60 分・12 時間にした。
- e2e は、settings で時間を短くして、知らせと延長と失効を確かめる（`e2e/test_session_timeout.py`）。

## 未確認の点

- 実データの運用で、12 時間が勤務の形（夜勤・長い面談の日）に合うか。合わなければ環境変数で変える（24 時間以下）。
