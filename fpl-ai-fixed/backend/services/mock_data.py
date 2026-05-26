import random
import math

random.seed(42)

TEAMS = [
    (1,"Arsenal","ARS"), (2,"Aston Villa","AVL"), (3,"Bournemouth","BOU"),
    (4,"Brentford","BRE"), (5,"Brighton","BHA"), (6,"Chelsea","CHE"),
    (7,"Crystal Palace","CRY"), (8,"Everton","EVE"), (9,"Fulham","FUL"),
    (10,"Ipswich","IPS"), (11,"Leicester","LEI"), (12,"Liverpool","LIV"),
    (13,"Man City","MCI"), (14,"Man Utd","MUN"), (15,"Newcastle","NEW"),
    (16,"Nott'm Forest","NFO"), (17,"Southampton","SOU"), (18,"Spurs","TOT"),
    (19,"West Ham","WHU"), (20,"Wolves","WOL")
]

PLAYERS_DATA = [
    # GKPs
    {"name":"Flekken","team":4,"pos":"GKP","price":4.8,"pts":84,"form":4.8,"xg":0,"xa":0,"sel":9.2,"mins":2430,"goals":0,"assists":0,"cs":10},
    {"name":"Raya","team":1,"pos":"GKP","price":5.8,"pts":131,"form":6.2,"xg":0,"xa":0,"sel":24.1,"mins":2880,"goals":0,"assists":0,"cs":17},
    {"name":"Flaherty","team":9,"pos":"GKP","price":4.5,"pts":75,"form":4.2,"xg":0,"xa":0,"sel":5.4,"mins":2160,"goals":0,"assists":0,"cs":8},
    {"name":"Verbruggen","team":5,"pos":"GKP","price":5.0,"pts":92,"form":5.1,"xg":0,"xa":0,"sel":11.2,"mins":2520,"goals":0,"assists":0,"cs":11},
    {"name":"Alisson","team":12,"pos":"GKP","price":5.8,"pts":118,"form":5.9,"xg":0,"xa":0,"sel":18.4,"mins":2700,"goals":0,"assists":0,"cs":14},
    # DEFs
    {"name":"Alexander-Arnold","team":12,"pos":"DEF","price":7.2,"pts":148,"form":7.4,"xg":2.8,"xa":7.4,"sel":33.2,"mins":2700,"goals":4,"assists":10,"cs":14},
    {"name":"Pedro Porro","team":18,"pos":"DEF","price":5.8,"pts":122,"form":6.1,"xg":2.1,"xa":4.2,"sel":16.4,"mins":2610,"goals":3,"assists":6,"cs":8},
    {"name":"Trent","team":12,"pos":"DEF","price":7.4,"pts":152,"form":7.8,"xg":3.1,"xa":8.1,"sel":35.4,"mins":2790,"goals":5,"assists":11,"cs":14},
    {"name":"Mykolenko","team":8,"pos":"DEF","price":4.5,"pts":68,"form":3.8,"xg":0.4,"xa":1.2,"sel":4.1,"mins":2250,"goals":1,"assists":2,"cs":5},
    {"name":"Pedro","team":15,"pos":"DEF","price":4.8,"pts":91,"form":5.0,"xg":0.8,"xa":2.1,"sel":8.4,"mins":2340,"goals":2,"assists":3,"cs":9},
    {"name":"Gvardiol","team":13,"pos":"DEF","price":6.8,"pts":124,"form":6.4,"xg":3.8,"xa":3.2,"sel":22.1,"mins":2520,"goals":5,"assists":4,"cs":11},
    {"name":"Trippier","team":15,"pos":"DEF","price":6.2,"pts":104,"form":5.6,"xg":1.4,"xa":5.4,"sel":14.2,"mins":2160,"goals":2,"assists":7,"cs":8},
    {"name":"Saliba","team":1,"pos":"DEF","price":5.8,"pts":118,"form":6.0,"xg":0.8,"xa":1.2,"sel":19.4,"mins":2790,"goals":1,"assists":2,"cs":17},
    {"name":"Kerkez","team":3,"pos":"DEF","price":5.0,"pts":88,"form":4.9,"xg":0.9,"xa":3.1,"sel":7.8,"mins":2430,"goals":1,"assists":4,"cs":7},
    {"name":"Gomez","team":12,"pos":"DEF","price":4.8,"pts":96,"form":5.2,"xg":0.4,"xa":1.1,"sel":9.1,"mins":2340,"goals":1,"assists":2,"cs":13},
    # MIDs
    {"name":"Salah","team":12,"pos":"MID","price":13.0,"pts":214,"form":10.8,"xg":18.2,"xa":12.4,"sel":68.4,"mins":2790,"goals":21,"assists":14,"cs":0},
    {"name":"Palmer","team":6,"pos":"MID","price":11.4,"pts":198,"form":9.8,"xg":14.2,"xa":11.8,"sel":58.1,"mins":2610,"goals":18,"assists":13,"cs":0},
    {"name":"Saka","team":1,"pos":"MID","price":10.1,"pts":172,"form":8.4,"xg":12.1,"xa":9.4,"sel":42.4,"mins":2700,"goals":14,"assists":11,"cs":0},
    {"name":"Mbeumo","team":4,"pos":"MID","price":8.4,"pts":164,"form":8.1,"xg":14.8,"xa":6.2,"sel":28.2,"mins":2610,"goals":17,"assists":8,"cs":0},
    {"name":"Isak","team":15,"pos":"FWD","price":8.9,"pts":154,"form":7.8,"xg":16.1,"xa":3.4,"sel":22.4,"mins":2430,"goals":18,"assists":4,"cs":0},
    {"name":"Haaland","team":13,"pos":"FWD","price":14.2,"pts":196,"form":9.8,"xg":22.4,"xa":4.1,"sel":55.4,"mins":2700,"goals":25,"assists":5,"cs":0},
    {"name":"Watkins","team":2,"pos":"FWD","price":9.2,"pts":142,"form":7.1,"xg":12.8,"xa":5.2,"sel":18.4,"mins":2520,"goals":14,"assists":6,"cs":0},
    {"name":"Wood","team":16,"pos":"FWD","price":6.2,"pts":138,"form":6.9,"xg":13.4,"xa":2.1,"sel":11.2,"mins":2430,"goals":15,"assists":2,"cs":0},
    {"name":"Diaby","team":2,"pos":"MID","price":6.4,"pts":88,"form":4.4,"xg":4.2,"xa":3.8,"sel":6.2,"mins":1980,"goals":5,"assists":5,"cs":0},
    {"name":"Andreas","team":9,"pos":"MID","price":5.8,"pts":104,"form":5.2,"xg":3.1,"xa":4.8,"sel":7.4,"mins":2520,"goals":4,"assists":6,"cs":0},
    {"name":"B.Fernandes","team":14,"pos":"MID","price":8.4,"pts":128,"form":6.4,"xg":8.4,"xa":7.2,"sel":16.8,"mins":2610,"goals":10,"assists":9,"cs":0},
    {"name":"Son","team":18,"pos":"MID","price":9.8,"pts":148,"form":7.4,"xg":10.4,"xa":8.1,"sel":24.1,"mins":2520,"goals":12,"assists":10,"cs":0},
    {"name":"Eze","team":7,"pos":"MID","price":7.1,"pts":112,"form":5.6,"xg":7.8,"xa":5.4,"sel":10.4,"mins":2340,"goals":9,"assists":6,"cs":0},
    {"name":"Rashford","team":14,"pos":"MID","price":6.8,"pts":72,"form":3.6,"xg":4.8,"xa":3.2,"sel":5.4,"mins":1800,"goals":6,"assists":4,"cs":0},
    {"name":"Martinelli","team":1,"pos":"MID","price":7.8,"pts":108,"form":5.4,"xg":7.4,"xa":4.8,"sel":11.8,"mins":2340,"goals":9,"assists":6,"cs":0},
    {"name":"Bowen","team":19,"pos":"MID","price":7.2,"pts":98,"form":4.9,"xg":6.4,"xa":5.8,"sel":9.4,"mins":2160,"goals":8,"assists":7,"cs":0},
    {"name":"Adama","team":20,"pos":"MID","price":5.2,"pts":72,"form":3.6,"xg":3.1,"xa":4.2,"sel":4.2,"mins":1980,"goals":4,"assists":5,"cs":0},
    {"name":"M.Salisu","team":17,"pos":"DEF","price":4.2,"pts":42,"form":2.1,"xg":0.2,"xa":0.4,"sel":1.8,"mins":1620,"goals":0,"assists":0,"cs":3},
    {"name":"Gordon","team":15,"pos":"MID","price":7.8,"pts":118,"form":5.9,"xg":8.1,"xa":6.4,"sel":12.4,"mins":2340,"goals":10,"assists":8,"cs":0},
    {"name":"Muniz","team":9,"pos":"FWD","price":6.4,"pts":102,"form":5.1,"xg":10.4,"xa":2.1,"sel":7.8,"mins":2250,"goals":12,"assists":2,"cs":0},
    {"name":"Zirkzee","team":14,"pos":"FWD","price":6.8,"pts":78,"form":3.9,"xg":6.4,"xa":3.2,"sel":5.4,"mins":2070,"goals":7,"assists":4,"cs":0},
    {"name":"Neto","team":3,"pos":"FWD","price":5.8,"pts":94,"form":4.7,"xg":8.2,"xa":1.8,"sel":5.2,"mins":2160,"goals":9,"assists":2,"cs":0},
    {"name":"Delap","team":10,"pos":"FWD","price":5.2,"pts":72,"form":3.6,"xg":5.8,"xa":1.2,"sel":4.1,"mins":1980,"goals":7,"assists":1,"cs":0},
    {"name":"Wissa","team":4,"pos":"FWD","price":6.8,"pts":118,"form":5.9,"xg":9.8,"xa":3.4,"sel":8.4,"mins":2340,"goals":11,"assists":4,"cs":0},
    {"name":"Cunha","team":20,"pos":"MID","price":7.2,"pts":112,"form":5.6,"xg":7.2,"xa":5.8,"sel":9.2,"mins":2250,"goals":9,"assists":7,"cs":0},
]

