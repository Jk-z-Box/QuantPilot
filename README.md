# QuantPilot 0.2

中文多账户永续合约研究与模拟交易工作台。先观察市场、查看评分依据，再用独立模拟资金验证。默认不发送真实订单。

## 打开后怎么用

1. **市场与机会**：默认 BTC、ETH、SOL、AVGO、SOXL，使用 Binance USDT 永续合约名称。点品种切换 K 线，右侧选择周期；“管理交易对”可换成 OKX / Gate，或添加其他已上市合约。
2. **看机会说明**：周线、日线、4 小时、1 小时共同评估，显示方向、开仓区间、止损、两档止盈及比例、含费用的收益风险比、加减仓观察价和有效期。达到 80 分并通过全部阻断检查才有效，评分不是胜率。
3. **账户与仓位**：默认独立的 10,000 USDT 模拟账户。可新增账户、重置资金、设置逐品种固定/最大名义金额及加仓次数比例，再手动启用自动交易。
4. **数据与 AI**：公开行情、美联储、Deribit、Gate 清算统计无需密钥。CoinGlass / Finnhub / OpenAI 按需在网页加密保存密钥。AI 默认关闭。
5. **交易记录**：查看开仓、加仓、分批退出、未确认订单和操作记录。顶部“暂停开仓”停用全部自动交易，已有仓位保护继续。重启后自动交易需要重新启用。

旧版现货模拟账本保留在 `/legacy`，不迁移为合约资金，不与新账户混用；旧版说明见 [历史手册](docs/LEGACY-0.1.md)。

## 已实现的能力与边界

| 功能 | 本版行为 |
|---|---|
| 市场 | Binance / OKX / Gate 的已上市 USDT 线性永续，按交易所合约目录校验；不支持反向、交割或期权下单 |
| 图表 | 5m / 15m / 1h / 4h / 1d / 1w，蜡烛、成交量、悬停详情、缩放；展示未收盘线，评分仅用完成线 |
| 更新 | 报价及保护循环约 3 秒，网页约 5 秒，多周期评估约 30 秒；网络耗时会延长，属于轮询而非 WebSocket |
| 技术评分 | EMA20/50、RSI14、MACD12/26/9、ATR14、波动率、量比、多周期趋势、费率及 OI |
| 外部证据 | 来源、披露/查询时间、失效时间、覆盖范围和缺失状态；不把缺失证据按比例放大成满分 |
| 模拟账户 | 独立账本、多空、保证金预留、费用滑点、部分退出、资金重置、持久化幂等 |
| 仓位配置 | 固定金额不足风险额度即拒绝；最大金额动态评估且包含加仓，为计划加仓预留额度；比例基于初始名义金额 |
| 交易所账户 | 隔离凭据、余额/仓位只读核对、逐账户同交易所品种授权；资金、已持仓、强平距离和未知订单检查 |
| 执行组件 | 测试环境/实盘独立门槛；IOC 入场、确认成交量后放置原生只减仓止损及两档止盈；不确定结果锁定账户、不盲目重试 |
| 动态保护 | 趋势反转/事件风险退出、机会失效一次性减半、分批止盈；AI 可受限减仓/退出/收紧止损及止盈，不能放宽风险 |
| 系统 | 密码登录、会话/CSRF、加密凭据、审计、单进程、Linux 安装升级、CLI、Cloudflare 回源说明 |

**本版没有完成交易所测试网及实盘认证。** 私有 API、不同账户模式和原生保护单必须逐交易所联调；本地替身测试不能证明交易所实盘可用。生产默认关闭 `QP_LIVE_EXECUTION` 和 `QP_DEMO_EXECUTION`，网页不能解除服务端门槛。详见 [执行验收](docs/ACCEPTANCE.md)。

模拟账户的“模拟交易所”保存环境身份，使用所选监测品种的市场价格；账本使用统一费用/滑点/强平估算模型，不声称精确复制各交易所的撮合、手续费等级或保证金档位。没有历史回测、多人权限、跨进程扩容和全市场新闻覆盖。

## 数据来源

