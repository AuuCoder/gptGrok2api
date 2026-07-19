# GPTGrok2API 从零搭建与使用手册（macOS 本地版）

本文面向第一次接触命令行、Docker 和本地服务的用户。手册以已实测的 macOS 混合部署为主线：

Ubuntu/Linux 用户请阅读 [Ubuntu/Linux 从零搭建手册](BEGINNER_UBUNTU_SETUP.md)。

注册成功后需要自动上传到作者魔改 NovaApi（Sub2API）或 CPA 时，请阅读 [自动上传配置手册](AUTO_UPLOAD_SUB2API_CPA.md)。

- GPTGrok2API 主程序在 macOS 宿主机上运行。
- WARP、Privoxy、FlareSolverr 和 iCloud Privacy Mail 在 Docker 中运行。
- Captcha Solver 在 macOS 宿主机上运行，使用真实 Chromium 处理 Grok Turnstile。
- 主程序和 Captcha Solver 通过 `launchd` 自动启动。

> 先完成“最小可用搭建”，确认管理页和各个健康检查正常，再配置邮箱、代理和注册任务。不要一上来就开高并发。

## 1. 你将得到什么

搭建完成后，本机会有以下服务：

| 服务 | 用途 | 本机地址 |
| --- | --- | --- |
| GPTGrok2API | 管理后台、账号池、注册中心、OpenAI 兼容 API | `http://127.0.0.1:8000` |
| Captcha Solver | Grok Turnstile 和其他验证码 sidecar | `http://127.0.0.1:8877` |
| WARP SOCKS5 | WARP 出口 | `127.0.0.1:40000` |
| Privoxy | 把 WARP SOCKS5 转成 HTTP 代理 | `http://127.0.0.1:40080` |
| FlareSolverr | Cloudflare clearance 清障 | `http://127.0.0.1:8191` |
| iCloud Privacy Mail | 本地 iCloud 隐私邮箱 sidecar | `http://127.0.0.1:8788` |

### 1.1 不要同时启动两个主程序

本手册只在 Docker 中启动辅助服务，主程序由 macOS `launchd` 启动。不要再执行不带服务名的：

```bash
docker compose -f docker-compose.warp.yml up -d
```

该命令还会启动 Docker 版 `app`，可能与宿主机主程序产生端口、数据和配置混淆。可以用下面的命令检查是否重复运行：

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
docker ps --format '{{.Names}}\t{{.Ports}}' | grep chatgpt2api
```

常用数据保存在：

- `chatgpt2api/config.json`：系统配置和管理密钥。
- `chatgpt2api/.env`：Docker 和宿主机环境变量。
- `chatgpt2api/data/`：账号、注册配置、邮箱、任务和统计数据。
- `chatgpt2api/logs/`：主程序日志。
- `chatgpt2api/logs/captcha-solver*.log`：打码服务日志。

## 2. 费用和硬件要求

### 2.1 可能产生的费用

代码本身可本地运行，但整体不等于永久零成本：

- Turnstile 本地浏览器解题不按次向 YesCaptcha/2Captcha 付费。
- WARP 基础服务可免费，但住宅代理或其他稳定代理通常收费。
- reCAPTCHA/hCaptcha 图片题会调用 Mistral Vision，需要 API Key，可能按量收费。Grok Turnstile 路径不需要 Mistral。
- VPS、公网域名、流量、邮箱 API 和第三方账号都可能产生费用。

### 2.2 推荐硬件

| 用途 | 最低建议 | 更舒适的配置 |
| --- | --- | --- |
| 单线程调试 | 8 GB 内存，4 核 CPU | 16 GB 内存 |
| 2-3 个注册并发 | 16 GB 内存 | 24 GB 以上 |
| 多 solver worker | 每个 Chromium 预留数百 MB | 32 GB 以上 |

需要保留至少 5 GB 可用磁盘空间。CloakBrowser 首次运行会下载大约 140-200 MB 的 Chromium。

## 3. 安装基础工具

### 3.1 安装 Xcode Command Line Tools

打开“终端”，执行：

```bash
xcode-select --install
```

如果提示已安装，可以继续。

### 3.2 安装 Homebrew

先检查：

```bash
brew --version
```

如果提示 `command not found`，按 [Homebrew 官网](https://brew.sh/) 的安装命令安装。Apple Silicon Mac 的 Homebrew 默认位于 `/opt/homebrew`。

### 3.3 安装 Git、Node.js 和 uv

```bash
brew install git node uv
```

检查：

```bash
git --version
node --version
npm --version
uv --version
```

Node.js 是 Grok Castle 安全 token 的必需依赖。不能只安装 Python。

### 3.4 安装 Docker Desktop

从 [Docker Desktop for Mac](https://www.docker.com/products/docker-desktop/) 安装并启动 Docker Desktop。等菜单栏的 Docker 图标显示已运行，再执行：

```bash
docker version
docker compose version
```

## 4. 下载项目

本文统一使用目录 `~/Documents/注册机`。路径可以更改，但后面的命令必须保持一致。

```bash
mkdir -p "$HOME/Documents/注册机"
cd "$HOME/Documents/注册机"
```

仓库已经公开，直接使用 HTTPS 下载主项目：

```bash
git clone https://github.com/AuuCoder/gptGrok2api.git chatgpt2api
```

Captcha Solver 已经内置在主仓库中。最终目录应类似：

```text
~/Documents/注册机/
└── chatgpt2api/
    ├── captcha-solver/
    ├── services/
    └── web-vue/
