import os
import re
import sys
import time
import random
import platform
import subprocess
import traceback
import requests

from pyvirtualdisplay import Display
from seleniumbase import SB


# ================= 配置区域 =================
BASE_URL   = os.getenv("BASE_URL", "https://vps8.zz.cd")
LOGIN_URL  = os.getenv("LOGIN_URL", f"{BASE_URL}/login")
SIGNIN_URL = os.getenv("SIGNIN_URL", f"{BASE_URL}/points/signin")

# 第三方登录方式: github / google / nodeloc
LOGIN_PROVIDER = os.getenv("LOGIN_PROVIDER", "nodeloc").strip().lower()
PROVIDER_LOGIN_URLS = {
    "github":  f"{BASE_URL}/github/login",
    "google":  f"{BASE_URL}/google/login",
    "nodeloc": f"{BASE_URL}/nodeloc/login",
}

# 持久化 Chrome Profile —— 复用同一份（含第三方登录态 + Discord 登录态 + NopeCHA）
PROFILE_DIR = os.getenv(
    "BROWSER_USER_DATA_DIR",
    os.path.expanduser("~/.chrome-profile-discord"),
)

# NopeCHA 插件目录（解压后的扩展文件夹，含 manifest.json）
NOPECHA_EXT_DIR = os.getenv(
    "NOPECHA_EXT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "chromium"),
)
# NopeCHA 套餐 key（可选）。设置后直接注入并跳过 Discord 领取流程
NOPECHA_KEY = (os.getenv("NOPECHA_KEY") or "").strip()

# NopeCHA 官方 Discord —— 用于自动领取每日 CDK（激活码）
DISCORD_CHANNEL_URL = os.getenv(
    "DISCORD_CHANNEL_URL",
    "https://discord.com/channels/1046086326077882479/1243188924520726538",
)
DISCORD_DM_URL = os.getenv(
    "DISCORD_DM_URL",
    "https://discord.com/channels/@me/1519516877074731018",
)

SS_DIR = os.getenv("SS_DIR", "screenshots")
IP_CHECK_URL = os.getenv("IP_CHECK_URL", "https://api.ipify.org?format=json")
BROWSER_IP_PROBE_URLS = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
]

PROXY = (os.getenv("PROXY") or os.getenv("BROWSER_PROXY") or "").strip()
TG_TOKEN = os.getenv("TG_BOT_TOKEN") or os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")
DISPLAY = os.environ.get("DISPLAY", ":99")
# ===========================================


def log(level, msg):
    print(f"[{time.strftime('%H:%M:%S')}] [{level}] {msg}", flush=True)


# ── 脱敏工具 ──────────────────────────────────────────────

def mask_ip(ip):
    value = (ip or "").strip()
    if not value:
        return "unknown"

    if ":" in value:  # IPv6
        parts = value.split(":")
        if len(parts) >= 2:
            return ":".join(parts[:-2] + ["*", "*"])
        return "*:*"

    parts = value.split(".")  # IPv4
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.*.*"
    return value[:-2] + "**" if len(value) >= 2 else "**"


def mask_proxy(proxy):
    p = (proxy or "").strip()
    if not p:
        return "<none>"
    m = re.match(r"^([a-zA-Z0-9+.-]+://)", p)
    if m:
        return f"{m.group(1)}***"
    return "***"


def extract_ip_from_text(text):
    if not text:
        return ""
    m4 = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
    if m4:
        return m4.group(0)
    m6 = re.search(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b", text)
    if m6:
        return m6.group(0)
    return ""


def detect_exit_ip(proxy=None):
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = requests.get(IP_CHECK_URL, proxies=proxies, timeout=20)
        r.raise_for_status()
        ip = str(r.json().get("ip", "")).strip()
        if not ip:
            return False, "empty ip"
        return True, ip
    except Exception as e:
        return False, str(e)


# ── Telegram 通知 ──────────────────────────────────────────

def send_tg_text(token, chat_id, text):
    if not (token and chat_id and text):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=30,
        )
        log("INFO", "Telegram 文本发送成功")
    except Exception as e:
        log("ERROR", f"Telegram 文本发送失败: {e}")


