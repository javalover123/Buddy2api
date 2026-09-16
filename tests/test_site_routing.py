"""混装国内/国际账号时的站点判定与模型路由。

背景（实测，2026-09）：国内站与国际站暴露的模型集几乎不重叠 ——
国际站 18 个（default-model / gpt-5.6-sol / gemini-3.5-flash / …），
国内站 29 个（auto / deepseek-v4.1-flash / glm-4.6 / kimi-k2.5 / …），
交集只有 hy3、hy4-preview、glm-5.3 三个。

而模型目录是按「通道」存的，混装账号时目录必然是并集，单看目录无法判断某个账号
能不能服务某个模型。于是此前会这样失败：

  1. 选号完全不看模型，粘性又只按 provider 分一个槽 —— 一旦粘住某个账号，所有
     请求都压在它身上（「只会请求一个账号」）。
  2. 请求只属于另一个站的模型时，上游回 HTTP 400（code 11102：
     `model [...] service info not found` 或 `model [...] is only available for
     authorized users`），而 400 不在换号重试的集合里，客户端直接看到报错。

本文件把修复后的契约钉住：站点判定只有一处、选号按模型过滤、粘性按模型分槽且会
在明显跑偏时让位、模型不可用的 400 视为可换号错误。
"""

import asyncio

import pytest

import auth_manager
import catalog
import database as db
import fingerprint
import proxy
import site_preference
import sites


@pytest.fixture(autouse=True)
def _clear_route_state():
    """路由与能力缓存都是模块级状态，测试之间必须清干净。"""
    def _reset():
        auth_manager._sticky_account_id.clear()
        auth_manager._account_failures.clear()
        auth_manager._auth_failures.clear()
        auth_manager._verify_inflight.clear()
        auth_manager._account_models.clear()
        auth_manager._account_denied.clear()

    _reset()
    yield
    _reset()


def _make_account(uid: str, domain: str, provider: str = "workbuddy", status: str = "active") -> int:
    return db.add_account(
        {
            "name": uid,
            "uid": uid,
            "provider": provider,
            "access_token": f"token-{uid}",
            "refresh_token": f"refresh-{uid}",
            "domain": domain,
            "status": status,
            "expires_at": 4_000_000_000_000,
        }
    )


def _set_requests(aid: int, total: int) -> None:
    """total_requests 不在 update_account 的白名单里，直接改库。"""
    conn = db.get_conn()
    conn.execute("UPDATE accounts SET total_requests = ? WHERE id = ?", (total, aid))
    conn.commit()
    conn.close()


def _bump_requests(aid: int) -> None:
    conn = db.get_conn()
    conn.execute("UPDATE accounts SET total_requests = total_requests + 1 WHERE id = ?", (aid,))
    conn.commit()
    conn.close()


# ============================================================
# 站点判定：只有一处定义
# ============================================================

def test_origin_matches_upstream_site_for_cn_accounts():
    """国内账号：请求发到哪个站，Origin/Referer 就必须自称哪个站。

    回归点：以前 fingerprint.origin_for 按域名里有没有 "workbuddy" 判定，
    于是 www.workbuddy.cn 的请求发往国内站、请求头却自称 www.workbuddy.ai。
    """
    for domain in ("www.workbuddy.cn", "www.codebuddy.cn"):
        assert auth_manager.backend_url_for({"domain": domain}) == f"https://{domain}"
        assert fingerprint.origin_for(domain) == f"https://{domain}"


def test_origin_for_non_cn_accounts_keeps_global_default():
    """国际账号不受影响：上游回退全局 backend_url，Origin 仍是国际站。

    copilot.tencent.com 是国际版的后端，Origin 用产品域名 workbuddy.ai 是既定行为，
    这里只钉住「不要被国内链路改动带跑」。
    """
    for domain in ("www.workbuddy.ai", "www.codebuddy.ai", ""):
        assert auth_manager.backend_url_for({"domain": domain}) == auth_manager.backend_url()
        assert fingerprint.origin_for(domain) == "https://www.workbuddy.ai"


