import json
import logging
import os
import re
import sys

import chainlit as cl
import httpx
from langchain_openai import ChatOpenAI


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("TRAVEL_UI")

LLM_AGENT_URL = os.environ.get("LLM_AGENT_URL", "http://localhost:8000/chat")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")


def _llm():
    if not OPENAI_API_KEY:
        return None
    return ChatOpenAI(model=LLM_MODEL, temperature=0, api_key=OPENAI_API_KEY)


def _extract_budget(text: str):
    match = re.search(r"(?:budget|under|below|around|usd|\$)\s*\$?\s*(\d+(?:,\d{3})*(?:\.\d+)?)", text, re.I)
    if not match:
        match = re.search(r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)", text)
    return float(match.group(1).replace(",", "")) if match else None


async def parse_user_request(text: str):
    prompt = f"""
Extract travel details from the user's request.
User Request: "{text}"

Return ONLY valid JSON with:
- origin: string or "unknown"
- destination: string or "unknown"
- travel_date_input: string or "unknown"
- total_budget: number or null

Rules:
- Do not guess missing fields.
- Keep dates as the user wrote them, for example "May 15", "next Friday", or "2026-06-01".
"""
    client = _llm()
    if not client:
        return {
            "origin": "unknown",
            "destination": "unknown",
            "travel_date_input": "unknown",
            "total_budget": _extract_budget(text),
        }

    try:
        response = await client.ainvoke(prompt)
        content = response.content.strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.endswith("```"):
            content = content[:-3]
        parsed = json.loads(content.strip())
        parsed["total_budget"] = parsed.get("total_budget") or _extract_budget(text)
        return parsed
    except Exception as exc:
        logger.error("Request parsing error: %s", exc)
        return None


async def call_agent(payload: dict):
    async with httpx.AsyncClient(timeout=90.0) as client:
        logger.info("Calling agent action=%s", payload.get("action"))
        response = await client.post(LLM_AGENT_URL, json=payload)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise RuntimeError(detail)
        return response.json()


def _new_travel_data():
    return {
        "origin": "unknown",
        "destination": "unknown",
        "travel_date_input": "unknown",
        "total_budget": None,
    }


@cl.on_chat_start
async def start():
    cl.user_session.set("thread_id", cl.user_session.get("id"))
    cl.user_session.set("activities_shown", False)
    cl.user_session.set("notes_shown", False)
    await cl.Message(
        content=(
            "**Smart Travel Planner**\n"
            "Share your source city, destination, travel date, and total budget."
        )
    ).send()


@cl.on_message
async def handle_message(message: cl.Message):
    user_input = message.content.strip()

    if user_input.lower().startswith("/retrieve"):
        parts = user_input.split()
        if len(parts) < 2:
            await cl.Message(content="Please provide the reference ID. Example: `/retrieve TRV-A1B2C3`").send()
            return

        reference = parts[1].strip().upper()
        try:
            res_data = await call_agent({"thread_id": reference, "reference": reference, "action": "retrieve"})
            cl.user_session.set("thread_id", reference)
            await process_agent_response(res_data)
        except Exception as exc:
            await cl.Message(content=f"Could not find `{reference}`. {exc}").send()
        return

    thread_id = cl.user_session.get("thread_id")

    if user_input.replace(".", "", 1).isdigit():
        try:
            res_data = await call_agent(
                {
                    "thread_id": thread_id,
                    "action": "fix_budget",
                    "data": {"total_budget": float(user_input)},
                }
            )
            await process_agent_response(res_data)
        except Exception as exc:
            await cl.Message(content=f"Could not update budget: {exc}").send()
        return

    current_data = cl.user_session.get("travel_data") or _new_travel_data()
    new_details = await parse_user_request(user_input)

    if new_details:
        for key in ["origin", "destination", "travel_date_input"]:
            if new_details.get(key) and new_details.get(key) != "unknown":
                current_data[key] = new_details[key]
        if new_details.get("total_budget") is not None:
            current_data["total_budget"] = float(new_details["total_budget"])

    cl.user_session.set("travel_data", current_data)

    missing = []
    if current_data["origin"] == "unknown":
        missing.append("source city")
    if current_data["destination"] == "unknown":
        missing.append("destination")
    if current_data["travel_date_input"] == "unknown":
        missing.append("travel date")
    if current_data["total_budget"] is None:
        missing.append("total budget")

    if missing:
        await cl.Message(content=f"Got it. I still need: **{', '.join(missing)}**.").send()
        return

    cl.user_session.set("travel_data", None)
    await cl.Message(
        content=(
            f"Searching live options for **{current_data['origin']} -> {current_data['destination']}** "
            f"on **{current_data['travel_date_input']}**."
        )
    ).send()

    try:
        res_data = await call_agent({"thread_id": thread_id, "action": "start", "data": current_data})
        await process_agent_response(res_data)
    except Exception as exc:
        await cl.Message(content=f"Agent error: {exc}").send()


async def _show_source_notes(res_data):
    notes = res_data.get("data_source_notes") or []
    if notes and not cl.user_session.get("notes_shown"):
        cl.user_session.set("notes_shown", True)
        await cl.Message(content="Data note: " + " ".join(notes)).send()


