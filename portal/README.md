# Mail Shadow MCP — 邀请制托管 portal

把一个 [mail-shadow-mcp](../README.md) 实例托管给多个用户：用户输入邀请码 → 填写邮箱和 IMAP 授权码 → portal 验证登录 → 启动一个隔离的 Docker 容器 → 返回专属 MCP 配置。

整个服务只暴露 **一个公网端口**。

---

## 先回答：同一个端口，还是每个实例一个端口？

**推荐同一个端口。** 不需要为每个用户开不同端口。

所有用户共用：

```
https://mcp.example.com/mcp
Authorization: Bearer <每个用户不同的 token>
```

portal 的网关收到请求后：

1. 取 `Authorization: Bearer <token>`；
2. 计算 token 的 SHA-256；
3. 在控制数据库里查到对应的实例；
4. 把请求转发到该实例的 MCP 端口：
   - portal 在 Docker 内（`MSM_BACKEND_MODE=network`）：`msm-<instance-id>:8080`；
   - portal 直接跑在宿主机（`MSM_BACKEND_MODE=port`）：`127.0.0.1:<Docker 分配的随机 loopback 端口>`。

用户之间靠 **Bearer token** 区分，而不是靠端口或路径。`port` 模式也只会把实例端口发布到 `127.0.0.1`，公网仍然只有 portal 的一个端口。

| 方案 | 优点 | 问题 |
|---|---|---|
| **单端口 + Bearer token 路由（本实现）** | 一个 TLS 证书、一个防火墙规则；容器不暴露；可集中限流/吊销/审计；扩容不受端口数量限制 | 需要一个小型网关（已实现） |
| 每实例一个**公网**宿主端口（`-p 18001:8080`） | 实现最简单 | 端口很快耗尽；每个端口都要防火墙/NAT；TLS 难处理；容器直接暴露；无法集中吊销 |
| 单端口 + 路径前缀 `/i/<id>/mcp` | 可以用普通反向代理 | 每个用户 URL 不同；MCP 客户端对路径处理不一；路径会进日志；仍要自己处理前缀重写 |

> 注意区分两个“密码”：
> - **IMAP 授权码 / 应用专用密码**：用户邮箱的凭据，只用于同步邮件。
> - **MCP Bearer Token**：portal 生成给 AI 客户端的密码。二者完全不同。

### 和“直接 docker run 一个 MCP 容器”有什么区别？

以前单用户的做法通常是：

```text
docker run -p 60001:8080 ...
→ http://IP:60001/mcp + 这个容器自己的 Bearer token
```

现在 portal 接管了“启动容器”和“公网入口”两件事：

```text
portal 监听 IP:60001
├── /        邀请码 / 表单 / 结果页
└── /mcp     按 Bearer token 查实例并转发
       ├── token A → msm-<id-A>:8080（内部 loopback 随机端口）
       └── token B → msm-<id-B>:8080（内部 loopback 随机端口）
```

所以：

- **公网只有一个端口 60001**；
- 所有用户的 MCP URL 都是 `http://IP:60001/mcp`；
- 不同用户靠 `Authorization: Bearer <不同 token>` 区分；
- 每个 token 只指向一个实例，不能访问别人的实例；
- 每个实例内部的端口是 Docker 分配的 loopback 随机端口，不对外；
- 结果页会自动给出该用户自己的 token 和 MCP 配置。

如果你只有一个固定邮箱、也不需要邀请码和自助创建，仍然可以继续用原来的 `docker run -p 60001:8080` 方式；portal 是为了多用户/自助开通而加的。

### `mcp.example.com` 是什么？可以直接用 IP 吗？

`mcp.example.com` 只是文档里的**占位域名**，不是真实域名。替换成你自己的域名或公网 IP 即可。

- 用域名 + HTTPS（推荐）：`https://你的域名/mcp`
- 直接用 IP + HTTP：`http://203.0.113.10:8080/mcp`
- 用 IP + 反向代理到 80 端口：`http://203.0.113.10/mcp`

IP 直连是可行的，但有一个关键限制：**公网 CA 通常不为裸 IP 签发受信任的 HTTPS 证书**。因此：

| 场景 | 可用 URL | 说明 |
|---|---|---|
| 公网 + 域名 + Caddy | `https://mcp.example.com/mcp` | 推荐，自动 Let's Encrypt 证书 |
| 内网 / VPN / Tailscale | `http://192.168.1.10:8080/mcp` | 可以，但要确保网络本身可信 |
| 公网 + 纯 IP + HTTP | `http://203.0.113.10:8080/mcp` | 能用，但 Token 和邮箱授权码会明文传输，**不建议** |
| 公网 + 纯 IP + HTTPS | `https://203.0.113.10/mcp` | 需要自签证书并让每个 MCP 客户端信任，较麻烦 |

如果暂时没有域名，又需要公网访问，比较省事的做法是 Cloudflare Tunnel、Tailscale Funnel、ngrok 等，它们会给你一个 HTTPS 域名，不需要自己买域名或开 80/443。

用 IP 直连时，portal 这样配：

```bash
export MSM_PUBLIC_URL="http://203.0.113.10:8080"
export MSM_SECURE_COOKIES="0"
export MSM_LISTEN_HOST="0.0.0.0"
export MSM_LISTEN_PORT="8080"
export MSM_BACKEND_MODE="port"
python -m app
```

MCP 客户端里就填：

```json
{
  "mcpServers": {
    "mail_shadow": {
      "url": "http://203.0.113.10:8080/mcp",
      "headers": { "Authorization": "Bearer <你的 token>" }
    }
  }
}
```

