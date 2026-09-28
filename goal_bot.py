import json, os, re, time
from datetime import datetime, timezone
from pathlib import Path
import requests

WEBHOOK=os.environ["DISCORD_WEBHOOK"]
SPORTSCORE="https://sportscore.com/api/v1"
ESPN="https://site.api.espn.com/apis/site/v2/sports/soccer"
LEAGUES=["eng.1","eng.2","esp.1","esp.2","ger.1","ger.2","ita.1","ita.2","fra.1","fra.2","ned.1","por.1","usa.1","mex.1","bra.1","arg.1","uefa.champions","uefa.europa"]
MAX_DETAILS=200
COOLDOWN=25
S=requests.Session()
S.headers.update({"User-Agent":"Mozilla/5.0 GoalRadar/2.1","Accept":"application/json"})

def now(): return datetime.now(timezone.utc)
def get(url,params=None):
    r=S.get(url,params=params,timeout=15); r.raise_for_status(); return r.json()
def post(title,desc,color=3066993):
    r=S.post(WEBHOOK,json={"username":"Goal Radar","embeds":[{"title":title,"description":desc[:4000],"color":color,"timestamp":now().isoformat(),"footer":{"text":"Canlı veri sinyali • gol/kazanç garantisi değildir"}}]},timeout=15); r.raise_for_status()
def integer(v):
    if v is None:return 0
    if isinstance(v,(int,float)):return int(v)
    s=str(v).strip()
    if re.search(r"\b(1st|2nd|first|second)\s*half\b",s,re.I):return 0
    m=re.search(r"(\d{1,3})(?:\s*\+\s*\d{1,2})?\s*['′’]?",s)
    return int(m.group(1)) if m else 0
def live_status(v):
    s=str(v or "").lower()
    return any(x in s for x in ("live","in progress","1st half","2nd half","half time","1h","2h","inplay","in_play"))
def normalize(x):
    home=x.get("home") or x.get("home_team") or {}
    away=x.get("away") or x.get("away_team") or {}
    if isinstance(home,dict): hn=home.get("name") or home.get("displayName") or home.get("teamName") or "Home"
    else: hn=str(home or "Home")
    if isinstance(away,dict): an=away.get("name") or away.get("displayName") or away.get("teamName") or "Away"
    else: an=str(away or "Away")
    hs=x.get("home_score",x.get("score_home",0)); ass=x.get("away_score",x.get("score_away",0))
    status=x.get("status_text") or x.get("status") or x.get("state") or ""
    clock=x.get("minute") or x.get("elapsed") or status
    url_slug=str(x.get("url") or "").rstrip("/").split("/")[-1]
    slug=x.get("slug") or x.get("match_slug") or url_slug
    return {"id":str(x.get("id") or x.get("event_id") or slug or ""),
            "slug":slug,
            "home":hn,"away":an,"hg":integer(hs),"ag":integer(ass),
            "minute":integer(clock),"clock":str(clock),"status":str(status),
            "league":str(x.get("competition_name") or x.get("competition") or x.get("league") or "Canlı maç"),
            "raw":x}
def sportscore_live():
    d=get(f"{SPORTSCORE}/fixtures/",{"sport":"football","status":"live","limit":200})
    if isinstance(d,list): rows=d
    else: rows=d.get("fixtures") or d.get("matches") or d.get("events") or []
    return [normalize(x) for x in rows if isinstance(x,dict) and live_status(x.get("status_text") or x.get("status") or x.get("state"))]
def espn_live():
    out=[]
    for league in LEAGUES:
        try:
            d=get(f"{ESPN}/{league}/scoreboard")
            for e in d.get("events",[]):
                st=e.get("status") or {}
                if (st.get("type") or {}).get("state")!="in":continue
                c=(e.get("competitions") or [{}])[0]; h=a=None
                for t in c.get("competitors",[]):
                    if t.get("homeAway")=="home":h=t
                    if t.get("homeAway")=="away":a=t
                if not h or not a:continue
                out.append({"id":str(e.get("id")),"slug":"","home":h["team"].get("displayName","Home"),"away":a["team"].get("displayName","Away"),"hg":integer(h.get("score")),"ag":integer(a.get("score")),"minute":integer(st.get("displayClock")),"clock":str(st.get("displayClock","")),"status":"live","league":league,"raw":e,"source":"espn"})
        except Exception as e: print(f"ESPN {league}: {type(e).__name__}")
    return out
def match_stats(d):
    # Read common stat labels from SportScore match payloads, including nested team objects.
    teams=[]
    def walk(obj):
        if isinstance(obj,dict):
            if any(k in obj for k in ("statistics","stats")) and any(k in obj for k in ("home","name","team","team_name","displayName")):teams.append(obj)
            for v in obj.values():walk(v)
        elif isinstance(obj,list):
            for v in obj:walk(v)
    walk(d)
    out=[]
    for t in teams:
        name=t.get("name") or t.get("team_name") or t.get("displayName")
        if isinstance(t.get("team"),dict):name=name or t["team"].get("name") or t["team"].get("displayName")
        vals=t.get("statistics") or t.get("stats") or {}
        if isinstance(vals,list):
            vals={str(v.get("name") or v.get("label") or v.get("type") or "").lower():integer(v.get("value") or v.get("displayValue")) for v in vals if isinstance(v,dict)}
        elif isinstance(vals,dict): vals={str(k).lower():integer(v.get("value") if isinstance(v,dict) else v) for k,v in vals.items()}
        if name:out.append((str(name),vals))
    return out
