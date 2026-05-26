from __future__ import annotations

import copy
import sqlite3
import time
from typing import Any

from ai_engine import apply_learning_adjustments, clamp, enrich_players, set_model_weights
from data_sync import enrich_player_batch, get_metadata_value
from services.image_service import (
    DEFAULT_PLAYER_PHOTO,
    is_blocked_image_source,
    is_local_player_image_source,
    is_low_quality_image_source,
    local_player_image_path,
)
from services.player_avatar_service import generate_player_avatar


DEFAULT_TEAM_LOGO = "/assets/teams/default.png"
_PLAYERS_PAYLOAD_CACHE: dict[str, dict[str, Any]] = {}
PLAYERS_PAYLOAD_CACHE_TTL = 600

TEAM_LOGOS = {
    "arsenal": "/assets/teams/arsenal.png",
    "aston villa": "/assets/teams/aston_villa.png",
    "bournemouth": "/assets/teams/bournemouth.png",
    "afc bournemouth": "/assets/teams/bournemouth.png",
    "brentford": "/assets/teams/brentford.png",
    "brighton": "/assets/teams/brighton.png",
    "brighton hove albion": "/assets/teams/brighton.png",
    "burnley": "/assets/teams/burnley.png",
    "chelsea": "/assets/teams/chelsea.png",
    "crystal palace": "/assets/teams/crystal_palace.png",
    "everton": "/assets/teams/everton.png",
    "fulham": "/assets/teams/fulham.png",
    "ipswich": "/assets/teams/ipswich.png",
    "ipswich town": "/assets/teams/ipswich.png",
    "leeds": "/assets/teams/leeds.png",
    "leeds united": "/assets/teams/leeds.png",
    "liverpool": "/assets/teams/liverpool.png",
    "man city": "/assets/teams/man_city.png",
    "manchester city": "/assets/teams/man_city.png",
    "man utd": "/assets/teams/man_united.png",
    "manchester united": "/assets/teams/man_united.png",
    "newcastle": "/assets/teams/newcastle.png",
    "newcastle united": "/assets/teams/newcastle.png",
    "nottm forest": "/assets/teams/nottingham_forest.png",
    "nott m forest": "/assets/teams/nottingham_forest.png",
    "nottingham forest": "/assets/teams/nottingham_forest.png",
    "spurs": "/assets/teams/tottenham.png",
    "tottenham": "/assets/teams/tottenham.png",
    "tottenham hotspur": "/assets/teams/tottenham.png",
    "sunderland": "/assets/teams/sunderland.png",
    "west ham": "/assets/teams/west_ham.png",
    "west ham united": "/assets/teams/west_ham.png",
    "wolves": "/assets/teams/wolves.png",
    "wolverhampton": "/assets/teams/wolves.png",
    "wolverhampton wanderers": "/assets/teams/wolves.png",
}

TEAM_DISPLAY_NAMES = {
    "afc bournemouth": "Bournemouth",
    "brighton": "Brighton & Hove Albion",
    "man city": "Manchester City",
    "man utd": "Manchester United",
    "newcastle": "Newcastle United",
    "nottm forest": "Nottingham Forest",
    "nott m forest": "Nottingham Forest",
    "spurs": "Tottenham Hotspur",
    "west ham": "West Ham United",
    "wolves": "Wolverhampton Wanderers",
    "ipswich": "Ipswich Town",
    "leeds": "Leeds United",
}


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


