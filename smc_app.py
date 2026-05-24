import os, json, time, secrets, requests, threading, hmac, hashlib, urllib.parse
from datetime import datetime, timezone, timedelta
from collections import deque
from flask import Flask, request, jsonify, make_response

LOCAL_TZ = timezone(timedelta(hours=1))   # UTC+1
app = Flask(__name__)

# ════════ CONFIG ══════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "7411219487")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "signal123")
MEXC_API_KEY       = os.environ.get("MEXC_API_KEY",    "")
MEXC_API_SECRET    = os.environ.get("MEXC_API_SECRET", "")

MAX_SIGNALS = 500
signals     = deque(maxlen=MAX_SIGNALS)
sessions    = set()

trade_config = {
    "enabled":    False,
    "api_key":    MEXC_API_KEY,
    "api_secret": MEXC_API_SECRET,
    "risk_pct":   1.0,
    "max_trades": 3,
    "leverage":   10,
}
open_trades = {}
trade_lock  = threading.Lock()
MEXC_FUTURES = "https://contract.mexc.com/api/v1/private"
MEXC_BASE    = "https://contract.mexc.com/api/v1/contract"

scan_state = {
    "running": False, "enabled": True, "current_pair": "",
    "pairs_done": 0, "total_pairs": 0, "scan_count": 0,
    "signals_found": 0, "last_scan": None,
    "log": deque(maxlen=200),
}
scan_lock = threading.Lock()

# Paper trading
paper_config = {
    "enabled": False, "auto_trade": False,
    "balance": 10000.0, "risk_pct": 1.0, "max_trades": 4,
}
paper_trades  = {}
paper_history = deque(maxlen=50)
paper_lock    = threading.Lock()
paper_stats   = {"total": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}

recent_trades = deque(maxlen=10)

diag = {
    "no_candles": 0, "neutral": 0, "no_bos": 0,
    "no_choch": 0,  "no_ob": 0, "no_fvg": 0,
    "rr_low": 0, "passed": 0, "score_low": 0,
}

# SMC timeframes
HTF_TFS = ["Day1", "Hour4", "Hour2"]       # for trend / BOS
MTF_TFS = ["Hour4", "Hour2", "Min60"]      # for OB / FVG / CHOCH
LTF_TFS = ["Min15", "Min5"]               # for entry confirmation

TOP_PAIRS = ["BTC_USDT","ETH_USDT","SOL_USDT","BNB_USDT","XRP_USDT","DOGE_USDT"]

TF_MINUTES = {
    "Day1": 1440, "Hour4": 240, "Hour3": 180, "Hour2": 120, "Min60": 60,
    "Min45": 45, "Min30": 30, "Min15": 15, "Min10": 10, "Min5": 5,
    "Min4": 4, "Min3": 3, "Min2": 2, "Min1": 1,
}

# ════════ HELPERS ════════════════════════════════════════════════════

def log(msg):
    ts = datetime.now(LOCAL_TZ).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"; print(line, flush=True)
    with scan_lock: scan_state["log"].appendleft(line)

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or "PASTE" in TELEGRAM_BOT_TOKEN: return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
        return r.status_code == 200
    except: return False

# ════════ MEXC API ════════════════════════════════════════════════════

def get_all_pairs():
    try:
        r = requests.get(f"{MEXC_BASE}/detail", timeout=15)
        data = r.json()
        if not data.get("success"): return []
        seen = set(); pairs = []
        for item in data.get("data", []):
            sym = item.get("symbol", "")
            if item.get("state") == 0 and sym.endswith("_USDT") and sym not in seen:
                seen.add(sym); pairs.append(sym)
        return sorted(pairs)
    except Exception as e:
        log(f"Pairs error: {e}"); return []

def get_candles(symbol, interval, limit=150):
    try:
        r = requests.get(f"{MEXC_BASE}/kline/{symbol}",
                         params={"interval": interval, "limit": limit}, timeout=10)
        data = r.json()
        if not data.get("success") or not data.get("data"): return []
        raw  = data["data"]
        out  = []
        times  = raw.get("time",  [])
        opens  = raw.get("open",  [])
        highs  = raw.get("high",  [])
        lows   = raw.get("low",   [])
        closes = raw.get("close", [])
        for i in range(len(times)):
            try:
                out.append({"time": int(times[i]), "open": float(opens[i]),
                            "high": float(highs[i]), "low": float(lows[i]),
                            "close": float(closes[i])})
            except: continue
        return out
    except: return []

def get_ticker(symbol):
    try:
        r = requests.get(f"{MEXC_BASE}/ticker", params={"symbol": symbol}, timeout=6)
        data = r.json()
        if data.get("success") and data.get("data"):
            d = data["data"]
            if isinstance(d, list): d = d[0]
            price  = float(d.get("lastPrice", d.get("last", 0)))
            high   = float(d.get("high24h",   d.get("high",  0)))
            low    = float(d.get("low24h",    d.get("low",   0)))
            raw_chg = (d.get("priceChangePercent") or d.get("changeRate") or
                       d.get("riseFallRate") or d.get("rate") or d.get("change24h") or 0)
            change = float(raw_chg)
            if change != 0 and abs(change) < 1.5:
                change = change * 100
            if change == 0 and price > 0:
                open24 = float(d.get("open24h", d.get("openPrice", d.get("indexPrice", 0))))
                if open24 > 0:
                    change = round((price - open24) / open24 * 100, 2)
            return {"price": round(price, 8), "change": round(change, 2),
                    "high": round(high, 8),   "low": round(low, 8)}
    except: pass
    return None

# ════════ SMC MARKET STRUCTURE ══════════════════════════════════════

def find_swing_highs_lows(candles, n=3):
    """Return swing highs and swing lows as (index, price) tuples."""
    highs = [c["high"] for c in candles]
    lows  = [c["low"]  for c in candles]
    sh = []; sl = []
    for i in range(n, len(candles) - n):
        if all(highs[i] >= highs[i-j] and highs[i] >= highs[i+j] for j in range(1, n+1)):
            sh.append((i, highs[i]))
        if all(lows[i] <= lows[i-j] and lows[i] <= lows[i+j] for j in range(1, n+1)):
            sl.append((i, lows[i]))
    return sh, sl

def detect_market_structure(candles, lookback=100):
    """
    SMC structure detection.
    Returns (structure, bos_level, choch_level, swing_highs, swing_lows)
    structure: BULLISH | BEARISH | NEUTRAL
    """
    c = candles[-lookback:] if len(candles) >= lookback else candles
    if len(c) < 20: return "NEUTRAL", None, None, [], []

    sh, sl = find_swing_highs_lows(c, n=3)
    if len(sh) < 2 or len(sl) < 2:
        return "NEUTRAL", None, None, sh, sl

    # BOS: Break of Structure — price closes beyond last swing high/low
    last_sh = sh[-1]; prev_sh = sh[-2]
    last_sl = sl[-1]; prev_sl = sl[-2]

    hh = last_sh[1] > prev_sh[1]  # Higher High
    hl = last_sl[1] > prev_sl[1]  # Higher Low
    lh = last_sh[1] < prev_sh[1]  # Lower High
    ll = last_sl[1] < prev_sl[1]  # Lower Low

    bos_level   = None
    choch_level = None

    # Detect CHOCH (Change of Character): first sign of trend reversal
    # In a downtrend, first HH = CHOCH; in an uptrend, first LL = CHOCH
    if hh and hl:
        structure = "BULLISH"
        bos_level = last_sh[1]
    elif ll and lh:
        structure = "BEARISH"
        bos_level = last_sl[1]
    else:
        # Mixed — look for CHOCH
        if hh and not hl:   # Higher High in downtrend = CHOCH
            structure   = "BULLISH"
            choch_level = last_sh[1]
            bos_level   = last_sh[1]
        elif ll and not lh:  # Lower Low in uptrend = CHOCH
            structure   = "BEARISH"
            choch_level = last_sl[1]
            bos_level   = last_sl[1]
        else:
            # Fallback: simple 20-bar average
            closes = [x["close"] for x in c[-20:]]
            a1 = sum(closes[:10]) / 10; a2 = sum(closes[10:]) / 10
            if a2 > a1 * 1.003:   structure = "BULLISH"
            elif a2 < a1 * 0.997: structure = "BEARISH"
            else:                  structure = "NEUTRAL"

    return structure, bos_level, choch_level, sh, sl

