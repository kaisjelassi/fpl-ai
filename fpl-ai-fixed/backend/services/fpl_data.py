import requests
import json
import random
import math
from datetime import datetime

FPL_BASE = "https://fantasy.premierleague.com/api"

TEAM_NAMES = {
    1: "Arsenal", 2: "Aston Villa", 3: "Bournemouth", 4: "Brentford",
    5: "Brighton", 6: "Chelsea", 7: "Crystal Palace", 8: "Everton",
    9: "Fulham", 10: "Ipswich", 11: "Leicester", 12: "Liverpool",
    13: "Man City", 14: "Man Utd", 15: "Newcastle", 16: "Nott'm Forest",
    17: "Southampton", 18: "Spurs", 19: "West Ham", 20: "Wolves"
}

TEAM_SHORT = {
    1: "ARS", 2: "AVL", 3: "BOU", 4: "BRE", 5: "BHA", 6: "CHE",
    7: "CRY", 8: "EVE", 9: "FUL", 10: "IPS", 11: "LEI", 12: "LIV",
    13: "MCI", 14: "MUN", 15: "NEW", 16: "NFO", 17: "SOU", 18: "TOT",
    19: "WHU", 20: "WOL"
}

POSITION_MAP = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

_cache = {}

def fetch_bootstrap():
    if 'bootstrap' in _cache:
        return _cache['bootstrap']
    try:
        r = requests.get(f"{FPL_BASE}/bootstrap-static/", timeout=10)
        data = r.json()
        _cache['bootstrap'] = data
        return data
    except:
        return None

def fetch_fixtures():
    if 'fixtures' in _cache:
        return _cache['fixtures']
    try:
        r = requests.get(f"{FPL_BASE}/fixtures/", timeout=10)
        data = r.json()
        _cache['fixtures'] = data
        return data
    except:
        return []

def get_current_gw(events):
    for e in events:
        if e.get('is_current'):
            return e['id']
    for e in events:
        if e.get('is_next'):
            return e['id']
    return 1

def compute_ai_score(player, fixtures_map):
    form = float(player.get('form', 0) or 0)
    pts = float(player.get('total_points', 0) or 0)
    xg = float(player.get('expected_goals', 0) or 0)
    xa = float(player.get('expected_assists', 0) or 0)
    ep_next = float(player.get('ep_next', 0) or 0)
    selected_pct = float(player.get('selected_by_percent', 0) or 0)
    minutes = int(player.get('minutes', 0) or 0)
    cost = int(player.get('now_cost', 0)) / 10
    chance = player.get('chance_of_playing_next_round')

    mins_score = min(minutes / 3000, 1.0)
    form_score = min(form / 10, 1.0)
    xg_score = min((xg + xa) / 15, 1.0)
    pts_score = min(pts / 200, 1.0)
    ep_score = min(ep_next / 12, 1.0)
    diff_score = 1 - min(selected_pct / 60, 1.0)

    ai = (form_score * 0.25 + xg_score * 0.20 + pts_score * 0.20 +
          ep_score * 0.20 + mins_score * 0.10 + diff_score * 0.05)

    if chance == 0:
        ai *= 0.2
    elif chance == 25:
        ai *= 0.6
    elif chance == 50:
        ai *= 0.8
    elif chance == 75:
        ai *= 0.9

    return round(ai * 100, 1)

def get_fixture_difficulty(team_id, fixtures, current_gw, next_n=5):
    upcoming = []
    for f in fixtures:
        if f.get('event') and f['event'] >= current_gw and not f.get('finished'):
            if f['team_h'] == team_id:
                upcoming.append(f['team_h_difficulty'])
            elif f['team_a'] == team_id:
                upcoming.append(f['team_a_difficulty'])
            if len(upcoming) >= next_n:
                break
    if not upcoming:
        return 3, upcoming
    return round(sum(upcoming) / len(upcoming), 1), upcoming

def get_status_badge(player):
    chance = player.get('chance_of_playing_next_round')
    status = player.get('status', 'a')
    if status == 'i':
        return 'injury'
    if status == 's':
        return 'suspended'
    if chance == 0:
        return 'unavailable'
    if chance in [25, 50, 75]:
        return 'doubtful'
    return 'available'

