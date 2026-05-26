# 🚀 FPL AI Assistant — Real Data Edition

Live Fantasy Premier League platform powered by your real FPL database.

## Quick Start

```bash
cd fpl-ai
pip install flask requests
python app.py
```

Open → http://localhost:5000

## Data

- **fpl.db** — 826 players · 380 fixtures · 20 teams (GW33, 2025/26)
- Synced from the Official FPL API
- AI engine: form · xG · xA · minutes · fixtures · team strength

## API Endpoints

| Endpoint | Description |
|---|---|
| /api/players | All players with AI scores, predictions, risk |
| /api/injuries | Injury tracker with severity + ownership |
| /api/captain-picks | Top captain recommendations |
| /api/differentials | Low-ownership AI picks |
| /api/transfers | Transfer recommendations |
| /api/fixture-matrix | Full GW fixture matrix |
| /api/standings | Premier League table |
| /api/best-fixture-runs | Teams with best upcoming fixtures |
| /api/price-changes | Price risers & fallers |
| /api/top-scorers | Top goal scorers |

## Architecture

```
app.py              ← Flask routes (integrated hub)
backend/
  ai_engine.py      ← 12-signal AI scoring model
  player_api.py     ← Player data + enrichment
  fixtures_api.py   ← Fixtures + difficulty matrix
  stats_api.py      ← Standings + top scorers
  data_sync.py      ← FPL API sync layer
  intelligence_engine.py ← Advanced analytics
  services/         ← Image, avatar, season services
database/
  fpl.db            ← Main SQLite database (real FPL data)
assets/
  player_images/    ← Player photos
  teams/            ← Team badges
```
