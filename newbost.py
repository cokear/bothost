"""
bot-hosting.net 自动续期 (OAuth + Turnstile xdotool 物理破解版)

依赖:
  - seleniumbase
  - xdotool (apt install xdotool)
  - Xvfb (xvfb=True 已内置)
  - cf_turnstile_solver.py (同目录)

环境变量:
  BROWSER_PROXY          代理地址
  BROWSER_USER_DATA_DIR  Chrome 持久化 Profile 目录
  TG_BOT_TOKEN           Telegram Bot Token
  TG_CHAT_ID             Telegram Chat ID
  CI                     设为 "true" 启用 CI 模式
"""

import time
import os
import json
import requests
import traceback
import sys
from pathlib import Path

from seleniumbase import SB
from cf_turnstile_solver import solve as cf_solve, is_solved

# ================= 配置区域 =================
HERE = Path(__file__).resolve().parent

PROXY_URL   = os.getenv("BROWSER_PROXY", "")
PROFILE_DIR = os.getenv("BROWSER_USER_DATA_DIR",
                        os.path.expanduser("~/.chrome-profile-discord"))

HOME_URL     = "https://bot-hosting.net/"
LOGIN_URL    = "https://bot-hosting.net/login"
BILLINGS_URL = "https://bot-hosting.net/a/billings"
RENEW_TEXT   = "Renew"
COOLDOWN_BETWEEN_CLICKS = 5

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


# ── JS 辅助 ───────────────────────────────────────────────

def js_eval(sb, expr: str):
    try:
        return sb.execute_script(f"return ({expr})")
    except Exception as e:
        log("WARN", f"js_eval 失败: {e}")
        return None


def js_click_by_text(sb, texts, tags=("a", "button"), href_contains=None):
    """按可见文本 / href 找元素并 JS 点击，命中返回 True。"""
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