@pytest.mark.parametrize(
    "domain",
    ["www.workbuddy.cn", "www.codebuddy.cn", "workbuddy.cn", "foo.bar.cn", "WWW.CodeBuddy.CN/"],
)
def test_cn_domain_detection_accepts_real_domains(domain):
    assert sites.is_cn_domain(domain) is True
    assert sites.site_url(domain) == f"https://{sites.normalize_domain(domain)}"


@pytest.mark.parametrize(
    "domain",
    ["foocn.com", "www.workbuddy.cn.evil.com", "cn", "www.workbuddy.ai", "", None],
)
def test_cn_domain_detection_rejects_lookalikes(domain):
    assert sites.is_cn_domain(domain) is False
    assert sites.site_url(domain) is None


def test_cn_domain_detection_ignores_port():
    """带端口也要能识别，否则会静默回退到全局上游。"""
    assert sites.is_cn_domain("www.workbuddy.cn:443") is True
    assert sites.site_url("www.workbuddy.cn:443") == "https://www.workbuddy.cn:443"


# ============================================================
# 选号按模型过滤
# ============================================================

def _mixed_pool():
    """两个国际账号 + 两个国内账号，模型能力互不重叠（真实情况就是如此）。"""
    intl_a = _make_account("intl-a", "www.workbuddy.ai")
    intl_b = _make_account("intl-b", "www.workbuddy.ai")
    cn_a = _make_account("cn-a", "www.workbuddy.cn")
    cn_b = _make_account("cn-b", "www.codebuddy.cn")
    auth_manager.record_account_models(intl_a, ["gpt-5.6-sol", "hy3"])
    auth_manager.record_account_models(intl_b, ["gpt-5.6-sol", "hy3"])
    auth_manager.record_account_models(cn_a, ["auto", "deepseek-v4.1-flash", "hy3"])
    auth_manager.record_account_models(cn_b, ["auto", "deepseek-v4.1-flash", "hy3"])
    return intl_a, intl_b, cn_a, cn_b


def test_pick_account_only_returns_accounts_that_can_serve_the_model():
    intl_a, intl_b, cn_a, cn_b = _mixed_pool()

    for _ in range(5):
        assert auth_manager.pick_account(model="gpt-5.6-sol")["id"] in {intl_a, intl_b}
    for _ in range(5):
        assert auth_manager.pick_account(model="auto")["id"] in {cn_a, cn_b}
    # 两边都有的模型则都可以用
    for _ in range(5):
        assert auth_manager.pick_account(model="hy3")["id"] in {intl_a, intl_b, cn_a, cn_b}


def test_pick_account_without_model_considers_everyone():
    """没带模型信息时保持旧行为，不因能力过滤把账号排除掉。"""
    intl_a, intl_b, cn_a, cn_b = _mixed_pool()
    assert auth_manager.pick_account()["id"] in {intl_a, intl_b, cn_a, cn_b}


def test_unknown_capability_is_optimistic():
    """能力未知时先让它试（由上游 400 自愈），不要一上来就排除。"""
    aid = _make_account("fresh", "www.workbuddy.cn")
    assert auth_manager.account_supports_model(aid, "gpt-5.6-sol") is True


def test_denied_model_is_skipped_even_without_supplier_list():
    a = _make_account("a", "www.workbuddy.ai")
    b = _make_account("b", "www.workbuddy.ai")
    auth_manager.mark_model_denied(a, "m")
    assert auth_manager.account_supports_model(a, "m") is False
    assert auth_manager.pick_account(model="m")["id"] == b


def test_recording_supplier_models_prunes_stale_denials():
    aid = _make_account("a", "www.workbuddy.ai")
    auth_manager.mark_model_denied(aid, "gone")
    auth_manager.record_account_models(aid, ["kept"])
    # 新列表里没有的模型不可能再被选中，记录顺手清掉
    assert auth_manager.account_supports_model(aid, "gone") is False
    assert auth_manager.account_supports_model(aid, "kept") is True


