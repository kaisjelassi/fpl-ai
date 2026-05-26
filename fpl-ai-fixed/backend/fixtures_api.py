from __future__ import annotations

import sqlite3
from typing import Any

from data_sync import get_metadata_value
from player_api import query_counts, resolve_team_display_name, resolve_team_logo


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value in {None, ""}:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in {None, ""}:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def current_gameweek(conn: sqlite3.Connection) -> int:
    return _to_int(get_metadata_value(conn, "current_gameweek", "1"), 1)


def difficulty_band(value: float) -> str:
    if value <= 2.0:
        return "easy"
    if value <= 3.0:
        return "medium"
    return "hard"


def difficulty_label(value: float) -> str:
    if value <= 2.0:
        return "Easy"
    if value <= 3.0:
        return "Medium"
    return "Hard"


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def fetch_fixtures_payload(
    conn: sqlite3.Connection, limit: int = 10, start_gw: int | None = None
) -> dict[str, Any]:
    focus_gw = start_gw or current_gameweek(conn)
    rows = conn.execute(
        """
        SELECT f.id, f.gameweek, f.kickoff_time,
               f.started, f.finished,
               f.home_team_id, f.away_team_id,
               f.home_team_name, f.away_team_name,
               f.home_fdr, f.away_fdr,
               f.home_score, f.away_score,
               th.short_name AS home_short,
               ta.short_name AS away_short
        FROM fixtures f
        JOIN teams th ON th.id = f.home_team_id
        JOIN teams ta ON ta.id = f.away_team_id
        WHERE f.gameweek >= ?
        ORDER BY f.gameweek, f.kickoff_time
        LIMIT ?
        """,
        (focus_gw, limit),
    ).fetchall()

    fixtures: list[dict[str, Any]] = []
    for row in rows:
        fixture = _row_to_dict(row)
        raw_home_name = str(fixture.get("home_team_name") or "")
        raw_home_short = str(fixture.get("home_short") or "")
        raw_away_name = str(fixture.get("away_team_name") or "")
        raw_away_short = str(fixture.get("away_short") or "")
        home_name = resolve_team_display_name(raw_home_name, raw_home_short)
        away_name = resolve_team_display_name(raw_away_name, raw_away_short)
        home_fdr = _to_int(fixture.get("home_fdr"), 3)
        away_fdr = _to_int(fixture.get("away_fdr"), 3)
        fixture_score = round((home_fdr + away_fdr) / 2, 1)
        fixtures.append(
            {
                "fixture_id": _to_int(fixture.get("id"), 0),
                "gameweek": _to_int(fixture.get("gameweek"), 0),
                "kickoff_time": fixture.get("kickoff_time") or "",
                "started": bool(fixture.get("started")),
                "finished": bool(fixture.get("finished")),
                "home_team_id": _to_int(fixture.get("home_team_id"), 0),
                "away_team_id": _to_int(fixture.get("away_team_id"), 0),
                "home_team_name": home_name,
                "away_team_name": away_name,
                "home_short": raw_home_short,
                "away_short": raw_away_short,
                "home_logo": resolve_team_logo(raw_home_name, raw_home_short),
                "away_logo": resolve_team_logo(raw_away_name, raw_away_short),
                "home_fdr": home_fdr,
                "away_fdr": away_fdr,
                "home_score": fixture.get("home_score"),
                "away_score": fixture.get("away_score"),
                "team_h_score": fixture.get("home_score"),
                "team_a_score": fixture.get("away_score"),
                "home_band": difficulty_band(home_fdr),
                "away_band": difficulty_band(away_fdr),
                "fixture_score": fixture_score,
                "fixture_label": difficulty_label(fixture_score),
            }
        )

    return {
        "fixtures": fixtures,
        "best_runs": best_fixture_runs(conn),
        "counts": query_counts(conn),
    }


