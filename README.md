# QuantPilot

一个可本地运行、可部署到 Linux 的现货量化研究与模拟交易控制台。

**版本 0.1.0 是模拟盘版本。没有真实订单发送通道。** Web、环境变量、配置文件和 AI 输出均不能启用实盘。账户查询使用只读交易所凭据；不要给予提币权限。

## 项目来源与范围

开发日期：2026-10-26（Asia/Shanghai）。本次环境无法访问先前 ChatGPT 环境中的 `/mnt/data/quantpilot-starter.zip`、`README.md` 或 `CODEX_TASKS.md`，关联对话也未提供可下载附件。因此这是按照当前需求重新建立的独立实现，不是对初版代码的合并或审查结论。拿到原始压缩包后，应先比较再合并。

目前包含：

- Binance、OKX、Bybit、Kraken、Coinbase 的 CCXT 现货行情适配与只读余额查询；具体交易对可用性由交易所决定。
- 完成 K 线过滤、EMA20/50、Wilder RSI14、ATR14、量比和带有效期的策略信号。
- 单策略自动模拟运行、手动限价 IOC 模拟下单、止盈止损轮询、手续费与滑点。
- 持久化订单幂等、现金/仓位账本、风控、暂停开仓、审计和重启保护。
- 中文响应式 Web 控制台，密码登录、会话撤销、CSRF、防暴力登录、加密凭据。
- 可选 OpenAI Responses API 风险复核，结构化输出校验、限频、无交易工具权限。
- Linux systemd 安装与更新脚本、`qp` / `quantpilot` 快捷菜单、备份、Cloudflare Origin Rules 配置生成。
- 固定依赖及下载哈希、测试、静态检查、秘密扫描和 GitHub Actions 待启用模板。

尚不包含：实盘执行、合约/杠杆/做空、交易所原生止损单、历史回测、多人权限系统、跨进程扩容。不能把模拟测试结果解释为实盘认证或收益证明。

## 本地启动

需要 Python 3.11–3.13，Linux 或 macOS。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .
qp init --username admin --port 8080
qp serve
```

初始化会交互式要求密码（至少 12 位），不会将密码放入命令行、日志或代码。打开 `http://127.0.0.1:8080`，使用初始化账号登录。默认尝试公共交易所行情。

离线演示需要单独的数据目录，并显式设置 `--demo`：

```bash
export QP_CONFIG="$PWD/data-demo/config.json"
qp init --username demo --port 8081 --demo
qp serve
```

演示界面持续显示合成数据标记。交易所失败不会自动切换为演示数据。演示与公共行情不要共享一个账本。

## 使用流程

1. 在总览选择交易所、交易对和周期，检查盘口时间、图表和信号有效期。
2. 先用模拟订单验证：买单必须输入数量、限价、止损价和止盈价。限价不满足立即成交条件时直接拒单，不保留挂单。
3. 在策略页设置单次预算、止损/止盈比例并启用。策略使用完成 K 线；同一策略市场已有仓位时不会加仓。
4. 在连接页按需保存只读交易所凭据。公共行情不需要密钥。余额查询只返回余额，不导入模拟账本。
5. 如需 AI，保存 OpenAI Key，填写账号可用的模型 ID 并启用。仅点击“AI 风险复核”才产生请求及可能的 API 费用。AI 不会下单或修改配置。
6. 查看订单与审计。暂停开仓按钮同时停用策略，仍允许减仓和保护退出。解除暂停不会自动恢复策略。

默认观察列表为 BTC、ETH、SOL、BNB、XRP 对 USDT；这是本版可配置默认值，不代表已恢复原项目的五资产清单。Kraken/Coinbase 等交易所可能不提供列表内所有交易对，会明确拒绝不支持的市场。

## 风控默认值

| 项目 | 默认值 |
|---|---:|
| 初始虚拟资金 | 10,000 USDT |
| 单笔模拟订单上限 | 500 USDT |
| 总持仓敞口上限 | 3,000 USDT |
| 单市场仓位上限 | 1,500 USDT |
| 单仓预计止损损失上限 | 50 USDT |
| 每日权益损失限额 | 300 USDT |
| 模拟手续费 / 滑点 | 10 / 5 bps |
| 允许最大报价年龄 / 价差 | 30 秒 / 30 bps |
| 订单有效期 | 前端 30 秒；服务端最多 60 秒 |
| 模拟保护轮询 | 15 秒，网络耗时可能延长 |

限额在配置文件中保存，修改后重启服务。每日基准按 UTC 日期切换，在当天首次有效估值时建立，不是追溯计算的零点价格。估值使用买一价。持仓、手续费和盈亏均为模拟；所有交易所共用一个虚拟 USDT 资金池，不模拟跨所转账。

止损风险预算是按止损价估算的损失，跳空、滑点和停机可能导致实际模拟损失超过该值。保护卖出可超过开仓单笔限额，以免大幅上涨后的仓位无法退出。其他手动减仓仍受单笔限额约束。

## Linux 部署与管理

完整说明见 [部署手册](docs/DEPLOYMENT.md)。在下载并审阅代码后，一条命令开始交互式安装：

```bash
sudo bash scripts/install.sh
```

或指定端口与账号，密码仍交互输入：

```bash
sudo bash scripts/install.sh --port 8080 --username admin
```

安装后使用：

```bash
sudo qp                 # 管理菜单
sudo qp status          # 状态
sudo qp start           # 启动
sudo qp stop            # 停止
sudo qp restart         # 重启；自动策略回到停用状态
sudo qp logs            # 最近 100 行日志
sudo qp kill on         # 暂停开仓
sudo qp kill off        # 允许开仓，策略仍需手动启用
sudo qp password        # 修改密码，立即撤销旧会话
sudo qp backup          # 数据库一致性备份，包含主密钥
sudo qp doctor          # 配置、密钥和数据库检查
```

## 测试

```bash
python -m pip install --require-hashes -r requirements.lock -r requirements-dev.lock
bash scripts/check.sh
python -m pip_audit -r requirements.lock --disable-pip
```

测试不需要交易所或 OpenAI 密钥，不发送真实订单。覆盖范围与实际验收结果见 [测试报告](docs/TEST_REPORT.md)。本机没有 Linux/systemd 环境，不能将脚本检查当作服务器部署验收。

## GitHub 与密钥

聊天里曾出现的 GitHub Token 必须撤销。项目不使用该 Token。后续授权使用 GitHub 安全连接或用户自行配置的 OAuth/SSH。此次发布经用户明确授权，凭据仅临时在进程内存中用于 GitHub API；未写入文件、Git 配置、仓库 URL 或输出。不要把 Token 放进仓库 URL、命令参数或安装脚本。

仓库地址：<https://github.com/Jk-z-Box/QuantPilot>（私有）。安装命令和 CI 启用步骤见部署手册；仓库发布不代表已经部署到 Linux 服务器。

## 设计与资料

- [架构与数据流程](docs/ARCHITECTURE.md)
- [安全边界与实盘验收门槛](docs/SECURITY.md)
- [剩余任务与交接记录](CODEX_TASKS.md)
- CCXT 的统一行情接口及限速行为参考 [官方 Manual](https://docs.ccxt.com/#/README)。实际市场、精度和交易限制从交易所元数据读取。
- OpenAI 接口遵循 [Structured Outputs 官方文档](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)，使用 Responses API 的 `text.format`，并再次在本地校验。
- Cloudflare 的端口覆盖方式见 [Origin Rules 官方文档](https://developers.cloudflare.com/rules/origin-rules/examples/change-port/)。
