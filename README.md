# GPTGrok2API

GPTGrok2API 是一个自托管的 GPT 与 Grok 统一网关，将已接入的订阅账号能力转换为 OpenAI 兼容 API，并提供账号、代理、注册、日志、图片和运行状态管理控制台。

## 社区入口

- Telegram：[加入 Telegram 群组](https://t.me/+olcHGKKXEwRmOTQx)
- QQ 群：`934890216`

<img src="docs/images/qq-group-934890216.png" alt="AI 智障 QQ 群 934890216 二维码" width="256">

## 核心能力

| 模块 | 当前能力 |
| --- | --- |
| 统一 API 网关 | 合并 GPT、Grok SSO 与 Grok Build OAuth 模型目录；根 `/v1` 根据模型自动分流，同时保留 `/grok/v1` 完整 Grok 接口。 |
| 协议兼容 | 提供 Chat Completions、Responses、Images、Anthropic Messages 兼容入口，支持流式与非流式响应、工具调用、搜索和推理强度。 |
| GPT 运行时 | 支持 GPT 文本对话、网页搜索、图片生成、图片编辑、会话复用、文件下载以及 PPT/PSD 等可编辑文件任务。 |
| Grok SSO 运行时 | 内置 Grok 账号池、额度刷新、账号调度、聊天、Responses、Messages、图片生成、图片编辑、视频生成和媒体缓存。 |
| Grok 后台探测与恢复 | 服务启动后自动探测已加入运行池的非禁用账号；明确失效时使用已保存邮箱和密码重新登录、验证并替换 SSO，无需手动操作。 |
| Grok Build OAuth | 支持 Device Code 与公网版 Authorization Code + PKCE 授权、Access/Refresh Token 导入、自动刷新、模型探测以及独立 OAuth 凭据存储；新注册可把完整会话直接交给 PKCE，不落盘临时 cookie。 |
| 账号生命周期 | 支持账号导入、分组、标签、额度同步、异常状态识别、限流恢复、无效账号清理、批量操作、代理绑定和运行状态监控。 |
| iCloud Privacy Mail | 内置 sidecar；新接口负责 Apple 登录、2FA 和创建隐私邮箱，旧接口负责登录、2FA 和同步已有邮箱，IMAP App 专用密码负责取验证码。 |
| iCloud 定时创建 | 按 Apple 账号定时创建邮箱；新接口每小时最多 20 个、旧接口每小时最多 5 个，每账号累计 750 个后自动停止。sidecar 使用 Compose 内部网络，不需要独立账号或宿主机端口。 |
| 邮箱平台标签 | 同一邮箱可分别标记 GPT 和 Grok；GPT 标签为绿色、Grok 标签为蓝色，两个标签同时存在才算已使用，注册领取按目标平台隔离。 |
| 注册中心 | 支持 OpenAI 与 Grok 注册任务，整合临时邮箱、GPTMail、Outlook Token、Microsoft Alias 和 iCloud Privacy Mail；内置 iCloud provider 不需要填写域名、API Base 或 API Key。 |
| 本地 Captcha Solver | 源码内置于 `captcha-solver/`，使用 Docker/Xvfb 中的有头 CloakBrowser/Chromium 处理 Grok Turnstile，支持动态并发、代理透传和浏览器资源回收，不需要额外克隆第二个仓库。 |
| Checkout 提链 | 注册 Checkout 仅保留 UPI 最终支付链接提取；IN Checkout、Provider、Approve 共享同一 sticky 出口，VN Promotion 使用独立代理持续轮换重试。 |
| 代理与稳定出口 | 支持全局代理、账号代理、代理配置、代理组、节点并发限制、故障反馈、备用出口、WARP、Privoxy、FlareSolverr 和 Clearance 刷新。 |
| 外部系统接入 | 支持从 Sub2API、远程 CPA、本地 CPA 和 Access Token 导入账号；Grok 注册成功后立即进入 2-worker OAuth 优先级队列，并按配置投递 NovaApi 与 CPA；服务器部署提供 `gptgrok2api` Docker 网络别名。 |
| 管理控制台 | 提供概览、GPT/Grok 账号与运行池管理、iCloud 邮箱、注册任务、Checkout 任务、代理管理、日志、实时监控、图片管理、调试中心和系统设置。 |
| 存储与备份 | 账号数据支持 JSON、SQLite、PostgreSQL 和 Git 后端；图片支持本地与 WebDAV；备份支持本地归档和 R2。 |
| 运维与发布 | 支持 Docker Compose、WARP 编排、内置 sidecar、Nginx HTTPS、运行日志、指标趋势、健康检查和 GitHub Releases 更新源。 |

## iCloud Privacy Mail 模块

控制台的“iCloud 邮箱”是内置 sidecar 的统一管理入口。sidecar 仅作为 Compose 内部服务运行，不暴露独立端口，也不需要额外的 sidecar 账户；主系统管理员登录后直接使用 Apple 登录态、2FA、隐私邮箱和取件功能。

sidecar 构建源码已随本仓库保存于 `deploy/icloud-privacy-mail/source`，构建过程不再从其他代码仓库下载源码；如需在线更新，可通过配置 manifest 或明确指定更新仓库启用。

使用 WARP 编排启动 sidecar：

```bash
docker compose -f docker-compose.warp.yml --profile local-icloud up -d
```

主应用通过内部网络访问 sidecar；通常无需额外配置 `ICLOUD_PRIVACY_MAIL_BASE_URL`。进入控制台“iCloud 邮箱”后，直接按页面流程完成 Apple 新/旧接口 2FA：新接口用于创建隐私邮箱，旧接口用于同步已有隐私邮箱；iCloud App 专用密码仅用于 IMAP 取验证码。历史版本产生的未绑定邮箱会在 sidecar 重启时按当前 Apple 登录态自动补齐归属，不会改变邮箱地址、API token 或 GPT/Grok 领取记录。

注册区的邮箱 provider 有两种 iCloud 入口：`iCloud 邮箱（本系统）` 直接使用当前模块创建邮箱和取码，不需要填写 API Base、API Key 或域名；`iCloud API` 继续保留给独立部署的外部服务。已有 GPT 邮箱会显示绿色“GPT 已注册”标签，已有 Grok 邮箱会显示蓝色“Grok 已注册”标签；注册流程会按目标平台领取未标记邮箱，成功后写入对应标签，失败会释放该平台标签。

定时创建按账号执行：新接口每个账号每小时最多 `20` 个，旧接口每个账号每小时最多 `5` 个；两种登录态都保存后每小时最多 `25` 个。每个账号累计达到 `750` 个后自动停止该账号，所有账号达到目标后定时器自动结束。控制台支持按选中账号启动定时创建，默认每 `60` 分钟执行一轮；邮箱卡片可直接查看或复制邮箱地址、单邮箱 API 及 `邮箱----API` 组合。

## OpenAI 注册归档与存活追踪

Outlook Token 邮箱池使用 `data/outlook_token_used.db` 原子预占邮箱，支持多进程注册；旧版 `data/outlook_token_used.json` 会在首次访问时自动迁移。每次领取都有独立 lease，超时 worker 不会覆盖邮箱后来产生的新状态。

OpenAI 注册默认把 Agent Identity 独立加密归档到 `data/openai_agent_identities.db`，密钥保存在权限为 `0600` 的 `data/openai_agent_identities.key`，私钥不会写入普通账号列表。账号页可以导出单账号 `auth.json` 或多账号 ZIP；列表元数据接口为 `GET /api/accounts/agent-identities`。

OpenAI 存活追踪默认每 `60` 分钟运行，并通过跨进程 scheduler lease 避免重复探测。配置、状态和手动执行接口分别为 `POST /api/register/openai/survival`、`GET /api/register/openai/survival` 和 `POST /api/register/openai/survival/run`；Token 失效或网络错误只记为本次探测失败，不会直接把账号判定为停用。

## Grok 后台探测与自动恢复

Grok 账号探测随服务启动自动在后台运行，无需在账号管理页手动启用或点击执行。默认每 `60` 分钟运行一轮，每批处理 `50` 个账号；首次启动且没有历史完成记录时会立即执行。

调度器只探测已经加入内置 Grok 运行池、保存了 SSO 且未禁用的账号；探测复用 Fast 配额验证接口，不发送 Console 对话，也不会自动删除或禁用账号。只有运行时明确返回“失效”时才会读取该账号已保存的邮箱和密码重新登录；“未知”通常是超时或临时错误，只记录并等待下一轮。

新 SSO 会先加入运行池并再次验证，确认有效后才删除旧 SSO并原子更新本地账号档案。恢复失败时保留旧档案，按 `1 / 2 / 4 / 8 / 16 / 24` 小时退避重试，最长间隔 `24` 小时。账号列表显示探测结果、恢复状态和恢复时间；恢复记录保存在 `data/grok_accounts.json`，调度完成记录保存在 `data/register.json`。

## 内置 Captcha Solver

本地打码源码已经并入主仓库的 `captcha-solver/`，包含 xAI 实页 `window.turnstile.render()`、callback token 采集和注册代理按请求透传修复。安装主项目后直接创建独立虚拟环境：

```bash
cd captcha-solver
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

macOS 需要“有头但不弹窗”时，推荐使用 `deploy/docker-compose.captcha-solver.yml` 在 Docker/Xvfb 中运行；原生 LaunchAgent 会显示 Chromium 窗口。Ubuntu/Linux 使用 `deploy/systemd/captcha-solver.service.example`。详细步骤见 macOS/Ubuntu 新手手册。

## 系统结构

![GPTGrok2API 系统结构](docs/images/gptgrok2api-architecture.png)

图中使用四层布局：入口层、主网关层、业务任务层、数据与状态层；iCloud Privacy Mail 作为主网关右侧的内部 sidecar 展示。可复用的 GPT 架构图生成提示词见 [`docs/gptgrok2api-architecture-prompt.md`](docs/gptgrok2api-architecture-prompt.md)。

## API 入口

| 功能 | 请求地址 |
| --- | --- |
| 模型列表 | `GET /v1/models` |
| 对话补全 | `POST /v1/chat/completions` |
| Responses | `POST /v1/responses` |
| Anthropic Messages 兼容 | `POST /v1/messages` |
| 图片生成 | `POST /v1/images/generations` |
| 图片编辑 | `POST /v1/images/edits` |
| Grok 视频 | `POST /grok/v1/videos` |
| 管理控制台 | `/` |

请求使用 Bearer 密钥：

```bash
curl http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer YOUR_AUTH_KEY"
```

对话示例：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_AUTH_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5",
    "messages": [
      {"role": "user", "content": "你好"}
    ]
  }'
```

图片编辑示例：

```bash
curl http://127.0.0.1:8000/v1/images/edits \
  -H "Authorization: Bearer YOUR_AUTH_KEY" \
  -F "model=gpt-image-2" \
  -F "image=@/path/to/input.png;type=image/png" \
  -F "mask=@/path/to/mask.png;type=image/png" \
  -F "prompt=只把人物头发改成鲜艳的红色，其他内容保持不变" \
  -F "quality=standard" \
  -F "n=1" \
  -F "response_format=b64_json" \
  -o image-edit-response.json
```

## 部署与排错文档

| 文档 | 适用场景 |
| --- | --- |
| [macOS 从零搭建](docs/BEGINNER_LOCAL_SETUP.md) | Mac 本地混合部署、launchd、本地 solver 与日常维护 |
| [Ubuntu/Linux 从零搭建](docs/BEGINNER_UBUNTU_SETUP.md) | Ubuntu 22.04/24.04、systemd、Xvfb、Nginx 与防火墙 |
| [NovaApi（Sub2API）与 CPA 自动上传](docs/AUTO_UPLOAD_SUB2API_CPA.md) | OpenAI/Grok 注册成功后的远程账号投递 |
| [日志错误示例与排查](docs/TROUBLESHOOTING_LOG_EXAMPLES.md) | Node、邮箱、Turnstile、solver、NovaApi、CPA 常见错误日志 |
| [内置 Captcha Solver 说明](captcha-solver/README.md) | Solver API、环境变量、支持类型和底层运行方式 |

服务模板：

- macOS：[主程序 LaunchAgent](deploy/launchd/com.chatgpt2api.app.plist.example) / [Captcha Solver LaunchAgent](deploy/launchd/com.chatgpt2api.captcha-solver.plist.example)
- macOS 隐藏有头浏览器：[Captcha Solver Docker/Xvfb Compose](deploy/docker-compose.captcha-solver.yml)
- Ubuntu：[主程序 systemd unit](deploy/systemd/chatgpt2api.service.example) / [Captcha Solver systemd unit](deploy/systemd/captcha-solver.service.example)

## Docker 部署

### 环境要求

- Docker Engine 24+
- Docker Compose v2
- 至少 2 GB 可用内存
- 可访问所需上游服务的网络出口

### 克隆项目

仓库已经公开，直接使用 HTTPS 克隆，不需要 GitHub Token 或 SSH Key：

```bash
git clone https://github.com/AuuCoder/gptGrok2api.git
cd gptGrok2api
```

### 创建配置

```bash
cp .env.example .env
printf '{ "auth-key": "YOUR_AUTH_KEY" }\n' > config.json
```

至少需要修改：

```dotenv
CHATGPT2API_AUTH_KEY=YOUR_AUTH_KEY
CHATGPT2API_PORT=3000
CHATGPT2API_BASE_URL=https://your-domain.example
```

项目继续保留 `CHATGPT2API_*` 环境变量名称，以兼容已有部署、数据目录和自动化脚本。

### 标准启动

```bash
docker compose up -d --build
```

默认地址：

- 控制台：`http://127.0.0.1:3000`
- API：`http://127.0.0.1:3000/v1`
- 数据目录：`./data`
- 日志目录：`./logs`

### WARP 稳定出口

```bash
docker compose -f docker-compose.warp.yml up -d --build
```

该编排会启动：

- `app`：GPTGrok2API 主服务。
- `warp-proxy`：WARP SOCKS5 出口。
- `privoxy`：把 SOCKS5 转换为 HTTP 代理。
- `flaresolverr`：处理需要浏览器挑战的网络链路。
- `init-config`：初始化代理运行时配置。

查看状态：

```bash
docker compose -f docker-compose.warp.yml ps
docker logs -f chatgpt2api-warp
```

## 服务器部署

仓库提供服务器覆盖配置：

```bash
docker compose \
  -f docker-compose.warp.yml \
  -f deploy/docker-compose.server.yml \
  up -d --build
```

服务器覆盖配置会：

- 只将应用端口绑定到 `127.0.0.1`。
- 加入外部 Docker 网络 `deploy_sub2api-network`。
- 注册内部网络别名 `gptgrok2api`。
- 由 Nginx 对外提供 HTTPS。

Nginx 示例位于：

- `deploy/pro.muyuai.top.nginx.conf`

上线前验证：

```bash
docker compose \
  -f docker-compose.warp.yml \
  -f deploy/docker-compose.server.yml \
  config

nginx -t
```

## NovaApi / Sub2API 对接

注册中心的自动同步以项目作者魔改版 [AuuCoder/NovaApi](https://github.com/AuuCoder/NovaApi) 为兼容目标。普通 Sub2API 上游可能缺少 xAI OAuth 账号类型或对应管理接口；部署时还要确认实际镜像不是默认的 `weishaw/sub2api:latest`。完整部署、连接和自动投递步骤见 [NovaApi 与 CPA 配置手册](docs/AUTO_UPLOAD_SUB2API_CPA.md)。

当前服务器部署与 Sub2API 共用 `deploy_sub2api-network`。

Sub2API 容器内建议配置：

```text
Base URL: http://gptgrok2api/v1
API Key: YOUR_AUTH_KEY
```

外部服务使用：

```text
Base URL: https://pro.muyuai.top/v1
API Key: YOUR_AUTH_KEY
```

验证模型列表：

```bash
curl https://pro.muyuai.top/v1/models \
  -H "Authorization: Bearer YOUR_AUTH_KEY"
```

## 主要配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CHATGPT2API_AUTH_KEY` | 无 | 控制台和 API 认证密钥 |
| `CHATGPT2API_PORT` | `3000` | 宿主机监听端口 |
| `CHATGPT2API_BASE_URL` | 自动识别 | 生成公开资源 URL 的基础地址 |
| `CHATGPT2API_THREAD_TOKENS` | `80` | 后端同步线程池容量 |
| `CHATGPT2API_IMAGE` | 自有 GHCR 镜像 | Docker 镜像名称 |
| `STORAGE_BACKEND` | `json` | `json`、`sqlite`、`postgres` 或 `git` |
| `DATABASE_URL` | 无 | SQLite 或 PostgreSQL 连接地址 |
| `WARP_SOCKS_PORT` | `40000` | WARP SOCKS5 宿主机端口 |
| `PRIVOXY_PORT` | `40080` | Privoxy 宿主机端口 |
| `FLARESOLVERR_PORT` | `8191` | FlareSolverr 宿主机端口 |

完整配置示例见 `.env.example` 和 `config.defaults.toml`。

## 数据与密钥

以下内容属于运行时数据，不会提交到 Git：

- `.env`
- `config.json`
- `data/`
- `logs/`
- `services/checkout_protocol/proxy_state.json`

生产环境建议：

- 使用随机高强度认证密钥。
- 限制 `.env`、`config.json` 和账号文件权限。
- 不在日志、截图、Issue 或提交记录中暴露账号 Token、Cookie 和代理密码。
- 定期备份 `data/`，并验证备份恢复流程。
- Nginx 只反向代理本机监听端口，不直接暴露容器管理端口。

## 版本更新

控制台统一从 GitHub 读取最新版本和更新日志：

```text
https://api.github.com/repos/AuuCoder/gptGrok2api/releases
https://raw.githubusercontent.com/AuuCoder/gptGrok2api/main/CHANGELOG.md
```

工作方式：

1. 当前运行版本由本机 `/version` 返回。
2. 最新版本从 GitHub Releases API 读取。
3. 更新日志从 GitHub 仓库 `main` 分支的 `CHANGELOG.md` 读取。
4. 后端缓存 GitHub 查询结果，控制台比较版本号并显示更新状态。

发布新版本时同步更新根目录 `VERSION`、Python/前端包版本和 `CHANGELOG.md`，推送版本标签并等待 GitHub Actions 完成，不再维护独立更新服务器。

## 本地开发

后端：

```bash
uv sync --frozen
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

前端：

```bash
cd web-vue
npm install
npm run dev
```

构建前端：

```bash
npm --prefix web-vue run build
```

运行测试：

```bash
uv run --with pytest --with pytest-asyncio python -m pytest -q
```

## Git 工作流

- `origin`：`git@github.com:AuuCoder/gptGrok2api.git`
- `main`：当前生产主分支

提交前建议执行：

```bash
npm --prefix web-vue run build
python3 -m compileall -q api app services utils
uv run --with pytest --with pytest-asyncio python -m pytest -q
```

## 社区支持

学技术，了解 AI，上 L 站：[LinuxDO](https://linux.do/)

## 许可证

项目中的自有修改与继承代码分别受仓库内许可证文件约束：

- `LICENSE`
- `GROK2API_LICENSE`

许可证和来源声明必须随分发内容保留。运行时、版本检查、安装地址和容器发布均使用 GPTGrok2API 自有仓库与服务。

本项目基于 [yukkcat/chatgpt2api](https://github.com/yukkcat/chatgpt2api) 二开。
