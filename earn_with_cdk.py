import time
import os
import re
import random
import subprocess
import requests
from seleniumbase import SB

# ================= 配置区域 =================
PROXY_URL       = os.getenv("BROWSER_PROXY", "")
PROFILE_DIR     = os.getenv("BROWSER_USER_DATA_DIR",
                            os.path.expanduser("~/.chrome-profile-discord"))
NOPECHA_EXT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chromium")

DISCORD_CHANNEL_URL = "https://discord.com/channels/1046086326077882479/1243188924520726538"
DISCORD_DM_URL      = "https://discord.com/channels/@me/1514785932203528202"

TARGET_URL = "https://legacy.bot-hosting.net/panel/"
EARN_URL   = "https://legacy.bot-hosting.net/panel/earn"
TOKEN      = os.getenv("TOKEN")

TG_TOKEN   = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

IS_CI   = os.getenv("CI") == "true"
DISPLAY = os.environ.get("DISPLAY", ":99")
# ===========================================


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


def send_tg_screenshot(sb, caption="debug"):
    """截图并发送到 Telegram"""
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        path = f"/tmp/{caption}.png"
        sb.save_screenshot(path)
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
            data={"chat_id": TG_CHAT_ID, "caption": caption},
            files={"photo": open(path, "rb")},
            timeout=15
        )
        log(f"📸 截图已推送 TG: {caption}")
        os.remove(path)
    except Exception as e:
        log(f"⚠️ 截图推送失败: {e}")


# ── JS 执行辅助 ───────────────────────────────────────────

def js_eval(sb, expr: str):
    try:
        return sb.execute_script(f"return ({expr})")
    except Exception as e:
        log(f"⚠️ js_eval 失败: {e}")
        return None


# ── xdotool 工具函数 ──────────────────────────────────────

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
        log(f"  🖱️ 点击 ({x},{y}) {label}")


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


# ── 获取元素屏幕坐标 ──────────────────────────────────────

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


# ── 读取页面文字 ──────────────────────────────────────────

def get_page_text(sb) -> str:
    result = js_eval(sb, "document.body.innerText")
    return result or ""


# ── 读取账户当前总金币余额 ───────────────────────────────────

def get_total_coins(sb) -> str:
    """尽力读取面板上的总金币余额。页面结构变化时请调整下方选择器。"""
    selectors = [
        '.maintitle span', 'h1.maintitle span',
        '#coins', '.coins', '[data-coins]',
        '#coin-balance', '.coin-balance', '.balance',
        '.navbar .coins', 'span.coin-amount', '#coinAmount',
    ]
    for sel in selectors:
        safe = sel.replace('"', '\\"')
        val = js_eval(sb,
            f'(function(){{var el=document.querySelector("{safe}");'
            f'return el?el.innerText.trim():null;}})()'
        )
        if val:
            m = re.search(r'[\d,]+', val)
            if m:
                coins = m.group(0).replace(",", "")
                log(f"  💰 总金币余额: {coins}（选择器: {sel}）")
                return coins

    text = get_page_text(sb)
    m = (re.search(r'(?:coins?|金币|积分)\s*[:：]?\s*([\d,]+)', text, re.IGNORECASE)
         or re.search(r'([\d,]+)\s*(?:coins?|金币|积分)', text, re.IGNORECASE))
    if m:
        coins = m.group(1).replace(",", "")
        log(f"  💰 总金币余额: {coins}（来源：页面文字）")
        return coins

    log("  ⚠️ 未能读取总金币余额")
    return "未知"


# ── CDK 提取（取最后一条，判断是否24h内）────────────────

def extract_latest_cdk(text: str):
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

def inject_nopecha_key(sb, cdk: str):
    log(f"💉 注入 key 到 NopeCHA 插件...")
    sb.open(f"https://nopecha.com/setup#{cdk}")
    time.sleep(3)
    log(f"✅ key 注入完成: {cdk[:4]}****{cdk[-4:]}")


# ── 发送命令并轮询 CDK ────────────────────────────────────

