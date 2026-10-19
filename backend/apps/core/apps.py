from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"
    verbose_name = _("Organization and users")

    def ready(self) -> None:
        from apps.core import checks  # noqa: F401  システムチェックの登録
        from services import audit

        audit.connect_signals()

        from django.contrib.auth.signals import user_logged_in

        from services import sessions

        def start_session(sender: object, request: object, **kwargs: object) -> None:
            session = getattr(request, "session", None)
            if session is not None:
                sessions.started(session)  # ログインからの上限を数え始める（J8）

        user_logged_in.connect(start_session, dispatch_uid="core.start_session")