def wait_for_renew_button_enabled(sb, timeout: float = 15.0) -> bool:
    """等待弹窗内的 'Renew for' 按钮从 disabled 变为 enabled。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.5)
        enabled = js_eval(sb, """
            (function() {
                var btns = document.querySelectorAll('button, a, div[role="button"], span[role="button"]');
                for (var i = 0; i < btns.length; i++) {
                    var t = btns[i].textContent || '';
                    if (t.indexOf('Renew for') !== -1) {
                        return !btns[i].disabled;
                    }
                }
                return false;
            })()
        """)
        if enabled:
            return True
    return False


def click_renew_confirm(sb, label: str = "renew 内层按钮") -> bool:
    """多策略点击弹窗内的确认续期按钮，返回是否成功。"""
    # 策略 1: JS 遍历所有按钮，按文本匹配点击
    clicked = js_click_by_text(sb, texts=["Renew for", "Renew free"], tags=("button", "a", "div"))
    if clicked:
        log("INFO", f"  ✓ JS 点击 {label} 成功")
        return True

    # 策略 2: XPath 宽泛匹配
    for xpath in [
        '//button[contains(.,"Renew for")]',
        '//a[contains(.,"Renew for")]',
        '//button[contains(.,"Renew")]',
        '//a[contains(.,"Renew")]',
        '//*[contains(@class,"btn") or contains(@class,"button")]//span[contains(.,"Renew")]/..',
    ]:
        try:
            el = sb.find_element(xpath)
            sb.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            sb.sleep(0.3)
            sb.execute_script("arguments[0].click();", el)
            log("INFO", f"  ✓ XPath 点击 {label} 成功 (xpath={xpath})")
            return True
        except Exception:
            continue

    # 策略 3: 遍历所有可见元素，找含 Renew 文本的可点击元素
    found = js_eval(sb, """
        (function() {
            var all = document.querySelectorAll('*');
            for (var i = all.length - 1; i >= 0; i--) {
                var e = all[i];
                var r = e.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                var t = (e.textContent || '').trim();
                if (t.indexOf('Renew for') !== -1 && t.length < 50) {
                    e.scrollIntoView({block:'center'});
                    e.click();
                    return true;
                }
            }
            return false;
        })()
    """)
    if found:
        log("INFO", f"  ✓ 兜底遍历点击 {label} 成功")
        return True

    return False


def diagnose_after_turnstile(sb, step: int) -> dict:
    """Turnstile 之后的诊断：token 状态 + 按钮状态。"""
    diag = js_eval(sb, """
        (function() {
            var input = document.querySelector(
                'input[name="cf-turnstile-response"], input[name="cf_turnstile_response"]'
            );
            var btns = document.querySelectorAll('button, a, div[role="button"]');
            var renewBtn = null;
            for (var i = 0; i < btns.length; i++) {
                var t = (btns[i].textContent || '').trim();
                if (t.indexOf('Renew for') !== -1) {
                    renewBtn = btns[i];
                    break;
                }
            }
            // 查找所有 modal / dialog
            var modals = document.querySelectorAll(
                '[class*="modal"], [class*="dialog"], [role="dialog"]'
            );
            var modalInfo = [];
            modals.forEach(function(m) {
                modalInfo.push({
                    tag: m.tagName,
                    classes: m.className.substring(0, 100),
                    visible: m.getBoundingClientRect().height > 0,
                    childBtnCount: m.querySelectorAll('button').length,
                    innerHTML: m.innerHTML.substring(0, 200),
                });
            });
            return {
                tokenLength: input ? input.value.length : 0,
                tokenPrefix: input ? input.value.substring(0, 30) : '(no input)',
                buttonFound: !!renewBtn,
                buttonDisabled: renewBtn ? renewBtn.disabled : null,
                buttonTag: renewBtn ? renewBtn.tagName : null,
                buttonClasses: renewBtn ? renewBtn.className.substring(0, 100) : null,
                buttonText: renewBtn ? renewBtn.textContent.trim().substring(0, 50) : null,
                buttonOffsetTop: renewBtn ? renewBtn.getBoundingClientRect().top : null,
                modalCount: modals.length,
                modals: modalInfo,
            };
        })()
    """) or {}
    log("INFO", f"  🔍 [Step {step} 诊断] token={diag.get('tokenLength', '?')} chars, "
        f"btn={diag.get('buttonFound')}, disabled={diag.get('buttonDisabled')}, "
        f"tag={diag.get('buttonTag')}, classes={diag.get('buttonClasses')}, "
        f"modals={diag.get('modalCount')}")
    return diag


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
        chromium_arg="--disable-dev-shm-usage,--no-sandbox,--window-size=1366,768",
    )
    if proxy:
        sb_kwargs["proxy"] = proxy.replace("http://", "").replace("https://", "")
        log("INFO", f"SeleniumBase 使用代理: {sb_kwargs['proxy']}")
    else:
        log("INFO", "SeleniumBase 直连运行")

    with SB(**sb_kwargs) as sb:
        sb.driver.set_page_load_timeout(60)

        # ──────────── Step 1: OAuth 登录 ────────────
        log("INFO", "\n=== [Step 1] Discord OAuth 登录 ===")
        sb.uc_open_with_reconnect("https://bot-hosting.net/login/discord", 4)
        sb.sleep(4)

        oauth_success = False
        authorized_seen = False
        for _ in range(20):
            sb.sleep(2)
            try:
                u = sb.get_current_url()
            except Exception as e:
                log("WARN", f"get_current_url 抖动: {e}")
                continue

            if "discord.com/oauth2/authorize" in u:
                authorized_seen = True
                log("INFO", "[OAuth] 拦截到 Discord 授权确认页，执行滑动...")
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
                if js_click_by_text(sb, texts=["authorize", "授权"], tags=("button",)):
                    log("INFO", "[OAuth] ✓ 点击 Discord 授权按钮")
                sb.sleep(3)
            elif "bot-hosting.net" in u and ("login" not in u or authorized_seen):
                log("INFO", f"✓ 进入面板 ({u})")
                oauth_success = True
                break

        if not oauth_success:
            (HERE / "debug_step1.html").write_text(sb.get_page_source(), encoding="utf-8")
            sb.save_screenshot(str(HERE / "debug_step1.png"))
            raise RuntimeError("OAuth 流程超时，未能跳回面板")

        # ──────────── Step 2: 跳转 Billings ────────────
        log("INFO", f"\n=== [Step 2] 跳转 {BILLINGS_URL} ===")
        sb.uc_open_with_reconnect(BILLINGS_URL, 4)
        sb.sleep(5)

        cur_url = sb.get_current_url()
        log("INFO", f"页面标题: {sb.get_title()}")
        log("INFO", f"当前 URL: {cur_url}")

        if "billings" not in cur_url.lower():
            (HERE / "debug_step2.html").write_text(sb.get_page_source(), encoding="utf-8")
            sb.save_screenshot(str(HERE / "debug_step2.png"))
            raise RuntimeError("OAuth 登录后未能进入 billings 页面")

        log("INFO", "✓ 成功进入续期页面！")

        # ──────────── Step 3: 找 Renew 按钮 ────────────
        log("INFO", f"\n=== [Step 3] 查找 '{RENEW_TEXT}' 按钮 ===")

        renew_xpath = (
            f'//button[contains(text(),"{RENEW_TEXT}")]'
            f' | //a[contains(text(),"{RENEW_TEXT}")]'
            f' | //button[.//span[contains(text(),"{RENEW_TEXT}")]]'
            f' | //a[.//span[contains(text(),"{RENEW_TEXT}")]]'
        )

        try:
            sb.wait_for_element_visible(renew_xpath, timeout=15)
        except Exception:
            log("INFO", f"✓ 未找到 '{RENEW_TEXT}' 按钮，所有机器已续期")
            (HERE / "debug_step3.html").write_text(sb.get_page_source(), encoding="utf-8")
            sb.save_screenshot(str(HERE / "debug_step3.png"))
            return False, 0

        buttons = sb.find_elements(renew_xpath)
        total = len(buttons)
        log("INFO", f"✓ 找到 {total} 个 Renew 按钮")

        # ──────────── Step 4: 逐个续期 ────────────
        log("INFO", "\n=== [Step 4] 逐个续期 (Turnstile 物理破解) ===")
        clicked = 0

        for i in range(total):
            try:
                current_buttons = sb.find_elements(renew_xpath)
                if i >= len(current_buttons):
                    break

                btn = current_buttons[i]
                text = (btn.text or "").strip()
                log("INFO", f"\n[{i+1}/{total}] '{text}'")

                # 4a. 点击外层按钮，触发弹窗
                sb.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                sb.sleep(1)
                sb.execute_script("arguments[0].click();", btn)
                log("INFO", "  ✓ 已点击外层按钮，等待弹窗...")
                sb.sleep(3)

                # 4b. cf_turnstile_solver 物理破解 Turnstile
                log("INFO", "  🛡️ 启动 Turnstile xdotool 物理破解...")
                result = cf_solve(
                    sb,
                    rounds=3,
                    post_click_wait=30,
                    on_log=lambda m: log("INFO", f"    {m}"),
                    debug=True,
                )

                if result.solved:
                    log("INFO", f"  ✅ Turnstile solver 报告通过 (耗时 {result.elapsed:.1f}s)")
                else:
                    log("WARN", f"  ❌ Turnstile 未通过: {result.status} - {result.note}")
                    sb.save_screenshot(str(HERE / f"turnstile_fail_{i+1}.png"))

                # 4c. 诊断：检查 token 和按钮实际状态
                sb.sleep(2)
                diag = diagnose_after_turnstile(sb, i + 1)
                sb.save_screenshot(str(HERE / f"diag_{i+1}.png"))

                # 如果按钮 disabled 但 solver 报告通过，等一下让前端更新
                if diag.get("buttonDisabled"):
                    log("INFO", "  ⏳ 按钮仍 disabled，等待前端状态更新...")
                    wait_for_renew_button_enabled(sb, timeout=15)
                    # 重新诊断
                    diag = diagnose_after_turnstile(sb, i + 1)

                # 4d. 点击内层确认按钮
                if click_renew_confirm(sb, label=f"[{i+1}/{total}]"):
                    clicked += 1
                    log("INFO", "  ✓ 续期确认已提交！")
                else:
                    log("ERROR", "  ✗ 所有策略均无法点击确认按钮")
                    sb.save_screenshot(str(HERE / f"click_fail_{i+1}.png"))

                sb.sleep(COOLDOWN_BETWEEN_CLICKS)

                # 兼容旧版 Swal 确认弹窗
                try:
                    if sb.is_element_visible("button.swal-button--confirm"):
                        sb.click("button.swal-button--confirm")
                        log("INFO", "  ✓ 关闭旧版确认弹窗")
                        sb.sleep(2)
                except Exception:
                    pass

            except Exception as e:
                log("ERROR", f"  ✗ 第 {i+1} 台处理失败: {type(e).__name__}: {e}")
                sb.save_screenshot(str(HERE / f"error_{i+1}.png"))

        sb.save_screenshot(str(HERE / "after_renew.png"))
        log("INFO", f"\n✅ 共成功处理了 {clicked} 台机器的续期")
        return clicked > 0, clicked


# ── 入口 ──────────────────────────────────────────────────

def main():
    proxy = PROXY_URL if PROXY_URL else None

    tg_text(
        f"🚀 <b>bot-hosting renew</b>\n开始运行\n"
        f"代理: {'✓ ' + proxy if proxy else '✗ 直连'}\n"
        f"登录: OAuth (Discord)\n"
        f"验证码: xdotool 物理破解"
    )

    try:
        success, clicked = do_renew(proxy)
    except Exception as e:
        tb = traceback.format_exc()
        log("ERROR", tb)
        tg_text(f"❌ <b>bot-hosting renew</b>\n<pre>{str(e)[:1000]}</pre>")
        tg_file(HERE / "debug_step1.png", "debug_step1", "photo")
        tg_file(HERE / "debug_step1.html", "debug_step1.html", "document")
        sys.exit(1)

    if success:
        tg_text(f"✅ <b>bot-hosting renew</b>\n成功续期 {clicked} 台机器")
        tg_file(HERE / "after_renew.png", f"after_renew ({clicked} clicks)", "photo")
    else:
        tg_text("🎉 <b>bot-hosting renew</b>\n没有需要续期的，全部已满！")
        tg_file(HERE / "debug_step3.png", "debug_step3", "photo")

    sys.exit(0)


if __name__ == "__main__":
    main()
