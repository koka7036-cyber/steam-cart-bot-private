"""Optional read-only Playerok event monitor.

The upstream SDK is unofficial. This module imports it lazily and never performs
marketplace writes. In particular, it does not send messages, update deal states,
create/delete listings, or execute Steam trades.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from typing import Any, Callable

from playerok_orders import map_item_to_sets, normalize_platform_status


_TRADE_URL_RE = re.compile(
    r"https?://steamcommunity\.com/tradeoffer/new/\?[^\s<>]+",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"\b(?:token|access_token|session|cookie|jwt)\s*[=:]\s*[^\s&<>]+",
    re.IGNORECASE,
)
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)


def _get(obj: Any, *names: str) -> Any:
    for name in names:
        try:
            value = getattr(obj, name, None)
        except Exception:
            value = None
        if value is not None:
            return value
    return None


def _safe_label(value: Any, limit: int = 140) -> str | None:
    if value is None:
        return None
    result = str(value)
    # Listing names are untrusted metadata; do not pass embedded URLs/tokens
    # into the event callback or persist them in the local order ledger.
    result = _URL_RE.sub("[URL скрыта]", result)
    result = _SECRET_RE.sub("[секрет скрыт]", result)
    result = " ".join(result.split()).strip()
    return result[:limit] or None


class PlayerokMonitor:
    """Runs an opt-in, read-only event listener on its own asyncio thread."""

    def __init__(
        self,
        *,
        token: str | None,
        user_agent: str | None = None,
        one_set_item_id: str | None = None,
        five_set_item_id: str | None = None,
        on_event: Callable[[dict], None] | None = None,
        on_status: Callable[[dict], None] | None = None,
    ) -> None:
        self._token = (token or "").strip()
        self._user_agent = (user_agent or _DEFAULT_USER_AGENT).strip()
        self.one_set_item_id = (one_set_item_id or "").strip()
        self.five_set_item_id = (five_set_item_id or "").strip()
        self._on_event = on_event or (lambda _event: None)
        self._on_status = on_status or (lambda _status: None)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._state = {
            "state": "not_configured" if not self._token else "stopped",
            "username": None,
            "last_event_at": None,
            "last_sync_at": None,
            "last_sync_count": 0,
            "last_sync_error": None,
            "last_error": None,
        }

    def status(self) -> dict:
        with self._lock:
            state = dict(self._state)
        state["configured"] = bool(self._token)
        state["one_set_mapping_ready"] = bool(self.one_set_item_id)
        state["five_set_mapping_ready"] = bool(self.five_set_item_id)
        state["mappings_distinct"] = bool(
            self.one_set_item_id
            and self.five_set_item_id
            and self.one_set_item_id != self.five_set_item_id
        )
        state["auto_delivery"] = False
        return state

    def _set_status(self, state: str, *, username: str | None = None, error: str | None = None) -> None:
        with self._lock:
            previous = dict(self._state)
            self._state["state"] = state
            if username is not None:
                self._state["username"] = _safe_label(username, 80)
            self._state["last_error"] = error
            current = dict(self._state)
        if previous != current:
            try:
                self._on_status(current)
            except Exception:
                logging.exception("Playerok status callback failed")

    def start(self) -> bool:
        if not self._token:
            self._set_status("not_configured")
            return False
        with self._lock:
            if self._thread and self._thread.is_alive():
                return True
            self._thread = threading.Thread(
                target=self._thread_main,
                name="PlayerokReadOnlyMonitor",
                daemon=True,
            )
            self._thread.start()
            return True

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:
            # Do not log the SDK exception text; it can contain request headers
            # or session data. The exception type is enough for initial diagnosis.
            self._set_status("error", error=type(exc).__name__)
            logging.error("Playerok monitor stopped (%s)", type(exc).__name__)

    async def _sync_open_deals(self, account: Any) -> tuple[int, str | None]:
        """Reconcile existing paid/pending seller deals without marketplace writes."""
        try:
            from PyPlayerokAPI.types.enums import ItemDealDirections, ItemDealStatuses
        except ImportError:
            return 0, "DealStatusTypesUnavailable"

        seen_ids = set()
        scanned = 0
        cursor = None
        try:
            # The SDK limits each page to 24. Cap reconciliation to five pages so
            # a very old account cannot trigger a large scan during bot startup.
            for _ in range(5):
                page = await account.get_deals(
                    count=24,
                    statuses=[ItemDealStatuses.PAID, ItemDealStatuses.PENDING],
                    direction=ItemDealDirections.OUT,
                    after_cursor=cursor,
                )
                deals = _get(page, "deals") or []
                for deal in deals:
                    deal_id = _get(deal, "id")
                    if not deal_id or str(deal_id) in seen_ids:
                        continue
                    seen_ids.add(str(deal_id))
                    item = _get(deal, "item")
                    chat = _get(deal, "chat")
                    item_id = _get(item, "id")
                    item_name = _safe_label(_get(item, "name") or _get(item, "title"))
                    chat_id = _get(chat, "id")
                    payload = {
                        "event_type": "INITIAL_SYNC",
                        "deal_id": str(deal_id),
                        "status": normalize_platform_status(_get(deal, "status")),
                        "item_id": str(item_id) if item_id else None,
                        "item_name": item_name,
                        "chat_id": str(chat_id) if chat_id else None,
                        "sets": map_item_to_sets(item_id, self.one_set_item_id, self.five_set_item_id),
                        "has_trade_url": False,
                        "is_initial_sync": True,
                        "seen_at": int(time.time()),
                    }
                    # The callback stores only safe fields and deduplicates by deal ID.
                    await asyncio.to_thread(self._on_event, payload)
                    scanned += 1

                page_info = _get(page, "page_info", "pageInfo")
                if not _get(page_info, "has_next_page", "hasNextPage"):
                    break
                next_cursor = _get(page_info, "end_cursor", "endCursor")
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
            return scanned, None
        except Exception as exc:
            # Keep streaming events even if the initial reconciliation endpoint fails.
            return scanned, type(exc).__name__

    async def _run(self) -> None:
        try:
            from PyPlayerokAPI.account import AccountClient
            from PyPlayerokAPI.stream import PlayerokAccountListener
        except ImportError:
            self._set_status("missing_dependency", error="OptionalSdkNotInstalled")
            return

        self._set_status("connecting")
        account = AccountClient(token=self._token, user_agent=self._user_agent)
        profile = await account.me
        username = _get(profile, "username")
        account_id = _get(profile, "id")
        if not username:
            raise RuntimeError("Playerok account profile unavailable")

        try:
            sync_count, sync_error = await self._sync_open_deals(account)
        except Exception as exc:
            sync_count, sync_error = 0, type(exc).__name__
        with self._lock:
            self._state["last_sync_at"] = int(time.time())
            self._state["last_sync_count"] = sync_count
            self._state["last_sync_error"] = sync_error

        listener = PlayerokAccountListener(account)
        # SDK 1.1.5 exposes its router as _router (some releases/documentation
        # expose router publicly). Prefer public API if present, then fall back.
        router = getattr(listener, "router", None) or getattr(listener, "_router", None)
        if router is None:
            raise RuntimeError("Playerok listener has no supported event router")

        async def publish(event_type: str, event: Any, event_account: Any = None) -> None:
            deal = _get(event, "deal")
            chat = _get(event, "chat")
            message = _get(event, "message")
            if deal is None and message is not None:
                deal = _get(message, "deal")

            sender = _get(message, "user")
            sender_id = _get(sender, "id")
            if event_type == "NEW_MESSAGE" and account_id and sender_id and str(sender_id) == str(account_id):
                return

            # Only a boolean leaves this adapter; raw message text and the URL/token
            # are never passed to callbacks or written to logs.
            message_text = _get(message, "text") or ""
            has_trade_url = bool(_TRADE_URL_RE.search(str(message_text)))
            deal_id = _get(deal, "id")
            if not deal_id and event_type != "NEW_MESSAGE":
                return
            item = _get(deal, "item")
            raw_status = _get(deal, "status")
            status = normalize_platform_status(raw_status)
            if status == "unknown":
                status = normalize_platform_status(event_type)
            item_id = _get(item, "id") or _get(deal, "item_id")
            item_name = _safe_label(_get(item, "name") or _get(item, "title"))
            chat_id = _get(chat, "id") or _get(_get(deal, "chat"), "id") or _get(_get(message, "chat"), "id")
            sets = map_item_to_sets(item_id, self.one_set_item_id, self.five_set_item_id)
            payload = {
                "event_type": event_type,
                "deal_id": str(deal_id) if deal_id else None,
                "status": status,
                "item_id": str(item_id) if item_id else None,
                "item_name": item_name,
                "chat_id": str(chat_id) if chat_id else None,
                "sets": sets,
                "has_trade_url": has_trade_url,
                "seen_at": int(time.time()),
            }
            with self._lock:
                self._state["last_event_at"] = payload["seen_at"]
            await asyncio.to_thread(self._on_event, payload)

        event_routes = (
            ("on_item_paid", "ITEM_PAID"),
            ("on_new_deal", "NEW_DEAL"),
            ("on_new_message", "NEW_MESSAGE"),
            ("on_item_sent", "ITEM_SENT"),
            ("on_deal_confirmed", "DEAL_CONFIRMED"),
            ("on_deal_confirmed_automatically", "DEAL_CONFIRMED_AUTOMATICALLY"),
            ("on_deal_rolled_back", "DEAL_ROLLED_BACK"),
            ("on_deal_has_problem", "DEAL_HAS_PROBLEM"),
            ("on_deal_problem_resolved", "DEAL_PROBLEM_RESOLVED"),
            ("on_deal_status_changed", "DEAL_STATUS_CHANGED"),
        )
        installed = 0
        for method_name, event_type in event_routes:
            register = getattr(router, method_name, None)
            if not callable(register):
                continue

            def decorate(callback, route_name=event_type):
                return register()(callback)

            async def handler(event_account, event, route_name=event_type):
                await publish(route_name, event, event_account)

            decorate(handler)
            installed += 1

        if installed == 0:
            raise RuntimeError("Playerok listener exposes no supported event routes")

        # The SDK's start() is async and must be awaited inside this loop.
        await listener.start()
        self._set_status("listening", username=str(username))
        # The SDK owns reconnects and websocket lifecycle. Keep the event loop alive.
        while True:
            await asyncio.sleep(30)


def monitor_enabled_from_environment() -> bool:
    return bool(os.getenv("PLAYEROK_TOKEN", "").strip())
