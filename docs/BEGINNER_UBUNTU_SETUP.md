# GPTGrok2API 从零搭建与使用手册（Ubuntu/Linux 版）

本文面向刚接触 Linux、Docker 和 systemd 的用户，重点覆盖 Ubuntu 22.04/24.04。Debian 类系统也可参考，但软件包名称可能有差异。

推荐架构：

- GPTGrok2API 主程序直接运行在 Linux 宿主机。
- Captcha Solver 直接运行在 Linux 宿主机，由 Xvfb 提供虚拟显示器。
- WARP、Privoxy、FlareSolverr 和 iCloud Privacy Mail 运行在 Docker。
- systemd 负责开机启动、异常重启和日志管理。
- 只将 GPTGrok2API 主程序通过 Nginx/HTTPS 对外提供，不公开 solver 和内部代理端口。

Ubuntu Server 不需要安装桌面环境，也不需要通过远程桌面一直打开浏览器窗口。Captcha Solver 仍会访问 xAI 实际页面，但 Chromium 画面由 Xvfb 提供的虚拟显示器承载。

macOS 用户请阅读 [macOS 从零搭建手册](BEGINNER_LOCAL_SETUP.md)。

## 1. 服务结构和端口

| 服务 | 用途 | 监听地址 |
| --- | --- | --- |
| GPTGrok2API | 管理后台、账号池、注册中心、统一 API | `127.0.0.1:8000` |
| Captcha Solver | Grok Turnstile 实页解题 | `127.0.0.1:8877` |
| WARP SOCKS5 | WARP 出口 | `127.0.0.1:40000` |
| Privoxy | HTTP 代理出口 | `127.0.0.1:40080` |
| FlareSolverr | Cloudflare clearance | `127.0.0.1:8191` |
| iCloud Privacy Mail | 本地邮箱 sidecar | `127.0.0.1:8788` |
| Nginx | 公网 HTTP/HTTPS 入口 | `0.0.0.0:80/443` |

不要将 `8877`、`40000`、`40080`、`8191` 或 `8788` 放行到公网。

## 2. 服务器要求

### 2.1 系统和权限

推荐：

- Ubuntu Server 22.04 LTS 或 24.04 LTS。
- x86_64 或 arm64。
- 一个可使用 `sudo` 的普通用户。
- 公网部署时需要域名解析到服务器 IP。

不建议长期使用 root 直接运行主程序和 Chromium。

### 2.2 硬件建议

| 用途 | 最低配置 | 推荐配置 |
| --- | --- | --- |
| 单线程测试 | 2 vCPU / 4 GB RAM | 4 vCPU / 8 GB RAM |
| 2-3 并发 | 4 vCPU / 8 GB RAM | 8 vCPU / 16 GB RAM |
| 多 solver worker | 8 GB RAM 起 | 16-32 GB RAM |

磁盘建议至少预留 10 GB，因为 Docker 镜像、Chromium、日志和备份都会占用空间。

### 2.3 费用说明

代码、本地 Captcha Solver、Xvfb、Docker 和 systemd 本身不按解题次数收费，但整套部署不一定是零成本：

- 本地 Turnstile 解题不需要向 YesCaptcha、2Captcha 等第三方打码平台按次付费。
- 已有 Ubuntu 电脑可以不新增服务器费用；云服务器/VPS 通常按月收费。
- 只通过 SSH 隧道自用时可以不购买域名；公网 HTTPS 通常需要域名，Let's Encrypt 证书本身免费。
- WARP 基础服务可免费使用，但住宅代理、移动代理或其他更稳定的出口一般收费。
- 外部邮箱 API、第三方账号、云流量和备份存储可能另外收费。

因此，“本地打码”可以免掉第三方打码平台的按次费用，不代表服务器、代理和邮箱资源全部永久免费。

## 3. 初始化 Ubuntu

更新系统：

```bash
sudo apt-get update
sudo apt-get upgrade -y
```

安装基础工具：

```bash
sudo apt-get install -y \
  git curl ca-certificates jq openssl build-essential \
  unzip tar xvfb nginx ufw
```

检查时间和时区：