def send_command_and_poll(sb) -> str:
    sb.open(DISCORD_CHANNEL_URL)
    time.sleep(3)

    log("🖱️ 定位消息输入框...")
    ix, iy = get_element_screen_pos(sb, '[data-slate-editor="true"]')
    if not ix:
        log("❌ 找不到 Discord 输入框")
        return ''

    mouse_click(ix, iy, "输入框")
    time.sleep(random.uniform(0.5, 1.0))

    log("⌨️ 输入 !nopecha...")
    keyboard_key("ctrl+a")
    time.sleep(0.2)
    keyboard_type("!nopecha")
    time.sleep(random.uniform(0.4, 0.8))
    keyboard_key("Return")

    log("✅ 命令已发送，等待私聊回复...")
    time.sleep(5)

    for attempt in range(20):
        time.sleep(3)
        log(f"  [{attempt*3+3}s] 打开私聊检查...")

        sb.open(DISCORD_DM_URL)
        time.sleep(5)

        page_text = get_page_text(sb)
        if page_text:
            cdk, is_recent = extract_latest_cdk(page_text)
            if cdk and is_recent:
                log(f"✅ 提取到有效 CDK: {cdk[:4]}****{cdk[-4:]}")
                return cdk

        sb.open(DISCORD_CHANNEL_URL)
        time.sleep(2)

    return ''


# ── CDK 获取主逻辑（有则直接用，无则发命令）─────────────

def ensure_cdk(sb) -> str:
    log("=" * 50)
    log("🔑 开始 CDK 检查流程")
    log("=" * 50)

    if "login" in sb.get_current_url().lower():
        msg = "❌ Discord 登录态失效，请重新初始化 Profile"
        log(msg)
        send_tg(msg)
        return ''

    log("🔍 检查私聊是否已有24h内的 CDK...")
    sb.open(DISCORD_DM_URL)
    time.sleep(5)

    page_text = get_page_text(sb)
    cdk, is_recent = extract_latest_cdk(page_text) if page_text else ('', False)

    if cdk and is_recent:
        log(f"✅ 已有24h内的 CDK，直接注入，无需发送命令: {cdk[:4]}****{cdk[-4:]}")
        inject_nopecha_key(sb, cdk)
        return cdk

    log("📭 无24h内的 CDK，去频道发送命令领取新的...")
    cdk = send_command_and_poll(sb)

    if not cdk:
        msg = "❌ 未能获取 CDK，请检查 Discord"
        log(msg)
        send_tg(msg)
        return ''

    inject_nopecha_key(sb, cdk)
    return cdk


# ── 强制关闭残留弹窗 ──────────────────────────────────────

def force_close_all_modals(sb):
    log("  → 清理残留弹窗...")
    try:
        btn = sb.find_element('button.swal-button.swal-button--confirm', timeout=2)
        if btn and btn.is_displayed():
            btn.click()
            log("  ✓ 已关闭 SweetAlert 弹窗")
            time.sleep(2)
    except:
        pass

    try:
        for selector in ['div.modal-content span.close', 'span.close', '.modal-content .close']:
            btn = sb.find_element(selector, timeout=1)
            if btn and btn.is_displayed():
                btn.click()
                log("  ✓ 已关闭广告弹窗")
                time.sleep(2)
                break
    except:
        pass


# ── 处理弹窗并解析进度 ────────────────────────────────────