```

> 本地 Captcha Solver provider、实页 Turnstile 修复、代理透传和 solver 源码都在当前主仓库中，不需要再下载 `xai-grok-mass`。

## 5. 安装 GPTGrok2API

### 5.1 安装 Python 依赖

```bash
cd "$HOME/Documents/注册机/chatgpt2api"
uv sync
```

完成后会生成 `.venv/`。检查：

```bash
.venv/bin/python --version
```

项目要求 Python 3.13 或更高版本。

### 5.2 安装前端依赖并构建

```bash
cd "$HOME/Documents/注册机/chatgpt2api/web-vue"
npm ci
npm run build:server
```

`build:server` 会将前端产物复制到主程序使用的 `web_dist/`。

### 5.3 创建管理密钥

进入项目根目录：

```bash
cd "$HOME/Documents/注册机/chatgpt2api"
```

如果还没有 `config.json`，创建一个最小配置：

```json
{
  "auth-key": "请替换为至少-24-位的随机密钥"
}
```

可以使用下面的命令生成随机值：

```bash
openssl rand -hex 24
```

不要使用 `123456`、`admin`或公开示例值。也不要将真实密钥发到群聊或提交到 Git。

### 5.4 创建 `.env`

```bash
cp .env.example .env
```

本机混合部署至少确认下列项：

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
```

`CHATGPT2API_AUTH_KEY` 可以不写，此时使用 `config.json` 中的 `auth-key`。

## 6. 启动 WARP、Privoxy、FlareSolverr 和 iCloud sidecar

在 `chatgpt2api` 目录执行：

```bash
cd "$HOME/Documents/注册机/chatgpt2api"
docker compose -f docker-compose.warp.yml --profile local-icloud up -d \
  warp-proxy privoxy flaresolverr icloud-privacy-mail
```

检查容器：

```bash
docker compose -f docker-compose.warp.yml --profile local-icloud ps
```

正常情况下，相关服务应显示 `Up` 或 `healthy`。

逐个测试：

```bash
curl -I http://127.0.0.1:8191/
curl -I http://127.0.0.1:8788/login
curl -x http://127.0.0.1:40080 https://www.cloudflare.com/cdn-cgi/trace
```

如果 `8788` 没有监听，确认启动命令包含 `--profile local-icloud`。

## 7. 安装 Captcha Solver

### 7.1 推荐方式：Docker/Xvfb 后台有头浏览器

macOS 会把原生 Chromium 窗口强制放回可见屏幕。需要“有头模式但不弹窗”时，使用 Docker Desktop 中的 Linux Xvfb：

