#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot-Hosting 金币自动领取 (DrissionPage 版) - 纯净版
- Discord OAuth 登录 Bot-Hosting（替代 token 注入，免过期）
- Bot-Hosting 自动领取金币
- Cloudflare Turnstile 自动处理 (CDP)
"""

import time
import os
import re
import random
import requests
from DrissionPage import ChromiumPage, ChromiumOptions

# ================= 配置区域 =================
PROXY_URL       = os.getenv("BROWSER_PROXY", "")
PROFILE_DIR     = os.getenv("BROWSER_USER_DATA_DIR",
                            os.path.expanduser("~/.chrome-profile-discord"))
NOPECHA_EXT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chromium")

TARGET_URL = "https://legacy.bot-hosting.net/panel/"
EARN_URL   = "https://legacy.bot-hosting.net/panel/earn"
LOGIN_URL  = "https://legacy.bot-hosting.net/login/discord"
TOKEN      = os.getenv("TOKEN")

TG_TOKEN   = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

IS_CI   = os.getenv("CI") == "true"
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
            timeout=10
        )
    except Exception as e:
        log(f"⚠️ TG 推送失败: {e}")


def send_tg_screenshot(page, caption="debug"):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        path = f"/tmp/{caption}.png"
        page.get_screenshot(path=path)
        with open(path, 'rb') as f:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                data={"chat_id": TG_CHAT_ID, "caption": caption},
                files={"photo": f},
                timeout=15
            )
        log(f"📸 截图已推送 TG: {caption}")
        os.remove(path)
    except Exception as e:
        log(f"⚠️ 截图推送失败: {e}")


# ── Cloudflare Turnstile 处理 (CDP) ──────────────────────

def _is_cf_page(page):
    title = page.title or ""
    if "Just a moment" in title or "Attention Required" in title:
        return True
    try:
        return bool(page.run_js("""
            return !!(
                document.getElementById('VXzI4') ||
                document.querySelector('[id^="cf-chl-widget"]') ||
                document.querySelector('h2#kxxo4') ||
                document.querySelector('.cf-turnstile')
            );
        """))
    except:
        return False


def _cf_passed(page):
    try:
        token = page.run_js("""
            var el = document.querySelector('[id$="_response"]');
            return el ? el.value : '';
        """)
        if token:
            return True
    except:
        pass
    title = page.title or ""
    if "Just a moment" not in title and "Attention Required" not in title:
        try:
            if not _is_cf_page(page):
                return True
        except:
            return True
    return False


def _get_cf_iframe_rect(page):
    try:
        search = page.run_cdp(
            "DOM.performSearch",
            query="iframe[src*='challenges.cloudflare.com']",
            includeUserAgentShadowDOM=True
        )
        sid = search.get("searchId")
        cnt = search.get("resultCount", 0)

        if cnt > 0 and sid:
            results = page.run_cdp(
                "DOM.getSearchResults",
                searchId=sid, fromIndex=0, toIndex=cnt
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
                            return {
                                left: r.left, top: r.top,
                                width: r.width, height: r.height
                            };
                        }
                        """,
                        returnByValue=True
                    )
                    rv = rr.get("result", {}).get("value", {})
                    if rv.get("width", 0) > 20 and rv.get("height", 0) > 20:
                        try:
                            page.run_cdp("DOM.discardSearchResults", searchId=sid)
                        except:
                            pass
                        return rv
                except:
                    continue
            try:
                page.run_cdp("DOM.discardSearchResults", searchId=sid)
            except:
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
        log(f"  🖱️ CDP 点击: ({vx}, {vy})")
        return True
    except Exception as e:
        log(f"  ⚠️ CDP 点击失败: {e}")
        return False


def _cf_iframe_exists(page):
    try:
        result = page.run_js("""
            return document.querySelectorAll(
                'iframe[src*="challenges.cloudflare.com"], [id^="cf-chl-widget"]'
            ).length;
        """)
        return result and result > 0
    except:
        return False