async def process_agent_response(res_data):
    await _show_source_notes(res_data)

    if res_data.get("is_booked"):
        ref = res_data.get("booking_reference")
        await cl.Message(
            content=(
                f"Booking confirmed.\n"
                f"Reference ID: `{ref}`\n"
                f"Use `/retrieve {ref}` anytime to view this itinerary."
            )
        ).send()

        if res_data.get("activities") and not cl.user_session.get("activities_shown"):
            actions = [cl.Action(name="show_spots", label="View sightseeing spots", payload={})]
            await cl.Message(content="Sightseeing recommendations are ready.", actions=actions).send()
        return

    if res_data.get("flight_options") and res_data.get("selected_flight_price") is None:
        actions = []
        for index, flight in enumerate(res_data["flight_options"]):
            label = f"{flight.get('airline') or flight.get('info')} (${flight['price']})"
            actions.append(
                cl.Action(
                    name="select_flight",
                    label=label[:80],
                    payload={
                        "price": flight["price"],
                        "info": flight.get("info") or label,
                        "index": index,
                    },
                )
            )
        await cl.Message(content="Select a flight:", actions=actions).send()
        return

    if res_data.get("hotel_options") and res_data.get("selected_hotel_price") is None:
        actions = [cl.Action(name="skip_hotel", label="Skip hotel", payload={})]
        for index, hotel in enumerate(res_data["hotel_options"]):
            rating = f" | {hotel.get('rating')} stars" if hotel.get("rating") else ""
            label = f"{hotel.get('name')} (${hotel['price']}){rating}"
            actions.append(
                cl.Action(
                    name="select_hotel",
                    label=label[:80],
                    payload={
                        "price": hotel["price"],
                        "name": hotel.get("name"),
                        "index": index,
                    },
                )
            )
        await cl.Message(content="Select a hotel:", actions=actions).send()
        return

    if res_data.get("remaining_budget", 0) < 0:
        over = abs(res_data["remaining_budget"])
        await cl.Message(
            content=f"Budget alert: this trip is over by **${over:.2f}**. Enter a new total budget to continue."
        ).send()
        return

    if res_data.get("selected_flight_price") is not None and res_data.get("selected_hotel_price") is not None:
        hotel_line = (
            "Hotel: **Skipped**"
            if res_data.get("hotel_skipped")
            else f"Hotel: **${res_data.get('selected_hotel_price', 0):.2f}**"
        )
        summary = (
            "**Final confirmation**\n"
            f"Flight: **${res_data.get('selected_flight_price', 0):.2f}**\n"
            f"{hotel_line}\n"
            f"Remaining budget: **${res_data.get('remaining_budget', 0):.2f}**"
        )
        actions = [cl.Action(name="confirm_booking", label="Confirm and generate ID", payload={})]
        await cl.Message(content=summary, actions=actions).send()


@cl.action_callback("select_flight")
async def on_flight(action: cl.Action):
    try:
        price = float(action.payload["price"])
        res = await call_agent(
            {
                "thread_id": cl.user_session.get("thread_id"),
                "action": "select_prices",
                "data": {
                    "selected_flight_price": price,
                    "selected_flight_info": action.payload.get("info"),
                },
            }
        )
        await process_agent_response(res)
    except Exception as exc:
        await cl.Message(content=f"Could not select flight: {exc}").send()


@cl.action_callback("select_hotel")
async def on_hotel(action: cl.Action):
    try:
        price = float(action.payload["price"])
        await cl.Message(content=f"Hotel selected: **{action.payload.get('name')}** (${price:.2f}).").send()
        res = await call_agent(
            {
                "thread_id": cl.user_session.get("thread_id"),
                "action": "select_prices",
                "data": {
                    "selected_hotel_price": price,
                    "selected_hotel_name": action.payload.get("name"),
                },
            }
        )
        await process_agent_response(res)
    except Exception as exc:
        await cl.Message(content=f"Could not select hotel: {exc}").send()


@cl.action_callback("skip_hotel")
async def on_skip_hotel(action: cl.Action):
    try:
        await cl.Message(content="Hotel skipped. I will continue with the flight-only itinerary.").send()
        res = await call_agent({"thread_id": cl.user_session.get("thread_id"), "action": "skip_hotel"})
        await process_agent_response(res)
    except Exception as exc:
        await cl.Message(content=f"Could not skip hotel: {exc}").send()


@cl.action_callback("confirm_booking")
async def on_confirm(action: cl.Action):
    try:
        res = await call_agent({"thread_id": cl.user_session.get("thread_id"), "action": "confirm_booking"})
        await process_agent_response(res)
    except Exception as exc:
        await cl.Message(content=f"Could not confirm booking: {exc}").send()


@cl.action_callback("show_spots")
async def on_show_spots(action: cl.Action):
    cl.user_session.set("activities_shown", True)
    try:
        res_data = await call_agent({"thread_id": cl.user_session.get("thread_id"), "action": "retrieve"})
        for activity in res_data.get("activities", [])[:5]:
            image = [cl.Image(url=activity["thumbnail"], display="inline")] if activity.get("thumbnail") else []
            link = f"\n{activity['link']}" if activity.get("link") else ""
            await cl.Message(
                content=f"**{activity['title']}**\n{activity.get('price', 'Check availability')}{link}",
                elements=image,
            ).send()
    except Exception as exc:
        await cl.Message(content=f"Could not load sightseeing spots: {exc}").send()
