"""
bot-hosting.net 自动续期 — 纯正 OAuth 登录版

利用持久化的 Chrome Profile（内含 Discord 登录态和 NopeCHA 插件），
直接点击 Discord 登录按钮，模拟真人走真实 OAuth 流程，
彻底绕过所有 Token 失效和 UA 指纹绑定的验证机制。
"""

import os
import sys
import traceback
import time
from pathlib import Path

import requests
from seleniumbase import SB

HERE = Path(__file__).resolve().parent
HOME_URL = "https://bot-hosting.net/"
LOGIN_URL = "https://bot-hosting.net/login"
BILLINGS_URL = "https://bot-hosting.net/a/billings"
RENEW_TEXT = "Renew"
COOLDOWN_BETWEEN_CLICKS = 3

# ============ Telegram ============

def _tg_enabled() -> bool:
    return bool(os.environ.get("TG_BOT_TOKEN") and os.environ.get("TG_CHAT_ID"))

def tg_text(text: str) -> None:
    if not _tg_enabled():
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{os.environ['TG_BOT_TOKEN']}/sendMessage",
            data={
                "chat_id": os.environ["TG_CHAT_ID"],
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
            timeout=20,
        )
    except Exception as e:
        print(f"[TG] sendMessage failed: {e}")

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
            r = requests.post(
                f"https://api.telegram.org/bot{os.environ['TG_BOT_TOKEN']}/{method}",
                data={"chat_id": os.environ["TG_CHAT_ID"], "caption": caption[:1000]},
                files={field: f},
                timeout=60,
            )
    except Exception as e:
        print(f"[TG] {method} failed: {e}")

# ============ 续期主流程 ============