def send_tg_photo(token, chat_id, path, caption=""):
    if not (token and chat_id and os.path.exists(path)):
        return
    try:
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                files={"photo": f},
                data={"chat_id": chat_id, "caption": caption},
                timeout=30,
            )
        log("INFO", "Telegram 截图发送成功")
    except Exception as e:
        log("ERROR", f"Telegram 截图发送失败: {e}")


# ── 截图 / HTML dump ──────────────────────────────────────

def screenshot(sb, name):
    os.makedirs(SS_DIR, exist_ok=True)
    path = os.path.join(SS_DIR, f"{name}.png")
    try:
        sb.save_screenshot(path)
        log("INFO", f"截图: {name}")
        return path
    except Exception as e:
        log("ERROR", f"截图失败 ({name}): {e}")
        return ""


def dump_html(sb, name):
    os.makedirs(SS_DIR, exist_ok=True)
    path = os.path.join(SS_DIR, f"{name}.html")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(sb.get_page_source())
        log("INFO", f"HTML: {name}")
    except Exception:
        pass


def finish(sb, name, caption):
    dump_html(sb, name)
    img = screenshot(sb, name)
    if img:
        send_tg_photo(TG_TOKEN, TG_CHAT_ID, img, caption)


# ── JS / xdotool 辅助（操作 Discord 输入框需要）────────────

def js_eval(sb, expr):
    try:
        return sb.execute_script(f"return ({expr})")
    except Exception as e:
        log("WARN", f"js_eval 失败: {e}")
        return None


def xdo(args):
    env = {**os.environ, "DISPLAY": DISPLAY}
    subprocess.run(["xdotool"] + args, env=env, capture_output=True, check=False)


def mouse_click(x, y, label=""):
    mid_x = x + random.randint(-30, 30)
    mid_y = y + random.randint(-20, 20)
    xdo(["mousemove", "--", str(mid_x), str(mid_y)])
    time.sleep(random.uniform(0.1, 0.2))
    xdo(["mousemove", "--", str(x), str(y)])
    time.sleep(random.uniform(0.1, 0.25))
    xdo(["mousedown", "1"])
    time.sleep(random.uniform(0.08, 0.15))
    xdo(["mouseup", "1"])
    if label:
        log("INFO", f"  🖱️ 点击 ({x},{y}) {label}")


def keyboard_type(text):
    env = {**os.environ, "DISPLAY": DISPLAY}
    for ch in text:
        subprocess.run(
            ["xdotool", "type", "--clearmodifiers", "--delay", "80", "--", ch],
            env=env, capture_output=True,
        )
        time.sleep(random.uniform(0.05, 0.15))


def keyboard_key(key):
    xdo(["key", "--clearmodifiers", key])
    time.sleep(0.1)


def get_element_screen_pos(sb, selector):
    safe = selector.replace('"', '\\"')
    info = js_eval(sb,
        f'(function(){{'
        f'  var el = document.querySelector("{safe}");'
        f'  if (!el) return null;'
        f'  var r = el.getBoundingClientRect();'
        f'  return {{'
        f'    "cx": r.left + r.width / 2,'
        f'    "cy": r.top + r.height / 2,'
        f'    "sx": window.screenX || 0,'
        f'    "sy": window.screenY || 0,'
        f'    "dh": window.outerHeight - window.innerHeight,'
        f'    "dw": (window.outerWidth - window.innerWidth) / 2'
        f'  }};'
        f'}})()'
    )
    if not info:
        return None, None
    ox = int(info.get("sx", 0) + info.get("dw", 0))
    oy = int(info.get("sy", 0) + info.get("dh", 0))
    return int(info["cx"]) + ox, int(info["cy"]) + oy


def get_page_text(sb):
    return js_eval(sb, "document.body.innerText") or ""


