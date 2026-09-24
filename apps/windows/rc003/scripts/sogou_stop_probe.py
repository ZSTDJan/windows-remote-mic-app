"""Read-only, source-only Sogou end-control feasibility probe.

RC003 diagnostic script, not imported by the runtime or packaged. Observe only
Sogou UI control metadata and existing Core Audio capture states. No key/mouse
injection, UI invocation, WM_CLOSE, process termination, app launch or config
writes. Text names outside a fixed action-label list are redacted; no speech,
clipboard, screenshots or input-field contents are collected.

Each snapshot runs in an owned, hidden subprocess with a hard timeout. Timeout
may terminate that probe child only, never Sogou. Results go to the ignored
.build/sogou-stop-probe directory; remove them after the investigation.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time


LABELS = frozenset({"关闭", "关闭窗口", "结束", "结束录音", "完成", "停止", "停止录音",
                    "暂停", "暂停录音", "继续", "继续录音", "开始", "开始录音",
                    "搜狗语音输入", "搜狗语音助手", "FloatingBallWindow", "PopupWindow"})


def action_label(name):
    text = " ".join(str(name or "").split())
    return text if text in LABELS else ("<非操作文字已省略>" if text else "")


def controls(root, *, max_nodes=192, max_depth=14, seconds=1.2):
    """Pattern availability is evidence only; no default action is executed."""
    import uiautomation as auto
    pending, rows = [(root, ())], []
    deadline = time.monotonic() + seconds
    while pending:
        if len(rows) >= max_nodes or time.monotonic() >= deadline:
            return {"complete": False, "reason": "budget", "nodes": rows}
        control, route = pending.pop()
        try:
            rect = control.BoundingRectangle
            rows.append({
                "route": list(route), "type": control.ControlTypeName,
                "name": action_label(control.Name),
                "automation_id": str(control.AutomationId or "")[:96],
                "enabled": bool(control.IsEnabled), "offscreen": bool(control.IsOffscreen),
                "rect": [rect.left, rect.top, rect.right, rect.bottom],
                "invoke": bool(control.GetPattern(auto.PatternId.InvokePattern)),
                "toggle": bool(control.GetPattern(auto.PatternId.TogglePattern)),
                "legacy": bool(control.GetPattern(auto.PatternId.LegacyIAccessiblePattern)),
            })
            children = control.GetChildren()
            if children and len(route) >= max_depth:
                return {"complete": False, "reason": "depth", "nodes": rows}
            pending.extend((child, route + (index,))
                           for index, child in reversed(list(enumerate(children))))
        except Exception as exc:
            return {"complete": False, "reason": type(exc).__name__, "nodes": rows}
    return {"complete": True, "nodes": rows}


def snapshot():
    import uiautomation as auto
    from ovb_rc003 import voice_playback_session_windows as audio, voice_program_manager
    rows = [{"pid": pid, "name": name}
            for provider, pid, name in voice_program_manager.diagnostic_voice_processes()
            if provider == "sogou"]
    pids = {row["pid"] for row in rows}
    result = {"processes": rows, "windows": []}
    try:
        result["capture"] = [{"pid": s.pid, "state": s.state} for s in audio.read_capture_sessions(pids)]
    except Exception as exc:
        result["capture_error"] = type(exc).__name__
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    handles = []

    @callback_type
    def visit(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            handles.append((int(hwnd), pid.value, bool(user32.IsWindowVisible(hwnd))))
        return True

    if not user32.EnumWindows(visit, 0):
        result["window_error"] = "EnumWindows failed"
        return result
    with auto.UIAutomationInitializerInThread():
        for hwnd, pid, visible in handles:
            row = {"hwnd": hwnd, "pid": pid, "visible": visible}
            result["windows"].append(row)
            if visible:
                row["ui"] = controls(auto.ControlFromHandle(hwnd))
    return result


def collect_once(timeout=4.0):
    try:
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--snapshot"],
            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            return {"error": "snapshot_failed", "returncode": result.returncode}
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return {"error": "snapshot_timeout"}
    except (OSError, ValueError) as exc:
        return {"error": type(exc).__name__}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--seconds", type=int, choices=range(10, 61), default=55, metavar="10..60")
    args = parser.parse_args(argv)
    if sys.platform != "win32" or sys.version_info[:2] != (3, 12):
        parser.error("Use this workspace's Windows Python 3.12 venv.")
    if args.snapshot:
        print(json.dumps(snapshot(), ensure_ascii=False))
        return 0
    if not ctypes.windll.shell32.IsUserAnAdmin():
        print("请关闭此窗口，右键启动脚本，选择“以管理员身份运行”；工具不会自动提权。")
        return 2
    directory = Path(__file__).resolve().parents[1] / ".build" / "sogou-stop-probe"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (datetime.now().strftime("observe-%Y%m%d-%H%M%S-%f") + ".jsonl")
    print("只读观察已开始，约55秒后自动结束。不发送按键、不点击或关闭任何程序。", flush=True)
    print("切回记事本，短按遥控语音键说一句测试话；本工具不要求也不建议点左侧×。", flush=True)
    print("本轮只观察气泡和采集变化；正常提交✓的单次验证另有专用工具。", flush=True)
    print("日志：" + str(path), flush=True)
    deadline = time.monotonic() + args.seconds
    with path.open("x", encoding="utf-8") as stream:
        while time.monotonic() < deadline:
            started = datetime.now().isoformat(timespec="milliseconds")
            value = collect_once(timeout=min(4.0, max(.1, deadline - time.monotonic())))
            stream.write(json.dumps({"started": started,
                "finished": datetime.now().isoformat(timespec="milliseconds"), **value}, ensure_ascii=False) + "\n")
            stream.flush()
            time.sleep(min(.5, max(0, deadline - time.monotonic())))
    print("观察结束。请告诉我是否保留末尾文字；日志已保存。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
