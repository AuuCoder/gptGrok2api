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
| Grok Build OAuth | 支持 Device Code 授权、Access/Refresh Token 导入、自动刷新、模型探测以及独立 OAuth 凭据存储。 |
| 账号生命周期 | 支持账号导入、分组、标签、额度同步、异常状态识别、限流恢复、无效账号清理、批量操作、代理绑定和运行状态监控。 |
| iCloud Privacy Mail | 内置 sidecar；新接口负责 Apple 登录、2FA 和创建隐私邮箱，旧接口负责登录、2FA 和同步已有邮箱，IMAP App 专用密码负责取验证码。 |
| iCloud 定时创建 | 按 Apple 账号定时创建邮箱；新接口每小时最多 20 个、旧接口每小时最多 5 个，每账号累计 750 个后自动停止。sidecar 使用 Compose 内部网络，不需要独立账号或宿主机端口。 |
| 邮箱平台标签 | 同一邮箱可分别标记 GPT 和 Grok；GPT 标签为绿色、Grok 标签为蓝色，两个标签同时存在才算已使用，注册领取按目标平台隔离。 |
| 注册中心 | 支持 OpenAI 与 Grok 注册任务，整合临时邮箱、GPTMail、Outlook Token、Microsoft Alias 和 iCloud Privacy Mail；内置 iCloud provider 不需要填写域名、API Base 或 API Key。 |
| Checkout 提链 | 注册 Checkout 仅保留 UPI 最终支付链接提取；IN Checkout、Provider、Approve 共享同一 sticky 出口，VN Promotion 使用独立代理持续轮换重试。 |
| 代理与稳定出口 | 支持全局代理、账号代理、代理配置、代理组、节点并发限制、故障反馈、备用出口、WARP、Privoxy、FlareSolverr 和 Clearance 刷新。 |
| 外部系统接入 | 支持从 Sub2API、远程 CPA、本地 CPA 和 Access Token 导入账号；服务器部署提供 `gptgrok2api` Docker 网络别名。 |
| 管理控制台 | 提供概览、GPT/Grok 账号管理、Grok Runtime、iCloud 邮箱、注册任务、Checkout 任务、代理管理、日志、实时监控、图片管理、调试中心和系统设置。 |
| 存储与备份 | 账号数据支持 JSON、SQLite、PostgreSQL 和 Git 后端；图片支持本地与 WebDAV；备份支持本地归档和 R2。 |
| 运维与发布 | 支持 Docker Compose、WARP 编排、内置 sidecar、Nginx HTTPS、运行日志、指标趋势、健康检查和自有版本更新源。 |

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
curl https://pro.muyuai.top/v1/models \
  -H "Authorization: Bearer YOUR_AUTH_KEY"
```

对话示例：

```bash
curl https://pro.muyuai.top/v1/chat/completions \
  -H "Authorization: Bearer YOUR_AUTH_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5",
    "messages": [
      {"role": "user", "content": "你好"}
    ]
  }'
```

## Docker 部署

### 环境要求

- Docker Engine 24+
- Docker Compose v2
- 至少 2 GB 可用内存
- 可访问所需上游服务的网络出口

### 克隆项目

仓库当前为私有仓库，需要使用已授权的 GitHub SSH Key：

```bash
git clone git@github.com:AuuCoder/gptGrok2api.git
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

## Sub2API 对接

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

控制台从部署的更新服务器读取更新源：

```text
https://pro.muyuai.top/updates/VERSION
https://pro.muyuai.top/updates/CHANGELOG.md
```

GitHub 仅用于打开项目发布页，不作为控制台版本号和更新日志的读取源。发布新版本时需先更新本地 `VERSION`、`CHANGELOG.md`，再同步到该更新服务器。

工作方式：

1. 当前运行版本由本机 `/version` 返回。
2. 最新版本从自有更新源 `https://pro.muyuai.top/updates/VERSION` 读取。
3. 更新日志从 `https://pro.muyuai.top/updates/CHANGELOG.md` 读取。
4. 控制台比较版本号并显示更新状态。

发布新版本时需要同步修改根目录 `VERSION` 和 `CHANGELOG.md`，然后重新构建并部署更新源服务器。

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