```bash
cd "$HOME/Documents/注册机/chatgpt2api"
docker compose -f deploy/docker-compose.captcha-solver.yml build
docker compose -f deploy/docker-compose.captcha-solver.yml up -d
curl http://127.0.0.1:8877/health
```

容器设置了 `restart: unless-stopped`，Docker Desktop 启动后会自动恢复。浏览器下载保存在命名卷 `deploy_cloakbrowser-cache`，重建容器不会重复下载。

注册代理如果是 `http://127.0.0.1:40080`，容器会自动转换成 `http://host.docker.internal:40080`，仍然访问 Mac 上的代理。

常用维护命令：

```bash
docker compose -f deploy/docker-compose.captcha-solver.yml ps
docker compose -f deploy/docker-compose.captcha-solver.yml logs -f
docker compose -f deploy/docker-compose.captcha-solver.yml restart
docker compose -f deploy/docker-compose.captcha-solver.yml down
```

不要同时加载后文的 solver LaunchAgent，否则两者会争用 `8877` 端口。

### 7.2 备用方式：创建原生 Python 环境

不使用 Docker 时可按下面方式运行，但 `TURNSTILE_HEADLESS=0` 会在 macOS 桌面显示 Chromium 窗口。

```bash
cd "$HOME/Documents/注册机/chatgpt2api/captcha-solver"
uv venv --python "$HOME/Documents/注册机/chatgpt2api/.venv/bin/python" .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

检查：

```bash
.venv/bin/python -c 'import cloakbrowser, fastapi, uvicorn; print("solver dependencies ok")'
```

### 7.3 Captcha Solver 重要环境变量

| 变量 | 本机推荐值 | 说明 |
| --- | --- | --- |
| `TURNSTILE_HEADLESS` | `0` | xAI 实测中无头模式无法在合理时间获得 token |
| `SOLVER_ALLOW_PRIVATE` | `1` | Clash/fake-IP 常把公网域名解析到 `198.18.0.0/15`，会被 SSRF 防护误拦截 |
| `TURNSTILE_PROXY` | 可留空 | 注册机会按次将当前注册代理透传给 solver |
| `PORT` | `8877` | Solver HTTP 端口 |
| `SOLVER_LOOPBACK_PROXY_HOST` | Docker 中为 `host.docker.internal` | 将容器内收到的本机回环代理转换到 Mac 宿主机 |

16 GB Mac 建议注册线程和“注册解题并发”都从 `3` 开始。推荐参数：单次解题 `45` 秒、排队超时 `60` 秒、本地尝试 `3` 次、总解题超时 `180` 秒。开启即时 OAuth 后会自动增加第 4 个 solver 槽位。直接提高到 `5+` 通常会先耗尽 CPU 和压缩内存，成功率未必更高。

`SOLVER_ALLOW_PRIVATE=1` 只应在 solver 绑定 `127.0.0.1` 时使用。不要在公网开放的 solver 上关闭 SSRF 保护。

### 7.4 原生方式手动启动测试

```bash
cd "$HOME/Documents/注册机/chatgpt2api/captcha-solver"
PORT=8877 TURNSTILE_HEADLESS=0 SOLVER_ALLOW_PRIVATE=1 .venv/bin/python server.py
```

保持该终端窗口不要关闭，再打开一个新终端测试：

```bash
curl http://127.0.0.1:8877/health
```

应返回：

```json
{
  "status": "ok",
  "supported_types": ["turnstile", "recaptcha", "hcaptcha", "cloudflare", "awswaf", "botguard", "datadome", "perimeterx"]
}
```

按 `Control+C` 停止手动测试。

### 7.5 首次浏览器下载

首次真正解题时，CloakBrowser 会下载并校验 Chromium，日志会显示下载进度。只要最终出现 `Binary ready` 就是正常的。

## 8. 配置 macOS 开机自启

主程序使用 LaunchAgent。Captcha Solver 如果已经按 7.1 使用 Docker，就不要再加载 solver LaunchAgent。

项目提供了两个原生模板：

- `deploy/launchd/com.chatgpt2api.app.plist.example`
- `deploy/launchd/com.chatgpt2api.captcha-solver.plist.example`

### 8.1 生成主程序 plist

```bash
APP_DIR="$HOME/Documents/注册机/chatgpt2api"
mkdir -p "$HOME/Library/LaunchAgents" "$APP_DIR/logs"
sed "s|__APP_DIR__|$APP_DIR|g" \
  "$APP_DIR/deploy/launchd/com.chatgpt2api.app.plist.example" \
  > "$HOME/Library/LaunchAgents/com.chatgpt2api.app.plist"
