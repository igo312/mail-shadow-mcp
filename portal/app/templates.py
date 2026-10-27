"""Server-rendered HTML.  No template engine, no third-party dependencies."""

from __future__ import annotations

import html
import json
import time
from datetime import datetime, timezone
from typing import Any

from .providers import PROVIDERS
from .stats import human_bytes, human_count


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
    nav: str = "",
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
    {nav}
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
        "stopped": "已暂停",
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
        "stopped": "已暂停",
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
# admin console
# ---------------------------------------------------------------------------
_ADMIN_TABS = (
    ("overview", "概览", "/admin"),
    ("invites", "邀请码", "/admin/invites"),
    ("audit", "审计日志", "/admin/audit"),
)


def _admin_nav(active: str, csrf: str) -> str:
    links = "".join(
        f'<a class="admin-tab{" active" if key == active else ""}" href="{href}">{label}</a>'
        for key, label, href in _ADMIN_TABS
    )
    return f"""
    <nav class="admin-nav">
      <div class="admin-tabs">{links}</div>
      <div class="admin-nav-actions">
        <form method="post" action="/admin/refresh" class="inline-form">
          <input type="hidden" name="csrf" value="{e(csrf)}">
          <button class="btn btn-ghost btn-small" type="submit">重新采集</button>
        </form>
        <form method="post" action="/admin/logout" class="inline-form">
          <input type="hidden" name="csrf" value="{e(csrf)}">
          <button class="btn btn-ghost btn-small" type="submit">退出管理</button>
        </form>
      </div>
    </nav>"""


def _admin_message(message: str, kind: str = "ok") -> str:
    if not message:
        return ""
    kind = kind if kind in {"ok", "warn", "error", "info"} else "info"
    title = {"ok": "完成", "warn": "注意", "error": "失败", "info": "提示"}[kind]
    return f'<div class="callout callout-{kind}"><strong>{title}</strong><p>{e(message)}</p></div>'


def admin_login_page(*, error: str = "") -> str:
    error_html = f'<div class="form-error" role="alert">{e(error)}</div>' if error else ""
    body = f"""
    <section class="page-head">
      <div class="eyebrow"><span class="pulse"></span> 运营控制台</div>
      <h1>管理登录</h1>
      <p>查看邮箱数量、同步文件夹与数据量，并启动 / 停止实例。</p>
    </section>
    <section class="card card-narrow">
      {error_html}
      <form method="post" action="/admin/login" class="stack" autocomplete="off">
        <label class="field">
          <span class="label">管理密码</span>
          <input class="input" type="password" name="password" required autofocus
                 autocomplete="current-password" spellcheck="false">
        </label>
        <button class="btn btn-primary" type="submit">进入控制台</button>
      </form>
      <p class="fineprint">连续输错会按来源 IP 限速并记入审计日志；管理会话到期后需要重新登录。</p>
    </section>"""
    return layout("管理登录", body)


