#!/bin/bash
# Buddy2api — macOS 本地启动脚本
#
# 与仓库自带的 start.sh 的区别：
#   - 复用本机已安装好的项目 .venv，不会因为 conda 存在而另建 buddy2api 环境重新下载依赖
#   - 默认收窄到 macOS 上真正可用的 workbuddy 通道
#
# 用法：
#   ./run-macos.sh                 # 默认 workbuddy 通道，端口 8787
#   ./run-macos.sh --port 8788     # 换端口
#   CB_GATEWAY_PROVIDERS=workbuddy,traework ./run-macos.sh   # 自行覆盖通道
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    echo "[错误] 未找到 .venv。请先安装依赖："
    echo "  python3 -m venv .venv"
    echo "  .venv/bin/python -m pip install -r requirements.txt"
    exit 1
fi

# QClaw / 千问办公用 Windows DPAPI 解密本机登录文件，macOS 读不了；
# TraeWork 的默认登录目录是 Windows 路径。默认只开 workbuddy，避免管理页出现无用通道。
export CB_GATEWAY_PROVIDERS="${CB_GATEWAY_PROVIDERS:-workbuddy}"

echo "  通道: $CB_GATEWAY_PROVIDERS"
echo "  管理页: http://127.0.0.1:8787"
echo "  停止: Ctrl+C"
echo

exec .venv/bin/python server.py "$@"
