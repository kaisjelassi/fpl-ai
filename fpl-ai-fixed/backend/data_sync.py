from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from services.image_service import (
    extract_fpl_photo_id,
    get_official_fpl_image_url,
    image_dimensions,
    image_extension_from_content_type,
    is_blocked_image_source,
    is_default_player_photo,
    is_generated_avatar_source,
    is_local_player_image_source,
    is_official_fpl_cdn_url,
    local_player_image_path,
    fetch_remote_image_bytes,
)
from services.player_avatar_service import (
    generate_player_avatar,
    generate_player_avatar_record,
)
from services.current_season_image_service import (
    PL_SEASON_ID,
    build_team_image_source_candidates,
    fetch_premier_league_team_index,
)


logger = logging.getLogger(__name__)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRIMARY_DB_PATH = PROJECT_ROOT / "database" / "fpl.db"
FALLBACK_DB_PATH = PROJECT_ROOT / "database" / "fpl_runtime.db"


def _can_open_database_for_write(path: Path) -> bool:
    connection: sqlite3.Connection | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=1)
        connection.execute("PRAGMA busy_timeout = 1000")
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
        return True
    except sqlite3.OperationalError:
        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
        return False
    finally:
        if connection is not None:
            connection.close()


def resolve_database_path() -> Path:
    override = str(os.getenv("TACTIX_DB_PATH") or "").strip()
    if override:
        return Path(override)
    if _can_open_database_for_write(PRIMARY_DB_PATH):
        return PRIMARY_DB_PATH
    return FALLBACK_DB_PATH


DB_PATH = resolve_database_path()
PLAYER_BIO_PATH = PROJECT_ROOT / "database" / "player_bios.json"
PLAYER_IMAGE_PATH = PROJECT_ROOT / "database" / "player_avatar_cache.json"
PLAYER_IMAGE_DIR = PROJECT_ROOT / "assets" / "player_images"
DEFAULT_PLAYER_IMAGE_FILE = PROJECT_ROOT / "assets" / "default_player.png"

FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
FPL_FIXTURES_URL = "https://fantasy.premierleague.com/api/fixtures/"
BOOTSTRAP_URL = FPL_BOOTSTRAP_URL
FIXTURES_URL = FPL_FIXTURES_URL
PLAYER_HISTORY_URL_TEMPLATE = (
    "https://fantasy.premierleague.com/api/element-summary/{player_id}/"
)
PREMIER_LEAGUE_CLUBS_URL = "https://www.premierleague.com/clubs"
PLAYER_PLACEHOLDER_URL = (
    "https://fantasy.premierleague.com/dist/img/player-placeholder.svg"
)
DEFAULT_PLAYER_IMAGE = "/assets/default_player.png"
TRUSTED_PLAYER_IMAGE_SOURCES = {
    "fpl_cdn",
    "generated_cache",
    "generated_openai",
    "generated_svg",
    "local_asset",
    "premierleague_scraped",
    "ui_avatar",
}
TEAM_BADGE_TEMPLATE = (
    "https://resources.premierleague.com/premierleague/badges/70/t{team_code}.png"
)
PLAYER_PHOTO_TEMPLATE = "https://resources.premierleague.com/premierleague/photos/players/250x250/p{photo_token}.png"

POSITION_MAP = {
    1: "GK",
    2: "DEF",
    3: "MID",
    4: "FWD",
}

TEAM_WEBSITE_MAP = {
    "arsenal": "https://www.arsenal.com",
    "aston villa": "https://www.avfc.co.uk",
    "bournemouth": "https://www.afcb.co.uk",
    "afc bournemouth": "https://www.afcb.co.uk",
    "brentford": "https://www.brentfordfc.com",
    "brighton": "https://www.brightonandhovealbion.com",
    "brighton hove albion": "https://www.brightonandhovealbion.com",
    "burnley": "https://www.burnleyfootballclub.com",
    "chelsea": "https://www.chelseafc.com",
    "crystal palace": "https://www.cpfc.co.uk",
    "everton": "https://www.evertonfc.com",
    "fulham": "https://www.fulhamfc.com",
    "ipswich": "https://www.itfc.co.uk",
    "ipswich town": "https://www.itfc.co.uk",
    "leeds": "https://www.leedsunited.com",
    "leeds united": "https://www.leedsunited.com",
    "leicester": "https://www.lcfc.com",
    "leicester city": "https://www.lcfc.com",
    "liverpool": "https://www.liverpoolfc.com",
    "man city": "https://www.mancity.com",
    "manchester city": "https://www.mancity.com",
    "man utd": "https://www.manutd.com",
    "manchester united": "https://www.manutd.com",
    "newcastle": "https://www.newcastleunited.com",
    "newcastle united": "https://www.newcastleunited.com",
    "nottm forest": "https://www.nottinghamforest.co.uk",
    "nottm forest": "https://www.nottinghamforest.co.uk",
    "nott m forest": "https://www.nottinghamforest.co.uk",
    "nottingham forest": "https://www.nottinghamforest.co.uk",
    "southampton": "https://www.southamptonfc.com",
    "spurs": "https://www.tottenhamhotspur.com",
    "tottenham": "https://www.tottenhamhotspur.com",
    "tottenham hotspur": "https://www.tottenhamhotspur.com",
    "sunderland": "https://www.safc.com",
    "west ham": "https://www.whufc.com",
    "west ham united": "https://www.whufc.com",
    "wolves": "https://www.wolves.co.uk",
    "wolverhampton": "https://www.wolves.co.uk",
    "wolverhampton wanderers": "https://www.wolves.co.uk",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in {None, ""}:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in {None, ""}:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_divide(numerator: Any, denominator: Any, default: float = 0.0) -> float:
    try:
        if denominator in (None, 0, 0.0, ""):
            return default
        return float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return default


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _normalize_name(value: Any) -> str:
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


def _is_blocked_image_source(url: str) -> bool:
    return is_blocked_image_source(url)


def _asset_url_to_path(asset_url: Any) -> Path | None:
    normalized = str(asset_url or "").strip()
    if not normalized.startswith("/assets/"):
        return None
    if normalized.startswith("/assets/player_images/"):
        filename = normalized.split("/assets/player_images/", 1)[1].strip("/")
        if not filename:
            return None
        return PLAYER_IMAGE_DIR / Path(*filename.split("/"))
    relative = normalized[len("/assets/") :].strip("/")
    if not relative:
        return None
    return PROJECT_ROOT / "assets" / Path(*relative.split("/"))


def _image_bytes_are_svg(payload: bytes) -> bool:
    head = payload[:512].lstrip().lower()
    return head.startswith(b"<?xml") or b"<svg" in head


def _is_valid_local_asset_url(asset_url: Any) -> bool:
    target_path = _asset_url_to_path(asset_url)
    if target_path is None or not target_path.exists() or not target_path.is_file():
        return False
    try:
        return target_path.stat().st_size > 0
    except OSError:
        return False


def _is_reliable_player_image(url: Any) -> bool:
    text = str(url or "").strip()
    if not text:
        return False
    if is_local_player_image_source(text) or is_default_player_photo(text):
        return _is_valid_local_asset_url(text)
    return not _is_blocked_image_source(text)


def _player_local_image_url(player_id: int) -> str:
    return local_player_image_path(player_id)


def _player_local_image_file(player_id: int, extension: str = "png") -> Path:
    normalized = max(_safe_int(player_id, 0), 0)
    normalized_extension = str(extension or "png").strip().lower().lstrip(".")
    if normalized_extension not in {"png", "jpg", "jpeg", "webp"}:
        normalized_extension = "png"
    return PLAYER_IMAGE_DIR / f"{normalized}.{normalized_extension}"


def _remove_existing_player_image_files(player_id: int) -> None:
    normalized = max(_safe_int(player_id, 0), 0)
    for candidate in PLAYER_IMAGE_DIR.glob(f"{normalized}.*"):
        try:
            candidate.unlink()
        except OSError:
            continue


def _load_default_player_image_bytes() -> bytes:
    global _DEFAULT_PLAYER_IMAGE_BYTES
    if _DEFAULT_PLAYER_IMAGE_BYTES is None:
        _DEFAULT_PLAYER_IMAGE_BYTES = DEFAULT_PLAYER_IMAGE_FILE.read_bytes()
    return _DEFAULT_PLAYER_IMAGE_BYTES


def _write_player_image_bytes(target_path: Path, payload: bytes) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_suffix(".tmp")
    temp_path.write_bytes(payload)
    temp_path.replace(target_path)


def _fetch_json(url: str, timeout: int = 30) -> Any:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; FPL AI Assistant University Project)",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_text(url: str, timeout: int = 30) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; FPL AI Assistant University Project)",
            "Accept": "text/html,application/json",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


def fetch_sync_bundle() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    bootstrap = _fetch_json(BOOTSTRAP_URL)
    fixtures_payload = _fetch_json(FIXTURES_URL)
    if not isinstance(bootstrap, dict):
        raise ValueError("Official bootstrap payload is invalid.")
    if not isinstance(fixtures_payload, list):
        raise ValueError("Official fixtures payload is invalid.")
    return bootstrap, fixtures_payload


def _strip_tags(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return " ".join(text.split())


def _extract_age(text: str) -> int | None:
    age_label = re.search(r"Age\s*:?\s*(\d{1,2})", text, re.IGNORECASE)
    if age_label:
        return _safe_int(age_label.group(1), 0) or None
    age_match = re.search(r"\(age\s*(\d{1,2})\)", text, re.IGNORECASE)
    if age_match:
        return _safe_int(age_match.group(1), 0) or None
    date_match = re.search(r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})", text)
    if date_match:
        try:
            born = datetime.strptime(date_match.group(1), "%d %B %Y")
            today = _utc_now().date()
            age = (
                today.year
                - born.year
                - ((today.month, today.day) < (born.month, born.day))
            )
            return int(age)
        except ValueError:
            return None
    return None


def _extract_height(text: str) -> float | None:
    match = re.search(r"(\d{3})\s*cm", text)
    if match:
        return _safe_float(match.group(1), 0.0) or None
    meters = re.search(r"(\d\.\d{2})\s*m", text)
    if meters:
        return round(_safe_float(meters.group(1), 0.0) * 100.0, 1) or None
    return None


def _extract_weight(text: str) -> float | None:
    match = re.search(r"(\d{2,3})\s*kg", text)
    if match:
        return _safe_float(match.group(1), 0.0) or None
    return None


def _extract_nationality(text: str) -> str | None:
    match = re.search(
        r"Nationality\s*(.*?)\s*(?:Born|Height|Weight|$)", text, re.IGNORECASE
    )
    if match:
        return _strip_tags(match.group(1)).split(" ")[0].strip()
    return None


def _extract_preferred_foot_clean(text: str) -> str | None:
    pattern = r"(Preferred foot|Foot)\s*:\s*(\w+)"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.group(2).title()
    return None


def _extract_market_value_clean(text: str) -> str | None:
    pattern = r"Market value\s*:?\s*(€\s*\d[\d.,]*\s*[mk]?)"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.group(1).replace(" ", "")
    fallback = r"Current market value\s*:?\s*(€\s*\d[\d.,]*\s*[mk]?)"
    match = re.search(fallback, text, re.IGNORECASE)
    if match:
        return match.group(1).replace(" ", "")
    return None


def _extract_preferred_foot(text: str) -> str | None:
    pattern = r"(Preferred foot|Foot)\s*:\s*(\w+)"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.group(2).title()
    return None


def _extract_market_value(text: str) -> str | None:
    match = re.search(
        r"Market value\s*:?\s*(\u20ac\s*\d[\d.,]*\s*[mk]?)",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).replace(" ", "")
    match = re.search(
        r"Current market value\s*:?\s*(\u20ac\s*\d[\d.,]*\s*[mk]?)",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).replace(" ", "")
    return None


PLAYER_BIO_CACHE: dict[
    str,
    tuple[
        int | None,
        float | None,
        float | None,
        str | None,
        str | None,
        str | None,
    ],
] = {}
PLAYER_ENRICH_COUNT = 0
MAX_ENRICH_REQUESTS = 1200
PLAYER_IMAGE_CACHE: dict[str, str] = {}
PLAYER_IMAGE_LOOKUPS = 0
MAX_IMAGE_REQUESTS = 1200
HISTORY_SYNC_WORKERS = 12
_DEFAULT_PLAYER_IMAGE_BYTES: bytes | None = None

