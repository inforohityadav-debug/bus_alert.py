
import os
import time
import math
import logging
import requests

# =========================================================
# TELEGRAM CONFIGURATION
# =========================================================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
DEFAULT_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("Missing environment variable: TELEGRAM_BOT_TOKEN")

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# =========================================================
# BUS TRACKER SETTINGS
# =========================================================
FIXED_BUSES = ["104", "51", "15"]

# Optional: put your stop coordinates here to sort nearest bus first
STOP_LAT, STOP_LON = None, None
# Example:
# STOP_LAT, STOP_LON = 18.5679, 73.9143

MAX_AGE_MIN = 10
STALE_WARN_MIN = 3
MAX_BUSES_LISTED = 3

LIVE_FEED_URL = "https://prod-pmpml-live-data-api.chartr.in/all-buses"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
MAPS_URL = "https://www.google.com/maps?q="

HEADERS = {
    "User-Agent": "PMPMLBusTrackerBot/1.0 (Telegram bus tracker)"
}

# =========================================================
# LOGGING / SESSION / CACHE
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update(HEADERS)

geo_cache = {}
last_geocode_time = 0


def safe(text):
    """Prevent accidental bot token disclosure in logs."""
    return str(text).replace(BOT_TOKEN, "***")


# =========================================================
# DISTANCE CALCULATION
# =========================================================
def km(lat1, lon1, lat2, lon2):
    p = math.pi / 180

    a = (
        (0.5 - math.cos((lat2 - lat1) * p) / 2)
        + math.cos(lat1 * p)
        * math.cos(lat2 * p)
        * (1 - math.cos((lon2 - lon1) * p))
        / 2
    )

    return 12742 * math.asin(math.sqrt(max(0, min(1, a))))


# =========================================================
# GPS TIMESTAMP
# =========================================================
def age_minutes(ts):
    try:
        t = float(ts)

        # Unix milliseconds -> seconds
        if t > 1e12:
            t /= 1000

        # Ignore unrecognized timestamps
        if t <= 0:
            return None

        return max(0.0, (time.time() - t) / 60)

    except (TypeError, ValueError):
        return None


def fmt_age(minutes):
    if minutes is None:
        return "GPS age unknown"

    if minutes < 1:
        return "just now"

    if minutes < 60:
        return f"{minutes:.0f} min ago"

    return f"{minutes / 60:.1f} hours ago"


# =========================================================
# REVERSE GEOCODING
# =========================================================
def area_name(lat, lon):
    global last_geocode_time

    key = (round(lat, 3), round(lon, 3))

    if key in geo_cache:
        return geo_cache[key]

    name = "Map location"

    try:
        # Respect public Nominatim rate limits
        wait = 1.1 - (time.time() - last_geocode_time)
        if wait > 0:
            time.sleep(wait)

        response = session.get(
            NOMINATIM_URL,
            params={
                "format": "jsonv2",
                "lat": lat,
                "lon": lon,
                "zoom": 16
            },
            headers={"User-Agent": "PMPMLBusTrackerBot/1.0"},
            timeout=8
        )

        last_geocode_time = time.time()
        response.raise_for_status()

        result = response.json()
        address = result.get("address", {})

        road = (
            address.get("road")
            or address.get("amenity")
            or address.get("building")
            or address.get("pedestrian")
            or ""
        )

        area = (
            address.get("suburb")
            or address.get("neighbourhood")
            or address.get("village")
            or address.get("city_district")
            or address.get("town")
            or ""
        )

        parts = [x for x in (road, area) if x]
        name = ", ".join(parts)

        if not name:
            name = result.get("display_name", "").split(",")[0] or "Map location"

    except Exception as e:
        logging.warning("Geocoding failed: %s", safe(e))

    geo_cache[key] = name
    return name


# =========================================================
# TELEGRAM API
# =========================================================
def tg(method, **payload):
    try:
        response = requests.post(
            f"{BASE_URL}/{method}",
            json=payload,
            timeout=25
        )

        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            logging.error(
                "Telegram %s API error: %s",
                method,
                result.get("description")
            )
        else:
            logging.info("Telegram %s successful", method)

        return result

    except Exception as e:
        logging.error("Telegram %s failed: %s", method, safe(e))
        return {"ok": False, "description": safe(e)}


def send_text(chat_id, text):
    return tg(
        "sendMessage",
        chat_id=chat_id,
        text=text,
        disable_web_page_preview=True
    )


# =========================================================
# FETCH LIVE BUS DATA
# =========================================================
def fetch_all():
    response = requests.get(
        LIVE_FEED_URL,
        headers=HEADERS,
        timeout=20
    )

    response.raise_for_status()
    data = response.json()

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        buses = data.get("data", [])
        if isinstance(buses, list):
            return buses

    raise ValueError("Unexpected bus feed format")


# =========================================================
# ROUTE NORMALIZATION
# =========================================================
def base_route(raw):
    raw = raw.upper().strip()

    for suffix in ("DOWN", "UP", "RING"):
        if raw.endswith(suffix):
            raw = raw[:-len(suffix)].strip()
            break

    return raw


# =========================================================
# BUS RECORD NORMALIZATION
# =========================================================
def to_bus(bus, ref=None):
    lat = bus.get("lat")
    lon = bus.get("lon")

    if lat is None or lon is None:
        return None

    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return None

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    raw_route = str(bus.get("route", "")).upper().strip()

    if not raw_route:
        return None

    return {
        "veh": str(bus.get("id", "Bus")),
        "route": raw_route,
        "base": base_route(raw_route),
        "desc": bus.get("route_desc", ""),
        "ac": (
            "❄️ AC"
            if str(bus.get("ac", "")).lower() == "ac"
            else "Non-AC"
        ),
        "lat": lat,
        "lon": lon,
        "age": age_minutes(bus.get("timestamp")),
        "dist": (
            km(ref[0], ref[1], lat, lon)
            if ref else None
        )
    }