def test_forget_account_clears_route_state():
    """账号删除后，能力/粘性/失败计数都不该留下残留。"""
    a = _make_account("a", "www.workbuddy.ai")
    b = _make_account("b", "www.workbuddy.ai")
    auth_manager.record_account_models(a, ["m"])
    auth_manager.mark_model_denied(a, "m")
    auth_manager._set_sticky_account(a, "workbuddy", "m")
    auth_manager.mark_account_failure(a, 401)

    db.delete_account(a)
    auth_manager.forget_account(a)

    assert a not in auth_manager._account_models
    assert a not in auth_manager._account_denied
    assert a not in auth_manager._sticky_account_id.values()
    assert auth_manager.auth_failure_count(a) == 0
    assert auth_manager.account_is_cooling_down(a) is False
    # 粘性槽已清空，选号重新开始 → 落到剩下的账号
    assert auth_manager.pick_account(model="m")["id"] == b


def test_capability_filter_falls_back_when_capable_accounts_exhausted():
    """能力过滤只调整优先级，不能让请求彻底无账号可用。

    上游的供应商列表双向不准（实测：国内站的列表里没有 kimi-k3，但它照样 200；
    反过来 glm-4.6 在列表里、所有账号却都回 service info not found）。如果过滤把候选
    清空就返回 None，就会出现「明明有账号，却报 No available accounts」。正确行为是：
    能服务的优先，都试过了就退回其余账号再试。
    """
    intl = _make_account("intl", "www.workbuddy.ai")
    cn = _make_account("cn", "www.workbuddy.cn")
    auth_manager.record_account_models(intl, ["m"])
    auth_manager.record_account_models(cn, ["other"])

    assert auth_manager.pick_account(model="m")["id"] == intl
    # 国际账号已被试过 → 退回国内账号，而不是返回 None
    assert auth_manager.pick_account(exclude_ids={intl}, model="m")["id"] == cn


# ============================================================
# 粘性：按模型分槽，且会为负载让位
# ============================================================

def test_sticky_slots_are_per_model():
    """不同模型各留各的粘性槽 —— 共用一个槽正是「只压一个账号」的来源。"""
    intl_a, intl_b, cn_a, cn_b = _mixed_pool()
    intl = auth_manager.pick_account(model="gpt-5.6-sol")["id"]
    cn = auth_manager.pick_account(model="auto")["id"]
    assert intl in {intl_a, intl_b}
    assert cn in {cn_a, cn_b}
    # 互相顶不掉：再问一次仍然各回各的
    assert auth_manager.pick_account(model="gpt-5.6-sol")["id"] == intl
    assert auth_manager.pick_account(model="auto")["id"] == cn


def test_sticky_holds_while_balanced():
    a = _make_account("a", "www.workbuddy.ai")
    b = _make_account("b", "www.workbuddy.ai")
    auth_manager.record_account_models(a, ["m"])
    auth_manager.record_account_models(b, ["m"])
    first = auth_manager.pick_account(model="m")["id"]
    _bump_requests(first)
    # 只领先 1 个请求，还在容差内，继续粘住（保住 prompt cache）
    assert auth_manager.pick_account(model="m")["id"] == first


def test_sticky_yields_so_load_spreads():
    """粘住的账号明显更累时让位，负载不再长期压在同一个账号上。

    两个账号从同一水位出发：旧逻辑会一直粘住第一个选中的账号，20 次请求全落在它身上；
    修好之后应该在两者之间交替，偏差不超过容差（一个权重单位的两倍）。
    """
    a = _make_account("a", "www.workbuddy.ai")
    b = _make_account("b", "www.workbuddy.ai")
    auth_manager.record_account_models(a, ["m"])
    auth_manager.record_account_models(b, ["m"])
    _set_requests(a, 0)
    _set_requests(b, 0)

    picks = []
    for _ in range(20):
        chosen = auth_manager.pick_account(model="m")["id"]
        picks.append(chosen)
        _bump_requests(chosen)

    assert set(picks) == {a, b}, "两个账号都该被用上"
    assert abs(picks.count(a) - picks.count(b)) <= 4, picks


