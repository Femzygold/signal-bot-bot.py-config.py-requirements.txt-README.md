import os, json, time, secrets, requests, threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify, make_response
from collections import deque

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = "8668028976:AAE2u1in1KGr1nRTJbaQXNPeDtMO35unoQ8"
TELEGRAM_CHAT_ID   = "7411219487"
DASHBOARD_PASSWORD = "signal123"
# ── MEXC API KEYS (for auto-trading) ─────────────────────────────
MEXC_API_KEY    = "mx0vglgGjqnoPDiTFu"   # paste your MEXC API key here
MEXC_API_SECRET = "e13578211318499baa3852677365d3cb"   # paste your MEXC secret key here

MAX_SIGNALS = 500
signals     = deque(maxlen=MAX_SIGNALS)
sessions    = set()

# ── MEXC AUTO-TRADE CONFIG ────────────────────────────────────────────
trade_config = {
    "enabled":       False,        # master on/off
    "api_key":       MEXC_API_KEY,
    "api_secret":    MEXC_API_SECRET,
    "risk_pct":      1.0,          # % of account balance per trade
    "max_trades":    3,            # max open positions at once
    "leverage":      10,           # default leverage
}
open_trades  = {}                  # symbol -> trade info
trade_lock   = threading.Lock()
MEXC_FUTURES = "https://contract.mexc.com/api/v1/private"

scan_state = {
    "running": False, "enabled": True, "current_pair": "",
    "pairs_done": 0, "total_pairs": 0, "scan_count": 0,
    "signals_found": 0, "last_scan": None,
    "log": deque(maxlen=100),
}
scan_lock = threading.Lock()

TOP_PAIRS = ["BTC_USDT","ETH_USDT","SOL_USDT","BNB_USDT","XRP_USDT","DOGE_USDT"]
MEXC_BASE = "https://contract.mexc.com/api/v1/contract"
CRT_TFS   = ["Day1","Hour4","Hour3","Hour2","Min60"]
OB_TFS    = ["Hour4","Hour3","Hour2","Min60","Min45"]
TBS_TFS   = ["Min30","Min15","Min10","Min5"]

# ════════ MEXC API ═══════════════════════════════════════════════════

def get_all_pairs():
    try:
        r = requests.get(f"{MEXC_BASE}/detail", timeout=15)
        data = r.json()
        if not data.get("success"): return []
        pairs = []
        for item in data.get("data", []):
            sym = item.get("symbol","")
            if item.get("state") == 0 and sym.endswith("_USDT"):
                pairs.append(sym)
        return sorted(pairs)
    except Exception as e:
        log(f"Pairs error: {e}"); return []

def get_candles(symbol, interval, limit=150):
    try:
        r = requests.get(f"{MEXC_BASE}/kline/{symbol}",
                         params={"interval":interval,"limit":limit}, timeout=10)
        data = r.json()
        if not data.get("success") or not data.get("data"): return []
        raw = data["data"]
        out = []
        times=raw.get("time",[]); opens=raw.get("open",[])
        highs=raw.get("high",[]); lows=raw.get("low",[]); closes=raw.get("close",[])
        for i in range(len(times)):
            try:
                out.append({"time":int(times[i]),"open":float(opens[i]),
                            "high":float(highs[i]),"low":float(lows[i]),"close":float(closes[i])})
            except: continue
        return out
    except: return []

def get_ticker(symbol):
    try:
        r = requests.get(f"{MEXC_BASE}/ticker", params={"symbol":symbol}, timeout=6)
        data = r.json()
        if data.get("success") and data.get("data"):
            d = data["data"]
            if isinstance(d, list): d = d[0]
            return {"price":float(d.get("lastPrice",0)),"change":float(d.get("priceChangePercent",0)),
                    "high":float(d.get("high24h",0)),"low":float(d.get("low24h",0))}
    except: pass
    return None

# ════════ MARKET STRUCTURE ═══════════════════════════════════════════

def find_swings(candles, n=2):
    highs=[c["high"] for c in candles]; lows=[c["low"] for c in candles]
    sh=[]; sl=[]
    for i in range(n, len(candles)-n):
        if all(highs[i]>=highs[i-j] and highs[i]>=highs[i+j] for j in range(1,n+1)):
            sh.append((i,highs[i]))
        if all(lows[i]<=lows[i-j] and lows[i]<=lows[i+j] for j in range(1,n+1)):
            sl.append((i,lows[i]))
    return sh, sl

def detect_trend(candles, lookback=80):
    c = candles[-lookback:] if len(candles)>=lookback else candles
    if len(c)<20: return "NEUTRAL",[],[]
    sh,sl = find_swings(c,n=2)
    if len(sh)>=2 and len(sl)>=2:
        hh=sh[-1][1]>sh[-2][1]; hl=sl[-1][1]>sl[-2][1]
        lh=sh[-1][1]<sh[-2][1]; ll=sl[-1][1]<sl[-2][1]
        if hh and hl: return "BULLISH",sh,sl
        if lh and ll: return "BEARISH",sh,sl
    closes=[c["close"] for c in c[-20:]]
    a1=sum(closes[:10])/10; a2=sum(closes[10:])/10
    if a2>a1*1.003: return "BULLISH",sh,sl
    if a2<a1*0.997: return "BEARISH",sh,sl
    return "NEUTRAL",sh,sl

def is_continuous(sh, sl, direction, min_pts=2):
    """
    Accept structure if EITHER highs OR lows are printing correctly.
    Full alignment (both) = stronger signal (higher score).
    This stops perfectly valid setups being killed by one slightly off swing.
    """
    if direction=="BULLISH":
        if len(sh)<min_pts and len(sl)<min_pts: return False
        highs_ok = len(sh)>=min_pts and all(sh[i][1]>sh[i-1][1] for i in range(1,len(sh)))
        lows_ok  = len(sl)>=min_pts and all(sl[i][1]>sl[i-1][1] for i in range(1,len(sl)))
        return highs_ok or lows_ok   # at least one must be true
    else:
        if len(sh)<min_pts and len(sl)<min_pts: return False
        highs_ok = len(sh)>=min_pts and all(sh[i][1]<sh[i-1][1] for i in range(1,len(sh)))
        lows_ok  = len(sl)>=min_pts and all(sl[i][1]<sl[i-1][1] for i in range(1,len(sl)))
        return highs_ok or lows_ok

# ════════ ORDER BLOCKS ════════════════════════════════════════════════

def find_obs(candles, direction):
    obs = []
    if len(candles)<5: return obs
    for i in range(2, len(candles)-2):
        c=candles[i]; cn=candles[i+1]
        if direction=="BULLISH":
            if c["close"]<c["open"] and cn["close"]>c["high"] and cn["close"]>cn["open"]:
                obs.append({"top":c["open"],"bot":c["close"],"high":c["high"],"low":c["low"],"idx":i,"time":c["time"],"type":"BULLISH_OB"})
        else:
            if c["close"]>c["open"] and cn["close"]<c["low"] and cn["close"]<cn["open"]:
                obs.append({"top":c["close"],"bot":c["open"],"high":c["high"],"low":c["low"],"idx":i,"time":c["time"],"type":"BEARISH_OB"})
    return sorted(obs, key=lambda x:x["idx"], reverse=True)

def ob_at_key_level(ob, direction, sh, sl, tol=0.025):
    """
    OB must be near the last Higher Low (bull) or Lower High (bear).
    Tolerance = 2.5%. Checks last 2 swing levels for flexibility.
    """
    if direction=="BULLISH" and sl:
        for _, last_hl in sl[-2:]:
            if ob["bot"] <= last_hl*(1+tol) and ob["top"] >= last_hl*(1-tol):
                return True
    elif direction=="BEARISH" and sh:
        for _, last_lh in sh[-2:]:
            if ob["top"] >= last_lh*(1-tol) and ob["bot"] <= last_lh*(1+tol):
                return True
    return False


def ob_in_pd_zone(ob, candles, direction):
    """OB must be in Discount zone (bull) or Premium zone (bear)."""
    if not candles or len(candles)<20: return False,"UNKNOWN"
    recent = candles[-50:]
    swing_high = max(c["high"] for c in recent)
    swing_low  = min(c["low"]  for c in recent)
    full_range = swing_high - swing_low
    if full_range<=0: return False,"UNKNOWN"
    eq = swing_low + full_range*0.5
    ob_mid = (ob["top"]+ob["bot"])/2
    if direction=="BULLISH":
        return ob_mid<eq, ("DISCOUNT" if ob_mid<eq else "PREMIUM")
    else:
        return ob_mid>eq, ("PREMIUM" if ob_mid>eq else "DISCOUNT")

def fvg_in_pd_zone(candles, direction):
    """
    Find a Fair Value Gap on the HTF that is in the correct
    discount (bullish) or premium (bearish) zone.
    Returns (found, fvg_top, fvg_bot, zone_name)
    The CRT must form INSIDE this FVG zone.
    """
    if not candles or len(candles)<20: return False, None, None, "UNKNOWN"
    recent = candles[-50:]
    swing_high = max(c["high"] for c in recent)
    swing_low  = min(c["low"]  for c in recent)
    full_range = swing_high - swing_low
    if full_range <= 0: return False, None, None, "UNKNOWN"
    eq = swing_low + full_range * 0.5

    # Find FVGs on the HTF candles
    for i in range(len(candles)-3, max(0, len(candles)-40), -1):
        c1 = candles[i]
        c3 = candles[i+2]
        if direction == "BULLISH":
            # Bullish FVG: gap between c1.high and c3.low
            if c3["low"] > c1["high"]:
                fvg_bot = c1["high"]
                fvg_top = c3["low"]
                fvg_mid = (fvg_top + fvg_bot) / 2
                # Must be in DISCOUNT zone (below 50%)
                if fvg_mid < eq:
                    return True, fvg_top, fvg_bot, "DISCOUNT (FVG)"
        else:
            # Bearish FVG: gap between c3.high and c1.low
            if c3["high"] < c1["low"]:
                fvg_top = c1["low"]
                fvg_bot = c3["high"]
                fvg_mid = (fvg_top + fvg_bot) / 2
                # Must be in PREMIUM zone (above 50%)
                if fvg_mid > eq:
                    return True, fvg_top, fvg_bot, "PREMIUM (FVG)"
    return False, None, None, "UNKNOWN"

def find_breaker_block(candles, direction):
    """
    Breaker Block (BB): a previously valid OB that was broken through.
    Bullish BB: old bearish OB that price broke above (now support).
    Bearish BB: old bullish OB that price broke below (now resistance).
    Returns list of BB dicts.
    """
    bbs = []
    if len(candles) < 10: return bbs
    obs = find_obs(candles, "BULLISH" if direction=="BULLISH" else "BEARISH")
    for ob in obs:
        idx = ob["idx"]
        after = candles[idx+1:]
        if direction == "BULLISH":
            # Old bearish OB broken above = Bullish BB
            broken = any(c["close"] > ob["top"] for c in after[:8])
            if broken:
                bbs.append({**ob, "type": "BB", "kl_type": "Breaker Block"})
        else:
            broken = any(c["close"] < ob["bot"] for c in after[:8])
            if broken:
                bbs.append({**ob, "type": "BB", "kl_type": "Breaker Block"})
    return bbs

def find_rejection_block(candles, direction):
    """
    Rejection Block (RJB): candle with a large wick rejected from a level,
    body is small relative to the wick.
    """
    rjbs = []
    if len(candles) < 5: return rjbs
    for i in range(2, len(candles)-2):
        c = candles[i]
        body = abs(c["close"] - c["open"])
        total = c["high"] - c["low"]
        if total <= 0: continue
        wick_ratio = (total - body) / total
        if direction == "BULLISH":
            lower_wick = c["close"] - c["low"] if c["close"] > c["open"] else c["open"] - c["low"]
            if wick_ratio > 0.65 and lower_wick > body * 2:
                rjbs.append({"top": c["high"], "bot": c["low"],
                              "high": c["high"], "low": c["low"],
                              "idx": i, "time": c["time"],
                              "type": "RJB", "kl_type": "Rejection Block"})
        else:
            upper_wick = c["high"] - c["close"] if c["close"] < c["open"] else c["high"] - c["open"]
            if wick_ratio > 0.65 and upper_wick > body * 2:
                rjbs.append({"top": c["high"], "bot": c["low"],
                              "high": c["high"], "low": c["low"],
                              "idx": i, "time": c["time"],
                              "type": "RJB", "kl_type": "Rejection Block"})
    return sorted(rjbs, key=lambda x: x["idx"], reverse=True)

def find_all_key_levels(candles, direction):
    """
    Find ALL valid key levels: OB, BB, FVG, IFVG, RJB.
    Returns list of zone dicts with kl_type field.
    """
    zones = []
    # Order Blocks
    for ob in find_obs(candles, "BULLISH" if direction=="BULLISH" else "BEARISH")[:5]:
        zones.append({**ob, "kl_type": "OB"})
    # Breaker Blocks
    for bb in find_breaker_block(candles, direction)[:3]:
        zones.append(bb)
    # Rejection Blocks
    for rjb in find_rejection_block(candles, direction)[:3]:
        zones.append(rjb)
    # FVG zones (raw HTF)
    for i in range(max(0, len(candles)-40), len(candles)-3):
        c1 = candles[i]; c3 = candles[i+2]
        if direction == "BULLISH" and c3["low"] > c1["high"]:
            zones.append({"top": c3["low"], "bot": c1["high"],
                          "high": c3["low"], "low": c1["high"],
                          "idx": i, "time": c1["time"], "kl_type": "FVG"})
        elif direction == "BEARISH" and c3["high"] < c1["low"]:
            zones.append({"top": c1["low"], "bot": c3["high"],
                          "high": c1["low"], "low": c3["high"],
                          "idx": i, "time": c1["time"], "kl_type": "IFVG"})
    return sorted(zones, key=lambda x: x.get("idx",0), reverse=True)

def crt_inside_zone(crt, zone_top, zone_bot):
    """Check if CRT candles overlap with the HTF OB or FVG zone."""
    crh = crt["crh"]; crl = crt["crl"]
    # CRT range must overlap the zone
    return crl <= zone_top and crh >= zone_bot

def prev_obs_respected(obs, candles, direction, min_resp=1):
    """Check at least 1 previous OB got respected (price reacted from it)."""
    if len(obs)<2: return False
    respected=0
    for ob in obs[1:]:
        after = candles[ob["idx"]+1 : ob["idx"]+10]
        if not after: continue
        if direction=="BULLISH":
            tap    = any(c["low"]<=ob["top"] for c in after[:4])
            react  = any(c["close"]>ob["top"]*1.002 for c in after)
            if tap and react: respected+=1
        else:
            tap    = any(c["high"]>=ob["bot"] for c in after[:4])
            react  = any(c["close"]<ob["bot"]*0.998 for c in after)
            if tap and react: respected+=1
    return respected>=min_resp

def liq_sweep_before_ob(candles, ob, direction):
    """Liquidity sweep (wick OR body) before OB forms."""
    idx = ob["idx"]
    lb  = candles[max(0,idx-20):idx]
    if not lb: return False
    if direction=="BULLISH":
        prev_low = min(c["low"] for c in lb[:-1]) if len(lb)>1 else lb[0]["low"]
        return any(c["low"]<prev_low for c in lb[-8:])
    else:
        prev_high = max(c["high"] for c in lb[:-1]) if len(lb)>1 else lb[0]["high"]
        return any(c["high"]>prev_high for c in lb[-8:])

def price_tapping_ob(candles, ob, direction):
    """Price tapping OB in the last 14 candles."""
    recent = candles[-14:]
    if direction=="BULLISH":
        return any(c["low"]<=ob["top"] and c["high"]>=ob["bot"] for c in recent)
    else:
        return any(c["high"]>=ob["bot"] and c["low"]<=ob["top"] for c in recent)

# ════════ CRT DETECTION ══════════════════════════════════════════════

