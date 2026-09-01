"""Pure hold-to-talk state machine for the RC003 voice shortcut.

The RC003 device autonomously tells the host when its own physical mic button
is pressed (ATVV control opcode 0x08) and when its audio stream stops
(opcode 0x00) - see atvv_session.py. This module emits logical start/stop
edges for the physical hold gesture. It performs no I/O itself, so it's
fully unit testable; app.py translates those edges into the selected
provider's host shortcut protocol.

- The controller reports key-down on mic-button-press and key-up on physical
  release. Hold providers receive those edges directly; toggle providers may
  translate each edge into one completed shortcut tap. The device's
  AUDIO_STOP remains a fallback for machines that expose no release edge.
- Cleanup is provable: reset() reports whether KEY_UP is still owed and never
  leaves the controller thinking a session is active.

``VoiceHostAction.TAP`` remains available for app.py's optional, separate
"release then tap once" compatibility action. It is not a voice lifecycle.
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
    def __init__(self, trigger_mode: VoiceTriggerMode = VoiceTriggerMode.HOLD) -> None:
        if trigger_mode != VoiceTriggerMode.HOLD:
            raise ValueError("RC003 voice controller supports hold-to-talk only")
        self.trigger_mode = VoiceTriggerMode.HOLD
        self._holding = False

    @property
    def holding(self) -> bool:
        """Whether a key-down is currently outstanding and owes key-up."""

        return self._holding

    @property
    def active(self) -> bool:
        """Whether a hold-to-talk session is open."""

        return self._holding

    def on_mic_button_pressed(self) -> VoiceHostAction:
        """React to the device's own MIC_BUTTON control opcode."""

        self._holding = True
        return VoiceHostAction.KEY_DOWN

    def on_audio_stopped(self) -> Optional[VoiceHostAction]:
        """React to the device's own AUDIO_STOP control opcode.

        This is the fallback release when Windows did not expose the physical
        button-up edge.
        """

        return self.on_mic_button_released()

    def on_mic_button_released(self) -> Optional[VoiceHostAction]:
        """Release an outstanding HOLD shortcut on the physical button-up.

        Releasing here prevents Ctrl/Alt/Win from remaining logically down if
        the BLE control channel never reports AUDIO_STOP. Repeating the call
        from a later AUDIO_STOP is harmless and produces no second key-up.
        """

        if self._holding:
            self._holding = False
            return VoiceHostAction.KEY_UP
        return None

    def reset(self) -> Optional[VoiceHostAction]:
        """Force any outstanding session closed, e.g. on disconnect/shutdown
        cleanup.

        Returns KEY_UP if a key-down is still outstanding, otherwise None.
        ``active`` is False immediately after this returns.
        """

        if self._holding:
            self._holding = False
            return VoiceHostAction.KEY_UP
        return None

    def restore_pending(self, action: VoiceHostAction) -> None:
        """Undoes a closing action's eager state-clearing
        for a closing ``action`` that is now known to have failed to
        deliver (XRBM-019 review round 1 P1 #4).

        ``reset()`` clears the outstanding state before the caller has
        actually attempted to deliver the closing action. If it fails
        (``win32_input.send_key_combo_up`` now raises instead of swallowing
        that - see win32_input.py), the controller must go back to thinking
        the session is still owed, not silently "closed": a failed HOLD-mode
        KEY_UP should not stop the caller from retrying the release, and a
        A caller passes exactly the closing ``VoiceHostAction`` that failed.
        """

        if action == VoiceHostAction.KEY_UP:
            self._holding = True

    def cancel_pending(self) -> None:
        """Clear an outstanding session WITHOUT emitting a compensating host
        action - used when the action ``on_mic_button_pressed()`` just
        returned failed to actually deliver (see app.py's
        ``_handle_mic_button_pressed``): nothing physically landed, so there
        is nothing to release, and attempting a compensating action would
        itself be a second delivery attempt liable to fail the same way.
        """

        self._holding = False
