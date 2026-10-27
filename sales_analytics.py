"""Pure analytics helpers for CardBot stock and voucher health reports."""
from __future__ import annotations

from datetime import datetime
import math
import time


PLATFORMS = ("funpay", "playerok")
LOW_STOCK_THRESHOLD = 10
CRITICAL_STOCK_THRESHOLD = 5
STALE_CACHE_SECONDS = 6 * 60 * 60


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_timestamp(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def summarize_sales(vouchers, stock_sets, cache_updated_at, now=None):
    """Summarize successful trade offers and current code pools without inventing revenue.

    The existing used flag is set after Steam returns a trade-offer ID. It means
    an offer was sent, not that the buyer has accepted it. Historical vouchers
    without used_at remain in all-time totals but are excluded from today's
    metrics so the report does not fabricate timestamps.
    """
    current_ts = float(time.time() if now is None else now)
    today_start = datetime.fromtimestamp(current_ts).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()

    def empty_stats():
        return {
            "generated_codes": 0,
            "generated_today": 0,
            "unused_codes": 0,
            "unused_1_set": 0,
            "unused_5_sets": 0,
            "offers_sent": 0,
            "sets_sent": 0,
            "offers_sent_today": 0,
            "sets_sent_today": 0,
        }

    by_platform = {platform: empty_stats() for platform in PLATFORMS}
    by_platform["other"] = empty_stats()

    if not isinstance(vouchers, dict):
        vouchers = {}

    for voucher in vouchers.values():
        if not isinstance(voucher, dict):
            continue

        platform = str(voucher.get("platform", "funpay") or "funpay").lower()
        if platform not in by_platform:
            platform = "other"
        stats = by_platform[platform]
        sets_count = _safe_int(voucher.get("sets"), 0)
        created_at = _safe_timestamp(voucher.get("created_at"))
        used_at = _safe_timestamp(voucher.get("used_at"))
        is_used = voucher.get("used") is True

        stats["generated_codes"] += 1
        if created_at is not None and today_start <= created_at <= current_ts:
            stats["generated_today"] += 1

        if not is_used:
            stats["unused_codes"] += 1
            if sets_count == 1:
                stats["unused_1_set"] += 1
            elif sets_count == 5:
                stats["unused_5_sets"] += 1
            continue

        stats["offers_sent"] += 1
        stats["sets_sent"] += max(0, sets_count)
        if used_at is not None and today_start <= used_at <= current_ts:
            stats["offers_sent_today"] += 1
            stats["sets_sent_today"] += max(0, sets_count)

    stock_sets = max(0, _safe_int(stock_sets, 0))
    cache_ts = _safe_timestamp(cache_updated_at)
    cache_age_seconds = (
        max(0, int(current_ts - cache_ts)) if cache_ts is not None else None
    )

    if stock_sets < CRITICAL_STOCK_THRESHOLD:
        stock_state = "critical"
    elif stock_sets < LOW_STOCK_THRESHOLD:
        stock_state = "low"
    else:
        stock_state = "ok"

    return {
        "by_platform": by_platform,
        "stock_sets": stock_sets,
        "stock_state": stock_state,
        "cache_age_seconds": cache_age_seconds,
        "cache_stale": (
            cache_age_seconds is None or cache_age_seconds > STALE_CACHE_SECONDS
        ),
        "low_stock_threshold": LOW_STOCK_THRESHOLD,
        "critical_stock_threshold": CRITICAL_STOCK_THRESHOLD,
        "stale_cache_seconds": STALE_CACHE_SECONDS,
    }
