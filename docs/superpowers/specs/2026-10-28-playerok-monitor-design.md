# Playerok order monitoring integration — design

## Intent
Add a separate Playerok adapter to the existing Windows-hosted Telegram bot so the owner receives reliable notifications when Playerok sales and deal statuses change, can see recent deals from Telegram, and can review orders without relying on manually generated codes alone.

## Current behavior
The bot currently creates Playerok voucher codes in a pool isolated by the `platform=playerok` field. The buyer instructions mention the Telegram bot and/or sending a trade link in the marketplace chat, but the bot itself does not authenticate to Playerok or listen for actual Playerok paid-deal events. FunPay has a separate order ledger and must remain isolated.

## Chosen approach
Use an optional adapter over the unofficial `PyPlayerokAPI` event library. It is not a Playerok-official API. The adapter only reads the seller profile, paid-deal events, chat events, and deal status changes. It never creates or deletes listings, sends messages to buyers, changes deal status, or sends Steam offers. The base bot continues to start normally if the optional library or credentials are absent.

A fully native undocumented GraphQL client was rejected because it would couple this project directly to unstable private query schemas. Immediate auto-delivery was rejected for this iteration because the account integration is not yet live-tested and Playerok's seller channel warns that third-party scripts receiving profile data carry risk. An official seller API/approval is preferred if available.

## Inputs and configuration
- `PLAYEROK_TOKEN`: optional session token, stored only as a Windows user environment variable, never in code, Telegram messages, repository files, or logs.
- `PLAYEROK_USER_AGENT`: optional browser user-agent associated with the account's current session.
- `PLAYEROK_ITEM_1_SET_ID` and `PLAYEROK_ITEM_5_SET_ID`: exact Playerok listing IDs, used for deterministic 1-set/5-set mapping. Unknown or colliding IDs must go to manual review.
- Optional monitoring package is kept in `requirements-playerok.txt`, not the core `requirements.txt`, so the existing bot does not suddenly acquire third-party API dependencies.

## Order ledger
- Use Playerok deal ID as the unique primary key and deduplicate repeated events.
- Persist only deal ID, listing ID/name, chat ID, safe status, mapped set count, timestamps, and booleans describing whether a trade link was seen.
- Never persist a trade URL, token, message contents, or cookies.
- Use explicit listing-ID mapping; never infer quantity from an item title.
- Only associate a chat event with an order if exactly one open order exists in that chat. Ambiguous/unmatched cases are surfaced for manual review.
- Keep marketplace status separate from fulfillment status; don't treat `PAID` as delivered or `SENT` as buyer-confirmed.
- Treat confirmed, rolled-back, and problem states distinctly.

## Telegram UI
Add a Playerok dashboard button and read-only commands showing connection state, account identity (username only), item mappings, monitor last-event time, auto-delivery status (always OFF in this release), open orders, ambiguous messages, and the most recent deals. Admin notifications should avoid raw cookies, trade URLs, and message bodies.

## Safety / non-goals
- This release does not automate listing creation/deletion or deal-status mutation.
- Auto-delivery stays OFF; no Steam trade offer is sent from a Playerok event.
- No Playerok session token is sent to Telegram or stored in GitHub.
- No retries of side-effecting actions are introduced.
- The monitor may stop working if Playerok changes undocumented endpoints or restricts third-party clients. Do not evade a platform challenge; surface an error and stop.
- Playerok's official seller communications caution that third-party scripts handling account data carry risk; seller should confirm an approved method with support before enabling any write actions.

## Success criteria
1. Without optional credentials/dependency, the current bot still starts and shows Playerok as not configured.
2. With valid local configuration, the monitor authenticates and reports paid/status events to the admin, without changing Playerok state.
3. Repeated events create one ledger row.
4. Unknown listing mapping and chats with multiple open deals require manual review.
5. Tests and Windows PowerShell syntax checks pass.
