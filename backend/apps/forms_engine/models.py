"""帳票の基盤（仕様書 第6章「様式のバージョン管理」「生成履歴」、第7章 form_templates /
form_mappings / generated_documents。T29、ADR-0018）。

- 様式マスタ（FormTemplate）とマッピング定義（FormMapping）は、リポジトリのファイル
  （templates/forms/・mappings/）から読み込む（load_form_templates）。旧版は消さない（制約5）。
- 生成履歴（GeneratedDocument）は上書きしない。依頼 → 完了／失敗の状態の変更だけを許す。
  データのスナップショットは暗号化して持つ（在留カード番号などを含むため。制約2）。
"""

from __future__ import annotations

from typing import Any

from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from apps.core.fields import EncryptedCharField
from apps.core.models import Attachment, PrivateFileField, TrackedModel
from services.files import upload_path


class ImmutableRowError(Exception):
    """生成履歴は完了・失敗の後に変更・削除できない。"""


class FormOwner(models.TextChoices):
    COMMON = "common", _("common")
    HOST = "host", _("host organization")


class FormMethod(models.TextChoices):
    XLSX_CELLS = "xlsx-cells", _("Excel: write values into cells")
    DOCX_TAGS = "docx-tags", _("Word: fill in tags")
    # 入管のマクロつきの一括登録用ツール（1 人 1 行。T47、ADR-0039）
    XLSM_ROWS = "xlsm-rows", _("Excel tool with macros: one row per person")