注意：

- `MSM_PUBLIC_URL` 必须和用户实际访问的 scheme/host/port 完全一致；结果页的 MCP URL 是它拼出来的，不是写死的。
- 用 `http://` 时 `MSM_SECURE_COOKIES` 必须是 `0`，否则浏览器不会带会话 Cookie。
- 用 `https://` 时保持 `1`。
- 端口不是 8080 时，URL 里要带端口；经过 Caddy/Nginx 转到 80/443 时，URL 里不用带内部端口。
- 直接暴露 `:8080` 时记得在防火墙/安全组放行该端口；如果前面有 Caddy/Nginx，portal 只监听 `127.0.0.1` 更安全。

---

## 工作方式

```
浏览器 ──HTTPS──▶ Caddy ──▶ portal :8080
                              ├── /               邀请码 → 邮箱表单 → 结果页
                              ├── /mcp            Bearer token 路由网关
                              └── /healthz        健康检查

portal ──Docker socket / CLI──▶ Docker
                                   ├── msm-<id> 容器
                                   │     ├─ network 模式：msm-net:8080（无发布端口）
                                   │     └─ port 模式：127.0.0.1:<随机端口>（仅 loopback）
                                   ├── msm-config-<id> 卷（config.yaml，只读挂载）
                                   └── msm-data-<id> 卷（SQLite + 附件，持久化）
```

### 用户流程

1. 打开首页，输入邀请码。邀请码一次性写入 HttpOnly 会话 Cookie。
2. 选择邮箱服务商（阿里企业邮箱需要手动选「阿里企业邮箱」），填写邮箱、IMAP 授权码；可展开高级设置填写自定义 IMAP host/port/TLS。
3. 可选：点「获取文件夹列表」，portal 用一次只读 IMAP 连接列出全部文件夹（阿里云 100+ 个文件夹约 0.2 秒），勾选要同步的文件夹。不点就沿用该实例当前设置（新实例用 `MSM_SYNC_FOLDERS`）。
4. portal 先用 `imaplib` 做一次只读登录验证：
   - 支持 TLS / STARTTLS；
   - 对 163 / 126 先发送 RFC 2971 `ID`；
   - 优先 `LOGIN`，服务器只支持 `AUTH=PLAIN` 时自动回退；
   - 阿里企业邮箱使用预设 `imap.qiye.aliyun.com:993`。
4. 验证通过后：
   - 生成实例 ID；
   - 创建 `msm-config-<id>` 卷，把 `config.yaml` 写进去（含该邮箱的授权码，文件权限 0600）；
   - 创建 `msm-data-<id>` 卷；
   - 启动 `msm-<id>` 容器；`network` 模式加入 `msm-net`，`port` 模式发布一个 `127.0.0.1` 随机端口；
   - 等待容器内 `/mcp` 返回 401（说明 MCP handler 已就绪）。
5. 返回结果页：MCP URL、Bearer Token、Claude/Cursor、Hermes、OpenClaw、cURL 四份配置，以及当前同步文件夹。

### “相同账户 + 相同授权码直接返回配置”

- 每个邮箱只允许一个实例（`instances.email` 唯一）。
- 幂等键：`HMAC(master_key, "idem" + email + NUL + password)`。
- MCP token：`HMAC(master_key, "mcp-token" + token_salt + NUL + email + NUL + password)`，再 base64url。
- `token_salt` 是每个实例独立的随机值；同一实例、同一凭据会得到同一个 token，修改授权码或删除后重建会轮换 salt，因此旧 token 不会被“恢复”。
- 控制数据库只保存幂等键、salt 和 token 的哈希，**不保存授权码，也不保存明文 token**。token 不从未经加密的幂等键推导，因此即使控制数据库和 master key 一起泄露，也不能直接重建现有 token。
- **一个邀请码可以创建多个实例**：`max_uses` 限制的是邀请码被兑换成会话的次数，不是邮箱数量；同一个会话可以继续添加多个邮箱（受全局 `MSM_MAX_INSTANCES` 限制）。实例通过 `owner_invite` 记录归属，同一邀请码创建的实例都可以在该会话的结果页查看，其他邀请码的会话看不到。
- 同一对 `(email, password)` 再次提交（实例仍存在）：
  - 不会创建第二个容器；
  - 不会重新同步；
  - 直接返回同一份 token 和 MCP 配置。
- **同一对 `(email, password)` + 不同的文件夹选择**：
  - 视为改配置而不是改凭据：**token 不轮换**，已有 MCP 客户端保持可用；
  - 重写 config 并重建容器（几秒内完成），数据卷不动；
  - 结果页会提示「同步文件夹已更新，MCP Token 未变」。
- 同一邮箱、不同授权码：
  - 先验证新授权码能登录；
  - 重写 config、重建容器、轮换 token 和 salt；
  - 旧 MCP 配置立即失效（这是修改授权码后的预期行为）。
- 如果容器被 `docker rm` 了，再次提交同一凭据会从已有的数据卷重新建容器；如果实例被 `instance remove` 过，重建会轮换 token，旧配置不会复活。

### 选择同步文件夹（大邮箱的性能开关）

表单里的「同步哪些文件夹？」是可选的：

