from __future__ import annotations

import sqlite3
import re
import threading
from typing import Any

try:
    from ai.engine import predict_player as elite_predict
except Exception:  # pragma: no cover - elite engine is optional fallback
    elite_predict = None

_weights_lock = threading.Lock()


TEAM_STRUCTURE = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
BUDGET_LIMIT = 100.0
CHIPS = {"wildcard", "free_hit", "bench_boost", "triple_captain"}

TACTIX_ANALYST_SYSTEM_PROMPT = """
You are Tactix, an elite Fantasy Premier League analyst.

You think like a human expert, not a generic data tool.

Style:
- Direct, confident, opinionated
- Clear English, no jargon overload
- Always explain the reasoning
- Always give one clear recommendation first

Thinking rules:
- Form plus fixtures beat reputation
- Minutes security matters
- Ownership matters for rank
- Differentials win stretches of the season
- Good decisions look beyond one gameweek

You must always:
- Use the user's squad context
- Justify advice with stats and fixtures
- Mention risk when it matters
- Stay focused on FPL and Premier League decisions

You must never:
- Invent stats
- Be vague when the data points to a clear call
- Ignore injuries, rotation risk, or affordability
""".strip()

AI_MODES: dict[str, str] = {
    "captain": "Pick the best captain. Give 3 reasons. Add risk.",
    "transfer": "Suggest ONE transfer (OUT → IN). Justify with fixtures and expected points.",
    "strategy": "Give overall strategy for next 3 gameweeks.",
    "debrief": "Analyse last gameweek and suggest improvement.",
}

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
MODEL_WEIGHTS = dict(DEFAULT_MODEL_WEIGHTS)

MAX_FORM = 10.0
MAX_XG_PER_90 = 1.2
MAX_XA_PER_90 = 0.8
MAX_MINUTES = 3000.0
MAX_SHOTS_SIGNAL = 100.0
MAX_KEY_PASSES_SIGNAL = 100.0
MIN_TEAM_STRENGTH = 900.0
MAX_TEAM_STRENGTH = 1400.0


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def set_model_weights(weights: dict[str, float] | None) -> None:
    global MODEL_WEIGHTS
    merged = dict(DEFAULT_MODEL_WEIGHTS)
    if weights:
        for key, value in weights.items():
            if key in merged:
                merged[key] = max(float(value), 0.0)
        total = sum(merged.values())
        if total > 0:
            merged = {key: value / total for key, value in merged.items()}
    with _weights_lock:
        MODEL_WEIGHTS = merged


def get_model_weights() -> dict[str, float]:
    """Return a thread-safe snapshot of current weights."""
    with _weights_lock:
        return dict(MODEL_WEIGHTS)


def normalize(value: float, minimum: float, maximum: float) -> float:
    if maximum <= minimum:
        return 0.0
    return clamp((value - minimum) / (maximum - minimum), 0.0, 1.0)


def fixture_ease(fixture_difficulty: float) -> float:
    return clamp(6.0 - fixture_difficulty, 1.0, 5.0)


def per_90(value: float, minutes: float) -> float:
    matches = max(minutes / 90.0, 1.0)
    return float(value) / matches


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in {None, ""}:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in {None, ""}:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _player_ai_score(player: dict[str, Any]) -> float:
    return round(
        _safe_float(player.get("ai_score", player.get("score")), 0.0),
        2,
    )


def _player_xpts(player: dict[str, Any]) -> float:
    return round(
        _safe_float(
            player.get("expected_points", player.get("predicted_points_next_game")),
            0.0,
        ),
        2,
    )