def detect_mss(candles, direction):
    """
    Market Structure Shift (MSS): a more aggressive CHOCH — the first
    candle that closes beyond the most recent swing opposite to trend,
    signaling a potential reversal.
    Returns (found, mss_level)
    """
    sh, sl = find_swing_highs_lows(candles, n=2)
    if direction == "BULLISH":
        # MSS in a downtrend: first close ABOVE a previous swing high
        if len(sh) < 2: return False, None
        ref_level = sh[-2][1]  # previous swing high
        for c in candles[sh[-2][0]+1:]:
            if c["close"] > ref_level:
                return True, round(ref_level, 8)
    else:
        if len(sl) < 2: return False, None
        ref_level = sl[-2][1]  # previous swing low
        for c in candles[sl[-2][0]+1:]:
            if c["close"] < ref_level:
                return True, round(ref_level, 8)
    return False, None

# ════════ LIQUIDITY SWEEP ════════════════════════════════════════════

def detect_liquidity_sweep(candles, direction, lookback=60):
    """
    SMC liquidity sweep: price wicks beyond equal highs/lows (buy-side or
    sell-side liquidity), then rejects back.
    Returns (swept, sweep_level, sweep_idx)
    """
    c = candles[-lookback:] if len(candles) >= lookback else candles
    sh, sl = find_swing_highs_lows(c, n=2)

    if direction == "BULLISH":
        # Sweep of sell-side liquidity (lows swept, then rejection up)
        if len(sl) < 2: return False, None, None
        # Find equal lows (within 0.1% of each other)
        ref_low = sl[-1][1]
        eq_lows = [s for s in sl if abs(s[1] - ref_low) / ref_low < 0.001]
        if not eq_lows: eq_lows = [sl[-1]]
        sweep_level = min(s[1] for s in eq_lows)
        # Check if any recent candle wicked below but closed above
        for i in range(len(c) - 10, len(c)):
            if i < 0: continue
            ci = c[i]
            if ci["low"] < sweep_level and ci["close"] > sweep_level:
                return True, round(sweep_level, 8), i
        # Relax: wick below last swing low
        last_ll = sl[-1]
        for i in range(last_ll[0]+1, len(c)):
            ci = c[i]
            if ci["low"] < last_ll[1] and ci["close"] > last_ll[1]:
                return True, round(last_ll[1], 8), i
    else:
        # Sweep of buy-side liquidity (highs swept, then rejection down)
        if len(sh) < 2: return False, None, None
        ref_high = sh[-1][1]
        eq_highs = [s for s in sh if abs(s[1] - ref_high) / ref_high < 0.001]
        if not eq_highs: eq_highs = [sh[-1]]
        sweep_level = max(s[1] for s in eq_highs)
        for i in range(len(c) - 10, len(c)):
            if i < 0: continue
            ci = c[i]
            if ci["high"] > sweep_level and ci["close"] < sweep_level:
                return True, round(sweep_level, 8), i
        last_hh = sh[-1]
        for i in range(last_hh[0]+1, len(c)):
            ci = c[i]
            if ci["high"] > last_hh[1] and ci["close"] < last_hh[1]:
                return True, round(last_hh[1], 8), i

    return False, None, None

# ════════ ORDER BLOCK ════════════════════════════════════════════════

def find_order_blocks(candles, direction, lookback=80):
    """
    SMC Order Block: the last opposite-colour candle before an impulsive
    move that causes a BOS.
    Returns list of OBs sorted newest-first.
    """
    c = candles[-lookback:] if len(candles) >= lookback else candles
    obs = []
    for i in range(2, len(c) - 3):
        ci = c[i]; cn = c[i+1]; cn2 = c[i+2]
        if direction == "BULLISH":
            # Bearish candle (OB) followed by strong bullish impulse
            is_bearish_ob = ci["close"] < ci["open"]
            impulse = cn["close"] > ci["high"] and cn["close"] > cn["open"]
            if is_bearish_ob and impulse:
                # Confirm BOS: impulse breaks structure
                obs.append({
                    "top":  ci["open"],
                    "bot":  ci["close"],
                    "high": ci["high"],
                    "low":  ci["low"],
                    "idx":  i,
                    "time": ci["time"],
                    "type": "BULLISH_OB",
                })
        else:
            # Bullish candle (OB) followed by strong bearish impulse
            is_bullish_ob = ci["close"] > ci["open"]
            impulse = cn["close"] < ci["low"] and cn["close"] < cn["open"]
            if is_bullish_ob and impulse:
                obs.append({
                    "top":  ci["close"],
                    "bot":  ci["open"],
                    "high": ci["high"],
                    "low":  ci["low"],
                    "idx":  i,
                    "time": ci["time"],
                    "type": "BEARISH_OB",
                })
    return sorted(obs, key=lambda x: x["idx"], reverse=True)

def ob_is_unmitigated(ob, candles, direction):
    """True if no candle after the OB has closed inside the OB zone."""
    for c in candles[ob["idx"] + 1:]:
        if direction == "BULLISH":
            if c["close"] < ob["top"] and c["close"] > ob["bot"]:
                return False   # mitigated
        else:
            if c["close"] > ob["bot"] and c["close"] < ob["top"]:
                return False
    return True

def price_at_ob(current_price, ob, direction, tolerance=0.002):
    """True if price is currently tapping into the OB zone."""
    if direction == "BULLISH":
        return ob["bot"] * (1 - tolerance) <= current_price <= ob["top"] * (1 + tolerance)
    else:
        return ob["bot"] * (1 - tolerance) <= current_price <= ob["top"] * (1 + tolerance)

# ════════ FAIR VALUE GAP ════════════════════════════════════════════

def find_fvg(candles, direction, lookback=80):
    """
    Fair Value Gap (FVG / Imbalance): 3-candle pattern where C1 and C3
    do not overlap, leaving an unfilled gap.
    Returns newest unmitigated FVG or None.
    """
    c = candles[-lookback:] if len(candles) >= lookback else candles
    fvgs = []
    for i in range(len(c) - 3):
        c1 = c[i]; c3 = c[i+2]
        if direction == "BULLISH":
            if c3["low"] > c1["high"]:
                fvg_top = c3["low"]; fvg_bot = c1["high"]
                mitigated = any(c[j]["low"] <= fvg_top for j in range(i+3, len(c)))
                if not mitigated:
                    fvgs.append({"top": fvg_top, "bot": fvg_bot, "idx": i,
                                 "mid": (fvg_top + fvg_bot)/2, "type": "FVG"})
        else:
            if c3["high"] < c1["low"]:
                fvg_top = c1["low"]; fvg_bot = c3["high"]
                mitigated = any(c[j]["high"] >= fvg_bot for j in range(i+3, len(c)))
                if not mitigated:
                    fvgs.append({"top": fvg_top, "bot": fvg_bot, "idx": i,
                                 "mid": (fvg_top + fvg_bot)/2, "type": "FVG"})
    if fvgs:
        return max(fvgs, key=lambda x: x["idx"])
    return None

# ════════ CHOCH CONFIRMATION ════════════════════════════════════════