def admin_dashboard(
    *,
    snapshot: dict[str, Any],
    csrf: str,
    message: str = "",
    message_kind: str = "ok",
    stats_interval: int = 300,
) -> str:
    entries = sorted(
        (snapshot.get("instances") or {}).values(),
        key=lambda item: str(item.get("email") or ""),
    )
    total = len(entries)
    running = sum(1 for item in entries if _badge(item)[0] == "running")
    paused = sum(1 for item in entries if str(item.get("status")) == "stopped")
    broken = total - running - paused
    folder_total = sum(int(item.get("folder_count") or 0) for item in entries)
    messages = sum(int(item.get("messages") or 0) for item in entries)
    total_bytes = sum(int(item.get("total_bytes") or 0) for item in entries)
    growth_messages = 0
    growth_bytes = 0
    for item in entries:
        growth = item.get("growth") or {}
        growth_messages += int(growth.get("messages") or 0)
        growth_bytes += int(growth.get("bytes") or 0)
    pending_total = sum(int(item.get("pending") or 0) for item in entries)
    host = snapshot.get("host") or {}
    generated = int(snapshot.get("generated_at") or 0)

    rows = "".join(_admin_row(item, csrf) for item in entries)
    if not rows:
        rows = '<tr><td colspan="8" class="empty">还没有任何邮箱实例。</td></tr>'

    disk = human_bytes(host.get("disk_free")) if host.get("disk_free") else "—"
    memory = "—"
    if host.get("mem_total"):
        used = int(host["mem_total"]) - int(host.get("mem_available") or 0)
        memory = f"{human_bytes(used)} / {human_bytes(host['mem_total'])}"
    load = "—" if host.get("load1") is None else f"{host['load1']:.2f}"
    cpu_count = host.get("cpu_count") or 0
    collected = f"{_ago(generated)}（每 {int(stats_interval)} 秒自动采集）" if generated else "首次采集进行中…"

    body = f"""
    <section class="page-head">
      <h1>控制台</h1>
      <p>采集于 {e(collected)} · 数据来自各实例那本只读的影子数据库</p>
    </section>
    {_admin_message(message, message_kind)}
    {_admin_message(str(snapshot.get("error") or ""), "warn")}
    <div class="kpi-grid">
      <div class="kpi"><span>邮箱</span><strong>{total}</strong>
        <small>{running} 运行中 · {paused} 已暂停 · {broken} 异常</small></div>
      <div class="kpi"><span>影子库文件夹</span><strong>{folder_total}</strong>
        <small>{'平均 %.1f 个 / 邮箱' % (folder_total / total) if total else '—'} · 含历史同步过的文件夹</small></div>
      <div class="kpi"><span>邮件总数</span><strong>{human_count(messages)}</strong>
        <small>待拉取 UID 差 {pending_total}</small></div>
      <div class="kpi"><span>影子数据</span><strong>{human_bytes(total_bytes)}</strong>
        <small>近 24h {human_count(growth_messages)} 封 / {human_bytes(growth_bytes)}</small></div>
      <div class="kpi"><span>主机</span><strong>{e(load)}</strong>
        <small>负载 / {cpu_count} 核 · 内存 {memory} · 磁盘剩余 {disk}</small></div>
    </div>

    <section class="card">
      <div class="card-head">
        <h2>邮箱实例</h2>
        <p>「待拉取」是各文件夹最新 UID 与已同步 UID 的差值，用来判断同步是否落后。</p>
      </div>
      <div class="table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>邮箱</th>
              <th>状态</th>
              <th class="num">同步文件夹</th>
              <th class="num">邮件</th>
              <th class="num">占用</th>
              <th>最新邮件</th>
              <th class="num">待拉取</th>
              <th></th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <p class="fineprint">占用 = 影子数据库 + WAL + 附件目录。邮件数与文件夹数只统计已同步进影子库的内容。</p>
    </section>

    <section class="card card-muted">
      <h3>这里能做什么</h3>
      <ul class="ticks">
        <li>查看每个邮箱同步了几个文件夹、同步了多少封邮件、占多少空间。</li>
        <li>启动 / 停止 / 重启单个实例；删除实例需要输入邮箱地址确认。</li>
        <li>发放和停用邀请码，查看最近的登录、开通、异常审计记录。</li>
      </ul>
    </section>"""
    return layout("控制台", body, csrf=csrf, wide=True, nav=_admin_nav("overview", csrf))