def _normalize_team_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    replacements = {
        "&": " and ",
        "'": "",
        ".": " ",
        "-": " ",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return " ".join(text.split())


def _is_reliable_player_photo(url: Any) -> bool:
    text = str(url or "").strip()
    if not text:
        return False
    return is_local_player_image_source(text) or text == DEFAULT_PLAYER_PHOTO


def resolve_team_logo(team_name: str, short_name: str = "") -> str:
    for candidate in (
        _normalize_team_name(team_name),
        _normalize_team_name(short_name),
    ):
        if candidate and candidate in TEAM_LOGOS:
            return TEAM_LOGOS[candidate]
    return DEFAULT_TEAM_LOGO


def resolve_team_display_name(team_name: str, short_name: str = "") -> str:
    normalized_name = _normalize_team_name(team_name)
    normalized_short = _normalize_team_name(short_name)
    if normalized_name in TEAM_DISPLAY_NAMES:
        return TEAM_DISPLAY_NAMES[normalized_name]
    if normalized_short in TEAM_DISPLAY_NAMES:
        return TEAM_DISPLAY_NAMES[normalized_short]
    return str(team_name or short_name or "Unknown")


def build_player_photo_url(
    photo_value: Any,
    player_code: int,
    player_id: int | None = None,
    player_name: Any = None,
    team_name: Any = None,
    position: Any = None,
) -> str:
    del photo_value, player_code, player_name, team_name, position
    return local_player_image_path(player_id)


def _apply_generated_avatar_fields(player: dict[str, Any]) -> None:
    avatar_url = build_player_photo_url(
        photo_value=player.get("photo"),
        player_code=_to_int(player.get("code"), 0),
        player_id=_to_int(player.get("id") or player.get("player_id"), 0),
        player_name=player.get("name") or player.get("web_name"),
        team_name=player.get("team_name") or player.get("team"),
        position=player.get("position"),
    )
    player["local_image_path"] = avatar_url or DEFAULT_PLAYER_PHOTO
    player["photo_url"] = player["local_image_path"]
    player["image_url"] = player["local_image_path"]


def query_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "players": _to_int(conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]),
        "teams": _to_int(conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]),
        "fixtures": _to_int(
            conn.execute("SELECT COUNT(*) FROM fixtures").fetchone()[0]
        ),
        "saved_squad": _to_int(
            conn.execute("SELECT COUNT(*) FROM selected_squad").fetchone()[0]
        ),
    }


def player_tier(score: float) -> dict[str, str]:
    if score >= 6.0:
        return {
            "code": "S",
            "label": "S Tier",
            "bucket": "s",
            "description": "Best FPL picks",
        }
    if score >= 5.0:
        return {
            "code": "A",
            "label": "A Tier",
            "bucket": "a",
            "description": "Strong picks",
        }
    if score >= 4.0:
        return {
            "code": "B",
            "label": "B Tier",
            "bucket": "b",
            "description": "Decent options",
        }
    return {
        "code": "C",
        "label": "C Tier",
        "bucket": "c",
        "description": "Low priority picks",
    }


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _load_model_weights(conn: sqlite3.Connection) -> dict[str, float]:
    rows = conn.execute("SELECT key, value FROM ai_model_weights").fetchall()
    weights: dict[str, float] = {}
    for row in rows:
        try:
            weights[str(row[0])] = float(row[1])
        except (TypeError, ValueError):
            continue
    return weights


def _normalize_filter_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _players_payload_cache_key(
    conn: sqlite3.Connection,
    sort_by: str,
    limit: int | None,
    search: str = "",
    position: str = "",
    team: str = "",
    min_price: float | None = None,
    max_price: float | None = None,
    only_starters: bool = False,
    high_form: bool = False,
) -> str:
    last_sync_row = conn.execute(
        "SELECT value FROM metadata WHERE key = 'last_sync_utc'"
    ).fetchone()
    image_cache_row = conn.execute(
        "SELECT value FROM metadata WHERE key = 'image_cache_version'"
    ).fetchone()
    weights_row = conn.execute(
        "SELECT MAX(updated_at) FROM ai_model_weights"
    ).fetchone()
    external_row = conn.execute(
        "SELECT MAX(updated_at) FROM player_external_stats"
    ).fetchone()
    last_sync = str(last_sync_row[0] if last_sync_row and last_sync_row[0] else "")
    image_cache_version = str(
        image_cache_row[0] if image_cache_row and image_cache_row[0] else ""
    )
    weights_version = str(weights_row[0] if weights_row and weights_row[0] else "")
    external_version = str(external_row[0] if external_row and external_row[0] else "")
    return "|".join(
        [
            last_sync,
            image_cache_version,
            weights_version,
            external_version,
            sort_by,
            str(limit or 0),
            _normalize_filter_text(search),
            _normalize_filter_text(position),
            _normalize_filter_text(team),
            str(min_price if min_price is not None else ""),
            str(max_price if max_price is not None else ""),
            "1" if only_starters else "0",
            "1" if high_form else "0",
        ]
    )


