"""監査ログを DB でも追記専用にする（UPDATE・DELETE を拒否するトリガー）。

TRUNCATE は止めない（テストの後片付けで使う）。本番はアプリの DB ユーザーから
UPDATE / DELETE / TRUNCATE 権限も外す（tasks-phase1.md 本番準備）。
保持期間満了後の削除は、DB 所有者がトリガーを一時的に外して行う（同上・将来運用）。
"""

from django.db import migrations

CREATE = """
CREATE OR REPLACE FUNCTION core_auditlog_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'core_auditlog is append-only: % is not allowed', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER core_auditlog_append_only
    BEFORE UPDATE OR DELETE ON core_auditlog
    FOR EACH ROW EXECUTE FUNCTION core_auditlog_append_only();
"""

DROP = """
DROP TRIGGER IF EXISTS core_auditlog_append_only ON core_auditlog;
DROP FUNCTION IF EXISTS core_auditlog_append_only();
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0008_auditlog")]

    operations = [migrations.RunSQL(CREATE, DROP)]
