import os, json, time, secrets, requests, threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify, make_response
from collections import deque

app = Flask(__name__)

# ── Runtime-editable settings (all changeable from the Settings tab) ──
bot_config = {
    "tg_token":    os.environ.get("TELEGRAM_BOT_TOKEN_2", ""),
    "tg_chat_id":  os.environ.get("TELEGRAM_CHAT_ID_2",   ""),
    "password":    os.environ.get("DASHBOARD_PASSWORD_2",  "signal123"),
    "mexc_key":    os.environ.get("MEXC_API_KEY",          ""),
    "mexc_secret": os.environ.get("MEXC_API_SECRET",       ""),
    # scanner
    "min_score":   15,
    "min_rr":      2.0,
    "scan_delay":  5,
}

MAX_SIGNALS = 500
signals     = deque(maxlen=MAX_SIGNALS)
sessions    = set()

trade_config = {
    "enabled":    False,
    "risk_pct":   1.0,
    "max_trades": 3,
    "leverage":   10,
}
open_trades = {}
trade_lock  = threading.Lock()
MEXC_FUTURES = "https://contract.mexc.com/api/v1/private"

scan_state = {
    "running": False, "enabled": True, "current_pair": "",
    "pairs_done": 0, "total_pairs": 0, "scan_count": 0,
    "signals_found": 0, "last_scan": None,
    "log": deque(maxlen=200),
}
scan_lock = threading.Lock()

TOP_PAIRS = ["BTC_USDT","ETH_USDT","SOL_USDT","BNB_USDT","XRP_USDT","DOGE_USDT"]
MEXC_BASE = "https://contract.mexc.com/api/v1/contract"

# ── PAPER TRADING ENGINE ──────────────────────────────────────────────
paper_config = {
    "enabled":    False,
    "auto_trade": False,
    "balance":    10000.0,
    "risk_pct":   1.0,
    "max_trades": 4,
}
paper_trades  = {}
paper_history = deque(maxlen=50)
paper_lock    = threading.Lock()
paper_stats   = {"total": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}

recent_trades = deque(maxlen=10)

diag = {
    "no_candles": 0, "neutral": 0, "no_bias": 0,
    "no_pd_zone": 0, "no_displacement": 0, "no_ob": 0,
    "no_bos": 0, "score_low": 0, "rr_low": 0, "passed": 0,
}

# ════════ MEXC API ═══════════════════════════════════════════════════

def get_all_pairs():
    try:
        r = requests.get(f"{MEXC_BASE}/detail", timeout=15)
        data = r.json()
        if not data.get("success"): return []
        seen = set(); pairs = []
        for item in data.get("data", []):
            sym = item.get("symbol","")
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
        r = requests.get(f"{MEXC_BASE}/ticker", params={"symbol": symbol}, timeout=6)
        data = r.json()
        if data.get("success") and data.get("data"):
            d = data["data"]
            if isinstance(d, list): d = d[0]
            price  = float(d.get("lastPrice", d.get("last", 0)))
            high   = float(d.get("high24h",   d.get("high", 0)))
            low    = float(d.get("low24h",    d.get("low",  0)))
            raw_chg = (d.get("priceChangePercent") or d.get("changeRate") or
                       d.get("riseFallRate") or d.get("rate") or d.get("change24h") or 0)
            change = float(raw_chg)
            if change != 0 and abs(change) < 1.5: change = change * 100
            if change == 0 and price > 0:
                open24 = float(d.get("open24h", d.get("openPrice", d.get("indexPrice", 0))))
                if open24 > 0: change = round((price - open24) / open24 * 100, 2)
            return {"price": round(price,8), "change": round(change,2),
                    "high": round(high,8), "low": round(low,8)}
    except: pass
    return None

# ════════ ICT STRATEGY — CORE FUNCTIONS ══════════════════════════════

def find_swing_highs_lows(candles, n=3):
    highs = [c["high"] for c in candles]
    lows  = [c["low"]  for c in candles]
    sh, sl = [], []
    for i in range(n, len(candles)-n):
        if all(highs[i] >= highs[i-j] and highs[i] >= highs[i+j] for j in range(1, n+1)):
            sh.append((i, highs[i]))
        if all(lows[i]  <= lows[i-j]  and lows[i]  <= lows[i+j]  for j in range(1, n+1)):
            sl.append((i, lows[i]))
    return sh, sl

def detect_htf_bias(candles):
    """
    Returns ('BULLISH'|'BEARISH'|'NEUTRAL', bos_level, choch_level, sh, sl).
    Uses BOS and CHoCH on the given candle set.
    """
    if len(candles) < 30: return "NEUTRAL", None, None, [], []
    sh, sl = find_swing_highs_lows(candles, n=3)
    if len(sh) < 2 or len(sl) < 2: return "NEUTRAL", None, None, sh, sl

    last_sh = sh[-1]; prev_sh = sh[-2]
    last_sl = sl[-1]; prev_sl = sl[-2]

    hh = last_sh[1] > prev_sh[1]
    hl = last_sl[1] > prev_sl[1]
    lh = last_sh[1] < prev_sh[1]
    ll = last_sl[1] < prev_sl[1]

    bos_level = None; choch_level = None
    closes = [c["close"] for c in candles[-20:]]

    if hh and hl:
        bos_level = last_sh[1]
        return "BULLISH", bos_level, choch_level, sh, sl
    if lh and ll:
        bos_level = last_sl[1]
        return "BEARISH", bos_level, choch_level, sh, sl
    if hh and ll:
        choch_level = last_sh[1]
        return "BULLISH", bos_level, choch_level, sh, sl
    if lh and hl:
        choch_level = last_sl[1]
        return "BEARISH", bos_level, choch_level, sh, sl

    # Fallback: moving average direction
    a1 = sum(closes[:10])/10; a2 = sum(closes[10:])/10
    if a2 > a1 * 1.002: return "BULLISH", None, None, sh, sl
    if a2 < a1 * 0.998: return "BEARISH", None, None, sh, sl
    return "NEUTRAL", None, None, sh, sl

def is_market_structured(sh, sl, direction, min_pts=2):
    """Check that market is printing clear structure (not choppy/ranging)."""
    if direction == "BULLISH":
        highs_ok = len(sh) >= min_pts and all(sh[i][1] > sh[i-1][1] for i in range(1, len(sh)))
        lows_ok  = len(sl) >= min_pts and all(sl[i][1] > sl[i-1][1] for i in range(1, len(sl)))
    else:
        highs_ok = len(sh) >= min_pts and all(sh[i][1] < sh[i-1][1] for i in range(1, len(sh)))
        lows_ok  = len(sl) >= min_pts and all(sl[i][1] < sl[i-1][1] for i in range(1, len(sl)))
    return highs_ok or lows_ok

def get_premium_discount(candles, lookback=50):
    """Return (swing_high, swing_low, equilibrium, range_size)."""
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    swing_high = max(c["high"] for c in recent)
    swing_low  = min(c["low"]  for c in recent)
    full_range = swing_high - swing_low
    eq = swing_low + full_range * 0.5
    return swing_high, swing_low, eq, full_range

def price_in_pd_zone(price, swing_high, swing_low, eq, direction):
    """Check if price is in the correct Premium/Discount zone for the trade direction."""
    if direction == "BUY":
        return price <= eq, "DISCOUNT" if price <= eq else "PREMIUM"
    else:
        return price >= eq, "PREMIUM" if price >= eq else "DISCOUNT"

def detect_liquidity_sweep(candles, direction, lookback=40):
    """
    Detect if recent candles swept previous lows (BUY) or highs (SELL).
    Returns (swept: bool, sweep_level: float|None)
    """
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    if len(recent) < 10: return False, None

    # Use candles up to second-to-last 8 for previous structure
    ref = recent[:-8]
    if not ref: return False, None

    if direction == "BUY":
        prev_lows = [c["low"] for c in ref]
        if not prev_lows: return False, None
        prev_low = min(prev_lows)
        # Check if any recent candle dipped below and then closed back above
        sweep_candles = recent[-8:]
        for c in sweep_candles:
            if c["low"] < prev_low:
                return True, round(c["low"], 8)
    else:
        prev_highs = [c["high"] for c in ref]
        if not prev_highs: return False, None
        prev_high = max(prev_highs)
        sweep_candles = recent[-8:]
        for c in sweep_candles:
            if c["high"] > prev_high:
                return True, round(c["high"], 8)
    return False, None

def detect_displacement(candles, direction, lookback=20):
    """
    Detect a strong impulsive move (displacement).
    Returns (found: bool, strength: float 0-1, displacement_candle: dict|None)
    """
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    if len(recent) < 5: return False, 0.0, None

    # Find the largest body candle in recent history
    best = None; best_body = 0.0
    for c in recent[-15:]:
        body = abs(c["close"] - c["open"])
        rng  = c["high"] - c["low"]
        if rng <= 0: continue
        body_ratio = body / rng
        if direction == "BUY":
            if c["close"] > c["open"] and body_ratio > 0.6 and body > best_body:
                best = c; best_body = body
        else:
            if c["close"] < c["open"] and body_ratio > 0.6 and body > best_body:
                best = c; best_body = body

    if best is None: return False, 0.0, None

    # Compare body size to average candle range
    avg_range = sum(c["high"]-c["low"] for c in recent) / len(recent)
    if avg_range <= 0: return False, 0.0, None
    strength = min(best_body / avg_range, 1.0)
    if strength < 0.5: return False, strength, None
    return True, round(strength, 3), best

def detect_bos_after_displacement(candles, direction, displacement_candle, lookback=20):
    """
    After displacement, check if there's a Break of Structure (BOS):
    price closes above a previous swing high (BUY) or below a previous swing low (SELL).
    """
    if displacement_candle is None: return False, None
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    if len(recent) < 5: return False, None

    # Find displacement candle index
    disp_idx = None
    for i, c in enumerate(recent):
        if c["time"] == displacement_candle["time"]:
            disp_idx = i; break
    if disp_idx is None: disp_idx = len(recent) - 5

    pre_displacement = recent[:disp_idx]
    post_displacement = recent[disp_idx+1:]

    if not pre_displacement or not post_displacement: return False, None

    if direction == "BUY":
        prev_sh, _ = find_swing_highs_lows(pre_displacement, n=2)
        if not prev_sh: return False, None
        last_high = max(h for _, h in prev_sh)
        for c in post_displacement:
            if c["close"] > last_high:
                return True, round(last_high, 8)
    else:
        _, prev_sl = find_swing_highs_lows(pre_displacement, n=2)
        if not prev_sl: return False, None
        last_low = min(l for _, l in prev_sl)
        for c in post_displacement:
            if c["close"] < last_low:
                return True, round(last_low, 8)
    return False, None

def find_order_block(candles, direction, displacement_candle=None, lookback=30):
    """
    Find the most recent valid Order Block.
    OB for BUY: last bearish candle before the bullish displacement move.
    OB for SELL: last bullish candle before the bearish displacement move.
    Returns (ob: dict|None)
    """
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    if len(recent) < 5: return None

    # Find displacement candle position
    disp_idx = len(recent) - 5
    if displacement_candle:
        for i, c in enumerate(recent):
            if c["time"] == displacement_candle["time"]:
                disp_idx = i; break

    # Search backwards from displacement for the OB candle
    search_range = recent[max(0, disp_idx-15):disp_idx+1]
    if not search_range: return None

    if direction == "BUY":
        # Last bearish candle before the displacement
        for c in reversed(search_range):
            if c["close"] < c["open"]:
                body_size = abs(c["close"] - c["open"])
                rng = c["high"] - c["low"]
                body_ratio = body_size / rng if rng > 0 else 0
                return {
                    "top":  c["open"],
                    "bot":  c["close"],
                    "high": c["high"],
                    "low":  c["low"],
                    "time": c["time"],
                    "type": "BULLISH_OB",
                    "body_ratio": round(body_ratio, 3),
                }
    else:
        # Last bullish candle before the displacement
        for c in reversed(search_range):
            if c["close"] > c["open"]:
                body_size = abs(c["close"] - c["open"])
                rng = c["high"] - c["low"]
                body_ratio = body_size / rng if rng > 0 else 0
                return {
                    "top":  c["close"],
                    "bot":  c["open"],
                    "high": c["high"],
                    "low":  c["low"],
                    "time": c["time"],
                    "type": "BEARISH_OB",
                    "body_ratio": round(body_ratio, 3),
                }
    return None

