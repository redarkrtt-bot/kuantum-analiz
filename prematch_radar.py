import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

API_BASE = "https://v3.football.api-sports.io"
STATE_FILE = "prematch_state.json"
TZ = ZoneInfo("Europe/Berlin")

API_KEY = os.environ["API_FOOTBALL_KEY"]
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]

session = requests.Session()
session.headers.update({"x-apisports-key": API_KEY})

MAX_DEEP_MATCHES_PER_DAY = 7
FIXTURE_CACHE_HOURS = 8
PREMATCH_MIN = 5
PREMATCH_MAX = 25


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"fixture_cache": {}, "analyses": {}, "sent": {}, "quota": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def api_get(path, params):
    r = session.get(API_BASE + path, params=params, timeout=25)
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(str(data["errors"]))
    return data.get("response", []), r.headers


def now_berlin():
    return datetime.now(TZ)


def parse_dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TZ)


def refresh_fixtures(state, now):
    cache = state.get("fixture_cache", {})
    cached_at = cache.get("cached_at")
    if cached_at:
        try:
            age = now - datetime.fromisoformat(cached_at)
            if age < timedelta(hours=FIXTURE_CACHE_HOURS):
                return cache.get("fixtures", [])
        except Exception:
            pass

    fixtures = []
    for day in (now.date(), now.date() + timedelta(days=1)):
        rows, _ = api_get("/fixtures", {
            "date": day.isoformat(),
            "timezone": "Europe/Berlin",
        })
        fixtures.extend(rows)

    state["fixture_cache"] = {
        "cached_at": now.isoformat(),
        "fixtures": fixtures,
    }
    print(f"FIXTURE CACHE YENİLENDİ: {len(fixtures)} maç")
    return fixtures


def pct(v):
    try:
        return float(str(v).replace("%", ""))
    except Exception:
        return 0.0


def prediction_score(pred):
    p = pred.get("predictions", {}) or {}
    percent = p.get("percent", {}) or {}
    home = pct(percent.get("home"))
    draw = pct(percent.get("draw"))
    away = pct(percent.get("away"))
    vals = sorted([home, draw, away], reverse=True)
    edge = vals[0] - vals[1] if len(vals) > 1 else 0
    goals = p.get("goals", {}) or {}
    hg = pct((goals.get("home") or {}).get("total"))
    ag = pct((goals.get("away") or {}).get("total"))
    under_over = str(p.get("under_over") or "")
    advice = str(p.get("advice") or "")

    score = 0.0
    score += min(vals[0], 70) * 0.55
    score += min(edge, 35) * 0.65
    score += 5 if "Over" in under_over else 0
    score += 4 if "Under" in under_over else 0
    score += 3 if advice else 0
    if hg or ag:
        score += min(hg + ag, 4) * 2

    return {
        "score": round(score, 2),
        "home_pct": home,
        "draw_pct": draw,
        "away_pct": away,
        "advice": advice,
        "under_over": under_over,
        "home_goals": (goals.get("home") or {}).get("total"),
        "away_goals": (goals.get("away") or {}).get("total"),
        "winner": ((p.get("winner") or {}).get("name") if isinstance(p.get("winner"), dict) else None),
        "comparison": p.get("comparison", {}),
    }


def odds_signal(odds_rows):
    if not odds_rows:
        return {"score": 0, "text": "Oran verisi yok", "agreement": 0}

    prices = []
    for row in odds_rows:
        for book in row.get("bookmakers", []) or []:
            for bet in book.get("bets", []) or []:
                if str(bet.get("name", "")).lower() in {"match winner", "1x2"}:
                    for val in bet.get("values", []) or []:
                        try:
                            prices.append((str(val.get("value")), float(val.get("odd"))))
                        except Exception:
                            pass

    buckets = {"Home": [], "Draw": [], "Away": []}
    for name, odd in prices:
        key = "Home" if name in {"Home", "1"} else "Draw" if name in {"Draw", "X"} else "Away" if name in {"Away", "2"} else None
        if key:
            buckets[key].append(odd)

    implied = {}
    for k, arr in buckets.items():
        if arr:
            avg = sum(arr) / len(arr)
            implied[k] = 1 / avg

    total = sum(implied.values())
    probs = {k: (v / total * 100) for k, v in implied.items()} if total else {}
    if not probs:
        return {"score": 0, "text": "1X2 oranı bulunamadı", "agreement": 0}

    best = max(probs, key=probs.get)
    confidence = probs[best]
    text = f"Market: {best} %{confidence:.1f}"
    return {"score": min(confidence / 8, 10), "text": text, "agreement": confidence}


def enrichment(fixture):
    fid = fixture["fixture"]["id"]
    injuries, _ = api_get("/injuries", {"fixture": fid, "timezone": "Europe/Berlin"})
    lineups, _ = api_get("/fixtures/lineups", {"fixture": fid})
    return injuries, lineups


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
        return w
    except Exception:
        return None


def availability_text(injuries, lineups):
    missing = []
    for item in injuries or []:
        player = (item.get("player") or {}).get("name", "Oyuncu")
        reason = item.get("player", {}).get("reason") or item.get("reason") or "Belirsiz"
        missing.append(f"{player} ({reason})")

    lineup_text = "Kadrolar henüz kesinleşmemiş"
    if lineups:
        lineup_text = "İlk 11 verisi mevcut"
    return ", ".join(missing[:8]) if missing else "Belirgin eksik bilgisi yok", lineup_text