def build_fixture_matrix(
    conn: sqlite3.Connection, window: int = 4
) -> tuple[list[int], list[dict[str, Any]]]:
    start_gw = current_gameweek(conn)
    gameweek_rows = conn.execute(
        """
        SELECT id
        FROM gameweeks
        WHERE id >= ?
        ORDER BY id
        LIMIT ?
        """,
        (start_gw, window),
    ).fetchall()
    gameweeks = [
        _to_int(row["id"], 0) for row in gameweek_rows if _to_int(row["id"], 0) > 0
    ]
    if not gameweeks:
        return [], []

    team_rows = conn.execute(
        "SELECT id, name, short_name FROM teams ORDER BY name"
    ).fetchall()
    fixture_rows = conn.execute(
        """
        SELECT gameweek, home_team_id, away_team_id,
               id,
               home_team_name, away_team_name,
               home_fdr, away_fdr
        FROM fixtures
        WHERE gameweek BETWEEN ? AND ?
        ORDER BY gameweek, kickoff_time
        """,
        (gameweeks[0], gameweeks[-1]),
    ).fetchall()

    fixture_map: dict[tuple[int, int], dict[str, Any]] = {}
    for row in fixture_rows:
        fixture_map[(_to_int(row["home_team_id"]), _to_int(row["gameweek"]))] = {
            "fixture_id": _to_int(row["id"], 0),
            "opponent": resolve_team_display_name(str(row["away_team_name"] or "")),
            "venue": "H",
            "difficulty": _to_int(row["home_fdr"], 3),
        }
        fixture_map[(_to_int(row["away_team_id"]), _to_int(row["gameweek"]))] = {
            "fixture_id": _to_int(row["id"], 0),
            "opponent": resolve_team_display_name(str(row["home_team_name"] or "")),
            "venue": "A",
            "difficulty": _to_int(row["away_fdr"], 3),
        }

    matrix: list[dict[str, Any]] = []
    for row in team_rows:
        raw_name = str(row["name"] or "")
        raw_short = str(row["short_name"] or "")
        cells: list[dict[str, Any]] = []
        for gameweek in gameweeks:
            fixture = fixture_map.get((_to_int(row["id"], 0), gameweek))
            if not fixture:
                cells.append({"label": "Blank", "difficulty": 0, "band": "blank"})
                continue
            difficulty = _to_float(fixture.get("difficulty"), 3.0)
            cells.append(
                {
                    "fixture_id": _to_int(fixture.get("fixture_id"), 0),
                    "label": f"{fixture['opponent']} ({fixture['venue']})",
                    "difficulty": int(difficulty),
                    "band": difficulty_band(difficulty),
                }
            )
        matrix.append(
            {
                "team_id": _to_int(row["id"], 0),
                "team_name": resolve_team_display_name(raw_name, raw_short),
                "team_short": raw_short,
                "badge_url": resolve_team_logo(raw_name, raw_short),
                "cells": cells,
            }
        )

    matrix.sort(key=lambda item: item["team_name"])
    return gameweeks, matrix


def best_fixture_runs(
    conn: sqlite3.Connection, horizon: int = 5, limit: int = 6
) -> list[dict[str, Any]]:
    start_gw = current_gameweek(conn)
    team_rows = conn.execute(
        "SELECT id, name, short_name FROM teams ORDER BY name"
    ).fetchall()
    fixture_rows = conn.execute(
        """
        SELECT gameweek, home_team_id, away_team_id,
               home_team_name, away_team_name,
               home_fdr, away_fdr
        FROM fixtures
        WHERE gameweek >= ?
        ORDER BY gameweek, kickoff_time
        """,
        (start_gw,),
    ).fetchall()

    team_schedule: dict[int, list[dict[str, Any]]] = {}
    for row in fixture_rows:
        home_id = _to_int(row["home_team_id"], 0)
        away_id = _to_int(row["away_team_id"], 0)
        if home_id > 0:
            team_schedule.setdefault(home_id, []).append(
                {
                    "opponent": resolve_team_display_name(
                        str(row["away_team_name"] or "")
                    ),
                    "venue": "H",
                    "difficulty": _to_int(row["home_fdr"], 3),
                    "gameweek": _to_int(row["gameweek"], 0),
                }
            )
        if away_id > 0:
            team_schedule.setdefault(away_id, []).append(
                {
                    "opponent": resolve_team_display_name(
                        str(row["home_team_name"] or "")
                    ),
                    "venue": "A",
                    "difficulty": _to_int(row["away_fdr"], 3),
                    "gameweek": _to_int(row["gameweek"], 0),
                }
            )

    best_runs: list[dict[str, Any]] = []
    for row in team_rows:
        team_id = _to_int(row["id"], 0)
        raw_name = str(row["name"] or "")
        raw_short = str(row["short_name"] or "")
        upcoming = team_schedule.get(team_id, [])[:horizon]
        if not upcoming:
            continue
        avg_fdr = round(
            sum(_to_float(item.get("difficulty"), 3.0) for item in upcoming)
            / len(upcoming),
            2,
        )
        best_runs.append(
            {
                "team_id": team_id,
                "team_name": resolve_team_display_name(raw_name, raw_short),
                "team_short": raw_short,
                "logo": resolve_team_logo(raw_name, raw_short),
                "average_fdr": avg_fdr,
                "band": difficulty_band(avg_fdr),
                "label": difficulty_label(avg_fdr),
                "opponents": [
                    f"{item['opponent']} ({item['venue']})" for item in upcoming
                ],
            }
        )
    best_runs.sort(key=lambda item: (item["average_fdr"], item["team_name"]))
    return best_runs[:limit]
