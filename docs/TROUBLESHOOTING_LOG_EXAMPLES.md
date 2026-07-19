# 日志错误示例与排查手册

本文用于根据注册中心、GPTGrok2API、Captcha Solver、Docker、NovaApi 和 CPA 日志快速判断失败位置。示例中的邮箱、域名、密钥和任务号均为脱敏占位符。

## 1. 先确定错误属于哪一层

| 最后成功步骤 | 下一步失败位置 | 优先查看 |
| --- | --- | --- |
| `准备注册环境` | Node、配置、代理初始化 | 主程序日志 |
| `获取注册邮箱` | 邮箱 API、iCloud sidecar、Outlook IMAP | 主程序和邮箱服务日志 |
| `验证码已发送，等待邮件` | 收件、验证码提取、邮箱超时 | 主程序和邮箱服务日志 |
| `邮箱验证完成，正在进行安全校验` | Turnstile / Captcha Solver | 主程序和 solver 日志 |
| `安全校验完成，正在创建账号` | xAI Server Action、token 校验、账号创建 | 主程序日志 |
| `注册成功` | xAI OAuth、NovaApi、CPA 自动投递 | OAuth 投递、NovaApi、CPA 日志 |

不要只看最后一行。至少保留错误发生前后各 30 行，并记录准确时间和任务编号。

## 2. 日志查看命令

### 2.1 macOS

```bash
cd "$HOME/Documents/注册机/chatgpt2api"

tail -n 150 logs/chatgpt2api-launchd.log
tail -n 150 logs/chatgpt2api-launchd.error.log
tail -n 150 logs/captcha-solver.log
tail -n 150 logs/captcha-solver.error.log

launchctl print "gui/$(id -u)/com.chatgpt2api.app"
launchctl print "gui/$(id -u)/com.chatgpt2api.captcha-solver"
```

### 2.2 Ubuntu/Linux

```bash
sudo journalctl -u chatgpt2api -n 150 --no-pager
sudo journalctl -u captcha-solver -n 150 --no-pager

sudo journalctl -u chatgpt2api -f
sudo journalctl -u captcha-solver -f
```

### 2.3 Docker 辅助服务

```bash
docker logs --tail 150 chatgpt2api-warp-proxy
docker logs --tail 150 chatgpt2api-privoxy
docker logs --tail 150 chatgpt2api-flaresolverr
docker logs --tail 150 chatgpt2api-icloud-privacy-mail
```

NovaApi：

```bash
cd /opt/gptgrok-stack/NovaApi/deploy
docker compose -f docker-compose.nova.yml logs --tail=150 sub2api
```

## 3. 正常日志示例

注册中心正常顺序：

```text
准备注册环境
获取注册邮箱
验证码已发送，等待邮件
邮箱验证完成，正在进行安全校验
安全校验完成，正在创建账号
注册成功
```

Solver 正常实页解题示例：

```text
INFO captcha-solver: Solve: type=turnstile sitekey=0x4AAAA... url=https://accounts.x.ai/sign-up
INFO turnstile.solve: Real-page checkbox clicked=True
POST /solve HTTP/1.1 200 OK
```

调用端最终响应还应满足：

```json
{
  "solved": true,
  "method": "real-page",
  "token_present": true
}
```

`clicked=True` 不是唯一成功条件，必须以最终 `HTTP 200`、`solved:true` 和非空 token 为准。

## 4. 常见错误示例

### 4.1 缺少 Node.js

日志：

```text
注册失败（3.9 秒）：Grok Castle 运行需要 Node.js
```

含义：Grok Castle 安全 token 还未生成，流程没有进入邮箱或打码阶段。

检查：

```bash
command -v node
node --version
```

macOS 还要确认 LaunchAgent `PATH` 包含 `/opt/homebrew/bin`；Ubuntu 应让 Node 位于 `/usr/bin/node` 或 systemd 的 `PATH` 中。

### 4.2 iCloud 请求超时

日志：

```text
获取注册邮箱（第 1/3 次）
注册失败（30.0 秒）：iCloud Privacy Mail 请求失败（Timeout）
```

含义：还没进入发码和 Turnstile，先排查邮箱来源。

检查：

```bash
curl -I http://127.0.0.1:8788/login
docker logs --tail 150 chatgpt2api-icloud-privacy-mail
```

确认注册中心选择的是“iCloud 邮箱（本系统）”还是外部“iCloud API”。宿主机部署应访问 `http://127.0.0.1:8788`。

### 4.3 Turnstile token 被 xAI 拒绝

日志：

