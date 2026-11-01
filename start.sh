#!/bin/sh
# AI 用量卡片 — macOS / Linux 启动脚本(等价于 Windows 上的 启动卡片.bat)
#
#   ./start.sh              前台运行(终端里能看到报错,适合排查)
#   ./start.sh --detach     后台运行,关掉终端也不影响(双击 启动卡片.command 用的就是它)
#   ./start.sh --check      不开界面,只拉一次数据并打印诊断信息
#
# 会自动挑一个"能直接跑"的 Python:≥ 3.9、带 tkinter(Tk ≥ 8.6)、已装 requests。
# 想指定解释器:QUOTA_CARD_PYTHON=/path/to/python3 ./start.sh
cd "$(dirname "$0")" || exit 1

DETACH=0
case "$1" in
    -d|--detach) DETACH=1; shift ;;
esac

usable() {  # $1 = 候选解释器
    [ -n "$1" ] && [ -x "$1" ] || return 1
    "$1" -c 'import sys, tkinter, requests; sys.exit(0 if sys.version_info >= (3, 9) and tkinter.TkVersion >= 8.6 else 1)' >/dev/null 2>&1
}
has_tk() {
    [ -n "$1" ] && [ -x "$1" ] || return 1
    "$1" -c 'import tkinter' >/dev/null 2>&1
}

IS_MAC=0
[ "$(uname -s)" = "Darwin" ] && IS_MAC=1

# 候选列表(按优先级,一行一个)。
# macOS:/usr/bin/python3(Xcode 命令行工具自带)放最后——它的 Tk 很旧、界面异常,
# 而且在没装命令行工具的机器上一探测就会弹出安装窗口。
candidates() {
    [ -n "$QUOTA_CARD_PYTHON" ] && echo "$QUOTA_CARD_PYTHON"
    for name in python3 python; do
        p="$(command -v "$name" 2>/dev/null)" || continue
        [ "$IS_MAC" = 1 ] && [ "$p" = "/usr/bin/python3" ] && continue
        echo "$p"
    done
    if [ "$IS_MAC" = 1 ]; then
        for v in 3.14 3.13 3.12 3.11 3.10 3.9; do
            echo "/Library/Frameworks/Python.framework/Versions/$v/bin/python3"   # python.org 官方安装包
        done
        for v in 3.14 3.13 3.12 3.11 3.10; do
            echo "/opt/homebrew/bin/python$v"; echo "/usr/local/bin/python$v"     # Homebrew(Apple 芯片 / Intel)
        done
        echo "/opt/homebrew/bin/python3"; echo "/usr/local/bin/python3"
        xcode-select -p >/dev/null 2>&1 && echo "/usr/bin/python3"   # 仅在已装命令行工具时才探测
    fi
}

PY=""
FALLBACK=""
for c in $(candidates | awk '!seen[$0]++'); do
    if usable "$c"; then PY="$c"; break; fi
    [ -z "$FALLBACK" ] && has_tk "$c" && FALLBACK="$c"
done
# QUOTA_CARD_PYTHON 路径里含空格时上面的 for 会拆开,单独再试一次
if [ -z "$PY" ] && usable "$QUOTA_CARD_PYTHON"; then PY="$QUOTA_CARD_PYTHON"; fi

if [ -z "$PY" ]; then
    if [ -n "$FALLBACK" ]; then
        echo "找到了带 tkinter 的 Python($FALLBACK),但缺少依赖。请先安装:" >&2
        echo "    \"$FALLBACK\" -m pip install -r requirements.txt" >&2
    else
        echo "没有找到带 tkinter 的 Python 3.9+。" >&2
        if [ "$IS_MAC" = 1 ]; then
            echo "macOS 推荐二选一:" >&2
            echo "  · 从 https://www.python.org/downloads/macos/ 安装 Python 3.12+(自带 Tk)" >&2
            echo "  · 或 Homebrew:brew install python-tk@3.12" >&2
        else
            echo "Linux:安装发行版的 python3-tk 包(如 sudo apt install python3-tk)。" >&2
        fi
        echo "然后:python3 -m pip install -r requirements.txt" >&2
    fi
    exit 1
fi

if [ "$DETACH" = 1 ]; then
    nohup "$PY" quota_card.py "$@" >/dev/null 2>&1 &
    exit 0
fi
exec "$PY" quota_card.py "$@"