def test_sticky_yields_to_idle_account_immediately():
    a = _make_account("a", "www.workbuddy.ai")
    b = _make_account("b", "www.workbuddy.ai")
    auth_manager.record_account_models(a, ["m"])
    auth_manager.record_account_models(b, ["m"])
    auth_manager._set_sticky_account(a, "workbuddy", "m")
    _set_requests(a, 500)
    _set_requests(b, 0)
    assert auth_manager.pick_account(model="m")["id"] == b


def test_sticky_slack_is_constant_not_weight_scaled():
    """容差是常量，不按账号权重放大。

    否则高权重账号会被允许无限领先（权重 100 就容差 100 个负载单位），粘性会退化成
    「永远压在权重最大的那个账号上」。
    """
    light = _make_account("light", "www.workbuddy.ai")
    heavy = _make_account("heavy", "www.workbuddy.ai")
    db.update_account(light, {"weight": 100})
    db.update_account(heavy, {"weight": 200})
    auth_manager.record_account_models(light, ["m"])
    auth_manager.record_account_models(heavy, ["m"])
    _set_requests(light, 5000)   # 负载 50.0
    _set_requests(heavy, 4000)   # 负载 20.0
    auth_manager._set_sticky_account(light, "workbuddy", "m")
    # heavy 权重更高、负载更低，应该让位给它
    assert auth_manager.pick_account(model="m")["id"] == heavy


# ============================================================
# 上游「模型不可用」的识别与换号
# ============================================================

@pytest.mark.parametrize(
    "detail",
    [
        {"code": 11102, "msg": "model [default-model] service info not found"},
        {"code": 11102, "msg": "model [gpt-5.6-sol] is only available for authorized users"},
        {"msg": "model [x] service info not found"},
        {"error": {"code": 11102, "message": "model [x] nope"}},
    ],
)
def test_model_unavailable_error_recognizes_upstream_rejections(detail):
    assert proxy._model_unavailable_error(400, detail) is True


@pytest.mark.parametrize(
    "status,detail",
    [
        (401, {"code": 11102, "msg": "model [x]"}),
        (500, {"code": 11102}),
        (400, {"code": 11155, "msg": "reasoning_content is required"}),
        (400, {"code": 11128, "msg": "first message is not system prompt"}),
        (400, b"not a dict"),
        (400, None),
    ],
)
def test_model_unavailable_error_ignores_other_failures(status, detail):
    assert proxy._model_unavailable_error(status, detail) is False


def test_model_blocked_400_switches_account_without_blaming_it(monkeypatch):
    """400「模型不属于这个账号」要换号，但不能把账号记成故障。"""
    blocked = _make_account("blocked", "www.workbuddy.cn")
    healthy = _make_account("healthy", "www.workbuddy.ai")
    _set_requests(blocked, 0)
    _set_requests(healthy, 1)  # 让被拒的那个先被选中

    seen: list[int] = []

    async def fake_headers(account):
        return {"Authorization": "Bearer x"}

    async def fake_collect(url, headers, body, account, api_key_info, model_name, t0):
        seen.append(account["id"])
        if account["id"] == blocked:
            return ("error", (400, {"code": 11102, "msg": "model [m] service info not found"}))
        return ("json", {"id": "ok", "choices": [], "usage": {"total_tokens": 0}})

    async def no_delay(_attempt):
        return None

    monkeypatch.setattr(auth_manager, "get_valid_headers", fake_headers)
    monkeypatch.setattr(proxy, "_collect_stream", fake_collect)
    monkeypatch.setattr(proxy, "_retry_delay", no_delay)

    result = asyncio.run(proxy._json_chat_with_stall_retry({"model": "m"}, None, "m"))

    assert seen == [blocked, healthy]
    assert result[0] == "json"
    # 账号本身没坏：不该进冷却，也不该被判成鉴权失败
    assert auth_manager.account_is_cooling_down(blocked) is False
    assert auth_manager.auth_failure_count(blocked) == 0
    assert db.get_account(blocked)["status"] == "active"
    # 但已经学到「这个账号服务不了这个模型」，下次直接跳过
    assert auth_manager.account_supports_model(blocked, "m") is False


