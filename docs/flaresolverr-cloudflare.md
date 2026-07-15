# FlareSolverr 处理 Cloudflare 拦截

本文说明如何配合 `docker-compose.warp.yml` 中的 WARP、Privoxy、FlareSolverr，解决注册流程遇到 Cloudflare 拦截的问题。

## 工作方式

注册流程不会每个请求都调用 FlareSolverr。当前逻辑是：

1. 注册请求先按配置走代理链路。
2. 如果响应被识别为 Cloudflare challenge 或 403/503 拦截，后端调用 FlareSolverr。
3. FlareSolverr 打开目标地址并返回 `cf_clearance` cookie 和 User-Agent。
4. 后端把 clearance 写入当前注册 session，并重试当前请求一次。
5. 同进程内会缓存 clearance，后续同 host 请求可复用。

## 启动容器

推荐直接使用仓库里的 WARP compose：

```bash
docker compose -f docker-compose.warp.yml up -d
```

相关服务：

| 服务 | 用途 | 默认内部地址 |
| --- | --- | --- |
| `warp-proxy` | 提供 WARP 出站网络 | `socks5://warp-proxy:1080` |
| `privoxy` | 把 HTTP 代理转发到 WARP | `http://privoxy:8118` |
| `flaresolverr` | 获取 Cloudflare clearance | `http://flaresolverr:8191` |
| `app` | chatgpt2api 主服务 | `http://localhost:3000` |

如果 app 运行在 Docker compose 网络内，FlareSolverr URL 使用：

```text
http://flaresolverr:8191
```

如果 app 直接运行在宿主机，FlareSolverr URL 通常需要改成：

```text
http://127.0.0.1:8191
```

## 设置页配置

打开：

```text
http://localhost:3000/settings/
```

进入 `FlareSolverr` tab，推荐配置：

| 字段 | 推荐值 |
| --- | --- |
| 启用 FlareSolverr 清障 | 开启 |
| 出站模式 | `单代理/WARP` |
| 清障代理 URL | `http://privoxy:8118` |
| Clearance 模式 | `FlareSolverr` |
| FlareSolverr URL | `http://flaresolverr:8191` |
| 超时秒数 | `60` |
| 刷新间隔秒数 | `3600` |

保存后可以点击：

- `测试当前清障代理`
- `测试 Clearance`

两个测试都通过后，再启动注册任务。

## 注册页使用

打开：

```text
http://localhost:3000/register/
```

正常配置邮箱和注册参数即可。遇到 Cloudflare 拦截时，日志会出现类似：

```text
检测到 Cloudflare 拦截，尝试刷新 clearance
Cloudflare clearance 刷新完成，重试当前请求
```

如果刷新失败，日志会提示 `clearance 刷新失败或重试后仍失败`，这通常表示当前 IP、WARP 出口或 FlareSolverr 浏览器环境仍被拦截。

## 常见问题

### FlareSolverr 地址填哪个？

- Docker compose 内运行 app：`http://flaresolverr:8191`
- 宿主机运行 app：`http://127.0.0.1:8191`

### 为什么打开了 FlareSolverr，但注册一开始没有调用？

这是正常的。FlareSolverr 只在检测到 Cloudflare 拦截后触发，用来刷新 clearance 并重试当前请求。

### 为什么测试代理成功，但 Clearance 失败？

代理只说明出站网络可用。Clearance 失败可能是：

- `flaresolverr` 容器没有启动
- app 无法访问 `flaresolverr_url`
- WARP 出口 IP 仍被 Cloudflare 拦截
- FlareSolverr 启动浏览器超时

### 如何确认容器在运行？

```bash
docker compose -f docker-compose.warp.yml ps
```

查看 FlareSolverr 日志：

```bash
docker logs -f chatgpt2api-flaresolverr
```

查看主服务注册日志：

```bash
docker logs -f chatgpt2api-warp
```