def detect_choch_ltf(candles, direction):
    """
    LTF CHOCH for entry confirmation: on LTF, detect the shift from
    bearish to bullish (for BUY) or bullish to bearish (for SELL).
    Returns (found, choch_level)
    """
    if len(candles) < 10: return False, None
    sh, sl = find_swing_highs_lows(candles[-40:], n=2)
    c = candles[-40:]
    if direction == "BULLISH":
        # Need a Higher High on LTF = CHOCH bullish
        if len(sh) >= 2 and sh[-1][1] > sh[-2][1]:
            return True, round(sh[-1][1], 8)
        # Or a close above the last swing high
        if sh:
            last_sh_val = sh[-1][1]
            for ci in c[sh[-1][0]+1:]:
                if ci["close"] > last_sh_val:
                    return True, round(last_sh_val, 8)
    else:
        if len(sl) >= 2 and sl[-1][1] < sl[-2][1]:
            return True, round(sl[-1][1], 8)
        if sl:
            last_sl_val = sl[-1][1]
            for ci in c[sl[-1][0]+1:]:
                if ci["close"] < last_sl_val:
                    return True, round(last_sl_val, 8)
    return False, None

# ════════ PREMIUM / DISCOUNT ════════════════════════════════════════

def get_pd_zone(candles, lookback=50):
    """
    Returns (equilibrium, swing_high, swing_low).
    Discount = below EQ (BUY zone), Premium = above EQ (SELL zone).
    """
    c = candles[-lookback:] if len(candles) >= lookback else candles
    swing_high = max(ci["high"] for ci in c)
    swing_low  = min(ci["low"]  for ci in c)
    eq = (swing_high + swing_low) / 2
    return eq, swing_high, swing_low

def is_in_pd_zone(price, candles, direction, lookback=50):
    eq, sh, sl = get_pd_zone(candles, lookback)
    if direction == "BULLISH":
        return price <= eq, "DISCOUNT" if price <= eq else "PREMIUM"
    else:
        return price >= eq, "PREMIUM" if price >= eq else "DISCOUNT"

# ════════ SMC SIGNAL SCORING ════════════════════════════════════════

def score_smc_signal(direction, structure, bos_level, choch_level,
                     liq_swept, ob, fvg, ltf_choch, mss_found,
                     pd_zone, rr, sh, sl):
    score = 0; details = []

    # 1. Market Structure (BOS / CHOCH)
    if structure in ("BULLISH", "BEARISH"):
        aligned = (direction == "BUY" and structure == "BULLISH") or \
                  (direction == "SELL" and structure == "BEARISH")
        if aligned:
            if bos_level:  score += 18; details.append("✅ BOS confirmed & trend aligned (+18)")
            else:          score += 10; details.append("⚠️ Structure aligned (no BOS level) (+10)")
        else:
            details.append("❌ Counter-structure (+0)")
    if choch_level:
        score += 8; details.append("✅ CHOCH identified (+8)")

    # 2. Liquidity Sweep (mandatory gate)
    if liq_swept:
        score += 20; details.append("✅ Liquidity sweep confirmed (+20)")
    else:
        details.append("❌ No liquidity sweep — quality reduced (+0)")

    # 3. Order Block quality
    if ob:
        score += 15; details.append(f"✅ Unmitigated Order Block ({ob['type']}) (+15)")
    else:
        details.append("⚠️ No clean OB (+0)")

    # 4. Fair Value Gap — confluence info only, NOT used for entry
    if fvg:
        score += 8; details.append("✅ FVG nearby — extra confluence (+8)")
    else:
        details.append("ℹ️ No FVG nearby (+0)")

    # 5. LTF CHOCH for entry confirmation
    if ltf_choch:
        score += 14; details.append("✅ LTF CHOCH entry confirmation (+14)")
    else:
        details.append("⚠️ No LTF CHOCH (+0)")

    # 6. MSS
    if mss_found:
        score += 8; details.append("✅ Market Structure Shift (MSS) (+8)")

    # 7. Premium / Discount zone
    if "DISCOUNT" in pd_zone and direction == "BUY":
        score += 12; details.append("✅ OB in Discount zone — A+ quality (+12)")
    elif "PREMIUM" in pd_zone and direction == "SELL":
        score += 12; details.append("✅ OB in Premium zone — A+ quality (+12)")
    else:
        details.append(f"⚠️ OB zone: {pd_zone} (+0)")

    # 8. RR
    if rr >= 5:   score += 10; details.append(f"✅ Exceptional RR {rr}R (+10)")
    elif rr >= 4: score += 8;  details.append(f"✅ Strong RR {rr}R (+8)")
    elif rr >= 3: score += 6;  details.append(f"⚠️ Min RR {rr}R (+6)")
    elif rr >= 2: score += 3;  details.append(f"⚠️ Low RR {rr}R (+3)")

    # 9. Core confluence: OB + Liquidity Sweep + LTF CHOCH
    if ob and liq_swept and ltf_choch:
        score = min(score + 10, 100)
        details.append("⭐ Core confluence: OB + Liq Sweep + LTF CHOCH (+10)")

    # Grade
    score = min(score, 100)
    pd_ok = ("DISCOUNT" in pd_zone and direction == "BUY") or \
            ("PREMIUM" in pd_zone and direction == "SELL")
    if score >= 75 and liq_swept and pd_ok:    grade = "A+"
    elif score >= 60 and liq_swept:            grade = "A"
    elif score >= 50:                          grade = "B"
    elif score >= 38:                          grade = "C"
    else:                                      grade = "D"

    return score, grade, details

# ════════ CORE SMC SCAN ═════════════════════════════════════════════

