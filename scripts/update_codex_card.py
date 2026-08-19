#!/usr/bin/env python3
"""Aggregate Codex Tempo traces and render a GitHub profile SVG card."""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo


TRACE_QUERY = (
    '{ resource.service.name = "codex-app-server" '
    '&& name = "session_task.turn" }'
)
TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "non_cached_input_tokens",
    "reasoning_output_tokens",
    "cache_write_input_tokens",
    "total_tokens",
)
KST = ZoneInfo("Asia/Seoul")
DEFAULT_STATE = {
    "schema_version": 1,
    "baseline": {
        "total_tokens": 0,
        "max_daily_tokens": 0,
        "longest_streak": 0,
        "active_dates": [],
    },
    "days": {},
    "seen_turns": {},
    "last_updated": None,
}


class TempoClient:
    def __init__(self, base_url: str, user: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        encoded = base64.b64encode(f"{user}:{token}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json",
            "User-Agent": "codex-profile-card/1.0",
        }

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        last_error: Exception | None = None
        for attempt in range(4):
            request = urllib.request.Request(url, headers=self.headers)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code not in {429, 500, 502, 503, 504}:
                    raise
            except (TimeoutError, urllib.error.URLError) as error:
                last_error = error

            if attempt < 3:
                time.sleep(2**attempt)

        assert last_error is not None
        raise last_error

    def search(self, start: int, end: int, limit: int = 1000) -> list[dict[str, Any]]:
        payload = self.get_json(
            "/api/search",
            {
                "q": TRACE_QUERY,
                "start": start,
                "end": end,
                "limit": limit,
                "spss": 10,
            },
        )
        traces = payload.get("traces", [])
        if len(traces) >= limit:
            raise RuntimeError(
                f"Tempo returned the search limit ({limit}) for {start}..{end}; "
                "reduce SEARCH_CHUNK_HOURS to avoid undercounting."
            )
        return traces

    def trace(self, trace_id: str) -> Any:
        try:
            return self.get_json(f"/api/v2/traces/{trace_id}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            return self.get_json(f"/api/traces/{trace_id}")


def attribute_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in (
        "stringValue",
        "intValue",
        "doubleValue",
        "boolValue",
        "bytesValue",
    ):
        if key in value:
            raw = value[key]
            if key == "intValue":
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return 0
            return raw
    return value


def attributes_to_dict(attributes: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not isinstance(attributes, list):
        return result
    for item in attributes:
        if not isinstance(item, dict) or "key" not in item:
            continue
        result[str(item["key"])] = attribute_value(item.get("value"))
    return result


def walk_dicts(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk_dicts(value)


def parse_turns(trace_payload: Any, fallback_start_ns: int = 0) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    seen: set[str] = set()

    for span in walk_dicts(trace_payload):
        if span.get("name") != "session_task.turn":
            continue
        attributes = attributes_to_dict(span.get("attributes"))
        turn_id = str(attributes.get("turn.id", ""))
        if not turn_id or turn_id in seen:
            continue

        tokens: dict[str, int] = {}
        for key in TOKEN_KEYS:
            raw = attributes.get(f"codex.turn.token_usage.{key}", 0)
            try:
                tokens[key] = max(0, int(raw))
            except (TypeError, ValueError):
                tokens[key] = 0

        if not tokens["total_tokens"]:
            continue

        start_ns = span.get("startTimeUnixNano", fallback_start_ns)
        try:
            start_ns = int(start_ns)
        except (TypeError, ValueError):
            start_ns = fallback_start_ns

        seen.add(turn_id)
        turns.append(
            {
                "turn_id": turn_id,
                "start_ns": start_ns,
                "model": str(attributes.get("model", "unknown")),
                "tokens": tokens,
            }
        )
    return turns


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_STATE))
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema_version") != 1:
        raise ValueError("Unsupported codex stats schema version")
    state.setdefault("days", {})
    state.setdefault("seen_turns", {})
    baseline = state.setdefault("baseline", {})
    baseline.setdefault("total_tokens", 0)
    baseline.setdefault("max_daily_tokens", 0)
    baseline.setdefault("longest_streak", 0)
    baseline.setdefault("active_dates", [])
    return state


def add_turn(state: dict[str, Any], turn: dict[str, Any]) -> bool:
    turn_id = turn["turn_id"]
    if turn_id in state["seen_turns"]:
        return False

    timestamp = datetime.fromtimestamp(turn["start_ns"] / 1_000_000_000, tz=timezone.utc)
    day_key = timestamp.astimezone(KST).date().isoformat()
    day = state["days"].setdefault(
        day_key,
        {
            "turns": 0,
            "models": {},
            **{key: 0 for key in TOKEN_KEYS},
        },
    )
    day["turns"] += 1
    for key in TOKEN_KEYS:
        day[key] = int(day.get(key, 0)) + int(turn["tokens"].get(key, 0))
    model = turn["model"]
    day["models"][model] = int(day["models"].get(model, 0)) + 1
    state["seen_turns"][turn_id] = day_key
    return True


def prune_seen_turns(state: dict[str, Any], today: date, keep_days: int = 30) -> None:
    cutoff = today - timedelta(days=keep_days)
    state["seen_turns"] = {
        turn_id: day_key
        for turn_id, day_key in state["seen_turns"].items()
        if date.fromisoformat(day_key) >= cutoff
    }


def collect(client: TempoClient, state: dict[str, Any], now: datetime) -> int:
    initial_days = int(os.getenv("INITIAL_LOOKBACK_DAYS", "14"))
    overlap_hours = int(os.getenv("OVERLAP_HOURS", "3"))
    chunk_hours = int(os.getenv("SEARCH_CHUNK_HOURS", "6"))
    oldest_available = now - timedelta(days=initial_days)
    if state["days"] and state.get("last_updated"):
        previous = datetime.fromisoformat(state["last_updated"]).astimezone(timezone.utc)
        start_at = max(oldest_available, previous - timedelta(hours=overlap_hours))
    else:
        start_at = oldest_available
    trace_summaries: dict[str, dict[str, Any]] = {}

    cursor = start_at
    while cursor < now:
        chunk_end = min(cursor + timedelta(hours=chunk_hours), now)
        traces = client.search(int(cursor.timestamp()), int(chunk_end.timestamp()))
        for trace in traces:
            trace_id = trace.get("traceID")
            if trace_id:
                trace_summaries[str(trace_id)] = trace
        cursor = chunk_end

    added = 0
    for index, (trace_id, summary) in enumerate(sorted(trace_summaries.items()), 1):
        print(f"Reading trace {index}/{len(trace_summaries)}: {trace_id}")
        try:
            payload = client.trace(trace_id)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                print(f"Skipping unavailable trace {trace_id}", file=sys.stderr)
                continue
            raise
        fallback_start_ns = int(summary.get("startTimeUnixNano", 0) or 0)
        for turn in parse_turns(payload, fallback_start_ns):
            added += int(add_turn(state, turn))
    return added


def active_dates(state: dict[str, Any]) -> set[date]:
    collected = {
        date.fromisoformat(day_key)
        for day_key, values in state["days"].items()
        if int(values.get("turns", 0)) > 0
    }
    baseline = {
        date.fromisoformat(day_key)
        for day_key in state.get("baseline", {}).get("active_dates", [])
    }
    return collected | baseline


def streaks(days: set[date], today: date) -> tuple[int, int]:
    if not days:
        return 0, 0

    longest = 0
    run = 0
    previous: date | None = None
    for current in sorted(days):
        run = run + 1 if previous and current == previous + timedelta(days=1) else 1
        longest = max(longest, run)
        previous = current

    end = today if today in days else today - timedelta(days=1)
    current_streak = 0
    while end in days:
        current_streak += 1
        end -= timedelta(days=1)
    return current_streak, longest


def compact_number(value: int) -> str:
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}억"
    if value >= 10_000:
        return f"{value / 10_000:.1f}만"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def color_level(value: int, maximum: int) -> int:
    if value <= 0 or maximum <= 0:
        return 0
    ratio = math.log1p(value) / math.log1p(maximum)
    if ratio < 0.35:
        return 1
    if ratio < 0.55:
        return 2
    if ratio < 0.75:
        return 3
    return 4


def primary_model(models: Counter[str]) -> str:
    user_models = Counter(
        {
            model: count
            for model, count in models.items()
            if model
            and model.lower() not in {"unknown", "none"}
            and not model.lower().startswith("codex-auto-")
        }
    )
    return user_models.most_common(1)[0][0] if user_models else "No user model yet"


def display_model_name(model: str, max_length: int = 26) -> str:
    if len(model) <= max_length:
        return model
    return f"{model[: max_length - 1]}…"


def render_svg(state: dict[str, Any], today: date) -> str:
    day_values = {
        date.fromisoformat(key): int(value.get("total_tokens", 0))
        for key, value in state["days"].items()
    }
    baseline = state.get("baseline", {})
    total_tokens = int(baseline.get("total_tokens", 0)) + sum(day_values.values())
    max_daily = max(
        int(baseline.get("max_daily_tokens", 0)),
        max(day_values.values(), default=0),
    )
    current_streak, collected_longest_streak = streaks(active_dates(state), today)
    longest_streak = max(
        int(baseline.get("longest_streak", 0)), collected_longest_streak
    )

    models: Counter[str] = Counter()
    for values in state["days"].values():
        models.update({key: int(value) for key, value in values.get("models", {}).items()})
    top_model = primary_model(models)
    top_model_display = display_model_name(top_model)

    weeks = 52
    cell = 9
    gap = 4
    grid_x = 164
    grid_y = 210
    end_of_week = today + timedelta(days=(5 - today.weekday()) % 7)
    grid_start = end_of_week - timedelta(days=weeks * 7 - 1)
    colors = ["#263241", "#173f5f", "#20679a", "#2f9bd3", "#6dd5ed"]
    cells: list[str] = []
    month_nodes: list[str] = []
    previous_month = -1
    for week in range(weeks):
        week_start = grid_start + timedelta(days=week * 7)
        if week_start.month != previous_month:
            month_nodes.append(
                f'<text x="{grid_x + week * (cell + gap)}" y="194" '
                f'class="month">{week_start.strftime("%b")}</text>'
            )
            previous_month = week_start.month
        for weekday in range(7):
            current = grid_start + timedelta(days=week * 7 + weekday)
            value = day_values.get(current, 0) if current <= today else 0
            level = color_level(value, max_daily)
            opacity = "0.35" if current > today else "1"
            x = grid_x + week * (cell + gap)
            y = grid_y + weekday * (cell + gap)
            cell_class = "activity-cell today-cell" if current == today else "activity-cell"
            cells.append(
                f'<rect class="{cell_class}" x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2.5" '
                f'fill="{colors[level]}" opacity="{opacity}"><title>'
                f'{escape(current.isoformat())}: {value:,} tokens</title></rect>'
            )

    today_tokens = day_values.get(today, 0)
    stats = (
        (compact_number(max_daily), "최다 사용일"),
        (f"{current_streak}일", "현재 연속"),
        (f"{longest_streak}일", "최장 연속"),
    )
    stat_nodes: list[str] = []
    for index, (value, label) in enumerate(stats):
        x = 175 + index * 285
        stat_nodes.append(
            f'<text x="{x}" y="331" text-anchor="middle" class="stat-value">'
            f'{escape(value)}</text><text x="{x}" y="352" text-anchor="middle" '
            f'class="stat-label">{escape(label)}</text>'
        )
        if index < 2:
            divider_x = 317 + index * 285
            stat_nodes.append(
                f'<line x1="{divider_x}" y1="311" x2="{divider_x}" y2="347" '
                'stroke="#27384a"/>'
            )

    return f'''<svg width="920" height="365" viewBox="0 0 920 365" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title desc">
  <title id="title">Se Min Kong Codex usage</title>
  <desc id="desc">Codex token usage, primary model, activity heatmap, and streaks.</desc>
  <style>
    .eyebrow {{ font: 700 10px 'Segoe UI', Arial, sans-serif; fill: #6e8499; letter-spacing: 1.5px; }}
    .title {{ font: 700 22px 'Segoe UI', Arial, sans-serif; fill: #f0f6fc; letter-spacing: -.35px; }}
    .handle {{ font: 600 12px 'Segoe UI', Arial, sans-serif; fill: #8b9eb0; }}
    .total {{ font: 800 45px 'Segoe UI', Arial, sans-serif; fill: #f0f6fc; letter-spacing: -2px; }}
    .total-label {{ font: 600 11px 'Noto Sans KR', 'Segoe UI', sans-serif; fill: #8b9eb0; }}
    .panel-label {{ font: 700 9px 'Segoe UI', Arial, sans-serif; fill: #71879b; letter-spacing: 1.25px; }}
    .model-value {{ font: 700 15px 'Segoe UI', Arial, sans-serif; fill: #dcecf8; }}
    .today-value {{ font: 700 18px 'Noto Sans KR', 'Segoe UI', sans-serif; fill: #dcecf8; }}
    .today-label {{ font: 500 10px 'Noto Sans KR', 'Segoe UI', sans-serif; fill: #758da2; }}
    .month {{ font: 600 10px 'Segoe UI', Arial, sans-serif; fill: #71869a; }}
    .weekday {{ font: 500 10px 'Segoe UI', Arial, sans-serif; fill: #667c90; }}
    .stat-value {{ font: 700 18px 'Noto Sans KR', 'Segoe UI', sans-serif; fill: #e6edf3; }}
    .stat-label {{ font: 500 11px 'Noto Sans KR', 'Segoe UI', sans-serif; fill: #71869a; }}
  </style>
  <rect x="1" y="1" width="918" height="363" rx="16" fill="#0d1117" stroke="#30363d" stroke-width="2"/>
  <path d="M18 1H902A17 17 0 0 1 919 18" fill="none" stroke="#58a6ff" stroke-opacity=".42"/>
  <text x="30" y="28" class="eyebrow">DEVELOPER TELEMETRY</text>
  <text x="30" y="51" class="title">Codex Usage</text>
  <text x="890" y="43" text-anchor="end" class="handle">@SeMinKong</text>

  <!-- information split: 326px / 520px -->
  <g transform="translate(30 67)">
    <rect width="326" height="98" rx="10" fill="#161b22" stroke="#30363d"/>
    <text x="18" y="23" class="panel-label">TOTAL TOKENS</text>
    <text x="18" y="68" class="total">{escape(compact_number(total_tokens))}</text>
    <text x="20" y="86" class="total-label">Codex 전체 사용량</text>
  </g>
  <g transform="translate(370 67)">
    <rect width="520" height="98" rx="10" fill="#161b22" stroke="#30363d"/>
    <text x="20" y="23" class="panel-label">PRIMARY MODEL</text>
    <circle cx="23" cy="47" r="4" fill="#58a6ff"/>
    <text x="36" y="53" class="model-value"><title>{escape(top_model)}</title>{escape(top_model_display)}</text>
    <line x1="310" y1="17" x2="310" y2="81" stroke="#30363d"/>
    <text x="334" y="23" class="panel-label">TODAY</text>
    <text x="334" y="54" class="today-value">{escape(compact_number(today_tokens))}</text>
    <text x="334" y="76" class="today-label">오늘 기록된 토큰</text>
  </g>

  <line x1="30" y1="181" x2="890" y2="181" stroke="#30363d"/>
  <text x="30" y="194" class="panel-label">ACTIVITY</text>
  {''.join(month_nodes)}
  <text x="148" y="218" text-anchor="end" class="weekday">Mon</text><text x="148" y="244" text-anchor="end" class="weekday">Wed</text><text x="148" y="270" text-anchor="end" class="weekday">Fri</text>
  {''.join(cells)}
  <line x1="30" y1="303" x2="890" y2="303" stroke="#30363d"/>
  {''.join(stat_nodes)}
</svg>
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=Path("profile/codex-stats.json"))
    parser.add_argument("--output", type=Path, default=Path("profile/codex-card.svg"))
    parser.add_argument("--render-only", action="store_true")
    args = parser.parse_args()

    state = load_state(args.state)
    now = datetime.now(timezone.utc)
    today = now.astimezone(KST).date()

    if not args.render_only:
        required = ("GRAFANA_TEMPO_URL", "GRAFANA_TEMPO_USER", "GRAFANA_TEMPO_TOKEN")
        missing = [key for key in required if not os.getenv(key)]
        if missing:
            raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")
        client = TempoClient(*(os.environ[key] for key in required))
        added = collect(client, state, now)
        print(f"Added {added} new Codex turns")

    prune_seen_turns(state, today)
    state["last_updated"] = now.astimezone(KST).isoformat(timespec="seconds")
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output.write_text(render_svg(state, today), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
