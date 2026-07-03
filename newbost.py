"""
bot-hosting.net 自动续期 (纯正 OAuth 登录版)
"""
import time
import os
import re
import json
import requests
import traceback
import sys
from pathlib import Path
from seleniumbase import SB

# ================= 配置区域 =================
HERE = Path(__file__).resolve().parent

PROXY_URL       = os.getenv("BROWSER_PROXY", "")
PROFILE_DIR     = os.getenv("BROWSER_USER_DATA_DIR",
                            os.path.expanduser("~/.chrome-profile-discord"))
NOPECHA_EXT_DIR = os.getenv("NOPECHA_EXT_DIR", 
                            os.path.join(os.path.dirname(os.path.abspath(__file__)), "chromium"))

# 面板续期所需的链接
HOME_URL     = "https://bot-hosting.net/"
LOGIN_URL    = "https://bot-hosting.net/login"
BILLINGS_URL = "https://bot-hosting.net/a/billings"
RENEW_TEXT   = "Renew"
COOLDOWN_BETWEEN_CLICKS = 3

TG_TOKEN   = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

IS_CI   = os.getenv("CI") == "true"
DISPLAY = os.environ.get("DISPLAY", ":99")
# ===========================================

def log(level: str, msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] [{level}] {msg}", flush=True)

# ── Telegram 推送 ─────────────────────────────────────────

def _tg_enabled() -> bool:
    return bool(TG_TOKEN and TG_CHAT_ID)

def tg_text(text: str) -> None:
    if not _tg_enabled():
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={
                "chat_id": TG_CHAT_ID,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
            timeout=10,
        )
    except Exception as e:
        log("WARN", f"[TG] sendMessage failed: {e}")

def tg_file(path: str | Path, caption: str = "", kind: str = "document") -> None:
    if not _tg_enabled():
        return
    p = Path(path)
    if not p.is_file():
        return
    method = "sendPhoto" if kind == "photo" else "sendDocument"
    field = "photo" if kind == "photo" else "document"
    try:
        with open(p, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
                data={"chat_id": TG_CHAT_ID, "caption": caption[:1000]},
                files={field: f},
                timeout=60,
            )
    except Exception as e:
        log("WARN", f"[TG] {method} failed: {e}")


# ── JS 与外设操作 ─────────────────────────────────────────

def js_eval(sb, expr: str):
    try:
        return sb.execute_script(f"return ({expr})")
    except Exception as e:
        log("WARN", f"js_eval 失败: {e}")
        return None

def js_click_by_text(sb, texts, tags=("a", "button"), href_contains=None):
    """CDP 模式下按可见文本 / href 找元素并 JS 点击，命中返回 True。"""
    payload = {
        "texts": [t.lower() for t in texts],
        "tags": list(tags),
        "href": (href_contains or "").lower(),
    }
    js = """(function(){
        var cfg = __PAYLOAD__;
        var els = [];
        cfg.tags.forEach(function(tag){
            document.querySelectorAll(tag).forEach(function(e){ els.push(e); });
        });
        for (var i = els.length - 1; i >= 0; i--) {
            var e = els[i];
            var r = e.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            var txt  = (e.innerText || e.textContent || '').trim().toLowerCase();
            var href = (e.getAttribute('href') || '').toLowerCase();
            var hit = false;
            if (cfg.href && href.indexOf(cfg.href) !== -1) hit = true;
            if (!hit) cfg.texts.forEach(function(t){ if (t && txt.indexOf(t) !== -1) hit = true; });
            if (hit) { e.scrollIntoView({block:'center'}); e.click(); return true; }
        }
        return false;
    })()""".replace("__PAYLOAD__", json.dumps(payload))
    try:
        return bool(sb.execute_script(js))
    except Exception as e:
        log("WARN", f"js_click_by_text 失败: {e}")
        return False


# ── bot-hosting 续期主逻辑 ──────────────────────────────

