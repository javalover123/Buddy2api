"""`python -m buddy2api` 入口。

原先直接跑 `python server.py`；模块收进包之后用包入口，`server.main()` 本身
不变（参数、环境变量、端口行为都保持原样）。
"""

from buddy2api.server import main

if __name__ == "__main__":
    main()