def admin_instance_page(
    *,
    instance: dict[str, Any],
    entry: dict[str, Any],
    csrf: str,
    logs: str = "",
    message: str = "",
    message_kind: str = "ok",
) -> str:
    kind, label = _badge(entry)
    configured = entry.get("configured") or {}
    if configured.get("source") == "deployment":
        defaults = [str(name) for name in configured.get("default") or []]
        scope = (
            f"部署默认：{'、'.join(defaults)}"
            if defaults
            else "部署默认：全部文件夹"
        )
    elif configured.get("all"):
        scope = "全部文件夹"
    else:
        names = [str(name) for name in configured.get("names") or []]
        scope = "、".join(names) if names else "部署默认"

    folder_rows = "".join(
        f"""<tr>
              <td class="cell-main">{e(folder['name'])}</td>
              <td class="num">{int(folder['messages']):,}</td>
              <td class="num">{int(folder['newest_uid']):,}</td>
              <td class="num">{int(folder['synced_uid']):,}</td>
              <td class="num">{_pending_cell(int(folder['pending']))}</td>
              <td>{'已完成' if folder['cjk_done'] else '<span class="warn-text">补全中</span>'}</td>
            </tr>"""
        for folder in entry.get("folders") or []
    ) or '<tr><td colspan="6" class="empty">还没有同步任何文件夹。</td></tr>'

    growth = entry.get("growth") or {}
    growth_text = "—"
    if growth.get("messages") is not None:
        growth_text = f"+{human_count(growth['messages'])} 封 / +{human_bytes(growth['bytes'])}"
    elif entry.get("growth") is not None:
        growth_text = "历史还在积累"

    read_error = ""
    if not entry.get("ok"):
        read_error = _admin_message(str(entry.get("error") or "影子数据读取失败"), "warn")
    if entry.get("log_error"):
        read_error += _admin_message(f"容器最近报错：{entry['log_error']}", "error")

    state = str(entry.get("container_state") or "")
    if state == "running":
        power = """
        <form method="post" action="/admin/action" class="inline-form">
          <input type="hidden" name="csrf" value="{csrf}">
          <input type="hidden" name="instance" value="{instance_id}">
          <input type="hidden" name="action" value="stop">
          <button class="btn btn-secondary btn-small" type="submit">停止实例</button>
        </form>
        <form method="post" action="/admin/action" class="inline-form">
          <input type="hidden" name="csrf" value="{csrf}">
          <input type="hidden" name="instance" value="{instance_id}">
          <input type="hidden" name="action" value="restart">
          <button class="btn btn-ghost btn-small" type="submit">重启容器</button>
        </form>"""
    else:
        power = """
        <form method="post" action="/admin/action" class="inline-form">
          <input type="hidden" name="csrf" value="{csrf}">
          <input type="hidden" name="instance" value="{instance_id}">
          <input type="hidden" name="action" value="start">
          <button class="btn btn-primary btn-small" type="submit">启动实例</button>
        </form>
        <form method="post" action="/admin/action" class="inline-form">
          <input type="hidden" name="csrf" value="{csrf}">
          <input type="hidden" name="instance" value="{instance_id}">
          <input type="hidden" name="action" value="restart">
          <button class="btn btn-ghost btn-small" type="submit">重启容器</button>
        </form>"""
    power = power.replace("{csrf}", e(csrf)).replace("{instance_id}", e(str(instance["id"])))

    body = f"""
    <section class="page-head">
      <a class="back" href="/admin">← 控制台</a>
      <h1>{e(instance['email'])}</h1>
      <p>
        <span class="status-badge status-{kind}"><span></span> {e(label)}</span>
        <span class="badge-gap"></span>
        {e(entry.get('provider_name') or 'IMAP')} · {e(instance.get('imap_host'))}:{e(instance.get('imap_port'))}
        · 容器 <code>{e(instance.get('container_name'))}</code>
      </p>
    </section>
    {_admin_message(message, message_kind)}
    {read_error}
    <div class="kpi-grid kpi-grid-3">
      <div class="kpi"><span>邮件</span><strong>{human_count(entry.get('messages'))}</strong>
        <small>最近 24h {e(growth_text)}</small></div>
      <div class="kpi"><span>影子数据</span><strong>{human_bytes(entry.get('total_bytes'))}</strong>
        <small>库 {human_bytes(entry.get('db_bytes'))} · WAL {human_bytes(entry.get('wal_bytes'))} · 附件 {human_bytes(entry.get('attachment_disk_bytes'))}</small></div>
      <div class="kpi"><span>文件夹</span><strong>{int(entry.get('folder_count') or 0)}</strong>
        <small>待拉取 {int(entry.get('pending') or 0)} · 隔离 {int(entry.get('skipped') or 0)}</small></div>
    </div>

    <section class="card">
      <div class="card-head">
        <h2>同步文件夹明细</h2>
        <p>配置范围：{e(scope)}。实际写入影子库的文件夹见下表。</p>
      </div>
      <div class="table-wrap">
        <table class="data">
          <thead><tr>
            <th>文件夹</th><th class="num">邮件</th><th class="num">最新 UID</th>
            <th class="num">已同步 UID</th><th class="num">待拉取</th><th>CJK 索引</th>
          </tr></thead>
          <tbody>{folder_rows}</tbody>
        </table>
      </div>
      <p class="fineprint">「待拉取」= 最新 UID − 已同步 UID，仅供参考：删除邮件或 UIDVALIDITY 变化也会让它不为零。</p>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>运行状态</h2>
        <p>{e(entry.get('container_status') or '容器状态未知')} · 镜像 <code>{e(entry.get('image') or '—')}</code></p>
      </div>
      <div class="kv-grid">
        <div><span>CPU</span><strong>{float(entry.get('cpu_percent') or 0):.1f}%</strong></div>
        <div><span>内存</span><strong>{human_bytes(entry.get('mem_bytes'))} / {human_bytes(entry.get('mem_limit'))}</strong></div>
        <div><span>网络 收/发</span><strong>{human_bytes(entry.get('net_rx'))} / {human_bytes(entry.get('net_tx'))}</strong></div>
        <div><span>最新邮件</span><strong>{_date(entry.get('newest_mail'))}</strong></div>
        <div><span>最早邮件</span><strong>{_date(entry.get('oldest_mail'))}</strong></div>
        <div><span>最近写入</span><strong>{_ago(entry.get('last_write_at'))}</strong></div>
        <div><span>创建时间</span><strong>{_ts(instance.get('created_at'))}</strong></div>
        <div><span>附件</span><strong>{int(entry.get('attachment_disk_files') or 0)} 个 · {human_bytes(entry.get('attachment_disk_bytes'))}</strong></div>
      </div>
      <div class="button-row">{power}</div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>容器日志</h2>
        <p>最近 80 行，含同步与错误信息。</p>
      </div>
      <details class="advanced">
        <summary>展开日志</summary>
        <pre class="log">{e(logs or '（暂无日志）')}</pre>
      </details>
    </section>

    <section class="card card-danger">
      <div class="card-head">
        <h2>删除实例</h2>
        <p>删除容器和两个数据卷（影子库会被永久删除），并标记该邮箱为已移除。需要输入邮箱地址确认。</p>
      </div>
      <form method="post" action="/admin/action" class="stack">
        <input type="hidden" name="csrf" value="{e(csrf)}">
        <input type="hidden" name="instance" value="{e(str(instance['id']))}">
        <input type="hidden" name="action" value="remove">
        <label class="field">
          <span class="label">输入 <code>{e(instance['email'])}</code> 以确认</span>
          <input class="input" type="text" name="confirm" placeholder="{e(instance['email'])}"
                 autocomplete="off" spellcheck="false" autocapitalize="none">
        </label>
        <button class="btn btn-danger btn-small" type="submit">永久删除</button>
      </form>
    </section>"""
    return layout(
        f"实例 · {instance['email']}",
        body,
        csrf=csrf,
        wide=True,
        nav=_admin_nav("overview", csrf),
    )


