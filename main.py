import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BASE_WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # ClawCloud domain
PORT = int(os.getenv("PORT", "5000"))        # ClawCloud exposes port 5000

WEBHOOK_PATH = f"/{TOKEN}"
WEBHOOK_URL = BASE_WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Webhook 已成功运行在 ClawCloud ✅")


async def main():
    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))

    # --- Setup webhook on Telegram ---
    await app.bot.set_webhook(
        url=WEBHOOK_URL,
        drop_pending_updates=True
    )

    print("Webhook 设置成功：", WEBHOOK_URL)

    # --- Start webhook server (inside container) ---
    await app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN,
    )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
