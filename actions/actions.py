"""Custom actions for the Eco-Travel Advisor. Run: rasa run actions --port 5055

Data access lives in services.py; this file holds dialogue logic and scoring only.
"""
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Text

import dateparser
import requests
from rasa_sdk import Action, FormValidationAction, Tracker
from rasa_sdk.events import EventType, FollowupAction, SlotSet
from rasa_sdk.executor import CollectingDispatcher

from actions import services as svc

log = logging.getLogger(__name__)
HANDOVER_URL = os.getenv("HANDOVER_WEBHOOK_URL", "")
HANDOVER_FILE = os.getenv("HANDOVER_FILE", "handover_queue.jsonl")

# Weighted scoring: (carbon, price, preference-fit) by sustainability level. Document and defend in the report.
WEIGHTS = {"low": (0.2, 0.6, 0.2), "medium": (0.35, 0.35, 0.3), "high": (0.6, 0.15, 0.25)}
# Indicative running cost EUR/km (NOT a fare quote) and ground-route detour factor.
COST_KM = {"train": 0.12, "coach": 0.07, "car_petrol": 0.15, "flight_short": 0.14, "flight_long": 0.10}
LABEL = {"train": "Train", "coach": "Coach", "car_petrol": "Car (1 traveller)", "flight_short": "Flight (short-haul)",
         "flight_long": "Flight (long-haul)"}
CURRENCY_SYMBOLS = {"£": "GBP", "$": "USD", "€": "EUR"}


def to_number(value: Any):
    m = re.search(r"\d+(?:[.,]\d+)?", str(value))
    return float(m.group().replace(",", ".")) if m else None


def minmax(values: List[float], invert: bool = False) -> List[float]:
    lo, hi = min(values), max(values)
    if hi == lo:
        return [1.0] * len(values)
    return [(1 - (v - lo) / (hi - lo)) if invert else (v - lo) / (hi - lo) for v in values]


def weighted_score(carbon: float, price: float, pref: float, level: str) -> float:
    """Inputs 0..1 (higher = better) -> 0..100."""
    wc, wp, wf = WEIGHTS.get(level, WEIGHTS["medium"])
    return round(100 * (wc * carbon + wp * price + wf * pref), 1)


def g_band(g_per_km: float) -> str:
    return "green" if g_per_km < 60 else "amber" if g_per_km < 120 else "red"


# ------------------------------------------------------------------ form validation
class ValidateTripForm(FormValidationAction):
    def name(self) -> Text:
        return "validate_trip_form"

    def validate_trip_type(self, value, dispatcher, tracker, domain):
        v = str(value).lower()
        for key, hints in (("business_trip", ("business", "work", "conference")),
                           ("rural_eco_tour", ("rural", "eco", "nature", "country")),
                           ("city_break", ("city", "break", "weekend"))):
            if v == key or any(h in v for h in hints):
                return {"trip_type": key}
        dispatcher.utter_message(text="Please choose city break, rural eco-tour or business trip.")
        return {"trip_type": None}

    def _place(self, slot, value, dispatcher):
        try:
            g = svc.geocode(str(value))
        except Exception as exc:  # noqa: BLE001
            log.warning("geocode failed: %s", exc)
            dispatcher.utter_message(text="The place lookup is unavailable right now. Please try that again in a moment.")
            return {slot: None}
        if not g:
            dispatcher.utter_message(text=f"I couldn't find '{value}'. Try a city name, optionally with the country.")
            return {slot: None}
        dispatcher.utter_message(text=f"Understood as {g['short']}. If that's the wrong place, type it again with the country.")
        return {slot: g["short"].split(",")[0], f"{slot}_geo": f"{g['lat']},{g['lon']}"}

    def validate_origin(self, value, dispatcher, tracker, domain):
        return self._place("origin", value, dispatcher)

    def validate_destination(self, value, dispatcher, tracker, domain):
        res = self._place("destination", value, dispatcher)
        if res.get("destination_geo") and res["destination_geo"] == tracker.get_slot("origin_geo"):
            dispatcher.utter_message(text="Origin and destination are the same place. Please pick a different destination.")
            return {"destination": None, "destination_geo": None}
        return res

    def validate_travel_date(self, value, dispatcher, tracker, domain):
        d = dateparser.parse(str(value), settings={"PREFER_DATES_FROM": "future"})
        if not d:
            dispatcher.utter_message(text="I couldn't read that date. Try '14 March' or '2026-03-14'.")
            return {"travel_date": None}
        if d.date() < datetime.now().date():
            dispatcher.utter_message(text="That date is in the past. Please give a future date.")
            return {"travel_date": None}
        return {"travel_date": d.date().isoformat()}

    def validate_nights(self, value, dispatcher, tracker, domain):
        n = to_number(value)
        if n and 1 <= n <= 60:
            return {"nights": float(int(n))}
        dispatcher.utter_message(text="Please give a number of nights between 1 and 60.")
        return {"nights": None}

    def validate_budget(self, value, dispatcher, tracker, domain):
        b = to_number(value)
        if not (b and b > 0):
            dispatcher.utter_message(text="Please give your budget as a number, e.g. 600 or £500.")
            return {"budget": None}
        text = tracker.latest_message.get("text", "")
        cur = next((c for s, c in CURRENCY_SYMBOLS.items() if s in text), None) \
            or next((m.upper() for m in re.findall(r"\b[A-Za-z]{3}\b", text) if m.upper() in ("EUR", "GBP", "USD", "JPY", "PKR", "TRY", "CHF", "PLN", "SEK", "DKK")), "EUR")
        return {"budget": b, "budget_currency": cur}

    def validate_sustainability_level(self, value, dispatcher, tracker, domain):
        v = str(value).lower()
        if v in ("low", "medium", "high"):
            return {"sustainability_level": v}
        dispatcher.utter_message(text="Please choose Price first, Balanced or Lowest carbon.")
        return {"sustainability_level": None}


