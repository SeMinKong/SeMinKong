import importlib.util
import tempfile
import unittest
from datetime import date
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "update_codex_card.py"
SPEC = importlib.util.spec_from_file_location("update_codex_card", MODULE_PATH)
card = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(card)


class CodexCardTests(unittest.TestCase):
    def test_parse_turn_and_deduplicate(self):
        span = {
            "name": "session_task.turn",
            "startTimeUnixNano": "1787106952000000000",
            "attributes": [
                {"key": "turn.id", "value": {"stringValue": "turn-1"}},
                {"key": "model", "value": {"stringValue": "gpt-5.6-luna"}},
                {
                    "key": "codex.turn.token_usage.input_tokens",
                    "value": {"intValue": "9164"},
                },
                {
                    "key": "codex.turn.token_usage.output_tokens",
                    "value": {"intValue": "163"},
                },
                {
                    "key": "codex.turn.token_usage.total_tokens",
                    "value": {"intValue": "9327"},
                },
            ],
        }
        turns = card.parse_turns({"trace": {"resourceSpans": [{"spans": [span, span]}]}})
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["tokens"]["total_tokens"], 9327)
        self.assertEqual(turns[0]["model"], "gpt-5.6-luna")

    def test_state_deduplicates_turns(self):
        state = card.load_state(Path("missing-for-test.json"))
        turn = {
            "turn_id": "turn-1",
            "start_ns": 1787106952000000000,
            "model": "gpt-5.6-luna",
            "tokens": {key: 0 for key in card.TOKEN_KEYS},
        }
        turn["tokens"]["total_tokens"] = 9327
        self.assertTrue(card.add_turn(state, turn))
        self.assertFalse(card.add_turn(state, turn))
        self.assertEqual(sum(day["turns"] for day in state["days"].values()), 1)

    def test_streaks_allow_today_to_be_empty(self):
        days = {date(2026, 8, 16), date(2026, 8, 17), date(2026, 8, 18)}
        self.assertEqual(card.streaks(days, date(2026, 8, 19)), (3, 3))

    def test_render_svg(self):
        state = card.load_state(Path("missing-for-test.json"))
        state["days"] = {
            "2026-08-19": {
                "turns": 1,
                "models": {"gpt-5.6-luna": 1},
                **{key: 0 for key in card.TOKEN_KEYS},
                "total_tokens": 9327,
            }
        }
        svg = card.render_svg(state, date(2026, 8, 19))
        self.assertIn("9.3K", svg)
        self.assertIn("gpt-5.6-luna", svg)
        self.assertIn("PRIMARY MODEL", svg)
        self.assertIn('width="322"', svg)
        self.assertIn('width="522"', svg)
        self.assertIn("@keyframes botFloat", svg)
        self.assertIn("prefers-reduced-motion", svg)
        self.assertIn("today-cell", svg)
        self.assertIn("<svg", svg)

    def test_primary_model_excludes_internal_codex_automation(self):
        state = card.load_state(Path("missing-for-test.json"))
        state["days"] = {
            "2026-08-19": {
                "turns": 65,
                "models": {
                    "codex-auto-review": 34,
                    "gpt-5.6-luna": 21,
                    "gpt-5.6-sol": 10,
                },
                **{key: 0 for key in card.TOKEN_KEYS},
            }
        }

        svg = card.render_svg(state, date(2026, 8, 19))

        self.assertIn("gpt-5.6-luna", svg)
        self.assertNotIn("codex-auto-review", svg)

    def test_baseline_is_preserved_and_new_usage_is_added(self):
        state = card.load_state(Path("missing-for-test.json"))
        state["baseline"] = {
            "total_tokens": 4_050_000_000,
            "max_daily_tokens": 410_000_000,
            "longest_streak": 15,
            "active_dates": ["2026-08-18", "2026-08-19"],
        }
        state["days"] = {
            "2026-08-19": {
                "turns": 1,
                "models": {"gpt-5.6-luna": 1},
                **{key: 0 for key in card.TOKEN_KEYS},
                "total_tokens": 10_000_000,
            }
        }

        svg = card.render_svg(state, date(2026, 8, 19))

        self.assertIn("40.6억", svg)
        self.assertIn("4.1억", svg)
        self.assertIn("2일", svg)
        self.assertIn("15일", svg)


if __name__ == "__main__":
    unittest.main()
