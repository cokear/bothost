"""
cf_turnstile_solver.py — Cloudflare Turnstile widget solver

A library-style helper for SeleniumBase workflows that need to solve
embedded Cloudflare Turnstile widgets via xdotool physical clicking.

Public API:
    - SolveResult
    - is_present(sb)
    - is_iframe_rendered(sb)
    - is_solved(sb)
    - get_token(sb)
    - solve(sb, ...)
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Optional


TOKEN_MIN_LENGTH = 20
DEFAULT_WINDOW_METRICS = {"sx": 0, "sy": 0, "oh": 800, "ih": 768, "dpr": 1}
WINDOW_CLASSES = ("chrome", "chromium", "Chromium", "Chrome", "google-chrome")
MAX_CLICK_X_OFFSET = 30
CLICK_X_RATIO = 0.15
MIN_HOST_WIDTH = 100
MIN_HOST_HEIGHT = 30
MAX_PARENT_STEPS = 5
MAX_EXPAND_STEPS = 20
IFRAME_WIDTH = "300px"
IFRAME_HEIGHT = "65px"
WIDGET_INPUT_SELECTOR = 'input[name="cf-turnstile-response"], input[name="cf_turnstile_response"]'

__all__ = ["SolveResult", "is_present", "is_iframe_rendered", "is_solved", "get_token", "solve"]

_EXISTS_JS = f"""
(function() {{
    return document.querySelector('{WIDGET_INPUT_SELECTOR}') !== null;
}})()
"""

_SOLVED_JS = f"""
(function() {{
    var i = document.querySelector('{WIDGET_INPUT_SELECTOR}');
    return !!(i && i.value && i.value.length > {TOKEN_MIN_LENGTH});
}})()
"""

_HAS_CF_IFRAME_JS = """
(function() {
    var iframes = document.querySelectorAll('iframe');
    for (var i = 0; i < iframes.length; i++) {
        var src = (iframes[i].src || '').toLowerCase();
        if (
            src.indexOf('challenges.cloudflare.com') >= 0 ||
            src.indexOf('turnstile') >= 0 ||
            src.indexOf('cloudflare') >= 0
        ) {
            return true;
        }
    }
    return false;
})()
"""

_COORDS_JS = f"""
(function() {{
    function clickOffset(width) {{
        return Math.round(Math.min({MAX_CLICK_X_OFFSET}, width * {CLICK_X_RATIO}));
    }}

    var iframes = document.querySelectorAll('iframe');
    for (var i = 0; i < iframes.length; i++) {{
        var src = (iframes[i].src || '').toLowerCase();
        if (
            src.indexOf('cloudflare') >= 0 ||
            src.indexOf('turnstile') >= 0 ||
            src.indexOf('challenges') >= 0
        ) {{
            var rect = iframes[i].getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {{
                return {{
                    cx: Math.round(rect.x + clickOffset(rect.width)),
                    cy: Math.round(rect.y + rect.height / 2)
                }};
            }}
        }}
    }}

    var input = document.querySelector('{WIDGET_INPUT_SELECTOR}');
    if (!input) return null;

    var parent = input.parentElement;
    for (var step = 0; step < {MAX_PARENT_STEPS}; step++) {{
        if (!parent) break;
        var hostRect = parent.getBoundingClientRect();
        if (hostRect.width > {MIN_HOST_WIDTH} && hostRect.height > {MIN_HOST_HEIGHT}) {{
            return {{
                cx: Math.round(hostRect.x + clickOffset(hostRect.width)),
                cy: Math.round(hostRect.y + hostRect.height / 2)
            }};
        }}
        parent = parent.parentElement;
    }}
    return null;
}})()
"""

_WININFO_JS = """
(function() {
    return {
        sx: window.screenX || 0,
        sy: window.screenY || 0,
        oh: window.outerHeight,
        ih: window.innerHeight,
        dpr: window.devicePixelRatio || 1
    };
})()
"""

_EXPAND_JS = f"""
(function() {{
    var ts = document.querySelector('{WIDGET_INPUT_SELECTOR}');
    if (!ts) return 'no-turnstile';

    var el = ts;
    for (var i = 0; i < {MAX_EXPAND_STEPS}; i++) {{
        el = el.parentElement;
        if (!el) break;
        var style = window.getComputedStyle(el);
        if (
            style.overflow === 'hidden' ||
            style.overflowX === 'hidden' ||
            style.overflowY === 'hidden' ||
            el.classList.contains('modal-content') ||
            el.classList.contains('modal-dialog')
        ) {{
            el.style.overflow = 'visible';
            el.style.zIndex = '999999';
        }}
        el.style.minWidth = 'max-content';
    }}

    document.querySelectorAll('iframe').forEach(function(frame) {{
        var src = (frame.src || '').toLowerCase();
        if (src.indexOf('challenges.cloudflare.com') >= 0 || src.indexOf('turnstile') >= 0) {{
            frame.style.width = '{IFRAME_WIDTH}';
            frame.style.height = '{IFRAME_HEIGHT}';
            frame.style.minWidth = '{IFRAME_WIDTH}';
            frame.style.visibility = 'visible';
            frame.style.opacity = '1';
            frame.style.zIndex = '999999';
        }}
    }});
    return 'done';
}})()
"""

_INJECT_TOKEN_LISTENER_JS = f"""
(function() {{
    if (window.__cf_token_listener_injected__) return;
    window.__cf_token_listener_injected__ = true;
    window.__cf_turnstile_token__ = '';

    window.addEventListener('message', function(event) {{
        try {{
            var data = event.data;
            if (!data || typeof data !== 'object') return;

            var token = data.token || data.response;
            if (!token || token.length <= {TOKEN_MIN_LENGTH}) return;

            window.__cf_turnstile_token__ = token;
            var inputs = document.querySelectorAll('{WIDGET_INPUT_SELECTOR}');
            for (var i = 0; i < inputs.length; i++) {{
                try {{
                    var nativeSet = Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype,
                        'value'
                    ).set;
                    nativeSet.call(inputs[i], token);
                    inputs[i].dispatchEvent(new Event('input', {{ bubbles: true }}));
                    inputs[i].dispatchEvent(new Event('change', {{ bubbles: true }}));
                }} catch (err) {{
                    inputs[i].value = token;
                }}
            }}
        }} catch (err) {{}}
    }});
}})()
"""

_READ_CAPTURED_TOKEN_JS = """
(function() {
    return window.__cf_turnstile_token__ || '';
})()
"""

_BLOCKED_JS = """
(function() {
    var title = (document.title || '').toLowerCase();
    if (title.indexOf('blocked') >= 0 || title.indexOf('access denied') >= 0) {
        return true;
    }

    var body = ((document.body && document.body.innerText) || '').toLowerCase();
    var phrases = [
        'sorry, you have been blocked',
        'you have been blocked',
        'access denied',
        'error code: 1006',
        'error code: 1010',
        'error code: 1015',
        'error code: 1020'
    ];

    for (var i = 0; i < phrases.length; i++) {
        if (body.indexOf(phrases[i]) >= 0) {
            return true;
        }
    }
    return false;
})()
"""

_READ_INPUT_TOKEN_JS = f"""
(function() {{
    var input = document.querySelector('{WIDGET_INPUT_SELECTOR}');
    return input ? (input.value || '') : '';
}})()
"""

_SCROLL_INPUT_INTO_VIEW_JS = f"""
(function() {{
    var input = document.querySelector('{WIDGET_INPUT_SELECTOR}');
    if (input && input.parentElement) {{
        input.parentElement.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
        return true;
    }}
    return false;
}})()
"""


@dataclass
class SolveResult:
    """Result contract for solve().

    When solved is True, token should be a non-empty Turnstile token.
    When solved is False, token should remain None.
    """

    solved: bool = False
    status: str = "not_present"
    token: Optional[str] = None
    rounds_used: int = 0
    elapsed: float = 0.0
    note: str = ""


def _now() -> float:
    return time.monotonic()


def _log(on_log: Optional[Callable[[str], None]], message: str) -> None:
    if on_log is None:
        return
    on_log(message)


def _debug_log(on_log: Optional[Callable[[str], None]], debug: bool, message: str) -> None:
    if debug:
        _log(on_log, message)


def _eval_js(sb, script: str, default=None):
    try:
        return sb.execute_script("return (" + script + "\n);")
    except Exception as exc:
        message = str(exc).lower()
        if "illegal return" in message or "syntaxerror" in message or "return statement" in message:
            try:
                return sb.execute_script(script)
            except Exception:
                return default
        return default


def _read_token_from_input(sb) -> Optional[str]:
    token = _eval_js(sb, _READ_INPUT_TOKEN_JS, default="") or ""
    token = str(token)
    return token if len(token) > TOKEN_MIN_LENGTH else None


def _read_token_from_listener(sb) -> Optional[str]:
    token = _eval_js(sb, _READ_CAPTURED_TOKEN_JS, default="") or ""
    token = str(token)
    return token if len(token) > TOKEN_MIN_LENGTH else None


def _inject_token_listener(sb, on_log: Optional[Callable[[str], None]] = None, debug: bool = False) -> None:
    """Inject a best-effort token listener.

    This listener is only a backup channel. If Turnstile posts the token before
    injection, the main path still relies on reading the hidden input value.
    """

    try:
        sb.execute_script(_INJECT_TOKEN_LISTENER_JS)
    except Exception as exc:
        _debug_log(on_log, debug, f"[cf] 注入 token 监听器失败: {exc}")


def _expand_widget_container(sb, on_log: Optional[Callable[[str], None]] = None, debug: bool = False) -> None:
    try:
        _eval_js(sb, _EXPAND_JS, default=None)
    except Exception as exc:
        _debug_log(on_log, debug, f"[cf] 扩展 widget 容器失败: {exc}")


def _get_window_metrics(sb):
    metrics = _eval_js(sb, _WININFO_JS, default=DEFAULT_WINDOW_METRICS)
    if not isinstance(metrics, dict):
        return dict(DEFAULT_WINDOW_METRICS)
    merged = dict(DEFAULT_WINDOW_METRICS)
    merged.update(metrics)
    return merged


def _get_widget_screen_coords(sb, on_log: Optional[Callable[[str], None]] = None, debug: bool = False):
    coords = _eval_js(sb, _COORDS_JS, default=None)
    if not coords:
        _debug_log(on_log, debug, "[cf] 未能提取 widget 坐标")
        return None

    metrics = _get_window_metrics(sb)
    dpr = float(metrics.get("dpr", 1) or 1)
    browser_bar = metrics.get("oh", 800) - metrics.get("ih", 768)
    css_x = coords["cx"] + metrics.get("sx", 0)
    css_y = coords["cy"] + metrics.get("sy", 0) + browser_bar
    return {
        "x": int(css_x * dpr),
        "y": int(css_y * dpr),
    }


def _activate_browser_window(on_log: Optional[Callable[[str], None]] = None, debug: bool = False) -> None:
    for window_class in WINDOW_CLASSES:
        try:
            result = subprocess.run(
                ["xdotool", "search", "--onlyvisible", "--class", window_class],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            window_ids = [item for item in result.stdout.strip().split("\n") if item.strip()]
            if not window_ids:
                continue
            subprocess.run(
                ["xdotool", "windowactivate", "--sync", window_ids[0]],
                timeout=3,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            time.sleep(0.2)
            return
        except Exception as exc:
            _debug_log(on_log, debug, f"[cf] 激活窗口失败 class={window_class}: {exc}")

    try:
        subprocess.run(
            ["xdotool", "getactivewindow", "windowactivate"],
            timeout=3,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception as exc:
        _debug_log(on_log, debug, f"[cf] 激活当前窗口失败: {exc}")


def _physical_click(x: int, y: int, on_log: Optional[Callable[[str], None]] = None, debug: bool = False) -> bool:
    _activate_browser_window(on_log=on_log, debug=debug)
    try:
        move_result = subprocess.run(
            ["xdotool", "mousemove", "--sync", str(x), str(y)],
            timeout=3,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if move_result.returncode != 0:
            _debug_log(on_log, debug, f"[cf] xdotool mousemove 失败: rc={move_result.returncode}")
            return False

        time.sleep(0.15)

        click_result = subprocess.run(
            ["xdotool", "click", "1"],
            timeout=2,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if click_result.returncode != 0:
            _debug_log(on_log, debug, f"[cf] xdotool click 失败: rc={click_result.returncode}")
            return False
        return True
    except Exception as exc:
        _debug_log(on_log, debug, f"[cf] xdotool 点击失败: {exc}")
        return False


def _click_widget(sb, on_log: Optional[Callable[[str], None]] = None, debug: bool = False) -> bool:
    coords = _get_widget_screen_coords(sb, on_log=on_log, debug=debug)
    if not coords:
        _log(on_log, "[cf] 无法定位 turnstile 坐标")
        return False
    clicked = _physical_click(coords["x"], coords["y"], on_log=on_log, debug=debug)
    if not clicked:
        _log(on_log, "[cf] xdotool 点击未完成")
    return clicked


def _scroll_widget_into_view(sb, on_log: Optional[Callable[[str], None]] = None, debug: bool = False) -> None:
    try:
        _eval_js(sb, _SCROLL_INPUT_INTO_VIEW_JS, default=False)
    except Exception as exc:
        _debug_log(on_log, debug, f"[cf] scrollIntoView 失败: {exc}")


def _is_blocked(sb) -> bool:
    return bool(_eval_js(sb, _BLOCKED_JS, default=False))


def _wait_for_widget(
    sb,
    timeout: float,
    poll: float,
    on_log: Optional[Callable[[str], None]] = None,
    debug: bool = False,
):
    waited = 0.0
    input_seen_at: Optional[float] = None

    while waited < timeout:
        if is_solved(sb):
            return {"state": "solved", "waited": waited, "input_seen_at": input_seen_at}

        if _is_blocked(sb):
            return {"state": "blocked", "waited": waited, "input_seen_at": input_seen_at}

        if is_iframe_rendered(sb):
            return {"state": "iframe", "waited": waited, "input_seen_at": input_seen_at}

        if input_seen_at is None and bool(_eval_js(sb, _EXISTS_JS, default=False)):
            input_seen_at = waited
            _debug_log(on_log, debug, f"[cf] input 已出现，继续等待 iframe (wait={waited:.1f}s)")

        try:
            title = (sb.get_title() or "").lower()
            if "just a moment" in title or "verify" in title:
                _log(on_log, f"[cf] 检测到挑战页标题 (wait={waited:.1f}s)")
                return {"state": "challenge", "waited": waited, "input_seen_at": input_seen_at}
        except Exception as exc:
            _debug_log(on_log, debug, f"[cf] 读取标题失败: {exc}")

        time.sleep(poll)
        waited += poll

    return {"state": "timeout", "waited": waited, "input_seen_at": input_seen_at}


def _wait_for_token(sb, timeout: float, poll: float = 0.5) -> bool:
    deadline = _now() + timeout
    while _now() < deadline:
        time.sleep(poll)
        if is_solved(sb):
            return True
    return is_solved(sb)


def is_present(sb) -> bool:
    if bool(_eval_js(sb, _EXISTS_JS, default=False)):
        return True
    return bool(_eval_js(sb, _HAS_CF_IFRAME_JS, default=False))


def is_iframe_rendered(sb) -> bool:
    return bool(_eval_js(sb, _HAS_CF_IFRAME_JS, default=False))


def is_solved(sb) -> bool:
    if bool(_eval_js(sb, _SOLVED_JS, default=False)):
        return True
    return _read_token_from_listener(sb) is not None


def get_token(sb) -> Optional[str]:
    token = _read_token_from_input(sb)
    if token:
        return token
    return _read_token_from_listener(sb)


def solve(
    sb,
    *,
    appearance_timeout: float = 40.0,
    appearance_poll: float = 1.0,
    rounds: int = 2,
    round_cooldown: float = 2.0,
    post_click_wait: float = 120.0,
    pre_click_settle: float = 1.5,
    on_log: Optional[Callable[[str], None]] = print,
    debug: bool = False,
) -> SolveResult:
    log = on_log if on_log is not None else None
    start = _now()
    result = SolveResult()
    total_rounds = max(1, int(rounds))

    _inject_token_listener(sb, on_log=log, debug=debug)

    if is_solved(sb):
        result.solved = True
        result.status = "solved"
        result.token = get_token(sb)
        result.note = "已有 token"
        result.elapsed = _now() - start
        _log(log, "[cf] 已有 token，跳过")
        return result

    widget_wait = _wait_for_widget(
        sb,
        timeout=max(0.0, appearance_timeout),
        poll=max(0.1, appearance_poll),
        on_log=log,
        debug=debug,
    )

    if widget_wait["state"] == "solved":
        result.solved = True
        result.status = "solved"
        result.token = get_token(sb)
        result.note = "等待期间 token 自动到位"
        result.elapsed = _now() - start
        _log(log, f"[cf] 等待期间 token 已到位 (wait={widget_wait['waited']:.1f}s)")
        return result

    if widget_wait["state"] == "blocked":
        result.status = "blocked"
        result.note = "检测到 Cloudflare blocked 页面"
        result.elapsed = _now() - start
        _log(log, "[cf] 检测到 blocked 页面，停止 widget 求解")
        return result

    if widget_wait["state"] == "timeout" and widget_wait["input_seen_at"] is None and not is_present(sb):
        result.status = "not_present"
        result.note = f"{appearance_timeout}s 内未检测到 widget"
        result.elapsed = _now() - start
        _log(log, f"[cf] {appearance_timeout}s 内未检测到验证组件")
        return result

    if widget_wait["state"] == "iframe":
        _log(log, f"[cf] CF iframe 已渲染 (wait={widget_wait['waited']:.1f}s)")
    elif widget_wait["state"] == "challenge":
        _log(log, "[cf] 检测到挑战页标题，继续按 widget 模式尝试")
    else:
        _log(log, "[cf] iframe 未渲染但组件在场，按 present 兜底尝试")

    for round_index in range(total_rounds):
        round_number = round_index + 1
        result.rounds_used = round_number
        _log(log, f"[cf] 处理验证 round={round_number}/{total_rounds}")

        for _ in range(2):
            _expand_widget_container(sb, on_log=log, debug=debug)
            time.sleep(0.4)

        if is_solved(sb):
            result.solved = True
            result.status = "solved"
            result.token = get_token(sb)
            result.note = f"round {round_number} 前已通过"
            result.elapsed = _now() - start
            return result

        _scroll_widget_into_view(sb, on_log=log, debug=debug)
        time.sleep(max(0.0, pre_click_settle))

        clicked = _click_widget(sb, on_log=log, debug=debug)
        if clicked:
            _log(log, "[cf] 点击完成（单次 xdotool）")

        round_wait_started = _now()
        if _wait_for_token(sb, timeout=max(0.0, post_click_wait), poll=0.5):
            result.solved = True
            result.status = "solved"
            result.token = get_token(sb)
            result.note = f"round {round_number} 通过"
            result.elapsed = _now() - start
            waited = _now() - round_wait_started
            _log(log, f"[cf] 验证已通过 (round {round_number}, wait {waited:.1f}s)")
            return result

        waited = _now() - round_wait_started
        _log(log, f"[cf] round {round_number} 等了 {waited:.0f}s 仍无 token")
        if round_index < total_rounds - 1:
            time.sleep(max(0.0, round_cooldown))

    result.status = "timeout"
    result.note = f"{total_rounds} 轮均未通过"
    result.elapsed = _now() - start
    _log(log, f"[cf] {total_rounds} 轮均未通过，总耗时 {result.elapsed:.1f}s")
    return result


if __name__ == "__main__":
    print("cf_turnstile_solver.py — 提供 solve(sb) / is_present(sb) / is_solved(sb) / get_token(sb)")