plutil -lint "$HOME/Library/LaunchAgents/com.chatgpt2api.app.plist"
```

### 8.2 仅原生方式：生成 solver plist

```bash
APP_DIR="$HOME/Documents/注册机/chatgpt2api"
mkdir -p "$APP_DIR/logs"
sed "s|__APP_DIR__|$APP_DIR|g" \
  "$APP_DIR/deploy/launchd/com.chatgpt2api.captcha-solver.plist.example" \
  > "$HOME/Library/LaunchAgents/com.chatgpt2api.captcha-solver.plist"
plutil -lint "$HOME/Library/LaunchAgents/com.chatgpt2api.captcha-solver.plist"
```

### 8.3 加载服务

主程序始终加载：

```bash
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.chatgpt2api.app.plist"
```

只有选择原生 solver 时才执行：

```bash
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.chatgpt2api.captcha-solver.plist"
```

如果提示已经加载，先执行：

```bash
launchctl bootout "gui/$(id -u)/com.chatgpt2api.app"
launchctl bootout "gui/$(id -u)/com.chatgpt2api.captcha-solver"
```

然后重新执行 `bootstrap`。

### 8.4 检查服务

```bash
launchctl print "gui/$(id -u)/com.chatgpt2api.app"
curl -I http://127.0.0.1:8000/
curl http://127.0.0.1:8877/health
```

Docker solver 额外检查：

```bash
docker compose -f "$HOME/Documents/注册机/chatgpt2api/deploy/docker-compose.captcha-solver.yml" ps
```

原生 solver 额外检查：

```bash
launchctl print "gui/$(id -u)/com.chatgpt2api.captcha-solver"
```

常用重启命令：

```bash
launchctl kickstart -k "gui/$(id -u)/com.chatgpt2api.app"
```

solver 根据实际方式二选一：

```bash
docker compose -f "$HOME/Documents/注册机/chatgpt2api/deploy/docker-compose.captcha-solver.yml" restart
# 或者原生方式：
launchctl kickstart -k "gui/$(id -u)/com.chatgpt2api.captcha-solver"
```

## 9. 第一次登录管理后台

浏览器打开：

```text
http://127.0.0.1:8000
```

在“管理密钥”中输入 `config.json` 的 `auth-key`。

首次登录后先不要立即开始注册，按下列顺序检查：

1. 打开“系统设置”，确认管理密钥和 API 基本配置。
2. 打开“代理管理”，确认 WARP/Privoxy 可用。
3. 打开“注册中心”，配置邮箱来源。
4. 将注册数量设为 `1`，并发设为 `1`。
5. 只有单任务跑通后才提高并发。

## 10. 配置代理

### 10.1 宿主机混合部署的地址

当 GPTGrok2API 直接运行在 macOS 上时，使用：

```text
HTTP 代理：http://127.0.0.1:40080
FlareSolverr：http://127.0.0.1:8191
```

不要填 `http://privoxy:8118` 或 `http://flaresolverr:8191`，这些名称只能在 Docker 网络内解析。

### 10.2 推荐顺序

1. 先使用 WARP/Privoxy 跑单任务。
2. 如果出口被限制，再替换稳定的 HTTP/SOCKS5 代理。
3. 注册请求和 Captcha Solver 必须使用同一出口。
4. 改代理后要重新测试 Turnstile，不要沿用旧 token。

### 10.3 测试出口

```bash
curl -x http://127.0.0.1:40080 https://www.cloudflare.com/cdn-cgi/trace
```

