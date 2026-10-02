"""External data services (no API key needed except optional Climatiq).

Design rule: DATA (this file) -> LOGIC (actions.py scoring) -> PRESENTATION (frontend).
Every function raises on failure; callers decide how to degrade.
"""
import logging
import math
import os
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": f"EcoTravelAdvisor/1.0 (coursework; {os.getenv('CONTACT_EMAIL', 'your.name@example.com')})"}
OVERPASS_URLS = [os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter"),
                 "https://overpass.kumi.systems/api/interpreter"]
OVERPASS_TIMEOUT = float(os.getenv("OVERPASS_TIMEOUT", "8"))
HTTP_TIMEOUT = float(os.getenv("API_TIMEOUT", "5"))
VISITABLE_HISTORIC = "castle|monument|ruins|fort|archaeological_site|city_gate"

_cache: Dict[str, Tuple[float, Any]] = {}


def cached(key: str, ttl: float, fn: Callable[[], Any]) -> Any:
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    val = fn()
    _cache[key] = (time.time() + ttl, val)
    return val


def haversine(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


# ---- 01 Geocoding (Nominatim: 1 request/second, cache everything) ----------
_nom_lock, _nom_last = threading.Lock(), [0.0]


def geocode(place: str) -> Optional[Dict[str, Any]]:
    def go():
        with _nom_lock:
            time.sleep(max(0.0, 1.05 - (time.time() - _nom_last[0])))
            r = requests.get("https://nominatim.openstreetmap.org/search", headers=HEADERS, timeout=HTTP_TIMEOUT,
                             params={"q": place, "format": "json", "limit": 1})
            _nom_last[0] = time.time()
        r.raise_for_status()
        res = r.json()
        if not res:
            return None
        parts = [p.strip() for p in res[0]["display_name"].split(",")]
        return {"lat": float(res[0]["lat"]), "lon": float(res[0]["lon"]),
                "short": f"{parts[0]}, {parts[-1]}" if len(parts) > 1 else parts[0]}
    return cached("geo:" + place.strip().lower(), 30 * 86400, go)


# ---- Overpass helper --------------------------------------------------------
def overpass(query: str) -> List[Dict]:
    def go():
        last = None
        for url in OVERPASS_URLS:  # 504s are common on the shared server: try a mirror
            try:
                r = requests.post(url, data={"data": query}, headers=HEADERS, timeout=OVERPASS_TIMEOUT)
                r.raise_for_status()
                return r.json().get("elements", [])
            except requests.RequestException as exc:
                last = exc
        raise last
    return cached("op:" + query, 6 * 3600, go)


def _coords(el):
    return el.get("lat") or el.get("center", {}).get("lat"), el.get("lon") or el.get("center", {}).get("lon")


# ---- 02 Hotels (nwr + out center: big hotels are mapped as ways) -----------
def find_hotels(lat, lon, radius=3000, limit=30) -> List[Dict]:
    els = overpass(f'[out:json][timeout:25];nwr["tourism"="hotel"](around:{radius},{lat},{lon});out center {limit};')
    out = []
    for el in els:
        t = el.get("tags", {})
        la, lo = _coords(el)
        if not t.get("name") or la is None:
            continue
        eco = ("Green Key" if t.get("green_key") else "Ecolabel: " + t["ecolabel"] if t.get("ecolabel") else None)
        out.append({"name": t["name"], "stars": t.get("stars"), "eco": eco, "lat": la, "lon": lo})
    return out


# ---- 03 Public transport access points -------------------------------------
def find_transport(lat, lon, radius=2000) -> List[Dict]:
    q = (f'[out:json][timeout:25];(node["railway"="station"](around:{radius},{lat},{lon});'
         f'node["railway"="subway_entrance"](around:{radius},{lat},{lon});'
         f'node["railway"="tram_stop"](around:{radius},{lat},{lon}););out body 60;')
    return [{"name": e.get("tags", {}).get("name", "(unnamed)"), "type": e.get("tags", {}).get("railway", "?"),
             "lat": e["lat"], "lon": e["lon"]} for e in overpass(q)]


def transport_verdict(n: int) -> str:
    # Thresholds are arbitrary (10 / 3): justify or tune per city size in the report.
    return ("excellent, a car is not needed" if n >= 10 else
            "reasonable, check routes for your specific trip" if n >= 3 else
            "limited, factor transport emissions into your planning")


# ---- 04 Cultural attractions -----------------------------------------------
def find_attractions(lat, lon, radius=1500, limit=15) -> List[Dict]:
    q = (f'[out:json][timeout:25];(nwr["tourism"="museum"](around:{radius},{lat},{lon});'
         f'nwr["historic"~"^({VISITABLE_HISTORIC})$"](around:{radius},{lat},{lon}););out center {limit};')
    out = []
    for el in overpass(q):
        t = el.get("tags", {})
        if t.get("name"):
            out.append({"name": t["name"], "kind": t.get("tourism") or t.get("historic"), "wikipedia": t.get("wikipedia")})
    return out


# ---- 05 Wikipedia summaries (CC BY-SA: always attribute) --------------------
def describe(title: str, lang: str = "en", sentences: int = 2) -> Optional[Dict[str, str]]:
    def go():
        r = requests.get(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/"
                         + urllib.parse.quote(title.replace(" ", "_")), headers=HEADERS, timeout=HTTP_TIMEOUT)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        d = r.json()
        text = ". ".join(d.get("extract", "").split(". ")[:sentences])
        if text and not text.endswith("."):
            text += "."
        return {"summary": text, "url": d.get("content_urls", {}).get("desktop", {}).get("page", "")}
    return cached(f"wp:{lang}:{title}", 7 * 86400, go)


# ---- 06 Weather (Open-Meteo) -----------------------------------------------
def get_weather(lat, lon, date: Optional[str] = None) -> Dict[str, Any]:
    params = {"latitude": lat, "longitude": lon, "timezone": "auto",
              "current": "temperature_2m,precipitation,wind_speed_10m",
              "daily": "temperature_2m_max,precipitation_sum"}
    if date:
        params.update(start_date=date, end_date=date)
    else:
        params["forecast_days"] = 3
    r = requests.get("https://api.open-meteo.com/v1/forecast", params=params, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def travel_advice(temp_c: float, rain_mm: float) -> str:
    if rain_mm > 5:
        return "wet, so plan indoor activities and prefer public transport over cycling"
    if temp_c < 5:
        return "cold, so walking tours will be hard going"
    if temp_c > 30:
        return "hot, so avoid cycling and long walks in the midday heat"
    return "good conditions for walking and cycling"


# ---- 07 Currency (Frankfurter / ECB reference rates, daily) -----------------
def get_rate(frm: str, to: str) -> Tuple[float, str]:
    if frm == to:
        return 1.0, ""

    def go():
        r = requests.get("https://api.frankfurter.dev/v1/latest", params={"base": frm, "symbols": to},
                         headers=HEADERS, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        d = r.json()
        return d["rates"][to], d["date"]
    return cached(f"fx:{frm}:{to}", 6 * 3600, go)


# ---- 08 Carbon: local factor table, optional Climatiq -----------------------
# Rounded teaching figures from the course file (g CO2e per passenger-km).
# REPLACE with citable DESNZ/DEFRA or EEA values before submission and reference them (Harvard).
EMISSION_FACTORS = {"train": 35, "coach": 27, "electric_car": 50, "car_petrol": 170,
                    "ferry": 115, "flight_short": 250, "flight_long": 150}
CLIMATIQ_KEY = os.getenv("CLIMATIQ_API_KEY", "")
CLIMATIQ_VERSION = os.getenv("CLIMATIQ_DATA_VERSION", "^6")
CLIMATIQ_IDS = {  # check these in the Climatiq data explorer; others fall back to the table
    "car_petrol": os.getenv("CLIMATIQ_CAR_ID", "passenger_vehicle-vehicle_type_car-fuel_source_na-engine_size_na-vehicle_age_na-vehicle_weight_na"),
    "train": os.getenv("CLIMATIQ_TRAIN_ID", ""), "coach": os.getenv("CLIMATIQ_COACH_ID", ""),
    "flight_short": os.getenv("CLIMATIQ_FLIGHT_ID", ""), "flight_long": os.getenv("CLIMATIQ_FLIGHT_ID", ""),
}


def carbon_kg(mode: str, km: float) -> Tuple[float, str]:
    act = CLIMATIQ_IDS.get(mode)
    if CLIMATIQ_KEY and act:
        try:
            r = requests.post("https://api.climatiq.io/data/v1/estimate", timeout=HTTP_TIMEOUT,
                              headers={"Authorization": f"Bearer {CLIMATIQ_KEY}"},
                              json={"emission_factor": {"activity_id": act, "data_version": CLIMATIQ_VERSION},
                                    "parameters": {"distance": round(km, 1), "distance_unit": "km"}})
            r.raise_for_status()
            return float(r.json()["co2e"]), "Climatiq"
        except Exception as exc:  # noqa: BLE001
            log.warning("Climatiq failed (%s): %s", mode, exc)
    return EMISSION_FACTORS[mode] * km / 1000.0, "average factor"


def round_kg(kg: float) -> int:
    """Avoid false precision: whole kg under 10, nearest 5 under 100, else nearest 10."""
    return round(kg) if kg < 10 else int(round(kg / 5) * 5) if kg < 100 else int(round(kg / 10) * 10)