```text
邮箱验证完成，正在进行安全校验
安全校验完成，正在创建账号
注册失败（24.1 秒）：[internal] Failed to verify Cloudflare turnstile token.
```

含义：solver 返回过 token，但 xAI 在创建账号时不接受该 token。

检查：

- Solver 最终结果必须是 `method=real-page`。
- `TURNSTILE_HEADLESS=0` 必须生效。
- 注册请求与 solver 必须使用同一代理/出口 IP。
- token 获取后要立即提交，不能重复使用。

如果日志显示 `method=route`，说明没有走 xAI 实页模式。

### 4.4 Solver 连接被拒绝

调用端常见错误：

```text
本地 Turnstile 求解失败
Connection refused
Couldn't connect to server
```

检查：

```bash
curl http://127.0.0.1:8877/health
```

macOS：

```bash
launchctl print "gui/$(id -u)/com.chatgpt2api.captcha-solver"
lsof -nP -iTCP:8877 -sTCP:LISTEN
```

Ubuntu：

```bash
sudo systemctl status captcha-solver --no-pager
sudo journalctl -u captcha-solver -n 150 --no-pager
```

### 4.5 Solver 返回 408 Timeout

日志：

```text
INFO captcha-solver: Solve: type=turnstile ...
INFO turnstile.solve: Real-page checkbox clicked=False
POST /solve HTTP/1.1 408 Request Timeout
```

含义：浏览器已启动，但在 `timeout_s` 内没有得到 token。

检查：

- 当前是否有多个请求在单 worker 的 Turnstile 锁中排队。
- 代理能否访问 xAI 和 `challenges.cloudflare.com`。
- 是否使用 `TURNSTILE_HEADLESS=0`。
- 超时建议至少 `90` 秒，注册中心解题超时建议 `180` 秒。

单独出现：

```text
Real-page checkbox clicked=False
```

不一定代表最终失败。页面可能自动完成 challenge；继续看最终 HTTP 状态和 `solved` 字段。

### 4.6 私网/fake-IP 被 SSRF 防护拦截

日志：

```text
url: private/loopback host blocked
```

含义：目标域名被解析为回环、私网或 `198.18.0.0/15` fake-IP。

仅当 solver 只绑定 `127.0.0.1` 且明确使用 fake-IP 代理时设置：

```text
SOLVER_ALLOW_PRIVATE=1
```

公开暴露的 solver 不得关闭该防护。

### 4.7 Chromium/Xvfb 启动失败

日志示例：

```text
xvfb-run: error: Xvfb failed to start
Executable doesn't exist
BrowserType.launch: Target page, context or browser has been closed
```

Ubuntu 检查：

```bash
command -v xvfb-run
cd /opt/gptgrok-stack/chatgpt2api/captcha-solver
sudo .venv/bin/python -m playwright install-deps chromium
```

首次 CloakBrowser 下载应看到：

```text
Download complete
Checksum verified
Binary ready
```

### 4.8 Uvicorn WebSocket warning

日志：

```text
WARNING: Unsupported upgrade request.
WARNING: No supported WebSocket library detected.
```

这通常不是 Turnstile 失败的决定性原因。如果 `/health` 正常且 `/solve` 最终返回 `solved:true`，可以先忽略。只有业务明确需要 solver WebSocket 接口时才需要额外安装 `websockets` 或 `wsproto`；当前 `/solve` 使用普通 HTTP。

### 4.9 macOS Chromium 窗口反复弹出

现象：开启多线程后，桌面不断出现 `about:blank` Chromium 窗口或 Dock 图标。

原因：xAI Turnstile 需要 `TURNSTILE_HEADLESS=0`，macOS 又会把原生窗口强制钳制回可见屏幕，负坐标参数不能保证隐藏。

处理：改用 Docker/Xvfb 运行 solver，浏览器仍为有头模式，但只绘制到容器虚拟屏幕：

```bash
launchctl bootout "gui/$(id -u)/com.chatgpt2api.captcha-solver" 2>/dev/null || true
cd "$HOME/Documents/注册机/chatgpt2api"
docker compose -f deploy/docker-compose.captcha-solver.yml up -d --build
curl http://127.0.0.1:8877/health
```

确认宿主机没有原生 CloakBrowser，容器内存在 Chromium：

```bash
ps -ax | grep '/.cloakbrowser/.*/Chromium.app/'
docker exec chatgpt2api-captcha-solver ps aux
```

不要同时运行原生 solver LaunchAgent 和 Docker solver。

### 4.10 端口被占用

日志示例：

```text
[Errno 48] Address already in use
error while attempting to bind on address ('127.0.0.1', 8877)
```

