"""Manually confirmed one-shot diagnostic using the product's Sogou finish matcher.

Never sends shortcuts, clicks coordinates, starts Sogou or changes its settings.
The normal-submit action still requires the two console confirmations below.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime
import json
from pathlib import Path
import sys
import time

from ovb_rc003 import sogou_submit_windows as submit

CONFIRM_START = "我已理解"
CONFIRM_SCHEDULE = "定时提交"
SUBMIT_DELAY_SECONDS = 20
PROBE_REVISION = "submit-pattern-2"


def wait_for_completion(pid, executable, *, seconds=5.0, observe=None):
    """Observe only; an unconfirmed result never causes a second invocation."""
    deadline, clear_since = time.monotonic() + seconds, None
    last = {"candidate_state": "completion_unknown", "action_pair_present": None, "capture_active": None}
    while time.monotonic() < deadline:
        current, candidate = submit._inspect_with_executable(executable)
        active = submit._capture_active(pid)
        last = {"candidate_state": current["state"],
                "action_pair_present": current.get("action_pair_present"), "capture_active": active}
        if observe is not None:
            observe(current)
        if current.get("action_pair_present") is False and active is False:
            clear_since = clear_since or time.monotonic()
            if time.monotonic() - clear_since >= .75:
                return {"state": "completion_observed", **last}
        else:
            clear_since = None
        time.sleep(.25)
    return {"state": "completion_unconfirmed", **last}


def _write(stream, stage, value):
    row = {"at": datetime.now().isoformat(timespec="milliseconds"),
           "revision": PROBE_REVISION, "stage": stage, **value}
    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    stream.flush()


def run_confirmed_test():
    print("搜狗正常提交测试：" + PROBE_REVISION, flush=True)
    if input(f"本工具只会在你二次确认后点一次 ✓；不会点 ×。输入“{CONFIRM_START}”继续：").strip() != CONFIRM_START:
        print("未确认，未做任何操作。")
        return 0
    executable, identity_reason = submit.installed_sogou()
    if executable is None:
        print("当前搜狗版本不符合已核实的安全规则：" + identity_reason + "；未操作。")
        return 1
    directory = Path(__file__).resolve().parents[1] / ".build" / "sogou-normal-submit-test"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (datetime.now().strftime("submit-%Y%m%d-%H%M%S-%f") + ".jsonl")
    print("下一步会在20秒后尝试一次✓。请留在测试输入框，避免切换窗口影响验证。")
    if input(f"输入“{CONFIRM_SCHEDULE}”开始20秒倒计时；其他内容会取消：").strip() != CONFIRM_SCHEDULE:
        print("已取消，未点任何控件。")
        return 0
    print("现在切到记事本，短按遥控打开搜狗，12秒内说完一句测试话，然后留在记事本等待。", flush=True)
    time.sleep(SUBMIT_DELAY_SECONDS)
    with path.open("x", encoding="utf-8") as stream:
        before, candidate = submit._inspect_with_executable(executable)
        _write(stream, "before", {"package": "verified", **before})
        if candidate is None:
            print("没有找到同时满足版本、活跃采集和两按钮结构的 ✓；未操作。")
            print("日志：" + str(path))
            return 1
        _write(stream, "invoke_requested", candidate.record())
        outcome = submit.invoke_once(executable, candidate.record())
        _write(stream, "invoke", outcome)
        if outcome["state"] != "invoked_once":
            print("未确认✓调用成功，未重试；具体原因已写入日志。")
            print("日志：" + str(path))
            return 1
        completion = wait_for_completion(candidate.pid, executable,
                                         observe=lambda value: _write(stream, "after", value))
        _write(stream, "completion", completion)
    print("已只调用一次 ✓。请看记事本中的末尾文字是否保留。")
    print("结果：" + ("已观察到采集结束和窗口隐藏；文字是否完整请实际确认。"
                      if completion["state"] == "completion_observed"
                      else "尚未确认结束；已保存后续状态，不会再次点击。"))
    print("日志：" + str(path))
    return 0 if completion["state"] == "completion_observed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", action="store_true", help="只读检查是否存在可验证的 ✓，不调用它。")
    parser.add_argument("--confirm-submit", action="store_true", help="进入二次人工确认的一次性测试。")
    args = parser.parse_args(argv)
    if sys.platform != "win32" or sys.version_info[:2] != (3, 12):
        parser.error("Use this workspace's Windows Python 3.12 venv.")
    if args.snapshot:
        value, _ = submit.inspect_once()
        print(json.dumps(value, ensure_ascii=False))
        return 0
    if not args.confirm_submit:
        parser.error("默认不调用控件；请仅在人工确认测试时使用 --confirm-submit。")
    if not ctypes.windll.shell32.IsUserAnAdmin():
        print("请关闭此窗口，右键启动脚本，选择“以管理员身份运行”；工具不会自动提权。")
        return 2
    return run_confirmed_test()


if __name__ == "__main__":
    raise SystemExit(main())