def open_and_wait(sb, url, must_contain=None, timeout=25, settle=6, min_len=50):
    """打开 URL 并等待内容真正加载出来，缓解慢网络下过早检测导致的误判。

    - settle: 打开后先等待的秒数，给重型 SPA（如 Discord）渲染时间，
      避免过早调用 execute_script 触发 "script timeout"
    - must_contain: 页面文本需包含的关键字（如 "NopeCHA"）；命中即返回
    - 否则等待文本长度达到 min_len 视为已加载
    返回最后一次读取到的页面文本。
    """
    try:
        sb.open(url)
    except Exception as e:
        log("WARN", f"打开 {url} 失败: {e}")

    # 先给重型 SPA 一点渲染时间，避免过早 execute_script 卡死
    time.sleep(settle)

    end = time.time() + timeout
    last = ""
    while time.time() < end:
        txt = get_page_text(sb)  # js_eval 内部已 try/except，超时返回 ""
        last = txt
        if must_contain:
            if must_contain in txt:
                return txt
        elif txt and len(txt) >= min_len:
            return txt
        time.sleep(2)
    return last


# ── NopeCHA CDK 领取 & 注入 ───────────────────────────────

def extract_latest_cdk(text):
    """从 Discord 私信文本里提取最新一条 NopeCHA CDK，并判断是否 24h 内。

    注意：key 提取与时间标记解析解耦，避免因括号/文案变化整体匹配失败。
    时间标记兼容中英文括号 ( ) （ ）。
    """
    if not text:
        return "", False

    # 1) 先独立提取所有 key，取最后一条（最新）
    keys = re.findall(
        r"Here is your Discord key for NopeCHA[:\s]+([A-Za-z0-9]{6,})",
        text, re.IGNORECASE,
    )
    if not keys:
        return "", False
    cdk = keys[-1]

    # 2) 在该 key 之后查找括号内的时间标记（兼容半角/全角括号）
    m = re.search(
        re.escape(cdk) + r".*?[\(（]([^\)）]+)[\)）]",
        text, re.IGNORECASE | re.DOTALL,
    )
    time_label = m.group(1).strip() if m else ""
    is_recent = (("内" in time_label) or ("within" in time_label.lower())) if time_label else False
    log("INFO", f"  📋 最新 CDK 时间标记: {time_label or '<无>'} | 24h内: {is_recent}")
    return cdk, is_recent


def inject_nopecha_key(sb, cdk):
    """通过 nopecha.com/setup 激活 NopeCHA 插件额度。"""
    if not cdk:
        return
    log("INFO", "💉 注入 key 到 NopeCHA 插件...")
    sb.open(f"https://nopecha.com/setup#{cdk}")
    time.sleep(3)
    log("INFO", f"✅ key 注入完成: {cdk[:4]}****{cdk[-4:]}")


def send_command_and_poll(sb):
    """在 NopeCHA Discord 频道发送 !nopecha，并轮询私信提取 CDK。"""
    open_and_wait(sb, DISCORD_CHANNEL_URL, timeout=25)

    log("INFO", "🖱️ 定位消息输入框...")
    editor_sel = '[data-slate-editor="true"]'
    # 显式等输入框元素出现（Discord 加载慢），再多次重试拿屏幕坐标
    try:
        sb.wait_for_element_present(editor_sel, timeout=25)
    except Exception:
        pass

    ix, iy = None, None
    for _ in range(6):
        ix, iy = get_element_screen_pos(sb, editor_sel)
        if ix:
            break
        time.sleep(2)

    if not ix:
        log("ERROR", "❌ 找不到 Discord 输入框")
        screenshot(sb, "discord_no_input")
        return ""

    mouse_click(ix, iy, "输入框")
    time.sleep(random.uniform(0.5, 1.0))

    log("INFO", "⌨️ 输入 !nopecha ...")
    keyboard_key("ctrl+a")
    time.sleep(0.2)
    keyboard_type("!nopecha")
    time.sleep(random.uniform(0.4, 0.8))
    keyboard_key("Return")

    log("INFO", "✅ 命令已发送，等待私聊回复...")
    time.sleep(5)

    for attempt in range(20):
        time.sleep(3)
        log("INFO", f"  [{attempt*3+3}s] 打开私聊检查...")

        # 等私信里真正出现 key 消息正文再解析（侧边栏只有机器人名，不足以判定已加载）
        page_text = open_and_wait(sb, DISCORD_DM_URL, must_contain="Here is your Discord key", timeout=15)
        if page_text:
            # 刚发完命令，私信里最新一条即为新 key，只要提取到就接受
            cdk, _is_recent = extract_latest_cdk(page_text)
            if cdk:
                log("INFO", f"✅ 提取到 CDK: {cdk[:4]}****{cdk[-4:]}")
                return cdk

        sb.open(DISCORD_CHANNEL_URL)
        time.sleep(2)

    return ""