class FormTemplate(TrackedModel):
    """様式マスタの 1 行＝様式 ID × 適用開始日 × 言語 × 所有者。"""

    form_id = models.CharField(_("form ID"), max_length=32)
    name = models.CharField(_("name"), max_length=200)
    valid_from = models.DateField(_("valid from"))
    language = models.CharField(_("language"), max_length=10, default="ja")
    owner = models.CharField(
        _("owner"), max_length=16, choices=FormOwner.choices, default=FormOwner.COMMON
    )
    # 受入れ機関固有のテンプレート（段階4以降。今は列だけ）
    host_organization = models.ForeignKey(
        "hosts.HostOrganization",
        verbose_name=_("host organization"),
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    method = models.CharField(_("method"), max_length=16, choices=FormMethod.choices)
    requires_signed_copy = models.BooleanField(_("signed copy required"), default=False)
    # 出典（manifest.json の source）
    source_url = models.URLField(_("source URL"), max_length=500)
    source_page = models.URLField(_("source page"), max_length=500, blank=True)
    retrieved_on = models.DateField(_("retrieved on"))
    revision_date = models.DateField(_("revision date"), null=True, blank=True)
    revision_unknown = models.BooleanField(
        _("revision date unknown"),
        default=False,
        help_text=_("The valid-from date is the earliest retrieval date recorded in an ADR."),
    )
    # リポジトリの中のファイル（FORM_TEMPLATES_ROOT からの相対パス）と SHA-256
    original_path = models.CharField(_("original file"), max_length=255)
    original_sha256 = models.CharField(_("original SHA-256"), max_length=64)
    template_path = models.CharField(_("template file"), max_length=255, blank=True)
    template_sha256 = models.CharField(_("template SHA-256"), max_length=64, blank=True)
    manifest = models.JSONField(_("manifest"), default=dict)

    class Meta:
        verbose_name = _("form template")
        verbose_name_plural = _("form templates")
        ordering = ["form_id", "-valid_from", "language"]
        constraints = [
            models.UniqueConstraint(
                fields=["form_id", "valid_from", "language", "owner"],
                condition=Q(host_organization__isnull=True),
                name="forms_engine_template_unique_common",
            ),
            models.UniqueConstraint(
                fields=["form_id", "valid_from", "language", "owner", "host_organization"],
                condition=Q(host_organization__isnull=False),
                name="forms_engine_template_unique_host",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.form_id} {self.name} ({self.valid_from:%Y-%m-%d} {self.language})"


class FormMapping(TrackedModel):
    """様式の版ごとのマッピング定義（データ項目 → セル・差込タグ、書式、検査の規則。JSON）。"""

    template = models.OneToOneField(
        FormTemplate,
        verbose_name=_("form template"),
        on_delete=models.PROTECT,
        related_name="mapping",
    )
    path = models.CharField(_("mapping file"), max_length=255)
    sha256 = models.CharField("SHA-256", max_length=64)
    definition = models.JSONField(_("definition"), default=dict)
    # 生成前の検査の規則（mappings/<様式ID>/<適用開始日>.rules.json。置き換えてよい。ADR-0019）
    rules_path = models.CharField(_("rules file"), max_length=255, blank=True)
    rules_sha256 = models.CharField(_("rules SHA-256"), max_length=64, blank=True)
    rules = models.JSONField(_("rules"), default=list)

    class Meta:
        verbose_name = _("form mapping")
        verbose_name_plural = _("form mappings")

    def __str__(self) -> str:
        return f"{self.template} mapping"


def generated_upload_to(instance: object, filename: str) -> str:
    return upload_path("generated", filename)


class GenerationStatus(models.TextChoices):
    REQUESTED = "requested", _("requested")
    COMPLETED = "completed", _("completed")
    FAILED = "failed", _("failed")


class GeneratedDocument(TrackedModel):
    """生成履歴（上書きしない）。どの様式の版・どのデータから作ったかを残す。"""

    template = models.ForeignKey(
        FormTemplate,
        verbose_name=_("form template"),
        on_delete=models.PROTECT,
        related_name="generated_documents",
    )
    # 帳票の対象（外国人・受入れ機関など）。外国人の帳票は worker にも入れる（一覧のため）
    content_type = models.ForeignKey(
        ContentType, verbose_name=_("target type"), on_delete=models.PROTECT
    )
    object_id = models.PositiveBigIntegerField(_("target ID"))
    target = GenericForeignKey("content_type", "object_id")
    worker = models.ForeignKey(
        "workers.ForeignWorker",
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="generated_documents",
    )
    basis_on = models.DateField(pgettext_lazy("generated document", "basis date"))
    # 基準日に適用される版が無く、最も古い版（改正日不明）を使った印
    revision_unknown = models.BooleanField(_("revision date unknown"), default=False)
    # データのスナップショット（JSON を暗号化。長さの上限なし）
    snapshot = EncryptedCharField(_("data snapshot"))
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=GenerationStatus.choices,
        default=GenerationStatus.REQUESTED,
    )
    failure_reason = models.CharField(_("failure reason"), max_length=200, blank=True)
    generated_at = models.DateTimeField(_("generated at"), null=True, blank=True)
    converter_version = models.CharField(_("converter"), max_length=100, blank=True)
    # 記入済みの docx・xlsx（保存だけ。取り出しは段階4 の F13 で決める）と PDF
    filled_file = PrivateFileField(
        _("filled file"), upload_to=generated_upload_to, max_length=255, blank=True
    )
    filled_sha256 = models.CharField(_("filled file SHA-256"), max_length=64, blank=True)
    pdf_file = PrivateFileField(_("PDF"), upload_to=generated_upload_to, max_length=255, blank=True)
    pdf_sha256 = models.CharField(_("PDF SHA-256"), max_length=64, blank=True)
    page_count = models.PositiveSmallIntegerField(_("pages"), null=True, blank=True)
    # 生成前の検査（ADR-0019）: 使った規則のファイルと、確かめて生成した警告
    rules_sha256 = models.CharField(_("rules SHA-256"), max_length=64, blank=True)
    findings = models.JSONField(_("warnings at generation"), default=list, blank=True)

    class Meta:
        verbose_name = _("generated document")
        verbose_name_plural = _("generated documents")
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["content_type", "object_id"])]

    def __str__(self) -> str:
        return f"{self.template.form_id} #{self.pk}"

    attachments = GenericRelation(Attachment)

    def save(self, *args: Any, **kwargs: Any) -> None:
        # 完了・失敗の後は変えない（上書きしない。再生成は新しい行）
        if not self._state.adding:
            stored = GeneratedDocument.objects.filter(pk=self.pk).values_list("status", flat=True)
            if stored and stored[0] != GenerationStatus.REQUESTED:
                raise ImmutableRowError("generated documents cannot be changed after completion")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRowError("generated documents cannot be deleted")


