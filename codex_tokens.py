#!/usr/bin/env python3
"""Show token usage from local agent logs."""

from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import sqlite3
import sys
import termios
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import ClassVar, Iterable, Protocol


@dataclass(frozen=True)
class Usage:
    timestamp: datetime
    model: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    agent: str = "unknown"

@dataclass
class ScanResult:
    usage: list[Usage]
    files_scanned: int = 0
    malformed_lines: int = 0


class UsageProvider(Protocol):
    """Interface implemented by each local agent usage source."""

    key: ClassVar[str]
    display_name: ClassVar[str]
    supported_metrics: ClassVar[frozenset[str]]
    location: Path | str

    @classmethod
    def default_location(cls) -> Path: ...

    def scan(self) -> ScanResult: ...


def parse_timestamp(value: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _usage_from_payload(timestamp: datetime, model: str, payload: dict, agent: str = "Codex") -> Usage:
    return Usage(
        timestamp=timestamp,
        model=model or "unknown",
        input_tokens=int(payload.get("input_tokens", 0) or 0),
        output_tokens=int(payload.get("output_tokens", 0) or 0),
        cached_input_tokens=int(payload.get("cached_input_tokens", 0) or 0),
        agent=agent,
    )


def parse_session(path: Path, agent: str = "Codex") -> tuple[list[Usage], int]:
    """Parse one rollout, preferring new usage records over duplicate legacy events."""
    model = "unknown"
    direct: list[Usage] = []
    legacy: list[Usage] = []
    malformed = 0

    try:
        lines = path.open("r", encoding="utf-8")
    except OSError:
        return [], 1

    with lines:
        for line in lines:
            try:
                item = json.loads(line)
                payload = item.get("payload") or {}
                item_type = item.get("type")

                if item_type == "turn_context" and payload.get("model"):
                    model = str(payload["model"])
                    continue

                stamp_value = item.get("timestamp")
                if not stamp_value:
                    continue
                stamp = parse_timestamp(stamp_value)

                if item_type == "token_usage_record" and isinstance(payload.get("usage"), dict):
                    direct.append(_usage_from_payload(stamp, model, payload["usage"], agent))
                elif item_type == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    last = info.get("last_token_usage")
                    if isinstance(last, dict):
                        legacy.append(_usage_from_payload(stamp, model, last, agent))
            except (json.JSONDecodeError, TypeError, ValueError):
                malformed += 1

    return (direct if direct else legacy), malformed


def session_files(codex_home: Path) -> list[Path]:
    paths: set[Path] = set()
    for folder in (codex_home / "sessions", codex_home / "archived_sessions"):
        if folder.is_dir():
            paths.update(folder.rglob("*.jsonl"))
    return sorted(paths)


@dataclass
class CodexProvider:
    """Read usage from Codex rollout JSONL files."""

    location: Path
    key: ClassVar[str] = "codex"
    display_name: ClassVar[str] = "Codex"
    supported_metrics: ClassVar[frozenset[str]] = frozenset({"input", "output", "cached_input"})

    @classmethod
    def default_location(cls) -> Path:
        return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))

    def scan(self) -> ScanResult:
        result = ScanResult([])
        for path in session_files(self.location):
            found, malformed = parse_session(path)
            result.usage.extend(found)
            result.files_scanned += 1
            result.malformed_lines += malformed
        return result


def _opencode_usage(data: dict, agent: str = "OpenCode") -> Usage | None:
    if data.get("role") != "assistant" or not isinstance(data.get("tokens"), dict):
        return None
    tokens = data["tokens"]
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    cache_read = int(cache.get("read", 0) or 0)
    cache_write = int(cache.get("write", 0) or 0)
    cached = cache_read + cache_write
    timing = data.get("time") if isinstance(data.get("time"), dict) else {}
    stamp_ms = timing.get("completed") or timing.get("created")
    if stamp_ms is None:
        return None
    # OpenCode records non-cached input, cached input, and reasoning output in
    # separate buckets. Normalize them to the inclusive semantics used by the
    # shared UI and by Codex's input/output counts.
    return Usage(
        timestamp=datetime.fromtimestamp(float(stamp_ms) / 1000, tz=timezone.utc),
        model=str(data.get("modelID") or "unknown"),
        input_tokens=int(tokens.get("input", 0) or 0) + cached,
        output_tokens=int(tokens.get("output", 0) or 0) + int(tokens.get("reasoning", 0) or 0),
        cached_input_tokens=cached,
        agent=agent,
    )


