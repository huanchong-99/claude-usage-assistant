import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import quota_card as card


class ClaudeUsageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.desktop = Path(directory.name)
        self.patch("_desktop_dir", return_value=self.desktop)
        self.patch("_safestorage_key", return_value=object())
        self.patch("_desktop_decrypt", side_effect=lambda value, key: value.encode())
        self.patch("_desk_cache", {"stamp": None, "tokens": []})
        self.patch("_DESK_DIAG", {})
        self.patch("_ua_cache", "test")
        self.patch("_claude_last_source", "—")
        self.patch("_profile_token", None)
        self.patch("_profile_plan", None)
        self.patch("_profile_retry_at", 0.0)
        self.profile_payload = None
        self.profile_requests = []
        self.load = self.patch("_claude_load", return_value=(None, None))
        self.refresh = self.patch("_claude_refresh", side_effect=AssertionError("unexpected refresh"))
        self.session = self.patch("_http").return_value
        self.requests = []

    def patch(self, name, *args, **kwargs):
        p = patch.object(card, name, *args, **kwargs)
        self.addCleanup(p.stop)
        return p.start()

    def cache(self, *, old=(), new=(), plan="max"):
        config = {}
        for name, entries in (("oauth:tokenCache", old), ("oauth:tokenCacheV2", new)):
            records = {}
            for index, (scope, token, ttl) in enumerate(entries):
                key = f"account{index}:https://api.anthropic.com:{scope}"
                records[key] = {"token": token, "expiresAt": time.time() + ttl,
                                "subscriptionType": plan}
            config[name] = json.dumps(records)
        (self.desktop / "config.json").write_text(json.dumps(config), encoding="utf-8")

    def serve(self, outcomes):
        def get(url, *, headers, timeout):
            token = headers["Authorization"].removeprefix("Bearer ")
            if url == card.API_URL_PROFILE:
                self.profile_requests.append(token)
                status, payload = (200, self.profile_payload) if self.profile_payload else (503, {})
            else:
                self.assertEqual(url, card.API_URL_USAGE)
                self.requests.append(token)
                status, message = outcomes[token]
                payload = ({"five_hour": {"utilization": 7, "resets_at": "2026-01-01T12:00:00Z"}}
                           if status == 200 else {"error": {"message": message}})
            response = requests.Response()
            response.status_code = status
            response._content = json.dumps(payload).encode()
            return response
        self.session.get.side_effect = get

    def cli(self, ttl):
        creds = {"claudeAiOauth": {"accessToken": "cli", "refreshToken": "test-refresh",
                                   "expiresAt": time.time() + ttl, "subscriptionType": "max"}}
        self.load.return_value = (creds, "keychain")

    def test_cli_expiry_switches_to_profile_token_without_refreshing(self):
        now = time.time()
        self.cli(300)
        self.cache(old=[("user:inference user:office", "office", 8_000_000),
                        ("user:inference user:profile:write", "wrong-scope", 9_000_000)],
                   new=[("user:inference user:profile", "read-usage", 3600)])
        self.serve({"cli": (200, ""), "read-usage": (200, "")})
        self.assertIn("windows", card.fetch_claude())
        self.assertEqual(self.requests, ["cli"])
        with patch.object(card.time, "time", return_value=now + 600):
            self.assertEqual(card.fetch_claude()["windows"][0][1], 7)
        self.assertEqual(self.requests, ["cli", "read-usage"])
        self.refresh.assert_not_called()
        report = "\n".join(card._claude_token_report())
        self.assertIn("可用(1 个含 user:profile 的候选)", report)
        self.assertIn("已跳过 2 个", report)

    def test_rejections_reach_later_candidate_and_skip_duplicate_tokens(self):
        self.cli(3600)
        self.cache(old=[("user:profile", "denied", 7200), ("user:profile", "denied", 7100),
                        ("user:profile", "invalid", 7000), ("user:profile", "works", 6900)])
        self.serve({"cli": (401, ""), "denied": (403, "Missing required scope: user:profile"),
                    "invalid": (401, ""), "works": (200, "")})
        self.assertEqual(card.fetch_claude()["plan"], "MAX")
        self.assertEqual(self.requests, ["cli", "denied", "invalid", "works"])
        self.refresh.assert_not_called()

    def test_no_desktop_usage_scope_falls_back_to_one_cli_refresh(self):
        self.cli(-600)
        self.cache(old=[("user:inference", "inference-only", 7200)])
        def renew(creds, source):
            creds["claudeAiOauth"]["accessToken"] = "renewed"
        self.refresh.side_effect = renew
        self.serve({"renewed": (200, "")})
        self.assertIn("windows", card.fetch_claude())
        self.assertEqual(self.requests, ["renewed"])
        self.refresh.assert_called_once()
        self.assertIn("缺少读取用量所需的 user:profile", "\n".join(card._claude_token_report()))

    def test_no_token_rotation_on_rate_limits_policy_denials_or_server_errors(self):
        self.cli(3600)
        self.patch("_claude_desktop_tokens", side_effect=AssertionError("unexpected desktop lookup"))
        for status in (403, 429, 503):
            with self.subTest(status=status):
                self.requests.clear()
                self.serve({"cli": (status, "Request rejected")})
                self.assertIn(f"HTTP {status}", card.fetch_claude()["error"])
                self.assertEqual(self.requests, ["cli"])
                self.refresh.assert_not_called()

    def test_desktop_plan_comes_from_the_profile_of_the_successful_token(self):
        self.cache(new=[("user:profile", "first", 7200)], plan="pro")
        self.serve({"first": (200, ""), "second": (200, "")})
        self.profile_payload = {"organization": {"organization_type": "claude_max"}}
        for _ in range(2):
            self.assertEqual(card.fetch_claude()["plan"], "MAX")
        self.assertEqual(self.profile_requests, ["first"])
        self.cache(new=[("user:profile", "second", 7200)], plan="max")
        self.profile_payload = {"account": {"has_claude_pro": True}}
        self.assertEqual(card.fetch_claude()["plan"], "PRO")
        self.assertEqual(self.profile_requests, ["first", "second"])


if __name__ == "__main__":
    unittest.main()
