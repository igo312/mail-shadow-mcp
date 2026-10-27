"""Minimal IMAP credential check used before a container is provisioned.

This is deliberately a *validation* step, not a mail client.  It only opens a
connection, optionally sends the RFC 2971 ID command that some Chinese
providers require, authenticates, and disconnects.  A successful check means
the app password can be used by mail-shadow-mcp's sync engine.
"""

from __future__ import annotations

import base64
import imaplib
import re
import socket
import ssl
from typing import Any

from .providers import ImapSettings

# 163/126 reject LOGIN with "Unsafe Login" unless the client identifies itself.
imaplib.Commands.setdefault("ID", ("NONAUTH", "AUTH", "SELECTED"))

_ID_COMMAND = '("name" "mail-shadow-mcp" "version" "1.0" "vendor" "mail-shadow-mcp")'


class ImapError(RuntimeError):
    """Base class for sanitised, user-displayable IMAP errors."""


class ImapAuthError(ImapError):
    """The server was reachable but rejected the credentials."""


class ImapUnreachable(ImapError):
    """The server could not be reached or TLS could not be established."""


def _capabilities(client: imaplib.IMAP4) -> set[str]:
    raw = getattr(client, "capabilities", ()) or ()
    result: set[str] = set()
    for item in raw:
        if isinstance(item, bytes):
            item = item.decode("ascii", "ignore")
        result.add(str(item).upper())
    return result


def _send_id(client: imaplib.IMAP4) -> None:
    try:
        typ, data = client._simple_command("ID", _ID_COMMAND)  # noqa: SLF001
        client._untagged_response(typ, data, "ID")  # noqa: SLF001
    except (imaplib.IMAP4.error, imaplib.IMAP4.abort, OSError):
        # Most servers without RFC 2971 answer BAD; that is not fatal.
        pass


def _sanitize(message: str, password: str) -> str:
    text = message.replace(password, "***") if password else message
    text = " ".join(text.split())
    return text[:240]


def _close_quietly(client: imaplib.IMAP4 | None) -> None:
    if client is None:
        return
    try:
        client.logout()
    except (imaplib.IMAP4.error, imaplib.IMAP4.abort, OSError, AttributeError):
        pass


def _open(settings: ImapSettings, password: str, *, timeout: float) -> imaplib.IMAP4:
    """Connect, identify and authenticate.

    Returns a logged-in client that the caller must close.  Errors are mapped to
    ImapAuthError / ImapUnreachable so the web layer can show a sanitised
    message instead of a raw traceback.
    """
    context = ssl.create_default_context()
    client: imaplib.IMAP4 | None = None
    ok = False
    try:
        if settings.tls_mode == "tls":
            client = imaplib.IMAP4_SSL(
                settings.host,
                settings.port,
                timeout=timeout,
                ssl_context=context,
            )
        elif settings.tls_mode == "starttls":
            client = imaplib.IMAP4(settings.host, settings.port, timeout=timeout)
            client.starttls(ssl_context=context)
        else:
            client = imaplib.IMAP4(settings.host, settings.port, timeout=timeout)

        _send_id(client)
        caps = _capabilities(client)

        try:
            client.login(settings.username, password)
        except (imaplib.IMAP4.error, imaplib.IMAP4.abort) as first_error:
            # Some servers advertise AUTH=PLAIN but reject LOGIN.  Aliyun is a
            # real-world example.  Only fall back when PLAIN is advertised, so
            # a wrong password is not retried pointlessly on every server.
            if "AUTH=PLAIN" not in caps:
                raise ImapAuthError(
                    "IMAP 登录失败：" + _sanitize(str(first_error), password)
                ) from first_error
            try:
                client.authenticate(
                    "PLAIN", lambda _challenge: f"\0{settings.username}\0{password}".encode()
                )
            except (imaplib.IMAP4.error, imaplib.IMAP4.abort) as second_error:
                raise ImapAuthError(
                    "IMAP 认证失败：" + _sanitize(str(second_error), password)
                ) from second_error
        ok = True
        return client
    except ImapError:
        raise
    except ssl.SSLError as exc:
        raise ImapUnreachable(
            "TLS 握手失败，请检查服务器地址、端口或 TLS 模式（" + _sanitize(str(exc), password) + "）"
        ) from exc
    except (socket.gaierror, socket.herror) as exc:
        raise ImapUnreachable("无法解析 IMAP 服务器地址：" + _sanitize(str(exc), password)) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ImapUnreachable("连接 IMAP 服务器超时，请检查网络或服务器地址") from exc
    except ConnectionRefusedError as exc:
        raise ImapUnreachable(
            "IMAP 服务器拒绝连接，请检查端口或 TLS 模式"
        ) from exc
    except OSError as exc:
        raise ImapUnreachable("无法连接 IMAP 服务器：" + _sanitize(str(exc), password)) from exc
    except (imaplib.IMAP4.error, imaplib.IMAP4.abort) as exc:
        raise ImapAuthError("IMAP 服务器返回错误：" + _sanitize(str(exc), password)) from exc
    finally:
        if not ok:
            _close_quietly(client)


