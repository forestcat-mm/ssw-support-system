"""接続元 IP の取得（監査ログと django-axes で共通）。

本番は Cloudflare Tunnel 経由でしか届かない構成のため、TRUST_CF_CONNECTING_IP=true で
CF-Connecting-IP を信頼する。直接届く経路がある構成で true にするとヘッダーを偽装できるので不可。
手元（false）は REMOTE_ADDR を使い、ヘッダーは無視する。
"""

import ipaddress

from django.conf import settings
from django.http import HttpRequest


def client_ip(request: HttpRequest) -> str | None:
    if getattr(settings, "TRUST_CF_CONNECTING_IP", False):
        forwarded = _valid_ip(request.META.get("HTTP_CF_CONNECTING_IP"))
        if forwarded is not None:
            return forwarded
    return _valid_ip(request.META.get("REMOTE_ADDR"))


def _valid_ip(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None
