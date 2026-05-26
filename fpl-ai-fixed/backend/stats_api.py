from __future__ import annotations

import sqlite3
from typing import Any

from player_api import (
    DEFAULT_PLAYER_PHOTO,
    query_counts,
    resolve_team_display_name,
    resolve_team_logo,
)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value in {None, ""}:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def build_standings_table(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    team_rows = conn.execute(
        "SELECT id, name, short_name FROM teams ORDER BY name"
    ).fetchall()
    standings = {
        _to_int(row["id"], 0): {
            "team_id": _to_int(row["id"], 0),
            "team": resolve_team_display_name(
                str(row["name"] or ""), str(row["short_name"] or "")
            ),
            "short_name": str(row["short_name"] or ""),
            "logo": resolve_team_logo(
                str(row["name"] or ""), str(row["short_name"] or "")
            ),
            "played": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "goals_for": 0,
            "goals_against": 0,
            "goal_difference": 0,
            "points": 0,
        }
        for row in team_rows
    }

    fixture_rows = conn.execute(
        """
        SELECT home_team_id, away_team_id, home_score, away_score
        FROM fixtures
        WHERE finished = 1 AND home_score IS NOT NULL AND away_score IS NOT NULL
        """
    ).fetchall()

    for row in fixture_rows:
        home_id = _to_int(row["home_team_id"], 0)
        away_id = _to_int(row["away_team_id"], 0)
        home_score = _to_int(row["home_score"], 0)
        away_score = _to_int(row["away_score"], 0)
        if home_id not in standings or away_id not in standings:
            continue

        home = standings[home_id]
        away = standings[away_id]
        home["played"] += 1
        away["played"] += 1
        home["goals_for"] += home_score
        home["goals_against"] += away_score
        away["goals_for"] += away_score
        away["goals_against"] += home_score

        if home_score > away_score:
            home["wins"] += 1
            away["losses"] += 1
            home["points"] += 3
        elif away_score > home_score:
            away["wins"] += 1
            home["losses"] += 1
            away["points"] += 3
        else:
            home["draws"] += 1
            away["draws"] += 1
            home["points"] += 1
            away["points"] += 1

    table = list(standings.values())
    for row in table:
        row["goal_difference"] = row["goals_for"] - row["goals_against"]

    table.sort(
        key=lambda item: (
            -item["points"],
            -item["goal_difference"],
            -item["goals_for"],
            item["goals_against"],
            item["team"],
        )
    )
    for index, row in enumerate(table, start=1):
        row["position"] = index
    return table


def _top_player_rows(
    conn: sqlite3.Connection,
    metric_column: str,
    limit: int = 10,
    position_filter: str | None = None,
) -> list[dict[str, Any]]:
    query = f"""
        SELECT id, code, photo, photo_url, name, image_url, team_name, team_short,
               position, minutes, goals, assists, clean_sheets,
               {metric_column} AS stat_value
        FROM players
    """
    params: list[Any] = []
    if position_filter:
        query += " WHERE position = ?"
        params.append(position_filter)
    query += f" ORDER BY {metric_column} DESC, minutes ASC, name ASC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()

    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        payload = _row_to_dict(row)
        raw_team_name = str(payload.get("team_name") or "")
        raw_team_short = str(payload.get("team_short") or "")
        payload["rank"] = index
        payload["team_name"] = resolve_team_display_name(raw_team_name, raw_team_short)
        payload["logo"] = resolve_team_logo(raw_team_name, raw_team_short)
        payload["team_logo"] = payload["logo"]
        stored_photo = str(payload.get("photo_url") or "").strip()
        fallback_photo = str(payload.get("image_url") or "").strip()
        if not stored_photo:
            stored_photo = fallback_photo
        if not stored_photo:
            stored_photo = DEFAULT_PLAYER_PHOTO
        if "player-placeholder" in stored_photo and fallback_photo:
            stored_photo = fallback_photo
        payload["photo_url"] = stored_photo
        payload["image_url"] = stored_photo or fallback_photo
        output.append(payload)
    return output


def top_scorers(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    return _top_player_rows(conn, "goals", limit=limit)


def top_assists(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    return _top_player_rows(conn, "assists", limit=limit)


def top_clean_sheets(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    return _top_player_rows(conn, "clean_sheets", limit=limit, position_filter="GK")


def standings_summary_cards(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    table = build_standings_table(conn)
    scorers = top_scorers(conn, limit=1)
    assist_rows = top_assists(conn, limit=1)

    most_goals_team = max(table, key=lambda item: item["goals_for"]) if table else None
    attacking_team = (
        max(table, key=lambda item: item["goals_for"] / max(item["played"], 1))
        if table
        else None
    )
    defensive_team = (
        min(
            table,
            key=lambda item: (
                item["goals_against"] / max(item["played"], 1),
                item["goals_against"],
            ),
        )
        if table
        else None
    )

    cards: list[dict[str, Any]] = []
    if most_goals_team:
        cards.append(
            {
                "label": "Most Goals Scored by a Team",
                "value": str(most_goals_team["goals_for"]),
                "name": most_goals_team["team"],
                "logo": most_goals_team["logo"],
            }
        )
    if attacking_team:
        cards.append(
            {
                "label": "Best Attacking Team",
                "value": f"{(attacking_team['goals_for'] / max(attacking_team['played'], 1)):.2f} per game",
                "name": attacking_team["team"],
                "logo": attacking_team["logo"],
            }
        )
    if defensive_team:
        cards.append(
            {
                "label": "Best Defensive Team",
                "value": f"{defensive_team['goals_against']} conceded",
                "name": defensive_team["team"],
                "logo": defensive_team["logo"],
            }
        )
    if scorers:
        cards.append(
            {
                "label": "Highest Scoring Player",
                "value": f"{_to_int(scorers[0]['goals'], 0)} goals",
                "name": scorers[0]["name"],
                "logo": scorers[0]["logo"],
            }
        )
    if assist_rows:
        cards.append(
            {
                "label": "Most Assists Player",
                "value": f"{_to_int(assist_rows[0]['assists'], 0)} assists",
                "name": assist_rows[0]["name"],
                "logo": assist_rows[0]["logo"],
            }
        )
    return cards


def standings_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "standings": build_standings_table(conn),
        "summary_cards": standings_summary_cards(conn),
        "counts": query_counts(conn),
    }