def _page_has_content(page):
    try:
        return page.run_js("""
            var body = document.body;
            if (!body) return false;
            var text = body.innerText || '';
            var html = body.innerHTML || '';
            var cfKeywords = [
                'Just a moment', 'Attention Required', 'Verify you are human',
                'Performing security verification', 'Checking if the site',
                'challenge-platform', 'cf-chl-widget', 'cf-turnstile'
            ];
            for (var i = 0; i < cfKeywords.length; i++) {
                if (text.indexOf(cfKeywords[i]) !== -1 || html.indexOf(cfKeywords[i]) !== -1) {
                    return false;
                }
            }
            return text.length > 50;
        """) or False
    except:
        return False


def wait_for_cloudflare(page, timeout=90):
    start = time.time()
    click_count = 0

    while time.time() - start < timeout:
        if not _cf_iframe_exists(page) and _page_has_content(page):
            log("  ✅ Cloudflare 已通过（页面已加载）")
            return True

        rect = _get_cf_iframe_rect(page)
        if rect:
            cx = int(rect["left"] + 12)
            cy = int(rect["top"] + rect["height"] / 2)

            if click_count == 0:
                log(f"  ⚡ 检测到 Cloudflare Turnstile，点击复选框...")

            _cdp_click(page, cx, cy)
            click_count += 1

            for wait_i in range(20):
                time.sleep(1)
                iframe_gone = not _cf_iframe_exists(page)
                page_loaded = _page_has_content(page)
                if iframe_gone and page_loaded:
                    log(f"  ✅ Cloudflare 通过（点击 {click_count} 次，{wait_i+1}s）")
                    return True
                if wait_i % 5 == 4:
                    log(f"  ⏳ 等待 CF 消失+页面加载... ({wait_i+1}s) iframe={not iframe_gone} content={page_loaded}")

            log(f"  ⚠️ 点击后未通过，重试...")
        else:
            time.sleep(2)

    log(f"  ❌ Cloudflare 验证超时（{timeout}s）")
    send_tg_screenshot(page, "cf_timeout")
    return False


# ── Cookie 弹窗处理 ──────────────────────────────────────

def dismiss_cookie_consent(page):
    log("  → 检查 cookie 弹窗...")
    try:
        time.sleep(2)

        called = page.run_js("""
            try { __ncmp('save'); return true; } catch(e) { return false; }
        """)
        if called:
            log("  ✓ 已调用 __ncmp('save')")
            time.sleep(3)
            return True

        try:
            coords = page.run_js("""
                var btns = document.querySelectorAll('button.ncmp__btn');
                for (var b of btns) {
                    if (b.innerText.trim() === 'Accept') {
                        var r = b.getBoundingClientRect();
                        return {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)};
                    }
                }
                return null;
            """)
            if coords:
                wi = page.run_js("""
                    return {
                        sx: window.screenX || 0,
                        sy: window.screenY || 0,
                        oh: window.outerHeight,
                        ih: window.innerHeight
                    };
                """) or {"sx": 0, "sy": 0, "oh": 1080, "ih": 900}
                bar = wi.get("oh", 1080) - wi.get("ih", 900)
                if bar < 0 or bar > 200:
                    bar = 85
                ax = coords["x"] + wi.get("sx", 0)
                ay = coords["y"] + wi.get("sy", 0) + bar
                os.system(f"xdotool mousemove --sync {ax} {ay}")
                time.sleep(0.3)
                os.system("xdotool click 1")
                log(f"  ✓ xdotool 点击 Accept ({ax}, {ay})")
                time.sleep(3)
                return True
        except Exception as e:
            log(f"  ⚠️ xdotool 点击失败: {e}")

        try:
            btn = page.ele('css:button.ncmp__btn:not(.ncmp__btn-border)', timeout=3)
            if btn:
                btn.click()
                log("  ✓ 已点击 Accept 按钮")
                time.sleep(3)
                return True
        except:
            pass

        log("  → 未检测到 cookie 弹窗")
    except Exception as e:
        log(f"  ⚠️ 关闭 cookie 弹窗异常: {e}")
    return False


