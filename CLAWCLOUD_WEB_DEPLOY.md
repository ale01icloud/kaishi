# ClawCloud部署指南 - Web查账版本

## 📋 必需的环境变量

**⚠️ 重要：所有环境变量都必须正确设置，否则部署会失败！**

| 环境变量 | 是否必需 | 说明 | 示例值 |
|---------|---------|------|--------|
| `TELEGRAM_BOT_TOKEN` | ✅ 必需 | Bot Token | `123456:ABC-DEF...` |
| `OWNER_ID` | ✅ 必需 | 管理员Telegram ID | `7784416293` |
| `SESSION_SECRET` | ✅ **必需（新增）** | Web查账加密密钥 | 见下方生成方法 |
| `WEB_BASE_URL` | ✅ 必需 | ClawCloud应用URL | `https://你的域名` |
| `PORT` | 可选 | Bot健康检查端口 | `10000` (默认值) |
| `WEB_PORT` | 可选 | Web应用端口 | `5000` (默认值) |

---

## 🔐 生成SESSION_SECRET

**这是最重要的安全设置！** 在终端执行：

```bash
openssl rand -hex 32
```

会输出类似：
```
a1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef123456
```

**复制这个字符串，在ClawCloud中设置为 `SESSION_SECRET` 环境变量！**

---

## 🚀 ClawCloud部署步骤

### 1. 准备Docker镜像

确保GitHub Actions已成功构建并推送镜像到Docker Hub：

**镜像名称**：`usdiele/tron-calculator-rental:latest`

检查地址：https://hub.docker.com/r/usdiele/tron-calculator-rental

### 2. 在ClawCloud创建应用

访问：https://www.claw.cloud/

1. 点击 **"New Deployment"**
2. **Application Name**: `telegram-finance-bot` （或您喜欢的名字）

### 3. 配置镜像

**Image**：
- 选择：`Public`
- **Image Name**: `usdiele/tron-calculator-rental:latest`

### 4. 配置网络

**Network**：
- **Container Port**: `5000` ⚠️ **重要：必须是5000（Web应用端口）**
- **Public Access**: ✅ **必须开启**

**📌 说明**：
- ClawCloud会自动将Container Port设置为环境变量`PORT`
- Web应用会监听`PORT`环境变量指定的端口
- Bot的健康检查运行在内部端口10000（无需公开）

### 5. 配置环境变量

点击 **Environment Variables** 右侧的 **"+ Add"**，逐个添加：

```
Key: TELEGRAM_BOT_TOKEN
Value: 您的Bot Token（从@BotFather获取）

Key: OWNER_ID
Value: 7784416293

Key: SESSION_SECRET
Value: （粘贴刚才生成的64位字符串）

Key: WEB_BASE_URL
Value: https://您的ClawCloud域名
```

**⚠️ 注意**：
- `WEB_BASE_URL` 部署后才知道，先填 `https://temp.com`
- 部署成功后会获得ClawCloud分配的域名
- 然后回来更新 `WEB_BASE_URL` 为真实域名
- 更新后需要重启应用

### 6. 配置资源

**Usage**：
- **CPU**: `0.5` 或 `1`
- **Memory**: `1G` （512M也够用）
- **Replicas**: `1`

### 7. 部署

点击底部的 **"Deploy"** 或 **"Create"** 按钮。

---

## 📊 部署后配置

### 1. 获取应用域名

部署成功后，ClawCloud会分配一个域名，例如：
```
https://eu-central-1.run.claw.cloud/your-app-id
```

### 2. 更新WEB_BASE_URL

1. 复制您的应用域名
2. 在ClawCloud **Environment Variables** 中更新 `WEB_BASE_URL`
3. 点击 **Restart** 重启应用

### 3. 测试Web查账功能

1. 在Telegram群组中发送 `+0`
2. Bot回复账单汇总，并附带 **"📊 查看账单明细"** 按钮
3. 点击按钮，应该能打开Web查账界面
4. 如果打开失败，检查 `WEB_BASE_URL` 是否正确

---

## ✅ 功能验证

### 基础功能测试
```
群组中发送：
+1000      → 应该显示入金账单 + Web按钮
-500       → 应该显示出金账单 + Web按钮
+0         → 应该显示当前账单 + Web按钮
```

### Web查账测试
1. 点击 **"📊 查看账单明细"** 按钮
2. 应该打开Web界面
3. 查看：
   - ✅ 统计数据正确显示
   - ✅ 交易记录表格完整
   - ✅ 日期筛选功能正常
   - ✅ 按操作员统计可用

### OWNER专属功能（仅`OWNER_ID`用户可见）
1. 交易记录表格有 **"🔄 回退"** 按钮
2. 点击后弹出确认对话框
3. 确认回退后，记录从数据中删除

---

## 🔧 故障排查

### 问题1：容器启动失败
**错误信息**：`SESSION_SECRET环境变量未设置`

**解决方法**：
1. 检查Environment Variables是否包含 `SESSION_SECRET`
2. 确认值已正确粘贴（64位hex字符串）
3. 保存后重启应用

### 问题2：Web按钮无法打开
**症状**：点击按钮后404或无法访问

**检查清单**：
- [ ] `WEB_BASE_URL` 是否设置为应用的真实域名
- [ ] Container Port 是否设置为 `5000`
- [ ] Public Access 是否已开启
- [ ] 应用状态是否为 Running

**解决方法**：
```bash
# 正确的WEB_BASE_URL格式：
https://eu-central-1.run.claw.cloud/your-app-id

# 错误格式（不要加端口号）：
https://eu-central-1.run.claw.cloud:5000/your-app-id  ❌
```

### 问题3：Token过期
**症状**：Web界面提示 "Token无效或已过期"

**原因**：Token有效期24小时

**解决方法**：
1. 返回Telegram群组
2. 重新发送 `+0` 获取新token
3. 点击新的Web按钮

### 问题4：无法回退交易
**症状**：点击"回退"按钮没反应

**检查清单**：
- [ ] 当前用户ID是否等于 `OWNER_ID`
- [ ] 浏览器控制台是否有错误
- [ ] 交易记录是否有 `message_id` 字段

---

## 📈 性能和成本

**预估资源消耗**：
- **CPU**: 0.5 核
- **内存**: 512MB - 1GB
- **流量**: ~100MB/月

**ClawCloud免费额度**（2025年数据）：
- $5/月免费额度
- 足够运行本Bot

**预估月成本**：
- **~$2-4/月**（在免费额度内）

---

## 🔒 安全最佳实践

1. **定期轮换SESSION_SECRET**（每3-6个月）
2. **不要在代码中硬编码任何密钥**
3. **仅授予信任的用户OWNER权限**
4. **定期备份 `data/groups/` 数据文件**
5. **监控异常登录和回退操作**

---

## 📚 相关文档

- [Web查账系统使用指南](./WEB_DASHBOARD_GUIDE.md)
- [管理员指令大全](./管理员指令大全.md)
- [私聊功能说明](./PRIVATE_CHAT_GUIDE.md)

---

## 🆘 获取帮助

如有问题：
1. 检查应用日志（ClawCloud控制台）
2. 查看GitHub Issues
3. 联系开发者

**项目地址**：https://github.com/ale01icloud/tron-calculator-rental
