"""Tests for the stock and sales analytics helpers."""
from datetime import datetime
from sales_analytics import summarize_sales


def ts(hour=12, minute=0):
    today = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return today.timestamp()


def test_counts_unused_code_pools_per_platform():
    summary = summarize_sales(
        {
            "a": {"platform": "funpay", "sets": 1, "used": False},
            "b": {"platform": "funpay", "sets": 5, "used": False},
            "c": {"platform": "playerok", "sets": 5, "used": False},
        },
        stock_sets=30,
        cache_updated_at=ts(),
        now=ts(12, 30),
    )
    assert summary["by_platform"]["funpay"]["unused_codes"] == 2
    assert summary["by_platform"]["funpay"]["unused_1_set"] == 1
    assert summary["by_platform"]["funpay"]["unused_5_sets"] == 1
    assert summary["by_platform"]["playerok"]["unused_5_sets"] == 1


def test_used_vouchers_count_as_offers_sent_not_revenue():
    summary = summarize_sales(
        {
            "old": {"platform": "funpay", "sets": 5, "used": True},
            "one": {"platform": "playerok", "sets": 1, "used": True},
        },
        stock_sets=12,
        cache_updated_at=ts(),
        now=ts(12, 30),
    )
    assert summary["by_platform"]["funpay"]["offers_sent"] == 1
    assert summary["by_platform"]["funpay"]["sets_sent"] == 5
    assert summary["by_platform"]["playerok"]["offers_sent"] == 1
    assert summary["by_platform"]["playerok"]["sets_sent"] == 1


def test_today_metrics_include_only_records_with_new_timestamps():
    now = ts(12, 30)
    summary = summarize_sales(
        {
            "legacy": {"platform": "funpay", "sets": 5, "used": True},
            "today": {
                "platform": "funpay",
                "sets": 1,
                "used": True,
                "created_at": ts(10),
                "used_at": ts(11),
            },
            "unused": {
                "platform": "funpay",
                "sets": 5,
                "used": False,
                "created_at": ts(9),
            },
        },
        stock_sets=15,
        cache_updated_at=ts(12),
        now=now,
    )
    stats = summary["by_platform"]["funpay"]
    assert stats["offers_sent"] == 2
    assert stats["sets_sent"] == 6
    assert stats["generated_today"] == 2
    assert stats["offers_sent_today"] == 1
    assert stats["sets_sent_today"] == 1


def test_stock_health_thresholds():
    now = ts(12, 30)
    fresh = ts(12)
    assert summarize_sales({}, 30, fresh, now)["stock_state"] == "ok"
    assert summarize_sales({}, 9, fresh, now)["stock_state"] == "low"
    assert summarize_sales({}, 4, fresh, now)["stock_state"] == "critical"


def test_cache_is_stale_when_missing_or_older_than_six_hours():
    now = ts(12, 30)
    assert summarize_sales({}, 30, None, now)["cache_stale"] is True
    assert summarize_sales({}, 30, now - 7 * 60 * 60, now)["cache_stale"] is True
    assert summarize_sales({}, 30, now - 60, now)["cache_stale"] is False


def test_bad_records_and_unknown_platform_do_not_crash():
    summary = summarize_sales(
        {
            "bad": None,
            "unknown": {"platform": "other-platform", "sets": "bad", "used": True},
        },
        stock_sets="bad",
        cache_updated_at="bad",
        now=ts(12, 30),
    )
    assert summary["stock_sets"] == 0
    assert summary["cache_stale"] is True
    assert summary["by_platform"]["other"]["offers_sent"] == 1
    assert summary["by_platform"]["other"]["sets_sent"] == 0
