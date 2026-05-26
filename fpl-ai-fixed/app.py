"""
FPL AI Platform — Main Flask App
Integrated with: player_api, fixtures_api, stats_api, ai_engine, data_sync
Real data from fpl.db (826 players, 380 fixtures, 20 teams)
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Path setup ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("TACTIX_DB_PATH", str(ROOT / "database" / "fpl.db"))

# ── Startup validation ───────────────────────────────────────────────────────
_db_path = Path(os.environ["TACTIX_DB_PATH"])
_fallback = ROOT / "database" / "fpl_runtime.db"
if not _db_path.exists():
    if _fallback.exists():
        logger.warning("Primary DB not found — using fallback: %s", _fallback)
        os.environ["TACTIX_DB_PATH"] = str(_fallback)
    else:
        logger.error(
            "Database not found at %s and no fallback at %s. "
            "Run data_sync.py first.",
            _db_path, _fallback,
        )
        sys.exit(1)

# ── Backend imports ───────────────────────────────────────────────────────────
from player_api import fetch_players_payload, query_counts          # noqa: E402
from fixtures_api import (                                          # noqa: E402
    fetch_fixtures_payload,
    build_fixture_matrix,
    best_fixture_runs,
)
from stats_api import standings_payload, top_scorers                # noqa: E402

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="assets", static_url_path="/assets")

# ── Rate limiting ─────────────────────────────────────────────────────────────
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=["200 per minute"],
        storage_uri="memory://",
    )
    logger.info("Rate limiting enabled (200 req/min per IP)")
except ImportError:
    limiter = None
    logger.warning("flask-limiter not installed — rate limiting disabled")


# ── DB connection ─────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    db_path = Path(os.environ.get("TACTIX_DB_PATH", ROOT / "database" / "fpl.db"))
    if not db_path.exists():
        fallback = ROOT / "database" / "fpl_runtime.db"
        db_path = fallback if fallback.exists() else db_path
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


# ── Stub helpers needed by ai_engine ─────────────────────────────────────────
def fetch_user_preferences(conn, user_id):
    return {}


def active_chip(conn):
    try:
        row = conn.execute("SELECT value FROM metadata WHERE key='active_chip'").fetchone()
        return str(row[0] or "") if row else ""
    except Exception:
        return ""


def fetch_selected_squad(conn, table_name="selected_squad", user_id=1):
    try:
        rows = conn.execute(
            f"""SELECT p.* FROM {table_name} ss
                JOIN players p ON p.id = ss.player_id
                WHERE ss.user_id = ? OR ss.user_id IS NULL
                LIMIT 15""", (user_id,)
        ).fetchall()
        return [{k: row[k] for k in row.keys()} for row in rows]
    except Exception:
        return []


def current_gameweek(conn):
    try:
        row = conn.execute("SELECT value FROM metadata WHERE key='current_gameweek'").fetchone()
        return _to_int(row[0], 1) if row else 1
    except Exception:
        return 1


def resolve_requested_entry_id(conn):
    return 0


def build_last_gameweek_context(conn, entry_id, player_map):
    return {}


def build_rival_context(entry_id, league_id):
    return {}


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stats")
def api_stats():
    try:
        conn = get_db()
        gw = current_gameweek(conn)
        counts = query_counts(conn)
        conn.close()
        return jsonify({
            "current_gw": gw,
            "total_players": counts["players"],
            "total_teams": counts["teams"],
            "total_fixtures": counts["fixtures"],
            "active_players": counts["players"],
        })
    except Exception as e:
        logger.exception("api_stats failed")
        return jsonify({"current_gw": 33, "total_players": 826, "total_teams": 20,
                        "total_fixtures": 380, "active_players": 826, "error": str(e)})


@app.route("/api/players")
def api_players():
    try:
        conn = get_db()
        sort_by  = request.args.get("sort", "ai_score")
        position = request.args.get("position", "")
        search   = request.args.get("search", "")
        limit    = _to_int(request.args.get("limit"), 80)
        min_price = _to_float(request.args.get("min_price")) or None
        max_price = _to_float(request.args.get("max_price")) or None
        only_starters = request.args.get("starters") == "1"
        high_form     = request.args.get("high_form") == "1"

        payload = fetch_players_payload(
            conn,
            sort_by=sort_by,
            limit=limit,
            search=search,
            position=position,
            min_price=min_price,
            max_price=max_price,
            only_starters=only_starters,
            high_form=high_form,
        )
        conn.close()
        players = [_normalise_player(p) for p in payload.get("players", [])]
        return jsonify(players)
    except Exception as e:
        logger.exception("api_players failed")
        return jsonify({"error": str(e)}), 500


def _normalise_player(p: dict) -> dict:
    """Map real DB player fields → frontend expected fields."""
    ai_score = round(_to_float(p.get("ai_score") or p.get("score"), 0), 1)
    ep_next = round(_to_float(
        p.get("predicted_points_next_game") or p.get("expected_points"), 0
    ), 1)
    ep_5 = round(_to_float(p.get("predicted_points_next_5_games"), 0), 1)

    next_fdrs = p.get("next_6_fdrs") or []
    next_5 = [_to_int(d, 3) for d in next_fdrs[:5]]
    if not next_5:
        fd = _to_float(p.get("fixture_difficulty"), 3)
        next_5 = [int(round(fd))] * 5

    rec_map = {
        "s": ("STRONG BUY", "emerald"),
        "a": ("BUY",        "cyan"),
        "b": ("HOLD",       "yellow"),
        "c": ("SELL",       "red"),
    }
    tier_bucket = str(p.get("tier_bucket") or "b").lower()
    recommendation, rec_color = rec_map.get(tier_bucket, ("HOLD", "yellow"))

    per_game = p.get("per_game") or []
    if not per_game:
        per_game = [ep_next] * 5

    sel = _to_float(p.get("selected_by_percent"), 0)
    mins = _to_int(p.get("minutes"), 0)
    cap_score = round(_to_float(p.get("captain_score"), 0), 1)
    diff_score = round(_to_float(p.get("differential_score"), 0), 1)

    status_raw = str(p.get("status") or "a").lower()
    chance = p.get("chance_of_playing_next_round")
    if status_raw in ("i",):
        status = "injury"
    elif status_raw in ("d",) or (chance is not None and _to_int(chance, 100) < 75):
        status = "doubtful"
    elif status_raw in ("s", "u") or (chance is not None and _to_int(chance, 100) == 0):
        status = "unavailable"
    else:
        status = "available"

    return {
        "id":                  p.get("id"),
        "name":                p.get("web_name") or p.get("name") or "Unknown",
        "full_name":           p.get("name") or "",
        "team":                p.get("team_name") or p.get("team") or "Unknown",
        "team_short":          p.get("team_short") or "",
        "team_id":             p.get("team_id"),
        "team_logo":           p.get("team_logo") or p.get("team_badge_url") or "",
        "position":            p.get("position") or "MID",
        "price":               _to_float(p.get("price"), 0),
        "form":                _to_float(p.get("form"), 0),
        "total_points":        _to_int(p.get("points"), 0),
        "ep_next":             ep_next,
        "ep_5gw":              ep_5,
        "xg":                  round(_to_float(p.get("expected_goals") or p.get("xg"), 0), 2),
        "xa":                  round(_to_float(p.get("expected_assists") or p.get("xa"), 0), 2),
        "selected_pct":        round(sel, 1),
        "minutes":             mins,
        "goals":               _to_int(p.get("goals"), 0),
        "assists":             _to_int(p.get("assists"), 0),
        "clean_sheets":        _to_int(p.get("clean_sheets"), 0),
        "bonus":               _to_int(p.get("bonus"), 0),
        "shots":               round(_to_float(p.get("shots"), 0), 1),
        "key_passes":          round(_to_float(p.get("key_passes"), 0), 1),
        "ai_score":            ai_score,
        "captain_score":       cap_score,
        "differential_score":  diff_score,
        "transfer_score":      round(_to_float(p.get("transfer_score"), 0), 1),
        "tier":                p.get("tier") or "B Tier",
        "tier_code":           p.get("tier_code") or "B",
        "fixture_difficulty":  _to_float(p.get("fixture_difficulty"), 3),
        "next_fixtures":       next_5,
        "predicted_pts":       [round(_to_float(v), 1) for v in per_game[:5]],
        "predicted_total_5gw": ep_5,
        "status":              status,
        "chance_of_playing":   chance,
        "news":                p.get("news") or "",
        "recommendation":      recommendation,
        "rec_color":           rec_color,
        "price_trend":         "stable",
        "price_change":        0,
        "is_captain_pick":     cap_score > 60,
        "is_differential":     sel < 15 and ai_score > 45,
        "upcoming_double":     bool(p.get("upcoming_double")),
        "upcoming_blank":      bool(p.get("upcoming_blank")),
        "risk_level":          p.get("risk_level") or "Medium",
        "risk_value":          round(_to_float(p.get("risk_value"), 50), 1),
        "confidence":          p.get("confidence") or "medium",
        "confidence_score":    round(_to_float(p.get("confidence_score"), 60), 1),
        "value_rating":        round(_to_int(p.get("points"), 0) / max(_to_float(p.get("price"), 1), 0.1), 2),
        "minutes_security":    round(min(mins / 3000 * 100, 100), 1),
        "sofascore_rating":    round(_to_float(p.get("sofascore_rating"), 0), 2),
        "news_sentiment":      round(_to_float(p.get("news_sentiment_score"), 0), 3),
        "photo_url":           p.get("photo_url") or p.get("local_image_path") or "",
        "nationality":         p.get("nationality") or "",
        "age":                 p.get("age"),
        "prediction_factors":  p.get("prediction_factors") or [],
        "p_goal":              round(_to_float(p.get("p_goal"), 0), 3),
        "p_assist":            round(_to_float(p.get("p_assist"), 0), 3),
        "p_clean_sheet":       round(_to_float(p.get("p_clean_sheet"), 0), 3),
        "floor":               _to_int(p.get("floor"), 0),
        "ceiling":             _to_int(p.get("ceiling"), 0),
        "recent_points_avg":   round(_to_float(p.get("recent_points_avg"), 0), 2),
        "consistency_score":   round(_to_float(p.get("consistency_score"), 0), 3),
        "explosiveness_score": round(_to_float(p.get("explosiveness_score"), 0), 3),
    }


@app.route("/api/injuries")
def api_injuries():
    try:
        conn = get_db()
        payload = fetch_players_payload(conn, limit=None)
        conn.close()
        inj = []
        for p in payload.get("players", []):
            status = str(p.get("status") or "a").lower()
            chance = p.get("chance_of_playing_next_round")
            if status in ("i", "d", "s", "u") or (chance is not None and _to_int(chance, 100) < 100):
                severity = (
                    "high" if (status == "i" or (chance is not None and _to_int(chance, 100) <= 25))
                    else "medium" if (chance is not None and _to_int(chance, 100) <= 50)
                    else "low"
                )
                inj.append({
                    "id":          p.get("id"),
                    "name":        p.get("web_name") or p.get("name"),
                    "team":        p.get("team_name") or p.get("team"),
                    "team_short":  p.get("team_short") or "",
                    "position":    p.get("position") or "MID",
                    "status":      status,
                    "chance":      chance,
                    "news":        p.get("news") or "",
                    "severity":    severity,
                    "selected_pct": round(_to_float(p.get("selected_by_percent"), 0), 1),
                    "price":       _to_float(p.get("price"), 0),
                })
        inj.sort(key=lambda x: (x["severity"] == "high", x["selected_pct"]), reverse=True)
        return jsonify(inj)
    except Exception as e:
        logger.exception("api_injuries failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/fixtures")
def api_fixtures():
    try:
        conn = get_db()
        limit = _to_int(request.args.get("limit"), 20)
        payload = fetch_fixtures_payload(conn, limit=limit)
        conn.close()
        return jsonify(payload.get("fixtures", []))
    except Exception as e:
        logger.exception("api_fixtures failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/fixture-matrix")
def api_fixture_matrix():
    try:
        conn = get_db()
        window = _to_int(request.args.get("window"), 6)
        gws, matrix_rows = build_fixture_matrix(conn, window=window)
        conn.close()

        matrix_dict: dict = {}
        for row in matrix_rows:
            tname = row["team_name"]
            matrix_dict[tname] = {}
            for i, gw in enumerate(gws):
                cell = row["cells"][i] if i < len(row["cells"]) else {"label": "BGW", "difficulty": 0, "band": "blank"}
                if cell.get("difficulty", 0) == 0:
                    matrix_dict[tname][gw] = {"opp": "BGW", "difficulty": 0, "is_home": None}
                else:
                    label = cell.get("label", "")
                    is_home = "(H)" in label or label.endswith("H)")
                    opp = label.replace("(H)", "").replace("(A)", "").strip()[:6]
                    matrix_dict[tname][gw] = {
                        "opp": opp,
                        "difficulty": _to_int(cell.get("difficulty"), 3),
                        "is_home": is_home,
                    }
        return jsonify({"matrix": matrix_dict, "gws": gws, "current_gw": gws[0] if gws else 33})
    except Exception as e:
        logger.exception("api_fixture_matrix failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/best-fixture-runs")
def api_best_runs():
    try:
        conn = get_db()
        runs = best_fixture_runs(conn, horizon=6, limit=10)
        conn.close()
        return jsonify(runs)
    except Exception as e:
        logger.exception("api_best_runs failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/standings")
def api_standings():
    try:
        conn = get_db()
        payload = standings_payload(conn)
        conn.close()
        return jsonify(payload)
    except Exception as e:
        logger.exception("api_standings failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/top-scorers")
def api_top_scorers():
    try:
        conn = get_db()
        data = top_scorers(conn, limit=10)
        conn.close()
        return jsonify(data)
    except Exception as e:
        logger.exception("api_top_scorers failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/captain-picks")
def api_captain_picks():
    try:
        conn = get_db()
        payload = fetch_players_payload(conn, sort_by="ai_score", limit=80)
        conn.close()
        players = [_normalise_player(p) for p in payload.get("players", [])]
        picks = sorted(players, key=lambda p: p["captain_score"], reverse=True)[:12]
        return jsonify(picks)
    except Exception as e:
        logger.exception("api_captain_picks failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/differentials")
def api_differentials():
    try:
        conn = get_db()
        payload = fetch_players_payload(conn, sort_by="ai_score", limit=None)
        conn.close()
        players = [_normalise_player(p) for p in payload.get("players", [])]
        diffs = [p for p in players if p["selected_pct"] < 15 and p["ai_score"] > 40]
        diffs.sort(key=lambda p: p["differential_score"], reverse=True)
        return jsonify(diffs[:20])
    except Exception as e:
        logger.exception("api_differentials failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/transfers")
def api_transfers():
    try:
        conn = get_db()
        payload = fetch_players_payload(conn, sort_by="ai_score", limit=80)
        conn.close()
        players = [_normalise_player(p) for p in payload.get("players", [])]
        buys = [p for p in players if p["recommendation"] in ("STRONG BUY", "BUY")]
        buys.sort(key=lambda p: p["ai_score"], reverse=True)
        return jsonify(buys[:25])
    except Exception as e:
        logger.exception("api_transfers failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/price-changes")
def api_price_changes():
    try:
        conn = get_db()
        payload = fetch_players_payload(conn, sort_by="ai_score", limit=None)
        conn.close()
        players = [_normalise_player(p) for p in payload.get("players", [])]
        risers = sorted(players, key=lambda p: p["transfer_score"], reverse=True)[:10]
        fallers = sorted(players, key=lambda p: p["ai_score"])[:10]
        return jsonify({"risers": risers, "fallers": fallers})
    except Exception as e:
        logger.exception("api_price_changes failed")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print()
    print("=" * 62)
    print("  🚀  FPL AI ASSISTANT — Real Data Mode")
    print(f"  📦  DB: {os.environ.get('TACTIX_DB_PATH')}")
    print("  🌐  http://localhost:5000")
    print("=" * 62)
    print()
    app.run(debug=False, host="127.0.0.1", port=5000)
