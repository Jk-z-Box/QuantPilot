# QuantPilot

**A self-hosted, paper-first workspace for perpetual futures research and trade management.**

[繁體中文](README.zh-TW.md) · [Deployment](docs/DEPLOYMENT.md) · [0.15.0 changes](docs/V0150.md) · [Documentation](docs/README.md)

QuantPilot brings market context, configurable strategies, separate account ledgers and optional AI review into one responsive web console. The current release is **0.15.0**, maintained on **main**. Live execution is disabled by default.

## What you can do

| Area | Capabilities |
|---|---|
| Markets | Search and monitor listed Binance, OKX and Gate USDT linear or coin-margined inverse perpetuals. Settlement wallets remain separate. |
| Charts and state | Exchange-to-server and server-to-browser WebSocket streams, candle history, reconnect status, timestamps, REST recovery and SSE fallback. Millisecond timestamps do not promise millisecond exchange updates. |
| Opportunities | Completed weekly, daily, 4-hour and hourly candles; EMA, RSI, MACD, ATR, volume, volatility, funding, OI and sourced market context. Configurable score threshold, expiry and parameter switches. |
| Accounts | Independent paper, exchange test and live account configurations; per-pair quantity, leverage, isolated/cross margin, system sizing and addition budgets. |
| Account budgets | Independent margin/exposure/stop-loss/daily/group/liquidation limits, explicit pair allocation, auditable rejection amounts and bounded AI capital management. |
| Protection | Stops, staged targets, optional loss-addition evaluation and durable trailing take profit. Increasing exposure still requires fresh evidence, authorization and deterministic risk checks. |
| Optional AI | OpenAI API or server-side Codex login; opportunity review, account-specific pre-trade risk review, position review, sourced research and strategy retrospectives. AI cannot bypass execution gates. |
| Operations | Telegram categories, browser sounds and volume, strategy export/import/retention, audit history, Linux installer, CLI and web updates. |

Default watch labels are BTC, ETH, SOL, AVGO and SOXL. An exchange must actually list a requested contract; an unavailable symbol stays unavailable. The simulator does not replicate every exchange's fee tiers, maintenance tiers or matching engine.

## Get started

Use Linux with systemd, Python 3.11+, `python3-venv`, curl and tar. The command resolves **main** to a full commit once, then installs that exact source. Choose the port and username when prompted; enter the password only in the terminal prompt.

```bash
QP_REF=$(curl --proto '=https' --tlsv1.2 -fsSL https://api.github.com/repos/Jk-z-Box/QuantPilot/commits/main | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])') && curl --proto '=https' --tlsv1.2 -fsSL "https://raw.githubusercontent.com/Jk-z-Box/QuantPilot/$QP_REF/install.sh" -o /tmp/quantpilot-install.sh && sudo bash /tmp/quantpilot-install.sh --ref "$QP_REF"
```

For an existing installation, append `--upgrade`. For domain access and Cloudflare Origin Rules, follow [Deployment](docs/DEPLOYMENT.md) before exposing the console. Public installation requires no GitHub token.

1. **Markets** — select a symbol and timeframe, inspect score components and missing evidence; use “调整排序” to arrange monitor tiles.
2. **Accounts** — start with a paper account. Enable only intended pairs, configure sizing, then explicitly enable automation. Edit capital limits and AI authority within that account; “Evaluate and apply” can run without automatic capital management.
3. **Data & AI** — choose optional connections and model; enable only the reviews you want. A ChatGPT subscription and API billing are separate.
4. **Strategy** — edit active rules, inspect execution evidence and review history. Every change creates a backup; disabled parameters are excluded, not reset to defaults.
5. **Settings** — manage sign-in, notifications, volume and updates. Refresh restores the current page, instrument and timeframe in that tab.

## Evidence and execution boundaries

Public adapters include exchange data, Deribit, Federal Reserve announcements, ETF disclosure tables, news indexes and [Alternative.me's Bitcoin Fear & Greed Index](https://alternative.me/crypto/fear-and-greed-index/). Each observation carries its source, publication time, scope and freshness. Fear & Greed is a daily **BTC market proxy**, not a dedicated ETH/SOL/equity index. It is opt-in for scoring. Missing data is never invented or silently replaced by an AI estimate.

**Local tests are not exchange certification.** Real orders require server execution gates and a private, current, credential-bound certification covering native protection, partial fills, reconnects and margin modes. Unknown order results freeze further exposure. See [execution acceptance](docs/ACCEPTANCE.md). Existing protective exits continue when AI is unavailable; entries and additions requiring AI wait.

Trailing take profit runs on the server; it is not an exchange-hosted trailing order. Confirmed native stops remain in place if server connectivity fails. Stops and trailing exits can slip or gap and cannot guarantee profit.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install --require-hashes -r requirements-dev.lock
.venv/bin/pip install --no-deps --no-build-isolation -e .
.venv/bin/pytest
.venv/bin/ruff check .
```

Run one application process per data directory. Keep configuration, databases, encryption keys, authorization state and backups out of Git. The retired v1 spot console is removed in 0.14.0; upgrade backs it up before deleting its own tables/keys and preserves the current workspace.
