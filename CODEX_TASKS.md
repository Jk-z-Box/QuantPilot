# Maintainer handoff / 維護交接

Current source: 0.15.0 on main. Read README, docs/V0150.md and docs/ARCHITECTURE.md first.

- Preserve live execution gates and credential-bound certification; never equate mocks with exchange acceptance.
- Preserve v2 accounts, positions, history generations, strategy archives, vault and server-side Codex login during upgrades.
- Do not raw-open/close an existing SQLite database file: POSIX descriptor closure can release process locks. Use Store's exclusive connection checkout and SQLite APIs.
- Keep trading and display ordering separate. Account edits must include the expected revision.
- New loss-addition and trailing controls are per pair and disabled by default; test linear/inverse and long/short paths when changing execution.
- Run pytest, Ruff, JS syntax checks, responsive browser checks and read-only production health checks appropriate to the change.
- Keep secrets and backups outside Git. Publish reviewed source to main; deploy a full commit, never a moving ref.

仍需逐交易所測試網與實盤驗收；尚未提供完整歷史回測、交易所精確保證金／撮合模型、多使用者權限或高可用叢集。公開證據缺失時保留缺失，不以 AI 編造數值。

- Account capital policy is independent of signal profiles and strategy optimization. Never reintroduce hidden fallbacks for disabled limits. Risk AI changes only explicitly bounded fields; current protection continues on review failure.
