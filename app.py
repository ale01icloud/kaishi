# app.py
import os
from bot import init_bot

if __name__ == "__main__":
    print("=" * 50)
    print("🚀 正在启动财务记账机器人 (JSON 本地文件 + Polling 模式)")
    print("=" * 50)

    # 给 Render / UptimeRobot 用的健康检查端口
    if "PORT" not in os.environ:
        os.environ["PORT"] = "10000"

    # 直接调用 bot.py 里的启动函数
    init_bot()