def do_renew(proxy: str | None) -> tuple[bool, int]:
    if not PROFILE_DIR:
        log("WARN", "未配置 BROWSER_USER_DATA_DIR，未加载持久化 Profile！")
    
    sb_kwargs = dict(
        uc=True,
        test=False,
        headed=True,
        xvfb=True,
        locale="en",
        user_data_dir=PROFILE_DIR if PROFILE_DIR else None,
        extension_dir=NOPECHA_EXT_DIR if NOPECHA_EXT_DIR else None,
        chromium_arg="--disable-dev-shm-usage,--no-sandbox,--window-size=1366,768",
    )
    if proxy:
        sb_kwargs["proxy"] = proxy.replace("http://", "").replace("https://", "")
        log("INFO", f"SeleniumBase 使用代理：{sb_kwargs['proxy']}")
    else:
        log("INFO", "SeleniumBase 直连运行")

    with SB(**sb_kwargs) as sb:
        sb.driver.set_page_load_timeout(60)

        # ---------------- OAuth 登录 ----------------
        log("INFO", "\n=== [Step 1] 打开登录页，尝试使用 Discord 一键登录 ===")
        # 登录页有固定链接 <a href="/login/discord">Continue with Discord</a>，
        # 直接开这个 URL 等价于点它，最稳。
        log("INFO", "直接跳转 Discord OAuth 入口 /login/discord ...")
        sb.uc_open_with_reconnect("https://bot-hosting.net/login/discord", 4)
        sb.sleep(4)
        
        cur_url = sb.get_current_url()
        oauth_success = False
        authorized_seen = False   # 是否已经进过 Discord 授权页
        for _ in range(20):
            sb.sleep(2)
            try:
                u = sb.get_current_url()
            except Exception as e:
                log("WARN", f"get_current_url 抖动（重连窗口期），重试: {e}")
                continue

            if "discord.com/oauth2/authorize" in u:
                authorized_seen = True
                log("INFO", "[OAuth] 拦截到 Discord 授权确认页，执行终极滑动逻辑...")
                # 1. 终极滑动（照搬参考脚本的最强滑动黑科技）
                sb.execute_script("""
                    const sels = ['[class*="scroller"]','[class*="oauth2"]','[class*="permissionList"]',
                        '[class*="content"] [class*="scroll"]','[class*="listScroller"]',
                        'div[class*="modal"] div[style*="overflow"]','div[class*="root"] div[style*="overflow"]'];
                    let scrolled = false;
                    for (const sel of sels) {
                        for (const el of document.querySelectorAll(sel)) {
                            const s = getComputedStyle(el);
                            if (el.scrollHeight > el.clientHeight &&
                                ['auto','scroll'].some(v => s.overflowY === v || s.overflow === v))
                                { el.scrollTop = el.scrollHeight; scrolled = true; }
                        }
                    }
                    if (!scrolled) document.querySelectorAll('div').forEach(el => {
                        if (el.scrollHeight > el.clientHeight + 10) {
                            const s = getComputedStyle(el);
                            if (['auto','scroll','hidden'].includes(s.overflowY)) el.scrollTop = el.scrollHeight;
                        }
                    });
                    scrollTo(0, document.body.scrollHeight);
                """)
                sb.sleep(1.5)
                
                # 2. 寻找并点击授权按钮（CDP 安全版：JS 文本匹配）
                if js_click_by_text(sb, texts=["authorize", "授权"], tags=("button",)):
                    log("INFO", "[OAuth] ✓ 成功点击 Discord 授权按钮！")
                sb.sleep(3)   # 给重定向留时间
            elif "bot-hosting.net" in u and ("login" not in u or authorized_seen):
                log("INFO", f"✓ 成功进入或返回面板 (URL: {u})")
                oauth_success = True
                break
        
        if not oauth_success:
            (HERE / "page_debug.html").write_text(sb.get_page_source(), encoding="utf-8")
            sb.save_screenshot(str(HERE / "page_debug.png"))
            raise RuntimeError("OAuth 流程超时，未能成功跳回面板")

        # ---------------- 跳转 Billings ----------------
        log("INFO", f"\n=== [Step 2] 跳转 {BILLINGS_URL} ===")
        sb.uc_open_with_reconnect(BILLINGS_URL, 4)
        sb.sleep(5)
        
        cur_url = sb.get_current_url()
        log("INFO", f"页面标题: {sb.get_title()}")
        log("INFO", f"当前 URL: {cur_url}")

        if "billings" not in cur_url.lower():
            log("ERROR", "未停留在 /a/billings，授权登录可能失败。")
            (HERE / "page_debug.html").write_text(sb.get_page_source(), encoding="utf-8")
            sb.save_screenshot(str(HERE / "page_debug.png"))
            raise RuntimeError("OAuth 登录后未能进入 billings 页面")
            
        log("INFO", "✓ 成功进入续期页面！")

        # ---------------- 找 Renew 按钮 ----------------
        log("INFO", f"\n=== [Step 3] 查找 '{RENEW_TEXT}' 按钮 ===")
        renew_css = f'button:contains("{RENEW_TEXT}"), a:contains("{RENEW_TEXT}")'
        try:
            sb.wait_for_element_visible(renew_css, timeout=15)
        except Exception:
            log("INFO", f"✓ 15 秒内没找到 '{RENEW_TEXT}' 按钮，说明机器都已经续期满了")
            (HERE / "page_debug.html").write_text(sb.get_page_source(), encoding="utf-8")
            sb.save_screenshot(str(HERE / "page_debug.png"))
            return False, 0

        # 如果页面有 Cloudflare Turnstile，等待 NopeCHA 插件自动解决
        log("INFO", "⏳ 预留 15 秒钟等待可能存在的人机验证自动打勾...")
        sb.sleep(15)

        buttons = sb.find_elements(renew_css)
        total_buttons = len(buttons)
        log("INFO", f"✓ 找到 {total_buttons} 个按钮，准备点击")

        # ---------------- 逐个强制 JS 点击 ----------------
        log("INFO", "\n=== [Step 4] 逐个点击 Renew 按钮 ===")
        clicked = 0
        for i in range(total_buttons):
            try:
                # 重新抓取元素，防止 StaleElement 报错
                current_buttons = sb.find_elements(renew_css)
                if i >= len(current_buttons):
                    break
                
                btn = current_buttons[i]
                text = (btn.text or "").strip()
                
                log("INFO", f"[{i+1}/{total_buttons}] '{text}' 准备强行点击！")
                
                sb.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                sb.sleep(1)
                # 使用原生 JS Click，无视 Selenium 所谓的是否可聚焦、是否被遮挡
                sb.execute_script("arguments[0].click();", btn)
                clicked += 1
                sb.sleep(COOLDOWN_BETWEEN_CLICKS)

                # 处理弹窗
                try:
                    sb.wait_for_element_visible("button.swal-button--confirm", timeout=3)
                    sb.click("button.swal-button--confirm")
                    log("INFO", "  ✓ 点掉确认弹窗")
                    sb.sleep(2)
                except Exception:
                    pass

            except Exception as e:
                log("ERROR", f"  ✗ 第 {i+1} 个按钮点击失败: {type(e).__name__}: {e}")

        sb.save_screenshot(str(HERE / "after_renew.png"))
        log("INFO", f"\n✅ 共点击 {clicked} 个 Renew 按钮")
        return clicked > 0, clicked

