import json
import os
from datetime import datetime, timezone

import requests

API_BASE = "https://v3.football.api-sports.io"
STATE_FILE = "state.json"
MINUTE_MIN = 8
MINUTE_MAX = 88
MAX_TOTAL_GOALS = 3
WEATHER_CACHE_HOURS = 2
INJURY_CACHE_HOURS = 4
VENUE_CACHE_HOURS = 24

api_key = os.environ["API_FOOTBALL_KEY"]
discord_webhook = os.environ["DISCORD_WEBHOOK"]

session = requests.Session()
session.headers.update({"x-apisports-key": api_key})
API_REMAINING = None


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"checked": {}, "alerts": {}, "enrichment": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def api_get(path, params):
    global API_REMAINING
    r = session.get(API_BASE + path, params=params, timeout=25)
    remaining = r.headers.get("x-ratelimit-requests-remaining")
    try:
        API_REMAINING = int(remaining) if remaining is not None else API_REMAINING
    except (TypeError, ValueError):
        pass
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
    now = datetime.now(timezone.utc)
    freshness_penalty = 0.0
    if last:
        try:
            age_min = (now - datetime.fromisoformat(last)).total_seconds() / 60
            if age_min < 30:
                # A match whose stats just failed should not monopolize the
                # next scan. Give another live match the API call instead.
                return -1000.0
            elif age_min < 60:
                freshness_penalty = 8.0
            elif age_min < 90:
                freshness_penalty = 2.0
        except Exception:
            pass
    phase_bonus = 8.0 if 45 <= minute <= 78 else 3.0
    score_bonus = 3.0 if total_goals(fixture) <= 1 else 0.0
    unseen_bonus = 6.0 if last is None else 0.0
    return phase_bonus + score_bonus + unseen_bonus - freshness_penalty - (minute / 1000)


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


def get_stat(stats, name):
    return num(stats.get(name))


def pressure_components(s):
    return {
        "attacks": get_stat(s, "Dangerous Attacks"),
        "shots": get_stat(s, "Total Shots"),
        "sot": get_stat(s, "Shots on Goal"),
        "corners": get_stat(s, "Corner Kicks"),
        "possession": get_stat(s, "Ball Possession"),
        "passes": get_stat(s, "Total passes"),
        "pass_accuracy": get_stat(s, "Passes %"),
        "blocked": get_stat(s, "Blocked Shots"),
        "inside": get_stat(s, "Shots insidebox"),
        "outside": get_stat(s, "Shots outsidebox"),
        "offsides": get_stat(s, "Offsides"),
        "saves": get_stat(s, "Goalkeeper Saves"),
        "fouls": get_stat(s, "Fouls"),
        "yellow": get_stat(s, "Yellow Cards"),
        "red": get_stat(s, "Red Cards"),
    }