def ensure_cdk(sb):
    """
    确保 NopeCHA 插件有可用 key。
    优先级: 环境变量 NOPECHA_KEY > 私信 24h 内旧 CDK > 发命令领新 CDK。
    """
    log("INFO", "=" * 50)
    log("INFO", "🔑 开始 CDK 检查流程")
    log("INFO", "=" * 50)

    # 0) 显式配置了 key，直接注入
    if NOPECHA_KEY:
        log("INFO", "使用环境变量 NOPECHA_KEY，跳过 Discord 领取")
        inject_nopecha_key(sb, NOPECHA_KEY)
        return NOPECHA_KEY

    # 1) 打开 Discord，确认登录态
    open_and_wait(sb, DISCORD_CHANNEL_URL, timeout=20)
    try:
        cur = sb.get_current_url().lower()
    except Exception:
        cur = ""
    if "login" in cur:
        msg = "❌ Discord 登录态失效，请重新初始化 Profile"
        log("ERROR", msg)
        send_tg_text(TG_TOKEN, TG_CHAT_ID, f"VPS8 签到失败: {msg}")
        return ""

    # 2) 私信里找 24h 内的旧 CDK
    log("INFO", "🔍 检查私聊是否已有24h内的 CDK...")
    page_text = open_and_wait(sb, DISCORD_DM_URL, must_contain="Here is your Discord key", timeout=25)
    log("INFO", f"  (DM 文本长度: {len(page_text or '')})")
    cdk, is_recent = extract_latest_cdk(page_text) if page_text else ("", False)

    if cdk and is_recent:
        log("INFO", f"✅ 已有24h内的 CDK，直接注入: {cdk[:4]}****{cdk[-4:]}")
        inject_nopecha_key(sb, cdk)
        return cdk

    # 3) 去频道发命令领新的
    log("INFO", "📭 无24h内的 CDK，去频道发送命令领取...")
    cdk = send_command_and_poll(sb)
    if not cdk:
        msg = "❌ 未能获取 CDK，请检查 Discord"
        log("ERROR", msg)
        send_tg_text(TG_TOKEN, TG_CHAT_ID, f"VPS8 签到失败: {msg}")
        return ""

    inject_nopecha_key(sb, cdk)
    return cdk


# ── 页面状态解析 ──────────────────────────────────────────

def is_signed(html):
    m = re.search(r"今日签到状态：\s*([^\n<]+)", html)
    if m:
        status = m.group(1).strip()
        if status == "已签到":
            return True
        if status == "未签到":
            return False

    if "签到成功" in html and "今日签到状态" not in html:
        return True
    if "未签到" not in html and "当前连续签到" in html:
        return True
    return False


def extract_points(html):
    m = re.search(r"当前积分：\s*<strong>(\d+)</strong>", html)
    return m.group(1) if m else "未知"


def build_result_caption(account, result_text, before_points=None, current_points=None, fail_reason=None):
    lines = [
        "VPS8 每日签到",
        f"登录方式: {LOGIN_PROVIDER}",
        f"账号: {account}",
        f"签到结果: {result_text}",
    ]
    if before_points is not None:
        lines.append(f"签到前积分: {before_points}")
    if current_points is not None:
        lines.append(f"当前积分: {current_points}")
    if fail_reason:
        lines.append(f"失败原因: {fail_reason}")
    return "\n".join(lines)


