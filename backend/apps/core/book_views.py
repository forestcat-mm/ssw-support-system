"""機関全体の帳簿（① 支援実施体制・② 支援の委託契約・月ごとの集計。T57、ADR-0044）。"""

from __future__ import annotations

import datetime
import io
from typing import Any

from django import forms
from django.http import FileResponse, HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import Action, AuditAction, Organization, User
from apps.core.settings_views import SettingsView
from apps.core.web_forms import StyledFormMixin
from services import audit, book_exports


class BookPeriodForm(StyledFormMixin, forms.Form):
    starts_on = forms.DateField(label=_("period from"), widget=forms.DateInput)
    ends_on = forms.DateField(label=_("period to"), widget=forms.DateInput)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.style_fields()

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        starts_on, ends_on = data.get("starts_on"), data.get("ends_on")
        if starts_on and ends_on and starts_on > ends_on:
            self.add_error("ends_on", _("The end of the period must be on or after the start."))
        if starts_on and ends_on and (ends_on - starts_on).days > 366 * 5:
            self.add_error("ends_on", _("Choose a period of five years or less."))
        return data


class OrganizationBooksView(SettingsView):
    """機関全体の帳簿の xlsx（自作。監査ログに EXPORT。T57-D3）。"""

    required_action = Action.ORGANIZATION_MANAGE
    tab = "books"
    template_name = "settings/books.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        today = timezone.localdate()
        start = datetime.date(today.year - 1 if today.month < 4 else today.year, 4, 1)
        form = BookPeriodForm(initial={"starts_on": start, "ends_on": today})
        return self.render(request, form=form)

    def post(self, request: HttpRequest) -> HttpResponse | FileResponse:
        form = BookPeriodForm(request.POST)
        if not form.is_valid():
            return self.render(request, form=form)
        starts_on, ends_on = form.cleaned_data["starts_on"], form.cleaned_data["ends_on"]
        data = book_exports.organization_book_xlsx(starts_on, ends_on)
        organization = Organization.objects.first()
        if organization is not None:
            audit.record(
                AuditAction.EXPORT,
                target=organization,
                actor=request.user if isinstance(request.user, User) else None,
                context={
                    "books": "organization",
                    "from": starts_on.isoformat(),
                    "to": ends_on.isoformat(),
                },
            )
        name = f"帳簿_機関_{starts_on:%Y%m%d}-{ends_on:%Y%m%d}.xlsx"
        return FileResponse(io.BytesIO(data), as_attachment=True, filename=name)
