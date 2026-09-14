"""Pure two-button RC003 combination arbitration.

The configured modifier behaves like a small Fn layer.  Its ordinary click is
delayed until release so a second key can claim the gesture; a matched
combination suppresses both underlying single-key actions.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, FrozenSet, List, Optional, Set


class ComboCommandKind(str, Enum):
    FORWARD_PRESS = "forward_press"
    FORWARD_RELEASE = "forward_release"
    TRIGGER = "trigger"


@dataclass(frozen=True)
class ComboCommand:
    kind: ComboCommandKind
    button_id: str

    @classmethod
    def forward_press(cls, button_id: str) -> "ComboCommand":
        return cls(ComboCommandKind.FORWARD_PRESS, button_id)

    @classmethod
    def forward_release(cls, button_id: str) -> "ComboCommand":
        return cls(ComboCommandKind.FORWARD_RELEASE, button_id)

    @classmethod
    def trigger(cls, button_id: str) -> "ComboCommand":
        return cls(ComboCommandKind.TRIGGER, button_id)


class ButtonComboRecognizer:
    """Delay one configured modifier and consume matching second keys."""

    MAX_HOLD_SECONDS = 10.0

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        self._modifier_down: Optional[str] = None
        self._configured_buttons: FrozenSet[str] = frozenset()
        self._modifier_used = False
        self._consumed_buttons: Set[str] = set()
        self._expires_at: Optional[float] = None

    def _reset_locked(self) -> None:
        self._modifier_down = None
        self._configured_buttons = frozenset()
        self._modifier_used = False
        self._consumed_buttons.clear()
        self._expires_at = None

    def _expire_stale_locked(self, now: float) -> bool:
        if self._expires_at is None or now < self._expires_at:
            return False
        self._reset_locked()
        return True

    def press(
        self,
        button_id: str,
        *,
        modifier: Optional[str],
        configured_buttons: FrozenSet[str],
    ) -> List[ComboCommand]:
        with self._lock:
            now = self._clock()
            self._expire_stale_locked(now)
            if button_id in self._consumed_buttons:
                return []
            if self._modifier_down is not None:
                if button_id == self._modifier_down:
                    return []
                if button_id in self._configured_buttons:
                    self._modifier_used = True
                    self._consumed_buttons.add(button_id)
                    return [ComboCommand.trigger(button_id)]
                return [ComboCommand.forward_press(button_id)]
            if modifier is not None and configured_buttons and button_id == modifier:
                self._modifier_down = modifier
                self._configured_buttons = frozenset(configured_buttons)
                self._modifier_used = False
                self._expires_at = now + self.MAX_HOLD_SECONDS
                return []
            return [ComboCommand.forward_press(button_id)]

    def release(self, button_id: str) -> List[ComboCommand]:
        with self._lock:
            self._expire_stale_locked(self._clock())
            if button_id in self._consumed_buttons:
                self._consumed_buttons.discard(button_id)
                if self._modifier_down is None and not self._consumed_buttons:
                    self._expires_at = None
                return []
            if button_id != self._modifier_down:
                return [ComboCommand.forward_release(button_id)]

            modifier = self._modifier_down
            used = self._modifier_used
            self._modifier_down = None
            self._configured_buttons = frozenset()
            self._modifier_used = False
            if not self._consumed_buttons:
                self._expires_at = None
            if modifier is None or used:
                return []
            return [
                ComboCommand.forward_press(modifier),
                ComboCommand.forward_release(modifier),
            ]

    def reset(self) -> None:
        with self._lock:
            self._reset_locked()

    def cancel_buttons(self, button_ids: Set[str]) -> Set[str]:
        """Cancel one intersecting combo while preserving unrelated input."""

        buttons = set(button_ids)
        if not buttons:
            return set()
        with self._lock:
            self._expire_stale_locked(self._clock())
            active = set(self._consumed_buttons)
            if self._modifier_down is not None:
                active.add(self._modifier_down)
            if not active.intersection(buttons):
                return set()
            self._reset_locked()
            return active

    def expire_stale(self) -> bool:
        """Drop an incomplete physical combo after its bounded hold window."""

        with self._lock:
            return self._expire_stale_locked(self._clock())

    def has_active_combo(self) -> bool:
        """Return whether a physical combination still owns its bindings."""

        with self._lock:
            self._expire_stale_locked(self._clock())
            return self._modifier_down is not None or bool(self._consumed_buttons)
