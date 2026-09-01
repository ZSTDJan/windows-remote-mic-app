"""User-selected audio output endpoint resolution, with a strict fail-closed
policy: voice never falls back to the system default device.

Enumeration and PCM playback (Windows-only, via the ``sounddevice`` package)
are separated from the selection logic below so the selection/fail-closed
contract is unit-testable on any OS without the real dependency installed.

This module never changes which device Windows considers "default" for
anything; it only ever writes PCM to the one endpoint the user explicitly
picked in the settings UI, by name, each time a voice session starts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence


class AudioOutputUnavailableError(Exception):
    """Raised whenever voice cannot be routed. Callers must fail the voice
    path closed on this (buttons keep working; only voice is affected).
    """


@dataclass(frozen=True)
class AudioEndpoint:
    name: str
    host_api: str = ""


UNSUPPORTED_BLOCKING_OUTPUT_HOST_APIS = frozenset({"Windows WDM-KS"})
PREFERRED_OUTPUT_HOST_APIS = (
    "Windows WASAPI",
    "Windows DirectSound",
    "MME",
)


def is_supported_output_host_api(host_api: str) -> bool:
    """Whether the current blocking ``sounddevice.OutputStream`` path can
    use this PortAudio host API.

    PortAudio exposes WDM-KS devices during enumeration, but its blocking
    read/write API is not implemented for that backend. Showing or accepting
    one therefore creates a selection that can never carry RC003 PCM.
    """

    return host_api not in UNSUPPORTED_BLOCKING_OUTPUT_HOST_APIS


def ensure_supported_output_endpoint(endpoint: AudioEndpoint) -> AudioEndpoint:
    if not is_supported_output_host_api(endpoint.host_api):
        raise AudioOutputUnavailableError(
            f"output host API {endpoint.host_api!r} is not supported by "
            "the current blocking playback path; select Windows WASAPI or "
            "Windows DirectSound"
        )
    return endpoint


def output_host_api_rank(host_api: str) -> int:
    try:
        return PREFERRED_OUTPUT_HOST_APIS.index(host_api)
    except ValueError:
        return len(PREFERRED_OUTPUT_HOST_APIS)


def select_preferred_output_endpoint(
    endpoints: Sequence[AudioEndpoint],
) -> AudioEndpoint:
    """Choose one usable host-API view without guessing between devices.

    Windows commonly exposes the same physical endpoint through several
    PortAudio host APIs. Prefer WASAPI, then DirectSound. If the best rank is
    still represented by more than one endpoint, identity remains ambiguous
    and the caller must fail closed.
    """

    usable = [
        endpoint
        for endpoint in endpoints
        if is_supported_output_host_api(endpoint.host_api)
    ]
    if not usable:
        raise AudioOutputUnavailableError(
            "no output endpoint uses a supported blocking playback host API"
        )
    best_rank = min(output_host_api_rank(endpoint.host_api) for endpoint in usable)
    preferred = [
        endpoint
        for endpoint in usable
        if output_host_api_rank(endpoint.host_api) == best_rank
    ]
    if len(preferred) != 1:
        raise AudioOutputUnavailableError(
            f"{len(preferred)} equally preferred output endpoints remain; "
            "select a unique device explicitly"
        )
    return preferred[0]


def resolve_selected_endpoint(
    endpoints: Sequence[AudioEndpoint],
    selected_name: Optional[str],
    selected_host_api: Optional[str] = None,
) -> AudioEndpoint:
    """Return the endpoint the user selected, or fail closed.

    Never falls back to the system default or a differently named endpoint.
    A saved ``selected_host_api`` must still resolve exactly. Older configs
    that saved only a name may match several host-API views of that same
    endpoint; those are ranked WASAPI, DirectSound, then MME. More than one
    candidate at the best rank still fails closed instead of guessing.
    """

    if not selected_name:
        raise AudioOutputUnavailableError(
            "no output endpoint has been selected; open settings and choose one"
        )

    if selected_host_api and not is_supported_output_host_api(selected_host_api):
        ensure_supported_output_endpoint(
            AudioEndpoint(name=selected_name, host_api=selected_host_api)
        )

    name_matches = [endpoint for endpoint in endpoints if endpoint.name == selected_name]

    if not name_matches:
        raise AudioOutputUnavailableError(
            f"selected output endpoint is not currently present: {selected_name!r}"
        )

    if selected_host_api:
        host_api_matches = [
            endpoint for endpoint in name_matches if endpoint.host_api == selected_host_api
        ]
        if not host_api_matches:
            raise AudioOutputUnavailableError(
                f"selected output endpoint {selected_name!r} is no longer present "
                f"under host API {selected_host_api!r}"
            )
        if len(host_api_matches) > 1:
            raise AudioOutputUnavailableError(
                f"{len(host_api_matches)} output endpoints share the selected "
                "name and host API; select a unique device explicitly"
            )
        return ensure_supported_output_endpoint(host_api_matches[0])

    return select_preferred_output_endpoint(name_matches)


def enumerate_output_endpoints() -> List[AudioEndpoint]:
    """Enumerate real Windows playback endpoints via ``sounddevice``/PortAudio.

    Windows-only in practice (PortAudio also runs on other platforms, but this
    project only ships a Windows client); guarded so importing this module
    never fails on a machine without the optional dependency installed.
    """

    return [
        endpoint
        for endpoint in _enumerate_endpoints("max_output_channels")
        if is_supported_output_host_api(endpoint.host_api)
    ]


def enumerate_input_endpoints() -> List[AudioEndpoint]:
    """Enumerate real Windows recording endpoints via ``sounddevice``/PortAudio
    (XRBM-031 In-scope item 11): needed only so the diagnostics page can
    confirm a VB-CABLE ``CABLE Output`` recording endpoint exists alongside
    the ``CABLE Input`` playback endpoint - this project's own voice path
    never reads from a recording endpoint itself (see
    ``resolve_selected_endpoint``, which is playback-only).
    """

    return _enumerate_endpoints("max_input_channels")


def _enumerate_endpoints(channel_count_key: str) -> List[AudioEndpoint]:
    try:
        import sounddevice as sd  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only on Windows
        raise AudioOutputUnavailableError(
            "the 'sounddevice' package is not installed; cannot enumerate "
            "audio endpoints"
        ) from exc

    try:
        host_apis = sd.query_hostapis()
        endpoints: List[AudioEndpoint] = []
        for device in sd.query_devices():
            if device.get(channel_count_key, 0) <= 0:
                continue
            host_api_name = host_apis[device["hostapi"]]["name"] if host_apis else ""
            endpoints.append(AudioEndpoint(name=device["name"], host_api=host_api_name))
        return endpoints
    except AudioOutputUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed on ANY real PortAudio/
        # sounddevice failure (a raw PortAudioError, an unexpected device
        # dict shape, etc.) rather than letting it propagate as a
        # differently-shaped exception callers/tests do not already handle
        # (XRBM-031 RETRY 1 item 2). The message is deliberately generic -
        # never str(exc) - since some PortAudio error strings can embed a
        # real device name/path, which this project's privacy contract
        # (config.py's FORBIDDEN_KEYS, logging_setup.py) forbids surfacing;
        # the original exception is still chained via ``from exc`` for a
        # developer inspecting a real traceback/log.
        raise AudioOutputUnavailableError(
            "PortAudio 音频端点查询失败"
        ) from exc


# Canonical VB-CABLE (Basic/Donationware) endpoint display names, as VB-Audio's
# own driver names them: the playback ("speaker") side the app writes decoded
# voice PCM to, and the recording ("microphone") side a recognizer reads from.
# Selection remains narrowly scoped: diagnostics may offer the detected Basic
# CABLE Input explicitly, and settings may preselect its preferred usable view
# when migrating an already-saved, unsupported WDM-KS endpoint. Nothing is
# persisted until the user saves and the real stream preflight succeeds.
CABLE_INPUT_NAME = "CABLE Input"
CABLE_OUTPUT_NAME = "CABLE Output"
DJI_MIC_2_NAME_PREFIXES = ("DJI-MIC2", "DJI Mic 2", "DJI Mic2")


def _matches_cable_endpoint(name: str, canonical: str) -> bool:
    """True only if ``name`` is exactly ``canonical``, or ``canonical``
    followed by a parenthesized host-API/driver decoration Windows/PortAudio
    commonly appends (e.g. ``"CABLE Input (VB-Audio Virtual Cable)"``) -
    never a bare substring match, which could also accept an unrelated
    device whose name merely contains ``canonical`` as a fragment of a
    longer, different name (e.g. a hypothetical "CABLE Input Splitter Pro").
    """

    name = name.strip()
    if name == canonical:
        return True
    decorated_prefix = canonical + " ("
    return name.startswith(decorated_prefix) and name.endswith(")")


def is_cable_input_endpoint(name: str) -> bool:
    """True if ``name`` names VB-CABLE's playback ("CABLE Input") endpoint."""

    return _matches_cable_endpoint(name, CABLE_INPUT_NAME)


def is_cable_output_endpoint(name: str) -> bool:
    """True if ``name`` names VB-CABLE's recording ("CABLE Output") endpoint."""

    return _matches_cable_endpoint(name, CABLE_OUTPUT_NAME)


def is_dji_mic_2_input_endpoint(name: str) -> bool:
    """Matches DJI Mic 2 system recording endpoints without depending on or
    exposing the per-device suffix Windows may append to the display name.
    Localized Windows names commonly wrap the product token, for example
    ``Microphone (DJI-MIC2-xxxx Hands-Free)``. Match a bounded token anywhere
    in the endpoint name while still rejecting names such as ``DJI-MIC20``.
    """

    value = name.strip().casefold()
    for raw_prefix in DJI_MIC_2_NAME_PREFIXES:
        prefix = raw_prefix.casefold()
        search_from = 0
        while True:
            index = value.find(prefix, search_from)
            if index < 0:
                break
            end = index + len(prefix)
            before_is_boundary = index == 0 or not value[index - 1].isalnum()
            after_is_boundary = end == len(value) or not value[end].isalnum()
            if before_is_boundary and after_is_boundary:
                return True
            search_from = index + 1
    return False
