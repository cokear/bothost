#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ulzix 每日签到 (DrissionPage + NopeCHA)
────────────────────────────────────────
- Discord 领取 NopeCHA CDK（每日动态）
- CDK 直接作为 NopeCHA API Key 解 Cloudflare hCaptcha
- DrissionPage 驱动浏览器
- 自动登录 Ulzix + 签到 + 积分读取
- Telegram 通知
"""

import os
import re
import sys
import time
import random
import requests
from DrissionPage import ChromiumPage, ChromiumOptions

# ================= 配置区域 =================
PROXY_URL   = os.getenv("BROWSER_PROXY", "")
PROFILE_DIR = os.getenv("BROWSER_USER_DATA_DIR",
                        os.path.expanduser("~/.chrome-profile-ulzix"))

# NopeCHA 扩展目录（CDP 回退时备用）
NOPECHA_EXT_DIR = os.getenv(
    "NOPECHA_EXT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "chromium"),
)

# Discord（领取 CDK）
DISCORD_CHANNEL_URL = "https://discord.com/channels/1046086326077882479/1243188924520726538"
DISCORD_DM_URL      = "https://discord.com/channels/@me/1514785932203528202"

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
    """CDP 点击方式过 Cloudflare（回退方案）"""
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
    """将 CDK 注入 NopeCHA 扩展（通过 setup 页面）"""
    if not cdk:
        return False
    log(f"💉 注入 CDK 到 NopeCHA 扩展: {cdk[:4]}****{cdk[-4:]}")
    page.get(f"https://nopecha.com/setup#{cdk}")
    time.sleep(4)
    # 确认注入成功
    try:
        success = page.run_js("""
            return document.body.innerText.includes('Key saved') ||
                   document.body.innerText.includes('key') ||
                   document.body.innerText.includes('activated');
        """)
        if success:
            log("  ✅ CDK 注入成功")
        else:
            log("  ⚠️ CDK 注入状态不确定，继续...")
    except Exception:
        pass
    time.sleep(2)
    return True


def wait_hcaptcha_solved(page, timeout=60):
    """等待 NopeCHA 扩展自动解完 hCaptcha（检查 response token 是否被填充）"""
    log("  ⏳ 等待 NopeCHA 扩展自动解 hCaptcha...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            token = page.run_js("""
                var ta = document.querySelector(
                    'textarea[name="h-captcha-response"],'
                    + 'textarea[id*="h-captcha-response"],'
                    + 'textarea[data-hcaptcha-response]'
                );
                return (ta && ta.value && ta.value.length > 20) ? ta.value.substring(0, 30) : '';
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
    """尝试点击 hCaptcha 复选框（扩展未自动解时的兜底）"""
    try:
        # hCaptcha checkbox 通常在 iframe 里
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
    """
    hCaptcha 处理主流程：
    1. 注入 CDK 到 NopeCHA 扩展（首次）
    2. 等待扩展自动解
    3. 兜底：点击复选框
    """
    log(f"  → [{scene}] 处理 hCaptcha 验证")

    # 注入 CDK 到扩展
    if cdk:
        inject_cdk_to_extension(page, cdk)
        # 回到原页面
        time.sleep(2)

    # 等待扩展自动解
    for attempt in range(max_attempts):
        log(f"  [{scene}] 等待扩展解题 ({attempt + 1}/{max_attempts})...")
        if wait_hcaptcha_solved(page, timeout=30):
            return True
        # 兜底：点击复选框
        click_hcaptcha_checkbox(page)
        time.sleep(5)

    log(f"  ❌ [{scene}] hCaptcha 未解决")
    return False


# ══════════════════════════════════════════════════════════
# ── Discord CDK 领取流程 ────────────────────────────────
# ══════════════════════════════════════════════════════════

def get_page_text(page):
    try:
        return page.run_js("return document.body.innerText;") or ""
    except Exception:
        return ""


def extract_latest_cdk(text):
    pattern = re.findall(
        r'Here is your Discord key for NopeCHA[:\s]+([a-z0-9]+).*?\(([^)]+)\)',
        text, re.IGNORECASE | re.DOTALL,
    )
    if not pattern:
        return "", False
    cdk, time_label = pattern[-1]
    is_recent = "内" in time_label
    log(f"  📋 CDK 时间标记: {time_label} | 24h内: {is_recent}")
    return cdk, is_recent


def inject_nopecha_key(page, cdk):
    """将 CDK 注入 NopeCHA 扩展（扩展回退方案使用）"""
    log(f"💉 注入 key 到 NopeCHA 插件...")
    page.get(f"https://nopecha.com/setup#{cdk}")
    time.sleep(3)
    log(f"✅ 插件注入完成: {cdk[:4]}****{cdk[-4:]}")