- 点「获取文件夹列表」才会连一次 IMAP 并列出全部文件夹；阿里企业邮箱 108 个文件夹实测 **0.18 秒**（connect 0.04 + login 0.12 + LIST 0.02），所以按钮上的转圈只是防呆，正常一次眨眼就结束。
- 不点就完全沿用原行为：新实例用 `MSM_SYNC_FOLDERS`，已存在实例保持原 config。
- 勾选结果写进 `config.yaml` 的 `folders:`；只同步 3 个常用文件夹的 476k 封邮箱，一轮同步从 **19 分钟降到 30~60 秒**，MCP 查询也不再排队超时。
- 服务器返回的是 RFC 3501 modified UTF-7 文件夹名（例如 `&XfJT0ZAB-`），portal 会解码成 `已发送` 再写进 YAML，和同步引擎看到的名称一致。
- 同一邮箱 + 同一授权码重新提交并改文件夹：只重建容器、**不轮换 token**，客户端配置不用改。

### 阿里企业邮箱

阿里企业邮箱使用自定义域名，无法从邮箱后缀自动推断，所以表单里增加了「邮箱服务商」下拉框：

1. 「邮箱服务商」选择 **阿里企业邮箱**；
2. 邮箱地址填完整企业邮箱；
3. IMAP 授权码填客户端专用密码（不是登录密码）；
4. 高级设置不用填，host 会自动使用 `imap.qiye.aliyun.com:993`（TLS）。

如果企业管理员开启了安全策略，还需要在阿里企业邮箱管理后台：

- 开启 IMAP/SMTP 服务；
- 允许第三方客户端登录；
- 必要时生成/重置客户端专用密码。

> 如果选「自动识别」，`user@your-company.com` 这种自定义域名会提示无法识别 IMAP 服务器；这时必须选择「阿里企业邮箱」或在高级设置里手动填 `imap.qiye.aliyun.com` / `993` / `TLS`。

---

## 运行方式

Python 进程**不需要**放进 Docker。可以二选一：

### 方式 A：Python 直接跑在宿主机（推荐给你）

前提：

- Python 3.10+（推荐 3.12；3.10 / 3.12 / 3.14 都跑通了全部测试）；
- **不需要 `pip install`**：portal 只用 Python 标准库（`http.server`、`sqlite3`、`imaplib`、`hmac`、`subprocess`），没有 `requirements.txt`；
- 已安装 Docker Engine / Docker Desktop，`docker` 命令可用；
- 当前用户能访问 Docker socket（Linux 上通常在 `docker` 组，Windows 上 Docker Desktop 默认可用）。

启动：

```bash
cd portal

export MSM_DATA_DIR="./data"
export MSM_PUBLIC_URL="https://mcp.example.com"
export MSM_SECURE_COOKIES="1"
export MSM_LISTEN_HOST="127.0.0.1"
export MSM_BACKEND_MODE="port"
export MSM_TRUST_PROXY="1"
export MSM_IMAGE="ghcr.io/dryas/mail-shadow-mcp:latest"

python -m app
```

Windows PowerShell：

```powershell
cd portal
$env:MSM_DATA_DIR = "D:\msm\data"
$env:MSM_PUBLIC_URL = "https://mcp.example.com"
$env:MSM_SECURE_COOKIES = "1"
$env:MSM_LISTEN_HOST = "127.0.0.1"
$env:MSM_BACKEND_MODE = "port"
$env:MSM_TRUST_PROXY = "1"
$env:MSM_IMAGE = "ghcr.io/dryas/mail-shadow-mcp:latest"
python -m app
```

`MSM_BACKEND_MODE=port` 的行为：

- portal 直接调用宿主机的 Docker；
- 每个实例创建时用 `-p 127.0.0.1::8080` 让 Docker 分配一个**只绑定 loopback 的随机端口**；
- portal 通过 `127.0.0.1:<随机端口>` 转发 `/mcp`；
- **`port`/`ip` 模式每次转发前都会用 `docker inspect` 读取容器当前的端口/IP**，不会使用控制数据库里可能过期的 `host_port`。Docker 会在容器删除后立刻复用宿主机端口，只信数据库会把 A 的 token 转发到 B 的容器；现在如果容器已停止、消失或 `msm.instance` label 不匹配，网关直接返回 502，而不是冒险转发。`network` 模式靠 Docker DNS 和唯一的容器名解析，不经过宿主端口，因此不做这次 inspect；
- 容器使用 Docker 默认 bridge，不需要 `msm-net`；
- 这个随机端口不对外网开放，公网仍然只有 portal 的一个端口；
- Linux、Windows Docker Desktop、macOS Docker Desktop 都可以用。

前面放一个 Caddy/Nginx 反向代理即可：

```caddyfile
mcp.example.com {
    reverse_proxy 127.0.0.1:8080 {
        flush_interval -1
    }
}
```

> `MSM_TRUST_PROXY=1` 只在前方确实有 Caddy/Nginx 时使用；如果直接暴露 `:8080`，保持默认 `0`，否则客户端可以伪造 `X-Forwarded-For` 绕过限流。

创建邀请码（在同一个 `portal/` 目录和同样的环境变量下）：

```bash
python cli.py invite create --label "alice" --uses 1 --days 7
```

### 不打扰用户，升级已有实例

新镜像/新配置（例如只同步 INBOX）不需要用户重新提交邮箱授权码。`reconcile` 会从每个实例的 config 卷读回授权码和当前 token，用同一个 token、同一个 data 卷重建容器：

