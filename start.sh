#!/bin/bash

echo "======================================"
echo "🚀 启动 Telegram 财务 Bot（Polling 模式）"
echo "📦 工作目录: $(pwd)"
echo "======================================"

echo "环境变量："
echo "  PORT=${PORT}"
echo "  TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}"
echo "  OWNER_ID=${OWNER_ID}"
echo "--------------------------------------"

# 关键：这里不要再跑 app.py 了，直接跑 JSON 版的 bot.py
python bot.py