def scan_smc_pair(symbol):
    results = []

    # ── Step 1: HTF trend / BOS ───────────────────────────────────────
    htf_candles = get_candles(symbol, "Hour4", limit=200)
    if not htf_candles or len(htf_candles) < 30:
        diag["no_candles"] += 1; return results

    structure, bos_level, choch_level, sh, sl = detect_market_structure(htf_candles)
    if structure == "NEUTRAL":
        diag["neutral"] += 1; return results

    direction = "BUY" if structure == "BULLISH" else "SELL"

    # ── Step 2: Liquidity sweep on HTF ───────────────────────────────
    liq_swept, sweep_level, sweep_idx = detect_liquidity_sweep(htf_candles, structure)

    # ── Step 3: Find best Order Block on MTF ─────────────────────────
    best_ob   = None
    best_ob_tf = None
    for tf in MTF_TFS:
        ob_candles = get_candles(symbol, tf, limit=150)
        if not ob_candles or len(ob_candles) < 20: continue
        obs = find_order_blocks(ob_candles, structure)
        # Filter: unmitigated only
        valid_obs = [ob for ob in obs if ob_is_unmitigated(ob, ob_candles, structure)]
        # Filter: in correct P/D zone
        current_price = ob_candles[-1]["close"]
        for ob in valid_obs[:5]:
            ob_mid = (ob["top"] + ob["bot"]) / 2
            in_zone, pd_label = is_in_pd_zone(ob_mid, ob_candles, structure)
            if in_zone:
                ob["pd_zone"] = pd_label
                ob["tf"]      = tf
                best_ob       = ob
                best_ob_tf    = tf
                break
        if best_ob: break

    if not best_ob:
        diag["no_ob"] += 1
        # Still try on daily for 1D setups
        d1_candles = get_candles(symbol, "Day1", limit=120)
        if d1_candles and len(d1_candles) >= 15:
            obs = find_order_blocks(d1_candles, structure)
            valid = [ob for ob in obs if ob_is_unmitigated(ob, d1_candles, structure)]
            if valid:
                ob = valid[0]; ob_mid = (ob["top"] + ob["bot"]) / 2
                in_zone, pd_label = is_in_pd_zone(ob_mid, d1_candles, structure)
                ob["pd_zone"] = pd_label; ob["tf"] = "Day1"
                best_ob = ob; best_ob_tf = "Day1"
        if not best_ob: return results

    ob_tf_candles = get_candles(symbol, best_ob_tf, limit=150) if best_ob_tf else htf_candles

    # ── Step 4: FVG on same or lower TF ──────────────────────────────
    fvg_found = None
    fvg_tf    = None
    for tf in ([best_ob_tf] if best_ob_tf else []) + MTF_TFS + LTF_TFS:
        fc = get_candles(symbol, tf, limit=100)
        if not fc or len(fc) < 10: continue
        fg = find_fvg(fc, structure)
        if fg:
            fvg_found = fg; fvg_tf = tf; break

    if not fvg_found:
        diag["no_fvg"] += 1

    # ── Step 5: MSS detection on HTF ────────────────────────────────
    mss_found, mss_level = detect_mss(htf_candles, structure)

    # ── Step 6: LTF CHOCH for entry confirmation ─────────────────────
    ltf_choch_found = False
    ltf_choch_level = None
    for tf in LTF_TFS:
        ltf_c = get_candles(symbol, tf, limit=80)
        if not ltf_c: continue
        found, level = detect_choch_ltf(ltf_c, structure)
        if found:
            ltf_choch_found = True; ltf_choch_level = level; break

    # ── Step 7: Entry, SL, TP ────────────────────────────────────────
    ob = best_ob
    current_price = htf_candles[-1]["close"]

    if direction == "BUY":
        entry = ob["top"]           # Enter at top of OB (price taps into OB)
        sl_p  = ob["low"]           # SL below the OB low (sweep extreme)
        # TP: next swing high or BOS level
        tp_candidates = [s[1] for s in sh if s[1] > entry]
        tp_p = min(tp_candidates) if tp_candidates else entry * 1.03
        if bos_level and bos_level > entry:
            tp_p = max(tp_p, bos_level)
    else:
        entry = ob["bot"]           # Enter at bottom of OB
        sl_p  = ob["high"]          # SL above the OB high
        tp_candidates = [s[1] for s in sl if s[1] < entry]
        tp_p = max(tp_candidates) if tp_candidates else entry * 0.97
        if bos_level and bos_level < entry:
            tp_p = min(tp_p, bos_level)

    risk   = abs(entry - sl_p)
    reward = abs(tp_p - entry)
    rr     = round(reward / risk, 2) if risk > 0 else 0

    if rr < 1.5:
        diag["rr_low"] += 1; return results

    pd_zone = ob.get("pd_zone", "UNKNOWN")

    score, grade, details = score_smc_signal(
        direction, structure, bos_level, choch_level,
        liq_swept, ob, fvg_found, ltf_choch_found, mss_found,
        pd_zone, rr, sh, sl
    )

    if score < 30:
        diag["score_low"] += 1; return results

    diag["passed"] += 1
    sig = {
        "symbol":       symbol,
        "direction":    direction,
        "strategy":     "SMC",
        "structure":    structure,
        "htf_tf":       "Hour4",
        "ob_tf":        best_ob_tf or "–",
        "fvg_tf":       fvg_tf or "–",
        "ob_zone":      pd_zone,
        "ob_top":       round(ob["top"], 8),
        "ob_bot":       round(ob["bot"], 8),
        "bos_level":    round(bos_level, 8) if bos_level else "–",
        "choch_level":  round(choch_level, 8) if choch_level else "–",
        "mss_level":    round(mss_level, 8) if mss_level else "–",
        "sweep_level":  round(sweep_level, 8) if sweep_level else "–",
        "fvg_top":      round(fvg_found["top"], 8) if fvg_found else "–",
        "fvg_bot":      round(fvg_found["bot"], 8) if fvg_found else "–",
        "ltf_choch":    ltf_choch_found,
        "ltf_choch_lvl":round(ltf_choch_level, 8) if ltf_choch_level else "–",
        "liq_swept":    liq_swept,
        "mss_found":    mss_found,
        "fvg_found":    fvg_found is not None,
        "entry":        round(entry, 8),
        "sl":           round(sl_p, 8),
        "tp":           round(tp_p, 8),
        "rr":           rr,
        "score":        score,
        "grade":        grade,
        "details":      details,
        "timestamp":    datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M UTC+1"),
    }
    results.append(sig)
    return results

# ════════ TELEGRAM FORMAT ═════════════════════════════════════════