```bash
# 1. 先拉取/构建新镜像，并确保 MSM_IMAGE 指向它
export MSM_IMAGE="ghcr.io/dryas/mail-shadow-mcp:latest"
docker pull "$MSM_IMAGE"

# 2. 先预演，确认哪些实例需要重建
python cli.py instance reconcile --all --dry-run

# 3. 真正执行；每个实例会有几秒 503，随后恢复
#    默认只升级镜像，完全不改现有 config（文件夹/trash 行为保持原样）
python cli.py instance reconcile --all

# 4. 如果确实要把已有实例改成只同步 INBOX / 配置 trash_folder，
#    在确认 MSM_SYNC_FOLDERS / MSM_TRASH_FOLDER 后加 --update-config
python cli.py instance reconcile --all --update-config

# 只处理一个实例
python cli.py instance reconcile --id <instance-id> [--update-config]
```

行为：

- 读取 `msm-config-<id>` 卷里的 `config.yaml`，保留原来的邮箱授权码和 **MCP token**；客户端配置不需要改。
- 默认只换镜像：复用 config 卷原文，**不会**改变现有实例的文件夹/trash 行为。
- 加 `--update-config` 才用当前 `MSM_SYNC_FOLDERS` / `MSM_TRASH_FOLDER` 重新生成 config；`msm-data-<id>` 数据卷不动。
- 镜像 ID 和 config 都没变化时自动跳过；`--force` 可强制重建。
- 容器规格（内存/CPU/PID 上限、GOMEMLIMIT/GOGC/GOMAXPROCS、TMPDIR 等）也会参与比对：旧容器没有 `msm.spec` label，或规格与当前配置不同时会重建。因此升级到 4GB/100 实例的内存配置时，即使镜像 tag 没变，也会应用新的 128m/GOMEMLIMIT 设置。
- 如果 config 里的 token 与控制库的路由哈希不一致（例如上次轮换在写 config 前失败），会以 config 为准修复控制库哈希，现有客户端 token 继续可用。
- 读不到授权码、token 与控制库不一致、或新镜像写入 config 失败时，**不会删除旧容器**，旧实例继续服务；修复后再执行一次即可。
- 重建期间实例状态变为 `updating`/`provisioning`，网关返回 503，不会把请求转给别的容器。

#### 用 supervisor 守护（Linux）

仓库带了一份 supervisor 配置模板：

```bash
sudo cp deploy/msm-portal.supervisor.conf /etc/supervisor/conf.d/msm-portal.conf
sudo editor /etc/supervisor/conf.d/msm-portal.conf
sudo mkdir -p /var/lib/msm-portal
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl status msm-portal
```

需要改的几项：

- `command` 指向你的 Python 3.12，例如 `/usr/bin/python3.12`（用 `pyenv`/venv 时写绝对路径）；
- `directory` 指向仓库里的 `portal/`；
- `MSM_DATA_DIR`、`MSM_PUBLIC_URL`、`MSM_LISTEN_PORT` 换成实际值；
- 用 `http://` 时 `MSM_SECURE_COOKIES="0"`，用 `https://` 时改成 `"1"`；
- 如果前面有 Caddy/Nginx，把 `MSM_LISTEN_HOST` 改成 `127.0.0.1` 并加 `MSM_TRUST_PROXY="1"`；
- 不想用 root 时设置 `user=`/`group=`，该用户需在 `docker` 组且能写 `MSM_DATA_DIR`。

supervisor 不会展开 shell 变量，模板里的值必须直接写死。日志在 `/var/log/msm-portal.log`：

```bash
sudo tail -f /var/log/msm-portal.log
```

> supervisor 只守护 portal 进程；已经启动的 `msm-<id>` 容器由各自的 Docker restart 策略管理，portal 重启不会影响它们。

> `MSM_BACKEND_MODE=ip` 是 Linux 宿主机直连 Docker bridge IP 的另一种方式，但 Docker Desktop（Windows/macOS）通常不可靠；优先用 `port`。
>
> 如果 `docker` 不在 PATH，设置 `MSM_DOCKER_BIN` 指向 `docker.exe` / `docker` 的绝对路径。
>
> 生产环境请用 supervisor（见上）、systemd、NSSM 或任务计划程序守护 `python -m app`，让它随机器启动；portal 重启不会停止已经运行的实例容器。
>
> `MSM_BACKEND_MODE=network` 只适用于 portal 容器和实例容器在同一个 Docker 网络里的 compose 部署。

### 方式 B：portal 也放进 Docker（compose）

适合一台专门的 Linux VPS，portal、Caddy 和实例都由 Docker 管理。

#### 1. 准备

- 一台 Linux 主机，已安装 Docker Engine + Compose 插件；
- 一个域名，A/AAAA 记录指向主机；
- 放行 80/443（TLS）。

#### 2. 配置

```bash
cd portal
cp .env.example .env
```

编辑 `.env`：

```dotenv
MSM_PUBLIC_URL=https://mcp.example.com
MSM_DOMAIN=mcp.example.com
MSM_IMAGE=ghcr.io/dryas/mail-shadow-mcp:latest
MSM_MAX_INSTANCES=100
```

#### 3. 启动（带自动 HTTPS）

```bash
docker compose --profile tls up -d --build
```

> 第一次创建实例时如果本地还没有 `MSM_IMAGE`，Docker 会先拉取镜像，页面可能等待较久；可以提前执行 `docker pull ghcr.io/dryas/mail-shadow-mcp:latest`。

Caddy 会自动申请证书，并反向代理到 portal。首次启动会生成：

- `msm-control` 卷：`control.db` + `master.key`；
- `msm-net` 网络；
- 如果设置了 `MSM_BOOTSTRAP_INVITE`，会创建一个引导邀请码（日志里只打印 invite id，不打印 code）。

