import chainlit as cl
import httpx
import os
import logging
import sys
import json
from langchain_openai import ChatOpenAI

# =========================================================
# LOGGING & CONFIG
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("TRAVEL_UI")

LLM_AGENT_URL = os.environ.get("LLM_AGENT_URL", "http://your-ecs-agent:8000/chat")

llm = ChatOpenAI(
    model="gpt-4o-mini", 
    temperature=0,
    api_key=os.environ.get("OPENAI_API_KEY")
)

# =========================================================
# UTILITY FUNCTIONS
# =========================================================

async def parse_user_request(text: str):
    """Extracts entities and identifies missing fields."""
    prompt = f"""
    Extract travel details from the user's request. 
    User Request: "{text}"
    
    Return ONLY a JSON object with:
    - origin (string or "unknown")
    - destination (string or "unknown")
    - travel_date_input (string or "unknown")
    - total_budget (number or null)
    
    Rules:
    - If a field is missing, set it to "unknown" or null.
    - Do not guess.
    """
    try:
        response = await llm.ainvoke(prompt)
        content = response.content.strip()
        if content.startswith("```json"): content = content[7:-3]
        return json.loads(content.strip())
    except Exception as e:
        logger.error(f"Shield Parsing Error: {e}")
        return None

async def call_agent(payload: dict):
    async with httpx.AsyncClient(timeout=60.0) as client:
        logger.info(f"Calling Agent | Action: {payload.get('action')}")
        response = await client.post(LLM_AGENT_URL, json=payload)
        response.raise_for_status()
        return response.json()

# =========================================================
# CHAINLIT HANDLERS
# =========================================================

@cl.on_chat_start
async def start():
    cl.user_session.set("thread_id", cl.user_session.get("id"))
    await cl.Message(content="✈️ **Smart Travel Planner**\nWhere are we heading? Please include your **Source, Destination, Date, and Budget**.").send()

@cl.on_message
async def handle_message(message: cl.Message):
    thread_id = cl.user_session.get("thread_id")
    user_input = message.content.strip()

    # 1. Budget Adjustment Shortcut (Keep this for quick updates)
    if user_input.replace('.', '', 1).isdigit():
        payload = {"thread_id": thread_id, "action": "fix_budget", "data": {"total_budget": float(user_input)}}
        res_data = await call_agent(payload)
        await process_agent_response(res_data)
        return

    # 2. Retrieve existing data from session or initialize fresh
    current_data = cl.user_session.get("travel_data", {
        "origin": "unknown",
        "destination": "unknown",
        "travel_date_input": "unknown",
        "total_budget": None
    })

    # 3. Use the Shield to extract NEW details from the LATEST message
    new_details = await parse_user_request(user_input)
    
    # 4. MERGE: Only overwrite "unknown" or None fields with actual values
    if new_details:
        if new_details.get("origin") != "unknown":
            current_data["origin"] = new_details["origin"]
        if new_details.get("destination") != "unknown":
            current_data["destination"] = new_details["destination"]
        if new_details.get("travel_date_input") != "unknown":
            current_data["travel_date_input"] = new_details["travel_date_input"]
        if new_details.get("total_budget"):
            current_data["total_budget"] = new_details["total_budget"]

    # Save progress back to session
    cl.user_session.set("travel_data", current_data)

    # 5. Check what is STILL missing
    missing = []
    if current_data["origin"] == "unknown": missing.append("Source City")
    if current_data["destination"] == "unknown": missing.append("Destination")
    if not current_data["total_budget"]: missing.append("Total Budget")

    if missing:
        msg = f"Got it! Still need: **{', '.join(missing)}** to start the search."
        await cl.Message(content=msg).send()
        return

    # 6. Success - Clear session data for this trip and call Agent
    cl.user_session.set("travel_data", None) 
    
    payload = {
        "thread_id": thread_id,
        "action": "start",
        "data": {**current_data, "messages": []}
    }
    
    await cl.Message(content=f"🚀 All set! Searching flights from {current_data['origin']} to {current_data['destination']}...").send()
    
    try:
        res_data = await call_agent(payload)
        await process_agent_response(res_data)
    except Exception as e:
        await cl.Message(content=f"⚠️ Agent Error: {e}").send()
async def process_agent_response(res_data):
    """UI Rendering Logic"""
    # 1. Flight Buttons
    if "flight_options" in res_data and not res_data.get("selected_flight_price"):
        actions = [
            cl.Action(name="select_flight", label=f["info"], payload={"price": f['price']})
            for f in res_data["flight_options"]
        ]
        await cl.Message(content="✈️ **Select a Flight:**", actions=actions).send()

    # 2. Hotel Buttons (NO LONGER HARDCODED)
    elif "hotel_options" in res_data and not res_data.get("selected_hotel_price"):
        actions = [
            cl.Action(name="select_hotel", label=f"{h['name']} (${h['price']})", payload={"price": h['price']})
            for h in res_data["hotel_options"]
        ]
        await cl.Message(content="🏨 **Select a Hotel:**", actions=actions).send()

    # 3. Budget Alert
    elif res_data.get("remaining_budget", 0) < 0:
        over = abs(res_data['remaining_budget'])
        await cl.Message(content=f"❌ **Budget Alert!**\nYou are over by **${over:.2f}**. Please enter a new total budget.").send()

    # 4. Activities
    elif res_data.get("activities"):
        await cl.Message(content=f"✅ **Itinerary Ready!**\nRemaining Budget: **${res_data.get('remaining_budget', 0):.2f}**").send()
        # Activity card logic...
        for act in (res_data["activities"][0] if isinstance(res_data["activities"][0], list) else res_data["activities"])[:5]:
            img = [cl.Image(url=act['thumbnail'], display="inline")] if act.get('thumbnail') else []
            await cl.Message(content=f"**{act['title']}**\n{act.get('price', 'Free')}", elements=img).send()

# =========================================================
# CALLBACKS
# =========================================================

@cl.action_callback("select_flight")
async def on_flight(action: cl.Action):
    price = float(action.payload["price"])
    # Get current budget from the session we tracked in handle_message
    current_data = cl.user_session.get("travel_data", {})
    total_budget = current_data.get("total_budget", 0)

    # UI-SIDE VALIDATION (Shift-Left)
    if total_budget and price > total_budget:
        await cl.Message(content=f"⚠️ This flight (${price}) exceeds your total budget (${total_budget}). Please pick another or increase your budget first.").send()
        return

    payload = {
        "thread_id": cl.user_session.get("thread_id"), 
        "action": "select_prices", 
        "data": {"selected_flight_price": price}
    }
    res = await call_agent(payload)
    await process_agent_response(res)

@cl.action_callback("select_hotel")
async def on_hotel(action: cl.Action):
    price = float(action.payload["price"])
    # We now send ONLY the hotel price to let the agent combine it with the flight in its state
    payload = {"thread_id": cl.user_session.get("thread_id"), "action": "select_prices", "data": {"selected_hotel_price": price}}
    await cl.Message(content=f"🏨 Hotel selected: ${price}. Finalizing itinerary...").send()
    res = await call_agent(payload)
    await process_agent_response(res)