# =========================================================
# FILTER BUS ROUTES
# =========================================================
def find_buses(feed, route_query, ref=None):
    results = []

    for raw_bus in feed:
        bus = to_bus(raw_bus, ref)

        if not bus:
            continue

        if route_query not in (bus["base"], bus["route"]):
            continue

        # Ignore GPS pings older than MAX_AGE_MIN
        if bus["age"] is not None and bus["age"] > MAX_AGE_MIN:
            continue

        results.append(bus)

    if ref:
        results.sort(key=lambda b: b["dist"])
    else:
        results.sort(
            key=lambda b: b["age"] if b["age"] is not None else 999
        )

    return results


# =========================================================
# MAIN BUS SEARCH
# =========================================================
def main(target_chat):
    logging.info("Fetching bus data for chat %s", target_chat)

    try:
        feed = fetch_all()

    except Exception as e:
        send_text(
            target_chat,
            f"❌ Error fetching live bus feed:\n{safe(e)}"
        )
        logging.exception("Bus feed failed")
        return

    ref = (
        (STOP_LAT, STOP_LON)
        if STOP_LAT is not None and STOP_LON is not None
        else None
    )

    for route_query in FIXED_BUSES:

        buses = find_buses(feed, route_query, ref)

        if not buses:
            send_text(
                target_chat,
                f"❌ No Route {route_query} buses with GPS "
                f"updates within the last {MAX_AGE_MIN} minutes."
            )
            continue

        title = (
            f"🚌 Route {route_query} – {len(buses)} bus(es)"
            + (" (nearest first)" if ref else " (freshest first)")
        )

        lines = [title, ""]

        for bus in buses[:MAX_BUSES_LISTED]:

            stale = (
                " ⚠️ Older signal"
                if bus["age"] is not None
                and bus["age"] > STALE_WARN_MIN
                else ""
            )

            lines.append(
                f"🔹 Bus {bus['veh']} "
                f"({bus['route']} | {bus['ac']})"
            )

            if bus["desc"]:
                lines.append(f"   Route: {bus['desc']}")

            location_name = area_name(bus["lat"], bus["lon"])
            lines.append(f"   📍 Near: {location_name}")

            info = []

            if bus["dist"] is not None:
                info.append(f"{bus['dist']:.1f} km from stop")

            if bus["age"] is not None:
                info.append(f"Updated {fmt_age(bus['age'])}{stale}")

            if info:
                lines.append("   ⏱ " + " • ".join(info))

            lines.append(
                f"   🗺️ {MAPS_URL}{bus['lat']},{bus['lon']}"
            )
            lines.append("")

        # Send route information text
        send_text(target_chat, "\n".join(lines).strip())

        # Send one Telegram map pin for the freshest/nearest bus
        top_bus = buses[0]

        location_result = tg(
            "sendLocation",
            chat_id=target_chat,
            latitude=top_bus["lat"],
            longitude=top_bus["lon"]
        )

        if not location_result.get("ok"):
            send_text(
                target_chat,
                f"⚠️ Could not send GPS location for Route {route_query}."
            )


# =========================================================
# COMMAND LISTENER
# =========================================================
def listen():
    logging.info("🚌 PMPML Bus Tracker is running. Waiting for Telegram commands.")

    # Ensure webhook is disabled for long polling
    webhook_result = tg("deleteWebhook", drop_pending_updates=False)

    if not webhook_result.get("ok"):
        logging.warning("Could not delete webhook. Polling may fail.")

    offset = None

    while True:
        try:
            params = {"timeout": 25}

            if offset is not None:
                params["offset"] = offset

            response = requests.get(
                f"{BASE_URL}/getUpdates",
                params=params,
                timeout=35
            )

            response.raise_for_status()
            data = response.json()

            if not data.get("ok"):
                logging.error("getUpdates error: %s", data)
                time.sleep(4)
                continue

            for update in data.get("result", []):

                # Advance update offset so updates aren't processed repeatedly
                offset = update["update_id"] + 1

                message = (
                    update.get("message")
                    or update.get("edited_message")
                    or {}
                )

                chat_id = message.get("chat", {}).get("id")
                text = message.get("text", "").strip()

                if not chat_id or not text:
                    continue

                # Remove bot username from commands: /start@MyBot
                command = text.split()[0].split("@")[0].lower()

                logging.info(
                    "Received command %s from chat %s",
                    command,
                    chat_id
                )

                if command == "/start":
                    send_text(
                        chat_id,
                        "🚌 PMPML Bus Tracker is online!\n\n"
                        "Commands:\n"
                        "/bus – Fetch live bus locations\n"
                        "/help – Show help\n\n"
                        "Fetching your buses now..."
                    )

                    main(chat_id)

                elif command == "/bus":
                    send_text(chat_id, "🔄 Refreshing live bus locations...")
                    main(chat_id)

                elif command == "/help":
                    send_text(
                        chat_id,
                        "🚌 PMPML Bus Tracker Commands\n\n"
                        "/start – Start tracker and fetch buses\n"
                        "/bus – Refresh bus locations\n"
                        "/help – Display commands"
                    )

        except requests.exceptions.Timeout:
            # Long polling timeout is normal
            continue

        except requests.exceptions.RequestException as e:
            logging.error("Network error: %s", safe(e))
            time.sleep(5)

        except Exception as e:
            logging.exception("Listener error: %s", safe(e))
            time.sleep(3)


# =========================================================
# START BOT
# =========================================================
if __name__ == "__main__":
    listen()
