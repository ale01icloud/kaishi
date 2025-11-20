#!/bin/sh
set -e

echo "======================================"
echo "🚀 启动 Telegram 财务 Bot（Polling 模式）"
echo "📦 工作目录: $(pwd)"
echo "======================================"
echo "环境变量："
echo "  PORT=${PORT}"
echo "  TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}"
echo "  OWNER_ID=${OWNER_ID}"
echo "--------------------------------------"

# 直接运行 bot.py（里面已经启动 HTTP 健康检查 + polling）
python bot.py