如果很快返回文本，说明 Privoxy 链路基本可用。这不代表目标站一定不会风控。

## 11. 配置邮箱来源

注册中心可配置多个邮箱 provider，但小白建议每次只启用一个，否则很难判断失败来自哪个邮箱源。

### 11.1 Cloudflare Temp Email

需要填写对应服务的 API Base、管理密码和域名。先使用提供商文档中的健康检查确认 API 可访问。

常见问题：

- API Base 末尾重复填写 `/api`。
- 管理密码错误。
- 域名 DNS/MX 尚未生效。
- 邮件到达延迟超过 `wait_timeout`。

### 11.2 iCloud 邮箱（本系统）

选择 `iCloud 邮箱（本系统）` 时，主程序通过 `http://127.0.0.1:8788` 访问 Docker sidecar，不需要在注册 provider 中填 API Key。

使用前必须：

1. Docker 中的 `chatgpt2api-icloud-privacy-mail` 显示 healthy。
2. 打开后台“iCloud 邮箱”页面。
3. 完成 Apple 登录和 2FA。
4. 先手动创建一个隐私邮箱，并确认状态正常。
5. 在注册中心启用 `iCloud 邮箱（本系统）`。

健康检查：

```bash
curl -I http://127.0.0.1:8788/login
```

如果在宿主机运行主程序，但配成 `http://icloud-privacy-mail:8787`，将出现连接错误或 Timeout。

### 11.3 iCloud API（外部服务）

`iCloud API` 与本地 sidecar 不是同一选项。它需要填写外部 API Base 和 API Key。外部服务超时时，日志会显示：

```text
iCloud Privacy Mail 请求失败（Timeout）
```

这不代表本地 `8788` sidecar 一定有问题，要先确认当前启用的 provider 类型。

### 11.4 Outlook Token 邮箱池

导入前先确认凭据格式和授权类型。小白建议先导入 1-2 条做收件测试，不要一次导入大量未验证凭据。

## 12. 配置 Grok 本地打码

打开“注册中心”，将目标选为 `Grok（协议）`。

Turnstile 配置：

| 字段 | 推荐值 |
| --- | --- |
| Turnstile 服务 | `本地 Captcha Solver` |
| API Key | 本地模式不需要 |
| API Base | `http://127.0.0.1:8877` |
| HTTP 超时 | `30` |
| 注册解题并发 | 与注册线程数相同，建议从 `1-3` 开始 |
| 解题超时 | `180` |
| 轮询间隔 | 本地模式基本不使用，保持 `3` |

当前定制版的本地 provider 会：

1. 将 xAI 注册页 URL 和 sitekey 发送到 `/solve`。
2. 默认使用 `real_page:true`。
3. 将本次注册代理透传给 solver。
4. 在真实 xAI 页面上执行 `window.turnstile.render()`。
5. 将 callback 产生的 token 返回注册协议。

本机实测中：

- 实页有界面模式约 11 秒获得 token。
- 无头实页模式等待 90 多秒仍无 token。
- route-intercept 可以产生 token，但 xAI 会拒绝该 token。

因此小白不要修改 `TURNSTILE_HEADLESS=0` 和 `real_page:true`。

## 13. 执行第一个 Grok 注册任务

首次建议：

| 设置 | 值 |
| --- | --- |
| 注册数量 | `1` |
| 并发数 | `1` |
| 邮箱 provider | 只启用一个已验证 provider |
| 代理 | 使用默认 WARP/Privoxy 或一个已验证代理 |
| Turnstile provider | `本地 Captcha Solver` |

正常日志顺序类似：

```text
准备注册环境
获取注册邮箱
验证码已发送，等待邮件
邮箱验证完成，正在进行安全校验
安全校验完成，正在创建账号
注册成功
Grok OAuth 授权已进入即时上传队列
Grok OAuth 授权完成，已上传到 NovaApi
```

使用本文的 Docker/Xvfb 部署时，Chromium 在虚拟显示器中运行，不会弹出本机浏览器窗口。

## 14. 并发和多线程

### 14.1 当前默认行为