INJURIES_DATA = [
    {"name":"Rodri","team":13,"pos":"MID","chance":0,"news":"Serious knee injury - season over","severity":"high","sel":22.4,"price":6.2},
    {"name":"Reece James","team":6,"pos":"DEF","chance":0,"news":"Hamstring - 6-8 weeks","severity":"high","sel":8.4,"price":5.8},
    {"name":"Trossard","team":1,"pos":"MID","chance":25,"news":"Muscle strain - doubt for next GW","severity":"high","sel":6.2,"price":7.1},
    {"name":"Pedro Neto","team":6,"pos":"MID","chance":50,"news":"Knock - being assessed","severity":"medium","sel":4.8,"price":6.4},
    {"name":"Diogo Jota","team":12,"pos":"FWD","chance":25,"news":"Hamstring - doubt","severity":"high","sel":11.4,"price":7.8},
    {"name":"Odegaard","team":1,"pos":"MID","chance":0,"news":"Ankle ligament - 4-6 weeks","severity":"high","sel":18.4,"price":8.8},
    {"name":"Maddison","team":18,"pos":"MID","chance":50,"news":"Ankle - 50% chance","severity":"medium","sel":8.2,"price":7.4},
    {"name":"Gabriel","team":1,"pos":"DEF","chance":75,"news":"Minor knock - likely fit","severity":"low","sel":16.4,"price":6.2},
    {"name":"Rashford","team":14,"pos":"MID","chance":75,"news":"Illness - expected to return","severity":"low","sel":5.4,"price":6.8},
    {"name":"Vardy","team":11,"pos":"FWD","chance":0,"news":"Achilles - long term","severity":"high","sel":2.1,"price":5.2},
]

