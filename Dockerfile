FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data/groups /app/data/logs/private_chats

COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

EXPOSE 10000 5000

ENV WEB_PORT=5000

CMD ["/bin/bash", "start.sh"]
