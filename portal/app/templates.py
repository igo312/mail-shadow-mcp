"""Server-rendered HTML.  No template engine, no third-party dependencies."""

from __future__ import annotations

import html
import json
from typing import Any

from .providers import PROVIDERS


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


LOGO = """
<svg class="logo-mark" viewBox="0 0 40 40" aria-hidden="true" fill="none">
  <path d="M20 4c6 0 10 4.4 10 10v13.5c0 2.2 1.6 3.2 3 4.1-1.8 1.6-3.7 2.4-5.8 2.4-2 0-3.8-.7-5.2-2-1.4 1.3-3.3 2-5.2 2-2.1 0-4-.8-5.8-2.4 1.4-.9 3-1.9 3-4.1V14c0-5.6 4-10 10-10Z"
        stroke="currentColor" stroke-width="2.2" stroke-linejoin="round"/>
  <path d="M12 18h16v9.5H12z" stroke="currentColor" stroke-width="2.2" stroke-linejoin="round"/>
  <path d="m12.5 18.5 7.5 6 7.5-6" stroke="currentColor" stroke-width="2.2" stroke-linejoin="round"/>
  <circle cx="17" cy="12.5" r="1.3" fill="currentColor"/>
  <circle cx="23" cy="12.5" r="1.3" fill="currentColor"/>
</svg>
"""


def layout(
    title: str,
    body: str,
    *,
    show_logout: bool = False,
    csrf: str = "",
    wide: bool = False,
) -> str:
    logout = ""
    if show_logout:
        logout = f"""
        <form method="post" action="/logout" class="logout-form">
          <input type="hidden" name="csrf" value="{e(csrf)}">
          <button type="submit" class="btn btn-ghost btn-small">退出</button>
        </form>"""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <title>{e(title)} · Mail Shadow</title>
  <link rel="stylesheet" href="/static/app.css">
  <script src="/static/app.js" defer></script>
</head>
<body>
  <div class="aurora" aria-hidden="true"></div>
  <header class="topbar{' topbar-wide' if wide else ''}">
    <a class="brand" href="/" aria-label="Mail Shadow 首页">
      {LOGO}
      <span class="brand-text"><strong>Mail Shadow</strong><small>MCP hosting</small></span>
    </a>
    {logout}
  </header>
  <main class="shell{' shell-wide' if wide else ''}">
    {body}
  </main>
  <footer class="footer{' footer-wide' if wide else ''}">
    <span>mail-shadow-mcp · 每个邮箱一个隔离容器</span>
    <span class="footer-dot">·</span>
    <span>AI 只能读取影子副本，不能操作你的真实邮箱</span>
  </footer>
