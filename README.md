# 🚀 NeoHeberg VPS 自动登录与重启保活工作流

基于 **Playwright** 自动化框架开发，专门用于自动登录 [NeoHeberg 新版控制台](https://dash.neoheberg.fr/login)，穿透人机验证与两步登录，进入面板定位目标 VPS 并触发 **`Redémarrer` (重启)** 以维持机器与账号的活跃度。任务执行完成后自动通过 **Telegram Bot 发送带真实操作截图的通知报告**。

---

## 📌 支持特性

- **适配全新 NeoHeberg 面板**：针对 `https://dash.neoheberg.fr` 全新交互架构与路由设计。
- **集成 sing-box 代理中间件**：内置多协议代理支持（Hysteria2 / VMess / VLESS / TUIC / Trojan / SOCKS5 / HTTP），彻底绕过 GitHub Actions 机房 IP 触发的 Cloudflare 5 秒盾与 Turnstile 质询。
- **两步登录自动化**：智能处理 `Identifiant (用户名/邮箱)` 与 `Mot de passe (密码)` 两阶段输入切换。
- **自动穿透 Axel-L Cap-Widget 人机验证**：自动调用并执行 PoW (Proof-of-Work) 挑战计算，验证通过后自动提交。
- **自动处理 Google GDPR Consent 弹窗**：自动识别并授权/移除遮罩层，防止干扰主界面元素交互。
- **精准触发 Redémarrer 重启**：
  - 优先在首页「Mes services」卡片中通过 `[data-vps-power="reboot"]` 直接派发原生 DOM 点击；
  - 若在首页未暴露，则自动进入 `Gérer le VPS` 详情面板并在「ACTIONS」中触发重启。
- **自动确认二次弹窗**：如遇二次确认提示框（`Confirmer` / `Valider` / `Oui`）自动确认。
- **独立仓库自包含运行**：核心脚本与依赖直接存放于本仓库，无需依赖外部私有仓库，开箱即用。
- **多账号批量轮询**：账号间会话（Context / Cookies）严格隔离，互不干扰。
- **Telegram 推送**：每次运行结束自动推送美化卡片消息，并附带真实 VPS 仪表盘实时操作截图。
- **运行快照留存**：每次运行均会保存结果截图（如 `reboot_success_username.png`），并上传至 Actions Artifacts。
- **全自动无人值守**：配套 GitHub Actions 工作流，每天自动定时运行一次，免电脑开机。

---

## ⚙️ 环境变量与密钥配置 (GitHub Secrets)

进入 GitHub 仓库页面 -> **Settings** -> **Secrets and variables** -> **Actions**：

| Secret 变量名 | 必填 | 示例 / 说明 |
| :--- | :--- | :--- |
| `NEOHEBERG_ACCOUNTS` | 选填 | 多个账号（格式为 `账号:密码,账号2:密码2`）。若未设置则默认使用内置账号 `yxj0322` |
| `PROXY_NODE` / `NODE_LINK` | 选填 | 自定义代理节点链接（支持 `hysteria2://...`、`vmess://...`、`vless://...` 等）。内置高可用节点备选 |
| `TG_BOT_TOKEN` | 选填 | 你的 Telegram Bot Token（如 `123456789:ABCdefGhI...`） |
| `TG_CHAT_ID` | 选填 | 你的 Telegram 用户 ID 或频道/群组 ID（如 `987654321`） |

---

## 🚀 部署运行方法

### 方式 1：GitHub Actions 自动定时运行（最推荐，免开机）
1. 在仓库顶部的 **Actions** 标签页，点击 `NeoHeberg Auto Reboot & Keepalive`；
2. 点击 **Run workflow** 即可随时手动测试执行；
3. **定时执行**：默认配置为每天自动执行一次 (北京时间 11:20)。执行完成后 Telegram 会立刻收到通知！

---

### 方式 2：本地电脑运行
1. 安装依赖：
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```
2. 运行脚本：
   ```bash
   python neoheberg_renew.py
   ```
