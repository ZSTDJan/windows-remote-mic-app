"""Own the optional receiver process; expose only validated button edges.

No GUI or hardware work on import. start() is a blocking background-operation
entry, while stop() never forgets an unconfirmed child or kills another owner.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import os
import json
import logging
import queue
import threading
import time

from .chromecast_buttons import ButtonEdge
from . import chromecast_diagnostics_windows as diagnostics
from .chromecast_channel import Channel, SessionIdentity
from .chromecast_pipe_windows import Pipe, PipeError, api, inspect_peer, verify_peer, bind_worker_peer, launch_worker

_MESSAGES = {
    "source_unconfirmed": "还没有核实这只遥控器的信号，请唤醒遥控器后重新启动服务。",
    "unsupported_layout": "这只 Chromecast 的报告格式尚未验证，本次没有接收按键。",
    "input_interface_unavailable": "Windows 尚未提供谷歌遥控器的按键接口，请短按遥控器方向键后重新启动服务；仍失败请导出日志。",
    "input_identity_unconfirmed": "未找到属于所选谷歌遥控器的按键接口，请导出日志核查设备身份。",
    "radio_ambiguous": "目前只支持一个蓝牙适配器；适配器变化后请重新启动服务。",
    "sensitive_logging_disabled": "蓝牙详细事件未开启，请在“选择设备”中重新使用谷歌遥控器。",
    "sensitive_logging_enable_failed": "蓝牙详细事件开启失败，谷歌遥控器暂不可用；请检查管理员权限或系统限制后重试。",
    "configured": "谷歌遥控器接收已准备。",
    "permission_cancelled": "管理员授权未完成，本次没有接收按键。",
    "capture_lost": "谷歌遥控器接收数据有丢失，已取消当前按键；请重新启动服务并导出日志。",
    "input_payload_unavailable": "Windows 只提供了谷歌遥控器的按键包头，缺少按键内容，已停止接收；请导出日志核查。",
    "hid_source_unconfirmed": "未能确认所选谷歌遥控器的按键来源，已停止接收；请导出日志核查。",
    "hid_symbol_unavailable": "未能定位当前蓝牙驱动的按键入口，已停止接收；请检查网络后重试或导出日志。",
    "hid_capture_failed": "谷歌按键接收没有取得可靠报告，已停止接收；请导出日志核查。",
    "stopped": "Chromecast 按键接收已停止。",
    "peer_identity_failed": "接收进程身份核对失败，本次未开始接收；请提供日志核查。",
    "pipe_connect_failed": "接收进程未能建立通信，本次未开始接收；请提供日志核查。",
}


def message(reason):
    return _MESSAGES.get(reason, "Chromecast 按键接收未就绪或已中断，请重新启动服务。")


class Client:
    def __init__(self, entity, *, mode, on_edge, on_lost=None, on_voice=None):
        if mode not in ("detect", "run", "setup") or (mode == "setup" and on_voice is not None):
            raise ValueError("invalid_mode")
        self.identity = SessionIdentity.create(entity)
        self.mode, self.on_edge = mode, on_edge
        self.on_lost = on_lost
        self.on_voice = on_voice
        self._voice_commands = queue.Queue(maxsize=32)
        self.cancel, self.finished, self.ready = threading.Event(), threading.Event(), threading.Event()
        self.thread = None
        self.reason = "capture_failed"
        self.cleanup_confirmed = False
        self.exit_succeeded = False
        self._callback_lock = threading.RLock()
        self._process_lock = threading.Lock()
        self._process = self._kernel = None
        self._worker_process = None
        self._started = False
        self._external_cancel = None
        self.failure_stage = "initializing"
        self.failure_code = ""
        self._first_worker_failure = ""

    def _observe_diagnostic(self, event):
        if event["phase"] == "failed":
            logging.getLogger("ovb_rc003").warning(
                "Chromecast failure evidence: stage=%s reason=%s", event["stage"], event["reason"],
                extra={"failure_key": f"chromecast:{event['stage']}:{event['reason']}"})
        # A recoverable 'done' observation is not a terminal failure. Cleanup
        # errors must not overwrite the first authenticated receive failure.
        if (event["phase"] == "failed" and event["stage"] in
                {"logging", "device_open", "source_probe", "capture", "receive", "voice_open", "voice_tick"}
                and not self._first_worker_failure):
            self._first_worker_failure = event["reason"]
            diagnostics.request(self.identity.entity, event["reason"])

    @property
    def is_running(self):
        return bool(self.thread and self.thread.is_alive() and not self.finished.is_set())

    def start(self, *, cancel_event=None):
        if self.thread is not None:
            raise RuntimeError("接收器不能重复启动。")
        self._external_cancel = cancel_event
        if cancel_event is not None and cancel_event.is_set():
            self.cancel.set()
            raise RuntimeError("谷歌遥控器操作已取消。")
        self.thread = threading.Thread(target=self._run, name="chromecast-receiver", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 40
        if self.mode == "setup":
            while not self.finished.wait(.02):
                if time.monotonic() >= deadline or (cancel_event and cancel_event.is_set()):
                    self.stop()
                    raise RuntimeError(message(self.reason))
            if (self.reason != "configured" or not self.cleanup_confirmed or not self.exit_succeeded
                    or self.cancel.is_set() or (cancel_event and cancel_event.is_set())):
                if self.reason == "configured" and not self.exit_succeeded:
                    self.reason = "capture_failed"
                self.stop()
                raise RuntimeError(message(self.reason))
            return
        while not self.ready.wait(.02):
            if self.finished.is_set() or time.monotonic() >= deadline or (cancel_event and cancel_event.is_set()):
                self.stop()
                raise RuntimeError(message(self.reason))
        if self.finished.is_set() or self.cancel.is_set() or (cancel_event and cancel_event.is_set()):
            self.stop()
            raise RuntimeError(message(self.reason))

    def stop(self):
        # Serialize with delivery: after stop returns no ordinary edge can run.
        with self._callback_lock:
            self.cancel.set()
        if self.thread is None:
            self.cleanup_confirmed = True
            return
        if threading.current_thread() is self.thread:
            raise RuntimeError("停止接收必须由资源所有者完成。")
        self.thread.join(5)
        if not self.thread.is_alive():
            self._confirm_exit()
        if self.thread.is_alive() or not self.cleanup_confirmed:
            snapshot = (self.thread.is_alive(), self.cleanup_confirmed, self._process is not None,
                        self._worker_process is not None)
            if snapshot != getattr(self, "_last_stop_snapshot", None):
                self._last_stop_snapshot = snapshot
                logging.getLogger("ovb_rc003").warning(
                    "Chromecast stop pending: thread_alive=%s cleanup_confirmed=%s launcher_retained=%s worker_retained=%s",
                    *snapshot)
            raise RuntimeError("Chromecast 接收进程尚未确认退出；暂时不能切换设备，请稍后重试停止。")

    def _confirm_exit(self):
        with self._process_lock:
            if self._process is None:
                return
            handles = [handle for handle in (self._process, self._worker_process) if handle is not None]
            waits = tuple(self._kernel.WaitForSingleObject(handle, 0) for handle in handles)
            if waits != getattr(self, "_last_exit_waits", None):
                self._last_exit_waits = waits
                logging.getLogger("ovb_rc003").info("Chromecast process exit wait: results=%s", waits)
            if any(result != 0 for result in waits):
                return
            self.cleanup_confirmed = True
            self.exit_succeeded = True
            for handle in handles:
                code = W.DWORD()
                read_ok = bool(self._kernel.GetExitCodeProcess(handle, C.byref(code)))
                logging.getLogger("ovb_rc003").info(
                    "Chromecast process exited: role=%s exit_code_read=%s exit_code=%s started=%s",
                    "launcher" if handle == self._process else "worker", read_ok,
                    code.value if read_ok else None, self._started)
                # The OS wait proves process termination. A nonzero exit is a
                # recorded runtime/cleanup failure, not a still-running owner.
                # Conflating them discards the handles but blocks every retry.
                self.exit_succeeded &= bool(read_ok and code.value == 0)
                self._kernel.CloseHandle(handle)
            self._process = self._worker_process = None

    def _deliver(self, edge):
        with self._callback_lock:
            if not self.cancel.is_set() or edge.action == "cancel":
                self.on_edge(edge)

    def voice_host(self, attempt, result):
        if self.cancel.is_set() or self.finished.is_set():
            return False
        try:
            self._voice_commands.put_nowait((attempt, result))
            return True
        except queue.Full:
            self.cancel.set()
            return False

    def _run(self):
        pipe, child, kernel = None, None, None
        started, terminal = False, False
        incoming, outgoing = Channel(self.identity, commands=False), Channel(self.identity, commands=True)
        try:
            parent = inspect_peer(os.getpid())
            pipe = Pipe(self.identity, server=True)
            if self.cancel.is_set() or (self._external_cancel and self._external_cancel.is_set()):
                return
            self.failure_stage = "launch"
            child = launch_worker(self.identity, parent)
            kernel, _ = api()
            launched = inspect_peer(kernel.GetProcessId(child))
            self.failure_stage = "pipe_connect"
            pipe.connect(time.monotonic() + 10, self.cancel)
            self.failure_stage = "peer_identity"
            expected, self._worker_process = bind_worker_peer(pipe, launched, parent)
            pid = expected.pid
            if self.cancel.is_set() or (self._external_cancel and self._external_cancel.is_set()):
                return
            self.failure_stage = "receive"
            voice_fields = {"voice": True} if self.on_voice is not None else {}
            pipe.write(outgoing.encode("start", mode=self.mode, **voice_fields))
            started = True
            last_heartbeat, last_peer = 0.0, 0.0
            ready_deadline = time.monotonic() + 30
            stop_deadline = None
            while True:
                now = time.monotonic()
                if self.cancel.is_set() and not outgoing.closed:
                    pipe.write(outgoing.encode("stop"))
                    # A fallback symbol lookup is bounded to 18 seconds; let
                    # its worker finish and detach before declaring peer loss.
                    stop_deadline = now + 25
                if stop_deadline is not None and now >= stop_deadline:
                    raise PipeError("peer_lost")
                if not self.ready.is_set() and now > ready_deadline:
                    raise PipeError("source_unconfirmed")
                if now - last_peer >= 1:
                    verify_peer(inspect_peer(pid), expected)
                    last_peer = now
                if not outgoing.closed and now - last_heartbeat >= 1:
                    pipe.write(outgoing.encode("heartbeat"))
                    last_heartbeat = now
                while not outgoing.closed:
                    try:
                        attempt, result = self._voice_commands.get_nowait()
                    except queue.Empty:
                        break
                    pipe.write(outgoing.encode("voice_host", attempt=attempt, result=result))
                for _ in range(64):
                    raw = pipe.read()
                    if raw is None:
                        break
                    event = incoming.decode(raw)
                    if event["type"] == "evidence":
                        logging.getLogger("ovb_rc003").info("Chromecast evidence run=%s seq=%s record=%s",
                            self.identity.generation[:12], event["seq"], json.dumps(event["record"], separators=(",", ":")))
                    elif event["type"] == "ready":
                        self.ready.set()
                        diagnostics.request(self.identity.entity, "ready")
                    elif event["type"] == "edge":
                        age = time.monotonic() - event["time"]
                        if not -.1 <= age <= 1:
                            raise PipeError("capture_lost")
                        self._deliver(ButtonEdge(self.identity.entity, self.identity.generation,
                                                event["time"], event["button"], event["action"]))
                    elif event["type"] == "diagnostic":
                        self._observe_diagnostic(event)
                        logging.getLogger("ovb_rc003").info(
                            "Chromecast worker: stage=%s phase=%s reason=%s event_kind=%s attribute=%s length=%s report_code=%s tail_nonzero=%s acl_header_parsed=%s acl_issue=%s acl_flags=%s acl_handle=%s acl_boundary=%s acl_declared_length=%s acl_actual_length=%s acl_length_relation=%s acl_handle_valid=%s acl_reserved_flags_clear=%s acl_payload_nonempty=%s acl_length_match=%s l2cap_header_parsed=%s l2cap_declared_length=%s l2cap_cid=%s att_header_parsed=%s att_opcode=%s att_attribute=%s att_missing_length=%s capture_metadata_available=%s capture_layout=%s capture_event_id=%s capture_event_version=%s capture_user_data_length=%s capture_property_length=%s capture_extracted_length=%s capture_length_match=%s",
                            *(event[key] for key in (
                                "stage", "phase", "reason", "event_kind", "attribute", "length", "report_code",
                                "tail_nonzero", "acl_header_parsed", "acl_issue", "acl_flags", "acl_handle",
                                "acl_boundary", "acl_declared_length", "acl_actual_length", "acl_length_relation",
                                "acl_handle_valid", "acl_reserved_flags_clear", "acl_payload_nonempty", "acl_length_match",
                                "l2cap_header_parsed", "l2cap_declared_length", "l2cap_cid", "att_header_parsed",
                                "att_opcode", "att_attribute", "att_missing_length",
                                "capture_metadata_available", "capture_layout", "capture_event_id", "capture_event_version",
                                "capture_user_data_length", "capture_property_length", "capture_extracted_length",
                                "capture_length_match",
                            )))
                    elif event["type"] == "voice":
                        if not -.1 <= time.monotonic() - event["time"] <= 1:
                            raise PipeError("capture_lost")
                        if event["event"] == "probe":
                            logging.getLogger("ovb_rc003").info("Chromecast voice receive %s", event["data"])
                        if event["event"] == "lifecycle":
                            from .chromecast_voice import lifecycle_log_text
                            logging.getLogger("ovb_rc003").info("Chromecast recording attempt=%s %s",
                                event["attempt"], lifecycle_log_text(event["data"]))
                            continue  # Diagnostic metadata never drives host actions.
                        if self.on_voice:
                            self.on_voice(event)
                    else:
                        terminal, self.reason = True, event["reason"]
                        logging.getLogger("ovb_rc003").info(
                            "Chromecast terminal: type=%s reason=%s", event["type"], self.reason,
                            extra={"failure_key": f"chromecast:terminal:{self.reason}"
                                   if event["type"] == "error" else ""})
                        return
                time.sleep(.01)
        except Exception as error:
            # Log only fixed error codes; never paths, tokens or arbitrary text.
            code = str(error)
            safe_codes = {"peer_not_same_user_session_image", "peer_launch_mismatch", "peer_changed",
                          "peer_unavailable", "peer_identity_unavailable", "peer_parent_unavailable",
                          "pipe_connect_cancelled", "pipe_connect_failed", "pipe_read_failed", "pipe_write_failed"}
            self.failure_code = code if code in safe_codes or code in _MESSAGES else type(error).__name__
            self.reason = (code if code in _MESSAGES else "peer_identity_failed" if self.failure_stage == "peer_identity"
                           else "pipe_connect_failed" if self.failure_stage == "pipe_connect" else "capture_failed")
            if self._first_worker_failure:
                self.reason = self._first_worker_failure
            if not self.cancel.is_set():
                logging.getLogger("ovb_rc003").warning(
                    "Chromecast receiver failed: stage=%s reason=%s code=%s",
                    self.failure_stage, self.reason, self.failure_code,
                    extra={"failure_key": f"chromecast:receiver:{self.reason}"})
        finally:
            if self.on_lost:
                try:
                    self.on_lost()
                except Exception:
                    pass
            edge = incoming.cancel_pending(time.monotonic())
            if edge:
                try:
                    self._deliver(edge)
                except Exception:
                    pass
            # Ask only this authenticated worker to stop. Closing the pipe also
            # expires its lease. Keep the owner until OS exit is confirmed.
            if pipe:
                try:
                    if started and not outgoing.closed:
                        pipe.write(outgoing.encode("stop"))
                except Exception:
                    pass
                finally:
                    try:
                        pipe.close()
                    except Exception:
                        if not self._first_worker_failure:
                            self.reason = "capture_failed"
            if child:
                kernel = kernel or api()[0]
                self._process, self._kernel, self._started = child, kernel, started
                kernel.WaitForSingleObject(child, 12000)
                self._confirm_exit()
            else:
                self.cleanup_confirmed = True
            self.ready.clear()
            self.finished.set()