检查：

```bash
# macOS
lsof -nP -iTCP:8877 -sTCP:LISTEN

# Ubuntu
sudo ss -lntp | grep ':8877'
```

确认是否同时启动了旧 solver、手动 Uvicorn 和开机服务。不要直接对未知进程使用 `kill -9`。

### 4.11 OpenAI 注册成功，但 NovaApi 同步失败

日志：

```text
正在同步账号到 Sub2API
Sub2API 同步未成功，本地账号已保留：Sub2API 同步账号失败（HTTP 404）
```

含义：本地账号已经保存，失败仅发生在远程投递。

检查：

- 实际运行镜像是否为作者魔改 `AuuCoder/NovaApi`。
- Sub2API 地址是否只填写站点根地址。
- 管理员邮箱/密码或 Admin API Key 是否正确。
- 目标 OpenAI 分组是否存在，或改用自定义分组。

### 4.12 Grok 注册成功，但 NovaApi/CPA 没有账号

日志示例：

```text
注册成功
Grok OAuth 授权已进入即时上传队列
xAI Device Code OAuth 协议授权失败
```

或：

```text
协议授权完成，外部投递部分失败
```

前者表示没有得到可投递的 xAI OAuth 凭据；后者表示 OAuth 已保存到本地，但至少一个远程目标失败。

正常即时上传应在注册成功后继续出现：

```text
Grok OAuth 授权已进入即时上传队列
Grok OAuth 授权完成，已上传到 NovaApi
```

如果长时间只有“已进入即时上传队列”，检查 solver `/status` 是否出现 `sign-in`，并确认启动日志显示 `solver 总槽位 = 注册线程数 + 1`。没有 `sign-in` 通常表示 OAuth worker 没有消费队列；出现 `sign-in` 后失败则继续查看 Turnstile 或 Device Code 阶段错误。

分别检查：

- “注册后自动协议授权”是否开启。
- NovaApi 的 xAI 分组、管理员认证和魔改镜像。
- CPA 根地址、远程管理密钥和 `/v0/management/auth-files` 接口。

### 4.13 NovaApi 401/403/404/422

```text
HTTP 401 / 403
```

通常是管理员凭据或 Admin API Key 错误。

```text
HTTP 404 / 422
```

通常是地址包含了多余路径、接口版本不兼容，或错误启动了 `weishaw/sub2api:latest`。

检查：

```bash
docker inspect sub2api --format '{{.Config.Image}}'
```

应返回 `auucoder/novaapi:local` 或作者明确发布的魔改镜像。

### 4.14 CPA 上传失败

日志示例：

```text
CPA 上传 OpenAI OAuth 文件失败（HTTP 401）
CPA 上传 OpenAI OAuth 文件失败（HTTP 404）
CPA 上传 xAI OAuth 文件失败（HTTP 401）
CPA 上传 xAI OAuth 文件失败（HTTP 404）
CPA 上传 xAI OAuth 文件请求失败
```

OpenAI 会上传 `codex-*.json`，Grok 会上传 `xai-*.json`；两者使用同一个 CPA 管理接口。

判断：

- `401/403`：管理密钥错误。
- `404`：CPA 地址错误或不兼容 `/v0/management/auth-files`。
- 请求失败/Timeout：网络、HTTPS 证书或 CPA 服务未运行。

CPA 地址只填写根地址，例如 `http://127.0.0.1:8317`，不要追加管理接口路径。

## 5. 建议的排查顺序

1. 根据注册中心最后成功步骤确定失败层。
2. 检查主程序、solver 和 Docker 服务是否运行。
3. 用 `/health` 验证端口，不先重复注册。
4. 确认邮箱 provider、Turnstile provider、代理和远端连接只启用预期项。
5. 使用数量 `1`、并发 `1` 重现。
6. 对照同一时间段的主程序与 solver 日志。
7. 只有单任务稳定成功后才提高并发。

提交问题时至少提供：

```text
系统版本：macOS / Ubuntu 22.04 / Ubuntu 24.04
项目 commit：git rev-parse --short HEAD
错误时间：包含时区
任务编号：例如 任务1
注册目标：OpenAI / Grok
邮箱 provider：名称
Turnstile provider：本地 / YesCaptcha / 2Captcha
代理类型：直连 / WARP / 自定义（不要提供密码）
主程序错误前后 30 行
solver 同一时间段前后 30 行
```

发送日志前删除邮箱、Cookie、Bearer token、OAuth token、代理密码、NovaApi 管理员凭据和 CPA 管理密钥。
