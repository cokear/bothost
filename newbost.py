"""
bot-hosting.net 自动续期 (纯正 OAuth 登录版 + 完整 CDK 领取逻辑)
"""
import time
import os
import re
import random
import subprocess
import requests
import traceback
from pathlib import Path
from seleniumbase import SB

# ================= 配置区域 =================
HERE = Path(__file__).resolve().parent

PROXY_URL       = os.getenv("BROWSER_PROXY", "")
PROFILE_DIR     = os.getenv("BROWSER_USER_DATA_DIR",
                            os.path.expanduser("~/.chrome-profile-discord"))
NOPECHA_EXT_DIR = os.getenv("NOPECHA_EXT_DIR", 
                            os.path.join(os.path.dirname(os.path.abspath(__file__)), "chromium"))

# 提取 CDK 所需的 Discord 链接
DISCORD_CHANNEL_URL = "https://discord.com/channels/1046086326077882479/1243188924520726538"
DISCORD_DM_URL      = "https://discord.com/channels/@me/1514785932203528202"

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

def xdo(args: list):
    env = {**os.environ, "DISPLAY": DISPLAY}
    subprocess.run(["xdotool"] + args, env=env,
                   capture_output=True, check=False)

def mouse_click(x: int, y: int, label: str = ""):
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

def keyboard_type(text: str):
    env = {**os.environ, "DISPLAY": DISPLAY}
    for ch in text:
        subprocess.run(
            ["xdotool", "type", "--clearmodifiers", "--delay", "80", "--", ch],
            env=env, capture_output=True
        )
        time.sleep(random.uniform(0.05, 0.15))

def keyboard_key(key: str):
    xdo(["key", "--clearmodifiers", key])
    time.sleep(0.1)

def get_element_screen_pos(sb, selector: str):
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
    ox = int(info.get('sx', 0) + info.get('dw', 0))
    oy = int(info.get('sy', 0) + info.get('dh', 0))
    return int(info['cx']) + ox, int(info['cy']) + oy

def get_page_text(sb) -> str:
    result = js_eval(sb, "document.body.innerText")
    return result or ""

# ── CDK 获取主逻辑（从参考脚本搬运）───────────────────────────

def extract_latest_cdk(text: str):
    pattern = re.findall(
        r'Here is your Discord key for NopeCHA[:\s]+([a-z0-9]+).*?\(([^)]+)\)',
        text, re.IGNORECASE | re.DOTALL
    )
    if not pattern:
        return '', False

    cdk, time_label = pattern[-1]
    is_recent = '内' in time_label
    log("INFO", f"  📋 最新 CDK 时间标记: {time_label} | 24h内: {is_recent}")
    return cdk, is_recent

def inject_nopecha_key(sb, cdk: str):
    log("INFO", f"💉 注入 key 到 NopeCHA 插件...")
    sb.open(f"https://nopecha.com/setup#{cdk}")
    time.sleep(3)
    log("INFO", f"✅ key 注入完成: {cdk[:4]}****{cdk[-4:]}")

def send_command_and_poll(sb) -> str:
    sb.open(DISCORD_CHANNEL_URL)
    time.sleep(3)

    log("INFO", "🖱️ 定位消息输入框...")
    ix, iy = get_element_screen_pos(sb, '[data-slate-editor="true"]')
    if not ix:
        log("ERROR", "❌ 找不到 Discord 输入框")
        return ''

    mouse_click(ix, iy, "输入框")
    time.sleep(random.uniform(0.5, 1.0))

    log("INFO", "⌨️ 输入 !nopecha...")
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

        sb.open(DISCORD_DM_URL)
        time.sleep(5)

        page_text = get_page_text(sb)
        if page_text:
            cdk, is_recent = extract_latest_cdk(page_text)
            if cdk and is_recent:
                log("INFO", f"✅ 提取到有效 CDK: {cdk[:4]}****{cdk[-4:]}")
                return cdk

        sb.open(DISCORD_CHANNEL_URL)
        time.sleep(2)

    return ''

def ensure_cdk(sb) -> str:
    log("INFO", "=" * 50)
    log("INFO", "🔑 开始 CDK 检查流程")
    log("INFO", "=" * 50)

    if "login" in sb.get_current_url().lower():
        log("WARN", "Discord 似乎未登录，直接去尝试后续步骤")
        return ''

    log("INFO", "🔍 检查私聊是否已有24h内的 CDK...")
    sb.open(DISCORD_DM_URL)
    time.sleep(5)

    page_text = get_page_text(sb)
    cdk, is_recent = extract_latest_cdk(page_text) if page_text else ('', False)

    if cdk and is_recent:
        log("INFO", f"✅ 已有24h内的 CDK，直接注入，无需发送命令: {cdk[:4]}****{cdk[-4:]}")
        inject_nopecha_key(sb, cdk)
        return cdk

    log("INFO", "📭 无24h内的 CDK，去频道发送命令领取新的...")
    cdk = send_command_and_poll(sb)

    if not cdk:
        log("WARN", "❌ 未能获取 CDK，插件可能无法正常工作")
        return ''

    inject_nopecha_key(sb, cdk)
    return cdk

# ── bot-hosting 续期主逻辑 ──────────────────────────────

