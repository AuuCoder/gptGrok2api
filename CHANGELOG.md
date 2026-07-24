# Changelog

## 1.1.0 - 2026-07-23

+ [修复] Grok 注册后 OAuth 可切换为公网版 Authorization Code + PKCE，并把注册会话 cookie 仅通过内存传入 PKCE worker；不再要求新账号走 Device Code。
+ [新增] OpenAI 账号存活追踪、跨进程 scheduler lease 和手动探测接口；网络或 Token 异常仅记录当次失败，不直接判定账号停用。
+ [新增] Outlook Token 邮箱池改用 SQLite lease 原子预占，支持多进程注册与旧 JSON 状态自动迁移，避免并发 worker 重复领取或覆盖新状态。
+ [新增] Codex Agent Identity 独立加密归档，支持单账号 `auth.json` 和批量 ZIP 导出，私钥不写入普通账号列表。
+ [修复] Grok 默认切换到已验证的 xconsole 注册链路，对齐无 Castle 发码、`ValidatePassword`、`tos=$undefined` 和创建阶段空 Castle；真实 OAuth 探测可获得 100 万 Token 初始额度，同时保留旧链路回退。
+ [优化] Grok Device Code OAuth 优先复用已有 SSO，失效时再回退邮箱密码；增加可选 Authorization Code + PKCE 对照授权和 402 权益延迟复检。
+ [优化] 本地 Turnstile 不再受 solver 内部固定 2 线程隐藏阀限制；注册页“注册解题并发”成为唯一控制项，默认为 2，设为 0 可取消共享并发上限。

## 1.0.9 - 2026-07-22

+ [新增] GPT 对接 Sub2API 支持 Codex Agent Identity 新模式；兼容新版 JWT claims、Cloudflare WARP 重试，并在上游不支持 Agent Identity 时自动降级为 Codex Session 导入。
+ [新增] Grok OAuth 新增无额度监测，正确识别 `personal-team-blocked:spending-limit`，账号页独立显示“无额度/无订阅”，支持筛选、汇总并保留原始 402 错误详情；同时优化内置运行时操作超时处理。

## 1.0.8 - 2026-07-21

+ [新增] OpenAI 与 Grok 账号导出统一支持 CPA ZIP 和 Sub2API JSON，可分别导出选中账号或全部账号，并保留对应 OAuth/SSO 元数据。
+ [新增] Grok 账号页增加 OAuth 未授权、正常、限流、过期和失效筛选，支持单个或批量加入 OAuth 授权队列，并展示请求额度、Token 额度、探测与恢复状态。
+ [优化] Grok 账号列表改为先安全加载持久化快照、再后台刷新运行时数据；概览统计按来源设置超时和降级状态，运行时繁忙或暂时不可用时页面仍可正常打开。
+ [优化] Grok OAuth 日志从注册日志中独立分流，成功、失败、权限待生效和上传结果统一脱敏；后台恢复队列按本地 Solver 容量使用 2 个 worker，并只重试明确的瞬时错误。
+ [修复] 兼容 xAI `createSession` 新旧嵌套响应、相对 Cookie Setter URL 和直接写入 Cookie 的流程，修复 `missing-host/`；同时解析 RPC 登录错误，账号密码错误不再重复消耗验证码。
+ [修复] Outlook 邮箱池显示本次任务失败邮箱，可勾选指定记录并只释放、重试所选邮箱；“释放全部占用/失败”和“删除未使用邮箱”恢复为各自准确语义。
+ [修复] SQLite 本地账号后端增加连接串行化、WAL 回退和 busy timeout，降低并发读取时的锁冲突；账号导出、运行池同步和恢复时间展示补齐异常兼容。

## 1.0.7 - 2026-07-20