Captcha Solver 使用一个共享的动态并发限制器。配置中的“注册解题并发”表示注册任务可使用的浏览器槽位：

- 注册机可以并发处理邮箱和网络步骤。
- 注册线程数为 `3`、注册解题并发为 `3` 时，最多同时运行 3 个注册 Turnstile。
- 开启“注册后自动协议授权”后，程序会自动预留第 4 个槽位给单线程 OAuth 上传队列。
- 注册成功后不再等待整批结束，而是立即依次完成 OAuth 和 NovaApi/CPA 投递。
- 日志中的 `solver 总槽位 4` 是 `3 个注册槽位 + 1 个 OAuth 槽位`，不是配置被错误修改。

首次搭建保持并发 `1`。稳定后可以增加到 `2-3`。

### 14.2 资源占用

每个活跃槽位都可能启动独立 Chromium。注册 3 线程并开启即时 OAuth 时，Docker 内存通常会比纯注册增加约一个浏览器实例。内存紧张时优先把注册线程和“注册解题并发”同时降为 `2`，不要启动多个 Uvicorn worker；多 worker 会拆散限流状态和 `/status` 数据。

## 15. OpenAI 注册说明

注册中心同时支持 OpenAI 目标。需要注意：

- 本文的本地 Captcha Solver provider 目前主要接入 Grok 协议注册的 Turnstile。
- OpenAI Sentinel/Turnstile 还有自己的 token 逻辑，不能把 Grok token 直接复用到 OpenAI。
- 切换注册目标时，检查邮箱 provider 的 `project`、关键字和领取状态。
- 先使用数量 `1`、并发 `1` 单独验证 OpenAI 流程。

## 16. 使用统一 API

已经导入或注册账号后，可以通过 OpenAI 兼容 API 调用。

查看模型：

```bash
curl http://127.0.0.1:8000/v1/models \
  -H 'Authorization: Bearer 你的管理或API密钥'
```

文本对话：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Authorization: Bearer 你的管理或API密钥' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "auto",
    "messages": [
      {"role": "user", "content": "你好"}
    ]
  }'
```

如果返回 `401`，检查 Bearer 密钥。如果返回“无可用账号”，进入后台账号管理页检查账号状态、分组和额度。

## 17. 日志和状态检查

### 17.1 主程序日志

```bash
tail -f "$HOME/Documents/注册机/chatgpt2api/logs/chatgpt2api-launchd.log"
tail -f "$HOME/Documents/注册机/chatgpt2api/logs/chatgpt2api-launchd.error.log"
```

### 17.2 Solver 日志

```bash
tail -f "$HOME/Documents/注册机/chatgpt2api/logs/captcha-solver.log"
tail -f "$HOME/Documents/注册机/chatgpt2api/logs/captcha-solver.error.log"
```

Solver HTTP 状态：

```bash
curl http://127.0.0.1:8877/status
curl 'http://127.0.0.1:8877/logs?lines=20'
```

### 17.3 Docker 日志

```bash
docker logs -f chatgpt2api-warp-proxy
docker logs -f chatgpt2api-privoxy
docker logs -f chatgpt2api-flaresolverr
docker logs -f chatgpt2api-icloud-privacy-mail
```

## 18. 常见错误对照表

更多完整日志片段、判断依据和 NovaApi/CPA 投递错误见 [日志错误示例与排查手册](TROUBLESHOOTING_LOG_EXAMPLES.md)。

### 18.1 `Grok Castle 运行需要 Node.js`

原因：

- 没有安装 Node.js。
- Node 在 `/opt/homebrew/bin/node`，但 `launchd` 的 `PATH` 没有 `/opt/homebrew/bin`。

检查：

```bash
command -v node
node --version
launchctl print "gui/$(id -u)/com.chatgpt2api.app" | grep PATH
```

主程序 plist 中应包含：

```text
/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
```

### 18.2 `iCloud Privacy Mail 请求失败（Timeout）`

依次检查：

1. 当前启用的是 `iCloud API` 还是 `iCloud 邮箱（本系统）`。
2. 本地 sidecar 是否 healthy。
3. 宿主机配置是否使用 `http://127.0.0.1:8788`。
4. 外部 API Base 是否可访问。
5. 邮箱 provider 是否误开了多个。

