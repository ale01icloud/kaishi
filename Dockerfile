FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 先复制并安装依赖
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 再把项目所有代码复制进来（包含 bot.py、start.sh、app.py 等）
COPY . /app

# 创建数据目录
RUN mkdir -p /app/data/groups /app/data/logs/private_chats

# 确保启动脚本有执行权限
RUN chmod +x /app/start.sh

# 暴露端口（健康检查 + 备用）
EXPOSE 10000 5000

# ClawCloud 会覆盖 PORT，这里只是默认值
ENV WEB_PORT=5000

# 用绝对路径执行启动脚本，更保险
CMD ["/bin/bash", "/app/start.sh"]