def run():
    try:state=json.loads(Path("state.json").read_text(encoding="utf-8"))
    except Exception:state={}
    alerts=state.setdefault("alerts",{})
    try:
        matches=sportscore_live(); source="SportScore"
        print(f"SportScore live feed: {len(matches)}; sample={json.dumps(matches[:2],ensure_ascii=False)[:1800]}")
    except Exception as ex:
        print(f"SportScore failed: {type(ex).__name__}: {ex}; trying ESPN fallback")
        matches=espn_live();source="ESPN fallback"
    if not matches:
        print("No live matches returned by current free sources.")
    matches.sort(key=lambda x:x["minute"],reverse=True)
    checked=sent=stats_count=0
    for m in matches:
        if checked>=MAX_DETAILS:break
        if not m["id"]:continue
        checked+=1
        try:
            if source=="SportScore":
                if not m["slug"]:continue
                d=get(f"{SPORTSCORE}/match/",{"sport":"football","slug":m["slug"]})
            else:
                d=get(f"{ESPN}/{m['league']}/summary",{"event":m["id"]})
            if m["minute"]<=2:
                def scan_clock(o):
                    if isinstance(o,dict):
                        for k,v in o.items():
                            if str(k).lower() in ("minute","elapsed","matchminute","currentminute","live_minute"):
                                n=integer(v)
                                if 1<=n<=120:return n
                            n=scan_clock(v)
                            if n:return n
                    elif isinstance(o,list):
                        for v in o:
                            n=scan_clock(v)
                            if n:return n
                    return 0
                detail_clock=scan_clock(d)
                if detail_clock:m["minute"]=detail_clock
                if not m["minute"] or m["minute"]<=2:
                    print("CLOCK UNAVAILABLE: "+m["home"]+" - "+m["away"]+" | status="+m["status"])
            if m["minute"] and not 1<=m["minute"]<=130:continue
            ts=match_stats(d)
            # ESPN fallback stats are available directly on the scoreboard.
            if not ts and source=="ESPN fallback":
                ts=[]
                for t in (m["raw"].get("competitions") or [{}])[0].get("competitors",[]):
                    vals={str(v.get("name") or v.get("abbreviation") or "").lower():integer(v.get("displayValue")) for v in t.get("statistics",[])}
                    ts.append((t.get("team",{}).get("displayName",""),vals))
            if ts:stats_count+=1
            allstats={}
            for name,v in ts:allstats[name.lower()]=v
            hs=next((v for n,v in allstats.items() if n in m["home"].lower() or m["home"].lower() in n),{})
            aws=next((v for n,v in allstats.items() if n in m["away"].lower() or m["away"].lower() in n),{})
            def stat(v,terms):
                for term in terms:
                    for k,x in v.items():
                        if k.strip()==term:return x
                for term in terms:
                    for k,x in v.items():
                        if term in k:return x
                return 0
            shots=stat(hs,("total shots","total_shots","shots","shot"))+stat(aws,("total shots","total_shots","shots","shot"))
            sot=stat(hs,("shots on target","shots_on_target","shots on goal","sog"))+stat(aws,("shots on target","shots_on_target","shots on goal","sog"))
            corners=stat(hs,("corner kicks","corner_kicks","corners","corner"))+stat(aws,("corner kicks","corner_kicks","corners","corner"))
            goals=m["hg"]+m["ag"]
            pressure=sot>=5 or (sot>=4 and shots>=12) or (sot>=3 and shots>=10 and corners>=5)
            late=m["minute"]>=65 and goals<=2 and (sot>=3 or corners>=6)
            watch=m["minute"]>=20 and goals<=3 and abs(m["hg"]-m["ag"])<=1 and (sot>=2 or shots>=8 or corners>=4)
            if not (ts and goals<=4 and abs(m["hg"]-m["ag"])<=1 and (pressure or late or watch)):continue
            last=alerts.get(m["id"])
            if last:
                try:
                    if (now()-datetime.fromisoformat(last)).total_seconds()<COOLDOWN*60:continue
                except Exception:pass
            post("⚽ GOAL RADAR — GOL BASKISI",f"**{m['league']}**\\n**{m['home']} – {m['away']}**\\n⏱️ {m['minute']}' ({m['clock']}) | ⚽ **{m['hg']}-{m['ag']}**\\n📊 Şut: {shots} • İsabetli şut: {sot} • Korner: {corners}\\nKaynak: {source}\\n\\nBu bir canlı istatistik sinyalidir; gol veya kazanç garantisi değildir.")
            alerts[m["id"]]=now().isoformat();sent+=1
            print(f"ALERT SENT: {m['id']} {m['home']} - {m['away']}")
        except Exception as ex:print(f"DETAIL ERROR {m['id']}: {type(ex).__name__}: {ex}")
        time.sleep(.15)
    if matches and stats_count==0:
        last_health=state.get("health_last")
        due=True
        if last_health:
            try:due=(now()-datetime.fromisoformat(last_health)).total_seconds()>=21600
            except Exception:pass
        if due:
            post("🛠️ GOAL RADAR — VERİ UYARISI",f"Canlı maç bulundu: {len(matches)}. Ayrıntılı istatistik alınan: 0. Bu nedenle gol sinyali üretilmedi; veri eksikliğini tahmin gibi göstermiyoruz.",15105570)
            state["health_last"]=now().isoformat()
    state["last_run"]={"at":now().isoformat(),"source":source,"live":len(matches),"checked":checked,"stats_available":stats_count,"sent":sent}
    Path("state.json").write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"RUN COMPLETE: source={source} live={len(matches)} checked={checked} alerts={sent}")
if __name__=="__main__":run()