> **Streaming 注意**：MCP StreamableHTTP / SSE 不能被反向代理缓冲。Caddy 已配置 `flush_interval -1`；如果改用 Nginx，请对 `/mcp` 设置 `proxy_buffering off;` 和足够长的 `proxy_read_timeout`（例如 `3600s`）。

如果只想本地试玩，不用 Caddy：

```bash
MSM_PUBLIC_URL=http://localhost:8080 MSM_SECURE_COOKIES=0 docker compose up -d --build
```

然后访问 `http://localhost:8080`。

#### 4. 创建邀请码

```bash
docker compose exec portal python cli.py invite create --label "alice" --uses 1 --days 7
```

输出：

```text
invite_id: 1a2b3c4d5e6f7788
code:      msm_xxxxxxxxxxxxxxxxxxxxxxxx
uses:      1
expires:   2026-10-28 10:00
```

`code` 只显示一次。把 code 发给用户，把 invite_id 留在管理记录里。

#### 5. 用户访问

用户打开 `https://mcp.example.com`，输入邀请码和邮箱授权码，拿到 MCP 配置。

---

## 100 个实例的容量说明

代码默认 `MSM_MAX_INSTANCES=0`（不限制），但 4GB 档的部署示例都设为 **100**：100 是 4GB 的容量规划值，设成硬上限才能在超卖时拒绝新实例，而不是让宿主 OOM。内存更大的宿主可以调高或设为 0。

控制面在 100 个实例下的设计：

- **网关解析目标容器**：`port`/`ip` 模式不再为每个 MCP 请求启动一次 `docker inspect` 子进程，而是优先走 Docker Unix socket HTTP API（默认 `/var/run/docker.sock`，可用 `MSM_DOCKER_SOCKET` 指定），单次约 1–4ms；远程 daemon/Windows context 自动回退到 CLI。
- **控制库**：SQLite WAL，令牌查找走 `token_hash` 唯一索引；单连接 + 锁保护，令牌查找/`last_seen` 更新都是微秒级。
- **实例隔离**：每个实例仍有自己的 SQLite 数据库、同步引擎和容器；100 个实例之间互不共享数据库连接。
- **数据面**：所有用户仍只暴露一个公网端口，靠 Bearer token 路由；不需要为 100 个实例开 100 个公网端口。
- **扩容方式**：加内存/CPU/磁盘即可；如果需要更高并发，优先改用 `MSM_BACKEND_MODE=network`（走 Docker DNS，不经过宿主机端口映射），或把 portal 放到更强的机器。

需要按宿主机资源估算：

| 资源 | 100 实例估算 |
|---|---|
| 内存 | 100 × `MSM_CONTAINER_MEMORY` 是 cgroup **上限之和**，不是预留；128m 时上限和约 12.8GB，但实际目标 25–35MB/容器 |
| 磁盘 | 每个实例的 SQLite + FTS/CJK 索引 + 附件；按邮箱大小 × 100 |
| 文件描述符 | 100 个容器、portal 的 SSE 连接、Docker socket；建议 `ulimit -n` ≥ 65535 |
| Docker | 100 个容器、100 个 loopback 端口（port 模式）或 100 个 bridge IP（network 模式） |

单 portal 进程可以服务 100 个实例；如果后续要到数百个，建议把网关/控制面做成多副本或按租户分片，因为限流器和控制库目前是单实例设计。

### 4GB 宿主的 100 实例内存预算

一组可落地的默认值（本仓库默认已按此设置）：

- 容器 cgroup 上限：`MSM_CONTAINER_MEMORY=128m`；这是上限，不是预留。
- Go 堆：`GOMEMLIMIT=48MiB`、`GOGC=50`、`GOMAXPROCS=1`。
- SQLite：最多 2 个连接，每连接 4MB page cache，`temp_store=FILE`、`mmap_size=0`。
- SQLite 临时文件/排序溢出：`TMPDIR=/data/tmp`、`SQLITE_TMPDIR=/data/tmp`，不占 8MB 的 `/tmp` tmpfs。
- 单封邮件入库正文上限 256KB；CJK 索引同样最多 256KB；raw fallback 8MB；单附件 16MB。
- 同步窗口 50 封，但正文是**逐封提取后立即截断**，不会把 50 封的原始 HTML 同时留在内存里（一封 5MB 的 newsletter 不会变成 250MB 峰值）。
- 100 个容器**重启**时 initial sync/IDLE 连接随机抖动最多 15 分钟，避免同时拉满内存；**全新实例第一次同步不抖动**，用户提交后立即可见。
- 轮询间隔默认 30 分钟；INBOX 的 IDLE 仍然即时。

粗算：

| 项目 | 估算 |
|---|---|
| OS + Docker daemon | 0.5–0.8GB |
| portal + Caddy | 50–100MB |
| 100 个实例 | 目标 25–35MB/个 ≈ 2.5–3.5GB |
| 合计 | 约 3.0–4.4GB，需要按实际邮件量观察 |

**4GB 档请保持 `MSM_MAX_INSTANCES=100`**（本仓库的 `.env.example`、compose 和 supervisor 示例已设为 100）。它不会降低单实例上限，但能防止第 101 个用户悄悄把宿主推过 4GB；`0`（不限制）只适合内存更大的宿主。

`25–35MB/个` 是目标值，不是强制值：真正硬限制只有 cgroup 的 128m，`GOMEMLIMIT` 是软限制。首次上线後请用下面的脚本在**所有实例完成初次同步后**实测一次，而不是只看 `docker stats` 的 LIMIT 列：

