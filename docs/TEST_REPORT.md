# Validation / 驗證紀錄

## 0.15.1

- **586 tests passed**, with no failures/skips; Ruff and syntax checks passed. This includes the full 0.15.0 regression suite.
- Added broker coverage for the account-capital review format under the restricted permission profile, and a large multilingual review request covering all evidence collections and actual HTTPX wire bytes.
- A read-only production-data diagnostic reproduced the previous 350,390-byte request exceeding the 256,000-byte broker limit. The patched sample was 186,688 bytes, with explicit omissions and two complete trade-evidence records instead of zero. No model request or policy change occurred in that diagnostic.

## 0.15.0

- Python 3.12 local suite: **584 tests passed**, no failures/skips; 84% total line coverage. Ruff, JavaScript syntax and installer shell syntax checks passed.
- New coverage: independent and disabled account limits, fixed rejection breakdowns, shared add budgets, explicit pair allocation, legacy migration, UTC equity baselines, AI authorization/ranges, stale configuration/fill/model results, queue fairness, manual evaluation with auto off, unchanged-result notification suppression, protection during AI failures, and existing protection surviving policy edits.
- Exchange mocks check regular/algo order ID collisions, OKX algo scopes and fail-closed pagination. No paid model request or real order was sent during these tests. They are not venue certification.
- Browser verification at desktop and 390px mobile: paused-account saves, independent switches, revision-safe edits, history, restore-to-form without saving, refresh retaining the page, no horizontal overflow, dialog scroll reset and no console errors.
- Production deployment and preservation checks are recorded separately outside the repository.

本版通過 584 項本地測試及桌面／手機界面驗證。舊帳戶按原有效額度遷移；真實交易、交易所撮合及精確維持保證金檔位仍須另行驗收。

## 0.14.0

- Python 3.12 local suite: 550 tests passed. Ruff and JavaScript syntax checks passed.
- Tests include paused-account persistence, concurrent edit conflicts, display-order isolation, v1 cleanup preserving current data, sentiment source dates and scoring switches, loss-addition cooldown/count/risk constraints, long/short trailing ratchets, restart durability, one-shot partial exits, stale quote rejection and AI outage protection.
- An exchange mock verifies a partial trailing exit does not suppress a later hard-stop exit. This is not exchange certification.
- Browser verification at desktop and 390px mobile width: paused account edits, refresh preserving page, saved custom sound, sound decode/test, ordering and notification layout. Browser console contained no errors at completion.
- Production deployment and health checks are recorded separately outside the repository; this file does not assert a deployment succeeded.

本地測試與瀏覽器操作已驗證；交易所測試網／實盤撮合、各帳戶權限和精確保證金模型仍需逐項驗收。聲音受瀏覽器手勢、裝置音量與背景限制。
