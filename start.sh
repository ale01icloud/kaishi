#!/bin/bash
# 启动脚本 - 同时运行Telegram Bot和Web应用

echo "🚀 启动Telegram财务Bot和Web查账系统..."
echo "📋 环境变量检查："
echo "   PORT=${PORT:-未设置}"
echo "   WEB_PORT=${WEB_PORT:-未设置}"
echo "   TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:+已设置}"
echo "   OWNER_ID=${OWNER_ID:-未设置}"

# 在后台启动Web应用
echo ""
echo "🌐 启动Web查账系统..."
python web_app.py 2>&1 | sed 's/^/[WEB] /' &
WEB_PID=$!
echo "   - Web应用 PID: $WEB_PID"

# 等待3秒确保Web应用启动
sleep 3

# 启动Telegram Bot（后台运行）
echo ""
echo "🤖 启动Telegram Bot..."
python bot.py 2>&1 | sed 's/^/[BOT] /' &
BOT_PID=$!
echo "   - Bot PID: $BOT_PID"

echo ""
echo "✅ 两个服务已启动"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📊 Web查账系统: http://0.0.0.0:${PORT:-5000}"
echo "🤖 Telegram Bot: 运行中"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 健康检查函数
check_web_health() {
    if ! kill -0 $WEB_PID 2>/dev/null; then
        echo "❌ Web应用进程已退出，尝试重启..."
        python web_app.py 2>&1 | sed 's/^/[WEB] /' &
        WEB_PID=$!
        echo "   - 新的Web应用 PID: $WEB_PID"
    fi
}

check_bot_health() {
    if ! kill -0 $BOT_PID 2>/dev/null; then
        echo "⚠️ Bot进程已退出，尝试重启..."
        python bot.py 2>&1 | sed 's/^/[BOT] /' &
        BOT_PID=$!
        echo "   - 新的Bot PID: $BOT_PID"
    fi
}

# 无限循环保持容器运行
echo ""
echo "🔄 进入监控循环（每30秒检查一次）..."
while true; do
    sleep 30
    check_web_health
    check_bot_health
done
