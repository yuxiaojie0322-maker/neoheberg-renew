#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NeoHeberg VPS 自动登录与重启保活脚本
- 登录地址: https://dash.neoheberg.fr/login (适配全新 NeoHeberg 仪表盘)
- 全自动穿透 Cloudflare 5 秒盾与 Turnstile 人机验证
- 集成 sing-box 本地代理中间件 (自适应 Hysteria2 / VMess / VLESS / TUIC / Trojan / SOCKS5 / HTTP)
- 两步登录自动化 (Identifiant -> Mot de passe)
- 自动穿透 Axel-L Cap-Widget 人机验证 (PoW 自动求解与触发)
- 自动处理 Google GDPR Consent 弹窗 (Autoriser / Refuser) 并彻底消除浮层遮罩
- 自动定位 [data-vps-power="reboot"] (Redémarrer) 并完成服务器重启与保活
- 支持多账号轮询 (独立 Session / Context 隔离)
- 支持 Telegram Bot 实时图文与仪表盘全屏截图推送 (HTML 实体转义安全保护)
"""

import os
import sys
import time
import json
import logging
import datetime
import re
import html
import base64
import urllib.parse
import subprocess
import requests
from playwright.sync_api import sync_playwright

# 配置日志输出格式
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("NeoHeberg-Renew")

# ==================== 配置项 ====================
# ==================== 配置项 ====================
# Telegram Bot 推送凭据（从 GitHub Secrets 或环境变量读取）
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

# 默认代理节点备选配置（支持环境变量 PROXY_NODE / NODE_LINK / PROXY_URL 注入）
DEFAULT_HY2_NODE = os.environ.get("PROXY_NODE_HY2", "").strip()
DEFAULT_VMESS_NODE = os.environ.get("PROXY_NODE_VMESS", "").strip()

# 默认账号配置（从环境变量 NEOHEBERG_ACCOUNTS 注入，源码内不硬编码任何凭据）
DEFAULT_ACCOUNTS = []

# Anti-Detection Stealth Script (抹除自动化特征)
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {
    get: () => false
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

def parse_proxy_node(link):
    """解析主流代理节点链接为 sing-box outbound 结构"""
    link = (link or "").strip()
    if not link:
        return None

    if link.startswith("http://") or link.startswith("https://") or link.startswith("socks5://"):
        return {"type": "direct_proxy", "url": link}

    proto = link.split("://")[0].lower()

    if proto in ("hysteria2", "hy2"):
        raw = link.split("://", 1)[1].split("#")[0]
        auth, rest = raw.split("@", 1) if "@" in raw else ("", raw)
        host_port, query_str = rest.split("?", 1) if "?" in rest else (rest, "")
        server, port_str = host_port.split(":", 1) if ":" in host_port else (host_port, "443")
        params = urllib.parse.parse_qs(query_str)
        sni = params.get("sni", [""])[0] or server
        insecure = params.get("insecure", ["0"])[0] in ["1", "true", "True"]
        alpn = params.get("alpn", ["h3"])
        if isinstance(alpn, str):
            alpn = [alpn]

        outbound = {
            "type": "hysteria2",
            "tag": "proxy-out",
            "server": server,
            "server_port": int(port_str),
            "password": auth,
            "tls": {
                "enabled": True,
                "server_name": sni,
                "insecure": insecure,
                "alpn": alpn
            }
        }
        if params.get("obfs"):
            outbound["obfs"] = {
                "type": params.get("obfs", [""])[0],
                "password": params.get("obfs-password", [""])[0]
            }
        return outbound

    if proto == "vmess":
        raw_b64 = link[8:].split("#")[0]
        mod = len(raw_b64) % 4
        if mod == 2:
            raw_b64 += "=="
        elif mod == 3:
            raw_b64 += "="
        data = json.loads(base64.b64decode(raw_b64).decode("utf-8", "ignore"))
        outbound = {
            "type": "vmess",
            "tag": "proxy-out",
            "server": data.get("add", ""),
            "server_port": int(data.get("port", 443)),
            "uuid": data.get("id", ""),
            "security": data.get("scy", "auto"),
            "alter_id": int(data.get("aid", 0)),
        }
        if data.get("tls") == "tls":
            tls_cfg = {
                "enabled": True,
                "server_name": data.get("sni") or data.get("host") or data.get("add", ""),
                "insecure": False,
            }
            if data.get("ufp"):
                tls_cfg["utls"] = {"enabled": True, "fingerprint": data["ufp"]}
            outbound["tls"] = tls_cfg
        if data.get("net") == "ws":
            outbound["transport"] = {
                "type": "ws",
                "path": urllib.parse.unquote(data.get("path", "/")),
                "headers": {
                    "Host": data.get("host") or data.get("sni") or data.get("add", "")
                }
            }
        return outbound

    if proto == "vless":
        raw = link[8:]
        user_host = raw.split("#")[0]
        uuid, rest = user_host.split("@", 1) if "@" in user_host else ("", user_host)
        host_port, query_str = rest.split("?", 1) if "?" in rest else (rest, "")
        server, port_str = host_port.split(":", 1) if ":" in host_port else (host_port, "443")
        params = urllib.parse.parse_qs(query_str)
        get_p = lambda k, d="": params.get(k, [d])[0]

        outbound = {
            "type": "vless",
            "tag": "proxy-out",
            "server": server,
            "server_port": int(port_str),
            "uuid": uuid,
        }
        if get_p("flow"):
            outbound["flow"] = get_p("flow")
        sec = get_p("security")
        if sec == "tls":
            outbound["tls"] = {
                "enabled": True,
                "server_name": get_p("sni") or get_p("host") or server,
                "insecure": get_p("insecure") == "1" or get_p("allowInsecure") == "1",
            }
        elif sec == "reality":
            outbound["tls"] = {
                "enabled": True,
                "server_name": get_p("sni") or server,
                "reality": {
                    "enabled": True,
                    "public_key": get_p("pbk"),
                    "short_id": get_p("sid"),
                },
                "utls": {"enabled": True, "fingerprint": get_p("fp", "chrome")},
            }
        if get_p("type") == "ws":
            outbound["transport"] = {
                "type": "ws",
                "path": urllib.parse.unquote(get_p("path", "/")),
                "headers": {
                    "Host": get_p("host") or get_p("sni") or server
                }
            }
        return outbound

    if proto == "tuic":
        raw = link[7:].split("#")[0]
        auth, rest = raw.split("@", 1) if "@" in raw else ("", raw)
        uuid, password = auth.split(":", 1) if ":" in auth else (auth, auth)
        host_port, query_str = rest.split("?", 1) if "?" in rest else (rest, "")
        server, port_str = host_port.split(":", 1) if ":" in host_port else (host_port, "443")
        params = urllib.parse.parse_qs(query_str)
        get_p = lambda k, d="": params.get(k, [d])[0]
        return {
            "type": "tuic",
            "tag": "proxy-out",
            "server": server,
            "server_port": int(port_str),
            "uuid": uuid,
            "password": password,
            "congestion_control": get_p("congestion_control", "bbr"),
            "udp_relay_mode": get_p("udp_relay_mode", "native"),
            "tls": {
                "enabled": True,
                "server_name": get_p("sni") or server,
                "alpn": [get_p("alpn", "h3")],
                "insecure": get_p("allow_insecure") == "1" or get_p("insecure") == "1",
            }
        }

    if proto == "trojan":
        raw = link[9:].split("#")[0]
        password, rest = raw.split("@", 1) if "@" in raw else ("", raw)
        host_port, query_str = rest.split("?", 1) if "?" in rest else (rest, "")
        server, port_str = host_port.split(":", 1) if ":" in host_port else (host_port, "443")
        params = urllib.parse.parse_qs(query_str)
        get_p = lambda k, d="": params.get(k, [d])[0]
        outbound = {
            "type": "trojan",
            "tag": "proxy-out",
            "server": server,
            "server_port": int(port_str),
            "password": password,
            "tls": {
                "enabled": True,
                "server_name": get_p("sni") or server,
                "insecure": get_p("allowInsecure") == "1" or get_p("insecure") == "1",
            }
        }
        if get_p("type") == "ws":
            outbound["transport"] = {
                "type": "ws",
                "path": urllib.parse.unquote(get_p("path", "/")),
                "headers": {"Host": get_p("host") or get_p("sni") or server}
            }
        return outbound

    return None

def start_proxy(listen_port=10808):
    """
    启动本地 sing-box 代理进程，将代理节点转换为本地 SOCKS5/HTTP 混合端口
    返回本地代理 URL（如 'http://127.0.0.1:10808'）供 Playwright 使用
    """
    node_candidates = []
    env_node = (os.environ.get("PROXY_NODE") or os.environ.get("NODE_LINK") or os.environ.get("PROXY_URL") or "").strip()
    if env_node:
        for item in re.split(r'[\r\n,]+', env_node):
            item = item.strip()
            if item and item not in node_candidates:
                node_candidates.append(item)
    if DEFAULT_HY2_NODE and DEFAULT_HY2_NODE not in node_candidates:
        node_candidates.append(DEFAULT_HY2_NODE)
    if DEFAULT_VMESS_NODE and DEFAULT_VMESS_NODE not in node_candidates:
        node_candidates.append(DEFAULT_VMESS_NODE)

    singbox_bin = "./sing-box"
    if sys.platform.startswith("linux"):
        if not os.path.exists(singbox_bin):
            logger.info("📦 正在下载 sing-box 代理中间件...")
            try:
                url = "https://github.com/SagerNet/sing-box/releases/download/v1.9.3/sing-box-1.9.3-linux-amd64.tar.gz"
                subprocess.run(["curl", "-sLo", "sing-box.tar.gz", url], check=True)
                subprocess.run(["tar", "-xzf", "sing-box.tar.gz", "--strip-components=1"], check=True)
                subprocess.run(["chmod", "+x", singbox_bin], check=True)
                logger.info("✅ sing-box 二进制就绪！")
            except Exception as e:
                logger.error(f"❌ 下载 sing-box 失败: {e}，将尝试直连")
                return None
    else:
        singbox_bin = "sing-box.exe"
        if not os.path.exists(singbox_bin):
            logger.info("ℹ️ 非 Linux 环境未找到 sing-box.exe，使用系统直连/默认网络")
            return None

    for idx, candidate in enumerate(node_candidates, 1):
        try:
            parsed = parse_proxy_node(candidate)
            if not parsed:
                continue

            if parsed.get("type") == "direct_proxy":
                logger.info(f"🌐 使用直接代理地址: {parsed['url']}")
                return parsed["url"]

            logger.info(f"🚀 尝试启动代理节点 [{idx}/{len(node_candidates)}]: {parsed['type']} -> 目标 {parsed.get('server')}:{parsed.get('server_port')}")

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
                "outbounds": [parsed]
            }

            config_file = "singbox_proxy_config.json"
            with open(config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)

            proc = subprocess.Popen([singbox_bin, "run", "-c", config_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3)

            local_proxy = f"http://127.0.0.1:{listen_port}"
            try:
                res = requests.get(
                    "https://api.ipify.org?format=json",
                    proxies={"http": local_proxy, "https": local_proxy},
                    timeout=10
                )
                out_ip = res.json().get("ip")
                logger.info(f"✨ 代理连通成功！节点出口 IP: {out_ip}")
                return local_proxy
            except Exception as e:
                logger.warning(f"⚠️ 节点 [{idx}] 连通测试失败 ({e})，尝试切换下一候选节点...")
                proc.terminate()
                time.sleep(1)

        except Exception as e:
            logger.warning(f"解析或启动节点异常: {e}")

    logger.warning("⚠️ 所有代理节点均不可用，降级为直连访问")
    return None

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

def dismiss_gdpr_and_overlays(page):
    """清理 Google GDPR Consent、通知浮层以及页面注入的广告遮罩"""
    try:
        # 1. 点击 'Autoriser' / 'Refuser' / 'Accepter'
        for text in ["Autoriser", "Refuser", "Accepter", "Consent"]:
            btn = page.locator(f'.fc-consent-root button:has-text("{text}"), button:has-text("{text}")')
            if btn.count() > 0 and btn.first.is_visible():
                logger.info(f"👉 点击 GDPR 弹窗按钮: {text}")
                try:
                    btn.first.click(timeout=2000, force=True)
                except Exception:
                    pass
                time.sleep(1)
                break

        # 2. 彻底从 DOM 移除 GDPR、遮罩层及底部广告条
        page.evaluate("""() => {
            document.querySelectorAll(
                '.fc-consent-root, .fc-dialog-overlay, .modal-backdrop, [data-action="dismiss-alert"], ' +
                'iframe[id*="aswift"], iframe[id*="google_ads"], ins.adsbygoogle, #ad-container, [class*="ad-"]'
            ).forEach(el => el.remove());
            document.body.style.overflow = 'auto';
            const btns = Array.from(document.querySelectorAll('button, a'));
            btns.forEach(b => {
                const text = (b.innerText || '').toLowerCase().trim();
                if (text === 'accepter' || text === 'accept' || text === 'fermer') {
                    b.click();
                }
            });
        }""")
    except Exception as e:
        logger.debug(f"清理弹窗遮罩提示: {e}")

def cdp_native_click(cdp_session, x, y):
    """利用 CDP 发送真实的操作系统级硬件鼠标点击事件"""
    try:
        cdp_session.send('Input.dispatchMouseEvent', {
            'type': 'mouseMoved',
            'x': int(x),
            'y': int(y)
        })
        time.sleep(0.1)
        cdp_session.send('Input.dispatchMouseEvent', {
            'type': 'mousePressed',
            'x': int(x),
            'y': int(y),
            'button': 'left',
            'clickCount': 1
        })
        time.sleep(0.12)
        cdp_session.send('Input.dispatchMouseEvent', {
            'type': 'mouseReleased',
            'x': int(x),
            'y': int(y),
            'button': 'left',
            'clickCount': 1
        })
        return True
    except Exception as e:
        logger.warning(f"CDP 点击失败: {e}")
        return False

def safe_page_content(page):
    """安全读取页面 HTML，捕获页面导航中的状态异常"""
    try:
        return (page.content() or "").lower()
    except Exception:
        return ""

def safe_page_title(page):
    """安全读取页面标题，捕获页面导航中的状态异常"""
    try:
        return (page.title() or "").lower()
    except Exception:
        return ""

def try_click_cloudflare(page, cdp_session):
    """检测并尝试通过 CDP 和 DOM 穿透 Cloudflare Turnstile 复选框"""
    title = safe_page_title(page)
    content = safe_page_content(page)

    is_cf = (
        "cloudflare" in title
        or "just a moment" in title
        or "un instant" in title
        or "vérification" in title
        or "challenges.cloudflare.com" in content
        or "cf-turnstile" in content
        or "turnstile" in content
    )

    if not is_cf:
        return False

    logger.info(f"🛡️ 检测到 Cloudflare 验证盾 (标题: '{title}')，正在尝试穿透...")

    for frame in page.frames:
        try:
            chk = frame.locator("input[type='checkbox'], span.mark, .ctp-checkbox-label, #challenge-stage")
            if chk.count() > 0 and chk.first.is_visible():
                logger.info(f"👆 点击 Frame ({frame.url[:40]}...) 内的 Turnstile 复选框")
                chk.first.click(timeout=1500)
                time.sleep(2)
                return True
        except Exception:
            pass

    found_geom = False
    try:
        iframes = page.locator("iframe")
        for i in range(iframes.count()):
            ifr = iframes.nth(i)
            box = ifr.bounding_box()
            if box and box["width"] > 80 and box["height"] > 25:
                click_x = box["x"] + 28
                click_y = box["y"] + (box["height"] / 2)
                logger.info(f"👆 通过 CDP 点击识别到的 Cloudflare iframe 坐标: ({click_x:.1f}, {click_y:.1f})")
                cdp_native_click(cdp_session, click_x, click_y)
                found_geom = True
                time.sleep(2.5)
                return True
    except Exception:
        pass

    if not found_geom:
        logger.info("👆 触发 CDP 视口基准坐标硬件点击: (294, 372)...")
        cdp_native_click(cdp_session, 294, 372)
        time.sleep(0.2)
        cdp_native_click(cdp_session, 298, 372)
        time.sleep(2)
        return True

    return False

def wait_for_login_form_or_cf(page, cdp_session, max_wait_sec=40):
    """等待登录表单就绪，如果遇到 Cloudflare 盾或 Turnstile 验证，则自动穿透"""
    logger.info("⏳ 等待登录页面加载（含 Cloudflare 质询检测与穿透）...")
    start_t = time.time()
    while time.time() - start_t < max_wait_sec:
        title = safe_page_title(page)
        content = safe_page_content(page)

        # 检测官方源站临时 524/502 错误并自动重载
        if "error code 524" in content or "error code 502" in content or "504 gateway" in content:
            logger.warning("⚠️ 检测到 NeoHeberg 官方源站短暂 524/502 超时，等待 6 秒后刷新...")
            time.sleep(6)
            try:
                page.reload(wait_until="commit", timeout=30000)
            except Exception:
                pass
            continue

        try:
            id_loc = page.locator('input#identifier, input[name="identifier"]')
            if id_loc.count() > 0 and id_loc.first.is_visible():
                logger.info("✅ 登录表单已就绪！")
                return True
        except Exception:
            pass

        try:
            if page.locator(':text("Mes services"), :text("Tableau de bord"), a:has-text("Déconnexion")').count() > 0:
                logger.info("🎉 检测到后台组件，已处于登录态！")
                return True
        except Exception:
            pass

        try_click_cloudflare(page, cdp_session)
        time.sleep(1.5)

    logger.warning("⚠️ 等待登录表单超时")
    return False

def wait_for_dashboard_or_cf(page, cdp_session, max_wait_sec=40):
    """表单提交后，等待进入控制台；若遇 Cloudflare 质询则自动点击穿透"""
    logger.info("⏳ 监控登录跳转（含 Cloudflare 质询检测与穿透）...")
    start_t = time.time()
    while time.time() - start_t < max_wait_sec:
        title = safe_page_title(page)
        content = safe_page_content(page)

        if "error code 524" in content or "error code 502" in content:
            logger.warning("⚠️ 提交后检测到源站短暂 524 超时，等待 6 秒后刷新重试...")
            time.sleep(6)
            try:
                page.reload(wait_until="commit", timeout=30000)
            except Exception:
                pass
            continue

        try:
            if page.locator(':text("Mes services"), :text("Tableau de bord"), a:has-text("Déconnexion"), [data-action="logout"]').count() > 0:
                logger.info("🎉 成功进入控制台后台！")
                return True
        except Exception:
            pass

        try:
            err_loc = page.locator('#identifierError, #loginCaptchaError, .text-red-400')
            if err_loc.count() > 0 and err_loc.first.is_visible():
                err_text = err_loc.first.text_content().strip()
                if err_text:
                    logger.warning(f"⚠️ 页面提示错误: {err_text}")
        except Exception:
            pass

        try_click_cloudflare(page, cdp_session)
        time.sleep(1.5)

    return False

def solve_dash_cap_widget(page):
    """
    处理新面板中的 Axel-L Cap-Widget 人机验证 (dash.neoheberg.fr)
    通过触发 solve() 方法与点击 trigger，等待客户端 PoW 计算完成
    """
    logger.info("🔍 检查 Cap-Widget 人机验证状态...")
    try:
        page.evaluate("""async () => {
            const widget = document.getElementById('cap-login') || document.querySelector('cap-widget');
            if (widget && typeof widget.solve === 'function') {
                try {
                    widget.solve();
                } catch(e) {}
            }
        }""")
        time.sleep(1)

        widget_trigger = page.locator('#cap-login, cap-widget, cap-widget .captcha-trigger, cap-widget [role="button"]')
        if widget_trigger.count() > 0:
            try:
                widget_trigger.first.click(timeout=3000)
                logger.info("👉 已点击 Cap-Widget 验证组件")
            except Exception:
                pass

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

def get_vps_power_status(page):
    """
    精确检查当前 VPS 的电源状态。
    严格区分关机 (ARRÊTÉ) 与开机运行 (EN LIGNE)。
    返回: 'STOPPED' (关机), 'RUNNING' (开机运行), 或 'UNKNOWN'
    """
    try:
        return page.evaluate(r"""() => {
            // 1. 优先检查状态徽章元素 (微小标签，文本包含 ARRÊTÉ 或 EN LIGNE)
            const badges = Array.from(document.querySelectorAll('span, div, p, [class*="badge"], [class*="status"]'))
                .filter(el => el.children.length <= 2);
                
            for (const el of badges) {
                const text = (el.innerText || '').trim();
                if (text.length > 0 && text.length < 35) {
                    const clean = text.replace(/^[^\wÀ-ſ]+/i, '').trim().toUpperCase();
                    if (clean === 'ARRÊTÉ' || clean === 'ARRETE' || clean.includes('ARRÊTÉ') || clean.includes('ARRETE')) {
                        return 'STOPPED';
                    }
                    if (clean === 'EN LIGNE' || clean.includes('EN LIGNE') || clean === 'RUNNING') {
                        return 'RUNNING';
                    }
                }
            }
            
            // 2. 检查控制面板三个核心电源按钮 (Démarrer / Arrêter / Redémarrer) 的启用/禁用状态
            const buttons = Array.from(document.querySelectorAll('button, a'));
            const startBtn = buttons.find(b => {
                const t = (b.innerText || '').trim();
                return /D[eé]marrer/i.test(t) && !/Red/i.test(t);
            });
            const stopBtn = buttons.find(b => {
                const t = (b.innerText || '').trim();
                return /Arr[eê]ter/i.test(t);
            });
            const rebootBtn = buttons.find(b => {
                const t = (b.innerText || '').trim();
                return /Red[eé]marrer/i.test(t);
            });
            
            const isBtnDisabled = (btn) => {
                if (!btn) return true;
                if (btn.disabled || btn.getAttribute('disabled') !== null || btn.getAttribute('aria-disabled') === 'true') return true;
                const cls = (btn.className || '').toLowerCase();
                if (cls.includes('disabled') || cls.includes('opacity-50') || cls.includes('cursor-not-allowed') || cls.includes('pointer-events-none')) return true;
                const style = window.getComputedStyle(btn);
                if (style.opacity && parseFloat(style.opacity) < 0.6) return true;
                if (style.pointerEvents === 'none') return true;
                return false;
            };
            
            // 开机按钮可用且关机按钮被禁用 -> 必定为关机状态 (STOPPED)
            if (startBtn && !isBtnDisabled(startBtn) && stopBtn && isBtnDisabled(stopBtn)) {
                return 'STOPPED';
            }
            // 关机或重启按钮可用且开机按钮被禁用 -> 必定为运行中状态 (RUNNING)
            if ((stopBtn && !isBtnDisabled(stopBtn)) || (rebootBtn && !isBtnDisabled(rebootBtn) && startBtn && isBtnDisabled(startBtn))) {
                return 'RUNNING';
            }
            
            // 3. 检查全局文本
            const bodyText = (document.body.innerText || '');
            if (/\bARR[EÊ]T[EÉ]\b/i.test(bodyText)) {
                return 'STOPPED';
            }
            if (/\bEN\s+LIGNE\b/i.test(bodyText)) {
                return 'RUNNING';
            }
            
            return 'UNKNOWN';
        }""")
    except Exception as e:
        logger.warning(f"读取 VPS 电源状态异常: {e}")
        return 'UNKNOWN'

def navigate_to_vps_panel(page, cdp_session=None):
    """确保直接进入具体的 VPS 专属管理面板 (如 /services/xiao 或 /services/181)"""
    logger.info("🧭 正在定位并进入 VPS 专属管理详情面板...")
    # 1. 如果已在服务详情页 (URL 包含 /services/ 且有控制台按钮或电源控制区)
    if ("/services/" in page.url and page.locator('button:has-text("Ouvrir la console"), a:has-text("Ouvrir la console")').count() > 0) or \
       (page.locator('button:has-text("Ouvrir la console"), a:has-text("Ouvrir la console")').count() > 0):
        logger.info(f"📌 当前已位于 VPS 专属详情面板: {page.url}")
        return True

    # 2. 在主界面寻找 'Gérer le VPS' 或 'Gérer' 按钮/链接，直接提取 href 跳转或点击
    gerer_locator = page.locator('a:has-text("Gérer le VPS"), button:has-text("Gérer le VPS"), a:has-text("Gérer"), a:has-text("Gerer")')
    if gerer_locator.count() > 0 and gerer_locator.first.is_visible():
        href = gerer_locator.first.get_attribute("href")
        if href:
            target_url = urllib.parse.urljoin(page.url, href)
            logger.info(f"🔗 提取到专属面板链接，直接跳转: {target_url}")
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                logger.warning(f"跳转专属面板提示: {e}")
        else:
            logger.info("👉 点击 'Gérer le VPS' 进入专属面板...")
            try:
                gerer_locator.first.click(timeout=8000)
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception as e:
                logger.warning(f"点击 Gérer 按钮提示: {e}")
        
        time.sleep(3)
        dismiss_gdpr_and_overlays(page)
        logger.info(f"📌 当前页面地址: {page.url}")
        return True

    # 3. 若主页卡片没找到，从左侧边栏点击 'Mes services'
    mes_services_btn = page.locator('a:has-text("Mes services"), a[href*="/services"]').first
    if mes_services_btn.count() > 0 and mes_services_btn.is_visible():
        logger.info("👉 点击侧边栏 'Mes services'...")
        href = mes_services_btn.get_attribute("href")
        if href:
            page.goto(urllib.parse.urljoin(page.url, href), wait_until="domcontentloaded", timeout=30000)
        else:
            mes_services_btn.click(timeout=8000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        time.sleep(3)
        dismiss_gdpr_and_overlays(page)

        # 在列表页点击第一个服务的 Gérer
        sub_gerer = page.locator('a:has-text("Gérer"), a:has-text("Gérer le VPS")')
        if sub_gerer.count() > 0 and sub_gerer.first.is_visible():
            sub_href = sub_gerer.first.get_attribute("href")
            if sub_href:
                page.goto(urllib.parse.urljoin(page.url, sub_href), wait_until="domcontentloaded", timeout=30000)
            else:
                sub_gerer.first.click(timeout=8000)
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            time.sleep(3)
            dismiss_gdpr_and_overlays(page)
            return True

    return False

def check_and_confirm_modal(page, cdp_session=None):
    """检测并确认二次确认弹窗 (严格限定在可见的 dialog / modal 容器内，杜绝误点页面其它按钮)"""
    time.sleep(1.2)
    try:
        dialogs = page.locator('dialog:visible, [role="dialog"]:visible, [aria-modal="true"]:visible, div[class*="modal"]:visible, .swal2-container:visible')
        if dialogs.count() > 0:
            logger.info("⚠️ 检测到可见弹窗容器，正在检查确认按钮...")
            confirm_btn = dialogs.first.locator('button, a, [role="button"]').filter(
                has_text=re.compile(r'^(Confirmer|Valider|Oui|Yes|Confirm|D[eé]marrer|Red[eé]marrer|Lancer|Continuer)$', re.I)
            )
            if confirm_btn.count() > 0 and confirm_btn.first.is_visible():
                btn_txt = confirm_btn.first.text_content().strip()
                logger.info(f"👉 点击弹窗确认按钮: '{btn_txt}'")
                confirm_btn.first.click(force=True, timeout=5000)
                time.sleep(2)
                return True
    except Exception as e:
        logger.warning(f"检查弹窗提示: {e}")
    return False

def trigger_power_action(page, cdp_session, action_type, username):
    """
    触发电源控制按钮 (开机 Démarrer / 重启 Redémarrer)：
    执行单次精准触发，绝不多次连击造成请求冲突
    """
    btn_label = "Démarrer (开机)" if action_type == "start" else "Redémarrer (重启)"
    logger.info(f"👉 准备定位并触发 '{btn_label}' 按钮...")
    
    if action_type == "start":
        btn = page.locator('button, a, [role="button"]').filter(
            has_text=re.compile(r'D[eé]marrer', re.I)
        ).filter(
            has_not_text=re.compile(r'Red', re.I)
        )
    else:
        btn = page.locator('button, a, [role="button"]').filter(
            has_text=re.compile(r'Red[eé]marrer', re.I)
        )
        
    clicked = False
    if btn.count() > 0 and btn.first.is_visible():
        target = btn.first
        try:
            target.scroll_into_view_if_needed()
            time.sleep(0.5)
            # 1. 优先使用 Playwright 原生安全点击
            target.click(timeout=5000)
            logger.info(f"✅ Playwright 原生点击 '{btn_label}' 成功！")
            clicked = True
        except Exception as e1:
            logger.warning(f"Playwright 直接点击提示: {e1}，切换至 CDP 物理坐标点击...")
            try:
                box = target.bounding_box()
                if box and cdp_session:
                    cx = box['x'] + box['width'] / 2
                    cy = box['y'] + box['height'] / 2
                    cdp_native_click(cdp_session, cx, cy)
                    logger.info(f"✅ CDP 硬件级坐标 ({round(cx,1)}, {round(cy,1)}) 点击成功！")
                    clicked = True
            except Exception as e2:
                logger.warning(f"CDP 物理坐标点击提示: {e2}")

    # 2. 若仍未触发成功，使用 DOM 原生派发作为兜底
    if not clicked:
        logger.info(f"👉 尝试 DOM 原生事件触发 '{btn_label}'...")
        clicked = page.evaluate(r"""(type) => {
            const all = Array.from(document.querySelectorAll('button, a, [role="button"]'));
            let target = null;
            if (type === 'start') {
                target = all.find(b => /D[eé]marrer/i.test((b.innerText || '').trim()) && !/Red/i.test((b.innerText || '').trim()));
            } else {
                target = all.find(b => /Red[eé]marrer/i.test((b.innerText || '').trim()));
            }
            if (target) {
                target.scrollIntoView({ behavior: 'instant', block: 'center' });
                target.click();
                ['pointerdown', 'mousedown', 'click'].forEach(evt => {
                    target.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                });
                return true;
            }
            return false;
        }""", action_type)

    # 保存点击后现场快照
    time.sleep(1)
    try:
        page.screenshot(path=f"after_{action_type}_click_{username}.png", full_page=True)
    except Exception:
        pass

    # 检查并确认弹窗
    check_and_confirm_modal(page, cdp_session)
    return clicked

def wait_and_verify_vps_online(page, cdp_session, expected_action, username, max_wait_sec=75):
    """
    严格轮询监控直到确认 VPS 处于开机在线 (EN LIGNE)！
    如果等待中途发现仍为 STOPPED，会自动刷新页面同步；
    若刷新后依然为 STOPPED，会重新补点一次 Démarrer！
    绝不在关机状态下误报成功！
    """
    action_desc = "开机 (Démarrer)" if expected_action == "START" else "重启 (Redémarrer)"
    logger.info(f"⏳ 正在启动严格在线状态轮询监控 (动作: {action_desc}, 最大等待: {max_wait_sec} 秒)...")
    start_time = time.time()
    retry_clicked = False

    while time.time() - start_time < max_wait_sec:
        elapsed = round(time.time() - start_time, 1)
        st = get_vps_power_status(page)
        logger.info(f"⏱️ [{elapsed}s/{max_wait_sec}s] 当前 VPS 状态检测结果: [{st}]")

        if st == "RUNNING":
            logger.info(f"🎉 状态已确认转为在线运行 (EN LIGNE)！(耗时 {elapsed} 秒)")
            time.sleep(3)
            return True, "RUNNING"

        # 如果过了 15 秒还是 STOPPED，主动刷新页面以从后台拉取最新状态 (后台每 5 秒刷新一次)
        if elapsed >= 15 and int(elapsed) % 15 == 0:
            logger.info("🔄 刷新页面以拉取后台实时状态...")
            try:
                page.reload(wait_until="domcontentloaded", timeout=20000)
                time.sleep(3)
                dismiss_gdpr_and_overlays(page)
            except Exception as e:
                logger.warning(f"刷新页面提示: {e}")

            st_reload = get_vps_power_status(page)
            logger.info(f"📊 刷新后状态: [{st_reload}]")
            if st_reload == "RUNNING":
                return True, "RUNNING"

            # 如果预期是开机动作且依然为 STOPPED，在 30 秒时再次尝试点击开机按钮
            if expected_action == "START" and st_reload == "STOPPED" and not retry_clicked and elapsed >= 30:
                logger.info("⚠️ 状态仍为关机，执行补充点击 'Démarrer' 开机...")
                trigger_power_action(page, cdp_session, 'start', username)
                retry_clicked = True

        time.sleep(5)

    final_status = get_vps_power_status(page)
    return (final_status == "RUNNING"), final_status

def process_single_account(browser, account, index, total, proxy_server=None):
    """处理单个 NeoHeberg 账号的登录与 VPS 重启任务"""
    username = account.get("username", "").strip()
    password = account.get("password", "").strip()
    start_time = time.time()

    logger.info("=" * 60)
    logger.info(f"[{index}/{total}] 开始处理账号: {username}")
    logger.info("=" * 60)

    # 独立会话隔离
    context_kwargs = {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "viewport": {"width": 1440, "height": 900},
        "locale": "fr-FR",
        "timezone_id": "Europe/Paris"
    }
    if proxy_server:
        context_kwargs["proxy"] = {"server": proxy_server}

    context = browser.new_context(**context_kwargs)
    context.add_init_script(STEALTH_JS)

    page = context.new_page()
    cdp_session = context.new_cdp_session(page)

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

        # 尝试访问登录页并完成登录
        for attempt in range(1, 4):
            logger.info(f"🌐 [第 {attempt}/3 次尝试] 访问 NeoHeberg 登录页面: {login_url}")
            try:
                page.goto(login_url, wait_until="commit", timeout=45000)
            except Exception as e:
                logger.warning(f"page.goto 提示: {e}")

            time.sleep(2)

            # 等待表单就绪或穿透 Cloudflare
            form_ready = wait_for_login_form_or_cf(page, cdp_session, max_wait_sec=40)

            # 检查是否已直接处于登录态
            if page.locator(':text("Mes services"), :text("Tableau de bord"), a:has-text("Déconnexion")').count() > 0:
                logger.info("🎉 识别到后台核心组件，当前已处于登录态！")
                logged_in = True
                break

            if form_ready and page.locator('#identifier, input[name="identifier"]').count() > 0:
                # 步骤 1: 填写用户名/邮箱并点击 Continuer
                logger.info(f"✍️ [第 1 步] 填写 Identifiant: {username}")
                id_input = page.locator('input#identifier, input[name="identifier"]')
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

                # 监测跳转或穿透提交后的 Cloudflare
                if wait_for_dashboard_or_cf(page, cdp_session, max_wait_sec=40):
                    logged_in = True
                    break

            time.sleep(2)

        if not logged_in:
            err_text = "登录未能成功跳转到后台控制台"
            logger.error(f"❌ {err_text}")
            result_info["message"] = err_text
            page.screenshot(path=f"error_{username}.png", full_page=True)
            result_info["screenshot"] = f"error_{username}.png"
            return result_info

        logger.info("🎉 成功登录后台！")
        time.sleep(3)
        dismiss_gdpr_and_overlays(page)

        # ==================== 1. 确保进入 VPS 专属管理面板 ====================
        navigate_to_vps_panel(page, cdp_session)
        result_info["panel_url"] = page.url
        time.sleep(2)
        dismiss_gdpr_and_overlays(page)

        # ==================== 2. 严格检测当前 VPS 电源状态 ====================
        current_status = get_vps_power_status(page)
        logger.info(f"📊 VPS 当前精准运行状态: [{current_status}]")

        action_performed = None

        if current_status == "STOPPED":
            # 明确关机状态：执行开机 (Démarrer)
            logger.info("⚠️ 【情况 1：关机状态】检测到 VPS 当前处于关机 (ARRÊTÉ)！执行开机动作：点击 'Démarrer'...")
            action_performed = "START"
            trigger_power_action(page, cdp_session, 'start', username)
        elif current_status == "RUNNING":
            # 明确运行状态：执行重启保活 (Redémarrer)
            logger.info("🔄 【情况 2：开机状态】检测到 VPS 当前处于运行 (EN LIGNE)！执行保活动作：点击 'Redémarrer'...")
            action_performed = "REBOOT"
            trigger_power_action(page, cdp_session, 'reboot', username)
        else:
            logger.info("ℹ️ 【情况 3：未知状态】优先尝试点击开机 Démarrer...")
            action_performed = "START"
            clicked = trigger_power_action(page, cdp_session, 'start', username)
            if not clicked:
                trigger_power_action(page, cdp_session, 'reboot', username)
                action_performed = "REBOOT"

        # ==================== 3. 严格监控验证开机在线后再推送 ====================
        logger.info(f"⏳ 操作指令已触发 (动作: {action_performed})，严格轮询监控直到确认开机在线 (EN LIGNE)...")
        is_online, final_status = wait_and_verify_vps_online(
            page, cdp_session, expected_action=action_performed, username=username, max_wait_sec=75
        )

        # 最终截取状态凭证截图
        final_shot = f"reboot_success_{username}.png" if is_online else f"error_{username}.png"
        page.screenshot(path=final_shot, full_page=True)
        result_info["screenshot"] = final_shot

        if is_online:
            result_info["status"] = "SUCCESS"
            if action_performed == "START":
                result_info["message"] = "原处于关机状态，已点击 Démarrer 并确认成功开机在线 (EN LIGNE)"
                logger.info(f"🎉 账号 {username} 的 VPS 已成功开机！状态确认: EN LIGNE，截图: {final_shot}")
            else:
                result_info["message"] = "原处于开机状态，已点击 Redémarrer 重启并确认恢复在线 (EN LIGNE)"
                logger.info(f"🎉 账号 {username} 的 VPS 已成功重启保活！状态确认: EN LIGNE，截图: {final_shot}")
        else:
            logger.warning(f"⚠️ 账号 {username} 的 VPS 在下发指令后等待超时，当前状态为: [{final_status}]")
            result_info["status"] = "FAIL"
            if action_performed == "START":
                result_info["message"] = f"已下发 Démarrer 开机指令，但在等待 75 秒后状态仍未转为 EN LIGNE (当前: {final_status})"
            else:
                result_info["message"] = f"已下发 Redémarrer 重启指令，但在等待 75 秒后状态仍为: {final_status}"

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
            cdp_session.detach()
        except Exception:
            pass
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

    # 启动代理中间件（彻底解决 Cloudflare 5 秒盾与机房 IP 拦截）
    proxy_server = start_proxy(listen_port=10808)
    if proxy_server:
        logger.info(f"🛡️ 全局网络已接入代理中间件: {proxy_server}")
    else:
        logger.info("🌐 未使用代理中间件，将使用系统直连并启用 Turnstile 本地穿透模式")

    headless = os.environ.get("HEADLESS", "true").lower() != "false"

    results = []
    with sync_playwright() as p:
        launch_kwargs = {
            "headless": headless,
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1440,900"
            ]
        }
        if proxy_server:
            launch_kwargs["proxy"] = {"server": proxy_server}

        browser = p.chromium.launch(**launch_kwargs)

        for i, acc in enumerate(accounts, 1):
            res = process_single_account(browser, acc, i, len(accounts), proxy_server=proxy_server)
            results.append(res)
            if i < len(accounts):
                time.sleep(5)

        browser.close()

    success_list = [r for r in results if r["status"] == "SUCCESS"]
    fail_list = [r for r in results if r["status"] != "SUCCESS"]

    logger.info("=" * 60)
    logger.info(f"📊 任务执行完毕: 成功 {len(success_list)} 个, 失败 {len(fail_list)} 个")
    logger.info("=" * 60)

    # 发送 Telegram 结果报告 (全面做 html.escape 防御，避免 HTML 实体错误)
    now_str = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")

    # 1. 逐个推送带图报告
    for r in results:
        if r.get("screenshot") and os.path.exists(r["screenshot"]):
            safe_user = html.escape(str(r['username']))
            safe_msg = html.escape(str(r['message']))
            if r["status"] == "SUCCESS":
                badge = "✅ 开机成功 (EN LIGNE)" if "Démarrer" in str(r['message']) else "✅ 重启保活成功 (EN LIGNE)"
            else:
                badge = f"❌ 异常: {safe_msg}"
            caption = (
                f"🚀 <b>NeoHeberg VPS 电源与保活报告</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>账号</b>: <code>{safe_user}</code>\n"
                f"📊 <b>状态</b>: {badge}\n"
                f"📝 <b>详情</b>: <code>{safe_msg}</code>\n"
                f"⏱ <b>耗时</b>: {r['duration']} 秒\n"
                f"⏰ <b>时间</b>: {now_str} (北京时间)\n"
            )
            if r.get("panel_url"):
                caption += f"🔗 <b>面板</b>: <code>{html.escape(str(r['panel_url']))}</code>\n"
            send_tg_photo(r["screenshot"], caption)
            time.sleep(1)

    # 2. 推送总汇总消息
    status_icon = "🎉" if len(fail_list) == 0 else "⚠️"
    summary_lines = [
        f"{status_icon} <b>NeoHeberg VPS 自动保活总运行报告</b>",
        "━━━━━━━━━━━━━━━━━━"
    ]
    for r in results:
        safe_user = html.escape(str(r['username']))
        safe_msg = html.escape(str(r['message']))
        if r["status"] == "SUCCESS":
            tag = "✅ 开机成功" if "开机" in str(r['message']) else "✅ 重启成功"
        else:
            tag = f"❌ {safe_msg}"
        summary_lines.append(f"• 👤 <code>{safe_user}</code>: {tag} ({r['duration']}s)")
    summary_lines.append("──────────────────")
    summary_lines.append(f"📈 <b>总计</b>: 成功 {len(success_list)} / 失败 {len(fail_list)}")
    summary_lines.append(f"⏰ <b>完成时间</b>: {now_str}")

    send_tg_message("\n".join(summary_lines))

    if len(fail_list) > 0 and len(success_list) == 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
