"""Attempt-local WeType capture metadata, not speech or bubble visibility."""
from __future__ import annotations

from . import voice_playback_session_windows as audio, voice_program_manager

# Shared startup cadence. A time limit and a count limit both fence the burst.
STARTUP_POLL_SECONDS = .05
STARTUP_FAST_WINDOW_SECONDS = .5
STARTUP_MAX_FAST_POLLS = 10


def wetype_pids(*, capture=False):
    candidates = (voice_program_manager.diagnostic_voice_processes(include_wetype_capture=True)
                  if capture else voice_program_manager.diagnostic_voice_processes())
    if len(candidates) > 32:
        raise OSError("voice process enumeration is incomplete")
    return {pid for provider, pid, _name in candidates if provider == "wetype"}


def read_wetype_capture():
    return audio.read_capture_sessions(wetype_pids(capture=True))


def read_sogou_capture():
    candidates = voice_program_manager.diagnostic_voice_processes()
    if len(candidates) > 32:
        raise OSError("voice process enumeration is incomplete")
    return audio.read_capture_sessions({pid for provider, pid, _ in candidates if provider == "sogou"})


def doubao_capture_pids():
    """Return only the adapter's exact, still-to-be-verified capture candidate."""

    candidates = voice_program_manager.diagnostic_voice_processes()
    if len(candidates) > 32:
        raise OSError("voice process enumeration is incomplete")
    return {
        pid
        for provider, pid, name in candidates
        if provider == voice_program_manager.VOICE_PROGRAM_DOUBAO_IME
        and name.casefold() == "imeservice.exe"
    }


def read_doubao_capture():
    return audio.read_capture_sessions(doubao_capture_pids())


def read_doubao_capture_for_pid(pid: int):
    """Read capture metadata only for the verified adapter target."""

    target_pid = int(pid)
    if target_pid <= 0:
        return ()
    return audio.read_capture_sessions({target_pid})


class CaptureWatch:
    """Bind once to a newly active session; unknown readings never mean ended.

    Grace tolerates engine state transitions, not pauses in speech. Silence
    leaves a running capture Active. Replacement sessions are not auto-adopted.
    """
    POLL_SECONDS = .25
    STARTUP_POLL_SECONDS = STARTUP_POLL_SECONDS
    STARTUP_FAST_WINDOW_SECONDS = STARTUP_FAST_WINDOW_SECONDS
    END_GRACE_SECONDS = .75

    def __init__(self, reader=None, *, fast_start=False):
        self.reader = reader or read_wetype_capture
        self._fast_start = fast_start
        self._fast_start_until = None
        self._fast_polls = 0
        self.baseline = None
        self.identity = None
        self.inactive_since = None
        self.next_poll = 0.0
        self.ended = False
        self.status = "waiting"

    def begin(self):
        try:
            self.baseline = {s.identity for s in self.reader() if s.state == 1}
        except Exception:
            self.status = "unavailable"

    def poll(self, now):
        if self.ended or self.baseline is None or now < self.next_poll:
            return self.ended
        if self._fast_start_until is None:
            self._fast_start_until = now + self.STARTUP_FAST_WINDOW_SECONDS
        # Only opted-in startup probes get a short burst. Slow/failed hosts
        # return to the ordinary rate, as do all bound capture sessions.
        fast = (self._fast_start and self.identity is None
                and now < self._fast_start_until
                and self._fast_polls < STARTUP_MAX_FAST_POLLS)
        if fast:
            self._fast_polls += 1
        self.next_poll = now + (self.STARTUP_POLL_SECONDS if fast else self.POLL_SECONDS)
        try:
            active = {s.identity for s in self.reader() if s.state == 1}
        except Exception:
            self.inactive_since = None
            self.status = "unknown"
            return False
        if self.identity is None:
            candidates = active - self.baseline
            if len(candidates) != 1:
                self.status = "waiting" if not candidates else "ambiguous"
                return False
            self.identity = next(iter(candidates))
            self.next_poll = now + self.POLL_SECONDS
        if self.identity in active:
            self.inactive_since = None
            self.status = "tracking"
        else:
            if self.inactive_since is None:
                self.inactive_since = now
            self.ended = now - self.inactive_since >= self.END_GRACE_SECONDS
            self.status = "ended" if self.ended else "ending"
        return self.ended
