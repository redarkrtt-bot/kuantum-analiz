import json
import os
from datetime import datetime, timezone

import requests

API_BASE = "https://v3.football.api-sports.io"
STATE_FILE = "state.json"

api_key = os.environ["API_FOOTBALL_KEY"]
discord_webhook = os.environ["DISCORD_WEBHOOK"]

session = requests.Session()
session.headers.update({"x-apisports-key": api_key})


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"checked": {}, "alerts": {}, "enrichment": {}, "prematch": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def api_get(path, params):
    r = session.get(API_BASE + path, params=params, timeout=25)
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(str(data["errors"]))
    return data.get("response", [])


def num(v):
    try:
        return float(str(v).replace("%", "").replace(",", "."))
    except Exception:
        return 0.0


def pick(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, "", "-"):
            return d[k]
    return None


def fixture_score(f):
    league = f.get("league", {}) or {}
    teams = f.get("teams", {}) or {}
    # Prefer fixtures with both named teams, a known league and venue.
    score = 0
    score += 2 if teams.get("home", {}).get("name") and teams.get("away", {}).get("name") else 0
    score += 1 if league.get("name") else 0
    score += 1 if league.get("country") else 0
    score += 1 if (f.get("fixture", {}).get("venue", {}) or {}).get("name") else 0
    return score


def weather(city):
    if not city:
        return None
    try:
        g = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "en", "format": "json"},
            timeout=10,
        ).json()
        loc = (g.get("results") or [None])[0]
        if not loc:
            return None
        w = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": loc["latitude"],
                "longitude": loc["longitude"],
                "current": "temperature_2m,precipitation,rain,snowfall,wind_speed_10m",
                "timezone": "auto",
            },
            timeout=10,
        ).json().get("current", {})
        return {
            "temperature": w.get("temperature_2m"),
            "rain": w.get("rain"),
            "snow": w.get("snowfall"),
            "wind": w.get("wind_speed_10m"),
        }
    except Exception as e:
        print(f"Hava verisi alınamadı: {e}")
        return None


def prediction_summary(p):
    pred = p.get("predictions", {}) or {}
    comparison = p.get("comparison", {}) or {}
    goals = p.get("goals", {}) or {}

    home = pick(pred, "winner") or {}
    advice = pick(pred, "advice", "win_or_draw", "under_over", "goals") or ""

    percent = {}
    for k, v in (pred.get("percent") or {}).items():
        if v is not None:
            percent[k] = v

    # API-Football prediction responses commonly expose winner,
    # advice, percentages, goals and a team comparison block.
    return {
        "winner": home.get("name") if isinstance(home, dict) else str(home),
        "winner_comment": home.get("comment") if isinstance(home, dict) else "",
        "advice": advice,
        "percent": percent,
        "home_goals": (goals.get("home") or {}),
        "away_goals": (goals.get("away") or {}),
        "comparison": comparison,
        "form": p.get("teams", {}),
        "league": p.get("league", {}),
    }


def send_discord(fixture, summary, wx):
    teams = fixture.get("teams", {}) or {}
    league = fixture.get("league", {}) or {}
    fx = fixture.get("fixture", {}) or {}
    home = teams.get("home", {}).get("name", "?")
    away = teams.get("away", {}).get("name", "?")
    date = fx.get("date", "?")
    venue = (fx.get("venue", {}) or {}).get("name", "Bilinmiyor")
    city = (fx.get("venue", {}) or {}).get("city", "")

    pct = summary["percent"]
    pct_text = " • ".join(f"{k}: {v}" for k, v in pct.items()) if pct else "Yüzde verisi yok"
    hg = summary["home_goals"]
    ag = summary["away_goals"]
    goal_text = f"Ev gol: {hg} | Dep gol: {ag}" if hg or ag else "Gol dağılımı verisi yok"

    weather_text = "Hava verisi yok"
    if wx:
        weather_text = (
            f"{wx.get('temperature','?')}°C • yağış {wx.get('rain','?')}mm • "
            f"rüzgar {wx.get('wind','?')}km/s"
        )

    payload = {
        "username": "Goal Radar",
        "embeds": [{
            "title": "🧠 MAÇ ÖNÜ RADAR",
            "description": (
                f"**{home} – {away}**\n"
                f"🏆 {league.get('name','Bilinmeyen Lig')} / {league.get('country','')}\n"
                f"🕒 {date}\n"
                f"🏟️ {venue} {('• ' + city) if city else ''}\n\n"
                f"🎯 Model kazananı: **{summary['winner'] or 'Belirlenemedi'}** "
                f"{summary['winner_comment']}\n"
                f"📌 Tavsiye/senaryo: **{summary['advice'] or 'Belirlenemedi'}**\n"
                f"📊 Olasılık dağılımı: {pct_text}\n"
                f"⚽ Gol modeli: {goal_text}\n"
                f"🌦️ {weather_text}\n\n"
                "Not: Bu, maç önü model verilerinin özetidir; garanti sonuç değildir."
            ),
            "footer": {"text": "Goal Radar • maç önü + canlı doğrulama sistemi"},
        }]
    }
    r = requests.post(discord_webhook, json=payload, timeout=15)
    r.raise_for_status()


def main():
    state = load_state()
    # One fixture-list call + one prediction call per run. This keeps the
    # free 100-request/day API-Football budget compatible with the 30-min live radar.
    fixtures = api_get("/fixtures", {"next": 20})
    fixtures = [
        f for f in fixtures
        if (f.get("fixture", {}).get("status", {}) or {}).get("short") in {"NS", "TBD"}
    ]
    if not fixtures:
        print("Yaklaşan uygun maç bulunamadı.")
        return

    fixtures.sort(key=fixture_score, reverse=True)
    fixture = fixtures[0]
    fid = str(fixture["fixture"]["id"])

    # Avoid repeating the same fixture more than once per day.
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("prematch", {}).get(fid) == today:
        print(f"Bu maç bugün zaten raporlandı: {fid}")
        return

    rows = api_get("/predictions", {"fixture": fid})
    if not rows:
        print(f"Tahmin verisi yok: {fid}")
        return

    summary = prediction_summary(rows[0])
    city = (fixture.get("fixture", {}).get("venue", {}) or {}).get("city") or ""
    wx = weather(city)

    send_discord(fixture, summary, wx)
    state.setdefault("prematch", {})[fid] = today
    save_state(state)
    print(f"Maç önü raporu gönderildi: {fid}")


if __name__ == "__main__":
    main()
