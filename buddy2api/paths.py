"""项目根目录的唯一来源。

代码收进 `buddy2api/` 包后，`Path(__file__).parent` 指向的是包目录而不是项目根，
数据库、`web/`、`debug_rejects/`、`.debug/` 都会落到包内部。凡是需要项目根的模块
都从这里取，避免每处各写一次 `parents[N]`。
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
