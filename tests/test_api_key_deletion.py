"""删 API key 不得留下会卡死启动的孤儿数据。

回归（2026-09-17）：删掉一把已产生请求日志的 key 后重启，`_migrate_daily_usage`
把 logs 里今天的记录聚合进 `api_key_daily_usage`（对 api_keys 有外键），撞上
孤儿 `api_key_id` 直接 IntegrityError —— init_db 在启动路径上，整个服务起不来
（launchd 无限重启循环，模型路由全断）。

两层修复，两层都要钉住：

1. `delete_api_key` 删 key 前先摘掉 logs 里的引用（logs.api_key_id 无外键，是
   唯一能产生孤儿的地方）；
2. `_migrate_daily_usage` 只聚合仍存在的 key —— 启动路径必须对历史脏数据健壮，
   不能让一行坏数据把服务卡死。
"""

import sqlite3
import time

import pytest

import buddy2api.auth_manager as auth_manager
import buddy2api.control_plane as control_plane
import buddy2api.credential_crypto as credential_crypto
import buddy2api.database as db


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield path
    credential_crypto.reset_cache()


def _make_key(name: str = "k") -> int:
    return db.add_api_key("sk-cb-test-" + name, name)


def _now_ts() -> int:
    return int(time.time())


def _log_row(api_key_id, created_at):
    return {
        "api_key_id": api_key_id,
        "api_key_name": "k",
        "account_id": None,
        "model": "deepseek-v4.1-flash",
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
        "credit": 0.0,
        "status_code": 200,
        "created_at": created_at,
    }


def test_delete_api_key_leaves_no_orphan_logs(isolated_db):
    """删 key 后 logs 里不得残留指向它的 api_key_id。"""
    kid = _make_key()
    db.add_log(_log_row(kid, _now_ts()))
    assert _orphan_count(isolated_db, kid) == 1

    db.delete_api_key(kid)

    assert _orphan_count(isolated_db, kid) == 0, "删 key 必须同时摘掉 logs 引用"
    # 日志原文保留，只是去掉归属
    assert _log_total(isolated_db) == 1


def test_migrate_daily_usage_skips_orphaned_keys(isolated_db):
    """历史孤儿行不得让启动迁移崩溃，且不影响正常 key 的聚合。"""
    kid = _make_key()
    db.add_log(_log_row(kid, _now_ts()))
    # 模拟历史遗留：直接塞一条指向不存在 key 的日志（绕过 delete_api_key）
    conn = sqlite3.connect(isolated_db)
    conn.execute(
        "INSERT INTO logs (api_key_id, model, created_at) VALUES (99999, 'm', ?)",
        (_now_ts(),),
    )
    conn.commit()
    conn.close()

    # 修复前这里抛 IntegrityError，服务卡死在启动。
    # 迁移只执行不提交（init_db 外层统一 commit），测试里要自己提交。
    conn2 = sqlite3.connect(isolated_db)
    db._migrate_daily_usage(conn2)
    conn2.commit()
    conn2.close()

    conn = sqlite3.connect(isolated_db)
    rows = dict(conn.execute("SELECT api_key_id, request_count FROM api_key_daily_usage"))
    conn.close()
    assert rows.get(kid) == 1, "正常 key 的聚合不受影响"
    assert 99999 not in rows, "孤儿 key 不得进入 daily_usage"


def test_migrate_daily_usage_still_aggregates_valid_keys(isolated_db):
    kid = _make_key()
    db.add_log(_log_row(kid, _now_ts()))
    db.add_log(_log_row(kid, _now_ts()))

    conn2 = sqlite3.connect(isolated_db)
    db._migrate_daily_usage(conn2)
    conn2.commit()
    conn2.close()

    conn = sqlite3.connect(isolated_db)
    rows = dict(conn.execute("SELECT api_key_id, request_count FROM api_key_daily_usage"))
    conn.close()
    assert rows.get(kid) == 2


def _orphan_count(path, kid) -> int:
    conn = sqlite3.connect(path)
    n = conn.execute("SELECT COUNT(*) FROM logs WHERE api_key_id=?", (kid,)).fetchone()[0]
    conn.close()
    return n


def _log_total(path) -> int:
    conn = sqlite3.connect(path)
    n = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    conn.close()
    return n


def test_import_update_preserves_account_extra(isolated_db, tmp_path, monkeypatch):
    """启动自动导入更新已有账号时，extra 必须合并而不是整体覆盖。

    否则账号上的 route_exclude 等附属标记会在每次重启时被静默抹掉
    （_TOKEN_FIELDS 含 extra，而解析结果只带本次的 auth_path）。
    """
    info = tmp_path / "a.info"
    info.write_text(
        '{"account":{"uid":"u-extra","nickname":"n"},"auth":{"accessToken":"tok","refreshToken":"ref"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        auth_manager, "parse_auth_file",
        lambda path: {
            "name": "n", "uid": "u-extra", "nickname": "n", "phone": "",
            "access_token": "tok", "refresh_token": "ref",
            "expires_at": 4_000_000_000_000, "domain": "www.codebuddy.cn",
        },
    )
    monkeypatch.setattr(control_plane, "_allowed_roots_workbuddy", lambda auth_dir: [tmp_path])
    monkeypatch.setattr(control_plane, "lookup_preview", lambda token, channel: {"hashes": set()})

    # 先造一个带标记的已有账号（同 uid）
    aid = db.add_account({
        "name": "n", "uid": "u-extra", "provider": "workbuddy",
        "access_token": "old", "domain": "www.codebuddy.cn", "status": "active",
        "extra": {"route_exclude": True, "auth_path": "/old/path"},
    })

    preview = {"hashes": {control_plane.path_hash(str(info.resolve()))}}
    result = control_plane.import_workbuddy([info], preview, str(tmp_path))
    assert result["updated"] == 1

    fresh = db.get_account(aid)
    assert fresh["extra"]["auth_path"] != "/old/path", "auth_path 应更新为本次路径"
    assert fresh["extra"]["route_exclude"] is True, "已有标记不得被导入覆盖"