# ── 读取页面文字 ──────────────────────────────────────────

def get_page_text(page):
    try:
        return page.run_js("return document.body.innerText;") or ""
    except:
        return ""


# ── 读取账户当前总金币余额 ───────────────────────────────────

def get_total_coins(page):
    selectors = [
        '.maintitle span', 'h1.maintitle span',
        '#coins', '.coins', '[data-coins]',
        '#coin-balance', '.coin-balance', '.balance',
        '.navbar .coins', 'span.coin-amount', '#coinAmount',
    ]
    for sel in selectors:
        try:
            val = page.run_js(f"""
                var el = document.querySelector('{sel}');
                return el ? el.innerText.trim() : null;
            """)
            if val:
                m = re.search(r'[\d,]+', val)
                if m:
                    coins = m.group(0).replace(",", "")
                    log(f"  💰 总金币余额: {coins}（选择器: {sel}）")
                    return coins
        except:
            continue

    text = get_page_text(page)
    m = (re.search(r'(?:coins?|金币|积分)\s*[:：]?\s*([\d,]+)', text, re.IGNORECASE)
         or re.search(r'([\d,]+)\s*(?:coins?|金币|积分)', text, re.IGNORECASE))
    if m:
        coins = m.group(1).replace(",", "")
        log(f"  💰 总金币余额: {coins}（来源：页面文字）")
        return coins

    log("  ⚠️ 未能读取总金币余额")
    return "未知"


# ── Discord OAuth 登录 bot-hosting ──────────────────────

def is_logged_in(page):
    """已登录 bot-hosting 面板判定"""
    host = (page.run_js("return location.hostname || '';") or '').lower()
    path = (page.run_js("return location.pathname || '';") or '').lower()
    if 'bot-hosting.net' not in host:
        return False
    if '/login' in path:
        return False
    try:
        if page.run_js("return !!localStorage.getItem('token');"):
            return True
    except:
        pass
    if '/panel' in path:
        return _page_has_content(page)
    return False


def _scroll_discord_permissions(page):
    """把 Discord 授权页的权限列表滚到底——不滚到底授权按钮点了不生效。"""
    try:
        page.run_js("""
            var sels = ['[class*="scroller"]','[class*="oauth2"]','[class*="permissionList"]',
                '[class*="content"] [class*="scroll"]','[class*="listScroller"]',
                'div[class*="modal"] div[style*="overflow"]','div[class*="root"] div[style*="overflow"]'];
            var scrolled = false;
            sels.forEach(function(sel){
                document.querySelectorAll(sel).forEach(function(el){
                    var s = getComputedStyle(el);
                    if (el.scrollHeight > el.clientHeight &&
                        ['auto','scroll'].some(function(v){return s.overflowY===v||s.overflow===v;}))
                        { el.scrollTop = el.scrollHeight; scrolled = true; }
                });
            });
            if (!scrolled) document.querySelectorAll('div').forEach(function(el){
                if (el.scrollHeight > el.clientHeight + 10) {
                    var s = getComputedStyle(el);
                    if (['auto','scroll','hidden'].indexOf(s.overflowY) !== -1) el.scrollTop = el.scrollHeight;
                }
            });
            window.scrollTo(0, document.body.scrollHeight);
        """)
    except Exception as e:
        log(f"  ⚠️ 滚动权限列表异常: {e}")


