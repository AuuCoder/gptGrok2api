# GPTGrok2API 1.0.0

发布日期：2026-07-15

GPTGrok2API `1.0.0` 是当前统一版本的首次正式发布。该版本将 GPT、Grok SSO、Grok Build OAuth、账号管理、自动注册、iCloud Privacy Mail、代理出口和运行监控整合到同一套自托管控制台与 OpenAI 兼容 API 中。

## 发布重点

### 统一 GPT / Grok API

- 根 `/v1` 根据模型自动分流 GPT 与 Grok 请求。
- 提供 Chat Completions、Responses、Images 和 Anthropic Messages 兼容接口。
- 支持流式与非流式响应、工具调用、网页搜索、推理强度、图片生成与编辑。
- Grok 完整接口同时开放在 `/grok/v1/*`，并支持视频生成和媒体缓存。
- 支持 Grok Build Device Code 授权、Access/Refresh Token 导入和自动刷新。

### 内置 iCloud Privacy Mail

- iCloud sidecar 通过 Docker Compose 内部网络运行，不需要单独的 sidecar 账号或宿主机端口。
- 新接口用于 Apple 账号登录、2FA 和创建隐私邮箱。
- 旧接口用于 Apple 账号登录、2FA 和同步已有隐私邮箱。
- iCloud IMAP App 专用密码仅用于收取邮件和提取验证码。
- Apple 账号切换时，邮箱列表会同步切换到对应账号。
- 支持复制邮箱地址、单邮箱 API，以及 `邮箱----API` 组合。
- WARP 编排默认通过内部 Privoxy 请求 Apple；HTTP 502/503/504 会有限退避重试，降低临时上游波动对登录流程的影响。
- 从旧版 sidecar 升级且系统只有一个管理员时，旧版全局 iCloud 会话会自动合并到管理员账号；已有 IMAP 取码态、邮箱 API token、平台标签和邮件记录保持不变。
- 主系统内部请求会直接使用唯一管理员的 iCloud 数据，不需要 sidecar 独立登录，同时不会把未登录占位状态计入 Apple 登录态数量。
- ChatGPT 注册发现邮箱已进入 OpenAI 登录验证码分支时，会将其补标为 GPT 并自动领取下一个未标 GPT 的邮箱继续；同一任务不会再因命中一个已有账号邮箱而直接失败。

### 邮箱创建与平台标签

- 支持按 Apple 账号定时创建：新接口每个账号每小时最多 `20` 个，旧接口每个账号每小时最多 `5` 个。
- 两种登录态均可用时，每个账号每小时最多创建 `25` 个邮箱。
- 每个 Apple 账号累计达到 `750` 个邮箱后自动停止。
- GPT 注册成功后自动添加绿色 GPT 标签，Grok 注册成功后自动添加蓝色 Grok 标签。
- 同一邮箱可以分别用于 GPT 和 Grok；两个标签都存在时才标记为已使用。
- 注册任务按目标平台独立领取邮箱，避免重复使用已经注册过该平台的邮箱。
- 系统会根据已有 GPT/Grok 账号邮箱自动回填对应标签。

### 注册中心

- 支持 OpenAI 与 Grok 注册任务。
- 内置 `iCloud 邮箱（本系统）` provider，不需要填写域名、API Base 或 API Key。
- 保留独立 `iCloud API` provider，用于对接外部部署的邮箱服务。
- Grok 纯协议注册支持邮箱验证、Turnstile 任务服务、Next.js Server Action、SSO 保存和凭据导出。
- 注册失败时会释放对应平台的邮箱占用状态，注册成功后自动写入平台标签。

### Checkout 与代理出口

- Checkout 仅保留 UPI 最终支付链接提取。
- IN Checkout、Provider 和 Approve 共享同一 sticky 出口。
- VN Promotion 使用独立代理和持续轮换重试。
- 支持 WARP、Privoxy、FlareSolverr、代理组、节点并发限制和故障反馈。

### 管理与运维

- 提供 GPT/Grok 账号池、分组、标签、额度刷新、异常识别和批量操作。
- 提供注册任务、Checkout 任务、iCloud 邮箱、日志、实时监控、图片管理和调试中心。
- 支持 JSON、SQLite、PostgreSQL 和 Git 存储后端。
- 支持 Docker Compose、WARP 编排、健康检查、本地备份和 R2 备份。

## 主要接口

| 功能 | 请求地址 |
| --- | --- |
| 模型列表 | `GET /v1/models` |
| 对话补全 | `POST /v1/chat/completions` |
| Responses | `POST /v1/responses` |
| Anthropic Messages | `POST /v1/messages` |
| 图片生成 | `POST /v1/images/generations` |
| 图片编辑 | `POST /v1/images/edits` |
| Grok 视频 | `POST /grok/v1/videos` |
| 管理控制台 | `/` |

## 全新部署

环境要求：Docker Engine 24+、Docker Compose v2，建议至少准备 2 GB 可用内存。

```bash
git clone git@github.com:AuuCoder/gptGrok2api.git
cd gptGrok2api
cp .env.example .env
```

编辑 `.env` 和 `config.json` 后，使用 WARP 编排并启用内置 iCloud 模块：

```bash
docker compose \
  -f docker-compose.warp.yml \
  --profile local-icloud \
  up -d --build
```

仅启动标准服务：

```bash
docker compose up -d --build
```

## 从旧版本升级

升级前请备份 `.env`、`config.json` 和 `data/`。

```bash
git pull origin main
docker compose \
  -f docker-compose.warp.yml \
  --profile local-icloud \
  up -d --build --remove-orphans
```

现有账号、邮箱、标签和运行数据继续使用原来的持久化目录。iCloud sidecar 数据默认保存在 `data/icloud-privacy-mail/`。

## 发布验证

本版本发布前已完成以下验证：

- Python：`393 passed`，另有 `24` 个子测试通过。
- Vue：TypeScript 检查与生产构建通过。
- iCloud sidecar：Go 测试与 Docker 健康检查通过。
- 主应用 `/version` 返回 `1.0.0`，主页面返回 HTTP `200`。
- 主应用可通过 Compose 内部网络访问 iCloud sidecar。

部署后可执行：

```bash
curl http://127.0.0.1:3000/version
docker compose -f docker-compose.warp.yml --profile local-icloud ps
```

实际端口以 `.env` 中的 `CHATGPT2API_PORT` 为准。

## 社区

- Telegram：[加入 Telegram 群组](https://t.me/+olcHGKKXEwRmOTQx)
- QQ 群：`934890216`

<img src="https://github.com/AuuCoder/gptGrok2api/raw/main/docs/images/qq-group-934890216.png" alt="AI 智障 QQ 群 934890216 二维码" width="256">