def fetch_user_gw_history(
    conn: sqlite3.Connection, user_id: int, limit: int = 5
) -> list[dict[str, Any]]:
    if user_id <= 0:
        return []
    rows = conn.execute(
        """
        SELECT gameweek, points, bench_points, captain_points, rank, overall_rank
        FROM gw_history
        WHERE user_id = ?
        ORDER BY gameweek DESC
        LIMIT ?
        """,
        (user_id, max(limit, 1)),
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def fetch_user_transfer_outcomes(
    conn: sqlite3.Connection, user_id: int, limit: int = 5
) -> list[dict[str, Any]]:
    if user_id <= 0:
        return []
    rows = conn.execute(
        """
        SELECT gameweek, sold_player_name, bought_player_name, net_gain, ai_recommended
        FROM transfer_outcomes
        WHERE user_id = ?
        ORDER BY gameweek DESC, id DESC
        LIMIT ?
        """,
        (user_id, max(limit, 1)),
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def calculate_available_budget(squad: list[dict[str, Any]]) -> float:
    squad_cost = sum(_safe_float(player.get("price"), 0.0) for player in squad)
    return round(max(BUDGET_LIMIT - squad_cost, 0.0), 1)


def _parse_chips_remaining(preferred_chip: Any) -> list[str]:
    text = str(preferred_chip or "").strip().lower()
    if not text:
        return sorted(CHIPS)
    chips = [token for token in re.split(r"[^a-z_]+", text) if token in CHIPS]
    return chips or sorted(CHIPS)


def _history_trend_label(history: list[dict[str, Any]]) -> str:
    if len(history) < 4:
        return "steady"
    ordered = list(reversed(history))
    early = sum(_safe_float(item.get("points"), 0.0) for item in ordered[:2]) / 2.0
    late = sum(_safe_float(item.get("points"), 0.0) for item in ordered[-2:]) / 2.0
    if late > early + 4:
        return "improving"
    if late < early - 4:
        return "slipping"
    return "steady"


def _build_user_summary(history: list[dict[str, Any]]) -> str:
    if not history:
        return "No tracked gameweeks yet. Save GW history to unlock performance trends."
    average = sum(_safe_float(item.get("points"), 0.0) for item in history) / max(
        len(history),
        1,
    )
    best = max(history, key=lambda item: _safe_float(item.get("points"), 0.0))
    trend = _history_trend_label(history)
    return (
        f"Average {average:.1f} pts across the last {len(history)} GWs. "
        f"Best was GW{_safe_int(best.get('gameweek'), 0)} with {_safe_int(best.get('points'), 0)} pts. "
        f"Current trend: {trend}."
    )


def _build_transfer_performance(outcomes: list[dict[str, Any]]) -> str:
    if not outcomes:
        return "No transfer outcomes tracked yet."
    average = sum(_safe_float(item.get("net_gain"), 0.0) for item in outcomes) / max(
        len(outcomes),
        1,
    )
    positive = sum(1 for item in outcomes if _safe_float(item.get("net_gain"), 0.0) > 0)
    return (
        f"{average:+.1f} pts average gain across the last {len(outcomes)} tracked transfers. "
        f"{positive} of those moves finished positive."
    )


def _captain_reason(player: dict[str, Any]) -> str:
    reasons: list[str] = []
    if _player_xpts(player) > 0:
        reasons.append(f"{_player_xpts(player):.2f} xPts next GW")
    if _safe_float(player.get("xgi_per_90"), 0.0) >= 0.55:
        reasons.append(f"{_safe_float(player.get('xgi_per_90'), 0.0):.2f} xGI per 90")
    if _safe_int(player.get("next_6_green_fixtures"), 0) >= 3:
        reasons.append(
            f"{_safe_int(player.get('next_6_green_fixtures'), 0)} green fixtures in the next 6"
        )
    if str(player.get("form_trend") or "") == "rising":
        reasons.append("form trend is rising")
    if not reasons:
        reasons.append(
            f"fixture {player.get('next_fixture', 'TBC')} rates {_safe_float(player.get('next_fixture_fdr'), 0.0):.0f} FDR"
        )
    return " | ".join(reasons[:3])


def _strength_tone(player: dict[str, Any]) -> str:
    ai_score = _player_ai_score(player)
    xpts = _player_xpts(player)
    if ai_score >= 78 or xpts >= 5.5:
        return "strong"
    if ai_score >= 62 or xpts >= 4.0:
        return "medium"
    return "weak"


def build_full_context(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    from flask import has_request_context, request

    from fixtures_api import fetch_fixtures_payload
    from player_api import fetch_players_payload

    import app as app_module

    preferences = app_module.fetch_user_preferences(conn, user_id) or {}
    risk_preference = (
        str(preferences.get("risk_preference") or "balanced").strip().lower()
    )
    if risk_preference not in {"safe", "balanced", "aggressive"}:
        risk_preference = "balanced"

    chip = app_module.active_chip(conn)
    squad = app_module.fetch_selected_squad(conn, user_id=user_id)
    if chip == "free_hit":
        free_hit_squad = app_module.fetch_selected_squad(
            conn,
            table_name="free_hit_squad",
            user_id=user_id,
        )
        if free_hit_squad:
            squad = free_hit_squad

    all_players = fetch_players_payload(conn)["players"]
    fixtures_payload = fetch_fixtures_payload(conn, limit=10)
    fixtures = (
        fixtures_payload.get("fixtures", [])
        if isinstance(fixtures_payload, dict)
        else []
    )
    gw = app_module.current_gameweek(conn)
    history = fetch_user_gw_history(conn, user_id, limit=5)
    transfer_outcomes = fetch_user_transfer_outcomes(conn, user_id, limit=5)
    budget = calculate_available_budget(squad)
    analysis = (
        build_team_analysis(
            squad,
            all_players,
            active_chip=chip,
            risk_preference=risk_preference,
        )
        if len(squad) == 15
        else None
    )
    weak_links = sorted(
        squad,
        key=lambda player: (_player_ai_score(player), _player_xpts(player)),
    )[:3]
    captain_options = sorted(
        squad,
        key=lambda player: (
            _safe_float(player.get("captain_score"), 0.0),
            _player_xpts(player),
        ),
        reverse=True,
    )[:3]
    captain_options = [
        {**player, "captain_reason": _captain_reason(player)}
        for player in captain_options
    ]
    top_players = sorted(
        all_players,
        key=lambda player: (_player_ai_score(player), _player_xpts(player)),
        reverse=True,
    )[:10]
    user_summary = _build_user_summary(history)
    transfer_performance = _build_transfer_performance(transfer_outcomes)
    gw_history_summary = (
        ", ".join(
            f"GW{_safe_int(item.get('gameweek'), 0)}→{_safe_int(item.get('points'), 0)}pts"
            for item in history
        )
        if history
        else "No history tracked yet."
    )
    transfer_outcomes_summary = (
        " | ".join(
            f"GW{_safe_int(item.get('gameweek'), 0)} OUT {item.get('sold_player_name', 'Unknown')} → IN {item.get('bought_player_name', 'Unknown')}: {_safe_int(item.get('net_gain'), 0):+d}pts"
            for item in transfer_outcomes
        )
        if transfer_outcomes
        else "No transfer outcomes tracked yet."
    )
    chips_remaining = _parse_chips_remaining(preferences.get("preferred_chip"))
    entry_id = 0
    league_id = 0
    last_gameweek_context: dict[str, Any] = {}
    rival_context: dict[str, Any] = {}
    if has_request_context():
        try:
            entry_id = app_module.resolve_requested_entry_id(conn)
        except Exception:
            entry_id = _safe_int(preferences.get("fpl_entry_id"), 0)
        league_id = _safe_int(request.args.get("league_id"), 0)
    else:
        entry_id = _safe_int(preferences.get("fpl_entry_id"), 0)
    if entry_id > 0:
        player_map = {int(player.get("id", 0)): player for player in all_players}
        last_gameweek_context = app_module.build_last_gameweek_context(
            conn,
            entry_id,
            player_map,
        )
    if entry_id > 0 and league_id > 0:
        rival_context = app_module.build_rival_context(entry_id, league_id)

    squad_strength_map = [
        {
            **player,
            "strength_tone": _strength_tone(player),
            "expected_points": _player_xpts(player),
            "floor": _safe_int(
                player.get("floor"), max(int(_player_xpts(player) * 0.5), 0)
            ),
            "ceiling": _safe_int(player.get("ceiling"), int(_player_xpts(player) * 2)),
            "confidence": str(player.get("confidence") or "medium"),
        }
        for player in squad
    ]
    safe_picks = sorted(
        squad,
        key=lambda player: (
            str(player.get("confidence") or "").lower() == "high",
            str(player.get("risk_level") or "").lower() == "low",
            _player_ai_score(player),
        ),
        reverse=True,
    )[:4]
    high_risk_picks = sorted(
        squad,
        key=lambda player: (
            str(player.get("risk_level") or "").lower() == "high",
            str(player.get("form_trend") or "") == "falling",
            -_player_ai_score(player),
        ),
        reverse=True,
    )[:4]
    injury_risks = [
        player
        for player in squad
        if str(player.get("status") or "a").lower() != "a"
        or _safe_int(player.get("chance_of_playing_next_round"), 100) < 100
    ]
    return {
        "user_id": user_id,
        "gameweek": gw,
        "budget": budget,
        "squad": squad,
        "weak_links": weak_links,
        "captain_options": captain_options,
        "fixtures": fixtures[:10],
        "fixture_runs": fixtures_payload.get("best_runs", [])
        if isinstance(fixtures_payload, dict)
        else [],
        "history": history,
        "top_players": top_players,
        "analysis": analysis,
        "all_players": all_players,
        "chips_remaining": chips_remaining,
        "active_chip": chip,
        "risk_preference": risk_preference,
        "user_summary": user_summary,
        "transfer_performance": transfer_performance,
        "gw_history_summary": gw_history_summary,
        "transfer_outcomes_summary": transfer_outcomes_summary,
        "transfer_outcomes": transfer_outcomes,
        "squad_strength_map": squad_strength_map,
        "safe_picks": safe_picks,
        "high_risk_picks": high_risk_picks,
        "injury_risks": injury_risks,
        "entry_id": entry_id,
        "league_id": league_id,
        "last_gameweek_context": last_gameweek_context,
        "rival_context": rival_context,
    }


def format_context(ctx: dict[str, Any]) -> str:
    def fmt(players: list[dict[str, Any]]) -> str:
        if not players:
            return "- None"
        return "\n".join(
            [
                f"- {player.get('name', 'Player')} ({player.get('team_name', 'Team')}) | Form {player.get('form', 0)} | xGI {round(_safe_float(player.get('xgi_per_90'), 0.0), 2)}"
                for player in players
            ]
        )

    return (
        f"Gameweek {ctx.get('gameweek', 0)}\n"
        f"Budget: £{_safe_float(ctx.get('budget'), 0.0):.1f}m\n"
        f"Chips remaining: {', '.join(ctx.get('chips_remaining') or []) or 'Unknown'}\n"
        f"User performance: {ctx.get('user_summary', 'No tracked form yet.')}\n"
        f"Transfer efficiency: {ctx.get('transfer_performance', 'No tracked transfers yet.')}\n\n"
        f"=== Squad ===\n{fmt(ctx.get('squad') or [])}\n\n"
        f"=== Weak Players ===\n{fmt(ctx.get('weak_links') or [])}\n\n"
        f"=== Captain Options ===\n{fmt(ctx.get('captain_options') or [])}\n\n"
        f"=== Fixtures ===\n{len(ctx.get('fixtures') or [])} upcoming matches\n\n"
        f"=== Top Players ===\n{fmt(ctx.get('top_players') or [])}"
    )


def _verdict_header(ctx: dict[str, Any]) -> dict[str, str]:
    history = ctx.get("history") or []
    trend = _history_trend_label(history)
    risk = str(ctx.get("risk_preference") or "balanced").lower()
    if trend == "slipping" or risk == "aggressive":
        return {
            "label": "ATTACK mode",
            "summary": "Take calculated risks and push for upside over the next 3 GWs.",
        }
    if trend == "improving" and risk == "safe":
        return {
            "label": "PROTECT mode",
            "summary": "Back secure minutes and protect rank with fewer unnecessary swings.",
        }
    return {
        "label": "BALANCE mode",
        "summary": "Press the strongest data-backed edge without forcing volatility.",
    }


def build_ai_dashboard_payload(ctx: dict[str, Any]) -> dict[str, Any]:
    analysis = ctx.get("analysis") or {}
    captain = (
        (analysis.get("captain") or (ctx.get("captain_options") or [{}])[0])
        if analysis
        else ((ctx.get("captain_options") or [{}])[0])
    )
    best_transfer = (analysis.get("best_transfer") or {}) if analysis else {}
    verdict = _verdict_header(ctx)
    squad_strength_map = ctx.get("squad_strength_map") or []
    transfer_in = best_transfer.get("in") or {}
    transfer_out = best_transfer.get("out") or {}
    transfer_gain = round(
        max(
            _safe_float(transfer_in.get("predicted_points_next_5_games"), 0.0)
            - _safe_float(transfer_out.get("predicted_points_next_5_games"), 0.0),
            _safe_float(best_transfer.get("transfer_score_gain"), 0.0),
        ),
        2,
    )

    team_fixture_cards: list[dict[str, Any]] = []
    seen_teams: set[int] = set()
    for player in sorted(
        ctx.get("squad") or [],
        key=lambda item: _safe_float(item.get("next_6_avg_fdr"), 99.0),
    ):
        team_id = _safe_int(player.get("team_id"), 0)
        if team_id <= 0 or team_id in seen_teams:
            continue
        seen_teams.add(team_id)
        team_fixture_cards.append(
            {
                "team_id": team_id,
                "team_name": str(player.get("team_name") or "Team"),
                "badge_url": str(
                    player.get("team_badge_url") or player.get("team_logo") or ""
                ),
                "average_fdr": round(_safe_float(player.get("next_6_avg_fdr"), 0.0), 2),
                "fixtures": [
                    {
                        "label": f"{fixture.get('opponent_name', 'TBC')} ({fixture.get('venue', 'H')})",
                        "fdr": _safe_int(fixture.get("fdr"), 0),
                    }
                    for fixture in (player.get("next_6_fixtures") or [])[:5]
                ],
            }
        )
    safe_picks = ctx.get("safe_picks") or []
    high_risk = ctx.get("high_risk_picks") or []
    injury_risks = ctx.get("injury_risks") or []
    return {
        "header": {
            "gameweek": _safe_int(ctx.get("gameweek"), 0),
            "budget": _safe_float(ctx.get("budget"), 0.0),
            "chips_remaining": ctx.get("chips_remaining") or [],
            "verdict_label": verdict["label"],
            "verdict_summary": verdict["summary"],
            "user_summary": str(ctx.get("user_summary") or ""),
            "transfer_performance": str(ctx.get("transfer_performance") or ""),
            "predicted_points": _safe_float(analysis.get("expected_team_points"), 0.0),
        },
        "captain_card": {
            "player": captain or {},
            "reasons": (captain or {}).get("prediction_factors")
            or [_captain_reason(captain or {})],
            "fixture": str((captain or {}).get("next_fixture") or "No fixture"),
            "fdr": _safe_int((captain or {}).get("next_fixture_fdr"), 0),
        },
        "transfer_card": {
            "out": transfer_out,
            "in": transfer_in,
            "gain": transfer_gain,
            "summary": str(
                best_transfer.get("explanation")
                or "No clear transfer is forcing itself right now."
            ),
        },
        "strategy_card": {
            "headline": verdict["label"],
            "body": (
                f"{verdict['summary']} {_build_user_summary(ctx.get('history') or [])} {_build_transfer_performance(ctx.get('transfer_outcomes') or [])}"
            ).strip(),
        },
        "squad_strength_map": squad_strength_map,
        "fixture_analysis": team_fixture_cards,
        "differentials": (analysis.get("differentials") or [])[:5] if analysis else [],
        "risk_radar": {
            "safe_picks": safe_picks[:4],
            "high_risk_picks": high_risk[:4],
            "injury_risks": injury_risks[:4],
        },
    }


def build_structured_insight(
    *,
    score: float,
    summary: str,
    reasons: list[str],
    risks: list[str],
    fixture_analysis: str,
    recommendation: str,
    action: str,
) -> dict[str, Any]:
    return {
        "score": round(clamp(score, 0.0, 100.0), 1),
        "summary": str(summary or "").strip(),
        "reasons": [str(item).strip() for item in reasons if str(item).strip()][:4],
        "risks": [str(item).strip() for item in risks if str(item).strip()][:3],
        "fixture_analysis": str(fixture_analysis or "").strip(),
        "recommendation": str(recommendation or "hold").strip(),
        "action": str(action or "").strip(),
    }


def build_captain_module_payload(ctx: dict[str, Any]) -> dict[str, Any]:
    analysis = ctx.get("analysis") or {}
    captain = (
        (analysis.get("captain") or (ctx.get("captain_options") or [{}])[0])
        if analysis
        else ((ctx.get("captain_options") or [{}])[0])
    )
    vice = (
        (analysis.get("vice_captain") or (ctx.get("captain_options") or [{}, {}])[1])
        if analysis
        else (
            (ctx.get("captain_options") or [{}, {}])[1]
            if len(ctx.get("captain_options") or []) > 1
            else {}
        )
    )
    if not captain:
        return {
            "module": "captain",
            "title": "Captain Picker",
            "reply": "Save a full squad first so Tactix can make a captain call.",
            "insight": build_structured_insight(
                score=0.0,
                summary="TACTIX needs a saved 15-player squad before it can rank armband options.",
                reasons=["no valid squad context is available"],
                risks=["captain analysis is locked until the squad is saved"],
                fixture_analysis="No squad fixture analysis available.",
                recommendation="hold",
                action="Save your squad first.",
            ),
        }
    reasons = (captain.get("prediction_factors") or [_captain_reason(captain)])[:3]
    risks: list[str] = []
    if str(captain.get("risk_level") or "").lower() == "high":
        risks.append("the captain carries elevated volatility")
    if str(captain.get("form_trend") or "") == "falling":
        risks.append("recent form trend is cooling off")
    if _safe_int(captain.get("chance_of_playing_next_round"), 100) < 100:
        risks.append("availability is not fully secure")
    if not risks:
        risks.append("the main risk is that the ceiling is tied to one fixture")
    fixture_analysis = (
        f"Next fixture: {captain.get('next_fixture', 'TBC')} with FDR {_safe_int(captain.get('next_fixture_fdr'), 0)}. "
        f"The squad also has {_safe_int(captain.get('next_6_green_fixtures'), 0)} green fixtures in the next 6 around this captaincy anchor."
    )
    insight = build_structured_insight(
        score=max(
            _player_ai_score(captain), _safe_float(captain.get("captain_score"), 0.0)
        ),
        summary=(
            f"{captain.get('name', 'Your top attacker')} is the clearest captain because they combine the best projection, fixture, and role security in your squad."
        ),
        reasons=reasons,
        risks=risks,
        fixture_analysis=fixture_analysis,
        recommendation="captain",
        action=f"Captain {captain.get('name', 'your best option')}; vice-captain {vice.get('name', 'your next best option')}.",
    )
    return {
        "module": "captain",
        "title": "Captain Picker",
        "reply": fallback_ai_mode_response("captain", ctx),
        "captain": captain,
        "vice_captain": vice,
        "insight": insight,
    }


def build_transfer_module_payload(ctx: dict[str, Any]) -> dict[str, Any]:
    analysis = ctx.get("analysis") or {}
    move = analysis.get("best_transfer") or {}
    outgoing = move.get("out") or {}
    incoming = move.get("in") or {}
    gain = max(
        _safe_float(incoming.get("predicted_points_next_5_games"), 0.0)
        - _safe_float(outgoing.get("predicted_points_next_5_games"), 0.0),
        _safe_float(move.get("transfer_score_gain"), 0.0),
    )
    if not outgoing or not incoming:
        return {
            "module": "transfer",
            "title": "Transfer Suggestions",
            "reply": fallback_ai_mode_response("transfer", ctx),
            "insight": build_structured_insight(
                score=42.0,
                summary="There is no single move forcing itself right now, so keeping flexibility is the stronger decision.",
                reasons=["the current squad does not show a high-leverage upgrade"],
                risks=[
                    "forcing a sideways move can waste a transfer before the next fixture swing"
                ],
                fixture_analysis="No transfer upgrade clears the current squad by a meaningful margin over the next 3-5 GWs.",
                recommendation="hold",
                action="Hold the transfer and reassess after the next data refresh.",
            ),
        }
    reasons = [
        str(
            move.get("explanation")
            or "The incoming player has the stronger projection and role."
        ),
        f"Projected next-5 edge is about +{gain:.1f} points.",
        f"Fixture swing moves from {outgoing.get('next_fixture', 'TBC')} to {incoming.get('next_fixture', 'TBC')}.",
    ]
    risks = []
    if _safe_float(incoming.get("price"), 0.0) > (
        _safe_float(outgoing.get("price"), 0.0) + _safe_float(ctx.get("budget"), 0.0)
    ):
        risks.append("budget is tight, so price movement could close the door")
    if str(incoming.get("risk_level") or "").lower() == "high":
        risks.append("the incoming option still carries notable volatility")
    if str(outgoing.get("status") or "a").lower() != "a":
        risks.append("the outgoing player is already carrying availability downside")
    if not risks:
        risks.append(
            "the main risk is bypassing flexibility if fixture news changes later in the week"
        )
    fixture_analysis = (
        f"Outgoing fixture: {outgoing.get('next_fixture', 'TBC')} (FDR {_safe_int(outgoing.get('next_fixture_fdr'), 0)}). "
        f"Incoming fixture: {incoming.get('next_fixture', 'TBC')} (FDR {_safe_int(incoming.get('next_fixture_fdr'), 0)})."
    )
    return {
        "module": "transfer",
        "title": "Transfer Suggestions",
        "reply": fallback_ai_mode_response("transfer", ctx),
        "out": outgoing,
        "in": incoming,
        "insight": build_structured_insight(
            score=clamp(58.0 + gain * 6.5, 0.0, 100.0),
            summary=(
                f"Selling {outgoing.get('name', 'the outgoing player')} for {incoming.get('name', 'the target')} is the cleanest move because the incoming profile is stronger for both projection and fixture runway."
            ),
            reasons=reasons,
            risks=risks,
            fixture_analysis=fixture_analysis,
            recommendation="buy",
            action=f"Sell {outgoing.get('name', 'the outgoing player')} and buy {incoming.get('name', 'the target')}",
        ),
    }


def build_differential_module_payload(ctx: dict[str, Any]) -> dict[str, Any]:
    analysis = ctx.get("analysis") or {}
    differentials = (analysis.get("differentials") or [])[:5]
    if not differentials:
        return {
            "module": "differentials",
            "title": "Differential Finder",
            "reply": "No standout low-owned differential is clearing the AI threshold right now.",
            "players": [],
            "insight": build_structured_insight(
                score=35.0,
                summary="There is no clear low-owned upside play worth forcing from the current pool.",
                reasons=[
                    "ownership discounts do not currently align with enough projected upside"
                ],
                risks=[
                    "forcing a weak differential can create downside without real rank leverage"
                ],
                fixture_analysis="The strongest projected differentials are not materially ahead of your safer options right now.",
                recommendation="hold",
                action="Stay with stronger core picks unless the data shifts.",
            ),
        }
    lead = differentials[0]
    reasons = [
        f"{lead.get('name', 'This player')} is still only {_safe_float(lead.get('selected_by_percent'), 0.0):.1f}% owned.",
        f"They project {_player_xpts(lead):.2f} xPts next game with {_safe_int(lead.get('next_6_green_fixtures'), 0)} green fixtures in the next 6.",
        str(
            lead.get("reason")
            or lead.get("confidence_reason")
            or "The role and fixture profile support the upside case."
        ),
    ]
    risks = []
    if str(lead.get("risk_level") or "").lower() == "high":
        risks.append("the upside comes with meaningful volatility")
    if str(lead.get("form_trend") or "") == "falling":
        risks.append("recent trend is not clean")
    if not risks:
        risks.append("low-owned picks naturally bring more week-to-week variance")
    fixture_analysis = f"{lead.get('name', 'This player')} faces {lead.get('next_fixture', 'TBC')} next, rated FDR {_safe_int(lead.get('next_fixture_fdr'), 0)}."
    return {
        "module": "differentials",
        "title": "Differential Finder",
        "reply": f"Best differential right now: {lead.get('name', 'Player')}. They offer real upside without carrying heavy ownership.",
        "players": differentials,
        "insight": build_structured_insight(
            score=clamp(_player_ai_score(lead), 0.0, 100.0),
            summary=f"{lead.get('name', 'This player')} is the best differential because the projection is strong and the ownership is still low enough to move rank.",
            reasons=reasons,
            risks=risks,
            fixture_analysis=fixture_analysis,
            recommendation="buy",
            action=f"Target {lead.get('name', 'this differential')} if you need rank upside.",
        ),
    }


def build_head_to_head_module_payload(ctx: dict[str, Any]) -> dict[str, Any]:
    rival = ctx.get("rival_context") or {}
    if not rival:
        return {
            "module": "head_to_head",
            "title": "Head-to-Head Comparison",
            "reply": "Add a mini-league ID to unlock rival squad comparison.",
            "insight": build_structured_insight(
                score=0.0,
                summary="TACTIX needs rival context before it can model head-to-head threats and opportunities.",
                reasons=["no mini-league rival is connected yet"],
                risks=[
                    "you are missing visibility on captaincy and ownership threats above you"
                ],
                fixture_analysis="No rival fixture comparison is available yet.",
                recommendation="hold",
                action="Connect a mini-league ID to unlock rival analysis.",
            ),
            "shared_players": [],
            "threats": [],
            "opportunities": [],
        }
    player_map = {
        int(player.get("id", 0)): player for player in ctx.get("all_players") or []
    }
    squad_ids = {int(player.get("id", 0)) for player in ctx.get("squad") or []}
    rival_ids = [
        _safe_int(item.get("player_id"), 0)
        for item in rival.get("picks", [])
        if _safe_int(item.get("player_id"), 0) > 0
    ]
    shared = [
        player_map[player_id]
        for player_id in rival_ids
        if player_id in player_map and player_id in squad_ids
    ]
    threats = [
        player_map[player_id]
        for player_id in rival_ids
        if player_id in player_map and player_id not in squad_ids
    ][:4]
    opportunities = [
        player
        for player in (ctx.get("analysis") or {}).get("differentials", [])[:4]
        if int(player.get("id", 0)) not in rival_ids
    ]
    lead_threat = threats[0] if threats else {}
    reasons = []
    if lead_threat:
        reasons.append(
            f"{lead_threat.get('name', 'The main rival threat')} is a live swing because you do not own them and they project {_player_xpts(lead_threat):.2f} xPts next game."
        )
    if shared:
        reasons.append(
            f"Shared core reduces variance through {', '.join(str(player.get('name') or 'Player') for player in shared[:3])}."
        )
    if opportunities:
        reasons.append(
            f"Your best attack route is {opportunities[0].get('name', 'your top differential')} if you need separation."
        )
    if not reasons:
        reasons.append(
            "The current squads look close, so captaincy and one transfer can decide the swing."
        )
    risks = [
        f"Gap to {rival.get('rival_name', 'rival')} is {_safe_float(rival.get('gap_points'), 0.0):.0f} points.",
    ]
    if lead_threat:
        risks.append(
            "Uncovered rival ownership can punish you quickly if that player hauls."
        )
    fixture_analysis = (
        f"Main rival threat: {lead_threat.get('name', 'No single threat')} with next fixture {lead_threat.get('next_fixture', 'TBC')}"
        if lead_threat
        else "No standout uncovered rival fixture threat is showing right now."
    )
    return {
        "module": "head_to_head",
        "title": "Head-to-Head Comparison",
        "reply": (
            f"Closest rival: {rival.get('rival_name', 'Rival')} - the key swing comes from {lead_threat.get('name', 'captaincy and differentials')}"
            if lead_threat
            else f"Closest rival: {rival.get('rival_name', 'Rival')} - there is no major uncovered threat right now."
        ),
        "shared_players": shared[:5],
        "threats": threats,
        "opportunities": opportunities,
        "insight": build_structured_insight(
            score=clamp(
                55.0 + min(len(opportunities), 3) * 8.0 - min(len(threats), 3) * 5.0,
                0.0,
                100.0,
            ),
            summary=(
                f"TACTIX sees the head-to-head battle against {rival.get('rival_name', 'your rival')} being decided by uncovered threats plus one attacking differential you can use to gain ground."
            ),
            reasons=reasons,
            risks=risks,
            fixture_analysis=fixture_analysis,
            recommendation="track",
            action=(
                f"Protect the main threat and consider {opportunities[0].get('name', 'one strong differential')} as your upside route."
                if opportunities
                else "Back your strongest core and win the captaincy decision."
            ),
        ),
    }


def build_my_team_module_payload(ctx: dict[str, Any]) -> dict[str, Any]:
    analysis = ctx.get("analysis") or {}
    weak_links = ctx.get("weak_links") or []
    history = ctx.get("history") or []
    captain = analysis.get("captain") or {}
    best_transfer = analysis.get("best_transfer") or {}
    best_transfer_in = best_transfer.get("in") or {}
    reasons = [
        str(ctx.get("user_summary") or "No tracked performance history yet."),
        str(ctx.get("transfer_performance") or "No transfer outcomes tracked yet."),
    ]
    if weak_links:
        reasons.append(
            f"Weak links are {', '.join(str(player.get('name') or 'Player') for player in weak_links[:3])}."
        )
    risks = []
    if ctx.get("injury_risks"):
        risks.append("Availability issues are still present inside the squad.")
    if weak_links:
        risks.append("At least one weak link is suppressing the projected floor.")
    if not risks:
        risks.append(
            "The main risk is staying passive if fixture swings create stronger opportunities elsewhere."
        )
    fixture_analysis = (
        f"Captain anchor: {captain.get('name', 'No captain')} with next fixture {captain.get('next_fixture', 'TBC')}. "
        f"Best upgrade lane points to {best_transfer_in.get('name', 'no forced target')} over the next 3-5 GWs."
    )
    trend = _history_trend_label(history)
    recommendation = "hold" if trend == "improving" and not weak_links else "buy"
    action = (
        f"Primary squad action: move on {weak_links[0].get('name', 'your softest spot')} and build around {captain.get('name', 'your captain')} over the next 3 GWs."
        if weak_links and captain
        else "Primary squad action: keep building around your strongest captain and fixture run."
    )
    return {
        "module": "my_team_analysis",
        "title": "My Team Analysis",
        "reply": (
            f"Your squad trend is {trend}. The next priority is {weak_links[0].get('name', 'protecting flexibility')}."
            if weak_links
            else f"Your squad trend is {trend}. There is no urgent structural issue right now."
        ),
        "insight": build_structured_insight(
            score=clamp(
                _safe_float(analysis.get("average_ai_rating"), 0.0), 0.0, 100.0
            ),
            summary="TACTIX is reading both your squad shape and your manager trend, so the recommendation balances short-term points with the next phase of squad evolution.",
            reasons=reasons,
            risks=risks,
            fixture_analysis=fixture_analysis,
            recommendation=recommendation,
            action=action,
        ),
        "weak_links": weak_links,
        "captain": captain,
        "best_transfer": best_transfer,
    }


def build_price_change_module_payload(
    ctx: dict[str, Any],
    risers: list[dict[str, Any]],
    fallers: list[dict[str, Any]],
) -> dict[str, Any]:
    squad_names = {
        str(player.get("name") or "").strip().lower()
        for player in ctx.get("squad") or []
    }
    squad_risers = [
        item
        for item in risers
        if str(item.get("name") or "").strip().lower() in squad_names
    ]
    squad_fallers = [
        item
        for item in fallers
        if str(item.get("name") or "").strip().lower() in squad_names
    ]
    lead_riser = squad_risers[0] if squad_risers else (risers[0] if risers else {})
    lead_faller = squad_fallers[0] if squad_fallers else (fallers[0] if fallers else {})
    reasons = []
    if lead_riser:
        reasons.append(
            f"Riser pressure is building around {lead_riser.get('name', 'the top riser')} at £{_safe_float(lead_riser.get('current_price'), 0.0):.1f}m."
        )
    if lead_faller:
        reasons.append(
            f"Fall risk is strongest on {lead_faller.get('name', 'the top faller')} if you are planning that move soon."
        )
    if not reasons:
        reasons.append(
            "There is no urgent price movement pressure on the current data pull."
        )
    risks = []
    if squad_fallers:
        risks.append("You have at least one squad player under fall pressure.")
    if lead_riser and not squad_risers:
        risks.append("Waiting can price you out of a key incoming target.")
    if not risks:
        risks.append("The current price movement risk is manageable.")
    fixture_analysis = f"Top riser to watch: {lead_riser.get('name', 'None')} | top faller to watch: {lead_faller.get('name', 'None')}."
    return {
        "module": "price_changes",
        "title": "Price Changes Tracker",
        "reply": (
            f"Price watch: {lead_riser.get('name', 'No urgent riser')} is the main buy-side pressure and {lead_faller.get('name', 'no urgent faller')} is the main sell-side risk."
        ),
        "risers": risers[:6],
        "fallers": fallers[:6],
        "squad_risers": squad_risers[:3],
        "squad_fallers": squad_fallers[:3],
        "insight": build_structured_insight(
            score=clamp(
                50.0
                + min(len(squad_risers), 2) * 10.0
                + min(len(squad_fallers), 2) * 12.0,
                0.0,
                100.0,
            ),
            summary="TACTIX tracks price pressure in the context of your actual squad so you know when patience is worth it and when value is about to move.",
            reasons=reasons,
            risks=risks,
            fixture_analysis=fixture_analysis,
            recommendation="track",
            action="Move quickly if a target rise or squad fall directly affects your next planned transfer.",
        ),
    }


def fallback_ai_mode_response(mode: str, ctx: dict[str, Any]) -> str:
    analysis = ctx.get("analysis") or {}
    if mode == "captain":
        captain = (
            (analysis.get("captain") or (ctx.get("captain_options") or [{}])[0])
            if analysis
            else ((ctx.get("captain_options") or [{}])[0])
        )
        if not captain:
            return "Save a full squad first so Tactix can make a captain call."
        reasons = (captain.get("prediction_factors") or [_captain_reason(captain)])[:3]
        return (
            f"Captain {captain.get('name', 'Unknown')} is the best armband pick. "
            f"Reasons: {'; '.join(str(reason) for reason in reasons)}. "
            f"Fixture: {captain.get('next_fixture', 'TBC')} (FDR {_safe_int(captain.get('next_fixture_fdr'), 0)})."
        )
    if mode == "transfer":
        move = analysis.get("best_transfer") or {}
        if not move:
            return "No single transfer is worth forcing right now. Keep flexibility and reassess after the next deadline swing."
        outgoing = move.get("out") or {}
        incoming = move.get("in") or {}
        gain = max(
            _safe_float(incoming.get("predicted_points_next_5_games"), 0.0)
            - _safe_float(outgoing.get("predicted_points_next_5_games"), 0.0),
            _safe_float(move.get("transfer_score_gain"), 0.0),
        )
        return (
            f"Transfer call: {outgoing.get('name', 'Sell')} → {incoming.get('name', 'Buy')}. "
            f"Expected gain is about +{gain:.1f} over the next stretch, with fixtures moving from "
            f"{outgoing.get('next_fixture', 'TBC')} to {incoming.get('next_fixture', 'TBC')}."
        )
    if mode == "debrief":
        last_ctx = ctx.get("last_gameweek_context") or {}
        if not last_ctx:
            return "No completed gameweek debrief is available yet. Sync your FPL history to unlock this review."
        return (
            f"GW{_safe_int(last_ctx.get('gameweek'), 0)} finished on {_safe_float(last_ctx.get('user_score'), 0.0):.0f} points versus "
            f"{_safe_float(last_ctx.get('average_score'), 0.0):.0f} average. Captain {last_ctx.get('captain_name', 'Unknown')} returned "
            f"{_safe_float(last_ctx.get('captain_points'), 0.0):.0f} points and you left {_safe_float(last_ctx.get('bench_points'), 0.0):.0f} on the bench."
        )
    return (
        f"Next 3-GW strategy: {(_verdict_header(ctx))['summary']} "
        f"Start with your weakest links ({', '.join(player.get('name', 'Player') for player in (ctx.get('weak_links') or [])[:3]) or 'none flagged'}) "
        f"and build around captain leaders like {', '.join(player.get('name', 'Player') for player in (ctx.get('captain_options') or [])[:2]) or 'your best attackers'}."
    )


def _availability_label(player: dict[str, Any]) -> str:
    upcoming_fixture_count = int(player.get("upcoming_fixture_count", 0) or 0)
    if upcoming_fixture_count <= 0 or int(player.get("upcoming_blank", 0) or 0):
        return "Blank Gameweek"
    status = str(player.get("status") or "a").lower()
    chance = _safe_float(player.get("chance_of_playing_next_round"), 100.0)
    if status in {"i", "s", "u"} or chance <= 0:
        return "Unavailable"
    if status == "d" or chance < 75:
        return "Doubtful"
    return "Available"


def _prediction_reason(player: dict[str, Any], features: dict[str, float]) -> str:
    if float(features.get("upcoming_fixture_count", 0.0) or 0.0) <= 0:
        return "Blank Gameweek"
    if float(features.get("status_penalty", 0.0) or 0.0) >= 0.35:
        return "Availability risk is suppressing the projection."
    drivers: list[str] = []
    if float(features.get("recent_form_signal", 0.0) or 0.0) >= 0.65:
        drivers.append("strong recent form")
    if float(features.get("xg_per_90", 0.0) or 0.0) >= 0.4:
        drivers.append("high xG per 90")
    if float(features.get("xa_per_90", 0.0) or 0.0) >= 0.2:
        drivers.append("high xA per 90")
    if float(player.get("recent_minutes_avg", 0.0) or 0.0) >= 75:
        drivers.append("secure minutes")
    if float(player.get("fixture_difficulty", 3.0) or 3.0) <= 2.0:
        drivers.append("a strong fixture")
    if not drivers:
        drivers.append("a balanced all-round profile")
    return f"Projection is driven by {', '.join(drivers[:3])}."


def _player_status_penalty(player: dict[str, Any]) -> float:
    status = str(player.get("status") or "a").lower()
    if status in {"d", "i", "s", "u"}:
        return 0.35
    chance = float(player.get("chance_of_playing_next_round") or 100)
    if chance <= 25:
        return 0.35
    if chance <= 50:
        return 0.2
    if chance <= 75:
        return 0.1
    return 0.0


def _scaled_stat_signal(value: float, cap: float) -> float:
    return normalize(float(value), 0.0, cap)


def _shots_signal(player: dict[str, Any]) -> float:
    return _scaled_stat_signal(float(player.get("shots", 0.0) or 0.0), MAX_SHOTS_SIGNAL)


def _key_pass_signal(player: dict[str, Any]) -> float:
    return _scaled_stat_signal(
        float(player.get("key_passes", 0.0) or 0.0), MAX_KEY_PASSES_SIGNAL
    )


def _team_attack_signal(player: dict[str, Any]) -> float:
    attack = float(player.get("team_attack_strength", 0.0) or 0.0)
    if attack <= 10:
        attack *= 250.0
    return normalize(attack, MIN_TEAM_STRENGTH, MAX_TEAM_STRENGTH)


def _team_defence_signal(player: dict[str, Any]) -> float:
    defence = float(player.get("team_defence_strength", 0.0) or 0.0)
    if defence <= 10:
        defence *= 250.0
    return normalize(defence, MIN_TEAM_STRENGTH, MAX_TEAM_STRENGTH)


def _position_team_signal(player: dict[str, Any]) -> float:
    attack_signal = _team_attack_signal(player)
    defence_signal = _team_defence_signal(player)
    position = str(player.get("position") or "")
    if position in {"GK", "DEF"}:
        return defence_signal
    if position == "MID":
        return (attack_signal * 0.65) + (defence_signal * 0.35)
    return attack_signal


def build_feature_pack(player: dict[str, Any]) -> dict[str, float]:
    form = float(player.get("form", 0.0) or 0.0)
    minutes = float(player.get("minutes", 0.0) or 0.0)
    xg = float(player.get("expected_goals", 0.0) or 0.0)
    xa = float(player.get("expected_assists", 0.0) or 0.0)
    ownership = float(player.get("selected_by_percent", 0.0) or 0.0)
    difficulty = float(player.get("fixture_difficulty", 3.0) or 3.0)

    xg_per_90 = per_90(xg, minutes)
    xa_per_90 = per_90(xa, minutes)
    shots_signal = _shots_signal(player)
    key_pass_signal = _key_pass_signal(player)
    form_signal = normalize(form, 0.0, MAX_FORM)
    recent_form_signal = normalize(
        float(player.get("recent_points_avg", 0.0) or 0.0), 0.0, 10.0
    )
    xg_signal = (normalize(xg_per_90, 0.0, MAX_XG_PER_90) * 0.75) + (
        shots_signal * 0.25
    )
    xa_signal = (normalize(xa_per_90, 0.0, MAX_XA_PER_90) * 0.75) + (
        key_pass_signal * 0.25
    )
    minutes_signal = (
        normalize(minutes, 0.0, MAX_MINUTES) * 0.55
        + normalize(float(player.get("recent_minutes_avg", 0.0) or 0.0), 0.0, 90.0)
        * 0.45
    )
    fixture_signal = normalize(fixture_ease(difficulty), 1.0, 5.0)
    team_signal = _position_team_signal(player)
    ownership_signal = normalize(100.0 - ownership, 0.0, 100.0)
    consistency_signal = clamp(
        float(player.get("consistency_score", 0.0) or 0.0), 0.0, 1.0
    )
    explosiveness_signal = clamp(
        float(player.get("explosiveness_score", 0.0) or 0.0), 0.0, 1.0
    )
    upcoming_fixture_count = float(player.get("upcoming_fixture_count", 0.0) or 0.0)
    blank_signal = (
        1.0
        if int(player.get("upcoming_blank", 0) or 0) or upcoming_fixture_count <= 0
        else 0.0
    )
    double_signal = 1.0 if int(player.get("upcoming_double", 0) or 0) else 0.0

    return {
        "form_signal": round(form_signal, 4),
        "recent_form_signal": round(recent_form_signal, 4),
        "xg_signal": round(xg_signal, 4),
        "xa_signal": round(xa_signal, 4),
        "minutes_signal": round(minutes_signal, 4),
        "fixture_signal": round(fixture_signal, 4),
        "team_signal": round(team_signal, 4),
        "ownership_signal": round(ownership_signal, 4),
        "shots_signal": round(shots_signal, 4),
        "key_pass_signal": round(key_pass_signal, 4),
        "consistency_signal": round(consistency_signal, 4),
        "explosiveness_signal": round(explosiveness_signal, 4),
        "upcoming_fixture_count": round(upcoming_fixture_count, 2),
        "blank_signal": round(blank_signal, 4),
        "double_signal": round(double_signal, 4),
        "xg_per_90": round(xg_per_90, 3),
        "xa_per_90": round(xa_per_90, 3),
        "fixture_ease": round(fixture_ease(difficulty), 2),
        "status_penalty": round(_player_status_penalty(player), 4),
    }


def _prediction_risk(
    player: dict[str, Any], features: dict[str, float]
) -> tuple[str, float]:
    risk_value = (
        (1.0 - features["minutes_signal"]) * 0.35
        + (1.0 - features["recent_form_signal"]) * 0.25
        + (1.0 - features["fixture_signal"]) * 0.15
        + (1.0 - features["team_signal"]) * 0.1
        + features["status_penalty"]
    )
    risk_value = clamp(risk_value, 0.0, 1.0)
    if risk_value < 0.33:
        return "Low", round(risk_value * 100, 1)
    if risk_value < 0.66:
        return "Medium", round(risk_value * 100, 1)
    return "High", round(risk_value * 100, 1)


def _confidence_score_from_label(label: str) -> float:
    normalized = str(label or "").strip().lower()
    if normalized == "high":
        return 85.0
    if normalized == "medium":
        return 60.0
    return 35.0


def predict_points_next_game(player: dict[str, Any]) -> dict[str, Any]:
    """
    Returns a full prediction dict instead of a single number.

    Returns:
        {
            "expected_points": float,
            "floor": int,
            "ceiling": int,
            "p_goal": float,
            "p_assist": float,
            "p_clean_sheet": float,
            "p_double_digit": float,
            "confidence": str,
            "confidence_reason": str,
            "prediction_factors": list[str],
        }
    """
    safe_player = dict(player or {})
    features = build_feature_pack(safe_player)
    if features["upcoming_fixture_count"] <= 0:
        return {
            "expected_points": 0.0,
            "predicted_points_next_game": 0.0,
            "floor": 0,
            "ceiling": 0,
            "p_goal": 0.0,
            "p_assist": 0.0,
            "p_clean_sheet": 0.0,
            "p_double_digit": 0.0,
            "confidence": "low",
            "confidence_reason": "No upcoming fixture in the next gameweek",
            "prediction_factors": ["Blank gameweek"],
            "reason": "Blank Gameweek",
            "risk_level": "High",
            "risk_value": 100.0,
        }

    position = str(safe_player.get("position") or "MID").upper()
    minutes = _safe_float(safe_player.get("minutes"), 0.0)
    fixture_difficulty = _safe_float(safe_player.get("fixture_difficulty"), 3.0)
    xg_per_90 = _safe_float(safe_player.get("xg_per_90"), features["xg_per_90"])
    xa_per_90 = _safe_float(safe_player.get("xa_per_90"), features["xa_per_90"])
    fixture_ease_factor = fixture_ease(fixture_difficulty) / 5.0
    minutes_factor = min(minutes / 900.0, 1.0)
    team_cs_rate = clamp(_safe_float(safe_player.get("cs_rate"), 0.25), 0.0, 1.0)

    if position == "FWD":
        p_goal = min(xg_per_90 * fixture_ease_factor * minutes_factor, 0.75)
        p_assist = min(xa_per_90 * fixture_ease_factor * minutes_factor, 0.45)
        p_cs = 0.0
    elif position == "MID":
        p_goal = min(xg_per_90 * fixture_ease_factor * minutes_factor, 0.55)
        p_assist = min(xa_per_90 * fixture_ease_factor * minutes_factor, 0.50)
        p_cs = team_cs_rate * 0.3
    elif position == "DEF":
        p_goal = min(xg_per_90 * 0.5 * fixture_ease_factor, 0.15)
        p_assist = min(xa_per_90 * 0.6 * fixture_ease_factor, 0.20)
        p_cs = team_cs_rate * 0.85
    else:
        p_goal = 0.01
        p_assist = 0.02
        p_cs = team_cs_rate * 0.90

    p_goal = clamp(p_goal, 0.0, 1.0)
    p_assist = clamp(p_assist, 0.0, 1.0)
    p_cs = clamp(p_cs, 0.0, 1.0)
    if position == "GK":
        _goal_factor, _cs_factor = 0.1, 0.50
    elif position == "DEF":
        _goal_factor, _cs_factor = 0.45, 0.45
    elif position == "MID":
        _goal_factor, _cs_factor = 0.90, 0.15
    else:  # FWD
        _goal_factor, _cs_factor = 0.90, 0.05
    p_double_digit = clamp(
        (p_goal * _goal_factor)
        + (p_assist * 0.45)
        + (p_cs * _cs_factor)
        + (min(_safe_float(safe_player.get("bonus_rate"), 0.0), 1.0) * 0.2)
        + (0.08 if str(safe_player.get("form_trend") or "") == "rising" else 0.0),
        0.0,
        0.95,
    )

    appearance_pts = 2 if minutes > 0 else 0
    goal_pts = {"GK": 6, "DEF": 6, "MID": 5, "FWD": 4}[position]
    cs_pts = {"GK": 4, "DEF": 4, "MID": 1, "FWD": 0}[position]
    ep = (
        appearance_pts
        + p_goal * goal_pts
        + p_assist * 3
        + p_cs * cs_pts
        + _safe_float(safe_player.get("bonus_rate"), 0.3) * 1.5
    )
    ep = round(max(ep, 0.0), 2)

    floor = max(int(ep * 0.35), 1 if minutes > 0 else 0)
    ceiling = int(ep * 2.5) if p_double_digit > 0.15 else int(ep * 1.8)
    ceiling = max(ceiling, floor)

    form_trend = str(safe_player.get("form_trend") or "stable")
    if minutes >= 810 and form_trend != "falling":
        confidence = "high"
        confidence_reason = (
            f"Secure starter ({int(minutes)} mins) with {form_trend} form"
        )
    elif minutes >= 450:
        confidence = "medium"
        confidence_reason = "Regular starter but minutes or form uncertain"
    else:
        confidence = "low"
        confidence_reason = (
            f"Limited minutes ({int(minutes)}) - rotation or injury risk"
        )

    factors: list[str] = []
    if form_trend == "rising":
        factors.append(
            f"Form trending up (last 5 scores: {safe_player.get('recent_scores', [])})"
        )
    if _safe_int(safe_player.get("next_6_green_fixtures"), 0) >= 3:
        factors.append(
            f"Strong fixture run: {_safe_int(safe_player.get('next_6_green_fixtures'), 0)} green fixtures in next 6"
        )
    if p_goal > 0.4:
        factors.append(f"High goal threat: {xg_per_90:.2f} xG per 90")
    if p_cs > 0.5:
        factors.append(
            f"Good CS chance: team kept {_safe_float(safe_player.get('team_clean_sheets_last_5'), 0.0):.0f} CS in last 5"
        )
    if (
        len(factors) < 2
        and _safe_float(safe_player.get("team_form_rating"), 0.0) >= 0.6
    ):
        factors.append(
            f"Team form is solid: {_safe_float(safe_player.get('team_form_rating'), 0.0):.2f} over the last 5 fixtures"
        )
    if not factors:
        difficulty_text = (
            "favourable"
            if fixture_difficulty <= 2
            else "moderate"
            if fixture_difficulty == 3
            else "difficult"
        )
        factors.append(f"FDR {fixture_difficulty:.0f} - {difficulty_text} fixture")
    elif len(factors) < 2:
        difficulty_text = (
            "favourable"
            if fixture_difficulty <= 2
            else "moderate"
            if fixture_difficulty == 3
            else "difficult"
        )
        factors.append(f"FDR {fixture_difficulty:.0f} - {difficulty_text} fixture")

    risk_level, risk_value = _prediction_risk(safe_player, features)
    return {
        "expected_points": ep,
        "predicted_points_next_game": ep,
        "floor": floor,
        "ceiling": ceiling,
        "p_goal": round(p_goal, 3),
        "p_assist": round(p_assist, 3),
        "p_clean_sheet": round(p_cs, 3),
        "p_double_digit": round(p_double_digit, 3),
        "confidence": confidence,
        "confidence_reason": confidence_reason,
        "prediction_factors": factors[:3],
        "reason": confidence_reason,
        "risk_level": risk_level,
        "risk_value": risk_value,
    }


def predict_points_next_5_games(player: dict[str, Any]) -> dict[str, Any]:
    """Return a five-game projection with per-game detail and range."""
    safe_player = dict(player or {})
    next_game = predict_points_next_game(safe_player)
    if (
        _safe_float(next_game.get("expected_points"), 0.0) <= 0.0
        and _safe_int(
            safe_player.get("upcoming_fixture_count"),
            0,
        )
        <= 0
    ):
        return {
            "total": 0.0,
            "per_game": [0.0, 0.0, 0.0, 0.0, 0.0],
            "floor_total": 0,
            "ceiling_total": 0,
            "best_gw": 0,
            "blank_risk": True,
            "predicted_points_next_5_games": 0.0,
            "reason": "Blank Gameweek",
            "risk_level": str(next_game.get("risk_level") or "High"),
            "risk_value": float(next_game.get("risk_value") or 100.0),
        }

    fixture_difficulties = [
        _safe_float(value, 0.0)
        for value in (safe_player.get("next_6_fdrs") or [])
        if _safe_float(value, 0.0) > 0
    ]
    if not fixture_difficulties:
        upcoming_fixture_count = max(
            _safe_int(safe_player.get("upcoming_fixture_count"), 1), 1
        )
        fixture_difficulties = [
            _safe_float(safe_player.get("fixture_difficulty"), 3.0)
        ] * min(upcoming_fixture_count, 5)

    per_game: list[float] = []
    floor_total = 0
    ceiling_total = 0
    blank_risk = len(fixture_difficulties) < 5
    for index in range(5):
        if index >= len(fixture_difficulties):
            per_game.append(0.0)
            continue
        simulated_player = dict(safe_player)
        simulated_player["fixture_difficulty"] = fixture_difficulties[index]
        game_prediction = predict_points_next_game(simulated_player)
        per_game.append(
            round(_safe_float(game_prediction.get("expected_points"), 0.0), 2)
        )
        floor_total += _safe_int(game_prediction.get("floor"), 0)
        ceiling_total += _safe_int(game_prediction.get("ceiling"), 0)

    total = round(sum(per_game), 2)
    best_gw = per_game.index(max(per_game)) if per_game else 0
    return {
        "total": total,
        "per_game": per_game,
        "floor_total": floor_total,
        "ceiling_total": ceiling_total,
        "best_gw": best_gw,
        "blank_risk": blank_risk,
        "predicted_points_next_5_games": total,
        "reason": str(
            next_game.get("reason")
            or "Five-game projection built from next fixture run"
        ),
        "risk_level": str(next_game.get("risk_level") or "Medium"),
        "risk_value": float(next_game.get("risk_value") or 0.0),
    }


def calculate_expected_points(player: dict[str, Any]) -> float:
    return float(predict_points_next_game(player)["expected_points"])


def calculate_player_score(player: dict[str, Any]) -> dict[str, float | str]:
    features = build_feature_pack(player)
    next_game = predict_points_next_game(player)
    next_five = predict_points_next_5_games(player)
    if elite_predict is not None:
        elite_payload = dict(elite_predict(player) or {})
        ai_rating = round(
            _safe_float(elite_payload.get("ai_score", elite_payload.get("score"))), 2
        )
        enriched = {
            **features,
            **elite_payload,
            **next_game,
            **next_five,
            "score": ai_rating,
            "ai_score": ai_rating,
            "ai_rating": ai_rating,
            "expected_points": round(_safe_float(next_game.get("expected_points")), 2),
            "predicted_points_next_game": round(
                _safe_float(next_game.get("expected_points")),
                2,
            ),
            "predicted_points_next_5_games": round(
                _safe_float(next_five.get("total")),
                2,
            ),
            "risk_level": str(
                elite_payload.get("risk_level") or next_game.get("risk_level") or "High"
            ),
            "risk_value": _safe_float(
                elite_payload.get("risk_value"),
                _safe_float(next_game.get("risk_value"), 100.0),
            ),
            "confidence_score": _safe_float(
                elite_payload.get("confidence_score"),
                _confidence_score_from_label(
                    str(next_game.get("confidence") or "medium")
                ),
            ),
            "reason": str(
                next_game.get("confidence_reason")
                or elite_payload.get("reason")
                or elite_payload.get("prediction_reason")
                or _prediction_reason(player, features)
            ),
            "availability": str(
                elite_payload.get("availability") or _availability_label(player)
            ),
        }
        return enriched

    w = get_model_weights()  # thread-safe snapshot
    weighted_score = (
        (features["form_signal"] * w["form"])
        + (features["xg_signal"] * w["xg"])
        + (features["xa_signal"] * w["xa"])
        + (features["minutes_signal"] * w["minutes"])
        + (features["fixture_signal"] * w["fixture"])
        + (features["team_signal"] * w["team"])
        + (features["ownership_signal"] * w["ownership"])
        + (features["shots_signal"] * w["shots"])
        + (features["key_pass_signal"] * w["key_passes"])
        + (features["recent_form_signal"] * w["recent_form"])
        + (features["consistency_signal"] * w["consistency"])
        + (features["explosiveness_signal"] * w["explosiveness"])
    )
    ai_rating = round(weighted_score * 100.0, 2)
    return {
        "score": ai_rating,
        "ai_score": ai_rating,
        "ai_rating": ai_rating,
        "expected_points": round(_safe_float(next_game.get("expected_points")), 2),
        "predicted_points_next_game": round(
            _safe_float(next_game.get("expected_points")),
            2,
        ),
        "predicted_points_next_5_games": round(_safe_float(next_five.get("total")), 2),
        "risk_level": str(next_game["risk_level"]),
        "risk_value": float(next_game["risk_value"]),
        "confidence_score": round(
            _confidence_score_from_label(str(next_game.get("confidence") or "medium")),
            1,
        ),
        "reason": str(
            next_game.get("confidence_reason")
            or next_game.get("reason")
            or _prediction_reason(player, features)
        ),
        "availability": _availability_label(player),
        **features,
        **next_game,
        **next_five,
    }


def calculate_transfer_score(player: dict[str, Any]) -> float:
    ai_score = _safe_float(player.get("score", player.get("ai_score")), 0.0)
    predicted_5 = _safe_float(player.get("predicted_points_next_5_games"), 0.0)
    confidence = _safe_float(player.get("confidence_score"), 0.0)
    risk = _safe_float(player.get("risk_value"), 100.0)
    return round(
        max(ai_score * 0.52 + predicted_5 * 4.0 + confidence * 0.2 - risk * 0.12, 0.0),
        2,
    )


def calculate_captain_score(player: dict[str, Any]) -> float:
    base = float(player.get("score", 0.0) or 0.0)
    predicted = float(player.get("predicted_points_next_game", 0.0) or 0.0)
    confidence = _safe_float(player.get("confidence_score"), 0.0)
    attacker_bonus = 8.0 if str(player.get("position") or "") in {"MID", "FWD"} else 0.0
    penalty_bonus = 4.0 if float(player.get("goals", 0.0) or 0.0) >= 5 else 0.0
    fixture_bonus = (
        max(0.0, 5.0 - _safe_float(player.get("fixture_difficulty"), 3.0)) * 2.0
    )
    return round(
        base * 0.44
        + predicted * 5.0
        + attacker_bonus
        + penalty_bonus
        + fixture_bonus
        + confidence * 0.1,
        2,
    )


def calculate_differential_score(player: dict[str, Any]) -> float:
    if _availability_label(player) == "Blank Gameweek":
        return 0.0
    ownership = clamp(_safe_float(player.get("selected_by_percent"), 0.0), 0.0, 100.0)
    predicted = _safe_float(player.get("predicted_points_next_5_games"), 0.0)
    base = _safe_float(player.get("score"), 0.0)
    availability_penalty = (
        18.0
        if _availability_label(player) in {"Unavailable", "Blank Gameweek"}
        else 0.0
    )
    upside = clamp((20.0 - ownership) / 20.0, 0.0, 1.0)
    return round(
        max(predicted * 4.2 + base * 0.24 + upside * 28.0 - availability_penalty, 0.0),
        2,
    )



# ── Transfer outcome learning loop ───────────────────────────────────────────
# Reads transfer_outcomes and ai_learning_history from the DB to build
# per-player bias adjustments. Adjustments are small and decay over time so
# a bad week doesn't permanently tank a good player's score.

_LEARNING_CACHE: dict[str, Any] = {}
_LEARNING_CACHE_TTL = 300  # seconds


def _load_learning_adjustments(conn: sqlite3.Connection) -> dict[int, float]:
    """Return {player_id: score_bias} built from recent transfer outcomes.

    Logic:
    - For each completed transfer (OUT → IN pair with a net_gain recorded),
      if the AI recommended it and net_gain > 0 → small positive bias for the
      bought player (+0.5 per confirmed win, capped at +3.0).
    - If the AI recommended it and net_gain < 0 → small negative bias for that
      player type (-0.3 per miss, floor at -2.0).
    - Biases decay by 50% per gameweek elapsed (based on gameweek column).
    """
    import time

    cache_key = "learning_adj"
    cached = _LEARNING_CACHE.get(cache_key)
    if cached and cached.get("expires_at", 0) > time.time():
        return cached["data"]

    adjustments: dict[int, float] = {}
    try:
        current_gw_row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'current_gameweek'"
        ).fetchone()
        current_gw = int(current_gw_row[0]) if current_gw_row else 33

        rows = conn.execute(
            """
            SELECT to2.gameweek, to2.bought_player_id, to2.net_gain, to2.ai_recommended
            FROM transfer_outcomes to2
            WHERE to2.bought_player_id IS NOT NULL
              AND to2.net_gain IS NOT NULL
            ORDER BY to2.gameweek DESC
            LIMIT 100
            """
        ).fetchall()

        for row in rows:
            player_id = int(row[1] or 0)
            if player_id <= 0:
                continue
            net_gain = float(row[2] or 0.0)
            ai_recommended = bool(row[3])
            gw = int(row[0] or current_gw)
            decay = 0.5 ** max(current_gw - gw, 0)

            if not ai_recommended:
                continue

            bias = (0.5 if net_gain > 0 else -0.3) * decay
            adjustments[player_id] = round(
                max(-2.0, min(3.0, adjustments.get(player_id, 0.0) + bias)), 3
            )
    except Exception:
        pass

    _LEARNING_CACHE[cache_key] = {
        "data": adjustments,
        "expires_at": time.time() + _LEARNING_CACHE_TTL,
    }
    return adjustments


def apply_learning_adjustments(
    players: list[dict[str, Any]],
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Apply learned score biases to an enriched player list in-place."""
    if conn is None:
        return players
    adjustments = _load_learning_adjustments(conn)
    if not adjustments:
        return players
    for player in players:
        pid = int(player.get("id") or player.get("player_id") or 0)
        bias = adjustments.get(pid, 0.0)
        if bias:
            player["ai_score"] = round(
                max(0.0, float(player.get("ai_score", 0.0)) + bias), 2
            )
            player["score"] = player["ai_score"]
            player["learning_bias"] = bias
    return players


def enrich_players(players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for player in players:
        payload = dict(player)
        payload.update(calculate_player_score(payload))
        payload["transfer_score"] = float(
            payload.get("transfer_score") or calculate_transfer_score(payload)
        )
        payload["captain_score"] = float(
            payload.get("captain_score") or calculate_captain_score(payload)
        )
        payload["differential_score"] = float(
            payload.get("differential_score") or calculate_differential_score(payload)
        )
        payload["availability"] = str(
            payload.get("availability") or _availability_label(payload)
        )
        enriched.append(payload)
    return enriched


def _pick_outfield_lineup(players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    defenders = sorted(
        [player for player in players if player["position"] == "DEF"],
        key=lambda item: item["predicted_points_next_game"],
        reverse=True,
    )
    midfielders = sorted(
        [player for player in players if player["position"] == "MID"],
        key=lambda item: item["predicted_points_next_game"],
        reverse=True,
    )
    forwards = sorted(
        [player for player in players if player["position"] == "FWD"],
        key=lambda item: item["predicted_points_next_game"],
        reverse=True,
    )

    starting: list[dict[str, Any]] = defenders[:3] + midfielders[:2] + forwards[:1]
    selected_ids = {player["id"] for player in starting}
    remaining_slots = 10 - len(starting)
    remainder = sorted(
        [
            player
            for player in players
            if player["position"] != "GK" and player["id"] not in selected_ids
        ],
        key=lambda item: item["predicted_points_next_game"],
        reverse=True,
    )
    position_counts = {
        "DEF": len([player for player in starting if player["position"] == "DEF"]),
        "MID": len([player for player in starting if player["position"] == "MID"]),
        "FWD": len([player for player in starting if player["position"] == "FWD"]),
    }
    max_limits = {"DEF": 5, "MID": 5, "FWD": 3}

    for player in remainder:
        if remaining_slots <= 0:
            break
        position = player["position"]
        if position_counts[position] >= max_limits[position]:
            continue
        starting.append(player)
        position_counts[position] += 1
        remaining_slots -= 1

    return starting


def pick_best_lineup(players: list[dict[str, Any]]) -> dict[str, Any]:
    enriched = enrich_players(players)
    goalkeepers = sorted(
        [player for player in enriched if player["position"] == "GK"],
        key=lambda item: item["predicted_points_next_game"],
        reverse=True,
    )
    starting = goalkeepers[:1] + _pick_outfield_lineup(enriched)
    starting_ids = {player["id"] for player in starting}
    bench_goalkeepers = [
        player for player in goalkeepers[1:] if player["id"] not in starting_ids
    ]
    bench_outfield = sorted(
        [
            player
            for player in enriched
            if player["position"] != "GK" and player["id"] not in starting_ids
        ],
        key=lambda item: item["predicted_points_next_game"],
        reverse=True,
    )
    bench = (bench_goalkeepers[:1] + bench_outfield)[:4]
    formation = {
        "DEF": len([player for player in starting if player["position"] == "DEF"]),
        "MID": len([player for player in starting if player["position"] == "MID"]),
        "FWD": len([player for player in starting if player["position"] == "FWD"]),
    }
    pitch_rows = [
        {"position": "FWD", "players": [p for p in starting if p["position"] == "FWD"]},
        {"position": "MID", "players": [p for p in starting if p["position"] == "MID"]},
        {"position": "DEF", "players": [p for p in starting if p["position"] == "DEF"]},
        {"position": "GK", "players": [p for p in starting if p["position"] == "GK"]},
    ]
    return {
        "starting": starting,
        "bench": bench,
        "formation": f"{formation['DEF']}-{formation['MID']}-{formation['FWD']}",
        "pitch_rows": pitch_rows,
    }


def _risk_level(players: list[dict[str, Any]]) -> dict[str, Any]:
    if not players:
        return {"level": "Low", "value": 0.0}
    risk_value = sum(
        float(player.get("risk_value", 0.0) or 0.0) for player in players
    ) / len(players)
    if risk_value < 33:
        return {"level": "Low", "value": round(risk_value, 1)}
    if risk_value < 66:
        return {"level": "Medium", "value": round(risk_value, 1)}
    return {"level": "High", "value": round(risk_value, 1)}


def _available_transfer_budget(selected: list[dict[str, Any]]) -> float:
    squad_cost = sum(float(player.get("price", 0.0) or 0.0) for player in selected)
    return round(max(BUDGET_LIMIT - squad_cost, 0.0), 1)


def suggest_transfers(
    selected_players: list[dict[str, Any]],
    all_players: list[dict[str, Any]],
    risk_preference: str = "balanced",
) -> list[dict[str, Any]]:
    selected = enrich_players(selected_players)
    pool = enrich_players(all_players)
    selected_ids = {player["id"] for player in selected}
    available_budget = _available_transfer_budget(selected)
    suggestions: list[dict[str, Any]] = []
    used_incoming: set[int] = set()

    for current in sorted(selected, key=lambda item: item["score"]):
        budget_ceiling = float(current.get("price", 0.0) or 0.0) + available_budget
        options = [
            player
            for player in pool
            if player["position"] == current["position"]
            and player["id"] not in selected_ids
            and player["id"] not in used_incoming
            and float(player.get("price", 0.0) or 0.0) <= budget_ceiling
        ]
        options.sort(
            key=lambda item: (
                item["predicted_points_next_5_games"],
                item["score"],
                item["ownership_signal"] if risk_preference == "aggressive" else 0.0,
            ),
            reverse=True,
        )
        if not options:
            continue
        candidate = options[0]
        gain = round(candidate["score"] - current["score"], 2)
        if gain < 4.0:
            continue
        used_incoming.add(candidate["id"])
        suggestions.append(
            {
                "out": current,
                "in": candidate,
                "transfer_score_gain": gain,
                "price_change": round(
                    float(candidate.get("price", 0.0) or 0.0)
                    - float(current.get("price", 0.0) or 0.0),
                    1,
                ),
                "explanation": (
                    f"{candidate['name']} projects {candidate['predicted_points_next_5_games']:.2f} points over the next five games, "
                    f"offers a stronger AI rating, and carries a better fixture and minutes profile than {current['name']}."
                ),
            }
        )
        if len(suggestions) == 5:
            break
    return suggestions


def captain_rankings(players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = enrich_players(players)
    ranked.sort(
        key=lambda item: (
            item["captain_score"],
            item["predicted_points_next_game"],
            item["score"],
        ),
        reverse=True,
    )
    return ranked[:5]


def expected_points_table(players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = enrich_players(players)
    ranked.sort(
        key=lambda item: item["predicted_points_next_5_games"],
        reverse=True,
    )
    return ranked


def differential_picks(
    players: list[dict[str, Any]], selected_ids: set[int]
) -> list[dict[str, Any]]:
    ranked = [
        player
        for player in enrich_players(players)
        if float(player.get("selected_by_percent", 0.0) or 0.0) < 10.0
        and player["id"] not in selected_ids
    ]
    ranked.sort(
        key=lambda item: (
            item.get("differential_score", item["score"]),
            item["predicted_points_next_5_games"],
        ),
        reverse=True,
    )
    return ranked[:5]


def weak_players(players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = enrich_players(players)
    ranked.sort(
        key=lambda item: (
            item["score"],
            item["predicted_points_next_game"],
        )
    )
    return ranked[:3]


def injury_replacements(
    selected_players: list[dict[str, Any]], all_players: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    selected = enrich_players(selected_players)
    pool = enrich_players(all_players)
    selected_ids = {player["id"] for player in selected}
    replacements: list[dict[str, Any]] = []
    for current in selected:
        status = str(current.get("status") or "a").lower()
        chance = float(current.get("chance_of_playing_next_round") or 100)
        if status == "a" and chance >= 75:
            continue
        candidates = [
            player
            for player in pool
            if player["position"] == current["position"]
            and player["id"] not in selected_ids
        ]
        candidates.sort(key=lambda item: item["score"], reverse=True)
        if candidates:
            replacements.append({"out": current, "in": candidates[0]})
    return replacements[:3]


def chip_adjusted_points(
    lineup: dict[str, Any], captain: dict[str, Any] | None, active_chip: str
) -> float:
    starting_total = sum(
        float(player.get("predicted_points_next_game", 0.0) or 0.0)
        for player in lineup["starting"]
    )
    total = float(starting_total)
    if active_chip == "bench_boost":
        total += sum(
            float(player.get("predicted_points_next_game", 0.0) or 0.0)
            for player in lineup["bench"]
        )
    if active_chip == "triple_captain" and captain:
        total += float(captain.get("predicted_points_next_game", 0.0) or 0.0) * 2
    return round(total, 1)


def build_team_analysis(
    selected_players: list[dict[str, Any]],
    all_players: list[dict[str, Any]],
    active_chip: str = "",
    risk_preference: str = "balanced",
) -> dict[str, Any]:
    chip = active_chip if active_chip in CHIPS else ""
    enriched_selected = enrich_players(selected_players)
    lineup = pick_best_lineup(enriched_selected)
    player_rankings = sorted(
        enriched_selected,
        key=lambda item: (item["score"], item["predicted_points_next_5_games"]),
        reverse=True,
    )
    captain_options = captain_rankings(lineup["starting"])
    captain = captain_options[0] if captain_options else None
    vice_captain = captain_options[1] if len(captain_options) > 1 else None
    transfer_plan = suggest_transfers(
        enriched_selected,
        all_players,
        risk_preference=risk_preference,
    )
    xpoints_table = expected_points_table(enriched_selected)
    expected_team_points = chip_adjusted_points(lineup, captain, chip)
    risk = _risk_level(enriched_selected)
    squad_cost = round(
        sum(float(player.get("price", 0.0) or 0.0) for player in enriched_selected), 1
    )
    budget_remaining = round(BUDGET_LIMIT - squad_cost, 1)
    chart_players = sorted(
        lineup["starting"],
        key=lambda item: item["predicted_points_next_game"],
        reverse=True,
    )
    selected_ids = {player["id"] for player in enriched_selected}
    differentials = differential_picks(all_players, selected_ids)
    sell_recommendations = weak_players(enriched_selected)
    injury_moves = injury_replacements(enriched_selected, all_players)
    best_transfer = transfer_plan[0] if transfer_plan else None
    best_differential = differentials[0] if differentials else None
    average_ai_rating = round(
        sum(float(player.get("score", 0.0) or 0.0) for player in enriched_selected)
        / max(len(enriched_selected), 1),
        2,
    )
    summary = [
        "AI ratings blend form, xG, xA, minutes consistency, team strength, fixtures, and differential potential.",
        "Predictions use next-game and next-five-game models with risk labels built from minutes, form, and availability.",
        "Captain and transfer advice prioritise predicted returns, role strength, and low-risk upside.",
    ]

    return {
        "active_chip": chip,
        "captain": captain,
        "vice_captain": vice_captain,
        "captain_rankings": captain_options,
        "expected_team_points": expected_team_points,
        "risk": risk,
        "squad_cost": squad_cost,
        "budget_remaining": budget_remaining,
        "lineup": lineup,
        "player_rankings": player_rankings,
        "transfer_plan": transfer_plan,
        "best_transfer": best_transfer,
        "expected_points_table": xpoints_table,
        "differentials": differentials,
        "best_differential": best_differential,
        "sell_recommendations": sell_recommendations,
        "injury_replacements": injury_moves,
        "average_ai_rating": average_ai_rating,
        "summary": summary,
        "smart_recommendations": {
            "best_captain": captain,
            "best_transfer": best_transfer,
            "differentials": differentials[:3],
        },
        "risk_preference": risk_preference,
        "charts": {
            "expected_points": {
                "labels": [player["name"] for player in chart_players[:7]],
                "values": [
                    player["predicted_points_next_game"] for player in chart_players[:7]
                ],
            },
            "form": {
                "labels": [player["name"] for player in player_rankings[:7]],
                "values": [
                    round(float(player.get("form", 0.0) or 0.0), 1)
                    for player in player_rankings[:7]
                ],
            },
            "fixture_difficulty": {
                "labels": [player["name"] for player in player_rankings[:7]],
                "values": [
                    round(float(player.get("fixture_difficulty", 3.0) or 3.0), 1)
                    for player in player_rankings[:7]
                ],
            },
        },
    }


def build_chat_response(
    message: str,
    analysis_data: dict[str, Any],
    squad: list[dict[str, Any]],
    all_players: list[dict[str, Any]] | None = None,
) -> str:
    question = (message or "").strip().lower()
    if not question:
        return "Ask about captains, transfers, predicted points, fixture runs, or your squad weaknesses."

    def normalize_text(value: str) -> str:
        return " ".join(
            "".join(
                ch if ch.isalnum() or ch.isspace() else " " for ch in value.lower()
            ).split()
        )

    def player_name_candidates(player: dict[str, Any]) -> set[str]:
        candidates: set[str] = set()
        for field in ("name", "web_name", "second_name", "first_name"):
            value = str(player.get(field) or "").strip()
            if value:
                candidates.add(normalize_text(value))
        full_name = normalize_text(str(player.get("name") or ""))
        for token in full_name.split():
            if len(token) > 2:
                candidates.add(token)
        return {candidate for candidate in candidates if candidate}

    def find_player_match(players: list[dict[str, Any]]) -> dict[str, Any] | None:
        normalized_question = normalize_text(question)
        best_match = None
        best_length = 0
        for player in players:
            for candidate in player_name_candidates(player):
                if candidate in normalized_question and len(candidate) > best_length:
                    best_match = player
                    best_length = len(candidate)
        return best_match

    pool = enrich_players(all_players or []) if all_players else []
    squad_enriched = enrich_players(squad)
    pool = pool or squad_enriched
    squad_ids = {player.get("id") for player in squad_enriched}
    player_match = find_player_match(pool)

    if "captain" in question:
        captain = analysis_data.get("captain")
        vice = analysis_data.get("vice_captain")
        if not captain:
            return "I need a full squad before I can recommend a captain."
        reply = (
            f"Captain {captain['name']} is the strongest pick with AI {captain['score']:.1f}, "
            f"{captain['predicted_points_next_game']:.2f} predicted points next game, and {captain['risk_level']} risk."
        )
        if vice:
            reply += f" Vice-captain {vice['name']} is the backup."
        return reply

    if any(
        keyword in question
        for keyword in ("wildcard", "free hit", "bench boost", "triple captain", "chip")
    ):
        captain = analysis_data.get("captain")
        if "wildcard" in question:
            return (
                f"Wildcard works best when your squad has multiple weak spots. Right now your risk is {analysis_data.get('risk', {}).get('level', 'Unknown')} "
                f"and the transfer planner sees {len(analysis_data.get('transfer_plan', []))} meaningful upgrades."
            )
        if "free hit" in question:
            return "Free Hit is strongest in a blank or highly asymmetric gameweek when your saved squad has poor fixtures."
        if "bench boost" in question:
            return "Bench Boost is strongest when all 15 players have secure minutes and at least decent fixture outlooks."
        if "triple captain" in question:
            if captain:
                return f"Triple Captain looks strongest on {captain['name']} because they lead your squad in projected points and captain score."
            return "Triple Captain is best used on your top projected attacker in a strong fixture or double gameweek."
        return "Chip strategy depends on squad weakness, fixtures, and available doubles/blanks."

    if "double gameweek" in question or "blank gameweek" in question:
        flagged = [
            player
            for player in pool
            if player.get("upcoming_double") or player.get("upcoming_blank")
        ][:5]
        if flagged:
            names = ", ".join(
                f"{player['name']} ({'DGW' if player.get('upcoming_double') else 'BGW'})"
                for player in flagged
            )
            return f"Tracked blank/double gameweek signals: {names}."
        return "No strong blank or double gameweek signal is currently cached in the synced data."

    if "differential" in question:
        differentials = analysis_data.get("differentials") or []
        if not differentials:
            return "I do not see a strong low-ownership differential pick right now."
        picks = ", ".join(
            f"{player['name']} ({player['selected_by_percent']:.1f}% owned, AI {player['score']:.1f})"
            for player in differentials[:3]
        )
        return f"Best differential picks: {picks}."

    if any(keyword in question for keyword in ("transfer", "buy", "sell", "replace")):
        if player_match and player_match.get("id") in squad_ids:
            for move in analysis_data.get("transfer_plan", []):
                if move["out"]["id"] == player_match["id"]:
                    return (
                        f"Selling {move['out']['name']} for {move['in']['name']} is the clearest move. "
                        f"Projected gain: {move['transfer_score_gain']:.1f} AI points and {move['in']['predicted_points_next_5_games']:.2f} points over the next five games."
                    )
        transfers = analysis_data.get("transfer_plan") or []
        if transfers:
            move = transfers[0]
            return (
                f"Best transfer: sell {move['out']['name']} for {move['in']['name']}. "
                f"{move['explanation']}"
            )
        return "No strong transfer upgrade stands out right now."

    if "fixture" in question:
        if player_match:
            return (
                f"{player_match['name']} has fixture ease {player_match['fixture_ease']:.1f} and "
                f"projects {player_match['predicted_points_next_5_games']:.2f} points over the next five games."
            )
        ranked = sorted(pool, key=lambda item: item["fixture_ease"], reverse=True)[:3]
        names = ", ".join(
            f"{player['name']} (ease {player['fixture_ease']:.1f})" for player in ranked
        )
        return f"Best fixture runs: {names}."

    if player_match and any(
        keyword in question for keyword in ("form", "performance", "stats", "compare")
    ):
        return (
            f"{player_match['name']} | AI {player_match['score']:.1f}, Form {player_match['form']:.1f}, "
            f"xG/90 {player_match['xg_per_90']:.2f}, xA/90 {player_match['xa_per_90']:.2f}, "
            f"Next game {player_match['predicted_points_next_game']:.2f}, Next 5 {player_match['predicted_points_next_5_games']:.2f}."
        )

    if "expected" in question or "predict" in question or "points" in question:
        if player_match:
            return (
                f"{player_match['name']} projects {player_match['predicted_points_next_game']:.2f} points next game and "
                f"{player_match['predicted_points_next_5_games']:.2f} over the next five, with {player_match['risk_level']} risk."
            )
        return (
            f"Your squad projects {analysis_data.get('expected_team_points', 0):.1f} points next gameweek, "
            f"with an average AI rating of {analysis_data.get('average_ai_rating', 0):.1f}."
        )

    if "weak" in question or "sell" in question:
        weak = analysis_data.get("sell_recommendations") or []
        if weak:
            names = ", ".join(
                f"{player['name']} (AI {player['score']:.1f}, risk {player['risk_level']})"
                for player in weak[:3]
            )
            return f"Weakest squad spots right now: {names}."

    if "analyze" in question or "review" in question or "team" in question:
        captain = analysis_data.get("captain")
        differential = analysis_data.get("best_differential")
        best_transfer = analysis_data.get("best_transfer")
        parts = [
            f"Expected team score: {analysis_data.get('expected_team_points', 0):.1f}",
            f"Risk: {analysis_data.get('risk', {}).get('level', 'Unknown')}",
        ]
        if captain:
            parts.append(f"Captain: {captain['name']}")
        if differential:
            parts.append(f"Differential: {differential['name']}")
        if best_transfer:
            parts.append(
                f"Transfer: {best_transfer['out']['name']} -> {best_transfer['in']['name']}"
            )
        return " | ".join(parts)

    return (
        "I can help with captain picks, transfers, differentials, expected points, fixtures, or squad reviews. "
        "Try: Who should I captain? Best differential? or Which midfielder should I buy?"
    )
