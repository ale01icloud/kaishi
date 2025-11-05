# Python Development Environment

## Overview
A simple Python environment for manual code execution and library management. This setup gives you full control to write code, run scripts, and manage packages yourself.

## Project Structure
- `main.py` - Main entry point with example code
- `requirements.txt` - Python package dependencies
- `.gitignore` - Git ignore rules for Python projects

## How to Use

### Running Python Code
1. Edit `main.py` or create new `.py` files
2. Click the "Run" button or the workflow will auto-run
3. View output in the console

### Installing Packages
You have several options:

**Option 1: Using requirements.txt**
1. Add package names to `requirements.txt`
2. Run: `pip install -r requirements.txt`

**Option 2: Direct pip install**
Run in the shell:
```bash
pip install package-name
```

**Option 3: Using the Replit packager**
The packager tool can also install Python packages automatically.

### Running Different Scripts
To run a different Python file:
```bash
python filename.py
```

Or update the workflow configuration to run your preferred script.

## Recent Changes
- 2025-11-05:
  - **修复精度计算问题**：
    - 入金（已入账）：使用截断到小数点后两位（trunc2），不四舍五入
    - 出金（已出账）：使用四舍五入到小数点后两位（round2）
    - 解决了多笔交易累加时的小数位误差问题
    - 确保显示的金额与实际存储的金额完全一致
  - **改进撤销功能**：
    - 修复：撤销失败的问题（原依赖不可靠的文本匹配）
    - 新实现：基于message_id的精确撤销
    - 功能增强：可撤销任何管理员操作的记录（不限于自己的）
    - 支持类型：入金、出金、下发三种交易类型
    - 自动保存：每笔交易自动保存Bot回复消息的message_id
    - 反馈优化：撤销时显示详细的交易信息（金额、国家、类型）
  - **新增"清除数据"命令**：
    - 命令：在群组发送"清除数据"
    - 功能：清除今日00:00（北京时间）至当前时间的所有交易数据
    - 权限：仅管理员可用
    - 清除范围：已入账、已出账、已下发所有记录
    - 自动重算：清除后自动重新计算应下发和已下发金额
    - 统计反馈：显示清除的笔数和USDT金额
    - 日志记录：所有清除操作记录到日志文件
- 2025-11-02:
  - **Web查账系统上线**（完整版）:
    - Flask Web应用提供可视化查账界面
    - 功能：交易记录查询、日期筛选、操作员统计、数据可视化
    - 安全认证：HMAC-SHA256 Token，24小时有效期
    - 交易回退功能（仅OWNER权限）
    - 响应式UI设计，支持手机/平板/电脑
    - Bot集成：所有账单消息附带"📊 查看账单明细"按钮
    - 安全修复：强制要求SESSION_SECRET环境变量
    - 技术栈：Flask + Bootstrap 5 + Vanilla JS
    - 部署：Docker多进程启动（Bot + Web应用）
    - 新增文件：web_app.py, templates/, static/, start.sh
    - 新增依赖：Flask==3.0.0
    - 新增文档：CLAWCLOUD_WEB_DEPLOY.md, WEB_DASHBOARD_GUIDE.md
- 2025-11-01:
  - **ClawCloud Run deployment support added**:
    - Created Dockerfile for containerized deployment
    - Added CLAWCLOUD_DEPLOY.md with complete deployment guide
    - Added .dockerignore for optimized Docker builds
    - Platform features: $5/month free tier, Docker native, visual management
    - Estimated cost: ~$4/month (within free tier)
- 2025-10-22:
  - **Broadcast feature added**: OWNER can now broadcast messages to all users who have privately messaged the bot
    - New commands: `广播 消息内容` or `群发 消息内容` (use in private chat with bot)
    - Automatically sends to all users (excluding OWNER)
    - Shows delivery statistics: success/failed/total counts
    - User list extracted from data/logs/private_chats/ directory
- 2025-10-14: Initial project setup with Python 3.11
- Created basic structure with main.py and requirements.txt
- Replaced with advanced finance bot using python-telegram-bot library
- Installed Flask, python-dotenv dependencies
- Configured workflow to run bot.py on port 5000
- Created comprehensive README.md for finance bot
- 2025-10-15: 
  - Implemented multi-group support - each group has independent accounting data
  - Refactored data structure to use per-group state management
  - Added group-specific log directories (data/logs/group_<chat_id>/)
  - Removed OKX exchange rate query feature
  - Added dual-mode support: Polling (Replit) + Webhook (Render Web Service)
  - Code now supports both local development and production deployment
  - Successfully uploaded project to GitHub: lea499579-stack/telegram-finance-bot
