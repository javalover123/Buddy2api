"""质量统计口径：`finish_reason='retry'` 的中间尝试不计入 errors。

回归（2026-09-18）：管理页「今日质量」显示 errors≈392、成功率 87%，但其中 386 行
是 HTTP 429 + finish_reason='retry' —— 那是网关换账号重试时留下的**中间尝试**记录
（见 proxy.py 的重试循环），后续尝试多数已成功返回给客户端。实测 391 行 retry 中
387 行随后成功，用户侧真实失败仅 4 次。

若把 retry 行计入 errors，成功率会严重低估。本测试钉死：
retry 行既不进 errors，也不进 success，但**必须仍留在 logs 表里**（排障要用）。
"""

import buddy2api.database as db


def _add(finish_reason, status_code, **kw):
    db.add_log({
        "model": "deepseek-v4.1-flash",
        "finish_reason": finish_reason,
        "status_code": status_code,
        **kw,
    })


def test_retry_rows_excluded_from_error_count():
    """retry 行（429/401/502）不得计入 errors。"""
    # 三次失败尝试 + 最终成功，模拟一次真实的重试序列
    _add("retry", 429)
    _add("retry", 429)
    _add("retry", 401)
    _add("stop", 200)

    stats = db.get_stats()
    assert stats["error_requests"] == 0, "retry 行不是最终失败，不该计入 errors"
    assert stats["today"]["errors"] == 0
    assert stats["success_requests"] == 1
    assert stats["today"]["success"] == 1


def test_real_errors_still_counted():
    """真正的最终失败必须照常计入 —— 修口径不能把真错误也吞掉。"""
    _add("error", 400)
    _add("error", 502)
    _add("network_error", 502)
    _add("stop", 200)

    stats = db.get_stats()
    assert stats["error_requests"] == 3
    assert stats["today"]["errors"] == 3


def test_retry_rows_remain_in_logs():
    """retry 行必须保留在 logs 表里供排障，只是不计入质量口径。"""
    _add("retry", 429)
    _add("stop", 200)

    page = db.list_logs(50)
    reasons = [r["finish_reason"] for r in page]
    assert "retry" in reasons, "retry 行不该被删除或过滤掉"
    assert len(page) == 2


def test_success_rate_reflects_client_experience():
    """成功率应按最终结果算，不能被中间重试拉低。"""
    for _ in range(9):
        _add("retry", 429)
    _add("stop", 200)

    stats = db.get_stats()
    assert stats["success_requests"] == 1
    assert stats["error_requests"] == 0
    assert stats["success_rate"] == 100.0, "9 次重试后成功，用户视角成功率应为 100%"


def test_request_count_excludes_retry():
    """分母（requests）也必须排除 retry —— 否则成功率同样被稀释。"""
    _add("retry", 429)
    _add("stop", 200)

    stats = db.get_stats()
    assert stats["total_requests"] == 1, "retry 是过程不是请求，不该进分母"
    assert stats["today"]["requests"] == 1