def wait_any_visible(sb, selectors, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        for sel in selectors:
            try:
                if sb.is_element_visible(sel):
                    return sel
            except Exception:
                pass
        time.sleep(0.5)
    return None


def get_page_blob(sb):
    body_text = ""
    html = ""
    try:
        if sb.is_element_present("body"):
            body_text = sb.get_text("body") or ""
    except Exception:
        pass
    try:
        html = sb.get_page_source() or ""
    except Exception:
        pass
    return f"{body_text}\n{html}"


def detect_chrome_error(blob):
    checks = [
        ("ERR_EMPTY_RESPONSE", "ERR_EMPTY_RESPONSE"),
        ("ERR_TUNNEL_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED"),
        ("ERR_PROXY_CONNECTION_FAILED", "ERR_PROXY_CONNECTION_FAILED"),
        ("This page isn", "Chrome 错误页"),
        ("didn't send any data", "目标站无响应"),
    ]
    for key, msg in checks:
        if key in blob:
            return msg
    return ""


def detect_browser_exit_ip(sb, timeout=20):
    last_err = "unknown"
    for url in BROWSER_IP_PROBE_URLS:
        try:
            sb.open(url)
            end = time.time() + timeout
            while time.time() < end:
                blob = get_page_blob(sb)
                err = detect_chrome_error(blob)
                if err:
                    return False, f"{url} -> {err}"
                ip = extract_ip_from_text(blob)
                if ip:
                    return True, ip
                time.sleep(1)
            last_err = f"{url} -> timeout/no ip"
        except Exception as e:
            last_err = f"{url} -> {e}"
    return False, last_err


# ── hCaptcha（交给 NopeCHA 插件解）─────────────────────────

def has_hcaptcha(sb):
    try:
        return bool(sb.execute_script(
            "return !!document.querySelector("
            "'.h-captcha, iframe[src*=\"hcaptcha\"], "
            "textarea[name=\"h-captcha-response\"], "
            "textarea[name=\"g-recaptcha-response\"]');"
        ))
    except Exception:
        return False


def wait_hcaptcha_solved(sb, scene="page", timeout=90):
    if not has_hcaptcha(sb):
        log("INFO", f"[{scene}] 未检测到 hCaptcha，跳过")
        return True

    log("INFO", f"[{scene}] 检测到 hCaptcha，等待 NopeCHA 自动求解（最多 {timeout}s）...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            solved = sb.execute_script(
                """
                var t = document.querySelector('textarea[name="h-captcha-response"]')
                     || document.querySelector('textarea[name="g-recaptcha-response"]');
                return !!(t && t.value && t.value.length > 20);
                """
            )
        except Exception:
            solved = False

        if solved:
            log("INFO", f"[{scene}] hCaptcha 已由 NopeCHA 解决")
            screenshot(sb, f"hcaptcha_ok_{scene}")
            return True
        time.sleep(2)

    log("ERROR", f"[{scene}] hCaptcha 求解超时")
    screenshot(sb, f"hcaptcha_fail_{scene}")
    return False


# ── 第三方登录 ────────────────────────────────────────────

def login_via_oauth(sb):
    provider_url = PROVIDER_LOGIN_URLS.get(LOGIN_PROVIDER)
    if not provider_url:
        return False, f"不支持的 LOGIN_PROVIDER: {LOGIN_PROVIDER}（可选 github/google/nodeloc）"

    log("INFO", f"打开登录页，使用第三方登录: {LOGIN_PROVIDER}")
    if hasattr(sb, "uc_open_with_reconnect"):
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=8)
    else:
        sb.open(LOGIN_URL)
    time.sleep(4)
    screenshot(sb, "01_login_loaded")

    provider_selectors = {
        "github":  ["a[href*='/github/login']", "a[title='GitHub']"],
        "google":  ["a[href*='/google/login']", "a[title='Google']"],
        "nodeloc": ["a[href*='/nodeloc/login']", "a[title='Nodeloc']"],
    }.get(LOGIN_PROVIDER, [])

    clicked = False
    sel = wait_any_visible(sb, provider_selectors, timeout=8)
    if sel:
        try:
            sb.click(sel)
            clicked = True
            log("INFO", f"已点击第三方登录按钮: {sel}")
        except Exception as e:
            log("WARN", f"点击第三方按钮失败，改为直接访问 URL: {e}")

    if not clicked:
        log("INFO", f"直接访问第三方登录 URL: {provider_url}")
        sb.open(provider_url)

    time.sleep(6)
    wait_hcaptcha_solved(sb, "oauth")
    time.sleep(3)

    cur = ""
    try:
        cur = sb.get_current_url().lower()
    except Exception:
        pass

    if "/login" in cur or "oauth" in cur or "authorize" in cur or ("signin" in cur and "points" not in cur):
        blob = get_page_blob(sb)
        net_err = detect_chrome_error(blob)
        screenshot(sb, "03_login_not_finished")
        dump_html(sb, "03_login_not_finished")
        if net_err:
            return False, f"第三方登录网络异常: {net_err}，当前URL: {cur}"
        return False, (
            f"第三方登录未完成，当前URL: {cur}。"
            f"请确认 Profile({PROFILE_DIR}) 已登录 {LOGIN_PROVIDER} 账号并已授权 VPS8"
        )

    screenshot(sb, "03_login_success")
    log("INFO", f"第三方登录成功，当前URL: {cur}")
    return True, None