def build_player_data(limit=80):
    data = fetch_bootstrap()
    if not data:
        return []

    events = data.get('events', [])
    current_gw = get_current_gw(events)
    players_raw = data.get('elements', [])
    fixtures = fetch_fixtures()

    players_raw.sort(key=lambda p: float(p.get('total_points', 0)), reverse=True)
    top_players = players_raw[:limit]

    result = []
    for p in top_players:
        team_id = p.get('team', 1)
        pos_id = p.get('element_type', 3)
        cost = int(p.get('now_cost', 0)) / 10
        form = float(p.get('form', 0) or 0)
        ep_next = float(p.get('ep_next', 0) or 0)
        xg = float(p.get('expected_goals', 0) or 0)
        xa = float(p.get('expected_assists', 0) or 0)
        selected = float(p.get('selected_by_percent', 0) or 0)
        minutes = int(p.get('minutes', 0) or 0)
        total_pts = int(p.get('total_points', 0) or 0)
        goals = int(p.get('goals_scored', 0) or 0)
        assists = int(p.get('assists', 0) or 0)
        cs = int(p.get('clean_sheets', 0) or 0)
        cost_change = int(p.get('cost_change_start', 0) or 0)

        avg_diff, next_fixtures = get_fixture_difficulty(team_id, fixtures, current_gw)
        ai_score = compute_ai_score(p, {})
        status = get_status_badge(p)

        # Predicted points next 5 GWs
        predicted_pts = []
        for diff in (next_fixtures[:5] + [3] * 5)[:5]:
            base = ep_next if ep_next > 0 else (form * 1.2)
            modifier = (6 - diff) / 5
            predicted_pts.append(round(base * modifier + random.uniform(-0.5, 0.5), 1))

        # AI recommendation
        if ai_score >= 80:
            recommendation = "STRONG BUY"
            rec_color = "emerald"
        elif ai_score >= 65:
            recommendation = "BUY"
            rec_color = "cyan"
        elif ai_score >= 45:
            recommendation = "HOLD"
            rec_color = "yellow"
        else:
            recommendation = "SELL"
            rec_color = "red"

        # Captain score
        captain_score = round((form * 0.4 + ep_next * 3 + ai_score * 0.3) / 3, 1)

        # Differential score (low ownership + high AI)
        diff_score = round(ai_score * (1 - selected / 100) * 1.5, 1)

        result.append({
            "id": p.get('id'),
            "name": p.get('web_name', 'Unknown'),
            "full_name": f"{p.get('first_name','')} {p.get('second_name','')}".strip(),
            "team": TEAM_NAMES.get(team_id, 'Unknown'),
            "team_short": TEAM_SHORT.get(team_id, 'UNK'),
            "team_id": team_id,
            "position": POSITION_MAP.get(pos_id, 'MID'),
            "pos_id": pos_id,
            "price": cost,
            "form": form,
            "total_points": total_pts,
            "ep_next": ep_next,
            "xg": round(xg, 2),
            "xa": round(xa, 2),
            "selected_pct": selected,
            "minutes": minutes,
            "goals": goals,
            "assists": assists,
            "clean_sheets": cs,
            "ai_score": ai_score,
            "captain_score": min(captain_score, 100),
            "differential_score": min(diff_score, 100),
            "fixture_difficulty": avg_diff,
            "next_fixtures": next_fixtures[:5],
            "predicted_pts": predicted_pts,
            "predicted_total_5gw": round(sum(predicted_pts), 1),
            "status": status,
            "chance_of_playing": p.get('chance_of_playing_next_round'),
            "news": p.get('news', ''),
            "recommendation": recommendation,
            "rec_color": rec_color,
            "price_trend": "up" if cost_change > 0 else ("down" if cost_change < 0 else "stable"),
            "price_change": cost_change / 10,
            "is_captain_pick": captain_score > 70,
            "is_differential": selected < 10 and ai_score > 60,
            "has_double_gw": False,  # Would need more fixture analysis
            "risk_rating": round(100 - ai_score + (avg_diff * 5), 1),
            "value_rating": round(total_pts / max(cost, 1), 1),
            "minutes_security": round(minutes / 3000 * 100, 1),
        })

    return result

