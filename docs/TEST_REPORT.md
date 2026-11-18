# Validation / 驗證紀錄

## 0.14.0

- Python 3.12 local suite: 550 tests passed. Ruff and JavaScript syntax checks passed.
- Tests include paused-account persistence, concurrent edit conflicts, display-order isolation, v1 cleanup preserving current data, sentiment source dates and scoring switches, loss-addition cooldown/count/risk constraints, long/short trailing ratchets, restart durability, one-shot partial exits, stale quote rejection and AI outage protection.
- An exchange mock verifies a partial trailing exit does not suppress a later hard-stop exit. This is not exchange certification.
- Browser verification at desktop and 390px mobile width: paused account edits, refresh preserving page, saved custom sound, sound decode/test, ordering and notification layout. Browser console contained no errors at completion.
- Production deployment and health checks are recorded separately outside the repository; this file does not assert a deployment succeeded.

本地測試與瀏覽器操作已驗證；交易所測試網／實盤撮合、各帳戶權限和精確保證金模型仍需逐項驗收。聲音受瀏覽器手勢、裝置音量與背景限制。