+ [新增] Grok SSO 与 OAuth 后台探测：服务启动后按周期分批验证运行池账号和真实 `grok-4.5` 能力，持久化有效、限流、失效、未知、请求额度和 Token 额度。
+ [新增] Grok 异常账号自动恢复：SSO 明确失效时使用已保存邮箱和密码重新登录，新 SSO 验证成功后才替换旧值；OAuth 未授权或失效时自动重新授权，并在恢复后补传 NovaApi/CPA。
+ [优化] Grok OAuth 协议队列升级为 3 worker 优先级队列，新注册优先于未授权回填和失败重试；新增队列状态接口，账号恢复支持任务复用和启动遗留任务回收。
+ [修复] 新 OAuth 返回 `permission-denied` 时不再误判封禁或重复完整授权；改为权限待生效状态，每 15 分钟以 3 并发复检 `grok-4.5`，转为有效后立即补传尚未成功的外部目标。
+ [优化] Grok 账号页合并展示 SSO/OAuth 状态、真实额度、最近探测、恢复状态和下次重试时间；移除重复的 `Grok Runtime` 侧栏入口，旧地址自动跳转到 Grok 账号视图。
+ [修复] 本地 Turnstile Solver 优先复用实页可见控件，必要时显式加载、重建稳定尺寸 widget，并记录 iframe 可见性和失败阶段，改善页面无可点击挑战时的诊断与成功率。
+ [修复] OpenAI `PlatformRegistrar` 正确区分新注册、已有账号和已停用账号；OTP 已直接进入 OAuth callback 时跳过资料创建，并将不可注册邮箱写回 GPT 标签后自动更换。
+ [文档] 更新 README、macOS/Ubuntu、NovaApi/CPA 自动上传和日志排错说明，对齐 3-worker OAuth、权限延迟复检、后台恢复和当前 solver 并发模型。

## 1.0.6 - 2026-07-20

+ [新增] macOS 本地 Captcha Solver 支持 Docker/Xvfb 运行有头 Chromium，浏览器不再弹到桌面，并提供 Compose、缓存持久化和一键启动脚本。
+ [优化] Turnstile 从全局单锁升级为共享动态并发限制器；注册中心可配置注册解题并发、单次解题超时、排队超时和本地尝试次数。
+ [优化] 浏览器上下文和 CloakBrowser 进程在成功、失败与超时后统一释放，降低批量注册时的内存、PID 和残留 Chromium 数量。
+ [修复] Grok Device Code OAuth 复用注册任务的真实代理，并将代理继续透传给登录 Turnstile，修复 OAuth `sign-in` 实际走直连导致的 `600010` 和持续无 token。
+ [变更] Grok 注册成功后立即进入单线程 OAuth 上传队列，不再等待整批注册结束；注册运行时自动预留一个额外 solver 槽位，注册线程与上传互不串行。
+ [优化] OAuth 的 Turnstile、登录、consent 和 token 等瞬时失败自动有限重试；授权队列保持单线程，避免多个 OAuth `sign-in` 同时争抢浏览器资源。
+ [新增] NovaApi 与 CPA 在 OAuth 完成后按启用配置并行即时投递，分别记录成功、失败或跳过状态，并在注册日志中显示明确终态。
+ [文档] 更新 README、macOS/Ubuntu 从零部署、NovaApi/CPA 自动上传和日志排错教程，说明 Docker/Xvfb、即时上传、额外 OAuth 槽位及内存调优方式。

## 1.0.5 - 2026-07-19

+ [修复] OpenAI 注册默认切回 `PlatformRegistrar`，对齐上游最新 authorize/passwordless signup 链路，保留传统密码流程作为显式回退。
+ [新增] OpenAI 注册成功后可自动上传到 CPA / CLIProxyAPI，生成官方 `codex-邮箱.json` OAuth 文件，并与 Sub2API 独立投递。
+ [新增] 注册中心“外部同步”增加 OpenAI CPA 开关和连接选择，上传失败不影响本地账号保存。
+ [修复] Outlook Token IMAP 取码复用已认证连接，并对 `authenticated but not connected` 等瞬时断线进行退避重试。
+ [安全] CPA/Sub2API 投递错误继续隐藏 access token、refresh token、id token 和密码，CPA Codex 文件不写入注册密码。
+ [文档] 更新 macOS、Ubuntu/Linux、NovaApi 与 CPA 自动投递和错误日志说明，补充 `codex-*.json` 验收步骤。

