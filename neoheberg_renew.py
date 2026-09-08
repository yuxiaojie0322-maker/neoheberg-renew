#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NeoHeberg VPS 自动登录与重启保活脚本 (支持多账号 + Telegram 结果推送)
- 登录: https://extranet.neoheberg.fr/login
- 自动处理 Cap-Widget 验证
- 查找 VPS 并点击 "Gerer" (管理)
- 找到 ACTIONS 中的 "Redémarrer" (重启) 并执行
- 支持多账号轮询与 GitHub Actions 定时运行
- 支持 Telegram Bot 运行结果与截图消息推送
"""

import os
import sys
import time
import json
import logging
import datetime
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# 配置日志输出
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("NeoHeberg-Renew")

# ==================== 配置区域 ====================
# Telegram 推送配置 (从环境变量读取)
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

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
            data = json.loads(env_accounts)
            if isinstance(data, list):
                logger.info(f"成功从环境变量加载了 {len(data)} 个账号 (JSON 格式)")
                return data
        except Exception:
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

def send_tg_message(text):
    """发送纯文本消息到 Telegram"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        logger.info("未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过 Telegram 推送")
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        res = requests.post(url, json=payload, timeout=20)
        res_json = res.json()
        if res_json.get("ok"):
            logger.info("Telegram 结果推送成功！")
            return True
        else:
            logger.warning(f"Telegram 推送返回失败: {res_json}")
            return False
    except Exception as e:
        logger.error(f"Telegram 发送异常: {e}")
        return False

