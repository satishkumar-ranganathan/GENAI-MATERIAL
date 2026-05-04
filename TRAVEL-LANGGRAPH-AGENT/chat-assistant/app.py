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
    cl.user_session.set("activities_shown", False)
    await cl.Message(content="✈️ **Smart Travel Planner**\nWhere are we heading? Please include your **Source, Destination, Date, and Budget**.").send()

@cl.on_message
async def handle_message(message: cl.Message):
    user_input = message.content.strip()
    
    # 1. RETRIEVE COMMAND
    if user_input.lower().startswith("/retrieve"):
        parts = user_input.split(" ")
        if len(parts) < 2:
            await cl.Message(content="⚠️ Please provide the ID. Example: `/retrieve TRV-A1B2C3`").send()
            return
        
        ref_id = parts[1].strip().upper()
        cl.user_session.set("thread_id", ref_id) # Switch context to the booking ID
        
        try:
            res_data = await call_agent({"thread_id": ref_id, "action": "retrieve"})
            await cl.Message(content=f"🔍 Pulling record for `{ref_id}`...").send()
            await process_agent_response(res_data)
        except Exception as e:
            await cl.Message(content=f"❌ Could not find Reference ID: {ref_id}").send()
        return

    thread_id = cl.user_session.get("thread_id")

    # 2. Budget Adjustment Shortcut
    if user_input.replace('.', '', 1).isdigit():
        payload = {"thread_id": thread_id, "action": "fix_budget", "data": {"total_budget": float(user_input)}}
        res_data = await call_agent(payload)
        await process_agent_response(res_data)
        return

    # 3. Session Initialization (FIXED: Prevents NoneType error)
    current_data = cl.user_session.get("travel_data")
    if current_data is None:
        current_data = {
            "origin": "unknown",
            "destination": "unknown",
            "travel_date_input": "unknown",
            "total_budget": None
        }

    # 4. Extract and Merge
    new_details = await parse_user_request(user_input)
    if new_details:
        for key in ["origin", "destination", "travel_date_input"]:
            if new_details.get(key) != "unknown":
                current_data[key] = new_details[key]
        if new_details.get("total_budget"):
            current_data["total_budget"] = new_details["total_budget"]

    cl.user_session.set("travel_data", current_data)

    # 5. Missing Fields Check
    missing = []
    if current_data["origin"] == "unknown": missing.append("Source City")
    if current_data["destination"] == "unknown": missing.append("Destination")
    if not current_data["total_budget"]: missing.append("Total Budget")

    if missing:
        msg = f"Got it! Still need: **{', '.join(missing)}** to start the search."
        await cl.Message(content=msg).send()
        return

    # 6. Start Agent Search
    cl.user_session.set("travel_data", None) # Clear draft
    payload = {
        "thread_id": thread_id,
        "action": "start",
        "data": {
            "origin": current_data.get("origin", "unknown"),
            "destination": current_data.get("destination", "unknown"),
            "travel_date_input": current_data.get("travel_date_input", "unknown"),
            "total_budget": current_data.get("total_budget")
        }
    }
    
    await cl.Message(content=f"🚀 Searching flights from {current_data['origin']} to {current_data['destination']}...").send()
    
    try:
        res_data = await call_agent(payload)
        await process_agent_response(res_data)
    except Exception as e:
        await cl.Message(content=f"⚠️ Agent Error: {e}").send()

# =========================================================
# UI RENDERING LOGIC
# =========================================================

async def process_agent_response(res_data):
    # 1. Flight Selection Buttons
    if "flight_options" in res_data and not res_data.get("selected_flight_price"):
        actions = [
            cl.Action(name="select_flight", label=f"{f['info']} (${f['price']})", payload={"price": f['price']})
            for f in res_data["flight_options"]
        ]
        await cl.Message(content="✈️ **Select a Flight:**", actions=actions).send()

    # 2. Hotel Selection Buttons
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

    # 4. HITL: Confirmation Before Final Booking
    elif res_data.get("selected_hotel_price") and not res_data.get("is_booked"):
        summary = (
            f"### 🛡️ Final Confirmation\n"
            f"Ready to book? Your remaining budget will be **${res_data.get('remaining_budget', 0):.2f}**."
        )
        actions = [cl.Action(name="confirm_booking", label="✅ Confirm & Generate ID", payload={})]
        await cl.Message(content=summary, actions=actions).send()

    # 5. Post-Booking: Reference ID & Sightseeing Toggle
    elif res_data.get("is_booked"):
        ref = res_data.get("booking_reference")
        await cl.Message(content=f"🎉 **Booking Confirmed!**\nReference ID: `{ref}`\nUse `/retrieve {ref}` to see this later.").send()
        
        if res_data.get("activities") and not cl.user_session.get("activities_shown"):
            actions = [cl.Action(name="show_spots", label="🎡 View Sightseeing Spots", value="show",payload={})]
            await cl.Message(content="Would you like to see local attractions?", actions=actions).send()

# =========================================================
# CALLBACKS
# =========================================================

@cl.action_callback("select_flight")
async def on_flight(action: cl.Action):
    price = float(action.payload["price"])
    payload = {"thread_id": cl.user_session.get("thread_id"), "action": "select_prices", "data": {"selected_flight_price": price}}
    res = await call_agent(payload)
    await process_agent_response(res)

@cl.action_callback("select_hotel")
async def on_hotel(action: cl.Action):
    price = float(action.payload["price"])
    payload = {"thread_id": cl.user_session.get("thread_id"), "action": "select_prices", "data": {"selected_hotel_price": price}}
    await cl.Message(content=f"🏨 Hotel selected: ${price}. Calculating final itinerary...").send()
    res = await call_agent(payload)
    await process_agent_response(res)

@cl.action_callback("confirm_booking")
async def on_confirm(action: cl.Action):
    payload = {"thread_id": cl.user_session.get("thread_id"), "action": "confirm_booking"}
    res = await call_agent(payload)
    await process_agent_response(res)

@cl.action_callback("show_spots")
async def on_show_spots(action: cl.Action):
    cl.user_session.set("activities_shown", True)
    res_data = await call_agent({"thread_id": cl.user_session.get("thread_id"), "action": "retrieve"})
    
    for act in res_data.get("activities", [])[:5]:
        img = [cl.Image(url=act['thumbnail'], display="inline")] if act.get('thumbnail') else []
        await cl.Message(content=f"**{act['title']}**\n{act.get('price', 'Free')}", elements=img).send()