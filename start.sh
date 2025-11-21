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

echo "当前目录文件列表："
ls -al
echo "--------------------------------------"

# 直接跑 bot.py（里面已经有 init_bot 和 run_polling）
python bot.py