def close_all_modals(sb):
    claimed, total = None, None
    try:
        log("  → 等待成功弹窗...")
        sb.wait_for_element('.swal-modal', timeout=15)
        time.sleep(1.5)

        try:
            title = sb.get_text('.swal-title')
            text  = sb.get_text('.swal-text')
            log(f"  弹窗标题: {title}")
            log(f"  弹窗内容: {text}")
            m = re.search(r'(\d+)\s*/\s*(\d+)', text)
            if m:
                claimed, total = int(m.group(1)), int(m.group(2))
                log(f"  📊 进度: {claimed}/{total}")
        except Exception as e:
            log(f"  ⚠️ 解析弹窗文本失败: {e}")

        try:
            sb.click('button.swal-button.swal-button--confirm')
            log("  ✓ 已点击 OK")
            time.sleep(2)
        except:
            pass

        try:
            sb.wait_for_element_absent('.swal-modal', timeout=10)
        except:
            pass

        try:
            for selector in ['div.modal-content span.close', 'span.close', '.modal-content .close']:
                btn = sb.find_element(selector, timeout=2)
                if btn and btn.is_displayed():
                    btn.click()
                    log("  ✓ 已关闭广告弹窗")
                    time.sleep(2)
                    break
        except:
            pass

    except Exception as e:
        log(f"  ⚠️ 处理弹窗失败: {e}")

    return claimed, total


# ── 检查按钮状态 ──────────────────────────────────────────

def check_button_ready(sb, max_retries=3) -> bool:
    selector = 'button.btn.green[type="submit"]'
    for i in range(max_retries):
        try:
            log(f"  → 检查按钮状态 ({i+1}/{max_retries})...")

            # 用 JS 直接查 DOM，绕过 Selenium 可见性检查
            btn_info = js_eval(sb,
                f'(function(){{'
                f'  var el = document.querySelector("{selector}");'
                f'  if (!el) return null;'
                f'  return {{'
                f'    text: el.innerText.trim(),'
                f'    disabled: el.disabled,'
                f'    displayed: el.offsetParent !== null'
                f'  }};'
                f'}})()'
            )

            if not btn_info:
                log(f"  ⚠️ 按钮不在 DOM 中，等待 5 秒重试...")
                time.sleep(5)
                continue

            log(f"  按钮: {btn_info}")

            if not btn_info['disabled']:
                log("  ✓ 按钮可用")
                return True

            if "complete the captcha" in btn_info['text'].lower():
                log("  ⚠️ 需要 hCaptcha，等待 NopeCHA 插件自动解决...")
                for wait_round in range(20):
                    time.sleep(3)
                    info = js_eval(sb,
                        f'(function(){{'
                        f'  var e = document.querySelector("{selector}");'
                        f'  if (!e) return null;'
                        f'  return {{disabled: e.disabled, text: e.innerText.trim()}};'
                        f'}})()'
                    )
                    if info and not info['disabled']:
                        log("  ✓ hCaptcha 已自动解决，按钮可用")
                        return True
                    if wait_round % 5 == 4:
                        log(f"  ⏳ 已等待 {(wait_round+1)*3}s ...")
                log("  ⚠️ 等待超时，hCaptcha 未解决")
                send_tg_screenshot(sb, "hcaptcha_timeout")
                return False

            elif "you are on cooldown" in btn_info['text'].lower():
                log("  ⚠️ 冷却中")
                return False
            else:
                log(f"  ⚠️ 按钮 disabled（其他原因）: {btn_info['text']}")
                return False

        except Exception as e:
            log(f"  ⚠️ 检查按钮异常: {e}")
            send_tg_screenshot(sb, f"check_btn_error_{i}")
            time.sleep(5)

    return False


# ── 领取主循环 ────────────────────────────────────────────