def test_pick_account_skips_account_denied_earlier():
    """被拒过的账号在后续请求里直接不再被选中。"""
    blocked = _make_account("blocked", "www.workbuddy.cn")
    healthy = _make_account("healthy", "www.workbuddy.cn")
    auth_manager.mark_model_denied(blocked, "m")
    for _ in range(5):
        assert auth_manager.pick_account(model="m")["id"] == healthy


# ============================================================
# 模型目录：多账号并集
# ============================================================

def test_catalog_samples_every_account_and_records_capability(monkeypatch):
    """目录取全部账号的并集，并把每个账号的能力写回路由层。"""
    intl = _make_account("intl", "www.workbuddy.ai")
    cn = _make_account("cn", "www.workbuddy.cn")

    async def fake_fetch(account):
        if str(account["domain"]).endswith(".cn"):
            return [{"id": "auto", "name": "Auto"}, {"id": "deepseek-v4.1-flash", "name": "DS"}]
        return [{"id": "default-model", "name": "Auto"}, {"id": "gpt-5.6-sol", "name": "Sol"}]

    monkeypatch.setitem(catalog.LIVE_FETCHERS, "workbuddy", fake_fetch)

    result = asyncio.run(catalog.refresh_one("workbuddy"))
    assert result["mode"] == "live"
    ids = {item["id"] for item in result["models"]}
    assert {"auto", "deepseek-v4.1-flash", "default-model", "gpt-5.6-sol"} <= ids

    assert auth_manager.account_supports_model(intl, "gpt-5.6-sol") is True
    assert auth_manager.account_supports_model(intl, "auto") is False
    assert auth_manager.account_supports_model(cn, "auto") is True
    assert auth_manager.account_supports_model(cn, "gpt-5.6-sol") is False


def test_catalog_ignores_disabled_accounts(monkeypatch):
    """停用的账号不该被采样，否则会把它的模型混进目录。"""
    active = _make_account("active", "www.workbuddy.ai")
    _make_account("off", "www.workbuddy.cn", status="inactive")

    async def fake_fetch(account):
        assert account["id"] == active
        return [{"id": "only-active", "name": "Only"}]

    monkeypatch.setitem(catalog.LIVE_FETCHERS, "workbuddy", fake_fetch)
    result = asyncio.run(catalog.refresh_one("workbuddy"))
    assert "only-active" in {item["id"] for item in result["models"]}


# ============================================================
# 站点分组与站点偏好
# ============================================================

def test_site_group_splits_domestic_from_international():
    """同一模型两边计费不同，所以「这个账号要不要花钱」只能按域名后缀判定。"""
    for domain in ("www.workbuddy.cn", "www.codebuddy.cn", "WWW.WorkBuddy.CN/"):
        assert sites.site_group(domain) == sites.SITE_DOMESTIC
    for domain in ("www.workbuddy.ai", "www.codebuddy.ai", "copilot.tencent.com"):
        assert sites.site_group(domain) == sites.SITE_INTERNATIONAL
    # domain 为空时与 backend_url_for 的回退方向一致（走全局上游 = 国际站）
    assert sites.site_group("") == sites.SITE_INTERNATIONAL
    assert sites.site_group(None) == sites.SITE_INTERNATIONAL


def test_site_preference_is_off_by_default():
    """没配置过就不能改变路由：默认不区分站点，而不是默默偏向某一边。"""
    assert auth_manager.model_site_preference() == {"default": "", "models": {}}
    assert auth_manager.preferred_site_for("deepseek-v4.1-flash") == ""
    assert auth_manager.preferred_site_for(None) == ""


def test_site_preference_falls_back_to_default_for_unlisted_model():
    db.set_setting(
        "model_site_preference",
        {"default": "international", "models": {"deepseek-v4.1-flash": "domestic"}},
    )
    assert auth_manager.preferred_site_for("deepseek-v4.1-flash") == "domestic"
    assert auth_manager.preferred_site_for("gpt-5.6-sol") == "international"


