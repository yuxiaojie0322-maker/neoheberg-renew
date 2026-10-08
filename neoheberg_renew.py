#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NeoHeberg VPS 自动登录与重启保活脚本
- 登录地址: https://dash.neoheberg.fr/login (适配全新 NeoHeberg 仪表盘)
- 集成 Stealth 反爬指纹绕过与 Cloudflare Turnstile 5 秒盾自动穿透
- 两步登录自动化 (Identifiant -> Mot de passe)
- 自动穿透 Axel-L Cap-Widget 人机验证 (PoW 自动求解与触发)
- 自动进入控制台并点击 "Redémarrer" (重启服务器) 保持机器与账号活跃度
- 支持多账号轮询 (独立 Session / Context 隔离)
- 支持 Telegram Bot 实时图文与仪表盘全屏截图推送
"""

import os
import sys
import time
import json
import logging
import datetime
import re
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# 配置日志输出格式
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("NeoHeberg-Renew")

# ==================== 配置项 ====================
_env_tg_token = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_BOT_TOKEN = _env_tg_token if _env_tg_token else "8867499536:AAF2vlfTao3wvy0x7HdlNhZJgfqi5i_vINk"

_env_tg_chat = os.environ.get("TG_CHAT_ID", "").strip()
TG_CHAT_ID = _env_tg_chat if _env_tg_chat else "7772205808"

# 默认账号配置（若环境变量 NEOHEBERG_ACCOUNTS 未指定则使用此默认配置）
DEFAULT_ACCOUNTS = [
    {"username": "yxj0322", "password": "YxJ223512@"}
]

def load_accounts():
    """从环境变量解析账号列表，支持 JSON 或 'user:pass,user2:pass2' 格式"""
    env_accounts = os.environ.get("NEOHEBERG_ACCOUNTS", "").strip()
    if env_accounts:
        try:
            data = json.loads(env_accounts)
            if isinstance(data, list):
                logger.info(f"✅ 成功从环境变量加载了 {len(data)} 个账号 (JSON 格式)")
                return data
        except Exception:
            accounts = []
            for item in env_accounts.split(','):
                item = item.strip()
                if ':' in item:
                    parts = item.split(':', 1)
                    accounts.append({"username": parts[0].strip(), "password": parts[1].strip()})
            if accounts:
                logger.info(f"✅ 成功从环境变量加载了 {len(accounts)} 个账号 (多账号格式)")
                return accounts
    return DEFAULT_ACCOUNTS

def send_tg_message(text):
    """向 Telegram 发送 HTML 格式纯文本汇报"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        logger.info("未配置 TG_BOT_TOKEN / TG_CHAT_ID，跳过 Telegram 发送")
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
            logger.info("📨 Telegram 汇总消息推送成功！")
            return True
        else:
            logger.warning(f"⚠️ Telegram 消息推送失败: {res_json}")
            return False
    except Exception as e:
        logger.error(f"❌ Telegram 消息发送异常: {e}")
        return False

def send_tg_photo(photo_path, caption=""):
    """向 Telegram 发送带截图的图文通知"""
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
                "caption": caption[:1024],
                "parse_mode": "HTML"
            }
            res = requests.post(url, files=files, data=data, timeout=35)
        res_json = res.json()
        if res_json.get("ok"):
            logger.info(f"📸 Telegram 截图推送成功: {os.path.basename(photo_path)}")
            return True
        else:
            logger.warning(f"⚠️ Telegram 图片发送失败: {res_json}，降级发送文本")
            return send_tg_message(caption)
    except Exception as e:
        logger.error(f"❌ Telegram 图片发送异常: {e}")
        return send_tg_message(caption)

def clean_overlays_and_alerts(page):
    """清理页面干扰项、通知弹窗与 Cookie 提示"""
    try:
        page.evaluate("""() => {
            document.querySelectorAll('[data-action="dismiss-alert"], .alert-card button').forEach(b => b.click());
            document.querySelectorAll('[data-action="close-modal"]').forEach(b => b.click());
            const btns = Array.from(document.querySelectorAll('button, a'));
            btns.forEach(b => {
                const text = (b.innerText || '').toLowerCase();
                if (text === 'accepter' || text === 'accept' || text === 'fermer') {
                    b.click();
                }
            });
        }""")
    except Exception:
        pass

