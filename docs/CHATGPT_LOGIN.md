# OpenAI 官方登录授权

QuantPilot 0.3.0 同时支持 API Key 和官方 **Continue with ChatGPT**。

- API Key 使用 OpenAI API 余额，与 ChatGPT Plus 账单分开。
- 官方登录使用 OpenAI 实际授予的 ChatGPT 计划权限。Plus 的共享用量与其他应用合并；不能保证每个账户都有资格、所有模型或联网能力均可用。
- 两种连接互不替代，不会因官方额度不足自动调用已保存的 API Key。

## 连接到你的服务器

1. 登录 QuantPilot → **数据与 AI → AI 绑定方式 → OpenAI 官方登录授权**。
2. 选择“添加新账户”，点击 **Continue with ChatGPT**。网页生成一个 10 分钟有效、只能使用一次的连接码。
3. 在**浏览器所在电脑**的终端启动登录助手：

```bash
qp chatgpt-login --server https://你的域名
```

4. 按终端提示粘贴连接码，输入不会显示。在随后打开的 `auth.openai.com` 官方页面登录并确认是否允许 QuantPilot 使用你的 ChatGPT 计划。不要把 ChatGPT 密码填写到 QuantPilot。
5. 助手通过 HTTPS 把验证后的凭据交给服务器。网页刷新连接状态后，获取模型、选择模型并保存设置。只有你启用相应开关，AI 复核或联网补全才会运行。账户自动交易需要重新手动启用。

**连接码不是 OpenAI 密钥，但它能绑定账号，不要分享。** 令牌不会放进命令参数、浏览器存储或日志；本机助手不会保留或继续刷新已交给服务器的令牌。

### 本机还没有 qp

要求 macOS / Linux、Python 3.11–3.13。下载本次发布的源码 ZIP，解压后在 `QuantPilot` 目录运行以下命令。无需在本机设置交易账户或启动服务器：

```bash
python3 -m venv .venv-login
.venv-login/bin/python -m pip install --require-hashes -r requirements.lock
.venv-login/bin/python -m pip install --no-deps --no-build-isolation .
.venv-login/bin/qp chatgpt-login --server https://你的域名
```

远程服务器已经安装 qp，不能代替这一步：官方回调地址固定在浏览器所在电脑的 `http://127.0.0.1:<随机端口>/auth/callback`。本机助手会先启动回调监听，再打开浏览器；不需要修改 Cloudflare、开放服务器回调端口或降低 HTTPS 安全性。

## 账号、模型与断开

- 每个 OpenAI 注册和验证身份独立保存，即使邮箱相同也不会合并。选中已保存账户后可重新登录，或使用已有连接。
- 新增/切换/断开当前官方连接会暂停 AI 自动任务和账户自动交易，供你重新检查模型与设置；已有仓位的原有保护继续工作。
- “获取 / 刷新模型”读取所选连接的目录。目录可读取不意味着本次推理一定获准执行；权限或能力不匹配会明确显示错误。
- 计划额度耗尽、资格/权限失效时暂停该连接的新请求。在 **查看 ChatGPT 用量与授权** 中处理后，点击 **重试授权连接**；失效的登录需要重新授权。
- “断开授权”尝试撤销可续期会话并删除服务器上的 access / refresh / ID token，保留注册映射以便重连。网络失败时会明确提示远端撤销未确认，可在 ChatGPT 设置中断开 QuantPilot。
- 删除 API Key 不会删除独立的官方登录。

## 技术与数据边界

OAuth 使用 PKCE S256、随机 state/nonce、RS256/JWKS 校验签名、issuer/audience/期限与身份。连接码只以摘要存储，ID token 在服务器再次校验。令牌使用现有加密凭据库，续期跨线程/进程串行化，access/refresh/期限/权限一起替换。

服务器保留自己的稳定 host ID；本机保留自己的 host ID。API Key 与 OAuth 使用同一官方 Responses 地址，但 OAuth 使用 `store:false`、`stream:true`，只接受 `response.completed` 的完整结果。官方授权不支持的 `max_output_tokens` / `max_tool_calls` 不会发送，因此不承诺固定搜索次数；应用仍限制每轮品种数和等待时间，可在 ChatGPT 中设置应用用量限制。

启用风险复核会发送行情、事件摘要及匿名化账户资金/持仓数据；联网补全只发送公开市场标识及缺失类别。模型建议仍需通过本地校验和交易风控，不会直接执行订单。

本次测试为签名令牌及网络替身测试，包含真实本机 loopback 协议测试。没有替用户完成 OpenAI 登录，也未验证此 ChatGPT 账户的实际共享资格。真实交易保持禁用。

## 官方依据

- [开放源码客户端官方登录流程](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [自托管服务器连接流程](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms)
- [账号、续期与撤销](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [模型与推理协议](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [错误与恢复](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery)
