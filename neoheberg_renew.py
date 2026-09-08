#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NeoHeberg VPS 自动登录与重启保活脚本 (支持多账号)
- 登录: https://extranet.neoheberg.fr/login
- 自动处理 Cap-Widget 验证
- 查找 VPS 并点击 "Gerer" (管理)
- 找到 ACTIONS 中的 "Redémarrer" (重启) 并执行
- 支持多账号轮询与 GitHub Actions 定时运行
"""

import os
import sys
import time
import json
import logging
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# 配置日志输出
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("NeoHeberg-Renew")

# 账号列表配置
# 优先从环境变量 NEOHEBERG_ACCOUNTS 读取，格式为 JSON: [{"username": "...", "password": "..."}, ...]
# 或以英文逗号分隔: username:password,username2:password2
# 如果环境变量未设置，则使用下方默认列表
DEFAULT_ACCOUNTS = [
    {"username": "yxj0322", "password": "YxJ223512@"},
    # 在此添加更多账号:
    # {"username": "your_account_2", "password": "your_password_2"},
]

def load_accounts():
    env_accounts = os.environ.get("NEOHEBERG_ACCOUNTS", "").strip()
    if env_accounts:
        try:
            # 尝试按 JSON 解析
            data = json.loads(env_accounts)
            if isinstance(data, list):
                logger.info(f"成功从环境变量加载了 {len(data)} 个账号 (JSON 格式)")
                return data
        except Exception:
            # 尝试按 逗号和冒号 解析: user1:pass1,user2:pass2
            accounts = []
            for item in env_accounts.split(','):
                item = item.strip()
                if ':' in item:
                    parts = item.split(':', 1)
                    accounts.append({"username": parts[0].strip(), "password": parts[1].strip()})
            if accounts:
                logger.info(f"成功从环境变量加载了 {len(accounts)} 个账号 (格式 user:pass)")
                return accounts
    return DEFAULT_ACCOUNTS

def handle_cap_widget(page):
    """
    处理 NeoHeberg 登录页面的 Cap-Widget 人机验证组件
    """
    logger.info("检查是否存在 Cap-Widget 人机验证...")
    try:
        # Cap-Widget 是一个 Web Component (Shadow DOM)
        widget = page.locator('#cap-login, cap-widget')
        if widget.count() > 0 and widget.first.is_visible(timeout=3000):
            logger.info("发现 Cap-Widget，正在触发验证...")
            # 点击控件本身或内部区域
            widget.first.click()
            time.sleep(1)

            # 尝试点击 Shadow DOM 内的复选框或按钮 (Playwright 默认支持穿透 Shadow DOM)
            checkbox = page.locator('cap-widget input[type="checkbox"], cap-widget .cap-checkbox, cap-widget button')
            if checkbox.count() > 0 and checkbox.first.is_visible():
                try:
                    checkbox.first.click(timeout=2000)
                except Exception:
                    pass

            # 等待验证完成（通常会变成已解决状态或带有 data-cap-solved="true"）
            logger.info("等待人机验证通过...")
            for _ in range(15):
                # 检查属性或文本
                try:
                    solved = page.evaluate("""() => {
                        const w = document.querySelector('cap-widget');
                        if (!w) return true;
                        if (w.getAttribute('data-cap-solved') === 'true') return true;
                        const text = w.innerText || (w.shadowRoot ? w.shadowRoot.textContent : '');
                        return text.includes('humain') || text.includes('Vérifié') || text.includes('Solved');
                    }""")
                    if solved:
                        logger.info("Cap-Widget 人机验证已通过！")
                        return True
                except Exception:
                    pass
                time.sleep(1)
    except Exception as e:
        logger.warning(f"Cap-Widget 处理异常(可能无须手动操作): {e}")
    return True

def process_single_account(browser, account, index, total):
    username = account.get("username", "").strip()
    password = account.get("password", "").strip()
    logger.info(f"==================================================")
    logger.info(f"[{index}/{total}] 开始处理账号: {username}")
    logger.info(f"==================================================")

    # 每一个账号使用完全独立的上下文 (隔离 Cookies 和 Cache)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        viewport={"width": 1366, "height": 768},
        locale="fr-FR"
    )
    page = context.new_page()

    try:
        # 1. 打开登录页面
        login_url = "https://extranet.neoheberg.fr/login"
        logger.info(f"正在访问登录页面: {login_url}")
        page.goto(login_url, wait_until="networkidle", timeout=30000)

        # 检查是否已经在仪表盘（如果已登录）
        if "/login" not in page.url:
            logger.info(f"当前已处于登录状态: {page.url}")
        else:
            # 2. 输入用户名与密码
            logger.info(f"输入用户名: {username}")
            page.fill('input#email, input[name="email"]', username)
            time.sleep(0.5)

            logger.info("输入密码...")
            page.fill('input#password, input[name="password"]', password)
            time.sleep(0.5)

            # 3. 处理人机验证
            handle_cap_widget(page)

            # 4. 点击登录按钮
            logger.info("点击登录按钮 (Se connecter)...")
            submit_btn = page.locator('button[type="submit"]:has-text("Se connecter"), button[type="submit"]')
            submit_btn.first.click()

            # 等待登录跳转完成
            try:
                page.wait_for_url(lambda u: "/login" not in u, timeout=20000)
                logger.info(f"登录成功！当前页面: {page.url}")
            except PlaywrightTimeout:
                # 检查页面是否显示错误提示
                error_msg = page.locator('.text-red-500, .alert-danger, [role="alert"]').text_content(timeout=3000) if page.locator('.text-red-500, .alert-danger').count() > 0 else ""
                if error_msg:
                    logger.error(f"登录失败，提示错误: {error_msg.strip()}")
                else:
                    logger.error(f"登录后未检测到页面跳转，当前依然在: {page.url}")
                page.screenshot(path=f"login_fail_{username}.png")
                return False

        time.sleep(2)

        # 5. 查找 "Mes services" 下的 VPS 区域并点击 "Gerer"
        logger.info("正在查找 VPS 管理按钮 (Gerer / Gérer)...")

        # 如果有 VPS 选项卡未激活，先确保点击 VPS 选项卡
        vps_tab = page.locator('div:has-text("VPS"), button:has-text("VPS"), a:has-text("VPS")').filter(has_text="VPS")
        if vps_tab.count() > 0:
            try:
                vps_tab.first.click(timeout=2000)
                time.sleep(1)
            except Exception:
                pass

        # 寻找 "Gerer" 按钮（如图一所示的蓝色 Gerer 齿轮按钮）
        gerer_btn = page.locator('a:has-text("Gerer"), button:has-text("Gerer"), a:has-text("Gérer"), button:has-text("Gérer"), [href*="/vps/"]')
        
        if gerer_btn.count() == 0:
            logger.warning("未直接匹配到包含 Gerer 文字的按钮，尝试通过图标或卡片链接查找...")
            gerer_btn = page.locator('.fa-cog, .fa-gear').locator('..')

        if gerer_btn.count() == 0:
            logger.error("未找到任何 VPS 的 Gerer 管理按钮，请检查该账号下是否有活跃 VPS。")
            page.screenshot(path=f"no_vps_{username}.png")
            return False

        logger.info(f"找到 {gerer_btn.count()} 个管理入口，正在点击进入第一个 VPS 详情页...")
        gerer_btn.first.click()

        # 等待进入管理详情页 (包含 ACTIONS 区域)
        page.wait_for_load_state("networkidle", timeout=20000)
        time.sleep(2)
        logger.info(f"已进入 VPS 管理详情页: {page.url}")

        # 6. 在 ACTIONS 中查找并点击 "Redémarrer" (重启) 按钮 (如图二所示)
        logger.info("正在查找 ACTIONS 区域中的 'Redémarrer' (重启) 按钮...")
        
        # 精确匹配包含 Redémarrer 或 Redemarrer 的按钮
        reboot_btn = page.locator('button:has-text("Redémarrer"), a:has-text("Redémarrer"), button:has-text("Redemarrer"), a:has-text("Redemarrer"), [title*="Redémarrer"]')

        if reboot_btn.count() == 0:
            # 备用方案：按 class 或带有 fa-sync / fa-redo 图标的按钮查找
            reboot_btn = page.locator('button:has(.fa-sync), button:has(.fa-redo), button:has(.fa-rotate-right)')

        if reboot_btn.count() == 0:
            logger.error("未找到 'Redémarrer' 按钮！正在保存当前页面快照...")
            page.screenshot(path=f"no_reboot_btn_{username}.png")
            return False

        logger.info("点击 'Redémarrer' (重启) 按钮...")
        reboot_btn.first.click()
        time.sleep(1.5)

        # 检查是否弹出了二次确认弹窗 (例如 "Confirmer" / "Oui" / "Valider")
        import re
        confirm_btn = page.locator('button, a').filter(has_text=re.compile(r'(Confirmer|Valider|Oui|Yes|Confirm)', re.I))
        if confirm_btn.count() > 0 and confirm_btn.first.is_visible():
            logger.info("检测到二次确认弹窗，正在点击确认...")
            confirm_btn.first.click()
            time.sleep(1)

        # 截图保存成功状态
        screenshot_path = f"reboot_success_{username}.png"
        page.screenshot(path=screenshot_path)
        logger.info(f"✅ 账号 {username} 的 VPS 已成功触发重启！截图已保存至: {screenshot_path}")
        time.sleep(3)
        return True

    except Exception as e:
        logger.error(f"❌ 处理账号 {username} 时发生异常: {e}")
        try:
            page.screenshot(path=f"error_{username}.png")
        except Exception:
            pass
        return False
    finally:
        context.close()

def main():
    accounts = load_accounts()
    if not accounts:
        logger.error("未配置任何账号！请在脚本中填写 DEFAULT_ACCOUNTS 或设置环境变量 NEOHEBERG_ACCOUNTS。")
        sys.exit(1)

    logger.info(f"开始执行 NeoHeberg 自动保活任务，共加载 {len(accounts)} 个账号")
    headless = os.environ.get("HEADLESS", "true").lower() != "false"

    success_count = 0
    fail_count = 0

    with sync_playwright() as p:
        # 启动 Chromium 浏览器
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"
            ]
        )

        for i, acc in enumerate(accounts, 1):
            ok = process_single_account(browser, acc, i, len(accounts))
            if ok:
                success_count += 1
            else:
                fail_count += 1
            # 账号间休息几秒，避免被风控
            if i < len(accounts):
                time.sleep(5)

        browser.close()

    logger.info("==================================================")
    logger.info(f"任务执行结束: 成功 {success_count} 个, 失败 {fail_count} 个")
    logger.info("==================================================")

    if fail_count > 0 and success_count == 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