```bash
timedatectl
sudo timedatectl set-timezone Asia/Shanghai
```

时间严重不准会导致 token、Cookie 和 HTTPS 校验异常。

## 4. 安装 Docker Engine

先检查：

```bash
docker --version
docker compose version
```

如果尚未安装，可使用 Docker 官方安装脚本：

```bash
curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
sudo sh /tmp/get-docker.sh
sudo usermod -aG docker "$USER"
```

退出 SSH 后重新登录，或执行：

```bash
newgrp docker
```

启用 Docker：

```bash
sudo systemctl enable --now docker
docker run --rm hello-world
```

如果 `docker` 仍提示权限不足，检查：

```bash
id
getent group docker
```

## 5. 安装 Node.js 22

Ubuntu 自带的 Node.js 可能过旧。使用 NodeSource 安装 Node.js 22：

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
```

检查：

```bash
command -v node
node --version
npm --version
```

`command -v node` 应返回 `/usr/bin/node` 或其他 systemd 可访问的系统路径。Grok Castle token 依赖 Node.js。

## 6. 安装 uv 和 Python 3.13

Ubuntu 22.04/24.04 默认 Python 版本可能低于项目要求，不要强行替换系统 Python。使用 uv 独立安装 Python 3.13：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
uv python install 3.13
uv --version
```

如果重新登录后提示 `uv: command not found`，将下面一行加入 `~/.bashrc`：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## 7. 创建安装目录

本手册使用 `/opt/gptgrok-stack`：

```bash
sudo mkdir -p /opt/gptgrok-stack
sudo chown -R "$USER":"$(id -gn)" /opt/gptgrok-stack
cd /opt/gptgrok-stack
```

### 7.1 发布者和使用者的区别

本手册后续命令面向最终使用者。发布者先在自己的 Mac 上把功能源码提交并推送到 GitHub；使用者不需要也不应该接触发布者 Mac 上的运行目录、账号数据或密钥。

Mac 上只上传以下内容：

- GPTGrok2API 源码、前端源码和部署模板。
- 已内置的 `captcha-solver/` 源码。
- `.env.example`、README 和安装手册等不含真实密钥的示例文件。

下列内容必须留在 Mac 本机，不能提交到仓库，也不要通过 `rsync` 发给其他用户：

```text
.venv/
captcha-solver/.venv/
node_modules/
web_dist/
.env
config.json
data/
logs/
*.log
```

其中 `data/`、`.env` 和 `config.json` 可能包含账号、Cookie、OAuth token、邮箱材料和管理密钥。当前仓库的 `.gitignore` 已排除这些内容；发布前仍应执行 `git status` 和密钥扫描确认。

使用者只从仓库获取干净源码，并在自己的 Ubuntu 上重新创建 `.venv`、`.env`、`config.json` 和 `data/`。

### 7.2 使用者克隆主仓库

当前主仓库已经同时包含：

- GPTGrok2API 的“本地 Captcha Solver” provider 和代理透传。
- `captcha-solver/` 完整源码。
- xAI 实页渲染和 callback token 采集修复。

克隆主仓库即可，不再需要另外下载 `xai-grok-mass`：

```bash
git clone https://github.com/AuuCoder/gptGrok2api.git chatgpt2api
```

仓库已经公开，使用者直接执行上面的 HTTPS 克隆命令即可，不需要 Personal Access Token、SSH Key 或其他仓库凭据。

目录结构：

```text
/opt/gptgrok-stack/
└── chatgpt2api/
    ├── captcha-solver/
    ├── services/
    └── web-vue/
```

确认关键文件已经到位：

```bash
test -f /opt/gptgrok-stack/chatgpt2api/services/register/grok_protocol.py
test -f /opt/gptgrok-stack/chatgpt2api/deploy/systemd/chatgpt2api.service.example
test -f /opt/gptgrok-stack/chatgpt2api/captcha-solver/server.py
echo "source files ready"
```

如果仓库中没有 `captcha-solver/server.py`，说明代码版本过旧，需要先更新主仓库。

## 8. 安装 GPTGrok2API

```bash
cd /opt/gptgrok-stack/chatgpt2api
uv sync --python 3.13
```

