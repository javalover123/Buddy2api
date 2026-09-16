"""全局测试隔离。

任何测试都不应该写到仓库里的 `codebuddy_gateway.db`。此前 `tests/test_core.py` 里
`_collect_non_stream_upstream` 这类辅助函数既没有屏蔽 `proxy._log_request`、也没有隔离
DB 路径，于是每跑一次测试就往真实库的 `logs` 表写一条 `account_name='test-account'`、
`duration_ms` 约等于 epoch 毫秒（因为传了 `t0=0`）的脏记录，污染管理页的请求日志与统计。

这里统一把 `db.DB_PATH` 指向每个测试自己的临时目录，并初始化表结构；测试文件里已有的
`isolated_db` fixture 用的是同一个 `tmp_path`，因此不会冲突。
"""

import pytest

import credential_crypto
import database as db


@pytest.fixture(autouse=True)
def _isolate_gateway_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "gateway.db")
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield
    credential_crypto.reset_cache()
