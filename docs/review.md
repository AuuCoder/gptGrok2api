# Review

- [P2] Poll when the image tool was invoked  
  `services/protocol/conversation.py:586`  
  对于没有输入图片的图像生成任务，延迟结果仍可能在 SSE 返回 `tool_invoked: true` 之后到达，但这里新的判断条件忽略了 `tool_invoked`。这样会直接返回中间文本，而不会继续轮询会话来拿图片 ID。

- [P2] Align CloudMail domain validation with the UI  
  `web/src/app/register/components/register-card.tsx:266`  
  `cloudmail_gen` 的 placeholder 写的是留空会使用服务默认域名，但后端 `create_mailbox` 不接受空域名。用户按 UI 提示保存后，这个 provider 会在真正创建地址前失败。