def try_pass_cloudflare_turnstile(page, max_wait_sec=25):
    """检测并尝试穿透 Cloudflare 5 秒盾与 Turnstile 验证组件"""
    for _ in range(max_wait_sec):
        title = (page.title() or "").lower()
        content = (page.content() or "").lower()

        # 检查是否处于 Cloudflare 质询状态
        is_cf = (
            "cloudflare" in title
            or "just a moment" in title
            or "vérification de sécurité" in title
            or "challenges.cloudflare.com" in content
            or "cf-turnstile" in content
        )

        if not is_cf:
            # 已经脱离 Cloudflare 盾页面
            return True

        logger.info("🛡️ 正在尝试穿透 Cloudflare Turnstile 验证框...")

        # 1. 尝试在所有 Frame 中寻找 checkbox
        for frame in page.frames:
            try:
                chk = frame.locator("input[type='checkbox'], span.mark, .ctp-checkbox-label, #challenge-stage")
                if chk.count() > 0 and chk.first.is_visible():
                    chk.first.click(timeout=1500)
                    logger.info("👆 已点击 Frame 内的 Turnstile 复选框")
                    time.sleep(2)
                    break
            except Exception:
                pass

        # 2. 模拟鼠标点击 Turnstile iframe 的复选框中心偏左区域
        for sel in ["iframe[src*='challenges.cloudflare.com']", "iframe[src*='turnstile']", "iframe[title*='Cloudflare']"]:
            try:
                cf_frame = page.locator(sel)
                if cf_frame.count() > 0 and cf_frame.first.is_visible():
                    box = cf_frame.first.bounding_box()
                    if box:
                        page.mouse.click(box["x"] + 28, box["y"] + box["height"] / 2)
                        logger.info("👆 模拟鼠标点击 Turnstile 区域")
                        time.sleep(2)
                        break
            except Exception:
                pass

        time.sleep(1)

    return False

def solve_dash_cap_widget(page):
    """
    处理新面板中的 Axel-L Cap-Widget 人机验证 (dash.neoheberg.fr)
    通过触发 solve() 方法与点击 trigger，等待客户端 PoW 计算完成
    """
    logger.info("🔍 检查 Cap-Widget 人机验证状态...")
    try:
        # 1. 尝试直接调用 Web Component 的 solve() 方法
        page.evaluate("""async () => {
            const widget = document.getElementById('cap-login') || document.querySelector('cap-widget');
            if (widget && typeof widget.solve === 'function') {
                try {
                    widget.solve();
                } catch(e) {}
            }
        }""")
        time.sleep(1)

        # 2. 如果存在 trigger 按钮则执行点击
        widget_trigger = page.locator('#cap-login, cap-widget, cap-widget .captcha-trigger, cap-widget [role="button"]')
        if widget_trigger.count() > 0:
            try:
                widget_trigger.first.click(timeout=3000)
                logger.info("👉 已点击 Cap-Widget 验证组件")
            except Exception:
                pass

        # 3. 轮询等待验证完成（最长等待 30 秒）
        logger.info("⏳ 等待 Cap-Widget 验证完成...")
        for _ in range(30):
            token_ready = page.evaluate("""() => {
                const w = document.getElementById('cap-login') || document.querySelector('cap-widget');
                if (!w) return true;
                if (w.token && typeof w.token === 'string' && w.token.length > 0) return true;
                const state = w.getAttribute('data-state');
                if (state === 'done') return true;
                const text = (w.innerText || '') + (w.shadowRoot ? w.shadowRoot.textContent : '');
                if (text.includes('humain') || text.includes('Vérifié') || text.includes('Solved') || text.includes('continuer')) {
                    return true;
                }
                return false;
            }""")
            if token_ready:
                logger.info("✨ Cap-Widget 验证已通过！")
                time.sleep(1)
                return True
            time.sleep(1)
        
        logger.warning("⚠️ Cap-Widget 等待超时，尝试直接提交")
    except Exception as e:
        logger.warning(f"Cap-Widget 处理过程出现提示: {e}")
    return True

