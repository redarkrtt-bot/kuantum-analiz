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
# Live radar: broad rotation + pressure-memory engine.
# The free API tier has 100 requests/day, so deep API calls are deliberately
# batched and only made on every 3rd scan. The broad scan can still use FotMob.
BATCH_DETAIL_EVERY_SCANS = 3
MAX_DEEP_SCAN = 12


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
    now = datetime.now(timezone.utc)
    checked = state.get("checked", {})
    failed = state.get("stats_failed", {})
    recent = state.get("recent_analysis", {})

    def age_minutes(bucket):
        value = bucket.get(fid)
        if not value:
            return None
        try:
            return (now - datetime.fromisoformat(value)).total_seconds() / 60
        except Exception:
            return None

    checked_age = age_minutes(checked)
    failed_age = age_minutes(failed)
    recent_item = recent.get(fid, {})
    recent_age = None
    if recent_item.get("updated_at"):
        try:
            recent_age = (now - datetime.fromisoformat(recent_item["updated_at"])).total_seconds() / 60
        except Exception:
            pass

    # A missing-data match must not disappear from the radar for two hours.
    # It gets a penalty so healthy matches are preferred, but it can return
    # quickly on later rotations when it becomes one of the stronger candidates.
    if failed_age is not None and failed_age < 45:
        return -250.0
    if checked_age is not None and checked_age < 35:
        return -3000.0

    phase_bonus = 10.0 if 50 <= minute <= 82 else 5.0
    score_bonus = 5.0 if total_goals(fixture) == 0 else 3.0 if total_goals(fixture) == 1 else 0.0
    late_bonus = 4.0 if minute >= 65 else 0.0
    unseen_bonus = 8.0 if checked_age is None else 0.0
    cached_pressure = float(recent_item.get("pressure", 0.0) or 0.0)
    cache_bonus = min(cached_pressure / 4.0, 12.0) if recent_age is not None and recent_age < 90 else 0.0
    freshness = 0.0 if checked_age is None else min(12.0, checked_age / 8.0)
    return phase_bonus + score_bonus + late_bonus + unseen_bonus + cache_bonus + freshness - (minute / 1000)


FOTMOB_MATCH_CACHE = {}


