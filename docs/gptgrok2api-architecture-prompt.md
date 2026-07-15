# GPT 架构图生成提示词

请生成一张用于项目 README 的专业软件架构图，项目名称为“GPTGrok2API”。画布使用横向 16:10 比例，白色或极浅灰背景，采用蓝色、紫色、金色、绿色四种低饱和配色，扁平化、清晰、克制，不要渐变、不要装饰性插画、不要人物和设备照片。

请严格按四层布局绘制，并让连线尽量水平或垂直，避免交叉：

1. 入口层：OpenAI SDK / 第三方客户端、Vue 管理控制台、Sub2API、反向代理 / HTTPS。
2. 主网关层：认证与模型路由、GPT 运行时、Grok SSO 运行时、Grok Build OAuth。标注 `/v1` 与 `/grok/v1` 两类接口。
3. 业务任务层：OpenAI / Grok 注册引擎、邮箱 Provider 路由、平台标签与领取状态、Checkout、代理与观测。注册引擎连接到“内置 iCloud 邮箱”与其他邮箱 Provider；GPT 标签使用绿色，Grok 标签使用蓝色；Checkout 只展示 UPI 最终支付链接，并标注 IN Checkout / Provider / Approve 共享 sticky 出口。
4. 数据与状态层：账号与平台标签、iCloud sidecar 状态、媒体与文件、备份与审计。

在主网关右侧单独放置“iCloud Privacy Mail sidecar”边界，明确标注“Compose 内部网络、无独立账号、无宿主机端口”。边界内部包含四个小模块：Apple 新接口（登录 / 2FA / 创建邮箱）、iCloud 旧接口（登录 / 2FA / 同步邮箱）、IMAP 取码（App 专用密码）、定时创建（新接口 20 个/小时、旧接口 5 个/小时、每账号 750 个）。

箭头关系必须表达：入口层进入主网关；主网关分流到 GPT 和 Grok；管理 API 进入注册、Checkout、代理和 iCloud；注册引擎经过 Provider 路由获取邮箱并写入 GPT/Grok 标签；iCloud sidecar 为创建、同步和验证码提供服务；各模块最终写入账号、邮箱、媒体和备份状态。

所有文字使用简体中文，技术名词保留 GPT、Grok、OAuth、IMAP、UPI、API、Provider、Checkout、sidecar 的英文写法。不要添加控制台网址、API Base URL、Git 仓库地址、版本号或免责声明。输出一张可用于 README 的高清 SVG 或 PNG，文字必须清晰可读。

负面要求：不要使用复杂的 3D 效果、霓虹色、渐变背景、大面积阴影、营销海报风格、无意义图标、过长段落、交叉连线或无法辨认的小字。
