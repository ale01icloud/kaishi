# 使用 Python 3.11 作为基础镜像
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 复制依赖文件
COPY requirements.txt /app/

# 安装依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制完整项目（包含 kaishi 内的 bot.py 和 app.py）
COPY . /app/

# 创建数据目录（避免运行时报错）
RUN mkdir -p /app/data/groups /app/data/logs/private_chats

# 复制启动脚本
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# 暴露端口（Polling + Healthcheck）
EXPOSE 5000
EXPOSE 10000

# Polling 模式默认端口
ENV WEB_PORT=5000

# 默认启动（执行 Polling 机器人 + 健康检查）
CMD ["/bin/bash", "start.sh"]