def apply_stealth_scripts(context):
    """注入反检测特征脚本，消除 automation controlled 标志"""
    context.add_init_script("""
        // 伪装 navigator.webdriver
        Object.defineProperty(navigator, 'webdriver', {
            get: () => false
        });
        // 伪装语言与插件
        Object.defineProperty(navigator, 'languages', {
            get: () => ['fr-FR', 'fr', 'en-US', 'en']
        });
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5]
        });
        // 伪装 chrome 对象
        window.chrome = {
            runtime: {},
            loadTimes: function() {},
            csi: function() {},
            app: {}
        };
        // 伪装 permissions
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
        );
    """)

def process_single_account(browser, account, index, total):
    """处理单个 NeoHeberg 账号的登录与 VPS 重启任务 (带自动重试机制)"""
    username = account.get("username", "").strip()
    password = account.get("password", "").strip()
    start_time = time.time()

    logger.info("=" * 60)
    logger.info(f"[{index}/{total}] 开始处理账号: {username}")
    logger.info("=" * 60)

    # 独立会话隔离
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        viewport={"width": 1440, "height": 900},
        locale="fr-FR"
    )
    apply_stealth_scripts(context)

    page = context.new_page()

    # 尝试加载 playwright_stealth
    try:
        from playwright_stealth import stealth_sync
        stealth_sync(page)
    except Exception:
        pass

    result_info = {
        "username": username,
        "status": "FAIL",
        "message": "未知原因",
        "duration": 0,
        "screenshot": None,
        "panel_url": ""
    }

    try:
        login_url = "https://dash.neoheberg.fr/login"
        logged_in = False

        # 登录重试循环（支持 Cloudflare 质询穿透与 Cookie 保持重试）
        for attempt in range(1, 4):
            logger.info(f"🌐 [第 {attempt}/3 次尝试] 访问 NeoHeberg 登录页面: {login_url}")
            page.goto(login_url, wait_until="domcontentloaded", timeout=35000)
            time.sleep(2)

            # 检查是否有前置 Cloudflare 盾
            try_pass_cloudflare_turnstile(page, max_wait_sec=15)

            # 检查是否已直接处于已登录页面
            if page.locator(':text("Mes services"), :text("Tableau de bord"), a:has-text("Déconnexion")').count() > 0:
                logger.info("🎉 识别到后台核心组件，当前已处于登录态！")
                logged_in = True
                break

            if "/login" in page.url or page.locator('#identifier, input[name="identifier"]').count() > 0:
                # 步骤 1: 填写用户名/邮箱并点击 Continuer (两步式登录)
                logger.info(f"✍️ [第 1 步] 填写 Identifiant: {username}")
                id_input = page.locator('input#identifier, input[name="identifier"]')
                id_input.wait_for(state="visible", timeout=15000)
                id_input.fill(username)
                time.sleep(0.5)

                continue_btn = page.locator('button#goToPassword, button:has-text("Continuer")')
                if continue_btn.count() > 0 and continue_btn.first.is_visible():
                    logger.info("👉 点击 'Continuer' 进入密码输入步骤...")
                    continue_btn.first.click()
                else:
                    id_input.press("Enter")

                # 等待步骤 2 密码区域显示
                time.sleep(1)
                pw_input = page.locator('input#password, input[name="password"]')
                pw_input.wait_for(state="visible", timeout=10000)

                # 步骤 2: 填写密码并处理验证码
                logger.info("🔒 [第 2 步] 填写密码...")
                pw_input.fill(password)
                time.sleep(0.5)

                # 处理人机验证
                solve_dash_cap_widget(page)

                # 点击登录提交按钮
                logger.info("🚀 提交登录表单 (Se connecter)...")
                submit_btn = page.locator('form.login-form button[type="submit"], button:has-text("Se connecter")')
                submit_btn.first.click()

                # 提交后可能触发 Cloudflare Turnstile 质询盾，给予检测与穿透
                logger.info("⏳ 表单已提交，监测登录跳转与 Cloudflare 质询状态...")
                for w in range(35):
                    time.sleep(1)

                    # 1. 检测是否已成功登录（出现 Mes services、Tableau de bord、Déconnexion）
                    if page.locator(':text("Mes services"), :text("Tableau de bord"), a:has-text("Déconnexion"), [data-action="logout"]').count() > 0:
                        logger.info(f"🎉 登录成功！当前控制台页面: {page.url}")
                        logged_in = True
                        break

                    # 2. 检测并穿透 Cloudflare Turnstile
                    try_pass_cloudflare_turnstile(page, max_wait_sec=3)

                    # 3. 检测是否有明显的错误提示
                    err_loc = page.locator('#identifierError, #loginCaptchaError, .text-red-400')
                    if err_loc.count() > 0 and err_loc.first.is_visible():
                        err_text = err_loc.first.text_content().strip()
                        if err_text:
                            logger.warning(f"⚠️ 登录提示: {err_text}")

                if logged_in:
                    break

            time.sleep(2)

        if not logged_in:
            err_text = "登录未能成功跳转到后台控制台"
            logger.error(f"❌ 账号 {username} 最终登录失败")
            fail_shot = f"login_fail_{username}.png"
            page.screenshot(path=fail_shot, full_page=True)
            result_info["message"] = err_text
            result_info["screenshot"] = fail_shot
            return result_info

        # 登录成功，进入主界面后休眠等待动态内容加载
        time.sleep(3)
        clean_overlays_and_alerts(page)
        result_info["panel_url"] = page.url

        # ==================== 执行 VPS 重启操作 ====================
        logger.info("🔍 开始定位 VPS 状态与 'Redémarrer' (重启) 按钮...")

        reboot_clicked = False

        # 方式 1: 直接在当前页面（Tableau de bord / Mes services）寻找 Redémarrer 按钮
        reboot_locators = page.locator('button, a').filter(has_text=re.compile(r'Red[eé]marrer', re.I))
        if reboot_locators.count() > 0 and reboot_locators.first.is_visible():
            logger.info("🎯 在主面板服务列表中直接找到 'Redémarrer' 重启按钮，准备点击...")
            reboot_locators.first.click()
            reboot_clicked = True
        else:
            # 方式 2: 点击 'Gérer le VPS' 进入 VPS 专属管理面板
            logger.info("ℹ️ 主界面未直接暴露重启按钮，正在尝试进入 VPS 详情面板...")
            gerer_btn = page.locator('a:has-text("Gérer le VPS"), button:has-text("Gérer le VPS"), a:has-text("Gérer"), a:has-text("Gerer"), [href*="/vps"], [href*="/services"]')
            if gerer_btn.count() > 0 and gerer_btn.first.is_visible():
                logger.info("👉 点击 'Gérer le VPS' 进入详情面板...")
                gerer_btn.first.click()
                page.wait_for_load_state("domcontentloaded", timeout=20000)
                time.sleep(3)
                clean_overlays_and_alerts(page)
                result_info["panel_url"] = page.url
                logger.info(f"📌 已进入详情页: {page.url}")

                # 在详情页中再次寻找 ACTIONS 里的 Redémarrer
                reboot_locators_inner = page.locator('button, a').filter(has_text=re.compile(r'Red[eé]marrer', re.I))
                if reboot_locators_inner.count() > 0:
                    logger.info("🎯 在 VPS 详情页中找到 'Redémarrer' 按钮，点击重启...")
                    reboot_locators_inner.first.click()
                    reboot_clicked = True

        if not reboot_clicked:
            logger.error("❌ 未能在页面上找到任何可点击的 'Redémarrer' 按钮")
            no_btn_shot = f"no_reboot_btn_{username}.png"
            page.screenshot(path=no_btn_shot, full_page=True)
            result_info["message"] = "未找到 Redémarrer 重启按钮"
            result_info["screenshot"] = no_btn_shot
            return result_info

        time.sleep(1.5)

        # 检查是否存在二次确认弹窗 (Confirmer / Valider / Oui / Yes)
        logger.info("👀 检查是否存在重启二次确认弹窗...")
        confirm_btn = page.locator('.modal button, [role="dialog"] button, div[class*="modal"] button, button').filter(
            has_text=re.compile(r'^(Confirmer|Valider|Oui|Yes|Confirm|Red[eé]marrer)$', re.I)
        )
        if confirm_btn.count() > 0 and confirm_btn.first.is_visible():
            logger.info("⚠️ 检测到确认弹窗，点击确认重启...")
            try:
                confirm_btn.first.click(timeout=3000)
            except Exception:
                pass
            time.sleep(2)

        # 等待重启指令下发与页面状态响应
        time.sleep(5)
        clean_overlays_and_alerts(page)

        # 截取重启成功操作凭证截图
        success_shot = f"reboot_success_{username}.png"
        page.screenshot(path=success_shot, full_page=True)
        logger.info(f"✅ 账号 {username} 的 VPS 已成功触发重启！截图已保存: {success_shot}")

        result_info["status"] = "SUCCESS"
        result_info["message"] = "服务器已成功下发 Redémarrer 重启指令"
        result_info["screenshot"] = success_shot
        return result_info

    except Exception as e:
        logger.error(f"❌ 处理账号 {username} 发生异常: {e}")
        err_shot = f"error_{username}.png"
        try:
            page.screenshot(path=err_shot, full_page=True)
            result_info["screenshot"] = err_shot
        except Exception:
            pass
        result_info["message"] = str(e)
        return result_info

    finally:
        result_info["duration"] = round(time.time() - start_time, 1)
        try:
            context.close()
        except Exception:
            pass

