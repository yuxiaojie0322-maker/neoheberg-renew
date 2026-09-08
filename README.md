# NeoHeberg VPS 自动登录与重启保活脚本

基于 **Playwright** 自动化框架开发，专门用于自动登录 [NeoHeberg Extranet](https://extranet.neoheberg.fr/login)，穿透人机验证组件，进入 VPS 管理面板（`Gerer`）并触发重启（`Redémarrer`）以维持机器与账号的活跃度。运行完成后支持通过 **Telegram Bot 发送带截图的运行报告**。

---

## 📌 支持的特性
- **多账号批量轮询**：账号间环境严格隔离（Independent Context），互不干扰。
- **自动处理 Cap-Widget 验证**：处理登录页面的 Axel L 验证组件。
- **智能定位元素**：自动匹配 `Gerer` / `Gérer` 按钮与 ACTIONS 中的 `Redémarrer` 重启按钮。
- **自动确认弹窗**：如遇二次确认提示自动处理。
- **Telegram 推送**：每次运行结束自动推送美化卡片消息，并附带 VPS 仪表盘实时操作截图。
- **运行快照留存**：每次运行均会保存结果截图（如 `reboot_success_username.png`），方便核验。
- **全自动无人值守**：配套 GitHub Actions 工作流，每 3 天自动定时运行，无需电脑开机。

---

## ⚙️ 环境变量与密钥配置 (GitHub Secrets)

进入 GitHub 仓库页面 -> **Settings** -> **Secrets and variables** -> **Actions**：

| Secret 变量名 | 必填 | 示例 / 说明 |
| :--- | :--- | :--- |
| `NEOHEBERG_ACCOUNTS` | 选填 | 多个账号（格式为 `账号:密码,账号2:密码2`）。若未设置则默认使用代码内置账号 |
| `TG_BOT_TOKEN` | 选填 | 你的 Telegram Bot Token（如 `123456789:ABCdefGhI...`） |
| `TG_CHAT_ID` | 选填 | 你的 Telegram 用户 ID 或频道/群组 ID（如 `987654321`） |

---

## 🚀 部署运行方法

### 方案 A：GitHub Actions 自动定时运行（最推荐，免开机）
1. 在仓库顶部的 **Actions** 标签页，点击 `NeoHeberg Auto Reboot & Keepalive`；
2. 点击 **Run workflow** 即可随时手动测试执行；
3. **定时执行**：默认配置为每 3 天自动执行一次。执行完成后 Telegram 会立刻收到通知！

---

### 方案 B：本地电脑运行
1. 安装依赖：
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```
2. 运行脚本：
   ```bash
   python neoheberg_renew.py
   ```
