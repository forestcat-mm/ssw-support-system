# ADR-0005: 日次の処理は Django-Q2 の実行予定で動かす

- 状態: 採用
- 日付: 2026-10-16
- 関連: CLAUDE.md（ジョブ: Django-Q〔Redis〕）、仕様書 第9章「場所に依存しないコードの5原則」、第10章（ジョブ実行）、T11

## 背景

T11 で、日付の到来で届出タスクを起票する処理（在留期限の 3 か月前、登録更新の受付開始日、各有効期限の着手日、定期届出の 4/1）を毎日動かす必要がある。
CLAUDE.md はジョブ実行に Django-Q（Redis）を挙げており、段階3の帳票生成（Word→PDF）でも使う予定である。
元の django-q は保守が止まっているため、後継として保守されている django-q2 を使う（T01 の計画時に合意済み）。

## 決定

- django-q2（1.11.1）と redis（8.1.0）を導入し、Redis をブローカーにする。
- docker-compose に `qcluster` サービス（`python manage.py qcluster`）を足す。手元と本番で同じ compose を使う。
- 日次の起票は、データマイグレーションで登録する実行予定 `daily_filings`（毎日、初回は次の 6:00 Asia/Tokyo）が `apps.filings.tasks.create_scheduled_filings` を呼ぶ。
- 中身は管理コマンド `create_scheduled_filings`（`make filings`）と同じ `services.filings.create_scheduled()` にする。手でも実行できる。
- 処理は冪等（同じ種別×対象×起算日は 1 件）にする。止まっていた日の分は `catch_up=False` でまとめて実行せず、次に動いたときに追いつく。
- 実行予定・成功・失敗の admin は、管理者だけ（操作 `system.manage`）が見られる。

## 影響

- `qcluster` が止まっていると日次の起票が動かない。ログ・監視（第8章）で qcluster の稼働を見る（本番準備）。
- 段階3の帳票生成も同じ仕組み（`async_task`）で動かす。
- 実行予定の時刻を変えるときは、admin の「Django Q > 実行予定」で次回の実行日時を直す。

## 検討した代替案

- **サーバーの cron から管理コマンドを呼ぶ**: サーバーごとに crontab の設定が要り、手元と本番で動かし方が変わる（第9章の「同じ compose で動かす」に合わない）。実行結果も画面で見られない。
- **cron 用のコンテナ（supercronic など）を compose に足す**: 日次の起票には足りるが、段階3で別にジョブ基盤が要り、仕組みが二重になる。
- **django-q（保守停止）**: 依存の更新や Django の新しい版への追随が見込めない。