def detect_crt(candles, direction, ob=None):
    """
    Detect 3-candle CRT formation.
    If ob is provided, C1 must be rooted inside the OB zone.
    Returns list of valid CRT dicts with RR >= 3.
    """
    found = []
    if len(candles)<5: return found
    limit = min(20, len(candles)-2)
    for offset in range(1, limit):
        i3=len(candles)-1-offset; i2=i3-1; i1=i2-1
        if i1<0: break
        c1=candles[i1]; c2=candles[i2]; c3=candles[i3]
        crh=c1["high"]; crl=c1["low"]; cr_range=crh-crl
        if cr_range<=0: continue
        # If OB given, C1 must overlap OB
        if ob:
            if not (c1["low"]<=ob["top"] and c1["high"]>=ob["bot"]):
                continue
        if direction=="BULLISH":
            swept     = c2["low"] < crl
            c2_inside = crl <= c2["close"] <= crh
            wick_ok   = (c2["close"]-c2["low"]) > cr_range*0.03
            c3_bull   = c3["close"] > c3["open"]
            if swept and c2_inside and wick_ok:
                entry=c2["close"]; sl=c2["low"]; tp=crh
                risk=abs(entry-sl); reward=abs(tp-entry)
                rr=round(reward/risk,2) if risk>0 else 0
                if rr>=3.0:
                    found.append({"direction":"BUY","c1":c1,"c2":c2,"c3":c3,
                                  "crh":crh,"crl":crl,"entry":round(entry,8),
                                  "sl":round(sl,8),"tp":round(tp,8),"rr":rr,
                                  "sweep":round(crl-c2["low"],8),"c3_confirms":c3_bull})
        else:
            swept     = c2["high"] > crh
            c2_inside = crl <= c2["close"] <= crh
            wick_ok   = (c2["high"]-c2["close"]) > cr_range*0.03
            c3_bear   = c3["close"] < c3["open"]
            if swept and c2_inside and wick_ok:
                entry=c2["close"]; sl=c2["high"]; tp=crl
                risk=abs(sl-entry); reward=abs(entry-tp)
                rr=round(reward/risk,2) if risk>0 else 0
                if rr>=3.0:
                    found.append({"direction":"SELL","c1":c1,"c2":c2,"c3":c3,
                                  "crh":crh,"crl":crl,"entry":round(entry,8),
                                  "sl":round(sl,8),"tp":round(tp,8),"rr":rr,
                                  "sweep":round(c2["high"]-crh,8),"c3_confirms":c3_bear})
    return found

# ════════ TBS ════════════════════════════════════════════════════════

def check_tbs(symbol, direction, crl, crh, crt_tf="Hour4"):
    """
    Turtle Body Soup — Model #1 entry.
    Uses the correct mapped LTF per CRT timeframe:
      1H CRT  -> 1m | 2H CRT -> 2-3m | 3H/4H CRT -> 3-4m | 1D CRT -> 45m-1H
    Bullish: body CLOSES below CRL, next candle recovers above CRL.
    Bearish: body CLOSES above CRH, next candle recovers below CRH.
    Entry = OPEN of TBS candle. SL = extreme of TBS candle.
    Returns: (found, tf, tbs_entry, tbs_sl)
    """
    tfs_to_check = TBS_TF_MAP.get(crt_tf, TBS_TFS)
    for tf in tfs_to_check:
        candles = get_candles(symbol, tf, limit=120)
        if not candles or len(candles)<4: continue
        recent = candles[-80:]
        for i in range(len(recent)-1):
            c   = recent[i]
            nxt = recent[i+1]
            if direction=="BUY":
                if c["close"] < crl and nxt["close"] > crl:
                    return True, tf, round(c["open"],8), round(c["low"],8)
            else:
                if c["close"] > crh and nxt["close"] < crh:
                    return True, tf, round(c["open"],8), round(c["high"],8)
    return False, None, None, None

# ════════ CHOCH ═══════════════════════════════════════════════════════

def check_choch(symbol, tf, direction):
    """Change of Character — structural break on LTF."""
    candles = get_candles(symbol, tf, limit=60)
    if not candles or len(candles)<5: return False, None
    recent = candles[-35:]
    sh=[]; sl=[]
    for i in range(2, len(recent)-2):
        c=recent[i]
        if (c["high"]>recent[i-1]["high"] and c["high"]>recent[i-2]["high"] and
            c["high"]>recent[i+1]["high"] and c["high"]>=recent[i+2]["high"]):
            sh.append((i,c["high"]))
        if (c["low"]<recent[i-1]["low"] and c["low"]<recent[i-2]["low"] and
            c["low"]<recent[i+1]["low"] and c["low"]<=recent[i+2]["low"]):
            sl.append((i,c["low"]))
    if direction=="BUY":
        if not sh:
            for i in range(len(recent)-1,0,-1):
                c=recent[i]; p=recent[i-1]
                if c["close"]>p["high"] and c["close"]>c["open"]:
                    return True, round(p["high"],8)
            return False,None
        last_idx,last_val = sh[-1]
        for i in range(last_idx+1,len(recent)):
            c=recent[i]
            if c["close"]>last_val and c["close"]>c["open"]:
                return True, round(last_val,8)
    else:
        if not sl:
            for i in range(len(recent)-1,0,-1):
                c=recent[i]; p=recent[i-1]
                if c["close"]<p["low"] and c["close"]<c["open"]:
                    return True, round(p["low"],8)
            return False,None
        last_idx,last_val = sl[-1]
        for i in range(last_idx+1,len(recent)):
            c=recent[i]
            if c["close"]<last_val and c["close"]<c["open"]:
                return True, round(last_val,8)
    return False,None

# ════════ FVG + IFVG ══════════════════════════════════════════════════

def find_fvg(symbol, tf, direction):
    """
    Entry at the TIP of the FVG/IFVG.
    Bullish FVG tip = C1.high (bottom of gap, first touch entering from below).
    Bearish FVG tip = C1.low  (top of gap, first touch entering from above).
    IFVG = previously filled FVG, now flipped S/R.
    """
    candles = get_candles(symbol, tf, limit=80)
    if not candles or len(candles)<5: return False,None,None,None,None
    fresh=[]; ifvg=[]
    for i in range(len(candles)-3):
        c1=candles[i]; c3=candles[i+2]
        if direction=="BUY":
            if c3["low"]>c1["high"]:
                zbot=c1["high"]; ztop=c3["low"]
                mit=any(candles[j]["low"]<=ztop for j in range(i+3,len(candles)))
                if not mit:
                    fresh.append({"type":"FVG","entry":round(zbot,8),
                                  "zone_top":round(ztop,8),"zone_bot":round(zbot,8),"idx":i})
                else:
                    ifvg.append({"type":"IFVG","entry":round(ztop,8),
                                 "zone_top":round(ztop,8),"zone_bot":round(zbot,8),"idx":i})
        else:
            if c3["high"]<c1["low"]:
                ztop=c1["low"]; zbot=c3["high"]
                mit=any(candles[j]["high"]>=zbot for j in range(i+3,len(candles)))
                if not mit:
                    fresh.append({"type":"FVG","entry":round(ztop,8),
                                  "zone_top":round(ztop,8),"zone_bot":round(zbot,8),"idx":i})
                else:
                    ifvg.append({"type":"IFVG","entry":round(zbot,8),
                                 "zone_top":round(ztop,8),"zone_bot":round(zbot,8),"idx":i})
    if fresh:
        b=max(fresh,key=lambda x:x["idx"])
        return True,b["type"],b["entry"],b["zone_top"],b["zone_bot"]
    if ifvg:
        b=max(ifvg,key=lambda x:x["idx"])
        return True,b["type"],b["entry"],b["zone_top"],b["zone_bot"]
    return False,None,None,None,None

# ════════ MULTI-TF HELPERS ════════════════════════════════════════════

def check_choch_multi(symbol, tfs, direction):
    """Try CHOCH across multiple LTFs, return first found."""
    for tf in tfs:
        found, level = check_choch(symbol, tf, direction)
        if found and level:
            return True, level
    return False, None

def find_fvg_multi(symbol, tfs, direction):
    """Try FVG/IFVG across multiple LTFs, return best (most recent)."""
    for tf in tfs:
        found, fvg_type, fvg_entry, fvg_top, fvg_bot = find_fvg(symbol, tf, direction)
        if found and fvg_entry:
            return found, fvg_type, fvg_entry, fvg_top, fvg_bot
    return False, None, None, None, None

# ════════ SIGNAL SCORING ═════════════════════════════════════════════

def score_signal(crt, trend, liq_swept, tbs_found, tbs_tf,
                 fvg_found, fvg_type, choch_found, continuous,
                 is_1d, ob=None, at_key=False, ob_resp=False, ob_zone=None,
                 sh=None, sl=None, direction=None):
    score=0; details=[]
    if direction is None: direction=crt["direction"]
    if sh is None: sh=[]
    if sl is None: sl=[]
    rr=crt["rr"]

    # 1. Structure quality (20 pts)
    # Full alignment (both HH/HL or LH/LL) = 20pts, partial = 12pts
    if continuous:
        sh_ok = len(sh)>=2 and ((direction=="BUY" and all(sh[i][1]>sh[i-1][1] for i in range(1,len(sh)))) or (direction=="SELL" and all(sh[i][1]<sh[i-1][1] for i in range(1,len(sh)))))
        sl_ok = len(sl)>=2 and ((direction=="BUY" and all(sl[i][1]>sl[i-1][1] for i in range(1,len(sl)))) or (direction=="SELL" and all(sl[i][1]<sl[i-1][1] for i in range(1,len(sl)))))
        if sh_ok and sl_ok:
            score+=20; details.append("✅ Full HH/HL or LH/LL structure (+20)")
        else:
            score+=12; details.append("⚠️ Partial structure alignment (+12)")
    else:
        details.append("⚠️ Weak structure (+0)")

    # 2. Trend alignment (10 pts)
    if (direction=="BUY" and trend=="BULLISH") or (direction=="SELL" and trend=="BEARISH"):
        score+=10; details.append("✅ Trend aligned (+10)")
    else:
        details.append("❌ Counter-trend (+0)")

    # 3. TBS — mandatory gatekeeper (20 pts)
    if tbs_found:
        score+=20; details.append(f"✅ TBS body close on {tbs_tf} (+20)")
    else:
        details.append("❌ No TBS — gate failed (+0)")

    # 4. Liquidity sweep (15 pts)
    if liq_swept:
        score+=15; details.append("✅ Liquidity sweep confirmed (+15)")
    else:
        details.append("⚠️ No liquidity sweep (+0)")

    # 5. RR quality (10 pts)
    if rr>=5:   score+=10; details.append(f"✅ Exceptional {rr}R (+10)")
    elif rr>=4: score+=8;  details.append(f"✅ Strong {rr}R (+8)")
    elif rr>=3: score+=6;  details.append(f"⚠️ Minimum {rr}R (+6)")

    # 6. CHOCH (10 pts)
    if choch_found:
        score+=10; details.append("✅ CHOCH confirmed (+10)")
    else:
        details.append("⚠️ No CHOCH (+0)")

    # 7. FVG/IFVG (10 pts)
    if fvg_found:
        score+=10; details.append(f"✅ {fvg_type} entry tip found (+10)")
    else:
        details.append("⚠️ No FVG/IFVG (+0)")

    if not is_1d:
        # 8. Key level type bonus
        kl = str(ob_zone) if ob_zone else ""
        if "OB" in kl:   score+=6; details.append("✅ Order Block confluence (+6)")
        elif "BB" in kl: score+=7; details.append("✅ Breaker Block confluence (+7)")
        elif "FVG" in kl or "IFVG" in kl: score+=5; details.append("✅ FVG/IFVG confluence (+5)")
        elif "RJB" in kl: score+=4; details.append("✅ Rejection Block confluence (+4)")
        # 9. P/D zone bonus (upgrades to A+)
        has_pd = "DISCOUNT" in kl or "PREMIUM" in kl
        if has_pd:
            score+=8; details.append(f"⭐ Premium/Discount zone — A+ eligible (+8)")
        # 10. Key level at swing (at_key)
        if at_key:
            score+=5; details.append("✅ Key level at swing point (+5)")
        # 11. Previous OBs respected
        if ob_resp:
            score+=4; details.append("✅ Previous key levels respected (+4)")

    # Triple confluence bonus: TBS + FVG + CHOCH all confirmed (8 pts)
    if tbs_found and fvg_found and choch_found:
        score=min(score+8,100); details.append("✅ Triple confluence: TBS+FVG+CHOCH (+8)")

    # C3 confirms direction (5 pts bonus)
    if crt.get("c3_confirms"):
        score=min(score+5,100); details.append("✅ C3 confirms (+5)")

    # A+ requires P/D zone confluence (passed via ob_zone containing "DISCOUNT" or "PREMIUM")
    has_pd = ob_zone and ("DISCOUNT" in str(ob_zone) or "PREMIUM" in str(ob_zone))
    if score>=88 and has_pd and tbs_found and (fvg_found or choch_found):
        grade = "A+"
    elif score>=88:
        grade = "A"
    elif score>=75:
        grade = "A"
    elif score>=60:
        grade = "B"
    elif score>=45:
        grade = "C"
    else:
        grade = "D"
    return min(score,100), grade, details

# ════════ TELEGRAM ════════════════════════════════════════════════════

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or "PASTE" in TELEGRAM_BOT_TOKEN: return False
    try:
        r=requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        json={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"HTML"},timeout=10)
        return r.status_code==200
    except: return False