FIXTURE_DIFFICULTIES = {
    1: [2,2,3,4,1,2,3,2], 2: [3,4,2,2,3,4,1,3], 3: [2,1,3,2,4,2,3,1],
    4: [2,3,1,3,2,2,4,3], 5: [3,2,4,1,2,3,2,4], 6: [4,3,2,3,1,4,2,3],
    7: [3,2,3,1,4,2,3,2], 8: [1,2,3,2,3,1,4,2], 9: [2,3,1,4,2,3,1,3],
    10: [1,2,2,3,1,2,3,1], 11: [2,1,3,2,1,3,2,1], 12: [4,5,3,4,5,3,4,5],
    13: [5,4,5,3,4,5,3,4], 14: [3,2,4,3,2,3,4,2], 15: [3,4,2,3,4,2,3,4],
    16: [2,3,1,2,3,1,2,3], 17: [1,1,2,1,2,1,1,2], 18: [4,3,4,2,3,4,2,3],
    19: [2,3,2,4,1,3,2,3], 20: [2,2,3,1,3,2,1,2]
}

def get_mock_players(limit=80):
    result = []
    for i, p in enumerate(PLAYERS_DATA[:limit]):
        team_data = next((t for t in TEAMS if t[0] == p["team"]), TEAMS[0])
        team_id = team_data[0]

        form = p["form"]
        ep_next = round(form * random.uniform(0.8, 1.2), 1)
        selected = p["sel"]
        xg = p["xg"]
        xa = p["xa"]
        mins = p["mins"]
        total_pts = p["pts"]
        cost = p["price"]
        goals = p["goals"]
        assists = p["assists"]

        # AI score
        form_s = min(form / 10, 1)
        xg_s = min((xg + xa) / 15, 1)
        pts_s = min(total_pts / 220, 1)
        ep_s = min(ep_next / 12, 1)
        mins_s = min(mins / 3000, 1)
        diff_s = 1 - min(selected / 60, 1)
        ai_score = round((form_s*0.25 + xg_s*0.20 + pts_s*0.20 + ep_s*0.20 + mins_s*0.10 + diff_s*0.05) * 100, 1)

        # Fixtures
        diffs = FIXTURE_DIFFICULTIES.get(team_id, [3]*8)
        next_5 = diffs[:5]
        avg_diff = round(sum(next_5)/len(next_5), 1)

        # Predicted pts
        pred = []
        for d in next_5:
            base = ep_next if ep_next > 0 else form * 0.9
            mod = (6 - d) / 5
            pred.append(round(base * mod + random.uniform(-0.3, 0.3), 1))

        rec = "STRONG BUY" if ai_score >= 80 else "BUY" if ai_score >= 65 else "HOLD" if ai_score >= 45 else "SELL"
        rec_col = "emerald" if ai_score >= 80 else "cyan" if ai_score >= 65 else "yellow" if ai_score >= 45 else "red"
        cap_score = min(round((form*0.4 + ep_next*3 + ai_score*0.3)/3, 1), 100)
        diff_score = min(round(ai_score * (1 - selected/100) * 1.5, 1), 100)
        price_change = random.choice([-1, -1, 0, 0, 0, 0, 1, 1, 2])
        trend = "up" if price_change > 0 else "down" if price_change < 0 else "stable"

        result.append({
            "id": i+1,
            "name": p["name"],
            "full_name": p["name"],
            "team": team_data[1],
            "team_short": team_data[2],
            "team_id": team_id,
            "position": p["pos"],
            "pos_id": {"GKP":1,"DEF":2,"MID":3,"FWD":4}.get(p["pos"],3),
            "price": cost,
            "form": form,
            "total_points": total_pts,
            "ep_next": ep_next,
            "xg": round(xg, 2),
            "xa": round(xa, 2),
            "selected_pct": selected,
            "minutes": mins,
            "goals": goals,
            "assists": assists,
            "clean_sheets": random.randint(0,14),
            "ai_score": ai_score,
            "captain_score": cap_score,
            "differential_score": diff_score,
            "fixture_difficulty": avg_diff,
            "next_fixtures": next_5,
            "predicted_pts": pred,
            "predicted_total_5gw": round(sum(pred), 1),
            "status": "available",
            "chance_of_playing": 100,
            "news": "",
            "recommendation": rec,
            "rec_color": rec_col,
            "price_trend": trend,
            "price_change": price_change / 10,
            "is_captain_pick": cap_score > 70,
            "is_differential": selected < 15 and ai_score > 55,
            "has_double_gw": False,
            "risk_rating": round(100 - ai_score + avg_diff * 4, 1),
            "value_rating": round(total_pts / max(cost, 1), 1),
            "minutes_security": round(mins / 3000 * 100, 1),
        })

    result.sort(key=lambda p: p["ai_score"], reverse=True)
    return result

