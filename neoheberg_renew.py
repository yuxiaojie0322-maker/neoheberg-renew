#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NeoHeberg VPS 自动登录与重启保活脚本 (支持多账号 + 节点代理 + Cloudflare Turnstile 穿透 + Telegram 推送)
- 登录: https://extranet.neoheberg.fr/login
- 自动启动 Sing-box 转发 Hysteria2 / Vless / Socks5 / HTTP 节点，彻底规避机房 IP 风控
- 自动检测并穿透 Cloudflare Turnstile / Managed Challenge (5秒盾)
- 自动处理 Cap-Widget 验证
- 查找 VPS 并点击 "Gerer" (管理)
- 找到 ACTIONS 中的 "Redémarrer" (重启) 并执行
- 支持多账号轮询与 GitHub Actions 定时运行
- 支持 Telegram Bot 运行结果与实时截图消息推送
"""

import os
import re
import sys
import time
import json
import logging
import datetime
import urllib.parse
import subprocess
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
# Telegram 推送配置 (优先读取环境变量，为空时自动回退默认配置)
_env_tg_token = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_BOT_TOKEN = _env_tg_token if _env_tg_token else "8867499536:AAF2vlfTao3wvy0x7HdlNhZJgfqi5i_vINk"

_env_tg_chat = os.environ.get("TG_CHAT_ID", "").strip()
TG_CHAT_ID = _env_tg_chat if _env_tg_chat else "7772205808"

# 节点代理配置 (支持 Hysteria2 / Vless / Socks5 / HTTP)
_env_proxy_node = os.environ.get("PROXY_NODE", "").strip()
_env_proxy_url = os.environ.get("PROXY_URL", "").strip()
DEFAULT_PROXY_NODE = _env_proxy_node if _env_proxy_node else (_env_proxy_url if _env_proxy_url else "hysteria2://031e1b07-ac55-476d-a415-4b3c5fc411f5@83.168.94.238:30005?sni=www.bing.com&insecure=1&alpn=h3#PL-HY2-2")

# 账号列表配置 (支持多账号批量轮询)
DEFAULT_ACCOUNTS = [
    {"username": "yxj0322", "password": "YxJ223512@"},
    {"username": "xiaojieyu44m", "password": "YxJ223512@"},
    {"username": "xy137494", "password": "YxJ223512@"},
]

# Anti-Detection Stealth Script (抹除自动化指纹)
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined
});

if (!window.chrome) {
    window.chrome = {};
}
window.chrome.runtime = window.chrome.runtime || {
    PlatformOs: { MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' },
    PlatformArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' },
    PlatformNaclArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' }
};

Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5]
});

Object.defineProperty(navigator, 'languages', {
    get: () => ['fr-FR', 'fr', 'en-US', 'en']
});

const origQuery = window.navigator.permissions ? window.navigator.permissions.query : null;
if (origQuery) {
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ?
            Promise.resolve({ state: Notification.permission }) :
            origQuery(parameters)
    );
}
"""

def parse_proxy_link(link):
    """解析节点链接 (支持 direct HTTP/SOCKS5 与 Hysteria2)"""
    link = re.sub(r'\[sni=([^\]]+)\]\([^\)]+\)', r'sni=\1', link)
    link = link.strip()
    if not link:
        return None
        
    if link.startswith("http://") or link.startswith("https://") or link.startswith("socks5://"):
        return {"type": "direct_proxy", "url": link}
        
    if link.startswith("hysteria2://") or link.startswith("hy2://"):
        u = urllib.parse.urlparse(link)
        netloc = u.netloc
        if "@" in netloc:
            auth, host_port = netloc.split("@", 1)
        else:
            auth = ""
            host_port = netloc
            
        if ":" in host_port:
            server, port = host_port.split(":")
            port = int(port)
        else:
            server = host_port
            port = 443
            
        params = urllib.parse.parse_qs(u.query)
        sni = params.get("sni", [""])[0] or server
        insecure = params.get("insecure", ["0"])[0] in ["1", "true", "True"]
        alpn = params.get("alpn", ["h3"])
        if isinstance(alpn, str):
            alpn = [alpn]
            
        return {
            "type": "hysteria2",
            "server": server,
            "port": port,
            "auth": auth,
            "sni": sni,
            "insecure": insecure,
            "alpn": alpn
        }
    return None

