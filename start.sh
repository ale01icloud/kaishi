#!/bin/bash
set -e

echo "======================================"
echo "🚀 启动 Telegram 财务 Bot（Polling 模式）"
echo "📦 工作目录: $(pwd)"
echo "======================================"

# 打印一下环境变量方便排错
echo "环境变量："
echo "  PORT=${PORT}"
echo "  TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}"
echo "  OWNER_ID=${OWNER_ID}"
echo "--------------------------------------"

# 直接用 app.py 启动（里面会调用 bot.init_bot）
python app.py

while true; do
    sleep 30
    check_app_health
done