@dataclass
class OpenCodeProvider:
    """Read normalized usage from OpenCode's database or legacy JSON storage."""

    location: Path
    key: ClassVar[str] = "opencode"
    display_name: ClassVar[str] = "OpenCode"
    supported_metrics: ClassVar[frozenset[str]] = frozenset({"input", "output", "cached_input"})

    @classmethod
    def default_location(cls) -> Path:
        data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        return data_home / "opencode"

    def _scan_database(self, database: Path) -> ScanResult:
        result = ScanResult([], files_scanned=1)
        uri = f"{database.resolve().as_uri()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as connection:
                for (raw_data,) in connection.execute("SELECT data FROM message"):
                    try:
                        usage = _opencode_usage(json.loads(raw_data))
                        if usage:
                            result.usage.append(usage)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        result.malformed_lines += 1
        except (OSError, sqlite3.Error):
            result.malformed_lines += 1
        return result

    def _scan_legacy_json(self) -> ScanResult:
        result = ScanResult([])
        message_dir = self.location / "storage" / "message"
        for path in sorted(message_dir.rglob("*.json")) if message_dir.is_dir() else []:
            result.files_scanned += 1
            try:
                usage = _opencode_usage(json.loads(path.read_text(encoding="utf-8")))
                if usage:
                    result.usage.append(usage)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                result.malformed_lines += 1
        return result

    def scan(self) -> ScanResult:
        database = self.location / "opencode.db"
        # Recent OpenCode versions migrate the legacy JSON messages into this
        # database. Reading only one format prevents counting migrated data twice.
        return self._scan_database(database) if database.is_file() else self._scan_legacy_json()


@dataclass
class CombinedProvider:
    """Combine normalized records from several agent providers."""

    providers: tuple[UsageProvider, ...]
    key: ClassVar[str] = "all"
    display_name: ClassVar[str] = "All agents"
    supported_metrics: ClassVar[frozenset[str]] = frozenset({"input", "output", "cached_input"})

    @property
    def location(self) -> str:
        return " + ".join(provider.display_name for provider in self.providers)

    def scan(self) -> ScanResult:
        combined = ScanResult([])
        for provider in self.providers:
            result = provider.scan()
            combined.usage.extend(result.usage)
            combined.files_scanned += result.files_scanned
            combined.malformed_lines += result.malformed_lines
        return combined


# Adding another agent requires only a UsageProvider implementation and one
# registry entry; the filtering, aggregation, CLI, and TUI remain unchanged.
PROVIDERS: dict[str, type[UsageProvider]] = {
    CodexProvider.key: CodexProvider,
    OpenCodeProvider.key: OpenCodeProvider,
}
# Input totals include cached input.  Show the cached portion separately so a
# report contains all three quantities needed to price usage at API rates:
# uncached input (input - cached input), cached input, and output.
DISPLAY_METRICS = frozenset({"input", "output", "cached_input"})


def common_metrics() -> frozenset[str]:
    """Metrics every registered provider can report with comparable semantics."""
    metric_sets = [provider.supported_metrics for provider in PROVIDERS.values()]
    return (frozenset.intersection(*metric_sets) & DISPLAY_METRICS) if metric_sets else frozenset()


def filter_usage(records: Iterable[Usage], start: datetime | None, end: datetime | None) -> list[Usage]:
    return [
        row for row in records
        if (start is None or row.timestamp >= start) and (end is None or row.timestamp < end)
    ]


def parse_boundary(value: str, local_tz, *, inclusive_date_end: bool = False) -> datetime:
    try:
        if len(value) == 10:
            day = date.fromisoformat(value)
            if inclusive_date_end:
                day += timedelta(days=1)
            return datetime.combine(day, time.min, tzinfo=local_tz)
        parsed = parse_timestamp(value)
        return parsed.astimezone(local_tz)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date/time: {value!r}") from exc


PERIODS = ("today", "7d", "30d", "all")
PERIOD_NAMES = {"today": "Today", "7d": "7 days", "30d": "30 days", "all": "All time"}


