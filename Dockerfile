# 使用Python 3.11作为基础镜像
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制所有项目文件
COPY . .

# 创建数据目录
RUN mkdir -p /app/data/groups /app/data/logs/private_chats

# 复制启动脚本
COPY start.sh .
RUN chmod +x start.sh

# 暴露端口
EXPOSE 10000 5000

# 设置环境变量（WEB_PORT供本地开发使用，ClawCloud会覆盖PORT）
ENV WEB_PORT=5000

# 运行启动脚本（同时启动Bot和Web应用）
CMD ["./start.sh"]