def _live_stats_fallback(fixture):
    global FOTMOB_MATCH_CACHE
    from difflib import SequenceMatcher
    import unicodedata

    home = str((fixture.get("teams", {}).get("home", {}) or {}).get("name", ""))
    away = str((fixture.get("teams", {}).get("away", {}) or {}).get("name", ""))

    def norm(value):
        s = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
        for token in (" women", " fc", " afc", " cf", " sc", " club"):
            s = s.replace(token, " ")
        return "".join(ch for ch in s if ch.isalnum())

    try:
        kickoff = (fixture.get("fixture") or {}).get("timestamp")
        if kickoff:
            match_day = datetime.fromtimestamp(int(kickoff), timezone.utc).strftime("%Y%m%d")
        else:
            match_day = datetime.now(timezone.utc).strftime("%Y%m%d")

        # FotMob's date endpoint is date-sensitive; check adjacent UTC dates too,
        # because fixtures near midnight can be listed on a neighboring day.
        days = []
        base = datetime.strptime(match_day, "%Y%m%d").replace(tzinfo=timezone.utc)
        for offset in (-1, 0, 1):
            days.append((base + __import__("datetime").timedelta(days=offset)).strftime("%Y%m%d"))

        matches = []
        for day in days:
            if day not in FOTMOB_MATCH_CACHE:
                response = requests.get(
                    "https://www.fotmob.com/api/matches",
                    params={"date": day},
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                    timeout=15,
                )
                response.raise_for_status()
                data = response.json()
                FOTMOB_MATCH_CACHE[day] = [
                    m for league in data.get("leagues", []) or []
                    for m in league.get("matches", []) or []
                ]
            matches.extend(FOTMOB_MATCH_CACHE.get(day, []))

        fh, fa = norm(home), norm(away)
        best, best_score = None, 0.0
        seen = set()
        for match in matches:
            mid = str(match.get("id") or "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            mh = norm((match.get("home") or {}).get("name"))
            ma = norm((match.get("away") or {}).get("name"))
            if not mh or not ma:
                continue
            home_sim = 1.0 if fh == mh else SequenceMatcher(None, fh, mh).ratio()
            away_sim = 1.0 if fa == ma else SequenceMatcher(None, fa, ma).ratio()
            # Names sometimes include reserve/youth labels on only one source.
            score = (home_sim + away_sim) / 2
            if home_sim >= 0.68 and away_sim >= 0.68 and score > best_score:
                best, best_score = match, score

        if not best:
            print(f"FOTMOB EŞLEŞMESİ YOK: {home} - {away} | tarihler={','.join(days)}")
            return None

        detail_response = requests.get(
            "https://www.fotmob.com/api/matchDetails",
            params={"matchId": best["id"]},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=15,
        )
        detail_response.raise_for_status()
        detail = detail_response.json()

        stats_root = ((detail.get("content") or {}).get("stats") or {})
        periods = stats_root.get("Periods") or {}
        all_stats = periods.get("All") or periods.get("AllMatch") or {}
        if not all_stats:
            all_stats = next((v for v in periods.values() if isinstance(v, dict) and v.get("stats")), {})
        home_stats, away_stats = {}, {}
        mapping = {
            "ball possession": "Ball Possession",
            "total shots": "Total Shots",
            "shots on target": "Shots on Goal",
            "shots on goal": "Shots on Goal",
            "corner kicks": "Corner Kicks",
            "dangerous attacks": "Dangerous Attacks",
            "shots inside box": "Shots insidebox",
            "shots inside the box": "Shots insidebox",
            "shots outside box": "Shots outsidebox",
            "shots outside the box": "Shots outsidebox",
            "blocked shots": "Blocked Shots",
            "fouls": "Fouls",
            "yellow cards": "Yellow Cards",
            "red cards": "Red Cards",
            "offsides": "Offsides",
        }
        for group in all_stats.get("stats", []) or []:
            for item in group.get("stats", []) or []:
                target = mapping.get(str(item.get("title") or "").strip().lower())
                values = item.get("stats")
                if target and isinstance(values, list) and len(values) >= 2:
                    home_stats[target], away_stats[target] = values[0], values[1]

        hid = str((fixture.get("teams", {}).get("home", {}) or {}).get("id"))
        aid = str((fixture.get("teams", {}).get("away", {}) or {}).get("id"))
        if not home_stats and not away_stats:
            print(f"FOTMOB İSTATİSTİK YOK: {home} - {away} | matchId={best.get('id')} | eşleşme={best_score:.2f}")
            return None

        print(f"FOTMOB VERİSİ: {home} - {away} | eşleşme={best_score:.2f} | alan={len(home_stats)+len(away_stats)}")
        return [
            {"team": {"id": hid}, "statistics": [{"type": k, "value": v} for k, v in home_stats.items()]},
            {"team": {"id": aid}, "statistics": [{"type": k, "value": v} for k, v in away_stats.items()]},
        ]
    except Exception as e:
        print(f"İKİNCİ KAYNAK HATASI: {home} - {away} | {type(e).__name__}: {e}")
        return None

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
        combined_sot >= 5
        or (combined_sot >= 4 and combined_shots >= 11)
        or (combined_sot >= 3 and combined_shots >= 9 and combined_corners >= 4)
    )
    late_scoreless = home_goals == 0 and away_goals == 0 and minute >= 55 and combined_sot >= 3 and combined_shots >= 8
    one_goal_high_pressure = (
        home_goals + away_goals <= 1
        and minute >= 50
        and combined_sot >= 4
        and combined_shots >= 10
        and combined_corners >= 3
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

    # Do NOT spend the scarce API-Football quota on injuries during live radar.
    # Live goal detection is the priority; cached injury data is still used when present.

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




def send_radar_health(message):
    """Send a throttled diagnostic notice, distinct from a match goal signal."""
    payload = {
        "username": "Goal Radar",
        "embeds": [{
            "title": "🛠️ GOAL RADAR — SİSTEM UYARISI",
            "description": message,
            "footer": {"text": "Bu bir maç gol alarmı değildir; veri/çalışma durumu bildirimidir."},
        }]
    }
    response = requests.post(discord_webhook, json=payload, timeout=15)
    response.raise_for_status()


def main():
    state = load_state()
    state.setdefault("checked", {})
    state.setdefault("alerts", {})
    state.setdefault("enrichment", {})
    state.setdefault("stats_failed", {})
    state.setdefault("recent_analysis", {})

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

    # One quota-controlled API statistics call per run. The schedule is
    # limited to two runs/hour so the live feed + detail call stay near 96/day
    # on the Free plan. Never rely on the blocked FotMob JSON endpoint.
    scan_count = int(state.get("scan_count", 0) or 0) + 1
    state["scan_count"] = scan_count
    scan_candidates = candidates[:1]
    analyzed = []
    missing_stats = []
    checked_details = 0

    for fixture in scan_candidates:
        fid = str(fixture["fixture"]["id"])
        try:
            if API_REMAINING is not None and API_REMAINING <= 0:
                print(f"KOTA BİTTİ: istatistik isteği atlanıyor | fixture={fid}")
                break
            stats_response = api_get("/fixtures/statistics", {"fixture": fid})
            checked_details += 1
            now_iso = datetime.now(timezone.utc).isoformat()
            state.setdefault("checked", {})[fid] = now_iso

            if not stats_response:
                missing_stats.append(fid)
                state.setdefault("stats_failed", {})[fid] = now_iso
                print(f"API İSTATİSTİK YOK: {fid} — sonraki taramada sıradaki maç denenecek.")
                continue

            state.setdefault("stats_failed", {}).pop(fid, None)
            provisional = analyze(fixture, stats_response)
            analyzed.append((fixture, stats_response, provisional))
            state.setdefault("recent_analysis", {})[fid] = {
                "updated_at": now_iso,
                "pressure": round(
                    max(provisional["pressure_home"], provisional["pressure_away"])
                    + provisional["home_score"] + provisional["away_score"], 2
                ),
                "signal": bool(provisional["signal"]),
                "minute": provisional["minute"],
                "score": provisional["score"],
                "home": provisional["home"],
                "away": provisional["away"],
            }
            print(
                f"ADAY ANALİZİ: {provisional['home']} - {provisional['away']} | "
                f"{provisional['minute']}' | kaynak=API-STATISTICS | "
                f"kalite={provisional['data_quality']:.2f} | "
                f"ev={provisional['home_score']:.2f} | dep={provisional['away_score']:.2f} | "
                f"sinyal={provisional['signal']} | yön={provisional['direction']}"
            )
        except Exception as e:
            checked_details += 1
            print(f"ADAY ANALİZ HATASI: {fid} | {type(e).__name__}: {e}")

    print(
        f"DERİN TARAMA: kontrol={checked_details} | "
        f"istatistikli={len(analyzed)} | istatistiksiz={len(missing_stats)} | "
        f"kalan_kota={API_REMAINING}"
    )

    if not analyzed:
        print("SONUÇ: Seçilen aday için canlı istatistik verisi alınamadı; sonraki taramada başka aday seçilecek.")
        # A separate hourly health notice makes silent data-source failure visible.
        last_health = state.get("radar_health_last")
        health_due = True
        if last_health:
            try:
                health_due = (datetime.now(timezone.utc) - datetime.fromisoformat(last_health)).total_seconds() >= 3600
            except Exception:
                pass
        if health_due:
            send_radar_health(
                f"Canlı maç bulundu: **{len(live)}** | Uygun aday: **{len(candidates)}** | "
                f"Derin tarama: **{checked_details}** | İstatistik alınan: **0**.\\n"
                "Maç alarmı üretilmedi; çünkü doğrulanabilir canlı baskı verisi alınamadı. "
                "Bir sonraki taramada kaynaklar yeniden denenecek."
            )
            state["radar_health_last"] = datetime.now(timezone.utc).isoformat()
        state["last_scan"] = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "live_matches": len(live),
            "candidates": len(candidates),
            "deep_scanned": checked_details,
            "stats_available": 0,
            "scan_count": scan_count,
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

    # Do not stop at the first signal. If several matches are genuinely strong,
    # send up to 3 independent alarms in the same scan. Each fixture still has
    # its own 30-minute cooldown, so the radar can cover more matches without spam.
    selected = (strong[:3] if strong else pool[:1])
    sent_alerts = []
    selected_ids = []

    for detailed, stats_response, provisional in selected:
        fid = str(detailed["fixture"]["id"])
        selected_ids.append(fid)

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

        if result["signal"]:
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
                sent_alerts.append(fid)
                print(f"Discord alarmı gönderildi: {fid}")
            else:
                print(f"Aynı maç için son alarm 30 dakikadan daha yeni: {fid}")
        else:
            print(f"SONUÇ: Alarm yok — {result['strength']} / {result['direction']} | analiz={len(analyzed)}/{checked_details}")

    state["last_scan"] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "live_matches": len(live),
        "candidates": len(candidates),
        "deep_scanned": checked_details,
        "stats_available": len(analyzed),
        "scan_count": scan_count,
        "missing_stats": len(missing_stats),
        "strong_candidates": len(strong),
        "selected_fixtures": selected_ids,
        "alerts_sent": len(sent_alerts),
        "signal": bool(strong),
        "quota_remaining": API_REMAINING,
    }

    cutoff = datetime.now(timezone.utc).timestamp() - 86400 * 7
    for bucket in ("checked", "alerts", "stats_failed"):
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