检查 Python：

```bash
.venv/bin/python --version
```

构建前端：

```bash
cd /opt/gptgrok-stack/chatgpt2api/web-vue
npm ci
npm run build:server
```

返回项目根目录：

```bash
cd /opt/gptgrok-stack/chatgpt2api
```

## 9. 创建配置

### 9.1 `config.json`

生成随机管理密钥：

```bash
openssl rand -hex 24
```

创建 `config.json`：

```json
{
  "auth-key": "替换为上一步生成的随机密钥"
}
```

设置权限：

```bash
chmod 600 config.json
```

### 9.2 `.env`

```bash
cp .env.example .env
chmod 600 .env
```

宿主机混合部署推荐值：

```dotenv
CHATGPT2API_PORT=8000
WARP_SOCKS_PORT=40000
PRIVOXY_PORT=40080
FLARESOLVERR_PORT=8191
ICLOUD_PRIVACY_MAIL_PORT=8788
ICLOUD_PRIVACY_MAIL_BASE_URL=http://127.0.0.1:8788
ICLOUD_PRIVACY_MAIL_PUBLIC_BASE_URL=http://127.0.0.1:8788
CHATGPT2API_HOST_PRIVOXY_URL=http://127.0.0.1:40080
CHATGPT2API_HOST_FLARESOLVERR_URL=http://127.0.0.1:8191
TZ=Asia/Shanghai
```

`CHATGPT2API_AUTH_KEY` 可以不写，此时使用 `config.json` 的 `auth-key`。

## 10. 启动 Docker 辅助服务

只启动 WARP、Privoxy、FlareSolverr 和 iCloud sidecar：

```bash
cd /opt/gptgrok-stack/chatgpt2api
docker compose -f docker-compose.warp.yml --profile local-icloud up -d \
  warp-proxy privoxy flaresolverr icloud-privacy-mail
```

不要直接执行不带服务名的 `up -d`，否则 Docker 版 `app` 也会启动，与宿主机 systemd 主程序重复。

检查：

```bash
docker compose -f docker-compose.warp.yml --profile local-icloud ps
curl -I http://127.0.0.1:8191/
curl -I http://127.0.0.1:8788/login
curl -x http://127.0.0.1:40080 https://www.cloudflare.com/cdn-cgi/trace
```

## 11. 安装 Captcha Solver

```bash
cd /opt/gptgrok-stack/chatgpt2api/captcha-solver
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

安装 Chromium 常用系统库：

```bash
sudo .venv/bin/python -m playwright install-deps chromium
```

检查依赖：

```bash
.venv/bin/python -c 'import cloakbrowser, fastapi, uvicorn; print("solver dependencies ok")'
```

### 11.1 手动 Xvfb 测试

```bash
cd /opt/gptgrok-stack/chatgpt2api/captcha-solver
TURNSTILE_HEADLESS=0 xvfb-run -a \
  --server-args='-screen 0 1920x1080x24' \
  .venv/bin/python -m uvicorn server:app \
  --host 127.0.0.1 --port 8877
```

新开一个 SSH 终端测试：

```bash
curl http://127.0.0.1:8877/health
```

返回 `status: ok` 后，在原终端按 `Control+C` 停止手动测试。

### 11.2 首次 Chromium 下载

首次实际解题时，CloakBrowser 会下载自己的 Chromium。检查日志中是否出现：

```text
Download complete
Checksum verified
Binary ready
```

下载缓存保存在运行 systemd 服务的用户主目录中，因此 systemd 模板会显式配置 `HOME`。

## 12. 安装 systemd 服务

项目提供：

- `deploy/systemd/chatgpt2api.service.example`
- `deploy/systemd/captcha-solver.service.example`

设置变量：

```bash
APP_DIR=/opt/gptgrok-stack/chatgpt2api
RUN_USER="$(id -un)"
RUN_GROUP="$(id -gn)"
RUN_HOME="$HOME"
```

### 12.1 生成主程序 unit

```bash
sed \
  -e "s|__APP_DIR__|$APP_DIR|g" \
  -e "s|__RUN_USER__|$RUN_USER|g" \
  -e "s|__RUN_GROUP__|$RUN_GROUP|g" \
  -e "s|__RUN_HOME__|$RUN_HOME|g" \
  "$APP_DIR/deploy/systemd/chatgpt2api.service.example" \
  | sudo tee /etc/systemd/system/chatgpt2api.service >/dev/null
