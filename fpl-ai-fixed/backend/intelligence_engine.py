from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in {None, ""}:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value in {None, ""}:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=True)
    except TypeError:
        return "{}"


def _load_json(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _position_breakdown(players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for player in players:
        position = str(player.get("position") or "").upper()
        if position in groups:
            groups[position].append(player)

    breakdown: list[dict[str, Any]] = []
    labels = {
        "GK": "Goalkeepers",
        "DEF": "Defence",
        "MID": "Midfield",
        "FWD": "Forwards",
    }
    for code, items in groups.items():
        if not items:
            continue
        projected_total = sum(
            _to_float(item.get("predicted_points_next_game"), 0.0) for item in items
        )
        projected_avg = projected_total / max(len(items), 1)
        breakdown.append(
            {
                "code": code,
                "label": labels[code],
                "count": len(items),
                "projected_total": round(projected_total, 2),
                "projected_avg": round(projected_avg, 2),
            }
        )
    breakdown.sort(key=lambda item: item["projected_avg"], reverse=True)
    return breakdown


def compute_risk(player: dict[str, Any]) -> dict[str, Any]:
    recent_minutes = _to_float(
        player.get("recent_minutes_avg"), _to_float(player.get("minutes"), 0.0)
    )
    consistency = _clamp(_to_float(player.get("consistency_score"), 0.0), 0.0, 1.0)
    fixture_difficulty = _clamp(
        _to_float(player.get("fixture_difficulty"), 3.0), 1.0, 5.0
    )
    chance = _to_float(player.get("chance_of_playing_next_round"), 100.0)
    status = str(player.get("status") or "a").strip().lower()
    sentiment = _clamp(_to_float(player.get("news_sentiment_score"), 0.0), -1.0, 1.0)
    form = _to_float(player.get("form"), 0.0)

    score = 0.0
    reasons: list[str] = []

    if recent_minutes < 45:
        score += 38
        reasons.append("minutes are highly unstable")
    elif recent_minutes < 65:
        score += 22
        reasons.append("minutes are not fully secure")
    elif recent_minutes < 78:
        score += 10
        reasons.append("there is still some minutes volatility")

    if consistency < 0.35:
        score += 18
        reasons.append("recent returns have been volatile")
    elif consistency < 0.55:
        score += 9
        reasons.append("form has been mixed")

    if form < 3.5:
        score += 12
        reasons.append("recent form is weak")

    if status in {"i", "s", "u"} or chance <= 25:
        score += 32
        reasons.append("availability is a major concern")
    elif status == "d" or chance <= 60:
        score += 20
        reasons.append("there is a real fitness risk")
    elif chance < 90:
        score += 8
        reasons.append("selection is not fully clean")

    if fixture_difficulty >= 4.5:
        score += 14
        reasons.append("the fixture is difficult")
    elif fixture_difficulty >= 4.0:
        score += 8
        reasons.append("the fixture is tougher than ideal")

    if sentiment <= -0.45:
        score += 14
        reasons.append("team news is leaning negative")
    elif sentiment < -0.15:
        score += 6
        reasons.append("news flow adds some uncertainty")

    score = round(_clamp(score, 0.0, 100.0), 1)
    if score < 34:
        label = "Low"
    elif score < 67:
        label = "Medium"
    else:
        label = "High"

    if not reasons:
        reasons.append("the role looks stable for this gameweek")

    return {
        "label": label,
        "score": score,
        "reasons": reasons[:4],
        "summary": f"{label} risk because " + ", ".join(reasons[:2]) + ".",
    }


def compute_confidence(player: dict[str, Any]) -> dict[str, Any]:
    recent_minutes = _to_float(
        player.get("recent_minutes_avg"), _to_float(player.get("minutes"), 0.0)
    )
    confidence_seed = _clamp(_to_float(player.get("confidence_score"), 0.0), 0.0, 100.0)
    fixture_difficulty = _clamp(
        _to_float(player.get("fixture_difficulty"), 3.0), 1.0, 5.0
    )
    recent_form = max(
        _to_float(player.get("recent_form_score"), 0.0),
        _to_float(player.get("form"), 0.0),
    )
    xg = _to_float(player.get("expected_goals"), 0.0)
    xa = _to_float(player.get("expected_assists"), 0.0)
    consistency = _clamp(_to_float(player.get("consistency_score"), 0.0), 0.0, 1.0)
    chance = _to_float(player.get("chance_of_playing_next_round"), 100.0)
    status = str(player.get("status") or "a").strip().lower()
    risk = compute_risk(player)

    score = confidence_seed
    reasons: list[str] = []

    if recent_minutes >= 80:
        score += 12
        reasons.append("minutes look locked")
    elif recent_minutes >= 70:
        score += 7
        reasons.append("minutes look strong")

    if recent_form >= 7.0:
        score += 12
        reasons.append("recent form is strong")
    elif recent_form >= 5.5:
        score += 7
        reasons.append("recent form is solid")

    if xg >= 2.2 or xa >= 2.0:
        score += 10
        reasons.append("underlying data supports returns")
    elif xg >= 1.2 or xa >= 1.0:
        score += 6
        reasons.append("there is a healthy attacking floor")

    if consistency >= 0.65:
        score += 9
        reasons.append("output has been consistent")
    elif consistency >= 0.5:
        score += 5
        reasons.append("recent returns are reasonably steady")

    if fixture_difficulty <= 2.0:
        score += 8
        reasons.append("the fixture is favorable")
    elif fixture_difficulty <= 2.7:
        score += 4
        reasons.append("the fixture is workable")

    if status in {"i", "s", "u"} or chance < 60:
        score -= 28
    elif chance < 90:
        score -= 10

    if risk["label"] == "High":
        score -= 16
    elif risk["label"] == "Medium":
        score -= 6

    score = round(_clamp(score, 0.0, 100.0), 1)
    if score >= 70:
        label = "High"
    elif score >= 45:
        label = "Medium"
    else:
        label = "Low"

    if not reasons:
        reasons.append("the profile is playable but not completely clean")

    return {
        "label": label,
        "score": score,
        "reasons": reasons[:4],
        "summary": f"{label} confidence because " + ", ".join(reasons[:2]) + ".",
    }


def explain_player(player: dict[str, Any], focus: str = "general") -> dict[str, Any]:
    name = str(player.get("name") or "This player")
    confidence = compute_confidence(player)
    risk = compute_risk(player)
    fixture_difficulty = _to_float(player.get("fixture_difficulty"), 3.0)
    recent_form = max(
        _to_float(player.get("recent_form_score"), 0.0),
        _to_float(player.get("form"), 0.0),
    )
    xg = _to_float(player.get("expected_goals"), 0.0)
    xa = _to_float(player.get("expected_assists"), 0.0)
    recent_minutes = _to_float(
        player.get("recent_minutes_avg"), _to_float(player.get("minutes"), 0.0)
    )

    reasons: list[str] = []
    cautions: list[str] = []

    if recent_form >= 6.5:
        reasons.append("strong recent form")
    elif recent_form >= 5.0:
        reasons.append("steady recent form")

    if xg >= 1.7:
        reasons.append("strong goal threat")
    elif xa >= 1.5:
        reasons.append("creative numbers are healthy")
    elif xg + xa >= 2.2:
        reasons.append("underlying attacking data is healthy")

    if recent_minutes >= 75:
        reasons.append("minutes look secure")
    elif recent_minutes < 60:
        cautions.append("minutes are not fully safe")

    if fixture_difficulty <= 2.2:
        reasons.append("the fixture is favorable")
    elif fixture_difficulty >= 4.0:
        cautions.append("the fixture is difficult")

    if risk["label"] == "High":
        cautions.append("the risk profile is elevated")
    elif confidence["label"] == "Low":
        cautions.append("confidence is limited")

    if focus == "captain":
        verdict = (
            f"I would captain {name} this week."
            if confidence["label"] == "High" and risk["label"] != "High"
            else f"{name} is in the captain conversation, but not without risk."
        )
    elif focus == "transfer_in":
        verdict = (
            f"I like buying {name} this week."
            if confidence["label"] != "Low"
            else f"{name} is more of a wait-and-see transfer right now."
        )
    elif focus == "transfer_out":
        verdict = (
            f"I would move {name} on this week."
            if risk["label"] == "High" or confidence["label"] == "Low"
            else f"{name} is still usable, but there are cleaner alternatives."
        )
    else:
        verdict = (
            f"I like {name} this week."
            if confidence["label"] == "High" and risk["label"] == "Low"
            else f"{name} is playable, but not a no-brainer."
        )

    summary_bits = reasons[:3] or confidence["reasons"][:2]
    caution_bits = cautions[:2] or (
        risk["reasons"][:1] if risk["label"] != "Low" else []
    )
    summary = verdict
    if summary_bits:
        summary += " High confidence pick because " + ", ".join(summary_bits) + "."
    if caution_bits:
        summary += " Main caution: " + ", ".join(caution_bits) + "."

    return {
        "name": name,
        "verdict": verdict,
        "summary": summary,
        "confidence": confidence,
        "risk": risk,
        "reasons": (reasons or confidence["reasons"])[:4],
        "cautions": caution_bits[:2],
    }


def store_user_decision(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    gameweek: int,
    decision_type: str,
    player_id: int | None = None,
    related_player_id: int | None = None,
    decision_label: str = "",
    rationale: str = "",
    confidence_label: str = "",
    risk_label: str = "",
    expected_gain: float | None = None,
    outcome_points: float | None = None,
    actual_gain: float | None = None,
    source: str = "user",
    status: str = "tracked",
    context: dict[str, Any] | None = None,
    created_at: str = "",
) -> None:
    if user_id <= 0 or not decision_type:
        return

    timestamp = created_at or datetime.utcnow().isoformat(timespec="seconds")
    lookup = conn.execute(
        """
        SELECT id FROM user_decisions
        WHERE user_id = ?
          AND decision_type = ?
          AND gameweek = ?
          AND COALESCE(player_id, 0) = ?
          AND COALESCE(related_player_id, 0) = ?
          AND source = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            user_id,
            decision_type,
            gameweek,
            _to_int(player_id, 0),
            _to_int(related_player_id, 0),
            source,
        ),
    ).fetchone()
    payload = (
        decision_label,
        rationale,
        confidence_label,
        risk_label,
        expected_gain,
        outcome_points,
        actual_gain,
        status,
        _safe_json(context),
        timestamp,
    )
    if lookup:
        conn.execute(
            """
            UPDATE user_decisions
            SET decision_label = ?,
                rationale = ?,
                confidence_label = ?,
                risk_label = ?,
                expected_gain = ?,
                outcome_points = ?,
                actual_gain = ?,
                status = ?,
                context_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            payload + (int(lookup[0]),),
        )
        return

    conn.execute(
        """
        INSERT INTO user_decisions (
            user_id, gameweek, decision_type, player_id, related_player_id,
            decision_label, rationale, confidence_label, risk_label,
            expected_gain, outcome_points, actual_gain, source, status,
            context_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            gameweek,
            decision_type,
            player_id,
            related_player_id,
            decision_label,
            rationale,
            confidence_label,
            risk_label,
            expected_gain,
            outcome_points,
            actual_gain,
            source,
            status,
            _safe_json(context),
            timestamp,
            timestamp,
        ),
    )


def _sum_player_points(
    conn: sqlite3.Connection, player_id: int, start_gameweek: int, end_gameweek: int
) -> float:
    if player_id <= 0 or start_gameweek <= 0 or end_gameweek < start_gameweek:
        return 0.0
    row = conn.execute(
        """
        SELECT COALESCE(SUM(total_points), 0)
        FROM player_gameweek_history
        WHERE player_id = ? AND gameweek BETWEEN ? AND ?
        """,
        (player_id, start_gameweek, end_gameweek),
    ).fetchone()
    return round(_to_float(row[0] if row else 0.0, 0.0), 2)


def refresh_user_decision_outcomes(
    conn: sqlite3.Connection, user_id: int, latest_finished_gw: int
) -> None:
    if user_id <= 0 or latest_finished_gw <= 0:
        return

    rows = conn.execute(
        """
        SELECT * FROM user_decisions
        WHERE user_id = ?
          AND gameweek > 0
          AND gameweek <= ?
          AND (actual_gain IS NULL OR status != 'resolved')
        ORDER BY gameweek DESC, id DESC
        """,
        (user_id, latest_finished_gw),
    ).fetchall()

    for row in rows:
        decision_id = _to_int(row["id"], 0)
        gameweek = _to_int(row["gameweek"], 0)
        decision_type = str(row["decision_type"] or "").strip().lower()
        player_id = _to_int(row["player_id"], 0)
        related_player_id = _to_int(row["related_player_id"], 0)
        if decision_id <= 0 or gameweek <= 0:
            continue

        actual_gain: float | None = None
        outcome_points: float | None = None

        if (
            decision_type == "captain"
            and player_id > 0
            and gameweek <= latest_finished_gw
        ):
            outcome_points = _sum_player_points(conn, player_id, gameweek, gameweek)
            actual_gain = outcome_points
        elif decision_type == "transfer" and player_id > 0 and related_player_id > 0:
            end_window = min(gameweek + 1, latest_finished_gw)
            if end_window >= gameweek:
                sold_points = _sum_player_points(conn, player_id, gameweek, end_window)
                bought_points = _sum_player_points(
                    conn, related_player_id, gameweek, end_window
                )
                outcome_points = bought_points
                actual_gain = round(bought_points - sold_points, 2)

        if actual_gain is None and outcome_points is None:
            continue

        conn.execute(
            """
            UPDATE user_decisions
            SET actual_gain = ?,
                outcome_points = ?,
                status = 'resolved',
                updated_at = ?
            WHERE id = ?
            """,
            (
                actual_gain,
                outcome_points,
                datetime.utcnow().isoformat(timespec="seconds"),
                decision_id,
            ),
        )


def recall_user_memory(
    conn: sqlite3.Connection,
    user_id: int,
    decision_type: str = "",
    player_id: int | None = None,
    related_player_id: int | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    if user_id <= 0:
        return {"items": [], "summary": "No personal decision history tracked yet."}

    filters = ["user_id = ?"]
    params: list[Any] = [user_id]
    if decision_type:
        filters.append("decision_type = ?")
        params.append(decision_type)
    if player_id:
        filters.append("(player_id = ? OR related_player_id = ?)")
        params.extend([player_id, player_id])
    if related_player_id:
        filters.append("(player_id = ? OR related_player_id = ?)")
        params.extend([related_player_id, related_player_id])

    rows = conn.execute(
        f"""
        SELECT * FROM user_decisions
        WHERE {" AND ".join(filters)}
        ORDER BY gameweek DESC, created_at DESC
        LIMIT ?
        """,
        params + [max(limit, 1)],
    ).fetchall()

    items: list[dict[str, Any]] = []
    resolved_gains: list[float] = []
    for row in rows:
        item = {
            "id": _to_int(row["id"], 0),
            "gameweek": _to_int(row["gameweek"], 0),
            "decision_type": str(row["decision_type"] or ""),
            "player_id": _to_int(row["player_id"], 0),
            "related_player_id": _to_int(row["related_player_id"], 0),
            "label": str(row["decision_label"] or "").strip(),
            "rationale": str(row["rationale"] or "").strip(),
            "confidence": str(row["confidence_label"] or "").strip() or "Medium",
            "risk": str(row["risk_label"] or "").strip() or "Medium",
            "expected_gain": round(_to_float(row["expected_gain"], 0.0), 2),
            "actual_gain": round(_to_float(row["actual_gain"], 0.0), 2)
            if row["actual_gain"] is not None
            else None,
            "outcome_points": round(_to_float(row["outcome_points"], 0.0), 2)
            if row["outcome_points"] is not None
            else None,
            "source": str(row["source"] or "user"),
            "status": str(row["status"] or "tracked"),
            "context": _load_json(row["context_json"]),
        }
        items.append(item)
        if item["actual_gain"] is not None:
            resolved_gains.append(item["actual_gain"])

    if not items:
        return {"items": [], "summary": "No similar decisions have been tracked yet."}

    latest = items[0]
    if decision_type == "captain":
        if latest.get("outcome_points") is not None:
            summary = (
                f"Last tracked captain call was GW{latest['gameweek']} and returned "
                f"{latest['outcome_points']:.0f} points."
            )
        else:
            summary = "Your latest captain call is still waiting for a result."
    elif decision_type == "transfer":
        if resolved_gains:
            average_gain = sum(resolved_gains) / max(len(resolved_gains), 1)
            summary = (
                f"Your last {len(resolved_gains)} tracked transfer moves are averaging "
                f"{average_gain:+.1f} points across the review window."
            )
        else:
            summary = "Your recent transfer moves are tracked, but the outcome window is still open."
    else:
        if resolved_gains:
            summary = (
                f"Your recent tracked decisions are averaging "
                f"{(sum(resolved_gains) / max(len(resolved_gains), 1)):+.1f} points."
            )
        else:
            summary = "Your decision history is building, but most recent calls are still live."

    return {
        "items": items,
        "latest": latest,
        "summary": summary,
    }


def build_captain_brief(
    analysis_data: dict[str, Any],
    squad: list[dict[str, Any]],
    memory: dict[str, Any] | None = None,
    rival: dict[str, Any] | None = None,
) -> dict[str, Any]:
    captain = (analysis_data or {}).get("captain") or {}
    current_captain = next(
        (player for player in squad if bool(player.get("is_captain"))), None
    )
    if not captain:
        return {
            "title": "Captain call pending",
            "decision": "Save a full squad to unlock captain advice.",
            "summary": "TACTIX needs a full squad before it can make a captain call.",
            "confidence": "Medium",
            "risk": "Medium",
            "reasons": [],
        }

    explanation = explain_player(captain, focus="captain")
    expected_gain = max(
        _to_float(captain.get("predicted_points_next_game"), 0.0)
        - _to_float((current_captain or {}).get("predicted_points_next_game"), 0.0),
        0.0,
    )
    memory_note = str((memory or {}).get("summary") or "").strip()
    rival_note = str((rival or {}).get("summary") or "").strip()

    if current_captain and _to_int(current_captain.get("id"), 0) == _to_int(
        captain.get("id"), 0
    ):
        decision = f"Keep the armband on {captain.get('name', 'your captain')}."
    elif current_captain:
        decision = (
            f"Switch captain from {current_captain.get('name', 'your current captain')} "
            f"to {captain.get('name', 'the model leader')}."
        )
    else:
        decision = f"Captain {captain.get('name', 'the model leader')}."

    summary = (
        f"{decision} {captain.get('name', 'This player')} leads your squad for next-game projection "
        f"and carries {explanation['risk']['label'].lower()} risk with {explanation['confidence']['label'].lower()} confidence."
    )

    return {
        "title": "Captain verdict",
        "decision": decision,
        "summary": summary,
        "player": captain,
        "confidence": explanation["confidence"]["label"],
        "risk": explanation["risk"]["label"],
        "expected_gain": round(expected_gain, 2),
        "reasons": explanation["reasons"],
        "why": explanation["summary"],
        "memory_note": memory_note,
        "rival_note": rival_note,
    }


def build_transfer_brief(
    analysis_data: dict[str, Any], memory: dict[str, Any] | None = None
) -> dict[str, Any]:
    move = (analysis_data or {}).get("best_transfer") or {}
    if not move:
        return {
            "title": "Transfer call pending",
            "decision": "No clear transfer is being forced right now.",
            "summary": "The model does not see an urgent move, so protecting the transfer is reasonable.",
            "confidence": "Medium",
            "risk": "Low",
            "reasons": ["there is no standout upgrade gap in the current squad"],
        }

    outgoing = move.get("out") or {}
    incoming = move.get("in") or {}
    incoming_explainer = explain_player(incoming, focus="transfer_in")
    outgoing_explainer = explain_player(outgoing, focus="transfer_out")
    expected_gain = round(
        max(
            (
                _to_float(incoming.get("predicted_points_next_5_games"), 0.0)
                - _to_float(outgoing.get("predicted_points_next_5_games"), 0.0)
            )
            * 0.4,
            _to_float(move.get("transfer_score_gain"), 0.0) * 0.35,
        ),
        2,
    )

    decision = (
        f"Sell {outgoing.get('name', 'your weakest asset')} and buy "
        f"{incoming.get('name', 'the priority target')}."
    )
    summary = (
        f"{decision} This is the clearest upgrade because {incoming.get('name', 'the target')} "
        f"offers a better next-two-gameweek runway while {outgoing.get('name', 'the outgoing player')} "
        f"carries more downside."
    )

    return {
        "title": "Transfer verdict",
        "decision": decision,
        "summary": summary,
        "out": outgoing,
        "in": incoming,
        "confidence": incoming_explainer["confidence"]["label"],
        "risk": outgoing_explainer["risk"]["label"],
        "expected_gain": expected_gain,
        "reasons": incoming_explainer["reasons"][:3]
        + outgoing_explainer["cautions"][:1],
        "why": str(move.get("explanation") or incoming_explainer["summary"]),
        "memory_note": str((memory or {}).get("summary") or "").strip(),
    }


def rival_analysis(
    analysis_data: dict[str, Any],
    user_squad: list[dict[str, Any]],
    all_players: list[dict[str, Any]],
    rival_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = rival_context if isinstance(rival_context, dict) else {}
    if not context:
        return {
            "summary": "Add a mini-league ID to unlock rival tracking and captaincy threat analysis.",
            "threat_players": [],
            "swing_risk_points": 0.0,
            "recommended_response": "Use mini-league sync when you want true rival-aware advice.",
        }

    player_map = {int(player.get("id", 0)): player for player in all_players}
    user_ids = {int(player.get("id", 0)) for player in user_squad}
    user_captain_id = next(
        (int(player.get("id", 0)) for player in user_squad if player.get("is_captain")),
        0,
    )
    threat_players: list[dict[str, Any]] = []
    for item in context.get("picks", []) or []:
        player_id = _to_int(item.get("player_id"), 0)
        multiplier = max(_to_int(item.get("multiplier"), 1), 1)
        player = player_map.get(player_id)
        if not player:
            continue
        if player_id == user_captain_id:
            continue
        if player_id not in user_ids or multiplier > 1:
            swing_multiplier = 2 if player_id not in user_ids else 1
            swing = round(
                _to_float(player.get("predicted_points_next_game"), 0.0)
                * swing_multiplier,
                2,
            )
            if swing < 2.0:
                continue
            threat_players.append(
                {
                    "player_id": player_id,
                    "name": str(player.get("name") or item.get("name") or "Player"),
                    "team": str(player.get("team_name") or ""),
                    "projected_swing": swing,
                    "is_captain": multiplier > 1,
                }
            )
    threat_players.sort(key=lambda item: item["projected_swing"], reverse=True)
    threat_players = threat_players[:3]
    swing_risk = round(
        sum(item.get("projected_swing", 0.0) for item in threat_players), 2
    )
    rival_name = str(context.get("rival_name") or "your closest rival")
    gap = round(_to_float(context.get("gap_points"), 0.0), 1)
    rival_captain = next(
        (item for item in threat_players if bool(item.get("is_captain"))), None
    )

    if rival_captain:
        summary = (
            f"{rival_name} owns and is likely captaining {rival_captain['name']}. "
            f"If he lands, the swing could be roughly {rival_captain['projected_swing']:.1f} points."
        )
    elif threat_players:
        names = ", ".join(item["name"] for item in threat_players[:2])
        summary = f"{rival_name} is {gap:.0f} points away and their main uncovered threats are {names}."
    else:
        summary = f"{rival_name} is {gap:.0f} points away, but there is no major uncovered threat in the current projection set."

    recommended_response = (
        f"Protect yourself with {analysis_data.get('captain', {}).get('name', 'your strongest captain')} and keep at least one safe, high-minutes attacker."
        if rival_captain
        else "You can lean into your best projected move rather than chasing a forced differential right now."
    )

    return {
        "summary": summary,
        "rival_name": rival_name,
        "gap_points": gap,
        "threat_players": threat_players,
        "swing_risk_points": swing_risk,
        "recommended_response": recommended_response,
    }


def build_insights_payload(
    analysis_data: dict[str, Any],
    squad: list[dict[str, Any]],
    all_players: list[dict[str, Any]],
    report: dict[str, Any] | None = None,
    captain_brief: dict[str, Any] | None = None,
    transfer_brief: dict[str, Any] | None = None,
    rival_brief: dict[str, Any] | None = None,
    memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lineup = ((analysis_data or {}).get("lineup") or {}).get("starting") or squad
    breakdown = _position_breakdown(lineup)
    strongest_line = breakdown[0] if breakdown else None
    weakest_line = breakdown[-1] if breakdown else None
    weak_spots = (analysis_data or {}).get("sell_recommendations") or []
    differentials = (analysis_data or {}).get("differentials") or []
    flagged_player = weak_spots[0] if weak_spots else None
    captain = (analysis_data or {}).get("captain") or {}

    cards = [
        {
            "tone": "good",
            "title": "Strong area",
            "body": (
                f"Your {strongest_line['label'].lower()} are currently your strongest unit at "
                f"{strongest_line['projected_avg']:.1f} projected points per slot."
                if strongest_line
                else "Your strongest unit will appear once a full lineup is available."
            ),
        },
        {
            "tone": "bad" if flagged_player else "neutral",
            "title": "Pressure point",
            "body": (
                f"{flagged_player.get('name')} is your main weak spot because the role is less secure and the projection is softer."
                if flagged_player
                else (
                    f"{weakest_line['label']} is your softest line this week, so that is where most upside lives."
                    if weakest_line
                    else "No clear pressure point is showing yet."
                )
            ),
        },
        {
            "tone": "neutral",
            "title": "Rival radar",
            "body": str(
                (rival_brief or {}).get("summary")
                or "Rival tracking will appear once a mini-league is connected."
            ),
        },
    ]

    recommendations = [
        {
            "title": (captain_brief or {}).get("title") or "Captaincy",
            "body": (captain_brief or {}).get("summary")
            or "Captain advice is loading.",
        },
        {
            "title": (transfer_brief or {}).get("title") or "Transfer",
            "body": (transfer_brief or {}).get("summary")
            or "Transfer advice is loading.",
        },
        {
            "title": "Differential angle",
            "body": (
                f"If you need upside, {differentials[0].get('name')} is the best differential because the projection is strong and ownership is still low."
                if differentials
                else "There is no standout low-owned differential worth forcing this week."
            ),
        },
    ]

    spotlight_players = [
        item
        for item in [
            captain,
            flagged_player,
            differentials[0] if differentials else None,
        ]
        if item
    ]
    seen_ids: set[int] = set()
    player_explanations: list[dict[str, Any]] = []
    for player in spotlight_players:
        player_id = _to_int(player.get("id"), 0)
        if player_id <= 0 or player_id in seen_ids:
            continue
        seen_ids.add(player_id)
        focus = "captain" if player_id == _to_int(captain.get("id"), 0) else "general"
        explanation = explain_player(player, focus=focus)
        player_explanations.append(
            {
                "player_id": player_id,
                "name": str(player.get("name") or "Player"),
                "team": str(player.get("team_name") or ""),
                "role": (
                    "Captain favourite"
                    if player_id == _to_int(captain.get("id"), 0)
                    else "Squad warning"
                    if player_id == _to_int((flagged_player or {}).get("id"), 0)
                    else "Differential"
                ),
                "summary": explanation["summary"],
                "confidence": explanation["confidence"]["label"],
                "risk": explanation["risk"]["label"],
                "reasons": explanation["reasons"],
            }
        )

    return {
        "headline": str((report or {}).get("headline") or "TACTIX AI intelligence"),
        "cards": cards,
        "recommendations": recommendations,
        "player_explanations": player_explanations,
        "memory": memory
        or {"summary": "Decision history will deepen as more moves are tracked."},
    }


def generate_gameweek_report(
    *,
    user_name: str,
    current_gameweek: int,
    analysis_data: dict[str, Any],
    squad: list[dict[str, Any]],
    all_players: list[dict[str, Any]],
    last_gameweek_context: dict[str, Any] | None = None,
    rival_context: dict[str, Any] | None = None,
    memory_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    last_ctx = last_gameweek_context if isinstance(last_gameweek_context, dict) else {}
    memory = memory_context if isinstance(memory_context, dict) else {}
    rival = rival_analysis(analysis_data, squad, all_players, rival_context)
    captain_brief = build_captain_brief(
        analysis_data, squad, memory=memory, rival=rival
    )
    transfer_brief = build_transfer_brief(analysis_data, memory=memory)
    lineup = ((analysis_data or {}).get("lineup") or {}).get("starting") or squad
    breakdown = _position_breakdown(lineup)
    strongest_line = breakdown[0] if breakdown else None
    weakest_line = breakdown[-1] if breakdown else None
    weak_spot = ((analysis_data or {}).get("sell_recommendations") or [None])[0]

    last_gw = _to_int(last_ctx.get("gameweek"), 0)
    last_score = _to_float(last_ctx.get("user_score"), 0.0)
    average_score = _to_float(last_ctx.get("average_score"), 0.0)
    score_delta = round(last_score - average_score, 1)
    captain_name = str(last_ctx.get("captain_name") or "your captain")
    captain_points = _to_float(last_ctx.get("captain_points"), 0.0)
    bench_points = _to_float(last_ctx.get("bench_points"), 0.0)
    rank_change = _to_int(last_ctx.get("rank_change"), 0)
    best_unit = str(
        last_ctx.get("best_unit") or strongest_line.get("label")
        if strongest_line
        else "midfield"
    )
    weak_unit = str(
        last_ctx.get("weak_unit") or weakest_line.get("label")
        if weakest_line
        else "defence"
    )

    if last_gw > 0:
        if score_delta >= 8:
            score_read = f"well above average ({average_score:.0f})"
        elif score_delta >= 0:
            score_read = f"slightly above average ({average_score:.0f})"
        elif score_delta <= -8:
            score_read = f"well below average ({average_score:.0f})"
        else:
            score_read = f"just under average ({average_score:.0f})"

        if captain_points >= 10:
            captain_line = f"Your captain choice paid off with {captain_points:.0f} points from {captain_name}."
        elif captain_points >= 6:
            captain_line = f"{captain_name} was a steady captain return, but it was not a massive edge."
        else:
            captain_line = f"Captaincy hurt a bit because {captain_name} only returned {captain_points:.0f} points."

        bench_line = (
            f"You left {bench_points:.0f} points on the bench, so lineup discipline mattered."
            if bench_points >= 8
            else "The bench did not cost you much, so the bigger swings came from your starters."
        )
        rank_line = (
            f"Your overall rank improved by roughly {abs(rank_change):,} places."
            if rank_change > 0
            else f"Your rank slipped by about {abs(rank_change):,} places."
            if rank_change < 0
            else "Rank movement was basically flat."
        )
        last_summary = (
            f"You scored {last_score:.0f} points in GW{last_gw}, {score_read}. "
            f"{captain_line} {best_unit} carried more of the load, while {weak_unit.lower()} dragged the score down."
        )
        last_bullets = [captain_line, bench_line, rank_line]
    else:
        last_summary = "Connect your official FPL entry to unlock score-vs-average analysis, captain reviews, and true post-gameweek coaching."
        last_bullets = [
            "TACTIX can already coach the next move from your live squad.",
            "Add your FPL entry ID to unlock historical debriefs.",
        ]

    current_summary = (
        f"This week your {strongest_line['label'].lower()} look strongest"
        if strongest_line
        else "This week your squad has a few usable strengths"
    )
    if strongest_line:
        current_summary += f", averaging {strongest_line['projected_avg']:.1f} projected points per slot."
    else:
        current_summary += "."
    if weak_spot:
        current_summary += f" The main weak point is {weak_spot.get('name')}, who is carrying more downside than the rest of your core."
    elif weakest_line:
        current_summary += f" The softest line is your {weakest_line['label'].lower()}, so that is where an upgrade has the most leverage."

    current_bullets = [
        captain_brief["summary"],
        transfer_brief["summary"],
        rival["summary"],
    ]

    use_transfer = _to_float(transfer_brief.get("expected_gain"), 0.0) >= max(
        _to_float(captain_brief.get("expected_gain"), 0.0), 1.25
    )
    action_payload = transfer_brief if use_transfer else captain_brief
    expected_gain = _to_float(action_payload.get("expected_gain"), 0.0)
    expected_window = "next 2 gameweeks" if use_transfer else "this gameweek"
    recommendation_summary = f"Recommendation: {action_payload.get('decision')} Expected gain: +{expected_gain:.1f} points over the {expected_window}."
    if memory.get("summary"):
        recommendation_summary += f" {memory['summary']}"

    headline = f"{user_name or 'Manager'}, the next edge is clear: {action_payload.get('decision')}"
    coach_message = " ".join(
        part
        for part in [
            last_summary,
            current_summary,
            recommendation_summary,
            rival.get("recommended_response", ""),
        ]
        if part
    )

    return {
        "headline": headline,
        "coach_message": coach_message,
        "last_gameweek": {
            "title": "Last Gameweek Analysis",
            "summary": last_summary,
            "details": last_bullets,
            "gameweek": last_gw,
        },
        "current_gameweek": {
            "title": f"Current Gameweek Analysis - GW{current_gameweek}",
            "summary": current_summary,
            "details": current_bullets,
            "strongest_area": strongest_line,
            "weakest_area": weakest_line,
        },
        "recommendation": {
            "title": "Clear Recommendation",
            "action_type": "transfer" if use_transfer else "captain",
            "decision": action_payload.get("decision"),
            "summary": recommendation_summary,
            "expected_gain": round(expected_gain, 2),
            "expected_window": expected_window,
            "confidence": action_payload.get("confidence", "Medium"),
            "risk": action_payload.get("risk", "Medium"),
            "reasons": action_payload.get("reasons", []),
            "why": action_payload.get("why", action_payload.get("summary", "")),
            "memory_note": action_payload.get("memory_note", ""),
        },
        "rival": rival,
        "memory": memory,
        "captain": captain_brief,
        "transfer": transfer_brief,
    }
