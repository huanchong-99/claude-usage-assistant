#!/bin/sh
# macOS:在访达里双击即可启动 AI 用量卡片(后台运行,弹出的终端窗口可以直接关掉)。
# 首次若提示"无法打开",在终端执行一次:chmod +x 启动卡片.command start.sh
cd "$(dirname "$0")" || exit 1
sh ./start.sh --detach || { echo; echo "按回车键关闭…"; read -r _; }