def price_tapping_ob(candles, ob, direction):
    """Check if current price is tapping/inside the OB zone."""
    recent = candles[-10:]
    if direction == "BUY":
        return any(c["low"] <= ob["top"] and c["high"] >= ob["bot"] for c in recent)
    else:
        return any(c["high"] >= ob["bot"] and c["low"] <= ob["top"] for c in recent)

def find_tp_levels(candles, direction, entry, lookback=60):
    """
    Find TP1 = nearest opposing liquidity level (swing high/low).
    TP2 = next liquidity level beyond TP1.
    """
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    sh, sl = find_swing_highs_lows(recent, n=2)

    if direction == "BUY":
        # TP targets are swing highs above entry
        targets = sorted([h for _, h in sh if h > entry])
        tp1 = targets[0] if targets else None
        tp2 = targets[1] if len(targets) > 1 else None
    else:
        # TP targets are swing lows below entry
        targets = sorted([l for _, l in sl if l < entry], reverse=True)
        tp1 = targets[0] if targets else None
        tp2 = targets[1] if len(targets) > 1 else None

    return tp1, tp2

def check_prev_obs_respected(candles, direction, lookback=80):
    """
    Check if previous OB zones were respected (price tapped and reacted).
    Optional scoring bonus.
    """
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    if len(recent) < 20: return False

    sh, sl = find_swing_highs_lows(recent[:-15], n=2)
    if not sh or not sl: return False

    if direction == "BUY":
        # Did price tap near swing lows and bounce?
        for _, low_val in sl[-3:]:
            taps = [c for c in recent[-30:] if c["low"] <= low_val * 1.005]
            bounces = [c for c in recent[-20:] if c["close"] > low_val * 1.003 and c["close"] > c["open"]]
            if taps and bounces: return True
    else:
        for _, high_val in sh[-3:]:
            taps = [c for c in recent[-30:] if c["high"] >= high_val * 0.995]
            rejects = [c for c in recent[-20:] if c["close"] < high_val * 0.997 and c["close"] < c["open"]]
            if taps and rejects: return True
    return False

def find_fvg(candles, direction, lookback=30):
    """Find a Fair Value Gap near the OB for confluence."""
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    for i in range(len(recent)-3, max(0, len(recent)-25), -1):
        c1 = recent[i]; c3 = recent[i+2]
        if direction == "BUY":
            if c3["low"] > c1["high"]:
                return True, round(c1["high"], 8), round(c3["low"], 8)
        else:
            if c3["high"] < c1["low"]:
                return True, round(c3["high"], 8), round(c1["low"], 8)
    return False, None, None

# ════════ SIGNAL SCORING ══════════════════════════════════════════════

def score_signal_ict(
        htf_4h_bias, htf_1h_bias, direction,
        liq_swept, displacement_strength,
        bos_found, ob, ob_respected,
        fvg_found, in_pd_zone, pd_zone_name,
        is_structured, rr):
    """
    Score out of 20:
    - HTF alignment (0-5)
    - Liquidity sweep quality (0-5)
    - Displacement strength (0-5)
    - OB/FVG quality + market structure (0-5)
    Only signals >= 15/20 are emitted.
    """
    score = 0; details = []

    # 1. Higher Timeframe Alignment (0-5)
    dir_bias = "BULLISH" if direction == "BUY" else "BEARISH"
    if htf_4h_bias == dir_bias and htf_1h_bias == dir_bias:
        score += 5; details.append("✅ Both 4H & 1H aligned (+5)")
    elif htf_4h_bias == dir_bias or htf_1h_bias == dir_bias:
        score += 3; details.append("⚠️ One HTF aligned (+3)")
    else:
        details.append("❌ No HTF alignment (+0)")

    # 2. Liquidity Sweep Quality (0-5)
    if liq_swept:
        score += 5; details.append("✅ Liquidity sweep confirmed (+5)")
    else:
        score += 2; details.append("⚠️ No liquidity sweep (+2)")

    # 3. Displacement Strength (0-5)
    if displacement_strength >= 0.85:
        score += 5; details.append(f"✅ Very strong displacement ({displacement_strength:.0%}) (+5)")
    elif displacement_strength >= 0.65:
        score += 4; details.append(f"✅ Strong displacement ({displacement_strength:.0%}) (+4)")
    elif displacement_strength >= 0.5:
        score += 3; details.append(f"⚠️ Moderate displacement ({displacement_strength:.0%}) (+3)")
    else:
        details.append(f"❌ Weak displacement ({displacement_strength:.0%}) (+0)")

    # 4. OB/FVG Quality + Structure (0-5)
    sub = 0
    if bos_found:
        sub += 2; details.append("✅ BOS confirmed (+2)")
    else:
        details.append("⚠️ No BOS (+0)")

    if ob and ob.get("body_ratio", 0) >= 0.6:
        sub += 1; details.append(f"✅ Quality OB (body ratio {ob['body_ratio']:.0%}) (+1)")
    elif ob:
        details.append("⚠️ OB present but weak body (+0)")

    if fvg_found:
        sub += 1; details.append("✅ FVG confluence (+1)")

    if in_pd_zone:
        sub += 1; details.append(f"✅ {pd_zone_name} zone (+1)")

    if ob_respected:
        sub = min(sub + 1, 5); details.append("✅ Previous OBs respected (+bonus)")

    if is_structured:
        sub = min(sub + 1, 5); details.append("✅ Clear market structure (+bonus)")

    score += min(sub, 5)

    # Grade
    if score >= 18:   grade = "A+"
    elif score >= 16: grade = "A"
    elif score >= 15: grade = "B"
    else:             grade = "D"

    return min(score, 20), grade, details

# ════════ TELEGRAM ════════════════════════════════════════════════════

def send_telegram(msg):
    tok = bot_config.get("tg_token","")
    cid = bot_config.get("tg_chat_id","")
    if not tok or not cid: return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            json={"chat_id": cid, "text": msg, "parse_mode": "HTML"},
            timeout=10)
        return r.status_code == 200
    except: return False