def do_renew(proxy: str | None) -> tuple[bool, int]:
    profile_dir = os.environ.get("BROWSER_USER_DATA_DIR", "")
    ext_dir = os.environ.get("NOPECHA_EXT_DIR", "")
    
    if not profile_dir:
        print("⚠️ 未配置 BROWSER_USER_DATA_DIR，未加载持久化 Profile！")
    
    sb_kwargs = dict(
        uc=True,
        test=False,
        headed=True,
        xvfb=True,
        locale="en",
        user_data_dir=profile_dir if profile_dir else None,
        extension_dir=ext_dir if ext_dir else None,
        chromium_arg="--disable-dev-shm-usage,--no-sandbox",
    )
    if proxy:
        sb_kwargs["proxy"] = proxy.replace("http://", "").replace("https://", "")
        print(f"→ SeleniumBase 使用代理：{sb_kwargs['proxy']}")
    else:
        print("→ SeleniumBase 直连运行")

    with SB(**sb_kwargs) as sb:
        # 调大页面加载超时
        sb.driver.set_page_load_timeout(60)

        # 1) 打开登录页并触发真实 OAuth 登录
        print("\n【1/4】打开登录页，尝试使用 Discord 一键登录 ...")
        sb.uc_open_with_reconnect(LOGIN_URL, 4)
        sb.sleep(4)
        
        cur_url = sb.get_current_url()
        if "login" in cur_url:
            print("  当前在登录页，查找 Discord 登录按钮...")
            discord_btn_xpath = "//a[contains(@href, 'discord') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'discord')] | //button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'discord')]"
            try:
                sb.wait_for_element_visible(discord_btn_xpath, timeout=10)
                btn = sb.find_element(discord_btn_xpath, by="xpath")
                sb.execute_script("arguments[0].click();", btn)
                print("  ✓ 已点击面板的 Discord 登录按钮")
            except Exception as e:
                print(f"  ✗ 找不到 Discord 登录按钮: {e}")
                (HERE / "page_debug.html").write_text(sb.get_page_source(), encoding="utf-8")
                sb.save_screenshot(str(HERE / "page_debug.png"))
                raise RuntimeError("登录页找不到 Discord 按钮")

            # 开始轮询监听 URL 的变化，处理可能出现的授权页
            print("  正在监听跳转流程...")
            oauth_success = False
            for _ in range(15):
                sb.sleep(2)
                # 处理可能弹出的新窗口
                if len(sb.driver.window_handles) > 1:
                    sb.switch_to_newest_window()
                    
                u = sb.get_current_url()
                
                # 情况 A：来到了 Discord 的授权确认页
                if "discord.com/oauth2/authorize" in u:
                    print("  [OAuth] 拦截到 Discord 授权页，寻找【Authorize/授权】按钮...")
                    auth_xpath = "//button[contains(., 'Authorize') or contains(., '授权')]"
                    try:
                        sb.wait_for_element_visible(auth_xpath, timeout=3)
                        auth_btn = sb.find_element(auth_xpath, by="xpath")
                        sb.execute_script("arguments[0].click();", auth_btn)
                        print("  [OAuth] ✓ 成功点击 Discord 授权按钮！")
                        sb.sleep(2)
                    except Exception:
                        pass
                
                # 情况 B：已经成功跳回了面板并且离开了登录页
                elif "bot-hosting.net" in u and "login" not in u:
                    print(f"  ✓ OAuth 授权完成，成功返回面板: {u}")
                    oauth_success = True
                    break
            
            if not oauth_success:
                (HERE / "page_debug.html").write_text(sb.get_page_source(), encoding="utf-8")
                sb.save_screenshot(str(HERE / "page_debug.png"))
                raise RuntimeError("OAuth 流程超时，未能成功跳回面板")
        else:
            print(f"  ✓ 似乎已经处于登录状态 (URL: {cur_url})")

        # 2) 跳转 Billings 页面
        print(f"\n【2/4】跳转 {BILLINGS_URL} ...")
        sb.uc_open_with_reconnect(BILLINGS_URL, 4)
        sb.sleep(5)
        
        cur_url = sb.get_current_url()
        print(f"  页面标题: {sb.get_title()}")
        print(f"  当前 URL: {cur_url}")

        if "billings" not in cur_url.lower():
            print(f"  ✗ 未停留在 /a/billings，授权登录可能失败。")
            (HERE / "page_debug.html").write_text(sb.get_page_source(), encoding="utf-8")
            sb.save_screenshot(str(HERE / "page_debug.png"))
            raise RuntimeError("OAuth 登录后未能进入 billings 页面")
            
        print("  ✓ 成功进入续期页面！")

        # 3) 找 Renew 按钮
        print(f"\n【3/4】查找 '{RENEW_TEXT}' 按钮 ...")
        xpath = (
            f"//button[normalize-space()='{RENEW_TEXT}']"
            f" | //a[normalize-space()='{RENEW_TEXT}']"
            f" | //button[contains(normalize-space(), '{RENEW_TEXT}')]"
        )
        try:
            sb.wait_for_element_visible(xpath, by="xpath", timeout=15)
        except Exception:
            print(f"  ✓ 15 秒内没找到 '{RENEW_TEXT}' 按钮，说明机器都已经续期满了")
            (HERE / "page_debug.html").write_text(sb.get_page_source(), encoding="utf-8")
            sb.save_screenshot(str(HERE / "page_debug.png"))
            return False, 0

        buttons = sb.find_elements(xpath, by="xpath")
        total_buttons = len(buttons)
        print(f"  ✓ 找到 {total_buttons} 个按钮，准备点击")

        # 4) 逐个强制 JS 点击
        print("\n【4/4】逐个点击 Renew 按钮 ...")
        clicked = 0
        for i in range(total_buttons):
            try:
                # 重新通过独立 XPath 获取最新的 DOM 元素，防 StaleElement 报错
                btn_xpath = f"({xpath})[{i+1}]"
                if not sb.is_element_present(btn_xpath, by="xpath"):
                    continue
                
                btn = sb.find_element(btn_xpath, by="xpath")
                text = (btn.text or "").strip()
                enabled = btn.is_enabled()
                print(f"\n  [{i+1}/{total_buttons}] '{text}' enabled={enabled}")
                
                if not enabled:
                    print("    ⚠️  按钮 disabled，跳过")
                    continue
                
                # 暴力点击，无视页面特效和盾遮挡
                sb.execute_script("arguments[0].click();", btn)
                clicked += 1
                sb.sleep(COOLDOWN_BETWEEN_CLICKS)

                # 处理弹窗
                try:
                    sb.wait_for_element_visible("button.swal-button--confirm", timeout=3)
                    sb.click("button.swal-button--confirm")
                    print("    ✓ 点掉确认弹窗")
                    sb.sleep(2)
                except Exception:
                    pass

            except Exception as e:
                print(f"    ✗ 第 {i+1} 个按钮点击失败: {type(e).__name__}: {e}")

        sb.save_screenshot(str(HERE / "after_renew.png"))
        print(f"\n✅ 共点击 {clicked} 个 Renew 按钮")
        return clicked > 0, clicked

# ============ 入口 ============

def main() -> int:
    proxy = os.environ.get("PROXY", "").strip() or None

    tg_text(
        f"🚀 <b>bot-hosting renew</b>\n开始运行\n"
        f"代理：{'✓ ' + proxy if proxy else '✗ 直连'}\n"
        f"登录：自动 OAuth (Discord)"
    )

    try:
        success, clicked = do_renew(proxy)
    except Exception as e:
        tb = traceback.format_exc()
        print(tb)
        tg_text(f"❌ <b>bot-hosting renew</b>\n<pre>{str(e)[:1000]}</pre>")
        tg_file(HERE / "page_debug.png", "page_debug", "photo")
        tg_file(HERE / "page_debug.html", "page_debug.html", "document")
        return 1

    if success:
        tg_text(f"✅ <b>bot-hosting renew</b>\n成功，点击了 {clicked} 个机器")
        tg_file(HERE / "after_renew.png", f"after_renew ({clicked} clicks)", "photo")
        return 0
    else:
        tg_text("🎉 <b>bot-hosting renew</b>\n没找到可点的 Renew，大概率都已经续满啦！")
        tg_file(HERE / "page_debug.png", "page_debug", "photo")
        return 0

if __name__ == "__main__":
    sys.exit(main())