```

### 12.2 生成 solver unit

```bash
sed \
  -e "s|__APP_DIR__|$APP_DIR|g" \
  -e "s|__RUN_USER__|$RUN_USER|g" \
  -e "s|__RUN_GROUP__|$RUN_GROUP|g" \
  -e "s|__RUN_HOME__|$RUN_HOME|g" \
  "$APP_DIR/deploy/systemd/captcha-solver.service.example" \
  | sudo tee /etc/systemd/system/captcha-solver.service >/dev/null
```

### 12.3 校验和启动

```bash
sudo systemd-analyze verify /etc/systemd/system/chatgpt2api.service
sudo systemd-analyze verify /etc/systemd/system/captcha-solver.service
sudo systemctl daemon-reload
sudo systemctl enable --now chatgpt2api
sudo systemctl enable --now captcha-solver
```

检查：

```bash
sudo systemctl status chatgpt2api --no-pager
sudo systemctl status captcha-solver --no-pager
curl -I http://127.0.0.1:8000/
curl http://127.0.0.1:8877/health
```

## 13. systemd 常用命令

重启：

```bash
sudo systemctl restart chatgpt2api
sudo systemctl restart captcha-solver
```

停止：

```bash
sudo systemctl stop chatgpt2api
sudo systemctl stop captcha-solver
```

查看最近日志：

```bash
sudo journalctl -u chatgpt2api -n 100 --no-pager
sudo journalctl -u captcha-solver -n 100 --no-pager
```

实时日志：

```bash
sudo journalctl -u chatgpt2api -f
sudo journalctl -u captcha-solver -f
```

开机后确认是否自动启动：

```bash
systemctl is-enabled chatgpt2api
systemctl is-enabled captcha-solver
```

## 14. 配置本地 Captcha Solver

浏览器打开 GPTGrok2API 后台，进入“注册中心”，选择 `Grok（协议）`。

| 字段 | 推荐值 |
| --- | --- |
| Turnstile 服务 | `本地 Captcha Solver` |
| API Key | 留空 |
| API Base | `http://127.0.0.1:8877` |
| HTTP 超时 | `30` |
| 解题超时 | `180` |
| 轮询间隔 | `3` |

必须保持：

- `real_page:true`。
- `TURNSTILE_HEADLESS=0`。
- 注册请求和 solver 使用同一代理/IP。
- Solver 日志中 `method` 为 `real-page`。

### 14.1 可选的实页 smoke test

以下测试只获取 Turnstile token，不会提交注册表单：

```bash
curl -sS --max-time 130 \
  -X POST http://127.0.0.1:8877/solve \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "turnstile",
    "sitekey": "0x4AAAAAAAhr9JGVDZbrZOo0",
    "url": "https://accounts.x.ai/sign-up?redirect=grok-com",
    "real_page": true,
    "timeout_s": 90,
    "proxy": "http://127.0.0.1:40080"
  }' | jq '{solved, method, elapsed, token_present: (.token | length > 0)}'
```

期望返回：

```json
{
  "solved": true,
  "method": "real-page",
  "token_present": true
}
```

## 15. 邮箱 provider

小白每次只启用一个 provider。

### 15.1 iCloud 邮箱（本系统）

确认 sidecar：

```bash
docker ps --filter name=chatgpt2api-icloud-privacy-mail
curl -I http://127.0.0.1:8788/login
```

然后：

1. 进入后台“iCloud 邮箱”页。
2. 完成 Apple 登录和 2FA。
3. 手动创建一个隐私邮箱。
4. 在注册中心选择 `iCloud 邮箱（本系统）`。

宿主机模式使用 `http://127.0.0.1:8788`，不要使用 Docker 内部地址 `http://icloud-privacy-mail:8787`。

### 15.2 iCloud API（外部）