- 2025-10-16:
  - **Private chat feature**: Added bidirectional private messaging support
    - Users can privately message the bot
    - Messages automatically forwarded to OWNER_ID (7784416293)
    - OWNER can reply through bot by replying to forwarded messages
    - All conversations logged to data/logs/private_chats/user_{id}.log
  - **Architecture decision**: Switched from Webhook to Polling mode for production
    - Discovered Gunicorn+asyncio incompatibility issues with webhook initialization
    - Polling mode proved more stable and reliable for Render.com deployment
  - **Successful deployment to Render.com**:
    - Using Python direct execution: `python bot.py`
    - Polling mode with HTTP health check endpoint on port 10000
    - Configured UptimeRobot to ping /health every 5 minutes (prevents free tier sleep)
    - Service URL: https://telegram-finance-bot-c3wn.onrender.com
  - Created RENDER_POLLING_DEPLOY.md deployment guide
  - Bot now running 24/7 on Render.com with UptimeRobot keep-alive
- 2025-10-17:
  - **Fixed photo caption support**: Bot can now recognize numbers in photo captions
    - Modified handle_text to read both message.text and message.caption
    - Updated MessageHandler to listen to (filters.TEXT | filters.CAPTION)
    - Users can now send "-10018" with a photo and bot will process it correctly
  - **Added quick reset feature**: New "重置默认值" command
    - One-click reset to recommended default rates and exchange rates
    - Default: 入金费率10%/汇率153, 出金费率2%/汇率137
    - Solves the issue where new groups may have zero rates due to old data files
    - Also accepts "恢复默认值" as alternative command
  - **Enhanced admin management**: Support @mention for adding/removing admins
    - Can now use "@username 设置机器人管理员" (faster method)
    - Still supports traditional reply-to-message method
    - Both methods work for adding and removing admins
  - **Stricter undo control**: Transaction undo now requires exact keyword
    - Must type "撤销" exactly (no other text works)
    - Prevents accidental undos from random replies to transaction messages
    - Still works by replying to transaction message + typing "撤销"
- 2025-10-20:
  - **UI improvement: New transaction record format**
    - Changed display from emoji circles (①②③) to clean list format
    - 入金记录: 时间 金额^费率/ 汇率 = USDT (fee rate shown as superscript)
    - 出金记录: 时间 金额^费率 / 汇率 = USDT  
    - 下发记录单独分类显示
    - Records now save exchange rate (fx) and fee rate for accurate display
    - Applied to both summary and full record views
  - **Reverted to JSON file storage** (PostgreSQL removed)
    - PostgreSQL required credit card verification on Render free tier
    - Returned to simple JSON file storage in ./data/ directory
    - Removed psycopg2-binary dependency
    - All tests passing with file-based storage
    - Auto-repair feature detects and fixes zero rates on file load
  - **Data storage location**:
    - Group data: data/groups/group_<chat_id>.json
    - Admin list: data/admins.json
    - Logs: data/logs/ (ephemeral on Render)
  - ⚠️ **Important**: Render free tier resets files on redeploy
    - Use "重置默认值" command to quickly restore settings after redeploy
    - Avoid frequent redeployments to minimize data loss
  - **Initial setup change**: Default rates set to 0
    - New groups start with all rates/exchange rates at 0
    - Bot prompts admins to set rates before first transaction
    - Prevents accidental use of preset values
    - Use "重置默认值" for quick setup with recommended values
  - **Code cleanup and security improvements**:
    - Removed Flask and gunicorn dependencies (reduced from 6 to 3 packages)
    - Removed all Webhook mode code (simplified from 1208 to 1071 lines)
    - Added lightweight HTTP health check server using Python's built-in http.server
    - Eliminated token exposure in logs (no more printing sensitive URLs)
    - Deployment now requires only 2 environment variables: TELEGRAM_BOT_TOKEN, OWNER_ID
    - Pure Polling mode - simpler, more reliable, easier to maintain

## User Preferences
- Manual control over code execution and library installation
- Chinese language interface for documentation
- Financial tracking bot for Telegram
- **Important**: Each Telegram group has completely independent accounting data

## Bot Features
- Finance tracking with in/out transactions
- USDT conversion with custom rates and exchange rates
- Admin system with permission management
- **Multi-group support**: Each group maintains independent:
  - Transaction records (入金/出金)
  - USDT summary (应下发/已下发)
  - Rate and exchange settings
  - Daily reset schedule
  - Transaction logs
- **Private chat support**: 
  - Users can privately message the bot
  - All private messages are forwarded to the bot owner (OWNER_ID)
  - Owner can reply to users through the bot
  - All conversations are logged in data/logs/private_chats/
  - **Broadcast feature**: OWNER can send messages to all users who have privately messaged the bot
    - Command: `广播 消息内容` or `群发 消息内容`
    - Shows delivery statistics (success/failed/total)
- Data persistence with per-group JSON files (data/groups/group_<chat_id>.json)
- HTTP keepalive server on port 5000