def preset(name: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    midnight = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    if name == "today":
        return midnight, None
    if name == "7d":
        return midnight - timedelta(days=6), None
    if name == "30d":
        return midnight - timedelta(days=29), None
    return None, None


def shift_month(month: date, amount: int) -> date:
    """Return the first day of the month `amount` months away."""
    absolute = month.year * 12 + month.month - 1 + amount
    return date(absolute // 12, absolute % 12 + 1, 1)


def month_period(month: date, local_tz) -> tuple[datetime, datetime]:
    first = month.replace(day=1)
    following = shift_month(first, 1)
    return (
        datetime.combine(first, time.min, tzinfo=local_tz),
        datetime.combine(following, time.min, tzinfo=local_tz),
    )


def fmt_number(value: int) -> str:
    return f"{value:,}"


def period_dates(
    all_records: list[Usage], start: datetime | None, end: datetime | None, now: datetime
) -> str:
    local_tz = now.tzinfo
    if start:
        first = start.astimezone(local_tz).date()
    elif all_records:
        first = min(row.timestamp for row in all_records).astimezone(local_tz).date()
    else:
        first = None
    # End is exclusive internally. A date boundary therefore represents the
    # preceding calendar day to the user.
    last = (end - timedelta(microseconds=1)).astimezone(local_tz).date() if end else now.date()
    return f"{first.isoformat() if first else '—'}  →  {last.isoformat()}"


def report_data(
    result: ScanResult,
    start: datetime | None,
    end: datetime | None,
    provider: UsageProvider,
) -> dict:
    """Return the filtered report in a JSON-friendly, pricing-ready shape."""
    records = filter_usage(result.usage, start, end)
    totals: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    for row in records:
        bucket = totals[(row.agent, row.model)]
        bucket[0] += row.input_tokens
        bucket[1] += row.output_tokens
        bucket[2] += row.cached_input_tokens

    def token_buckets(values: list[int]) -> dict[str, int]:
        input_tokens, output_tokens, cached_input_tokens = values
        return {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "uncached_input_tokens": input_tokens - cached_input_tokens,
            "output_tokens": output_tokens,
        }

    total_values = [
        sum(values[index] for values in totals.values())
        for index in range(3)
    ]
    return {
        "agent": provider.key,
        "agent_name": provider.display_name,
        "period": {
            "start": start.isoformat() if start else None,
            # Filtering uses an exclusive upper boundary to avoid overlap.
            "end_exclusive": end.isoformat() if end else None,
        },
        "responses": len(records),
        "files_scanned": result.files_scanned,
        "malformed_lines": result.malformed_lines,
        "totals": token_buckets(total_values),
        "models": [
            {"agent": agent, "model": model, **token_buckets(values)}
            for (agent, model), values in sorted(
                totals.items(), key=lambda item: item[1][0], reverse=True
            )
        ],
    }


def report_lines(
    result: ScanResult,
    start: datetime | None,
    end: datetime | None,
    provider: UsageProvider,
    width: int,
    now: datetime | None = None,
    selected_period: str | None = None,
    selected_month: date | None = None,
    split_agents: bool = False,
) -> list[str]:
    now = now or datetime.now().astimezone()
    records = filter_usage(result.usage, start, end)
    totals: dict[str | tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    for row in records:
        key: str | tuple[str, str] = (row.agent, row.model) if split_agents else row.model
        bucket = totals[key]
        bucket[0] += row.input_tokens
        bucket[1] += row.output_tokens
        bucket[2] += row.cached_input_tokens

    metrics = common_metrics()
    all_input = sum(x[0] for x in totals.values())
    all_output = sum(x[1] for x in totals.values())
    metric_columns = [
        ("input", "Input", 0),
        ("output", "Output", 1),
        ("cached_input", "Cached input", 2),
    ]
    visible_columns = [column for column in metric_columns if column[0] in metrics]
    table_width = min(max(width, 62), 100)
    metric_width = 16 * len(visible_columns)
    agent_width = 15 if split_agents else 0
    model_width = max(16, table_width - metric_width - agent_width)
    rule = "─" * table_width
    if selected_month:
        tabs = f"Month:  ←  [{selected_month.strftime('%B %Y')}]  →"
    else:
        tabs = "  ".join(
            f"[{PERIOD_NAMES[key]}]" if key == selected_period else PERIOD_NAMES[key]
            for key in PERIODS
        )
    lines = [
        "Token usage",
        f"Agent: {provider.display_name}    Period: {period_dates(result.usage, start, end, now)}",
    ]
    if selected_period or selected_month:
        lines.append(tabs)
    metric_titles = []
    metric_values = []
    if "input" in metrics:
        metric_titles.append("INPUT TOKENS")
        metric_values.append(fmt_number(all_input))
    if "output" in metrics:
        metric_titles.append("OUTPUT TOKENS")
        metric_values.append(fmt_number(all_output))
    if "cached_input" in metrics:
        metric_titles.append("CACHED INPUT")
        metric_values.append(fmt_number(sum(x[2] for x in totals.values())))
    card_width = max(18, table_width // max(len(metric_titles), 1))
    lines += [
        "",
        "  " + "".join(value.ljust(card_width) for value in metric_titles),
        "  " + "".join(value.ljust(card_width) for value in metric_values),
        "",
        rule,
        (
            f"{'Agent':<14} {'Model':<{model_width}}"
            if split_agents else f"{'Model':<{model_width}}"
        ) + "".join(f" {title:>14}" for _, title, _ in visible_columns),
        rule,
    ]
    sort_index = visible_columns[0][2] if visible_columns else 0
    for key, values in sorted(totals.items(), key=lambda item: item[1][sort_index], reverse=True):
        agent, model = key if split_agents else ("", key)
        assert isinstance(model, str)
        short_model = model if len(model) <= model_width else model[: model_width - 1] + "…"
        prefix = f"{agent:<14} {short_model:<{model_width}}" if split_agents else f"{short_model:<{model_width}}"
        lines.append(prefix + "".join(
            f" {fmt_number(values[index]):>14}" for _, _, index in visible_columns
        ))
    if not totals:
        lines.append("No token usage found in this period.")
    file_label = "data file" if result.files_scanned == 1 else "data files"
    lines.extend([
        rule,
        f"{len(records):,} responses · {result.files_scanned:,} {file_label} · {provider.location}",
    ])
    if result.malformed_lines:
        lines.append(f"Warning: skipped {result.malformed_lines:,} malformed/unreadable lines")
    return lines


def draw(lines: list[str], previous_height: int) -> int:
    if previous_height:
        # The cursor is still on the final rendered line (there is no trailing
        # newline), so the first line is previous_height - 1 rows above it.
        if previous_height > 1:
            sys.stdout.write(f"\x1b[{previous_height - 1}F")
        else:
            sys.stdout.write("\r")
    for index, line in enumerate(lines):
        sys.stdout.write("\x1b[2K" + line)
        if index < len(lines) - 1:
            sys.stdout.write("\n")
    if previous_height > len(lines):
        for _ in range(previous_height - len(lines)):
            sys.stdout.write("\n\x1b[2K")
        sys.stdout.write(f"\x1b[{previous_height - len(lines)}F")
    sys.stdout.flush()
    return len(lines)


def read_key() -> str:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~(termios.ICANON | termios.ECHO)
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
        first = os.read(fd, 1)
        if first != b"\x1b":
            return first.decode(errors="ignore")
        sequence = first
        # Arrow keys arrive as a short escape sequence (ESC [ C / ESC [ D).
        while len(sequence) < 3 and select.select([fd], [], [], 0.05)[0]:
            sequence += os.read(fd, 1)
        return {b"\x1b[C": "right", b"\x1b[D": "left"}.get(sequence, "escape")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def custom_period(local_tz) -> tuple[datetime | None, datetime | None] | None:
    print("\nEnter dates as YYYY-MM-DD. Leave the end blank for now; 'to' is inclusive.")
    since = input("From: ").strip()
    until = input("To:   ").strip()
    try:
        start = parse_boundary(since, local_tz) if since else None
        end = parse_boundary(until, local_tz, inclusive_date_end=True) if until else None
    except argparse.ArgumentTypeError as exc:
        print(exc)
        input("Press Enter to continue...")
        return None
    if start and end and start >= end:
        print("The start must be before the end.")
        input("Press Enter to continue...")
        return None
    return start, end


def run_ui(providers: dict[str, UsageProvider], initial_agent: str, initial_period: str) -> int:
    local_tz = datetime.now().astimezone().tzinfo
    now = datetime.now(local_tz)
    period_index = PERIODS.index(initial_period)
    return_period_index = period_index
    start, end = preset(initial_period, now)
    selected_month: date | None = None
    split_agents = False
    agent_keys = list(providers)
    agent_index = agent_keys.index(initial_agent)
    results: dict[str, ScanResult] = {}
    height = 0
    exported_data: dict | None = None
    sys.stdout.write("\x1b[?25l")
    try:
        while True:
            agent_key = agent_keys[agent_index]
            provider = providers[agent_key]
            if agent_key not in results:
                results[agent_key] = provider.scan()
            result = results[agent_key]
            width = shutil.get_terminal_size((90, 24)).columns
            selected = PERIODS[period_index] if period_index is not None else None
            show_split = split_agents and provider.key == CombinedProvider.key
            lines = report_lines(
                result, start, end, provider, width,
                selected_period=selected, selected_month=selected_month, split_agents=show_split,
            )
            if selected_month:
                controls = "[←/→] Month    [m] Exit month mode    [j] JSON    [s] Split agents    [Tab] Agent    [q] Quit"
            else:
                controls = "[←/→] Period    [m] Browse months    [j] JSON    [s] Split agents    [Tab] Agent    [c] Custom    [q] Quit"
            lines += ["", controls]
            height = draw(lines, height)
            key = read_key().lower()
            if key in ("q", "\x03", "\x04"):
                break
            if key == "j":
                exported_data = report_data(result, start, end, provider)
                break
            if key == "\t":
                agent_index = (agent_index + 1) % len(agent_keys)
            elif key == "s" and provider.key == CombinedProvider.key:
                split_agents = not split_agents
            if key in ("left", "right"):
                step = -1 if key == "left" else 1
                if selected_month:
                    candidate = shift_month(selected_month, step)
                    current_month = datetime.now(local_tz).date().replace(day=1)
                    selected_month = min(candidate, current_month)
                    start, end = month_period(selected_month, local_tz)
                else:
                    period_index = ((period_index if period_index is not None else 0) + step) % len(PERIODS)
                    start, end = preset(PERIODS[period_index], datetime.now(local_tz))
            elif key == "m":
                if selected_month:
                    selected_month = None
                    period_index = return_period_index
                    start, end = preset(PERIODS[period_index], datetime.now(local_tz))
                else:
                    return_period_index = period_index if period_index is not None else PERIODS.index(initial_period)
                    selected_month = datetime.now(local_tz).date().replace(day=1)
                    period_index = None
                    start, end = month_period(selected_month, local_tz)
            elif key == "c":
                sys.stdout.write("\x1b[?25h")
                sys.stdout.flush()
                chosen = custom_period(local_tz)
                sys.stdout.write("\x1b[?25l")
                if chosen:
                    start, end = chosen
                    period_index = None
                    selected_month = None
                height = 0
    finally:
        sys.stdout.write("\x1b[?25h\n")
        sys.stdout.flush()
    if exported_data is not None:
        print(json.dumps(exported_data, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=(CombinedProvider.key, *PROVIDERS), default=CombinedProvider.key)
    parser.add_argument("--data-dir", "--codex-home", dest="data_dir", type=Path, help="agent data directory")
    parser.add_argument("--period", choices=PERIODS, default="7d")
    parser.add_argument("--since", help="start date (YYYY-MM-DD) or ISO timestamp")
    parser.add_argument("--until", help="inclusive end date (YYYY-MM-DD) or ISO timestamp")
    parser.add_argument("--no-ui", action="store_true", help="print once instead of opening the interactive UI")
    parser.add_argument("--json", action="store_true", help="print a pricing-ready JSON report")
    args = parser.parse_args(argv)

    local_tz = datetime.now().astimezone().tzinfo
    if args.since or args.until:
        try:
            start = parse_boundary(args.since, local_tz) if args.since else None
            end = parse_boundary(args.until, local_tz, inclusive_date_end=True) if args.until else None
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
        if start and end and start >= end:
            parser.error("--since must be before --until")
    else:
        start, end = preset(args.period, datetime.now(local_tz))

    if args.agent == CombinedProvider.key and args.data_dir:
        parser.error("--data-dir requires a specific --agent")
    individual_providers: dict[str, UsageProvider] = {
        key: provider_class(
            args.data_dir if key == args.agent and args.data_dir else provider_class.default_location()
        )
        for key, provider_class in PROVIDERS.items()
    }
    available_providers = {
        key: item for key, item in individual_providers.items() if Path(item.location).is_dir()
    }
    if args.agent == CombinedProvider.key:
        if not available_providers:
            parser.error("No supported agent data directories were found")
    elif args.agent not in available_providers:
        parser.error(f"Agent data directory does not exist: {individual_providers[args.agent].location}")
    combined = CombinedProvider(tuple(available_providers.values()))
    provider = combined if args.agent == CombinedProvider.key else available_providers[args.agent]

    if sys.stdin.isatty() and sys.stdout.isatty() and not args.no_ui and not args.json and not (args.since or args.until):
        providers = {CombinedProvider.key: combined, **available_providers}
        return run_ui(providers, args.agent, args.period)

    result = provider.scan()
    if args.json:
        print(json.dumps(report_data(result, start, end, provider, ), indent=2))
        return 0
    print("\n".join(report_lines(result, start, end, provider, shutil.get_terminal_size((90, 24)).columns)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