def fmt_tg_smc(sig):
    e    = "🟢" if sig["direction"] == "BUY" else "🔴"
    bars = "█" * (sig["score"] // 10) + "░" * (10 - sig["score"] // 10)
    liq  = "✅ Swept" if sig.get("liq_swept") else "⚠️ None"
    fvg  = f"✅ {sig['fvg_tf']}" if sig.get("fvg_found") else "⚠️ None"
    choch= "✅" if sig.get("ltf_choch") else "⚠️ Pending"
    mss  = "✅" if sig.get("mss_found") else "–"
    return (
        f"{e} <b>SMC SIGNAL — {sig['direction']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Pair:</b>       {sig['symbol']}\n"
        f"<b>Structure:</b>  {sig['structure']} | OB TF: {sig['ob_tf']}\n"
        f"<b>Zone:</b>       {sig['ob_zone']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>🎯 Entry:</b>    {sig['entry']} (OB tap)\n"
        f"<b>🛑 SL:</b>       {sig['sl']} (OB extreme)\n"
        f"<b>🎯 TP:</b>       {sig['tp']} (next liquidity)\n"
        f"<b>📊 RR:</b>       {sig['rr']}R\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>BOS:</b>        {sig.get('bos_level','–')}\n"
        f"<b>CHOCH:</b>      {sig.get('choch_level','–')}\n"
        f"<b>MSS:</b>        {mss}\n"
        f"<b>Liq Sweep:</b>  {liq} @ {sig.get('sweep_level','–')}\n"
        f"<b>OB:</b>         {sig['ob_bot']} – {sig['ob_top']}\n"
        f"<b>FVG:</b>        {fvg}\n"
        f"<b>LTF CHOCH:</b>  {choch}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Score:</b>      {sig['score']}/100 [{bars}] {sig['grade']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>SMC Strategy Scanner • {sig['timestamp']}</i>"
    )

# ════════ SCANNER LOOP ════════════════════════════════════════════

def scanner_loop():
    log("🔍 SMC Scanner started")
    while True:
        with scan_lock:
            enabled = scan_state["enabled"]
        if not enabled:
            time.sleep(5); continue

        pairs = get_all_pairs() or TOP_PAIRS
        with scan_lock:
            scan_state["total_pairs"] = len(pairs)
            scan_state["pairs_done"]  = 0
            scan_state["scan_count"] += 1
            scan_state["running"]     = True

        for sym in pairs:
            with scan_lock:
                if not scan_state["enabled"]:
                    break
                scan_state["current_pair"] = sym

            try:
                results = scan_smc_pair(sym)
                for sig in results:
                    # De-dupe: skip if same symbol+direction in last 20 signals
                    recent = list(signals)[:20]
                    dup = any(s["symbol"] == sig["symbol"] and
                              s["direction"] == sig["direction"] for s in recent)
                    if dup: continue

                    signals.appendleft(sig)
                    with scan_lock:
                        scan_state["signals_found"] += 1

                    # Telegram
                    send_telegram(fmt_tg_smc(sig))
                    log(f"✅ SMC SIGNAL: {sym} {sig['direction']} | RR:{sig['rr']} | Score:{sig['score']} {sig['grade']}")

                    # Paper trade auto-entry
                    if paper_config["enabled"] and paper_config["auto_trade"]:
                        open_paper_trade(sig)

                    # Live trade
                    if trade_config["enabled"]:
                        place_order(sig)

            except Exception as e:
                log(f"Error scanning {sym}: {e}")

            with scan_lock:
                scan_state["pairs_done"] += 1
            time.sleep(0.4)

        with scan_lock:
            scan_state["running"]   = False
            scan_state["last_scan"] = datetime.now(LOCAL_TZ).strftime("%H:%M:%S")

        log(f"✅ Scan #{scan_state['scan_count']} done | signals: {scan_state['signals_found']}")
        time.sleep(60)

# ════════ MEXC LIVE TRADING ══════════════════════════════════════

def mexc_sign(api_key, timestamp_ms, query_string, secret):
    raw = str(api_key) + str(timestamp_ms) + str(query_string)
    sig = hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    return sig.upper()

def mexc_request(method, path, params=None, signed=True):
    api_key    = trade_config.get("api_key", "")
    api_secret = trade_config.get("api_secret", "")
    if not api_key or not api_secret:
        return None, "API keys not configured"
    params = params or {}
    ts     = str(int(time.time() * 1000))
    if method == "GET":
        query_str = urllib.parse.urlencode(sorted(params.items())) if params else ""
    else:
        import json as _json
        query_str = _json.dumps(params, separators=(",", ":")) if params else ""
    signature = mexc_sign(api_key, ts, query_str, api_secret)
    headers = {"Content-Type": "application/json", "ApiKey": api_key,
               "Request-Time": ts, "Signature": signature}
    try:
        url = f"{MEXC_FUTURES}{path}"
        r   = requests.get(url, params=params, headers=headers, timeout=10) \
              if method == "GET" else \
              requests.post(url, json=params, headers=headers, timeout=10)
        data = r.json()
        if data.get("success") or str(data.get("code", "")) == "0":
            return data.get("data"), None
        err_msg = data.get("message", data.get("msg", f"code={data.get('code')}"))
        log(f"MEXC API error on {path}: {err_msg}")
        return None, err_msg
    except Exception as e:
        return None, str(e)

def get_account_balance():
    data, err = mexc_request("GET", "/account/assets")
    if err: return 0.0, err
    if not data: return 0.0, "No data"
    assets = data if isinstance(data, list) else [data]
    for asset in assets:
        if asset.get("currency", asset.get("coin", "")).upper() == "USDT":
            bal = float(asset.get("availableBalance",
                        asset.get("available", asset.get("walletBalance", 0))))
            return bal, None
    return 0.0, "USDT not found"

def get_symbol_info(symbol):
    try:
        r = requests.get(f"{MEXC_BASE}/detail", timeout=10)
        for item in r.json().get("data", []):
            if item.get("symbol") == symbol:
                return {"min_vol": float(item.get("minVol", 1)),
                        "contract_size": float(item.get("contractSize", 1)),
                        "price_unit": float(item.get("priceUnit", 0.01))}
    except: pass
    return {"min_vol": 1, "contract_size": 1, "price_unit": 0.01}

def calc_position_size(symbol, entry, sl, balance):
    risk_amount  = balance * trade_config["risk_pct"] / 100
    sl_distance  = abs(entry - sl)
    if sl_distance <= 0: return 0
    info         = get_symbol_info(symbol)
    contracts    = risk_amount / (sl_distance * info["contract_size"])
    min_vol      = info["min_vol"]
    return int(max(min_vol, round(contracts / min_vol) * min_vol))

def place_order(sig):
    with trade_lock:
        if not trade_config["enabled"]:      return False, "Auto-trade disabled"
        if len(open_trades) >= trade_config["max_trades"]:
            return False, "Max trades reached"
        if sig["symbol"] in open_trades:     return False, "Already in trade"
    balance, err = get_account_balance()
    if err:         return False, f"Balance error: {err}"
    if balance < 10: return False, "Insufficient balance"
    contracts = calc_position_size(sig["symbol"], sig["entry"], sig["sl"], balance)
    if contracts <= 0: return False, "Position size 0"
    side = 1 if sig["direction"] == "BUY" else 2
    # Set leverage
    mexc_request("POST", "/position/change_leverage",
                 {"symbol": sig["symbol"], "leverage": trade_config["leverage"],
                  "openType": 1, "positionType": side})
    order_params = {
        "symbol": sig["symbol"], "side": side, "openType": 1,
        "type": 5,   # 5 = limit order
        "vol":  contracts,
        "price": sig["entry"],
        "leverage": trade_config["leverage"],
    }
    data, err = mexc_request("POST", "/order/submit", order_params)
    if err: log(f"Order error {sig['symbol']}: {err}"); return False, err
    with trade_lock:
        open_trades[sig["symbol"]] = {
            "symbol": sig["symbol"], "direction": sig["direction"],
            "entry": sig["entry"], "sl": sig["sl"], "tp": sig["tp"],
            "contracts": contracts, "order_id": data,
            "opened_at": datetime.now(LOCAL_TZ).strftime("%H:%M UTC+1"),
            "strategy": "SMC",
        }
    log(f"📈 Order placed: {sig['symbol']} {sig['direction']} x{contracts}")
    return True, "OK"

def close_trade(symbol, reason="Manual"):
    with trade_lock:
        trade = open_trades.pop(symbol, None)
    if not trade: return False, "Not found"
    side = 2 if trade["direction"] == "BUY" else 1
    mexc_request("POST", "/order/submit",
                 {"symbol": symbol, "side": side, "openType": 1,
                  "type": 5, "vol": trade["contracts"],
                  "price": 0, "leverage": trade_config["leverage"]})
    log(f"🔴 Trade closed: {symbol} | {reason}")
    recent_trades.appendleft({**trade, "closed_at": datetime.now(LOCAL_TZ).strftime("%H:%M UTC+1"),
                              "reason": reason})
    return True, "Closed"

# ════════ PAPER TRADING ══════════════════════════════════════════

def open_paper_trade(sig):
    with paper_lock:
        if not paper_config["enabled"] or not paper_config["auto_trade"]: return
        if len(paper_trades) >= paper_config["max_trades"]: return
        if sig["symbol"] in paper_trades: return
        risk_amount = paper_config["balance"] * paper_config["risk_pct"] / 100
        sl_dist     = abs(sig["entry"] - sig["sl"])
        size        = risk_amount / sl_dist if sl_dist > 0 else 0
        paper_trades[sig["symbol"]] = {
            "symbol": sig["symbol"], "direction": sig["direction"],
            "entry": sig["entry"], "sl": sig["sl"], "tp": sig["tp"],
            "size": round(size, 4), "rr": sig["rr"],
            "opened_at": datetime.now(LOCAL_TZ).strftime("%H:%M UTC+1"),
            "strategy": "SMC",
        }
    log(f"📝 Paper trade: {sig['symbol']} {sig['direction']} @ {sig['entry']}")

def close_paper_trade(symbol, reason="Manual", close_price=None):
    with paper_lock:
        trade = paper_trades.pop(symbol, None)
    if not trade: return False, "Not found"
    cp = close_price or trade["tp"]
    if trade["direction"] == "BUY":
        pnl = (cp - trade["entry"]) * trade["size"]
    else:
        pnl = (trade["entry"] - cp) * trade["size"]
    win = pnl > 0
    with paper_lock:
        paper_config["balance"] += pnl
        paper_stats["total"] += 1
        paper_stats["wins" if win else "losses"] += 1
        paper_stats["total_pnl"] += pnl
    record = {**trade, "close_price": cp, "pnl": round(pnl, 4),
              "result": "WIN" if win else "LOSS", "reason": reason,
              "closed_at": datetime.now(LOCAL_TZ).strftime("%H:%M UTC+1")}
    paper_history.appendleft(record)
    log(f"📝 Paper closed: {symbol} | {'WIN' if win else 'LOSS'} | PnL: {pnl:.4f}")
    return True, f"{'WIN' if win else 'LOSS'} {pnl:.4f}"

def paper_monitor_loop():
    """Check paper trades against live prices for auto-close."""
    while True:
        time.sleep(30)
        with paper_lock:
            symbols = list(paper_trades.keys())
        for sym in symbols:
            t = get_ticker(sym)
            if not t: continue
            price = t["price"]
            with paper_lock:
                trade = paper_trades.get(sym)
            if not trade: continue
            if trade["direction"] == "BUY":
                if price <= trade["sl"]:
                    close_paper_trade(sym, "SL Hit", price)
                elif price >= trade["tp"]:
                    close_paper_trade(sym, "TP Hit", price)
            else:
                if price >= trade["sl"]:
                    close_paper_trade(sym, "SL Hit", price)
                elif price <= trade["tp"]:
                    close_paper_trade(sym, "TP Hit", price)

# ════════ DASHBOARD HTML ════════════════════════════════════════

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SMC Bot Dashboard</title>
<style>
  :root{--bg:#0a0e1a;--card:#111827;--accent:#6366f1;--green:#22c55e;--red:#ef4444;--text:#e2e8f0;--muted:#64748b;--border:#1e293b}
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
  #login{display:flex;align-items:center;justify-content:center;min-height:100vh}
  .login-box{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:40px;width:320px;text-align:center}
  .login-box h2{margin-bottom:24px;font-size:22px;color:var(--accent)}
  input[type=password]{width:100%;padding:12px 16px;border-radius:8px;border:1px solid var(--border);background:#1e293b;color:var(--text);font-size:15px;margin-bottom:14px}
  .btn{padding:11px 22px;border-radius:8px;border:none;cursor:pointer;font-size:14px;font-weight:600;transition:.2s}
  .btn-primary{background:var(--accent);color:#fff;width:100%}
  .btn-primary:hover{opacity:.88}
  .btn-sm{padding:6px 14px;font-size:13px;border-radius:6px}
  .btn-green{background:var(--green);color:#fff}
  .btn-red{background:var(--red);color:#fff}
  .btn-gray{background:#334155;color:var(--text)}
  #app{display:none}
  header{background:var(--card);border-bottom:1px solid var(--border);padding:14px 28px;display:flex;align-items:center;justify-content:space-between}
  header h1{font-size:18px;font-weight:700;color:var(--accent)}.header-sub{font-size:12px;color:var(--muted)}
  .grid{display:grid;gap:16px;padding:20px 28px}
  .grid-4{grid-template-columns:repeat(4,1fr)}
  .grid-2{grid-template-columns:1fr 1fr}
  @media(max-width:900px){.grid-4{grid-template-columns:1fr 1fr}.grid-2{grid-template-columns:1fr}}
  @media(max-width:560px){.grid-4{grid-template-columns:1fr}}
  .card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px}
  .card h3{font-size:13px;color:var(--muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:.05em}
  .stat{font-size:26px;font-weight:700}
  .green{color:var(--green)}.red{color:var(--red)}.accent{color:var(--accent)}
  .badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:11px;font-weight:700}
  .badge-green{background:rgba(34,197,94,.15);color:var(--green)}
  .badge-red{background:rgba(239,68,68,.15);color:var(--red)}
  .badge-blue{background:rgba(99,102,241,.15);color:var(--accent)}
  .badge-gray{background:rgba(100,116,139,.15);color:var(--muted)}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;padding:8px 10px;color:var(--muted);font-size:12px;border-bottom:1px solid var(--border)}
  td{padding:8px 10px;border-bottom:1px solid rgba(30,41,59,.5);vertical-align:middle}
  tr:hover td{background:rgba(99,102,241,.04)}
  .log-box{background:#080c16;border-radius:8px;padding:12px;height:180px;overflow-y:auto;font-size:12px;font-family:monospace;color:#94a3b8}
  .score-bar{display:inline-block;height:6px;border-radius:3px;background:var(--accent);vertical-align:middle}
  .section-title{font-size:15px;font-weight:700;margin:20px 28px 0;color:var(--text);border-left:3px solid var(--accent);padding-left:10px}
  .status-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
  .dot-green{background:var(--green)}.dot-red{background:var(--red)}.dot-gray{background:var(--muted)}
  .details-list{font-size:12px;color:var(--muted);line-height:1.7}
</style>
</head>
<body>

<div id="login">
  <div class="login-box">
    <h2>🧠 SMC Bot</h2>
    <p style="color:var(--muted);font-size:13px;margin-bottom:20px">Smart Money Concepts Scanner</p>
    <input type="password" id="pw" placeholder="Password" onkeydown="if(event.key==='Enter')doLogin()">
    <button class="btn btn-primary" onclick="doLogin()">Login</button>
    <p id="login-err" style="color:var(--red);font-size:13px;margin-top:10px"></p>
  </div>
</div>

<div id="app">
  <header>
    <div>
      <h1>🧠 SMC Strategy Bot</h1>
      <div class="header-sub">Smart Money Concepts — Order Block Entry</div>
    </div>
    <div style="display:flex;gap:10px;align-items:center">
      <span id="scan-status" class="badge badge-gray">Loading…</span>
      <button class="btn btn-sm btn-gray" id="toggle-btn" onclick="toggleScanner()">Pause</button>
      <button class="btn btn-sm btn-gray" onclick="doLogout()">Logout</button>
    </div>
  </header>

  <!-- Stats row -->
  <div class="grid grid-4" style="padding-top:20px">
    <div class="card"><h3>Total Signals</h3><div class="stat accent" id="total-signals">–</div></div>
    <div class="card"><h3>BUY Signals</h3><div class="stat green" id="buy-signals">–</div></div>
    <div class="card"><h3>SELL Signals</h3><div class="stat red" id="sell-signals">–</div></div>
    <div class="card"><h3>Scan #</h3><div class="stat" id="scan-count">–</div><div style="font-size:12px;color:var(--muted);margin-top:4px">Last: <span id="last-scan">–</span></div></div>
  </div>

  <!-- Paper stats -->
  <div class="grid grid-4">
    <div class="card"><h3>Paper Balance</h3><div class="stat" id="paper-bal">–</div></div>
    <div class="card"><h3>Paper Trades</h3><div class="stat" id="paper-total">–</div></div>
    <div class="card"><h3>Win Rate</h3><div class="stat green" id="paper-wr">–</div></div>
    <div class="card"><h3>Total PnL</h3><div class="stat" id="paper-pnl">–</div></div>
  </div>

  <!-- Scanner progress -->
  <div class="grid" style="grid-template-columns:1fr">
    <div class="card">
      <h3>Scanner Progress</h3>
      <div style="display:flex;align-items:center;gap:14px;margin-top:6px">
        <span id="pairs-progress" style="font-size:13px;color:var(--muted)">–</span>
        <span id="current-pair" style="font-size:13px;color:var(--accent)">–</span>
      </div>
    </div>
  </div>

  <!-- Signals table -->
  <div class="section-title" style="margin-top:4px">📡 SMC Signals</div>
  <div class="grid" style="grid-template-columns:1fr">
    <div class="card" style="overflow-x:auto">
      <table>
        <thead>
          <tr>
            <th>Pair</th><th>Dir</th><th>Score</th><th>Grade</th>
            <th>Entry</th><th>SL</th><th>TP</th><th>RR</th>
            <th>OB TF</th><th>Zone</th><th>Liq</th><th>FVG</th><th>CHOCH</th><th>Time</th>
          </tr>
        </thead>
        <tbody id="signals-table"></tbody>
      </table>
    </div>
  </div>

  <!-- Paper trades -->
  <div class="grid grid-2">
    <div class="card">
      <h3 style="margin-bottom:10px">📝 Open Paper Trades</h3>
      <table>
        <thead><tr><th>Pair</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP</th><th>RR</th><th>Action</th></tr></thead>
        <tbody id="paper-table"></tbody>
      </table>
    </div>
    <div class="card">
      <h3 style="margin-bottom:10px">📊 Paper History</h3>
      <table>
        <thead><tr><th>Pair</th><th>Result</th><th>PnL</th><th>Reason</th><th>Closed</th></tr></thead>
        <tbody id="paper-hist-table"></tbody>
      </table>
    </div>
  </div>

  <!-- Paper config -->
  <div class="grid grid-2">
    <div class="card">
      <h3 style="margin-bottom:12px">⚙️ Paper Config</h3>
      <div style="display:flex;flex-direction:column;gap:10px">
        <label style="font-size:13px">Balance ($) <input type="number" id="paper-bal-input" style="width:100%;padding:7px;border-radius:6px;border:1px solid var(--border);background:#1e293b;color:var(--text);margin-top:4px" placeholder="10000"></label>
        <label style="font-size:13px">Risk % <input type="number" id="paper-risk" style="width:100%;padding:7px;border-radius:6px;border:1px solid var(--border);background:#1e293b;color:var(--text);margin-top:4px" placeholder="1" step="0.1"></label>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-sm btn-green" onclick="setPaperConfig(true,true)">Enable + Auto</button>
          <button class="btn btn-sm btn-gray" onclick="setPaperConfig(true,false)">Enable (Manual)</button>
          <button class="btn btn-sm btn-red" onclick="setPaperConfig(false,false)">Disable</button>
          <button class="btn btn-sm btn-gray" onclick="resetPaper()">Reset Stats</button>
        </div>
        <div id="paper-status" style="font-size:12px;color:var(--muted)"></div>
      </div>
    </div>
    <div class="card">
      <h3 style="margin-bottom:12px">🔴 Live Trade Config</h3>
      <div style="display:flex;flex-direction:column;gap:10px">
        <label style="font-size:13px">API Key <input type="text" id="api-key" style="width:100%;padding:7px;border-radius:6px;border:1px solid var(--border);background:#1e293b;color:var(--text);margin-top:4px"></label>
        <label style="font-size:13px">API Secret <input type="password" id="api-secret" style="width:100%;padding:7px;border-radius:6px;border:1px solid var(--border);background:#1e293b;color:var(--text);margin-top:4px"></label>
        <label style="font-size:13px">Risk % <input type="number" id="live-risk" style="width:60px;padding:7px;border-radius:6px;border:1px solid var(--border);background:#1e293b;color:var(--text);margin-top:4px" value="1" step="0.1"></label>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-sm btn-green" onclick="setLiveConfig(true)">Enable Live</button>
          <button class="btn btn-sm btn-red"   onclick="setLiveConfig(false)">Disable</button>
        </div>
        <div id="live-status" style="font-size:12px;color:var(--muted)"></div>
      </div>
    </div>
  </div>

  <!-- Log -->
  <div class="grid" style="grid-template-columns:1fr;padding-bottom:30px">
    <div class="card">
      <h3 style="margin-bottom:8px">🖥️ Scanner Log</h3>
      <div class="log-box" id="log-box"></div>
    </div>
  </div>
</div>

<script>
let token = localStorage.getItem('smc_token') || '';
if(token) showApp();

async function doLogin(){
  const pw = document.getElementById('pw').value;
  const r  = await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
  const d  = await r.json();
  if(d.ok){ token=d.token; localStorage.setItem('smc_token',token); showApp(); }
  else document.getElementById('login-err').textContent='Wrong password';
}
function showApp(){ document.getElementById('login').style.display='none'; document.getElementById('app').style.display='block'; refresh(); setInterval(refresh,8000); }
async function doLogout(){ await fetch('/api/logout',{method:'POST'}); localStorage.removeItem('smc_token'); location.reload(); }

async function api(url,opts={}){ opts.headers={...opts.headers,'X-Token':token}; return (await fetch(url,opts)).json(); }

async function refresh(){
  await Promise.all([refreshStats(),refreshSignals(),refreshScanState(),refreshPaper(),refreshLog()]);
}

async function refreshStats(){
  const d = await api('/api/stats');
  document.getElementById('total-signals').textContent=d.total||0;
  document.getElementById('buy-signals').textContent=d.buys||0;
  document.getElementById('sell-signals').textContent=d.sells||0;
}

async function refreshScanState(){
  const d = await api('/api/scan-state');
  const running = d.running;
  const enabled = d.enabled;
  document.getElementById('scan-count').textContent=d.scan_count||0;
  document.getElementById('last-scan').textContent=d.last_scan||'–';
  document.getElementById('pairs-progress').textContent=`${d.pairs_done||0} / ${d.total_pairs||0} pairs`;
  document.getElementById('current-pair').textContent=d.current_pair||'–';
  const sb=document.getElementById('scan-status');
  const tb=document.getElementById('toggle-btn');
  if(!enabled){ sb.textContent='⏸ Paused'; sb.className='badge badge-gray'; tb.textContent='Resume'; }
  else if(running){ sb.textContent='🔍 Scanning'; sb.className='badge badge-green'; tb.textContent='Pause'; }
  else { sb.textContent='💤 Idle'; sb.className='badge badge-blue'; tb.textContent='Pause'; }
}

async function refreshSignals(){
  const sigs = await api('/api/signals?limit=50');
  const tbody = document.getElementById('signals-table');
  tbody.innerHTML='';
  for(const s of sigs){
    const dir = s.direction==='BUY'?'<span class="badge badge-green">BUY</span>':'<span class="badge badge-red">SELL</span>';
    const scoreBar = `<div style="display:inline-flex;align-items:center;gap:6px"><span style="font-size:13px;font-weight:700">${s.score}</span><div class="score-bar" style="width:${s.score*0.6}px"></div></div>`;
    const gradeColor = {'A+':'green','A':'green','B':'accent','C':'','D':'red'}[s.grade]||'';
    const liq  = s.liq_swept?'✅':'–';
    const fvg  = s.fvg_found?'✅':'–';
    const choch= s.ltf_choch?'✅':'–';
    tbody.innerHTML+=`<tr>
      <td style="font-weight:700">${s.symbol}</td>
      <td>${dir}</td>
      <td>${scoreBar}</td>
      <td><span class="${gradeColor}" style="font-weight:700">${s.grade}</span></td>
      <td>${s.entry}</td>
      <td style="color:var(--red)">${s.sl}</td>
      <td style="color:var(--green)">${s.tp}</td>
      <td style="font-weight:700">${s.rr}R</td>
      <td>${s.ob_tf}</td>
      <td style="font-size:11px">${s.ob_zone}</td>
      <td>${liq}</td><td>${fvg}</td><td>${choch}</td>
      <td style="font-size:11px;color:var(--muted)">${s.timestamp}</td>
    </tr>`;
  }
}

async function refreshPaper(){
  const stats = await api('/api/paper-stats');
  const config = await api('/api/paper-config');
  const wr = stats.total>0?Math.round(stats.wins/stats.total*100)+'%':'–';
  document.getElementById('paper-bal').textContent='$'+config.balance.toFixed(2);
  document.getElementById('paper-total').textContent=stats.total||0;
  document.getElementById('paper-wr').textContent=wr;
  const pnl=stats.total_pnl||0;
  const pe=document.getElementById('paper-pnl');
  pe.textContent=(pnl>=0?'+':'')+pnl.toFixed(2);
  pe.className='stat '+(pnl>=0?'green':'red');

  const trades=await api('/api/paper-trades');
  const ptb=document.getElementById('paper-table'); ptb.innerHTML='';
  for(const t of trades){
    const dir=t.direction==='BUY'?'<span class="badge badge-green">BUY</span>':'<span class="badge badge-red">SELL</span>';
    ptb.innerHTML+=`<tr><td style="font-weight:700">${t.symbol}</td><td>${dir}</td><td>${t.entry}</td><td style="color:var(--red)">${t.sl}</td><td style="color:var(--green)">${t.tp}</td><td>${t.rr}R</td><td><button class="btn btn-sm btn-red" onclick="closePaper('${t.symbol}')">Close</button></td></tr>`;
  }

  const hist=await api('/api/paper-history');
  const phb=document.getElementById('paper-hist-table'); phb.innerHTML='';
  for(const t of hist.slice(0,8)){
    const rc=t.result==='WIN'?'badge-green':'badge-red';
    const pc=t.pnl>=0?'green':'red';
    phb.innerHTML+=`<tr><td>${t.symbol}</td><td><span class="badge ${rc}">${t.result}</span></td><td class="${pc}">${t.pnl>=0?'+':''}${t.pnl}</td><td>${t.reason}</td><td style="font-size:11px;color:var(--muted)">${t.closed_at}</td></tr>`;
  }
}

async function refreshLog(){
  const d=await api('/api/log');
  document.getElementById('log-box').innerHTML=d.log.slice(0,60).join('<br>');
}

async function toggleScanner(){
  await api('/api/toggle-scanner',{method:'POST'});
  await refreshScanState();
}

async function setPaperConfig(enabled,auto){
  const bal=parseFloat(document.getElementById('paper-bal-input').value)||10000;
  const risk=parseFloat(document.getElementById('paper-risk').value)||1;
  const d=await api('/api/paper-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled,auto_trade:auto,balance:bal,risk_pct:risk})});
  document.getElementById('paper-status').textContent=d.ok?`✅ Paper: ${enabled?'ON':'OFF'}${auto?' + Auto':''}`:('❌ '+JSON.stringify(d));
}

async function resetPaper(){
  await api('/api/paper-reset',{method:'POST'});
  document.getElementById('paper-status').textContent='✅ Paper stats reset';
}

async function closePaper(sym){
  const d=await api('/api/paper-close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym})});
  await refreshPaper();
}

async function setLiveConfig(enabled){
  const key=document.getElementById('api-key').value;
  const sec=document.getElementById('api-secret').value;
  const risk=parseFloat(document.getElementById('live-risk').value)||1;
  const body={enabled,risk_pct:risk};
  if(key) body.api_key=key;
  if(sec) body.api_secret=sec;
  const d=await api('/api/trade-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  document.getElementById('live-status').textContent=d.ok?`✅ Live: ${enabled?'ON':'OFF'}`:'❌ Error';
}
</script>
</body>
</html>
"""

# ════════ FLASK ROUTES ════════════════════════════════════════════

def require_auth():
    token = request.cookies.get("session") or request.headers.get("X-Token")
    return token in sessions

@app.route("/")
def index():
    return make_response(DASHBOARD_HTML, 200, {"Content-Type": "text/html"})

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    if data.get("password") == DASHBOARD_PASSWORD:
        token = secrets.token_hex(32); sessions.add(token)
        resp = make_response(jsonify({"ok": True, "token": token}))
        resp.set_cookie("session", token, max_age=86400*7, httponly=True, samesite="Lax")
        return resp
    return jsonify({"ok": False}), 401

@app.route("/api/logout", methods=["POST"])
def api_logout():
    token = request.cookies.get("session"); sessions.discard(token)
    resp = make_response(jsonify({"ok": True})); resp.delete_cookie("session")
    return resp

@app.route("/api/toggle-scanner", methods=["POST"])
def api_toggle():
    with scan_lock:
        scan_state["enabled"] = not scan_state["enabled"]
        en = scan_state["enabled"]
    log(f"{'▶ RESUMED' if en else '⏸ PAUSED'} by user")
    return jsonify({"enabled": en})

@app.route("/api/signals")
def api_signals():
    limit = min(int(request.args.get("limit", 200)), MAX_SIGNALS)
    return jsonify(list(signals)[:limit])

@app.route("/api/stats")
def api_stats():
    all_s = list(signals)
    return jsonify({"total": len(all_s),
                    "buys":  sum(1 for s in all_s if s.get("direction") == "BUY"),
                    "sells": sum(1 for s in all_s if s.get("direction") == "SELL")})

@app.route("/api/scan-state")
def api_scan_state():
    with scan_lock:
        state = {k: v for k, v in scan_state.items() if k != "log"}
    return jsonify(state)

@app.route("/api/log")
def api_log():
    with scan_lock: return jsonify({"log": list(scan_state["log"])})

@app.route("/api/prices")
def api_prices():
    out = {}
    for sym in TOP_PAIRS:
        t = get_ticker(sym)
        if t: out[sym] = t
    return jsonify(out)

@app.route("/api/trade-config", methods=["GET", "POST"])
def api_trade_config():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        with trade_lock:
            if "api_key"    in data: trade_config["api_key"]    = data["api_key"]
            if "api_secret" in data: trade_config["api_secret"] = data["api_secret"]
            if "risk_pct"   in data: trade_config["risk_pct"]   = float(data["risk_pct"])
            if "max_trades" in data: trade_config["max_trades"] = int(data["max_trades"])
            if "leverage"   in data: trade_config["leverage"]   = int(data["leverage"])
            if "enabled"    in data: trade_config["enabled"]    = bool(data["enabled"])
        log(f"⚙️ Live trade config updated. Enabled: {trade_config['enabled']}")
        return jsonify({"ok": True, "config": {k: v for k, v in trade_config.items() if k != "api_secret"}})
    cfg = {k: ("***" if k == "api_secret" and v else v) for k, v in trade_config.items()}
    return jsonify(cfg)

@app.route("/api/trades")
def api_trades():
    with trade_lock: return jsonify(list(open_trades.values()))

@app.route("/api/trade-close", methods=["POST"])
def api_trade_close():
    data = request.get_json(silent=True) or {}
    symbol = data.get("symbol", "")
    if not symbol: return jsonify({"ok": False, "error": "symbol required"}), 400
    ok, msg = close_trade(symbol, reason="Manual")
    return jsonify({"ok": ok, "message": msg})

@app.route("/api/balance")
def api_balance():
    bal, err = get_account_balance()
    return jsonify({"balance": bal, "error": err})

@app.route("/api/paper-config", methods=["GET", "POST"])
def api_paper_config():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        with paper_lock:
            if "enabled"    in data: paper_config["enabled"]    = bool(data["enabled"])
            if "auto_trade" in data: paper_config["auto_trade"] = bool(data["auto_trade"])
            if "balance"    in data: paper_config["balance"]    = float(data["balance"])
            if "risk_pct"   in data: paper_config["risk_pct"]   = float(data["risk_pct"])
            if "max_trades" in data: paper_config["max_trades"] = int(data["max_trades"])
        log(f"📝 Paper config updated: enabled={paper_config['enabled']} auto={paper_config['auto_trade']}")
        return jsonify({"ok": True, "config": dict(paper_config)})
    with paper_lock: return jsonify(dict(paper_config))

@app.route("/api/paper-trades")
def api_paper_trades():
    with paper_lock: return jsonify(list(paper_trades.values()))

@app.route("/api/paper-history")
def api_paper_history():
    return jsonify(list(paper_history))

@app.route("/api/paper-stats")
def api_paper_stats():
    return jsonify(dict(paper_stats))

@app.route("/api/paper-close", methods=["POST"])
def api_paper_close():
    data = request.get_json(silent=True) or {}
    symbol = data.get("symbol", "")
    if not symbol: return jsonify({"ok": False, "message": "symbol required"}), 400
    ok, msg = close_paper_trade(symbol, reason="Manual")
    return jsonify({"ok": ok, "message": msg})

@app.route("/api/paper-reset", methods=["POST"])
def api_paper_reset():
    with paper_lock:
        paper_trades.clear(); paper_history.clear()
        paper_stats.update({"total": 0, "wins": 0, "losses": 0, "total_pnl": 0.0})
    log("📝 Paper stats reset")
    return jsonify({"ok": True})

@app.route("/api/diag")
def api_diag():
    return jsonify(dict(diag))

@app.route("/health")
def health():
    return jsonify({"status": "healthy", "signals": len(signals),
                    "scanning": scan_state["running"]}), 200

# ════════ STARTUP ════════════════════════════════════════════════

def start_scanner():
    threading.Thread(target=scanner_loop,      daemon=True, name="smc-scanner").start()
    threading.Thread(target=paper_monitor_loop, daemon=True, name="smc-paper").start()
    log("🚀 SMC Scanner + Paper Monitor launched")

_scanner_started = False

def _ensure_started():
    global _scanner_started
    if not _scanner_started:
        _scanner_started = True
        def _delayed():
            time.sleep(2); start_scanner()
        threading.Thread(target=_delayed, daemon=True).start()
        log("🚀 SMC Bot initialising…")

with app.app_context():
    _ensure_started()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