```bash
python3 deploy/memory-report.py --budget-mb 4096
```

脚本会打印实例数、实际 USAGE 总和、宿主可用内存和余量；余量小于 10% 时按下面的顺序调低。如果 `docker stats` 显示总和接近 4GB：

1. `MSM_CONTAINER_GOMEMLIMIT=32MiB`；
2. `MSM_CONTAINER_MEMORY=96m`；
3. `MSM_SYNC_INTERVAL_MIN=60`；
4. 减少 `MSM_CONTAINER_CPUS`（例如 0.5）以降低同时运行的同步 CPU 竞争；
5. 对特别大的邮箱，先用 `MSM_SYNC_FOLDERS` 限制只同步 INBOX。

监控命令：

```bash
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}' | grep msm-
```

注意：`docker stats` 的 `LIMIT` 是 cgroup 上限；只有 `USAGE` 计入宿主实际内存。100 个容器的 LIMIT 之和超过 4GB 是正常的，关键是 USAGE 之和 + 系统进程不超过宿主内存。

---

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MSM_DATA_DIR` | `data`（镜像内 `/srv/msm`） | 控制数据库与 master key 所在目录；必须持久化 |
| `MSM_PUBLIC_URL` | `http://localhost:8080` | 对外访问的 origin，**不能带路径**；决定 Secure Cookie 和返回的 MCP URL |
| `MSM_LISTEN_HOST` / `MSM_LISTEN_PORT` | `0.0.0.0` / `8080` | portal 监听地址 |
| `MSM_SECURE_COOKIES` | HTTPS 时自动 `1` | 是否给 Cookie 加 `Secure` |
| `MSM_MASTER_KEY` | 自动生成到 `$MSM_DATA_DIR/master.key` | 至少 32 字节；丢失或更换会让所有邀请码和浏览器会话失效，已有 MCP token 仍可用，直到重新提交凭据生成新 token |
| `MSM_DOCKER_BIN` | `docker` | Docker CLI 路径 |
| `MSM_DOCKER_NETWORK` | `msm-net` | `network`/`ip` 模式下 portal 与实例共享的 Docker 网络；`port` 模式使用 Docker 默认 bridge |
| `MSM_IMAGE` | `ghcr.io/dryas/mail-shadow-mcp:latest` | 实例镜像 |
| `MSM_BACKEND_MODE` | `port` | `port`：宿主机直接运行 portal（推荐）；`network`：portal 与实例同处一个 Docker 网络（compose）；`ip`：Linux 宿主机通过容器 IP 访问 |
| `MSM_BACKEND_PORT` | `8080` | 实例内部 MCP 端口（一般不用改） |
| `MSM_VERIFY_BACKEND` | `1` | 启动后探测 `/mcp` 是否返回 401 |
| `MSM_MAX_INSTANCES` | `0`（代码默认）；部署示例为 `100` | 同时存在的实例上限；`0` = 不限制。4GB 宿主请保持 `100`：单实例上限仍是 128m，但总数被封顶后可用内存才是可预测的。100 × `MSM_CONTAINER_MEMORY` 是上限之和，不是预留 |
| `MSM_CONTAINER_MEMORY` | `128m` | 每个实例 cgroup 内存上限（不是预留；100 个实例的上限之和可以大于宿主内存，实际看 `docker stats`） |
| `MSM_CONTAINER_CPUS` | `1.0` | 每个实例 CPU 上限 |
| `MSM_CONTAINER_PIDS` | `128` | 每个实例 PID 上限；`GOMAXPROCS=1` 下足够 |
| `MSM_CONTAINER_GOMEMLIMIT` | `48MiB` | 传给容器的 `GOMEMLIMIT`，让 Go 在 4GB 宿主上更早 GC |
| `MSM_CONTAINER_GOGC` | `50` | 传给容器的 `GOGC`，降低堆峰值（CPU 换内存） |
| `MSM_CONTAINER_GOMAXPROCS` | `1` | 每个实例一个 P，减少线程/调度开销 |
| `MSM_SYNC_INTERVAL_MIN` | `30` | 实例轮询间隔；IDLE 仍会对 INBOX 即时推送。调大可进一步降低 100 实例的同步峰值 |
| `MSM_SYNC_JITTER_SEC` | `1800` | **已有同步数据的实例**重启/IDLE 重连时，初始同步最多随机延迟多少秒；全新实例第一次同步不延迟。实际上限为 `min(SYNC_INTERVAL_MIN/2, 该值)`，所以 30 分钟间隔下最大 15 分钟 |
| `MSM_SYNC_FOLDERS` | `INBOX` | 每个实例同步的 IMAP 文件夹，逗号分隔；默认只同步 `INBOX`，避免把「所有邮件」/垃圾箱/大附件历史全部拉进每个用户的数据库。需要 Sent/Archive 时设为 `INBOX,Sent` 等；设为 `ALL` 可同步全部（不推荐） |
| `MSM_GATEWAY_TIMEOUT` | `120` | 网关等待实例响应头的秒数；MCP 工具调用超过该值会返回 502 |
| `MSM_TRASH_FOLDER` | 空 | 为每个实例配置 `delete_mail` 的软删除目标文件夹（必须在邮件服务器上已存在，例如 `llm_delete`）。为空时 `delete_mail` 工具会返回「未配置」错误 |
| `MSM_SESSION_TTL` | `7200` | 邀请码会话有效期（秒） |
| `MSM_FLASH_TTL` | `900` | 结果页 Token 显示窗口（秒） |
| `MSM_INVITE_RATE_LIMIT` / `_WINDOW` | `12` / `900` | 每 IP 邀请码尝试限制 |
| `MSM_PROVISION_RATE_LIMIT` / `_WINDOW` | `8` / `3600` | 每 IP 创建实例限制 |
| `MSM_FOLDERS_RATE_LIMIT` / `_WINDOW` | `30` / `900` | 每 IP 调用「获取文件夹列表」的限制（每次都会登录一次 IMAP） |
| `MSM_GATEWAY_RATE_LIMIT` / `_WINDOW` | `120` / `60` | 未知 token 的失败限制 |
| `MSM_ALLOW_INSECURE_IMAP` | `0` | 是否允许 `tls_mode: none`；公网部署保持 0 |
| `MSM_ALLOW_PRIVATE_IMAP` | `0` | 是否允许自定义 IMAP 指向内网/回环地址；公网部署保持 0 |
| `MSM_ALLOW_ANY_IMAP_PORT` | `0` | 是否允许 993/143 之外的 IMAP 端口；公网部署保持 0 |
| `MSM_TRUST_PROXY` | `0` | 是否信任 `X-Forwarded-For`；只有前面有可信反向代理时才设为 1 |
| `MSM_ADMIN_PASSWORD` | 空（禁用） | 管理控制台 `/admin` 的密码，至少 8 位。为空时 `/admin` 直接返回 404。**只应通过 HTTPS 访问**；登录表单会把密码发给服务端 |
| `MSM_ADMIN_SESSION_TTL` | `28800` | 管理会话有效期（秒），到期后需重新登录 |
| `MSM_ADMIN_RATE_LIMIT` / `_WINDOW` | `8` / `900` | 每 IP 管理登录尝试限制 |
| `MSM_DOCKER_VOLUME_ROOT` | `/var/lib/docker/volumes` | Docker 的命名卷目录。路径不存在时自动改用 `docker info` 里的 `DockerRootDir`，通常不用设 |
| `MSM_STATS_INTERVAL` | `300` | 控制台采集各实例影子库统计的间隔（秒） |
| `MSM_BOOTSTRAP_INVITE` | 空 | 启动时创建一次性引导邀请码 |
| `MSM_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARN` / `ERROR` |

