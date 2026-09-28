import json, os, re, time
from datetime import datetime, timezone
from pathlib import Path
import requests

WEBHOOK = os.environ["DISCORD_WEBHOOK"]
BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
LEAGUES = ["eng.1","eng.2","esp.1","esp.2","ger.1","ger.2","ita.1","ita.2","fra.1","fra.2","ned.1","por.1","usa.1","mex.1","bra.1","arg.1","uefa.champions","uefa.europa"]
MAX_DETAILS = 12
COOLDOWN = 25
S = requests.Session()
S.headers.update({"User-Agent":"Mozilla/5.0 GoalRadar/2.0","Accept":"application/json"})

def now():
    return datetime.now(timezone.utc)

def get(url, params=None):
    r=S.get(url,params=params,timeout=15); r.raise_for_status(); return r.json()

def post(title, desc, color=3066993):
    r=S.post(WEBHOOK,json={"username":"Goal Radar","embeds":[{"title":title,"description":desc[:4000],"color":color,"timestamp":now().isoformat(),"footer":{"text":"Ücretsiz canlı veri • sinyal garanti değildir"}}]},timeout=15)
    r.raise_for_status()

def integer(v):
    if v is None: return 0
    m=re.search(r"\d+",str(v).replace(",",""))
    return int(m.group()) if m else 0

def teams(e):
    c=(e.get("competitions") or [{}])[0]
    h=a=None
    for x in c.get("competitors",[]):
        if x.get("homeAway")=="home": h=x
        if x.get("homeAway")=="away": a=x
    return h,a

def minute(e):
    st=e.get("status") or {}
    return integer(st.get("displayClock") or (st.get("type") or {}).get("shortDetail"))

def live(e):
    return (e.get("status") or {}).get("type",{}).get("state")=="in"

def score(x):
    return integer(x.get("score"))

def summary_stats(d):
    out={}
    for t in (d.get("boxscore") or {}).get("teams",[]):
        name=(t.get("team") or {}).get("displayName","")
        vals={}
        for x in t.get("statistics",[]) or []:
            label=str(x.get("label") or x.get("name") or "").lower()
            vals[label]=integer(x.get("displayValue") or x.get("value"))
        out[name]=vals
    return out

def find(v, terms):
    for k,x in v.items():
        if any(t in k for t in terms): return x
    return 0

def run():
    try: state=json.loads(Path("state.json").read_text(encoding="utf-8"))
    except Exception: state={}
    alerts=state.setdefault("alerts",{})
    events=[]; errors=[]
    for league in LEAGUES:
        try:
            data=get(f"{BASE}/{league}/scoreboard")
            events += [(league,e) for e in data.get("events",[]) if live(e)]
        except Exception as ex:
            errors.append(f"{league}: {type(ex).__name__}")
    print(f"LIVE MATCHES: {len(events)}; SOURCE ERRORS: {len(errors)}")
    checked=sent=0
    events.sort(key=lambda x: minute(x[1]),reverse=True)
    for league,e in events:
        if checked>=MAX_DETAILS: break
        m=minute(e)
        if m<1 or m>95: continue
        h,a=teams(e)
        if not h or not a: continue
        checked+=1
        eid=str(e.get("id") or "")
        if not eid: continue
        try:
            d=get(f"{BASE}/{league}/summary",{"event":eid})
            stats=summary_stats(d)
            hn=(h.get("team") or {}).get("displayName","Home")
            an=(a.get("team") or {}).get("displayName","Away")
            hs=stats.get(hn,{}); aws=stats.get(an,{})
            shots=find(hs,["total shots","shots"])+find(aws,["total shots","shots"])
            sot=find(hs,["shots on target","shots on goal"])+find(aws,["shots on target","shots on goal"])
            corners=find(hs,["won corners","corners"])+find(aws,["won corners","corners"])
            goals=score(h)+score(a)
            pressure=(sot>=5 or (sot>=4 and shots>=12) or (sot>=3 and shots>=10 and corners>=5))
            late=(m>=65 and goals<=2 and (sot>=3 or corners>=6))
            if not (goals<=4 and abs(score(h)-score(a))<=1 and (pressure or late)): continue
            last=alerts.get(eid)
            if last:
                try:
                    if (now()-datetime.fromisoformat(last)).total_seconds()<COOLDOWN*60: continue
                except Exception: pass
            reason=f"İsabetli şut: {sot} • Şut: {shots} • Korner: {corners}"
            post("⚽ GOAL RADAR — GOL BASKISI",f"**{league}**\\n**{hn} – {an}**\\n⏱️ {m}' | ⚽ **{score(h)}-{score(a)}**\\n📊 {reason}\\n\\nBu bir istatistik sinyalidir; gol veya kazanç garantisi değildir.")
            alerts[eid]=now().isoformat(); sent+=1
            print(f"ALERT SENT: {eid} {hn} - {an}")
        except Exception as ex:
            print(f"DETAIL ERROR {eid}: {type(ex).__name__}: {ex}")
        time.sleep(.15)
    state["last_run"]={"at":now().isoformat(),"live":len(events),"checked":checked,"sent":sent,"errors":errors[:20]}
    Path("state.json").write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"RUN COMPLETE: live={len(events)} checked={checked} alerts={sent}")

if __name__=="__main__": run()
