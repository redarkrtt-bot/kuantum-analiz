import json
import os
from datetime import datetime, timezone

import requests

API_BASE = "https://v3.football.api-sports.io"
STATE_FILE = "state.json"
MINUTE_MIN = 8
MINUTE_MAX = 88
MAX_TOTAL_GOALS = 3

api_key = os.environ["API_FOOTBALL_KEY"]
discord_webhook = os.environ["DISCORD_WEBHOOK"]

session = requests.Session()
session.headers.update({"x-apisports-key": api_key})


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"checked": {}, "alerts": {}}


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


def parse_minute(fixture):
    elapsed = (fixture.get("fixture", {}).get("status", {}) or {}).get("elapsed")
    try:
        return int(elapsed or 0)
    except Exception:
        return 0


def total_goals(fixture):
    goals = fixture.get("goals", {}) or {}
    return int(goals.get("home") or 0) + int(goals.get("away") or 0)


def candidate_score(fixture, state):
    fid = str(fixture["fixture"]["id"])
    minute = parse_minute(fixture)
    last = state.get("checked", {}).get(fid)
    age_bonus = 999 if last is None else 0
    # Prefer matches not checked recently and those in the core 20-75 minute zone.
    core = 20 if 20 <= minute <= 75 else 0
    return age_bonus + core - (minute / 1000)


def extract_stats(stats_response):
    out = {}
    for team_block in stats_response:
        team = team_block.get("team", {}) or {}
        tid = team.get("id")
        if not tid:
            continue
        values = {}
        for item in team_block.get("statistics", []) or []:
            name = item.get("type")
            value = item.get("value")
            if name:
                values[name] = value
        out[str(tid)] = values
    return out


def num(value):
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace("%", "").replace(",", ".").strip()
    try:
        return float(s)
    except Exception:
        return 0.0


def analyze(fixture, stats_response):
    home = fixture["teams"]["home"]
    away = fixture["teams"]["away"]
    score = fixture.get("goals", {}) or {}
    home_goals = int(score.get("home") or 0)
    away_goals = int(score.get("away") or 0)
    minute = parse_minute(fixture)

    stats = extract_stats(stats_response)
    hs = stats.get(str(home["id"]), {})
    as_ = stats.get(str(away["id"]), {})

    h_att = num(hs.get("Dangerous Attacks"))
    a_att = num(as_.get("Dangerous Attacks"))
    h_shots = num(hs.get("Total Shots"))
    a_shots = num(as_.get("Total Shots"))
    h_sot = num(hs.get("Shots on Goal"))
    a_sot = num(as_.get("Shots on Goal"))
    h_pos = num(hs.get("Ball Possession"))
    a_pos = num(as_.get("Ball Possession"))
    h_corners = num(hs.get("Corner Kicks"))
    a_corners = num(as_.get("Corner Kicks"))

    pressure_home = h_att * 0.30 + h_shots * 1.5 + h_sot * 3 + h_corners * 1.0 + h_pos * 0.05
    pressure_away = a_att * 0.30 + a_shots * 1.5 + a_sot * 3 + a_corners * 1.0 + a_pos * 0.05

    # A conservative signal: multiple attacking indicators must agree.
    home_score = (
        (h_sot >= 3) * 2
        + (h_shots >= 7) * 1
        + (h_att >= 20) * 1
        + (h_corners >= 3) * 1
        + (h_pos >= 52) * 0.5
    )
    away_score = (
        (a_sot >= 3) * 2
        + (a_shots >= 7) * 1
        + (a_att >= 20) * 1
        + (a_corners >= 3) * 1
        + (a_pos >= 52) * 0.5
    )

    if home_score >= 4 and home_score >= away_score + 1:
        direction = f"{home['name']} gol baskısı"
        strength = "GÜÇLÜ"
        signal = True
    elif away_score >= 4 and away_score >= home_score + 1:
        direction = f"{away['name']} gol baskısı"
        strength = "GÜÇLÜ"
        signal = True
    elif max(home_score, away_score) >= 3.5 and abs(home_score - away_score) >= 0.5:
        direction = f"{home['name']} / {away['name']} baskı avantajı"
        strength = "ORTA"
        signal = False
    else:
        direction = "Belirgin gol baskısı yok"
        strength = "ZAYIF"
        signal = False

    return {
        "signal": signal,
        "strength": strength,
        "direction": direction,
        "minute": minute,
        "score": f"{home_goals}-{away_goals}",
        "home": home["name"],
        "away": away["name"],
        "home_score": home_score,
        "away_score": away_score,
        "pressure_home": pressure_home,
        "pressure_away": pressure_away,
        "stats_present": bool(stats),
    }


def send_discord(result):
    if not result["signal"]:
        return

    payload = {
        "username": "Goal Radar",
        "embeds": [{
            "title": "🚨 GOL SİNYALİ",
            "description": f"**{result['home']} – {result['away']}**\n"
                           f"⏱️ {result['minute']}' | ⚽ {result['score']}\n"
                           f"🎯 {result['direction']}\n"
                           f"🔥 Sinyal: **{result['strength']}**",
            "footer": {"text": "API-Football canlı istatistikleri • Otomatik tarama"},
        }]
    }
    r = requests.post(discord_webhook, json=payload, timeout=15)
    r.raise_for_status()


def main():
    state = load_state()

    live = api_get("/fixtures", {"live": "all"})
    candidates = [
        f for f in live
        if MINUTE_MIN <= parse_minute(f) <= MINUTE_MAX
        and total_goals(f) <= MAX_TOTAL_GOALS
        and (f.get("fixture", {}).get("status", {}) or {}).get("short") in {"1H", "2H"}
    ]

    if not candidates:
        print("Canlı uygun maç yok.")
        save_state(state)
        return

    candidates.sort(key=lambda f: candidate_score(f, state), reverse=True)
    fixture = candidates[0]
    fid = str(fixture["fixture"]["id"])

    stats_response = api_get("/fixtures/statistics", {"fixture": fid})
    state.setdefault("checked", {})[fid] = datetime.now(timezone.utc).isoformat()

    if not stats_response:
        print(f"İstatistik yok, atlandı: {fid}")
        save_state(state)
        return

    result = analyze(fixture, stats_response)
    print(json.dumps(result, ensure_ascii=False))

    if result["signal"]:
        last_alert = state.setdefault("alerts", {}).get(fid)
        # Aynı maç için 30 dakika içinde ikinci kez alarm gönderme.
        send_again = True
        if last_alert:
            try:
                last_dt = datetime.fromisoformat(last_alert)
                send_again = (datetime.now(timezone.utc) - last_dt).total_seconds() >= 1800
            except Exception:
                pass
        if send_again:
            send_discord(result)
            state["alerts"][fid] = datetime.now(timezone.utc).isoformat()
            print("Discord alarmı gönderildi.")
        else:
            print("Aynı maç için son alarm 30 dakikadan daha yeni.")
    else:
        print("Güçlü sinyal oluşmadı.")

    # Eski state kayıtlarını küçült.
    cutoff = datetime.now(timezone.utc).timestamp() - 86400 * 3
    for bucket in ("checked", "alerts"):
        for key in list(state.get(bucket, {})):
            try:
                dt = datetime.fromisoformat(state[bucket][key]).timestamp()
                if dt < cutoff:
                    del state[bucket][key]
            except Exception:
                pass

    save_state(state)


if __name__ == "__main__":
    main()