### 18.3 `[internal] Failed to verify Cloudflare turnstile token.`

常见原因：

- 使用了 route-intercept token，而目标要求实页 token。
- 注册请求与 solver 的代理/IP 不一致。
- 无头浏览器未正常完成挑战。
- token 等待过久或已被使用。

本定制版应使用 `real_page:true`、`TURNSTILE_HEADLESS=0` 和同一代理。查看 solver 日志的 `method` 应为 `real-page`。

### 18.4 `url: private/loopback host blocked`

本机代理软件可能将公网域名解析到 `198.18.x.x` fake-IP。对仅绑定 `127.0.0.1` 的 solver，在 plist 中设置：

```xml
<key>SOLVER_ALLOW_PRIVATE</key>
<string>1</string>
```

如果 solver 暴露到公网，不要使用该配置。

### 18.5 `Connection refused` / `Couldn't connect`

检查端口：

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
lsof -nP -iTCP:8877 -sTCP:LISTEN
lsof -nP -iTCP:8788 -sTCP:LISTEN
lsof -nP -iTCP:8191 -sTCP:LISTEN
```

没有输出表示对应服务没有监听。

### 18.6 Solver 一直等待 token

检查：

- `TURNSTILE_HEADLESS` 是否误设为 `1`。
- Chromium 窗口是否被手动关闭。
- 代理是否可访问 xAI 和 Cloudflare challenge 域名。
- Solver 日志是否出现 `Real-page checkbox clicked` 和 `method=real-page`。
- 解题超时是否低于 `60`。

### 18.7 端口被占用

例如 `8877` 被占用：

```bash
lsof -nP -iTCP:8877 -sTCP:LISTEN
```

先确认占用者是什么服务。不要盲目使用 `kill -9`。如果是旧 solver，使用 `launchctl bootout` 停止对应 LaunchAgent。

## 19. 备份

升级、改配置或批量注册前，先备份：

```bash
cd "$HOME/Documents/注册机/chatgpt2api"
mkdir -p backups
tar -czf "backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).tgz" \
  config.json .env data
```

还建议保存当前定制代码改动：

```bash
git status
git diff > "backups/local-code-$(date +%Y%m%d-%H%M%S).patch"
```

备份文件可能包含密钥、邮箱和账号凭据，不要上传到公开网盘或公开 Git 仓库。

## 20. 升级

先备份，然后查看本地改动：

```bash
cd "$HOME/Documents/注册机/chatgpt2api"
git status
```

当存在本地定制改动时，不要直接覆盖。先保存 patch 或提交到自己的分支，再拉取上游。

常规升级后执行：

```bash
uv sync
cd web-vue
npm ci
npm run build:server
cd ..
launchctl kickstart -k "gui/$(id -u)/com.chatgpt2api.app"
```

Solver 与主项目一起升级。主项目更新完成后重新同步 solver 依赖：

```bash
cd "$HOME/Documents/注册机/chatgpt2api/captcha-solver"
uv pip install --python .venv/bin/python -r requirements.txt
launchctl kickstart -k "gui/$(id -u)/com.chatgpt2api.captcha-solver"
```

不再需要单独更新 `xai-grok-mass`；本地实页修复跟随 GPTGrok2API 仓库版本发布。

## 21. 停止和卸载

停止主程序和 solver：

```bash
launchctl bootout "gui/$(id -u)/com.chatgpt2api.app"
launchctl bootout "gui/$(id -u)/com.chatgpt2api.captcha-solver"
```

停止 Docker 辅助服务：

```bash
cd "$HOME/Documents/注册机/chatgpt2api"
docker compose -f docker-compose.warp.yml --profile local-icloud down
```

取消开机自启：

```bash
rm "$HOME/Library/LaunchAgents/com.chatgpt2api.app.plist"
rm "$HOME/Library/LaunchAgents/com.chatgpt2api.captcha-solver.plist"
```

不要在没有备份时删除 `data/`、`config.json` 或 `.env`。

## 22. 安全注意事项

1. Captcha Solver 本身没有内置强制认证，必须绑定 `127.0.0.1`。
2. 不要把 `8877`、`8788`、`8191`、`40080` 直接映射到公网。
3. `config.json`、`.env`、`data/`、日志和备份中可能包含密钥或凭据。
4. 不要将管理密钥与公开 API Key 混用。
5. 公网部署时应在前面使用 HTTPS、反向代理和访问控制。
6. 不要在公网 solver 上使用 `SOLVER_ALLOW_PRIVATE=1`。
7. 账号、邮箱、代理和 API 的使用应限定在你有权管理的测试环境。

## 23. 最终验收清单

全部完成后逐项检查：

- [ ] `node --version` 正常。
- [ ] `docker compose version` 正常。
- [ ] `http://127.0.0.1:8000` 可打开并登录。
- [ ] `curl http://127.0.0.1:8877/health` 返回 `status: ok`。
- [ ] Privoxy `127.0.0.1:40080` 可访问公网。
- [ ] FlareSolverr `127.0.0.1:8191` 可访问。
- [ ] iCloud sidecar `127.0.0.1:8788/login` 可访问（使用 iCloud 时）。
- [ ] 注册中心只启用一个已验证邮箱 provider。
- [ ] Grok Turnstile 选择 `本地 Captcha Solver`。
- [ ] API Base 为 `http://127.0.0.1:8877`。
- [ ] 首次注册数量和并发均为 `1`。
- [ ] Solver 日志中的解题方式为 `real-page`。
- [ ] 单任务成功后再增加并发。