def analyze(fixture, stats_response, weather=None, injuries=None, venue=None):
    home = fixture["teams"]["home"]
    away = fixture["teams"]["away"]
    score = fixture.get("goals", {}) or {}
    home_goals = int(score.get("home") or 0)
    away_goals = int(score.get("away") or 0)
    minute = parse_minute(fixture)

    stats = extract_stats(stats_response)
    hs = stats.get(str(home["id"]), {})
    aws = stats.get(str(away["id"]), {})
    hc = pressure_components(hs)
    ac = pressure_components(aws)

    # Match-total pressure. Missing statistics are NOT treated as positive evidence.
    home_score = (
        (hc["sot"] >= 3) * 2.0
        + (hc["shots"] >= 7) * 1.0
        + (hc["attacks"] >= 20) * 1.0
        + (hc["corners"] >= 3) * 1.0
        + (hc["possession"] >= 52) * 0.5
        + (hc["inside"] >= 4) * 1.0
        + (hc["blocked"] >= 2) * 0.5
    )
    away_score = (
        (ac["sot"] >= 3) * 2.0
        + (ac["shots"] >= 7) * 1.0
        + (ac["attacks"] >= 20) * 1.0
        + (ac["corners"] >= 3) * 1.0
        + (ac["possession"] >= 52) * 0.5
        + (ac["inside"] >= 4) * 1.0
        + (ac["blocked"] >= 2) * 0.5
    )

    # Score-state and match-phase context.
    score_state = f"{home_goals}-{away_goals}"
    state_bonus = 0.0
    if home_goals == away_goals:
        state_bonus = 0.7 if minute >= 65 else 0.2
    elif abs(home_goals - away_goals) == 1:
        state_bonus = 0.4
    if minute <= 30:
        phase = "İLK_YARI_ERKEN"
    elif minute <= 45:
        phase = "İLK_YARI_SON"
    elif minute <= 70:
        phase = "İKİNCİ_YARI_ORTA"
    else:
        phase = "İKİNCİ_YARI_SON"

    # Weather is a modifier, never a standalone prediction.
    weather_penalty = 0.0
    weather_note = "Hava verisi yok"
    if weather:
        rain = num(weather.get("rain"))
        wind = num(weather.get("wind"))
        snowfall = num(weather.get("snowfall"))
        temp = num(weather.get("temperature"))
        if rain >= 4 or snowfall >= 1 or wind >= 45:
            weather_penalty = 0.6
            weather_note = f"Zorlu hava: yağış={rain:.1f}mm, rüzgar={wind:.0f}km/s"
        elif rain > 0.5 or wind >= 30:
            weather_penalty = 0.25
            weather_note = f"Hava etkisi: yağış={rain:.1f}mm, rüzgar={wind:.0f}km/s"
        else:
            weather_note = f"Normal: {temp:.1f}°C, yağış={rain:.1f}mm, rüzgar={wind:.0f}km/s"

    # Data quality prevents false confidence.
    fields = [
        hc["shots"], hc["sot"], hc["corners"], hc["attacks"],
        ac["shots"], ac["sot"], ac["corners"], ac["attacks"]
    ]
    available = sum(v > 0 for v in fields)
    data_quality = round(min(1.0, available / 8.0), 2)

    raw_home = home_score + state_bonus - weather_penalty
    raw_away = away_score + state_bonus - weather_penalty

    combined_shots = hc["shots"] + ac["shots"]
    combined_sot = hc["sot"] + ac["sot"]
    combined_attacks = hc["attacks"] + ac["attacks"]
    combined_corners = hc["corners"] + ac["corners"]
    combined_inside = hc["inside"] + ac["inside"]

    # The alarm targets the next goal using pressure from both teams.
    total_pressure = (
        combined_sot * 1.8
        + combined_shots * 0.45
        + combined_attacks * 0.08
        + combined_corners * 0.55
        + combined_inside * 0.35
    )

    strong_total = (
        combined_sot >= 6
        or (combined_sot >= 4 and combined_shots >= 14)
        or (combined_sot >= 3 and combined_shots >= 10 and combined_corners >= 6)
    )
    late_scoreless = home_goals == 0 and away_goals == 0 and minute >= 58 and combined_sot >= 3
    one_goal_high_pressure = (
        home_goals + away_goals <= 1
        and minute >= 52
        and combined_sot >= 4
        and combined_shots >= 11
    )

    if data_quality < 0.50:
        strength, signal = "VERİ YETERSİZ", False
        direction = "Yeterli canlı veri yok"
    elif strong_total or late_scoreless or one_goal_high_pressure:
        strength, signal = "GÜÇLÜ", True
        if raw_home >= raw_away + 1.0:
            direction = f"{home['name']} gol baskısı"
        elif raw_away >= raw_home + 1.0:
            direction = f"{away['name']} gol baskısı"
        else:
            direction = "Çift taraflı yüksek gol baskısı"
    elif total_pressure >= 13.0:
        strength, signal = "ORTA", False
        direction = "Orta seviyede toplam gol baskısı"
    else:
        strength, signal = "ZAYIF", False
        direction = "Belirgin gol baskısı yok"

    referee = (fixture.get("fixture", {}) or {}).get("referee") or "Bilinmiyor"
    venue_data = fixture.get("fixture", {}).get("venue", {}) or {}

    return {
        "signal": signal,
        "strength": strength,
        "direction": direction,
        "minute": minute,
        "score": score_state,
        "home": home["name"],
        "away": away["name"],
        "home_score": round(raw_home, 2),
        "away_score": round(raw_away, 2),
        "pressure_home": round(hc["attacks"] * 0.30 + hc["shots"] * 1.5 + hc["sot"] * 3 + hc["corners"], 2),
        "pressure_away": round(ac["attacks"] * 0.30 + ac["shots"] * 1.5 + ac["sot"] * 3 + ac["corners"], 2),
        "data_quality": data_quality,
        "phase": phase,
        "referee": referee,
        "venue": venue_data.get("name") or "Bilinmiyor",
        "venue_city": venue_data.get("city") or "",
        "weather": weather_note,
        "injuries": injuries or [],
        "venue_detail": venue or {},
        "stats_present": bool(stats),
    }


def cache_fresh(state, bucket, key, hours):
    value = state.get("enrichment", {}).get(bucket, {}).get(str(key))
    if not value or "updated_at" not in value:
        return False
    try:
        dt = datetime.fromisoformat(value["updated_at"])
        return (datetime.now(timezone.utc) - dt).total_seconds() < hours * 3600
    except Exception:
        return False