def _load_external_signal_map(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT player_id,
               MAX(CASE WHEN provider = 'sofascore' THEN rating END) AS sofascore_rating,
               MAX(CASE WHEN provider = 'sofascore' THEN form_score END) AS sofascore_form_score,
               MAX(CASE WHEN provider = 'flashscore' THEN form_score END) AS flashscore_form_score,
               MAX(CASE WHEN provider = 'news' THEN news_sentiment END) AS news_sentiment_score,
               MAX(CASE WHEN provider = 'social' THEN trend_score END) AS social_trend_score,
               MAX(confidence_score) AS confidence_score
        FROM player_external_stats
        GROUP BY player_id
        """
    ).fetchall()
    signal_map: dict[int, dict[str, Any]] = {}
    for row in rows:
        player_id = _to_int(row["player_id"], 0)
        if player_id <= 0:
            continue
        signal_map[player_id] = {
            "sofascore_rating": round(_to_float(row["sofascore_rating"], 0.0), 2),
            "sofascore_form_score": round(
                _to_float(row["sofascore_form_score"], 0.0), 2
            ),
            "flashscore_form_score": round(
                _to_float(row["flashscore_form_score"], 0.0), 2
            ),
            "news_sentiment_score": round(
                _to_float(row["news_sentiment_score"], 0.0), 3
            ),
            "social_trend_score": round(_to_float(row["social_trend_score"], 0.0), 3),
            "confidence_score": round(_to_float(row["confidence_score"], 0.0), 1),
        }
    return signal_map


def _search_priority(
    player: dict[str, Any], search_term: str
) -> tuple[int, float, str]:
    term = _normalize_filter_text(search_term)
    if not term:
        return (4, 0.0, str(player.get("name") or ""))
    name = _normalize_filter_text(player.get("name"))
    team = _normalize_filter_text(player.get("team_name"))
    if name == term:
        bucket = 0
    elif name.startswith(term):
        bucket = 1
    elif term in name:
        bucket = 2
    elif term in team:
        bucket = 3
    else:
        bucket = 4
    return (bucket, -_to_float(player.get("ai_score"), 0.0), name)


def fetch_players_payload(
    conn: sqlite3.Connection,
    sort_by: str = "ai_score",
    limit: int | None = None,
    search: str = "",
    position: str = "",
    team: str = "",
    min_price: float | None = None,
    max_price: float | None = None,
    only_starters: bool = False,
    high_form: bool = False,
) -> dict[str, Any]:
    cache_key = _players_payload_cache_key(
        conn,
        sort_by,
        limit,
        search=search,
        position=position,
        team=team,
        min_price=min_price,
        max_price=max_price,
        only_starters=only_starters,
        high_form=high_form,
    )
    cached = _PLAYERS_PAYLOAD_CACHE.get(cache_key)
    if cached and cached.get("expires_at", 0) > time.time():
        return copy.deepcopy(cached["payload"])

    set_model_weights(_load_model_weights(conn))
    search_text = _normalize_filter_text(search)
    position_value = str(position or "").strip().upper()
    team_value = _normalize_filter_text(team)
    where_clauses = [
        "p.element_type IS NOT NULL",
        "p.team_id IS NOT NULL",
        "COALESCE(p.team_id, 0) > 0",
        "NOT (COALESCE(p.minutes, 0) = 0 AND COALESCE(p.points, 0) = 0 AND (LOWER(COALESCE(p.news,'')) LIKE '%loan%' OR LOWER(COALESCE(p.news,'')) LIKE '%permanently%' OR LOWER(COALESCE(p.news,'')) LIKE '%transferred%'))",
    ]
    params: list[Any] = []
    if search_text:
        like_term = f"%{search_text}%"
        where_clauses.append(
            "(LOWER(COALESCE(p.name, '')) LIKE ? OR LOWER(COALESCE(p.web_name, '')) LIKE ?)"
        )
        params.extend([like_term, like_term])
    if position_value:
        where_clauses.append("p.position = ?")
        params.append(position_value)
    if team_value:
        where_clauses.append(
            "(LOWER(COALESCE(p.team_name, '')) = ? OR LOWER(COALESCE(p.team_short, '')) = ?)"
        )
        params.extend([team_value, team_value])
    if min_price is not None:
        where_clauses.append("COALESCE(p.price, 0) >= ?")
        params.append(float(min_price))
    if max_price is not None:
        where_clauses.append("COALESCE(p.price, 0) <= ?")
        params.append(float(max_price))
    if only_starters:
        where_clauses.append(
            "(COALESCE(p.starts, 0) >= 8 OR COALESCE(p.recent_minutes_avg, 0) >= 60)"
        )
    if high_form:
        where_clauses.append(
            "(COALESCE(p.form, 0) >= 5.5 OR COALESCE(p.recent_points_avg, 0) >= 5.5)"
        )

    rows = conn.execute(
        f"""
        SELECT p.id, p.code, p.photo, p.photo_url, p.squad_number,
               p.local_image_path,
               p.age, p.height, p.weight, p.nationality,
               p.preferred_foot, p.market_value,
               p.recent_points_avg, p.recent_minutes_avg, p.recent_xg_avg,
               p.recent_xa_avg, p.consistency_score, p.explosiveness_score,
               p.upcoming_fixture_count, p.upcoming_blank, p.upcoming_double,
               p.first_name,
                p.second_name, p.web_name, p.name, p.image_url,
                p.team_id, p.team_name, p.team_short, p.team_badge_url,
                p.position, p.price, p.points, p.form, p.fixture_difficulty,
               p.selected_by_percent, p.minutes, p.starts, p.goals, p.assists,
                p.clean_sheets, p.bonus, p.expected_goals, p.expected_assists,
                p.expected_goal_involvements, p.expected_goals_conceded,
                p.shots, p.shots_on_target, p.key_passes, p.yellow_cards, p.red_cards,
                p.chance_of_playing_next_round, p.status, p.news,
                 t.strength AS team_strength,
                 t.strength_attack_home, t.strength_attack_away,
                 t.strength_defence_home, t.strength_defence_away
        FROM players p
        JOIN teams t ON t.id = p.team_id
        WHERE {" AND ".join(where_clauses)}
        """,
        params,
    ).fetchall()
    external_signal_map = _load_external_signal_map(conn)

    base_players: list[dict[str, Any]] = []
    for row in rows:
        player = _row_to_dict(row)
        raw_team_name = str(player.get("team_name") or "")
        raw_team_short = str(player.get("team_short") or "")
        player["player_id"] = _to_int(player.get("id"), 0)
        player["team_name"] = resolve_team_display_name(raw_team_name, raw_team_short)
        player["team"] = player["team_name"]
        player["team_logo"] = resolve_team_logo(raw_team_name, raw_team_short)
        player["logo"] = player["team_logo"]
        player["team_badge_url"] = player["team_logo"]
        player["price"] = round(_to_float(player.get("price"), 0.0), 1)
        player["form"] = round(_to_float(player.get("form"), 0.0), 1)
        player["fixture_difficulty"] = round(
            _to_float(player.get("fixture_difficulty"), 3.0), 1
        )
        player["selected_by_percent"] = round(
            _to_float(player.get("selected_by_percent"), 0.0), 1
        )
        player["minutes"] = _to_int(player.get("minutes"), 0)
        player["goals"] = _to_int(player.get("goals"), 0)
        player["assists"] = _to_int(player.get("assists"), 0)
        player["bonus"] = _to_int(player.get("bonus"), 0)
        player["shots"] = round(_to_float(player.get("shots"), 0.0), 1)
        player["shots_on_target"] = round(
            _to_float(player.get("shots_on_target"), 0.0), 1
        )
        player["key_passes"] = round(_to_float(player.get("key_passes"), 0.0), 1)
        player["yellow_cards"] = _to_int(player.get("yellow_cards"), 0)
        player["red_cards"] = _to_int(player.get("red_cards"), 0)
        player["team_strength"] = _to_int(player.get("team_strength"), 0)
        attack_home = _to_int(player.get("strength_attack_home"), 0)
        attack_away = _to_int(player.get("strength_attack_away"), 0)
        defence_home = _to_int(player.get("strength_defence_home"), 0)
        defence_away = _to_int(player.get("strength_defence_away"), 0)
        player["team_attack_strength"] = round((attack_home + attack_away) / 2, 1)
        player["team_defence_strength"] = round((defence_home + defence_away) / 2, 1)
        age_value = _to_int(player.get("age"), 0)
        height_value = _to_float(player.get("height"), 0.0)
        weight_value = _to_float(player.get("weight"), 0.0)
        player["age"] = age_value if age_value > 0 else None
        player["height"] = height_value if height_value > 0 else None
        player["weight"] = weight_value if weight_value > 0 else None
        player["nationality"] = str(player.get("nationality") or "").strip()
        player["preferred_foot"] = str(player.get("preferred_foot") or "").strip()
        player["market_value"] = str(player.get("market_value") or "").strip()
        player["recent_points_avg"] = round(
            _to_float(player.get("recent_points_avg"), 0.0), 2
        )
        player["recent_minutes_avg"] = round(
            _to_float(player.get("recent_minutes_avg"), 0.0), 2
        )
        player["recent_xg_avg"] = round(_to_float(player.get("recent_xg_avg"), 0.0), 3)
        player["recent_xa_avg"] = round(_to_float(player.get("recent_xa_avg"), 0.0), 3)
        player["consistency_score"] = round(
            _to_float(player.get("consistency_score"), 0.0), 3
        )
        player["explosiveness_score"] = round(
            _to_float(player.get("explosiveness_score"), 0.0), 3
        )
        player["upcoming_fixture_count"] = _to_int(
            player.get("upcoming_fixture_count"), 0
        )
        player["upcoming_blank"] = _to_int(player.get("upcoming_blank"), 0)
        player["upcoming_double"] = _to_int(player.get("upcoming_double"), 0)
        player["expected_goals"] = round(
            _to_float(player.get("expected_goals"), 0.0), 2
        )
        player["expected_assists"] = round(
            _to_float(player.get("expected_assists"), 0.0), 2
        )
        player["expected_goal_involvements"] = round(
            _to_float(player.get("expected_goal_involvements"), 0.0), 2
        )
        player["expected_goals_conceded"] = round(
            _to_float(player.get("expected_goals_conceded"), 0.0), 2
        )
        player["xg"] = player["expected_goals"]
        player["xa"] = player["expected_assists"]
        player.update(external_signal_map.get(player["player_id"], {}))
        player["recent_form_score"] = round(
            max(
                _to_float(player.get("sofascore_form_score"), 0.0),
                _to_float(player.get("flashscore_form_score"), 0.0),
            ),
            2,
        )
        player["starter_score"] = round(
            clamp(
                (
                    _to_float(player.get("recent_minutes_avg"), 0.0) / 90.0 * 0.65
                    + min(_to_float(player.get("starts"), 0.0) / 16.0, 1.0) * 0.35
                ),
                0.0,
                1.0,
            )
            * 100.0,
            1,
        )
        _apply_generated_avatar_fields(player)
        base_players.append(player)

    base_players = enrich_player_batch(conn, base_players)
    players = enrich_players(base_players)
    players = apply_learning_adjustments(players, conn)  # learning loop bias
    for player in players:
        ai_score = round(_to_float(player.get("score"), 0.0), 2)
        tier = player_tier(ai_score)
        player["ai_score"] = ai_score
        player["tier"] = tier["label"]
        player["tier_code"] = tier["code"]
        player["tier_bucket"] = tier["bucket"]
        player["tier_description"] = tier["description"]
        player["availability"] = str(player.get("availability") or "Available")
        player["xg"] = round(_to_float(player.get("expected_goals"), 0.0), 2)
        player["xa"] = round(_to_float(player.get("expected_assists"), 0.0), 2)
        player["form"] = round(_to_float(player.get("form"), 0.0), 1)
        player["minutes"] = _to_int(player.get("minutes"), 0)
        player["confidence_score"] = round(
            _to_float(player.get("confidence_score"), 0.0), 1
        )
        player["sofascore_rating"] = round(
            _to_float(player.get("sofascore_rating"), 0.0), 2
        )
        player["flashscore_form_score"] = round(
            _to_float(player.get("flashscore_form_score"), 0.0), 2
        )
        player["news_sentiment_score"] = round(
            _to_float(player.get("news_sentiment_score"), 0.0), 3
        )
        player["social_trend_score"] = round(
            _to_float(player.get("social_trend_score"), 0.0), 3
        )
        player["fixture_difficulty"] = round(
            _to_float(player.get("fixture_difficulty"), 3.0), 1
        )
        player["consistency_score"] = round(
            _to_float(player.get("consistency_score"), 0.0), 3
        )
        player["explosiveness_score"] = round(
            _to_float(player.get("explosiveness_score"), 0.0), 3
        )

    sort_specs: dict[str, tuple[Any, bool]] = {
        "ai_score": (lambda item: _to_float(item.get("ai_score"), 0.0), True),
        "xpts": (
            lambda item: _to_float(
                item.get("expected_points") or item.get("predicted_points_next_game"),
                0.0,
            ),
            True,
        ),
        "points": (lambda item: _to_float(item.get("points"), 0.0), True),
        "form": (lambda item: _to_float(item.get("form"), 0.0), True),
        "goals": (lambda item: _to_float(item.get("goals"), 0.0), True),
        "assists": (lambda item: _to_float(item.get("assists"), 0.0), True),
        "minutes": (lambda item: _to_float(item.get("minutes"), 0.0), True),
        "price": (lambda item: _to_float(item.get("price"), 0.0), False),
        "name": (lambda item: str(item.get("name") or ""), False),
    }
    sort_key, reverse = sort_specs.get(sort_by, sort_specs["ai_score"])
    players.sort(
        key=lambda item: (sort_key(item), _to_float(item.get("points"), 0.0)),
        reverse=reverse,
    )
    if search_text:
        players.sort(key=lambda item: _search_priority(item, search_text))

    if limit is not None and limit > 0:
        players = players[:limit]

    payload = {
        "players": players,
        "counts": query_counts(conn),
        "metadata": {
            "image_cache_version": get_metadata_value(conn, "image_cache_version", "")
        },
    }
    _PLAYERS_PAYLOAD_CACHE.clear()
    _PLAYERS_PAYLOAD_CACHE[cache_key] = {
        "payload": copy.deepcopy(payload),
        "expires_at": time.time() + PLAYERS_PAYLOAD_CACHE_TTL,
    }
    return payload
