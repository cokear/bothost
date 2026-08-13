#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ulzix 每日签到 (DrissionPage + NopeCHA) 终极修复版
────────────────────────────────────────
修复点：
1. 消费 Token 后立即清空 `textarea`，彻底解决“连续弹出验证码时，脚本读取残留老 Token 导致抢跑”的问题。
2. 点击“立即签到”后，强制等待后续弹出的九宫格（如有）被处理完毕，并留足 8 秒钟时间让签到 AJAX 请求完成，防止页面刷新阻断请求。
"""

import os
import re
import sys
import time
import requests
from DrissionPage import ChromiumPage, ChromiumOptions

# ================= 配置区域 =================
PROXY_URL   = os.getenv("BROWSER_PROXY", "")
PROFILE_DIR = os.getenv("BROWSER_USER_DATA_DIR",
                        os.path.expanduser("~/.chrome-profile-ulzix"))

# NopeCHA 扩展目录（CDP 回退时备用）
NOPECHA_EXT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chromium")

# Ulzix
LOGIN_URL  = "https://idc-new.ulzix.com/login"
SIGNIN_URL = "https://idc-new.ulzix.com/pointmall/signin"

# Telegram
TG_TOKEN   = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

SS_DIR = "screenshots"
# ===========================================

# ── 日志 & 通知 ───────────────────────────────────────────

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def send_tg(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": message},
            timeout=10,
        )
    except Exception as e:
        log(f"⚠️ TG 推送失败: {e}")


def send_tg_screenshot(page, caption="debug"):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        path = f"/tmp/{caption}.png"
        page.get_screenshot(path=path)
        with open(path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                data={"chat_id": TG_CHAT_ID, "caption": caption},
                files={"photo": f},
                timeout=15,
            )
        log(f"📸 截图已推送 TG: {caption}")
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        log(f"⚠️ 截图推送失败: {e}")


def save_screenshot(page, name):
    os.makedirs(SS_DIR, exist_ok=True)
    path = os.path.join(SS_DIR, f"{name}.png")
    try:
        page.get_screenshot(path=path)
        return path
    except Exception as e:
        log(f"⚠️ 截图失败 ({name}): {e}")
        return ""


def mask_email(email):
    try:
        local, domain = (email or "").split("@", 1)
        if len(local) <= 2:
            masked = local[0] + "*"
        else:
            masked = local[0] + "*" * (len(local) - 2) + local[-1]
        return f"{masked}@{domain}"
    except Exception:
        return "***"


def parse_account(raw):
    value = (raw or "").strip()
    index = value.find(":")
    if index <= 0 or index == len(value) - 1:
        raise ValueError("ACCOUNTS 格式错误，应为 邮箱:密码")
    return value[:index].strip(), value[index + 1:].strip()


# ══════════════════════════════════════════════════════════
# ── Cloudflare hCaptcha (CDP) ──────────────────────────
# ══════════════════════════════════════════════════════════

def _is_hcaptcha_page(page):
    """检测页面是否有 hCaptcha"""
    try:
        return bool(page.run_js("""
            return !!(
                document.querySelector('.h-captcha') ||
                document.querySelector('iframe[src*="hcaptcha.com"]') ||
                document.querySelector('[data-hcaptcha-widget-id]') ||
                document.querySelector('textarea[name="h-captcha-response"]')
            );
        """))
    except Exception:
        return False


def _is_cf_page(page):
    """检测页面是否有 Cloudflare Turnstile"""
    title = page.title or ""
    if "Just a moment" in title or "Attention Required" in title:
        return True
    try:
        return bool(page.run_js("""
            return !!(
                document.querySelector('[id^="cf-chl-widget"]') ||
                document.querySelector('.cf-turnstile')
            );
        """))
    except Exception:
        return False


def _cf_iframe_exists(page):
    try:
        return page.run_js("""
            return document.querySelectorAll(
                'iframe[src*="challenges.cloudflare.com"], [id^="cf-chl-widget"]'
            ).length;
        """) > 0
    except Exception:
        return False


def _page_has_content(page):
    try:
        return page.run_js("""
            var body = document.body;
            if (!body) return false;
            var text = body.innerText || '';
            var cfKw = ['Just a moment','Attention Required','Verify you are human',
                         'Performing security verification','cf-chl-widget','cf-turnstile'];
            for (var i = 0; i < cfKw.length; i++) {
                if (text.indexOf(cfKw[i]) !== -1) return false;
            }
            return text.length > 50;
        """) or False
    except Exception:
        return False


def _get_cf_iframe_rect(page):
    try:
        search = page.run_cdp(
            "DOM.performSearch",
            query="iframe[src*='challenges.cloudflare.com']",
            includeUserAgentShadowDOM=True,
        )
        sid = search.get("searchId")
        cnt = search.get("resultCount", 0)
        if cnt > 0 and sid:
            results = page.run_cdp(
                "DOM.getSearchResults", searchId=sid, fromIndex=0, toIndex=cnt
            )
            for nid in results.get("nodeIds", []):
                try:
                    obj = page.run_cdp("DOM.resolveNode", nodeId=nid)
                    oid = obj["object"]["objectId"]
                    rr = page.run_cdp(
                        "Runtime.callFunctionOn",
                        objectId=oid,
                        functionDeclaration="""
                        function() {
                            var r = this.getBoundingClientRect();
                            return {left:r.left, top:r.top, width:r.width, height:r.height};
                        }""",
                        returnByValue=True,
                    )
                    rv = rr.get("result", {}).get("value", {})
                    if rv.get("width", 0) > 20 and rv.get("height", 0) > 20:
                        try:
                            page.run_cdp("DOM.discardSearchResults", searchId=sid)
                        except Exception:
                            pass
                        return rv
                except Exception:
                    continue
            try:
                page.run_cdp("DOM.discardSearchResults", searchId=sid)
            except Exception:
                pass
    except Exception as e:
        log(f"  ⚠️ CF iframe 定位失败: {e}")
    return None


def _cdp_click(page, vx, vy):
    try:
        page.run_cdp("Input.dispatchMouseEvent", type="mouseMoved",
                     x=vx, y=vy, button="none", clickCount=0)
        time.sleep(0.05)
        page.run_cdp("Input.dispatchMouseEvent", type="mousePressed",
                     x=vx, y=vy, button="left", clickCount=1)
        time.sleep(0.05)
        page.run_cdp("Input.dispatchMouseEvent", type="mouseReleased",
                     x=vx, y=vy, button="left", clickCount=1)
        return True
    except Exception as e:
        log(f"  ⚠️ CDP 点击失败: {e}")
        return False


def wait_for_cloudflare_cdp(page, timeout=90):
    start = time.time()
    click_count = 0
    while time.time() - start < timeout:
        if not _cf_iframe_exists(page) and _page_has_content(page):
            log("  ✅ Cloudflare 已通过")
            return True
        rect = _get_cf_iframe_rect(page)
        if rect:
            cx = int(rect["left"] + 12)
            cy = int(rect["top"] + rect["height"] / 2)
            if click_count == 0:
                log("  ⚡ CDP 点击 hCaptcha 复选框...")
            _cdp_click(page, cx, cy)
            click_count += 1
            for w in range(20):
                time.sleep(1)
                if not _cf_iframe_exists(page) and _page_has_content(page):
                    log(f"  ✅ Cloudflare 通过（点击 {click_count} 次，{w+1}s）")
                    return True
            log("  ⚠️ 点击后未通过，重试...")
        else:
            time.sleep(2)
    log(f"  ❌ Cloudflare 超时（{timeout}s）")
    send_tg_screenshot(page, "cf_timeout")
    return False


# ══════════════════════════════════════════════════════════
# ── NopeCHA API 解 hCaptcha ───────────────────────────
# ══════════════════════════════════════════════════════════

def inject_cdk_to_extension(page, cdk):
    if not cdk:
        return False
    log(f"💉 注入 API Key 到 NopeCHA 扩展: {cdk[:4]}****{cdk[-4:]}")
    page.get(f"https://nopecha.com/setup#{cdk}")
    time.sleep(5)
    log("✅ API Key 注入完成")
    return True


def wait_hcaptcha_solved(page, timeout=60):
    """【核心修复】等待 Token，并消费后立刻清空 textarea！"""
    log("  ⏳ 等待 NopeCHA 扩展自动解 hCaptcha...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            token = page.run_js("""
                var tas = document.querySelectorAll(
                    'textarea[name="h-captcha-response"],'
                    + 'textarea[id*="h-captcha-response"],'
                    + 'textarea[data-hcaptcha-response]'
                );
                for (var i = 0; i < tas.length; i++) {
                    var ta = tas[i];
                    if (ta && ta.value && ta.value.length > 20) {
                        var val = ta.value;
                        // 消费完毕后立即清空旧的值！绝对防止下一次死循环被旧 Token 骗过
                        ta.value = '';
                        return val.substring(0, 30);
                    }
                }
                return '';
            """)
            if token:
                log(f"  ✅ hCaptcha 已被扩展自动解决 (token: {token}...)")
                return True
        except Exception:
            pass
        time.sleep(2)
    log("  ⚠️ 扩展未在超时内解决 hCaptcha")
    return False


def click_hcaptcha_checkbox(page):
    try:
        iframe = page.run_js("""
            var frames = document.querySelectorAll('iframe[src*="hcaptcha.com"]');
            for (var f of frames) {
                var r = f.getBoundingClientRect();
                if (r.width > 50 && r.height > 50) {
                    return {x: r.left + 30, y: r.top + 30, w: r.width, h: r.height};
                }
            }
            return null;
        """)
        if iframe:
            log(f"  🖱️ 点击 hCaptcha 复选框 iframe ({iframe['x']:.0f}, {iframe['y']:.0f})")
            _cdp_click(page, int(iframe["x"]), int(iframe["y"]))
            return True
    except Exception as e:
        log(f"  ⚠️ 点击 hCaptcha 复选框失败: {e}")
    return False


def handle_captcha(page, cdk, scene="page", max_attempts=3):
    log(f"  → [{scene}] 等待 hCaptcha 被扩展自动解决...")

    for attempt in range(max_attempts):
        log(f"  [{scene}] 等待中 ({attempt + 1}/{max_attempts})...")
        if wait_hcaptcha_solved(page, timeout=30):
            return True
        # 兜底：点击复选框
        click_hcaptcha_checkbox(page)
        time.sleep(5)

    log(f"  ❌ [{scene}] hCaptcha 未解决")
    return False


# ══════════════════════════════════════════════════════════
# ── Ulzix 登录 ──────────────────────────────────────────
# ══════════════════════════════════════════════════════════

def ulzix_login(page, email, password, cdk):
    log("🔐 登录 Ulzix...")
    page.get(LOGIN_URL)
    time.sleep(5)

    if _is_cf_page(page) or _is_hcaptcha_page(page):
        handle_captcha(page, cdk, "login")

    time.sleep(3)
    save_screenshot(page, "01_login_loaded")

    try:
        email_el = page.ele("css:#email", timeout=10)
        if email_el:
            email_el.click()
            time.sleep(0.3)
            email_el.clear()
            email_el.input(email)
            log(f"  ✅ 邮箱: {mask_email(email)}")
    except Exception as e:
        log(f"  ⚠️ 邮箱填写失败: {e}")
        return False, "邮箱填写失败"

    time.sleep(0.5)

    try:
        pwd_el = page.ele("css:#password", timeout=5)
        if pwd_el:
            pwd_el.click()
            time.sleep(0.3)
            pwd_el.clear()
            pwd_el.input(password)
            log("  ✅ 密码已填写")
    except Exception as e:
        log(f"  ⚠️ 密码填写失败: {e}")
        return False, "密码填写失败"

    time.sleep(0.5)
    save_screenshot(page, "02_form_filled")

    try:
        submit = page.ele('css:button[type="submit"]', timeout=5)
        if submit:
            submit.click()
            log("  → 已点击登录按钮")
    except Exception as e:
        log(f"  ⚠️ 提交失败: {e}")
        return False, "登录按钮点击失败"

    time.sleep(6)

    if _is_cf_page(page) or _is_hcaptcha_page(page):
        handle_captcha(page, cdk, "login_callback")

    current_url = page.run_js("return location.href;") or ""
    if "login" in current_url.lower():
        log("  ❌ 登录失败")
        save_screenshot(page, "03_login_failed")
        return False, "登录失败"

    log("  ✅ 登录成功")
    save_screenshot(page, "03_login_success")
    return True, None


# ══════════════════════════════════════════════════════════
# ── Ulzix 签到 ──────────────────────────────────────────
# ══════════════════════════════════════════════════════════

def _setup_dialog_handler(page):
    try:
        page.run_js("""
            window.__dialogText = '';
            window.__dialogDismissed = false;
            var origAlert = window.alert;
            var origConfirm = window.confirm;
            var origPrompt = window.prompt;
            window.alert = function(msg) {
                window.__dialogText = msg;
                window.__dialogDismissed = true;
                console.log('[auto-dismiss alert]', msg);
            };
            window.confirm = function(msg) {
                window.__dialogText = msg;
                window.__dialogDismissed = true;
                console.log('[auto-dismiss confirm]', msg);
                return true;
            };
            window.prompt = function(msg) {
                window.__dialogText = msg;
                window.__dialogDismissed = true;
                console.log('[auto-dismiss prompt]', msg);
                return '';
            };
        """)
        log("  ✓ JS 弹窗处理器已注册")
    except Exception as e:
        log(f"  ⚠️ 弹窗处理器注册失败: {e}")


def _dismiss_sweetalert(page):
    try:
        page.run_js("""
            if (window.swal) { try { swal.close(); } catch(e) {} }
            var btn = document.querySelector('.swal-button.swal-button--confirm, .swal2-confirm');
            if (btn) btn.click();
            var close = document.querySelector('.swal-button.swal-button--cancel, .swal2-close');
            if (close) close.click();
        """)
    except Exception:
        pass


def dismiss_all_popups(page):
    _dismiss_sweetalert(page)
    _setup_dialog_handler(page)
    try:
        page.run_js("""
            var closeBtns = document.querySelectorAll(
                'div.modal-content span.close, span.close, .modal .close, .popup-close'
            );
            closeBtns.forEach(function(b) { if (b.offsetParent) b.click(); });
        """)
    except Exception:
        pass
    time.sleep(1)


def extract_points(html):
    match = re.search(r'data-points="(\d+)"', html)
    return match.group(1) if match else "未知"


def points_to_int(value):
    try:
        return int(str(value).replace(",", "").strip())
    except Exception:
        return None


def is_signed(html):
    if "今日还未签到" in html:
        return False
    if "签到成功" in html or "今日已签到" in html:
        return True
    if 'id="btn-signin"' in html or "立即签到" in html:
        return False
    return False


def do_signin(page, cdk):
    log("📝 打开签到页...")
    page.get(SIGNIN_URL)
    _setup_dialog_handler(page)
    time.sleep(10)

    if _is_cf_page(page) or _is_hcaptcha_page(page):
        log("  🔍 检测到验证码（CF/hCaptcha），尝试解决...")
        handle_captcha(page, cdk, "signin_page1")
    time.sleep(10)

    save_screenshot(page, "04_signin_loaded")

    html = page.run_js("return document.body.innerHTML;") or ""
    before_points = extract_points(html)

    if is_signed(html):
        log("  ℹ️ 今日已签到")
        return True, "今日已签到", before_points, before_points, None

    try:
        btn = page.ele('css:button.ncmp__btn', timeout=3)
        if btn:
            btn.click()
            log("  ✓ 关闭 cookie 弹窗")
            time.sleep(3)
    except Exception:
        pass

    if _is_cf_page(page) or _is_hcaptcha_page(page):
        log("  🔍 检测到验证码（CF/hCaptcha），尝试解决...")
        handle_captcha(page, cdk, "signin_page2")

    if _is_hcaptcha_page(page):
        log("  🔍 hCaptcha 仍存在，强制等待解决...")
        handle_captcha(page, cdk, "signin_before_click", max_attempts=5)

    try:
        btn = page.ele("css:#btn-signin", timeout=10)
        if not btn:
            log("  ❌ 找不到签到按钮")
            return False, "签到失败", before_points, before_points, "找不到签到按钮"

        btn_text = btn.text.strip()
        log(f"  按钮文本: '{btn_text}'")

        enabled = page.run_js("""
            var el = document.querySelector('#btn-signin');
            return el ? !el.disabled : false;
        """)
        if not enabled:
            log("  ⚠️ 签到按钮不可用")
            return False, "签到失败", before_points, before_points, "签到按钮不可用"

        btn.click()
        log("  ✅ 已点击签到按钮")
        _setup_dialog_handler(page)
    except Exception as e:
        log(f"  ⚠️ 签到按钮操作失败: {e}")
        return False, "签到失败", before_points, before_points, str(e)[:100]

    # 【核心修复】点击按钮后，可能会触发九宫格验证，此处必须留足时间等待处理！
    log("  🔍 检查点击签到后是否触发了新的验证码...")
    time.sleep(3)
    if _is_hcaptcha_page(page) or _is_cf_page(page):
        log("  ⚠️ 果然弹出了验证码！等待扩展火速破解...")
        handle_captcha(page, cdk, "signin_post_click", max_attempts=5)
    
    # 哪怕没有弹出验证码，也要让子弹飞一会，等后台把 AJAX 请求彻底处理完
    log("  ⏳ 正在等待服务器登记签到结果...")
    time.sleep(8)
    
    # 最后清理一下可能的成功/失败弹窗
    dismiss_all_popups(page)
    
    log("  🔄 强制刷新页面获取最新状态...")
    page.refresh()
    time.sleep(5)

    before_val = points_to_int(before_points)
    for i in range(15):
        time.sleep(2)
        html = page.run_js("return document.body.innerHTML;") or ""
        current_points = extract_points(html)
        current_val = points_to_int(current_points)

        if (before_val is not None and current_val is not None
                and current_val > before_val):
            log(f"  ✅ 签到成功！积分: {before_points} → {current_points}")
            save_screenshot(page, "05_signin_success")
            return True, "签到成功", before_points, current_points, None

        if is_signed(html):
            log(f"  ✅ 文案确认签到成功，积分: {current_points}")
            save_screenshot(page, "05_signin_success")
            return True, "签到成功", before_points, current_points, None

        log(f"  ⏳ 等待签到结果... ({i + 1})")

    current_points = extract_points(
        page.run_js("return document.body.innerHTML;") or ""
    )
    log("  ⚠️ 签到状态未确认")
    save_screenshot(page, "05_signin_uncertain")
    return False, "签到失败", before_points, current_points, "未确认签到状态"


def build_result_caption(email, result_text, before_points=None,
                         current_points=None, fail_reason=None):
    lines = [
        "🎮 Ulzix 每日签到",
        f"📧 账号：{mask_email(email)}",
        f"📊 结果: {result_text}",
    ]
    if before_points is not None:
        lines.append(f"🎉 签到前积分: {before_points}")
    if current_points is not None:
        lines.append(f"💰 当前积分: {current_points}")
    if fail_reason:
        lines.append(f"❌ 失败原因: {fail_reason}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# ── 主流程 ──────────────────────────────────────────────
# ══════════════════════════════════════════════════════════

def main():
    tg_token = os.getenv("TG_BOT_TOKEN")
    tg_chat_id = os.getenv("TG_CHAT_ID")
    account_raw = os.getenv("ACCOUNTS")
    
    if not account_raw:
        log("❌ 缺少 ACCOUNTS 环境变量（邮箱:密码）")
        return False

    try:
        email, password = parse_account(account_raw)
    except Exception as e:
        log(f"❌ {e}")
        return False

    log("=" * 50)
    log("🚀 Ulzix 每日签到 (DrissionPage + NopeCHA)")
    log("=" * 50)

    co = ChromiumOptions()
    co.set_argument('--no-sandbox')
    co.set_argument('--disable-dev-shm-usage')
    co.set_argument('--disable-gpu')
    co.set_argument('--window-size=1280,900')
    co.set_user_data_path(PROFILE_DIR)

    if os.path.isdir(NOPECHA_EXT_DIR):
        co.add_extension(NOPECHA_EXT_DIR)
        log(f"📦 加载扩展: {NOPECHA_EXT_DIR}")
    else:
        log(f"⚠️ 扩展目录不存在: {NOPECHA_EXT_DIR}")

    if PROXY_URL:
        co.set_argument(f"--proxy-server={PROXY_URL}")
        log(f"🌐 代理: {PROXY_URL}")

    log("🔧 启动浏览器...")
    try:
        page = ChromiumPage(co)
    except Exception as e:
        log(f"❌ 浏览器启动失败: {e}")
        return False

    success = False
    result_text = ""
    before_points = "未知"
    current_points = "未知"
    fail_reason = ""
    cdk = ""

    try:
        _setup_dialog_handler(page)

        cdk = os.getenv("NOPECHA_KEY", "")
        if cdk:
            log(f"🔑 检测到环境变量 NOPECHA_KEY，准备注入扩展...")
            inject_cdk_to_extension(page, cdk)
        else:
            log("ℹ️ 未检测到 NOPECHA_KEY 环境变量，假设浏览器配置中已填写 API Key")

        ok, reason = ulzix_login(page, email, password, cdk)
        if not ok:
            fail_reason = reason
            send_tg_screenshot(page, "login_failed")
            send_tg(build_result_caption(email, "登录失败", fail_reason=reason))
            return False

        success, result_text, before_points, current_points, fail_reason = \
            do_signin(page, cdk)

        caption = build_result_caption(
            email, result_text, before_points, current_points, fail_reason
        )
        send_tg_screenshot(page, "signin_ok" if success else "signin_fail")
        send_tg(caption)

    except Exception as e:
        log(f"❌ 运行异常: {e}")
        send_tg_screenshot(page, "error")
        send_tg(f"❌ Ulzix 签到异常: {str(e)[:200]}")
    finally:
        try:
            page.quit()
        except Exception:
            pass

    if success:
        log(f"\n✅ 签到完成！积分: {before_points} → {current_points}")
    else:
        log(f"\n⚠️ 签到失败: {fail_reason}")

    return success


if __name__ == "__main__":
    ok = main()
    if not ok:
        sys.exit(1)