def start_proxy(proxy_node_str, listen_port=10808):
    """启动本地 sing-box 转发并将本地 SOCKS5/HTTP 代理返回给 Playwright"""
    if not proxy_node_str:
        return None
        
    parsed = parse_proxy_link(proxy_node_str)
    if not parsed:
        logger.warning(f"无法识别代理节点链接: {proxy_node_str[:25]}...")
        return None
        
    if parsed["type"] == "direct_proxy":
        logger.info(f"使用直接代理: {parsed['url']}")
        return parsed["url"]
        
    logger.info(f"检测到 {parsed['type']} 节点，正在配置 sing-box 本地转发 (端口 {listen_port})...")
    
    # 针对 Linux 环境自动下载 sing-box 客户端
    singbox_bin = "./sing-box"
    if sys.platform.startswith("linux"):
        if not os.path.exists(singbox_bin):
            logger.info("正在下载 sing-box 官方二进制...")
            try:
                url = "https://github.com/SagerNet/sing-box/releases/download/v1.9.3/sing-box-1.9.3-linux-amd64.tar.gz"
                subprocess.run(["curl", "-sLo", "sing-box.tar.gz", url], check=True)
                subprocess.run(["tar", "-xzf", "sing-box.tar.gz", "--strip-components=1"], check=True)
                subprocess.run(["chmod", "+x", singbox_bin], check=True)
                logger.info("sing-box 安装完成")
            except Exception as e:
                logger.error(f"下载 sing-box 失败: {e}")
                return None
    else:
        singbox_bin = "sing-box.exe"
        if not os.path.exists(singbox_bin):
            logger.info("非 Linux 环境且未找到本地 sing-box.exe，尝试直接连接")
            return None

    # 生成 sing-box 客户端配置
    config = {
        "log": {"level": "warn"},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": listen_port
            }
        ],
        "outbounds": [
            {
                "type": "hysteria2",
                "tag": "hy2-out",
                "server": parsed["server"],
                "server_port": parsed["port"],
                "password": parsed["auth"],
                "tls": {
                    "enabled": True,
                    "server_name": parsed["sni"],
                    "insecure": parsed["insecure"],
                    "alpn": parsed["alpn"]
                }
            }
        ]
    }
    
    config_file = "singbox_proxy_config.json"
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        
    try:
        logger.info(f"启动 sing-box 转发服务...")
        proc = subprocess.Popen([singbox_bin, "run", "-c", config_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        
        # 测试出口 IP
        local_http_proxy = f"http://127.0.0.1:{listen_port}"
        try:
            res = requests.get("https://api.ipify.org?format=json", proxies={"http": local_http_proxy, "https": local_http_proxy}, timeout=10)
            out_ip = res.json().get("ip")
            logger.info(f"✅ 节点代理转发启动成功！代理出口 IP: {out_ip}")
        except Exception as e:
            logger.warning(f"代理测试请求未返回 (可能由于测试超时): {e}，仍使用本地代理")
            
        return f"socks5://127.0.0.1:{listen_port}"
    except Exception as e:
        logger.error(f"启动 sing-box 异常: {e}")
        return None

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
            logger.info("Telegram 汇总推送成功！")
            return True
        else:
            logger.warning(f"Telegram 推送返回失败: {res_json}")
            return False
    except Exception as e:
        logger.error(f"Telegram 发送异常: {e}")
        return False

def send_tg_photo(photo_path, caption=""):
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
                "caption": caption[:1024],
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

def is_cf_challenge_present(page):
    """检测当前页面是否处于 Cloudflare 质询 / 5秒盾页面"""
    try:
        title = page.title()
        content = page.content()
        cf_keywords = [
            "Vérification de sécurité en cours",
            "Vérifiez que vous êtes humain",
            "challenges.cloudflare.com",
            "Just a moment...",
            "Attention Required! | Cloudflare",
            "Checking your browser"
        ]
        for kw in cf_keywords:
            if kw in title or kw in content:
                return True
        for frame in page.frames:
            if frame != page.main_frame:
                return True
    except Exception:
        pass
    return False

def handle_cloudflare_challenge(page, timeout=30):
    """检测并尝试穿透 Cloudflare Turnstile / Managed Challenge 人机质询"""
    if not is_cf_challenge_present(page):
        return True

    logger.info("检测到 Cloudflare Turnstile / 5 秒盾质询，正在自动尝试穿透...")
    start_time = time.time()

    while time.time() - start_time < timeout:
        if not is_cf_challenge_present(page):
            logger.info("Cloudflare 安全质询已通过！")
            time.sleep(2)
            return True

        clicked = False
        # 1. 遍历所有 subframes 尝试点击复选框
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                checkbox = frame.locator('input[type="checkbox"], .ctp-checkbox-label, #challenge-stage')
                if checkbox.count() > 0 and checkbox.first.is_visible():
                    box = checkbox.first.bounding_box()
                    if box and box["width"] > 0 and box["height"] > 0:
                        logger.info(f"在框架内定位到复选框，正在模拟点击...")
                        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=5)
                        time.sleep(0.3)
                        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        clicked = True
                        time.sleep(3)
                        break
            except Exception as e:
                logger.debug(f"遍历 frame 异常: {e}")

        # 2. 定位主页面上的 iframe 元素并点击复选框位置
        if not clicked:
            try:
                iframes = page.locator('iframe').all()
                for ifr in iframes:
                    if ifr.is_visible():
                        box = ifr.bounding_box()
                        if box and box["width"] > 0 and box["height"] > 0:
                            click_x = box["x"] + 30
                            click_y = box["y"] + (box["height"] / 2)
                            logger.info(f"模拟点击 iframe 复选框区域 ({round(click_x, 1)}, {round(click_y, 1)})...")
                            page.mouse.move(click_x, click_y, steps=5)
                            time.sleep(0.3)
                            page.mouse.click(click_x, click_y)
                            clicked = True
                            time.sleep(3)
                            break
            except Exception as e:
                logger.debug(f"点击 iframe 容器异常: {e}")

        time.sleep(1)

    if not is_cf_challenge_present(page):
        logger.info("Cloudflare 安全质询已成功穿透！")
        return True

    logger.warning("Cloudflare 验证处理已达最大等待时间，尝试继续后续操作...")
    return False

def handle_gdpr_consent(page):
    """自动关闭欧洲 GDPR Cookie 授权弹窗 (Funding Choices)"""
    try:
        consent_modal = page.locator('.fc-consent-root, div[class*="fc-consent"]')
        if consent_modal.count() > 0 and consent_modal.first.is_visible(timeout=2000):
            logger.info("检测到 GDPR Cookie 授权弹窗，正在自动点击授权...")
            btn = page.locator('.fc-consent-root button.fc-primary-button, .fc-consent-root button:has-text("Consent"), .fc-consent-root button:has-text("Autoriser"), .fc-consent-root button:has-text("Accepter"), .fc-consent-root .fc-button-label')
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(timeout=3000, force=True)
                time.sleep(1)
    except Exception as e:
        logger.debug(f"处理 GDPR 弹窗异常: {e}")

def handle_migration_modal(page):
    """
    自动检测并穿透/关闭 NeoHeberg 'Extranet en cours de migration' 迁移提醒弹窗。
    NeoHeberg 官方迁移提示弹窗包含:
    1. 'Ouvrir le tableau de bord' (蓝底按钮，引导前往新面板 dash.neoheberg.fr)
    2. 'Continuer sur l\'extranet' (深色按钮，留在旧版 Extranet 继续管理机器)
    脚本优先点击 'Continuer sur l\'extranet'，并强力清理阻断操作的全屏半透明蒙层。
    """
    try:
        migration_indicators = [
            "Extranet en cours de migration",
            "Vos services vous attendent sur le tableau de bord",
            "Continuer sur l'extranet",
            "Continuer sur l’extranet",
            "dash.neoheberg.fr"
        ]

        has_modal = False
        try:
            page_text = page.evaluate("() => document.body ? document.body.innerText : ''")
            if any(kw.lower() in page_text.lower() for kw in migration_indicators):
                has_modal = True
        except Exception:
            pass

        if not has_modal:
            # 双重检查是否存在对应按钮
            btn_check = page.locator('button, a').filter(has_text=re.compile(r"Continuer sur l['’]extranet", re.I))
            if btn_check.count() > 0:
                has_modal = True

        if not has_modal:
            return False

        logger.info("📢 检测到 NeoHeberg 新面板迁移提示弹窗 (Extranet en cours de migration)！")

        # 方式 1: 点击 'Continuer sur l'extranet' 按钮
        continue_btn = page.locator('button, a').filter(has_text=re.compile(r"Continuer sur l['’]extranet", re.I))
        if continue_btn.count() > 0:
            logger.info("定位到 'Continuer sur l\'extranet' 按钮，正在模拟点击关闭弹窗...")
            try:
                continue_btn.first.click(timeout=3000)
            except Exception:
                continue_btn.first.click(force=True)
            time.sleep(1.5)

        # 方式 2: 按 Escape 键尝试关闭模态框
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

        # 方式 3: 强力移除残留 DOM 弹窗元素与全屏半透明遮罩层 (Backdrop)，彻底恢复页面交互能力
        page.evaluate("""() => {
            const modalKeywords = ['Extranet en cours de migration', 'dash.neoheberg.fr', 'Continuer sur'];
            document.querySelectorAll('div, section, aside, [role="dialog"]').forEach(el => {
                const txt = el.textContent || '';
                if (modalKeywords.some(k => txt.includes(k))) {
                    let target = el;
                    while (target && target.parentElement && target.parentElement !== document.body) {
                        const style = window.getComputedStyle(target);
                        if (style.position === 'fixed' || style.position === 'absolute' || target.getAttribute('role') === 'dialog') {
                            break;
                        }
                        target = target.parentElement;
                    }
                    if (target && target.parentNode) {
                        target.parentNode.removeChild(target);
                    }
                }
            });

            // 移除可能阻碍点击事件的全屏遮罩
            document.querySelectorAll('div').forEach(el => {
                const s = window.getComputedStyle(el);
                if (s.position === 'fixed' && (parseInt(s.zIndex, 10) >= 30 || s.backgroundColor.includes('rgba'))) {
                    if (el.children.length === 0 || el.innerText.trim() === '') {
                        el.remove();
                    }
                }
            });

            // 恢复页面滚动条与点击指针
            document.body.style.overflow = 'auto';
            document.body.style.pointerEvents = 'auto';
            if (document.documentElement) {
                document.documentElement.style.overflow = 'auto';
                document.documentElement.style.pointerEvents = 'auto';
            }
        }""")
        time.sleep(0.5)
        logger.info("✅ 迁移提示弹窗已自动关闭并清理，页面交互已恢复正常！")
        return True
    except Exception as e:
        logger.warning(f"处理迁移提示弹窗异常: {e}")
        return False

def dismiss_all_popups(page):
    """统一关闭页面上可能弹出的所有干扰层 (迁移弹窗、Cookie 授权等)"""
    handle_migration_modal(page)
    handle_gdpr_consent(page)

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

def process_single_account(browser, account, index, total, proxy_server=None):
    username = account.get("username", "").strip()
    password = account.get("password", "").strip()
    start_time = time.time()
    logger.info(f"==================================================")
    logger.info(f"[{index}/{total}] 开始处理账号: {username}")
    logger.info(f"==================================================")

    proxy_cfg = {"server": proxy_server} if proxy_server else None

    # 每一个账号使用独立的上下文环境，隔离 Cookies 和 Cache，并注入防指纹脚本
    context = browser.new_context(
        proxy=proxy_cfg,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        viewport={"width": 1366, "height": 768},
        locale="fr-FR",
        timezone_id="Europe/Paris"
    )
    context.add_init_script(STEALTH_JS)

    page = context.new_page()
    result_info = {
        "username": username,
        "status": "FAIL",
        "message": "未知原因",
        "duration": 0,
        "screenshot": None,
        "panel_url": ""
    }

    try:
        login_url = "https://extranet.neoheberg.fr/login"
        logger.info(f"正在访问登录页面: {login_url}")
        page.goto(login_url, wait_until="domcontentloaded", timeout=35000)
        time.sleep(2)

        # 检查初始进入是否有 CF 质询与 GDPR
        handle_cloudflare_challenge(page, timeout=25)
        handle_gdpr_consent(page)

        # 支持登录重试（若穿透 CF 后页面刷新，自动进行第二轮填写提交）
        login_success = False
        for attempt in range(1, 3):
            if "/login" not in page.url:
                login_success = True
                break

            logger.info(f"[第 {attempt}/2 次] 准备输入用户名与密码...")
            try:
                page.wait_for_selector('input#email, input[name="email"]', timeout=12000)
            except PlaywrightTimeout:
                if is_cf_challenge_present(page):
                    handle_cloudflare_challenge(page, timeout=25)
                    time.sleep(2)

            email_input = page.locator('input#email, input[name="email"]')
            if email_input.count() > 0 and email_input.first.is_visible():
                logger.info(f"输入用户名: {username}")
                email_input.first.fill(username)
                time.sleep(0.5)

            pass_input = page.locator('input#password, input[name="password"]')
            if pass_input.count() > 0 and pass_input.first.is_visible():
                logger.info("输入密码...")
                pass_input.first.fill(password)
                time.sleep(0.5)

            # 触发 Cap-Widget 验证
            handle_cap_widget(page)

            logger.info("点击登录按钮 (Se connecter)...")
            submit_btn = page.locator('button[type="submit"]:has-text("Se connecter"), button[type="submit"]')
            if submit_btn.count() > 0:
                submit_btn.first.click()

            time.sleep(3)
            # 点击登录后，如果遇到 Cloudflare 质询拦截，执行穿透处理
            if is_cf_challenge_present(page):
                logger.info("登录提交后触发 Cloudflare 质询，正在执行自动穿透...")
                handle_cloudflare_challenge(page, timeout=25)

            # 等待登录成功跳转
            try:
                page.wait_for_url(lambda u: "/login" not in u, timeout=25000)
                logger.info(f"登录成功！当前页面: {page.url}")
                login_success = True
                break
            except PlaywrightTimeout:
                logger.warning(f"第 {attempt} 次提交未立即跳转，检查页面状态...")
                if is_cf_challenge_present(page):
                    handle_cloudflare_challenge(page, timeout=20)

                if "/login" not in page.url:
                    logger.info(f"登录成功！当前页面: {page.url}")
                    login_success = True
                    break

                # 如果页面刷新重新出现了登录输入框，则在下一轮循环中自动重填提交
                if page.locator('input#email, input[name="email"]').count() > 0:
                    logger.info("检测到登录页面已刷新（可能已获取 CF clearance），进行重试...")
                    time.sleep(1)
                    continue

        if not login_success:
            error_msg = page.locator('.text-red-500, .alert-danger, [role="alert"]').text_content(timeout=3000) if page.locator('.text-red-500, .alert-danger').count() > 0 else "登录超时未跳转 (仍停留在登录或人机验证页)"
            logger.error(f"登录失败: {error_msg.strip()}")
            fail_shot = f"login_fail_{username}.png"
            page.screenshot(path=fail_shot)
            result_info["message"] = f"登录失败: {error_msg.strip()}"
            result_info["screenshot"] = fail_shot
            return result_info

        time.sleep(2)
        dismiss_all_popups(page)

        # 寻找 VPS 管理入口 (Gerer / Gérer)
        logger.info("正在查找 VPS 列表与管理入口 (Gerer / Gérer)...")
        dismiss_all_popups(page)

        # 1. 优先尝试点击 VPS 分类 Tab (若页面以分类标签展示服务)
        vps_tab = page.locator('button, a, div[role="tab"]').filter(has_text=re.compile(r'^\s*VPS\s*$', re.I))
        if vps_tab.count() == 0:
            vps_tab = page.locator('button:has-text("VPS"), a:has-text("VPS")')
        if vps_tab.count() > 0:
            try:
                first_tab = vps_tab.first
                if first_tab.is_visible():
                    first_tab.click(timeout=3000)
                    time.sleep(1.5)
                    dismiss_all_popups(page)
            except Exception as e:
                logger.debug(f"切换 VPS 标签异常: {e}")

        # 2. 智能定位目标 VPS 的管理按钮或独立面板链接
        def get_vps_target(p):
            # 查找具体包含 VPS 标识的链接 (必须排除单纯指向列表页的 /vps 或 /vps/)
            candidates = p.locator('a[href*="/vps/"], a[href*="/vps?"]').all()
            for cand in candidates:
                href = cand.get_attribute("href") or ""
                clean_href = href.split("?")[0].rstrip("/")
                if clean_href and not clean_href.endswith("/vps"):
                    return cand

            # 查找包含 "Gérer" / "Gerer" 的按钮或超链接
            gerer_loc = p.locator('a:has-text("Gérer"), button:has-text("Gérer"), a:has-text("Gerer"), button:has-text("Gerer")')
            if gerer_loc.count() > 0:
                return gerer_loc.first

            # 查找操作列设置图标
            cog = p.locator('.fa-cog, .fa-gear').locator('..')
            if cog.count() > 0:
                return cog.first

            return None

        gerer_btn = get_vps_target(page)

        # 若主页未直接找到且尚未在 /vps 页面，尝试直接跳转至 VPS 列表页
        if not gerer_btn and not page.url.rstrip("/").endswith("/vps"):
            logger.info("主页未直接发现 VPS 实例管理按钮，尝试直接进入 VPS 列表页: https://extranet.neoheberg.fr/vps ...")
            try:
                page.goto("https://extranet.neoheberg.fr/vps", wait_until="domcontentloaded", timeout=25000)
                time.sleep(2)
                dismiss_all_popups(page)
                gerer_btn = get_vps_target(page)
            except Exception as e:
                logger.debug(f"直接跳转 /vps 页面异常: {e}")

        if not gerer_btn:
            logger.error("未找到任何可用 VPS 的管理入口 (Gerer)")
            dismiss_all_popups(page)
            shot = f"no_vps_{username}.png"
            page.screenshot(path=shot)
            result_info["message"] = "未找到 VPS 管理按钮 (页面可能已被迁移弹窗遮挡或无有效机器)"
            result_info["screenshot"] = shot
            return result_info

        # 提取 href 直接导航进入 VPS 详情控制台
        href = gerer_btn.get_attribute("href")
        if href and (href.startswith("/") or href.startswith("http")):
            if href.startswith("/"):
                panel_url = f"https://extranet.neoheberg.fr{href}"
            else:
                panel_url = href
            logger.info(f"提取到 VPS 管理面板入口: {panel_url}，正在进入...")
            page.goto(panel_url, wait_until="domcontentloaded", timeout=30000)
        else:
            logger.info("找到管理入口，正在点击进入 VPS 控制面板...")
            gerer_btn.click()
            page.wait_for_load_state("domcontentloaded", timeout=20000)

        time.sleep(2)
        dismiss_all_popups(page)
        panel_url = page.url
        result_info["panel_url"] = panel_url
        logger.info(f"已成功进入 VPS 管理详情页: {panel_url}")

        # 查找 ACTIONS 区域中的 "Redémarrer" (重启) 按钮
        logger.info("正在查找 VPS 详情页中的 'Redémarrer' (重启) 按钮...")
        dismiss_all_popups(page)

        # 优先通过文字精准匹配重启操作 (避免误点表格自带的刷新列表按钮)
        reboot_btn = page.locator('button, a').filter(has_text=re.compile(r'^\s*(Redémarrer|Redemarrer|Reboot)\s*$', re.I))
        if reboot_btn.count() == 0:
            reboot_btn = page.locator('button:has-text("Redémarrer"), a:has-text("Redémarrer"), button:has-text("Redemarrer"), a:has-text("Redemarrer"), [title*="Redémarrer" i], [title*="Redemarrer" i]')
        if reboot_btn.count() == 0:
            # 仅在明确标有 ACTIONS 或电源控制的作用域内匹配重启图标
            reboot_btn = page.locator('.actions button:has(.fa-sync), .actions button:has(.fa-redo), div:has-text("Actions") button:has(.fa-sync), div:has-text("ACTIONS") button:has(.fa-redo)')

        if reboot_btn.count() == 0:
            logger.error("未找到 'Redémarrer' 重启按钮，请确认页面是否已成功进入 VPS 控制台")
            dismiss_all_popups(page)
            shot = f"no_reboot_{username}.png"
            page.screenshot(path=shot)
            result_info["message"] = "未找到 Redémarrer 重启按钮"
            result_info["screenshot"] = shot
            return result_info

        logger.info("找到重启按钮，正在滚动至可视区域并点击...")
        dismiss_all_popups(page)
        try:
            reboot_btn.first.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        reboot_btn.first.click()
        time.sleep(1.5)

        # 处理二次确认弹窗 (Confirmer / Valider / Oui / Yes / Confirm)
        confirm_btn = page.locator('button, a').filter(has_text=re.compile(r'^\s*(Confirmer|Valider|Oui|Yes|Confirm)\s*$', re.I))
        if confirm_btn.count() == 0:
            confirm_btn = page.locator('.modal button:has-text("Confirmer"), .modal button:has-text("Redémarrer"), [role="dialog"] button:has-text("Confirmer"), [role="dialog"] button:has-text("Oui")')
        if confirm_btn.count() > 0 and confirm_btn.first.is_visible():
            logger.info("检测到二次确认弹窗，正在点击确认重启...")
            confirm_btn.first.click()
            time.sleep(2)

        # 再次确保弹窗与遮罩层被彻底清除，保证截图呈现真实控制台状态
        dismiss_all_popups(page)
        time.sleep(2)

        success_shot = f"reboot_success_{username}.png"
        page.screenshot(path=success_shot)
        logger.info(f"✅ 账号 {username} 的 VPS 重启指令已成功执行！真实控制台截图保存至: {success_shot}")

        result_info["status"] = "SUCCESS"
        result_info["message"] = "VPS 重启指令下发成功"
        result_info["screenshot"] = success_shot
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
    
    # 启动节点代理转发
    proxy_server = start_proxy(DEFAULT_PROXY_NODE)
    if proxy_server:
        logger.info(f"使用代理服务: {proxy_server}")
    else:
        logger.info("未启用代理服务，将使用直连网络")

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
                "--window-size=1366,768"
            ]
        )

        for i, acc in enumerate(accounts, 1):
            res = process_single_account(browser, acc, i, len(accounts), proxy_server=proxy_server)
            results.append(res)
            if i < len(accounts):
                time.sleep(5)  # 账号之间间隔 5 秒，避免风控

        browser.close()

    success_list = [r for r in results if r["status"] == "SUCCESS"]
    fail_list = [r for r in results if r["status"] != "SUCCESS"]

    logger.info("==================================================")
    logger.info(f"任务执行结束: 成功 {len(success_list)} 个, 失败 {len(fail_list)} 个")
    logger.info("==================================================")

    # 构造 Telegram 推送通知
    now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. 逐个账号发送其实时执行截图
    for r in results:
        if r.get("screenshot") and os.path.exists(r["screenshot"]):
            status_tag = "✅ 重启成功" if r["status"] == "SUCCESS" else f"❌ 失败: {r['message']}"
            shot_caption = (
                f"📸 <b>NeoHeberg VPS 执行截图</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>账号</b>: <code>{r['username']}</code>\n"
                f"📊 <b>状态</b>: {status_tag}\n"
                f"⏱ <b>耗时</b>: {r['duration']}s\n"
                f"⏰ <b>时间</b>: {now_time}"
            )
            if r.get("panel_url"):
                shot_caption += f"\n🔗 <b>面板</b>: {r['panel_url']}"
            logger.info(f"正在推送账号 {r['username']} 的截图到 Telegram...")
            send_tg_photo(r["screenshot"], shot_caption)
            time.sleep(1)

    # 2. 发送最终总体运行报表
    status_emoji = "🎉" if len(fail_list) == 0 else "⚠️"
    msg_lines = [
        f"{status_emoji} <b>NeoHeberg VPS 自动保活总汇报</b>",
        "━━━━━━━━━━━━━━━━━━"
    ]
    for r in results:
        badge = "✅ 重启成功" if r["status"] == "SUCCESS" else f"❌ {r['message']}"
        msg_lines.append(f"👤 <code>{r['username']}</code>: {badge} ({r['duration']}s)")
    msg_lines.append("──────────────────")
    msg_lines.append(f"📈 <b>汇总</b>: 成功 {len(success_list)} 个 | 失败 {len(fail_list)} 个")
    msg_lines.append(f"⏰ <b>完成时间</b>: {now_time}")
    msg_lines.append("💡 <i>提示: 官方正推进新面板迁移 (dash.neoheberg.fr)，脚本已自动穿透弹窗确保旧 Extranet 机器正常重启保活</i>")

    tg_summary = "\n".join(msg_lines)
    send_tg_message(tg_summary)

    if len(fail_list) > 0 and len(success_list) == 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
