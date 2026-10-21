"""Email normalisation and IMAP provider presets.

The portal only needs one account per container.  Most consumer providers use
a well-known IMAP endpoint, so the form can pre-fill everything for them and
still allow an explicit host/port override for custom domains.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_TLS_MODES = {"tls", "starttls", "none"}


class InputError(ValueError):
    """A user-supplied value is invalid.  The message is safe to display."""

    def __init__(self, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    domains: tuple[str, ...]
    host: str
    port: int
    tls_mode: str
    hint: str
    strip_spaces: bool = False


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        key="gmail",
        name="Google Gmail",
        domains=("gmail.com", "googlemail.com"),
        host="imap.gmail.com",
        port=993,
        tls_mode="tls",
        hint="需要开启两步验证并生成「应用专用密码」，同时确认 Gmail 设置中已启用 IMAP。",
        strip_spaces=True,
    ),
    Provider(
        key="qq",
        name="QQ 邮箱",
        domains=("qq.com", "foxmail.com", "vip.qq.com"),
        host="imap.qq.com",
        port=993,
        tls_mode="tls",
        hint="在 QQ 邮箱设置 → 账户中开启 IMAP，并使用生成的「授权码」。",
    ),
    Provider(
        key="netease",
        name="网易邮箱",
        domains=("163.com", "126.com", "yeah.net", "188.com", "vip.163.com", "vip.126.com"),
        host="imap.163.com",
        port=993,
        tls_mode="tls",
        hint="在邮箱设置中开启 IMAP/SMTP，并使用「客户端授权密码」。",
    ),
    Provider(
        key="aliyun-enterprise",
        name="阿里企业邮箱",
        domains=(),
        host="imap.qiye.aliyun.com",
        port=993,
        tls_mode="tls",
        hint="阿里企业邮箱：请在管理后台开启 IMAP/SMTP，并使用客户端专用密码；部分企业安全策略需要放行第三方客户端。",
    ),
    Provider(
        key="outlook",
        name="Microsoft Outlook",
        domains=("outlook.com", "hotmail.com", "live.com", "msn.com"),
        host="outlook.office365.com",
        port=993,
        tls_mode="tls",
        hint="Microsoft 个人账户通常已禁用 IMAP 基本认证，可能需要应用密码或 OAuth2；若连接失败请改用其他邮箱。",
    ),
    Provider(
        key="icloud",
        name="Apple iCloud",
        domains=("icloud.com", "me.com", "mac.com"),
        host="imap.mail.me.com",
        port=993,
        tls_mode="tls",
        hint="需要在 Apple ID 中生成「App 专用密码」。",
    ),
    Provider(
        key="yahoo",
        name="Yahoo Mail",
        domains=("yahoo.com", "ymail.com", "rocketmail.com"),
        host="imap.mail.yahoo.com",
        port=993,
        tls_mode="tls",
        hint="需要在 Yahoo 账户安全设置中生成「应用密码」。",
    ),
    Provider(
        key="sina",
        name="新浪邮箱",
        domains=("sina.com", "sina.cn"),
        host="imap.sina.com",
        port=993,
        tls_mode="tls",
        hint="在邮箱设置中开启 IMAP 服务并使用客户端授权码。",
    ),
    Provider(
        key="sohu",
        name="搜狐邮箱",
        domains=("sohu.com",),
        host="imap.sohu.com",
        port=993,
        tls_mode="tls",
        hint="在邮箱设置中开启 IMAP 服务并使用客户端授权码。",
    ),
    Provider(
        key="139",
        name="中国移动 139 邮箱",
        domains=("139.com",),
        host="imap.139.com",
        port=993,
        tls_mode="tls",
        hint="在邮箱设置中开启 IMAP 服务并使用客户端授权码。",
    ),
)

_BY_DOMAIN: dict[str, Provider] = {}
for _provider in PROVIDERS:
    for _domain in _provider.domains:
        _BY_DOMAIN[_domain] = _provider

_BY_KEY: dict[str, Provider] = {provider.key: provider for provider in PROVIDERS}


@dataclass(frozen=True)
class ImapSettings:
    host: str
    port: int
    tls_mode: str
    username: str
    provider_key: str
    provider_name: str
    hint: str


def normalize_email(raw: str) -> str:
    email = raw.strip()
    if not email:
        raise InputError("请输入邮箱地址", field="email")
    if len(email) > 254:
        raise InputError("邮箱地址过长", field="email")
    if not _EMAIL_RE.match(email):
        raise InputError("邮箱地址格式不正确", field="email")
    # Consumer mailboxes are case-insensitive in practice; normalising the
    # whole address keeps idempotency stable across keyboard capitalisation.
    return email.lower()


def normalize_password(raw: str) -> str:
    if not raw:
        raise InputError("请输入邮箱授权码", field="password")
    if len(raw) > 1024:
        raise InputError("授权码过长", field="password")
    if any(ch in raw for ch in ("\x00", "\r", "\n")):
        raise InputError("授权码包含非法字符", field="password")
    return raw


def normalize_tls(raw: str) -> str:
    value = (raw or "tls").strip().lower()
    if value not in _TLS_MODES:
        raise InputError("TLS 模式必须是 tls、starttls 或 none", field="tls_mode")
    return value


def normalize_host(raw: str) -> str:
    host = raw.strip()
    if not host:
        raise InputError("请输入 IMAP 服务器地址", field="host")
    if len(host) > 253 or not _HOST_RE.match(host) or ".." in host:
        raise InputError("IMAP 服务器地址格式不正确", field="host")
    return host


def normalize_port(raw: str | int | None) -> int:
    if raw is None or str(raw).strip() == "":
        return 0
    try:
        port = int(str(raw).strip())
    except ValueError as exc:
        raise InputError("端口必须是数字", field="port") from exc
    if port < 1 or port > 65535:
        raise InputError("端口必须在 1-65535 之间", field="port")
    return port


def detect_provider(email: str) -> Provider | None:
    domain = email.rsplit("@", 1)[-1].lower()
    return _BY_DOMAIN.get(domain)


def resolve_settings(
    email: str,
    *,
    host: str = "",
    port: str | int | None = None,
    tls_mode: str = "",
    provider_key: str = "",
) -> ImapSettings:
    """Merge a provider preset with explicit overrides from the advanced form.

    ``provider_key`` comes from the provider dropdown.  ``auto`` (or empty)
    detects by email domain; a concrete key lets users pick a provider whose
    domain cannot be inferred, such as Alibaba enterprise mail on a custom
    domain; ``custom`` requires an explicit host.
    """
    selected_key = (provider_key or "auto").strip().lower()
    explicit_host = host.strip() if host else ""
    explicit_tls = normalize_tls(tls_mode) if tls_mode else ""
    selected = _BY_KEY.get(selected_key)
    if selected_key not in {"", "auto", "custom"} and selected is None:
        raise InputError("未知的邮箱服务商", field="provider")

    if selected is not None:
        resolved_host = normalize_host(explicit_host) if explicit_host else selected.host
        resolved_tls = explicit_tls or selected.tls_mode
        resolved_port = normalize_port(port) or selected.port
        if explicit_host and resolved_host != selected.host:
            # The user picked a preset but overrode the host with something
            # else: treat it as a custom server (and subject it to SSRF checks).
            return ImapSettings(
                host=resolved_host,
                port=resolved_port,
                tls_mode=resolved_tls,
                username=email,
                provider_key="custom",
                provider_name="自定义 IMAP",
                hint="",
            )
        return ImapSettings(
            host=resolved_host,
            port=resolved_port,
            tls_mode=resolved_tls,
            username=email,
            provider_key=selected.key,
            provider_name=selected.name,
            hint=selected.hint,
        )

    if selected_key == "custom":
        if not explicit_host:
            raise InputError("请填写 IMAP 服务器地址", field="host")
        resolved_host = normalize_host(explicit_host)
        resolved_tls = explicit_tls or "tls"
        resolved_port = normalize_port(port) or (993 if resolved_tls == "tls" else 143)
        return ImapSettings(
            host=resolved_host,
            port=resolved_port,
            tls_mode=resolved_tls,
            username=email,
            provider_key="custom",
            provider_name="自定义 IMAP",
            hint="",
        )

    provider = detect_provider(email)
    if explicit_host:
        resolved_host = normalize_host(explicit_host)
        resolved_tls = explicit_tls or (provider.tls_mode if provider else "tls")
        resolved_port = normalize_port(port) or (993 if resolved_tls == "tls" else 143)
        provider_key_out = provider.key if provider and provider.host == resolved_host else "custom"
        provider_name = provider.name if provider_key_out != "custom" else "自定义 IMAP"
        hint = provider.hint if provider_key_out != "custom" else ""
    elif provider:
        resolved_host = provider.host
        resolved_tls = explicit_tls or provider.tls_mode
        resolved_port = normalize_port(port) or provider.port
        provider_key_out = provider.key
        provider_name = provider.name
        hint = provider.hint
    else:
        raise InputError(
            "无法根据邮箱域名自动识别 IMAP 服务器。阿里企业邮箱请选择「阿里企业邮箱」，"
            "其他自定义域名请在「高级设置」中填写服务器地址。",
            field="host",
        )

    return ImapSettings(
        host=resolved_host,
        port=resolved_port,
        tls_mode=resolved_tls,
        username=email,
        provider_key=provider_key_out,
        provider_name=provider_name,
        hint=hint,
    )


def prepare_password(email: str, password: str) -> str:
    """Apply provider-specific password cleanup.

    Google displays app passwords in groups of four and ignores spaces in them;
    sending the spaces to IMAP can fail, so remove them for Gmail only.
    """
    provider = detect_provider(email)
    if provider and provider.strip_spaces:
        return password.replace(" ", "")
    return password
