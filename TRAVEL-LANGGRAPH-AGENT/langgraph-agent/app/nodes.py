import json
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests
from dateutil import parser
from langchain_openai import ChatOpenAI

from app.config import DUFFEL_ACCESS_TOKEN, LLM_MODEL, OPENAI_API_KEY, SERPAPI_API_KEY, logger
from app.state import TravelState


DUFFEL_URL = "https://api.duffel.com/air/offer_requests?return_offers=true"
SERPAPI_URL = "https://serpapi.com/search.json"


def _money(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    match = re.search(r"(\d+(?:,\d{3})*(?:\.\d+)?)", str(value))
    if not match:
        return None
    return round(float(match.group(1).replace(",", "")), 2)


def _has_api_key(value: Optional[str]) -> bool:
    return bool(value and value.strip() and not value.lower().startswith("your_"))


def normalize_date(user_input: str) -> str:
    try:
        dt = parser.parse(user_input, fuzzy=True, default=datetime(1900, 1, 1))
        if dt.year == 1900:
            dt = dt.replace(year=datetime.now().year)
        if dt.date() < datetime.now().date():
            dt = dt.replace(year=datetime.now().year + 1)
        return dt.strftime("%Y-%m-%d")
    except Exception as exc:
        logger.warning("Date parsing failed for %r: %s", user_input, exc)
        return (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")


def fallback_iata(city: str) -> str:
    mapping = {
        "abu dhabi": "AUH",
        "amsterdam": "AMS",
        "bangkok": "BKK",
        "bengaluru": "BLR",
        "bangalore": "BLR",
        "chennai": "MAA",
        "delhi": "DEL",
        "dubai": "DXB",
        "hyderabad": "HYD",
        "london": "LHR",
        "mumbai": "BOM",
        "new york": "JFK",
        "paris": "CDG",
        "singapore": "SIN",
        "tokyo": "HND",
    }
    return mapping.get((city or "").strip().lower(), "DXB")


def input_processor_node(state: TravelState) -> TravelState:
    logger.info("--- PROCESSING TRIP: %s to %s ---", state.get("origin"), state.get("destination"))
    formatted_date = normalize_date(state.get("travel_date_input", ""))

    origin = fallback_iata(state.get("origin", ""))
    destination = fallback_iata(state.get("destination", ""))

    if _has_api_key(OPENAI_API_KEY):
        prompt = (
            "Return only valid JSON with airport IATA codes for this trip. "
            f"Origin city: {state.get('origin')}. Destination city: {state.get('destination')}. "
            'Schema: {"origin_iata":"DXB","destination_iata":"BKK"}'
        )
        try:
            llm = ChatOpenAI(model=LLM_MODEL, temperature=0)
            raw = llm.invoke(prompt).content.strip()
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                parsed = json.loads(match.group(0))
                origin = parsed.get("origin_iata", origin).upper()
                destination = parsed.get("destination_iata", destination).upper()
        except Exception as exc:
            logger.warning("IATA lookup via LLM failed, using fallback mapping: %s", exc)

    return {
        "origin_iata": origin,
        "destination_iata": destination,
        "travel_date_formatted": formatted_date,
    }


def _demo_flights(state: TravelState) -> List[Dict[str, Any]]:
    return [
        {
            "info": f"Demo Air: {state.get('origin_iata')} -> {state.get('destination_iata')}",
            "price": 420.0,
            "airline": "Demo Air",
            "departure": state.get("travel_date_formatted"),
            "source": "demo",
        },
        {
            "info": f"Sample Airways: {state.get('origin_iata')} -> {state.get('destination_iata')}",
            "price": 390.0,
            "airline": "Sample Airways",
            "departure": state.get("travel_date_formatted"),
            "source": "demo",
        },
    ]


def flight_agent(state: TravelState) -> TravelState:
    logger.info("--- FLIGHTS: %s -> %s ---", state.get("origin_iata"), state.get("destination_iata"))
    notes = list(state.get("data_source_notes", []))

    if not _has_api_key(DUFFEL_ACCESS_TOKEN):
        notes.append("Duffel token is missing; flight options are demo data.")
        return {"flight_options": _demo_flights(state), "data_source_notes": notes}

    headers = {
        "Duffel-Version": "v2",
        "Authorization": f"Bearer {DUFFEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "data": {
            "slices": [
                {
                    "origin": state["origin_iata"],
                    "destination": state["destination_iata"],
                    "departure_date": state["travel_date_formatted"],
                }
            ],
            "passengers": [{"type": "adult"}],
            "cabin_class": "economy",
        }
    }

    try:
        response = requests.post(DUFFEL_URL, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        offers = response.json().get("data", {}).get("offers", [])[:5]
        flights = []
        for offer in offers:
            price = _money(offer.get("total_amount"))
            if price is None:
                continue
            owner = offer.get("owner", {}) or {}
            slices = offer.get("slices", []) or []
            segment = ((slices[0] or {}).get("segments", []) or [{}])[0] if slices else {}
            flights.append(
                {
                    "info": f"{owner.get('name', 'Airline')} - {state['origin_iata']} to {state['destination_iata']}",
                    "price": price,
                    "airline": owner.get("name", "Airline"),
                    "departure": segment.get("departing_at"),
                    "arrival": segment.get("arriving_at"),
                    "source": "duffel",
                }
            )

        if flights:
            return {"flight_options": flights, "data_source_notes": notes}

        notes.append("Duffel returned no flight offers; showing demo flight options.")
    except Exception as exc:
        logger.error("Duffel flight API failed: %s", exc)
        notes.append("Duffel flight API failed; showing demo flight options.")

    return {"flight_options": _demo_flights(state), "data_source_notes": notes}


def _hotel_dates(state: TravelState) -> tuple[str, str]:
    check_in = parser.parse(state.get("travel_date_formatted") or normalize_date(state.get("travel_date_input", "")))
    check_out = check_in + timedelta(days=1)
    return check_in.strftime("%Y-%m-%d"), check_out.strftime("%Y-%m-%d")


def _demo_hotels(destination: str) -> List[Dict[str, Any]]:
    return [
        {"name": f"Central Stay {destination}", "price": 180.0, "rating": 4.2, "source": "demo"},
        {"name": f"Riverside Hotel {destination}", "price": 240.0, "rating": 4.5, "source": "demo"},
        {"name": f"Business Inn {destination}", "price": 140.0, "rating": 4.0, "source": "demo"},
    ]


def hotel_agent(state: TravelState) -> TravelState:
    destination = state.get("destination", "")
    logger.info("--- HOTELS: %s ---", destination)
    notes = list(state.get("data_source_notes", []))

    if not _has_api_key(SERPAPI_API_KEY):
        notes.append("SerpAPI key is missing; hotel options are demo data.")
        return {"hotel_options": _demo_hotels(destination), "data_source_notes": notes}

    check_in, check_out = _hotel_dates(state)
    params = {
        "engine": "google_hotels",
        "q": f"hotels in {destination}",
        "check_in_date": check_in,
        "check_out_date": check_out,
        "adults": "1",
        "currency": "USD",
        "api_key": SERPAPI_API_KEY,
    }

    try:
        response = requests.get(SERPAPI_URL, params=params, timeout=20)
        response.raise_for_status()
        payload = response.json()
        properties = payload.get("properties", []) or payload.get("hotel_results", []) or []

        hotels = []
        for item in properties[:6]:
            rate = item.get("rate_per_night") or item.get("total_rate") or {}
            price = _money(rate.get("lowest") or rate.get("extracted_lowest") or item.get("price"))
            if price is None:
                continue
            hotels.append(
                {
                    "name": item.get("name") or item.get("title") or "Hotel",
                    "price": price,
                    "rating": item.get("overall_rating") or item.get("rating"),
                    "location": item.get("neighborhood") or item.get("address"),
                    "link": item.get("link") or item.get("serpapi_property_details_link"),
                    "source": "serpapi_google_hotels",
                }
            )

        if hotels:
            return {"hotel_options": hotels, "data_source_notes": notes}

        notes.append("SerpAPI returned no hotel prices; showing demo hotel options.")
    except Exception as exc:
        logger.error("SerpAPI hotel search failed: %s", exc)
        notes.append("SerpAPI hotel search failed; showing demo hotel options.")

    return {"hotel_options": _demo_hotels(destination), "data_source_notes": notes}


def budget_check_node(state: TravelState) -> TravelState:
    total = state.get("total_budget", 0) or 0
    flight = state.get("selected_flight_price", 0) or 0
    hotel = state.get("selected_hotel_price", 0) or 0
    remaining = round(float(total) - float(flight) - float(hotel), 2)
    logger.info("--- BUDGET CHECK: total=%s remaining=%s ---", total, remaining)
    return {"remaining_budget": remaining}


def supervisor_node(state: TravelState) -> TravelState:
    return budget_check_node(state)


def activity_agent(state: TravelState) -> TravelState:
    destination = state.get("destination", "")
    logger.info("--- ACTIVITIES: %s ---", destination)
    notes = list(state.get("data_source_notes", []))

    if not _has_api_key(SERPAPI_API_KEY):
        notes.append("SerpAPI key is missing; activity options are demo data.")
        return {
            "activities": [
                {"title": f"Guided city walk in {destination}", "price": "Check availability", "source": "demo"},
                {"title": f"Top landmarks in {destination}", "price": "Check availability", "source": "demo"},
            ],
            "data_source_notes": notes,
        }

    try:
        response = requests.get(
            SERPAPI_URL,
            params={
                "engine": "google",
                "q": f"best attractions and sightseeing in {destination}",
                "api_key": SERPAPI_API_KEY,
            },
            timeout=20,
        )
        response.raise_for_status()
        results = response.json().get("organic_results", [])[:5]
        activities = [
            {
                "title": item.get("title"),
                "price": "Check availability",
                "thumbnail": item.get("thumbnail"),
                "link": item.get("link"),
                "source": "serpapi_google",
            }
            for item in results
            if item.get("title")
        ]
        if activities:
            return {"activities": activities, "data_source_notes": notes}
        notes.append("SerpAPI returned no activities; showing demo activities.")
    except Exception as exc:
        logger.error("SerpAPI activity search failed: %s", exc)
        notes.append("SerpAPI activity search failed; showing demo activities.")

    return {
        "activities": [{"title": f"Local sightseeing in {destination}", "price": "Check availability", "source": "demo"}],
        "data_source_notes": notes,
    }


def budget_warning_node(state: TravelState) -> TravelState:
    logger.warning("--- OVER BUDGET: %s ---", state.get("remaining_budget"))
    return {}


def booking_node(state: TravelState) -> TravelState:
    return {"booking_reference": f"TRV-{uuid.uuid4().hex[:6].upper()}", "is_booked": True}


def review_itinerary(state: TravelState) -> TravelState:
    return {
        "confirmation_required": True,
        "remaining_budget": state.get("remaining_budget"),
    }
