"""Pure two-button RC003 combination arbitration.

The configured modifier behaves like a small Fn layer.  Its ordinary click is
delayed until release so a second key can claim the gesture; a matched
combination suppresses both underlying single-key actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, List, Optional, Set


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

    def __init__(self) -> None:
        self._modifier_down: Optional[str] = None
        self._configured_buttons: FrozenSet[str] = frozenset()
        self._modifier_used = False
        self._consumed_buttons: Set[str] = set()

    def press(
        self,
        button_id: str,
        *,
        modifier: Optional[str],
        configured_buttons: FrozenSet[str],
    ) -> List[ComboCommand]:
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
            return []
        return [ComboCommand.forward_press(button_id)]

    def release(self, button_id: str) -> List[ComboCommand]:
        if button_id in self._consumed_buttons:
            self._consumed_buttons.discard(button_id)
            return []
        if button_id != self._modifier_down:
            return [ComboCommand.forward_release(button_id)]

        modifier = self._modifier_down
        used = self._modifier_used
        self._modifier_down = None
        self._configured_buttons = frozenset()
        self._modifier_used = False
        if modifier is None or used:
            return []
        return [
            ComboCommand.forward_press(modifier),
            ComboCommand.forward_release(modifier),
        ]

    def reset(self) -> None:
        self._modifier_down = None
        self._configured_buttons = frozenset()
        self._modifier_used = False
        self._consumed_buttons.clear()