def wait_dm_rendered(page, timeout=25):
    start = time.time()
    while time.time() - start < timeout:
        try:
            ready = page.run_js("""
                return !!document.querySelector(
                    '[data-list-id="chat-messages"] li,'
                    + 'li[id^="chat-messages"],'
                    + '[class*="messageListItem"],'
                    + '[class*="messageContent"]'
                );
            """)
            if ready:
                txt_len = page.run_js(
                    "return (document.body.innerText || '').length;"
                ) or 0
                if txt_len > 80:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def read_dm_cdk(page, load_timeout=30):
    """打开 Discord 私聊，读取最新 CDK"""
    page.get(DISCORD_DM_URL)
    rendered = wait_dm_rendered(page, timeout=load_timeout)
    if not rendered:
        log("  ⚠️ 私聊消息区未渲染完成")
        return "", False, False

    last_cdk = ""
    for _ in range(3):
        text = get_page_text(page)
        cdk, is_recent = extract_latest_cdk(text) if text else ("", False)
        if cdk and is_recent:
            return cdk, True, True
        if cdk:
            last_cdk = cdk
        time.sleep(1.5)
    return last_cdk, False, True


# ── Discord 频道消息框定位 ────────────────────────────────

_FIND_CHANNEL_COMPOSER_JS = r"""
() => {
  function vis(el) {
    if (!el) return false;
    try {
      var st = getComputedStyle(el);
      if (!st || st.display === 'none' || st.visibility === 'hidden') return false;
      if (parseFloat(st.opacity || '1') < 0.05) return false;
      var r = el.getBoundingClientRect();
      return r.width > 40 && r.height > 12;
    } catch (e) { return false; }
  }
  function labelOf(el) {
    return ((el.getAttribute('aria-label') || '') + ' '
      + (el.getAttribute('placeholder') || '') + ' '
      + (el.getAttribute('data-placeholder') || '') + ' '
      + (el.getAttribute('aria-placeholder') || '')).toLowerCase();
  }
  function isSearch(el) {
    var lab = labelOf(el);
    if (/search|搜索|查找/.test(lab) && !/message|消息|给 |发送/.test(lab))
      return true;
    try {
      var r = el.getBoundingClientRect();
      if (r.top < 120 && r.left > window.innerWidth * 0.35) {
        if (/search|搜索/.test(lab) ||
            el.closest('[class*="searchBar"],[class*="SearchBar"],form[role="search"]'))
          return true;
      }
    } catch (e) {}
    if (el.closest('[class*="searchBar"],[class*="SearchBar"],form[role="search"]'))
      return true;
    return false;
  }
  function isComposerLike(el) {
    if (!vis(el) || isSearch(el)) return false;
    var lab = labelOf(el);
    if (/^message |^message#|message @|给 .* 发消息|发送消息到|chat input/i.test(lab))
      return true;
    if (/message|消息|发送|给 #|给 @/.test(lab) && !/search|搜索/.test(lab))
      return true;
    if (el.closest('[class*="channelTextArea"],[class*="ChannelTextArea"]')) {
      var r = el.getBoundingClientRect();
      if (r.top > window.innerHeight * 0.45) return true;
    }
    return false;
  }
  var nodes = Array.prototype.slice.call(document.querySelectorAll(
    '[data-slate-editor="true"], div[role="textbox"][contenteditable="true"], div[role="textbox"]'
  ));
  var cands = nodes.filter(isComposerLike);
  if (!cands.length) {
    cands = nodes.filter(function(el) { return vis(el) && !isSearch(el); });
  }
  if (!cands.length) return null;
  cands.sort(function(a, b) {
    var ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    return (rb.bottom - ra.bottom) || (rb.width - ra.width);
  });
  var el = cands[0];
  var r = el.getBoundingClientRect();
  document.querySelectorAll('[data-earn-composer="1"]').forEach(function(x) {
    try { x.removeAttribute('data-earn-composer'); } catch (e) {}
  });
  el.setAttribute('data-earn-composer', '1');
  return {
    top: r.top, left: r.left, width: r.width, height: r.height,
    bottom: r.bottom,
    label: (el.getAttribute('aria-label') || '').slice(0, 80)
  };
}
"""


def _channel_composer_info(page):
    try:
        info = page.run_js(
            "return (%s)();" % _FIND_CHANNEL_COMPOSER_JS.lstrip()
        )
        if isinstance(info, dict) and info.get("width"):
            return info
    except Exception as e:
        log(f"  ⚠️ 定位频道输入框失败: {e}")
    return None