def fmt_tg(sig):
    e = "🟢" if sig["direction"] == "BUY" else "🔴"
    bars = "█"*(sig["score"]*2//10) + "░"*(10-sig["score"]*2//10)
    liq  = "✅" if sig.get("liq_swept") else "⚠️ None"
    bos  = "✅" if sig.get("bos_found") else "⚠️"
    fvg  = "✅" if sig.get("fvg_found") else "⚠️ None"
    ob_zone = sig.get("pd_zone", "–")
    tf_map = {"Min15":"15M","Min30":"30M","Min60":"1H"}
    entry_tf = tf_map.get(sig.get("entry_tf",""),"–")
    return (
        f"{e} <b>ICT MODEL — {sig['direction']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Pair:</b>       {sig['symbol']}\n"
        f"<b>Entry TF:</b>   {entry_tf}\n"
        f"<b>HTF Bias:</b>   4H:{sig.get('bias_4h','–')} | 1H:{sig.get('bias_1h','–')}\n"
        f"<b>Zone:</b>       {ob_zone}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>🎯 Entry:</b>   {sig['entry']} (OB Zone)\n"
        f"<b>🛑 SL:</b>      {sig['sl']}\n"
        f"<b>🎯 TP1:</b>     {sig['tp']}\n"
        f"<b>🎯 TP2:</b>     {sig.get('tp2','–')}\n"
        f"<b>📊 RR:</b>      {sig['rr']}R\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>AI Score:</b>   {sig['score']}/20 [{bars}] {sig['grade']}\n"
        f"<b>Liq Sweep:</b>  {liq}\n"
        f"<b>BOS:</b>        {bos}\n"
        f"<b>FVG:</b>        {fvg}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>ICT Strategy Scanner • {sig['timestamp']}</i>"
    )

# ════════ LOGGER ══════════════════════════════════════════════════════

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"; print(line, flush=True)
    with scan_lock: scan_state["log"].appendleft(line)

# ════════ SCAN PAIR ════════════════════════════════════════════════════

def scan_pair(symbol):
    results = []

    # --- Step 1: HTF Bias on 4H ---
    candles_4h = get_candles(symbol, "Hour4", limit=150)
    if not candles_4h or len(candles_4h) < 30:
        diag["no_candles"] += 1; return results

    bias_4h, bos_4h, choch_4h, sh_4h, sl_4h = detect_htf_bias(candles_4h)
    if bias_4h == "NEUTRAL":
        diag["neutral"] += 1; return results

    # --- Step 2: HTF Bias on 1H ---
    candles_1h = get_candles(symbol, "Min60", limit=150)
    if not candles_1h or len(candles_1h) < 30:
        diag["no_candles"] += 1; return results

    bias_1h, bos_1h, choch_1h, sh_1h, sl_1h = detect_htf_bias(candles_1h)

    # Both must agree OR at least 4H must be clear
    direction = "BUY" if bias_4h == "BULLISH" else "SELL"
    if bias_4h == "NEUTRAL" and bias_1h == "NEUTRAL":
        diag["no_bias"] += 1; return results

    # Use 4H bias as primary
    htf_direction = "BULLISH" if bias_4h == "BULLISH" else "BEARISH"
    if bias_4h == "NEUTRAL": htf_direction = "BULLISH" if bias_1h == "BULLISH" else "BEARISH"

    direction = "BUY" if htf_direction == "BULLISH" else "SELL"

    # --- Step 3: Check clear market structure (not choppy) ---
    is_structured_4h = is_market_structured(sh_4h, sl_4h, htf_direction, min_pts=2)
    is_structured_1h = is_market_structured(sh_1h, sl_1h, htf_direction, min_pts=2)
    is_structured = is_structured_4h or is_structured_1h
    if not is_structured:
        diag["neutral"] += 1; return results

    # --- Step 4: Entry TF (15M then 30M) ---
    for entry_tf in ["Min15", "Min30"]:
        if results: break

        candles_etf = get_candles(symbol, entry_tf, limit=200)
        if not candles_etf or len(candles_etf) < 40:
            continue

        # Step 4a: Premium / Discount zone
        current_price = candles_etf[-1]["close"]
        swing_high, swing_low, eq, full_range = get_premium_discount(candles_etf, lookback=80)
        if full_range <= 0: continue

        in_pd, pd_zone_name = price_in_pd_zone(current_price, swing_high, swing_low, eq, direction)
        if not in_pd:
            diag["no_pd_zone"] += 1; continue

        # Step 4b: Liquidity Sweep (optional but scored)
        liq_swept, sweep_level = detect_liquidity_sweep(candles_etf, direction, lookback=50)

        # Step 4c: Displacement
        disp_found, disp_strength, disp_candle = detect_displacement(candles_etf, direction, lookback=30)
        if not disp_found:
            diag["no_displacement"] += 1; continue

        # Step 4d: BOS after displacement
        bos_found, bos_level = detect_bos_after_displacement(candles_etf, direction, disp_candle)
        if not bos_found:
            diag["no_bos"] += 1; continue

        # Step 4e: Find OB
        ob = find_order_block(candles_etf, direction, disp_candle)
        if ob is None:
            diag["no_ob"] += 1; continue

        # Step 4f: FVG confluence
        fvg_found, fvg_bot, fvg_top = find_fvg(candles_etf, direction)

        # Step 4g: Previous OBs respected (optional)
        ob_respected = check_prev_obs_respected(candles_etf, direction)

        # Step 4h: Entry, SL, TP
        if direction == "BUY":
            entry = round(ob["top"], 8)
            # SL: below sweep low OR below OB, whichever is safer (lower)
            sl_ob  = round(ob["bot"] * 0.9995, 8)
            sl_liq = round(sweep_level * 0.9995, 8) if sweep_level else sl_ob
            sl_p   = min(sl_ob, sl_liq)
        else:
            entry = round(ob["bot"], 8)
            sl_ob  = round(ob["top"] * 1.0005, 8)
            sl_liq = round(sweep_level * 1.0005, 8) if sweep_level else sl_ob
            sl_p   = max(sl_ob, sl_liq)

        tp1, tp2 = find_tp_levels(candles_etf, direction, entry)

        if tp1 is None:
            # Fallback TP from swing extremes
            if direction == "BUY":
                tp1 = round(swing_high * 0.998, 8)
            else:
                tp1 = round(swing_low * 1.002, 8)

        risk   = abs(entry - sl_p)
        reward = abs(tp1 - entry)
        rr     = round(reward / risk, 2) if risk > 0 else 0

        if rr < bot_config.get("min_rr", 2.0):
            diag["rr_low"] += 1; continue

        # Step 4i: Score
        score, grade, details = score_signal_ict(
            htf_4h_bias=bias_4h,
            htf_1h_bias=bias_1h,
            direction=direction,
            liq_swept=liq_swept,
            displacement_strength=disp_strength,
            bos_found=bos_found,
            ob=ob,
            ob_respected=ob_respected,
            fvg_found=fvg_found,
            in_pd_zone=in_pd,
            pd_zone_name=pd_zone_name,
            is_structured=is_structured,
            rr=rr,
        )

        if score < bot_config.get("min_score", 15):
            diag["score_low"] += 1; continue

        diag["passed"] += 1
        results.append({
            "symbol":    symbol,
            "tf":        entry_tf,
            "entry_tf":  entry_tf,
            "ob_tf":     entry_tf,
            "ob_zone":   pd_zone_name,
            "pd_zone":   pd_zone_name,
            "direction": direction,
            "trend":     htf_direction,
            "bias_4h":   bias_4h,
            "bias_1h":   bias_1h,
            "entry":     round(entry, 8),
            "entry_type":"OB Zone",
            "sl":        round(sl_p, 8),
            "tp":        round(tp1, 8),
            "tp2":       round(tp2, 8) if tp2 else "–",
            "rr":        rr,
            "crh":       swing_high,
            "crl":       swing_low,
            "ob_top":    ob["top"],
            "ob_bot":    ob["bot"],
            "score":     score,
            "grade":     grade,
            "details":   details,
            "liq_swept":         liq_swept,
            "sweep_level":       sweep_level or "–",
            "disp_strength":     disp_strength,
            "bos_found":         bos_found,
            "bos_level":         bos_level or "–",
            "fvg_found":         fvg_found,
            "fvg_bot":           fvg_bot or "–",
            "fvg_top":           fvg_top or "–",
            "ob_respected":      ob_respected,
            "is_structured":     is_structured,
            # Compatibility fields for dashboard
            "tbs_found":  bos_found,
            "tbs_tf":     entry_tf,
            "choch_found":bos_found,
            "choch_level":bos_level or "–",
            "continuous": is_structured,
            "timestamp":  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        })

    return results

# ════════ MEXC AUTO-TRADE ENGINE ══════════════════════════════════════

import hmac, hashlib, urllib.parse

def mexc_sign(params, secret):
    query = urllib.parse.urlencode(sorted(params.items()))
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()

def mexc_request(method, path, params=None, signed=True):
    api_key    = bot_config.get("mexc_key","")
    api_secret = bot_config.get("mexc_secret","")
    if not api_key or not api_secret:
        return None, "MEXC API keys not configured — add them in Settings tab"
    params = params or {}
    headers = {"Content-Type": "application/json", "ApiKey": api_key}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["ApiKey"]    = api_key
        params["sign"]      = mexc_sign(params, api_secret)
    try:
        url = f"{MEXC_FUTURES}{path}"
        if method == "GET":
            r = requests.get(url, params=params, headers=headers, timeout=10)
        else:
            r = requests.post(url, json=params, headers=headers, timeout=10)
        data = r.json()
        if data.get("success") or data.get("code") == 0:
            return data.get("data"), None
        return None, data.get("message","Unknown error")
    except Exception as e:
        return None, str(e)

def get_account_balance():
    data, err = mexc_request("GET", "/account/assets")
    if err or not data: return 0.0, err
    for asset in (data if isinstance(data, list) else [data]):
        if asset.get("currency") == "USDT":
            return float(asset.get("availableBalance", 0)), None
    return 0.0, "USDT balance not found"

def get_symbol_info(symbol):
    try:
        r = requests.get(f"{MEXC_BASE}/detail", timeout=10)
        data = r.json()
        for item in data.get("data", []):
            if item.get("symbol") == symbol:
                return {
                    "min_vol":       float(item.get("minVol", 1)),
                    "contract_size": float(item.get("contractSize", 1)),
                    "price_unit":    float(item.get("priceUnit", 0.01)),
                }
    except: pass
    return {"min_vol": 1, "contract_size": 1, "price_unit": 0.01}

def place_order(sig):
    with trade_lock:
        if not trade_config["enabled"]:       return False, "Auto-trade disabled"
        if len(open_trades) >= trade_config["max_trades"]: return False, f"Max trades reached"
        if sig["symbol"] in open_trades:      return False, f"Already open on {sig['symbol']}"

    balance, err = get_account_balance()
    if err: return False, f"Balance error: {err}"
    if balance < 10: return False, "Insufficient balance (min $10)"

    entry = float(sig["entry"]); sl = float(sig["sl"]); tp = float(sig["tp"])
    margin    = balance * 0.20
    info      = get_symbol_info(sig["symbol"])
    sl_dist   = abs(entry - sl)
    if sl_dist <= 0: return False, "SL distance is zero"
    size      = int(margin / (sl_dist * info["contract_size"]))
    size      = max(int(info["min_vol"]), size)
    if size <= 0: return False, "Position size too small"

    side      = 1 if sig["direction"] == "BUY" else 2
    open_type = 2
    sl_pct    = abs(entry - sl) / entry if entry > 0 else 0.01
    max_safe_lev = int(1.0 / sl_pct) if sl_pct > 0 else 10
    leverage  = max(10, min(500, min(max_safe_lev, trade_config["leverage"])))

    mexc_request("POST", "/position/change_leverage",
                 {"symbol": sig["symbol"], "leverage": leverage,
                  "openType": open_type, "positionType": side})

    order_data, err = mexc_request("POST", "/order/submit", {
        "symbol": sig["symbol"], "price": entry, "vol": size,
        "side": side, "type": 1, "openType": open_type, "leverage": leverage,
    })
    if err: return False, f"Order failed: {err}"
    order_id = order_data if isinstance(order_data, str) else order_data.get("orderId","")

    mexc_request("POST", "/order/set_stop_loss",
                 {"symbol": sig["symbol"], "stopLossPrice": sl,
                  "positionType": side, "openType": open_type, "vol": size})
    mexc_request("POST", "/order/set_take_profit",
                 {"symbol": sig["symbol"], "takeProfitPrice": tp,
                  "positionType": side, "openType": open_type, "vol": size})

    with trade_lock:
        open_trades[sig["symbol"]] = {
            "order_id":  order_id, "symbol": sig["symbol"],
            "direction": sig["direction"], "entry": entry,
            "sl": sl, "tp": tp, "size": size,
            "rr": sig["rr"], "score": sig["score"], "grade": sig["grade"],
            "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "status": "OPEN",
        }

    log(f"🤖 AUTO-TRADE PLACED: {sig['direction']} {sig['symbol']} Entry:{entry} SL:{sl} TP:{tp} Size:{size}")
    send_telegram(
        f"<b>AUTO-TRADE PLACED</b>\n<b>Pair:</b> {sig['symbol']}\n"
        f"<b>Side:</b> {sig['direction']}\n<b>Entry:</b> {entry}\n"
        f"<b>SL:</b> {sl}\n<b>TP:</b> {tp}\n<b>Size:</b> {size} contracts\n"
        f"<b>RR:</b> {sig['rr']}R | Score: {sig['score']}/20 {sig['grade']}\n"
        "<i>ICT Strategy Auto-Trade</i>"
    )
    return True, f"Order placed: {order_id}"

def close_trade(symbol, reason="Manual"):
    with trade_lock:
        if symbol not in open_trades: return False, "No open trade found"
        trade = open_trades[symbol]

    side = 2 if trade["direction"] == "BUY" else 1
    _, err = mexc_request("POST", "/order/submit",
                          {"symbol": symbol, "price": 0, "vol": trade["size"],
                           "side": side, "type": 5, "openType": 1})
    if err: return False, f"Close failed: {err}"

    with trade_lock:
        completed = dict(open_trades[symbol])
        completed["status"]    = f"CLOSED ({reason})"
        completed["closed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        recent_trades.appendleft(completed)
        del open_trades[symbol]

    log(f"TRADE CLOSED: {symbol} | Reason: {reason}")
    send_telegram(f"TRADE CLOSED: {symbol} {completed['direction']}\n"
                  f"Entry: {completed['entry']} | Size: {completed['size']}\nReason: {reason}")
    return True, "Position closed"

# ════════ PAPER TRADING ENGINE ════════════════════════════════════════

def place_paper_order(sig):
    with paper_lock:
        if not paper_config["enabled"]:  return False, "Paper trading disabled"
        if not paper_config["auto_trade"]: return False, "Paper auto-trade disabled"
        if len(paper_trades) >= paper_config["max_trades"]:
            return False, f"Max paper trades reached"
        if sig["symbol"] in paper_trades:
            return False, f"Already have paper trade on {sig['symbol']}"

        balance     = paper_config["balance"]
        entry       = float(sig["entry"]); sl = float(sig["sl"]); tp = float(sig["tp"])
        risk_amount = balance * paper_config["risk_pct"] / 100
        sl_distance = abs(entry - sl)
        if sl_distance <= 0: return False, "SL distance is zero"
        contracts = round(risk_amount / sl_distance, 6)

        paper_trades[sig["symbol"]] = {
            "symbol": sig["symbol"], "direction": sig["direction"],
            "entry": entry, "current_price": entry,
            "sl": sl, "tp": tp, "size": contracts,
            "risk_amount": round(risk_amount, 2),
            "rr": sig["rr"], "score": sig["score"], "grade": sig["grade"],
            "tf": sig.get("tf","–"), "ob_zone": sig.get("ob_zone","–"),
            "pnl": 0.0, "pnl_pct": 0.0,
            "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "status": "OPEN",
        }

    log(f"📝 PAPER TRADE: {sig['direction']} {sig['symbol']} Entry:{entry} SL:{sl} TP:{tp}")
    return True, f"Paper trade placed on {sig['symbol']}"

def close_paper_trade(symbol, reason="Manual", close_price=None):
    with paper_lock:
        if symbol not in paper_trades: return False, "No paper trade found"
        trade = dict(paper_trades[symbol])

    if close_price is None:
        ticker = get_ticker(symbol)
        close_price = ticker["price"] if ticker else trade["entry"]

    entry = trade["entry"]; size = trade["size"]; direction = trade["direction"]
    pnl = (close_price - entry) * size if direction == "BUY" else (entry - close_price) * size
    risk_amount = max(trade["risk_amount"], 1.0)
    pnl_pct = round((pnl / risk_amount) * 100, 2)

    with paper_lock:
        paper_config["balance"] = round(paper_config["balance"] + pnl, 2)
        completed = dict(paper_trades[symbol])
        completed.update({
            "status": f"CLOSED ({reason})",
            "close_price": round(close_price, 8),
            "pnl": round(pnl, 2), "pnl_pct": pnl_pct,
            "closed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        })
        paper_history.appendleft(completed)
        del paper_trades[symbol]
        paper_stats["total"] += 1
        if pnl > 0: paper_stats["wins"] += 1
        else: paper_stats["losses"] += 1
        paper_stats["total_pnl"] = round(paper_stats["total_pnl"] + pnl, 2)

    sign = "+" if pnl >= 0 else ""
    log(f"📝 PAPER CLOSED: {symbol} {direction} PnL:{sign}{pnl:.2f} USDT | {reason}")
    return True, f"Paper trade closed. PnL: {sign}{pnl:.2f} USDT"

def paper_monitor_loop():
    log("📝 Paper trading monitor started")
    while True:
        try:
            with paper_lock: symbols = list(paper_trades.keys())
            for symbol in symbols:
                with paper_lock:
                    if symbol not in paper_trades: continue
                    trade = dict(paper_trades[symbol])
                ticker = get_ticker(symbol)
                if not ticker: continue
                price = ticker["price"]
                entry = trade["entry"]; size = trade["size"]
                direction = trade["direction"]; sl = trade["sl"]; tp = trade["tp"]
                pnl = (price - entry)*size if direction=="BUY" else (entry - price)*size
                risk_amount = max(trade["risk_amount"], 1.0)
                pnl_pct = round((pnl / risk_amount) * 100, 2)
                with paper_lock:
                    if symbol in paper_trades:
                        paper_trades[symbol]["current_price"] = round(price, 8)
                        paper_trades[symbol]["pnl"]           = round(pnl, 2)
                        paper_trades[symbol]["pnl_pct"]       = pnl_pct
                if direction == "BUY":
                    if price <= sl:  close_paper_trade(symbol, "SL Hit", price)
                    elif price >= tp: close_paper_trade(symbol, "TP Hit", price)
                else:
                    if price >= sl:  close_paper_trade(symbol, "SL Hit", price)
                    elif price <= tp: close_paper_trade(symbol, "TP Hit", price)
        except Exception as e:
            log(f"❌ Paper monitor error: {e}")
        time.sleep(15)

# ════════ SCANNER LOOP ════════════════════════════════════════════════

def scanner_loop():
    with scan_lock: scan_state["running"] = True
    log("🚀 ICT Strategy Scanner started — scanning USDT perpetual pairs")
    while True:
        try:
            with scan_lock:
                if not scan_state["enabled"]:
                    scan_state["running"] = False
            if not scan_state["enabled"]:
                time.sleep(5); continue
            with scan_lock: scan_state["running"] = True

            pairs = get_all_pairs()
            if not pairs:
                log("⚠️ No pairs fetched — retrying in 30s")
                time.sleep(30); continue

            with scan_lock:
                scan_state["total_pairs"] = len(pairs)
                scan_state["pairs_done"]  = 0
                scan_state["scan_count"] += 1

            log(f"🔄 Scan #{scan_state['scan_count']} — {len(pairs)} USDT pairs")
            scanned_this_cycle = set()

            for i, symbol in enumerate(pairs):
                if not scan_state["enabled"]: break
                if symbol in scanned_this_cycle: continue
                scanned_this_cycle.add(symbol)

                with scan_lock:
                    scan_state["current_pair"] = symbol
                    scan_state["pairs_done"]   = i + 1

                try:
                    res = scan_pair(symbol)
                    for sig in res:
                        recent_sigs = list(signals)[:50]
                        duplicate = any(
                            s.get("symbol") == sig["symbol"] and
                            s.get("direction") == sig["direction"] and
                            s.get("tf") == sig["tf"]
                            for s in recent_sigs
                        )
                        if duplicate:
                            log(f"⏭ SKIP duplicate: {sig['direction']} {symbol} {sig['tf']}")
                            continue
                        signals.appendleft(sig)
                        with scan_lock: scan_state["signals_found"] += 1
                        tf_lbl = {"Min15":"15M","Min30":"30M","Min60":"1H"}.get(sig["tf"],"–")
                        log(f"🎯 {sig['direction']} {symbol} | {tf_lbl} | {sig['pd_zone']} | Score:{sig['score']}/20 {sig['grade']} | RR:{sig['rr']}R")
                        send_telegram(fmt_tg(sig))
                        if trade_config["enabled"] and bot_config.get("mexc_key",""):
                            ok, msg = place_order(sig)
                            log(f"{'✅' if ok else '❌'} Auto-trade: {msg}")
                        if paper_config["enabled"] and paper_config["auto_trade"]:
                            ok2, msg2 = place_paper_order(sig)
                            if ok2: log(f"📝 Paper auto: {msg2}")
                except Exception as e:
                    log(f"⚠️ Scan error {symbol}: {e}")
                time.sleep(5)
                if (i+1) % 50 == 0:
                    log(f"📊 Progress: {i+1}/{scan_state['total_pairs']} pairs scanned")

            with scan_lock:
                scan_state["last_scan"] = datetime.now(timezone.utc).strftime("%H:%M UTC")
            log(f"✅ Scan #{scan_state['scan_count']} complete — {len(pairs)} pairs")
            log(f"📊 GATES: neutral={diag.get('neutral',0)} no_pd={diag.get('no_pd_zone',0)} "
                f"no_disp={diag.get('no_displacement',0)} no_bos={diag.get('no_bos',0)} "
                f"no_ob={diag.get('no_ob',0)} score_low={diag.get('score_low',0)} "
                f"rr_low={diag.get('rr_low',0)} PASSED={diag['passed']}")
            for k in diag: diag[k] = 0
            log("⏸ Cycle rest — 60s before next scan round...")
            time.sleep(60)

        except Exception as e:
            log(f"❌ Scanner error: {e}"); time.sleep(15)

# ════════ HTML ════════════════════════════════════════════════════════

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>ICT Strategy Scanner 🎯</title>
<link href="https://fonts.googleapis.com/css2?family=Fredoka+One&family=Nunito:wght@400;600;700;800;900&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet"/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Nunito',sans-serif;background:#0f0e1a;min-height:100vh;display:flex;align-items:center;justify-content:center;overflow:hidden;padding:20px}
.stars{position:fixed;inset:0;z-index:0}
.star{position:absolute;border-radius:50%;background:#fff;animation:twink 3s infinite}
@keyframes twink{0%,100%{opacity:.15;transform:scale(1)}50%{opacity:.9;transform:scale(1.4)}}
.blob{position:fixed;border-radius:50%;filter:blur(70px);opacity:.18;animation:blob-float 10s ease-in-out infinite;z-index:0}
.b1{width:380px;height:380px;background:#7c3aed;top:-120px;left:-80px}
.b2{width:300px;height:300px;background:#db2777;bottom:-80px;right:-60px;animation-delay:-4s}
.b3{width:200px;height:200px;background:#0ea5e9;top:40%;left:40%;animation-delay:-7s}
@keyframes blob-float{0%,100%{transform:translate(0,0) scale(1)}33%{transform:translate(20px,-30px) scale(1.05)}66%{transform:translate(-15px,20px) scale(.95)}}
.card{position:relative;z-index:10;background:rgba(20,18,40,.92);border:2px solid rgba(124,58,237,.4);border-radius:28px;padding:44px 38px 36px;width:100%;max-width:420px;backdrop-filter:blur(24px);box-shadow:0 0 0 1px rgba(124,58,237,.1),0 40px 80px rgba(0,0,0,.7),inset 0 1px 0 rgba(255,255,255,.05)}
.card::before,.card::after{content:'';position:absolute;width:24px;height:24px;border:3px solid rgba(124,58,237,.5);border-radius:6px}
.card::before{top:-3px;left:-3px;border-right:none;border-bottom:none}
.card::after{bottom:-3px;right:-3px;border-left:none;border-top:none}
.head{text-align:center;margin-bottom:30px}
.rocket{font-size:3.6rem;display:block;animation:rocket-bounce 2s ease-in-out infinite;filter:drop-shadow(0 0 20px rgba(124,58,237,.7))}
@keyframes rocket-bounce{0%,100%{transform:translateY(0) rotate(-5deg)}50%{transform:translateY(-14px) rotate(5deg)}}
.title{font-family:'Fredoka One',sans-serif;font-size:2.2rem;letter-spacing:.04em;background:linear-gradient(135deg,#a78bfa,#f472b6,#38bdf8);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:5px}
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
    <span class="rocket">🎯</span>
    <div class="title">ICT Strategy Scanner</div>
    <div class="sub">MEXC Perpetual · USDT Pairs Only</div>
  </div>
  <div class="err" id="err"></div>
  <label class="lbl">Password</label>
  <input class="inp" type="password" id="pw" placeholder="Enter your password" autofocus/>
  <button class="btn" id="btn" onclick="login()">🔓 Enter Dashboard</button>
  <div class="badges">
    <span class="badge">📡 ICT Strategy</span>
    <span class="badge">📦 Order Blocks</span>
    <span class="badge">📊 HTF Bias</span>
    <span class="badge">⚡ Displacement</span>
    <span class="badge">🤖 Auto-Trade</span>
    <span class="badge">📝 Paper Trading</span>
  </div>
  <div class="dot-row"><div class="live-dot"></div><span class="live-txt">SCANNER LIVE · ALL USDT PERPS</span></div>
</div>
<script>
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
      if(d.ok){localStorage.setItem('crt_tok',d.token||'ok');btn.textContent='✅ Let\'s go!';setTimeout(()=>window.location.href='/dashboard',300);}
      else{err.textContent='❌ Wrong password, try again!';err.classList.add('show');btn.textContent='🔓 Enter Dashboard';btn.disabled=false;document.getElementById('pw').value='';document.getElementById('pw').focus();}
    }).catch(e=>{err.textContent='⚠️ Connection error. Try again.';err.classList.add('show');btn.textContent='🔓 Enter Dashboard';btn.disabled=false;});
}
document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')login();});
</script>
</body>
</html>"""

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>ICT Strategy Scanner 🎯</title>
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
.brand-name{font-family:'Fredoka One',sans-serif;font-size:1.18rem;letter-spacing:.04em;color:#c4b5fd;line-height:1.2}
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
.lv-lbl{font-family:'JetBrains Mono',monospace;font-size:.52rem;color:var(--dim);letter-spacing:.05em;margin-bottom:4px;text-transform:uppercase;font-weight:700}
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
.snum2{font-family:'Fredoka One',sans-serif;font-size:.95rem;white-space:nowrap;width:60px;text-align:right}
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
.info-green{background:rgba(16,185,129,.06);border:1.5px solid rgba(16,185,129,.2);color:rgba(16,185,129,.85)}
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
.ll-s{color:var(--green)}.ll-e{color:var(--red)}.ll-i{color:rgba(56,189,248,.7)}.ll-t{color:#f9a8d4}.ll-m{color:var(--yellow)}.ll-p{color:rgba(167,139,250,.9)}
.diag-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:8px;margin-bottom:14px}
.dg{background:var(--s2);border:1.5px solid var(--border);border-radius:12px;padding:10px 12px;transition:all .2s}
.dg:hover{border-color:var(--border2);transform:translateY(-2px)}
.dg-lbl{font-family:'JetBrains Mono',monospace;font-size:.52rem;color:var(--dim);letter-spacing:.06em;margin-bottom:5px;text-transform:uppercase;font-weight:700}
.dg-val{font-family:'Fredoka One',sans-serif;font-size:1.6rem;line-height:1}
.paper-stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:16px}
.pstat{background:var(--s2);border:1.5px solid var(--border);border-radius:14px;padding:14px}
.pstat-lbl{font-family:'JetBrains Mono',monospace;font-size:.52rem;color:var(--dim);text-transform:uppercase;letter-spacing:.07em;margin-bottom:5px;font-weight:700}
.pstat-val{font-family:'Fredoka One',sans-serif;font-size:1.7rem;line-height:1}
.toast{position:fixed;bottom:26px;left:50%;transform:translateX(-50%) translateY(100px);background:var(--s2);border:2px solid var(--border2);border-radius:16px;padding:12px 22px;font-family:'Nunito',sans-serif;font-size:.85rem;font-weight:800;box-shadow:0 18px 50px rgba(0,0,0,.6);opacity:0;transition:all .4s cubic-bezier(.34,1.56,.64,1);pointer-events:none;z-index:9999;white-space:nowrap}
.toast.show{transform:translateX(-50%) translateY(0);opacity:1}
.toast.bt{border-color:rgba(16,185,129,.4);color:var(--green)}.toast.st{border-color:rgba(239,68,68,.4);color:var(--red)}.toast.tt{border-color:rgba(249,115,22,.4);color:var(--orange)}.toast.pt{border-color:rgba(167,139,250,.4);color:#a78bfa}
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
    <div class="brand"><span class="brand-icon">🎯</span><div><div class="brand-name">ICT Strategy Scanner</div><div class="brand-sub">MEXC USDT PERP · HTF BIAS · OB ENTRIES · 15M/30M</div></div></div>
    <div class="scan-pill"><div class="sdot" id="sdot"></div><span class="stxt" id="stxt">SCANNING...</span></div>
    <div class="hdr-right">
      <span class="snum" id="snum">SCAN #0</span>
      <button class="tbtn on" id="tbtn" onclick="toggleScanner()">⏹ Stop</button>
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
    <div class="sc s4"><div class="sc-lbl">⭐ A+ Signals</div><div class="sc-val" style="color:var(--yellow)" id="smon">0</div><div class="sc-sub">score 18+/20</div></div>
  </div>
</div>
<div class="tab-wrap">
  <div class="tabs">
    <button class="tab active" onclick="sw('signals',this)">📊 Signals</button>
    <button class="tab" onclick="sw('trades',this)">💹 Live Trades</button>
    <button class="tab" onclick="sw('history',this)">📜 History</button>
    <button class="tab" onclick="sw('trade-cfg',this)">🤖 Auto-Trade</button>
    <button class="tab" onclick="sw('paper',this)">📝 Paper Trade</button>
    <button class="tab" onclick="sw('log',this)">🖥️ Log</button>
    <button class="tab" onclick="sw('settings',this)">⚙️ Settings</button>
  </div>
  <!-- SIGNALS -->
  <div id="tab-signals">
    <div class="frow">
      <div class="ftitle">🎯 ICT Model — OB Entries</div>
      <div class="fgrp">
        <select class="fsel" id="fd" onchange="renderSigs()"><option value="">All</option><option value="BUY">🟢 BUY</option><option value="SELL">🔴 SELL</option></select>
        <select class="fsel" id="fg" onchange="renderSigs()"><option value="">All Grades</option><option value="A+">⭐ A+</option><option value="A">A</option><option value="B">B</option></select>
        <select class="fsel" id="ftf" onchange="renderSigs()"><option value="">All TFs</option><option value="Min15">15M</option><option value="Min30">30M</option></select>
      </div>
    </div>
    <div class="sig-list" id="slist"><div class="empty"><div class="empty-ico">🔭</div><div class="empty-t">Scanning the galaxy...</div><div class="empty-s">Hunting ICT setups: HTF Bias → Premium/Discount → Displacement → BOS → OB Entry. Min 15/20 score. Min 2R. 🎯</div></div></div>
  </div>
  <!-- LIVE TRADES -->
  <div id="tab-trades" style="display:none">
    <div class="panel">
      <div class="panel-ttl">💹 Running Trades <span id="trades-count" style="font-family:'JetBrains Mono',monospace;font-size:.72rem;color:var(--dim)">(0)</span><button class="action-btn tb-chk" style="margin-left:auto;border:none;padding:6px 14px" onclick="fetchPnl()">🔄 Refresh</button></div>
      <div id="live-trades-wrap"><div class="empty" style="padding:40px"><div class="empty-ico">😴</div><div class="empty-t">No open trades</div></div></div>
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
      <div class="info-box info-blue">ℹ️ <b>MEXC API keys</b> are configured in the <b>⚙️ Settings</b> tab. Risk model: Cross margin · auto-leverage 10x–500x · SL capped at 100% of margin.</div>
      <div class="trade-form">
        <div class="tf-group"><div class="tf-lbl">Risk per Trade (%)</div><input class="tf-inp" type="number" id="t-risk" value="1" min="0.1" max="5" step="0.1"/></div>
        <div class="tf-group"><div class="tf-lbl">Max Open Trades</div><input class="tf-inp" type="number" id="t-max" value="3" min="1" max="10" step="1"/></div>
        <div class="tf-group"><div class="tf-lbl">Leverage (max)</div><input class="tf-inp" type="number" id="t-lev" value="10" min="1" max="500" step="1"/></div>
        <div class="tf-group"><div class="tf-lbl">Account Balance</div><div class="bal-chip">💰 $<span id="bal-val">–</span> USDT</div></div>
      </div>
      <div class="trade-actions">
        <button class="trade-btn tb-save" onclick="saveTradeConfig()">💾 Save</button>
        <button class="trade-btn tb-on" id="t-enable-btn" onclick="enableTrade(true)">▶ Enable</button>
        <button class="trade-btn tb-off" id="t-disable-btn" onclick="enableTrade(false)" style="display:none">⏹ Disable</button>
        <button class="trade-btn tb-chk" onclick="fetchBalance()">🔄 Balance</button>
      </div>
      <div class="t-status" id="trade-msg"></div>
      <div class="info-box info-red">⚠️ Real money risk. Start with 0.5–1% risk and monitor closely. Configure your MEXC API Key and Secret in ⚙️ Settings before enabling.</div>
    </div>
  </div>
  <!-- PAPER TRADING -->
  <div id="tab-paper" style="display:none">
    <div class="panel">
      <div class="panel-ttl">📝 Paper Trading Engine
        <span id="paper-badge" style="font-size:.7rem;padding:3px 10px;border-radius:8px;background:rgba(239,68,68,.1);border:1.5px solid rgba(239,68,68,.3);color:var(--red);font-family:'JetBrains Mono',monospace;font-weight:700">DISABLED</span>
        <span id="paper-auto-badge" style="display:none;font-size:.7rem;padding:3px 10px;border-radius:8px;background:rgba(167,139,250,.1);border:1.5px solid rgba(167,139,250,.35);color:#a78bfa;font-family:'JetBrains Mono',monospace;font-weight:700">AUTO ON</span>
      </div>
      <div class="info-box info-green">📝 Paper trading mirrors the live engine exactly — same entry, SL, TP, and risk % — but uses a virtual balance. Perfect for testing before going live.</div>
      <div class="trade-form">
        <div class="tf-group">
          <div class="tf-lbl">Virtual Balance (USDT)</div>
          <div style="display:flex;gap:8px">
            <input class="tf-inp" type="number" id="p-balance" placeholder="10000" min="100" step="100" style="flex:1"/>
            <button class="trade-btn tb-save" style="padding:9px 16px;white-space:nowrap" onclick="setPaperBalance()">Set</button>
          </div>
        </div>
        <div class="tf-group">
          <div class="tf-lbl">Risk per Trade (%)</div>
          <input class="tf-inp" type="number" id="p-risk" value="1" min="0.1" max="10" step="0.1"/>
        </div>
        <div class="tf-group">
          <div class="tf-lbl">Max Simultaneous Trades</div>
          <input class="tf-inp" type="number" id="p-max" value="4" min="1" max="10" step="1"/>
        </div>
        <div class="tf-group">
          <div class="tf-lbl">Current Balance</div>
          <div class="bal-chip" id="p-bal-chip">💰 $<span id="p-bal-val">10,000.00</span> USDT</div>
        </div>
      </div>
      <div class="trade-actions">
        <button class="trade-btn tb-save" onclick="savePaperConfig()">💾 Save Settings</button>
        <button class="trade-btn tb-on" id="p-enable-btn" onclick="enablePaper(true)">▶ Enable Paper</button>
        <button class="trade-btn tb-off" id="p-disable-btn" onclick="enablePaper(false)" style="display:none">⏹ Disable Paper</button>
        <button class="trade-btn" id="p-auto-btn" onclick="togglePaperAuto()" style="background:rgba(167,139,250,.1);border:2px solid rgba(167,139,250,.3);color:#a78bfa">🤖 Auto-Trade: OFF</button>
        <button class="trade-btn tb-chk" onclick="resetPaperStats()">🔄 Reset Stats</button>
      </div>
      <div class="t-status" id="paper-msg"></div>
    </div>
    <div class="panel">
      <div class="panel-ttl">📊 Paper Performance</div>
      <div class="paper-stats">
        <div class="pstat"><div class="pstat-lbl">Total Trades</div><div class="pstat-val" id="ps-total" style="color:#a78bfa">0</div></div>
        <div class="pstat"><div class="pstat-lbl">Wins</div><div class="pstat-val" id="ps-wins" style="color:var(--green)">0</div></div>
        <div class="pstat"><div class="pstat-lbl">Losses</div><div class="pstat-val" id="ps-losses" style="color:var(--red)">0</div></div>
        <div class="pstat"><div class="pstat-lbl">Win Rate</div><div class="pstat-val" id="ps-wr" style="color:var(--yellow)">0%</div></div>
        <div class="pstat"><div class="pstat-lbl">Total PnL</div><div class="pstat-val" id="ps-pnl" style="color:var(--green)">$0</div></div>
        <div class="pstat"><div class="pstat-lbl">Open Trades</div><div class="pstat-val" id="ps-open" style="color:var(--cyan)">0</div></div>
      </div>
    </div>
    <div class="panel">
      <div class="panel-ttl">📂 Open Paper Positions <span id="paper-trades-count" style="font-family:'JetBrains Mono',monospace;font-size:.72rem;color:var(--dim)">(0)</span></div>
      <div id="paper-trades-wrap"><div class="empty" style="padding:40px"><div class="empty-ico">📝</div><div class="empty-t">No open paper trades</div><div class="empty-s">Enable paper trading and turn on auto-trade to place trades from signals automatically</div></div></div>
    </div>
    <div class="panel">
      <div class="panel-ttl">📜 Paper Trade History</div>
      <div id="paper-history-wrap"><div class="empty" style="padding:30px"><div class="empty-ico">📭</div><div class="empty-t">No paper trades yet</div></div></div>
    </div>
  </div>
  <!-- LOG -->
  <div id="tab-log" style="display:none">
    <div style="background:var(--s1);border:2px solid var(--border);border-radius:18px;padding:18px 20px;margin-bottom:14px">
      <div style="font-family:'Fredoka One',sans-serif;font-size:.9rem;letter-spacing:.05em;color:#a78bfa;margin-bottom:14px">🔬 Gate Diagnostics</div>
      <div class="diag-grid" id="diag-grid"></div>
    </div>
    <div class="log-wrap">
      <div class="log-hdr">
        <span class="log-ttl">🖥️ Live Log</span>
        <div style="display:flex;align-items:center;gap:10px">
          <span class="log-sub">UPDATES EVERY 3S</span>
          <button class="action-btn tb-chk" onclick="fetchLog()" style="border:none;padding:4px 10px">🔄 Refresh</button>
        </div>
      </div>
      <div class="log-body" id="lbody"><div style="color:rgba(56,189,248,.5);font-style:italic">Waiting for log entries... Scanner logs appear here in real-time.</div></div>
    </div>
  </div>
  <!-- SETTINGS -->
  <div id="tab-settings" style="display:none">
    <!-- Telegram -->
    <div class="panel">
      <div class="panel-ttl">📡 Telegram Bot</div>
      <div class="info-box info-blue">ℹ️ Get your Bot Token from <b>@BotFather</b> on Telegram. Get your Chat ID by messaging <b>@userinfobot</b>.</div>
      <div class="trade-form">
        <div class="tf-group"><div class="tf-lbl">Bot Token</div><input class="tf-inp" type="password" id="s-tg-token" placeholder="1234567890:AABBcc..."/></div>
        <div class="tf-group"><div class="tf-lbl">Chat ID</div><input class="tf-inp" type="text" id="s-tg-chat" placeholder="-100123456789 or 123456789"/></div>
      </div>
      <div class="trade-actions">
        <button class="trade-btn tb-save" onclick="saveSettingsSection('telegram')">💾 Save Telegram</button>
        <button class="trade-btn tb-chk" onclick="testTelegram()">📨 Send Test</button>
      </div>
      <div class="t-status" id="tg-msg"></div>
    </div>
    <!-- MEXC API -->
    <div class="panel">
      <div class="panel-ttl">🔑 MEXC API Keys</div>
      <div class="info-box info-blue">ℹ️ Create API keys at <b>MEXC → Account → API Management</b>. Enable <b>Futures Trading</b> permission only. Never enable withdrawals.</div>
      <div class="trade-form">
        <div class="tf-group"><div class="tf-lbl">API Key</div><input class="tf-inp" type="text" id="s-mexc-key" placeholder="Your MEXC API key"/></div>
        <div class="tf-group"><div class="tf-lbl">Secret Key</div><input class="tf-inp" type="password" id="s-mexc-secret" placeholder="Your MEXC secret key"/></div>
      </div>
      <div class="trade-actions">
        <button class="trade-btn tb-save" onclick="saveSettingsSection('mexc')">💾 Save MEXC Keys</button>
        <button class="trade-btn tb-chk" onclick="fetchBalance()">🔄 Test Connection</button>
      </div>
      <div class="t-status" id="mexc-msg"></div>
    </div>
    <!-- Dashboard -->
    <div class="panel">
      <div class="panel-ttl">🔒 Dashboard Security</div>
      <div class="trade-form">
        <div class="tf-group"><div class="tf-lbl">New Password</div><input class="tf-inp" type="password" id="s-pw-new" placeholder="Enter new password"/></div>
        <div class="tf-group"><div class="tf-lbl">Confirm Password</div><input class="tf-inp" type="password" id="s-pw-confirm" placeholder="Confirm new password"/></div>
      </div>
      <div class="trade-actions">
        <button class="trade-btn tb-save" onclick="saveSettingsSection('password')">🔒 Change Password</button>
      </div>
      <div class="t-status" id="pw-msg"></div>
    </div>
    <!-- Scanner -->
    <div class="panel">
      <div class="panel-ttl">⚡ Scanner Settings</div>
      <div class="trade-form">
        <div class="tf-group"><div class="tf-lbl">Min AI Score (out of 20)</div><input class="tf-inp" type="number" id="s-min-score" value="15" min="10" max="20" step="1"/></div>
        <div class="tf-group"><div class="tf-lbl">Min Risk:Reward</div><input class="tf-inp" type="number" id="s-min-rr" value="2.0" min="1.0" max="10.0" step="0.5"/></div>
        <div class="tf-group"><div class="tf-lbl">Delay Between Pairs (secs)</div><input class="tf-inp" type="number" id="s-scan-delay" value="5" min="1" max="30" step="1"/></div>
      </div>
      <div class="trade-actions">
        <button class="trade-btn tb-save" onclick="saveSettingsSection('scanner')">💾 Save Scanner</button>
      </div>
      <div class="t-status" id="scan-msg"></div>
    </div>
  </div>
</div>
<div class="share-modal" id="share-modal">
  <div class="share-card">
    <div class="sh-title">📸 Signal Card</div>
    <div id="sh-content"></div>
    <button class="sh-close" onclick="document.getElementById('share-modal').classList.remove('show')">✕ Close</button>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
(function(){
'use strict';
let allSigs=[],toastT,activeTab='signals',tick=0,lastCount=0,paperAutoOn=false;
const $=id=>document.getElementById(id);
function toast(m,t,d=3500){const el=$("toast");el.textContent=m;el.className="toast show"+(t==="buy"?" bt":t==="sell"?" st":t==="trade"?" tt":t==="paper"?" pt":"");clearTimeout(toastT);toastT=setTimeout(()=>el.classList.remove("show"),d);}
function scoreColor(s){return s>=18?"#fbbf24":s>=16?"#a78bfa":s>=15?"var(--blue)":"var(--dim)";}
function fmt(v){if(v===null||v===undefined||v==="–"||v===false||v==="false")return"–";const n=Number(v);if(isNaN(n))return String(v);if(n>=10000)return n.toLocaleString(undefined,{maximumFractionDigits:2});if(n>=1)return n.toFixed(4);return n.toFixed(6);}
function fmtP(v){const n=Number(v);if(!n)return"–";if(n>=10000)return"$"+n.toLocaleString(undefined,{maximumFractionDigits:2});if(n>=1)return"$"+n.toFixed(4);return"$"+n.toFixed(6);}
const TFM={"Min15":"15M","Min30":"30M","Min60":"1H","Hour1":"1H","Hour2":"2H","Hour4":"4H","Day1":"1D"};
const TOP=["BTC_USDT","ETH_USDT","SOL_USDT","BNB_USDT","XRP_USDT","DOGE_USDT"];
function sw(tab,el){
  activeTab=tab;
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
  document.querySelectorAll('[id^="tab-"]').forEach(t=>t.style.display='none');
  $('tab-'+tab).style.display='';
  if(tab==='trades')fetchPnl();
  if(tab==='history')fetchHistory();
  if(tab==='log')fetchLog();
  if(tab==='paper')fetchPaperData();
  if(tab==='settings')fetchSettings();
}
function logout(){localStorage.removeItem('crt_tok');window.location.href='/';}
function getToken(){return localStorage.getItem('crt_tok')||'';}
function authHeaders(){return{'Content-Type':'application/json','X-Token':getToken()};}

async function fetchStatus(){
  try{
    const r=await fetch('/api/status',{headers:authHeaders()});
    if(r.status===401){window.location.href='/';return;}
    const d=await r.json();
    const running=d.running&&d.enabled;
    $('sdot').className='sdot'+(running?'':' off');
    $('stxt').textContent=running?'SCANNING...':'PAUSED';
    $('stxt').className='stxt'+(running?'':' off');
    $('tbtn').textContent=running?'⏹ Stop':'▶ Resume';
    $('tbtn').className='tbtn '+(running?'on':'off');
    $('pb').className='pb'+(d.enabled?'':' show');
    $('snum').textContent='SCAN #'+d.scan_count;
    $('cpair').textContent='🔍 '+(d.current_pair||'Waiting...');
    const pct=d.total_pairs>0?Math.round(d.pairs_done/d.total_pairs*100):0;
    $('pfill').style.width=pct+'%';
    $('pcnt').textContent=d.pairs_done+'/'+d.total_pairs;
    if(d.signals){
      const sigs=d.signals;
      $('st').textContent=sigs.length;
      $('sb').textContent=sigs.filter(s=>s.direction==='BUY').length;
      $('ss').textContent=sigs.filter(s=>s.direction==='SELL').length;
      $('smon').textContent=sigs.filter(s=>s.grade==='A+').length;
      if(sigs.length!==lastCount){lastCount=sigs.length;allSigs=sigs;renderSigs();}
    }
    $('sc2').textContent=d.scan_count;
    $('sl2').textContent=d.last_scan||'–';
  }catch(e){}
}

function renderSigs(){
  const fd=$('fd').value,fg=$('fg').value,ftf=$('ftf').value;
  let sigs=allSigs.filter(s=>{
    if(fd&&s.direction!==fd)return false;
    if(fg&&s.grade!==fg)return false;
    if(ftf&&s.tf!==ftf)return false;
    return true;
  });
  const el=$('slist');
  if(!sigs.length){el.innerHTML='<div class="empty"><div class="empty-ico">🔭</div><div class="empty-t">No signals yet</div><div class="empty-s">Scanner is running. ICT setups require HTF alignment + OB entry + min 15/20 score.</div></div>';return;}
  el.innerHTML=sigs.map((s,i)=>{
    const isBuy=s.direction==='BUY';
    const sc=s.score||0; const mx=20;
    const pct=Math.round(sc/mx*100);
    const gc=s.grade==='A+'?'gAp':s.grade==='A'?'gA':s.grade==='B'?'gB':s.grade==='C'?'gC':'gD';
    const tf=TFM[s.tf]||s.tf||'–';
    const obTf=TFM[s.ob_tf]||s.ob_tf||'–';
    const bias4h=s.bias_4h||'–'; const bias1h=s.bias_1h||'–';
    const disps=s.disp_strength?Math.round(s.disp_strength*100)+'%':'–';
    const cfms=[
      s.liq_swept?'<span class="cf cf-ok">Liq Sweep ✅</span>':'<span class="cf cf-no">No Sweep ⚠️</span>',
      s.bos_found?'<span class="cf cf-ok">BOS ✅</span>':'<span class="cf cf-no">No BOS ⚠️</span>',
      s.fvg_found?'<span class="cf cf-ok">FVG ✅</span>':'<span class="cf cf-no">No FVG</span>',
      s.ob_respected?'<span class="cf cf-g">OBs Respected ✅</span>':'',
      s.is_structured?'<span class="cf cf-ok">Clear Structure ✅</span>':'',
    ].filter(Boolean).join('');
    const tp2row=s.tp2&&s.tp2!=='–'?`<div class="lv lv-t"><div class="lv-lbl">TP2</div><div class="lv-val">${fmt(s.tp2)}</div></div>`:'';
    return `<div class="scard ${isBuy?'buy':'sell'}" id="sig-${i}">
      <div class="card-hdr">
        <span class="dtag ${s.direction}">${s.direction}</span>
        <span class="csym">${s.symbol}</span>
        <div class="chips">
          <span class="chip chip-tf">${tf}</span>
          <span class="chip chip-ob">${s.ob_zone||'–'}</span>
          <span class="chip chip-tr ${s.trend||'NEUTRAL'}">${s.trend||'NEUTRAL'}</span>
          ${s.grade==='A+'?'<span class="chip chip-aplus">⭐ A+</span>':''}
        </div>
        <span class="gtag ${gc}">${s.grade}</span>
        <span class="cts">${s.timestamp||''}</span>
      </div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:.6rem;color:var(--dim);margin-bottom:10px">
        4H: <b style="color:${bias4h==='BULLISH'?'var(--green)':'var(--red)'}">${bias4h}</b> &nbsp;|&nbsp;
        1H: <b style="color:${bias1h==='BULLISH'?'var(--green)':'var(--red)'}">${bias1h}</b> &nbsp;|&nbsp;
        Displacement: <b style="color:var(--yellow)">${disps}</b>
      </div>
      <div class="lvl-grid">
        <div class="lv lv-e"><div class="lv-lbl">Entry (OB)</div><div class="lv-val">${fmt(s.entry)}</div></div>
        <div class="lv lv-s"><div class="lv-lbl">Stop Loss</div><div class="lv-val">${fmt(s.sl)}</div></div>
        <div class="lv lv-t"><div class="lv-lbl">TP1</div><div class="lv-val">${fmt(s.tp)}</div></div>
        ${tp2row}
        <div class="lv lv-r"><div class="lv-lbl">RR</div><div class="lv-val">${s.rr}R</div></div>
        <div class="lv lv-o"><div class="lv-lbl">OB Top</div><div class="lv-val">${fmt(s.ob_top)}</div></div>
        <div class="lv lv-s"><div class="lv-lbl">OB Bot</div><div class="lv-val">${fmt(s.ob_bot)}</div></div>
      </div>
      <div class="cfms">${cfms}</div>
      <div class="srow">
        <span class="slbl">Score</span>
        <div class="strack"><div class="sfill" style="width:${pct}%;background:${scoreColor(sc)}"></div></div>
        <span class="snum2" style="color:${scoreColor(sc)}">${sc}/20</span>
      </div>
      <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
        <button class="dettog" onclick="this.nextElementSibling.classList.toggle('open');this.textContent=this.nextElementSibling.classList.contains('open')?'▲ Hide Details':'▼ Show Details'">▼ Show Details</button>
        <button class="action-btn share-btn" onclick="shareCard(${i})">📸 Share</button>
        ${trade_config_enabled?`<button class="action-btn tb-on" onclick="manualTrade(${i})" style="border:none">🤖 Trade</button>`:''}
      </div>
      <div class="detbox">${(s.details||[]).join('<br>')}</div>
    </div>`;
  }).join('');
}
let trade_config_enabled=false;

async function fetchPnl(){
  try{
    const r=await fetch('/api/trades',{headers:authHeaders()});
    const d=await r.json();
    trade_config_enabled=d.trade_enabled||false;
    const wrap=$('live-trades-wrap');
    $('trades-count').textContent='('+Object.keys(d.trades||{}).length+')';
    if(!d.trades||!Object.keys(d.trades).length){
      wrap.innerHTML='<div class="empty" style="padding:40px"><div class="empty-ico">😴</div><div class="empty-t">No open trades</div></div>';return;}
    wrap.innerHTML='<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Pair</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th><th>RR</th><th>Score</th><th>Opened</th><th>Action</th></tr></thead><tbody>'+
      Object.values(d.trades).map(t=>`<tr><td><b>${t.symbol}</b></td><td class="${t.direction.toLowerCase()}">${t.direction}</td><td>${fmt(t.entry)}</td><td style="color:var(--red)">${fmt(t.sl)}</td><td style="color:var(--green)">${fmt(t.tp)}</td><td>${t.rr}R</td><td style="color:${scoreColor(t.score)}">${t.score}/20 ${t.grade}</td><td style="color:var(--dim)">${t.opened_at}</td><td><button class="action-btn close-btn" onclick="closeTrade('${t.symbol}')">✕ Close</button></td></tr>`).join('')+
      '</tbody></table></div>';
  }catch(e){}
}

async function fetchHistory(){
  try{
    const r=await fetch('/api/history',{headers:authHeaders()});
    const d=await r.json();
    const wrap=$('history-wrap');
    if(!d.trades||!d.trades.length){wrap.innerHTML='<div class="empty" style="padding:40px"><div class="empty-ico">📭</div><div class="empty-t">No completed trades yet</div></div>';return;}
    wrap.innerHTML='<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Pair</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th><th>Score</th><th>Opened</th><th>Status</th></tr></thead><tbody>'+
      d.trades.map(t=>`<tr><td><b>${t.symbol}</b></td><td class="${t.direction.toLowerCase()}">${t.direction}</td><td>${fmt(t.entry)}</td><td style="color:var(--red)">${fmt(t.sl)}</td><td style="color:var(--green)">${fmt(t.tp)}</td><td style="color:${scoreColor(t.score)}">${t.score}/20</td><td style="color:var(--dim)">${t.opened_at||'–'}</td><td style="color:var(--dim)">${t.status||'–'}</td></tr>`).join('')+
      '</tbody></table></div>';
  }catch(e){}
}

function shareCard(i){
  const s=allSigs[i]; if(!s)return;
  const bias4h=s.bias_4h||'–'; const bias1h=s.bias_1h||'–';
  $('sh-content').innerHTML=`
    <div class="sh-row"><span class="sh-lbl">Pair</span><span class="sh-val">${s.symbol}</span></div>
    <div class="sh-row"><span class="sh-lbl">Direction</span><span class="sh-val" style="color:${s.direction==='BUY'?'var(--green)':'var(--red)'}">${s.direction}</span></div>
    <div class="sh-row"><span class="sh-lbl">4H Bias</span><span class="sh-val" style="color:${bias4h==='BULLISH'?'var(--green)':'var(--red)'}">${bias4h}</span></div>
    <div class="sh-row"><span class="sh-lbl">1H Bias</span><span class="sh-val" style="color:${bias1h==='BULLISH'?'var(--green)':'var(--red)'}">${bias1h}</span></div>
    <div class="sh-row"><span class="sh-lbl">Entry (OB)</span><span class="sh-val">${fmt(s.entry)}</span></div>
    <div class="sh-row"><span class="sh-lbl">Stop Loss</span><span class="sh-val" style="color:var(--red)">${fmt(s.sl)}</span></div>
    <div class="sh-row"><span class="sh-lbl">TP1</span><span class="sh-val" style="color:var(--green)">${fmt(s.tp)}</span></div>
    <div class="sh-row"><span class="sh-lbl">TP2</span><span class="sh-val" style="color:var(--green)">${fmt(s.tp2)}</span></div>
    <div class="sh-row"><span class="sh-lbl">RR</span><span class="sh-val">${s.rr}R</span></div>
    <div class="sh-row"><span class="sh-lbl">AI Score</span><span class="sh-val" style="color:${scoreColor(s.score)}">${s.score}/20 ${s.grade}</span></div>
    <div class="sh-row"><span class="sh-lbl">Zone</span><span class="sh-val">${s.pd_zone||'–'}</span></div>
  `;
  $('share-modal').classList.add('show');
}

async function manualTrade(i){
  const s=allSigs[i];if(!s)return;
  if(!confirm('Place trade on '+s.symbol+'?'))return;
  try{
    const r=await fetch('/api/trade/manual',{method:'POST',headers:authHeaders(),body:JSON.stringify(s)});
    const d=await r.json();
    toast(d.ok?'✅ Trade placed!':'❌ '+d.error,'trade');
    fetchPnl();
  }catch(e){toast('❌ Error','sell');}
}

async function closeTrade(sym){
  if(!confirm('Close trade on '+sym+'?'))return;
  try{
    const r=await fetch('/api/trade/close',{method:'POST',headers:authHeaders(),body:JSON.stringify({symbol:sym})});
    const d=await r.json();
    toast(d.ok?'✅ Closed!':'❌ '+d.error,'trade');
    fetchPnl();
  }catch(e){toast('❌ Error','sell');}
}

async function toggleScanner(){
  try{
    const r=await fetch('/api/scanner/toggle',{method:'POST',headers:authHeaders()});
    const d=await r.json();
    toast(d.enabled?'▶ Scanner resumed!':'⏹ Scanner stopped!');
    fetchStatus();
  }catch(e){}
}

async function saveTradeConfig(){
  const body={risk_pct:parseFloat($('t-risk').value)||1,max_trades:parseInt($('t-max').value)||3,leverage:parseInt($('t-lev').value)||10};
  try{
    const r=await fetch('/api/trade/config',{method:'POST',headers:authHeaders(),body:JSON.stringify(body)});
    const d=await r.json();
    const m=$('trade-msg');m.textContent=d.ok?'✅ Saved!':'❌ '+d.error;m.className='t-status '+(d.ok?'ok':'err');
    toast(d.ok?'✅ Saved!':'❌ Error');
  }catch(e){}
}

async function fetchSettings(){
  try{
    const r=await fetch('/api/settings',{headers:authHeaders()});
    const d=await r.json();
    if(d.tg_chat)$('s-tg-chat').value=d.tg_chat;
    if(d.mexc_key)$('s-mexc-key').value=d.mexc_key;
    $('s-min-score').value=d.min_score||15;
    $('s-min-rr').value=d.min_rr||2.0;
    $('s-scan-delay').value=d.scan_delay||5;
    // Don't pre-fill secrets (token, secret key, password) for security
  }catch(e){}
}

async function saveSettingsSection(section){
  let body={};
  let msgEl=null;
  if(section==='telegram'){
    body={tg_token:$('s-tg-token').value.trim(),tg_chat_id:$('s-tg-chat').value.trim()};
    if(!body.tg_token&&!body.tg_chat_id){toast('⚠️ Fill at least one field');return;}
    msgEl=$('tg-msg');
  }else if(section==='mexc'){
    body={mexc_key:$('s-mexc-key').value.trim(),mexc_secret:$('s-mexc-secret').value.trim()};
    if(!body.mexc_key&&!body.mexc_secret){toast('⚠️ Fill at least one field');return;}
    msgEl=$('mexc-msg');
  }else if(section==='password'){
    const n=$('s-pw-new').value;const c=$('s-pw-confirm').value;
    if(!n){toast('⚠️ Enter a new password');return;}
    if(n!==c){$('pw-msg').textContent='❌ Passwords do not match';$('pw-msg').className='t-status err';return;}
    body={password:n};
    msgEl=$('pw-msg');
  }else if(section==='scanner'){
    body={min_score:parseInt($('s-min-score').value)||15,min_rr:parseFloat($('s-min-rr').value)||2.0,scan_delay:parseInt($('s-scan-delay').value)||5};
    msgEl=$('scan-msg');
  }
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:authHeaders(),body:JSON.stringify(body)});
    const d=await r.json();
    if(msgEl){msgEl.textContent=d.ok?'✅ Saved successfully!':'❌ '+d.error;msgEl.className='t-status '+(d.ok?'ok':'err');}
    toast(d.ok?'✅ Settings saved!':'❌ Error');
    if(section==='password'&&d.ok){$('s-pw-new').value='';$('s-pw-confirm').value='';}
  }catch(e){toast('❌ Error saving','sell');}
}

async function testTelegram(){
  const m=$('tg-msg');m.textContent='📨 Sending test...';m.className='t-status ok';
  try{
    const r=await fetch('/api/telegram/test',{method:'POST',headers:authHeaders()});
    const d=await r.json();
    m.textContent=d.ok?'✅ Test message sent! Check Telegram.':'❌ Failed: '+d.error;
    m.className='t-status '+(d.ok?'ok':'err');
    toast(d.ok?'📨 Test sent!':'❌ Telegram error');
  }catch(e){m.textContent='❌ Connection error';m.className='t-status err';}
}

async function enableTrade(en){
  try{
    const r=await fetch('/api/trade/enable',{method:'POST',headers:authHeaders(),body:JSON.stringify({enabled:en})});
    const d=await r.json();
    $('trade-badge').textContent=en?'ENABLED':'DISABLED';
    $('trade-badge').style.background=en?'rgba(16,185,129,.1)':'rgba(239,68,68,.1)';
    $('trade-badge').style.color=en?'var(--green)':'var(--red)';
    $('trade-badge').style.borderColor=en?'rgba(16,185,129,.3)':'rgba(239,68,68,.3)';
    $('t-enable-btn').style.display=en?'none':'';
    $('t-disable-btn').style.display=en?'':'none';
    toast(en?'🤖 Auto-trade ON':'⏹ Auto-trade OFF','trade');
  }catch(e){}
}

async function fetchBalance(){
  try{
    const r=await fetch('/api/balance',{headers:authHeaders()});
    const d=await r.json();
    $('bal-val').textContent=d.balance?parseFloat(d.balance).toFixed(2):'Error';
    toast('💰 Balance: $'+parseFloat(d.balance||0).toFixed(2),'trade');
  }catch(e){}
}

async function savePaperConfig(){
  const body={risk_pct:parseFloat($('p-risk').value)||1,max_trades:parseInt($('p-max').value)||4};
  try{
    const r=await fetch('/api/paper/config',{method:'POST',headers:authHeaders(),body:JSON.stringify(body)});
    const d=await r.json();
    const m=$('paper-msg');m.textContent=d.ok?'✅ Saved!':'❌ '+d.error;m.className='t-status '+(d.ok?'ok':'err');
    toast(d.ok?'✅ Saved!':'❌ Error');
  }catch(e){}
}

async function setPaperBalance(){
  const bal=parseFloat($('p-balance').value);
  if(!bal||bal<100){toast('⚠️ Min balance $100','sell');return;}
  try{
    const r=await fetch('/api/paper/balance',{method:'POST',headers:authHeaders(),body:JSON.stringify({balance:bal})});
    const d=await r.json();
    if(d.ok){$('p-bal-val').textContent=bal.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});toast('💰 Balance set!','paper');}
  }catch(e){}
}

async function enablePaper(en){
  try{
    const r=await fetch('/api/paper/enable',{method:'POST',headers:authHeaders(),body:JSON.stringify({enabled:en})});
    const d=await r.json();
    $('paper-badge').textContent=en?'ENABLED':'DISABLED';
    $('paper-badge').style.background=en?'rgba(16,185,129,.1)':'rgba(239,68,68,.1)';
    $('paper-badge').style.color=en?'var(--green)':'var(--red)';
    $('paper-badge').style.borderColor=en?'rgba(16,185,129,.3)':'rgba(239,68,68,.3)';
    $('p-enable-btn').style.display=en?'none':'';
    $('p-disable-btn').style.display=en?'':'none';
    toast(en?'📝 Paper ON':'⏹ Paper OFF','paper');
  }catch(e){}
}

async function togglePaperAuto(){
  paperAutoOn=!paperAutoOn;
  try{
    const r=await fetch('/api/paper/auto',{method:'POST',headers:authHeaders(),body:JSON.stringify({auto:paperAutoOn})});
    const d=await r.json();
    $('p-auto-btn').textContent='🤖 Auto-Trade: '+(paperAutoOn?'ON':'OFF');
    $('paper-auto-badge').style.display=paperAutoOn?'':'none';
    toast(paperAutoOn?'🤖 Paper auto ON':'⏹ Paper auto OFF','paper');
  }catch(e){}
}

async function resetPaperStats(){
  if(!confirm('Reset all paper stats?'))return;
  try{
    const r=await fetch('/api/paper/reset',{method:'POST',headers:authHeaders()});
    const d=await r.json();
    toast(d.ok?'🔄 Stats reset!':'❌ Error','paper');
    fetchPaperData();
  }catch(e){}
}

async function fetchPaperData(){
  try{
    const r=await fetch('/api/paper/data',{headers:authHeaders()});
    const d=await r.json();
    paperAutoOn=d.auto_trade||false;
    $('p-auto-btn').textContent='🤖 Auto-Trade: '+(paperAutoOn?'ON':'OFF');
    $('paper-auto-badge').style.display=paperAutoOn?'':'none';
    $('p-bal-val').textContent=parseFloat(d.balance||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
    $('paper-badge').textContent=d.enabled?'ENABLED':'DISABLED';
    $('paper-badge').style.background=d.enabled?'rgba(16,185,129,.1)':'rgba(239,68,68,.1)';
    $('paper-badge').style.color=d.enabled?'var(--green)':'var(--red)';
    $('paper-badge').style.borderColor=d.enabled?'rgba(16,185,129,.3)':'rgba(239,68,68,.3)';
    $('p-enable-btn').style.display=d.enabled?'none':'';
    $('p-disable-btn').style.display=d.enabled?'':'none';
    const st=d.stats||{};
    $('ps-total').textContent=st.total||0;
    $('ps-wins').textContent=st.wins||0;
    $('ps-losses').textContent=st.losses||0;
    const wr=st.total>0?Math.round(st.wins/st.total*100):0;
    $('ps-wr').textContent=wr+'%';
    const pnl=st.total_pnl||0;
    $('ps-pnl').textContent=(pnl>=0?'+':'')+'$'+Math.abs(pnl).toFixed(2);
    $('ps-pnl').style.color=pnl>=0?'var(--green)':'var(--red)';
    $('ps-open').textContent=Object.keys(d.open_trades||{}).length;
    $('paper-trades-count').textContent='('+Object.keys(d.open_trades||{}).length+')';
    const ptw=$('paper-trades-wrap');
    if(!d.open_trades||!Object.keys(d.open_trades).length){
      ptw.innerHTML='<div class="empty" style="padding:40px"><div class="empty-ico">📝</div><div class="empty-t">No open paper trades</div></div>';
    }else{
      ptw.innerHTML='<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Pair</th><th>Side</th><th>Entry</th><th>Price</th><th>PnL</th><th>RR</th><th>Score</th><th>Action</th></tr></thead><tbody>'+
        Object.values(d.open_trades).map(t=>{
          const pnlC=t.pnl>=0?'var(--green)':'var(--red)';
          return `<tr><td><b>${t.symbol}</b></td><td class="${t.direction.toLowerCase()}">${t.direction}</td><td>${fmt(t.entry)}</td><td>${fmt(t.current_price)}</td><td class="pos-pnl ${t.pnl>=0?'pos':'neg'}">${t.pnl>=0?'+':''}$${Math.abs(t.pnl).toFixed(2)}</td><td>${t.rr}R</td><td style="color:${scoreColor(t.score)}">${t.score}/20</td><td><button class="action-btn close-btn" onclick="closePaper('${t.symbol}')">✕ Close</button></td></tr>`;
        }).join('')+'</tbody></table></div>';
    }
    const phw=$('paper-history-wrap');
    if(!d.history||!d.history.length){phw.innerHTML='<div class="empty" style="padding:30px"><div class="empty-ico">📭</div><div class="empty-t">No paper trades yet</div></div>';}
    else{
      phw.innerHTML='<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Pair</th><th>Side</th><th>Entry</th><th>Close</th><th>PnL</th><th>Score</th><th>Status</th></tr></thead><tbody>'+
        d.history.map(t=>`<tr><td><b>${t.symbol}</b></td><td class="${t.direction.toLowerCase()}">${t.direction}</td><td>${fmt(t.entry)}</td><td>${fmt(t.close_price||'–')}</td><td class="pos-pnl ${(t.pnl||0)>=0?'pos':'neg'}">${(t.pnl||0)>=0?'+':''}$${Math.abs(t.pnl||0).toFixed(2)}</td><td style="color:${scoreColor(t.score)}">${t.score}/20</td><td style="color:var(--dim)">${t.status||'–'}</td></tr>`).join('')+
        '</tbody></table></div>';
    }
  }catch(e){}
}

async function closePaper(sym){
  if(!confirm('Close paper trade on '+sym+'?'))return;
  try{
    const r=await fetch('/api/paper/close',{method:'POST',headers:authHeaders(),body:JSON.stringify({symbol:sym})});
    const d=await r.json();
    toast(d.ok?'✅ Paper closed! '+d.msg:'❌ '+d.error,'paper');
    fetchPaperData();
  }catch(e){}
}

async function fetchLog(){
  try{
    const r=await fetch('/api/log',{headers:authHeaders()});
    const d=await r.json();
    const lb=$('lbody');
    if(!d.log||!d.log.length){lb.innerHTML='<div style="color:rgba(56,189,248,.5);font-style:italic">No log entries yet.</div>';return;}
    lb.innerHTML=d.log.map(l=>{
      const cls=l.includes('🎯')||l.includes('✅')?'ll-s':l.includes('❌')||l.includes('error')||l.includes('Error')?'ll-e':l.includes('⚠️')?'ll-m':l.includes('📝')?'ll-p':l.includes('🔄')||l.includes('Scan')?'ll-i':'';
      return `<div class="${cls}">${l}</div>`;
    }).join('');
    lb.scrollTop=0;
    if(d.diag){
      $('diag-grid').innerHTML=Object.entries(d.diag).map(([k,v])=>`<div class="dg"><div class="dg-lbl">${k.replace(/_/g,' ')}</div><div class="dg-val" style="color:${k==='passed'?'var(--green)':v>0?'var(--orange)':'var(--dim)'}">${v}</div></div>`).join('');
    }
  }catch(e){}
}

async function fetchPrices(){
  try{
    const r=await fetch('/api/prices',{headers:authHeaders()});
    const d=await r.json();
    const grid=$('pgrid');
    if(!d.prices||!d.prices.length)return;
    grid.innerHTML=d.prices.map(p=>{
      const up=p.change>=0;
      return `<div class="pc ${up?'up':'dn'}"><div class="pc-sym">${p.symbol.replace('_USDT','')}</div><div class="pc-price ${up?'up':'dn'}">${fmtP(p.price)}</div><span class="pc-chg ${up?'up':'dn'}">${up?'+':''}${p.change.toFixed(2)}%</span></div>`;
    }).join('');
    $('pupd').textContent='UPDATED '+new Date().toLocaleTimeString();
  }catch(e){}
}

setInterval(fetchStatus,3000);
setInterval(()=>{if(activeTab==='paper')fetchPaperData();},8000);
setInterval(()=>{if(activeTab==='trades')fetchPnl();},10000);
setInterval(fetchPrices,15000);
fetchStatus();fetchPrices();
})();
</script>
</body>
</html>"""

# ════════ FLASK ROUTES ════════════════════════════════════════════════

def check_auth():
    token = request.headers.get("X-Token","")
    return token in sessions

@app.route("/")
def index():
    return make_response(LOGIN_HTML)

@app.route("/dashboard")
def dashboard():
    return make_response(DASHBOARD_HTML)

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    if data.get("password") == bot_config.get("password","signal123"):
        token = secrets.token_hex(16)
        sessions.add(token)
        return jsonify({"ok": True, "token": token})
    return jsonify({"ok": False})

@app.route("/api/status")
def api_status():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    with scan_lock:
        state = dict(scan_state)
        state["log"] = list(scan_state["log"])
    state["signals"] = list(signals)[:200]
    return jsonify(state)

@app.route("/api/log")
def api_log():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    with scan_lock:
        log_entries = list(scan_state["log"])
    return jsonify({"log": log_entries, "diag": dict(diag)})

@app.route("/api/prices")
def api_prices():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    prices = []
    for sym in TOP_PAIRS:
        t = get_ticker(sym)
        if t: prices.append({"symbol": sym, **t})
    return jsonify({"prices": prices})

@app.route("/api/scanner/toggle", methods=["POST"])
def api_scanner_toggle():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    with scan_lock:
        scan_state["enabled"] = not scan_state["enabled"]
        enabled = scan_state["enabled"]
    log(f"Scanner {'resumed' if enabled else 'paused'} via dashboard")
    return jsonify({"enabled": enabled})

@app.route("/api/trades")
def api_trades():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    with trade_lock:
        trades = dict(open_trades)
    return jsonify({"trades": trades, "trade_enabled": trade_config["enabled"]})

@app.route("/api/history")
def api_history():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    return jsonify({"trades": list(recent_trades)})

@app.route("/api/balance")
def api_balance():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    bal, err = get_account_balance()
    if err: return jsonify({"error": err})
    return jsonify({"balance": bal})

@app.route("/api/trade/config", methods=["POST"])
def api_trade_config():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    if "risk_pct"   in data: trade_config["risk_pct"]   = float(data["risk_pct"])
    if "max_trades" in data: trade_config["max_trades"] = int(data["max_trades"])
    if "leverage"   in data: trade_config["leverage"]   = int(data["leverage"])
    return jsonify({"ok": True})

@app.route("/api/trade/enable", methods=["POST"])
def api_trade_enable():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    trade_config["enabled"] = bool(data.get("enabled", False))
    return jsonify({"ok": True, "enabled": trade_config["enabled"]})

@app.route("/api/trade/manual", methods=["POST"])
def api_trade_manual():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    sig = request.get_json(silent=True) or {}
    ok, msg = place_order(sig)
    return jsonify({"ok": ok, "msg": msg, "error": "" if ok else msg})

@app.route("/api/trade/close", methods=["POST"])
def api_trade_close():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    ok, msg = close_trade(data.get("symbol",""))
    return jsonify({"ok": ok, "msg": msg, "error": "" if ok else msg})

@app.route("/api/paper/config", methods=["POST"])
def api_paper_config():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    if "risk_pct" in data:   paper_config["risk_pct"]   = float(data["risk_pct"])
    if "max_trades" in data: paper_config["max_trades"] = int(data["max_trades"])
    return jsonify({"ok": True})

@app.route("/api/paper/balance", methods=["POST"])
def api_paper_balance():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    bal = float(data.get("balance", 10000))
    if bal < 100: return jsonify({"ok": False, "error": "Min $100"})
    with paper_lock: paper_config["balance"] = bal
    return jsonify({"ok": True})

@app.route("/api/paper/enable", methods=["POST"])
def api_paper_enable():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    paper_config["enabled"] = bool(data.get("enabled", False))
    return jsonify({"ok": True})

@app.route("/api/paper/auto", methods=["POST"])
def api_paper_auto():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    paper_config["auto_trade"] = bool(data.get("auto", False))
    return jsonify({"ok": True})

@app.route("/api/paper/data")
def api_paper_data():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    with paper_lock:
        return jsonify({
            "enabled":     paper_config["enabled"],
            "auto_trade":  paper_config["auto_trade"],
            "balance":     paper_config["balance"],
            "stats":       dict(paper_stats),
            "open_trades": dict(paper_trades),
            "history":     list(paper_history),
        })

@app.route("/api/paper/close", methods=["POST"])
def api_paper_close():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    ok, msg = close_paper_trade(data.get("symbol",""))
    return jsonify({"ok": ok, "msg": msg, "error": "" if ok else msg})

@app.route("/api/paper/reset", methods=["POST"])
def api_paper_reset():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    with paper_lock:
        paper_trades.clear()
        paper_history.clear()
        paper_stats.update({"total": 0, "wins": 0, "losses": 0, "total_pnl": 0.0})
        paper_config["balance"] = 10000.0
    log("📝 Paper stats reset")
    return jsonify({"ok": True})

@app.route("/api/settings", methods=["GET","POST"])
def api_settings():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        allowed = {"tg_token","tg_chat_id","mexc_key","mexc_secret","password","min_score","min_rr","scan_delay"}
        for k, v in data.items():
            if k not in allowed: continue
            if k in ("min_score","scan_delay"):
                bot_config[k] = int(v)
            elif k == "min_rr":
                bot_config[k] = float(v)
            else:
                if str(v).strip(): bot_config[k] = str(v).strip()
        log(f"⚙️ Settings updated: {list(data.keys())}")
        return jsonify({"ok": True})
    # GET — return config, masking secrets
    def mask(val):
        return "***" if val else ""
    return jsonify({
        "tg_token":    mask(bot_config.get("tg_token","")),
        "tg_chat":     bot_config.get("tg_chat_id",""),
        "mexc_key":    bot_config.get("mexc_key",""),
        "mexc_secret": mask(bot_config.get("mexc_secret","")),
        "min_score":   bot_config.get("min_score", 15),
        "min_rr":      bot_config.get("min_rr",    2.0),
        "scan_delay":  bot_config.get("scan_delay", 5),
    })

@app.route("/api/telegram/test", methods=["POST"])
def api_telegram_test():
    if not check_auth(): return jsonify({"error":"Unauthorized"}), 401
    tok = bot_config.get("tg_token","")
    cid = bot_config.get("tg_chat_id","")
    if not tok or not cid:
        return jsonify({"ok": False, "error": "Bot Token and Chat ID not configured — save them in Settings first"})
    ok = send_telegram("✅ <b>ICT Bot — Test Message</b>\n\nTelegram connection is working correctly!")
    if ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Message delivery failed — check Token and Chat ID"})

@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})

# ════════ STARTUP ═════════════════════════════════════════════════════

if __name__ == "__main__":
    threading.Thread(target=scanner_loop,      daemon=True).start()
    threading.Thread(target=paper_monitor_loop, daemon=True).start()
    port = int(os.environ.get("PORT_2", 5001))
    log(f"🎯 ICT Strategy Scanner starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
