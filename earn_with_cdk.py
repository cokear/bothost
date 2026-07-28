#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot-Hosting 金币自动领取 (DrissionPage 版)
- Discord 获取 NopeCHA CDK → 注入插件
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

DISCORD_CHANNEL_URL = "https://discord.com/channels/1046086326077882479/1243188924520726538"
DISCORD_DM_URL      = "https://discord.com/channels/@me/1514785932203528202"

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


# ── CDK 提取 ─────────────────────────────────────────────

def extract_latest_cdk(text):
    pattern = re.findall(
        r'Here is your Discord key for NopeCHA[:\s]+([a-z0-9]+).*?\(([^)]+)\)',
        text, re.IGNORECASE | re.DOTALL
    )
    if not pattern:
        return '', False

    cdk, time_label = pattern[-1]
    is_recent = '内' in time_label
    log(f"  📋 最新 CDK 时间标记: {time_label} | 24h内: {is_recent}")
    return cdk, is_recent


# ── 注入 key 到插件 ───────────────────────────────────────

def inject_nopecha_key(page, cdk):
    log(f"💉 注入 key 到 NopeCHA 插件...")
    page.get(f"https://nopecha.com/setup#{cdk}")
    time.sleep(3)
    log(f"✅ key 注入完成: {cdk[:4]}****{cdk[-4:]}")


# ── 获取元素屏幕坐标（Discord 用）──────────────────────

def get_element_screen_pos(page, selector):
    try:
        info = page.run_js(f"""
            var el = document.querySelector('{selector}');
            if (!el) return null;
            var r = el.getBoundingClientRect();
            return {{
                cx: r.left + r.width / 2,
                cy: r.top + r.height / 2,
                sx: window.screenX || 0,
                sy: window.screenY || 0,
                dh: window.outerHeight - window.innerHeight,
                dw: (window.outerWidth - window.innerWidth) / 2
            }};
        """)
        if not info:
            return None, None
        ox = int(info.get('sx', 0) + info.get('dw', 0))
        oy = int(info.get('sy', 0) + info.get('dh', 0))
        return int(info['cx']) + ox, int(info['cy']) + oy
    except:
        return None, None


# ── xdotool 点击（Discord 输入用）────────────────────────

def xdo_click(page, x, y, label=""):
    try:
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
        ax = int(x + wi.get("sx", 0))
        ay = int(y + wi.get("sy", 0) + bar)
        os.system(f"xdotool mousemove --sync {ax} {ay}")
        time.sleep(0.2)
        os.system("xdotool click 1")
        if label:
            log(f"  🖱️ 点击 ({ax},{ay}) {label}")
        return True
    except Exception as e:
        log(f"  ⚠️ xdotool 点击失败: {e}")
        return False


def keyboard_type(text):
    for ch in text:
        os.system(f'xdotool type --clearmodifiers --delay 80 -- "{ch}"')
        time.sleep(random.uniform(0.05, 0.15))


def keyboard_key(key):
    os.system(f"xdotool key --clearmodifiers {key}")
    time.sleep(0.1)


# ── 等 Discord 消息区真正渲染出来（内容驱动，抗慢网）──────

def wait_dm_rendered(page, timeout=25):
    """
    轮询直到聊天消息节点出现且正文有一定长度。
    返回 True 表示消息区确实渲染完成；False 表示超时内未加载出来。
    """
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
                txt_len = page.run_js("return (document.body.innerText || '').length;") or 0
                if txt_len > 80:
                    return True
        except:
            pass
        time.sleep(1)
    return False


# ── 打开私聊并轮询读取 CDK（区分「没加载」与「没有CDK」）──────

def read_dm_cdk(page, load_timeout=30):
    """
    打开 Discord 私聊并稳健读取 CDK。
    返回 (cdk, is_recent, rendered):
      - rendered=True  表示消息区确实渲染完成（可信地判断有无 CDK）
      - rendered=False 表示超时内页面没加载完（网络慢，不应据此判定"没有 CDK"）
    """
    page.get(DISCORD_DM_URL)

    rendered = wait_dm_rendered(page, timeout=load_timeout)
    if not rendered:
        log("  ⚠️ 私聊消息区在超时内未渲染完成（可能网络慢）")
        return '', False, False

    # 已渲染，但懒加载可能还差最后一帧 → 多读几轮取稳定结果
    last_cdk = ''
    for _ in range(3):
        text = get_page_text(page)
        cdk, is_recent = extract_latest_cdk(text) if text else ('', False)
        if cdk and is_recent:
            return cdk, True, True
        if cdk:
            last_cdk = cdk
        time.sleep(1.5)

    # 消息区已渲染完成但没有 24h 内的 CDK → 确实没有
    return last_cdk, False, True


