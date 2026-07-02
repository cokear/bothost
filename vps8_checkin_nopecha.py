import os
import re
import sys
import time
import platform
import traceback
import requests

from pyvirtualdisplay import Display
from seleniumbase import SB


# ================= 配置区域 =================
BASE_URL   = os.getenv("BASE_URL", "https://vps8.zz.cd")
LOGIN_URL  = os.getenv("LOGIN_URL", f"{BASE_URL}/login")
SIGNIN_URL = os.getenv("SIGNIN_URL", f"{BASE_URL}/points/signin")

# 第三方登录方式: github / google / nodeloc
LOGIN_PROVIDER = os.getenv("LOGIN_PROVIDER", "github").strip().lower()
PROVIDER_LOGIN_URLS = {
    "github":  f"{BASE_URL}/github/login",
    "google":  f"{BASE_URL}/google/login",
    "nodeloc": f"{BASE_URL}/nodeloc/login",
}

# 持久化 Chrome Profile —— 保存第三方登录态，避免每次重新登录
PROFILE_DIR = os.getenv(
    "BROWSER_USER_DATA_DIR",
    os.path.expanduser("~/.chrome-profile-vps8"),
)

# NopeCHA 插件目录（解压后的扩展文件夹，含 manifest.json）
# 默认取脚本同目录下的 chromium/ 文件夹（与第二个脚本一致）
NOPECHA_EXT_DIR = os.getenv(
    "NOPECHA_EXT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "chromium"),
)
# NopeCHA 套餐 key（可选）。设置后会通过 nopecha.com/setup#<key> 激活插件额度
NOPECHA_KEY = (os.getenv("NOPECHA_KEY") or "").strip()

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


# ── NopeCHA 插件相关 ──────────────────────────────────────

def inject_nopecha_key(sb):
    """若配置了 NOPECHA_KEY，则通过 nopecha.com/setup 激活插件额度。"""
    if not NOPECHA_KEY:
        log("INFO", "未配置 NOPECHA_KEY，使用插件默认额度")
        return
    try:
        log("INFO", "注入 NopeCHA key...")
        sb.open(f"https://nopecha.com/setup#{NOPECHA_KEY}")
        time.sleep(3)
        log("INFO", f"NopeCHA key 注入完成: {NOPECHA_KEY[:4]}****{NOPECHA_KEY[-4:]}")
    except Exception as e:
        log("WARN", f"NopeCHA key 注入失败: {e}")


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
    """
    等待 NopeCHA 插件自动解出 hCaptcha。
    通过检测 h-captcha-response 隐藏字段是否被填充来判断。
    """
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
    """
    使用第三方登录（GitHub / Google / Nodeloc）。
    依赖持久化 Profile 中已登录的第三方账号，点击后自动回跳完成登录。
    """
    provider_url = PROVIDER_LOGIN_URLS.get(LOGIN_PROVIDER)
    if not provider_url:
        return False, f"不支持的 LOGIN_PROVIDER: {LOGIN_PROVIDER}（可选 github/google/nodeloc）"

    log("INFO", f"打开登录页，使用第三方登录: {LOGIN_PROVIDER}")
    sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=8) if hasattr(sb, "uc_open_with_reconnect") else sb.open(LOGIN_URL)
    time.sleep(4)
    screenshot(sb, "01_login_loaded")

    # 优先点击页面上的第三方登录按钮；点不到就直接访问对应 OAuth URL
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

    # 等待授权 / 回跳完成
    time.sleep(6)

    # 若第三方页面出现 hCaptcha（部分场景），尝试让 NopeCHA 解
    wait_hcaptcha_solved(sb, "oauth")
    time.sleep(3)

    cur = ""
    try:
        cur = sb.get_current_url().lower()
    except Exception:
        pass

    # 仍停留在本站登录页 or 第三方授权页 = 未登录成功
    if "/login" in cur or "oauth" in cur or "authorize" in cur or "signin" in cur and "points" not in cur:
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

    # 签到动作可能触发 hCaptcha，交给 NopeCHA 解
    if not wait_hcaptcha_solved(sb, "signin"):
        return False, "签到失败", before_points, before_points, "签到 hCaptcha 求解失败"

    if not sb.is_element_visible("#points-signin-submit"):
        screenshot(sb, "04_signin_no_button")
        dump_html(sb, "04_signin_no_button")
        return False, "签到失败", before_points, before_points, "找不到签到按钮"

    sb.click("#points-signin-submit")
    log("INFO", "已点击签到")
    time.sleep(3)

    # 点击后可能再次弹出 hCaptcha
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
    account_label = f"{LOGIN_PROVIDER} OAuth"

    log("INFO", "=" * 50)
    log("INFO", "🚀 启动: VPS8 每日签到（第三方登录 + NopeCHA）")
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
            # 激活 NopeCHA 额度（若配置了 key）
            inject_nopecha_key(sb)

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

            # 第三方登录
            ok, reason = login_via_oauth(sb)
            if not ok:
                finish(sb, "login_failed",
                       build_result_caption(account_label, "签到失败", fail_reason=reason))
                send_tg_text(TG_TOKEN, TG_CHAT_ID, f"VPS8 签到失败: {reason}")
                return False, reason

            # 签到
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