# ------------------------------------------------------------------ recommendation
def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as exc:  # noqa: BLE001 - each source degrades independently
        log.warning("%s failed: %s", getattr(fn, "__name__", fn), exc)
        return None


class ActionSearchOptions(Action):
    def name(self) -> Text:
        return "action_search_options"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]) -> List[EventType]:
        try:
            o_lat, o_lon = map(float, tracker.get_slot("origin_geo").split(","))
            d_lat, d_lon = map(float, tracker.get_slot("destination_geo").split(","))
        except (AttributeError, ValueError):
            dispatcher.utter_message(text="I'm missing a confirmed origin or destination. Please start the trip plan again.")
            return []
        org, dst = tracker.get_slot("origin"), tracker.get_slot("destination")
        level = tracker.get_slot("sustainability_level") or "medium"
        cur = tracker.get_slot("budget_currency") or "EUR"
        date = tracker.get_slot("travel_date")
        days_ahead = (datetime.fromisoformat(date).date() - datetime.now().date()).days if date else None

        # Budget -> EUR once (internal currency); display converts back (presentation concern).
        budget = float(tracker.get_slot("budget") or 0)
        fx = _safe(svc.get_rate, "EUR", cur)  # (EUR->cur rate, date) or None
        if cur != "EUR" and not fx:
            dispatcher.utter_message(text=f"Currency rates are unavailable, so I'll show amounts in EUR and ignore the budget in {cur}.")
            budget, cur, fx = 0.0, "EUR", (1.0, "")
        rate = fx[0] if fx else 1.0
        budget_eur = budget / rate if budget else 0.0

        # Independent lookups in parallel to protect latency.
        with ThreadPoolExecutor(max_workers=4) as ex:
            f_h = ex.submit(_safe, svc.find_hotels, d_lat, d_lon)
            f_t = ex.submit(_safe, svc.find_transport, d_lat, d_lon)
            f_a = ex.submit(_safe, svc.find_attractions, d_lat, d_lon)
            f_w = ex.submit(_safe, svc.get_weather, d_lat, d_lon, date if days_ahead is not None and 0 <= days_ahead <= 15 else None)
            hotels, stops, sights, weather = f_h.result(), f_t.result(), f_a.result(), f_w.result()

        # ---- journey options (carbon + indicative cost)
        gc = svc.haversine(o_lat, o_lon, d_lat, d_lon)
        modes = []
        if gc <= 2500:
            modes += ["train", "coach", "car_petrol"]
        if gc >= 300:
            modes.append("flight_short" if gc < 1500 else "flight_long")
        opts = []
        for m in modes:
            km = gc * (1.0 if m.startswith("flight") else 1.15)
            kg, src = svc.carbon_kg(m, km)
            opts.append({"mode": m, "kg": kg, "eur": COST_KM[m] * km, "src": src, "gpk": 1000 * kg / km})
        if opts:
            c = minmax([o["kg"] for o in opts], invert=True)
            p = minmax([o["eur"] for o in opts], invert=True)
            for o, ci, pi in zip(opts, c, p):
                fit = 1.0 if (not budget_eur or o["eur"] <= 0.5 * budget_eur) else 0.0
                o["score"] = weighted_score(ci, pi, fit, level)
            opts.sort(key=lambda o: -o["score"])
            items = [{"band": g_band(o["gpk"]), "label": {"green": "Low carbon", "amber": "Medium carbon", "red": "High carbon"}[g_band(o["gpk"])],
                      "title": LABEL[o["mode"]],
                      "lines": [f"about {svc.round_kg(o['kg'])} kg CO2e", f"about {round(o['eur'] * rate)} {cur} (indicative)"],
                      "small": f"Score {o['score']} | {o['src']}"} for o in opts]
            dispatcher.utter_message(
                text=f"{org} to {dst}: about {round(gc)} km in a straight line. Best match for you: {LABEL[opts[0]['mode']]}, "
                     f"about {svc.round_kg(opts[0]['kg'])} kg CO2e. Figures are rounded estimates for one traveller.",
                json_message={"type": "cards", "title": f"{org} to {dst}", "items": items})
            worst, best = max(opts, key=lambda o: o["kg"]), min(opts, key=lambda o: o["kg"])
            if g_band(worst["gpk"]) == "red":
                dispatcher.utter_message(
                    text=f"High-emission option: {LABEL[worst['mode']]} emits roughly {worst['kg'] / best['kg']:.0f}x more than {LABEL[best['mode']]} on this route.",
                    json_message={"type": "alert", "text": f"High-emission option: {LABEL[worst['mode']]} emits roughly {worst['kg'] / best['kg']:.0f}x more than {LABEL[best['mode']]} on this route."})

        # ---- local public transport
        if stops is not None:
            dispatcher.utter_message(text=f"Public transport near the centre of {dst}: {svc.transport_verdict(len(stops))} "
                                          f"({len(stops)} rail, metro or tram access points within 2 km). This shows where stops are, not timetables.")

        # ---- hotels: OSM has no prices and almost no eco tags, so use labelled proxies
        if hotels:
            certified = sum(1 for h in hotels if h["eco"])
            for h in hotels:
                d_stop = min((svc.haversine(h["lat"], h["lon"], s["lat"], s["lon"]) for s in stops), default=None) if stops else None
                prox = 0.3 if d_stop is None else 1.0 if d_stop <= 0.3 else 0.6 if d_stop <= 0.7 else 0.3 if d_stop <= 1.5 else 0.1
                try:
                    price_n = 1 - (min(max(float(h["stars"]), 1), 5) - 1) / 4
                except (TypeError, ValueError):
                    price_n = 0.5
                cert = bool(h["eco"])
                h["score"] = weighted_score(1.0 if cert else 0.8 * prox, price_n, 1.0 if cert else 0.0, level)
                h["band"] = "green" if cert else "amber" if prox >= 0.6 else "grey"
                h["label"] = "Certified" if cert else "Not certified" 
                h["note"] = (h["eco"] if cert else "No certification data") + \
                            ("" if d_stop is None else f"; about {round(d_stop * 1000, -1):.0f} m from a station or stop")
            hotels.sort(key=lambda h: -h["score"])
            dispatcher.utter_message(
                text=f"Stays near the centre: {certified} of {len(hotels)} hotels carry an eco-certification tag in OpenStreetMap. "
                     "The rest are ranked by proxies (distance to public transport, star tier) and are NOT certified. Prices are not available.",
                json_message={"type": "cards", "title": "Stays", "items": [
                    {"band": h["band"], "label": h["label"], "title": h["name"],
                     "lines": [h["note"], f"{h['stars']} stars" if h["stars"] else "stars unknown"],
                     "small": f"Score {h['score']} (proxy-based)"} for h in hotels[:3]]})
        else:
            dispatcher.utter_message(text="I couldn't retrieve hotels right now. The map service may be busy, so please try again shortly.")

        # ---- culture, with Wikipedia descriptions (CC BY-SA)
        if sights:
            top = sights[:3]

            def info(s):
                lang, _, t = (s["wikipedia"] or "").partition(":") if s["wikipedia"] and ":" in s["wikipedia"] else ("en", "", s["name"])
                return _safe(svc.describe, t, lang)
            with ThreadPoolExecutor(max_workers=3) as ex:
                infos = list(ex.map(info, top))
            items = [{"band": "grey", "label": s["kind"].replace("_", " ").title(), "title": s["name"],
                      "lines": [i["summary"] if i and i["summary"] else "No description available."],
                      "url": i["url"] if i else ""} for s, i in zip(top, infos)]
            dispatcher.utter_message(text="Cultural sites nearby: " + ", ".join(s["name"] for s in top) + ". Descriptions: Wikipedia (CC BY-SA).",
                                     json_message={"type": "cards", "title": "Culture", "items": items})

        # ---- weather
        if weather:
            try:
                if days_ahead is not None and 0 <= days_ahead <= 15:
                    t, r = weather["daily"]["temperature_2m_max"][0], weather["daily"]["precipitation_sum"][0]
                    when = f"on {date}"
                else:
                    t, r = weather["current"]["temperature_2m"], weather["current"]["precipitation"]
                    when = "right now (your date is too far ahead for a forecast)"
                dispatcher.utter_message(text=f"Weather in {dst} {when}: {t} C, {r} mm rain, {svc.travel_advice(t, r)}.")
            except (KeyError, IndexError, TypeError):
                pass
        if fx and cur != "EUR":
            dispatcher.utter_message(text=f"Currency: ECB reference rate for {fx[1]} (1 EUR = {fx[0]} {cur}). Real card or bureau rates differ.")
        dispatcher.utter_message(text="Map data: OpenStreetMap contributors.")
        return []


