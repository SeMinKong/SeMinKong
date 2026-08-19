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
    top_model = models.most_common(1)[0][0] if models else "No activity yet"

    weeks = 52
    cell = 10
    gap = 3
    grid_x = 122
    grid_y = 211
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
                f'<text x="{grid_x + week * (cell + gap)}" y="195" '
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
        (compact_number(today_tokens), "오늘 토큰"),
    )
    stat_nodes: list[str] = []
    for index, (value, label) in enumerate(stats):
        x = 132 + index * 220
        stat_nodes.append(
            f'<text x="{x}" y="344" text-anchor="middle" class="stat-value">'
            f'{escape(value)}</text><text x="{x}" y="361" text-anchor="middle" '
            f'class="stat-label">{escape(label)}</text>'
        )
        if index < 3:
            divider_x = 242 + index * 220
            stat_nodes.append(
                f'<line x1="{divider_x}" y1="326" x2="{divider_x}" y2="361" '
                'stroke="#2c4056"/>'
            )

    updated = escape(str(state.get("last_updated") or ""))
    return f'''<svg width="920" height="375" viewBox="0 0 920 375" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="title desc">
  <title id="title">Se Min Kong Codex activity</title>
  <desc id="desc">Animated AI-themed Codex token usage, model, and activity streaks.</desc>
  <style>
    .handle {{ font: 700 19px 'Segoe UI', Arial, sans-serif; fill: #f4fbff; }}
    .streak {{ font: 600 12px 'Segoe UI', Arial, sans-serif; fill: #8be9ff; }}
    .total {{ font: 800 48px 'Segoe UI', Arial, sans-serif; fill: #8cecff; letter-spacing: -2px; }}
    .total-label {{ font: 600 12px 'Noto Sans KR', 'Segoe UI', sans-serif; fill: #91aac1; }}
    .model-label {{ font: 700 9px 'Segoe UI', Arial, sans-serif; fill: #6385a3; letter-spacing: 1px; }}
    .model-value {{ font: 700 12px 'Segoe UI', Arial, sans-serif; fill: #d7f7ff; }}
    .scene-label {{ font: 700 12px 'Segoe UI', Arial, sans-serif; fill: #d9f7ff; letter-spacing: 1.4px; }}
    .scene-sub {{ font: 500 9px 'Segoe UI', Arial, sans-serif; fill: #6f9bb8; letter-spacing: .8px; }}
    .month {{ font: 600 11px 'Segoe UI', Arial, sans-serif; fill: #8298ad; }}
    .weekday {{ font: 500 10px 'Segoe UI', Arial, sans-serif; fill: #6f8498; }}
    .stat-value {{ font: 700 17px 'Noto Sans KR', 'Segoe UI', sans-serif; fill: #e7f5ff; }}
    .stat-label {{ font: 500 10px 'Noto Sans KR', 'Segoe UI', sans-serif; fill: #7890a6; }}
    .updated {{ font: 500 9px 'Segoe UI', Arial, sans-serif; fill: #587087; }}
    .neural-flow {{ stroke-dasharray: 3 8; animation: dataFlow 3s linear infinite; }}
    .node-core {{ animation: nodePulse 2.4s ease-in-out infinite; transform-box: fill-box; transform-origin: center; }}
    .node-delay {{ animation-delay: -1.2s; }}
    .bot-float {{ animation: botFloat 3.6s ease-in-out infinite; transform-box: fill-box; transform-origin: center; }}
    .bot-eyes {{ animation: blink 4.8s ease-in-out infinite; transform-box: fill-box; transform-origin: center; }}
    .coin-a {{ animation: coinFloat 2.8s ease-in-out infinite; transform-box: fill-box; transform-origin: center; }}
    .coin-b {{ animation: coinFloat 2.8s ease-in-out -1.4s infinite; transform-box: fill-box; transform-origin: center; }}
    .scan-line {{ animation: scan 4s ease-in-out infinite; }}
    .energy-bar {{ animation: energy 3.2s ease-in-out infinite; }}
    .today-cell {{ animation: grassPulse 2s ease-in-out infinite; transform-box: fill-box; transform-origin: center; }}
    @keyframes dataFlow {{ to {{ stroke-dashoffset: -44; }} }}
    @keyframes nodePulse {{ 0%,100% {{ opacity:.45; transform:scale(.82); }} 50% {{ opacity:1; transform:scale(1.22); }} }}
    @keyframes botFloat {{ 0%,100% {{ transform:translateY(0); }} 50% {{ transform:translateY(-6px); }} }}
    @keyframes blink {{ 0%,46%,50%,100% {{ transform:scaleY(1); }} 48% {{ transform:scaleY(.12); }} }}
    @keyframes coinFloat {{ 0%,100% {{ transform:translateY(0) rotate(0deg); }} 50% {{ transform:translateY(-5px) rotate(8deg); }} }}
    @keyframes scan {{ 0%,100% {{ transform:translateX(0); opacity:0; }} 15% {{ opacity:.75; }} 85% {{ opacity:.75; }} 100% {{ transform:translateX(462px); opacity:0; }} }}
    @keyframes energy {{ 0%,100% {{ opacity:.55; }} 50% {{ opacity:1; }} }}
    @keyframes grassPulse {{ 0%,100% {{ opacity:.75; transform:scale(.9); }} 50% {{ opacity:1; transform:scale(1.18); }} }}
    @media (prefers-reduced-motion: reduce) {{
      .neural-flow,.node-core,.bot-float,.bot-eyes,.coin-a,.coin-b,.scan-line,.energy-bar,.today-cell {{ animation:none; }}
    }}
  </style>
  <defs>
    <linearGradient id="cardBg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0f1b2b"/><stop offset=".55" stop-color="#0a1422"/><stop offset="1" stop-color="#080e17"/>
    </linearGradient>
    <linearGradient id="aiPanel" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#102d4b"/><stop offset=".55" stop-color="#122841"/><stop offset="1" stop-color="#112135"/>
    </linearGradient>
    <linearGradient id="cyanLine" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#36d7ff" stop-opacity="0"/><stop offset=".5" stop-color="#82f4ff"/><stop offset="1" stop-color="#7b61ff" stop-opacity="0"/>
    </linearGradient>
    <radialGradient id="orb"><stop offset="0" stop-color="#a8f6ff"/><stop offset=".35" stop-color="#36d7ff"/><stop offset="1" stop-color="#16719d"/></radialGradient>
    <pattern id="circuitGrid" width="22" height="22" patternUnits="userSpaceOnUse">
      <path d="M22 0H0V22" fill="none" stroke="#4ecff2" stroke-opacity=".08" stroke-width="1"/>
      <circle cx="0" cy="0" r="1.3" fill="#71e6ff" fill-opacity=".18"/>
    </pattern>
    <filter id="glow" x="-80%" y="-80%" width="260%" height="260%">
      <feGaussianBlur stdDeviation="3" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <filter id="softGlow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="1.8" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
  </defs>
  <rect x="2" y="2" width="916" height="371" rx="20" fill="url(#cardBg)" stroke="#29435a" stroke-width="2"/>
  <path d="M22 2H898A20 20 0 0 1 918 22" fill="none" stroke="#5ce1ff" stroke-opacity=".28"/>

  <!-- Golden-ratio summary region: 322px / 522px -->
  <g transform="translate(30 18)">
    <rect width="322" height="137" rx="16" fill="#0e2033" fill-opacity=".86" stroke="#28465f"/>
    <path d="M16 1H306" stroke="url(#cyanLine)" stroke-width="2"/>
    <text x="18" y="29" class="handle">@SeMinKong</text>
    <rect x="190" y="11" width="113" height="27" rx="13.5" fill="#123a52" stroke="#2a718f"/>
    <circle class="node-core" cx="207" cy="24.5" r="5" fill="url(#orb)" filter="url(#softGlow)"/>
    <text x="220" y="29" class="streak">{current_streak}-day streak</text>
    <text x="18" y="87" class="total">{escape(compact_number(total_tokens))}</text>
    <text x="20" y="108" class="total-label">누적 토큰 · 실시간 통합</text>
    <rect x="18" y="117" width="286" height="1" fill="#28425a"/>
    <text x="18" y="131" class="model-label">PRIMARY MODEL</text>
    <rect x="112" y="120" width="192" height="13" rx="6.5" fill="#142b42" stroke="#274b65"/>
    <circle cx="124" cy="126.5" r="3" fill="#8cf3ff" filter="url(#softGlow)"/>
    <text x="133" y="131" class="model-value">{escape(top_model)}</text>
  </g>

  <g transform="translate(368 18)">
    <rect width="522" height="137" rx="16" fill="url(#aiPanel)" stroke="#315c79"/>
    <rect width="522" height="137" rx="16" fill="url(#circuitGrid)"/>
    <text x="20" y="25" class="scene-label">NEURAL TOKEN MATRIX</text>
    <text x="20" y="40" class="scene-sub">LIVE CODEX TELEMETRY</text>
    <!-- animated neural graph -->
    <g fill="none" stroke="#48d9ff" stroke-opacity=".38" stroke-width="1.5">
      <path class="neural-flow" d="M32 91C92 45 123 116 183 75S276 41 333 79 420 113 492 65"/>
      <path class="neural-flow" style="animation-delay:-1.5s" d="M53 57C115 98 160 42 219 82S334 112 389 55 451 50 503 88"/>
    </g>
    <g filter="url(#glow)">
      <circle class="node-core" cx="54" cy="83" r="5" fill="url(#orb)"/><circle class="node-core node-delay" cx="145" cy="69" r="4" fill="url(#orb)"/>
      <circle class="node-core" cx="210" cy="88" r="5" fill="url(#orb)"/><circle class="node-core node-delay" cx="342" cy="81" r="4" fill="url(#orb)"/>
      <circle class="node-core" cx="438" cy="72" r="5" fill="url(#orb)"/><circle class="node-core node-delay" cx="492" cy="88" r="4" fill="url(#orb)"/>
    </g>
    <rect class="scan-line" x="18" y="48" width="2" height="72" fill="#91f4ff" opacity=".7" filter="url(#glow)"/>
    <!-- token orbs -->
    <g class="coin-a" transform="translate(105 86)"><circle r="12" fill="#173c59" stroke="#44dfff" stroke-width="2"/><path d="M-4-4h8v8h-8z" fill="#9cf5ff"/><circle r="4" fill="#42cfee"/></g>
    <g class="coin-b" transform="translate(414 95)"><circle r="12" fill="#242456" stroke="#9a87ff" stroke-width="2"/><path d="M0-6l6 6-6 6-6-6z" fill="#c4b9ff"/></g>
    <!-- floating Codex core -->
    <g transform="translate(229 40)"><g class="bot-float">
      <circle cx="32" cy="40" r="38" fill="#102f4a" stroke="#5ce5ff" stroke-opacity=".45"/>
      <circle cx="32" cy="40" r="30" fill="none" stroke="#866dff" stroke-opacity=".35" stroke-dasharray="3 6"/>
      <rect x="13" y="18" width="38" height="32" rx="10" fill="#dff9ff"/>
      <rect x="18" y="24" width="28" height="16" rx="5" fill="#12344f"/>
      <g class="bot-eyes"><rect x="23" y="29" width="5" height="5" rx="1" fill="#68e8ff"/><rect x="36" y="29" width="5" height="5" rx="1" fill="#a593ff"/></g>
      <rect x="22" y="50" width="20" height="20" rx="6" fill="#b9eff9"/>
      <path d="M27 57l5 4-5 4M37 57l-5 4 5 4" fill="none" stroke="#205d80" stroke-width="3" stroke-linecap="round"/>
      <circle cx="8" cy="34" r="5" fill="#5ce1ff"/><circle cx="56" cy="34" r="5" fill="#9988ff"/>
    </g></g>
    <rect x="20" y="122" width="482" height="3" rx="1.5" fill="#173b55"/>
    <rect class="energy-bar" x="20" y="122" width="298" height="3" rx="1.5" fill="url(#cyanLine)" filter="url(#softGlow)"/>
  </g>

  <line x1="30" y1="170" x2="890" y2="170" stroke="#24394e"/>
  {''.join(month_nodes)}
  <text x="78" y="229" class="weekday">Mon</text><text x="78" y="255" class="weekday">Wed</text><text x="84" y="281" class="weekday">Fri</text>
  {''.join(cells)}
  <line x1="30" y1="316" x2="890" y2="316" stroke="#24394e"/>
  {''.join(stat_nodes)}
  <text x="890" y="310" text-anchor="end" class="updated">Updated {updated}</text>
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
