"""One keyboard-like toggle pulse; no application recording state.

Private to Chromecast VoiceHost. Reuse profile preparation and tracked input;
only residual UPs may be retried. Never retry a DOWN or infer bubble ownership.
WeType remains the default preparation; Sogou supplies process-only preparation.
"""
from . import hotkey, win32_input, wetype_control_windows as wetype


class ToggleShortcut:
    def __init__(self, shortcut, *, prepare=None):
        spec = hotkey.HotkeySpec.parse(shortcut)
        self.tokens = (*spec.modifiers, spec.key)
        self.release_pending = False
        # Default WeType preparation is unchanged; Sogou only checks its process.
        self.prepare = prepare or (lambda: wetype._run_on_sta_thread(wetype._activate_wetype_for_voice_start))

    def send(self, cancelled, *, before_send=None):
        if self.release_pending:
            raise OSError("previous toggle key release is unconfirmed")
        if cancelled():
            raise OSError("toggle cancelled before input profile preparation")
        switched = self.prepare()
        if cancelled():
            raise OSError("toggle cancelled after input profile preparation; shortcut not sent")
        if before_send is not None:
            before_send()
        if cancelled():
            raise OSError("toggle cancelled before shortcut; shortcut not sent")
        self.release_pending = True
        try:
            win32_input.send_voice_key_combo_tap(self.tokens)
        except win32_input.InputCleanupIncompleteError:
            raise
        except BaseException:
            # The sender can fail before or during DOWN. UP-only cleanup is
            # conservative and does not toggle the target a second time.
            self.cleanup()
            raise
        self.release_pending = False
        return switched

    def cleanup(self):
        if self.release_pending:
            try:
                win32_input.send_voice_key_combo_up(self.tokens)
            except Exception:
                return False
            self.release_pending = False
        return True
