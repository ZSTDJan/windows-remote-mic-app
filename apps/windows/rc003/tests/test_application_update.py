import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path

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


class ApplicationVersionTests(unittest.TestCase):
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


class UpdateDownloadTests(unittest.TestCase):
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

    def test_interrupted_download_cleans_part_file(self):
        release, opener, package_data, _manifest = self._release_and_opener()
        opener._responses[release.installer.download_url] = [
            FakeResponse(
                package_data,
                "https://release-assets.githubusercontent.com/installer",
                content_length=len(package_data),
                fail_after=application_update.DOWNLOAD_CHUNK_BYTES,
            )
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir)
            with self.assertRaises(application_update.ApplicationUpdateError):
                application_update.download_update_package(
                    release, "installer", destination, opener=opener
                )

            self.assertFalse((destination / f"{release.installer.name}.part").exists())

    def test_timeout_during_download_closes_response_and_cleans_part_file(self):
        release, opener, package_data, _manifest = self._release_and_opener()
        response = FakeResponse(
            package_data,
            "https://release-assets.githubusercontent.com/installer",
            content_length=len(package_data),
            fail_after=application_update.DOWNLOAD_CHUNK_BYTES,
            fail_with=TimeoutError("timed out while downloading"),
        )
        opener._responses[release.installer.download_url] = [response]

        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir)
            with self.assertRaises(application_update.ApplicationUpdateError) as caught:
                application_update.download_update_package(
                    release, "installer", destination, opener=opener
                )

            self.assertEqual(caught.exception.code, "timeout")
            self.assertTrue(response.closed)
            self.assertFalse((destination / release.installer.name).exists())
            self.assertFalse((destination / f"{release.installer.name}.part").exists())

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