def do_renew(proxy: str | None) -> tuple[bool, int, str]:
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

        # ---------------- 注入 CDK ----------------
        global DISPLAY
        DISPLAY = os.environ.get("DISPLAY", DISPLAY)
        sb.open("https://discord.com/app")
        time.sleep(5)
        cdk = ensure_cdk(sb)

        # ---------------- OAuth 登录 ----------------
        log("INFO", "\n=== [Step 2] 打开登录页，尝试使用 Discord 一键登录 ===")
        sb.uc_open_with_reconnect(LOGIN_URL, 4)
        sb.sleep(4)
        
        cur_url = sb.get_current_url()
        if "login" in cur_url:
            log("INFO", "当前在登录页，查找 Discord 登录按钮...")
            discord_btn_xpath = "//a[contains(@href, 'discord') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'discord')] | //button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'discord')]"
            try:
                sb.wait_for_element_visible(discord_btn_xpath, by="xpath", timeout=10)
                btn = sb.find_element(discord_btn_xpath, by="xpath")
                sb.execute_script("arguments[0].click();", btn)
                log("INFO", "✓ 已点击面板的 Discord 登录按钮")
            except Exception as e:
                log("ERROR", f"找不到 Discord 登录按钮: {e}")
                (HERE / "page_debug.html").write_text(sb.get_page_source(), encoding="utf-8")
                sb.save_screenshot(str(HERE / "page_debug.png"))
                raise RuntimeError("登录页找不到 Discord 按钮")

            log("INFO", "正在监听跳转流程...")
            oauth_success = False
            for _ in range(20):
                sb.sleep(2)
                if len(sb.driver.window_handles) > 1:
                    sb.switch_to_newest_window()
                    
                u = sb.get_current_url()
                if "discord.com/oauth2/authorize" in u:
                    log("INFO", "[OAuth] 拦截到 Discord 授权确认页，寻找【Authorize/授权】按钮...")
                    auth_xpath = "//button[contains(., 'Authorize') or contains(., '授权')]"
                    try:
                        sb.wait_for_element_visible(auth_xpath, timeout=3)
                        auth_btn = sb.find_element(auth_xpath, by="xpath")
                        sb.execute_script("arguments[0].click();", auth_btn)
                        log("INFO", "[OAuth] ✓ 成功点击 Discord 授权按钮！")
                        sb.sleep(2)
                    except Exception:
                        pass
                elif "bot-hosting.net" in u and "login" not in u:
                    log("INFO", f"✓ OAuth 授权完成，成功返回面板: {u}")
                    oauth_success = True
                    break
            
            if not oauth_success:
                (HERE / "page_debug.html").write_text(sb.get_page_source(), encoding="utf-8")
                sb.save_screenshot(str(HERE / "page_debug.png"))
                raise RuntimeError("OAuth 流程超时，未能成功跳回面板")
        else:
            log("INFO", f"✓ 似乎已经处于登录状态 (URL: {cur_url})")

        # ---------------- 跳转 Billings ----------------
        log("INFO", f"\n=== [Step 3] 跳转 {BILLINGS_URL} ===")
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
        log("INFO", f"\n=== [Step 4] 查找 '{RENEW_TEXT}' 按钮 ===")
        xpath = (
            f"//button[normalize-space()='{RENEW_TEXT}']"
            f" | //a[normalize-space()='{RENEW_TEXT}']"
            f" | //button[contains(normalize-space(), '{RENEW_TEXT}')]"
        )
        try:
            sb.wait_for_element_visible(xpath, by="xpath", timeout=15)
        except Exception:
            log("INFO", f"✓ 15 秒内没找到 '{RENEW_TEXT}' 按钮，说明机器都已经续期满了")
            (HERE / "page_debug.html").write_text(sb.get_page_source(), encoding="utf-8")
            sb.save_screenshot(str(HERE / "page_debug.png"))
            return False, 0, cdk

        buttons = sb.find_elements(xpath, by="xpath")
        total_buttons = len(buttons)
        log("INFO", f"✓ 找到 {total_buttons} 个按钮，准备点击")

        # ---------------- 逐个强制 JS 点击 ----------------
        log("INFO", "\n=== [Step 5] 逐个点击 Renew 按钮 ===")
        clicked = 0
        for i in range(total_buttons):
            try:
                btn_xpath = f"({xpath})[{i+1}]"
                if not sb.is_element_present(btn_xpath, by="xpath"):
                    continue
                
                btn = sb.find_element(btn_xpath, by="xpath")
                text = (btn.text or "").strip()
                enabled = btn.is_enabled()
                log("INFO", f"[{i+1}/{total_buttons}] '{text}' enabled={enabled}")
                
                if not enabled:
                    log("WARN", "  ⚠️  按钮 disabled，跳过")
                    continue
                
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
        return clicked > 0, clicked, cdk

# ============ 入口 ============

def main():
    proxy = PROXY_URL if PROXY_URL else None

    tg_text(
        f"🚀 <b>bot-hosting renew</b>\n开始运行\n"
        f"代理：{'✓ ' + proxy if proxy else '✗ 直连'}\n"
        f"登录：自动 OAuth (Discord)"
    )

    try:
        success, clicked, cdk = do_renew(proxy)
    except Exception as e:
        tb = traceback.format_exc()
        log("ERROR", tb)
        tg_text(f"❌ <b>bot-hosting renew</b>\n<pre>{str(e)[:1000]}</pre>")
        tg_file(HERE / "page_debug.png", "page_debug", "photo")
        tg_file(HERE / "page_debug.html", "page_debug.html", "document")
        sys.exit(1)

    if success:
        tg_text(
            f"✅ <b>bot-hosting renew</b>\n成功，点击了 {clicked} 个机器\n"
            f"🔑 CDK: {cdk[:4]}****{cdk[-4:]}"
        )
        tg_file(HERE / "after_renew.png", f"after_renew ({clicked} clicks)", "photo")
        sys.exit(0)
    else:
        tg_text(
            f"🎉 <b>bot-hosting renew</b>\n没找到可点的 Renew，大概率都已经续满啦！\n"
            f"🔑 CDK: {cdk[:4]}****{cdk[-4:]}"
        )
        tg_file(HERE / "page_debug.png", "page_debug", "photo")
        sys.exit(0)

if __name__ == "__main__":
    main()