# ── 签到 ──────────────────────────────────────────────────

def do_signin(sb):
    log("INFO", "打开签到页")
    if hasattr(sb, "uc_open_with_reconnect"):
        sb.uc_open_with_reconnect(SIGNIN_URL, reconnect_time=8)
    else:
        sb.open(SIGNIN_URL)
    time.sleep(5)
    screenshot(sb, "04_signin_loaded")

    try:
        sb.wait_for_element_visible("strong", timeout=10)
    except Exception:
        pass

    html = sb.get_page_source()
    before_points = extract_points(html)
    if is_signed(html):
        return True, "今日已签到", before_points, before_points, None

    if not wait_hcaptcha_solved(sb, "signin"):
        return False, "签到失败", before_points, before_points, "签到 hCaptcha 求解失败"

    if not sb.is_element_visible("#points-signin-submit"):
        screenshot(sb, "04_signin_no_button")
        dump_html(sb, "04_signin_no_button")
        return False, "签到失败", before_points, before_points, "找不到签到按钮"

    sb.click("#points-signin-submit")
    log("INFO", "已点击签到")
    time.sleep(3)

    wait_hcaptcha_solved(sb, "signin_after_click")

    for i in range(10):
        time.sleep(2)
        html = sb.get_page_source()
        if is_signed(html):
            current_points = extract_points(html)
            return True, "签到成功", before_points, current_points, None
        log("INFO", f"等待签到结果... ({i + 1})")

    current_points = extract_points(sb.get_page_source())
    return False, "签到失败", before_points, current_points, "未确认签到状态"


# ── 主流程 ────────────────────────────────────────────────