外部 iCloud API 需要单独的 API Base 和 API Key。出现 `iCloud Privacy Mail 请求失败（Timeout）` 时，先确认当前启用的是外部 API 还是本地 sidecar。

### 15.3 Cloudflare Temp Email / Outlook Token

只导入少量数据做单次测试。确认能创建邮箱、能收件、能正确提取验证码后，再批量导入。

## 16. 执行第一个注册任务

首次配置：

| 项目 | 值 |
| --- | --- |
| 注册数量 | `1` |
| 并发 | `1` |
| 邮箱 provider | 只启用一个 |
| Turnstile | `本地 Captcha Solver` |
| 代理 | WARP/Privoxy 或一个已验证代理 |

正常日志顺序：

```text
准备注册环境
获取注册邮箱
验证码已发送，等待邮件
邮箱验证完成，正在进行安全校验
安全校验完成，正在创建账号
注册成功
```

如果失败，不要连续点击重试。先根据时间查看主程序和 solver journal。

需要让注册成功的 OpenAI/Grok 账号自动上传到作者魔改 NovaApi（Sub2API）或 CPA 时，请先完成第 18 节，再开始批量任务。

## 17. 并发和多 worker

Captcha Solver 使用共享动态并发限制器，不需要为了 Turnstile 并发启动多个 Uvicorn worker。注册中心的“注册解题并发”表示注册任务可用槽位；开启 Grok 自动协议授权后，程序会再预留一个 OAuth 槽位。

推荐步骤：

1. 先用注册并发 `1`。
2. 连续成功后改为 `2`。
3. 16 GB 以上服务器可尝试注册线程 `3`、注册解题并发 `3`；启用即时 OAuth 后实际 solver 总槽位为 `4`。

注意：

- 每个活跃槽位都可能启动独立 Chromium。
- 内存紧张时把注册线程和注册解题并发同时降为 `2`，即时 OAuth 会使用第 3 个槽位。
- 不建议设置 `--workers 2+`；多进程会拆散并发状态和 `/status` 数据，并放大内存占用。
- 注册成功后应立即出现“Grok OAuth 授权已进入即时上传队列”，随后显示 NovaApi/CPA 投递终态。

## 18. 自动上传到 NovaApi（Sub2API）和 CPA

本系统支持：

- OpenAI 注册成功后自动同步到 NovaApi，并可同时上传到 CPA。
- Grok 每个账号注册成功后立即执行 xAI OAuth，并同时或分别上传到 NovaApi 和 CPA，不等待整批任务结束。

Sub2API 必须优先使用项目作者魔改版 [AuuCoder/NovaApi](https://github.com/AuuCoder/NovaApi)。该仓库默认 Compose 仍可能引用普通上游镜像，因此不能只克隆仓库后直接启动。

从魔改源码构建镜像、添加 Sub2API/CPA 连接、选择远端分组、开启注册自动投递以及排查 401/404 的完整步骤见：

- [自动上传到 NovaApi（Sub2API）与 CPA 配置手册](AUTO_UPLOAD_SUB2API_CPA.md)

首次必须使用数量 `1`、并发 `1` 验证。确认 NovaApi 中出现对应 `openai/xai` OAuth 账号、CPA 中出现 OpenAI 的 `codex-*.json` 或 Grok 的 `xai-*.json` 后，再增加并发。

## 19. 配置 Nginx 和 HTTPS

如果只在 SSH 隧道中使用，可以不配置公网 Nginx。公网部署时，先将域名 A/AAAA 记录解析到服务器。

创建 `/etc/nginx/sites-available/gptgrok2api`：

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name api.example.com;

    client_max_body_size 100m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
```

启用：

```bash
sudo ln -s /etc/nginx/sites-available/gptgrok2api \
  /etc/nginx/sites-enabled/gptgrok2api
sudo nginx -t
sudo systemctl reload nginx
```

安装 Certbot：

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d api.example.com
```

测试续期：

```bash
sudo certbot renew --dry-run
```

只反向代理 `8000`，不要为 `8877` 配置公网 location。

## 20. 防火墙

使用 UFW：

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
sudo ufw status verbose
```

确认没有为下列端口添加公网 allow 规则：

```text
8000 8877 40000 40080 8191 8788
```

`8000` 也应保持绑定 `127.0.0.1`，公网通过 Nginx 80/443 进入。

## 21. 使用 API

查看模型：

```bash
curl https://api.example.com/v1/models \
  -H 'Authorization: Bearer 你的API密钥'
```

文本对话：

```bash
curl https://api.example.com/v1/chat/completions \
  -H 'Authorization: Bearer 你的API密钥' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "auto",
    "messages": [
      {"role": "user", "content": "你好"}
    ]
  }'