def main():
    accounts = load_accounts()
    if not accounts:
        logger.error("❌ 未配置任何可用账号，请检查环境！")
        sys.exit(1)

    logger.info(f"🚀 开始执行 NeoHeberg 自动保活重启任务，共加载 {len(accounts)} 个账号")
    headless = os.environ.get("HEADLESS", "true").lower() != "false"

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1440,900"
            ]
        )

        for i, acc in enumerate(accounts, 1):
            res = process_single_account(browser, acc, i, len(accounts))
            results.append(res)
            if i < len(accounts):
                time.sleep(5)

        browser.close()

    success_list = [r for r in results if r["status"] == "SUCCESS"]
    fail_list = [r for r in results if r["status"] != "SUCCESS"]

    logger.info("=" * 60)
    logger.info(f"📊 任务执行完毕: 成功 {len(success_list)} 个, 失败 {len(fail_list)} 个")
    logger.info("=" * 60)

    # 发送 Telegram 结果报告
    now_str = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")

    # 1. 逐个推送带图报告
    for r in results:
        if r.get("screenshot") and os.path.exists(r["screenshot"]):
            badge = "✅ 重启成功 (已保活)" if r["status"] == "SUCCESS" else f"❌ 失败: {r['message']}"
            caption = (
                f"🚀 <b>NeoHeberg VPS 自动保活报告</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>账号</b>: <code>{r['username']}</code>\n"
                f"📊 <b>状态</b>: {badge}\n"
                f"⏱ <b>耗时</b>: {r['duration']} 秒\n"
                f"⏰ <b>时间</b>: {now_str} (北京时间)\n"
            )
            if r.get("panel_url"):
                caption += f"🔗 <b>面板</b>: <code>{r['panel_url']}</code>\n"
            send_tg_photo(r["screenshot"], caption)
            time.sleep(1)

    # 2. 推送总汇总消息
    status_icon = "🎉" if len(fail_list) == 0 else "⚠️"
    summary_lines = [
        f"{status_icon} <b>NeoHeberg VPS 自动保活总运行报告</b>",
        "━━━━━━━━━━━━━━━━━━"
    ]
    for r in results:
        tag = "✅ 重启成功" if r["status"] == "SUCCESS" else f"❌ {r['message']}"
        summary_lines.append(f"• 👤 <code>{r['username']}</code>: {tag} ({r['duration']}s)")
    summary_lines.append("──────────────────")
    summary_lines.append(f"📈 <b>总计</b>: 成功 {len(success_list)} / 失败 {len(fail_list)}")
    summary_lines.append(f"⏰ <b>完成时间</b>: {now_str}")

    send_tg_message("\n".join(summary_lines))

    if len(fail_list) > 0 and len(success_list) == 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