class ActionResolveLocation(Action):
    def name(self) -> Text:
        return "action_resolve_location"

    def run(self, dispatcher, tracker, domain):
        try:
            lat = float(next(tracker.get_latest_entity_values("lat")))
            lon = float(next(tracker.get_latest_entity_values("lon")))
            dispatcher.utter_message(text="Using your current location as the starting point.")
            return [SlotSet("origin", "your location"), SlotSet("origin_geo", f"{lat},{lon}")]
        except (StopIteration, ValueError):
            dispatcher.utter_message(text="I couldn't read your location. Please type your departure city instead.")
            return []


# ------------------------------------------------------------------ escalation
class ActionHandover(Action):
    def name(self) -> Text:
        return "action_handover"

    def run(self, dispatcher, tracker, domain):
        history = [{"from": e["event"], "text": e.get("text")} for e in tracker.events
                   if e.get("event") in ("user", "bot") and e.get("text")][-12:]
        package = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "conversation_id": tracker.sender_id,
            "trip": {k: tracker.get_slot(k) for k in ("trip_type", "origin", "destination", "travel_date", "nights",
                                                       "budget", "budget_currency", "sustainability_level")},
            "last_user_intent": (tracker.latest_message.get("intent") or {}).get("name"),
            "recent_messages": history,
            "consent": True,
        }
        try:
            if HANDOVER_URL:
                requests.post(HANDOVER_URL, json=package, timeout=5).raise_for_status()
            else:
                with open(HANDOVER_FILE, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(package) + "\n")
            dispatcher.utter_message(text="You're being connected to a human advisor. They will see your trip details and our recent messages.",
                                     json_message={"type": "handover", "state": "connected"})
        except Exception as exc:  # noqa: BLE001
            log.exception("handover failed: %s", exc)
            dispatcher.utter_message(text="I couldn't reach an advisor right now. Please email support and quote your conversation ID: " + tracker.sender_id,
                                     json_message={"type": "handover", "state": "failed"})
        return []


# ------------------------------------------------------------------ two-stage fallback
class ActionDefaultFallback(Action):
    """Stage 1: ask to rephrase. Stage 2: offer options. Stage 3: offer human handover (with consent)."""

    def name(self) -> Text:
        return "action_default_fallback"

    def run(self, dispatcher, tracker, domain):
        misses = 0
        for e in reversed(tracker.events):
            if e.get("event") != "user":
                continue
            if (e.get("parse_data", {}).get("intent", {}) or {}).get("name") == "nlu_fallback":
                misses += 1
            else:
                break
        if misses <= 1:
            dispatcher.utter_message(response="utter_ask_rephrase")
            return []
        if misses == 2:
            dispatcher.utter_message(response="utter_fallback_options")
            return []
        return [FollowupAction("utter_ask_handover_consent")]
