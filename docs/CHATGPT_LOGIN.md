# ChatGPT 登录与服务器设备授权


## 推荐：服务器登录，无需终端

0.4.0 新增官方 Codex **设备码登录**。API Key 和旧版 SIWC 连接独立保留，不会自动回退或混用余额。

1. 在 QuantPilot 的「数据与 AI」中，将 AI 绑定方式选为 **Continue with ChatGPT · 服务器登录**。
2. 点击 **Continue with ChatGPT**，复制网页生成的短授权码，再点击 **打开官方授权页面**。
3. 在 `https://auth.openai.com/codex/device` 登录自己的 ChatGPT 账户、输入代码并确认。若提示未启用设备码登录，到 ChatGPT「设置 → 安全」开启；工作区账号可能需要管理员允许。
4. 返回 QuantPilot，页面会自动识别完成状态。点击「获取 / 刷新模型」，选择模型，勾选需要的 AI 功能并保存。可以先手动复核一次。

代码约 10 分钟内有效，以官方实际状态为准。可取消或重新生成；刷新页面后点击登录可显示仍有效的同一代码。不要向他人分享授权码。服务器重启保留已完成的登录，尚未完成的代码需要重新生成。若关掉网页后才完成授权，再打开页面点击「使用此连接」。

本流程只把官方登录/令牌续期/模型调用放到服务器；**你本人仍需在官方页面确认**。QuantPilot 不接收 ChatGPT 密码。服务器目前保存一个 Codex 登录，换账号前先断开。断开会执行官方 `account/logout` 清理本机登录；需要远端撤销会话时，请在 ChatGPT 安全设置处理。

登录、切换及断开操作会暂停账户自动交易；选用新连接会关闭 AI 自动任务并清空模型选择，供你核对后再启用。原有持仓保护继续运行。账户配额、模型权限和联网搜索能力，以 OpenAI 实际返回为准；模型目录可读取不代表推理已验证可用。

### 服务器运行方式

安装脚本安装固定版本 **Codex 0.165.0**，从 OpenAI 官方发布下载 Linux x86_64/aarch64 二进制并校验 SHA-256。无需在 Mac 安装助手或上传本机 `auth.json`，无需开放新公网端口或修改 Cloudflare。

- `quantpilot-ai` 是独立 Linux 用户和 systemd 服务；本地 Unix socket 只允许交易服务所在组调用固定接口。
- 凭据由官方 Codex 管理，位于 `/var/lib/quantpilot-ai`（0700、进程 umask 0077），不会复制到浏览器或当作 API Key 使用。**这里采用官方文件凭据存储，不声称额外应用层加密**；服务器管理员需保护磁盘和备份。
- AI 服务被禁止访问 `/var/lib/quantpilot` 与 `/etc/quantpilot`，看不到交易密钥、账本、配置或订单执行模块的数据。子进程环境不继承 API 密钥。
- 禁用 shell、应用连接、插件、子代理及电脑操作；临时分析会话使用仅可读取空工作目录的沙箱。任何执行/提权/外部工具审批都被拒绝。只允许模型内置公开网页搜索，返回建议还需经过已有校验和风控。
- 只采纳完成的回合和符合结构的最终结果。联网补全要求实际打开的来源 URL，再由 QuantPilot 核对引用、日期及域名；无法核对仍记缺失。
- 升级保留登录，已有 `auth.json` 备份到 root 专用的 `/var/backups/quantpilot-ai`。`qp backup` 的交易备份不包含此目录。回滚保留登录文件；备份同样视为凭据保护。

管理指令：

```bash
sudo qp ai-status
sudo qp ai-restart
sudo qp ai-logs
```

服务器 AI 日志不会记录原始协议、授权码或令牌。复核/联网请求有超时和单并发限制；超时仍可能产生 Codex 用量。不会因为额度不足自动扣 API Key 余额。

官方文档：[无界面设备登录](https://learn.chatgpt.com/docs/auth)、[设备码与模型调用协议](https://learn.chatgpt.com/docs/app-server)。

## 保留方式：API Key 与旧版 SIWC

旧版 SIWC 仍可使用，操作如下。

- API Key 使用 OpenAI API 余额，与 ChatGPT Plus 账单分开。
- 官方登录使用 OpenAI 实际授予的 ChatGPT 计划权限。Plus 的共享用量与其他应用合并；不能保证每个账户都有资格、所有模型或联网能力均可用。
- 两种连接互不替代，不会因官方额度不足自动调用已保存的 API Key。

## 旧版：本机助手连接到服务器

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