def admin_invites_page(
    *,
    invites: list[dict[str, Any]],
    csrf: str,
    new_code: str = "",
    message: str = "",
    message_kind: str = "ok",
) -> str:
    code_box = ""
    if new_code:
        code_box = f"""
        <div class="callout callout-ok">
          <strong>邀请码已创建，只显示这一次</strong>
          <p>把这个码发给对方；关闭页面后无法再次查看，只能停用后重新生成。</p>
          <div class="token-box">
            <span class="mini-label">邀请码</span>
            <code id="new-invite-code">{e(new_code)}</code>
            <button class="btn btn-ghost btn-small copy-btn" type="button" data-copy="new-invite-code">复制</button>
          </div>
        </div>"""

    rows = ""
    for invite in invites:
        expires = "永久" if not invite.get("expires_at") else _ts(invite["expires_at"])
        disabled = bool(invite.get("disabled"))
        left = max(0, int(invite["max_uses"]) - int(invite["uses"]))
        status = (
            '<span class="status-badge status-failed"><span></span> 已停用</span>'
            if disabled
            else (
                '<span class="status-badge status-running"><span></span> 可用</span>'
                if left > 0
                else '<span class="status-badge status-removed"><span></span> 已用完</span>'
            )
        )
        disable = ""
        if not disabled:
            disable = f"""
            <form method="post" action="/admin/invites/disable" class="inline-form">
              <input type="hidden" name="csrf" value="{e(csrf)}">
              <input type="hidden" name="invite" value="{e(invite['id'])}">
              <button class="btn btn-ghost btn-small" type="submit">停用</button>
            </form>"""
        rows += f"""<tr>
          <td class="cell-main">{e(invite.get('label') or '（无标签）')}
            <div class="cell-sub">{e(invite['id'])}</div></td>
          <td class="num">{int(invite['uses'])} / {int(invite['max_uses'])}</td>
          <td>{e(expires)}</td>
          <td>{status}</td>
          <td class="num">{int(invite.get('mailbox_count') or 0)}</td>
          <td class="nowrap">{_ts(invite.get('last_used_at'))}</td>
          <td class="row-actions">{disable}</td>
        </tr>"""
    if not rows:
        rows = '<tr><td colspan="7" class="empty">还没有邀请码。</td></tr>'

    body = f"""
    <section class="page-head">
      <h1>邀请码</h1>
      <p>邀请码决定谁能开通邮箱实例；每个码有独立使用次数和有效期。</p>
    </section>
    {_admin_message(message, message_kind)}
    {code_box}
    <div class="grid-2 admin-grid">
      <section class="card">
        <div class="card-head">
          <h2>现有邀请码</h2>
          <p>「邮箱数」是使用该邀请码开通且仍存在的实例数量。</p>
        </div>
        <div class="table-wrap">
          <table class="data">
            <thead><tr>
              <th>标签</th><th class="num">已用</th><th>到期</th><th>状态</th>
              <th class="num">邮箱数</th><th>最近使用</th><th></th>
            </tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
      </section>
      <aside class="stack-aside">
        <section class="card">
          <div class="card-head">
            <h2>创建邀请码</h2>
            <p>生成后只显示一次，请立刻复制。</p>
          </div>
          <form method="post" action="/admin/invites/create" class="stack">
            <input type="hidden" name="csrf" value="{e(csrf)}">
            <label class="field">
              <span class="label">备注标签</span>
              <input class="input" type="text" name="label" maxlength="80"
                     placeholder="例如：张伟 / 市场部" autocomplete="off">
            </label>
            <div class="field-row">
              <label class="field">
                <span class="label">可用次数</span>
                <input class="input" type="number" name="uses" value="1" min="1" max="1000">
              </label>
              <label class="field">
                <span class="label">有效天数</span>
                <input class="input" type="number" name="days" value="30" min="0" max="3650">
              </label>
            </div>
            <p class="help">有效天数填 0 表示永不过期。</p>
            <button class="btn btn-primary" type="submit">生成邀请码</button>
          </form>
        </section>
      </aside>
    </div>"""
    return layout("邀请码", body, csrf=csrf, wide=True, nav=_admin_nav("invites", csrf))