- **行情**：所选交易所自己的盘口、K 线、资金费率与 OI。行情不可达时显示缺失，不暗中切换交易所或合成价格。
- **期权**：Deribit 最近到期期权 OI、交割窗口及按 OI 估算的最大痛点。最大痛点是统计估算，不是目标价；没有股票期权公开源时保持缺失。
- **宏观**：美联储官方 FOMC 日历和公告；日历日期按通常 14:00 ET 转换，仅作风险窗口。Finnhub 连接可补充公司财报和新闻；不声称实时覆盖所有突发事件。
- **清算**：默认 Gate 对应合约的公开历史清算统计，清楚标注单市场范围；CoinGlass 可提供聚合清算和热力图。热力图是供应商模型，不能当作实际成交或确定清算价。
- **ETF**：公开来源不足时不编造。可人工导入已核验披露并保存链接/时间，也可配置 CoinGlass 获取已披露交易日净流量；它不是盘中即时资金流。权限和币种覆盖由供应商决定。
- **AI**：可选 OpenAI Responses API。启用会发送公共指标、事件摘要、匿名化账户资金与持仓数据，并产生 API 费用；不发送密钥和账户名称。严格校验结构，评分上调最多 5 分且不超过数据覆盖度，下调最多 30 分，只能缩短时效、收紧保护，仍须通过确定性风控。

默认五个名称不保证每个合约已上市、可在当前地区访问或已有 60 根周 K。新上市合约历史不足会阻止交易。公开数据不足以达到 80 分时，等待是正常行为；不会为“产生信号”而降低门槛。

## 风险偏好

| 参数 | 保守 | 平衡（默认） | 激进 |
|---|---:|---:|---:|
| 单笔止损风险预算 / 权益 | 0.3% | 0.6% | 1% |
| 总名义敞口 / 权益 | 30% | 60% | 90% |
| 最大杠杆 | 1x | 2x | 3x |
| 信号最长有效期 | 120 秒 | 180 秒 | 240 秒 |
| 最低含费用收益风险比 | 2.0 | 1.8 | 1.6 |
| 自动加仓次数上限 | 1 | 2 | 3 |

每组相关资产敞口最多占组合敞口限额 75%；当前按加密/股票 ETF 分组保守限制，尚不是滚动相关系数模型。组合止损风险最多 3 倍单笔预算，UTC 日损失达到 5 倍单笔预算阻止新增风险。默认两档止盈为 1.5R / 3R，各 50%。默认模拟手续费每边 5 bps、滑点 2 bps；资金费率按 8 小时近似，强平价为简化逐仓估算。跳空和停机仍可能超过计划损失。

## 本地启动

需要 Python 3.11–3.13。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .
qp init --username admin --port 8080
qp serve
```

打开 `http://127.0.0.1:8080`。账号/端口可自定义，密码至少 12 位、在终端隐藏输入。公共行情不需要交易所密钥。离线演示必须单独设置 `QP_CONFIG` 路径并在初始化时明确加 `--demo`；界面始终标记合成数据。

## Linux 与域名

```bash
sudo bash scripts/install.sh --port 8080 --username admin
sudo bash scripts/install.sh --upgrade
sudo qp                  # 快捷菜单
sudo qp status           # 含新工作区及账户状态
sudo qp kill on          # 同时暂停新旧工作区开仓
sudo qp kill off         # 解除暂停；不会重新开启自动交易
sudo qp password         # 更新密码并撤销会话
sudo qp backup           # 数据库一致性备份
sudo qp doctor
```

`qp start / stop / restart / logs` 管理服务。多账户密钥在网页设置，旧 `qp credential` 仅管理旧版连接。升级保留配置、密钥与旧账本。

Cloudflare 路径必须为浏览器 HTTPS → Cloudflare → 回源端口的 **HTTPS** 服务 → 本机应用。Origin Rules 改端口不会转换 HTTP/HTTPS。部署、备份和一键命令结构见 [部署手册](docs/DEPLOYMENT.md)。

## 验证与维护

```bash
python -m pip install --require-hashes -r requirements.lock -r requirements-dev.lock
bash scripts/check.sh
```

测试结果、实际外部探测和未验证项见 [验收记录](docs/TEST_REPORT.md)，后续工作见 [交接任务](CODEX_TASKS.md)。当前项目为按需求独立建立的实现；最早的 `/mnt/data/quantpilot-starter.zip` 在此环境不可访问，未声称导入或审查过该压缩包。

官方接口依据：[Gate](https://www.gate.com/docs/developers/apiv4/en/futures/)、[OKX](https://www.okx.com/docs-v5/en/)、[Binance](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info)、[Deribit](https://docs.deribit.com/api-reference/market-data/public-get_book_summary_by_currency)、[美联储](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)、[CoinGlass](https://docs.coinglass.com/reference/etf-flows-history)、[OpenAI](https://developers.openai.com/api/docs/guides/structured-outputs)。
