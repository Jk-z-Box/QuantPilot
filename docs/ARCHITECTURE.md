# Architecture / 架構

QuantPilot runs as one application process per SQLite data directory. A separate unprivileged Codex broker holds server-side login state. Persistent keys, databases and broker credentials are outside the source tree.

```text
Exchange public/private WebSocket + bounded REST recovery
                    ↓
MarketStreams / FundsStreams → Workspace → Realtime → browser WebSocket / SSE
                    ↓              ↓
completed candles → signals → account sizing/risk → optional AI review
                                   ↓
                     PaperBroker / ExchangeBroker
                                   ↓
            durable ledger, order journal, native protection
```

| Module | Responsibility / 職責 |
|---|---|
| `app.py`, `v2/api.py`, `admin_api.py` | Session, CSRF, bounded request models, current UI and administration |
| `store.py` | Exclusive pooled SQLite connections, transactional state and encrypted vault |
| `v2/market_streams.py`, `funds.py`, `realtime.py` | Exchange subscriptions, reconciliation, fanout and freshness |
| `v2/signals.py`, `rules.py`, `logic.py` | Active parameters, scoring, logic versions, review and backups |
| `v2/accounts.py`, `execution.py`, `risk_context.py` | Sizing, balances, exposure, liquidation and order lifecycle |
| `v2/position_automation.py` | Explicit loss-addition eligibility and durable one-shot trailing exits |
| `v2/journal.py` | Decision snapshots, fills, costs and retrospective evidence |
| `v2/feeds.py`, `sentiment.py`, `research.py` | Sourced observations, publication dates, scope and AI retrieval verification |
| `v2/action_review.py`, `position_review.py` | Shared market review, per-account risk review and position plans |
| `retired.py` | Backup-first removal of v1-only data; no current ledger deletion |

The browser consumes server state and does not hold exchange keys or make exchange orders. Refresh authenticates before showing either the app or login, then restores tab-local navigation. Account edits use compare-and-swap revisions; stale forms cannot overwrite new settings.

瀏覽器只顯示伺服器狀態，不持有交易所密鑰或直接下單。實時連線會重連及補讀；時間戳、資料過期和網頁延遲分開呈現。策略分數、AI 意見和實際成交分開記錄，不能互相替代。

Paper and real execution share risk rules, but paper fills are estimates. New exposure fails closed on missing risk inputs or unknown orders. Existing native protection remains at the exchange when the app is unavailable. Server-side trailing logic requires a working server and fresh quotes.
