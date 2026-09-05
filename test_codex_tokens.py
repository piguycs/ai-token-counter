import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from codex_tokens import (
    CodexProvider,
    CombinedProvider,
    OpenCodeProvider,
    _opencode_usage,
    common_metrics,
    filter_usage,
    parse_session,
    period_dates,
    report_lines,
    month_period,
    shift_month,
)


class TokenLogTests(unittest.TestCase):
    def write_log(self, rows):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "rollout.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return path

    def test_new_records_are_attributed_to_current_model(self):
        path = self.write_log([
            {"timestamp": "2026-09-01T10:00:00Z", "type": "turn_context", "payload": {"model": "model-a"}},
            {"timestamp": "2026-09-01T10:01:00Z", "type": "token_usage_record", "payload": {"usage": {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 40}}},
            {"timestamp": "2026-09-01T10:02:00Z", "type": "turn_context", "payload": {"model": "model-b"}},
            {"timestamp": "2026-09-01T10:03:00Z", "type": "token_usage_record", "payload": {"usage": {"input_tokens": 50, "output_tokens": 7}}},
        ])
        usage, malformed = parse_session(path)
        self.assertEqual(malformed, 0)
        self.assertEqual(
            [(x.model, x.input_tokens, x.output_tokens) for x in usage],
            [("model-a", 100, 20), ("model-b", 50, 7)],
        )

    def test_direct_records_win_over_duplicate_legacy_events(self):
        path = self.write_log([
            {"timestamp": "2026-09-01T10:00:00Z", "type": "turn_context", "payload": {"model": "m"}},
            {"timestamp": "2026-09-01T10:01:00Z", "type": "token_usage_record", "payload": {"usage": {"input_tokens": 10, "output_tokens": 2}}},
            {"timestamp": "2026-09-01T10:01:01Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 10, "output_tokens": 2}}}},
        ])
        usage, _ = parse_session(path)
        self.assertEqual(len(usage), 1)
        self.assertEqual((usage[0].input_tokens, usage[0].output_tokens), (10, 2))

    def test_legacy_fallback_and_half_open_filter(self):
        path = self.write_log([
            {"timestamp": "2026-09-01T10:00:00Z", "type": "turn_context", "payload": {"model": "old"}},
            {"timestamp": "2026-09-02T00:00:00Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 9, "output_tokens": 1}}}},
        ])
        usage, _ = parse_session(path)
        selected = filter_usage(usage, datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 2, tzinfo=timezone.utc))
        self.assertEqual(selected, [])
        self.assertEqual(usage[0].model, "old")

    def test_provider_scans_agent_location(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        session_dir = root / "sessions" / "2026" / "09" / "01"
        session_dir.mkdir(parents=True)
        (session_dir / "rollout.jsonl").write_text(
            '\n'.join([
                json.dumps({"timestamp": "2026-09-01T10:00:00Z", "type": "turn_context", "payload": {"model": "m"}}),
                json.dumps({"timestamp": "2026-09-01T10:01:00Z", "type": "token_usage_record", "payload": {"usage": {"input_tokens": 5, "output_tokens": 1}}}),
            ]) + '\n',
            encoding="utf-8",
        )
        result = CodexProvider(root).scan()
        self.assertEqual(result.files_scanned, 1)
        self.assertEqual(result.usage[0].input_tokens, 5)

    def test_period_dates_uses_actual_boundaries(self):
        now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        end = datetime(2026, 9, 6, tzinfo=timezone.utc)
        self.assertEqual(period_dates([], start, end, now), "2026-09-01  →  2026-09-05")

    def test_opencode_normalizes_cached_input_and_reasoning_output(self):
        usage = _opencode_usage({
            "role": "assistant",
            "modelID": "model-o",
            "time": {"created": 1_788_000_000_000},
            "tokens": {
                "input": 100,
                "output": 20,
                "reasoning": 7,
                "cache": {"read": 80, "write": 5},
            },
        })
        self.assertIsNotNone(usage)
        self.assertEqual(usage.input_tokens, 185)
        self.assertEqual(usage.cached_input_tokens, 85)
        self.assertEqual(usage.output_tokens, 27)

    def test_opencode_provider_reads_database(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        import sqlite3
        with sqlite3.connect(root / "opencode.db") as connection:
            connection.execute("CREATE TABLE message (data TEXT NOT NULL)")
            connection.execute("INSERT INTO message VALUES (?)", (json.dumps({
                "role": "assistant",
                "modelID": "m",
                "time": {"completed": 1_788_000_000_000},
                "tokens": {"input": 3, "output": 2, "reasoning": 1, "cache": {"read": 4, "write": 0}},
            }),))
        result = OpenCodeProvider(root).scan()
        self.assertEqual(result.files_scanned, 1)
        self.assertEqual((result.usage[0].input_tokens, result.usage[0].output_tokens), (7, 3))

    def test_shared_ui_uses_only_common_metrics(self):
        self.assertEqual(common_metrics(), frozenset({"input", "output"}))

    def test_combined_provider_merges_normalized_usage(self):
        class FakeProvider:
            display_name = "Fake"
            supported_metrics = frozenset({"input", "output"})
            location = Path("fake")

            def __init__(self, amount):
                self.amount = amount

            def scan(self):
                from codex_tokens import ScanResult, Usage
                return ScanResult([
                    Usage(datetime(2026, 9, 1, tzinfo=timezone.utc), "same-model", self.amount, 1)
                ], files_scanned=1)

        result = CombinedProvider((FakeProvider(3), FakeProvider(4))).scan()
        self.assertEqual(sum(row.input_tokens for row in result.usage), 7)
        self.assertEqual(result.files_scanned, 2)

    def test_calendar_month_boundaries(self):
        self.assertEqual(shift_month(date(2026, 1, 1), -1), date(2025, 12, 1))
        start, end = month_period(date(2024, 2, 14), timezone.utc)
        self.assertEqual(start, datetime(2024, 2, 1, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2024, 3, 1, tzinfo=timezone.utc))

    def test_report_can_split_model_usage_by_agent(self):
        from codex_tokens import ScanResult, Usage
        rows = [
            Usage(datetime(2026, 9, 1, tzinfo=timezone.utc), "same", 10, 2, agent="Codex"),
            Usage(datetime(2026, 9, 1, tzinfo=timezone.utc), "same", 20, 3, agent="OpenCode"),
        ]
        provider = CombinedProvider(())
        lines = report_lines(ScanResult(rows), None, None, provider, 90, split_agents=True)
        self.assertIn("Agent", lines[7])
        self.assertTrue(any("Codex" in line and "same" in line for line in lines))
        self.assertTrue(any("OpenCode" in line and "same" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
