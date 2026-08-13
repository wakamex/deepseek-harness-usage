from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import deepseek_harness_usage as usage


PAYLOAD = {
    "is_available": True,
    "balance_infos": [
        {
            "currency": "USD",
            "total_balance": "123.45",
            "granted_balance": "23.45",
            "topped_up_balance": "100.00",
        }
    ],
}


class Response:
    def __init__(self, payload: object):
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.dsh_home = self.root / "dsh"
        self.project = self.root / "project"
        self.dsh_home.mkdir()
        self.project.mkdir()
        self.environment = patch.dict(
            os.environ,
            {"DSH_HOME": str(self.dsh_home)},
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write_credential(self, text: str) -> Path:
        credential = self.dsh_home / ".credentials.yaml"
        credential.write_text(text, encoding="utf-8")
        credential.chmod(0o600)
        return credential

    def test_credential_precedence_matches_harness_defaults(self):
        self.write_credential("DEEPSEEK_API_KEY: stored-key\n")
        (self.project / ".env").write_text("DEEPSEEK_API_KEY=project-key\n")
        (self.dsh_home / ".env").write_text("DEEPSEEK_API_KEY=user-key\n")

        self.assertEqual(usage.get_api_key(self.project), "stored-key")
        os.environ["DEEPSEEK_API_KEY"] = "inherited-key"
        self.assertEqual(usage.get_api_key(self.project), "inherited-key")

    def test_project_env_beats_user_env_when_managed_store_is_absent(self):
        (self.project / ".env").write_text(
            "# comment\nexport DEEPSEEK_API_KEY='project-key'\n"
        )
        (self.dsh_home / ".env").write_text("DEEPSEEK_API_KEY=user-key\n")

        self.assertEqual(usage.get_api_key(self.project), "project-key")

    def test_user_env_is_the_last_fallback(self):
        (self.dsh_home / ".env").write_text('DEEPSEEK_API_KEY="user-key" # note\n')

        self.assertEqual(usage.get_api_key(self.project), "user-key")

    def test_dotenv_parser_matches_harness_node_semantics(self):
        cases = {
            "DEEPSEEK_API_KEY=abc#comment\n": "abc",
            "DEEPSEEK_API_KEY=abc # comment\n": "abc",
            "DEEPSEEK_API_KEY=`backtick-key`\n": "backtick-key",
            'DEEPSEEK_API_KEY="double-key" trailing\n': "double-key",
            "export DEEPSEEK_API_KEY = 'single-key'\n": "single-key",
            'DEEPSEEK_API_KEY="line\\nbreak"\n': "line\nbreak",
            "DEEPSEEK_API_KEY=first\r\nDEEPSEEK_API_KEY=second\r\n": "second",
            'DEEPSEEK_API_KEY="unterminated\nOTHER=value\n': '"unterminated',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                (self.project / ".env").write_text(text, encoding="utf-8")
                self.assertEqual(
                    usage._parse_node_env(text).get("DEEPSEEK_API_KEY"), expected
                )

    def test_dotenv_counterexamples_resolve_the_same_key_as_harness(self):
        for text, expected in (
            ("DEEPSEEK_API_KEY=abc#comment\n", "abc"),
            ("DEEPSEEK_API_KEY=`abc`\n", "abc"),
        ):
            with self.subTest(text=text):
                (self.project / ".env").write_text(text, encoding="utf-8")
                self.assertEqual(usage.get_api_key(self.project), expected)

    def test_managed_yaml_supports_harness_generated_string_styles(self):
        cases = {
            "DEEPSEEK_API_KEY: plain-key\n": "plain-key",
            "DEEPSEEK_API_KEY: 'key''quote'\n": "key'quote",
            'DEEPSEEK_API_KEY: "quoted-key"\n': "quoted-key",
            "DEEPSEEK_API_KEY: |-\n  block-key\n": "block-key",
            '"DEEPSEEK_API_KEY": quoted-name\n': "quoted-name",
            "{DEEPSEEK_API_KEY: flow-key}\n": "flow-key",
            "# café\nDEEPSEEK_API_KEY: utf8-document\n": "utf8-document",
            "DEEPSEEK_API_KEY: yes\n": "yes",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.write_credential(text)
                self.assertEqual(usage.get_api_key(self.project), expected)

    def test_managed_yaml_rejects_the_whole_invalid_document(self):
        secret = "fixture-secret"
        cases = [
            f"DEEPSEEK_API_KEY: {secret}\nOTHER: [unterminated\n",
            f"DEEPSEEK_API_KEY: {secret}\nOTHER: 42\n",
            f"DEEPSEEK_API_KEY: {secret}\nDEEPSEEK_API_KEY: duplicate\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.write_credential(text)
                with self.assertRaises(usage.UsageError) as caught:
                    usage.get_api_key(self.project)
                self.assertNotIn(secret, str(caught.exception))

    def test_live_reads_reread_credentials_and_never_modify_them(self):
        credential = self.write_credential("DEEPSEEK_API_KEY: first\n")
        requests: list[str] = []

        def urlopen(request, timeout):
            self.assertEqual(timeout, 15)
            requests.append(request.get_header("Authorization"))
            return Response(PAYLOAD)

        with patch("deepseek_harness_usage.urllib.request.build_opener") as build:
            build.return_value.open.side_effect = urlopen
            usage.fetch_balance()
            credential.write_text("DEEPSEEK_API_KEY: second\n")
            usage.fetch_balance()

        self.assertEqual(requests, ["Bearer first", "Bearer second"])
        self.assertEqual(credential.read_text(), "DEEPSEEK_API_KEY: second\n")

    def test_missing_and_invalid_credentials_fail_without_secret_values(self):
        with self.assertRaisesRegex(usage.UsageError, "no DEEPSEEK_API_KEY"):
            usage.get_api_key(self.project)
        secret = "sk-do-not-print"
        self.write_credential(f'DEEPSEEK_API_KEY: "{secret}\n')
        with self.assertRaises(usage.UsageError) as caught:
            usage.get_api_key(self.project)
        self.assertNotIn(secret, str(caught.exception))

    def test_store_permissions_match_harness_safety_rule(self):
        credential = self.write_credential("DEEPSEEK_API_KEY: secret\n")
        credential.chmod(0o644)

        with self.assertRaisesRegex(usage.UsageError, "chmod 600"):
            usage.get_api_key(self.project)

    def test_key_is_trimmed_and_illegal_header_characters_are_rejected(self):
        os.environ["DEEPSEEK_API_KEY"] = "  key-with-space-around-it  "
        self.assertEqual(usage.get_api_key(self.project), "key-with-space-around-it")
        os.environ["DEEPSEEK_API_KEY"] = "secret\nsecond-header"
        with self.assertRaisesRegex(usage.UsageError, "HTTP header"):
            usage.get_api_key(self.project)

    def test_known_custom_route_is_refused_before_credential_resolution(self):
        self.write_credential("DEEPSEEK_API_KEY: gateway-secret\n")
        (self.dsh_home / "settings.yaml").write_text(
            "llm-deepseek:\n  baseURL: https://gateway.example\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(usage.UsageError, "custom DeepSeek endpoint"):
            usage.get_api_key(self.project)

        os.environ[usage.EXPLICIT_KEY_ENV] = "explicit-official-key"
        self.assertEqual(usage.get_api_key(self.project), "explicit-official-key")

    def test_blank_custom_route_configuration_also_fails_closed(self):
        self.write_credential("DEEPSEEK_API_KEY: gateway-secret\n")
        cases = ["environment", "project", "settings"]
        for source in cases:
            with self.subTest(source=source):
                os.environ.pop("DEEPSEEK_BASE_URL", None)
                (self.project / ".env").unlink(missing_ok=True)
                (self.dsh_home / "settings.yaml").unlink(missing_ok=True)
                if source == "environment":
                    os.environ["DEEPSEEK_BASE_URL"] = ""
                elif source == "project":
                    (self.project / ".env").write_text(
                        "DEEPSEEK_BASE_URL=\n", encoding="utf-8"
                    )
                else:
                    (self.dsh_home / "settings.yaml").write_text(
                        'llm-deepseek:\n  baseURL: ""\n', encoding="utf-8"
                    )

                with self.assertRaisesRegex(
                    usage.UsageError, "custom DeepSeek endpoint"
                ):
                    usage.get_api_key(self.project)

    def test_custom_credential_reference_requires_explicit_official_key(self):
        self.write_credential("GATEWAY_KEY: gateway-secret\n")
        (self.dsh_home / "settings.yaml").write_text(
            "llm-deepseek:\n  apiKeyEnv: GATEWAY_KEY\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(usage.UsageError, "custom.*reference"):
            usage.get_api_key(self.project)

    def test_harness_tilde_expansion_is_lexical_and_narrow(self):
        home = str(Path.home())

        self.assertEqual(usage._expand_harness_home("~"), home)
        self.assertEqual(usage._expand_harness_home("~/dsh"), os.path.join(home, "dsh"))
        self.assertEqual(
            usage._expand_harness_home("~\\dsh"), os.path.join(home, "dsh")
        )
        self.assertEqual(usage._expand_harness_home("~other/dsh"), "~other/dsh")


class BalanceTests(unittest.TestCase):
    def test_fetch_uses_only_the_fixed_official_endpoint(self):
        seen = {}

        def urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["authorization"] = request.get_header("Authorization")
            seen["accept"] = request.get_header("Accept")
            seen["timeout"] = timeout
            return Response(PAYLOAD)

        with (
            patch("deepseek_harness_usage.get_api_key", return_value="secret"),
            patch("deepseek_harness_usage.urllib.request.build_opener") as build,
        ):
            build.return_value.open.side_effect = urlopen
            result = usage.fetch_balance()

        self.assertEqual(result, PAYLOAD)
        self.assertEqual(seen["url"], "https://api.deepseek.com/user/balance")
        self.assertEqual(seen["authorization"], "Bearer secret")
        self.assertEqual(seen["accept"], "application/json")
        self.assertEqual(seen["timeout"], 15)
        handlers = build.call_args.args
        self.assertTrue(
            any(isinstance(item, usage._RejectRedirects) for item in handlers)
        )

    def test_redirects_are_rejected_before_authorization_can_be_forwarded(self):
        handler = usage._RejectRedirects()

        self.assertIsNone(
            handler.redirect_request(
                object(), None, 302, "found", {}, "https://example.invalid"
            )
        )

    def test_normalization_preserves_decimal_strings_and_sorts_currencies(self):
        payload = {
            "is_available": False,
            "balance_infos": [
                {
                    "currency": "USD",
                    "total_balance": "0.1000",
                    "granted_balance": "0.0000",
                    "topped_up_balance": "0.1000",
                },
                {
                    "currency": "CNY",
                    "total_balance": "12.34",
                    "granted_balance": "2.34",
                    "topped_up_balance": "10.00",
                },
            ],
        }

        result = usage.normalize_balance(payload)

        self.assertFalse(result["available"])
        self.assertEqual(
            [item["currency"] for item in result["balances"]], ["CNY", "USD"]
        )
        self.assertEqual(result["balances"][1]["total"], "0.1000")

    def test_normalization_rejects_drift_and_duplicate_currency(self):
        cases = [
            {},
            {"is_available": True, "balance_infos": [{}]},
            {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "USD",
                        "total_balance": "NaN",
                        "granted_balance": "0",
                        "topped_up_balance": "0",
                    }
                ],
            },
            {
                "is_available": True,
                "balance_infos": [PAYLOAD["balance_infos"][0]] * 2,
            },
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(usage.UsageError):
                usage.normalize_balance(payload)

    def test_auth_and_network_failures_are_concise_and_secret_safe(self):
        secret = "sk-never-log-this"

        def unauthorized(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 401, "unauthorized", {}, None
            )

        with (
            patch("deepseek_harness_usage.get_api_key", return_value=secret),
            patch("deepseek_harness_usage.urllib.request.build_opener") as build,
        ):
            build.return_value.open.side_effect = unauthorized
            snapshot = usage.build_usage_json()

        self.assertEqual(snapshot["status"], "unavailable")
        self.assertIn("no longer accepts", snapshot["error"])
        self.assertNotIn(secret, json.dumps(snapshot))

    def test_unexpected_exception_does_not_escape_into_json(self):
        secret = "unexpected-secret"
        with patch(
            "deepseek_harness_usage.fetch_balance",
            side_effect=RuntimeError(secret),
        ):
            snapshot = usage.build_usage_json()

        self.assertEqual(
            snapshot["error"], "Unexpected error while checking DeepSeek balance"
        )
        self.assertNotIn(secret, json.dumps(snapshot))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name) / "nested" / "usage.json"
        self.cache_patch = patch.dict(
            os.environ,
            {
                usage.USAGE_FILE_ENV: str(self.cache),
                usage.EXPLICIT_KEY_ENV: "cache-key",
            },
            clear=False,
        )
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)

    @staticmethod
    def snapshot(retrieved_at: str | None = None) -> dict:
        return {
            "schema_version": 1,
            "provider": "deepseek",
            "client": "deepseek_harness",
            "source": "deepseek_user_balance_api",
            "retrieved_at": retrieved_at or datetime.now(UTC).isoformat(),
            "status": "live",
            "account_balance": usage.normalize_balance(PAYLOAD),
        }

    @staticmethod
    def cache_record(snapshot: dict, api_key: str = "cache-key") -> dict:
        return usage._bound_cache_record(
            snapshot,
            usage._credential_fingerprint(api_key),
            snapshot["retrieved_at"],
        )

    def test_atomic_write_creates_a_valid_cache_without_residue(self):
        snapshot = self.cache_record(self.snapshot())

        usage.write_usage_file(snapshot)

        self.assertEqual(json.loads(self.cache.read_text(encoding="utf-8")), snapshot)
        self.assertEqual(
            set(self.cache.parent.iterdir()),
            {self.cache, self.cache.with_name(f"{self.cache.name}.lock")},
        )
        if os.name != "nt":
            self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.cache.parent.stat().st_mode & 0o777, 0o700)

    def test_fresh_cache_avoids_network(self):
        usage.write_usage_file(self.cache_record(self.snapshot()))

        with patch("deepseek_harness_usage.build_usage_json") as fetch:
            result = usage.get_cached_usage()

        self.assertEqual(result["status"], "cached")
        fetch.assert_not_called()

    def test_failed_refresh_serves_a_valid_stale_cache(self):
        usage.write_usage_file(
            self.cache_record(self.snapshot("2020-01-01T00:00:00+00:00"))
        )
        unavailable = {
            "schema_version": 1,
            "provider": "deepseek",
            "client": "deepseek_harness",
            "source": "deepseek_user_balance_api",
            "retrieved_at": datetime.now(UTC).isoformat(),
            "status": "unavailable",
            "error": "offline",
        }

        with patch(
            "deepseek_harness_usage.build_usage_json", return_value=unavailable
        ) as fetch:
            result = usage.get_cached_usage()

        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["refresh_error"], "offline")
        self.assertEqual(result["account_balance"]["balances"][0]["total"], "123.45")
        fetch.assert_called_once_with("cache-key")

    def test_no_cache_and_failed_refresh_is_unavailable(self):
        unavailable = {
            "schema_version": 1,
            "provider": "deepseek",
            "client": "deepseek_harness",
            "source": "deepseek_user_balance_api",
            "retrieved_at": datetime.now(UTC).isoformat(),
            "status": "unavailable",
            "error": "offline",
        }
        with patch("deepseek_harness_usage.build_usage_json", return_value=unavailable):
            result = usage.get_cached_usage()

        self.assertEqual(result, unavailable)

    def test_force_refresh_replaces_a_fresh_cache(self):
        usage.write_usage_file(self.cache_record(self.snapshot()))
        next_snapshot = self.snapshot()
        next_snapshot["account_balance"]["balances"][0]["total"] = "8.50"

        with patch(
            "deepseek_harness_usage.build_usage_json", return_value=next_snapshot
        ) as fetch:
            result = usage.get_cached_usage(force_refresh=True)

        fetch.assert_called_once_with("cache-key")
        self.assertEqual(result["account_balance"]["balances"][0]["total"], "8.50")
        self.assertEqual(
            json.loads(self.cache.read_text())["account_balance"]["balances"][0][
                "total"
            ],
            "8.50",
        )

    def test_cache_is_never_reused_across_key_rotation(self):
        usage.write_usage_file(
            self.cache_record(self.snapshot(), api_key="account-a-key")
        )
        os.environ[usage.EXPLICIT_KEY_ENV] = "account-b-key"
        unavailable = usage._unavailable_snapshot("offline")

        with patch(
            "deepseek_harness_usage.build_usage_json", return_value=unavailable
        ) as fetch:
            result = usage.get_cached_usage()

        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("account_balance", result)
        fetch.assert_called_once_with("account-b-key")
        stored = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertEqual(
            stored["credential_fingerprint"],
            usage._credential_fingerprint("account-b-key"),
        )

    def test_failed_statusline_refresh_has_a_short_persisted_backoff(self):
        usage.write_usage_file(
            self.cache_record(self.snapshot("2020-01-01T00:00:00+00:00"))
        )
        unavailable = usage._unavailable_snapshot("offline")

        with patch(
            "deepseek_harness_usage.build_usage_json", return_value=unavailable
        ) as fetch:
            first = usage.get_cached_usage()
            second = usage.get_cached_usage()

        self.assertEqual(first["status"], "stale")
        self.assertEqual(second["status"], "stale")
        fetch.assert_called_once_with("cache-key")

    def test_concurrent_refreshes_have_one_writer(self):
        usage.write_usage_file(
            self.cache_record(self.snapshot("2020-01-01T00:00:00+00:00"))
        )
        started = threading.Event()
        finish = threading.Event()
        first_result: list[dict] = []
        unavailable = usage._unavailable_snapshot("offline")

        def slow_refresh(api_key):
            self.assertEqual(api_key, "cache-key")
            started.set()
            self.assertTrue(finish.wait(2))
            return unavailable

        with patch(
            "deepseek_harness_usage.build_usage_json", side_effect=slow_refresh
        ) as fetch:
            worker = threading.Thread(
                target=lambda: first_result.append(
                    usage.get_cached_usage(force_refresh=True)
                )
            )
            worker.start()
            self.assertTrue(started.wait(2))
            second = usage.get_cached_usage(force_refresh=True)
            finish.set()
            worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(second["status"], "stale")
        self.assertIn("already in progress", second["refresh_error"])
        self.assertEqual(first_result[0]["status"], "stale")
        stored = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertEqual(stored["status"], "stale")
        self.assertTrue(self.cache.with_name(f"{self.cache.name}.lock").exists())

    def test_stale_lock_file_does_not_block_a_new_owner(self):
        self.cache.parent.mkdir(mode=0o700)
        lock = self.cache.with_name(f"{self.cache.name}.lock")
        lock.write_text("999999\n", encoding="ascii")

        usage.write_usage_file(self.cache_record(self.snapshot()))

        self.assertTrue(usage._cache_is_valid(json.loads(self.cache.read_text())))
        self.assertEqual(lock.read_text(encoding="ascii"), f"{os.getpid()}\n")

    def test_atomic_replace_replaces_a_cache_symlink_not_its_target(self):
        self.cache.parent.mkdir(mode=0o700)
        target = Path(self.directory.name) / "target.json"
        target.write_text("keep me\n", encoding="utf-8")
        self.cache.symlink_to(target)

        usage.write_usage_file(self.cache_record(self.snapshot()))

        self.assertEqual(target.read_text(encoding="utf-8"), "keep me\n")
        self.assertFalse(self.cache.is_symlink())
        self.assertTrue(usage._cache_is_valid(json.loads(self.cache.read_text())))


class CliTests(unittest.TestCase):
    def test_status_and_statusline_outputs(self):
        snapshot = CacheTests.snapshot()
        status_output = io.StringIO()
        with (
            patch("deepseek_harness_usage.build_usage_json", return_value=snapshot),
            patch.object(os.sys, "argv", ["deepseek-harness-usage"]),
            redirect_stdout(status_output),
        ):
            self.assertEqual(usage.main(), 0)

        self.assertIn("DeepSeek Harness balance", status_output.getvalue())
        self.assertIn("USD: 123.45 total", status_output.getvalue())

        line_output = io.StringIO()
        with (
            patch(
                "deepseek_harness_usage.get_cached_usage",
                return_value={**snapshot, "status": "stale"},
            ),
            patch.object(os.sys, "argv", ["deepseek-harness-usage", "statusline"]),
            redirect_stdout(line_output),
        ):
            self.assertEqual(usage.main(), 0)

        self.assertEqual(line_output.getvalue().strip(), "dsh:bal:USD123.45 stale")

    def test_api_unavailable_is_distinct_from_refresh_unavailable(self):
        snapshot = CacheTests.snapshot()
        snapshot["account_balance"]["available"] = False

        self.assertEqual(
            usage._statusline_text(snapshot), "dsh:bal:USD123.45 api-unavailable"
        )


if __name__ == "__main__":
    unittest.main()
