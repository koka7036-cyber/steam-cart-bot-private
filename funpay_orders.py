"""Small, dependency-free order ledger helpers for the FunPay watcher.

The ledger deliberately stores no Steam trade URL, passwords, or session data.
It records only the minimum data required to match payment notifications and
keep one reservation per order.
"""
from __future__ import annotations

import hashlib
import re
import time


OPEN_STATUSES = {"awaiting_input", "input_received", "reserved", "review"}



STEAM_TRADE_URL_RE = re.compile(
    r"https?://steamcommunity\.com/tradeoffer/new/\?[^\s<>\"']+",
    re.IGNORECASE,
)
def extract_steam_trade_url(text: str) -> str | None:
    """Extract one Steam trade URL from plain text; reject ambiguous or malformed input."""
    matches = STEAM_TRADE_URL_RE.findall(text or "")
    normalized = []
    for candidate in matches:
        candidate = candidate.rstrip(".,;:!?)]}")
        parsed = __import__("urllib.parse", fromlist=["urlparse", "parse_qs"])
        url = parsed.urlparse(candidate)
        query = parsed.parse_qs(url.query)
        if (
            url.scheme in {"http", "https"}
            and url.netloc.lower() == "steamcommunity.com"
            and url.path.rstrip("/") == "/tradeoffer/new"
            and query.get("partner", [""])[0].isdigit()
            and query.get("token", [""])[0]
        ):
            normalized.append(candidate)
    # Exactly one valid URL is required. Multiple links are ambiguous.
    unique = list(dict.fromkeys(normalized))
    return unique[0] if len(unique) == 1 else None

def extract_order_number(text: str) -> str | None:
    """Extract the visible FunPay order number when it is present in a message."""
    match = re.search(r"(?:заказ|order)\s*[№#]?\s*([A-Z0-9-]{4,})", text or "", re.IGNORECASE)
    return match.group(1) if match else None


def payment_key(node_id: str, text: str) -> str:
    """Return a stable fallback key for payment messages without an order number."""
    order_no = extract_order_number(text)
    if order_no:
        return f"fp-order-{order_no}"
    normalized = " ".join((text or "").lower().split())
    digest = hashlib.sha256(f"{node_id}|{normalized}".encode("utf-8")).hexdigest()[:16]
    return f"fp-chat-{node_id}-{digest}"



def message_signature(sender_name: str, preview: str) -> str:
    """Create a stable signature without storing the message preview itself."""
    source = f"{sender_name or ''}:{preview or ''}".encode("utf-8", errors="replace")
    return hashlib.sha256(source).hexdigest()

def register_payment(orders: dict, *, node_id: str, buyer: str, text: str,
                     sets_per_item: int, quantity: int) -> tuple[dict, bool]:
    """Create exactly one record for a payment notification.

    Returns ``(record, created)``. A repeated FunPay notification reuses the
    existing record and can never create a second inventory reservation.
    """
    key = payment_key(node_id, text)
    existing = orders.get(key)
    if isinstance(existing, dict):
        return existing, False

    now = int(time.time())
    record = {
        "key": key,
        "funpay_order_number": extract_order_number(text),
        "node_id": str(node_id),
        "buyer": buyer,
        "sets_per_item": int(sets_per_item),
        "quantity": int(quantity),
        "sets_total": int(sets_per_item) * int(quantity),
        "status": "awaiting_input",
        "reservation": None,
        "input_kind": None,
        "input_received_at": None,
        "updated_at": now,
        "created_at": now,
        "notes": [],
    }
    orders[key] = record
    return record, True


def find_open_order_for_chat(orders: dict, node_id: str) -> dict | None:
    """Return an order only when exactly one candidate exists in this chat.

    If multiple open orders exist, an incoming file/link cannot be reliably
    assigned to one of them from the chat id alone. Returning ``None`` forces
    manual review instead of silently attaching the input to the wrong order.
    """
    candidates = [
        item for item in orders.values()
        if isinstance(item, dict)
        and str(item.get("node_id")) == str(node_id)
        and item.get("status") in OPEN_STATUSES
    ]
    return candidates[0] if len(candidates) == 1 else None


def mark_input_received(record: dict, input_kind: str) -> None:
    """Mark that input arrived without storing its sensitive contents."""
    # Repeated events must not overwrite the first input or move an order
    # backwards from review/delivered/cancelled to input_received.
    if record.get("status") not in {"awaiting_input", "reserved"}:
        return
    record["input_kind"] = input_kind
    record["input_received_at"] = int(time.time())
    record["updated_at"] = int(time.time())
    record["status"] = "input_received"