```

只在本机测试时，可以将域名换成 `http://127.0.0.1:8000`。

## 22. 常见错误

更多完整日志片段、判断依据和 NovaApi/CPA 投递错误见 [日志错误示例与排查手册](TROUBLESHOOTING_LOG_EXAMPLES.md)。

### 22.1 `Grok Castle 运行需要 Node.js`

```bash
command -v node
sudo -u "$(systemctl show -p User --value chatgpt2api)" \
  /usr/bin/env PATH=/usr/local/bin:/usr/bin:/bin command -v node
```

确认 Node 安装在 `/usr/bin/node` 或 systemd unit 的 `PATH` 内。

### 22.2 Captcha Solver 启动失败

```bash
sudo systemctl status captcha-solver --no-pager
sudo journalctl -u captcha-solver -n 150 --no-pager
command -v xvfb-run
```

如果 Chromium 报缺少系统库，重新执行：

```bash
cd /opt/gptgrok-stack/chatgpt2api/captcha-solver
sudo .venv/bin/python -m playwright install-deps chromium
```

### 22.3 `Failed to verify Cloudflare turnstile token`

检查：

- Solver 结果是否为 `method: real-page`。
- `TURNSTILE_HEADLESS=0` 是否生效。
- systemd 是否通过 `xvfb-run` 启动。
- 注册和 solver 是否使用同一代理/IP。
- token 是否在获取后立即提交。

### 22.4 `url: private/loopback host blocked`

普通 Ubuntu DNS 通常不需要关闭 SSRF 防护。只有明确使用 fake-IP 代理，公网域名被解析到 `198.18.0.0/15` 时，才在 solver unit 中加入：

```ini
Environment=SOLVER_ALLOW_PRIVATE=1
```

修改后：

```bash
sudo systemctl daemon-reload
sudo systemctl restart captcha-solver
```

只有 solver 绑定 `127.0.0.1` 时才能这样配置。

### 22.5 iCloud Timeout

```bash
curl -I http://127.0.0.1:8788/login
docker logs --tail 150 chatgpt2api-icloud-privacy-mail
```

宿主机主程序应访问 `http://127.0.0.1:8788`，不是 `http://icloud-privacy-mail:8787`。另外确认注册中心选择的是本地 iCloud 还是外部 iCloud API。

### 22.6 Docker 辅助服务异常

```bash
docker compose -f /opt/gptgrok-stack/chatgpt2api/docker-compose.warp.yml \
  --profile local-icloud ps
docker logs --tail 150 chatgpt2api-warp-proxy
docker logs --tail 150 chatgpt2api-privoxy
docker logs --tail 150 chatgpt2api-flaresolverr
```

### 22.7 端口被占用

```bash
sudo ss -lntp | grep -E ':(8000|8877|40000|40080|8191|8788)\b'
```

确认是否同时启动了 Docker 版 app 和 systemd 版 app。不要在不知道进程用途时直接 `kill -9`。

## 23. 备份

```bash
cd /opt/gptgrok-stack/chatgpt2api
mkdir -p backups
tar -czf "backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz" \
  config.json .env data
git diff > "backups/local-code-$(date +%Y%m%d-%H%M%S).patch"
```

将备份复制到另一块磁盘或加密存储。备份可能含账号凭据和密钥，不要上传到公开 Git 仓库。

## 24. 升级

升级前先备份和查看本地改动：

```bash
cd /opt/gptgrok-stack/chatgpt2api
git status
```

不要在存在未保存的本地定制代码时盲目覆盖。

