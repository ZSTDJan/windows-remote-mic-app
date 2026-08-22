"""Pure state machine deciding when to synthesize the voice hotkey edge.

The RC003 device autonomously tells the host when its own physical mic button
is pressed (ATVV control opcode 0x08) and when its audio stream stops
(opcode 0x00) - see atvv_session.py. This module only decides, given the
configured trigger mode, what host key action that should produce. It
performs no I/O itself, so it's fully unit testable; the actual SendInput
call lives in app.py.

Semantics:

- TOGGLE mode issues a key TAP for every accepted physical mic-button press.
  The first press marks the logical session active; the second marks it
  inactive. The device's AUDIO_STOP caused by releasing either short press
  does not change that logical state. app.py owns the corresponding
  MIC_OPEN/MIC_CLOSE writes that keep device audio aligned with it.
- HOLD mode holds the key down while the physical button is held:
  key-down on mic-button-press, key-up on physical release. The device's
  AUDIO_STOP remains a fallback for machines that expose no release edge.
- Both modes' cleanup is provable: reset() always reports whether a
  closing action (KEY_UP for HOLD, TAP for TOGGLE) is still owed, and
  never leaves the controller thinking a session is still active.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from .key_mapping import VoiceTriggerMode


class VoiceHostAction(str, Enum):
    TAP = "tap"
    KEY_DOWN = "key_down"
    KEY_UP = "key_up"


class VoiceController:
    def __init__(self, trigger_mode: VoiceTriggerMode = VoiceTriggerMode.TOGGLE) -> None:
        self.trigger_mode = trigger_mode
        self._holding = False  # HOLD mode: a key-down is outstanding
        self._toggle_active = False  # TOGGLE mode: next press closes the session

    @property
    def holding(self) -> bool:
        """Whether a HOLD-mode key-down is currently outstanding (owed a
        key-up). Always False in TOGGLE mode, since toggle never holds a
        key down - it owes a closing TAP instead (see ``active``).
        """

        return self._holding

    @property
    def active(self) -> bool:
        """Whether a mic session is open from this controller's point of
        view - a HOLD-mode key-down or a TOGGLE-mode closing tap is owed.
        """

        return self._holding or self._toggle_active

    def on_mic_button_pressed(self) -> VoiceHostAction:
        """React to the device's own MIC_BUTTON control opcode."""

        if self.trigger_mode == VoiceTriggerMode.HOLD:
            self._holding = True
            return VoiceHostAction.KEY_DOWN

        self._toggle_active = not self._toggle_active
        return VoiceHostAction.TAP

    def on_audio_stopped(self) -> Optional[VoiceHostAction]:
        """React to the device's own AUDIO_STOP control opcode.

        HOLD mode uses this as a fallback release when Windows did not expose
        the physical button-up edge.
        TOGGLE mode deliberately leaves its logical session unchanged. A
        short physical press ends the device's autonomous stream, but the
        application reopens it while the toggle remains active.
        """

        return self.on_mic_button_released()

    def on_mic_button_released(self) -> Optional[VoiceHostAction]:
        """Release an outstanding HOLD shortcut on the physical button-up.

        Releasing here prevents Ctrl/Alt/Win from remaining logically down if
        the BLE control channel never reports AUDIO_STOP. Repeating the call
        from a later AUDIO_STOP is harmless and produces no second key-up.
        """

        if self.trigger_mode == VoiceTriggerMode.HOLD and self._holding:
            self._holding = False
            return VoiceHostAction.KEY_UP
        return None

    def reset(self) -> Optional[VoiceHostAction]:
        """Force any outstanding session closed, e.g. on disconnect/shutdown
        cleanup.

        Provable cleanup: returns KEY_UP if a HOLD-mode key-down is still
        outstanding, TAP if a TOGGLE-mode session is still active, or None
        if nothing is owed. Either
        way, ``active`` is False immediately after this returns.
        """

        if self._holding:
            self._holding = False
            return VoiceHostAction.KEY_UP
        if self._toggle_active:
            self._toggle_active = False
            return VoiceHostAction.TAP
        return None

    def restore_pending(self, action: VoiceHostAction) -> None:
        """Undoes a closing action's eager state-clearing
        for a closing ``action`` that is now known to have failed to
        deliver (XRBM-019 review round 1 P1 #4).

        ``reset()`` and a TOGGLE-mode second press clear the outstanding
        state before the caller has actually attempted to deliver the
        closing action. If it fails
        (``win32_input.send_key_combo_up`` now raises instead of swallowing
        that - see win32_input.py), the controller must go back to thinking
        the session is still owed, not silently "closed": a failed HOLD-mode
        KEY_UP should not stop the caller from retrying the release, and a
        failed TOGGLE-mode closing TAP should not be forgotten either. A
        caller passes exactly the closing ``VoiceHostAction`` that failed.
        """

        if action == VoiceHostAction.KEY_UP:
            self._holding = True
        elif action == VoiceHostAction.TAP:
            self._toggle_active = True

    def cancel_pending(self) -> None:
        """Clear an outstanding session WITHOUT emitting a compensating host
        action - used when the action ``on_mic_button_pressed()`` just
        returned failed to actually deliver (see app.py's
        ``_handle_mic_button_pressed``): nothing physically landed, so there
        is nothing to release, and attempting a compensating action would
        itself be a second delivery attempt liable to fail the same way.
        """

        self._holding = False
        self._toggle_active = False