</body>
</html>"""


def invite_page(*, error: str = "", no_invites: bool = False) -> str:
    notice = ""
    if no_invites:
        notice = """
        <div class="callout callout-warn">
          <strong>服务尚未开放</strong>
          <p>还没有创建邀请码。管理员可以使用 <code>python cli.py invite create</code> 生成一个。</p>
        </div>"""
    error_html = f'<div class="form-error" role="alert">{e(error)}</div>' if error else ""
    body = f"""
    <div class="gate-layout">
    <section class="hero">
      <div class="eyebrow"><span class="pulse"></span> 私人邮件影子副本</div>
      <h1>让 AI 读邮件，<br><span class="grad">但不碰你的真实邮箱。</span></h1>
      <p class="lede">
        输入邀请码，为你的邮箱启动一个隔离的 mail-shadow-mcp 实例。
        它会先把邮件同步到本地影子数据库，AI 只能查询这份副本。
      </p>
      <div class="feature-row">
        <span>只读同步</span><span>全文搜索</span><span>独立容器</span><span>Token 隔离</span>
      </div>
    </section>

    <section class="card card-gate">
      <div class="card-head">
        <h2>输入邀请码</h2>
        <p>邀请码由服务管理员发放，每个邀请码有独立的使用次数。</p>
      </div>
      {notice}
      {error_html}
      <form method="post" action="/invite" class="stack" autocomplete="off">
        <label class="field">
          <span class="label">邀请码</span>
          <input class="input" type="text" name="invite" placeholder="msm_..." required
                 spellcheck="false" autocapitalize="none" autofocus>
        </label>
        <button class="btn btn-primary" type="submit">继续 <span aria-hidden="true">→</span></button>
      </form>
      <p class="fineprint">继续即表示你理解：邮箱授权码只会用于启动你自己的同步容器。</p>
    </section>
    </div>"""
    return layout("邀请码", body)


def provision_page(
    *,
    csrf: str,
    email: str = "",
    host: str = "",
    port: str = "",
    tls_mode: str = "tls",
    provider_key: str = "auto",
    error: str = "",
    field: str = "",
    hint: str = "",
    invite_remaining: str = "",
    selected_folders: list[str] | None = None,
    folders_all: bool = False,
) -> str:
    error_html = f'<div class="form-error" role="alert">{e(error)}</div>' if error else ""
    hint_html = f'<div class="form-hint">{e(hint)}</div>' if hint else ""
    selected_hidden = "".join(
        f'<input type="hidden" name="folders" value="{e(name)}" data-folder-preset>'
        for name in (selected_folders or [])
    )

    def invalid(name: str) -> str:
        return " input-invalid" if field == name else ""

    tls_options = "".join(
        f'<option value="{mode}"{" selected" if tls_mode == mode else ""}>{label}</option>'
        for mode, label in (
            ("tls", "TLS（993，推荐）"),
            ("starttls", "STARTTLS（143）"),
            ("none", "不加密（仅内网测试）"),
        )
    )
    provider_options = ['<option value="auto">自动识别（推荐）</option>']
    provider_options.extend(
        f'<option value="{e(provider.key)}"{" selected" if provider_key == provider.key else ""}>'
        f"{e(provider.name)}</option>"
        for provider in PROVIDERS
    )
    provider_options.append(
        f'<option value="custom"{" selected" if provider_key == "custom" else ""}>自定义 IMAP</option>'
    )
    body = f"""
    <section class="page-head">
      <a class="back" href="/">← 返回</a>
      <h1>创建你的邮件实例</h1>
      <p>填写邮箱和 IMAP 授权码。我们会先验证登录，再启动一个只属于这个邮箱的容器。</p>
      {f'<span class="invite-pill">{e(invite_remaining)}</span>' if invite_remaining else ''}
    </section>

    <div class="grid-2">
      <section class="card">
        <div class="card-head">
          <h2>邮箱凭据</h2>
          <p>授权码不是邮箱登录密码。它只会写入你的实例配置卷。</p>
        </div>
        {error_html}
        {hint_html}
        <form method="post" action="/provision" class="stack" autocomplete="off">
          <input type="hidden" name="csrf" value="{e(csrf)}">
          <label class="field">
            <span class="label">邮箱服务商</span>
            <select class="input{invalid('provider')}" name="provider">
              {''.join(provider_options)}
            </select>
            <span class="help">阿里企业邮箱请选择「阿里企业邮箱」；其他常见邮箱保持自动识别即可。</span>
          </label>
          <label class="field">
            <span class="label">邮箱地址</span>
            <input class="input{invalid('email')}" type="email" name="email" value="{e(email)}"
                   placeholder="you@example.com" required autocomplete="off" spellcheck="false"
                   autocapitalize="none">
            <span class="help">支持 Gmail、QQ、网易、iCloud 等常见服务商，也支持自定义 IMAP。</span>
          </label>
          <label class="field">
            <span class="label">IMAP 授权码 / 应用专用密码</span>
            <input class="input{invalid('password')}" type="password" name="password" required
                   autocomplete="new-password" spellcheck="false">
            <span class="help">Gmail 需开启两步验证并使用应用专用密码；QQ / 网易使用邮箱设置里生成的授权码。</span>
          </label>

          <details class="advanced"{' open' if host or field in {'host', 'port', 'tls_mode'} else ''}>
            <summary>高级设置 <span>（自定义 IMAP 服务器 / 端口 / TLS）</span></summary>
            <div class="advanced-body">
              <label class="field">
                <span class="label">IMAP 服务器</span>
                <input class="input{invalid('host')}" type="text" name="host" value="{e(host)}"
                       placeholder="留空自动识别，例如 imap.example.com" spellcheck="false"
                       autocapitalize="none">
              </label>
              <div class="field-row">
                <label class="field">
                  <span class="label">端口</span>
                  <input class="input{invalid('port')}" type="number" name="port" value="{e(port)}"
                         min="1" max="65535" placeholder="自动">
                </label>
                <label class="field">
                  <span class="label">TLS 模式</span>
                  <select class="input{invalid('tls_mode')}" name="tls_mode">{tls_options}</select>
                </label>
              </div>
            </div>
          </details>

          <div class="folder-picker" data-folder-picker>
            <div class="picker-head">
              <span class="label">同步哪些文件夹？</span>
              <button class="btn btn-ghost btn-small" type="button" data-folder-fetch>
                <span class="btn-label">获取文件夹列表</span>
                <span class="btn-spinner" aria-hidden="true"></span>
              </button>
            </div>
            <p class="help">只勾选常用的几个：文件夹越多，首次同步越慢，AI 查询也越容易排队。不点「获取文件夹列表」就沿用当前设置。</p>
            <div class="picker-status" data-folder-status role="status" aria-live="polite"></div>
            <div class="picker-toolbar" data-folder-toolbar hidden>
              <input class="input input-compact" type="search" data-folder-filter
                     placeholder="筛选文件夹…" spellcheck="false" autocomplete="off">
              <button class="btn btn-ghost btn-small" type="button" data-folder-all>全选</button>
              <button class="btn btn-ghost btn-small" type="button" data-folder-none>清空</button>
              <span class="picker-count" data-folder-count></span>
            </div>
            <div class="picker-list" data-folder-list hidden></div>
            <label class="checkline">
              <input type="checkbox" name="folders_mode" value="all" data-folder-mode-all{' checked' if folders_all else ''}>
              <span>同步全部文件夹（大邮箱会很慢，不建议）</span>
            </label>
            <input type="hidden" name="folders_used" value="" data-folder-used>
            {selected_hidden}
          </div>

          <button class="btn btn-primary btn-wide" type="submit">
            <span class="btn-label">验证并创建实例</span>
            <span class="btn-spinner" aria-hidden="true"></span>
          </button>
        </form>
      </section>

      <aside class="stack-aside">
        <section class="card card-muted">
          <h3>接下来会发生什么？</h3>
          <ol class="steps">
            <li><span>1</span><div><strong>验证登录</strong><p>用只读方式连接一次 IMAP，确认授权码有效。</p></div></li>
            <li><span>2</span><div><strong>启动容器</strong><p>为这个邮箱创建独立的数据卷和隔离容器。</p></div></li>
            <li><span>3</span><div><strong>领取配置</strong><p>生成专属 MCP URL 和 Bearer Token，填入你的 AI 客户端。</p></div></li>
          </ol>
        </section>
        <section class="card card-muted">
          <h3>安全边界</h3>
          <ul class="ticks">
            <li>容器不开放公网端口，只由网关按 Token 转发。</li>
            <li>你的授权码不会写进 portal 数据库。</li>
            <li>MCP 工具只能查询影子副本，不会拿到 IMAP 写权限。</li>
          </ul>
        </section>
        <section class="card card-muted">
          <h3>支持的邮箱</h3>
          <p class="provider-list">{' · '.join(e(p.name) for p in PROVIDERS[:7])} 等</p>
        </section>
      </aside>
    </div>"""
    return layout("创建实例", body, show_logout=True, csrf=csrf, wide=True)


def result_page(
    *,
    instance: dict[str, Any],
    token: str,
    csrf: str,
    reused: bool,
    rotated: bool,
    warning: str = "",
) -> str:
    configs = _config_snippets(token)
    status_note = ""
    if rotated:
        status_note = """
        <div class="callout callout-warn">
          <strong>授权码已更新，MCP Token 已轮换</strong>
          <p>这个邮箱之前生成的旧配置已经失效，请把下面的新配置更新到 AI 客户端。</p>
        </div>"""
    elif reused:
        status_note = """
        <div class="callout callout-ok">
          <strong>已找到现有实例</strong>
          <p>邮箱和授权码与之前一致，因此直接返回同一份 MCP 配置，没有重复创建容器。</p>
        </div>"""
    else:
        status_note = """
        <div class="callout callout-ok">
          <strong>实例已就绪</strong>
          <p>容器已启动并开始同步邮件。首次同步的时间取决于邮箱大小。</p>
        </div>"""
    if warning:
        status_note += f'<div class="callout callout-warn"><strong>注意</strong><p>{e(warning)}</p></div>'

    tabs = []
    panels = []
    for index, (label, language, text) in enumerate(configs):
        active = " active" if index == 0 else ""
        tabs.append(
            f'<button class="tab{active}" type="button" role="tab" data-tab="cfg-{index}" '
            f'aria-selected="{"true" if index == 0 else "false"}">{e(label)}</button>'
        )
        panels.append(
            f'<div class="tab-panel{active}" id="cfg-{index}" role="tabpanel">'
            f'<div class="code-head"><span>{e(language)}</span>'
            f'<button class="btn btn-ghost btn-small copy-btn" type="button" data-copy="code-{index}">复制</button></div>'
            f'<pre><code id="code-{index}">{e(text)}</code></pre></div>'
        )

    masked = _mask_email(instance.get("email", ""))
    status = str(instance.get("status", "running"))
    status_labels = {
        "running": "容器运行中",
        "provisioning": "容器创建中",
        "updating": "容器更新中",
        "failed": "需要重试",
        "removed": "已移除",
    }
    body = f"""
    <section class="page-head page-head-result">
      <div class="success-mark" aria-hidden="true">✓</div>
      <h1>你的邮件 MCP 已就绪</h1>
      <p>{e(masked)} · {e(instance.get('provider_name') or 'IMAP')} · {e(instance.get('imap_host'))}:{e(instance.get('imap_port'))}</p>
    </section>

    {status_note}

    <section class="card config-card">
      <div class="card-head">
        <h2>MCP 连接配置</h2>
        <p>所有用户共用同一个端点，靠下面的 Bearer Token 区分实例。请像密码一样保管它。</p>
      </div>
      <div class="endpoint-row">
        <div>
          <span class="mini-label">MCP URL</span>
          <code class="endpoint">{e(_mcp_url())}</code>
        </div>
        <span class="status-badge status-{e(status)}"><span></span> {e(status_labels.get(status, status))}</span>
      </div>
      <div class="token-box">
        <span class="mini-label">Bearer Token</span>
        <code id="mcp-token">{e(token)}</code>
        <button class="btn btn-ghost btn-small copy-btn" type="button" data-copy="mcp-token">复制</button>
      </div>
      <p class="fineprint">同步文件夹：{e(_folder_summary(instance))}　·　想调整？用同一邮箱和同一授权码重新提交，勾选新的文件夹即可；Token 不会变。</p>

      <div class="tabs" role="tablist">
        {''.join(tabs)}
      </div>
      {''.join(panels)}
      <p class="fineprint">配置包含访问令牌。不要截图分享，不要提交到 Git 仓库。若泄露，请用新的授权码重新提交以轮换令牌。</p>
    </section>

    <section class="card card-muted">
      <h3>之后如何再次查看？</h3>
      <p>用同一个邮箱和同一个授权码重新提交，就会直接返回这份配置。容器和数据库不会重建。</p>
      <div class="button-row">
        <a class="btn btn-secondary" href="/provision">返回，再次输入</a>
        <a class="btn btn-ghost" href="/">回到首页</a>
      </div>
    </section>"""
    return layout("MCP 配置", body, show_logout=True, csrf=csrf, wide=True)


def instance_status_page(
    *,
    instance: dict[str, Any],
    csrf: str,
    message: str = "",
    message_kind: str = "info",
) -> str:
    masked = _mask_email(instance.get("email", ""))
    status = str(instance.get("status", "unknown"))
    labels = {
        "running": "运行中",
        "provisioning": "创建中",
        "updating": "更新中",
        "failed": "需要重试",
        "removed": "已移除",
    }
    note = f'<div class="callout callout-{e(message_kind)}"><p>{e(message)}</p></div>' if message else ""
    error = ""
    if instance.get("last_error"):
        error = f'<div class="form-error"><strong>最近错误：</strong> {e(instance["last_error"])}</div>'
    body = f"""
    <section class="page-head">
      <a class="back" href="/provision">← 返回</a>
      <h1>实例状态</h1>
      <p>{e(masked)} · <span class="status-badge status-{e(status)}"><span></span> {e(labels.get(status, status))}</span></p>
    </section>
    {note}
    {error}
    <section class="card">
      <div class="card-head">
        <h2>同步范围</h2>
        <p>当前同步：{e(_folder_summary(instance))}</p>
      </div>
      <p class="help">想修改？用同一个邮箱和同一授权码重新提交，在「同步哪些文件夹」里重新勾选即可。只改文件夹不会轮换 MCP Token，客户端配置不用动。</p>
    </section>
    <section class="card">
      <div class="card-head">
        <h2>重新获取 MCP 配置</h2>
        <p>为保护 Token，配置只在提交凭据后的短时间内显示。请再次输入同一个邮箱和授权码。</p>
      </div>
      <form method="post" action="/provision" class="stack" autocomplete="off">
        <input type="hidden" name="csrf" value="{e(csrf)}">
        <label class="field">
          <span class="label">邮箱地址</span>
          <input class="input" type="email" name="email" value="" placeholder="{e(masked)}" required
                 autocomplete="off" spellcheck="false" autocapitalize="none">
        </label>
        <label class="field">
          <span class="label">IMAP 授权码</span>
          <input class="input" type="password" name="password" required autocomplete="new-password">
        </label>
        <button class="btn btn-primary" type="submit">验证并查看配置</button>
      </form>
    </section>"""
    return layout("实例状态", body, show_logout=True, csrf=csrf)


def error_page(
    *,
    title: str = "出错了",
    message: str = "",
    hint: str = "",
    csrf: str = "",
    show_logout: bool = False,
) -> str:
    hint_html = f'<p class="muted">{e(hint)}</p>' if hint else ""
    body = f"""
    <section class="page-head">
      <a class="back" href="/">← 回到首页</a>
      <h1>{e(title)}</h1>
    </section>
    <section class="card">
      <div class="form-error" role="alert">{e(message)}</div>
      {hint_html}
      <div class="button-row">
        <a class="btn btn-primary" href="/provision">重新填写</a>
        <a class="btn btn-ghost" href="/">首页</a>
      </div>
    </section>"""
    return layout(title, body, show_logout=show_logout, csrf=csrf)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _mcp_url() -> str:
    # The public origin is injected by the caller through a module global to
    # avoid threading it through every template function.
    return _PUBLIC_URL + "/mcp"


_PUBLIC_URL = ""


def set_public_url(url: str) -> None:
    global _PUBLIC_URL
    _PUBLIC_URL = url.rstrip("/")


def _mask_email(email: str) -> str:
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = local[0] + "*"
    else:
        masked_local = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked_local}@{domain}"


def _folder_summary(instance: dict[str, Any]) -> str:
    """Human readable description of the instance's sync scope."""
    raw = instance.get("folders")
    if raw in (None, ""):
        return "部署默认（未单独选择）"
    try:
        folders = json.loads(str(raw))
    except (TypeError, ValueError):
        return "部署默认（未单独选择）"
    if not isinstance(folders, list):
        return "部署默认（未单独选择）"
    if not folders:
        return "全部文件夹"
    names = [str(name) for name in folders]
    shown = "、".join(names[:6])
    if len(names) > 6:
        shown += f" 等 {len(names)} 个"
    elif len(names) > 1:
        shown += f"（{len(names)} 个）"
    return shown


