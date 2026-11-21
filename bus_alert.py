import os, time, math, requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ================= SETTINGS =================
FIXED_BUSES = ["104", "51", "15"]
STOP_LAT, STOP_LON = None, None   # optional stop, e.g. 18.5679, 73.9143 (sorts nearest bus first)
MAX_AGE_MIN = 10                  # ignore GPS pings older than this
STALE_WARN_MIN = 3                # warn if older than this
MAX_BUSES_LISTED = 3
# ============================================

LIVE_FEED_URL = "https://prod-pmpml-live-data-api.chartr.in/all-buses"
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
MAPS = "https://www.google.com/maps?q="
UA = {"User-Agent": "Mozilla/5.0"}
geo_cache = {}

def safe(e):
    return str(e).replace(BOT_TOKEN, "***")

def km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (0.5 - math.cos((lat2 - lat1) * p) / 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2)
    return 12742 * math.asin(math.sqrt(a))

def age_minutes(ts):
    try:
        t = float(ts)
        if t > 1e12: t /= 1000
        return max(0.0, (time.time() - t) / 60)
    except (TypeError, ValueError):
        return None

def fmt_age(m):
    if m is None: return ""
    if m < 1: return "just now"
    if m < 60: return f"{m:.0f} min ago"
    return f"{m/60:.0f} h ago"

def area_name(lat, lon):
    key = (round(lat, 3), round(lon, 3))
    if key in geo_cache: return geo_cache[key]
    try:
        r = requests.get(NOMINATIM_URL, params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 16},
                         headers={"User-Agent": "PMPMLBusTrackerBot/1.0"}, timeout=6).json()
        a = r.get("address", {})
        road = a.get("road") or a.get("amenity") or a.get("building") or ""
        area = a.get("suburb") or a.get("neighbourhood") or a.get("village") or a.get("city_district") or ""
        name = ", ".join(p for p in (road, area) if p) or r.get("display_name", "").split(",")[0] or "Map location"
        time.sleep(1.1)
    except Exception:
        name = "Map location"
    geo_cache[key] = name
    return name

def tg(method, **payload):
    try:
        r = requests.post(f"{BASE_URL}/{method}", json=payload, timeout=15).json()
    except Exception as e:
        print(f"Telegram {method} failed: {safe(e)}"); return {"ok": False}
    if not r.get("ok"):
        print(f"Telegram {method} error: {r.get('description')}")
    return r

def fetch_all():
    r = requests.get(LIVE_FEED_URL, headers=UA, timeout=15)
    r.raise_for_status()
    d = r.json()
    return d if isinstance(d, list) else d.get("data", [])

def base_route(raw):
    for suf in ("DOWN", "UP", "RING"):
        if raw.endswith(suf): return raw[:-len(suf)]
    return raw

def to_bus(b, ref=None):
    lat, lon = b.get("lat"), b.get("lon")
    if lat is None or lon is None: return None
    try: lat, lon = float(lat), float(lon)
    except (TypeError, ValueError): return None
    raw = str(b.get("route", "")).upper().strip()
    return dict(veh=str(b.get("id", "Bus")), route=raw, base=base_route(raw), desc=b.get("route_desc", ""),
                ac="❄️ AC" if str(b.get("ac", "")).lower() == "ac" else "Non-AC",
                lat=lat, lon=lon, age=age_minutes(b.get("timestamp")),
                dist=km(ref[0], ref[1], lat, lon) if ref else None)

def find_buses(feed, q, ref):
    res = []
    for b in feed:
        x = to_bus(b, ref)
        if not x or not x["route"] or q not in (x["base"], x["route"]): continue
        if x["age"] is not None and x["age"] > MAX_AGE_MIN: continue
        res.append(x)
    res.sort(key=(lambda x: x["dist"]) if ref else (lambda x: x["age"] if x["age"] is not None else 99))
    return res

def main():
    try:
        feed = fetch_all()
    except Exception as e:
        tg("sendMessage", chat_id=CHAT_ID, text=f"❌ Error fetching live feed: {safe(e)}")
        raise SystemExit(1)

    ref = (STOP_LAT, STOP_LON) if STOP_LAT is not None and STOP_LON is not None else None

    for q in FIXED_BUSES:
        buses = find_buses(feed, q, ref)
        if not buses:
            tg("sendMessage", chat_id=CHAT_ID,
               text=f"❌ No bus with a GPS ping in the last {MAX_AGE_MIN} min for Route {q}.")
            continue

        lines = [f"🚌 Route {q} – {len(buses)} bus(es)" + (" (nearest first)" if ref else " (freshest first)") + "\n"]
        for b in buses[:MAX_BUSES_LISTED]:
            stale = " ⚠️ old signal" if b["age"] is not None and b["age"] > STALE_WARN_MIN else ""
            lines.append(f"🔹 {b['veh']} ({b['route']} | {b['ac']})")
            if b["desc"]: lines.append(f"   Route: {b['desc']}")
            lines.append(f"   📍 Near: {area_name(b['lat'], b['lon'])}")
            info = []
            if b["dist"] is not None: info.append(f"{b['dist']:.1f} km from stop")
            if b["age"] is not None: info.append(f"updated {fmt_age(b['age'])}{stale}")
            if info: lines.append("   ⏱ " + " • ".join(info))
            lines.append(f"   🗺️ {MAPS}{b['lat']},{b['lon']}\n")
        tg("sendMessage", chat_id=CHAT_ID, text="\n".join(lines).strip(), disable_web_page_preview=True)

        top = buses[0]
        tg("sendLocation", chat_id=CHAT_ID, latitude=top["lat"], longitude=top["lon"])

main()