# ── 频道底部「发消息」输入框（绝不能点到右上角搜索）────────

# Discord 里有多处 slate / textbox：右上角 Search、成员搜索、频道消息框。
# 旧逻辑 querySelector('[data-slate-editor=true]') 常命中搜索栏。
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
    if (/search|搜索|查找/.test(lab) && !/message|消息|给 |发送/.test(lab)) return true;
    // 右上角搜索：一般在视口上半且偏右
    try {
      var r = el.getBoundingClientRect();
      if (r.top < 120 && r.left > window.innerWidth * 0.35) {
        if (/search|搜索/.test(lab) || el.closest('[class*="searchBar"],[class*="SearchBar"],form[role="search"]'))
          return true;
      }
    } catch (e) {}
    if (el.closest('[class*="searchBar"],[class*="SearchBar"],[class*="search-bar"],form[role="search"]'))
      return true;
    return false;
  }
  function isComposerLike(el) {
    if (!vis(el) || isSearch(el)) return false;
    var lab = labelOf(el);
    // 明确的消息框文案
    if (/^message |^message#|message @|给 .* 发消息|发送消息到|在 .* 中发送|write.*message|chat input/i.test(lab))
      return true;
    if (/message|消息|发送|给 #|给 @/.test(lab) && !/search|搜索/.test(lab))
      return true;
    // 结构：在 channelTextArea / form 底部区域
    if (el.closest('[class*="channelTextArea"],[class*="ChannelTextArea"],form[class*="form"]')) {
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
    // 退路：所有可见 textbox/slate，排除搜索，取最靠视口底部的
    cands = nodes.filter(function(el) {
      return vis(el) && !isSearch(el);
    });
  }
  if (!cands.length) return null;
  cands.sort(function(a, b) {
    var ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    // 更靠下优先，其次更宽
    return (rb.bottom - ra.bottom) || (rb.width - ra.width);
  });
  var el = cands[0];
  var r = el.getBoundingClientRect();
  // 打标，后续读写用同一节点
  document.querySelectorAll('[data-earn-composer="1"]').forEach(function(x) {
    try { x.removeAttribute('data-earn-composer'); } catch (e) {}
  });
  el.setAttribute('data-earn-composer', '1');
  return {
    top: r.top, left: r.left, width: r.width, height: r.height,
    bottom: r.bottom,
    label: (el.getAttribute('aria-label') || '').slice(0, 80),
    text: ((el.innerText || el.textContent || '') + '').trim().slice(0, 80)
  };
}
"""


def _channel_composer_info(page):
    """定位频道底部发消息框；排除右上角 Search。"""
    try:
        # run_js 既可传函数体也可传 IIFE；统一用表达式调用
        info = page.run_js("return (%s)();" % _FIND_CHANNEL_COMPOSER_JS.lstrip())
        if isinstance(info, dict) and info.get("width"):
            return info
    except Exception as e:
        log(f"  ⚠️ 定位频道输入框失败: {e}")
    return None


def _composer_text(page):
    try:
        return page.run_js("""
            var el = document.querySelector('[data-earn-composer="1"]')
                  || document.querySelector('[class*="channelTextArea"] [data-slate-editor="true"]')
                  || document.querySelector('[class*="channelTextArea"] [role="textbox"]');
            if (!el) return '';
            return ((el.innerText || el.textContent || '') + '').trim();
        """) or ""
    except Exception:
        return ""


def _focus_channel_composer(page):
    """点击并 focus 频道消息框（非搜索栏）。成功返回 info dict。"""
    info = _channel_composer_info(page)
    if not info:
        return None
    # 点中心，确保 focus 进 contenteditable
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
            // 再点内部可编辑子节点
            var inner = el.querySelector('[data-slate-node="element"], [contenteditable="true"]') || el;
            try { inner.focus(); } catch (e2) {}
            return true;
        """)
    except Exception:
        pass
    time.sleep(0.35)
    return info


def wait_channel_input_ready(page, timeout=30):
    """轮询直到频道底部「发消息」框就绪（不是右上角搜索）。"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            info = _channel_composer_info(page)
            if info and info.get("bottom", 0) > 100:
                # 必须在偏下半屏，避免搜索栏误判
                if info.get("top", 0) > (page.run_js("return window.innerHeight;") or 800) * 0.35:
                    log(f"  ✓ 频道输入框就绪 label={info.get('label')!r} top={info.get('top'):.0f}")
                    return True
                # 有的小窗 top 也可能偏中，只要不是搜索且靠下即可
                if info.get("top", 0) > 150 and not re.search(r"search|搜索", str(info.get("label") or ""), re.I):
                    log(f"  ✓ 频道输入框就绪(宽松) label={info.get('label')!r} top={info.get('top'):.0f}")
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


# ── 输入并验证命令确实发出去了 ────────────────────────────

def type_command_verified(page, cmd="!nopecha", retries=3):
    """
    聚焦【频道底部消息框】→ CDP 注入文本 → 校验 → 回车发送。

    禁止用全局 [data-slate-editor]：Discord 右上角搜索也是同类控件，
    querySelector 第一个常点到 Search（截图复现：命令进了搜索栏）。
    """
    for i in range(retries):
        log(f"  → 输入命令尝试 {i+1}/{retries}...")

        # 0) 若焦点还在搜索栏，先 Esc 关掉搜索 UI
        try:
            page.run_cdp(
                "Input.dispatchKeyEvent",
                type="keyDown", key="Escape", code="Escape",
                windowsVirtualKeyCode=27, nativeVirtualKeyCode=27,
            )
            page.run_cdp(
                "Input.dispatchKeyEvent",
                type="keyUp", key="Escape", code="Escape",
                windowsVirtualKeyCode=27, nativeVirtualKeyCode=27,
            )
        except Exception:
            pass
        time.sleep(0.25)

        info = _focus_channel_composer(page)
        if not info:
            log("  ⚠️ 未找到频道底部消息输入框（可能仍在加载）")
            time.sleep(2)
            continue
        lab = str(info.get("label") or "")
        if re.search(r"search|搜索", lab, re.I) and not re.search(r"message|消息", lab, re.I):
            log(f"  ⚠️ 仍疑似搜索框 label={lab!r}，重试定位...")
            time.sleep(1)
            continue
        log(f"  定位消息框: label={lab!r} top={info.get('top'):.0f}")

        # 清空残留（在已 focus 的 composer 上）
        try:
            page.run_cdp("Input.dispatchKeyEvent", type="keyDown", key="a",
                         code="KeyA", modifiers=2)  # Ctrl+A
            page.run_cdp("Input.dispatchKeyEvent", type="keyUp", key="a",
                         code="KeyA", modifiers=2)
            page.run_cdp("Input.dispatchKeyEvent", type="keyDown", key="Backspace",
                         code="Backspace", windowsVirtualKeyCode=8, nativeVirtualKeyCode=8)
            page.run_cdp("Input.dispatchKeyEvent", type="keyUp", key="Backspace",
                         code="Backspace", windowsVirtualKeyCode=8, nativeVirtualKeyCode=8)
        except Exception:
            pass
        time.sleep(0.3)

        # 再 focus 一次再插入，避免焦点跳回搜索
        _focus_channel_composer(page)
        try:
            page.run_cdp("Input.insertText", text=cmd)
        except Exception as e:
            log(f"  ⚠️ insertText 失败，尝试键盘输入兜底: {e}")
            keyboard_type(cmd)
        time.sleep(random.uniform(0.5, 0.9))

        cur = _composer_text(page)
        log(f"  当前消息框内容: '{cur}'")

        # 若文本跑进了搜索栏，composer 会是空的或没有命令
        if cmd.strip() not in cur:
            # 探测搜索栏是否吃了文本
            try:
                search_txt = page.run_js("""
                    var s = document.querySelector(
                      'input[aria-label*="Search" i], input[aria-label*="搜索"],'
                      + '[class*="searchBar"] input, [class*="SearchBar"] input,'
                      + 'form[role="search"] input, [aria-label*="Search" i][role="combobox"]'
                    );
                    if (!s) {
                      var boxes = document.querySelectorAll('[data-slate-editor="true"], [role="textbox"]');
                      for (var i=0;i<boxes.length;i++){
                        var lab=((boxes[i].getAttribute('aria-label')||'')+'').toLowerCase();
                        var r=boxes[i].getBoundingClientRect();
                        if ((/search|搜索/.test(lab) || r.top<120) && r.left>window.innerWidth*0.3)
                          return ((boxes[i].innerText||boxes[i].textContent||boxes[i].value||'')+'').trim();
                      }
                      return '';
                    }
                    return ((s.value||s.innerText||s.textContent||'')+'').trim();
                """) or ""
            except Exception:
                search_txt = ""
            if cmd.strip() in str(search_txt):
                log(f"  ⚠️ 命令误入搜索栏: '{search_txt}' — 清空搜索并重试")
                try:
                    page.run_cdp(
                        "Input.dispatchKeyEvent", type="keyDown", key="Escape",
                        code="Escape", windowsVirtualKeyCode=27, nativeVirtualKeyCode=27,
                    )
                    page.run_cdp(
                        "Input.dispatchKeyEvent", type="keyUp", key="Escape",
                        code="Escape", windowsVirtualKeyCode=27, nativeVirtualKeyCode=27,
                    )
                except Exception:
                    pass
            else:
                log("  ⚠️ 消息框仍未获取到命令文本，重试...")
            time.sleep(2)
            continue

        # 回车发送
        try:
            page.run_cdp("Input.dispatchKeyEvent", type="keyDown", key="Enter",
                         code="Enter", windowsVirtualKeyCode=13, nativeVirtualKeyCode=13, text="\r")
            page.run_cdp("Input.dispatchKeyEvent", type="keyUp", key="Enter",
                         code="Enter", windowsVirtualKeyCode=13, nativeVirtualKeyCode=13)
        except Exception as e:
            log(f"  ⚠️ CDP 回车失败，尝试键盘兜底: {e}")
            keyboard_key("Return")
        time.sleep(1.5)

        after = _composer_text(page)
        if cmd.strip() not in after:
            log("  ✅ 命令已成功发送（消息框已清空）")
            return True

        log("  ⚠️ 回车后消息框仍有内容，可能未发送，重试...")
        time.sleep(2)

    return False


# ── 发送命令并轮询 CDK ────────────────────────────────────

def send_command_and_poll(page):
    page.get(DISCORD_CHANNEL_URL)

    # 关键修复：先等频道输入框真正就绪，页面没加载好绝不敲命令
    log("⏳ 等待频道输入框就绪...")
    if not wait_channel_input_ready(page, timeout=30):
        log("❌ 频道输入框超时未就绪（页面没加载好），本轮放弃发送")
        return ''
    # 再确认消息区渲染，避免 slash-command 面板等干扰
    wait_dm_rendered(page, timeout=15)

    log("⌨️ 发送 !nopecha 命令（带验证）...")
    if not type_command_verified(page, "!nopecha", retries=3):
        log("❌ !nopecha 命令发送失败（输入框/页面未就绪）")
        return ''

    log("✅ 命令已发送，等待私聊回复...")
    time.sleep(5)

    for attempt in range(20):
        log(f"  [{attempt+1}/20] 打开私聊检查新 CDK...")

        # 内容驱动读取：慢网会等到真正渲染完再判断
        cdk, is_recent, rendered = read_dm_cdk(page, load_timeout=30)
        if cdk and is_recent:
            log(f"✅ 提取到有效 CDK: {cdk[:4]}****{cdk[-4:]}")
            return cdk

        if not rendered:
            log("  ⏳ 私聊未加载完成，稍后重试（不判定为无 CDK）...")

        # 未拿到有效 CDK → 回频道等 bot 私聊回复
        page.get(DISCORD_CHANNEL_URL)
        time.sleep(4)

    return ''


# ── CDK 获取主逻辑 ────────────────────────────────────────

def ensure_cdk(page):
    log("=" * 50)
    log("🔑 开始 CDK 检查流程")
    log("=" * 50)

    if "login" in (page.url or "").lower():
        msg = "❌ Discord 登录态失效，请重新初始化 Profile"
        log(msg)
        send_tg(msg)
        return ''

    log("🔍 检查私聊是否已有24h内的 CDK...")
    cdk, is_recent, rendered = read_dm_cdk(page, load_timeout=40)

    if cdk and is_recent:
        log(f"✅ 已有24h内的 CDK，直接注入: {cdk[:4]}****{cdk[-4:]}")
        inject_nopecha_key(page, cdk)
        return cdk

    # 关键修复：私聊没加载完时不要立刻当成"没有 CDK"去重发命令，先重试读取
    if not rendered:
        for i in range(2):
            log(f"  🔁 私聊未加载完成，重试读取 ({i+1}/2)...")
            cdk, is_recent, rendered = read_dm_cdk(page, load_timeout=40)
            if cdk and is_recent:
                log(f"✅ 重试读到24h内 CDK，直接注入: {cdk[:4]}****{cdk[-4:]}")
                inject_nopecha_key(page, cdk)
                return cdk
            if rendered:
                break

    log("📭 无24h内的 CDK，去频道发送命令领取新的...")
    cdk = send_command_and_poll(page)

    if not cdk:
        msg = "❌ 未能获取 CDK，请检查 Discord"
        log(msg)
        send_tg(msg)
        return ''

    inject_nopecha_key(page, cdk)
    return cdk


# ── Discord OAuth 登录 bot-hosting（新增，替代 token 注入）──

def is_logged_in(page):
    """已登录 bot-hosting 面板判定：当前域名是 bot-hosting、不在 /login，且有 token / 面板内容"""
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
    # 兜底：在 panel 页且有实际内容
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
        _cdp_click(page, int(rect['x']), int(rect['y']))   # CDP 真实鼠标事件（trusted）
    page.run_js(finder + " if (target) target.click();")     # JS 双保险
    return True


def click_discord_authorize(page, timeout=60):
    """在 Discord OAuth 页滚动权限列表并点「授权」。若已跳回 bot-hosting 则直接成功。"""
    start = time.time()
    clicked_once = False
    while time.time() - start < timeout:
        host = (page.run_js("return location.hostname || '';") or '').lower()
        path = (page.run_js("return location.pathname || '';") or '').lower()

        if 'bot-hosting.net' in host and '/login' not in path:
            log("  ✅ 已跳回 bot-hosting（OAuth 完成）")
            return True

        if 'discord.com' in host and 'oauth2/authorize' in path:
            _scroll_discord_permissions(page)
            time.sleep(1.5)
            if _click_authorize_btn(page) and not clicked_once:
                log("  🖱️ 已点击 Discord「授权」按钮（滚动+CDP+JS）")
                clicked_once = True
            time.sleep(3)
        time.sleep(1.5)

    log("  ⚠️ 等待 Discord 授权/跳转超时")
    return False


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

    # 参考可用脚本的思路：authorized_seen 标志 + 轮询；授权页判断放前面避免误判
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


# ── 处理弹窗并解析进度（唯一改动：用 JS 读弹窗文字）────

def close_all_modals(page):
    claimed, total = None, None
    try:
        log("  → 等待成功弹窗...")
        # 等待弹窗出现
        for _ in range(15):
            has_modal = page.run_js("return !!document.querySelector('.swal-modal');")
            if has_modal:
                break
            time.sleep(1)
        time.sleep(1.5)

        # 用 JS 读取弹窗内容（比 ele().text 更可靠）
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

        # 点击 OK
        try:
            page.run_js("var btn = document.querySelector('button.swal-button.swal-button--confirm'); if(btn) btn.click();")
            log("  ✓ 已点击 OK")
            time.sleep(2)
        except:
            pass

        # 等待弹窗消失
        for _ in range(10):
            still_there = page.run_js("return !!document.querySelector('.swal-modal');")
            if not still_there:
                break
            time.sleep(1)

        # 关闭广告弹窗
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
    cdk = ""
    total_balance = "未知"

    log("=" * 50)
    log("🚀 启动：CDK 注入 + Bot-Hosting 金币领取 (DrissionPage)")
    log(f"🖥️  运行模式: {'CI' if IS_CI else '本地'}")
    log("=" * 50)

    # ── 启动浏览器 ──────────────────────────────────────
    co = ChromiumOptions()
    co.set_argument('--no-sandbox')
    co.set_argument('--disable-dev-shm-usage')
    co.set_argument('--disable-gpu')
    co.set_argument('--window-size=1280,900')
    co.set_user_data_path(PROFILE_DIR)

    # 加载 NopeCHA 扩展
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
        # ── Step 1: 确保 CDK 有效并注入 ──────────────────
        log("📂 打开 Discord 频道...")
        page.get(DISCORD_CHANNEL_URL)
        time.sleep(8)

        cdk = ensure_cdk(page)
        if not cdk:
            return

        # ── Step 2: 用 Discord OAuth 登录 bot-hosting ─────
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
            f"💰 总金币余额: {total_balance}\n"
            f"🔑 CDK: {cdk[:4]}****{cdk[-4:]}"
        )
    else:
        send_tg(
            f"⚠️ Bot-Hosting 金币领取未完成\n"
            f"📊 本次进度: {claimed}/{total}\n"
            f"💰 总金币余额: {total_balance}\n"
            f"🔑 CDK: {cdk[:4]}****{cdk[-4:]}"
        )


if __name__ == "__main__":
    main()