## 24. 一页快速命令表

```bash
# 主程序健康
curl -I http://127.0.0.1:8000/

# Solver 健康
curl http://127.0.0.1:8877/health

# 代理测试
curl -x http://127.0.0.1:40080 https://www.cloudflare.com/cdn-cgi/trace

# iCloud sidecar
curl -I http://127.0.0.1:8788/login

# Docker 容器
docker compose -f docker-compose.warp.yml --profile local-icloud ps

# 重启主程序
launchctl kickstart -k "gui/$(id -u)/com.chatgpt2api.app"

# 重启 solver
launchctl kickstart -k "gui/$(id -u)/com.chatgpt2api.captcha-solver"

# 主程序日志
tail -f "$HOME/Documents/注册机/chatgpt2api/logs/chatgpt2api-launchd.log"

# Solver 日志
tail -f "$HOME/Documents/注册机/chatgpt2api/logs/captcha-solver.log"
```

如果出现问题，先对照“常见错误对照表”，然后保留以下信息：出错时间、任务编号、完整错误文本、主程序最后 100 行日志、solver 最后 100 行日志和当前启用的邮箱/Turnstile provider。

## 25. Docker-only 和 Linux VPS 说明

### 25.1 Docker 中运行主程序

如果主程序运行在 Docker 中，容器里的 `127.0.0.1` 指向容器自身，不是 macOS 宿主机。此时本地 solver API Base 应使用：

```text
http://host.docker.internal:8877
```

但 Docker 内的注册代理可能是 `http://privoxy:8118`，而宿主机 solver 无法解析 Docker 内部主机名 `privoxy`。需要额外将代理改成宿主机可访问的 `http://127.0.0.1:40080`，或者将 solver 也部署到同一 Docker 网络。小白建议先按本文的宿主机混合方案搭建。

### 25.2 Linux VPS

Linux 无图形桌面时，xAI Turnstile 仍需要有界面浏览器模式。可以使用 `Xvfb` 提供虚拟显示器：

```bash
sudo apt-get update
sudo apt-get install -y xvfb
xvfb-run -a --server-args='-screen 0 1920x1080x24' \
  .venv/bin/python server.py
```

Linux 上建议使用 systemd 管理主程序和 solver，不要使用 macOS LaunchAgent 模板。同样应让 solver 只监听本机或内网，不要直接暴露未认证的 `8877` 端口。