def send_tg_photo(photo_path, caption):
    """发送带截图的通知到 Telegram"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return False
    if not os.path.exists(photo_path):
        return send_tg_message(caption)
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
        with open(photo_path, "rb") as f:
            files = {"photo": f}
            data = {
                "chat_id": TG_CHAT_ID,
                "caption": caption,
                "parse_mode": "HTML"
            }
            res = requests.post(url, files=files, data=data, timeout=30)
        res_json = res.json()
        if res_json.get("ok"):
            logger.info(f"Telegram 截图 {photo_path} 发送成功！")
            return True
        else:
            logger.warning(f"Telegram 图片发送失败: {res_json}，降级发送纯文本")
            return send_tg_message(caption)
    except Exception as e:
        logger.error(f"Telegram 图片发送异常: {e}")
        return send_tg_message(caption)

def handle_cap_widget(page):
    """处理 NeoHeberg 登录页面的 Cap-Widget 人机验证组件"""
    logger.info("检查是否存在 Cap-Widget 人机验证...")
    try:
        widget = page.locator('#cap-login, cap-widget')
        if widget.count() > 0 and widget.first.is_visible(timeout=3000):
            logger.info("发现 Cap-Widget，正在触发验证...")
            widget.first.click()
            time.sleep(1)

            checkbox = page.locator('cap-widget input[type="checkbox"], cap-widget .cap-checkbox, cap-widget button')
            if checkbox.count() > 0 and checkbox.first.is_visible():
                try:
                    checkbox.first.click(timeout=2000)
                except Exception:
                    pass

            logger.info("等待人机验证通过...")
            for _ in range(15):
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
    start_time = time.time()
    logger.info(f"==================================================")
    logger.info(f"[{index}/{total}] 开始处理账号: {username}")
    logger.info(f"==================================================")

    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        viewport={"width": 1366, "height": 768},
        locale="fr-FR"
    )
    page = context.new_page()
    result_info = {
        "username": username,
        "status": "FAIL",
        "message": "未知原因",
        "duration": 0,
        "screenshot": None
    }

    try:
        login_url = "https://extranet.neoheberg.fr/login"
        logger.info(f"正在访问登录页面: {login_url}")
        page.goto(login_url, wait_until="networkidle", timeout=30000)

        if "/login" in page.url:
            logger.info(f"输入用户名: {username}")
            page.fill('input#email, input[name="email"]', username)
            time.sleep(0.5)

            logger.info("输入密码...")
            page.fill('input#password, input[name="password"]', password)
            time.sleep(0.5)

            handle_cap_widget(page)

            logger.info("点击登录按钮 (Se connecter)...")
            submit_btn = page.locator('button[type="submit"]:has-text("Se connecter"), button[type="submit"]')
            submit_btn.first.click()

            try:
                page.wait_for_url(lambda u: "/login" not in u, timeout=20000)
                logger.info(f"登录成功！当前页面: {page.url}")
            except PlaywrightTimeout:
                error_msg = page.locator('.text-red-500, .alert-danger, [role="alert"]').text_content(timeout=3000) if page.locator('.text-red-500, .alert-danger').count() > 0 else "登录超时未跳转"
                logger.error(f"登录失败: {error_msg.strip()}")
                fail_shot = f"login_fail_{username}.png"
                page.screenshot(path=fail_shot)
                result_info["message"] = f"登录失败: {error_msg.strip()}"
                result_info["screenshot"] = fail_shot
                return result_info

        time.sleep(2)

        # 寻找 VPS 管理入口 (Gerer)
        logger.info("正在查找 VPS 管理按钮 (Gerer / Gérer)...")
        vps_tab = page.locator('div:has-text("VPS"), button:has-text("VPS"), a:has-text("VPS")').filter(has_text="VPS")
        if vps_tab.count() > 0:
            try:
                vps_tab.first.click(timeout=2000)
                time.sleep(1)
            except Exception:
                pass

        gerer_btn = page.locator('a:has-text("Gerer"), button:has-text("Gerer"), a:has-text("Gérer"), button:has-text("Gérer"), [href*="/vps/"]')
        if gerer_btn.count() == 0:
            gerer_btn = page.locator('.fa-cog, .fa-gear').locator('..')

        if gerer_btn.count() == 0:
            logger.error("未找到任何 VPS 的 Gerer 管理按钮")
            shot = f"no_vps_{username}.png"
            page.screenshot(path=shot)
            result_info["message"] = "未找到 VPS 管理按钮"
            result_info["screenshot"] = shot
            return result_info

        logger.info(f"找到管理入口，正在进入 VPS 控制面板...")
        gerer_btn.first.click()

        page.wait_for_load_state("networkidle", timeout=20000)
        time.sleep(2)
        panel_url = page.url
        logger.info(f"已进入 VPS 管理详情页: {panel_url}")

        # 查找 ACTIONS 中的 "Redémarrer" (重启) 按钮
        logger.info("正在查找 ACTIONS 区域中的 'Redémarrer' (重启) 按钮...")
        reboot_btn = page.locator('button:has-text("Redémarrer"), a:has-text("Redémarrer"), button:has-text("Redemarrer"), a:has-text("Redemarrer"), [title*="Redémarrer"]')
        if reboot_btn.count() == 0:
            reboot_btn = page.locator('button:has(.fa-sync), button:has(.fa-redo), button:has(.fa-rotate-right)')

        if reboot_btn.count() == 0:
            logger.error("未找到 'Redémarrer' 重启按钮")
            shot = f"no_reboot_{username}.png"
            page.screenshot(path=shot)
            result_info["message"] = "未找到 Redémarrer 重启按钮"
            result_info["screenshot"] = shot
            return result_info

        logger.info("点击 'Redémarrer' (重启) 按钮...")
        reboot_btn.first.click()
        time.sleep(1.5)

        # 处理二次确认弹窗
        import re
        confirm_btn = page.locator('button, a').filter(has_text=re.compile(r'(Confirmer|Valider|Oui|Yes|Confirm)', re.I))
        if confirm_btn.count() > 0 and confirm_btn.first.is_visible():
            logger.info("检测到二次确认弹窗，点击确认...")
            confirm_btn.first.click()
            time.sleep(1)

        success_shot = f"reboot_success_{username}.png"
        page.screenshot(path=success_shot)
        logger.info(f"✅ 账号 {username} 的 VPS 已成功重启！截图保存至: {success_shot}")

        result_info["status"] = "SUCCESS"
        result_info["message"] = "VPS 重启指令下发成功"
        result_info["screenshot"] = success_shot
        result_info["panel_url"] = panel_url
        return result_info

    except Exception as e:
        logger.error(f"❌ 处理账号 {username} 时发生异常: {e}")
        err_shot = f"error_{username}.png"
        try:
            page.screenshot(path=err_shot)
            result_info["screenshot"] = err_shot
        except Exception:
            pass
        result_info["message"] = str(e)
        return result_info
    finally:
        result_info["duration"] = round(time.time() - start_time, 1)
        context.close()

def main():
    accounts = load_accounts()
    if not accounts:
        logger.error("未配置任何账号！")
        sys.exit(1)

    logger.info(f"开始执行 NeoHeberg 自动保活任务，共加载 {len(accounts)} 个账号")
    headless = os.environ.get("HEADLESS", "true").lower() != "false"

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )

        for i, acc in enumerate(accounts, 1):
            res = process_single_account(browser, acc, i, len(accounts))
            results.append(res)
            if i < len(accounts):
                time.sleep(5)

        browser.close()

    success_list = [r for r in results if r["status"] == "SUCCESS"]
    fail_list = [r for r in results if r["status"] != "SUCCESS"]

    logger.info("==================================================")
    logger.info(f"任务执行结束: 成功 {len(success_list)} 个, 失败 {len(fail_list)} 个")
    logger.info("==================================================")

    # 构造 Telegram 推送内容
    now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status_emoji = "🎉" if len(fail_list) == 0 else "⚠️"
    
    msg_lines = [
        f"{status_emoji} <b>NeoHeberg VPS 自动保活通知</b>",
        "━━━━━━━━━━━━━━━━━━"
    ]

    for r in results:
        badge = "✅ 重启成功" if r["status"] == "SUCCESS" else f"❌ {r['message']}"
        msg_lines.append(f"👤 <b>账号</b>: <code>{r['username']}</code>")
        msg_lines.append(f"📊 <b>状态</b>: {badge} (耗时 {r['duration']}s)")
        if r.get("panel_url"):
            msg_lines.append(f"🔗 <b>面板</b>: <a href=\"{r['panel_url']}\">查看详情</a>")
        msg_lines.append("──────────────────")

    msg_lines.append(f"📈 <b>汇总</b>: 成功 {len(success_list)} / 失败 {len(fail_list)}")
    msg_lines.append(f"⏰ <b>时间</b>: {now_time}")

    tg_content = "\n".join(msg_lines)

    # 如果有成功截图，带上第一张截图发送；否则发纯文本
    sent_photo = False
    for r in results:
        if r.get("screenshot") and os.path.exists(r["screenshot"]):
            logger.info(f"正在向 Telegram 发送带截图的运行报告: {r['screenshot']}")
            send_tg_photo(r["screenshot"], tg_content)
            sent_photo = True
            break

    if not sent_photo:
        send_tg_message(tg_content)

    if len(fail_list) > 0 and len(success_list) == 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
