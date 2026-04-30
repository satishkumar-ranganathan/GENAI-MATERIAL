import chainlit as cl
import httpx
import os
import logging
import sys

# =========================================================
# CLOUDWATCH LOGGING CONFIGURATION
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("TRAVEL_UI")

# Use the ECS Agent Service URL
LLM_AGENT_URL = os.environ.get("LLM_AGENT_URL", "http://your-ecs-agent:8000/chat")

async def call_agent(payload: dict):
    """Helper to communicate with the ECS LangGraph Agent"""
    async with httpx.AsyncClient(timeout=60.0) as client:
        logger.info(f"Calling Agent | Action: {payload.get('action')} | Thread: {payload.get('thread_id')}")
        response = await client.post(LLM_AGENT_URL, json=payload)
        response.raise_for_status()
        return response.json()

@cl.on_chat_start
async def start():
    thread_id = cl.user_session.get("id")
    cl.user_session.set("thread_id", thread_id)
    logger.info(f"New session started: {thread_id}")
    
    await cl.Message(
        content="✈️ **Travel Planner Active**\nReady to help you plan. Please provide your trip details in this format:\n`From, To, Date, Budget` (e.g., *Dubai, Bangkok, Dec 25, 2000*)"
    ).send()

@cl.on_message
async def handle_message(message: cl.Message):
    thread_id = cl.user_session.get("thread_id")
    user_input = message.content.strip()

    # Step 1: Initial Start Logic
    if "," in user_input and len(user_input.split(",")) >= 3:
        parts = [p.strip() for p in user_input.split(",")]
        payload = {
            "thread_id": thread_id,
            "action": "start",
            "data": {
                "origin": parts[0],
                "destination": parts[1],
                "travel_date_input": parts[2],
                "total_budget": float(parts[3]) if len(parts) > 3 else 1000,
                "messages": []
            }
        }
        res_data = await call_agent(payload)
        await process_agent_response(res_data)
    else:
        # Step 2: Handle manual budget fixes if the user typed a number
        if user_input.replace('.', '', 1).isdigit():
            payload = {
                "thread_id": thread_id,
                "action": "fix_budget",
                "data": {"total_budget": float(user_input)}
            }
            res_data = await call_agent(payload)
            await process_agent_response(res_data)
        else:
            await cl.Message(content="I didn't quite get that. Please use the format: `From, To, Date, Budget`").send()

async def process_agent_response(res_data):
    """Analyzes graph state and renders the appropriate UI elements"""
    thread_id = cl.user_session.get("thread_id")

    # Handle Flight Selection via Buttons
    if "flight_options" in res_data and not res_data.get("selected_flight_price"):
        actions = [
            cl.Action(name="select_flight", value=str(f['price']), label=f["info"])
            for f in res_data["flight_options"]
        ]
        await cl.Message(
            content="✈️ **I found several flight options. Which one would you like?**",
            actions=actions
        ).send()

    # Handle Budget Deficit
    elif res_data.get("remaining_budget", 0) < 0:
        logger.warning(f"Thread {thread_id} is over budget.")
        await cl.Message(
            content=f"❌ **Budget Alert!**\nYou are over budget by **${abs(res_data['remaining_budget'])}**.\n\nPlease type a new **Total Budget** to continue."
        ).send()

    # Handle Final Success
    elif res_data.get("activities"):
        content = f"✅ **Trip Planned Successfully!**\n\n"
        content += f"**Destination:** {res_data['destination_iata']}\n"
        content += f"**Budget Remaining:** ${res_data['remaining_budget']}\n\n"
        content += f"**Top Activities:**\n{res_data['activities'][0]}"
        await cl.Message(content=content).send()

@cl.action_callback("select_flight")
async def on_action(action: cl.Action):
    """Handles the button click for flight selection"""
    thread_id = cl.user_session.get("thread_id")
    price = float(action.value)
    
    logger.info(f"User selected flight: ${price} for thread {thread_id}")
    
    # We assume a fixed hotel cost for this interactive test or can add another button set
    payload = {
        "thread_id": thread_id,
        "action": "select_prices",
        "data": {"selected_flight_price": price, "selected_hotel_price": 500}
    }
    
    # Update UI to show the choice
    await cl.Message(content=f"Selected Flight: **${price}**. Calculating final plan...").send()
    
    res_data = await call_agent(payload)
    await process_agent_response(res_data)