## 1.0.4 - 2026-07-19

+ [新增] 将本地 Captcha Solver 完整源码并入主仓库 `captcha-solver/`，安装 GPTGrok2API 后不再需要额外克隆 `xai-grok-mass`。
+ [新增] Grok 注册中心增加“本地 Captcha Solver” provider，默认连接 `http://127.0.0.1:8877`，无需第三方打码 API Key。
+ [修复] xAI 实页 Turnstile 使用 `window.turnstile.render()` 和 callback 采集 token，避免只注入 DOM 占位节点时拿不到可验证 token。
+ [修复] 注册任务将当前代理按请求透传给 solver，使浏览器解题与账号创建保持同一出口 IP。
+ [新增] 提供 macOS launchd 和 Ubuntu systemd 服务模板，统一从主项目内的 `captcha-solver/` 启动，并支持 Xvfb、多 worker、日志和开机自启。
+ [文档] 新增 macOS、Ubuntu/Linux 从零部署手册，明确公开仓库使用者只需克隆一个主仓库，本机 `.env`、账号数据、虚拟环境和日志不得上传。
+ [文档] 新增作者魔改 NovaApi（Sub2API）与 CPA 自动投递配置，覆盖 OpenAI/Grok 投递链路、分组、认证、验收和升级。
+ [文档] 新增日志错误示例与排查手册，并在 README 增加部署、自动投递、solver 和排错文档入口。

## 1.0.3 - 2026-07-18

+ [修复] 更新中心的最新版本与更新日志统一从 GitHub Releases 和仓库读取，不再依赖独立更新服务器，手动检查会强制刷新 GitHub 缓存。
+ [修复] Outlook 旧版 IMAP OAuth Token 自动兼容 Graph 权限差异，缓存不可用通道并重试瞬时 IMAP 超时，避免重复刷新触发 `AADSTS50196` 或误判 Token 失效。
+ [优化] ChatGPT 注册邮箱等待保留真实 provider 错误；CF 邮箱短窗口重发一次后自动切换其他已启用邮箱来源，避免任务长期卡在无邮件状态。
+ [优化] 注册配置全面改为自动保存，离开页面不再丢失邮箱、Checkout、Sub2API 和 OAuth 投递设置；启动任务前会等待最新修改保存完成。
+ [修复] Grok OAuth 同步到 Sub2API 时使用 Grok 平台字段并补齐 CLI OAuth 元数据与 SSO，远程投递错误继续执行敏感字段脱敏。
+ [修复] Pix 方案 3 固定执行 BR 原价校验、VN 优惠注入、BR 税区与零金额 PIX 校验，并始终在原 BR 出口执行 Approve 与轮询。
+ [文档] 补充 iCloud Privacy Mail 对外 API 地址和本机 `8788` 端口配置说明。

## 1.0.2 - 2026-07-17

+ [新增] Grok 4.5 OAuth 账号支持按配置独立投递到 Sub2API 和 CPA；凭据始终先保存到本地，远程失败不影响授权结果。
+ [新增] OAuth 账号测试支持“全部账号”和“指定账号”两种模式，逐账号展示成功、失败、耗时和错误原因。
+ [优化] Grok 注册与协议授权日志收敛为关键步骤，并补充 OAuth 投递状态和账号运行状态展示。
+ [修复] Console 对话收到通用 HTTP 429 时不再把本地估算额度直接清零；仅记录限流并降低账号调度优先级。
+ [文档] README 补充上游项目二开来源声明。

## 1.0.1 - 2026-07-15

+ [新增] Pix 新增方案 3 BR/VN 独立协议，支持 BR Checkout/Provider 与 VN Promotion 双 sticky 出口，并保留方案 1、方案 2 独立选择。
+ [优化] Pix 方案 1、方案 2 补齐 Checkout、Stripe、PaymentMethod、Approve、Poll 与最终提取进度，收敛同一 sticky 出口的重试次数和请求超时，避免任务长时间无反馈。
+ [新增] Grok 注册默认执行 xAI Device Code OAuth；账号管理合并显示 OAuth 绑定状态，并提供授权、刷新、删除与模型测试操作。
+ [优化] 注册配置中的 Sub2API 同步和邮箱请求改为可展开/收起区块，收起时保留常用操作，展开切换不丢失未保存内容。
+ [新增] 推送 `v*` 标签后，在多架构 GHCR 镜像构建成功时自动创建 GitHub Release 并生成发布说明。

