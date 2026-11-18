# 0.13.0 验证记录

日期：2026-11-17；macOS ARM64、Python 3.12.14、CCXT 4.5.90。

- 全量自动测试：597 passed；语句覆盖率 83%。新增反向合约原生盈亏、逐仓/全仓强平、部分退出、资金币种隔离、合约精度、账户路由、AI 数量上限、原生风险档位、时钟差校准、行情乱序/过期、WebSocket 鉴权/Origin/重连快照/快速切换和有界队列测试。
- Python Ruff、JavaScript Node 语法、安装脚本 Bash 语法通过；仍有一条 Starlette 测试客户端 httpx 弃用提示。
- 锁定生产依赖安装校验和 pip check 通过。pip-audit 未报告已知漏洞；本次更新 websockets 17.4 和 opentelemetry-api 1.45.2。安全扫描不等于完整安全审计。
- 三家交易所公开 BTC/USD:BTC 合约目录与 WebSocket 盘口、标记价格、K 线更新实测。Binance 每张 100 USD、OKX 每张 100 USD（最小步长 0.1 张）、Gate 每张 1 USD；实际以交易所当前元数据为准。
- 公开链路时钟校准后的一次样本：Binance 约 91 ms ±93 ms、OKX 约 148 ms ±130 ms、Gate 约 114 ms ±90 ms。这是开发网络样本，非服务保证；频道推送频率和网络不同会改变结果。
- 本地浏览器：390×844 手机与 1440×960 桌面，无页面横向溢出；搜索币本位、重复提示、加入监测、创建 BTC 模拟钱包、币种金额精度、WebSocket 状态与推送均验证。合成演示环境网页传输 P95 约 1–2 ms，不代表生产外网延迟。
- 交易所私有币本位路由、资金归一化和执行组件用离线替身测试。没有发送真实订单，也未完成三家交易所币本位测试网/实盘认证；真实执行继续禁用。

生产升级检查另存交付验证报告，包括备份、配置/账户/历史/凭据保留、最终域名 WebSocket/SSE 与重新连接。上述本地测试不能替代生产检查。

依赖修复依据：[OpenTelemetry 官方公告](https://github.com/open-telemetry/opentelemetry-python/security/advisories/GHSA-mf5v-w3w3-f6cg)。
