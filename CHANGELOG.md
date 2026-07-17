# Changelog

## 1.0.2 - 2026-07-17

+ [新增] Grok 4.5 OAuth 账号支持按配置独立投递到 Sub2API 和 CPA；凭据始终先保存到本地，远程失败不影响授权结果。
+ [新增] OAuth 账号测试支持“全部账号”和“指定账号”两种模式，逐账号展示成功、失败、耗时和错误原因。
+ [优化] Grok 注册与协议授权日志收敛为关键步骤，并补充 OAuth 投递状态和账号运行状态展示。
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