def _click_authorize_btn(page):
    """滚动权限列表后点击授权按钮（CDP 真实点击 + JS 兜底）。返回是否点到按钮。"""
    finder = """
        var btns = Array.from(document.querySelectorAll('button'));
        var target = btns.find(function(b){
            var t = (b.innerText || '').trim().toLowerCase();
            return (t.indexOf('authorize') !== -1 || t.indexOf('授权') !== -1)
                   && t.indexOf('cancel') === -1 && t.indexOf('取消') === -1;
        });
        if (!target) target = document.querySelector('button[class*="primary"]');
    """
    rect = page.run_js(finder + """
        if (!target) return null;
        target.scrollIntoView({block:'center'});
        var r = target.getBoundingClientRect();
        return {x:r.left+r.width/2, y:r.top+r.height/2, w:r.width, disabled:!!target.disabled};
    """)
    if not rect:
        log("  ⏳ 授权按钮尚未出现...")
        return False
    if rect.get('disabled'):
        log("  ⏳ 授权按钮 disabled，等待...")
        return False
    if rect.get('w', 0) > 10:
        _cdp_click(page, int(rect['x']), int(rect['y']))
    page.run_js(finder + " if (target) target.click();")
    return True


def login_with_discord(page):
    log("=" * 50)
    log("🔐 使用 Discord OAuth 登录 bot-hosting")
    log("=" * 50)

    page.get(TARGET_URL)
    time.sleep(5)
    if _is_cf_page(page):
        wait_for_cloudflare(page, timeout=90)

    if is_logged_in(page):
        log("  ✅ 已是登录态，跳过 OAuth")
        return True

    log("  → 触发 Discord 登录 (/login/discord)...")
    page.get(LOGIN_URL)
    time.sleep(4)

    authorized_seen = False
    for _ in range(25):
        host = (page.run_js("return location.hostname || '';") or '').lower()
        path = (page.run_js("return location.pathname || '';") or '').lower()

        if 'discord.com' in host and 'oauth2/authorize' in path:
            authorized_seen = True
            log("  [OAuth] 授权页：滚动权限列表并点击授权...")
            _scroll_discord_permissions(page)
            time.sleep(1.5)
            _click_authorize_btn(page)
            time.sleep(3)
            continue

        if 'bot-hosting.net' in host and ('/login' not in path or authorized_seen):
            if _is_cf_page(page):
                log("  ⚡ 回调页 Cloudflare...")
                wait_for_cloudflare(page, timeout=60)
            if is_logged_in(page):
                log("  ✅ Discord OAuth 登录成功")
                return True

        time.sleep(2)

    log("  ❌ Discord OAuth 登录失败")
    send_tg_screenshot(page, "discord_login_fail")
    return False


# ── 强制关闭残留弹窗 ──────────────────────────────────────

def force_close_all_modals(page):
    log("  → 清理残留弹窗...")
    try:
        btn = page.ele('css:button.swal-button.swal-button--confirm', timeout=2)
        if btn:
            btn.click()
            log("  ✓ 已关闭 SweetAlert 弹窗")
            time.sleep(2)
    except:
        pass

    try:
        for selector in ['div.modal-content span.close', 'span.close', '.modal-content .close']:
            try:
                btn = page.ele(f'css:{selector}', timeout=1)
                if btn:
                    btn.click()
                    log("  ✓ 已关闭广告弹窗")
                    time.sleep(2)
                    break
            except:
                pass
    except:
        pass


# ── 处理弹窗并解析进度 ────────────────────────────────────

def close_all_modals(page):
    claimed, total = None, None
    try:
        log("  → 等待成功弹窗...")
        for _ in range(15):
            has_modal = page.run_js("return !!document.querySelector('.swal-modal');")
            if has_modal:
                break
            time.sleep(1)
        time.sleep(1.5)

        try:
            title = page.run_js("var el = document.querySelector('.swal-title'); return el ? el.innerText.trim() : '';") or ""
            text  = page.run_js("var el = document.querySelector('.swal-text'); return el ? el.innerText.trim() : '';") or ""
            log(f"  弹窗标题: {title}")
            log(f"  弹窗内容: {text}")
            m = re.search(r'(\d+)\s*/\s*(\d+)', text)
            if m:
                claimed, total = int(m.group(1)), int(m.group(2))
                log(f"  📊 进度: {claimed}/{total}")
        except Exception as e:
            log(f"  ⚠️ 解析弹窗文本失败: {e}")

        try:
            page.run_js("var btn = document.querySelector('button.swal-button.swal-button--confirm'); if(btn) btn.click();")
            log("  ✓ 已点击 OK")
            time.sleep(2)
        except:
            pass

        for _ in range(10):
            still_there = page.run_js("return !!document.querySelector('.swal-modal');")
            if not still_there:
                break
            time.sleep(1)

        try:
            for selector in ['div.modal-content span.close', 'span.close', '.modal-content .close']:
                btn = page.run_js(f"var btn = document.querySelector('{selector}'); if(btn && btn.offsetParent) {{ btn.click(); return true; }} return false;")
                if btn:
                    log("  ✓ 已关闭广告弹窗")
                    time.sleep(2)
                    break
        except:
            pass

    except Exception as e:
        log(f"  ⚠️ 处理弹窗失败: {e}")

    return claimed, total


