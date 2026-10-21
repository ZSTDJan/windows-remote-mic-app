import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest import mock

from ovb_rc003 import application_update


class FakeResponse:
    def __init__(
        self,
        data: bytes,
        url: str,
        *,
        status: int = 200,
        content_length: int | None = None,
        fail_after: int | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        self._data = data
        self._url = url
        self._offset = 0
        self._fail_after = fail_after
        self._fail_with = fail_with
        self.status = status
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if self._fail_after is not None and self._offset >= self._fail_after:
            raise self._fail_with or OSError("simulated interrupted response")
        if size < 0:
            size = len(self._data) - self._offset
        end = min(len(self._data), self._offset + size)
        chunk = self._data[self._offset:end]
        self._offset = end
        return chunk

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        self.closed = True


class MappingOpener:
    def __init__(self, responses):
        self._responses = {
            key: list(value) if isinstance(value, tuple) else [value]
            for key, value in responses.items()
        }
        self.calls = []

    def __call__(self, request, *, timeout):
        url = request.full_url
        self.calls.append((url, timeout, dict(request.header_items())))
        response = self._responses[url].pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _asset(name: str, data: bytes, tag: str = "test") -> dict:
    return {
        "name": name,
        "size": len(data),
        "state": "uploaded",
        "browser_download_url": (
            f"https://github.com/{application_update.REPOSITORY_SLUG}/"
            f"releases/download/{tag}/{name}"
        ),
        "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
    }


def _release_payload(
    version: str,
    *,
    installer_data: bytes = b"installer",
    portable_data: bytes = b"portable",
    tag: str = "unrelated-tag",
    prerelease: bool | None = None,
    draft: bool = False,
    notes: str = "release notes",
) -> tuple[dict, bytes]:
    installer_name = f"RemoteMicRC003Setup-{version}-unsigned.exe"
    portable_name = f"RemoteMicRC003-{version}-portable-unsigned.zip"
    manifest = (
        f"{hashlib.sha256(portable_data).hexdigest()}  {portable_name}\n"
        f"{hashlib.sha256(installer_data).hexdigest()}  {installer_name}\n"
    ).encode("ascii")
    if prerelease is None:
        prerelease = "-candidate" in version
    release = {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "html_url": (
            f"https://github.com/{application_update.REPOSITORY_SLUG}/"
            f"releases/tag/{tag}"
        ),
        "body": notes,
        "assets": [
            _asset(portable_name, portable_data, tag),
            _asset(installer_name, installer_data, tag),
            _asset("SHA256SUMS.txt", manifest, tag),
        ],
    }
    return release, manifest


def _api_opener(payload) -> MappingOpener:
    data = json.dumps(payload).encode("utf-8")
    return MappingOpener(
        {
            application_update.RELEASES_API_URL: FakeResponse(
                data,
                application_update.RELEASES_API_URL,
                content_length=len(data),
            )
        }
    )


class UpdateDeliveryTests(unittest.TestCase):
    def test_daily_attempt_survives_restart_and_cache_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            cache = Path(root) / "updates"
            self.assertTrue(application_update.claim_daily_update_check(cache, today=date(2026, 9, 18)))
            application_update.cleanup_obsolete_update_downloads(cache, "1.0.44")
            self.assertFalse(application_update.claim_daily_update_check(cache, today=date(2026, 9, 18)))
            self.assertTrue(application_update.claim_daily_update_check(cache, today=date(2026, 9, 19)))

    def test_unwritable_stamp_skips_automatic_network_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            cache = Path(root) / "not-a-directory"
            cache.write_text("keep", encoding="utf-8")
            self.assertFalse(application_update.claim_daily_update_check(cache))
            self.assertEqual(cache.read_text(), "keep")

    def _download(self, root):
        payload, _ = _release_payload("1.0.45")
        release = application_update.check_for_update(
            "1.0.44", opener=_api_opener([payload])
        ).release
        cache = Path(root) / "cache"
        cache.mkdir()
        source = cache / release.portable.name
        source.write_bytes(b"portable")
        return application_update.ApplicationUpdateDownload(
            release, application_update.PackageKind.PORTABLE, source, False
        )

    def test_redirected_desktop_copy_reuse_and_conflicting_user_file(self):
        with tempfile.TemporaryDirectory() as root:
            download = self._download(root)
            desktop = Path(root) / "重定向桌面"
            desktop.mkdir()
            occupied = desktop / download.path.name
            occupied.write_bytes(b"user-owned")
            result = application_update.save_update_to_desktop(download, desktop)
            self.assertEqual(occupied.read_bytes(), b"user-owned")
            self.assertEqual(result.path.parent, desktop)
            self.assertIn(" (1)", result.path.name)
            self.assertEqual(result.path.read_bytes(), b"portable")
            reused = application_update.save_update_to_desktop(download, desktop)
            self.assertEqual(reused.path, result.path)
            self.assertTrue(reused.reused_existing_file)
            self.assertEqual(len(list(desktop.iterdir())), 2)

    def test_desktop_failure_and_cancel_keep_verified_cache_without_partial_package(self):
        with tempfile.TemporaryDirectory() as root:
            download = self._download(root)
            desktop = Path(root) / "desktop"
            with self.assertRaises(application_update.ApplicationUpdateError):
                application_update.save_update_to_desktop(download, desktop)
            desktop.mkdir()
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(application_update.ApplicationUpdateCancelled):
                application_update.save_update_to_desktop(download, desktop, cancel_event=cancel)
            with mock.patch.object(Path, "rename", side_effect=PermissionError("locked")):
                if application_update.os.name == "nt":
                    with self.assertRaises(application_update.ApplicationUpdateError):
                        application_update.save_update_to_desktop(download, desktop)
            self.assertEqual(download.path.read_bytes(), b"portable")
            self.assertEqual(list(desktop.iterdir()), [])

    def test_desktop_publish_race_does_not_overwrite_new_user_file(self):
        if application_update.os.name != "nt":
            self.skipTest("Windows exclusive rename behavior")
        with tempfile.TemporaryDirectory() as root:
            download = self._download(root)
            desktop = Path(root) / "desktop"
            desktop.mkdir()
            original_rename = Path.rename
            def race(source, target):
                if target.name == download.path.name:
                    target.write_bytes(b"arrived during copy")
                return original_rename(source, target)
            with mock.patch.object(Path, "rename", race):
                result = application_update.save_update_to_desktop(download, desktop)
            self.assertEqual((desktop / download.path.name).read_bytes(), b"arrived during copy")
            self.assertEqual(result.path.read_bytes(), b"portable")
            self.assertNotEqual(result.path.name, download.path.name)


class ApplicationVersionTests(unittest.TestCase):
    def test_project_candidate_to_stable_sequence_never_goes_backwards(self):
        versions = [
            "0.2.0-candidate.30", "0.2.0-candidate.31", "1.0.31",
            "1.0.32-candidate.32", "1.0.32", "1.1.33-candidate.33", "1.1.33",
        ]
        parsed = [application_update.parse_application_version(v) for v in versions]
        self.assertEqual(parsed, sorted(parsed))

    def test_candidate_numbers_use_numeric_ordering(self):
        candidate_9 = application_update.parse_application_version(
            "0.2.0-candidate.9"
        )
        candidate_10 = application_update.parse_application_version(
            "0.2.0-candidate.10"
        )
        candidate_15 = application_update.parse_application_version(
            "0.2.0-candidate.15"
        )
        stable = application_update.parse_application_version("0.2.0")

        self.assertLess(candidate_9, candidate_10)
        self.assertLess(candidate_10, candidate_15)
        self.assertLess(candidate_15, stable)

    def test_candidate_without_number_matches_candidate_zero(self):
        self.assertEqual(
            application_update.parse_application_version("0.1.0-candidate"),
            application_update.parse_application_version("0.1.0-candidate.0"),
        )

    def test_invalid_or_ambiguous_versions_are_rejected(self):
        for value in (
            "v0.2.0",
            "0.2",
            "0.2.0-rc.1",
            "0.2.0-candidate.x",
            "00.2.0",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                application_update.parse_application_version(value)


class RepositoryContractTests(unittest.TestCase):
    def test_update_source_is_the_current_public_repository(self):
        self.assertEqual(
            application_update.REPOSITORY_SLUG,
            "ZSTDJan/windows-remote-mic-app",
        )
        self.assertEqual(
            application_update.RELEASES_PAGE_URL,
            "https://github.com/ZSTDJan/windows-remote-mic-app/releases",
        )
        self.assertEqual(
            application_update.RELEASES_API_URL,
            "https://api.github.com/repos/ZSTDJan/windows-remote-mic-app/"
            "releases?per_page=30",
        )

    def test_release_assets_from_another_repository_are_rejected(self):
        release, _ = _release_payload("0.2.0-candidate.2")
        release["assets"][0]["browser_download_url"] = release["assets"][0][
            "browser_download_url"
        ].replace("/ZSTDJan/", "/miaomiaozii/")

        with self.assertRaises(application_update.ApplicationUpdateError) as caught:
            application_update.check_for_update(
                "0.2.0-candidate.1", opener=_api_opener([release])
            )

        self.assertEqual(caught.exception.code, "invalid_release")


class UpdateCheckTests(unittest.TestCase):
    def test_selects_internal_asset_version_and_ignores_tag_number(self):
        old_release, _ = _release_payload(
            "0.2.0-candidate.9", tag="v99-unrelated"
        )
        new_release, _ = _release_payload(
            "0.2.0-candidate.10", tag="v1-unrelated"
        )
        qt_source_release = {
            "draft": False,
            "prerelease": True,
            "html_url": (
                f"https://github.com/{application_update.REPOSITORY_SLUG}/"
                "releases/tag/third-party-source"
            ),
            "body": "source",
            "assets": [_asset("qt-everywhere-src.tar.xz", b"source")],
        }

        result = application_update.check_for_update(
            "0.2.0-candidate.9",
            opener=_api_opener([qt_source_release, old_release, new_release]),
        )

        self.assertIs(
            result.outcome, application_update.UpdateCheckOutcome.UPDATE_AVAILABLE
        )
        self.assertEqual(result.release.version.text, "0.2.0-candidate.10")
        self.assertEqual(result.release.release_url.rsplit("/", 1)[-1], "v1-unrelated")

    def test_reports_current_and_local_newer_without_downgrading(self):
        release, _ = _release_payload("0.2.0-candidate.10")
        current = application_update.check_for_update(
            "0.2.0-candidate.10", opener=_api_opener([release])
        )
        newer = application_update.check_for_update(
            "0.2.0-candidate.15", opener=_api_opener([release])
        )

        self.assertIs(current.outcome, application_update.UpdateCheckOutcome.CURRENT)
        self.assertIs(
            newer.outcome, application_update.UpdateCheckOutcome.LOCAL_NEWER
        )

    def test_stable_build_does_not_offer_candidate_channel(self):
        candidate, _ = _release_payload("0.3.0-candidate.1")
        stable, _ = _release_payload("0.2.1", prerelease=False)

        result = application_update.check_for_update(
            "0.2.0", opener=_api_opener([candidate, stable])
        )

        self.assertEqual(result.release.version.text, "0.2.1")

    def test_draft_and_incomplete_product_release_do_not_pass(self):
        draft, _ = _release_payload("0.3.0-candidate.1", draft=True)
        incomplete, _ = _release_payload("0.2.1-candidate.1")
        incomplete["assets"] = incomplete["assets"][:1]

        with self.assertRaises(application_update.ApplicationUpdateError) as caught:
            application_update.check_for_update(
                "0.2.0-candidate.1",
                opener=_api_opener([draft, incomplete]),
            )

        self.assertEqual(caught.exception.code, "invalid_release")

    def test_incomplete_newest_release_never_falls_back_to_an_older_package(self):
        older, _ = _release_payload("0.2.0-candidate.15", tag="older")
        newest, _ = _release_payload("0.2.0-candidate.16", tag="newest")
        newest["assets"] = newest["assets"][:1]

        for current_version in (
            "0.2.0-candidate.14",
            "0.2.0-candidate.15",
            "0.2.0-candidate.16",
        ):
            with self.subTest(current_version=current_version), self.assertRaises(
                application_update.ApplicationUpdateError
            ) as caught:
                application_update.check_for_update(
                    current_version,
                    opener=_api_opener([older, newest]),
                )

            self.assertEqual(caught.exception.code, "invalid_release")

    def test_older_incomplete_release_does_not_block_a_newer_complete_package(self):
        older, _ = _release_payload("0.2.0-candidate.15", tag="older")
        older["assets"] = older["assets"][:1]
        newest, _ = _release_payload("0.2.0-candidate.16", tag="newest")

        result = application_update.check_for_update(
            "0.2.0-candidate.14",
            opener=_api_opener([older, newest]),
        )

        self.assertEqual(result.release.version.text, "0.2.0-candidate.16")

    def test_asset_upload_state_is_required_and_must_be_complete(self):
        for state in (None, "new"):
            release, _ = _release_payload("0.2.0-candidate.2")
            if state is None:
                release["assets"][0].pop("state")
            else:
                release["assets"][0]["state"] = state

            with self.subTest(state=state), self.assertRaises(
                application_update.ApplicationUpdateError
            ) as caught:
                application_update.check_for_update(
                    "0.2.0-candidate.1", opener=_api_opener([release])
                )

            self.assertEqual(caught.exception.code, "invalid_release")

    def test_duplicate_internal_versions_are_rejected(self):
        first, _ = _release_payload("0.2.0-candidate.2", tag="first")
        second, _ = _release_payload("0.2.0-candidate.2", tag="second")

        with self.assertRaises(application_update.ApplicationUpdateError) as caught:
            application_update.check_for_update(
                "0.2.0-candidate.1", opener=_api_opener([first, second])
            )

        self.assertEqual(caught.exception.code, "duplicate_release")

    def test_http_rate_limits_are_reported_and_closed(self):
        for status in (403, 429):
            error = urllib.error.HTTPError(
                application_update.RELEASES_API_URL,
                status,
                "rate limited",
                {},
                None,
            )
            opener = MappingOpener({application_update.RELEASES_API_URL: error})

            with self.subTest(status=status), self.assertRaises(
                application_update.ApplicationUpdateError
            ) as caught:
                application_update.check_for_update(
                    "0.2.0-candidate.1", opener=opener
                )

            self.assertEqual(caught.exception.code, "rate_limited")
            self.assertIn("稍后", str(caught.exception))
            self.assertTrue(error.closed)

    def test_timeout_is_sanitized_and_custom_limit_is_forwarded(self):
        opener = MappingOpener(
            {application_update.RELEASES_API_URL: TimeoutError("timed out")}
        )

        with self.assertRaises(application_update.ApplicationUpdateError) as caught:
            application_update.check_for_update(
                "0.2.0-candidate.1", opener=opener, timeout=2.5
            )

        self.assertEqual(caught.exception.code, "timeout")
        self.assertIn("超时", str(caught.exception))
        self.assertEqual(opener.calls[0][1], 2.5)

    def test_timeout_while_reading_api_response_closes_response(self):
        response = FakeResponse(
            b"[]",
            application_update.RELEASES_API_URL,
            fail_after=0,
            fail_with=TimeoutError("timed out while reading"),
        )
        opener = MappingOpener(
            {application_update.RELEASES_API_URL: response}
        )

        with self.assertRaises(application_update.ApplicationUpdateError) as caught:
            application_update.check_for_update(
                "0.2.0-candidate.1", opener=opener
            )

        self.assertEqual(caught.exception.code, "timeout")
        self.assertTrue(response.closed)

    def test_oversized_or_malformed_json_fails_closed(self):
        oversized = MappingOpener(
            {
                application_update.RELEASES_API_URL: FakeResponse(
                    b"[]",
                    application_update.RELEASES_API_URL,
                    content_length=application_update.MAX_RELEASES_RESPONSE_BYTES + 1,
                )
            }
        )
        with self.assertRaises(application_update.ApplicationUpdateError) as caught:
            application_update.check_for_update("0.2.0-candidate.1", opener=oversized)
        self.assertEqual(caught.exception.code, "response_too_large")

        malformed = MappingOpener(
            {
                application_update.RELEASES_API_URL: FakeResponse(
                    b"not json", application_update.RELEASES_API_URL
                )
            }
        )
        with self.assertRaises(application_update.ApplicationUpdateError) as caught:
            application_update.check_for_update("0.2.0-candidate.1", opener=malformed)
        self.assertEqual(caught.exception.code, "invalid_response")

    def test_untrusted_final_redirect_host_is_rejected(self):
        data = b"[]"
        opener = MappingOpener(
            {
                application_update.RELEASES_API_URL: FakeResponse(
                    data, "https://example.invalid/releases", content_length=len(data)
                )
            }
        )

        with self.assertRaises(application_update.ApplicationUpdateError) as caught:
            application_update.check_for_update("0.2.0-candidate.1", opener=opener)

        self.assertEqual(caught.exception.code, "unsafe_url")


class ReleasePaginationTests(unittest.TestCase):
    def page_url(self, page, *, numeric=False):
        path = "/repositories/12345/releases" if numeric else f"/repos/{application_update.REPOSITORY_SLUG}/releases"
        return f"https://api.github.com{path}?per_page=30&page={page}"

    def response(self, items, url, *, next_url=None):
        response = FakeResponse(json.dumps(items).encode("utf-8"), url)
        if next_url is not None:
            response.headers["Link"] = f'<{next_url}>; rel="next"'
        return response

    def two_pages(self, first, second, *, numeric=False):
        first_url = application_update.RELEASES_API_URL
        second_url = self.page_url(2, numeric=numeric)
        responses = [self.response(first, first_url, next_url=second_url),
                     self.response(second, second_url)]
        return MappingOpener(dict(zip((first_url, second_url), responses))), responses

    def test_formal_release_hidden_behind_30_candidates_is_found(self):
        candidates = [_release_payload(f"1.0.87-candidate.{i}")[0] for i in range(1, 31)]
        formal, _ = _release_payload("1.0.86")
        opener, responses = self.two_pages(candidates, [formal], numeric=True)
        result = application_update.check_for_update("1.0.72", opener=opener)
        self.assertEqual(result.release.version.text, "1.0.86")
        self.assertEqual(result.outcome, application_update.UpdateCheckOutcome.UPDATE_AVAILABLE)
        self.assertEqual(len(opener.calls), 2)
        self.assertTrue(all(response.closed for response in responses))

    def test_candidate_comparison_includes_the_later_page(self):
        old, _ = _release_payload("1.0.87-candidate.1")
        new, _ = _release_payload("1.0.87-candidate.2")
        opener, _ = self.two_pages([old], [new])
        result = application_update.check_for_update("1.0.87-candidate.1", opener=opener)
        self.assertEqual(result.release.version.text, "1.0.87-candidate.2")

    def test_incomplete_newest_release_on_later_page_is_not_hidden(self):
        old, _ = _release_payload("1.0.85")
        new, _ = _release_payload("1.0.86")
        new["assets"] = new["assets"][:1]
        opener, responses = self.two_pages([old], [new])
        with self.assertRaises(application_update.ApplicationUpdateError) as caught:
            application_update.check_for_update("1.0.85", opener=opener)
        self.assertEqual(caught.exception.code, "invalid_release")
        self.assertTrue(all(response.closed for response in responses))

    def test_shifted_release_id_is_merged_but_distinct_duplicate_versions_are_rejected(self):
        release, _ = _release_payload("1.0.86")
        release["id"] = 123
        for second_id in (123, 124):
            with self.subTest(second_id=second_id):
                shifted = dict(release, id=second_id, body="latest notes")
                opener, _ = self.two_pages([release], [shifted])
                if second_id == 123:
                    result = application_update.check_for_update("1.0.86", opener=opener)
                    self.assertEqual(result.outcome, application_update.UpdateCheckOutcome.CURRENT)
                    self.assertEqual(result.release.notes, "latest notes")
                else:
                    with self.assertRaises(application_update.ApplicationUpdateError) as caught:
                        application_update.check_for_update("1.0.86", opener=opener)
                    self.assertEqual(caught.exception.code, "duplicate_release")

    def test_unsafe_cross_repository_and_nonsequential_next_links_are_rejected(self):
        unsafe_links = (
            "https://example.invalid/releases?page=2",
            "http://api.github.com/repos/ZSTDJan/windows-remote-mic-app/releases?page=2",
            "https://api.github.com/repos/other/other/releases?page=2",
            self.page_url(1), self.page_url(3),
            self.page_url(2) + "&page=2", self.page_url(2) + "&token=secret",
            self.page_url(2).replace("per_page=30", "per_page=100"),
            self.page_url(2) + "#fragment",
        )
        for url in unsafe_links:
            with self.subTest(url=url):
                response = self.response([], application_update.RELEASES_API_URL, next_url=url)
                opener = MappingOpener({application_update.RELEASES_API_URL: response})
                with self.assertRaises(application_update.ApplicationUpdateError) as caught:
                    application_update.check_for_update("1.0.86", opener=opener)
                self.assertIn(caught.exception.code, ("invalid_response", "unsafe_url"))
                self.assertEqual(len(opener.calls), 1)
                self.assertTrue(response.closed)

    def test_numeric_repository_identity_cannot_change_on_a_later_page(self):
        first = self.response([], application_update.RELEASES_API_URL,
                              next_url=self.page_url(2, numeric=True))
        second_url = self.page_url(2, numeric=True)
        second = self.response([], second_url, next_url=self.page_url(3, numeric=True).replace("12345", "999"))
        opener = MappingOpener({application_update.RELEASES_API_URL: first, second_url: second})
        with self.assertRaises(application_update.ApplicationUpdateError) as caught:
            application_update.check_for_update("1.0.86", opener=opener)
        self.assertEqual(caught.exception.code, "invalid_response")
        self.assertTrue(first.closed and second.closed)

    def test_page_limit_never_reports_a_truncated_list_as_current(self):
        release, _ = _release_payload("1.0.86")
        responses = {}
        for page in range(1, application_update.MAX_RELEASE_PAGES + 1):
            url = application_update.RELEASES_API_URL if page == 1 else self.page_url(page)
            responses[url] = self.response([release] if page == 1 else [], url,
                                           next_url=self.page_url(page + 1))
        opener = MappingOpener(responses)
        with self.assertRaises(application_update.ApplicationUpdateError) as caught:
            application_update.check_for_update("1.0.86", opener=opener)
        self.assertEqual(caught.exception.code, "invalid_response")
        self.assertEqual(len(opener.calls), application_update.MAX_RELEASE_PAGES)
        self.assertTrue(all(response.closed for response in responses.values()))

    def test_all_pages_share_the_response_size_budget(self):
        opener, responses = self.two_pages([], [])
        responses[0]._data = b"[ ] "
        responses[1]._data = b"[ ] "
        with mock.patch.object(application_update, "MAX_RELEASES_RESPONSE_BYTES", 6):
            with self.assertRaises(application_update.ApplicationUpdateError) as caught:
                application_update.check_for_update("1.0.86", opener=opener)
        self.assertEqual(caught.exception.code, "response_too_large")
        self.assertTrue(all(response.closed for response in responses))

    def test_later_requests_use_the_remaining_total_timeout(self):
        release, _ = _release_payload("1.0.86")
        opener, responses = self.two_pages([], [release])
        clock = [0.0]
        original = responses[0].read
        def delayed_read(size):
            clock[0] += 0.4
            return original(size)
        responses[0].read = delayed_read
        with mock.patch.object(application_update.time, "monotonic", side_effect=lambda: clock[0]):
            result = application_update.check_for_update("1.0.86", opener=opener, timeout=2.5)
        self.assertEqual(result.outcome, application_update.UpdateCheckOutcome.CURRENT)
        self.assertEqual(opener.calls[0][1], 2.5)
        self.assertAlmostEqual(opener.calls[1][1], 1.7)

    def test_total_timeout_during_read_is_reported_and_closed(self):
        opener, responses = self.two_pages([], [])
        clock = [0.0]
        original = responses[0].read
        def delayed_read(size):
            clock[0] += 1.1
            return original(size)
        responses[0].read = delayed_read
        with mock.patch.object(application_update.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(application_update.ApplicationUpdateError) as caught:
                application_update.check_for_update("1.0.86", opener=opener, timeout=1.0)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertTrue(responses[0].closed)
        self.assertEqual(len(opener.calls), 1)

    def test_malformed_later_page_and_multiple_next_links_fail_closed(self):
        opener, responses = self.two_pages([], [])
        responses[1]._data = b"not JSON"
        with self.assertRaises(application_update.ApplicationUpdateError) as caught:
            application_update.check_for_update("1.0.86", opener=opener)
        self.assertEqual(caught.exception.code, "invalid_response")
        self.assertTrue(all(response.closed for response in responses))
        for link in ("broken", f'<{self.page_url(2)}>; rel="next", <{self.page_url(2)}>; rel="next"'):
            response = self.response([], application_update.RELEASES_API_URL)
            response.headers['Link'] = link
            with self.subTest(link=link), self.assertRaises(application_update.ApplicationUpdateError):
                application_update.check_for_update("1.0.86", opener=MappingOpener({application_update.RELEASES_API_URL: response}))
            self.assertTrue(response.closed)


class PortableOnlyReleaseTests(unittest.TestCase):
    def _payload(self, version="0.2.0-candidate.31", *, chinese=True, bom=False):
        payload, _ = _release_payload(version, tag=f"v{version}")
        name = (
            f"无线麦-Windows-{version}-免安装.zip" if chinese
            else f"RemoteMicRC003-{version}-portable-unsigned.zip"
        )
        data = b"portable package bytes"
        manifest = f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode(
            "utf-8-sig" if bom else "utf-8"
        )
        package = _asset(name, data, payload["tag_name"])
        package["browser_download_url"] = urllib.parse.quote(
            package["browser_download_url"], safe=":/"
        )
        payload["assets"] = [package, _asset("SHA256SUMS.txt", manifest, payload["tag_name"])]
        return payload, data, manifest

    def test_chinese_and_legacy_portable_downloads_work_without_installer(self):
        for chinese, bom in ((True, False), (True, True), (False, False)):
            with self.subTest(chinese=chinese, bom=bom):
                payload, data, manifest = self._payload(chinese=chinese, bom=bom)
                check = application_update.check_for_update(
                    "0.2.0-candidate.30", opener=_api_opener([payload])
                )
                release = check.release
                self.assertIsNone(release.installer)
                self.assertEqual(check.outcome, application_update.UpdateCheckOutcome.UPDATE_AVAILABLE)
                opener = MappingOpener({
                    release.manifest.download_url: FakeResponse(manifest, release.manifest.download_url),
                    release.portable.download_url: FakeResponse(data, release.portable.download_url),
                })
                with tempfile.TemporaryDirectory() as directory:
                    result = application_update.download_update_package(
                        release, "installer", Path(directory), opener=opener
                    )
                    self.assertEqual(result.package_kind, application_update.PackageKind.PORTABLE)
                    self.assertEqual(result.path.name, payload["assets"][0]["name"])
                    self.assertEqual(result.path.read_bytes(), data)
                    self.assertEqual(list(Path(directory).iterdir()), [result.path])

    def test_new_chinese_release_missing_manifest_blocks_fallback(self):
        old, _ = _release_payload("0.2.0-candidate.30")
        newest, _, _ = self._payload()
        newest["assets"].pop()
        with self.assertRaises(application_update.ApplicationUpdateError) as caught:
            application_update.check_for_update("0.2.0-candidate.29", opener=_api_opener([old, newest]))
        self.assertEqual(caught.exception.code, "invalid_release")

    def test_manifest_cannot_include_an_unpublished_installer(self):
        payload, _, manifest = self._payload()
        release = application_update._select_release(
            [payload], application_update.parse_application_version("0.2.0-candidate.30")
        )
        manifest += ("a" * 64 + "  old-installer.exe\n").encode()
        with self.assertRaises(application_update.ApplicationUpdateError):
            application_update._parse_manifest(manifest, release)

    def test_duplicate_new_and_old_zip_names_are_rejected(self):
        payload, _, _ = self._payload()
        legacy, _ = _release_payload("0.2.0-candidate.31")
        payload["assets"].append(legacy["assets"][0])
        with self.assertRaises(application_update.ApplicationUpdateError):
            application_update.check_for_update("0.2.0-candidate.30", opener=_api_opener([payload]))

    def test_cleanup_only_removes_known_chinese_cache_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "0.2.0-candidate.30"
            old.mkdir()
            (old / "无线麦-Windows-0.2.0-candidate.30-免安装.zip.part").write_bytes(b"partial")
            foreign = root / "0.2.0-candidate.29"
            foreign.mkdir()
            (foreign / "个人文件.zip").write_bytes(b"keep")
            self.assertEqual(application_update.cleanup_obsolete_update_downloads(root, "0.2.0-candidate.31"), 1)
            self.assertFalse(old.exists())
            self.assertTrue((foreign / "个人文件.zip").exists())


class UpdateDownloadTests(unittest.TestCase):
    def setUp(self):
        self.retry_wait = mock.patch.object(application_update, "_wait_download_retry")
        self.retry_wait.start()
        self.addCleanup(self.retry_wait.stop)

    def _release_and_opener(self, *, package_data: bytes = b"P" * 700_000):
        release_payload, manifest = _release_payload(
            "0.2.0-candidate.2", installer_data=package_data
        )
        release = application_update._select_release(
            [release_payload],
            application_update.parse_application_version("0.2.0-candidate.1"),
        )
        opener = MappingOpener(
            {
                release.manifest.download_url: FakeResponse(
                    manifest,
                    "https://release-assets.githubusercontent.com/manifest",
                    content_length=len(manifest),
                ),
                release.installer.download_url: FakeResponse(
                    package_data,
                    "https://release-assets.githubusercontent.com/installer",
                    content_length=len(package_data),
                ),
            }
        )
        return release, opener, package_data, manifest

    def test_downloads_to_part_then_verifies_and_renames(self):
        release, opener, package_data, _manifest = self._release_and_opener()
        progress = []
        with tempfile.TemporaryDirectory() as tmpdir:
            result = application_update.download_update_package(
                release,
                application_update.PackageKind.INSTALLER,
                Path(tmpdir),
                opener=opener,
                progress_callback=lambda received, total: progress.append(
                    (received, total)
                ),
            )

            self.assertEqual(result.path.read_bytes(), package_data)
            self.assertFalse(Path(str(result.path) + ".part").exists())
            self.assertFalse(result.reused_existing_file)
        self.assertEqual(progress[0][0], 0)
        self.assertEqual(progress[-1], (len(package_data), len(package_data)))

    def test_matching_existing_file_is_reused_without_package_request(self):
        release, opener, package_data, _manifest = self._release_and_opener()
        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir)
            final_path = destination / release.installer.name
            final_path.write_bytes(package_data)

            result = application_update.download_update_package(
                release,
                "installer",
                destination,
                opener=opener,
            )

        self.assertTrue(result.reused_existing_file)
        self.assertEqual(
            [call[0] for call in opener.calls], [release.manifest.download_url]
        )

    def test_checksum_failure_deletes_part_and_keeps_no_final_file(self):
        release, opener, package_data, _manifest = self._release_and_opener()
        bad_data = b"X" * len(package_data)
        opener._responses[release.installer.download_url] = [
            FakeResponse(
                bad_data,
                "https://release-assets.githubusercontent.com/installer",
                content_length=len(bad_data),
            )
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir)
            with self.assertRaises(application_update.ApplicationUpdateError) as caught:
                application_update.download_update_package(
                    release, "installer", destination, opener=opener
                )

            self.assertEqual(caught.exception.code, "checksum_mismatch")
            self.assertFalse((destination / release.installer.name).exists())
            self.assertFalse((destination / f"{release.installer.name}.part").exists())

    def test_interrupted_download_preserves_progress_after_bounded_retries(self):
        release, opener, package_data, _manifest = self._release_and_opener()
        opener._responses[release.installer.download_url] = [
            FakeResponse(
                package_data,
                "https://release-assets.githubusercontent.com/installer",
                content_length=len(package_data),
                fail_after=application_update.DOWNLOAD_CHUNK_BYTES,
            ), OSError("offline"), OSError("offline"), OSError("offline"), OSError("offline")
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir)
            with self.assertRaises(application_update.ApplicationUpdateError):
                application_update.download_update_package(
                    release, "installer", destination, opener=opener
                )

            part = destination / f"{release.installer.name}.part"
            self.assertEqual(part.read_bytes(), package_data[:application_update.DOWNLOAD_CHUNK_BYTES])
            self.assertFalse((destination / release.installer.name).exists())
            self.assertEqual(sum(call[0] == release.installer.download_url for call in opener.calls), 5)

    def test_timeout_closes_response_and_next_click_resumes(self):
        release, opener, package_data, _manifest = self._release_and_opener()
        response = FakeResponse(
            package_data,
            "https://release-assets.githubusercontent.com/installer",
            content_length=len(package_data),
            fail_after=application_update.DOWNLOAD_CHUNK_BYTES,
            fail_with=TimeoutError("timed out while downloading"),
        )
        opener._responses[release.installer.download_url] = [response] + [TimeoutError() for _ in range(4)]

        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir)
            with self.assertRaises(application_update.ApplicationUpdateError) as caught:
                application_update.download_update_package(
                    release, "installer", destination, opener=opener
                )

            self.assertEqual(caught.exception.code, "timeout")
            self.assertTrue(response.closed)
            self.assertFalse((destination / release.installer.name).exists())
            offset = application_update.DOWNLOAD_CHUNK_BYTES
            self.assertEqual((destination / f"{release.installer.name}.part").stat().st_size, offset)
            resumed = self._resumed_response(release, package_data, offset)
            opener._responses[release.installer.download_url] = [resumed]
            opener._responses[release.manifest.download_url] = [FakeResponse(
                _manifest, release.manifest.download_url, content_length=len(_manifest))]
            result = application_update.download_update_package(
                release, "installer", destination, opener=opener)
            self.assertEqual(result.path.read_bytes(), package_data)
            self.assertEqual(opener.calls[-1][2]["Range"], f"bytes={offset}-")
            self.assertFalse((destination / f"{release.installer.name}.part.json").exists())

    @staticmethod
    def _resumed_response(release, data, offset):
        response = FakeResponse(data[offset:], release.installer.download_url,
                                status=206, content_length=len(data) - offset)
        response.headers["Content-Range"] = f"bytes {offset}-{len(data)-1}/{len(data)}"
        return response

    def test_network_interruption_automatically_resumes_and_verifies_all_bytes(self):
        release, opener, data, _ = self._release_and_opener()
        offset = application_update.DOWNLOAD_CHUNK_BYTES
        failed = FakeResponse(data, release.installer.download_url, content_length=len(data),
                              fail_after=offset, fail_with=TimeoutError())
        resumed = self._resumed_response(release, data, offset)
        opener._responses[release.installer.download_url] = [failed, resumed]
        progress = []
        with tempfile.TemporaryDirectory() as root:
            result = application_update.download_update_package(release, "installer", Path(root),
                opener=opener, progress_callback=lambda value, total: progress.append(value))
            self.assertEqual(result.path.read_bytes(), data)
            self.assertFalse(list(Path(root).glob("*.part*")))
        self.assertTrue(failed.closed and resumed.closed)
        self.assertEqual(opener.calls[-1][2]["Range"], f"bytes={offset}-")
        self.assertEqual(progress[-1], len(data))
        self.assertEqual([v for v in progress if v == 0], [0])

    def test_server_ignoring_range_restarts_instead_of_appending(self):
        release, opener, data, _ = self._release_and_opener()
        opener._responses[release.installer.download_url] = [
            FakeResponse(data, release.installer.download_url, fail_after=application_update.DOWNLOAD_CHUNK_BYTES),
            FakeResponse(data, release.installer.download_url, content_length=len(data))]
        with tempfile.TemporaryDirectory() as root:
            result = application_update.download_update_package(release, "installer", Path(root), opener=opener)
            self.assertEqual(result.path.read_bytes(), data)

    def test_invalid_range_or_changed_manifest_never_publishes_mixed_bytes(self):
        for scenario in ("wrong_start", "wrong_total", "missing_range", "bad_length", "corrupt_prefix", "old_manifest"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as root:
                release, opener, data, _ = self._release_and_opener()
                path = Path(root) / f"{release.installer.name}.part"
                metadata = Path(str(path) + ".json")
                offset = application_update.DOWNLOAD_CHUNK_BYTES
                path.write_bytes((b"X" * offset) if scenario == "corrupt_prefix" else data[:offset])
                metadata.write_text(json.dumps({"size": len(data), "sha256":
                    "0" * 64 if scenario == "old_manifest" else hashlib.sha256(data).hexdigest()}))
                response = self._resumed_response(release, data, offset)
                if scenario == "wrong_start": response.headers["Content-Range"] = f"bytes 0-{len(data)-1}/{len(data)}"
                if scenario == "wrong_total": response.headers["Content-Range"] = f"bytes {offset}-{len(data)-1}/{len(data)+1}"
                if scenario == "missing_range": response.headers.pop("Content-Range")
                if scenario == "bad_length": response.headers["Content-Length"] = "1"
                if scenario == "old_manifest": response = FakeResponse(data, release.installer.download_url, content_length=len(data))
                opener._responses[release.installer.download_url] = [response]
                if scenario == "old_manifest":
                    result = application_update.download_update_package(release, "installer", Path(root), opener=opener)
                    self.assertEqual(result.path.read_bytes(), data)
                    self.assertNotIn("Range", opener.calls[-1][2])
                else:
                    with self.assertRaises(application_update.ApplicationUpdateError):
                        application_update.download_update_package(release, "installer", Path(root), opener=opener)
                    self.assertFalse((Path(root) / release.installer.name).exists())
                self.assertFalse(path.exists() or metadata.exists())

    def test_incomplete_response_resumes_and_download_timeout_is_separate(self):
        release, opener, data, _ = self._release_and_opener()
        offset = application_update.DOWNLOAD_READ_BYTES
        opener._responses[release.installer.download_url] = [
            FakeResponse(data[:offset], release.installer.download_url, content_length=len(data)),
            self._resumed_response(release, data, offset)]
        with tempfile.TemporaryDirectory() as root:
            result = application_update.download_update_package(release, "installer", Path(root), opener=opener)
            self.assertEqual(result.path.read_bytes(), data)
        self.assertTrue(all(call[1] == 30 for call in opener.calls))
        self.assertEqual(application_update.NETWORK_TIMEOUT_SECONDS, 10)

    def test_fast_reads_do_not_flood_progress_but_always_report_completion(self):
        release, opener, data, _ = self._release_and_opener()
        progress = []
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
                application_update.time, "monotonic", return_value=1.0):
            result = application_update.download_update_package(release, "installer", Path(root),
                opener=opener, progress_callback=lambda value, total: progress.append(value))
            self.assertEqual(result.path.read_bytes(), data)
        self.assertEqual(progress, [0, len(data)])

    def test_nonretryable_http_error_is_not_repeated(self):
        release, opener, _, _ = self._release_and_opener()
        error = urllib.error.HTTPError(release.installer.download_url, 403, "forbidden", {}, None)
        opener._responses[release.installer.download_url] = [error]
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(application_update.ApplicationUpdateError) as caught:
                application_update.download_update_package(release, "installer", Path(root), opener=opener)
            self.assertEqual(caught.exception.code, "rate_limited")
            self.assertEqual(list(Path(root).iterdir()), [])
        self.assertEqual(len(opener.calls), 2)

    def test_manifest_network_failure_retries_before_starting_package(self):
        release, opener, data, manifest = self._release_and_opener()
        opener._responses[release.manifest.download_url] = [TimeoutError(),
            FakeResponse(manifest, release.manifest.download_url, content_length=len(manifest))]
        with tempfile.TemporaryDirectory() as root:
            result = application_update.download_update_package(release, "installer", Path(root), opener=opener)
            self.assertEqual(result.path.read_bytes(), data)
        self.assertEqual([call[0] for call in opener.calls],
                         [release.manifest.download_url] * 2 + [release.installer.download_url])

    def test_manifest_and_package_can_succeed_on_fifth_attempt(self):
        release, opener, data, manifest = self._release_and_opener()
        for url, body in ((release.manifest.download_url, manifest),
                          (release.installer.download_url, data)):
            opener._responses[url] = [TimeoutError() for _ in range(4)] + [
                FakeResponse(body, url, content_length=len(body))]
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
                application_update, "_wait_download_retry") as wait:
            result = application_update.download_update_package(release, "installer", Path(root), opener=opener)
            self.assertEqual(result.path.read_bytes(), data)
        self.assertEqual([call[0] for call in opener.calls],
                         [release.manifest.download_url] * 5 + [release.installer.download_url] * 5)
        self.assertEqual(wait.call_args_list, [mock.call(i, None) for i in range(4)] * 2)

    def test_manifest_failure_stops_after_five_attempts_without_package_request(self):
        release, opener, _, _ = self._release_and_opener()
        opener._responses[release.manifest.download_url] = [TimeoutError() for _ in range(5)]
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(application_update.ApplicationUpdateError) as caught:
                application_update.download_update_package(release, "installer", Path(root), opener=opener)
            self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual([call[0] for call in opener.calls], [release.manifest.download_url] * 5)

    def test_complete_prefix_is_reverified_without_requesting_asset(self):
        release, opener, data, _ = self._release_and_opener()
        progress = []
        with tempfile.TemporaryDirectory() as root:
            part = Path(root) / (release.installer.name + ".part")
            part.write_bytes(data)
            Path(str(part) + ".json").write_text(json.dumps(
                {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}))
            result = application_update.download_update_package(release, "installer", Path(root), opener=opener,
                progress_callback=lambda received, total: progress.append(received))
            self.assertEqual(result.path.read_bytes(), data)
            self.assertFalse(list(Path(root).glob("*.part*")))
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(progress, [len(data)])

    def test_cancel_during_retry_wait_stops_before_another_request(self):
        release, opener, _, _ = self._release_and_opener()
        opener._responses[release.installer.download_url] = [TimeoutError()]
        cancel = threading.Event()
        def cancel_wait(*_):
            cancel.set()
            application_update._check_download_cancelled(cancel)
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
                application_update, "_wait_download_retry", side_effect=cancel_wait):
            with self.assertRaises(application_update.ApplicationUpdateCancelled):
                application_update.download_update_package(release, "installer", Path(root),
                    opener=opener, cancel_event=cancel)
            self.assertEqual(list(Path(root).iterdir()), [])
        self.assertEqual(len(opener.calls), 2)

    def test_cancel_during_download_cleans_part_file(self):
        release, opener, _package_data, _manifest = self._release_and_opener()
        cancel_event = threading.Event()

        def cancel_after_first_chunk(received, _total):
            if received > 0:
                cancel_event.set()

        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir)
            with self.assertRaises(application_update.ApplicationUpdateCancelled):
                application_update.download_update_package(
                    release,
                    "installer",
                    destination,
                    opener=opener,
                    cancel_event=cancel_event,
                    progress_callback=cancel_after_first_chunk,
                )

            self.assertFalse((destination / f"{release.installer.name}.part").exists())

    def test_manifest_must_match_both_release_packages(self):
        release, opener, _package_data, manifest = self._release_and_opener()
        wrong_name = ("x" * len(release.portable.name)).encode("ascii")
        bad_manifest = manifest.replace(
            release.portable.name.encode("ascii"), wrong_name
        )
        release = replace(
            release,
            manifest=replace(
                release.manifest,
                github_sha256=hashlib.sha256(bad_manifest).hexdigest(),
            ),
        )
        opener._responses[release.manifest.download_url] = [
            FakeResponse(
                bad_manifest,
                "https://release-assets.githubusercontent.com/manifest",
                content_length=len(bad_manifest),
            )
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(application_update.ApplicationUpdateError) as caught:
                application_update.download_update_package(
                    release, "installer", Path(tmpdir), opener=opener
                )

        self.assertEqual(caught.exception.code, "invalid_manifest")


class UpdateCleanupTests(unittest.TestCase):
    @staticmethod
    def _write_owned_package(directory: Path, version: str) -> None:
        directory.mkdir(parents=True)
        (directory / f"RemoteMicRC003-{version}-portable-unsigned.zip").write_bytes(
            b"package"
        )

    def test_cleanup_removes_only_owned_current_and_older_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "updates"
            for version in (
                "0.2.0-candidate.14",
                "0.2.0-candidate.15",
                "0.2.0-candidate.16",
            ):
                self._write_owned_package(root / version, version)
            (root / "0.2.0-candidate.14" / "RemoteMicRC003-0.2.0-candidate.14-portable-unsigned.zip.part.json").write_text("{}")
            preserved_foreign = root / "0.2.0-candidate.13"
            preserved_foreign.mkdir()
            (preserved_foreign / "user-note.txt").write_text(
                "keep", encoding="utf-8"
            )
            preserved_unknown = root / "manual-copy"
            preserved_unknown.mkdir()

            removed = application_update.cleanup_obsolete_update_downloads(
                root,
                "0.2.0-candidate.15",
            )

            self.assertEqual(removed, 2)
            self.assertFalse((root / "0.2.0-candidate.14").exists())
            self.assertFalse((root / "0.2.0-candidate.15").exists())
            self.assertTrue((root / "0.2.0-candidate.16").is_dir())
            self.assertTrue((preserved_foreign / "user-note.txt").is_file())
            self.assertTrue(preserved_unknown.is_dir())

    def test_predownload_cleanup_preserves_the_target_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "updates"
            for version in (
                "0.2.0-candidate.14",
                "0.2.0-candidate.15",
            ):
                self._write_owned_package(root / version, version)

            removed = application_update.cleanup_obsolete_update_downloads(
                root,
                "0.2.0-candidate.15",
                include_current=False,
            )

            self.assertEqual(removed, 1)
            self.assertFalse((root / "0.2.0-candidate.14").exists())
            self.assertTrue((root / "0.2.0-candidate.15").is_dir())


if __name__ == "__main__":
    unittest.main()