更新主程序后：

```bash
uv sync --python 3.13
cd web-vue
npm ci
npm run build:server
sudo systemctl restart chatgpt2api
```

更新 solver 后：

```bash
cd /opt/gptgrok-stack/chatgpt2api/captcha-solver
uv pip install --python .venv/bin/python -r requirements.txt
sudo systemctl restart captcha-solver
```

Solver 与主项目使用同一仓库版本，不再单独更新 `xai-grok-mass`。

## 25. 卸载

停止 systemd 服务：

```bash
sudo systemctl disable --now chatgpt2api
sudo systemctl disable --now captcha-solver
```

停止 Docker 辅助服务：

```bash
cd /opt/gptgrok-stack/chatgpt2api
docker compose -f docker-compose.warp.yml --profile local-icloud down
```

删除 systemd unit：

```bash
sudo rm /etc/systemd/system/chatgpt2api.service
sudo rm /etc/systemd/system/captcha-solver.service
sudo systemctl daemon-reload
```

删除源码和 `data/` 前必须先备份。

## 26. 安全检查

- [ ] GPTGrok2API 只监听 `127.0.0.1:8000`。
- [ ] Captcha Solver 只监听 `127.0.0.1:8877`。
- [ ] 公网只开放 SSH、HTTP 和 HTTPS。
- [ ] 管理密钥至少 24 字节且没有公开。
- [ ] `config.json` 和 `.env` 权限为 `600`。
- [ ] Nginx 使用 HTTPS。
- [ ] 没有把 `8877`、`40080`、`8191` 或 `8788` 公开到互联网。
- [ ] `SOLVER_ALLOW_PRIVATE=1` 只在明确需要且 solver 绑定回环时使用。
- [ ] 升级和批量操作前已备份。

## 27. 最终验收清单

- [ ] `docker run --rm hello-world` 成功。
- [ ] `node --version` 为 22.x 或兼容版本。
- [ ] `.venv/bin/python --version` 为 3.13.x 或更高。
- [ ] `systemctl status chatgpt2api` 为 active。
- [ ] `systemctl status captcha-solver` 为 active。
- [ ] `curl -I http://127.0.0.1:8000/` 返回 200。
- [ ] `curl http://127.0.0.1:8877/health` 返回 `status: ok`。
- [ ] Privoxy 出口测试成功。
- [ ] FlareSolverr 和 iCloud sidecar 容器为 healthy。
- [ ] Grok 打码 provider 为 `本地 Captcha Solver`。
- [ ] Solver smoke test 返回 `method: real-page`。
- [ ] 首个注册任务使用数量 `1`、并发 `1`。
- [ ] 单任务成功后再增加并发。
- [ ] 如启用 NovaApi 自动投递，实际镜像为作者魔改版且目标分组出现测试账号。
- [ ] 如启用 CPA 自动投递，CPA 中出现 OpenAI 的 `codex-*.json` 或 Grok 的 `xai-*.json`。

## 28. 快速命令表

```bash
# 主程序
sudo systemctl status chatgpt2api --no-pager
sudo systemctl restart chatgpt2api
sudo journalctl -u chatgpt2api -f

# Captcha Solver
sudo systemctl status captcha-solver --no-pager
sudo systemctl restart captcha-solver
sudo journalctl -u captcha-solver -f

# HTTP 健康
curl -I http://127.0.0.1:8000/
curl http://127.0.0.1:8877/health

# Docker 辅助服务
cd /opt/gptgrok-stack/chatgpt2api
docker compose -f docker-compose.warp.yml --profile local-icloud ps

# 代理
curl -x http://127.0.0.1:40080 https://www.cloudflare.com/cdn-cgi/trace

# iCloud sidecar
curl -I http://127.0.0.1:8788/login

# 端口
sudo ss -lntp | grep -E ':(8000|8877|40000|40080|8191|8788)\b'
```

故障排查时记录：错误时间、任务编号、完整错误文本、`journalctl -u chatgpt2api -n 150`、`journalctl -u captcha-solver -n 150`、当前启用的邮箱 provider、代理和 solver 配置。
