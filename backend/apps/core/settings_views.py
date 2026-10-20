"""管理者向けの「設定」画面（T15b）。ユーザー・担当者・自機関・事務所・監査ログ。

業務の処理は services（T04 のユーザーの招待・無効化・MFA リセット、T03 の添付の検査など）を
呼ぶだけにする。
削除はどこにもない。保存はすべて監査ログに残る（作成・更新は保存のシグナル、招待などはイベント）。
"""

from dataclasses import dataclass
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Model
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import Promise
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views import View

from apps.core.models import (
    Action,
    AuditLog,
    Office,
    OfficeManagerPeriod,
    Organization,
    OrganizationOfficer,
    Staff,
    User,
)
from apps.core.permissions import ActionRequiredMixin
from apps.core.settings_forms import (
    AuditSearchForm,
    CertificateForm,
    InviteForm,
    OfficeForm,
    OrganizationForm,
    OrganizationOfficerForm,
    StaffForm,
)
from apps.core.views import invitation_url_builder
from apps.core.web_forms import (
    PeriodVoidForm,
    StaffPeriodForm,
    consume_once_token,
    once_context,
)
from apps.support.display import plan_view
from apps.support.forms import PlanEditor
from services import (
    audit_search,
    authorization,
    host_application,
    invitations,
    mfa,
    organization,
    staff_periods,
    support_plans,
    users,
)


@dataclass(frozen=True)
class SettingsTab:
    key: str
    label: str | Promise
    url_name: str
    action: Action


SETTINGS_TABS = [
    SettingsTab("users", gettext_lazy("Users"), "settings:users", Action.USERS_MANAGE),
    SettingsTab(
        "staff", gettext_lazy("Staff members"), "settings:staff", Action.ORGANIZATION_MANAGE
    ),
    SettingsTab(
        "organization",
        gettext_lazy("Own organization"),
        "settings:organization",
        Action.ORGANIZATION_MANAGE,
    ),
    SettingsTab("offices", gettext_lazy("Offices"), "settings:offices", Action.ORGANIZATION_MANAGE),
    SettingsTab(
        "support_plan",
        gettext_lazy("Standard support plan"),
        "settings:support_plan",
        Action.ORGANIZATION_MANAGE,
    ),
    SettingsTab("forms", gettext_lazy("Form master"), "settings:forms", Action.MASTERS_MANAGE),
    SettingsTab("books", gettext_lazy("Books"), "settings:books", Action.ORGANIZATION_MANAGE),
    SettingsTab("audit", gettext_lazy("Audit logs"), "settings:audit", Action.AUDIT_VIEW),
]


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)
    return request.user


class SettingsView(ActionRequiredMixin, View):
    """設定画面の共通部分（上部のタブ・二重送信の防止・表示）。"""

    tab = ""
    template_name = ""

    def render(self, request: HttpRequest, **context: Any) -> HttpResponse:
        tabs = [
            {
                "label": tab.label,
                "url": reverse(tab.url_name),
                "active": tab.key == self.tab,
            }
            for tab in SETTINGS_TABS
            if authorization.has_action(request.user, tab.action)
        ]
        return TemplateResponse(
            request,
            self.template_name,
            {"settings_tabs": tabs, **once_context(request), **context},
        )

    def already_submitted(self, request: HttpRequest, redirect_to: str) -> HttpResponse:
        messages.info(request, _("This form has already been submitted."))
        return redirect(redirect_to)


class SettingsIndexView(ActionRequiredMixin, View):
    """ナビの「設定」。使えるタブのうち先頭へ。"""

    required_action = Action.USERS_MANAGE

    def get(self, request: HttpRequest) -> HttpResponse:
        for tab in SETTINGS_TABS:
            if authorization.has_action(request.user, tab.action):
                return redirect(tab.url_name)
        raise PermissionDenied


# --- ユーザー -----------------------------------------------------------------------


INVITATION_LABELS = {
    invitations.InvitationStatus.NONE: "—",
    invitations.InvitationStatus.PENDING: gettext_lazy("pending"),
    invitations.InvitationStatus.EXPIRED: gettext_lazy("expired"),
    invitations.InvitationStatus.ACCEPTED: gettext_lazy("accepted"),
}


