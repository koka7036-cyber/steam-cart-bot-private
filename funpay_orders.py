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
    match = re.search(r"(?:заказ|order)\s*[№#]?\s*(\d{4,})", text or "", re.IGNORECASE)
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

# --- Safe FunPay chat/file parsing helpers ---
from html.parser import HTMLParser as _HTMLParser
from urllib.parse import urljoin as _urljoin, urlparse as _urlparse, unquote as _urlunquote


class _FunPayChatMessageParser(_HTMLParser):
    """Extract text and links from message containers without adding dependencies."""

    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.messages = []
        self.current = None
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        tag = tag.lower()
        if self.current is None:
            if tag == "div" and "chat-msg-item" in classes:
                match = re.search(r"message-(\d+)", attrs.get("id", ""))
                self.current = {
                    "id": match.group(1) if match else None,
                    "text_parts": [],
                    "body_parts": [],
                    "links": [],
                }
                self.stack = [(tag, classes)]
            return

        if tag not in self._VOID_TAGS:
            self.stack.append((tag, classes))
        if tag == "a" and attrs.get("href"):
            self.current["links"].append({
                "href": attrs.get("href", ""),
                "class": attrs.get("class", ""),
                "download": attrs.get("download", ""),
                "title": attrs.get("title", ""),
            })
        if tag == "br":
            self.current["text_parts"].append("\n")
            if any("chat-msg-text" in item_classes for _, item_classes in self.stack):
                self.current["body_parts"].append("\n")

    def handle_data(self, data):
        if self.current is None:
            return
        self.current["text_parts"].append(data)
        if any("chat-msg-text" in item_classes for _, item_classes in self.stack):
            self.current["body_parts"].append(data)

    def handle_endtag(self, tag):
        if self.current is None:
            return
        tag = tag.lower()
        match_index = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                match_index = index
                break
        if match_index is None:
            return
        closes_message = match_index == 0 and "chat-msg-item" in self.stack[0][1]
        del self.stack[match_index:]
        if closes_message:
            body_text = "".join(self.current.pop("body_parts")).strip()
            all_text = "".join(self.current.pop("text_parts")).strip()
            self.current["text"] = body_text or all_text
            self.messages.append(self.current)
            self.current = None
            self.stack = []


def extract_funpay_chat_messages(markup: str) -> list[dict]:
    """Return message text, message ID and links for each rendered chat message."""
    parser = _FunPayChatMessageParser()
    try:
        parser.feed(markup or "")
        parser.close()
    except Exception:
        return []
    return parser.messages


def _host_is_allowed_file_source(host: str, trusted_hosts=()) -> bool:
    host = (host or "").lower().rstrip(".")
    if host == "funpay.com" or host.endswith(".funpay.com"):
        return True
    for trusted in trusted_hosts or ():
        allowed = str(trusted).lower().strip().rstrip(".")
        if allowed and (host == allowed or host.endswith("." + allowed)):
            return True
    return False


def extract_text_file_urls_from_links(links, base_url="https://funpay.com/", trusted_hosts=()) -> list[str]:
    """Find direct .txt attachment links; reject unsafe schemes and unknown non-text files."""
    found = []
    for link in links or ():
        if not isinstance(link, dict):
            continue
        href = str(link.get("href") or "").strip()
        if not href:
            continue
        candidate = _urljoin(base_url, href)
        parsed = _urlparse(candidate)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            continue
        path_name = _urlunquote(parsed.path).lower()
        download_name = str(link.get("download") or link.get("title") or "").lower()
        direct_text_file = path_name.endswith(".txt")
        named_text_attachment = download_name.endswith(".txt")
        if not direct_text_file and not named_text_attachment:
            continue
        if not _host_is_allowed_file_source(parsed.hostname, trusted_hosts) and not direct_text_file:
            continue
        if parsed.port not in (None, 443):
            continue
        candidate = parsed._replace(fragment="").geturl()
        if candidate not in found:
            found.append(candidate)
    return found


def extract_text_file_urls_from_text(text: str, trusted_hosts=()) -> list[str]:
    """Find explicit HTTPS .txt URLs in message text, not arbitrary web links."""
    found = []
    for match in re.findall(r"https://[^\s<>\"']+", text or "", re.IGNORECASE):
        candidate = match.rstrip(".,;:!?)]}")
        parsed = _urlparse(candidate)
        if parsed.scheme != "https" or not parsed.hostname or not _urlunquote(parsed.path).lower().endswith(".txt"):
            continue
        if parsed.username or parsed.password or parsed.port not in (None, 443):
            continue
        if candidate not in found:
            found.append(candidate)
    return found


def decode_text_file_trade_url(data: bytes, filename: str = "", content_type: str = "", max_bytes: int = 65536):
    """Decode a small text file and return exactly one valid Steam trade URL.

    Returns (url, None) on success or (None, reason_code) on failure.
    The payload is never saved to disk or logged.
    """
    if not isinstance(data, (bytes, bytearray)):
        return None, "not_bytes"
    if len(data) > max_bytes:
        return None, "file_too_large"
    filename = str(filename or "").lower().strip()
    content_type = str(content_type or "").split(";", 1)[0].lower().strip()
    if not filename.endswith(".txt") and content_type not in {"text/plain", "text/x-plain", "application/octet-stream"}:
        return None, "not_plain_text"
    if b"\x00" in data[:4096]:
        return None, "binary_file"

    decoded = None
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            decoded = bytes(data).decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        return None, "encoding_unsupported"

    raw_candidates = []
    for candidate in STEAM_TRADE_URL_RE.findall(decoded):
        candidate = candidate.rstrip(".,;:!?)]}")
        if candidate not in raw_candidates:
            raw_candidates.append(candidate)
    if not raw_candidates:
        return None, "trade_url_missing"
    if len(raw_candidates) != 1:
        return None, "trade_url_ambiguous"

    url = extract_steam_trade_url(decoded)
    if not url:
        return None, "trade_url_invalid"
    return url, None
