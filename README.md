# NeoHeberg VPS 自动登录与重启保活脚本

基于 **Playwright** 自动化框架开发，专门用于自动登录 [NeoHeberg Extranet](https://extranet.neoheberg.fr/login)，穿透人机验证组件，进入 VPS 管理面板（`Gerer`）并触发重启（`Redémarrer`）以维持机器与账号的活跃度。

---

## 📌 支持的特性
- **支持多账号批量轮询**：账号间环境严格隔离（Independent Context），互不干扰。
- **自动处理 Cap-Widget 验证**：处理登录页面的 Axel L 验证组件。
- **智能定位元素**：自动匹配 `Gerer` / `Gérer` 按钮与 ACTIONS 中的 `Redémarrer` 重启按钮。
- **自动确认弹窗**：如遇二次确认提示自动处理。
- **运行快照留存**：每次运行均会保存结果截图（如 `reboot_success_username.png`），方便核验。
- **全自动无人值守**：配套 GitHub Actions 工作流，每 3 天自动定时运行，无需电脑开机。

---

## ⚙️ 多账号配置说明

### 方式一：直接在 Python 脚本中配置
打开 `neoheberg_renew.py`，修改 `DEFAULT_ACCOUNTS` 列表：
```python
DEFAULT_ACCOUNTS = [
    {"username": "yxj0322", "password": "YxJ223512@"},
    {"username": "你的第二个账号", "password": "你的第二个密码"},
    {"username": "你的第三个账号", "password": "你的第三个密码"},
]
```

### 方式二：通过环境变量配置（推荐，适合 GitHub Actions）
设置环境变量 `NEOHEBERG_ACCOUNTS`：
- **简易格式**（英文逗号与冒号分隔）：
  ```text
  yxj0322:YxJ223512@,account2:password2,account3:password3
  ```
- **或 JSON 格式**：
  ```json
  [
    {"username": "yxj0322", "password": "YxJ223512@"},
    {"username": "account2", "password": "password2"}
  ]
  ```

---

## 🚀 部署运行方法

### 方案 A：GitHub Actions 自动定时运行（最推荐，免开机）
1. 在 GitHub 上新建一个**私有仓库**（Private Repository）。
2. 将本项目所有文件（包含 `.github/workflows/neoheberg_renew.yml`）上传/推送到仓库。
3. 进入 GitHub 仓库页面 -> **Settings** -> **Secrets and variables** -> **Actions**：
   - 点击 **New repository secret**
   - Name 填写：`NEOHEBERG_ACCOUNTS`
   - Secret 填写：`yxj0322:YxJ223512@,账号2:密码2`
4. 进入仓库顶部的 **Actions** 标签页，点击 `NeoHeberg Auto Reboot & Keepalive` -> 点击 **Run workflow** 即可手动测试执行！
5. 定时触发：脚本配置为每 3 天自动执行一次。执行完毕后，可在运行记录中下载 `neoheberg-screenshots` 压缩包查看运行截图。

---

### 方案 B：本地电脑运行
1. 安装 Python 依赖：
   ```bash
   pip install playwright
   playwright install chromium
   ```
2. 运行脚本（默认无头模式后台运行）：
   ```bash
   python neoheberg_renew.py
   ```
3. 如需在本地弹出浏览器窗口观看操作过程，设置环境变量 `HEADLESS=false` 后运行：
   - Windows PowerShell:
     ```powershell
     $env:HEADLESS="false"; python neoheberg_renew.py
     ```
   - Linux / macOS:
     ```bash
     HEADLESS=false python neoheberg_renew.py
     ```