def _config_snippets(token: str) -> list[tuple[str, str, str]]:
    url = _mcp_url()
    auth = f"Bearer {token}"
    claude = {
        "mcpServers": {
            "mail_shadow": {
                "url": url,
                "headers": {"Authorization": auth},
            }
        }
    }
    hermes = (
        "mail-shadow:\n"
        f"  url: {url}\n"
        "  headers:\n"
        f"    Authorization: {auth}\n"
    )
    openclaw = {
        "mcp": {
            "servers": {
                "mail_shadow": {
                    "url": url,
                    "transport": "streamable-http",
                    "headers": {"Authorization": auth},
                }
            }
        }
    }
    curl = (
        f"curl -s {url} \\\n"
        f"  -H 'Authorization: {auth}' \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        "  -H 'Accept: application/json, text/event-stream' \\\n"
        "  -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\","
        "\"params\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{},"
        "\"clientInfo\":{\"name\":\"curl\",\"version\":\"1\"}}}'\n"
    )
    return [
        ("Claude / Cursor", "JSON", json.dumps(claude, indent=2, ensure_ascii=False)),
        ("Hermes", "YAML", hermes),
        ("OpenClaw", "JSON", json.dumps(openclaw, indent=2, ensure_ascii=False)),
        ("cURL 测试", "Shell", curl),
    ]