def test_site_preference_survives_broken_setting():
    """配置写坏时用默认值，不能让路由整个挂掉。"""
    db.set_setting("model_site_preference", "not-a-dict")
    assert auth_manager.model_site_preference() == {"default": "", "models": {}}
    db.set_setting("model_site_preference", {"default": "nonsense", "models": {"m": 7}})
    assert auth_manager.model_site_preference() == {"default": "", "models": {"m": ""}}


def test_site_preference_overrides_load_balancing_for_that_model():
    """偏好只调优先级：不花钱那边即使更累，也要先被选中。"""
    intl = _make_account("intl", "www.workbuddy.ai", status="active")
    cn = _make_account("cn", "www.workbuddy.cn", status="active")
    auth_manager.record_account_models(intl, ["m"])
    auth_manager.record_account_models(cn, ["m"])
    _set_requests(intl, 999)   # 国际账号很累，但配置要求优先用它
    _set_requests(cn, 0)
    db.set_setting("model_site_preference", {"default": "", "models": {"m": "international"}})
    assert auth_manager.pick_account(model="m")["id"] == intl


def test_site_preference_falls_back_when_preferred_site_is_exhausted():
    """偏好那边的账号都被试过 → 退回另一边，不能报 No available accounts。"""
    intl = _make_account("intl", "www.workbuddy.ai", status="active")
    cn = _make_account("cn", "www.workbuddy.cn", status="active")
    db.set_setting("model_site_preference", {"default": "international", "models": {}})
    assert auth_manager.pick_account(model="m")["id"] == intl
    assert auth_manager.pick_account(exclude_ids={intl}, model="m")["id"] == cn


def test_site_preference_falls_back_when_preferred_site_has_no_account():
    """偏好站点一个账号都没导入时，照样用另一边的账号。"""
    cn = _make_account("cn", "www.workbuddy.cn", status="active")
    db.set_setting("model_site_preference", {"default": "international", "models": {}})
    assert auth_manager.pick_account(model="m")["id"] == cn


def test_site_preference_ignores_cooling_down_accounts():
    """偏好不是绕过冷却的理由：冷却中的账号不能被粘性重新捡回来。"""
    intl = _make_account("intl", "www.workbuddy.ai", status="active")
    cn = _make_account("cn", "www.workbuddy.cn", status="active")
    db.set_setting("model_site_preference", {"default": "international", "models": {}})
    auth_manager._set_sticky_account(intl, "workbuddy", "m")
    auth_manager.mark_account_failure(intl, 429)
    assert auth_manager.pick_account(model="m")["id"] == cn


def test_site_preference_clean_rejects_bad_values():
    """写入侧是严格的：管理页/API 传错要明确报错，不能静默存成部分配置。"""
    assert site_preference.clean({}) == {"default": "", "models": {}}
    assert site_preference.clean(
        {"default": "domestic", "models": {"m": "international"}}
    ) == {"default": "domestic", "models": {"m": "international"}}

    with pytest.raises(site_preference.SitePreferenceError, match="must be one of"):
        site_preference.clean({"default": "cn"})
    with pytest.raises(site_preference.SitePreferenceError, match="models must be an object"):
        site_preference.clean({"models": ["international"]})
    with pytest.raises(site_preference.SitePreferenceError, match="unsupported keys"):
        site_preference.clean({"sites": {}})
    with pytest.raises(site_preference.SitePreferenceError, match="model id is required"):
        site_preference.clean({"models": {"": "international"}})


def test_reset_request_counts_levels_the_field():
    """归零计数是让粘性/负载重新均衡的唯一途径（total_requests 只增不减）。"""
    a = _make_account("a", "www.workbuddy.ai", status="active")
    b = _make_account("b", "www.workbuddy.cn", status="active")
    _set_requests(a, 1110)
    _set_requests(b, 97)

    assert db.reset_account_request_counts() == 2
    assert db.get_account(a)["total_requests"] == 0
    assert db.get_account(b)["total_requests"] == 0

    # 同一水位之后两个账号都会被用上，而不是一直压在原来最闲的那个上
    picks = []
    for _ in range(10):
        chosen = auth_manager.pick_account(model="m")["id"]
        picks.append(chosen)
        _bump_requests(chosen)
    assert set(picks) == {a, b}, picks
