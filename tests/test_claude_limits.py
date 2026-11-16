"""Claude usage rows: the endpoint's limits[] (current format) vs the legacy fixed keys.

Run with: python -B -m unittest discover -s tests
"""
import unittest
from unittest.mock import patch

import quota_card as card


def row(kind, group, percent, severity="normal", active=False, model=None, resets_at="2026-11-20T10:00:00Z"):
    r = {"kind": kind, "group": group, "percent": percent, "resets_at": resets_at,
         "severity": severity, "is_active": active}
    if model:
        r["scope"] = {"model": {"display_name": model}, "surface": None}
    return r


# Ultra: no session (5-hour) meter, a weekly meter and a Fable weekly meter. The legacy
# fixed keys are still in the payload but do not describe this plan (five_hour has a value).
ULTRA = {
    "five_hour": {"utilization": 63.0, "resets_at": "2026-11-16T15:00:00Z"},
    "seven_day": {"utilization": 0.0, "resets_at": "2026-11-20T10:00:00Z"},
    "limits": [row("weekly_all", "weekly", 41, active=True),
               row("weekly_scoped", "weekly", 77, "warning", model="Fable"),
               row("weekly_scoped", "weekly", 12, model="Opus 5.5")],
}
MAX = {"limits": [row("session", "session", 88, "critical", True, resets_at="2026-11-16T15:00:00Z"),
                  row("weekly_all", "weekly", 30),
                  row("weekly_scoped", "weekly", 9, model="Sonnet 5.5")]}
LEGACY = {"five_hour": {"utilization": 7, "resets_at": "2026-11-16T15:00:00Z"},
          "seven_day": {"utilization": 31, "resets_at": "2026-11-20T10:00:00Z"},
          "iguana_necktie": {"utilization": 28, "resets_at": "2026-11-21T10:00:00Z"}}


class ClaudeLimitsTests(unittest.TestCase):
    def fetch(self, payload):
        class Resp:
            status_code, ok, headers = 200, True, {}

            def json(self):
                return payload
        self.urls = []
        session = type("S", (), {"get": lambda _s, url, **_k: (self.urls.append(url), Resp())[1]})()
        with patch.object(card, "_http", return_value=session), \
                patch.object(card, "_claude_usage_candidates", return_value=iter([("tok", None, "test")])), \
                patch.object(card, "_claude_usage_plan", return_value=None):
            return card.fetch_claude()

    @staticmethod
    def rows(res):
        return [(k, round(u), label) for k, u, _r, label in res["windows"]]

    def test_same_request_as_the_cli_windows_read(self):
        self.fetch(ULTRA)
        self.assertTrue(self.urls[0].endswith("/api/oauth/usage?skip_spend=1"))

    def test_ultra_uses_limits_and_has_no_five_hour_row(self):
        res = self.fetch(ULTRA)
        self.assertEqual(self.rows(res), [("seven_day", 41, "7 天"),
                                          ("seven_day_fable", 77, "7 天 · Fable"),
                                          ("seven_day_opus", 12, "7 天 · Opus 5.5")])
        self.assertEqual(res["meta"]["seven_day_fable"]["severity"], "warning")
        self.assertTrue(res["meta"]["seven_day"]["active"])

    def test_session_row_keeps_the_old_five_hour_key(self):
        res = self.fetch(MAX)
        self.assertEqual(self.rows(res)[0], ("five_hour", 88, "5 小时"))
        self.assertEqual(self.rows(res)[2][0], "seven_day_sonnet")  # model version not part of the key

    def test_legacy_payload_without_limits_still_parsed(self):
        self.assertEqual(self.rows(self.fetch(LEGACY)),
                         [("five_hour", 7, "5 小时"), ("seven_day", 31, "7 天"), ("iguana_necktie", 28, "7 天 · Fable")])

    def test_colours_follow_server_severity(self):
        self.assertEqual(card.bar_color(30, "critical"), card.C_RED)
        self.assertEqual(card.bar_color(95, "normal"), card.C_GREEN)
        self.assertEqual(card.bar_color(60), card.C_AMBER)  # no severity: local thresholds

    def test_plan_badges(self):
        self.assertEqual([card._claude_plan_badge(x) for x in ("claude_max", "ultra", "claude_ultra", "max_20x", "", None)],
                         ["MAX", "ULTRA", "ULTRA", "MAX 20X", None, None])


if __name__ == "__main__":
    unittest.main()