def fetch_weather(city):
    if not city:
        return None
    try:
        g = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "en", "format": "json"},
            timeout=12,
        ).json()
        result = (g.get("results") or [None])[0]
        if not result:
            return None
        lat, lon = result["latitude"], result["longitude"]
        w = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,precipitation,rain,snowfall,wind_speed_10m",
                "timezone": "auto",
            },
            timeout=12,
        ).json()
        current = w.get("current") or {}
        return {
            "temperature": current.get("temperature_2m"),
            "rain": current.get("rain"),
            "snowfall": current.get("snowfall"),
            "wind": current.get("wind_speed_10m"),
        }
    except Exception as e:
        print(f"Hava verisi alınamadı: {e}")
        return None


def fetch_optional_enrichment(state, fixture, provisional):
    fid = str(fixture["fixture"]["id"])
    enrichment = state.setdefault("enrichment", {})
    enrichment.setdefault("weather", {})
    enrichment.setdefault("injuries", {})
    enrichment.setdefault("venue", {})

    city = (fixture.get("fixture", {}).get("venue", {}) or {}).get("city") or ""
    weather = None
    if city:
        key = city.lower()
        if cache_fresh(state, "weather", key, WEATHER_CACHE_HOURS):
            weather = enrichment["weather"][key].get("data")
        else:
            weather = fetch_weather(city)
            enrichment["weather"][key] = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "data": weather,
            }

    injuries = []
    # Free API-Football is 100 requests/day. Live and pre-match scans share
    # the same API quota, so optional enrichment is deliberately capped.
    today = datetime.now(timezone.utc).date().isoformat()
    quota = state.setdefault("quota", {"date": today, "enrichment_calls": 0})
    if quota.get("date") != today:
        quota["date"] = today
        quota["enrichment_calls"] = 0

    if fid in enrichment["injuries"]:
        injuries = enrichment["injuries"][fid].get("data") or []

    if (
        provisional >= 4.0
        and not cache_fresh(state, "injuries", fid, INJURY_CACHE_HOURS)
        and quota.get("enrichment_calls", 0) < 2
    ):
        try:
            injuries = api_get("/injuries", {"fixture": fid})
            enrichment["injuries"][fid] = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "data": injuries,
            }
            quota["enrichment_calls"] += 1
        except Exception as e:
            print(f"Sakatlık verisi alınamadı: {e}")

    # Fixture data already contains the venue name/city; avoid a separate
    # /venues call on the free tier.
    venue = {}
    return weather, injuries, venue


