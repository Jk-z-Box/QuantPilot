# 逐仓与全仓（0.6.0）

在「账户与仓位 → 账户设置 → 对应交易对 → 保证金模式」选择逐仓或全仓，然后保存。每个交易对独立设置。旧设置和旧持仓保持逐仓；持仓或待核对订单存在时不能转换模式，平仓后再修改。杠杆仍按交易对单独设置并校验交易所限额。

## 本地模拟

- 逐仓：分配保证金为开仓名义价值 / 杠杆。资金费从该仓位保证金扣收；跳空造成的模拟净亏损最多消耗该仓位剩余保证金，调整额记入平仓记录。
- 全仓：同一模拟账户的全仓仓位共享 USDT 钱包资金。先扣除逐仓占用保证金；其他全仓仓位浮动盈亏、维持保证金、手续费和资金费都会影响估算。
- 可用资金 = 钱包余额 + 全仓浮动盈亏 − 全部仓位的初始 / 分配保证金。逐仓未实现盈利不供其他仓位开仓使用。
- 加仓后使用合并数量、加权均价和累计保证金。减仓释放对应比例保证金。全仓强平线每次估值和保护检查重新计算。

模型假定 USDT 线性永续、单向持仓、统一维持保证金率 m=0.005、预计平仓手续费 f=0.0005。这不是 Gate、OKX、Binance 的实际风险档位模型，也不模拟交易所保险基金、ADL、逐级减仓或组合保证金。

设数量 Q、平均开仓价 E、方向 s（多 +1，空 −1）、可用于本仓位的保证金 C，解 C+s×Q×(P−E)=Q×P×(m+f)：

`P = (Q×E − s×C) / (Q×(1 − s×(m+f)))`

逐仓的 C 为该仓位分配保证金。全仓的 C 为钱包余额减逐仓占用，加其他全仓仓位浮盈亏，再减其他全仓仓位的维持保证金和平仓费预留。求某仓位的价格时，假设其他仓位价格保持不变；多个市场同时波动时需重新估算。报价采用可执行买卖价近似，缺少新鲜报价时标记不可验证，不据此开仓或按旧价格强平。没有正价格解时显示「当前估算无正价格强平线」，不等于永远没有风险。

## 交易所账户

- Gate：全仓使用 leverage=0 和 cross_leverage_limit=所选倍数；逐仓使用 leverage=所选倍数，由当前 CCXT 适配器转换。
- OKX：杠杆设置传 mgnMode，订单传对应 tdMode，不切换全局账户类型。
- Binance USDT 永续：针对所选交易对切换 CROSSED / ISOLATED，再设置杠杆。
- 成交后与后续风控均核对交易所实际保证金模式、单向模式和实际强平价；模式不一致、强平数据不可验证时停止新增风险。原生只减仓保护订单使用持仓原模式。交易所返回 0 / 缺失强平价目前不作为可靠风险证明，执行会被保守阻止。
- 实盘总开关默认关闭。0.6.0 验收记录必须分别证明逐仓与全仓通过交易所联调；本地替身测试不能替代实际验收。

依据：[Gate 杠杆 API](https://www.gate.com/docs/developers/apiv4/en/#update-position-leverage)、[Gate 预估强平价](https://www.gate.com/help/futures/futures/27724/calculation-of-estimated-liquidation-price-and-bankruptcy-price-of-contracts)、[OKX API](https://www.okx.com/docs-v5/en/#rest-api-account-set-leverage)、[Binance 保证金模式](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Change-Margin-Type)。