def admin_audit_page(*, events: list[dict[str, Any]], csrf: str) -> str:
    rows = ""
    for event in events:
        detail = str(event.get("detail") or "")
        if len(detail) > 160:
            detail = detail[:160] + "…"
        rows += f"""<tr>
          <td class="nowrap">{_ts(event['at'])}</td>
          <td><code>{e(event['action'])}</code></td>
          <td class="nowrap">{e(event.get('ip') or '')}</td>
          <td class="cell-main">{e(event.get('email') or '')}</td>
          <td class="cell-sub">{e(detail)}</td>
        </tr>"""
    if not rows:
        rows = '<tr><td colspan="5" class="empty">还没有审计记录。</td></tr>'
    body = f"""
    <section class="page-head">
      <h1>审计日志</h1>
      <p>最近 200 条：邀请码、开通、登录、管理操作与异常。</p>
    </section>
    <section class="card">
      <div class="table-wrap">
        <table class="data">
          <thead><tr><th>时间</th><th>动作</th><th>IP</th><th>邮箱</th><th>详情</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </section>"""
    return layout("审计日志", body, csrf=csrf, wide=True, nav=_admin_nav("audit", csrf))


def _badge(entry: dict[str, Any]) -> tuple[str, str]:
    """Map an instance to (css suffix, label) using the *live* container state."""
    status = str(entry.get("status") or "")
    state = str(entry.get("container_state") or "")
    if state == "running":
        return "running", "运行中"
    if status == "stopped":
        return "stopped", "已暂停"
    if status == "removed":
        return "removed", "已移除"
    if status in {"provisioning", "updating"}:
        return status, {"provisioning": "创建中", "updating": "更新中"}[status]
    if state == "missing" and status != "failed":
        return "failed", "容器缺失"
    if status == "failed":
        return "failed", "需要重试"
    return "failed", "已停止"