class GeneratedDocumentSignature(TrackedModel):
    """生成物の署名済みのスキャンとの紐づけ（追記だけ。T33、ADR-0022）。

    状態（署名待ち・署名済み）は列で持たず、取り消していない添付の紐づけがあるかで決める。
    添付は、確認書（第5-8号・第5-9号）なら記録（支援実績）の確認書の添付、記録の無い様式（第1-17号など）
    なら生成物の添付（種別「署名済みの帳票」）。署名日はこの行に持つ。
    """

    document = models.ForeignKey(
        GeneratedDocument,
        verbose_name=_("generated document"),
        on_delete=models.PROTECT,
        related_name="signatures",
    )
    attachment = models.OneToOneField(
        Attachment,
        verbose_name=_("signed scan"),
        on_delete=models.PROTECT,
        related_name="signature",
    )
    signed_on = models.DateField(_("date of signature"))

    class Meta:
        verbose_name = _("signature of a generated document")
        verbose_name_plural = _("signatures of generated documents")
        ordering = ["document", "-signed_on", "-pk"]

    def __str__(self) -> str:
        return f"{self.document} signed {self.signed_on:%Y-%m-%d}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ImmutableRowError("signatures cannot be changed; void the attachment instead")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRowError("signatures cannot be deleted; void the attachment instead")


def bundle_upload_to(instance: object, filename: str) -> str:
    return upload_path("submission", filename)


class SubmissionBundle(TrackedModel):
    """提出用の統合 PDF の生成履歴（追記だけ。T42、ADR-0033）。

    申請の書類の一覧（申請の種類×基準日）の書類を、提出書類一覧表を先頭に番号順に 1 つの PDF に
    まとめたもの。中身（items）は綴じた順の行と元の生成物・添付の ID・頁数・SHA-256 で、
    ファイル名は持たない（要配慮のファイル名を出さない。ADR-0011）。
    """

    worker = models.ForeignKey(
        "workers.ForeignWorker",
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="submission_bundles",
    )
    set_id = models.CharField(_("application type"), max_length=8)
    definition_valid_from = models.DateField(_("list revised on"))
    basis_on = models.DateField(pgettext_lazy("generated document", "basis date"))
    file = PrivateFileField(_("PDF"), upload_to=bundle_upload_to, max_length=255)
    sha256 = models.CharField("SHA-256", max_length=64)
    size = models.PositiveBigIntegerField(_("size (bytes)"))
    page_count = models.PositiveIntegerField(_("pages"))
    # 要配慮の添付（健康診断の書類など）を含む: ダウンロードは担当者と管理者だけ
    contains_sensitive = models.BooleanField(_("contains sensitive documents"), default=False)
    items = models.JSONField(_("contents"), default=list)
    warnings = models.JSONField(_("warnings when made"), default=list, blank=True)
    # 提出書類一覧表の提出確認欄の印（行の ID → yes・no。空は書かない。T42 の追加）
    list_marks = models.JSONField(_("marks of the list of documents"), default=dict, blank=True)

    class Meta:
        verbose_name = _("submission PDF")
        verbose_name_plural = _("submission PDFs")
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.set_id} {self.basis_on:%Y-%m-%d} #{self.pk}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ImmutableRowError("submission PDFs cannot be changed; make a new one")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ImmutableRowError("submission PDFs cannot be deleted")


class SubmissionDraft(TrackedModel):
    """提出用の統合 PDF の下書き（外国人×申請の種類。T42-D3 の直し）。

    統合 PDF の画面でアップロードしたシステムの外の書類を、行ごとに保存して、
    作り直すときに使い回す。
    ファイルは添付（種別「提出書類（システムの外）」）で、取り除くときは取消（行とファイルは残る）。
    """

    worker = models.ForeignKey(
        "workers.ForeignWorker",
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="submission_drafts",
    )
    set_id = models.CharField(_("application type"), max_length=8)

    class Meta:
        verbose_name = _("draft of the submission PDF")
        verbose_name_plural = _("drafts of the submission PDF")
        constraints = [
            models.UniqueConstraint(
                fields=["worker", "set_id"], name="forms_engine_submission_draft_unique"
            )
        ]

    def __str__(self) -> str:
        return f"{self.set_id} #{self.pk}"

    attachments = GenericRelation(Attachment)


class SubmissionDraftFile(TrackedModel):
    """下書きの行（申請の書類の一覧の行の ID）と添付の紐づけ（追記だけ）。"""

    draft = models.ForeignKey(
        SubmissionDraft,
        verbose_name=_("draft of the submission PDF"),
        on_delete=models.PROTECT,
        related_name="files",
    )
    row_id = models.CharField(_("row"), max_length=64)
    attachment = models.OneToOneField(
        Attachment,
        verbose_name=_("file"),
        on_delete=models.PROTECT,
        related_name="submission_draft_file",
    )

    class Meta:
        verbose_name = _("file of the draft of the submission PDF")
        verbose_name_plural = _("files of the draft of the submission PDF")
        ordering = ["draft", "row_id", "pk"]

    def __str__(self) -> str:
        return f"{self.draft} {self.row_id}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ImmutableRowError("draft files cannot be changed; void the attachment instead")
        super().save(*args, **kwargs)