def get_injuries():
    data = fetch_bootstrap()
    if not data:
        return []
    players = data.get('elements', [])
    injured = []
    for p in players:
        status = p.get('status', 'a')
        chance = p.get('chance_of_playing_next_round')
        news = p.get('news', '')
        if status in ['i', 's', 'd'] or (chance is not None and chance < 100):
            team_id = p.get('team', 1)
            pos_id = p.get('element_type', 3)
            severity = 'high' if (status == 'i' or chance == 0) else ('medium' if chance in [25, 50] else 'low')
            injured.append({
                "id": p.get('id'),
                "name": p.get('web_name'),
                "team": TEAM_NAMES.get(team_id, 'Unknown'),
                "team_short": TEAM_SHORT.get(team_id, 'UNK'),
                "position": POSITION_MAP.get(pos_id, 'MID'),
                "status": status,
                "chance": chance,
                "news": news,
                "severity": severity,
                "selected_pct": float(p.get('selected_by_percent', 0) or 0),
                "price": int(p.get('now_cost', 0)) / 10,
            })
    return injured

def get_fixtures_data():
    data = fetch_bootstrap()
    fixtures = fetch_fixtures()
    if not data or not fixtures:
        return []

    events = data.get('events', [])
    current_gw = get_current_gw(events)

    result = []
    for f in fixtures:
        gw = f.get('event')
        if gw and gw >= current_gw and gw <= current_gw + 6:
            result.append({
                "gw": gw,
                "home_team": TEAM_NAMES.get(f.get('team_h'), 'Unknown'),
                "away_team": TEAM_NAMES.get(f.get('team_a'), 'Unknown'),
                "home_short": TEAM_SHORT.get(f.get('team_h'), 'UNK'),
                "away_short": TEAM_SHORT.get(f.get('team_a'), 'UNK'),
                "home_difficulty": f.get('team_h_difficulty', 3),
                "away_difficulty": f.get('team_a_difficulty', 3),
                "kickoff": f.get('kickoff_time', ''),
                "finished": f.get('finished', False),
                "home_score": f.get('team_h_score'),
                "away_score": f.get('team_a_score'),
            })
    return result

def get_team_fixture_matrix():
    data = fetch_bootstrap()
    fixtures = fetch_fixtures()
    if not data or not fixtures:
        return {}

    events = data.get('events', [])
    current_gw = get_current_gw(events)
    teams = list(TEAM_NAMES.keys())
    gws = list(range(current_gw, current_gw + 8))

    matrix = {}
    for team_id in teams:
        matrix[TEAM_NAMES[team_id]] = {}
        for gw in gws:
            for f in fixtures:
                if f.get('event') == gw:
                    if f['team_h'] == team_id:
                        matrix[TEAM_NAMES[team_id]][gw] = {
                            "opp": TEAM_SHORT.get(f['team_a'], 'UNK'),
                            "difficulty": f['team_h_difficulty'],
                            "is_home": True
                        }
                        break
                    elif f['team_a'] == team_id:
                        matrix[TEAM_NAMES[team_id]][gw] = {
                            "opp": TEAM_SHORT.get(f['team_h'], 'UNK'),
                            "difficulty": f['team_a_difficulty'],
                            "is_home": False
                        }
                        break
            else:
                matrix[TEAM_NAMES[team_id]][gw] = {"opp": "BGW", "difficulty": 0, "is_home": None}

    return {"matrix": matrix, "gws": gws, "current_gw": current_gw}

def get_stats():
    data = fetch_bootstrap()
    if not data:
        return {}
    events = data.get('events', [])
    current_gw = get_current_gw(events)
    players = data.get('elements', [])
    active = [p for p in players if int(p.get('minutes', 0) or 0) > 0]
    return {
        "current_gw": current_gw,
        "total_players": len(players),
        "active_players": len(active),
        "total_teams": 20,
    }
