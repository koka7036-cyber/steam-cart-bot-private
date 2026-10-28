"""Pure parsing helpers for FunPay order messages; no network or bot imports."""
import html
import re


def is_paid_order_notification(order_text):
    """Return True only for explicit payment-success wording, never order creation alone."""
    text = html.unescape(order_text or "").lower().replace("ё", "е")
    negative_payment = re.search(
        r"\bне\s+(?:оплатил|оплачен|оплачена|оплачено|оплачены)\b|"
        r"\bоплата\s+не\s+(?:прошла|получена|подтверждена)\b",
        text,
    )
    if negative_payment:
        return False

    paid_markers = (
        r"\b(?:покупатель\s+)?оплатил(?:\s+заказ)?\b",
        r"\bзаказ\s+(?:успешно\s+)?оплачен(?:а|о|ы)?\b",
        r"\bоплата\s+(?:успешно\s+)?(?:прошла|получена|подтверждена|успешна)\b",
        r"\b(?:успешная|успешную)\s+оплата\b",
        r"\bплат[её]ж\s+(?:получен|успешен|подтвержден)\b",
        r"\bпереведите\s+(?:товар|покупателю)\b",
    )
    return any(re.search(pattern, text) for pattern in paid_markers)


def detect_order_sets_and_quantity(order_text):
    """Return (sets_per_item, quantity) for supported FunPay card-set listings."""
    text = html.unescape(order_text or "").lower().replace("ё", "е")
    five_set_patterns = (
        r"5\s*(?:полных\s*)?(?:сет(?:а|ов|ы)?|набор(?:а|ов|ы)?)",
        r"(?:пять)\s*(?:полных\s*)?(?:сет(?:а|ов|ы)?|набор(?:а|ов|ы)?)",
        r"25\s*карт",
        r"500\s*xp",
    )
    one_set_patterns = (
        r"1\s*(?:полный\s*)?(?:сет|набор)",
        r"(?:один|одна)\s*(?:полный\s*)?(?:сет|набор)",
        r"5\s*шт\s*=\s*100\s*xp",
        r"100\s*xp",
    )
    if any(re.search(pattern, text) for pattern in five_set_patterns):
        sets_per_item = 5
    elif any(re.search(pattern, text) for pattern in one_set_patterns):
        sets_per_item = 1
    else:
        sets_per_item = None

    quantity = 1
    quantity_patterns = (
        r"(?:количество|кол-во|кол\.?|quantity|экземпляр(?:ов|а)?)\s*[:xх=]?\s*(\d{1,3})",
        r"(?:x|х)\s*(\d{1,3})\b",
        r"\b(\d{1,3})\s*(?:шт(?:ук)?|экземпляр(?:а|ов)?)\b",
    )
    for pattern in quantity_patterns:
        match = re.search(pattern, text)
        if match:
            quantity = max(1, min(100, int(match.group(1))))
            break
    return sets_per_item, quantity