# --- 在留申請オンラインの一括申請（T48。ADR-0039）---------------------------------------------


class BulkProcedure(models.TextChoices):
    COE = "coe", _("certificate of eligibility")
    EXTENSION = "extension", _("extension of period of stay")
    CHANGE = "change", _("change of status of residence")


# 手続 → 様式 ID（一括登録用ツール）
BULK_FORM_IDS = {
    BulkProcedure.COE: "bulk-coe",
    BulkProcedure.EXTENSION: "bulk-ext",
    BulkProcedure.CHANGE: "bulk-chg",
}


class BulkApplication(TrackedModel):
    """一括申請の 1 件（手続・基準日・取次者）。外国人ごとの行と、作った xlsm を持つ。"""

    procedure = models.CharField(_("procedure"), max_length=16, choices=BulkProcedure.choices)
    basis_on = models.DateField(pgettext_lazy("generated document", "basis date"))
    title = models.CharField(_("title"), max_length=100, blank=True)
    intermediary = models.ForeignKey(
        "core.Staff",
        verbose_name=_("intermediary (user of the online system)"),
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = _("bulk application")
        verbose_name_plural = _("bulk applications")
        ordering = ["-pk"]

    def __str__(self) -> str:
        return f"{self.get_procedure_display()} {self.basis_on} #{self.pk}"


class BulkApplicationEntry(TrackedModel):
    """一括申請の外国人ごとの行と、申請ごとに入れる値（T46-D1・T48-D1）。

    inputs は申請ごとの入力（配偶者・職業・希望する在留期間・理由・受領方法・雇用契約の項目など）。
    犯罪を理由とする処分の有無と内容は sensitive（暗号化。監査ログは ***）に持つ
    （ADR-0038 決定 6）。
    """

    application = models.ForeignKey(
        BulkApplication,
        verbose_name=_("bulk application"),
        on_delete=models.CASCADE,
        related_name="entries",
    )
    worker = models.ForeignKey(
        "workers.ForeignWorker",
        verbose_name=_("foreign worker"),
        on_delete=models.PROTECT,
        related_name="bulk_entries",
    )
    agent = models.ForeignKey(
        "hosts.HostApplicantContact",
        verbose_name=_("agent"),
        on_delete=models.PROTECT,
        related_name="+",
        null=True,
        blank=True,
    )
    inputs = models.JSONField(_("inputs for this application"), default=dict, blank=True)
    sensitive = EncryptedCharField(_("criminal record (sensitive)"), blank=True)

    class Meta:
        verbose_name = _("bulk application row")
        verbose_name_plural = _("bulk application rows")
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "worker"], name="forms_engine_bulk_entry_one_worker"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.application}: {self.worker}"


def bulk_upload_to(instance: object, filename: str) -> str:
    return upload_path("bulk", filename)


class BulkApplicationFile(TrackedModel):
    """作った一括の xlsm（300 人ごとに 1 つ。追記だけ）。スナップショットに犯罪の内容は残さない。"""

    application = models.ForeignKey(
        BulkApplication,
        verbose_name=_("bulk application"),
        on_delete=models.CASCADE,
        related_name="files",
    )
    template = models.ForeignKey(
        FormTemplate, verbose_name=_("form template"), on_delete=models.PROTECT, related_name="+"
    )
    part = models.PositiveSmallIntegerField(_("part"))
    parts = models.PositiveSmallIntegerField(_("number of parts"))
    file = PrivateFileField(_("file"), upload_to=bulk_upload_to, max_length=255)
    sha256 = models.CharField("SHA-256", max_length=64)
    worker_ids = models.JSONField(_("foreign workers"), default=list)
    includes_sensitive = models.BooleanField(_("includes sensitive information"), default=False)
    snapshot = EncryptedCharField(_("data snapshot"))

    class Meta:
        verbose_name = _("bulk application file")
        verbose_name_plural = _("bulk application files")
        ordering = ["-pk"]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ImmutableRowError("bulk application files cannot be changed")
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.application} ({self.part}/{self.parts})"