def get_mock_injuries():
    result = []
    for p in INJURIES_DATA:
        team_data = next((t for t in TEAMS if t[0] == p["team"]), TEAMS[0])
        result.append({
            "id": hash(p["name"]),
            "name": p["name"],
            "team": team_data[1],
            "team_short": team_data[2],
            "position": p["pos"],
            "status": "i" if p["chance"] == 0 else "d",
            "chance": p["chance"],
            "news": p["news"],
            "severity": p["severity"],
            "selected_pct": p["sel"],
            "price": p["price"],
        })
    return result

def get_mock_fixture_matrix():
    from backend.services.fpl_data import TEAM_NAMES, TEAM_SHORT
    gws = list(range(30, 38))
    matrix = {}
    for team_id, team_name in TEAM_NAMES.items():
        matrix[team_name] = {}
        diffs = FIXTURE_DIFFICULTIES.get(team_id, [3]*8)
        opponents = [t for t in list(TEAM_NAMES.keys()) if t != team_id]
        random.shuffle(opponents)
        for i, gw in enumerate(gws):
            if i < len(diffs):
                opp_id = opponents[i % len(opponents)]
                matrix[team_name][gw] = {
                    "opp": TEAM_SHORT.get(opp_id, "UNK"),
                    "difficulty": diffs[i],
                    "is_home": random.choice([True, False])
                }
            else:
                matrix[team_name][gw] = {"opp": "BGW", "difficulty": 0, "is_home": None}
    return {"matrix": matrix, "gws": gws, "current_gw": 30}

def get_mock_stats():
    return {
        "current_gw": 30,
        "total_players": 775,
        "active_players": 492,
        "total_teams": 20,
    }
