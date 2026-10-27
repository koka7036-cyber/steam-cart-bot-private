# Market notes: Steam trading-card set delivery (2026-10-28)

## Findings
- SteamTradeX's public Steam level-up bot documents badge-aware set selection, trade-offer checks, pricing, profit tracking, Telegram alerts, dashboard controls, and update rollback as differentiators. It is an adjacent competitor rather than a direct marketplace listing comparison.
- The current CardBot already has Telegram controls, an inventory cache, separated FunPay/Playerok voucher pools, code-level redemption state, an order ledger for FunPay, and guarded updates. The lowest-risk competitive gap to close first is visibility: stock age/low-stock state and code/offer counts, before implementing price-based decisions.
- A future profit dashboard needs marketplace price, fee, refund, and sale-confirmation records. None of these values should be guessed from code redemption alone.

## Policy constraint
- FunPay's seller rules state that Auto Delivery must not be used for goods requiring communication or additional services (rule 2.1.8), and also restrict off-platform contacts and unnecessary file-hosting links. Keep FunPay auto-delivery disabled unless the platform explicitly confirms the permitted transfer workflow for this product and account.
- Steam documents that trading and market access can be blocked by Steam Guard, trade holds, account restrictions, or other conditions. The bot should report offer-created vs offer-accepted states distinctly.

## Sources
- FunPay rules: https://funpay.com/trade/info
- Steam trading restrictions: https://help.steampowered.com/en/faqs/view/451E-96B3-D194-50FC
- SteamTradeX public repository: https://github.com/SteamTradeX/steam-levelup-bot
- Steam Trading Cards FAQ: https://steamcommunity.com/tradingcards/faq