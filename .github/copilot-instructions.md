# Copilot repository instructions — Steam Cart Bot Private

## Project
Private working Telegram/FunPay Steam card bot with Playerok monitoring, Steam trade delivery, Windows runtime scripts, backups, and local runtime state.

## Non-negotiable safety
- Never commit or expose TG tokens, Steam credentials, session data, FunPay configuration, Playerok session tokens, trade links, vouchers, inventory caches, logs, backups, or other runtime secrets.
- Treat `C:\CardBot` and local runtime files as production data.
- Preserve the existing backup/update workflow and its fast-forward safety.
- Preserve duplicate-instance protection and duplicate-delivery protection.
- Never guess an order state or trade-offer state. Ambiguous states must go to manual review.
- Do not change the production workflow merely to make a test pass.
- Do not add external contacts, bypasses, proxy tricks, or challenge circumvention.

## Validation
- Inspect tests and relevant operational documentation before edits.
- Prefer targeted tests, then the full suite.
- Verify changes do not alter runtime-data handling or secret boundaries.
- Review the final diff carefully.

## Git/PR safety
- Branch first; PR only.
- Never merge to `main`.
- Do not modify production/runtime data.
- Do not change workflows or update mechanisms without explicit task scope.