---

## 管理控制台 `/admin`

设置 `MSM_ADMIN_PASSWORD` 后，用浏览器打开 `/admin`，输入密码即可进入。控制台只做**只读统计 + 容器级操作**：

- **概览**：邮箱总数（运行 / 暂停 / 异常）、同步文件夹总数、邮件总数、影子数据总量、近 24 小时新增、主机负载 / 内存 / 磁盘剩余。
- **邮箱列表**：每个邮箱同步了几个文件夹（并列出文件夹名）、多少封邮件、占用多少空间、最新邮件时间、待拉取 UID 差，以及一键「停止 / 启动」。
- **实例详情**：每个文件夹的邮件数、最新 UID、已同步 UID、待拉取、CJK 索引状态；CPU / 内存 / 网络；容器日志最后 80 行；启动 / 停止 / 重启 / 删除（删除需输入邮箱地址确认）。
- **邀请码**：查看已用 / 上限、到期时间、关联邮箱数；在线生成（明文只显示一次）与停用。
- **审计日志**：最近 200 条邀请、开通、登录、管理操作与异常。

数据来源：控制数据库（邮箱、状态、文件夹选择）+ 各实例数据卷里的影子 SQLite（`<volume>/_data/mail.db`）。

- 影子库以 **只读**（`mode=ro`）方式打开，WAL 模式下不会阻塞实例同步；
- 每条查询都有 5 秒的墙钟预算，超大邮箱也不会拖住页面；
- 采集在后台线程按 `MSM_STATS_INTERVAL`（默认 300 秒）顺序执行，页面始终渲染上一次快照，并可随时点「重新采集」。

「待拉取」= 文件夹最新 UID − 已同步 UID，只是判断同步是否落后的近似值：删除邮件、UIDVALIDITY 变化也会让它不为零。

> ⚠️ 这个 portal 默认监听明文 HTTP。管理密码通过登录表单提交，**在没有 TLS 的地址上使用等于把密码暴露在网络上**。生产环境请在前面加一层 HTTPS（Caddy/Nginx），或至少只在内网/VPN 里访问 `/admin`。

---

## 管理命令

宿主机模式（在 `portal/` 目录、同样的环境变量下）：

```bash
python cli.py invite create --label "team-a" --uses 3 --days 30
python cli.py invite list
python cli.py invite disable <invite_id>
python cli.py instance list
python cli.py instance remove <instance_id> --purge-data
python cli.py doctor
```

Docker compose 模式：

```bash
# 邀请码
docker compose exec portal python cli.py invite create --label "team-a" --uses 3 --days 30
docker compose exec portal python cli.py invite list
docker compose exec portal python cli.py invite disable <invite_id>

# 实例
docker compose exec portal python cli.py instance list
docker compose exec portal python cli.py instance remove <instance_id> --purge-data

# 自检
docker compose exec portal python cli.py doctor
```

`--purge-data` 会同时删除 `msm-data-<id>`（邮件影子数据库）。不加则保留数据卷，之后同一邮箱重新提交可以复用。

> 不要用 `docker compose down -v` 清理 portal：`-v` 会删除 `msm-control` 卷（控制数据库和 master key）以及 Caddy 证书卷。日常重启用 `docker compose restart`，升级用 `docker compose up -d --build`。
>
> `docker compose down` 只停止 portal/Caddy，不会停止已经创建的 `msm-<id>` 实例容器；要清理实例请用 `cli.py instance remove`。

