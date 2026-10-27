# Design: Telegram sales and stock health report (2026-10-28)

## Intent
Make the current Telegram admin bot easier to operate from a phone and reduce unnoticed stock/cache problems. The user asked for autonomous improvements to the Steam card-set selling bot.

## Approaches considered
1. Build a full web panel with prices and profit accounting. Rejected for this iteration: no trusted price/fee data is stored, it adds hosting and setup, and user does not want PC installation.
2. Add analytics to the existing Telegram admin keyboard. Chosen: no new dependencies or hosting, compatible with current Windows/Telegram setup, and low deployment complexity.
3. Automatically enable FunPay delivery and parse chat attachments. Rejected: the account has an active moderation warning, and the platform rules restrict auto-delivery for products requiring communication or additional services.

## Scope
- Add a pure analytics module and tests.
- Add a Telegram report for full sets in cache, cache freshness, unused one-set/five-set voucher pools per marketplace, and codes marked used after Steam returned a trade-offer ID.
- Add timestamps to newly created voucher records and mark timestamp only after the existing successful-offer path.
- Delay acknowledgement of unread FunPay chats until detail retrieval and handling succeed; retry on temporary fetch errors.
- Store hashed message signatures instead of raw preview text and redact trade URLs from administrative notifications.
- Check deployment PowerShell scripts for parse errors on a Windows GitHub Actions runner.
- Warn on fewer than 10 full sets, critical below 5, and cache age greater than 6 hours.
- Include the module in local backups, updater deployment and CI compilation.

## Non-goals and safety constraints
- Do not calculate revenue/profit without price, commission, and refund data.
- Do not imply a trade offer was accepted merely because Steam returned an offer ID.
- Do not store or report trade URLs, golden_key, passwords, session cookies, or voucher contents in GitHub.
- Do not enable FunPay automatic delivery or change Playerok delivery behavior.