@dataclass(frozen=True)
class UserRow:
    user: User
    roles: list[str]
    has_mfa: bool
    locked: bool
    invitation: str
    invitation_label: str
    staff: Staff | None


def user_row(user: User, locked: set[str]) -> UserRow:
    status = invitations.invitation_status(user)
    return UserRow(
        user=user,
        roles=[str(user_role.role) for user_role in user.user_roles.all()],
        has_mfa=mfa.has_confirmed_totp(user),
        locked=user.get_username() in locked,
        invitation=str(status),
        invitation_label=str(INVITATION_LABELS[status]),
        staff=getattr(user, "staff", None),  # 紐づく担当者が無ければ None
    )


class UserListView(SettingsView):
    tab = "users"
    template_name = "settings/users.html"
    required_action = Action.USERS_MANAGE

    def get(self, request: HttpRequest) -> HttpResponse:
        locked = users.locked_usernames()
        rows = [
            user_row(user, locked)
            for user in User.objects.prefetch_related("user_roles__role").order_by(
                "-is_active", "username"
            )
        ]
        return self.render(request, rows=rows)


class UserInviteView(SettingsView):
    tab = "users"
    template_name = "settings/form.html"
    required_action = Action.USERS_MANAGE
    title = gettext_lazy("Invite a user")

    def get(self, request: HttpRequest) -> HttpResponse:
        return self.render(
            request, form=InviteForm(), title=self.title, back=reverse("settings:users")
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        if not consume_once_token(request):
            return self.already_submitted(request, reverse("settings:users"))
        form = InviteForm(request.POST)
        if not form.is_valid():
            return self.render(request, form=form, title=self.title, back=reverse("settings:users"))
        user = form.save(commit=False)
        invitations.invite_user(
            user,
            roles=form.cleaned_data["roles"],
            staff=form.cleaned_data.get("staff"),
            by=_actor(request),
            build_url=invitation_url_builder(request),
        )
        messages.success(
            request, _("Sent an invitation email to %(email)s.") % {"email": user.email}
        )
        return redirect("settings:user", user.pk)


class UserDetailView(SettingsView):
    tab = "users"
    template_name = "settings/user.html"
    required_action = Action.USERS_MANAGE

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        user = get_object_or_404(User.objects.prefetch_related("user_roles__role"), pk=pk)
        return self.render(request, row=user_row(user, users.locked_usernames()))


USER_ACTIONS: dict[str, dict[str, Any]] = {
    "deactivate": {
        "title": gettext_lazy("Deactivate the user"),
        "description": gettext_lazy(
            "The user can no longer sign in and is signed out immediately. The staff member "
            "linked to this user is not changed; turn it off in Staff members if they left."
        ),
        "button": gettext_lazy("Deactivate"),
        "danger": True,
    },
    "reactivate": {
        "title": gettext_lazy("Reactivate the user"),
        "description": gettext_lazy("The user can sign in again with the same password and MFA."),
        "button": gettext_lazy("Reactivate"),
        "danger": False,
    },
    "reset-mfa": {
        "title": gettext_lazy("Reset two-factor authentication"),
        "description": gettext_lazy(
            "Use this when the user lost their phone and recovery codes. The authenticator and "
            "recovery codes are deleted, and the user sets them up again at the next sign-in."
        ),
        "button": gettext_lazy("Reset"),
        "danger": True,
    },
    "unlock": {
        "title": gettext_lazy("Unlock sign-in"),
        "description": gettext_lazy(
            "Clears the failed sign-in attempts so the user can try again right away."
        ),
        "button": gettext_lazy("Unlock"),
        "danger": False,
    },
    "resend": {
        "title": gettext_lazy("Resend the invitation"),
        "description": gettext_lazy(
            "Sends a new invitation email. The link in the previous email no longer works."
        ),
        "button": gettext_lazy("Resend"),
        "danger": False,
    },
}


class UserActionView(SettingsView):
    """ユーザーの操作（確認画面をはさむ）。T04 のサービスをそのまま呼ぶ。"""

    tab = "users"
    template_name = "settings/confirm.html"
    required_action = Action.USERS_MANAGE

    def target(self, pk: int, action: str) -> User:
        if action not in USER_ACTIONS:
            raise Http404
        return get_object_or_404(User, pk=pk)

    def get(self, request: HttpRequest, pk: int, action: str) -> HttpResponse:
        user = self.target(pk, action)
        return self.render(
            request,
            subject=user,
            action=USER_ACTIONS[action],
            back=reverse("settings:user", args=[pk]),
        )

    def post(self, request: HttpRequest, pk: int, action: str) -> HttpResponse:
        user = self.target(pk, action)
        detail = reverse("settings:user", args=[pk])
        if not consume_once_token(request):
            return self.already_submitted(request, detail)
        actor = _actor(request)
        if action == "deactivate":
            try:
                users.deactivate_user(user, by=actor)
            except users.LastAdminError:
                messages.error(request, _("The last active administrator cannot be deactivated."))
                return redirect(detail)
        elif action == "reactivate":
            users.reactivate_user(user, by=actor)
        elif action == "reset-mfa":
            mfa.reset_mfa(user, by=actor, via="settings")
        elif action == "unlock":
            users.unlock_user(user, by=actor)
        elif action == "resend":
            invitations.resend_invitation(user, by=actor, build_url=invitation_url_builder(request))
        messages.success(request, _("Done: %(action)s") % {"action": USER_ACTIONS[action]["title"]})
        return redirect(detail)


# --- 担当者・自機関・事務所 -------------------------------------------------------------


class RecordFormView(SettingsView):
    """担当者・事務所の追加・編集の共通部分。"""

    template_name = "settings/form.html"
    required_action = Action.ORGANIZATION_MANAGE
    model: type[Model]
    form_class: Any
    list_url_name = ""
    create_title: str | Promise = ""
    edit_title: str | Promise = ""

    def instance(self, pk: int | None) -> Model | None:
        return get_object_or_404(self.model, pk=pk) if pk is not None else None

    def extra_context(self, instance: Model | None) -> dict[str, Any]:
        """フォームの下に出すもの（事務所の支援責任者の履歴など）。"""
        return {}

    def get(self, request: HttpRequest, pk: int | None = None) -> HttpResponse:
        instance = self.instance(pk)
        return self.render(
            request,
            form=self.form_class(instance=instance),
            title=self.edit_title if instance else self.create_title,
            back=reverse(self.list_url_name),
            **self.extra_context(instance),
        )

    def post(self, request: HttpRequest, pk: int | None = None) -> HttpResponse:
        instance = self.instance(pk)
        if not consume_once_token(request):
            return self.already_submitted(request, reverse(self.list_url_name))
        form = self.form_class(request.POST, instance=instance)
        if not form.is_valid():
            return self.render(
                request,
                form=form,
                title=self.edit_title if instance else self.create_title,
                back=reverse(self.list_url_name),
                **self.extra_context(instance),
            )
        record = form.save(commit=False)
        organization.save_record(record, by=_actor(request))
        form.save_m2m()
        messages.success(request, _("Saved."))
        return redirect(self.list_url_name)


class StaffListView(SettingsView):
    tab = "staff"
    template_name = "settings/staff.html"
    required_action = Action.ORGANIZATION_MANAGE

    def get(self, request: HttpRequest) -> HttpResponse:
        staff = Staff.objects.select_related("office", "user").prefetch_related("languages")
        return self.render(request, staff=staff.order_by("-is_active", "name_kana", "name"))


class StaffFormView(RecordFormView):
    tab = "staff"
    model = Staff
    form_class = StaffForm
    list_url_name = "settings:staff"
    create_title = gettext_lazy("Add a staff member")
    edit_title = gettext_lazy("Edit the staff member")


class OfficeListView(SettingsView):
    tab = "offices"
    template_name = "settings/offices.html"
    required_action = Action.ORGANIZATION_MANAGE

    def get(self, request: HttpRequest) -> HttpResponse:
        return self.render(request, offices=Office.objects.order_by("-is_active", "pk"))


class OfficeFormView(RecordFormView):
    tab = "offices"
    model = Office
    form_class = OfficeForm
    list_url_name = "settings:offices"
    create_title = gettext_lazy("Add an office")
    edit_title = gettext_lazy("Edit the office")

    def extra_context(self, instance: Model | None) -> dict[str, Any]:
        if not isinstance(instance, Office):
            return {}
        return {
            "after_form": "settings/office_managers.html",
            "office": instance,
            "manager_periods": staff_periods.office_periods(instance),
        }


class OfficeManagerView(SettingsView):
    """事務所の支援責任者の変更・履歴の行の取消（T45-D5）の共通部分。"""

    tab = "offices"
    required_action = Action.ORGANIZATION_MANAGE

    def office(self, pk: int) -> Office:
        return get_object_or_404(Office, pk=pk)

    def back(self, office: Office) -> str:
        return reverse("settings:office_edit", args=[office.pk]) + "#manager-periods"


class OfficeManagerChangeView(OfficeManagerView):
    """支援責任者を変える（新しい支援責任者・開始日・理由）。"""

    template_name = "settings/office_manager_change.html"

    def form(self, office: Office, data: Any = None) -> StaffPeriodForm:
        return StaffPeriodForm(
            data,
            queryset=organization.manager_choices(office),
            label=gettext_lazy("new support manager"),
            empty_label=gettext_lazy("not chosen"),
            help_text=gettext_lazy(
                "Printed as the support manager in form 1-17 (section III, item 7) for workers "
                "whose staff member belongs to this office. Staff members of this office marked "
                "as support managers can be chosen."
            ),
        )

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        office = self.office(pk)
        return self.render(request, office=office, form=self.form(office), back=self.back(office))

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        office = self.office(pk)
        if not consume_once_token(request):
            return self.already_submitted(request, self.back(office))
        form = self.form(office, request.POST)
        if form.is_valid():
            try:
                staff_periods.change_office_manager(
                    office,
                    form.cleaned_data["staff"],
                    starts_on=form.cleaned_data["starts_on"],
                    reason=form.cleaned_data["reason"],
                    by=_actor(request),
                    today=timezone.localdate(),
                )
            except ValidationError as error:
                for name, field_messages in error.message_dict.items():
                    for message in field_messages:
                        form.add_error(name if name in form.fields else None, message)
            else:
                messages.success(request, _("Changed the support manager."))
                return redirect(self.back(office))
        return self.render(request, office=office, form=form, back=self.back(office))


class OfficeManagerVoidView(OfficeManagerView):
    """支援責任者の履歴の行の取消（理由は必須）。"""

    template_name = "settings/office_manager_void.html"

    def period(self, office: Office, period_id: int) -> OfficeManagerPeriod:
        return get_object_or_404(
            OfficeManagerPeriod.objects.select_related("staff"),
            pk=period_id,
            office=office,
            voided_at__isnull=True,
        )

    def get(self, request: HttpRequest, pk: int, period_id: int) -> HttpResponse:
        office = self.office(pk)
        period = self.period(office, period_id)
        return self.render(
            request, office=office, period=period, form=PeriodVoidForm(), back=self.back(office)
        )

    def post(self, request: HttpRequest, pk: int, period_id: int) -> HttpResponse:
        office = self.office(pk)
        if not consume_once_token(request):
            return self.already_submitted(request, self.back(office))
        period = self.period(office, period_id)
        form = PeriodVoidForm(request.POST)
        if form.is_valid():
            staff_periods.void_office_period(
                period, by=_actor(request), reason=form.cleaned_data["reason"]
            )
            messages.success(request, _("Voided the record."))
            return redirect(self.back(office))
        return self.render(request, office=office, period=period, form=form, back=self.back(office))


class OrganizationView(SettingsView):
    tab = "organization"
    template_name = "settings/organization.html"
    required_action = Action.ORGANIZATION_MANAGE

    def get(self, request: HttpRequest) -> HttpResponse:
        org = Organization.objects.first()
        certificates = organization.council_certificates(org) if org else []
        current = next((c for c in certificates if c.voided_at is None), None)
        officers = list(org.officers.all()) if org else []
        return self.render(
            request,
            organization=org,
            certificates=certificates,
            current_certificate=current,
            officers=officers,
        )


class OrganizationEditView(RecordFormView):
    tab = "organization"
    model = Organization
    form_class = OrganizationForm
    list_url_name = "settings:organization"
    create_title = gettext_lazy("Register the own organization")
    edit_title = gettext_lazy("Edit the own organization")

    def instance(self, pk: int | None) -> Model | None:
        return Organization.objects.first()  # 1 行だけ（無ければ登録）


class OfficerEditView(SettingsView):
    """自機関の役員の追加（pk なし）・編集（T46b。帳簿①）。"""

    tab = "organization"
    template_name = "settings/form.html"
    required_action = Action.ORGANIZATION_MANAGE

    def target(self, pk: int | None) -> tuple[Organization, OrganizationOfficer | None]:
        org = Organization.objects.first()
        if org is None:
            raise Http404
        if pk is None:
            return org, None
        return org, get_object_or_404(
            OrganizationOfficer, pk=pk, organization=org, voided_at__isnull=True
        )

    def page(self, request: HttpRequest, officer: Any, form: Any) -> HttpResponse:
        return self.render(
            request,
            form=form,
            title=_("Edit the officer") if officer else _("Add an officer"),
            back=reverse("settings:organization") + "#officers",
        )

    def get(self, request: HttpRequest, pk: int | None = None) -> HttpResponse:
        _org, officer = self.target(pk)
        return self.page(request, officer, OrganizationOfficerForm(instance=officer))

    def post(self, request: HttpRequest, pk: int | None = None) -> HttpResponse:
        org, officer = self.target(pk)
        back = reverse("settings:organization") + "#officers"
        if not consume_once_token(request):
            return self.already_submitted(request, back)
        form = OrganizationOfficerForm(request.POST, instance=officer)
        if not form.is_valid():
            return self.page(request, officer, form)
        record = form.save(commit=False)
        record.organization = org
        host_application.save_row(record, by=_actor(request))
        messages.success(request, _("Saved."))
        return redirect(back)


class OfficerVoidView(SettingsView):
    """自機関の役員の行の取消（理由は必須）。"""

    tab = "organization"
    template_name = "settings/form.html"
    required_action = Action.ORGANIZATION_MANAGE

    def target(self, pk: int) -> OrganizationOfficer:
        return get_object_or_404(OrganizationOfficer, pk=pk, voided_at__isnull=True)

    def page(self, request: HttpRequest, officer: OrganizationOfficer, form: Any) -> HttpResponse:
        return self.render(
            request,
            form=form,
            title=_("Void the officer: %(name)s") % {"name": officer.name},
            back=reverse("settings:organization") + "#officers",
        )

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        return self.page(request, self.target(pk), PeriodVoidForm())

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        officer = self.target(pk)
        back = reverse("settings:organization") + "#officers"
        if not consume_once_token(request):
            return self.already_submitted(request, back)
        form = PeriodVoidForm(request.POST)
        if not form.is_valid():
            return self.page(request, officer, form)
        host_application.void_row(officer, by=_actor(request), reason=form.cleaned_data["reason"])
        messages.success(request, _("Voided the record."))
        return redirect(back)


class CertificateView(SettingsView):
    """食品産業特定技能協議会の構成員証明書の差し替え（古い証明書は残る）。"""

    tab = "organization"
    template_name = "settings/form.html"
    required_action = Action.ORGANIZATION_MANAGE
    title = gettext_lazy("Replace the membership certificate")

    def org(self) -> Organization:
        org = Organization.objects.first()
        if org is None:
            raise Http404
        return org

    def get(self, request: HttpRequest) -> HttpResponse:
        self.org()
        return self.render(
            request,
            form=CertificateForm(),
            title=self.title,
            back=reverse("settings:organization"),
            multipart=True,
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        org = self.org()
        if not consume_once_token(request):
            return self.already_submitted(request, reverse("settings:organization"))
        form = CertificateForm(request.POST, request.FILES)
        if not form.is_valid():
            return self.render(
                request,
                form=form,
                title=self.title,
                back=reverse("settings:organization"),
                multipart=True,
            )
        organization.replace_council_certificate(
            org,
            file=form.cleaned_data["file"],
            valid_until=form.cleaned_data.get("valid_until"),
            by=_actor(request),
        )
        messages.success(request, _("Replaced the membership certificate."))
        return redirect("settings:organization")


# --- 監査ログ -----------------------------------------------------------------------


class AuditListView(SettingsView):
    """監査ログの検索（読み取り専用）。検索 1 回で「監査ログの閲覧」を 1 件残す。"""

    tab = "audit"
    template_name = "settings/audit.html"
    required_action = Action.AUDIT_VIEW
    per_page = 50

    def get(self, request: HttpRequest) -> HttpResponse:
        form = AuditSearchForm(request.GET)
        query = form.to_query()
        paginator = Paginator(audit_search.search(query), self.per_page)
        page = paginator.get_page(request.GET.get("page"))
        audit_search.record_audit_view(
            {**form.cleaned_data, "page": page.number}, count=paginator.count
        )
        params = request.GET.copy()
        params.pop("page", None)
        return self.render(request, form=form, page=page, query_string=params.urlencode())


class AuditDetailView(SettingsView):
    tab = "audit"
    template_name = "settings/audit_entry.html"
    required_action = Action.AUDIT_VIEW

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        entry = get_object_or_404(AuditLog.objects.select_related("content_type"), pk=pk)
        audit_search.record_audit_view({"id": pk}, count=1)
        changes = [(name, values[0], values[1]) for name, values in sorted(entry.changes.items())]
        return self.render(request, entry=entry, changes=changes, back=reverse("settings:audit"))


# --- 標準の支援計画（T17） ---------------------------------------------------------------


class StandardPlanView(SettingsView):
    """自機関の標準の支援計画（外国人ごとの計画を作るときの複製の元）と、その版の履歴。"""

    tab = "support_plan"
    template_name = "settings/support_plan.html"
    required_action = Action.ORGANIZATION_MANAGE

    def get(self, request: HttpRequest) -> HttpResponse:
        plan = support_plans.template_plan(by=_actor(request))
        versions = list(plan.versions.select_related("created_by").order_by("-version"))
        number = request.GET.get("version", "")
        version = next((v for v in versions if str(v.version) == number), versions[0])
        previous = next((v for v in versions if v.version < version.version), None)
        content = support_plans.content_of(version)
        return self.render(
            request,
            versions=versions,
            version=version,
            previous=previous,
            view=plan_view(content, support_plans.warnings(content, require_staff=False)),
            diff=support_plans.diff_rows(previous, version) if previous else [],
        )


class StandardPlanEditView(SettingsView):
    """標準の支援計画の編集。保存すると変更理由つきの新しい版になる。"""

    tab = "support_plan"
    template_name = "settings/support_plan_edit.html"
    required_action = Action.ORGANIZATION_MANAGE

    def editor(self, request: HttpRequest, data: Any = None) -> PlanEditor:
        return PlanEditor(data, initial=support_plans.template_content(), with_applies_from=False)

    def page(self, request: HttpRequest, editor: PlanEditor) -> HttpResponse:
        return self.render(
            request,
            editor=editor,
            sections=editor.sections(editor.warnings(require_staff=False)),
            back=reverse("settings:support_plan"),
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        support_plans.template_plan(by=_actor(request))
        return self.page(request, self.editor(request))

    def post(self, request: HttpRequest) -> HttpResponse:
        if not consume_once_token(request):
            return self.already_submitted(request, reverse("settings:support_plan"))
        editor = self.editor(request, request.POST)
        if editor.is_valid():
            try:
                support_plans.update_template(
                    editor.to_content(),
                    change_reason=editor.version_form.cleaned_data["change_reason"],
                    by=_actor(request),
                )
            except ValidationError as error:
                editor.apply_errors(error)
            else:
                messages.success(request, _("Saved the standard support plan as a new version."))
                return redirect("settings:support_plan")
        return self.page(request, editor)