## 1.0.0 - 2026-07-15

+ [变更] 注册 Checkout 仅保留 UPI 最终支付链接提取，IN Checkout / Provider / Approve 共享同一 sticky 出口，VN Promotion 使用独立代理与持续轮换重试。
+ [新增] 注册账号新增 iCloud Privacy Mail 邮箱来源，支持 Apple 新接口创建隐私邮箱、旧接口同步已有邮箱、IMAP App 专用密码取验证码和 2FA 登录。
+ [新增] iCloud sidecar 作为 Compose 内部模块运行，不需要独立账户或宿主机端口；支持按 Apple 账号定时创建，每小时新接口最多 20 个、旧接口最多 5 个，每个账号目标 750 个。
+ [新增] 注册区新增“iCloud 邮箱（本系统）” provider，不需要填写 API Base、API Key 或域名；独立 `iCloud API` provider 继续保留。
+ [新增] iCloud 邮箱支持 GPT / Grok 注册标签，GPT 标签使用绿色，Grok 标签使用蓝色；已有 GPT/Grok 账号邮箱会自动回填标签。
+ [新增] 同一邮箱允许分别标记 GPT 和 Grok；注册领取按目标平台独立筛选，两个标签都存在时才标记为已使用。
+ [新增] Apple 账号切换时同步切换邮箱列表，支持邮箱 API 地址、邮箱地址和邮箱/API 组合复制。
+ [优化] 注册成功后自动写入对应平台标签，注册失败释放对应平台标签，避免同一平台重复使用已注册邮箱。
+ [优化] 旧接口登录状态区分“已保存、检测正常、检测异常”，未检测的登录态不再误显示为异常。
+ [新增] 注册账号新增 Grok 纯协议目标，支持 gRPC-Web 邮箱验证、Turnstile 任务服务、Next.js Server Action 注册、SSO 独立保存和凭据导出，全流程不依赖 Chromium。
+ [新增] 内置 Grok2API 运行时，单个 chatgpt2api 进程即可提供 Grok 账号池、额度刷新、流式/非流式聊天、Responses、Messages、图片和视频兼容接口；根 `/v1` 按 Grok 模型自动分流，完整接口同时开放在 `/grok/v1/*`。
+ [修复] Grok 注册兼容 React Flight `T` 长文本及嵌套记录引用、Next.js `x-action-redirect`，并完整跟随 xAI/Grok 多域 Cookie Setter 链获取 `sso/sso-rw`；提交前暂存账号凭据，避免账号已创建但会话交换失败时丢失邮箱和密码。
+ [修复] iCloud Apple 协议在 WARP 编排下默认复用内部 Privoxy 出口；Apple 临时返回 HTTP 502/503/504 时有限退避重试，并在最终错误中显示具体请求路径，避免一次上游波动直接判定模块不可用。
+ [修复] 取消 sidecar 独立账户后，唯一管理员启动时会自动接管并合并旧版全局 iCloud 会话；保留当前账号 ID、IMAP App 专用密码、邮箱 API token、平台标签和邮件记录，删除同 Apple ID 的重复全局会话，避免有效登录态被隐藏后反复触发 Apple 登录。
+ [修复] 主系统内部 iCloud 请求自动映射到唯一管理员 owner，避免移除 sidecar 独立账户后把“未登录占位对象”显示成一条 Apple 登录态，导致账号名称缺失且“可创建账号”错误显示为 0。
+ [修复] ChatGPT 注册遇到 OpenAI `passwordless_login` 已有账号分支时，自动把当前邮箱标记为 GPT 并更换未标 GPT 的邮箱继续注册；每次重试使用独立会话并设置跳过上限，避免已注册邮箱重新进入 GPT 候选池或直接终止整条任务。
