from django.apps import AppConfig


class TestAppConfig(AppConfig):
    """テスト専用（config.settings_test でだけ有効）。

    T07 で実モデルができるまで、暗号化のフィールド型をここで試す。
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "tests.testapp"
    label = "testapp"