def click_claim_coins(sb, max_attempts=15):
    selector       = 'button.btn.green[type="submit"]'
    total_coins    = 10
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

        force_close_all_modals(sb)

        if not check_button_ready(sb):
            try:
                btn_info = js_eval(sb,
                    f'(function(){{'
                    f'  var e = document.querySelector("{selector}");'
                    f'  return e ? e.innerText.trim() : "不存在";'
                    f'}})()'
                )
                log(f"  按钮状态: {btn_info}")
                if btn_info and "cooldown" in btn_info.lower():
                    log("  → 冷却等待 35 秒...")
                    time.sleep(35)
                    continue
            except:
                pass
            time.sleep(8)
            continue

        try:
            # 用 JS 直接点击，绕过 Selenium 交互
            clicked = js_eval(sb,
                f'(function(){{'
                f'  var e = document.querySelector("{selector}");'
                f'  if (e) {{ e.click(); return true; }}'
                f'  return false;'
                f'}})()'
            )
            if clicked:
                log("  ✓ 已点击（JS）")
            else:
                btn = sb.find_element(selector)
                btn.click()
                log("  ✓ 已点击（Selenium）")
        except Exception as e:
            log(f"  ⚠️ 点击失败: {e}")
            send_tg_screenshot(sb, f"click_fail_{attempt}")
            time.sleep(8)
            continue

        log("  → 等待 18 秒确保弹窗出现...")
        time.sleep(18)

        claimed, total = close_all_modals(sb)

        if claimed is not None and total is not None:
            claimed_so_far = claimed
            total_coins    = total
            log(f"  📊 进度: {claimed}/{total}")
            if claimed >= total:
                log("  🎉 已完成全部领取！")
                task_completed = True
        else:
            log("  ⚠️ 无法获取进度")
            send_tg_screenshot(sb, f"no_progress_{attempt}")

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
    log("🚀 启动：CDK 注入 + Bot-Hosting 金币领取")
    log(f"🖥️  运行模式: {'CI' if IS_CI else '本地'}")
    log("=" * 50)

    with SB(
        browser="chrome",
        headed=True,
        headless=False,
        xvfb=False,
        user_data_dir=PROFILE_DIR,
        extension_dir=NOPECHA_EXT_DIR,
        proxy=PROXY_URL if PROXY_URL else None,
        chromium_arg=(
            "--no-sandbox,"
            "--disable-dev-shm-usage,"
            "--disable-gpu,"
            "--window-size=1280,900"
        ),
    ) as sb:

        # ── Step 1: 确保 CDK 有效并注入 ──────────────────
        log("📂 打开 Discord 频道...")
        sb.open(DISCORD_CHANNEL_URL)
        time.sleep(8)

        global DISPLAY
        DISPLAY = os.environ.get("DISPLAY", DISPLAY)

        cdk = ensure_cdk(sb)
        if not cdk:
            return

        # ── Step 2: 领取 Bot-Hosting 金币 ────────────────
        log(f"\n→ 访问 {TARGET_URL}")
        sb.open(TARGET_URL)
        time.sleep(5)

        # 截图诊断：登录页还是面板页
        send_tg_screenshot(sb, "panel_after_token")
        log(f"  当前 URL: {sb.get_current_url()}")

        log("→ 写入 localStorage token")
        sb.execute_script(f"localStorage.setItem('token', '{TOKEN}')")
        log("  ✓ token 已写入")

        log(f"→ 跳转到 {EARN_URL}")
        sb.open(EARN_URL)
        time.sleep(5)

        # 截图诊断：earn 页面加载状态
        send_tg_screenshot(sb, "earn_page_after_token")
        log(f"  当前 URL: {sb.get_current_url()}")

        # 诊断按钮状态
        btn_diag = js_eval(sb,
            '(function(){'
            '  var el = document.querySelector(\'button.btn.green[type="submit"]\');'
            '  if (!el) return "按钮不在 DOM 中";'
            '  var r = el.getBoundingClientRect();'
            '  return "text=" + el.innerText.trim() +'
            '    " disabled=" + el.disabled +'
            '    " visible=" + (r.width > 0 && r.height > 0) +'
            '    " rect=" + JSON.stringify({t:Math.round(r.top),l:Math.round(r.left),w:Math.round(r.width),h:Math.round(r.height)});'
            '})()'
        )
        log(f"  诊断: {btn_diag}")

        log("→ 检查初始按钮状态")
        check_button_ready(sb, max_retries=2)

        log("→ 开始自动领取")
        success, claimed, total = click_claim_coins(sb, max_attempts=15)

        log("→ 读取账户总金币余额...")
        total_balance = get_total_coins(sb)

        log("→ 保持页面 30 秒...")
        time.sleep(30)
        log("✅ 完成，关闭浏览器")

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