# ── 检查按钮状态 ──────────────────────────────────────────

def check_button_ready(page, max_retries=3):
    selector = 'button.btn.green[type="submit"]'
    for i in range(max_retries):
        try:
            log(f"  → 检查按钮状态 ({i+1}/{max_retries})...")
            page.ele(f'css:{selector}', timeout=10)
            btn = page.ele(f'css:{selector}')
            text = btn.text.strip()
            log(f"  按钮文本: '{text}'")

            enabled = page.run_js(f"""
                var el = document.querySelector('{selector}');
                return el ? !el.disabled : false;
            """)
            if enabled:
                log("  ✓ 按钮可用")
                return True

            if "complete the captcha" in text.lower():
                log("  ⚠️ 需要 hCaptcha，等待 NopeCHA 插件自动解决...")
                for _ in range(20):
                    time.sleep(3)
                    enabled = page.run_js(f"""
                        var el = document.querySelector('{selector}');
                        return el ? !el.disabled : false;
                    """)
                    if enabled:
                        log("  ✓ hCaptcha 已自动解决，按钮可用")
                        return True
                log("  ⚠️ 等待超时，hCaptcha 未解决")
                return False

            elif "you are on cooldown" in text.lower():
                log("  ⚠️ 冷却中")
                return False
            else:
                log("  ⚠️ 按钮 disabled（其他原因）")
                return False

        except Exception as e:
            log(f"  ⚠️ 检查按钮失败: {e}")
            return False

    return False


# ── 领取主循环 ────────────────────────────────────────────

def click_claim_coins(page, max_attempts=15):
    selector = 'button.btn.green[type="submit"]'
    total_coins = 10
    claimed_so_far = 0
    task_completed = False

    log(f"\n🎯 开始领取流程（最多 {max_attempts} 次）...")

    for attempt in range(1, max_attempts + 1):
        if task_completed:
            break

        remaining = total_coins - claimed_so_far
        log(f"\n{'='*50}")
        log(f"【尝试 {attempt}/{max_attempts} | 剩余: {max(0, remaining)}】")
        log(f"{'='*50}")

        force_close_all_modals(page)

        if not check_button_ready(page):
            try:
                btn = page.ele(f'css:{selector}')
                if "you are on cooldown" in btn.text.lower():
                    log("  → 冷却等待 35 秒...")
                    time.sleep(35)
                    continue
            except:
                pass
            time.sleep(8)
            continue

        try:
            enabled = page.run_js(f"""
                var el = document.querySelector('{selector}');
                return el ? !el.disabled : false;
            """)
            if not enabled:
                log("  ⚠️ 按钮不可用，跳过")
                time.sleep(8)
                continue
            log("  → 点击领取按钮...")
            btn = page.ele(f'css:{selector}')
            btn.click()
            log("  ✓ 已点击")
        except Exception as e:
            log(f"  ⚠️ 点击失败: {e}")
            time.sleep(8)
            continue

        log("  → 等待 18 秒确保弹窗出现...")
        time.sleep(18)

        claimed, total = close_all_modals(page)

        if claimed is not None and total is not None:
            claimed_so_far = claimed
            total_coins = total
            log(f"  📊 进度: {claimed}/{total}")
            if claimed >= total:
                log("  🎉 已完成全部领取！")
                task_completed = True
        else:
            log("  ⚠️ 无法获取进度")

        time.sleep(1 if task_completed else 10)

    if task_completed or claimed_so_far >= total_coins:
        log(f"\n✅ 任务完成！最终进度: {claimed_so_far}/{total_coins}")
        return True, claimed_so_far, total_coins
    else:
        log(f"\n⚠️ 未完成目标（当前: {claimed_so_far}/{total_coins}）")
        return False, claimed_so_far, total_coins