---

## 安全模型

这是“邀请制小规模托管”，不是多租户 SaaS 的完整隔离方案。部署前请确认：

1. **Docker socket = root。** portal 需要创建容器，因此挂载了 `/var/run/docker.sock`。能访问 portal 进程或该 socket 的人基本等于宿主 root。不要给 portal 容器额外的宿主目录写权限；条件允许时，可在 socket 前加一个只放行 containers/images/networks/volumes 的代理（如 docker-socket-proxy）。
2. **必须用 HTTPS。** Bearer token 和邮箱授权码都走请求体/Cookie。公网部署请用 Caddy/Traefik/云 LB 终止 TLS，并设置 `MSM_PUBLIC_URL=https://...`。
3. **授权码落在 config 卷里。** 为了让容器重启后还能同步，`config.yaml` 必须包含 IMAP 授权码。它位于 `msm-config-<id>` 卷，文件权限 0600，只读挂载进容器；控制数据库不保存授权码。宿主 root 和 Docker 管理员仍可读取。
4. **Token 是邮箱读取权限。** 结果页只在 `MSM_FLASH_TTL` 内显示 token；配置页 `Cache-Control: no-store`；不要在 URL 里传 token。泄露后用新的授权码重新提交即可轮换。
5. **容器限制。** 每个实例：
   - 公网不发布端口；`port` 模式只发布到 `127.0.0.1` 的随机端口；
   - `--cap-drop ALL`；
   - `no-new-privileges`；
   - `--read-only` + tmpfs `/tmp`；
   - 内存 / CPU / PID 上限；
   - 日志大小限制。
6. **单副本假设。** 限流器、每邮箱锁都在进程内。不要同时跑多个 portal 副本；需要横向扩容时把限流和锁移到数据库/Redis，并把网关做成无状态。
7. **SSRF 限制。** 自定义 IMAP 默认只允许标准端口 993/143，并拒绝回环/内网/链路本地地址；如企业内网部署确实需要，显式设置 `MSM_ALLOW_PRIVATE_IMAP=1` / `MSM_ALLOW_ANY_IMAP_PORT=1`，并自行承担风险。
8. **master key 要备份。** 存在 `msm-control` 卷的 `master.key`。丢失后所有浏览器会话和邀请码失效；已有 MCP token 仍可继续工作，但用户无法再登录，直到创建新邀请码并重新提交凭据（会生成新 token）。备份 `msm-control` 卷时同时备份它。
9. **邀请码**使用 24 字节随机数，只存 HMAC；默认一次性、7 天有效。可以通过 CLI 创建多用途邀请码。

### 已知限制

- Microsoft 个人 Outlook 通常已关闭 IMAP 基本认证，应用密码也可能不可用；当前不支持 OAuth2。
- 网易/QQ 等需要在邮箱设置中手动开启 IMAP，并使用“授权码”而不是登录密码。
- 自签名 IMAP 证书未支持（`tls_skip_verify` 固定为 false）；请使用受信任证书或把 CA 加入镜像信任链。
- 首次同步大邮箱可能耗时；portal 只保证 MCP 进程已启动，不等待首次同步完成。
- 没有配置 `trash_folder`，因此 `delete_mail` 工具会返回配置错误；如果不需要软删除，这是安全默认值。
- 没有实现 `fileserver_port` 的公网附件下载链接；`download_attachments` 返回的是容器内路径。需要附件回传时，可后续给网关加 `/dl/` 路由。
- 没有内置用户账户体系；邀请码会话就是访问凭据。
- 没有自动清理长期不用实例的 janitor；用 `instance list` + `instance remove` 管理。

---

## 本地开发

```bash
cd portal
python -m unittest discover -s tests -v
```

运行 portal（需要本机有 Docker）：

```bash
export MSM_DATA_DIR=./data
export MSM_PUBLIC_URL=http://localhost:8080
export MSM_SECURE_COOKIES=0
export MSM_BACKEND_MODE=port        # 宿主机直接运行 portal
python -m app
```

创建邀请码：

```bash
python cli.py invite create --label dev --uses 10 --days 30
```

用 curl 测试网关：

```bash
curl -i http://localhost:8080/mcp \
  -H 'Authorization: Bearer <token>' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

---

## 目录结构

```
portal/
├── app/
│   ├── __main__.py        # 启动入口
│   ├── config.py          # 环境变量 / master key
│   ├── db.py              # SQLite：邀请码、实例、审计
│   ├── security.py        # HMAC、Cookie、CSRF、限流
│   ├── providers.py       # 邮箱服务商预设与输入校验
│   ├── imap_check.py      # 创建前的 IMAP 登录验证
│   ├── instance_config.py # 生成 config.yaml
│   ├── docker_cli.py      # Docker CLI 封装
│   ├── provisioner.py     # 幂等创建 / 重建 / 轮换
│   ├── gateway.py         # token → 容器 反向代理
│   ├── web.py             # HTTP 路由与页面
│   ├── templates.py       # 服务端 HTML
│   └── static/            # CSS / JS
├── tests/                 # 单元/集成测试（70 个）
├── cli.py                 # 管理命令
├── deploy/
│   └── msm-portal.supervisor.conf  # supervisor 配置模板
├── Dockerfile
├── docker-compose.yml
├── Caddyfile
└── README.md
```

---

## License

与主项目一致：Apache 2.0。