# ============ 入口 ============

def main():
    proxy = PROXY_URL if PROXY_URL else None

    tg_text(
        f"🚀 <b>bot-hosting renew</b>\n开始运行\n"
        f"代理：{'✓ ' + proxy if proxy else '✗ 直连'}\n"
        f"登录：自动 OAuth (Discord)"
    )

    try:
        success, clicked = do_renew(proxy)
    except Exception as e:
        tb = traceback.format_exc()
        log("ERROR", tb)
        tg_text(f"❌ <b>bot-hosting renew</b>\n<pre>{str(e)[:1000]}</pre>")
        tg_file(HERE / "page_debug.png", "page_debug", "photo")
        tg_file(HERE / "page_debug.html", "page_debug.html", "document")
        sys.exit(1)

    if success:
        tg_text(
            f"✅ <b>bot-hosting renew</b>\n成功，点击了 {clicked} 个机器"
        )
        tg_file(HERE / "after_renew.png", f"after_renew ({clicked} clicks)", "photo")
        sys.exit(0)
    else:
        tg_text(
            f"🎉 <b>bot-hosting renew</b>\n没找到可点的 Renew，大概率都已经续满啦！"
        )
        tg_file(HERE / "page_debug.png", "page_debug", "photo")
        sys.exit(0)

if __name__ == "__main__":
    main()