# ── 主流程 ────────────────────────────────────────────────

def main():
    success, claimed, total = False, 0, 0
    total_balance = "未知"

    log("=" * 50)
    log("🚀 启动：Bot-Hosting 金币领取 (DrissionPage)")
    log(f"🖥️  运行模式: {'CI' if IS_CI else '本地'}")
    log("=" * 50)

    # ── 启动浏览器 ──────────────────────────────────────
    co = ChromiumOptions()
    co.set_argument('--no-sandbox')
    co.set_argument('--disable-dev-shm-usage')
    co.set_argument('--disable-gpu')
    co.set_argument('--window-size=1280,900')
    co.set_user_data_path(PROFILE_DIR)

    # 保留 NopeCHA 扩展加载，前提是里面已经有可用的 key
    if os.path.isdir(NOPECHA_EXT_DIR):
        co.add_extension(NOPECHA_EXT_DIR)
        log(f"📦 加载扩展: {NOPECHA_EXT_DIR}")
    else:
        log(f"⚠️ 扩展目录不存在: {NOPECHA_EXT_DIR}")

    if PROXY_URL:
        co.set_argument(f'--proxy-server={PROXY_URL}')
        log(f"🌐 代理: {PROXY_URL}")

    log("🔧 启动浏览器...")
    try:
        page = ChromiumPage(co)
    except Exception as e:
        log(f"❌ 浏览器启动失败: {e}")
        return

    try:
        # ── Step 1: 用 Discord OAuth 登录 bot-hosting ─────
        if not login_with_discord(page):
            msg = "❌ Discord OAuth 登录失败，无法进入面板"
            log(msg)
            send_tg(msg)
            return

        send_tg_screenshot(page, "panel_ready")
        log(f"  当前 URL: {page.url}")

        log(f"→ 跳转到 {EARN_URL}")
        page.get(EARN_URL)
        time.sleep(5)

        if _is_cf_page(page):
            log("  ⚡ earn 页面 Cloudflare...")
            wait_for_cloudflare(page, timeout=60)

        # 关闭 cookie 弹窗
        dismiss_cookie_consent(page)
        time.sleep(2)
        dismiss_cookie_consent(page)
        time.sleep(3)

        send_tg_screenshot(page, "earn_ready")

        log("→ 检查初始按钮状态")
        check_button_ready(page, max_retries=2)

        log("→ 开始自动领取")
        success, claimed, total = click_claim_coins(page, max_attempts=15)

        log("→ 读取账户总金币余额...")
        total_balance = get_total_coins(page)

        log("→ 保持页面 30 秒...")
        time.sleep(30)
        log("✅ 完成，关闭浏览器")

    except Exception as e:
        log(f"❌ 运行异常: {e}")
        send_tg_screenshot(page, "error")
    finally:
        try:
            page.quit()
        except:
            pass

    if success:
        send_tg(
            f"✅ Bot-Hosting 金币领取完成\n"
            f"📊 本次进度: {claimed}/{total}\n"
            f"💰 总金币余额: {total_balance}"
        )
    else:
        send_tg(
            f"⚠️ Bot-Hosting 金币领取未完成\n"
            f"📊 本次进度: {claimed}/{total}\n"
            f"💰 总金币余额: {total_balance}"
        )


if __name__ == "__main__":
    main()
