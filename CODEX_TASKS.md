# QuantPilot 交接任务

## 已实现

- [x] 独立重建 Python/FastAPI/SQLite 项目与中文 Web 控制台。
- [x] 5 个交易所适配、公共盘口与 K 线、只读账户接口。
- [x] EMA/RSI/ATR/量比信号、有效期、自动模拟执行与策略停用恢复保护。
- [x] 模拟账户账本、限价 IOC、费用滑点、持久化幂等、并发风控、止盈止损。
- [x] 密码/会话/CSRF/限速/凭据加密/审计/单进程保护。
- [x] 可选 OpenAI Responses 风险建议与 Web 配置，禁止 AI 触发订单。
- [x] Linux 安装更新脚本、自定义端口账号密码、systemd、CLI 快捷菜单、备份。
- [x] Cloudflare HTTPS 回源说明和单条 Origin Rule JSON 生成。
- [x] 自动测试、依赖哈希、安全扫描、GitHub Actions 待启用模板。

## 需要外部输入或环境

- [ ] 导入用户提供的原始 starter ZIP，审查原始 README/CODEX_TASKS，比较后合并。本次 `/mnt/data` 不可访问，不得声称已经导入。
- [x] 创建私有 GitHub 仓库 `Jk-z-Box/QuantPilot`。此次上传使用用户再次明确授权的临时内存凭据，不在本项目保存；后续应换用安全连接或 OAuth/SSH。
- [ ] 完成远程提交核验，并在有 workflow 权限的授权下启用 `ci/github-actions.yml`：移动至 `.github/workflows/ci.yml` 后检查 CI。当前 Token 没有 workflow 权限，故只上传模板。
- [ ] 在用户 Linux 主机或独立测试虚拟机实际验收首次安装、更新、回滚、重启及备份恢复。
- [ ] 获得实际域名/服务器信息后配置 Cloudflare DNS、Origin Rules、证书、防火墙，并从最终域名验收。
- [ ] 用用户自行保存的只读密钥做各交易所账户查询验收。自动测试只使用替身，不证明账户权限正确。
- [ ] 用户选定 OpenAI 模型并保存 Key 后进行一次明确授权的 API 验收。本次未调用付费模型。

## 后续功能

- [ ] 历史回测、走样本外验证和更真实的深度/部分成交模拟。
- [ ] 外部不可篡改审计、监控、备份保留策略、MFA/RBAC。
- [ ] 可选择多个独立模拟策略，明确组合风险与资金分配。
- [ ] 只在完成 `docs/SECURITY.md` 的独立认证后考虑实盘组件。

继续开发时先阅读测试报告，不要把“已实现接口”“替身测试通过”“真实服务验收通过”混为同一状态。不要在日志或提交中保存账号密码、API Key、Token、真实余额或用户个人数据。
