# 自动上传到 NovaApi（Sub2API）与 CPA 配置手册

本文说明注册成功后如何把账号自动投递到外部系统，适用于 macOS 本地部署和 Ubuntu/Linux 服务器部署。

## 1. 先分清三条链路

| 注册目标 | 自动投递目标 | 投递内容 | 必要条件 |
| --- | --- | --- | --- |
| OpenAI | NovaApi（后台显示为 Sub2API） | OpenAI OAuth 账号 | 开启“注册成功后同步到 Sub2API” |
| OpenAI | CPA / CLIProxyAPI | `codex-邮箱.json` OAuth 文件 | 开启“注册成功后上传到 CPA” |
| Grok | NovaApi（后台显示为 Sub2API） | xAI OAuth 账号 | “注册后自动协议授权”成功，并开启“上传到 Sub2API” |
| Grok | CPA / CLIProxyAPI | `xai-邮箱.json` OAuth 文件 | “注册后自动协议授权”成功，并开启“上传到 CPA” |

重要区别：

- OpenAI 的 NovaApi 和 CPA 两个目标可以同时开启，程序会分别投递。
- Grok 注册成功只代表拿到 Grok 登录态；还要完成 xAI Device Code OAuth，才有可投递到 NovaApi/CPA 的 OAuth 凭据。
- Grok 每个账号注册成功后会立即进入单线程 OAuth 上传队列，不需要等待整批注册结束；NovaApi 和 CPA 会在该账号 OAuth 成功后并行投递。
- 所有远程投递都是 best-effort：远端失败时，本地注册结果和本地 OAuth 凭据仍会保留。
- Grok 的 NovaApi 和 CPA 两个目标可以同时开启，程序会分别投递，一个失败不会阻止另一个。

## 2. NovaApi 版本要求

本系统的“Sub2API”连接必须优先使用项目作者魔改版：