def send_discord(fixture, model, market, injuries, lineup_text, wx, minutes):
    teams = fixture.get("teams", {}) or {}
    league = fixture.get("league", {}) or {}
    fx = fixture.get("fixture", {}) or {}
    home = teams.get("home", {}).get("name", "?")
    away = teams.get("away", {}).get("name", "?")
    venue = (fx.get("venue", {}) or {}).get("name", "Bilinmiyor")
    city = (fx.get("venue", {}) or {}).get("city", "")
    referee = fx.get("referee") or "Bilinmiyor"

    weather_text = "Hava verisi yok"
    if wx:
        weather_text = (
            f"{wx.get('temperature_2m','?')}°C | yağış {wx.get('rain','?')} mm | "
            f"rüzgar {wx.get('wind_speed_10m','?')} km/s"
        )

    score = model["score"] + market["score"]
    if score >= 65:
        strength = "ÇOK GÜÇLÜ"
    elif score >= 52:
        strength = "GÜÇLÜ"
    else:
        strength = "ORTA"

    payload = {
        "username": "Goal Radar",
        "embeds": [{
            "title": "🧠 MAÇ ÖNCESİ RADAR — 15 DK KALA",
            "description": (
                f"**{home} – {away}**\\n"
                f"🏆 {league.get('name','Bilinmeyen Lig')} / {league.get('country','')}\\n"
                f"⏳ Başlamasına yaklaşık **{minutes} dk**\\n\\n"
                f"🎯 Sinyal gücü: **{strength}** ({score:.1f})\\n"
                f"🏆 Model yönü: **{model['winner'] or 'Belirlenemedi'}**\\n"
                f"📊 1X2 model: Ev %{model['home_pct']:.1f} | X %{model['draw_pct']:.1f} | Dep %{model['away_pct']:.1f}\\n"
                f"⚽ Gol senaryosu: **{model['under_over'] or 'Belirlenemedi'}**\\n"
                f"🔢 Tahmini goller: {model['home_goals'] or '?'} - {model['away_goals'] or '?'}\\n"
                f"📌 Model tavsiyesi: **{model['advice'] or 'Yok'}**\\n"
                f"💹 {market['text']}\\n"
                f"🏥 Eksikler: {injuries}\\n"
                f"👥 Kadro: {lineup_text}\\n"
                f"🌦️ {weather_text}\\n"
                f"🧑‍⚖️ Hakem: {referee}\\n"
                f"🏟️ Stadyum: {venue} {('• ' + city) if city else ''}\\n\\n"
                "Bu skor bir olasılık/sinyal ölçümüdür; garanti değildir."
            ),
            "footer": {"text": "Goal Radar • Berlin time • pre-match confirmation"},
        }]
    }
    r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=15)
    r.raise_for_status()


def main():
    state = load_state()
    now = now_berlin()
    today = now.date().isoformat()

    quota = state.setdefault("quota", {})
    if quota.get("date") != today:
        quota.clear()
        quota.update({"date": today, "deep_matches": 0})

    fixtures = refresh_fixtures(state, now)
    save_state(state)

    candidates = []
    for f in fixtures:
        status = (f.get("fixture", {}).get("status", {}) or {}).get("short")
        if status not in {"NS", "TBD"}:
            continue
        dt = parse_dt(f["fixture"]["date"])
        mins = (dt - now).total_seconds() / 60
        if PREMATCH_MIN <= mins <= PREMATCH_MAX:
            candidates.append((mins, f))

    print(f"MAÇ ÖNÜ RADARI: {len(candidates)} aday bulundu (T-{PREMATCH_MAX} ile T-{PREMATCH_MIN} dk arası).")

    if not candidates:
        return

    analyses = state.setdefault("analyses", {})
    sent = state.setdefault("sent", {})

    scored = []
    for mins, fixture in sorted(candidates, key=lambda x: x[0]):
        fid = str(fixture["fixture"]["id"])
        if sent.get(fid) == today:
            continue
        if analyses.get(fid, {}).get("date") == today:
            scored.append((analyses[fid]["score"], mins, fixture, analyses[fid]["model"], analyses[fid]["market"]))
            continue
        if quota["deep_matches"] >= MAX_DEEP_MATCHES_PER_DAY:
            continue

        rows, _ = api_get("/predictions", {"fixture": fid})
        if not rows:
            continue
        model = prediction_score(rows[0])

        odds_rows, _ = api_get("/odds", {"fixture": fid})
        market = odds_signal(odds_rows)

        total = model["score"] + market["score"]
        analyses[fid] = {"date": today, "score": round(total, 2), "model": model, "market": market}
        quota["deep_matches"] += 1
        scored.append((total, mins, fixture, model, market))

    save_state(state)

    if not scored:
        print("Güçlü aday yok veya günlük derin analiz kotası doldu.")
        return

    scored.sort(key=lambda x: x[0], reverse=True)
    total, mins, fixture, model, market = scored[0]

    # Only enrich the strongest match: injuries + lineups are the expensive final confirmation.
    fid = str(fixture["fixture"]["id"])
    injuries_raw, lineups = enrichment(fixture)
    injuries, lineup_text = availability_text(injuries_raw, lineups)
    city = (fixture.get("fixture", {}).get("venue", {}) or {}).get("city") or ""
    wx = weather(city)

    send_discord(fixture, model, market, injuries, lineup_text, wx, int(round(mins)))
    sent[fid] = today
    state["last_alert"] = {
        "fixture": fid,
        "home": fixture["teams"]["home"]["name"],
        "away": fixture["teams"]["away"]["name"],
        "minutes_before": round(mins, 1),
        "score": round(total, 2),
        "time": now.isoformat(),
    }
    save_state(state)
    print(f"MAÇ ÖNÜ ALARMI GÖNDERİLDİ: {fid} | skor={total:.1f} | T-{mins:.1f} dk")


if __name__ == "__main__":
    main()
