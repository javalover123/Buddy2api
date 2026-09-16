"""守住管理页内联脚本的语法。

回归（2026-09-16）：改 `web/index.html` 时在某个 Vue setup 里留了一个没闭合的数组，
内联脚本解析失败 → Vue 从不 mount → 页面整片空白，而 HTTP 响应仍是 200、字节数也正常。
任何"看响应"的检查都抓不到这种问题，必须真的去解析那段脚本。

配套的 `test_web_template_contract.py` 只检查「模板引用的名字是否由 setup 返回」，
它用正则解析，语法错时照样能提取出组件（实测注入语法错仍解析出 7 个组件），所以
两者互补、缺一不可。

CI 上没有 node 时自动 skip（不装 node 也不该让 CI 红）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = Path(__file__).resolve().parents[1] / "web" / "index.html"

_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def _inline_scripts() -> list[str]:
    return _INLINE_SCRIPT.findall(HTML.read_text(encoding="utf-8"))


def test_console_inline_scripts_parse(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available; cannot parse the console script")
    scripts = _inline_scripts()
    assert scripts, f"no inline <script> found in {HTML}"
    for index, code in enumerate(scripts):
        path = tmp_path / f"inline_{index}.js"
        path.write_text(code, encoding="utf-8")
        proc = subprocess.run(
            [node, "--check", str(path)], capture_output=True, text=True
        )
        assert proc.returncode == 0, (
            f"{HTML.name}: inline script #{index} has a syntax error "
            f"(the console would render blank):\n{proc.stderr}"
        )