- 仓库：[AuuCoder/NovaApi](https://github.com/AuuCoder/NovaApi)

不要把普通上游版本当成已验证兼容版本。自动同步依赖以下管理接口和账号字段：

```text
POST /api/v1/admin/accounts
GET  /api/v1/admin/groups
POST /api/v1/admin/groups
platform=openai / xai
type=oauth
```

普通 Sub2API 版本可能缺少 xAI OAuth 类型、通用账号创建字段或对应分组能力，常见表现是读取分组正常，但自动上传返回 `HTTP 400`、`404` 或 `422`。

### 2.1 默认 Compose 的陷阱

截至本文编写时，`AuuCoder/NovaApi` 仓库中的 `deploy/docker-compose.local.yml` 仍默认引用：

```yaml
image: weishaw/sub2api:latest
```

如果直接执行默认 Compose，会启动普通上游镜像，而不是当前克隆的魔改源码。下面的部署步骤会先从 `AuuCoder/NovaApi` 源码构建本地镜像，再生成一个替换镜像名的 Compose 文件。

同样不要直接使用该仓库 README 中的一键 Docker 准备脚本或二进制安装脚本；这些脚本当前仍可能从 `Wei-Shaw/sub2api` 下载文件或发行版。本文统一采用“克隆 `AuuCoder/NovaApi` 后本地构建镜像”的方式。

## 3. Ubuntu 上部署作者魔改 NovaApi

已有可用 NovaApi 服务时可跳到第 4 节。

### 3.1 硬件和端口

NovaApi 还会运行 PostgreSQL 和 Redis。与 GPTGrok2API 部署在同一台机器时，建议额外准备：

- 至少 2 vCPU、4 GB 可用内存；构建镜像时 8 GB 更稳妥。
- 至少 10 GB 可用磁盘。
- 本机端口 `127.0.0.1:8080`。

不要把 NovaApi 的 `8080`、PostgreSQL 或 Redis 端口直接开放到公网。

### 3.2 克隆并构建魔改源码

```bash
cd /opt/gptgrok-stack
git clone https://github.com/AuuCoder/NovaApi.git
cd /opt/gptgrok-stack/NovaApi
docker build -t auucoder/novaapi:local .
```

构建完成后检查镜像：

```bash
docker image inspect auucoder/novaapi:local \
  --format '{{.RepoTags}}'
```

### 3.3 生成 Compose 和环境变量

```bash
cd /opt/gptgrok-stack/NovaApi/deploy
cp docker-compose.local.yml docker-compose.nova.yml
sed -i \
  's|image: weishaw/sub2api:latest|image: auucoder/novaapi:local|' \
  docker-compose.nova.yml
cp .env.example .env
```

生成固定密钥：

```bash
POSTGRES_PASSWORD="$(openssl rand -hex 32)"
ADMIN_PASSWORD="$(openssl rand -hex 20)"
JWT_SECRET="$(openssl rand -hex 32)"
TOTP_ENCRYPTION_KEY="$(openssl rand -hex 32)"

sed -i "s|^BIND_HOST=.*|BIND_HOST=127.0.0.1|" .env
sed -i "s|^SERVER_PORT=.*|SERVER_PORT=8080|" .env
sed -i "s|^RUN_MODE=.*|RUN_MODE=simple|" .env
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$POSTGRES_PASSWORD|" .env
sed -i "s|^ADMIN_EMAIL=.*|ADMIN_EMAIL=admin@example.com|" .env
sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=$ADMIN_PASSWORD|" .env
sed -i "s|^JWT_SECRET=.*|JWT_SECRET=$JWT_SECRET|" .env
sed -i "s|^TOTP_ENCRYPTION_KEY=.*|TOTP_ENCRYPTION_KEY=$TOTP_ENCRYPTION_KEY|" .env
chmod 600 .env

printf 'NovaApi 管理员邮箱: admin@example.com\n'
printf 'NovaApi 管理员密码: %s\n' "$ADMIN_PASSWORD"
```

把最后输出的管理员密码保存到密码管理器。命令结束或退出 SSH 后，变量不会自动保留，但密码已经写入 `.env`。

`RUN_MODE=simple` 适合自用账号池。如果需要 NovaApi 的完整用户、计费和支付体系，将其改为：

```dotenv
RUN_MODE=standard
```

### 3.4 启动并验收

```bash
cd /opt/gptgrok-stack/NovaApi/deploy
docker compose -f docker-compose.nova.yml config
docker compose -f docker-compose.nova.yml up -d
docker compose -f docker-compose.nova.yml ps
curl -fsS http://127.0.0.1:8080/health
```

确认实际运行的是魔改镜像：

```bash
docker inspect sub2api --format '{{.Config.Image}}'
```

必须返回：

```text
auucoder/novaapi:local
```

如果返回 `weishaw/sub2api:latest`，立即回到第 3.3 节检查 `docker-compose.nova.yml`，不要继续配置自动上传。

查看日志：

```bash
docker compose -f docker-compose.nova.yml logs --tail=150 sub2api
```

### 3.5 打开 NovaApi 后台

NovaApi 只绑定在服务器回环地址时，在自己的电脑执行：

```bash
ssh -L 8080:127.0.0.1:8080 ubuntu@SERVER_IP
```

保持 SSH 窗口打开，然后浏览器访问：

```text
http://127.0.0.1:8080
```

使用第 3.3 节的 `ADMIN_EMAIL` 和 `ADMIN_PASSWORD` 登录。

## 4. 在 GPTGrok2API 中添加 NovaApi 连接

1. 登录 GPTGrok2API 管理后台。
2. 进入“设置”。
3. 打开“Sub2API”页签。
4. 点击“新增”。
5. 按下表填写并保存。

| 字段 | 同一台 Ubuntu 主机的推荐值 |
| --- | --- |
| 名称 | `作者 NovaApi` |
| Sub2API 地址 | `http://127.0.0.1:8080` |
| 管理员邮箱 | NovaApi 的 `ADMIN_EMAIL` |
| 密码 | NovaApi 的 `ADMIN_PASSWORD` |
| Admin API Key | 留空 |
| 默认分组 ID | 留空 |
| 验证 TLS 证书 | 保持开启 |

地址只填写站点根地址，不要追加 `/api/v1`、`/admin` 或具体接口路径。

认证方式二选一：

- 推荐：填写管理员邮箱和密码，`Admin API Key` 留空。
- 已在 NovaApi 配好 Admin API Key 时：填写该 Key，邮箱和密码可以留空。

“默认分组 ID”只用于从 NovaApi **导入到本地**时的默认筛选，不决定新注册账号上传到哪个分组。自动上传分组需要在注册任务中单独选择。

保存后依次点击：

1. “测试”，确认连接和认证成功。
2. “读分组”，确认能看到 NovaApi 中的分组。

### 4.1 NovaApi 在其他服务器

如果 NovaApi 不在同一台机器，不能填写 `127.0.0.1`。应为 NovaApi 配置 HTTPS，并填写：

```text
https://nova.example.com
```

两台服务器之间也可以使用内网 IP，但要限制防火墙来源。不要把 PostgreSQL、Redis 或 NovaApi 管理接口无保护地开放到互联网。

## 5. OpenAI 注册后自动同步到 NovaApi 和 CPA

1. 进入“注册中心”。
2. 注册目标选择 `OpenAI`。
3. 展开“外部同步”。
4. 开启“注册成功后同步到 Sub2API”。
5. 选择刚添加的“作者 NovaApi”连接。
6. 选择远端分组方式。

分组方式：

- `现有分组`：从列表选择 NovaApi 中已经存在的 OpenAI 分组。
- `自定义分组`：填写例如 `新注册 GPT`；首次同步会创建或复用同名 OpenAI 分组。

保存配置后先用注册数量 `1`、并发 `1` 测试。

成功日志应包含：

```text
正在提取 Sub2API OAuth 凭据
正在同步账号到 Sub2API
已同步到 Sub2API：作者 NovaApi / 新注册 GPT
```

如果远端同步失败，本地账号仍算注册成功，日志会显示：

```text
Sub2API 同步未成功，本地账号已保留
```

需要同时上传 CPA 时：

1. 在同一个“外部同步”区域开启“注册成功后上传到 CPA”。
2. 选择已保存的 CPA 连接。
3. 保存后使用数量 `1`、并发 `1` 测试。

成功时日志包含：

```text
正在上传账号到 CPA
已上传到 CPA：主 CPA / codex-user@example.com.json
```

CPA 文件使用 CLIProxyAPI 官方 Codex OAuth 格式，包含 `type: codex`、`email`、`account_id`、`access_token`、`refresh_token`、`id_token`、`expired` 和 `last_refresh`，不会写入注册密码。

## 6. 添加 CPA / CLIProxyAPI 连接

CPA 连接必须提供 CLIProxyAPI 兼容的远程管理接口：

```text
GET  /v0/management/auth-files
POST /v0/management/auth-files
```

1. 进入 GPTGrok2API“设置”。
2. 打开“CPA”页签。
3. 点击“新增”。
4. 按下表填写。

| 字段 | 示例 |
| --- | --- |
| 名称 | `主 CPA` |
| CPA 地址 | `http://127.0.0.1:8317` |
| 管理密钥 | CPA/CLIProxyAPI 的远程管理密钥 |

CPA 地址只填写根地址，不要追加 `/v0/management`。

这里必须填写**管理密钥**，不是客户端调用模型时使用的普通 API Key。GPTGrok2API 会发送：

```http
Authorization: Bearer 管理密钥
```

保存后点击“测试”。同一台主机可使用 `127.0.0.1:8317`；CPA 在其他服务器时应使用受保护的 HTTPS 地址或受限内网地址。

## 7. Grok 注册后同时上传 NovaApi 和 CPA

1. 进入“注册中心”。
2. 注册目标选择 `Grok（协议）`。
3. 在“Grok 运行时账号池”中保持“注册后自动协议授权”开启。
4. 展开“OAuth 投递”。
5. 点击“刷新连接”。
6. 开启“上传到 Sub2API”。
7. 选择“作者 NovaApi”连接以及 xAI 分组。
8. 开启“上传到 CPA”。
9. 选择“主 CPA”连接。
10. 保存配置，并先用数量 `1`、并发 `1` 测试。

NovaApi 分组同样支持两种方式：

- 选择现有 xAI 分组。
- 填写自定义分组名，例如 `Grok OAuth`，首次投递时自动创建或复用。

完整成功顺序是：

```text
注册成功
Grok 账号已保存并加入账号池
Grok OAuth 授权已进入即时上传队列
Grok OAuth 授权完成，已上传到 NovaApi、CPA
```

OAuth 队列保持单线程，避免同一账号重复授权；注册任务运行时会自动预留一个额外 solver 槽位。例如注册线程和“注册解题并发”均为 `3` 时，启动日志会显示 `solver 总槽位 4`，其中第 4 个槽位供 OAuth 使用。

CPA 中生成的文件名类似：

```text
xai-user@example.com.json
```

文件内容包含 `type: xai`、`access_token`、`refresh_token` 等 OAuth 字段。不要把该文件、NovaApi 凭据或 CPA 管理密钥发到公开日志和仓库。

## 8. 验收方法

### 8.1 NovaApi

登录 NovaApi 后台，进入账号/分组管理：

- OpenAI 注册账号应显示为 `platform=openai`、`type=oauth`。
- Grok OAuth 账号应显示为 `platform=xai`、`type=oauth`。
- 账号应位于注册中心选择的分组，而不是连接设置中的“默认分组 ID”。

### 8.2 CPA

在 CPA/CLIProxyAPI 后台确认新增认证文件：

- OpenAI：`codex-*.json`。
- Grok：`xai-*.json`。

也可以通过其管理接口检查：

```bash
curl -sS http://127.0.0.1:8317/v0/management/auth-files \
  -H 'Authorization: Bearer 你的CPA管理密钥' | jq
```

不要把真实管理密钥写进 shell 历史。正式使用时可临时读取环境变量：

```bash
read -rsp 'CPA 管理密钥: ' CPA_KEY
echo
curl -sS http://127.0.0.1:8317/v0/management/auth-files \
  -H "Authorization: Bearer $CPA_KEY" | jq
unset CPA_KEY
```

## 9. 常见问题

需要结合主程序、solver、NovaApi 和 CPA 的实际日志判断时，请同时阅读 [日志错误示例与排查手册](TROUBLESHOOTING_LOG_EXAMPLES.md)。

### 9.1 NovaApi 返回 401/403

检查：

- 管理员邮箱和密码是否正确。
- 使用 Admin API Key 时，是否误填了普通用户 API Key。
- NovaApi 管理员密码修改后，GPTGrok2API 中保存的连接是否同步更新。

### 9.2 NovaApi 返回 404/422

检查实际镜像：

```bash
docker inspect sub2api --format '{{.Config.Image}}'
```

必须是 `auucoder/novaapi:local` 或作者明确发布的魔改镜像。若为 `weishaw/sub2api:latest`，说明启动了普通上游版本。

另外确认 Sub2API 地址只填写根地址，例如 `http://127.0.0.1:8080`。

### 9.3 能测试连接，但读取不到分组

- 确认 NovaApi 中已经创建对应平台分组。
- 点击 GPTGrok2API 的“读分组”或注册中心的“刷新连接”。
- 也可以在注册配置中选择“自定义分组”，由首次同步创建。

### 9.4 OpenAI 注册成功但没有上传

- 确认注册目标确实是 OpenAI。
- 确认“注册成功后同步到 Sub2API”已开启。
- 如目标是 CPA，确认“注册成功后上传到 CPA”已开启并已选择 CPA 连接。
- 检查是否选择连接和远端分组。
- 查看注册日志中是否出现 OAuth refresh token 提取失败、Sub2API HTTP 错误或 CPA HTTP 错误。

同步失败不会回滚本地账号，需要修好连接后重新执行同步或重新测试一个账号。

### 9.5 Grok 注册成功但 NovaApi/CPA 都没有账号

优先检查“注册后自动协议授权”。Grok 外部投递发生在 xAI Device Code OAuth 成功之后；如果协议授权被关闭、超时或失败，就没有可上传的 `access_token` 和 `refresh_token`。

### 9.6 NovaApi 成功、CPA 失败，或反过来

两个目标独立执行。分别检查：

- NovaApi：连接、管理员认证、xAI 分组和镜像版本。
- CPA：根地址、管理密钥和 `/v0/management/auth-files` 兼容性。

一个目标失败不会删除另一个目标已经收到的账号。

### 9.7 `127.0.0.1` 连接失败

`127.0.0.1` 永远表示“当前运行 GPTGrok2API 的那台主机或容器”：

- GPTGrok2API 在 Ubuntu 宿主机运行，NovaApi/CPA 端口映射到同一宿主机：可以使用 `127.0.0.1`。
- GPTGrok2API 在 Docker 容器运行：容器内的 `127.0.0.1` 不是宿主机，需要使用同一 Docker 网络的服务名或 `host.docker.internal`/host-gateway。
- NovaApi/CPA 在另一台服务器：使用内网 IP 或 HTTPS 域名。

### 9.8 自签名 HTTPS 证书

优先给 NovaApi 配置受信任证书。只有在可信私网且明确知道风险时，才关闭 Sub2API 连接中的“验证 TLS 证书”。CPA 连接当前按严格 TLS 校验访问，不建议使用无法验证的自签名证书。

## 10. 升级作者魔改 NovaApi

```bash
cd /opt/gptgrok-stack/NovaApi
git status
git pull --ff-only
docker build -t auucoder/novaapi:local .

cd deploy
docker compose -f docker-compose.nova.yml up -d
docker inspect sub2api --format '{{.Config.Image}}'
curl -fsS http://127.0.0.1:8080/health
```

升级后再次执行一次数量 `1` 的自动上传测试。不要改回默认 `weishaw/sub2api:latest` 镜像。

## 11. 备份和安全

GPTGrok2API 中需要备份：

```text
data/sub2api_config.json
data/cpa_config.json
data/register.json
```

NovaApi 中需要备份：

```text
/opt/gptgrok-stack/NovaApi/deploy/.env
/opt/gptgrok-stack/NovaApi/deploy/data/
/opt/gptgrok-stack/NovaApi/deploy/postgres_data/
/opt/gptgrok-stack/NovaApi/deploy/redis_data/
```

安全检查：

- [ ] NovaApi 实际镜像是作者魔改版，不是默认上游镜像。
- [ ] NovaApi 和 CPA 管理端口没有直接暴露到公网。
- [ ] NovaApi 管理员密码、Admin API Key 和 CPA 管理密钥没有进入公开仓库。
- [ ] `.env` 和包含连接密钥的数据文件权限严格受限。
- [ ] 首次只用一个账号测试，确认分组和文件格式正确后再开批量任务。
- [ ] 备份中包含数据库和连接配置，并存放在加密或受控位置。