def _admin_row(entry: dict[str, Any], csrf: str) -> str:
    kind, label = _badge(entry)
    instance_id = e(str(entry.get("id") or ""))
    folder_count = int(entry.get("folder_count") or 0)
    configured = entry.get("configured") or {}
    if configured.get("source") == "deployment":
        scope_main = "默认"
        defaults = [str(name) for name in configured.get("default") or []]
        scope_names = (
            "、".join(defaults[:3]) + ("（部署默认）" if defaults else "")
            if defaults
            else "全部文件夹（部署默认）"
        )
        scope_count: int | None = None
    elif configured.get("all"):
        scope_main = "全部"
        scope_names = ""
        scope_count = None
    else:
        names = [str(name) for name in configured.get("names") or []]
        scope_main = str(len(names))
        scope_names = "、".join(names[:3]) + (" 等" if len(names) > 3 else "")
        scope_count = len(names)
    extra = ""
    if scope_count is None or folder_count != scope_count:
        extra = f'<div class="cell-sub">影子库 {folder_count} 个</div>'
    error = ""
    if not entry.get("ok"):
        error = f'<div class="cell-sub cell-error">{e(entry.get("error") or "读取失败")}</div>'
    if entry.get("log_error"):
        error += f'<div class="cell-sub cell-error">日志错误：{e(entry["log_error"])}</div>'

    growth = entry.get("growth") or {}
    growth_text = ""
    if growth.get("messages") is not None:
        growth_text = f"+{human_count(growth['messages'])} 封 / 24h"
    elif entry.get("growth") is not None:
        growth_text = "积累中"
    if str(entry.get("container_state")) == "running":
        power = (
            f'<input type="hidden" name="action" value="stop">'
            f'<button class="btn btn-ghost btn-small" type="submit">停止</button>'
        )
    else:
        power = (
            f'<input type="hidden" name="action" value="start">'
            f'<button class="btn btn-ghost btn-small" type="submit">启动</button>'
        )
    return f"""
          <tr>
            <td class="cell-main"><a href="/admin/instance/{instance_id}">{e(entry.get('email'))}</a>
              <div class="cell-sub">{e(entry.get('provider_name') or 'IMAP')} · {e(entry.get('imap_host'))}</div>
              {error}</td>
            <td><span class="status-badge status-{kind}"><span></span> {e(label)}</span></td>
            <td class="num">{e(scope_main)}
              <div class="cell-sub">{e(scope_names)}</div>{extra}</td>
            <td class="num">{human_count(entry.get('messages'))}
              <div class="cell-sub">{e(growth_text)}</div></td>
            <td class="num">{human_bytes(entry.get('total_bytes'))}</td>
            <td class="nowrap">{_date(entry.get('newest_mail'))}</td>
            <td class="num">{_pending_cell(int(entry.get('pending') or 0))}</td>
            <td class="row-actions">
              <div class="action-row">
                <a class="btn btn-ghost btn-small" href="/admin/instance/{instance_id}">详情</a>
                <form method="post" action="/admin/action" class="inline-form">
                  <input type="hidden" name="csrf" value="{e(csrf)}">
                  <input type="hidden" name="instance" value="{instance_id}">
                  {power}
                </form>
              </div>
            </td>
          </tr>"""


def _pending_cell(value: int) -> str:
    return f'<span class="warn-text">{value:,}</span>' if value else "0"


def _date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        stamp = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return e(text[:19])
    return e(stamp.astimezone().strftime("%Y-%m-%d %H:%M"))


def _ts(value: Any) -> str:
    try:
        stamp = int(value or 0)
    except (TypeError, ValueError):
        return "—"
    if stamp <= 0:
        return "—"
    return e(time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)))


def _ago(value: Any) -> str:
    try:
        stamp = int(value or 0)
    except (TypeError, ValueError):
        return "—"
    if stamp <= 0:
        return "—"
    delta = max(0, int(time.time()) - stamp)
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{delta // 60} 分钟前"
    if delta < 86400:
        return f"{delta // 3600} 小时前"
    return f"{delta // 86400} 天前"


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