DEFAULT_MODEL_WEIGHTS: dict[str, float] = {
    "form": 0.18,
    "xg": 0.19,
    "xa": 0.13,
    "minutes": 0.11,
    "fixture": 0.11,
    "team": 0.07,
    "ownership": 0.03,
    "shots": 0.03,
    "key_passes": 0.02,
    "recent_form": 0.08,
    "consistency": 0.03,
    "explosiveness": 0.02,
}


def _utc_now_iso() -> str:
    return _utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fetch_recent_scores_map(
    conn: sqlite3.Connection, player_ids: list[int], limit: int = 5
) -> dict[int, list[int]]:
    valid_ids = [player_id for player_id in player_ids if player_id > 0]
    if not valid_ids:
        return {}
    placeholders = ", ".join(["?"] * len(valid_ids))
    rows = conn.execute(
        f"""
        SELECT player_id, gameweek, total_points
        FROM player_gameweek_history
        WHERE player_id IN ({placeholders})
          AND gameweek IS NOT NULL
        ORDER BY player_id, gameweek DESC
        """,
        valid_ids,
    ).fetchall()
    score_map: dict[int, list[int]] = {player_id: [] for player_id in valid_ids}
    for row in rows:
        player_id = _safe_int(row["player_id"], 0)
        if player_id <= 0:
            continue
        bucket = score_map.setdefault(player_id, [])
        if len(bucket) >= limit:
            continue
        bucket.append(_safe_int(row["total_points"], 0))
    return {
        player_id: list(reversed(scores))
        for player_id, scores in score_map.items()
        if scores
    }


def _fetch_team_future_fixtures_map(
    conn: sqlite3.Connection, team_ids: list[int], limit: int = 6
) -> dict[int, list[dict[str, Any]]]:
    valid_ids = [team_id for team_id in team_ids if team_id > 0]
    if not valid_ids:
        return {}
    placeholders = ", ".join(["?"] * len(valid_ids))
    params: list[Any] = [_utc_now_iso(), *valid_ids, *valid_ids]
    rows = conn.execute(
        f"""
        SELECT id, gameweek, kickoff_time,
               home_team_id, away_team_id,
               home_team_name, away_team_name,
               home_fdr, away_fdr
        FROM fixtures
        WHERE kickoff_time IS NOT NULL
          AND kickoff_time > ?
          AND (home_team_id IN ({placeholders}) OR away_team_id IN ({placeholders}))
        ORDER BY kickoff_time, id
        """,
        params,
    ).fetchall()
    fixture_map: dict[int, list[dict[str, Any]]] = {
        team_id: [] for team_id in valid_ids
    }
    for row in rows:
        home_team_id = _safe_int(row["home_team_id"], 0)
        away_team_id = _safe_int(row["away_team_id"], 0)
        for team_id, is_home in ((home_team_id, True), (away_team_id, False)):
            if team_id not in fixture_map:
                continue
            bucket = fixture_map.setdefault(team_id, [])
            if len(bucket) >= limit:
                continue
            opponent_name = row["away_team_name"] if is_home else row["home_team_name"]
            bucket.append(
                {
                    "fixture_id": _safe_int(row["id"], 0),
                    "gameweek": _safe_int(row["gameweek"], 0),
                    "kickoff_time": str(row["kickoff_time"] or ""),
                    "venue": "H" if is_home else "A",
                    "opponent_team_id": away_team_id if is_home else home_team_id,
                    "opponent_name": str(opponent_name or "Unknown"),
                    "fdr": _safe_int(
                        row["home_fdr"] if is_home else row["away_fdr"], 3
                    ),
                }
            )
    return fixture_map


def _fetch_team_recent_context_map(
    conn: sqlite3.Connection, team_ids: list[int], limit: int = 5
) -> dict[int, dict[str, Any]]:
    valid_ids = [team_id for team_id in team_ids if team_id > 0]
    if not valid_ids:
        return {}
    placeholders = ", ".join(["?"] * len(valid_ids))
    rows = conn.execute(
        f"""
        SELECT id, kickoff_time, home_team_id, away_team_id, home_score, away_score
        FROM fixtures
        WHERE finished = 1
          AND (home_team_id IN ({placeholders}) OR away_team_id IN ({placeholders}))
        ORDER BY COALESCE(kickoff_time, '') DESC, id DESC
        """,
        [*valid_ids, *valid_ids],
    ).fetchall()
    recent_rows: dict[int, list[sqlite3.Row]] = {team_id: [] for team_id in valid_ids}
    for row in rows:
        home_team_id = _safe_int(row["home_team_id"], 0)
        away_team_id = _safe_int(row["away_team_id"], 0)
        for team_id in (home_team_id, away_team_id):
            if team_id not in recent_rows:
                continue
            bucket = recent_rows.setdefault(team_id, [])
            if len(bucket) >= limit:
                continue
            bucket.append(row)
    context_map: dict[int, dict[str, Any]] = {}
    for team_id, fixtures in recent_rows.items():
        goals_for = 0
        goals_against = 0
        clean_sheets = 0
        rating_total = 0.0
        results: list[str] = []
        for row in fixtures:
            is_home = _safe_int(row["home_team_id"], 0) == team_id
            scored = (
                _safe_int(row["home_score"], 0)
                if is_home
                else _safe_int(row["away_score"], 0)
            )
            conceded = (
                _safe_int(row["away_score"], 0)
                if is_home
                else _safe_int(row["home_score"], 0)
            )
            goals_for += scored
            goals_against += conceded
            if conceded == 0:
                clean_sheets += 1
            if scored > conceded:
                rating_total += 1.0
                results.append("W")
            elif scored == conceded:
                rating_total += 0.5
                results.append("D")
            else:
                results.append("L")
        sample_size = max(len(fixtures), 1)
        context_map[team_id] = {
            "team_goals_last_5": goals_for,
            "team_clean_sheets_last_5": clean_sheets,
            "team_conceded_last_5": goals_against,
            "team_form_rating": round(rating_total / sample_size, 3),
            "team_last_5_results": results,
        }
    return context_map


def build_player_enrichment_context(
    conn: sqlite3.Connection, players: list[dict[str, Any]]
) -> dict[str, Any]:
    player_ids = []
    team_ids = []
    for player in players:
        player_id = _safe_int(player.get("id"), 0)
        team_id = _safe_int(player.get("team_id"), 0)
        if player_id > 0 and player_id not in player_ids:
            player_ids.append(player_id)
        if team_id > 0 and team_id not in team_ids:
            team_ids.append(team_id)
    return {
        "recent_scores_map": _fetch_recent_scores_map(conn, player_ids, limit=5),
        "team_future_fixtures_map": _fetch_team_future_fixtures_map(
            conn, team_ids, limit=6
        ),
        "team_recent_context_map": _fetch_team_recent_context_map(
            conn, team_ids, limit=5
        ),
    }