def vps8_checkin():
    global DISPLAY
    account_label = f"{LOGIN_PROVIDER} OAuth"

    log("INFO", "=" * 50)
    log("INFO", "🚀 启动: VPS8 每日签到（第三方登录 + NopeCHA 自动 CDK）")
    log("INFO", f"代理: {mask_proxy(PROXY)}")
    log("INFO", f"登录方式: {LOGIN_PROVIDER}")
    log("INFO", f"Profile: {PROFILE_DIR}")
    log("INFO", f"NopeCHA 扩展目录: {NOPECHA_EXT_DIR}")
    log("INFO", "=" * 50)

    if not os.path.isdir(NOPECHA_EXT_DIR):
        msg = f"NopeCHA 扩展目录不存在: {NOPECHA_EXT_DIR}（请放置解压后的插件）"
        log("ERROR", msg)
        send_tg_text(TG_TOKEN, TG_CHAT_ID, f"VPS8 签到失败: {msg}")
        return False, msg

    ok_req_ip, req_ip_or_err = detect_exit_ip(proxy=PROXY or None)
    if ok_req_ip:
        log("INFO", f"请求出口IP(脱敏): {mask_ip(req_ip_or_err)}")
    else:
        log("WARN", f"请求出口IP检测失败: {req_ip_or_err}")

    display = None
    if platform.system().lower() == "linux" and not os.environ.get("DISPLAY"):
        try:
            display = Display(visible=False, size=(1366, 768))
            display.start()
            log("INFO", "虚拟显示已启动")
        except Exception as e:
            msg = f"虚拟显示失败: {e}"
            send_tg_text(TG_TOKEN, TG_CHAT_ID, f"VPS8 签到失败: {msg}")
            return False, msg

    try:
        sb_kwargs = dict(
            browser="chrome",
            headed=True,
            headless=False,
            xvfb=False,
            locale="zh-CN",
            user_data_dir=PROFILE_DIR,
            extension_dir=NOPECHA_EXT_DIR,
            chromium_arg=(
                "--no-sandbox,"
                "--disable-dev-shm-usage,"
                "--disable-gpu,"
                "--window-size=1366,768"
            ),
        )
        if PROXY:
            sb_kwargs["proxy"] = PROXY

        with SB(**sb_kwargs) as sb:
            # 更新 DISPLAY（xvfb-run 会注入）
            DISPLAY = os.environ.get("DISPLAY", DISPLAY)

            # 调大脚本/页面超时，避免重型 SPA 下 execute_script 触发 script timeout
            try:
                sb.driver.set_script_timeout(30)
                sb.driver.set_page_load_timeout(60)
            except Exception as e:
                log("WARN", f"设置超时失败（忽略）: {e}")

            # Step 1: 确保 NopeCHA 有可用 CDK（自动领取 + 注入）
            cdk = ensure_cdk(sb)
            if not cdk:
                return False, "未能获取/注入 NopeCHA CDK"

            # 浏览器出口 IP 检测
            ok_bip, bip_or_err = detect_browser_exit_ip(sb, timeout=20)
            if ok_bip:
                log("INFO", f"浏览器出口IP(脱敏): {mask_ip(bip_or_err)}")
            else:
                screenshot(sb, "00_browser_ip_failed")
                dump_html(sb, "00_browser_ip_failed")
                reason = f"浏览器出口IP检测失败: {bip_or_err}"
                send_tg_text(TG_TOKEN, TG_CHAT_ID, f"VPS8 签到失败: {reason}")
                return False, reason

            # Step 2: 第三方登录
            ok, reason = login_via_oauth(sb)
            if not ok:
                finish(sb, "login_failed",
                       build_result_caption(account_label, "签到失败", fail_reason=reason))
                send_tg_text(TG_TOKEN, TG_CHAT_ID, f"VPS8 签到失败: {reason}")
                return False, reason

            # Step 3: 签到
            success, result_text, before_points, current_points, fail_reason = do_signin(sb)
            caption = build_result_caption(
                account_label, result_text, before_points, current_points, fail_reason
            )
            finish(sb, "signin_ok" if success else "signin_fail", caption)

            if not success:
                send_tg_text(TG_TOKEN, TG_CHAT_ID, f"VPS8 签到失败: {fail_reason}")

            return success, (result_text if success else fail_reason)

    except Exception as e:
        traceback.print_exc()
        msg = f"异常: {str(e)[:200]}"
        send_tg_text(TG_TOKEN, TG_CHAT_ID, f"VPS8 签到异常: {msg}")
        return False, msg
    finally:
        if display:
            display.stop()


if __name__ == "__main__":
    ok, msg = vps8_checkin()
    log("INFO", msg)
    if not ok:
        sys.exit(1)