def send_discord(result):
    if not result["signal"]:
        return

    injury_names = []
    for item in result["injuries"][:8]:
        player = (item.get("player") or {}).get("name") or "Bilinmeyen"
        reason = item.get("reason") or item.get("type") or ""
        injury_names.append(f"{player} ({reason})")
    injury_text = ", ".join(injury_names) if injury_names else "Kritik eksik bilgisi yok/veri yok"

    venue = result["venue_detail"]
    venue_text = result["venue"]
    if venue.get("capacity"):
        venue_text += f" • {venue.get('capacity')} kapasite"
    if venue.get("surface"):
        venue_text += f" • {venue.get('surface')}"

    payload = {
        "username": "Goal Radar",
        "embeds": [{
            "title": "🚨 GOL SİNYALİ — GELİŞMİŞ MOTOR",
            "description": (
                f"**{result['home']} – {result['away']}**\n"
                f"⏱️ {result['minute']}' | ⚽ {result['score']} | {result['phase']}\n"
                f"🎯 **{result['direction']}**\n"
                f"🔥 Sinyal: **{result['strength']}**\n"
                f"📊 Veri güvenilirliği: **{result['data_quality']*100:.0f}%**\n"
                f"🌦️ {result['weather']}\n"
                f"👨‍⚖️ Hakem: {result['referee']}\n"
                f"🏟️ {venue_text}\n"
                f"🚑 Eksikler: {injury_text}"
            ),
            "footer": {"text": "Goal Radar • canlı istatistik + bağlam + hava + kadro kontrolü"},
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

    print(f"CANLI TARAMA: toplam={len(live)} | uygun={len(candidates)}")
    if not candidates:
        print("SONUÇ: Uygun canlı maç yok; alarm üretilmedi.")
        save_state(state)
        return

    candidates.sort(key=lambda f: candidate_score(f, state), reverse=True)
    preview = []
    for f in candidates[:10]:
        ht = f.get("teams", {}).get("home", {}).get("name", "?")
        at = f.get("teams", {}).get("away", {}).get("name", "?")
        preview.append(f"{ht}-{at} ({parse_minute(f)}')")
    print("ADAYLAR:", " | ".join(preview))

    # Free plan: one live fixture call + one direct statistics call per scan.
    # /fixtures/statistics is the correct endpoint for live team statistics.
    max_details_this_run = 3
    if API_REMAINING is not None:
        max_details_this_run = min(max_details_this_run, max(0, API_REMAINING))

    if max_details_this_run <= 0:
        print(f"DETAY ATLANDI: API kalan kota={API_REMAINING}.")
        state["last_scan"] = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "live_matches": len(live),
            "candidates": len(candidates),
            "deep_scanned": 0,
            "stats_available": 0,
            "missing_stats": 0,
            "signal": False,
            "quota_remaining": API_REMAINING,
        }
        save_state(state)
        return

    analyzed = []
    missing_stats = []
    checked_details = 0

    for fixture in candidates:
        if checked_details >= max_details_this_run:
            break

        fid = str(fixture["fixture"]["id"])
        try:
            stats_response = api_get("/fixtures/statistics", {"fixture": fid})
            checked_details += 1
            state.setdefault("checked", {})[fid] = datetime.now(timezone.utc).isoformat()

            if not stats_response:
                missing_stats.append(fid)
                print(f"İSTATİSTİK YOK: {fid} — bu lig/maç için canlı istatistik gelmedi.")
                continue

            provisional = analyze(fixture, stats_response)
            analyzed.append((fixture, stats_response, provisional))
            print(
                f"ADAY ANALİZİ: {provisional['home']} - {provisional['away']} | "
                f"{provisional['minute']}' | kalite={provisional['data_quality']:.2f} | "
                f"ev={provisional['home_score']:.2f} | dep={provisional['away_score']:.2f} | "
                f"sinyal={provisional['signal']} | yön={provisional['direction']}"
            )
        except Exception as e:
            checked_details += 1
            print(f"İSTATİSTİK HATASI: {fid} | {e}")

    print(
        f"DERİN TARAMA: kontrol={checked_details} | "
        f"istatistikli={len(analyzed)} | istatistiksiz={len(missing_stats)} | "
        f"kalan_kota={API_REMAINING}"
    )

    if not analyzed:
        print("SONUÇ: Seçilen aday için canlı istatistik verisi alınamadı; sonraki taramada başka aday seçilecek.")
        state["last_scan"] = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "live_matches": len(live),
            "candidates": len(candidates),
            "deep_scanned": checked_details,
            "stats_available": 0,
            "missing_stats": len(missing_stats),
            "signal": False,
            "quota_remaining": API_REMAINING,
        }
        save_state(state)
        return

    strong = [item for item in analyzed if item[2]["signal"]]
    pool = strong if strong else analyzed
    pool.sort(
        key=lambda item: (
            max(item[2]["home_score"], item[2]["away_score"]),
            item[2]["data_quality"],
            max(item[2]["pressure_home"], item[2]["pressure_away"]),
        ),
        reverse=True,
    )

    detailed, stats_response, provisional = pool[0]
    print(
        f"SEÇİLEN: {provisional['home']} - {provisional['away']} | "
        f"{provisional['minute']}' | skor={provisional['score']} | "
        f"ön_sinyal={provisional['signal']}"
    )

    provisional_score = max(provisional["home_score"], provisional["away_score"])
    weather, injuries, venue = fetch_optional_enrichment(state, detailed, provisional_score)
    result = analyze(detailed, stats_response, weather, injuries, venue)

    print(json.dumps(result, ensure_ascii=False))
    print(
        f"ANALİZ: {result['home']} - {result['away']} | "
        f"skor={result['score']} | dakika={result['minute']} | "
        f"kalite={result['data_quality']:.2f} | "
        f"ev={result['home_score']:.2f} | dep={result['away_score']:.2f} | "
        f"sinyal={result['signal']}"
    )

    state["last_scan"] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "live_matches": len(live),
        "candidates": len(candidates),
        "deep_scanned": checked_details,
        "stats_available": len(analyzed),
        "missing_stats": len(missing_stats),
        "strong_candidates": len(strong),
        "selected_fixture": str(detailed["fixture"]["id"]),
        "signal": bool(result["signal"]),
        "quota_remaining": API_REMAINING,
    }

    if result["signal"]:
        fid = str(detailed["fixture"]["id"])
        last_alert = state.setdefault("alerts", {}).get(fid)
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
        print(f"SONUÇ: Alarm yok — {result['strength']} / {result['direction']} | analiz={len(analyzed)}/{checked_details}")

    cutoff = datetime.now(timezone.utc).timestamp() - 86400 * 7
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