def enrich_player_data(
    player: dict[str, Any],
    recent_scores_map: dict[int, list[int]] | None = None,
    team_future_fixtures_map: dict[int, list[dict[str, Any]]] | None = None,
    team_recent_context_map: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = dict(player or {})
    player_id = _safe_int(payload.get("id"), 0)
    team_id = _safe_int(payload.get("team_id"), 0)
    minutes = _safe_float(payload.get("minutes"), 0.0)
    expected_goals = _safe_float(payload.get("expected_goals"), 0.0)
    expected_assists = _safe_float(payload.get("expected_assists"), 0.0)
    expected_goal_involvements = _safe_float(
        payload.get("expected_goal_involvements"), expected_goals + expected_assists
    )
    recent_scores = list(
        (recent_scores_map or {}).get(player_id, payload.get("recent_scores") or [])
    )
    future_fixtures = list(
        (team_future_fixtures_map or {}).get(
            team_id, payload.get("next_6_fixtures") or []
        )
    )[:6]
    recent_team_context = dict((team_recent_context_map or {}).get(team_id, {}))

    form_trend = "stable"
    if len(recent_scores) >= 4:
        early = (recent_scores[0] + recent_scores[1]) / 2.0
        late = (recent_scores[-2] + recent_scores[-1]) / 2.0
        if late > early + 1:
            form_trend = "rising"
        elif late < early - 1:
            form_trend = "falling"

    next_6_fdrs = [_safe_float(item.get("fdr"), 3.0) for item in future_fixtures]
    next_6_avg_fdr = (
        round(sum(next_6_fdrs) / len(next_6_fdrs), 2) if next_6_fdrs else 0.0
    )

    payload["recent_scores"] = recent_scores
    payload["xg_per_90"] = round(safe_divide(expected_goals, minutes) * 90, 3)
    payload["xa_per_90"] = round(safe_divide(expected_assists, minutes) * 90, 3)
    payload["xgi_per_90"] = round(
        safe_divide(expected_goal_involvements, minutes) * 90,
        3,
    )
    payload["shots_on_target_per_90"] = round(
        safe_divide(payload.get("shots_on_target", 0), minutes) * 90,
        3,
    )
    payload["npxg_per_90"] = round(
        safe_divide(payload.get("expected_goals_conceded", 0), minutes) * 90,
        3,
    )
    payload["form_trend"] = form_trend
    payload["next_6_avg_fdr"] = next_6_avg_fdr
    payload["next_6_green_fixtures"] = sum(1 for value in next_6_fdrs if value <= 2)
    payload["next_6_red_fixtures"] = sum(1 for value in next_6_fdrs if value >= 4)
    payload["team_goals_last_5"] = _safe_int(
        recent_team_context.get("team_goals_last_5"), 0
    )
    payload["team_clean_sheets_last_5"] = _safe_int(
        recent_team_context.get("team_clean_sheets_last_5"), 0
    )
    payload["team_conceded_last_5"] = _safe_int(
        recent_team_context.get("team_conceded_last_5"), 0
    )
    payload["team_form_rating"] = round(
        _safe_float(recent_team_context.get("team_form_rating"), 0.0),
        3,
    )
    payload["bonus_rate"] = round(
        safe_divide(payload.get("bonus", 0), minutes) * 90,
        3,
    )
    payload["cs_rate"] = round(
        safe_divide(payload.get("clean_sheets", 0), max(minutes / 90.0, 1)),
        3,
    )
    payload["next_6_fdrs"] = [round(value, 2) for value in next_6_fdrs]
    payload["next_6_fixtures"] = future_fixtures
    if future_fixtures:
        first_fixture = future_fixtures[0]
        payload["next_fixture"] = (
            f"{first_fixture.get('opponent_name', 'Unknown')} ({first_fixture.get('venue', 'H')})"
        )
        payload["next_fixture_fdr"] = _safe_int(first_fixture.get("fdr"), 3)
    else:
        payload["next_fixture"] = "No fixture"
        payload["next_fixture_fdr"] = 0
    return payload


def enrich_player_batch(
    conn: sqlite3.Connection, players: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    context = build_player_enrichment_context(conn, players)
    return [
        enrich_player_data(
            player,
            recent_scores_map=context["recent_scores_map"],
            team_future_fixtures_map=context["team_future_fixtures_map"],
            team_recent_context_map=context["team_recent_context_map"],
        )
        for player in players
    ]


def _team_badge_url(team_code: int) -> str:
    safe_code = max(1, _safe_int(team_code, 1))
    return TEAM_BADGE_TEMPLATE.format(team_code=safe_code)


def _player_image_url(
    photo_value: Any,
    player_code: int,
    player_name: str = "",
    player_id: int = 0,
    team_name: str = "",
    position: str = "",
) -> str:
    del photo_value, player_code, team_name, position
    image_url = local_player_image_path(player_id)
    normalized = _normalize_name(player_name)
    if normalized and image_url:
        PLAYER_IMAGE_CACHE[normalized] = image_url
    return image_url or DEFAULT_PLAYER_IMAGE


def _url_exists(url: str, timeout: int = 8) -> bool:
    if not url:
        return False
    try:
        request = Request(url, method="HEAD")
        with urlopen(request, timeout=timeout) as response:
            return getattr(response, "status", 200) < 400
    except HTTPError as exc:
        if exc.code in {403, 405}:
            try:
                request = Request(url)
                with urlopen(request, timeout=timeout) as response:
                    return getattr(response, "status", 200) < 400
            except (URLError, HTTPError, TimeoutError, ValueError):
                return False
        return False
    except (URLError, TimeoutError, ValueError):
        return False


def _player_image_cache_records(
    payload: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if isinstance(payload, dict):
        metadata = payload.get("metadata")
        records = payload.get("players")
        return (
            metadata if isinstance(metadata, dict) else {},
            records if isinstance(records, list) else [],
        )
    if isinstance(payload, list):
        return {}, payload
    return {}, []


def _load_player_image_cache_map() -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    if not PLAYER_IMAGE_PATH.exists():
        return {}, {}
    try:
        payload = json.loads(PLAYER_IMAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {}

    metadata, records = _player_image_cache_records(payload)
    cache: dict[int, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        player_id = _safe_int(record.get("player_id") or record.get("id"), 0)
        local_path = str(
            record.get("local_image_path")
            or record.get("image_url")
            or record.get("photo_url")
            or ""
        ).strip()
        if (
            player_id <= 0
            or not local_path
            or not _is_reliable_player_image(local_path)
        ):
            continue
        cache[player_id] = {
            "id": player_id,
            "player_id": player_id,
            "name": str(record.get("name") or "").strip(),
            "photo_id": str(record.get("photo_id") or "").strip(),
            "local_image_path": local_path,
            "image_url": local_path,
            "photo_url": local_path,
            "source": str(
                record.get("image_source") or record.get("source") or ""
            ).strip()
            or "unknown",
            "source_url": str(
                record.get("image_source_url") or record.get("source_url") or ""
            ).strip(),
            "is_verified": 1
            if _safe_int(
                record.get("image_is_verified")
                if record.get("image_is_verified") is not None
                else record.get("is_verified"),
                0,
            )
            else 0,
            "status": str(
                record.get("image_status") or record.get("status") or "fallback"
            ).strip(),
            "last_updated": str(record.get("last_updated") or "").strip(),
        }
    return metadata, cache


def _write_player_image_cache(
    cache: dict[int, dict[str, Any]], image_cache_version: str
) -> None:
    PLAYER_IMAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    players = []
    for player_id in sorted(cache):
        record = cache[player_id]
        players.append(
            {
                "player_id": player_id,
                "id": player_id,
                "name": str(record.get("name") or "").strip(),
                "photo_id": str(record.get("photo_id") or "").strip(),
                "local_image_path": str(
                    record.get("local_image_path") or record.get("image_url") or ""
                ).strip(),
                "image_url": str(
                    record.get("image_url") or record.get("local_image_path") or ""
                ).strip(),
                "photo_url": str(
                    record.get("photo_url")
                    or record.get("local_image_path")
                    or record.get("image_url")
                    or ""
                ).strip(),
                "source": str(record.get("source") or "default_placeholder").strip(),
                "image_source": str(
                    record.get("source") or "default_placeholder"
                ).strip(),
                "source_url": str(record.get("source_url") or "").strip(),
                "image_source_url": str(record.get("source_url") or "").strip(),
                "image_is_verified": 1
                if _safe_int(record.get("is_verified"), 0)
                else 0,
                "image_status": str(record.get("status") or "fallback").strip(),
                "last_updated": str(
                    record.get("last_updated") or _utc_now_iso()
                ).strip(),
            }
        )
    payload = {
        "metadata": {
            "image_cache_version": image_cache_version,
            "generated_at": _utc_now_iso(),
            "player_count": len(players),
        },
        "players": players,
    }
    PLAYER_IMAGE_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _image_source_for_url(url: Any) -> str:
    normalized = str(url or "").strip()
    if is_local_player_image_source(normalized):
        return "local_asset"
    if (
        "resources.premierleague.com/premierleague25/photos/players/250x250/"
        in normalized
    ):
        return "premierleague_scraped"
    if is_official_fpl_cdn_url(normalized):
        return "fpl_cdn"
    if is_generated_avatar_source(normalized):
        return "ui_avatar"
    if is_default_player_photo(normalized):
        return "default_placeholder"
    return "unknown"


def _player_name_from_element(element: dict[str, Any], fallback: str = "") -> str:
    first_name = str(element.get("first_name") or "").strip()
    second_name = str(element.get("second_name") or "").strip()
    full_name = f"{first_name} {second_name}".strip()
    return full_name or str(element.get("web_name") or fallback or "Player").strip()


def _season_id_from_database(conn: sqlite3.Connection) -> str:
    season_label = get_metadata_value(conn, "season_label", "")
    match = re.search(r"(20\d{2})", season_label)
    if match:
        return match.group(1)
    return PL_SEASON_ID


def _image_payload_is_valid(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    raw_bytes = bytes(payload.get("bytes") or b"")
    if not raw_bytes:
        return False
    width, height = image_dimensions(raw_bytes, payload.get("content_type"))
    if width <= 0 or height <= 0:
        return False
    if min(width, height) < 160:
        return False
    ratio = width / max(height, 1)
    return 0.45 <= ratio <= 2.2


def _resolved_local_image_path(player_id: int, payload: dict[str, Any]) -> str:
    extension = image_extension_from_content_type(
        payload.get("content_type"),
        payload.get("final_url") or payload.get("source_url") or "",
    )
    return local_player_image_path(player_id, extension)


def _resolved_local_image_file(player_id: int, payload: dict[str, Any]) -> Path:
    extension = image_extension_from_content_type(
        payload.get("content_type"),
        payload.get("final_url") or payload.get("source_url") or "",
    )
    return _player_local_image_file(player_id, extension)


def _player_image_candidate_url(candidate: dict[str, Any]) -> str:
    return str(
        candidate.get("source_url")
        or candidate.get("url")
        or candidate.get("reference_url")
        or ""
    ).strip()


def _build_team_source_candidates(
    rows: list[sqlite3.Row],
    latest_by_id: dict[int, dict[str, Any]],
    team_lookup: dict[int, dict[str, Any]],
    season_id: str,
    workers: int,
) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        player_id = _safe_int(row["id"], 0)
        if player_id <= 0:
            continue
        latest = latest_by_id.get(player_id, {})
        team_id = _safe_int(latest.get("team") or row["team_id"], 0)
        if team_id <= 0:
            continue
        grouped.setdefault(team_id, []).append(
            {
                "id": player_id,
                "first_name": str(
                    latest.get("first_name") or row["first_name"] or ""
                ).strip(),
                "second_name": str(
                    latest.get("second_name") or row["second_name"] or ""
                ).strip(),
                "web_name": str(
                    latest.get("web_name") or row["web_name"] or ""
                ).strip(),
                "name": _player_name_from_element(
                    latest, str(row["name"] or "").strip()
                ),
                "photo": str(latest.get("photo") or row["photo"] or "").strip(),
            }
        )
    team_index = fetch_premier_league_team_index(season_id)

    def resolve_team(team_id: int) -> tuple[int, dict[int, list[dict[str, Any]]]]:
        team_entry = team_lookup.get(team_id, {})
        return (
            team_id,
            build_team_image_source_candidates(
                team_name=str(team_entry.get("team_name") or "").strip(),
                official_website=str(team_entry.get("official_website") or "").strip(),
                players=grouped.get(team_id, []),
                season_id=season_id,
                team_index=team_index,
            ),
        )

    resolved: dict[int, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 2))) as executor:
        for _team_id, team_candidates in executor.map(resolve_team, sorted(grouped)):
            for player_id, player_candidates in team_candidates.items():
                resolved[player_id] = player_candidates
    return resolved


def _resolve_player_image_refresh_payload(payload: dict[str, Any]) -> dict[str, Any]:
    player_id = _safe_int(payload.get("player_id"), 0)
    player_name = str(payload.get("name") or "Player").strip()
    photo_value = str(payload.get("photo") or "").strip()
    candidates = [
        candidate
        for candidate in (payload.get("candidates") or [])
        if isinstance(candidate, dict)
    ]
    for candidate in candidates:
        candidate_url = _player_image_candidate_url(candidate)
        if not candidate_url or _is_blocked_image_source(candidate_url):
            continue
        try:
            remote_payload = fetch_remote_image_bytes(candidate_url)
        except Exception:
            continue
        if not _image_payload_is_valid(remote_payload):
            continue
        local_file = _resolved_local_image_file(player_id, remote_payload)
        local_path = _resolved_local_image_path(player_id, remote_payload)
        _remove_existing_player_image_files(player_id)
        _write_player_image_bytes(local_file, bytes(remote_payload.get("bytes") or b""))
        source = str(candidate.get("source") or _image_source_for_url(candidate_url)).strip() or "unknown"
        source_url = str(candidate.get("reference_url") or candidate_url).strip()
        is_verified = 1 if _safe_int(candidate.get("is_verified"), 1) else 0
        status = str(candidate.get("status") or "current").strip() or "current"
        last_updated = _utc_now_iso()
        return {
            "player_id": player_id,
            "id": player_id,
            "name": player_name,
            "photo": photo_value,
            "photo_id": extract_fpl_photo_id(photo_value),
            "local_image_path": local_path,
            "image_url": local_path,
            "photo_url": local_path,
            "source": source,
            "source_url": source_url,
            "is_verified": is_verified,
            "status": status,
            "last_updated": last_updated,
        }

    fallback_file = _player_local_image_file(player_id, "png")
    fallback_path = local_player_image_path(player_id)
    _remove_existing_player_image_files(player_id)
    _write_player_image_bytes(fallback_file, _load_default_player_image_bytes())
    source = "default_placeholder"
    source_url = DEFAULT_PLAYER_IMAGE
    is_verified = 0
    status = "fallback"
    last_updated = _utc_now_iso()
    return {
        "player_id": player_id,
        "id": player_id,
        "name": player_name,
        "photo": photo_value,
        "photo_id": extract_fpl_photo_id(photo_value),
        "local_image_path": fallback_path,
        "image_url": fallback_path,
        "photo_url": fallback_path,
        "source": source,
        "source_url": source_url,
        "is_verified": is_verified,
        "status": status,
        "last_updated": last_updated,
    }


def get_player_image(photo_value: Any) -> str:
    return get_official_fpl_image_url(photo_value) or DEFAULT_PLAYER_IMAGE


def refresh_player_images(
    conn: sqlite3.Connection,
    force: bool = False,
    workers: int = 8,
    limit: int | None = None,
    offset: int = 0,
    elements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cache_metadata, cache_map = _load_player_image_cache_map()
    PLAYER_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    all_rows = conn.execute(
        """
        SELECT id, first_name, second_name, web_name, name, code, photo,
               team_id, team_name, position, local_image_path, photo_url, image_url,
               image_source, image_source_url, image_is_verified, image_status
        FROM players
        ORDER BY id
        """
    ).fetchall()
    db_player_ids = {
        _safe_int(row["id"], 0) for row in all_rows if _safe_int(row["id"], 0) > 0
    }
    cache_map = {
        player_id: record
        for player_id, record in cache_map.items()
        if player_id in db_player_ids
    }

    latest_elements = elements
    if latest_elements is None:
        try:
            bootstrap = _fetch_json(FPL_BOOTSTRAP_URL)
            raw_elements = (
                bootstrap.get("elements", []) if isinstance(bootstrap, dict) else []
            )
            latest_elements = [item for item in raw_elements if isinstance(item, dict)]
        except (URLError, HTTPError, TimeoutError, ValueError, json.JSONDecodeError):
            latest_elements = []

    latest_by_id = {
        _safe_int(element.get("id"), 0): element
        for element in latest_elements or []
        if _safe_int(element.get("id"), 0) > 0
    }
    season_id = _season_id_from_database(conn)
    team_lookup = {
        _safe_int(row["id"], 0): {
            "team_name": str(row["name"] or "").strip(),
            "official_website": str(row["official_website"] or "").strip(),
        }
        for row in conn.execute(
            "SELECT id, name, official_website FROM teams"
        ).fetchall()
    }
    team_source_candidates = (
        _build_team_source_candidates(
            rows=all_rows,
            latest_by_id=latest_by_id,
            team_lookup=team_lookup,
            season_id=season_id,
            workers=workers,
        )
        if latest_by_id
        else {}
    )
    new_player_ids = sorted(set(latest_by_id) - db_player_ids)
    rows = list(all_rows)
    if offset > 0:
        rows = rows[offset:]
    if limit is not None and limit > 0:
        rows = rows[:limit]

    timestamp = _utc_now_iso()
    metadata_updates: list[
        tuple[str, str, str, str, str, str, str, int, str, str, int]
    ] = []
    pending: list[dict[str, Any]] = []
    skipped = 0

    for row in rows:
        player_id = _safe_int(row["id"], 0)
        if player_id <= 0:
            continue
        latest = latest_by_id.get(player_id, {})
        latest_name = _player_name_from_element(latest, str(row["name"] or "").strip())
        latest_photo = str(latest.get("photo") or row["photo"] or "").strip()
        latest_position = str(row["position"] or "").strip().upper()
        latest_team_name = str(row["team_name"] or "").strip()
        row_photo = str(row["photo"] or "").strip()
        current_url = str(
            row["local_image_path"] or row["photo_url"] or row["image_url"] or ""
        ).strip()
        current_source = str(row["image_source"] or "").strip()
        current_source_url = str(row["image_source_url"] or "").strip()
        current_is_verified = 1 if _safe_int(row["image_is_verified"], 0) else 0
        current_status = str(row["image_status"] or "").strip()
        cache_record = cache_map.get(player_id, {})
        cached_url = str(
            cache_record.get("local_image_path") or cache_record.get("image_url") or ""
        ).strip()
        cached_name = str(cache_record.get("name") or "").strip()
        cached_photo_id = str(cache_record.get("photo_id") or "").strip()
        latest_photo_id = extract_fpl_photo_id(latest_photo)
        photo_changed = latest_photo != row_photo
        should_refresh = (
            force
            or not current_url
            or not _is_reliable_player_image(current_url)
            or not cached_url
            or not _is_reliable_player_image(cached_url)
            or cached_name != latest_name
            or cached_photo_id != latest_photo_id
            or photo_changed
            or current_source not in TRUSTED_PLAYER_IMAGE_SOURCES
            or current_status in {"", "outdated", "missing", "fallback"}
            or current_is_verified == 0
        )
        if not should_refresh:
            skipped += 1
            cache_map[player_id] = {
                "id": player_id,
                "player_id": player_id,
                "name": latest_name,
                "photo_id": latest_photo_id,
                "local_image_path": current_url,
                "image_url": current_url,
                "photo_url": current_url,
                "source": current_source
                or str(cache_record.get("source") or "unknown"),
                "source_url": current_source_url
                or str(cache_record.get("source_url") or "").strip(),
                "is_verified": current_is_verified,
                "status": current_status or "current",
                "last_updated": str(cache_record.get("last_updated") or timestamp),
            }
            if photo_changed or latest_name != str(row["name"] or "").strip():
                metadata_updates.append(
                    (
                        latest_photo,
                        latest_name,
                        current_url,
                        current_url,
                        current_url,
                        current_source,
                        current_source_url,
                        current_is_verified,
                        current_status or "current",
                        timestamp,
                        player_id,
                    )
                )
            continue

        candidates: list[dict[str, Any]] = list(team_source_candidates.get(player_id, []))
        official_photo_url = get_official_fpl_image_url(latest_photo)
        if official_photo_url:
            candidates.append(
                {
                    "source": "fpl_cdn",
                    "source_url": official_photo_url,
                    "reference_url": official_photo_url,
                    "is_verified": 1,
                    "status": "current",
                }
            )
        pending.append(
            {
                "player_id": player_id,
                "name": latest_name,
                "photo": latest_photo,
                "team_name": latest_team_name,
                "position": latest_position,
                "force": force,
                "candidates": candidates,
            }
        )

    resolved_records: list[dict[str, Any]] = []
    if pending:
        for payload in pending:
            resolved_records.append(_resolve_player_image_refresh_payload(payload))

    for record in resolved_records:
        conn.execute(
            """
            UPDATE players
            SET photo = ?, name = ?, local_image_path = ?, photo_url = ?, image_url = ?,
                image_source = ?, image_source_url = ?, image_is_verified = ?, image_status = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                record["photo"],
                record["name"],
                record["local_image_path"],
                record["photo_url"],
                record["image_url"],
                record["source"],
                record["source_url"],
                record["is_verified"],
                record["status"],
                record["last_updated"],
                record["player_id"],
            ),
        )
        cache_map[record["player_id"]] = record

    if metadata_updates:
        conn.executemany(
            """
            UPDATE players
            SET photo = ?, name = ?, local_image_path = ?, photo_url = ?, image_url = ?,
                image_source = ?, image_source_url = ?, image_is_verified = ?, image_status = ?, updated_at = ?
            WHERE id = ?
            """,
            metadata_updates,
        )

    image_cache_version = str(cache_metadata.get("image_cache_version") or "").strip()
    if not image_cache_version or force or resolved_records or metadata_updates:
        image_cache_version = timestamp

    _write_player_image_cache(cache_map, image_cache_version)

    final_records = list(cache_map.values())
    source_counts = {
        "generated_openai": sum(
            1 for record in final_records if record.get("source") == "generated_openai"
        ),
        "generated_svg": sum(
            1 for record in final_records if record.get("source") == "generated_svg"
        ),
        "generated_cache": sum(
            1 for record in final_records if record.get("source") == "generated_cache"
        ),
        "default_placeholder": sum(
            1
            for record in final_records
            if record.get("source") == "default_placeholder"
        ),
    }
    result = {
        "players_processed": len(rows),
        "updated": len(resolved_records),
        "skipped": skipped,
        "new_players_detected": len(new_player_ids),
        "local_directory": str(PLAYER_IMAGE_DIR),
        "verified": sum(
            1 for record in final_records if _safe_int(record.get("is_verified"), 0)
        ),
        "source_counts": source_counts,
        "image_cache_version": image_cache_version,
    }
    _upsert_metadata(conn, "image_cache_version", image_cache_version)
    _upsert_metadata(conn, "player_image_source", "stable_local_assets")
    _upsert_metadata(conn, "player_image_last_refresh_utc", timestamp)
    _upsert_metadata(
        conn,
        "player_image_last_refresh_counts",
        json.dumps(result, ensure_ascii=True),
    )
    return result


def core_fpl_table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "teams": _safe_int(conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0], 0),
        "players": _safe_int(
            conn.execute("SELECT COUNT(*) FROM players").fetchone()[0],
            0,
        ),
        "fixtures": _safe_int(
            conn.execute("SELECT COUNT(*) FROM fixtures").fetchone()[0],
            0,
        ),
    }


def refresh_official_player_images(conn: sqlite3.Connection) -> None:
    refresh_player_images(conn, force=True)


def refresh_all_player_images(
    conn: sqlite3.Connection,
    force: bool = False,
    workers: int = 8,
    limit: int | None = None,
    offset: int = 0,
    elements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return refresh_player_images(
        conn,
        force=force,
        workers=workers,
        limit=limit,
        offset=offset,
        elements=elements,
    )


def update_current_season_images(
    conn: sqlite3.Connection,
    force: bool = False,
    workers: int = 8,
    limit: int | None = None,
    offset: int = 0,
    elements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return refresh_player_images(
        conn,
        force=force,
        workers=workers,
        limit=limit,
        offset=offset,
        elements=elements,
    )


def _resolve_team_website(team_name: str, short_name: str) -> str:
    candidates = [
        _normalize_name(team_name),
        _normalize_name(short_name),
        _normalize_name(team_name).replace("and", ""),
        _normalize_name(team_name).replace("united", "").strip(),
    ]
    for candidate in candidates:
        if candidate in TEAM_WEBSITE_MAP:
            return TEAM_WEBSITE_MAP[candidate]
    return ""


def _get_current_gameweek(events: list[dict[str, Any]]) -> int:
    for field in ("is_next", "is_current"):
        for event in events:
            if event.get(field):
                return _safe_int(event.get("id"), 1)

    unfinished = [
        _safe_int(event.get("id"), 0)
        for event in events
        if not event.get("finished") and _safe_int(event.get("id"), 0) > 0
    ]
    if unfinished:
        return min(unfinished)
    return 1


def _infer_season_label(events: list[dict[str, Any]]) -> str:
    deadline_values = [
        str(event.get("deadline_time") or "").strip()
        for event in events
        if str(event.get("deadline_time") or "").strip()
    ]
    if deadline_values:
        first_deadline = min(deadline_values)
        try:
            dt = datetime.fromisoformat(first_deadline.replace("Z", "+00:00"))
            start_year = dt.year if dt.month >= 7 else dt.year - 1
            return f"{start_year}/{str(start_year + 1)[-2:]}"
        except ValueError:
            pass

    current_year = _utc_now().year
    return f"{current_year}/{str(current_year + 1)[-2:]}"


def _avg_next_fdr(
    team_fixture_map: dict[int, list[tuple[int, int]]], team_id: int, horizon: int = 3
) -> float:
    fixtures = team_fixture_map.get(team_id, [])[:horizon]
    if not fixtures:
        return 3.0
    values = [difficulty for _, difficulty in fixtures]
    return round(sum(values) / len(values), 2)


def _upsert_metadata(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        """
        INSERT INTO metadata (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, str(value), _utc_now().isoformat()),
    )


def get_metadata_value(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    return str(row[0] or default)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            short_name TEXT NOT NULL,
            code INTEGER,
            fpl_team_id INTEGER,
            strength INTEGER DEFAULT 0,
            strength_overall_home INTEGER DEFAULT 0,
            strength_overall_away INTEGER DEFAULT 0,
            strength_attack_home INTEGER DEFAULT 0,
            strength_attack_away INTEGER DEFAULT 0,
            strength_defence_home INTEGER DEFAULT 0,
            strength_defence_away INTEGER DEFAULT 0,
            badge_url TEXT,
            official_website TEXT,
            website_source TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS gameweeks (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            deadline_time TEXT,
            average_entry_score INTEGER,
            highest_score INTEGER,
            is_current INTEGER DEFAULT 0,
            is_next INTEGER DEFAULT 0,
            is_finished INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS fixtures (
            id INTEGER PRIMARY KEY,
            gameweek INTEGER,
            kickoff_time TEXT,
            started INTEGER DEFAULT 0,
            finished INTEGER DEFAULT 0,
            home_team_id INTEGER NOT NULL,
            away_team_id INTEGER NOT NULL,
            home_team_name TEXT NOT NULL,
            away_team_name TEXT NOT NULL,
            home_fdr INTEGER DEFAULT 3,
            away_fdr INTEGER DEFAULT 3,
            home_score INTEGER,
            away_score INTEGER,
            updated_at TEXT,
            FOREIGN KEY (home_team_id) REFERENCES teams(id),
            FOREIGN KEY (away_team_id) REFERENCES teams(id)
        );

        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY,
            first_name TEXT,
            second_name TEXT,
            web_name TEXT,
            name TEXT NOT NULL,
            code INTEGER,
            photo TEXT,
            local_image_path TEXT,
            image_url TEXT NOT NULL,
            photo_url TEXT,
            image_source TEXT,
            image_source_url TEXT,
            image_is_verified INTEGER DEFAULT 0,
            image_status TEXT DEFAULT 'missing',
            team_id INTEGER NOT NULL,
            team_name TEXT NOT NULL,
            team_short TEXT NOT NULL,
            team_badge_url TEXT,
            position TEXT NOT NULL,
            element_type INTEGER NOT NULL,
            price REAL NOT NULL,
            points INTEGER DEFAULT 0,
            form REAL DEFAULT 0,
            fixture_difficulty REAL DEFAULT 3,
            selected_by_percent REAL DEFAULT 0,
            minutes INTEGER DEFAULT 0,
            starts INTEGER DEFAULT 0,
            goals INTEGER DEFAULT 0,
            assists INTEGER DEFAULT 0,
            clean_sheets INTEGER DEFAULT 0,
            bonus INTEGER DEFAULT 0,
            shots REAL DEFAULT 0,
            shots_on_target REAL DEFAULT 0,
            key_passes REAL DEFAULT 0,
            yellow_cards INTEGER DEFAULT 0,
            red_cards INTEGER DEFAULT 0,
            squad_number INTEGER,
            age INTEGER,
            height REAL,
            weight REAL,
            nationality TEXT,
            preferred_foot TEXT,
            market_value TEXT,
            recent_points_avg REAL DEFAULT 0,
            recent_minutes_avg REAL DEFAULT 0,
            recent_xg_avg REAL DEFAULT 0,
            recent_xa_avg REAL DEFAULT 0,
            consistency_score REAL DEFAULT 0,
            explosiveness_score REAL DEFAULT 0,
            upcoming_fixture_count INTEGER DEFAULT 0,
            upcoming_blank INTEGER DEFAULT 0,
            upcoming_double INTEGER DEFAULT 0,
            expected_goals REAL DEFAULT 0,
            expected_assists REAL DEFAULT 0,
            expected_goal_involvements REAL DEFAULT 0,
            expected_goals_conceded REAL DEFAULT 0,
            chance_of_playing_next_round INTEGER,
            status TEXT,
            news TEXT,
            updated_at TEXT,
            FOREIGN KEY (team_id) REFERENCES teams(id)
        );

        CREATE TABLE IF NOT EXISTS selected_squad (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            user_id INTEGER,
            squad_role TEXT NOT NULL,
            slot_order INTEGER NOT NULL,
            is_captain INTEGER NOT NULL DEFAULT 0,
            is_vice_captain INTEGER NOT NULL DEFAULT 0,
            saved_at TEXT NOT NULL,
            FOREIGN KEY (player_id) REFERENCES players(id)
        );

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS data_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT UNIQUE NOT NULL,
            source_url TEXT,
            notes TEXT,
            last_sync TEXT
        );

        CREATE TABLE IF NOT EXISTS player_external_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            season TEXT,
            rating REAL DEFAULT 0,
            form_score REAL DEFAULT 0,
            news_sentiment REAL DEFAULT 0,
            trend_score REAL DEFAULT 0,
            confidence_score REAL DEFAULT 0,
            minutes INTEGER DEFAULT 0,
            xg REAL DEFAULT 0,
            xa REAL DEFAULT 0,
            shots REAL DEFAULT 0,
            key_passes REAL DEFAULT 0,
            source_url TEXT,
            notes TEXT,
            updated_at TEXT,
            UNIQUE(player_id, provider, season),
            FOREIGN KEY (player_id) REFERENCES players(id)
        );

        CREATE TABLE IF NOT EXISTS player_gameweek_history (
            player_id INTEGER NOT NULL,
            fixture_id INTEGER NOT NULL,
            gameweek INTEGER,
            opponent_team_id INTEGER,
            was_home INTEGER DEFAULT 0,
            minutes INTEGER DEFAULT 0,
            total_points INTEGER DEFAULT 0,
            goals_scored INTEGER DEFAULT 0,
            assists INTEGER DEFAULT 0,
            clean_sheets INTEGER DEFAULT 0,
            expected_goals REAL DEFAULT 0,
            expected_assists REAL DEFAULT 0,
            selected INTEGER DEFAULT 0,
            value REAL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (player_id, fixture_id),
            FOREIGN KEY (player_id) REFERENCES players(id)
        );

        CREATE TABLE IF NOT EXISTS prediction_audit (
            player_id INTEGER NOT NULL,
            fixture_id INTEGER NOT NULL,
            gameweek INTEGER,
            predicted_points REAL DEFAULT 0,
            actual_points REAL DEFAULT 0,
            absolute_error REAL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (player_id, fixture_id),
            FOREIGN KEY (player_id) REFERENCES players(id)
        );

        CREATE TABLE IF NOT EXISTS ai_model_weights (
            key TEXT PRIMARY KEY,
            value REAL NOT NULL,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS gw_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            gameweek INTEGER NOT NULL,
            points INTEGER DEFAULT 0,
            bench_points INTEGER DEFAULT 0,
            captain_id INTEGER,
            captain_points INTEGER DEFAULT 0,
            transfers_made INTEGER DEFAULT 0,
            rank INTEGER DEFAULT 0,
            overall_rank INTEGER DEFAULT 0,
            saved_at TEXT NOT NULL,
            UNIQUE(user_id, gameweek),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS transfer_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            gameweek INTEGER NOT NULL,
            sold_player_id INTEGER,
            sold_player_name TEXT,
            bought_player_id INTEGER,
            bought_player_name TEXT,
            sold_points INTEGER DEFAULT 0,
            bought_points INTEGER DEFAULT 0,
            net_gain INTEGER DEFAULT 0,
            ai_recommended INTEGER DEFAULT 0,
            saved_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        """
    )
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_players_team_position
            ON players(team_id, position);
        CREATE INDEX IF NOT EXISTS idx_players_status
            ON players(status);
        CREATE INDEX IF NOT EXISTS idx_players_name
            ON players(name);
        CREATE INDEX IF NOT EXISTS idx_players_price
            ON players(price);
        CREATE INDEX IF NOT EXISTS idx_players_starts_minutes
            ON players(starts, recent_minutes_avg);
        CREATE INDEX IF NOT EXISTS idx_fixtures_gameweek
            ON fixtures(gameweek, finished, kickoff_time);
        CREATE INDEX IF NOT EXISTS idx_player_external_stats_lookup
            ON player_external_stats(player_id, provider, season);
        CREATE INDEX IF NOT EXISTS idx_player_history_gameweek
            ON player_gameweek_history(gameweek, player_id);
        CREATE INDEX IF NOT EXISTS idx_prediction_audit_gameweek
            ON prediction_audit(gameweek, player_id);
        CREATE INDEX IF NOT EXISTS idx_gw_history_user_gameweek
            ON gw_history(user_id, gameweek DESC);
        CREATE INDEX IF NOT EXISTS idx_transfer_outcomes_user_gameweek
            ON transfer_outcomes(user_id, gameweek DESC);
        CREATE INDEX IF NOT EXISTS idx_players_form
            ON players(form DESC);
        CREATE INDEX IF NOT EXISTS idx_players_ai_score
            ON players(ai_score DESC);
        CREATE INDEX IF NOT EXISTS idx_transfer_outcomes_bought_player
            ON transfer_outcomes(bought_player_id, gameweek DESC);
        CREATE INDEX IF NOT EXISTS idx_ai_learning_history_player
            ON ai_learning_history(player_id, gameweek DESC);
        """
    )


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _ensure_columns(
    conn: sqlite3.Connection, table_name: str, columns: dict[str, str]
) -> None:
    current_columns = _table_columns(conn, table_name)
    for column_name, column_def in columns.items():
        if column_name in current_columns:
            continue
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_def}")


def _migrate_schema(conn: sqlite3.Connection) -> None:
    _ensure_columns(
        conn,
        "players",
        {
            "bonus": "bonus INTEGER DEFAULT 0",
            "local_image_path": "local_image_path TEXT",
            "photo_url": "photo_url TEXT",
            "image_source": "image_source TEXT",
            "image_source_url": "image_source_url TEXT",
            "image_is_verified": "image_is_verified INTEGER DEFAULT 0",
            "image_status": "image_status TEXT DEFAULT 'missing'",
            "shots": "shots REAL DEFAULT 0",
            "shots_on_target": "shots_on_target REAL DEFAULT 0",
            "key_passes": "key_passes REAL DEFAULT 0",
            "yellow_cards": "yellow_cards INTEGER DEFAULT 0",
            "red_cards": "red_cards INTEGER DEFAULT 0",
            "age": "age INTEGER",
            "height": "height REAL",
            "weight": "weight REAL",
            "nationality": "nationality TEXT",
            "preferred_foot": "preferred_foot TEXT",
            "market_value": "market_value TEXT",
            "recent_points_avg": "recent_points_avg REAL DEFAULT 0",
            "recent_minutes_avg": "recent_minutes_avg REAL DEFAULT 0",
            "recent_xg_avg": "recent_xg_avg REAL DEFAULT 0",
            "recent_xa_avg": "recent_xa_avg REAL DEFAULT 0",
            "consistency_score": "consistency_score REAL DEFAULT 0",
            "explosiveness_score": "explosiveness_score REAL DEFAULT 0",
            "upcoming_fixture_count": "upcoming_fixture_count INTEGER DEFAULT 0",
            "upcoming_blank": "upcoming_blank INTEGER DEFAULT 0",
            "upcoming_double": "upcoming_double INTEGER DEFAULT 0",
            "expected_goal_involvements": "expected_goal_involvements REAL DEFAULT 0",
            "expected_goals_conceded": "expected_goals_conceded REAL DEFAULT 0",
        },
    )
    _ensure_columns(
        conn,
        "player_external_stats",
        {
            "rating": "rating REAL DEFAULT 0",
            "form_score": "form_score REAL DEFAULT 0",
            "news_sentiment": "news_sentiment REAL DEFAULT 0",
            "trend_score": "trend_score REAL DEFAULT 0",
            "confidence_score": "confidence_score REAL DEFAULT 0",
            "source_url": "source_url TEXT",
            "notes": "notes TEXT",
        },
    )
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_players_name
            ON players(name);
        CREATE INDEX IF NOT EXISTS idx_players_price
            ON players(price);
        CREATE INDEX IF NOT EXISTS idx_players_starts_minutes
            ON players(starts, recent_minutes_avg);
        CREATE INDEX IF NOT EXISTS idx_player_external_stats_lookup
            ON player_external_stats(player_id, provider, season);
        """
    )


def _schema_is_compatible(conn: sqlite3.Connection) -> bool:
    required_columns = {
        "teams": {"badge_url", "official_website", "website_source"},
        "players": {
            "local_image_path",
            "image_url",
            "photo_url",
            "image_source",
            "image_source_url",
            "image_is_verified",
            "image_status",
            "team_badge_url",
            "expected_goals",
            "expected_assists",
            "expected_goal_involvements",
            "expected_goals_conceded",
            "squad_number",
            "bonus",
            "shots",
            "shots_on_target",
            "key_passes",
            "yellow_cards",
            "red_cards",
            "age",
            "height",
            "weight",
            "nationality",
            "preferred_foot",
            "market_value",
            "recent_points_avg",
            "recent_minutes_avg",
            "recent_xg_avg",
            "recent_xa_avg",
            "consistency_score",
            "explosiveness_score",
            "upcoming_fixture_count",
            "upcoming_blank",
            "upcoming_double",
        },
        "player_external_stats": {
            "player_id",
            "provider",
            "rating",
            "form_score",
            "news_sentiment",
            "trend_score",
            "confidence_score",
            "minutes",
            "xg",
            "xa",
            "shots",
            "key_passes",
        },
        "fixtures": {"home_team_id", "away_team_id", "home_fdr", "away_fdr"},
        "player_gameweek_history": {"player_id", "fixture_id", "gameweek", "minutes"},
        "prediction_audit": {
            "player_id",
            "fixture_id",
            "predicted_points",
            "actual_points",
        },
        "ai_model_weights": {"key", "value", "updated_at"},
        "gw_history": {"user_id", "gameweek", "points", "saved_at"},
        "transfer_outcomes": {"user_id", "gameweek", "net_gain", "saved_at"},
    }

    for table_name, columns in required_columns.items():
        current_columns = _table_columns(conn, table_name)
        if not columns.issubset(current_columns):
            return False
    return True


def _reset_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS selected_squad;
        DROP TABLE IF EXISTS players;
        DROP TABLE IF EXISTS fixtures;
        DROP TABLE IF EXISTS gameweeks;
        DROP TABLE IF EXISTS teams;
        DROP TABLE IF EXISTS metadata;
        DROP TABLE IF EXISTS data_sources;
        DROP TABLE IF EXISTS player_external_stats;
        DROP TABLE IF EXISTS player_gameweek_history;
        DROP TABLE IF EXISTS prediction_audit;
        DROP TABLE IF EXISTS ai_model_weights;
        """
    )


def _seed_data_sources(conn: sqlite3.Connection, teams: list[dict[str, Any]]) -> None:
    synced_at = _utc_now().isoformat()
    conn.execute(
        "DELETE FROM data_sources WHERE source_name LIKE 'Official Club Website - %'"
    )
    base_sources = [
        (
            "Official Fantasy Premier League API",
            BOOTSTRAP_URL,
            "Primary source for live player, team, and gameweek data.",
            synced_at,
        ),
        (
            "Fantasy Premier League Website",
            "https://fantasy.premierleague.com",
            "Reference source for official rules, chip definitions, and scoring updates.",
            synced_at,
        ),
        (
            "Official FPL Fixtures API",
            FIXTURES_URL,
            "Official source for fixtures and difficulty ratings.",
            synced_at,
        ),
        (
            "Official FPL Player History API",
            "https://fantasy.premierleague.com/api/element-summary/{player_id}/",
            "Used for detailed per-player match history and charts.",
            synced_at,
        ),
        (
            "Premier League Official Website",
            "https://www.premierleague.com",
            "Reference source for league context, clubs, and official announcements.",
            synced_at,
        ),
        (
            "Premier League Clubs Directory",
            PREMIER_LEAGUE_CLUBS_URL,
            "Reference source for Premier League club website metadata.",
            synced_at,
        ),
        (
            "Understat",
            "https://understat.com",
            "Optional xG/xA provider for advanced expected goals data.",
            synced_at,
        ),
        (
            "FBref",
            "https://fbref.com",
            "Optional advanced statistics provider for detailed player and team metrics.",
            synced_at,
        ),
        (
            "SofaScore",
            "https://www.sofascore.com",
            "Priority external source for player ratings and live form context.",
            synced_at,
        ),
        (
            "Flashscore",
            "https://www.flashscore.com",
            "Supplementary source for recent form and match-level momentum signals.",
            synced_at,
        ),
        (
            "football-data.org",
            "https://www.football-data.org",
            "Optional fixtures and results provider for extended scheduling data.",
            synced_at,
        ),
    ]

    for row in base_sources:
        conn.execute(
            """
            INSERT INTO data_sources (source_name, source_url, notes, last_sync)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_name) DO UPDATE SET
                source_url = excluded.source_url,
                notes = excluded.notes,
                last_sync = excluded.last_sync
            """,
            row,
        )

    for team in teams:
        website = str(team.get("official_website") or "").strip()
        if not website:
            continue
        conn.execute(
            """
            INSERT INTO data_sources (source_name, source_url, notes, last_sync)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_name) DO UPDATE SET
                source_url = excluded.source_url,
                notes = excluded.notes,
                last_sync = excluded.last_sync
            """,
            (
                f"Official Club Website - {team['name']}",
                website,
                f"Official website reference for {team['name']}.",
                synced_at,
            ),
        )


def _apply_player_bio_overrides(conn: sqlite3.Connection) -> None:
    if not PLAYER_BIO_PATH.exists():
        return
    try:
        payload = json.loads(PLAYER_BIO_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, list):
        return

    for record in payload:
        if not isinstance(record, dict):
            continue
        player_id = _safe_int(record.get("id") or record.get("player_id"), 0)
        if player_id <= 0:
            continue
        age = record.get("age")
        height = record.get("height") or record.get("height_cm")
        weight = record.get("weight") or record.get("weight_kg")
        nationality = record.get("nationality")
        preferred_foot = record.get("preferred_foot") or record.get("foot")
        market_value = record.get("market_value")
        conn.execute(
            """
            UPDATE players
            SET age = ?, height = ?, weight = ?, nationality = ?, preferred_foot = ?, market_value = ?
            WHERE id = ?
            """,
            (
                _safe_int(age, 0) if age is not None else None,
                _safe_float(height, 0.0) if height is not None else None,
                _safe_float(weight, 0.0) if weight is not None else None,
                str(nationality) if nationality is not None else None,
                str(preferred_foot) if preferred_foot is not None else None,
                str(market_value) if market_value is not None else None,
                player_id,
            ),
        )


def _apply_player_image_overrides(conn: sqlite3.Connection) -> None:
    if not PLAYER_IMAGE_PATH.exists():
        return
    try:
        payload = json.loads(PLAYER_IMAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    _metadata, records = _player_image_cache_records(payload)
    if not records:
        return

    for record in records:
        if not isinstance(record, dict):
            continue
        player_id = _safe_int(record.get("id") or record.get("player_id"), 0)
        local_path = str(
            record.get("local_image_path")
            or record.get("image_url")
            or record.get("photo_url")
            or ""
        ).strip()
        if (
            player_id <= 0
            or not local_path
            or not _is_reliable_player_image(local_path)
        ):
            continue
        conn.execute(
            """
            UPDATE players
            SET local_image_path = ?, image_url = ?, photo_url = ?,
                image_source = ?, image_source_url = ?, image_is_verified = ?, image_status = ?
            WHERE id = ?
            """,
            (
                local_path,
                local_path,
                local_path,
                str(record.get("image_source") or record.get("source") or ""),
                str(record.get("image_source_url") or record.get("source_url") or ""),
                1 if _safe_int(record.get("image_is_verified"), 0) else 0,
                str(record.get("image_status") or "fallback"),
                player_id,
            ),
        )


def _seed_model_weights(conn: sqlite3.Connection) -> None:
    timestamp = _utc_now().isoformat()
    for key, value in DEFAULT_MODEL_WEIGHTS.items():
        conn.execute(
            """
            INSERT INTO ai_model_weights (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = COALESCE(ai_model_weights.value, excluded.value),
                updated_at = COALESCE(ai_model_weights.updated_at, excluded.updated_at)
            """,
            (key, value, timestamp),
        )


def _fetch_player_history_payload(player_id: int) -> dict[str, Any]:
    if player_id <= 0:
        return {"id": player_id, "history": []}
    try:
        payload = _fetch_json(
            PLAYER_HISTORY_URL_TEMPLATE.format(player_id=player_id), timeout=15
        )
    except (URLError, HTTPError, TimeoutError, ValueError, json.JSONDecodeError):
        payload = {}
    history = payload.get("history", []) if isinstance(payload, dict) else []
    return {"id": player_id, "history": history if isinstance(history, list) else []}


def _recent_player_metrics(history: list[dict[str, Any]]) -> dict[str, float]:
    recent = history[-5:] if history else []
    if not recent:
        return {
            "recent_points_avg": 0.0,
            "recent_minutes_avg": 0.0,
            "recent_xg_avg": 0.0,
            "recent_xa_avg": 0.0,
            "consistency_score": 0.0,
            "explosiveness_score": 0.0,
        }
    sample = max(len(recent), 1)
    points_values = [_safe_float(item.get("total_points"), 0.0) for item in recent]
    minutes_values = [_safe_float(item.get("minutes"), 0.0) for item in recent]
    xg_values = [_safe_float(item.get("expected_goals"), 0.0) for item in recent]
    xa_values = [_safe_float(item.get("expected_assists"), 0.0) for item in recent]
    consistency = sum(1 for value in points_values if value >= 5.0) / sample
    explosiveness = max(points_values) / 15.0 if points_values else 0.0
    return {
        "recent_points_avg": round(sum(points_values) / sample, 3),
        "recent_minutes_avg": round(sum(minutes_values) / sample, 3),
        "recent_xg_avg": round(sum(xg_values) / sample, 3),
        "recent_xa_avg": round(sum(xa_values) / sample, 3),
        "consistency_score": round(clamp(consistency, 0.0, 1.0), 3),
        "explosiveness_score": round(clamp(explosiveness, 0.0, 1.0), 3),
    }


def _news_sentiment_score(news: Any, status: Any, chance: Any) -> float:
    text = str(news or "").strip().lower()
    state = str(status or "a").strip().lower()
    play_chance = _safe_float(chance, 100.0)
    score = 0.0

    negative_markers = {
        "injury": 0.34,
        "hamstring": 0.26,
        "knee": 0.24,
        "ankle": 0.22,
        "illness": 0.18,
        "suspended": 0.35,
        "doubt": 0.2,
        "late test": 0.18,
        "ruled out": 0.38,
        "unknown return": 0.2,
        "knock": 0.12,
    }
    positive_markers = {
        "available": 0.16,
        "fit": 0.14,
        "returns": 0.12,
        "back in training": 0.18,
        "trained": 0.08,
        "cleared": 0.14,
        "starts": 0.1,
    }

    for marker, value in negative_markers.items():
        if marker in text:
            score -= value
    for marker, value in positive_markers.items():
        if marker in text:
            score += value

    if state in {"i", "s", "u"}:
        score -= 0.45
    elif state == "d":
        score -= 0.22

    if play_chance <= 25:
        score -= 0.38
    elif play_chance <= 50:
        score -= 0.24
    elif play_chance <= 75:
        score -= 0.12

    return round(clamp(score, -1.0, 1.0), 3)


def _sync_external_player_signals(
    conn: sqlite3.Connection, season_label: str, synced_at: str
) -> None:
    rows = conn.execute(
        """
        SELECT id, name, form, starts, minutes, selected_by_percent, shots, key_passes,
               expected_goals, expected_assists, recent_points_avg, recent_minutes_avg,
               recent_xg_avg, recent_xa_avg, consistency_score, explosiveness_score,
               fixture_difficulty, chance_of_playing_next_round, status, news
        FROM players
        """
    ).fetchall()
    if not rows:
        return

    payload_rows: list[tuple[Any, ...]] = []
    for row in rows:
        player_id = _safe_int(row["id"], 0)
        if player_id <= 0:
            continue

        recent_points_avg = _safe_float(row["recent_points_avg"], 0.0)
        recent_minutes_avg = _safe_float(row["recent_minutes_avg"], 0.0)
        recent_xg_avg = _safe_float(row["recent_xg_avg"], 0.0)
        recent_xa_avg = _safe_float(row["recent_xa_avg"], 0.0)
        consistency = clamp(_safe_float(row["consistency_score"], 0.0), 0.0, 1.0)
        explosiveness = clamp(_safe_float(row["explosiveness_score"], 0.0), 0.0, 1.0)
        fixture_difficulty = clamp(
            _safe_float(row["fixture_difficulty"], 3.0), 1.0, 5.0
        )
        ownership = clamp(_safe_float(row["selected_by_percent"], 0.0), 0.0, 100.0)
        base_form = clamp(_safe_float(row["form"], 0.0), 0.0, 10.0)
        starts = max(_safe_int(row["starts"], 0), 0)
        minutes = max(_safe_int(row["minutes"], 0), 0)
        shots = _safe_float(row["shots"], 0.0)
        key_passes = _safe_float(row["key_passes"], 0.0)
        xg_total = _safe_float(row["expected_goals"], 0.0)
        xa_total = _safe_float(row["expected_assists"], 0.0)
        news_sentiment = _news_sentiment_score(
            row["news"], row["status"], row["chance_of_playing_next_round"]
        )

        recent_form_score = round(
            clamp((recent_points_avg * 0.62) + (base_form * 0.38), 0.0, 10.0),
            2,
        )
        sofascore_rating = round(
            clamp(
                6.05
                + (recent_points_avg * 0.12)
                + (recent_xg_avg * 0.6)
                + (recent_xa_avg * 0.5)
                + (consistency * 0.28)
                + (explosiveness * 0.22)
                - ((fixture_difficulty - 2.5) * 0.08)
                + max(news_sentiment, 0.0) * 0.14,
                5.8,
                8.9,
            ),
            2,
        )
        flashscore_form = round(
            clamp(
                (recent_points_avg * 0.58)
                + (recent_minutes_avg / 90.0 * 2.2)
                + (starts / 12.0)
                + (consistency * 1.8),
                0.0,
                10.0,
            ),
            2,
        )
        trend_score = round(
            clamp(
                (ownership / 100.0 * 0.28)
                + (explosiveness * 0.32)
                + (recent_form_score / 10.0 * 0.26)
                + (max(news_sentiment, 0.0) * 0.14),
                0.0,
                1.0,
            ),
            3,
        )
        confidence_score = round(
            clamp(
                (recent_minutes_avg / 90.0 * 0.42)
                + (consistency * 0.24)
                + (min(minutes / 1800.0, 1.0) * 0.18)
                + ((1.0 - abs(news_sentiment)) * 0.16),
                0.0,
                1.0,
            )
            * 100.0,
            1,
        )

        payload_rows.extend(
            [
                (
                    player_id,
                    "sofascore",
                    season_label,
                    sofascore_rating,
                    recent_form_score,
                    news_sentiment,
                    trend_score,
                    confidence_score,
                    int(round(recent_minutes_avg * 5.0)),
                    xg_total,
                    xa_total,
                    shots,
                    key_passes,
                    "https://www.sofascore.com",
                    "SofaScore-style blended signal derived from recent form, threat, and availability.",
                    synced_at,
                ),
                (
                    player_id,
                    "flashscore",
                    season_label,
                    round(clamp(5.9 + (flashscore_form / 4.5), 5.9, 8.7), 2),
                    flashscore_form,
                    news_sentiment,
                    trend_score,
                    confidence_score,
                    minutes,
                    xg_total,
                    xa_total,
                    shots,
                    key_passes,
                    "https://www.flashscore.com",
                    "Flashscore-style momentum signal derived from starts, minutes security, and recent output.",
                    synced_at,
                ),
                (
                    player_id,
                    "news",
                    season_label,
                    0.0,
                    recent_form_score,
                    news_sentiment,
                    trend_score,
                    confidence_score,
                    minutes,
                    recent_xg_avg,
                    recent_xa_avg,
                    shots,
                    key_passes,
                    "https://fantasy.premierleague.com",
                    str(row["news"] or "").strip(),
                    synced_at,
                ),
                (
                    player_id,
                    "social",
                    season_label,
                    0.0,
                    recent_form_score,
                    news_sentiment,
                    trend_score,
                    confidence_score,
                    minutes,
                    xg_total,
                    xa_total,
                    shots,
                    key_passes,
                    "https://fantasy.premierleague.com",
                    "Optional social-style trend signal derived from ownership, explosiveness, and momentum.",
                    synced_at,
                ),
            ]
        )

    conn.executemany(
        """
        INSERT INTO player_external_stats (
            player_id, provider, season, rating, form_score, news_sentiment,
            trend_score, confidence_score, minutes, xg, xa, shots, key_passes,
            source_url, notes, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(player_id, provider, season) DO UPDATE SET
            rating = excluded.rating,
            form_score = excluded.form_score,
            news_sentiment = excluded.news_sentiment,
            trend_score = excluded.trend_score,
            confidence_score = excluded.confidence_score,
            minutes = excluded.minutes,
            xg = excluded.xg,
            xa = excluded.xa,
            shots = excluded.shots,
            key_passes = excluded.key_passes,
            source_url = excluded.source_url,
            notes = excluded.notes,
            updated_at = excluded.updated_at
        """,
        payload_rows,
    )


def _naive_predicted_points(
    minutes: float, xg: float, xa: float, fixture_ease: float
) -> float:
    base = (
        1.5 + ((minutes / 90.0) * 1.8) + (xg * 3.4) + (xa * 2.6) + (fixture_ease * 0.45)
    )
    return round(max(base, 0.0), 2)


def _adjust_model_weights_from_history(conn: sqlite3.Connection) -> None:
    existing = conn.execute("SELECT COUNT(*) FROM ai_model_weights").fetchone()[0]
    if _safe_int(existing, 0) > 0:
        return
    rows = conn.execute(
        """
        SELECT h.minutes, h.total_points, h.expected_goals, h.expected_assists,
               f.home_fdr, f.away_fdr, h.was_home
        FROM player_gameweek_history h
        JOIN fixtures f ON f.id = h.fixture_id
        WHERE h.gameweek IS NOT NULL
        ORDER BY h.gameweek DESC
        LIMIT 3000
        """
    ).fetchall()
    if not rows:
        _seed_model_weights(conn)
        return

    total_actual = 0.0
    total_xg = 0.0
    total_xa = 0.0
    total_minutes = 0.0
    total_error = 0.0
    timestamp = _utc_now().isoformat()
    for row in rows:
        actual = _safe_float(row[1], 0.0)
        minutes = _safe_float(row[0], 0.0)
        xg = _safe_float(row[2], 0.0)
        xa = _safe_float(row[3], 0.0)
        difficulty = _safe_float(row[4] if row[6] else row[5], 3.0)
        ease = clamp(6.0 - difficulty, 1.0, 5.0)
        predicted = _naive_predicted_points(minutes, xg, xa, ease)
        total_actual += actual
        total_xg += xg
        total_xa += xa
        total_minutes += minutes / 90.0
        total_error += abs(predicted - actual)

    total_signal = max(total_xg + total_xa + total_minutes, 0.001)
    learned = dict(DEFAULT_MODEL_WEIGHTS)
    learned["xg"] = round(clamp(total_xg / total_signal, 0.08, 0.28), 4)
    learned["xa"] = round(clamp(total_xa / total_signal, 0.06, 0.22), 4)
    learned["minutes"] = round(clamp(total_minutes / total_signal, 0.05, 0.18), 4)
    learned["consistency"] = round(
        clamp(
            0.02 + (1.0 - min(total_error / max(total_actual, 1.0), 1.0)) * 0.08,
            0.02,
            0.10,
        ),
        4,
    )
    learned["recent_form"] = round(
        clamp(0.03 + learned["consistency"] * 0.4, 0.03, 0.08), 4
    )
    learned["explosiveness"] = round(clamp(0.02 + learned["xg"] * 0.12, 0.02, 0.06), 4)

    used = sum(learned.values())
    if used > 0:
        scale = 1.0 / used
        learned = {key: round(value * scale, 4) for key, value in learned.items()}

    for key, value in learned.items():
        conn.execute(
            """
            INSERT INTO ai_model_weights (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, timestamp),
        )


def _run_automated_learning_cycle(
    conn: sqlite3.Connection, current_gameweek: int
) -> None:
    try:
        from ai.engine import load_weights_from_db, predict_player
        from ai.learning import (
            init_learning_tables,
            log_predictions,
            run_post_gameweek_learning,
        )
    except Exception:
        return

    init_learning_tables(conn)
    load_weights_from_db(conn)
    rows = conn.execute(
        """
        SELECT p.id, p.position, p.form, p.minutes, p.expected_goals, p.expected_assists,
               p.fixture_difficulty, p.selected_by_percent, p.shots, p.key_passes,
               p.recent_points_avg, p.recent_minutes_avg, p.consistency_score,
               p.explosiveness_score, p.upcoming_fixture_count, p.upcoming_blank,
               p.upcoming_double, p.status, p.chance_of_playing_next_round,
               t.strength_attack_home, t.strength_attack_away,
               t.strength_defence_home, t.strength_defence_away
        FROM players p
        JOIN teams t ON t.id = p.team_id
        WHERE COALESCE(p.team_id, 0) > 0
          AND COALESCE(p.status, 'a') <> 'u'
        """
    ).fetchall()

    predictions: list[tuple[int, float]] = []
    for row in rows:
        player = {key: row[key] for key in row.keys()}
        player["team_attack_strength"] = round(
            (
                _safe_int(row["strength_attack_home"], 0)
                + _safe_int(row["strength_attack_away"], 0)
            )
            / 2,
            1,
        )
        player["team_defence_strength"] = round(
            (
                _safe_int(row["strength_defence_home"], 0)
                + _safe_int(row["strength_defence_away"], 0)
            )
            / 2,
            1,
        )
        prediction = predict_player(player)
        predictions.append(
            (
                _safe_int(player.get("id"), 0),
                _safe_float(prediction.get("predicted_points_next_game"), 0.0),
            )
        )

    log_predictions(conn, current_gameweek, predictions)
    run_post_gameweek_learning(conn, current_gameweek)


def _sync_player_histories(
    conn: sqlite3.Connection,
    player_ids: list[int],
    current_gameweek: int,
    team_fixture_map: dict[int, list[tuple[int, int]]],
    player_team_map: dict[int, int],
    synced_at: str,
) -> None:
    if not player_ids:
        return

    history_rows: list[tuple[Any, ...]] = []
    prediction_rows: list[tuple[Any, ...]] = []
    player_updates: list[tuple[Any, ...]] = []

    with ThreadPoolExecutor(max_workers=HISTORY_SYNC_WORKERS) as executor:
        payloads = list(executor.map(_fetch_player_history_payload, player_ids))

    for payload in payloads:
        player_id = _safe_int(payload.get("id"), 0)
        history = payload.get("history", []) if isinstance(payload, dict) else []
        if player_id <= 0:
            continue
        metrics = _recent_player_metrics(history if isinstance(history, list) else [])
        upcoming_window = [
            fixture
            for fixture in team_fixture_map.get(player_team_map.get(player_id, 0), [])
            if current_gameweek <= fixture[0] <= current_gameweek + 4
        ]
        future_gameweeks = [fixture[0] for fixture in upcoming_window]
        upcoming_fixture_count = len(upcoming_window)
        upcoming_double = 1 if len(set(future_gameweeks)) < len(future_gameweeks) else 0
        upcoming_blank = 1 if upcoming_fixture_count == 0 else 0
        player_updates.append(
            (
                metrics["recent_points_avg"],
                metrics["recent_minutes_avg"],
                metrics["recent_xg_avg"],
                metrics["recent_xa_avg"],
                metrics["consistency_score"],
                metrics["explosiveness_score"],
                upcoming_fixture_count,
                upcoming_blank,
                upcoming_double,
                player_id,
            )
        )

        for entry in history if isinstance(history, list) else []:
            fixture_id = _safe_int(entry.get("fixture"), 0)
            if fixture_id <= 0:
                continue
            round_id = _safe_int(entry.get("round"), 0)
            was_home = 1 if entry.get("was_home") else 0
            minutes = _safe_int(entry.get("minutes"), 0)
            total_points = _safe_int(entry.get("total_points"), 0)
            xg = _safe_float(entry.get("expected_goals"), 0.0)
            xa = _safe_float(entry.get("expected_assists"), 0.0)
            history_rows.append(
                (
                    player_id,
                    fixture_id,
                    round_id,
                    _safe_int(entry.get("opponent_team"), 0),
                    was_home,
                    minutes,
                    total_points,
                    _safe_int(entry.get("goals_scored"), 0),
                    _safe_int(entry.get("assists"), 0),
                    _safe_int(entry.get("clean_sheets"), 0),
                    xg,
                    xa,
                    _safe_int(entry.get("selected"), 0),
                    _safe_float(entry.get("value"), 0.0),
                    synced_at,
                )
            )
            fixture_row = conn.execute(
                "SELECT home_fdr, away_fdr FROM fixtures WHERE id = ?",
                (fixture_id,),
            ).fetchone()
            difficulty = 3.0
            if fixture_row:
                difficulty = _safe_float(
                    fixture_row[0] if was_home else fixture_row[1],
                    3.0,
                )
            predicted = _naive_predicted_points(
                minutes, xg, xa, clamp(6.0 - difficulty, 1.0, 5.0)
            )
            prediction_rows.append(
                (
                    player_id,
                    fixture_id,
                    round_id,
                    predicted,
                    float(total_points),
                    round(abs(predicted - float(total_points)), 3),
                    synced_at,
                )
            )

    conn.execute("DELETE FROM player_gameweek_history")
    conn.execute("DELETE FROM prediction_audit")
    if history_rows:
        conn.executemany(
            """
            INSERT INTO player_gameweek_history (
                player_id, fixture_id, gameweek, opponent_team_id, was_home,
                minutes, total_points, goals_scored, assists, clean_sheets,
                expected_goals, expected_assists, selected, value, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            history_rows,
        )
    if prediction_rows:
        conn.executemany(
            """
            INSERT INTO prediction_audit (
                player_id, fixture_id, gameweek, predicted_points, actual_points,
                absolute_error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            prediction_rows,
        )
    if player_updates:
        conn.executemany(
            """
            UPDATE players
            SET recent_points_avg = ?,
                recent_minutes_avg = ?,
                recent_xg_avg = ?,
                recent_xa_avg = ?,
                consistency_score = ?,
                explosiveness_score = ?,
                upcoming_fixture_count = ?,
                upcoming_blank = ?,
                upcoming_double = ?
            WHERE id = ?
            """,
            player_updates,
        )
    _adjust_model_weights_from_history(conn)


def sync_official_data(conn: sqlite3.Connection) -> None:
    global PLAYER_ENRICH_COUNT, PLAYER_IMAGE_LOOKUPS
    PLAYER_ENRICH_COUNT = 0
    PLAYER_BIO_CACHE.clear()
    PLAYER_IMAGE_LOOKUPS = 0
    PLAYER_IMAGE_CACHE.clear()
    logger.info("Official FPL sync started")
    bootstrap, fixtures_payload = fetch_sync_bundle()

    teams = bootstrap.get("teams", []) if isinstance(bootstrap, dict) else []
    events = bootstrap.get("events", []) if isinstance(bootstrap, dict) else []
    elements = bootstrap.get("elements", []) if isinstance(bootstrap, dict) else []

    if not teams or not elements:
        raise ValueError("Official FPL API did not return team/player data.")

    current_gameweek = _get_current_gameweek(events)
    season_label = _infer_season_label(events)
    synced_at = _utc_now().isoformat()

    team_rows: list[tuple[Any, ...]] = []
    team_name_lookup: dict[int, str] = {}
    team_short_lookup: dict[int, str] = {}
    badge_lookup: dict[int, str] = {}

    for team in teams:
        team_id = _safe_int(team.get("id"), 0)
        team_name = str(team.get("name") or "Unknown")
        short_name = str(team.get("short_name") or "UNK")
        website = _resolve_team_website(team_name, short_name)
        badge_url = _team_badge_url(_safe_int(team.get("code"), 0))

        team_rows.append(
            (
                team_id,
                team_name,
                short_name,
                _safe_int(team.get("code"), 0),
                _safe_int(team.get("strength"), 0),
                _safe_int(team.get("strength_overall_home"), 0),
                _safe_int(team.get("strength_overall_away"), 0),
                _safe_int(team.get("strength_attack_home"), 0),
                _safe_int(team.get("strength_attack_away"), 0),
                _safe_int(team.get("strength_defence_home"), 0),
                _safe_int(team.get("strength_defence_away"), 0),
                badge_url,
                website,
                PREMIER_LEAGUE_CLUBS_URL,
                synced_at,
            )
        )
        team_name_lookup[team_id] = team_name
        team_short_lookup[team_id] = short_name
        badge_lookup[team_id] = badge_url

    team_fixture_map: dict[int, list[tuple[int, int]]] = {
        team_id: [] for team_id in team_name_lookup
    }
    fixture_rows: list[tuple[Any, ...]] = []

    for fixture in fixtures_payload:
        gameweek = _safe_int(fixture.get("event"), 0)
        home_team_id = _safe_int(fixture.get("team_h"), 0)
        away_team_id = _safe_int(fixture.get("team_a"), 0)
        home_fdr = _safe_int(fixture.get("team_h_difficulty"), 3)
        away_fdr = _safe_int(fixture.get("team_a_difficulty"), 3)
        fixture_id = _safe_int(fixture.get("id"), 0)

        if fixture_id <= 0 or home_team_id <= 0 or away_team_id <= 0:
            continue

        fixture_rows.append(
            (
                fixture_id,
                gameweek,
                fixture.get("kickoff_time"),
                1 if fixture.get("started") else 0,
                1 if fixture.get("finished") else 0,
                home_team_id,
                away_team_id,
                team_name_lookup.get(home_team_id, "Unknown"),
                team_name_lookup.get(away_team_id, "Unknown"),
                home_fdr,
                away_fdr,
                fixture.get("team_h_score"),
                fixture.get("team_a_score"),
                synced_at,
            )
        )

        if gameweek >= current_gameweek and not fixture.get("finished"):
            team_fixture_map.setdefault(home_team_id, []).append((gameweek, home_fdr))
            team_fixture_map.setdefault(away_team_id, []).append((gameweek, away_fdr))

    for team_id in team_fixture_map:
        team_fixture_map[team_id].sort(key=lambda item: item[0])

    gameweek_rows = [
        (
            _safe_int(event.get("id"), 0),
            str(event.get("name") or f"GW {_safe_int(event.get('id'), 0)}"),
            event.get("deadline_time"),
            _safe_int(event.get("average_entry_score"), 0),
            _safe_int(event.get("highest_score"), 0),
            1 if event.get("is_current") else 0,
            1 if event.get("is_next") else 0,
            1 if event.get("finished") else 0,
        )
        for event in events
        if _safe_int(event.get("id"), 0) > 0
    ]

    player_rows: list[tuple[Any, ...]] = []
    player_ids_for_history: list[int] = []
    player_team_map: dict[int, int] = {}

    for element in elements:
        player_id = _safe_int(element.get("id"), 0)
        team_id = _safe_int(element.get("team"), 0)
        position_code = POSITION_MAP.get(
            _safe_int(element.get("element_type"), 0), "MID"
        )
        player_code = _safe_int(element.get("code"), 0)
        photo_value = element.get("photo") or ""
        price = round(_safe_float(element.get("now_cost"), 0.0) / 10.0, 1)
        shots_value = element.get("shots")
        if shots_value is None:
            shots_value = element.get("threat")
        key_passes_value = element.get("key_passes")
        if key_passes_value is None:
            key_passes_value = element.get("creativity")

        if player_id <= 0 or team_id <= 0:
            continue

        player_ids_for_history.append(player_id)
        player_team_map[player_id] = team_id

        player_name = (
            f"{str(element.get('first_name') or '').strip()} {str(element.get('second_name') or '').strip()}".strip()
            or str(element.get("web_name") or "Unknown Player")
        )
        age, height, weight, nationality, preferred_foot, market_value = (
            None,
            None,
            None,
            None,
            None,
            None,
        )

        image_url = _player_image_url(
            photo_value,
            player_code,
            player_name,
            player_id,
            team_name_lookup.get(team_id, "Unknown"),
            position_code,
        )

        player_rows.append(
            (
                player_id,
                str(element.get("first_name") or ""),
                str(element.get("second_name") or ""),
                str(element.get("web_name") or ""),
                player_name,
                player_code,
                photo_value,
                image_url,
                image_url,
                "",
                "",
                0,
                "missing",
                team_id,
                team_name_lookup.get(team_id, "Unknown"),
                team_short_lookup.get(team_id, "UNK"),
                badge_lookup.get(team_id, _team_badge_url(0)),
                position_code,
                _safe_int(element.get("element_type"), 0),
                price,
                _safe_int(element.get("total_points"), 0),
                _safe_float(element.get("form"), 0.0),
                _avg_next_fdr(team_fixture_map, team_id, horizon=3),
                _safe_float(element.get("selected_by_percent"), 0.0),
                _safe_int(element.get("minutes"), 0),
                _safe_int(element.get("starts"), 0),
                _safe_int(element.get("goals_scored"), 0),
                _safe_int(element.get("assists"), 0),
                _safe_int(element.get("clean_sheets"), 0),
                _safe_int(element.get("bonus"), 0),
                _safe_float(shots_value, 0.0),
                _safe_float(element.get("shots_on_target"), 0.0),
                _safe_float(key_passes_value, 0.0),
                _safe_int(element.get("yellow_cards"), 0),
                _safe_int(element.get("red_cards"), 0),
                _safe_int(element.get("squad_number"), 0),
                age,
                height,
                weight,
                nationality,
                preferred_foot,
                market_value,
                _safe_float(element.get("expected_goals"), 0.0),
                _safe_float(element.get("expected_assists"), 0.0),
                _safe_float(element.get("expected_goal_involvements"), 0.0),
                _safe_float(element.get("expected_goals_conceded"), 0.0),
                element.get("chance_of_playing_next_round"),
                str(element.get("status") or "a"),
                str(element.get("news") or ""),
                synced_at,
            )
        )
    logger.info("Official FPL sync loaded %s players", len(player_rows))
    cursor = conn.cursor()
    existing_tables = {
        str(row[0])
        for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }

    cursor.execute("PRAGMA foreign_keys = OFF")

    if "free_hit_squad" in existing_tables:
        cursor.execute("DELETE FROM free_hit_squad")
    cursor.execute("DELETE FROM selected_squad")
    cursor.execute("DELETE FROM player_external_stats")
    cursor.execute("DELETE FROM player_gameweek_history")
    cursor.execute("DELETE FROM prediction_audit")
    cursor.execute("DELETE FROM players")
    cursor.execute("DELETE FROM fixtures")
    cursor.execute("DELETE FROM gameweeks")
    cursor.execute("DELETE FROM teams")

    cursor.execute("PRAGMA foreign_keys = ON")

    conn.commit()

    conn.executemany(
        """
        INSERT INTO teams (
            id, name, short_name, code, strength,
            strength_overall_home, strength_overall_away,
            strength_attack_home, strength_attack_away,
            strength_defence_home, strength_defence_away,
            badge_url, official_website, website_source, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        team_rows,
    )

    conn.executemany(
        """
        INSERT INTO gameweeks (
            id, name, deadline_time, average_entry_score,
            highest_score, is_current, is_next, is_finished
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        gameweek_rows,
    )

    conn.executemany(
        """
        INSERT INTO fixtures (
            id, gameweek, kickoff_time, started, finished,
            home_team_id, away_team_id, home_team_name, away_team_name,
            home_fdr, away_fdr, home_score, away_score, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        fixture_rows,
    )

    conn.executemany(
        """
        INSERT INTO players (
            id, first_name, second_name, web_name, name,
            code, photo, image_url, photo_url, image_source, image_source_url, image_is_verified, image_status,
            team_id, team_name, team_short, team_badge_url,
            position, element_type, price, points, form,
            fixture_difficulty, selected_by_percent, minutes, starts,
            goals, assists, clean_sheets, bonus, shots, shots_on_target, key_passes, yellow_cards, red_cards,
            squad_number, age, height, weight, nationality, preferred_foot, market_value,
            expected_goals, expected_assists, expected_goal_involvements, expected_goals_conceded,
            chance_of_playing_next_round, status, news, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        player_rows,
    )

    _sync_player_histories(
        conn,
        player_ids_for_history,
        current_gameweek,
        team_fixture_map,
        player_team_map,
        synced_at,
    )
    _sync_external_player_signals(conn, season_label, synced_at)

    _apply_player_bio_overrides(conn)
    _apply_player_image_overrides(conn)
    refresh_player_images(conn, force=True, elements=elements)

    conn.execute(
        "UPDATE players SET image_url = ? WHERE image_url IS NULL OR image_url = ''",
        (DEFAULT_PLAYER_IMAGE,),
    )
    conn.execute(
        "UPDATE players SET local_image_path = image_url WHERE local_image_path IS NULL OR local_image_path = ''"
    )
    conn.execute(
        "UPDATE players SET photo_url = image_url WHERE photo_url IS NULL OR photo_url = ''"
    )

    _seed_data_sources(
        conn,
        [
            {
                "name": row[1],
                "official_website": row[12],
            }
            for row in team_rows
        ],
    )

    _upsert_metadata(conn, "season_label", season_label)
    _upsert_metadata(conn, "current_gameweek", current_gameweek)
    _upsert_metadata(conn, "last_sync_utc", synced_at)
    _upsert_metadata(conn, "data_provider", "Official Fantasy Premier League API")
    _upsert_metadata(conn, "club_website_source", PREMIER_LEAGUE_CLUBS_URL)
    _upsert_metadata(conn, "total_teams", len(team_rows))
    _upsert_metadata(conn, "total_players", len(player_rows))
    _upsert_metadata(conn, "total_fixtures", len(fixture_rows))

    _run_automated_learning_cycle(conn, current_gameweek)

    conn.commit()


def full_data_sync(conn: sqlite3.Connection) -> dict[str, int]:
    _ensure_schema(conn)
    _migrate_schema(conn)
    _seed_model_weights(conn)
    try:
        sync_official_data(conn)
    except Exception:
        logger.exception("Official FPL sync failed")
        raise
    counts = core_fpl_table_counts(conn)
    _upsert_metadata(conn, "active_database_path", str(DB_PATH))
    _upsert_metadata(conn, "last_sync_counts", json.dumps(counts, ensure_ascii=True))
    _upsert_metadata(conn, "last_sync_success_utc", _utc_now().isoformat())
    conn.commit()
    return counts


def safe_sync(conn: sqlite3.Connection) -> dict[str, Any]:
    previous_counts = core_fpl_table_counts(conn)
    previous_sync = get_metadata_value(conn, "last_sync_utc", "")
    try:
        counts = full_data_sync(conn)
        _upsert_metadata(conn, "last_sync_status", "ok")
        _upsert_metadata(conn, "last_sync_time", _utc_now().isoformat())
        _upsert_metadata(conn, "last_sync_error", "")
        conn.commit()
        return {"status": "ok", "counts": counts, "source": "live"}
    except Exception as exc:
        logger.exception("Safe sync failed, reusing last valid dataset")
        _upsert_metadata(conn, "last_sync_status", "fallback")
        _upsert_metadata(conn, "last_sync_time", _utc_now().isoformat())
        _upsert_metadata(conn, "last_sync_error", str(exc))
        _upsert_metadata(
            conn,
            "last_valid_sync_utc",
            previous_sync or get_metadata_value(conn, "last_sync_success_utc", ""),
        )
        conn.commit()
        return {
            "status": "fallback",
            "counts": previous_counts,
            "source": "last_valid",
            "error": str(exc),
        }


def init_database(force_refresh: bool = False) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 60000")
    _ensure_schema(conn)
    _migrate_schema(conn)
    _seed_model_weights(conn)
    conn.commit()

    players_count = _safe_int(
        conn.execute("SELECT COUNT(*) FROM players").fetchone()[0],
        0,
    )
    last_sync_raw = get_metadata_value(conn, "last_sync_utc")
    stale = True
    if last_sync_raw:
        try:
            last_sync = datetime.fromisoformat(last_sync_raw)
            stale = (_utc_now() - last_sync) > timedelta(hours=12)
        except ValueError:
            stale = True

    should_sync = force_refresh or players_count == 0 or stale

    if should_sync:
        try:
            full_data_sync(conn)
            _upsert_metadata(conn, "last_sync_error", "")
            conn.commit()
        except (URLError, HTTPError, TimeoutError, ValueError) as exc:
            logger.exception("Database initialization sync failed")
            _upsert_metadata(conn, "last_sync_error", str(exc))
            conn.commit()
            if players_count == 0:
                conn.close()
                raise RuntimeError(
                    "Could not initialize the database from official FPL data. "
                    "Check your internet connection and try again."
                ) from exc

    conn.close()


if __name__ == "__main__":
    init_database(force_refresh=True)
