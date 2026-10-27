# Playerok Order Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Execute the tasks in order and verify each change with the tests listed.

**Goal:** Add a safe Playerok deal monitor and Telegram dashboard while preserving the existing Steam delivery path, FunPay isolation, and Playerok voucher pool.

**Architecture:** A pure `playerok_orders.py` module normalizes event statuses, maps listing IDs, deduplicates deals, and writes a local JSON ledger. An optional `playerok_monitor.py` adapter consumes Playerok events in a daemon thread and passes redacted event summaries into the existing synchronous TeleBot process. A Telegram dashboard reads the ledger. No marketplace write action is enabled.

**Tech Stack:** Python 3.11+ project, pyTelegramBotAPI, optional PyPlayerokAPI 1.1.5, PowerShell helper for Windows user environment setup, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-28-playerok-monitor-design.md`

## Global Constraints
- FunPay's existing auto-delivery protection remains unchanged.
- Playerok monitoring is read-only; do not create/delete listings, send buyer messages, update deal status, or send Steam offers from Playerok events.
- `PLAYEROK_TOKEN` must never be committed or sent to Telegram/logs.
- Raw trade URLs and message text must never be persisted.
- Map 1-set and 5-set listings by exact IDs only.
- Auto-delivery is OFF by default and remains outside this release.

## Review Focus
- Duplicate `ITEM_PAID` + `NEW_DEAL` notifications for the same deal must create one record.
- Stale `PAID` events must not regress a `SENT` or `CONFIRMED` record.
- An unknown listing ID must go to manual review rather than guessing set quantity.
- Multiple active deals in one chat must make trade-link matching ambiguous rather than choosing the newest.
- Missing optional dependency, missing token, or rejected authorization must not crash the main Telegram bot or log credentials.

---

### Task 1: Pure Playerok order ledger

**Files**
- Create: `playerok_orders.py`
- Test: `tests/test_playerok_orders.py`

**Interfaces**
- `normalize_platform_status(status) -> str`
- `map_item_to_sets(item_id, one_set_item_id, five_set_item_id) -> int | None`
- `upsert_deal(orders, *, deal_id, status, item_id=None, item_name=None, chat_id=None, sets=None, event_type="") -> (record, created, changed)`
- `active_orders_for_chat(orders, chat_id) -> list[dict]`
- `mark_trade_url_received(orders, *, chat_id) -> (record | None, result)`
- `summarize_orders(orders) -> dict`
- Safe atomic JSON load/save helpers must not persist raw URLs or credentials.

- [x] Write tests first and observe expected missing-module failure.
- [ ] Implement the pure helpers to pass tests.
- [ ] Run full `pytest -q` and Python compile checks.

### Task 2: Optional read-only Playerok event adapter

**Files**
- Create: `playerok_monitor.py`
- Create: `requirements-playerok.txt`
- Create: `configure_playerok.bat`

**Behavior**
- No import-time SDK dependency; when the optional package is absent, report `missing_dependency`.
- Read credentials only from local environment variables.
- Authenticate read-only, subscribe to paid-deal, new-message, confirmed, rolled-back, and problem events.
- Pass only deal IDs, item IDs/names, chat IDs, status enums, and a boolean indicating a trade URL was seen.
- Do not send marketplace messages or change transaction state.

- [ ] Add adapter tests using a fake event interface without network access.
- [ ] Confirm that missing token/dependency is a graceful disabled state.

### Task 3: Telegram panel and runtime wiring

**Files**
- Modify: `bot.py`
- Modify: `run_bot.ps1`
- Modify: `backup_bot.ps1`
- Modify: `update_bot.ps1`
- Modify: `.gitignore`

**Behavior**
- Add a read-only Playerok panel button and admin-only summary / latest deals.
- Start the monitor only when `PLAYEROK_TOKEN` is configured.
- Show mapping readiness for 1-set and 5-set listing IDs.
- Route trade-link events only to safe metadata; ambiguous/unmatched events go to review.
- Never change the Playerok listing or deal state.

- [ ] Test status formatting and admin gating through pure helper tests or isolated function tests.

### Task 4: Installer and documentation

**Files**
- Create: `requirements-playerok.txt`
- Create: `configure_playerok.bat`
- Modify: `README.md`
- Modify: `.github/workflows/python-checks.yml`

- [ ] Installer uses a hidden PowerShell prompt for the token, stores it only in the current Windows user's environment, and makes no Git or network API changes beyond installing the pinned optional dependency.
- [ ] Add all new runtime files to update and backup lists without changing core `requirements.txt`.
- [ ] Add compile check for new Python modules and Windows syntax checks for the setup helper scripts.
- [ ] Validate on GitHub Actions before opening the PR.