def _composer_text(page):
    try:
        return page.run_js("""
            var el = document.querySelector('[data-earn-composer="1"]')
                  || document.querySelector(
                       '[class*="channelTextArea"] [data-slate-editor="true"]')
                  || document.querySelector(
                       '[class*="channelTextArea"] [role="textbox"]');
            if (!el) return '';
            return ((el.innerText || el.textContent || '') + '').trim();
        """) or ""
    except Exception:
        return ""


def _focus_channel_composer(page):
    info = _channel_composer_info(page)
    if not info:
        return None
    try:
        cx = int(info["left"] + info["width"] / 2)
        cy = int(info["top"] + min(info["height"] / 2, 18))
        _cdp_click(page, cx, cy)
    except Exception:
        pass
    try:
        page.run_js("""
            var el = document.querySelector('[data-earn-composer="1"]');
            if (!el) return false;
            el.scrollIntoView({block:'center'});
            try { el.click(); } catch (e) {}
            try { el.focus(); } catch (e) {}
            var inner = el.querySelector(
              '[data-slate-node="element"], [contenteditable="true"]') || el;
            try { inner.focus(); } catch (e2) {}
            return true;
        """)
    except Exception:
        pass
    time.sleep(0.35)
    return info


def wait_channel_input_ready(page, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        try:
            info = _channel_composer_info(page)
            if info and info.get("bottom", 0) > 100:
                vh = page.run_js("return window.innerHeight;") or 800
                if info.get("top", 0) > vh * 0.35:
                    log(f"  ✓ 频道输入框就绪 label={info.get('label')!r}")
                    return True
                if (info.get("top", 0) > 150
                        and not re.search(
                            r"search|搜索", str(info.get("label") or ""), re.I)):
                    log(f"  ✓ 频道输入框就绪(宽松) label={info.get('label')!r}")
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def type_command_verified(page, cmd="!nopecha", retries=3):
    for i in range(retries):
        log(f"  → 输入命令尝试 {i+1}/{retries}...")
        # Esc 清除可能的搜索焦点
        try:
            for ev_type in ["keyDown", "keyUp"]:
                page.run_cdp(
                    "Input.dispatchKeyEvent",
                    type=ev_type, key="Escape", code="Escape",
                    windowsVirtualKeyCode=27, nativeVirtualKeyCode=27,
                )
        except Exception:
            pass
        time.sleep(0.25)

        info = _focus_channel_composer(page)
        if not info:
            log("  ⚠️ 未找到频道底部消息输入框")
            time.sleep(2)
            continue
        lab = str(info.get("label") or "")
        if (re.search(r"search|搜索", lab, re.I)
                and not re.search(r"message|消息", lab, re.I)):
            log(f"  ⚠️ 疑似搜索框 label={lab!r}，重试...")
            time.sleep(1)
            continue

        # 清空残留
        try:
            page.run_cdp("Input.dispatchKeyEvent", type="keyDown",
                         key="a", code="KeyA", modifiers=2)
            page.run_cdp("Input.dispatchKeyEvent", type="keyUp",
                         key="a", code="KeyA", modifiers=2)
            page.run_cdp("Input.dispatchKeyEvent", type="keyDown",
                         key="Backspace", code="Backspace",
                         windowsVirtualKeyCode=8, nativeVirtualKeyCode=8)
            page.run_cdp("Input.dispatchKeyEvent", type="keyUp",
                         key="Backspace", code="Backspace",
                         windowsVirtualKeyCode=8, nativeVirtualKeyCode=8)
        except Exception:
            pass
        time.sleep(0.3)

        _focus_channel_composer(page)
        try:
            page.run_cdp("Input.insertText", text=cmd)
        except Exception as e:
            log(f"  ⚠️ insertText 失败: {e}")
        time.sleep(random.uniform(0.5, 0.9))

        cur = _composer_text(page)
        log(f"  消息框内容: '{cur}'")

        if cmd.strip() not in cur:
            log("  ⚠️ 消息框未获取到命令，重试...")
            time.sleep(2)
            continue

        # 回车发送
        try:
            page.run_cdp(
                "Input.dispatchKeyEvent", type="keyDown", key="Enter",
                code="Enter", windowsVirtualKeyCode=13,
                nativeVirtualKeyCode=13, text="\r",
            )
            page.run_cdp(
                "Input.dispatchKeyEvent", type="keyUp", key="Enter",
                code="Enter", windowsVirtualKeyCode=13,
                nativeVirtualKeyCode=13,
            )
        except Exception:
            pass
        time.sleep(1.5)

        after = _composer_text(page)
        if cmd.strip() not in after:
            log("  ✅ 命令已成功发送")
            return True
        log("  ⚠️ 回车后消息框仍有内容，重试...")
        time.sleep(2)
    return False


def send_command_and_poll(page):
    """去频道发 !nopecha，轮询私聊等 CDK 回复"""
    page.get(DISCORD_CHANNEL_URL)
    log("⏳ 等待频道输入框就绪...")
    if not wait_channel_input_ready(page, timeout=30):
        log("❌ 频道输入框超时")
        return ""
    wait_dm_rendered(page, timeout=15)

    log("⌨️ 发送 !nopecha 命令...")
    if not type_command_verified(page, "!nopecha", retries=3):
        log("❌ !nopecha 发送失败")
        return ""

    log("✅ 命令已发送，等待私聊回复...")
    time.sleep(5)

    for attempt in range(20):
        log(f"  [{attempt+1}/20] 打开私聊检查 CDK...")
        cdk, is_recent, rendered = read_dm_cdk(page, load_timeout=30)
        if cdk and is_recent:
            log(f"✅ 获取到有效 CDK: {cdk[:4]}****{cdk[-4:]}")
            return cdk
        if not rendered:
            log("  ⏳ 私聊未加载完成，稍后重试...")
        page.get(DISCORD_CHANNEL_URL)
        time.sleep(4)
    return ""


def ensure_cdk(page):
    """
    CDK 领取主逻辑：
    1. 先查私聊是否已有 24h 内的 CDK
    2. 没有就去频道发 !nopecha 领新的
    返回: 当天的 CDK（直接用作 NopeCHA API Key）
    """
    log("=" * 50)
    log("🔑 CDK 检查流程")
    log("=" * 50)

    if "login" in (page.url or "").lower():
        log("❌ Discord 登录态失效")
        send_tg("❌ Discord 登录态失效，请重新初始化 Profile")
        return ""

    # 查私聊
    log("🔍 检查私聊是否已有24h内的 CDK...")
    cdk, is_recent, rendered = read_dm_cdk(page, load_timeout=40)
    if cdk and is_recent:
        log(f"✅ 已有24h内 CDK: {cdk[:4]}****{cdk[-4:]}")
        return cdk

    # 慢网重试
    if not rendered:
        for i in range(2):
            log(f"  🔁 私聊未加载完成，重试 ({i+1}/2)...")
            cdk, is_recent, rendered = read_dm_cdk(page, load_timeout=40)
            if cdk and is_recent:
                log(f"✅ 重试读到 CDK: {cdk[:4]}****{cdk[-4:]}")
                return cdk
            if rendered:
                break

    # 领新的
    log("📭 无有效 CDK，去频道领取...")
    cdk = send_command_and_poll(page)
    if not cdk:
        log("❌ 未能获取 CDK")
        send_tg("❌ 未能获取 NopeCHA CDK，请检查 Discord")
    return cdk


# ══════════════════════════════════════════════════════════
# ── Ulzix 登录 ──────────────────────────────────────────
# ══════════════════════════════════════════════════════════

def ulzix_login(page, email, password, cdk):
    """邮箱密码登录 Ulzix（可能触发 hCaptcha，用 CDK 解）"""
    log("🔐 登录 Ulzix...")
    page.get(LOGIN_URL)
    time.sleep(5)

    if _is_cf_page(page) or _is_hcaptcha_page(page):
        handle_captcha(page, cdk, "login")

    time.sleep(3)
    save_screenshot(page, "01_login_loaded")

    # 填写邮箱
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

    # 填写密码
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

    # 提交
    try:
        submit = page.ele('css:button[type="submit"]', timeout=5)
        if submit:
            submit.click()
            log("  → 已点击登录按钮")
    except Exception as e:
        log(f"  ⚠️ 提交失败: {e}")
        return False, "登录按钮点击失败"

    time.sleep(6)

    # 可能回调页有 CF
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
    """注册 JS 弹窗自动处理，防止 DrissionPage 崩溃"""
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
    """关闭 SweetAlert 弹窗（如果存在）"""
    try:
        page.run_js("""
            // 方式1：swal.close()
            if (window.swal) { try { swal.close(); } catch(e) {} }
            // 方式2：点击确认按钮
            var btn = document.querySelector('.swal-button.swal-button--confirm, .swal2-confirm');
            if (btn) btn.click();
            // 方式3：点击关闭按钮
            var close = document.querySelector('.swal-button.swal-button--cancel, .swal2-close');
            if (close) close.click();
        """)
    except Exception:
        pass


def dismiss_all_popups(page):
    """综合清理弹窗：JS alert + SweetAlert + 广告弹窗"""
    _dismiss_sweetalert(page)
    _setup_dialog_handler(page)
    # 关闭常见广告弹窗
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
    """判定是否已签到（文案驱动）"""
    if "今日还未签到" in html:
        return False
    if "签到成功" in html or "今日已签到" in html:
        return True
    if 'id="btn-signin"' in html or "立即签到" in html:
        return False
    return False


def do_signin(page, cdk):
    """
    打开签到页 → 解 hCaptcha → 点签到 → 确认结果
    cdk = 当天 CDK，用于 NopeCHA API 解 hCaptcha
    """
    log("📝 打开签到页...")
    page.get(SIGNIN_URL)
    _setup_dialog_handler(page)
    time.sleep(10)

    # 页面可能有 CF 或 hCaptcha
    if _is_cf_page(page) or _is_hcaptcha_page(page):
        log("  🔍 检测到验证码（CF/hCaptcha），尝试解决...")
        handle_captcha(page, cdk, "signin_page1")
    time.sleep(10)

    save_screenshot(page, "04_signin_loaded")

    html = page.run_js("return document.body.innerHTML;") or ""
    before_points = extract_points(html)

    # 已签到
    if is_signed(html):
        log("  ℹ️ 今日已签到")
        return True, "今日已签到", before_points, before_points, None

    # cookie 弹窗
    try:
        btn = page.ele('css:button.ncmp__btn', timeout=3)
        if btn:
            btn.click()
            log("  ✓ 关闭 cookie 弹窗")
            time.sleep(3)
    except Exception:
        pass

    # 签到页的 hCaptcha（签到按钮前可能有验证）
    if _is_cf_page(page) or _is_hcaptcha_page(page):
        log("  🔍 检测到验证码（CF/hCaptcha），尝试解决...")
        handle_captcha(page, cdk, "signin_page2")

    # 再次确认 hCaptcha 已解决（签到按钮可能要求先验证）
    if _is_hcaptcha_page(page):
        log("  🔍 hCaptcha 仍存在，强制调用 NopeCHA API...")
        handle_captcha(page, cdk, "signin_before_click", max_attempts=5)

    # 检查签到按钮
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

        # 立即注册弹窗处理器，防止后续弹窗导致崩溃
        _setup_dialog_handler(page)
    except Exception as e:
        log(f"  ⚠️ 签到按钮操作失败: {e}")
        return False, "签到失败", before_points, before_points, str(e)[:100]

    # 等待弹窗出现并处理
    time.sleep(3)
    dismiss_all_popups(page)

    # 等待并确认结果
    before_val = points_to_int(before_points)
    for i in range(15):
        time.sleep(2)
        html = page.run_js("return document.body.innerHTML;") or ""
        current_points = extract_points(html)
        current_val = points_to_int(current_points)

        # 积分增加 = 成功
        if (before_val is not None and current_val is not None
                and current_val > before_val):
            log(f"  ✅ 签到成功！积分: {before_points} → {current_points}")
            save_screenshot(page, "05_signin_success")
            return True, "签到成功", before_points, current_points, None

        # 文案兜底
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

    # ── 启动浏览器 ──
    co = ChromiumOptions()
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-gpu")
    co.set_argument("--window-size=1280,900")
    co.set_user_data_path(PROFILE_DIR)

    # 加载 NopeCHA 扩展（CDP 回退时备用）
    if os.path.isdir(NOPECHA_EXT_DIR):
        co.add_extension(NOPECHA_EXT_DIR)
        log(f"📦 加载扩展: {NOPECHA_EXT_DIR}")
    else:
        log(f"⚠️ 扩展目录不存在（仅 API 模式）")

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
        # 提前注册弹窗处理器
        _setup_dialog_handler(page)

        # ── Step 1: 从 Discord 领取当天 CDK ──
        cdk = ensure_cdk(page)
        if not cdk:
            fail_reason = "未能获取 CDK"
            send_tg(f"❌ 无法获取 NopeCHA CDK，签到终止")
            return False

        log(f"🔑 当天 CDK: {cdk[:4]}****{cdk[-4:]}")

        # ── 注入 CDK 到 NopeCHA 扩展（一次性）──
        inject_cdk_to_extension(page, cdk)

        # ── Step 2: 登录 Ulzix ──
        ok, reason = ulzix_login(page, email, password, cdk)
        if not ok:
            fail_reason = reason
            send_tg_screenshot(page, "login_failed")
            send_tg(build_result_caption(email, "登录失败", fail_reason=reason))
            return False

        # ── Step 3: 签到 ──
        success, result_text, before_points, current_points, fail_reason = \
            do_signin(page, cdk)

        # ── 通知 ──
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