def fmt_tg(sig):
    e    = "🟢" if sig["direction"]=="BUY" else "🔴"
    bars = "█"*(sig["score"]//10)+"░"*(10-sig["score"]//10)
    tbs  = f"✅ {sig.get('tbs_tf','–')}" if sig.get("tbs_found") else "❌"
    fvg  = f"✅ {sig.get('fvg_type','–')}" if sig.get("fvg_found") else "⚠️ None"
    choch= "✅" if sig.get("choch_found") else "⚠️"
    tf_label = {"Day1":"1D","Hour4":"4H","Hour3":"3H","Hour2":"2H","Min60":"1H"}.get(sig.get("tf",""),"–")
    ob_info  = f"\n<b>OB TF:</b>      {sig.get('ob_tf','–')} | {sig.get('ob_zone','–')}" if sig.get("ob_tf") and sig.get("ob_tf") not in ("N/A","N/A (1D)","–") else ""
    return (
        f"{e} <b>CRT MODEL #1 — {sig['direction']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Pair:</b>       {sig['symbol']}\n"
        f"<b>CRT TF:</b>     {tf_label}{ob_info}\n"
        f"<b>Trend:</b>      {sig['trend']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>🎯 Entry:</b>    {sig['entry']}\n"
        f"<b>   Type:</b>     Model #1 (TBS Candle Open)\n"
        f"<b>   TBS TF:</b>   {sig.get('tbs_tf','–')}\n"
        f"<b>🛑 SL:</b>       {sig['sl']} (Sweep Extreme)\n"
        f"<b>🎯 TP:</b>       {sig['tp']} ({'CRH' if sig['direction']=='SELL' else 'CRH'})\n"
        f"<b>📊 RR:</b>       {sig['rr']}R\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>CRH:</b>        {sig['crh']}\n"
        f"<b>CRL:</b>        {sig['crl']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Score:</b>      {sig['score']}/100 [{bars}] {sig['grade']}\n"
        f"<b>TBS:</b>        {tbs}\n"
        f"<b>FVG:</b>        {fvg} (confluence)\n"
        f"<b>CHOCH:</b>      {choch} (confluence)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>CRT Scanner Model #1 • {sig['timestamp']}</i>"
    )

# ════════ LOGGER ══════════════════════════════════════════════════════

def log(msg):
    ts=datetime.now(timezone.utc).strftime("%H:%M:%S")
    line=f"[{ts}] {msg}"; print(line)
    with scan_lock: scan_state["log"].appendleft(line)

# ════════ SCAN PAIR ═══════════════════════════════════════════════════

def get_ltf_for_crt(crt_tf):
    """Return the best LTF for CHOCH and FVG based on CRT timeframe."""
    return {
        "Day1":  "Min60",
        "Hour4": "Min15",
        "Hour3": "Min15",
        "Hour2": "Min10",
        "Min60": "Min5",
    }.get(crt_tf, "Min15")

# ── MANIPULATION MONITOR ─────────────────────────────────────────
# Tracks pairs where C1 (CRT) is confirmed and C2 (manipulation) is forming
# Once C2 closes back inside → search for TBS → auto-trade
manip_monitor = {}   # symbol -> monitor dict
manip_lock    = threading.Lock()
MAX_MONITORED = 4

# ── RECENT TRADES STORE ──────────────────────────────────────────
recent_trades = deque(maxlen=10)   # last 10 completed trades

# Global diagnostic counters
diag = {
    "no_candles":0, "neutral":0, "not_continuous":0,
    "no_obs":0, "not_at_key":0, "not_in_zone":0,
    "not_tapping":0, "no_crts":0,
    "no_tbs":0, "rr_low":0, "passed":0,
    "1d_no_crts":0, "1d_no_tbs":0, "1d_rr_low":0,
}

def scan_pair(symbol):
    results = []

    ref_candles = get_candles(symbol, "Hour4", limit=200)
    if not ref_candles or len(ref_candles)<30:
        diag["no_candles"]+=1; return results

    trend, sh, sl = detect_trend(ref_candles)
    if trend=="NEUTRAL":
        diag["neutral"]+=1; return results

    continuous = is_continuous(sh, sl, trend, min_pts=2)
    if not continuous:
        diag["not_continuous"]+=1; return results

    direction = "BUY" if trend=="BULLISH" else "SELL"

    # ── PATH A: 1D CRT (no OB required) ─────────────────────────────
    candles_1d = get_candles(symbol, "Day1", limit=120)
    if candles_1d and len(candles_1d)>=10:
        crts = detect_crt(candles_1d, direction, ob=None)
        if not crts:
            diag["1d_no_crts"]+=1
        for crt in crts:
            # Liq sweep check on 1D
            fake_ob = {"idx": len(candles_1d)-3, "top": crt["crh"], "bot": crt["crl"]}
            liq = liq_sweep_before_ob(candles_1d, fake_ob, direction)

            # TBS on LTFs — Model #1: entry at TBS candle open
            tbs_found, tbs_tf, tbs_entry, tbs_sl = check_tbs(symbol, direction, crt["crl"], crt["crh"])
            if not tbs_found:
                diag["1d_no_tbs"]+=1; continue

            # Model #1 entry: open of TBS candle
            # SL: sweep extreme (TBS candle low/high)
            # TP: opposite CRT level
            if tbs_entry:
                entry      = tbs_entry
                entry_type = "Model #1 (TBS Open)"
                sl_p       = tbs_sl          # SL at the sweep
                tp_p       = crt["tp"]       # TP at CRH (for BUY) or CRL (for SELL)
            else:
                entry      = crt["entry"]
                entry_type = "C2 Close"
                sl_p       = crt["sl"]
                tp_p       = crt["tp"]

            # CHOCH still useful for confluence scoring
            choch_found, choch_level = check_choch_multi(symbol, ["Min60","Min30"], direction)
            # FVG kept for scoring only, not entry
            fvg_found, fvg_type, fvg_entry, fvg_top, fvg_bot = find_fvg_multi(symbol, ["Min60","Min30"], direction)
            risk=abs(entry-sl_p); reward=abs(tp_p-entry)
            rr=round(reward/risk,2) if risk>0 else 0
            if rr<2.0:
                diag["1d_rr_low"]+=1; continue

            crt_s=dict(crt); crt_s["entry"]=entry; crt_s["rr"]=rr
            score,grade,details=score_signal(
                crt_s,trend,liq,tbs_found,tbs_tf,
                fvg_found,fvg_type,choch_found,continuous,is_1d=True,
                sh=sh,sl=sl,direction=direction)

            results.append({
                "symbol":symbol,"tf":"Day1","ob_tf":"N/A","ob_zone":"–",
                "direction":direction,"trend":trend,
                "entry":round(entry,8),"entry_type":entry_type,
                "sl":round(sl_p,8),"tp":round(tp_p,8),"rr":rr,
                "crh":crt["crh"],"crl":crt["crl"],
                "ob_top":"–","ob_bot":"–",
                "score":score,"grade":grade,"details":details,
                "tbs_found":tbs_found,"tbs_tf":tbs_tf or "–","tbs_entry":tbs_entry or "–","tbs_sl":tbs_sl or "–",
                "fvg_found":fvg_found,"fvg_type":fvg_type or "–",
                "fvg_entry":fvg_entry or "–","fvg_top":fvg_top or "–","fvg_bot":fvg_bot or "–",
                "choch_found":choch_found,"choch_level":choch_level or "–",
                "liq_swept":liq,"ob_respected":False,"continuous":continuous,
                "timestamp":datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            })
            break  # one 1D signal per pair

    if results: return results

    # ── PATH B: CRT forming inside ANY valid key level (OB/BB/FVG/IFVG/RJB)
    # P/D zone is NOT mandatory — it upgrades the signal to A+
    for crt_tf in ["Hour4","Hour3","Hour2","Min60"]:
        if results: break

        crt_candles = get_candles(symbol, crt_tf, limit=200)
        if not crt_candles or len(crt_candles)<20: continue

        for ob_tf in OB_TFS:
            if results: break
            ob_candles = get_candles(symbol, ob_tf, limit=150)
            if not ob_candles or len(ob_candles)<20: continue

            # ── Find ALL key levels (OB, BB, FVG, IFVG, RJB) ─────────
            all_kls = find_all_key_levels(ob_candles, direction)
            ob_resp  = prev_obs_respected(
                find_obs(ob_candles, "BULLISH" if direction=="BULLISH" else "BEARISH"),
                ob_candles, direction, min_resp=1)

            zone_found  = False
            zone_name   = "–"
            zone_top    = None
            zone_bot    = None
            zone_type   = "–"
            matched_ob  = None
            at_key      = False
            in_pd_zone  = False   # premium/discount — upgrades to A+

            for kl in all_kls[:8]:
                zt = kl["top"]; zb = kl["bot"]
                if zt <= zb: continue
                # Optional P/D zone check (not a hard gate)
                in_zone_pd, pd_name = ob_in_pd_zone(kl, ob_candles, direction)
                # Key level is valid regardless of P/D zone
                zone_found  = True
                zone_top    = zt
                zone_bot    = zb
                zone_type   = kl.get("kl_type","KL")
                zone_name   = (pd_name + " · " + zone_type) if in_zone_pd else zone_type
                matched_ob  = kl if zone_type=="OB" else None
                at_key      = ob_at_key_level(kl, direction, sh, sl)
                in_pd_zone  = in_zone_pd
                break

            if not zone_found:
                diag["not_in_zone"]+=1; continue

            # ── Detect CRT forming INSIDE the zone ────────────────
            crts_raw = detect_crt(crt_candles, direction, ob=None)
            crts = [c for c in crts_raw if crt_inside_zone(c, zone_top, zone_bot)]

            # ── If no completed CRT, check for manip phase ────────
            if not crts:
                diag["no_crts"]+=1
                # Check if pair is in manipulation phase (C2 still forming)
                with manip_lock:
                    already_monitored = symbol in manip_monitor
                    monitor_full = len(manip_monitor) >= MAX_MONITORED
                if not already_monitored and not monitor_full:
                    manip_pending = detect_manip_phase(crt_candles, direction)
                    manip_in_zone = [m for m in manip_pending
                                     if crt_inside_zone(m, zone_top, zone_bot)]
                    if manip_in_zone:
                        mp = manip_in_zone[0]
                        with manip_lock:
                            manip_monitor[symbol] = {
                                **mp,
                                "crt_tf":   crt_tf,
                                "ob_tf":    ob_tf,
                                "zone_name":zone_name,
                                "zone_top": zone_top,
                                "zone_bot": zone_bot,
                                "kl_type":  zone_type,
                                "trend":    trend,
                                "added_at": datetime.now(timezone.utc).strftime("%H:%M UTC"),
                            }
                        log(f"👁 MONITORING: {symbol} {direction} {crt_tf} — in manip phase at {zone_type}")
                continue

            for crt in crts:
                # TBS — MANDATORY — Model #1 entry
                tbs_found, tbs_tf, tbs_entry, tbs_sl = check_tbs(
                    symbol, direction, crt["crl"], crt["crh"], crt_tf)
                if not tbs_found:
                    diag["no_tbs"]+=1; continue

                # Model #1: entry = TBS candle open, SL = sweep extreme
                if tbs_entry:
                    entry      = tbs_entry
                    entry_type = "Model #1 (TBS Open)"
                    sl_p       = tbs_sl
                    tp_p       = crt["tp"]
                else:
                    entry      = crt["entry"]
                    entry_type = "C2 Close"
                    sl_p       = crt["sl"]
                    tp_p       = crt["tp"]

                ltf = get_ltf_for_crt(crt_tf)
                fallback_ltfs = {"Hour4":["Min15","Min10"],"Hour3":["Min15","Min10"],
                                  "Hour2":["Min10","Min5"],"Min60":["Min5"]}.get(crt_tf,[ltf])
                choch_found, choch_level = check_choch_multi(symbol, fallback_ltfs, direction)
                fvg_ltfs = {"Hour4":["Min15","Min10"],"Hour3":["Min15","Min10"],
                             "Hour2":["Min10","Min5"],"Min60":["Min5"]}.get(crt_tf,[ltf])
                fvg_found, fvg_type, fvg_entry, fvg_top, fvg_bot = find_fvg_multi(
                    symbol, fvg_ltfs, direction)

                risk   = abs(entry-sl_p)
                reward = abs(tp_p-entry)
                rr     = round(reward/risk,2) if risk>0 else 0
                if rr<2.0:
                    diag["rr_low"]+=1; continue

                crt_s = dict(crt); crt_s["entry"]=entry; crt_s["rr"]=rr
                score,grade,details = score_signal(
                    crt_s, trend, False, tbs_found, tbs_tf,
                    fvg_found, fvg_type, choch_found, continuous,
                    is_1d=False, ob=matched_ob, at_key=at_key,
                    ob_resp=ob_resp, ob_zone=zone_name,
                    sh=sh, sl=sl, direction=direction)

                diag["passed"]+=1
                results.append({
                    "symbol":    symbol,
                    "tf":        crt_tf,
                    "ob_tf":     ob_tf,
                    "ob_zone":   zone_name,
                    "zone_type": zone_type,
                    "direction": direction,
                    "trend":     trend,
                    "entry":     round(entry,8),
                    "entry_type":entry_type,
                    "sl":        round(sl_p,8),
                    "tp":        round(tp_p,8),
                    "rr":        rr,
                    "crh":       crt["crh"],
                    "crl":       crt["crl"],
                    "ob_top":    zone_top   or "–",
                    "ob_bot":    zone_bot   or "–",
                    "score":     score,
                    "grade":     grade,
                    "details":   details,
                    "tbs_found":    tbs_found,
                    "tbs_tf":       tbs_tf   or "–",
                    "tbs_entry":    tbs_entry or "–",
                    "tbs_sl":       tbs_sl   or "–",
                    "fvg_found":    fvg_found,
                    "fvg_type":     fvg_type  or "–",
                    "fvg_entry":    fvg_entry or "–",
                    "fvg_top":      fvg_top   or "–",
                    "fvg_bot":      fvg_bot   or "–",
                    "choch_found":  choch_found,
                    "choch_level":  choch_level or "–",
                    "liq_swept":    False,
                    "ob_respected": ob_resp,
                    "continuous":   continuous,
                    "timestamp":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                })
                break
            if results: break
    return results

# ════════ SCANNER LOOP ════════════════════════════════════════════════

# ════════ MEXC AUTO-TRADE ENGINE ═════════════════════════════════════

import hmac, hashlib, urllib.parse

def mexc_sign(params, secret):
    """Generate MEXC signature."""
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()

def mexc_request(method, path, params=None, signed=True):
    """Make authenticated request to MEXC futures API."""
    if not trade_config["api_key"] or not trade_config["api_secret"]:
        return None, "API keys not configured"
    params = params or {}
    headers = {
        "Content-Type": "application/json",
        "ApiKey": trade_config["api_key"],
    }
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["ApiKey"]    = trade_config["api_key"]
        params["sign"]      = mexc_sign(params, trade_config["api_secret"])
    try:
        url = f"{MEXC_FUTURES}{path}"
        if method == "GET":
            r = requests.get(url, params=params, headers=headers, timeout=10)
        else:
            r = requests.post(url, json=params, headers=headers, timeout=10)
        data = r.json()
        if data.get("success") or data.get("code") == 0:
            return data.get("data"), None
        return None, data.get("message", "Unknown error")
    except Exception as e:
        return None, str(e)

def get_account_balance():
    """Get available USDT balance."""
    data, err = mexc_request("GET", "/account/assets")
    if err or not data: return 0.0, err
    for asset in (data if isinstance(data, list) else [data]):
        if asset.get("currency") == "USDT":
            return float(asset.get("availableBalance", 0)), None
    return 0.0, "USDT balance not found"

def get_symbol_info(symbol):
    """Get contract details for position sizing."""
    try:
        r = requests.get(f"{MEXC_BASE}/detail", timeout=10)
        data = r.json()
        for item in data.get("data", []):
            if item.get("symbol") == symbol:
                return {
                    "min_vol":    float(item.get("minVol", 1)),
                    "contract_size": float(item.get("contractSize", 1)),
                    "price_unit": float(item.get("priceUnit", 0.01)),
                }
    except: pass
    return {"min_vol": 1, "contract_size": 1, "price_unit": 0.01}

def calc_position_size(symbol, entry, sl, balance):
    """
    Calculate position size based on risk %.
    Risk amount = balance * risk_pct / 100
    Position size (contracts) = risk_amount / (|entry - sl| * contract_size)
    """
    risk_amount = balance * trade_config["risk_pct"] / 100
    sl_distance = abs(entry - sl)
    if sl_distance <= 0: return 0
    info = get_symbol_info(symbol)
    contracts = risk_amount / (sl_distance * info["contract_size"])
    # Round to minimum volume
    min_vol = info["min_vol"]
    contracts = max(min_vol, round(contracts / min_vol) * min_vol)
    return int(contracts)

def place_order(sig):
    """
    Place a futures order on MEXC based on CRT signal.
    Uses limit order at TBS candle open (Model #1 entry).
    Sets SL and TP automatically.
    """
    with trade_lock:
        if not trade_config["enabled"]:
            return False, "Auto-trade disabled"
        if len(open_trades) >= trade_config["max_trades"]:
            return False, f"Max trades ({trade_config['max_trades']}) reached"
        if sig["symbol"] in open_trades:
            return False, f"Already have open trade on {sig['symbol']}"

    # Get balance
    balance, err = get_account_balance()
    if err: return False, f"Balance error: {err}"
    if balance < 10: return False, "Insufficient balance (min $10)"

    # Max 80% of total balance across all 4 trades (4 x 20%)
    total_used = len(open_trades) * (balance * 0.20)
    if total_used >= balance * 0.80:
        return False, "30% balance cap reached across open trades"

    entry = float(sig["entry"])
    sl    = float(sig["sl"])
    tp    = float(sig["tp"])

    # Cross margin: 20% of total balance as margin per trade
    margin = balance * 0.20
    # Calc size from margin
    info = get_symbol_info(sig["symbol"])
    sl_dist = abs(entry - sl)
    if sl_dist <= 0: return False, "SL distance is zero"
    # SL loss must not exceed 100% of margin (cross margin rule)
    max_loss = margin * 1.00
    size = int(max_loss / (sl_dist * info["contract_size"]))
    size = max(int(info["min_vol"]), size)
    if size <= 0: return False, "Position size too small"

    side = 1 if sig["direction"] == "BUY" else 2  # 1=Long, 2=Short
    open_type = 2  # Cross margin

    # ── Auto-calculate leverage to keep SL within 70% of margin ──
    is_aplus = sig.get("grade") == "A+"
    # Cross margin: 20% of total futures balance as margin per trade
    margin = balance * 0.20
    # SL distance as % of entry
    sl_pct = abs(entry - sl) / entry if entry > 0 else 0.01
    # Max leverage where SL loss = 70% of margin
    # SL loss must not exceed 100% of margin
    # loss = margin * sl_pct * leverage => leverage = 1.0 / sl_pct
    if sl_pct > 0:
        max_safe_lev = int(1.0 / sl_pct)   # 100% margin cap
    else:
        max_safe_lev = 10
    # Up to 500x on all setups, cross margin
    min_lev = 10
    max_lev = 500   # always allow up to 500x
    leverage = max(min_lev, min(max_lev, max_safe_lev))
    # Override with user config if lower
    if trade_config["leverage"] < leverage:
        leverage = trade_config["leverage"]

    lev_params = {
        "symbol":       sig["symbol"],
        "leverage":     leverage,
        "openType":     open_type,
        "positionType": side,
    }
    mexc_request("POST", "/position/change_leverage", lev_params)

    # Place limit order at entry
    order_params = {
        "symbol":    sig["symbol"],
        "price":     entry,
        "vol":       size,
        "side":      side,
        "type":      1,       # 1 = Limit order
        "openType":  open_type,
        "leverage":  trade_config["leverage"],
    }
    order_data, err = mexc_request("POST", "/order/submit", order_params)
    if err: return False, f"Order failed: {err}"

    order_id = order_data if isinstance(order_data, str) else order_data.get("orderId","")

    # Place SL order
    sl_params = {
        "symbol":    sig["symbol"],
        "stopLossPrice": sl,
        "positionType":  side,
        "openType":      open_type,
        "vol":           size,
    }
    mexc_request("POST", "/order/set_stop_loss", sl_params)

    # Place TP order
    tp_params = {
        "symbol":    sig["symbol"],
        "takeProfitPrice": tp,
        "positionType":    side,
        "openType":        open_type,
        "vol":             size,
    }
    mexc_request("POST", "/order/set_take_profit", tp_params)

    # Record trade
    with trade_lock:
        open_trades[sig["symbol"]] = {
            "order_id":  order_id,
            "symbol":    sig["symbol"],
            "direction": sig["direction"],
            "entry":     entry,
            "sl":        sl,
            "tp":        tp,
            "size":      size,
            "rr":        sig["rr"],
            "score":     sig["score"],
            "grade":     sig["grade"],
            "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "status":    "OPEN",
        }

    log(f"AUTO-TRADE PLACED: {sig['direction']} {sig['symbol']} Entry:{entry} SL:{sl} TP:{tp} Size:{size}")
    tg_msg = (
        "<b>AUTO-TRADE PLACED</b>\n"
        "---\n"
        f"<b>Pair:</b> {sig['symbol']}\n"
        f"<b>Side:</b> {sig['direction']}\n"
        f"<b>Entry:</b> {entry}\n"
        f"<b>SL:</b> {sl}\n"
        f"<b>TP:</b> {tp}\n"
        f"<b>Size:</b> {size} contracts\n"
        f"<b>RR:</b> {sig['rr']}R | Score: {sig['score']}/100 {sig['grade']}\n"
        "<i>CRT Model #1 Auto-Trade</i>"
    )
    send_telegram(tg_msg)
    return True, f"Order placed: {order_id}"

def close_trade(symbol, reason="Manual"):
    """Close an open position."""
    with trade_lock:
        if symbol not in open_trades:
            return False, "No open trade found"
        trade = open_trades[symbol]

    side = 2 if trade["direction"] == "BUY" else 1  # opposite side to close
    params = {
        "symbol":    symbol,
        "price":     0,       # market price
        "vol":       trade["size"],
        "side":      side,
        "type":      5,       # market close
        "openType":  1,
    }
    _, err = mexc_request("POST", "/order/submit", params)
    if err: return False, f"Close failed: {err}"

    with trade_lock:
        completed = dict(open_trades[symbol])
        completed["status"]    = f"CLOSED ({reason})"
        completed["closed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        recent_trades.appendleft(completed)
        del open_trades[symbol]

    log(f"TRADE CLOSED: {symbol} | Reason: {reason}")
    send_telegram(
        f"TRADE CLOSED: {symbol} {completed['direction']}\n"
        f"Entry: {completed['entry']} | Size: {completed['size']}\n"
        f"Reason: {reason}"
    )
    return True, "Position closed"


# ════════ MANIPULATION PHASE MONITOR ════════════════════════════════

def detect_manip_phase(candles, direction):
    """
    Detect pairs where C1 (CRT candle) is confirmed but C2 (manipulation)
    has NOT yet closed back inside. We monitor these.
    Returns list of pending manip dicts.
    """
    pending = []
    if len(candles) < 4: return pending
    limit = min(10, len(candles)-1)
    for offset in range(1, limit):
        i2 = len(candles)-1-offset   # C2 = current/last candle (still forming or just closed)
        i1 = i2-1
        if i1 < 0: break
        c1 = candles[i1]; c2 = candles[i2]
        crh = c1["high"]; crl = c1["low"]
        cr_range = crh - crl
        if cr_range <= 0: continue
        if direction == "BULLISH":
            # C2 has swept below CRL but NOT yet closed back inside
            swept = c2["low"] < crl
            still_below = c2["close"] < crl   # still outside = still in manip
            if swept and still_below:
                pending.append({
                    "c1": c1, "c2": c2, "crh": crh, "crl": crl,
                    "sweep_low": c2["low"], "direction": "BUY",
                    "phase": "MANIPULATION"
                })
        else:
            swept = c2["high"] > crh
            still_above = c2["close"] > crh
            if swept and still_above:
                pending.append({
                    "c1": c1, "c2": c2, "crh": crh, "crl": crh,
                    "sweep_high": c2["high"], "direction": "SELL",
                    "phase": "MANIPULATION"
                })
    return pending


def check_manip_completed(symbol, monitor):
    """
    Check if a monitored manipulation phase has completed
    (C2 closed back inside CRT range).
    Returns (completed, new_candles)
    """
    tf = monitor.get("crt_tf", "Hour4")
    candles = get_candles(symbol, tf, limit=50)
    if not candles: return False, []
    crh = monitor["crh"]; crl = monitor["crl"]
    direction = monitor["direction"]
    # Look at last 5 candles for completion
    recent = candles[-5:]
    for c in recent:
        if direction == "BUY":
            # Completed: candle body closed back above CRL
            if c["close"] > crl and c["close"] <= crh:
                return True, candles
        else:
            if c["close"] < crh and c["close"] >= crl:
                return True, candles
    return False, candles


def manip_monitor_loop():
    """
    Background thread that:
    1. Watches pairs in manip_monitor dict
    2. When C2 completes (closes back inside range) → search TBS → auto-trade
    3. Invalidates if sweep candle closes WITH WICK beyond CRH/CRL
    """
    log("🔍 Manipulation monitor started")
    while True:
        try:
            with manip_lock:
                symbols = list(manip_monitor.keys())

            for symbol in symbols:
                with manip_lock:
                    if symbol not in manip_monitor: continue
                    monitor = dict(manip_monitor[symbol])

                # ── Check invalidation ─────────────────────────────
                tf = monitor.get("crt_tf","Hour4")
                candles = get_candles(symbol, tf, limit=30)
                if candles:
                    last = candles[-1]
                    crh = monitor["crh"]; crl = monitor["crl"]
                    direction = monitor["direction"]
                    # Invalidation: sweep candle has a WICK beyond the level
                    # i.e. C2 closed with body beyond CRH/CRL (not just wick)
                    # Per strategy: if FIRST sweep candle closes with body beyond = invalid
                    c2 = monitor.get("c2",{})
                    if direction == "BUY":
                        # Invalid if C2 body fully below CRL (body, not just wick)
                        body_low = min(c2.get("open",0), c2.get("close",0))
                        if body_low < crl and c2.get("close",crl) < crl:
                            with manip_lock:
                                manip_monitor.pop(symbol, None)
                            log(f"❌ INVALIDATED: {symbol} — C2 body closed below CRL")
                            continue
                    else:
                        body_high = max(c2.get("open",0), c2.get("close",0))
                        if body_high > crh and c2.get("close",crh) > crh:
                            with manip_lock:
                                manip_monitor.pop(symbol, None)
                            log(f"❌ INVALIDATED: {symbol} — C2 body closed above CRH")
                            continue

                # ── Check if manip completed ───────────────────────
                completed, fresh_candles = check_manip_completed(symbol, monitor)
                if not completed:
                    continue

                log(f"✅ MANIP COMPLETE: {symbol} {direction} — searching TBS...")

                # ── Search TBS on mapped LTF ───────────────────────
                crt_tf = monitor.get("crt_tf","Hour4")
                crh = monitor["crh"]; crl = monitor["crl"]
                tbs_found, tbs_tf, tbs_entry, tbs_sl = check_tbs(
                    symbol, direction, crl, crh, crt_tf)

                if not tbs_found:
                    log(f"⏳ {symbol} — TBS not yet confirmed, still watching")
                    continue

                log(f"🐢 TBS CONFIRMED: {symbol} on {tbs_tf} | Entry:{tbs_entry} SL:{tbs_sl}")

                # ── Build and dispatch signal ──────────────────────
                tp = crh if direction=="BUY" else crl
                risk = abs(tbs_entry - tbs_sl)
                reward = abs(tp - tbs_entry)
                rr = round(reward/risk, 2) if risk > 0 else 0

                if rr < 2.0:
                    log(f"⚠️ {symbol} RR too low ({rr}R) after TBS — skipping")
                    with manip_lock: manip_monitor.pop(symbol, None)
                    continue

                # Get zone/trend info from monitor
                zone_name = monitor.get("zone_name","–")
                trend     = monitor.get("trend","NEUTRAL")
                kl_type   = monitor.get("kl_type","KL")
                crt_tf_label = {"Day1":"1D","Hour4":"4H","Hour3":"3H","Hour2":"2H","Min60":"1H"}.get(crt_tf,"–")

                # Determine A+ grade
                has_pd = "DISCOUNT" in zone_name or "PREMIUM" in zone_name
                grade  = "A+" if has_pd and rr >= 3.0 else "A" if rr >= 3.0 else "B"

                sig = {
                    "symbol":      symbol,
                    "tf":          crt_tf,
                    "ob_tf":       monitor.get("ob_tf","–"),
                    "ob_zone":     zone_name,
                    "zone_type":   kl_type,
                    "direction":   direction,
                    "trend":       trend,
                    "entry":       round(tbs_entry, 8),
                    "entry_type":  "Model #1 (TBS Open)",
                    "sl":          round(tbs_sl, 8),
                    "tp":          round(tp, 8),
                    "rr":          rr,
                    "crh":         crh,
                    "crl":         crl,
                    "ob_top":      monitor.get("zone_top","–"),
                    "ob_bot":      monitor.get("zone_bot","–"),
                    "score":       85 if grade=="A+" else 75 if grade=="A" else 60,
                    "grade":       grade,
                    "details":     [f"✅ Manip monitor confirmed","✅ TBS on {tbs_tf}",f"RR: {rr}R"],
                    "tbs_found":   True,
                    "tbs_tf":      tbs_tf,
                    "tbs_entry":   tbs_entry,
                    "tbs_sl":      tbs_sl,
                    "fvg_found":   False,
                    "fvg_type":    "–",
                    "fvg_entry":   "–",
                    "fvg_top":     "–",
                    "fvg_bot":     "–",
                    "choch_found": False,
                    "choch_level": "–",
                    "liq_swept":   False,
                    "ob_respected":False,
                    "continuous":  True,
                    "from_monitor":True,
                    "timestamp":   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                }

                signals.appendleft(sig)
                send_telegram(fmt_tg(sig))
                log(f"🎯 MONITOR SIGNAL: {direction} {symbol} | {grade} | {rr}R | TBS:{tbs_tf}")

                # Auto-trade if enabled
                if trade_config["enabled"] and trade_config["api_key"]:
                    ok, msg = place_order(sig)
                    log(f"{'✅' if ok else '❌'} Auto-trade: {msg}")

                # Remove from monitor
                with manip_lock:
                    manip_monitor.pop(symbol, None)

        except Exception as e:
            log(f"❌ Manip monitor error: {e}")

        time.sleep(10)   # check every 10 seconds


def scanner_loop():
    with scan_lock: scan_state["running"]=True
    log("🚀 CRT Scanner started — USDT perpetual pairs only")
    while True:
        try:
            with scan_lock:
                if not scan_state["enabled"]:
                    scan_state["running"]=False
            if not scan_state["enabled"]:
                time.sleep(5); continue
            with scan_lock: scan_state["running"]=True

            pairs = get_all_pairs()
            if not pairs:
                log("⚠️ No pairs fetched — retrying in 30s")
                time.sleep(30); continue

            with scan_lock:
                scan_state["total_pairs"]=len(pairs)
                scan_state["pairs_done"]=0
                scan_state["scan_count"]+=1

            log(f"🔄 Scan #{scan_state['scan_count']} — {len(pairs)} USDT pairs")

            for i,symbol in enumerate(pairs):
                if not scan_state["enabled"]: break
                with scan_lock:
                    scan_state["current_pair"]=symbol
                    scan_state["pairs_done"]=i+1
                try:
                    res = scan_pair(symbol)
                    for sig in res:
                        # Deduplication: skip if same symbol+direction+tf seen in last 50 signals
                        recent_sigs = list(signals)[:50]
                        duplicate = any(
                            s.get("symbol")==sig["symbol"] and
                            s.get("direction")==sig["direction"] and
                            s.get("tf")==sig["tf"]
                            for s in recent_sigs
                        )
                        if duplicate:
                            log(f"⏭ SKIP duplicate: {sig['direction']} {symbol} {sig['tf']}")
                            continue
                        diag["passed"]+=1
                        signals.appendleft(sig)
                        with scan_lock: scan_state["signals_found"]+=1
                        tf_lbl={"Day1":"1D","Hour4":"4H","Hour3":"3H","Hour2":"2H","Min60":"1H"}.get(sig["tf"],"–")
                        log(f"🎯 {sig['direction']} {symbol} | {tf_lbl} | OB:{sig['ob_tf']} | Score:{sig['score']} {sig['grade']} | RR:{sig['rr']}R | TBS:{sig['tbs_tf']}")
                        send_telegram(fmt_tg(sig))
                        # Auto-trade if enabled
                        if trade_config["enabled"] and trade_config["api_key"]:
                            ok, msg = place_order(sig)
                            log(f"{'✅' if ok else '❌'} Auto-trade: {msg}")
                except: pass
                time.sleep(0.35)
                # Log progress every 50 pairs
                if (i+1) % 50 == 0:
                    log(f"📊 Progress: {i+1}/{scan_state['total_pairs']} pairs scanned this cycle")

            with scan_lock: scan_state["last_scan"]=datetime.now(timezone.utc).strftime("%H:%M UTC")
            log(f"✅ Scan #{scan_state['scan_count']} done — {len(pairs)} pairs. Restarting...")
            log(f"📊 GATE STATS: neutral={diag['neutral']} no_cont={diag['not_continuous']} "
                f"no_obs={diag['no_obs']} not_key={diag['not_at_key']} not_zone={diag['not_in_zone']} "
                f"no_liq={diag['no_liq']} not_tap={diag['not_tapping']} no_crt={diag['no_crts']} "
                f"no_tbs={diag['no_tbs']} rr_low={diag['rr_low']}")
            log(f"📊 1D STATS: no_crt={diag['1d_no_crts']} no_tbs={diag['1d_no_tbs']} rr_low={diag['1d_rr_low']} PASSED={diag['passed']}")
            # Reset counters
            for k in diag: diag[k]=0

        except Exception as e:
            log(f"❌ Scanner error: {e}"); time.sleep(15)

# ════════ HTML ════════════════════════════════════════════════════════

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>CRT Scanner 🚀</title>
<link href="https://fonts.googleapis.com/css2?family=Fredoka+One&family=Nunito:wght@400;600;700;800;900&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet"/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Nunito',sans-serif;background:#0f0e1a;min-height:100vh;display:flex;align-items:center;justify-content:center;overflow:hidden;padding:20px}
/* stars */
.stars{position:fixed;inset:0;z-index:0}
.star{position:absolute;border-radius:50%;background:#fff;animation:twink 3s infinite}
@keyframes twink{0%,100%{opacity:.15;transform:scale(1)}50%{opacity:.9;transform:scale(1.4)}}
/* blobs */
.blob{position:fixed;border-radius:50%;filter:blur(70px);opacity:.18;animation:blob-float 10s ease-in-out infinite;z-index:0}
.b1{width:380px;height:380px;background:#7c3aed;top:-120px;left:-80px}
.b2{width:300px;height:300px;background:#db2777;bottom:-80px;right:-60px;animation-delay:-4s}
.b3{width:200px;height:200px;background:#0ea5e9;top:40%;left:40%;animation-delay:-7s}
@keyframes blob-float{0%,100%{transform:translate(0,0) scale(1)}33%{transform:translate(20px,-30px) scale(1.05)}66%{transform:translate(-15px,20px) scale(.95)}}
.card{position:relative;z-index:10;background:rgba(20,18,40,.92);border:2px solid rgba(124,58,237,.4);border-radius:28px;padding:44px 38px 36px;width:100%;max-width:420px;backdrop-filter:blur(24px);box-shadow:0 0 0 1px rgba(124,58,237,.1),0 40px 80px rgba(0,0,0,.7),inset 0 1px 0 rgba(255,255,255,.05)}
/* wobble corners */
.card::before,.card::after{content:'';position:absolute;width:24px;height:24px;border:3px solid rgba(124,58,237,.5);border-radius:6px}
.card::before{top:-3px;left:-3px;border-right:none;border-bottom:none}
.card::after{bottom:-3px;right:-3px;border-left:none;border-top:none}
.head{text-align:center;margin-bottom:30px}
.rocket{font-size:3.6rem;display:block;animation:rocket-bounce 2s ease-in-out infinite;filter:drop-shadow(0 0 20px rgba(124,58,237,.7))}
@keyframes rocket-bounce{0%,100%{transform:translateY(0) rotate(-5deg)}50%{transform:translateY(-14px) rotate(5deg)}}
.title{font-family:'Fredoka One',sans-serif;font-size:2.4rem;letter-spacing:.04em;background:linear-gradient(135deg,#a78bfa,#f472b6,#38bdf8);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:5px}
.sub{font-size:.75rem;color:rgba(200,210,255,.4);letter-spacing:.14em;text-transform:uppercase;font-weight:700}
.lbl{font-size:.7rem;font-weight:800;color:rgba(167,139,250,.7);letter-spacing:.1em;text-transform:uppercase;margin-bottom:7px;display:block}
.inp{width:100%;padding:13px 16px;background:rgba(255,255,255,.05);border:2px solid rgba(124,58,237,.25);border-radius:14px;color:#e2e8f0;font-size:.95rem;font-family:'Nunito',sans-serif;font-weight:700;outline:none;transition:all .2s;margin-bottom:18px}
.inp:focus{border-color:rgba(167,139,250,.6);background:rgba(124,58,237,.08);box-shadow:0 0 0 4px rgba(124,58,237,.1)}
.inp::placeholder{color:rgba(200,210,255,.2)}
.btn{width:100%;padding:14px;background:linear-gradient(135deg,#7c3aed,#db2777);color:#fff;border:none;border-radius:14px;font-family:'Fredoka One',sans-serif;font-size:1.15rem;letter-spacing:.06em;cursor:pointer;transition:all .25s;position:relative;overflow:hidden;box-shadow:0 6px 24px rgba(124,58,237,.4)}
.btn::before{content:'';position:absolute;top:0;left:-100%;width:100%;height:100%;background:linear-gradient(90deg,transparent,rgba(255,255,255,.15),transparent);transition:left .4s}
.btn:hover::before{left:100%}
.btn:hover{transform:translateY(-3px);box-shadow:0 10px 32px rgba(124,58,237,.55)}
.err{background:rgba(239,68,68,.1);border:2px solid rgba(239,68,68,.3);border-radius:12px;padding:10px 14px;font-size:.8rem;color:#f87171;margin-bottom:14px;display:none;font-weight:700}
.err.show{display:block}
.badges{display:flex;gap:6px;margin-top:22px;flex-wrap:wrap;justify-content:center}
.badge{background:rgba(124,58,237,.12);border:1.5px solid rgba(124,58,237,.25);border-radius:20px;padding:4px 11px;font-size:.65rem;color:rgba(167,139,250,.8);font-weight:800;letter-spacing:.04em}
.dot-row{display:flex;align-items:center;justify-content:center;gap:7px;margin-top:16px}
.live-dot{width:7px;height:7px;border-radius:50%;background:#10b981;box-shadow:0 0 8px #10b981;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.live-txt{font-family:'JetBrains Mono',monospace;font-size:.62rem;color:rgba(16,185,129,.7);letter-spacing:.06em;font-weight:700}
</style>
</head>
<body>
<div class="stars" id="stars"></div>
<div class="blob b1"></div><div class="blob b2"></div><div class="blob b3"></div>
<div class="card">
  <div class="head">
    <span class="rocket">🚀</span>
    <div class="title">CRT Scanner</div>
    <div class="sub">MEXC Perpetual · USDT Pairs Only</div>
  </div>
  <div class="err" id="err"></div>
  <label class="lbl">Password</label>
  <input class="inp" type="password" id="pw" placeholder="Enter your password" autofocus/>
  <button class="btn" id="btn" onclick="login()">🔓 Enter Dashboard</button>
  <div class="badges">
    <span class="badge">📡 CRT Strategy</span>
    <span class="badge">📦 Order Blocks</span>
    <span class="badge">🐢 TBS Body</span>
    <span class="badge">⚡ Model #1</span>
    <span class="badge">🤖 Auto-Trade</span>
  </div>
  <div class="dot-row"><div class="live-dot"></div><span class="live-txt">SCANNER LIVE · ALL USDT PERPS</span></div>
</div>
<script>
// Stars
const s=document.getElementById('stars');
for(let i=0;i<70;i++){
  const d=document.createElement('div');d.className='star';
  const sz=Math.random()*2.5+.5;
  d.style.cssText=`width:${sz}px;height:${sz}px;top:${Math.random()*100}%;left:${Math.random()*100}%;animation-delay:${Math.random()*3}s;animation-duration:${2+Math.random()*2}s`;
  s.appendChild(d);
}
function login(){
  const pw=document.getElementById('pw').value.trim();
  const err=document.getElementById('err');const btn=document.getElementById('btn');
  if(!pw){err.textContent='🔑 Password required!';err.classList.add('show');return;}
  btn.textContent='🛸 Launching...';btn.disabled=true;err.classList.remove('show');
  fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})})
    .then(r=>r.json()).then(d=>{
      if(d.ok){btn.textContent='✅ Let\'s go!';setTimeout(()=>window.location.href='/dashboard',300);}
      else{err.textContent='❌ Wrong password, try again!';err.classList.add('show');btn.textContent='🔓 Enter Dashboard';btn.disabled=false;document.getElementById('pw').value='';document.getElementById('pw').focus();}
    }).catch(()=>{btn.textContent='🔓 Enter Dashboard';btn.disabled=false;});
}
document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')login();});

window.toggleAutoTrade = async function () {
    try {
        const r = await fetch("/api/toggle-autotrade", {
            method: "POST"
        });

        const d = await r.json();

        const btn = document.getElementById("autoTradeBtn");

        if (d.enabled) {
            btn.innerText = "AUTO TRADE ON";
            btn.classList.remove("off");
            btn.classList.add("on");
            toast("Auto trading enabled 🚀", "buy");
        } else {
            btn.innerText = "AUTO TRADE OFF";
            btn.classList.remove("on");
            btn.classList.add("off");
            toast("Auto trading disabled ⛔", "sell");
        }

    } catch (e) {
        toast("Auto trade toggle failed", "sell");
    }
};

</script>
</body>
</html>"""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>CRT Scanner 🚀</title>
<link href="https://fonts.googleapis.com/css2?family=Fredoka+One&family=Nunito:wght@400;600;700;800;900&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet"/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0c0b18;--s1:#13122a;--s2:#1a1838;--s3:#201e45;--purple:#7c3aed;--pink:#db2777;--blue:#0ea5e9;--cyan:#06b6d4;--green:#10b981;--red:#ef4444;--yellow:#f59e0b;--orange:#f97316;--text:#e2e8f0;--dim:#94a3b8;--muted:#334155;--border:rgba(124,58,237,.2);--border2:rgba(124,58,237,.45)}
body{font-family:'Nunito',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding-bottom:80px}
body::before{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 3px,rgba(0,0,0,.025) 3px,rgba(0,0,0,.025) 4px);pointer-events:none;z-index:998}
.bg-glow{position:fixed;inset:0;pointer-events:none;z-index:0}
.bg-glow::before{content:'';position:absolute;width:600px;height:600px;border-radius:50%;background:radial-gradient(circle,rgba(124,58,237,.12),transparent 70%);top:-200px;left:-200px}
.bg-glow::after{content:'';position:absolute;width:500px;height:500px;border-radius:50%;background:radial-gradient(circle,rgba(219,39,119,.1),transparent 70%);bottom:-150px;right:-150px}
.hdr{background:rgba(12,11,24,.95);border-bottom:2px solid var(--border);position:sticky;top:0;z-index:200;backdrop-filter:blur(20px)}
.hdr-glow{position:absolute;bottom:-1px;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,var(--purple),var(--pink),transparent);opacity:.5}
.hdr-in{max-width:1360px;margin:0 auto;padding:0 20px;height:60px;display:flex;align-items:center;justify-content:space-between;gap:14px}
.brand{display:flex;align-items:center;gap:11px}
.brand-icon{font-size:1.7rem;animation:rock 3s ease-in-out infinite;filter:drop-shadow(0 0 8px rgba(124,58,237,.6))}
@keyframes rock{0%,100%{transform:rotate(-8deg)}50%{transform:rotate(8deg)}}
.brand-name{font-family:'Fredoka One',sans-serif;font-size:1.2rem;letter-spacing:.05em;background:linear-gradient(135deg,#a78bfa,#f472b6);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.brand-sub{font-family:'JetBrains Mono',monospace;font-size:.52rem;color:var(--dim);letter-spacing:.08em}
.scan-pill{display:flex;align-items:center;gap:7px;background:rgba(16,185,129,.08);border:1.5px solid rgba(16,185,129,.22);border-radius:20px;padding:6px 14px}
.sdot{width:7px;height:7px;border-radius:50%;background:var(--green);animation:sdot 2s infinite}
.sdot.off{background:var(--red);animation:none}
@keyframes sdot{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.3;transform:scale(.6)}}
.stxt{font-family:'JetBrains Mono',monospace;font-size:.62rem;color:var(--green);font-weight:700;letter-spacing:.05em}
.stxt.off{color:var(--red)}
.hdr-right{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.snum{font-family:'JetBrains Mono',monospace;font-size:.65rem;color:var(--dim);background:var(--s2);border:1.5px solid var(--muted);border-radius:10px;padding:4px 10px}
.tbtn{padding:7px 16px;border:2px solid;border-radius:12px;font-family:'Nunito',sans-serif;font-size:.8rem;font-weight:800;cursor:pointer;transition:all .22s}
.tbtn.on{background:rgba(239,68,68,.1);border-color:rgba(239,68,68,.4);color:var(--red)}
.tbtn.on:hover{background:rgba(239,68,68,.2);transform:scale(1.05)}
.tbtn.off{background:rgba(16,185,129,.1);border-color:rgba(16,185,129,.35);color:var(--green)}
.tbtn.off:hover{background:rgba(16,185,129,.18);transform:scale(1.05)}
.obtn{padding:7px 13px;background:transparent;border:1.5px solid var(--muted);border-radius:10px;color:var(--dim);font-family:'Nunito',sans-serif;font-size:.78rem;font-weight:700;cursor:pointer;transition:all .2s}
.obtn:hover{border-color:var(--red);color:var(--red)}
.pb{background:rgba(239,68,68,.08);border-bottom:2px solid rgba(239,68,68,.25);padding:10px;text-align:center;font-family:'Fredoka One',sans-serif;font-size:.85rem;letter-spacing:.1em;color:var(--red);display:none}
.pb.show{display:block}
.prog{background:rgba(12,11,24,.9);border-bottom:1px solid var(--border);padding:8px 20px;position:relative;z-index:10}
.prog-in{max-width:1360px;margin:0 auto;display:flex;align-items:center;gap:14px}
.prog-lbl{font-family:'JetBrains Mono',monospace;font-size:.62rem;color:var(--dim);white-space:nowrap;min-width:200px;overflow:hidden;text-overflow:ellipsis}
.prog-track{flex:1;height:6px;background:var(--s3);border-radius:3px;overflow:hidden}
.prog-fill{height:100%;background:linear-gradient(90deg,var(--purple),var(--pink),var(--blue));border-radius:3px;transition:width .5s ease}
.prog-cnt{font-family:'JetBrains Mono',monospace;font-size:.62rem;color:var(--dim);white-space:nowrap}
.sec{max-width:1360px;margin:20px auto 0;padding:0 20px;position:relative;z-index:1}
.sec-hdr{display:flex;align-items:center;gap:10px;margin-bottom:11px}
.sec-ttl{font-family:'Fredoka One',sans-serif;font-size:1rem;letter-spacing:.06em;color:rgba(167,139,250,.8)}
.sec-line{flex:1;height:2px;background:linear-gradient(90deg,rgba(124,58,237,.3),transparent);border-radius:1px}
.sec-note{font-family:'JetBrains Mono',monospace;font-size:.56rem;color:var(--dim)}
.prices-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}
.pc{background:var(--s1);border:2px solid var(--border);border-radius:16px;padding:13px 12px 11px;position:relative;overflow:hidden;transition:all .25s;cursor:default}
.pc::after{content:'';position:absolute;top:0;left:0;right:0;height:3px;border-radius:3px 3px 0 0;background:var(--muted);transition:background .3s}
.pc.up::after{background:linear-gradient(90deg,var(--green),rgba(16,185,129,.3))}
.pc.dn::after{background:linear-gradient(90deg,var(--red),rgba(239,68,68,.3))}
.pc:hover{border-color:var(--border2);transform:translateY(-4px) rotate(.5deg);box-shadow:0 12px 36px rgba(0,0,0,.5)}
.pc-sym{font-family:'Fredoka One',sans-serif;font-size:.75rem;letter-spacing:.06em;color:var(--dim);margin-bottom:5px}
.pc-price{font-family:'JetBrains Mono',monospace;font-size:.86rem;font-weight:700;margin-bottom:5px;line-height:1}
.pc-price.up{color:var(--green)}.pc-price.dn{color:var(--red)}
.pc-chg{font-family:'JetBrains Mono',monospace;font-size:.62rem;font-weight:700;padding:2px 7px;border-radius:8px;display:inline-block}
.pc-chg.up{background:rgba(16,185,129,.12);color:var(--green)}.pc-chg.dn{background:rgba(239,68,68,.12);color:var(--red)}
.stats-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
.sc{background:var(--s1);border:2px solid var(--border);border-radius:16px;padding:16px 16px 14px;position:relative;overflow:hidden;transition:all .22s}
.sc:hover{border-color:var(--border2);transform:translateY(-3px) rotate(.3deg)}
.sc::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;border-radius:3px 3px 0 0}
.s0::before{background:linear-gradient(90deg,var(--purple),var(--pink))}.s1::before{background:var(--green)}.s2::before{background:var(--red)}.s3::before{background:var(--blue)}.s4::before{background:var(--yellow)}
.sc-lbl{font-family:'JetBrains Mono',monospace;font-size:.54rem;color:var(--dim);letter-spacing:.08em;text-transform:uppercase;margin-bottom:7px;font-weight:700}
.sc-val{font-family:'Fredoka One',sans-serif;font-size:2rem;letter-spacing:.04em;line-height:1;color:#a78bfa}
.sc-sub{font-size:.64rem;color:var(--dim);margin-top:4px;font-weight:600}
.tab-wrap{max-width:1360px;margin:20px auto 0;padding:0 20px;position:relative;z-index:1}
.tabs{display:flex;gap:5px;background:var(--s1);border:2px solid var(--border);border-radius:16px;padding:5px;margin-bottom:18px;overflow-x:auto}
.tab{flex:1;min-width:75px;padding:9px 8px;border:none;border-radius:12px;font-family:'Nunito',sans-serif;font-size:.76rem;font-weight:800;cursor:pointer;transition:all .2s;color:var(--dim);background:transparent;white-space:nowrap;text-align:center}
.tab:hover{color:var(--text)}.tab.active{background:linear-gradient(135deg,var(--purple),var(--pink));color:#fff;box-shadow:0 4px 16px rgba(124,58,237,.4)}
.frow{display:flex;align-items:center;justify-content:space-between;margin-bottom:15px;flex-wrap:wrap;gap:9px}
.ftitle{font-family:'Fredoka One',sans-serif;font-size:1.05rem;letter-spacing:.04em;color:#a78bfa}
.fgrp{display:flex;gap:6px;flex-wrap:wrap}
.fsel{background:var(--s2);border:2px solid var(--border);border-radius:10px;color:var(--text);padding:7px 10px;font-size:.72rem;font-family:'Nunito',sans-serif;font-weight:700;outline:none}
.fsel:focus{border-color:rgba(124,58,237,.5)}
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:60px 20px;background:var(--s1);border:2px dashed var(--border);border-radius:20px;text-align:center;gap:12px}
.empty-ico{font-size:3rem;animation:wobble 3s ease-in-out infinite}
@keyframes wobble{0%,100%{transform:rotate(-5deg)}50%{transform:rotate(5deg)}}
.empty-t{font-family:'Fredoka One',sans-serif;font-size:1.2rem;letter-spacing:.04em;color:var(--dim)}
.empty-s{font-size:.8rem;color:var(--dim);max-width:380px;line-height:1.7;font-weight:600}
.sig-list{display:flex;flex-direction:column;gap:12px}
.scard{background:var(--s1);border:2px solid var(--border);border-radius:18px;padding:18px 20px;animation:card-pop .35s cubic-bezier(.34,1.56,.64,1);transition:all .22s;position:relative;overflow:hidden}
.scard::before{content:'';position:absolute;top:0;left:0;bottom:0;width:4px;border-radius:4px 0 0 4px}
.scard.buy::before{background:linear-gradient(180deg,var(--green),rgba(16,185,129,.2))}
.scard.sell::before{background:linear-gradient(180deg,var(--red),rgba(239,68,68,.2))}
.scard:hover{border-color:var(--border2);transform:translateY(-4px);box-shadow:0 16px 48px rgba(0,0,0,.55)}
@keyframes card-pop{from{opacity:0;transform:scale(.95) translateY(-12px)}to{opacity:1;transform:scale(1) translateY(0)}}
.card-hdr{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:13px;padding-bottom:11px;border-bottom:1.5px solid var(--border)}
.dtag{font-family:'Fredoka One',sans-serif;font-size:.85rem;letter-spacing:.06em;padding:5px 13px;border-radius:12px;border:2px solid;flex-shrink:0}
.dtag.BUY{background:rgba(16,185,129,.1);border-color:rgba(16,185,129,.35);color:var(--green)}
.dtag.SELL{background:rgba(239,68,68,.1);border-color:rgba(239,68,68,.35);color:var(--red)}
.csym{font-family:'Fredoka One',sans-serif;font-size:1.1rem;letter-spacing:.06em;color:var(--text)}
.chips{display:flex;gap:5px;flex-wrap:wrap;align-items:center}
.chip{font-family:'JetBrains Mono',monospace;font-size:.6rem;padding:3px 8px;border-radius:8px;letter-spacing:.04em;border:1.5px solid;font-weight:700}
.chip-tf{color:var(--cyan);border-color:rgba(6,182,212,.25);background:rgba(6,182,212,.07)}
.chip-ob{color:var(--orange);border-color:rgba(249,115,22,.25);background:rgba(249,115,22,.07)}
.chip-tr.BULLISH{color:var(--green);border-color:rgba(16,185,129,.25);background:rgba(16,185,129,.07)}
.chip-tr.BEARISH{color:var(--red);border-color:rgba(239,68,68,.25);background:rgba(239,68,68,.07)}
.chip-tr.NEUTRAL{color:var(--dim);border-color:var(--muted);background:transparent}
.chip-aplus{color:#fbbf24;border-color:rgba(251,191,36,.4);background:rgba(251,191,36,.1);animation:ap 2s infinite}
@keyframes ap{0%,100%{box-shadow:0 0 0 0 rgba(251,191,36,.3)}50%{box-shadow:0 0 0 4px rgba(251,191,36,0)}}
.gtag{font-family:'Fredoka One',sans-serif;font-size:.9rem;letter-spacing:.06em;padding:4px 11px;border-radius:10px;margin-left:auto;border:2px solid;flex-shrink:0}
.gAp{color:#fbbf24;border-color:rgba(251,191,36,.5);background:rgba(251,191,36,.12);animation:ap 2s infinite}
.gA{color:#a78bfa;border-color:rgba(167,139,250,.4);background:rgba(167,139,250,.08)}
.gB{color:#38bdf8;border-color:rgba(56,189,248,.35);background:rgba(56,189,248,.07)}
.gC{color:var(--orange);border-color:rgba(249,115,22,.3);background:rgba(249,115,22,.06)}
.gD{color:var(--dim);border-color:var(--muted);background:transparent}
.cts{font-family:'JetBrains Mono',monospace;font-size:.57rem;color:var(--dim);white-space:nowrap}
.lvl-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(112px,1fr));gap:8px;margin-bottom:13px}
.lv{background:var(--s2);border:1.5px solid var(--muted);border-radius:12px;padding:10px 12px;transition:all .2s}
.lv:hover{border-color:rgba(124,58,237,.3);transform:translateY(-2px)}
.lv-lbl{font-family:'JetBrains Mono',monospace;font-size:.53rem;color:var(--dim);letter-spacing:.07em;text-transform:uppercase;margin-bottom:5px;font-weight:700}
.lv-val{font-family:'JetBrains Mono',monospace;font-size:.8rem;font-weight:700}
.lv-e .lv-val{color:#f9a8d4}.lv-s .lv-val{color:var(--red)}.lv-t .lv-val{color:var(--green)}.lv-r .lv-val{color:var(--yellow)}.lv-o .lv-val{color:#a78bfa}
.cfms{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:12px}
.cf{font-family:'JetBrains Mono',monospace;font-size:.59rem;padding:3px 9px;border-radius:8px;border:1.5px solid;font-weight:700}
.cf-ok{color:var(--green);border-color:rgba(16,185,129,.25);background:rgba(16,185,129,.07)}
.cf-no{color:var(--dim);border-color:var(--muted);background:transparent}
.cf-w{color:var(--orange);border-color:rgba(249,115,22,.25);background:rgba(249,115,22,.06)}
.cf-g{color:#a78bfa;border-color:rgba(167,139,250,.3);background:rgba(167,139,250,.06)}
.srow{display:flex;align-items:center;gap:12px}
.slbl{font-family:'Fredoka One',sans-serif;font-size:.72rem;color:var(--dim);white-space:nowrap;width:55px}
.strack{flex:1;height:8px;background:var(--s3);border-radius:4px;overflow:hidden}
.sfill{height:100%;border-radius:4px;transition:width .8s cubic-bezier(.34,1.56,.64,1)}
.snum{font-family:'Fredoka One',sans-serif;font-size:.95rem;white-space:nowrap;width:60px;text-align:right}
.dettog{display:inline-flex;align-items:center;gap:5px;margin-top:10px;font-family:'Nunito',sans-serif;font-size:.68rem;font-weight:800;color:rgba(167,139,250,.5);cursor:pointer;transition:color .18s;border:none;background:transparent;padding:0}
.dettog:hover{color:#a78bfa}
.detbox{display:none;margin-top:10px;background:var(--s2);border:1.5px solid var(--border);border-radius:12px;padding:13px;font-family:'JetBrains Mono',monospace;font-size:.63rem;color:var(--dim);line-height:1.9}
.detbox.open{display:block}
.panel{background:var(--s1);border:2px solid var(--border);border-radius:18px;padding:20px;margin-bottom:14px}
.panel-ttl{font-family:'Fredoka One',sans-serif;font-size:1rem;letter-spacing:.05em;color:#a78bfa;margin-bottom:14px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.tbl{width:100%;border-collapse:collapse}
.tbl th{font-family:'JetBrains Mono',monospace;font-size:.56rem;color:var(--dim);letter-spacing:.07em;text-transform:uppercase;padding:7px 9px;text-align:left;border-bottom:1.5px solid var(--border)}
.tbl td{font-family:'JetBrains Mono',monospace;font-size:.68rem;padding:8px 9px;border-bottom:1px solid rgba(124,58,237,.07);vertical-align:middle}
.tbl tr:hover td{background:rgba(124,58,237,.04)}
.buy{color:var(--green);font-weight:800}.sell{color:var(--red);font-weight:800}
.pos-pnl{font-weight:800}.pos-pnl.pos{color:var(--green)}.pos-pnl.neg{color:var(--red)}
.action-btn{padding:4px 9px;border:1.5px solid;border-radius:8px;font-family:'Nunito',sans-serif;font-size:.68rem;font-weight:800;cursor:pointer;transition:all .2s}
.close-btn{background:rgba(239,68,68,.1);border-color:rgba(239,68,68,.3);color:var(--red)}.close-btn:hover{background:rgba(239,68,68,.2)}
.share-btn{background:rgba(124,58,237,.1);border-color:rgba(124,58,237,.3);color:#a78bfa}.share-btn:hover{background:rgba(124,58,237,.2)}
.monitor-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.mon-card{background:var(--s2);border:2px solid var(--border);border-radius:14px;padding:14px;position:relative;overflow:hidden;animation:card-pop .3s ease}
.mon-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px}
.mon-card.buy::before{background:var(--green)}.mon-card.sell::before{background:var(--red)}
.mon-card:hover{border-color:var(--border2);transform:translateY(-3px)}
.mon-sym{font-family:'Fredoka One',sans-serif;font-size:1rem;margin-bottom:6px}
.mon-row{display:flex;justify-content:space-between;font-family:'JetBrains Mono',monospace;font-size:.62rem;color:var(--dim);margin-bottom:3px}
.mon-row span:last-child{color:var(--text);font-weight:700}
.mon-status{margin-top:8px;padding:4px 10px;border-radius:8px;font-family:'JetBrains Mono',monospace;font-size:.6rem;font-weight:700;text-align:center;background:rgba(245,158,11,.12);color:var(--yellow);border:1px solid rgba(245,158,11,.3);animation:pulse-y 2s infinite}
@keyframes pulse-y{0%,100%{opacity:1}50%{opacity:.45}}
.trade-form{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px;margin-bottom:16px}
.tf-group{display:flex;flex-direction:column;gap:6px}
.tf-lbl{font-family:'JetBrains Mono',monospace;font-size:.6rem;color:var(--dim);letter-spacing:.08em;text-transform:uppercase;font-weight:700}
.tf-inp{background:var(--s2);border:1.5px solid var(--muted);border-radius:10px;color:var(--text);padding:9px 12px;font-size:.82rem;font-family:'Nunito',sans-serif;font-weight:700;outline:none;transition:border-color .2s;width:100%}
.tf-inp:focus{border-color:rgba(124,58,237,.5)}
.trade-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.trade-btn{padding:10px 20px;border:none;border-radius:12px;font-family:'Nunito',sans-serif;font-size:.82rem;font-weight:800;cursor:pointer;transition:all .2s}
.tb-save{background:linear-gradient(135deg,var(--purple),var(--pink));color:#fff;box-shadow:0 4px 16px rgba(124,58,237,.35)}.tb-save:hover{transform:translateY(-2px)}
.tb-on{background:rgba(16,185,129,.12);border:2px solid rgba(16,185,129,.35);color:var(--green)}
.tb-off{background:rgba(239,68,68,.1);border:2px solid rgba(239,68,68,.3);color:var(--red)}
.tb-chk{background:rgba(56,189,248,.1);border:2px solid rgba(56,189,248,.3);color:var(--blue)}
.bal-chip{display:flex;align-items:center;gap:7px;background:rgba(16,185,129,.07);border:1.5px solid rgba(16,185,129,.2);border-radius:10px;padding:8px 14px;font-family:'JetBrains Mono',monospace;font-size:.72rem;color:var(--green);font-weight:700}
.t-status{font-family:'JetBrains Mono',monospace;font-size:.7rem;padding:8px 14px;border-radius:10px;font-weight:700;margin-top:8px;display:none}
.t-status.ok{background:rgba(16,185,129,.1);border:1.5px solid rgba(16,185,129,.3);color:var(--green);display:block}
.t-status.err{background:rgba(239,68,68,.1);border:1.5px solid rgba(239,68,68,.3);color:var(--red);display:block}
.info-box{border-radius:12px;padding:12px 14px;font-size:.73rem;font-weight:700;line-height:1.6;margin-bottom:12px}
.info-blue{background:rgba(14,165,233,.07);border:1.5px solid rgba(14,165,233,.2);color:rgba(56,189,248,.8)}
.info-red{background:rgba(239,68,68,.06);border:1.5px solid rgba(239,68,68,.2);color:rgba(239,68,68,.8)}
.share-modal{position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:999;display:none;align-items:center;justify-content:center;backdrop-filter:blur(6px)}
.share-modal.show{display:flex}
.share-card{background:var(--s1);border:2px solid var(--border2);border-radius:20px;padding:28px;width:320px;max-width:95vw;text-align:center}
.sh-title{font-family:'Fredoka One',sans-serif;font-size:1.3rem;margin-bottom:16px;color:#a78bfa}
.sh-row{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--border);font-family:'JetBrains Mono',monospace;font-size:.72rem}
.sh-row:last-of-type{border-bottom:none}
.sh-lbl{color:var(--dim)}.sh-val{color:var(--text);font-weight:700}
.sh-close{margin-top:14px;padding:9px 24px;border:none;border-radius:10px;background:var(--s3);color:var(--dim);font-family:'Nunito',sans-serif;font-size:.82rem;font-weight:700;cursor:pointer}
.log-wrap{background:var(--s1);border:2px solid var(--border);border-radius:18px;overflow:hidden}
.log-hdr{padding:13px 18px;border-bottom:1.5px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.log-ttl{font-family:'Fredoka One',sans-serif;font-size:.9rem;letter-spacing:.05em;color:#a78bfa}
.log-sub{font-family:'JetBrains Mono',monospace;font-size:.58rem;color:var(--dim);font-weight:700}
.log-body{padding:13px 18px;max-height:500px;overflow-y:auto;font-family:'JetBrains Mono',monospace;font-size:.67rem;line-height:1.95;color:var(--dim)}
.log-body::-webkit-scrollbar{width:4px}.log-body::-webkit-scrollbar-thumb{background:var(--muted);border-radius:2px}
.ll-s{color:var(--green)}.ll-e{color:var(--red)}.ll-i{color:rgba(56,189,248,.7)}.ll-t{color:#f9a8d4}.ll-m{color:var(--yellow)}
.diag-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:8px;margin-bottom:14px}
.dg{background:var(--s2);border:1.5px solid var(--border);border-radius:12px;padding:10px 12px;transition:all .2s}
.dg:hover{border-color:var(--border2);transform:translateY(-2px)}
.dg-lbl{font-family:'JetBrains Mono',monospace;font-size:.52rem;color:var(--dim);letter-spacing:.06em;margin-bottom:5px;text-transform:uppercase;font-weight:700}
.dg-val{font-family:'Fredoka One',sans-serif;font-size:1.6rem;line-height:1}
.toast{position:fixed;bottom:26px;left:50%;transform:translateX(-50%) translateY(100px);background:var(--s2);border:2px solid var(--border2);border-radius:16px;padding:12px 22px;font-family:'Nunito',sans-serif;font-size:.85rem;font-weight:800;box-shadow:0 18px 50px rgba(0,0,0,.6);opacity:0;transition:all .4s cubic-bezier(.34,1.56,.64,1);pointer-events:none;z-index:9999;white-space:nowrap}
.toast.show{transform:translateX(-50%) translateY(0);opacity:1}
.toast.bt{border-color:rgba(16,185,129,.4);color:var(--green)}.toast.st{border-color:rgba(239,68,68,.4);color:var(--red)}.toast.tt{border-color:rgba(249,115,22,.4);color:var(--orange)}
@media(max-width:820px){.stats-grid{grid-template-columns:1fr 1fr 1fr}.prices-grid{grid-template-columns:repeat(3,1fr)}.hdr-in,.sec,.tab-wrap{padding:0 13px}.prog{padding:7px 13px}.snum{display:none}.lvl-grid{grid-template-columns:1fr 1fr}.trade-form{grid-template-columns:1fr}}
@media(max-width:480px){.stats-grid{grid-template-columns:1fr 1fr}.prices-grid{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<div class="bg-glow"></div>
<div class="pb" id="pb">⏸ SCANNER PAUSED — HIT RESUME! 🚀</div>
<header class="hdr">
  <div class="hdr-glow"></div>
  <div class="hdr-in">
    <div class="brand"><span class="brand-icon">📡</span><div><div class="brand-name">CRT Scanner</div><div class="brand-sub">MEXC USDT PERP · CROSS MARGIN · UP TO 500X</div></div></div>
    <div class="scan-pill"><div class="sdot" id="sdot"></div><span class="stxt" id="stxt">SCANNING...</span></div>
    <div class="hdr-right">
      <span class="snum" id="snum">SCAN #0</span>
      <button class="tbtn on" id="tbtn" onclick="toggleScanner()">⏹ Stop</button>

<button id="autoTradeBtn"
        class="tbtn off"
        onclick="toggleAutoTrade()">
    AUTO TRADE OFF
</button>

      <button class="obtn" onclick="logout()">👋 Exit</button>
    </div>
  </div>
</header>
<div class="prog"><div class="prog-in">
  <span class="prog-lbl" id="cpair">🔍 Initialising...</span>
  <div class="prog-track"><div class="prog-fill" id="pfill" style="width:0%"></div></div>
  <span class="prog-cnt" id="pcnt">0/0</span>
</div></div>
<div class="sec">
  <div class="sec-hdr"><span class="sec-ttl">📈 Live Prices</span><div class="sec-line"></div><span class="sec-note" id="pupd">–</span></div>
  <div class="prices-grid" id="pgrid"><div class="pc" style="min-height:70px"></div><div class="pc" style="min-height:70px"></div><div class="pc" style="min-height:70px"></div><div class="pc" style="min-height:70px"></div><div class="pc" style="min-height:70px"></div><div class="pc" style="min-height:70px"></div></div>
</div>
<div class="sec" style="margin-top:12px">
  <div class="stats-grid">
    <div class="sc s0"><div class="sc-lbl">Signals</div><div class="sc-val" id="st">0</div><div class="sc-sub">All time</div></div>
    <div class="sc s1"><div class="sc-lbl">🟢 Buy</div><div class="sc-val" style="color:var(--green)" id="sb">0</div></div>
    <div class="sc s2"><div class="sc-lbl">🔴 Sell</div><div class="sc-val" style="color:var(--red)" id="ss">0</div></div>
    <div class="sc s3"><div class="sc-lbl">Scans</div><div class="sc-val" style="color:var(--blue)" id="sc2">0</div><div class="sc-sub" id="sl2">–</div></div>
    <div class="sc s4"><div class="sc-lbl">👁 Monitoring</div><div class="sc-val" style="color:var(--yellow)" id="smon">0</div><div class="sc-sub">manip phase</div></div>
  </div>
</div>
<div class="tab-wrap">
  <div class="tabs">
    <button class="tab active" onclick="sw('signals',this)">📊 Signals</button>
    <button class="tab" onclick="sw('trades',this)">💹 Live Trades</button>
    <button class="tab" onclick="sw('monitor',this)">👁 Monitor</button>
    <button class="tab" onclick="sw('history',this)">📜 History</button>
    <button class="tab" onclick="sw('trade-cfg',this)">🤖 Auto-Trade</button>
    <button class="tab" onclick="sw('log',this)">🖥️ Log</button>
  </div>
  <!-- SIGNALS -->
  <div id="tab-signals">
    <div class="frow">
      <div class="ftitle">🎯 CRT Model #1</div>
      <div class="fgrp">
        <select class="fsel" id="fd" onchange="renderSigs()"><option value="">All</option><option value="BUY">🟢 BUY</option><option value="SELL">🔴 SELL</option></select>
        <select class="fsel" id="fg" onchange="renderSigs()"><option value="">All Grades</option><option value="A+">⭐ A+</option><option value="A">A</option><option value="B">B+</option></select>
        <select class="fsel" id="ftf" onchange="renderSigs()"><option value="">All TFs</option><option value="Day1">1D</option><option value="Hour4">4H</option><option value="Hour3">3H</option><option value="Hour2">2H</option><option value="Min60">1H</option></select>
      </div>
    </div>
    <div class="sig-list" id="slist"><div class="empty"><div class="empty-ico">🔭</div><div class="empty-t">Scanning the galaxy...</div><div class="empty-s">Hunting CRT Model #1 setups. TBS body close mandatory. Min 2R. 🎯</div></div></div>
  </div>
  <!-- LIVE TRADES -->
  <div id="tab-trades" style="display:none">
    <div class="panel">
      <div class="panel-ttl">💹 Running Trades <span id="trades-count" style="font-family:'JetBrains Mono',monospace;font-size:.72rem;color:var(--dim)">(0)</span><button class="action-btn tb-chk" style="margin-left:auto;border:none;padding:6px 14px" onclick="fetchPnl()">🔄 Refresh</button></div>
      <div id="live-trades-wrap"><div class="empty" style="padding:40px"><div class="empty-ico">😴</div><div class="empty-t">No open trades</div></div></div>
    </div>
  </div>
  <!-- MONITOR -->
  <div id="tab-monitor" style="display:none">
    <div class="panel">
      <div class="panel-ttl">👁 Manipulation Monitor <span id="mon-count" style="font-family:'JetBrains Mono',monospace;font-size:.72rem;color:var(--dim)">(0/4)</span></div>
      <div id="monitor-wrap"><div class="empty" style="padding:40px"><div class="empty-ico">🌙</div><div class="empty-t">Nothing monitored yet</div><div class="empty-s">Pairs in manipulation phase appear here automatically</div></div></div>
    </div>
  </div>
  <!-- HISTORY -->
  <div id="tab-history" style="display:none">
    <div class="panel">
      <div class="panel-ttl">📜 Recent Trades (Last 10)</div>
      <div id="history-wrap"><div class="empty" style="padding:40px"><div class="empty-ico">📭</div><div class="empty-t">No completed trades yet</div></div></div>
    </div>
  </div>
  <!-- AUTO-TRADE -->
  <div id="tab-trade-cfg" style="display:none">
    <div class="panel">
      <div class="panel-ttl">🤖 Auto-Trade Settings <span id="trade-badge" style="font-size:.7rem;padding:3px 10px;border-radius:8px;background:rgba(239,68,68,.1);border:1.5px solid rgba(239,68,68,.3);color:var(--red);font-family:'JetBrains Mono',monospace;font-weight:700">DISABLED</span></div>
      <div class="trade-form">
        <div class="tf-group"><div class="tf-lbl">MEXC API Key</div><input class="tf-inp" type="text" id="t-apikey" placeholder="Your API key"/></div>
        <div class="tf-group"><div class="tf-lbl">MEXC Secret Key</div><input class="tf-inp" type="password" id="t-secret" placeholder="Your secret key"/></div>
        <div class="tf-group"><div class="tf-lbl">Risk per Trade (%)</div><input class="tf-inp" type="number" id="t-risk" value="1" min="0.1" max="5" step="0.1"/></div>
        <div class="tf-group"><div class="tf-lbl">Account Balance</div><div class="bal-chip">💰 $<span id="bal-val">–</span> USDT</div></div>
      </div>
      <div class="info-box info-blue">ℹ️ <b>Risk model:</b> Cross margin · 20% of balance per trade · Max 4 simultaneous trades (80% total) · Auto-leverage 10x–500x · SL capped at 100% of margin per trade</div>
      <div class="trade-actions">
        <button class="trade-btn tb-save" onclick="saveTradeConfig()">💾 Save</button>
        <button class="trade-btn tb-on" id="t-enable-btn" onclick="enableTrade(true)">▶ Enable</button>
        <button class="trade-btn tb-off" id="t-disable-btn" onclick="enableTrade(false)" style="display:none">⏹ Disable</button>
        <button class="trade-btn tb-chk" onclick="fetchBalance()">🔄 Balance</button>
      </div>
      <div class="t-status" id="trade-msg"></div>
      <div class="info-box info-red">⚠️ Real money risk. The bot uses cross margin with auto-calculated leverage (up to 500x). SL never exceeds 100% of your 20% margin. Start with 0.5–1% risk setting and monitor closely.</div>
    </div>
  </div>
  <!-- LOG -->
  <div id="tab-log" style="display:none">
    <div style="background:var(--s1);border:2px solid var(--border);border-radius:18px;padding:18px 20px;margin-bottom:14px">
      <div style="font-family:'Fredoka One',sans-serif;font-size:.9rem;letter-spacing:.05em;color:#a78bfa;margin-bottom:14px">🔬 Gate Diagnostics</div>
      <div class="diag-grid" id="diag-grid"></div>
    </div>
    <div class="log-wrap">
      <div class="log-hdr"><span class="log-ttl">🖥️ Live Log</span><span class="log-sub">UPDATES EVERY 3S</span></div>
      <div class="log-body" id="lbody">Loading...</div>
    </div>
  </div>
</div>
<div class="share-modal" id="share-modal">
  <div class="share-card">
    <div class="sh-title">📸 Trade Card</div>
    <div id="sh-content"></div>
    <button class="sh-close" onclick="document.getElementById('share-modal').classList.remove('show')">✕ Close</button>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
(function(){
'use strict';
let allSigs=[],toastT,activeTab='signals',tick=0,lastCount=0;
const $=id=>document.getElementById(id);
function toast(m,t,d=3500){const el=$("toast");el.textContent=m;el.className="toast show"+(t==="buy"?" bt":t==="sell"?" st":t==="trade"?" tt":"");clearTimeout(toastT);toastT=setTimeout(()=>el.classList.remove("show"),d);}
function scoreColor(s){return s>=88?"#fbbf24":s>=75?"#a78bfa":s>=60?"var(--blue)":s>=45?"var(--orange)":"var(--dim)";}
function fmt(v){if(v===null||v===undefined||v==="–"||v===false||v==="false")return"–";const n=Number(v);if(isNaN(n))return String(v);if(n>=10000)return n.toLocaleString(undefined,{maximumFractionDigits:2});if(n>=1)return n.toFixed(4);return n.toFixed(6);}
function fmtP(v){const n=Number(v);if(!n)return"–";if(n>=10000)return"$"+n.toLocaleString(undefined,{maximumFractionDigits:2});if(n>=1)return"$"+n.toFixed(4);return"$"+n.toFixed(6);}
const TFM={"Day1":"1D","Hour4":"4H","Hour3":"3H","Hour2":"2H","Min60":"1H","Min45":"45m","Min30":"30m","Min15":"15m","Min10":"10m","Min5":"5m","Min4":"4m","Min3":"3m","Min2":"2m","Min1":"1m"};
const TOP=["BTC_USDT","ETH_USDT","SOL_USDT","BNB_USDT","XRP_USDT","DOGE_USDT"];
async function fetchPrices(){try{const r=await fetch("/api/prices");const data=await r.json();$("pupd").textContent="Updated "+new Date().toLocaleTimeString();$("pgrid").innerHTML=TOP.map(sym=>{const d=data[sym],name=sym.replace("_USDT","");if(!d)return`<div class="pc"><div class="pc-sym">${name}</div><div class="pc-price" style="color:var(--dim)">–</div></div>`;const up=d.change>=0;return`<div class="pc ${up?"up":"dn"}"><div class="pc-sym">${name}/USDT</div><div class="pc-price ${up?"up":"dn"}">${fmtP(d.price)}</div><span class="pc-chg ${up?"up":"dn"}">${up?"▲":"▼"} ${Math.abs(d.change).toFixed(2)}%</span></div>`;}).join("");}catch{}}
function buildCard(s,idx){const dir=(s.direction||"BUY").toUpperCase();const sc=s.score||0,gr=s.grade||"–";const gc={"A+":"gAp","A":"gA","B":"gB","C":"gC","D":"gD"}[gr]||"gD";const crtTF=TFM[s.tf]||s.tf||"–";const obTF=TFM[s.ob_tf]||s.ob_tf||"–";const isND=s.tf==="Day1";const zt=s.zone_type||s.ob_zone||"–";const isAplus=gr==="A+";const details=(s.details||[]).join("\n");const cf=(ok,l)=>`<span class="cf ${ok?"cf-ok":"cf-no"}">${ok?"✓":"✗"} ${l}</span>`;const cfw=(ok,l)=>`<span class="cf ${ok?"cf-ok":"cf-w"}">${ok?"✓":"⚠"} ${l}</span>`;const cfg=(ok,l)=>`<span class="cf ${ok?"cf-g":"cf-no"}">${ok?"💎":"◇"} ${l}</span>`;return`<div class="scard ${dir.toLowerCase()}"><div class="card-hdr"><span class="dtag ${dir}">${dir}</span><span class="csym">${s.symbol||"–"}</span><div class="chips"><span class="chip chip-tf">${crtTF} CRT</span>${!isND&&s.ob_tf&&s.ob_tf!=="N/A"?`<span class="chip chip-ob">${zt} ${obTF}</span>`:""}<span class="chip chip-tr ${s.trend}">${s.trend}</span>${isAplus?"<span class="chip chip-aplus">⭐ A+</span>":""}${s.from_monitor?"<span class="chip" style="color:#fbbf24;border-color:rgba(251,191,36,.3);background:rgba(251,191,36,.07)">👁 Monitored</span>":""}</div><span class="gtag ${gc}">${gr}</span><span class="cts">${s.timestamp||""}</span></div><div class="lvl-grid"><div class="lv lv-e"><div class="lv-lbl">🎯 Entry (TBS Open)</div><div class="lv-val">${fmt(s.entry)}</div></div><div class="lv lv-e" style="border-color:rgba(167,139,250,.2)"><div class="lv-lbl">TBS TF</div><div class="lv-val" style="color:#a78bfa">${TFM[s.tbs_tf]||s.tbs_tf||"–"}</div></div><div class="lv lv-s"><div class="lv-lbl">🛑 Stop Loss</div><div class="lv-val">${fmt(s.sl)}</div></div><div class="lv lv-t"><div class="lv-lbl">🎯 Take Profit</div><div class="lv-val">${fmt(s.tp)}</div></div><div class="lv lv-r"><div class="lv-lbl">📊 RR</div><div class="lv-val">${s.rr}R</div></div><div class="lv"><div class="lv-lbl">CRH</div><div class="lv-val" style="color:#f9a8d4">${fmt(s.crh)}</div></div><div class="lv"><div class="lv-lbl">CRL</div><div class="lv-val" style="color:#f9a8d4">${fmt(s.crl)}</div></div>${s.ob_top&&s.ob_top!=="–"?`<div class="lv lv-o"><div class="lv-lbl">${zt} Top</div><div class="lv-val">${fmt(s.ob_top)}</div></div>`:""}${s.ob_bot&&s.ob_bot!=="–"?`<div class="lv lv-o"><div class="lv-lbl">${zt} Bot</div><div class="lv-val">${fmt(s.ob_bot)}</div></div>`:""}</div><div class="cfms">${cf(s.tbs_found,"TBS · "+(TFM[s.tbs_tf]||s.tbs_tf||"–"))}${cfw(s.choch_found,"CHOCH")}${cfg(s.ob_zone&&s.ob_zone!=="–",s.ob_zone||"Zone")}${cfw(s.continuous,"Structure")}</div><div class="srow"><span class="slbl">Score</span><div class="strack"><div class="sfill" style="width:${sc}%;background:${scoreColor(sc)}"></div></div><span class="snum" style="color:${scoreColor(sc)}">${sc}/100</span></div>${details?`<button class="dettog" onclick="toggleDet(${idx})">▶ Score Breakdown</button><div class="detbox" id="det-${idx}"><pre style="font-size:.62rem;line-height:1.85;white-space:pre-wrap">${details}</pre></div>`:""}</div>`;}
window.toggleDet=function(i){const b=$("det-"+i);if(!b)return;b.classList.toggle("open");const t=b.previousElementSibling;if(t)t.textContent=b.classList.contains("open")?"▼ Score Breakdown":"▶ Score Breakdown";};
window.renderSigs=function(){const dF=$("fd").value,gF=$("fg").value,tfF=$("ftf").value;let f=allSigs.filter(s=>{if(dF&&s.direction!==dF)return false;if(tfF&&s.tf!==tfF)return false;if(gF){if(gF==="A+"&&s.grade!=="A+")return false;if(gF==="A"&&s.grade!=="A")return false;if(gF==="B"&&!["A+","A","B"].includes(s.grade))return false;}return true;});const list=$("slist");if(!f.length){list.innerHTML="<div class="empty"><div class="empty-ico">🔭</div><div class="empty-t">Scanning the galaxy...</div><div class="empty-s">Hunting CRT Model #1 setups. TBS body close mandatory. Min 2R.</div></div>";return;}list.innerHTML=f.slice(0,100).map((s,i)=>buildCard(s,i)).join("");};
async function fetchSigs(){try{const r=await fetch("/api/signals?limit=200");const data=await r.json();allSigs=data;if(data.length>lastCount&&lastCount>0){const n=data[0];toast(`🎯 ${n.direction} ${n.symbol} · ${n.score}/100 ${n.grade} · ${n.rr}R`,n.direction==="BUY"?"buy":"sell");}lastCount=data.length;renderSigs();}catch{}}
async function fetchStats(){try{const r=await fetch("/api/stats");const d=await r.json();$("st").textContent=d.total||0;$("sb").textContent=d.buys||0;$("ss").textContent=d.sells||0;}catch{}}
async function fetchState(){try{const r=await fetch("/api/scan-state");const d=await r.json();const pct=d.total_pairs>0?Math.round(d.pairs_done/d.total_pairs*100):0;$("pfill").style.width=pct+"%";$("pcnt").textContent=`${d.pairs_done}/${d.total_pairs}`;$("cpair").textContent=d.current_pair?`🔍 ${d.current_pair}`:"⏳ Waiting...";$("sc2").textContent=d.scan_count||0;$("sl2").textContent=d.last_scan?`Last: ${d.last_scan}`:"–";$("snum").textContent=`Scan #${d.scan_count||0}`;const en=d.enabled!==false;$("tbtn").textContent=en?"⏹ Stop":"▶ Resume";$("tbtn").className="tbtn "+(en?"on":"off");$("sdot").className="sdot"+(en?"":" off");$("stxt").textContent=en?"SCANNING...":"PAUSED";$("stxt").className="stxt"+(en?"":" off");$("pb").className="pb"+(en?"":" show");}catch{}}
async function fetchMonitor(){if(activeTab!=="monitor")return;try{const r=await fetch("/api/monitor");const data=await r.json();$("smon").textContent=data.length;$("mon-count").textContent=`(${data.length}/4)`;const wrap=$("monitor-wrap");if(!wrap)return;if(!data.length){wrap.innerHTML="<div class="empty" style="padding:40px"><div class="empty-ico">🌙</div><div class="empty-t">Nothing monitored</div><div class="empty-s">Pairs in manipulation phase appear here automatically</div></div>";return;}wrap.innerHTML=`<div class="monitor-grid">${data.map(m=>{const dir=(m.direction||"BUY").toUpperCase();const tf=TFM[m.crt_tf]||m.crt_tf||"–";return`<div class="mon-card ${dir.toLowerCase()}"><div class="mon-sym">${dir==="BUY"?"🟢":"🔴"} ${m.symbol||"–"}</div><div class="mon-row"><span>CRT TF</span><span>${tf}</span></div><div class="mon-row"><span>Key Level</span><span>${m.kl_type||"–"}</span></div><div class="mon-row"><span>Trend</span><span>${m.trend||"–"}</span></div><div class="mon-row"><span>CRH</span><span>${fmt(m.crh)}</span></div><div class="mon-row"><span>CRL</span><span>${fmt(m.crl)}</span></div><div class="mon-row"><span>Zone</span><span>${m.zone_name||"–"}</span></div><div class="mon-row"><span>Added</span><span>${m.added_at||"–"}</span></div><div class="mon-status">⏳ AWAITING C2 CLOSE</div></div>`;}).join("")}</div>`;}catch{}}
window.fetchPnl=async function(){if(activeTab!=="trades")return;try{const[tr,pnl]=await Promise.all([fetch("/api/trades").then(r=>r.json()),fetch("/api/pnl").then(r=>r.json())]);$("trades-count").textContent=`(${tr.length})`;const wrap=$("live-trades-wrap");if(!wrap)return;const pnlMap={};(pnl.positions||[]).forEach(p=>pnlMap[p.symbol]=p);if(!tr.length){wrap.innerHTML="<div class="empty" style="padding:40px"><div class="empty-ico">😴</div><div class="empty-t">No open trades</div></div>";return;}wrap.innerHTML=`<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Pair</th><th>Dir</th><th>Entry</th><th>Current</th><th>SL</th><th>TP</th><th>RR</th><th>Lev</th><th>Margin</th><th>Live PnL</th><th>ROI%</th><th>Grade</th><th>Action</th></tr></thead><tbody>${tr.map(t=>{const live=pnlMap[t.symbol]||{};const pv=live.pnl||0;const roi=live.roi_pct||0;const cur=live.current||0;const lev=live.leverage||t.leverage||"–";const margin=(live.margin||0).toFixed(2);return`<tr><td style="font-weight:800;color:var(--text)">${t.symbol}</td><td class="${t.direction==="BUY"?"buy":"sell"}">${t.direction}</td><td>${fmt(t.entry)}</td><td style="color:var(--yellow)">${cur?fmt(cur):"–"}</td><td style="color:var(--red)">${fmt(t.sl)}</td><td style="color:var(--green)">${fmt(t.tp)}</td><td style="color:var(--yellow)">${t.rr}R</td><td style="color:#a78bfa">${lev}x</td><td>$${margin}</td><td class="pos-pnl ${pv>=0?"pos":"neg"}">${pv>=0?"+":""}${pv.toFixed(2)}</td><td class="pos-pnl ${roi>=0?"pos":"neg"}">${roi>=0?"+":""}${roi.toFixed(2)}%</td><td style="color:${scoreColor(t.score||0)};font-family:'Fredoka One',sans-serif">${t.grade||"–"}</td><td><button class="action-btn close-btn" onclick="closeTrade('${t.symbol}')">✕</button></td></tr>`;}).join("")}</tbody></table></div>`;}catch{}};
async function fetchHistory(){if(activeTab!=="history")return;try{const r=await fetch("/api/recent-trades");const data=await r.json();const wrap=$("history-wrap");if(!wrap)return;if(!data.length){wrap.innerHTML="<div class="empty" style="padding:40px"><div class="empty-ico">📭</div><div class="empty-t">No completed trades yet</div></div>";return;}wrap.innerHTML=`<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Pair</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP</th><th>RR</th><th>Grade</th><th>Status</th><th>Opened</th><th>Share</th></tr></thead><tbody>${data.map((t,i)=>`<tr><td style="font-weight:800;color:var(--text)">${t.symbol}</td><td class="${t.direction==="BUY"?"buy":"sell"}">${t.direction}</td><td>${fmt(t.entry)}</td><td style="color:var(--red)">${fmt(t.sl)}</td><td style="color:var(--green)">${fmt(t.tp)}</td><td style="color:var(--yellow)">${t.rr}R</td><td style="color:${scoreColor(t.score||0)};font-family:'Fredoka One',sans-serif">${t.grade||"–"}</td><td style="color:var(--dim);font-size:.62rem">${t.status||"–"}</td><td style="color:var(--dim)">${(t.opened_at||"").replace(" UTC","")}</td><td><button class="action-btn share-btn" onclick="showShare(${i})">📸</button></td></tr>`).join("")}</tbody></table></div>`;window._histData=data;}catch{}}
window.showShare=function(i){const t=(window._histData||[])[i];if(!t)return;const modal=$("share-modal");const content=$("sh-content");const dir=t.direction==="BUY"?"🟢 LONG":"🔴 SHORT";content.innerHTML=`<div style="font-size:1.3rem;margin-bottom:10px">${dir} ${t.symbol}</div><div class="sh-row"><span class="sh-lbl">Entry</span><span class="sh-val">${fmt(t.entry)}</span></div><div class="sh-row"><span class="sh-lbl">Stop Loss</span><span class="sh-val" style="color:var(--red)">${fmt(t.sl)}</span></div><div class="sh-row"><span class="sh-lbl">Take Profit</span><span class="sh-val" style="color:var(--green)">${fmt(t.tp)}</span></div><div class="sh-row"><span class="sh-lbl">Risk:Reward</span><span class="sh-val" style="color:var(--yellow)">${t.rr}R</span></div><div class="sh-row"><span class="sh-lbl">Grade</span><span class="sh-val">${t.grade||"–"}</span></div><div class="sh-row"><span class="sh-lbl">Strategy</span><span class="sh-val">CRT Model #1</span></div><div class="sh-row"><span class="sh-lbl">Status</span><span class="sh-val">${t.status||"–"}</span></div>`;modal.classList.add("show");};
async function fetchLog(){if(activeTab!=="log")return;try{const r=await fetch("/api/log");const d=await r.json();$("lbody").innerHTML=d.log.map(l=>{const cls=l.includes("🎯")||l.includes("SIGNAL")?"ll-s":l.includes("🤖")||l.includes("TRADE")?"ll-t":l.includes("❌")||l.includes("Error")?"ll-e":l.includes("👁")||l.includes("MONITOR")?"ll-m":l.includes("STATS")?"ll-s":"ll-i";return`<div class="${cls}">${l}</div>`;}).join("");}catch{}try{const r=await fetch("/api/diag");const d=await r.json();const labels={neutral:"😴 Neutral",not_continuous:"📉 Structure",no_obs:"📦 No OBs",not_at_key:"🎯 Not Key",not_in_zone:"📍 Zone",not_tapping:"👆 Tapping",no_crts:"🕯 No CRT",no_tbs:"🐢 No TBS",rr_low:"📊 Low RR","1d_no_crts":"1D NoCRT","1d_no_tbs":"1D NoTBS","1d_rr_low":"1D LowRR",passed:"✅ PASSED"};const colors={neutral:"var(--dim)",not_continuous:"var(--dim)",no_obs:"var(--orange)",not_at_key:"var(--orange)",not_in_zone:"var(--orange)",not_tapping:"var(--red)",no_crts:"var(--red)",no_tbs:"var(--red)",rr_low:"var(--orange)","1d_no_crts":"var(--dim)","1d_no_tbs":"var(--dim)","1d_rr_low":"var(--dim)",passed:"var(--green)"};const grid=$("diag-grid");if(grid)grid.innerHTML=Object.entries(d).map(([k,v])=>`<div class="dg"><div class="dg-lbl">${labels[k]||k}</div><div class="dg-val" style="color:${colors[k]||"var(--text)"}">${v}</div></div>`).join("");}catch{}}
async function loadTradeConfig(){try{const r=await fetch("/api/trade-config");const d=await r.json();if(d.api_key)$("t-apikey").value=d.api_key;$("t-risk").value=d.risk_pct||1;updateTradeBadge(d.enabled);}catch{}}
function updateTradeBadge(en){const b=$("trade-badge"),eb=$("t-enable-btn"),db=$("t-disable-btn");if(en){b.textContent="ENABLED";b.style.cssText="font-size:.7rem;padding:3px 10px;border-radius:8px;background:rgba(16,185,129,.12);border:1.5px solid rgba(16,185,129,.35);color:var(--green);font-family:JetBrains Mono,monospace;font-weight:700";eb.style.display="none";db.style.display="";}else{b.textContent="DISABLED";b.style.cssText="font-size:.7rem;padding:3px 10px;border-radius:8px;background:rgba(239,68,68,.1);border:1.5px solid rgba(239,68,68,.3);color:var(--red);font-family:JetBrains Mono,monospace;font-weight:700";eb.style.display="";db.style.display="none";}}
function showTradeMsg(msg,ok){const el=$("trade-msg");el.textContent=msg;el.className="t-status "+(ok?"ok":"err");setTimeout(()=>el.className="t-status",4000);}
window.saveTradeConfig=async function(){const cfg={api_key:$("t-apikey").value.trim(),api_secret:$("t-secret").value.trim(),risk_pct:parseFloat($("t-risk").value)||1};if(!cfg.api_key){showTradeMsg("❌ API key required",false);return;}try{const r=await fetch("/api/trade-config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(cfg)});const d=await r.json();if(d.ok){showTradeMsg("✅ Saved!",true);toast("💾 Saved!","trade");}else showTradeMsg("❌ Save failed",false);}catch{showTradeMsg("❌ Error",false);}};
window.enableTrade=async function(en){try{const r=await fetch("/api/trade-config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({enabled:en})});const d=await r.json();if(d.ok){updateTradeBadge(en);showTradeMsg(en?"✅ Auto-trade ENABLED!":"✅ Disabled",true);toast(en?"🤖 Auto-trade ON!":"⏹ Off","trade");}}catch{showTradeMsg("❌ Error",false);}};
window.fetchBalance=async function(){try{const r=await fetch("/api/balance");const d=await r.json();if(d.error)showTradeMsg("❌ "+d.error,false);else{$("bal-val").textContent=Number(d.balance).toFixed(2);showTradeMsg("✅ Balance loaded",true);}}catch{showTradeMsg("❌ Check API keys",false);}};
window.closeTrade=async function(sym){if(!confirm("Close "+sym+"?"))return;try{const r=await fetch("/api/trade-close",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({symbol:sym})});const d=await r.json();toast(d.ok?"✅ "+sym+" closed":"❌ "+d.message,"trade");await window.fetchPnl();}catch{toast("❌ Close failed","trade");}};
window.toggleScanner=async function(){try{const r=await fetch("/api/toggle-scanner",{method:"POST"});const d=await r.json();toast(d.enabled?"▶ Scanner on! 🚀":"⏹ Paused",d.enabled?"buy":"sell");await fetchState();}catch{}};
window.sw=function(tab,btn){activeTab=tab;document.querySelectorAll(".tab").forEach(b=>b.classList.remove("active"));btn.classList.add("active");["signals","trades","monitor","history","trade-cfg","log"].forEach(t=>{const el=$("tab-"+t);if(el)el.style.display=t===tab?"block":"none";});if(tab==="log")fetchLog();if(tab==="trades")window.fetchPnl();if(tab==="monitor")fetchMonitor();if(tab==="history")fetchHistory();if(tab==="trade-cfg")loadTradeConfig();};
window.logout=function(){fetch("/api/logout",{method:"POST"}).finally(()=>window.location.href="/");};
async function poll(){tick++;const ps=[fetchSigs(),fetchStats(),fetchState()];if(tick%2===0)ps.push(fetchPrices());if(activeTab==="log"&&tick%2===0)ps.push(fetchLog());if(activeTab==="monitor"&&tick%2===0)ps.push(fetchMonitor());if(activeTab==="trades"&&tick%3===0)ps.push(window.fetchPnl());if(activeTab==="history"&&tick%5===0)ps.push(fetchHistory());await Promise.all(ps);setTimeout(poll,3000);}
fetchPrices();loadTradeConfig();poll();
})();

window.toggleAutoTrade = async function () {
    try {
        const r = await fetch("/api/toggle-autotrade", {
            method: "POST"
        });

        const d = await r.json();

        const btn = document.getElementById("autoTradeBtn");

        if (d.enabled) {
            btn.innerText = "AUTO TRADE ON";
            btn.classList.remove("off");
            btn.classList.add("on");
            toast("Auto trading enabled 🚀", "buy");
        } else {
            btn.innerText = "AUTO TRADE OFF";
            btn.classList.remove("on");
            btn.classList.add("off");
            toast("Auto trading disabled ⛔", "sell");
        }

    } catch (e) {
        toast("Auto trade toggle failed", "sell");
    }
};

</script>
</body>
</html>"""


# ════════ FLASK ROUTES ════════════════════════════════════════════════

@app.route("/")
def root():
    token=request.cookies.get("session")
    if token and token in sessions:
        return make_response(DASHBOARD_HTML,200,{"Content-Type":"text/html"})
    return make_response(LOGIN_HTML,200,{"Content-Type":"text/html"})

@app.route("/dashboard")
def dashboard():
    token=request.cookies.get("session")
    if not token or token not in sessions:
        return make_response(LOGIN_HTML,200,{"Content-Type":"text/html"})
    return make_response(DASHBOARD_HTML,200,{"Content-Type":"text/html"})

@app.route("/api/login",methods=["POST"])
def api_login():
    data=request.get_json(silent=True) or {}
    if data.get("password")==DASHBOARD_PASSWORD:
        token=secrets.token_hex(32); sessions.add(token)
        resp=make_response(jsonify({"ok":True}))
        resp.set_cookie("session",token,max_age=86400*7,httponly=True,samesite="Lax")
        return resp
    return jsonify({"ok":False}),401

@app.route("/api/logout",methods=["POST"])
def api_logout():
    token=request.cookies.get("session"); sessions.discard(token)
    resp=make_response(jsonify({"ok":True})); resp.delete_cookie("session")
    return resp

@app.route("/api/toggle-scanner",methods=["POST"])
def api_toggle():
    with scan_lock:
        scan_state["enabled"]=not scan_state["enabled"]
        en=scan_state["enabled"]
    log(f"{'▶ RESUMED' if en else '⏸ PAUSED'} by user")
    return jsonify({"enabled":en})

@app.route("/api/signals")
def api_signals():
    limit=min(int(request.args.get("limit",200)),MAX_SIGNALS)
    return jsonify(list(signals)[:limit])

@app.route("/api/stats")
def api_stats():
    all_s=list(signals)
    return jsonify({"total":len(all_s),
                    "buys": sum(1 for s in all_s if s.get("direction")=="BUY"),
                    "sells":sum(1 for s in all_s if s.get("direction")=="SELL")})

@app.route("/api/scan-state")
def api_scan_state():
    with scan_lock:
        state = {k:v for k,v in scan_state.items() if k!="log"}
    state["diag"] = dict(diag)
    return jsonify(state)

@app.route("/api/log")
def api_log():
    with scan_lock: return jsonify({"log":list(scan_state["log"])})

@app.route("/api/prices")
def api_prices():
    out={}
    for sym in TOP_PAIRS:
        t=get_ticker(sym)
        if t: out[sym]=t
    return jsonify(out)

@app.route("/api/trade-config", methods=["GET","POST"])
def api_trade_config():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        with trade_lock:
            if "api_key"     in data: trade_config["api_key"]    = data["api_key"]
            if "api_secret"  in data: trade_config["api_secret"] = data["api_secret"]
            if "risk_pct"    in data: trade_config["risk_pct"]   = float(data["risk_pct"])
            if "max_trades"  in data: trade_config["max_trades"] = int(data["max_trades"])
            if "leverage"    in data: trade_config["leverage"]   = int(data["leverage"])
            if "enabled"     in data: trade_config["enabled"]    = bool(data["enabled"])
        log(f"⚙️ Trade config updated. Auto-trade: {'ON' if trade_config['enabled'] else 'OFF'}")
        return jsonify({"ok": True, "config": {k:v for k,v in trade_config.items() if k!="api_secret"}})
    # GET — return config (hide secret)
    cfg = {k: ("***" if k=="api_secret" and v else v) for k,v in trade_config.items()}
    return jsonify(cfg)

@app.route("/api/trades")
def api_trades():
    with trade_lock:
        return jsonify(list(open_trades.values()))

@app.route("/api/trade-close", methods=["POST"])
def api_trade_close():
    data   = request.get_json(silent=True) or {}
    symbol = data.get("symbol","")
    if not symbol: return jsonify({"ok":False,"error":"symbol required"}),400
    ok, msg = close_trade(symbol, reason="Manual (Dashboard)")
    return jsonify({"ok":ok,"message":msg})

@app.route("/api/balance")
def api_balance():
    bal, err = get_account_balance()
    return jsonify({"balance":bal,"error":err})

@app.route("/health")
def health():
    return jsonify({"status":"healthy","signals":len(signals),"scanning":scan_state["running"]}),200

@app.route("/api/diag")
def api_diag():
    return jsonify(dict(diag))

@app.route("/api/monitor")
def api_monitor():
    """Return pairs being monitored in manipulation phase."""
    with manip_lock:
        return jsonify(list(manip_monitor.values()))

@app.route("/api/recent-trades")
def api_recent_trades():
    """Return last 10 completed trades."""
    return jsonify(list(recent_trades))

@app.route("/api/pnl")
def api_pnl():
    """Get live PnL for all open positions."""
    data, err = mexc_request("GET", "/position/open_positions")
    if err or not data:
        return jsonify({"error": err or "No positions", "positions": []})
    positions = []
    for p in (data if isinstance(data, list) else []):
        positions.append({
            "symbol":    p.get("symbol",""),
            "direction": "BUY" if p.get("positionType")==1 else "SELL",
            "entry":     float(p.get("openAvgPrice",0)),
            "current":   float(p.get("closeAvgPrice",0) or p.get("currentPrice",0)),
            "size":      float(p.get("vol",0)),
            "leverage":  int(p.get("leverage",1)),
            "margin":    float(p.get("im",0)),
            "pnl":       float(p.get("unrealisedPnl",0)),
            "roi_pct":   round(float(p.get("unrealisedPnl",0)) /
                               max(float(p.get("im",1)),1) * 100, 2),
        })
    return jsonify({"positions": positions})

# ════════ STARTUP ═════════════════════════════════════════════════════

def start_scanner():
    t=threading.Thread(target=scanner_loop,daemon=True); t.start()
    m=threading.Thread(target=manip_monitor_loop,daemon=True); m.start()
    log("Scanner + manipulation monitor threads launched.")

with app.app_context():
    start_scanner()

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0",port=port,debug=False)


@app.route("/api/toggle-autotrade", methods=["POST"])
def toggle_autotrade():
    trade_config["enabled"] = not trade_config["enabled"]
    return jsonify({
        "success": True,
        "enabled": trade_config["enabled"]
    })