def check_login(settings: ImapSettings, password: str, *, timeout: float = 15.0) -> None:
    """Raise ImapAuthError/ImapUnreachable if the credentials do not work."""
    client = _open(settings, password, timeout=timeout)
    _close_quietly(client)


# ---------------------------------------------------------------------------
# Folder listing (used by the web UI so users can pick what to sync)
# ---------------------------------------------------------------------------
def _decode_modified_utf7(value: str) -> str:
    """Decode an IMAP folder name (RFC 3501 modified UTF-7) to normal Unicode.

    Aliyun/163/QQ return folder names like ``&XfJT0ZAB-`` for Chinese mailboxes;
    the sync engine and the YAML config use the decoded UTF-8 form.
    """
    if "&" not in value:
        return value
    out: list[str] = []
    i = 0
    while i < len(value):
        char = value[i]
        if char != "&":
            out.append(char)
            i += 1
            continue
        end = value.find("-", i)
        if end == -1:
            out.append(value[i:])
            break
        chunk = value[i + 1 : end]
        if chunk == "":
            out.append("&")
        else:
            try:
                padded = chunk.replace(",", "/")
                padded += "=" * (-len(padded) % 4)
                out.append(base64.b64decode(padded).decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                out.append(value[i : end + 1])
        i = end + 1
    return "".join(out)


def _parse_list_entry(raw: Any) -> str | None:
    """Parse one ``LIST`` response line into a folder name."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    match = re.match(
        r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.*)$',
        text.strip(),
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    flags = match.group("flags").lower()
    if "\\noselect" in flags or "\\nonexistent" in flags:
        return None
    name = match.group("name").strip()
    # A literal ({n}\r\n...) is only used for names with special characters; the
    # trailing size marker is not part of the name.
    name = re.sub(r"\{\d+\}$", "", name).strip()
    if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if not name:
        return None
    return _decode_modified_utf7(name)


def list_folders(settings: ImapSettings, password: str, *, timeout: float = 20.0) -> list[str]:
    """Return every selectable mailbox folder, INBOX first."""
    client = _open(settings, password, timeout=timeout)
    names: list[str] = []
    try:
        typ, data = client.list()
        if typ != "OK":
            raise ImapError("IMAP 服务器拒绝列出文件夹：" + _sanitize(str(data), password))
        seen: set[str] = set()
        for raw in data or []:
            name = _parse_list_entry(raw)
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    except (imaplib.IMAP4.error, imaplib.IMAP4.abort) as exc:
        raise ImapError("IMAP 列出文件夹失败：" + _sanitize(str(exc), password)) from exc
    finally:
        _close_quietly(client)
    return sorted(names, key=lambda name: (name.upper() != "INBOX", name.casefold()))


def probe_result(settings: ImapSettings, password: str) -> dict[str, Any]:
    """Convenience wrapper for the CLI/doctor command."""
    try:
        check_login(settings, password)
    except ImapError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}
