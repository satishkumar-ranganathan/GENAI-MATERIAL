import os, json, re, requests
from datetime import datetime
from app.state import TravelState
from langchain_openai import ChatOpenAI
from langchain_community.utilities import SerpAPIWrapper
from app.config import logger

llm = ChatOpenAI(model="gpt-4o", temperature=0)
search_tool = SerpAPIWrapper()

def normalize_date(user_input: str):
    current_year = datetime.now().year
    # Added more common formats for better extraction
    formats = ("%b %d %Y", "%B %d %Y", "%Y-%m-%d", "%d/%m/%Y")
    
    for fmt in formats:
        try:
            # If the format already includes a year, don't append current_year
            date_str = user_input if any(char.isdigit() for char in user_input[-4:]) else f"{user_input} {current_year}"
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except: continue
        
    return datetime.now().strftime("%Y-%m-%d")

def fallback_iata(city: str):
    mapping = {"dubai": "DXB", "bangkok": "BKK", "london": "LHR"}
    return mapping.get(city.lower(), "DXB")

def input_processor_node(state: TravelState):
    logger.info(f"--- 🔍 PROCESSING: {state.get('origin')} ---")
    formatted_date = normalize_date(state["travel_date_input"])
    prompt = f"Return ONLY JSON: {{'origin_iata': '...', 'destination_iata': '...'}} for Origin: {state['origin']}, Destination: {state['destination']}"
    
    try:
        raw = llm.invoke(prompt).content.strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        data = json.loads(match.group(0))
        origin, dest = data["origin_iata"].upper(), data["destination_iata"].upper()
    except Exception as e:
        logger.warning(f"LLM Processor failed, using fallbacks: {e}")
        origin, dest = fallback_iata(state["origin"]), fallback_iata(state["destination"])
        
    return {"origin_iata": origin, "destination_iata": dest, "travel_date_formatted": formatted_date}

def flight_agent(state: TravelState):
    logger.info(f"--- ✈️ FLIGHTS: {state['origin_iata']} -> {state['destination_iata']} ---")
    url = "https://api.duffel.com/air/offer_requests?return_offers=true"
    headers = {"Duffel-Version": "v2", "Authorization": f"Bearer {os.getenv('DUFFEL_ACCESS_TOKEN')}", "Content-Type": "application/json"}
    payload = {"data": {"slices": [{"origin": state['origin_iata'], "destination": state['destination_iata'], "departure_date": state['travel_date_formatted']}], "passengers": [{"type": "adult"}], "cabin_class": "economy"}}
    
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        res.raise_for_status()
        offers = res.json().get("data", {}).get("offers", [])[:3]
        results = [{"info": f"{o['owner']['name']}: ${o['total_amount']}", "price": float(o["total_amount"])} for o in offers]
        logger.info(f"Found {len(results)} flight offers")
        return {"flight_options": results}
    except Exception as e:
        logger.error(f"Flight API Error: {e}")
        return {"flight_options": [{"info": "Emirates: $420", "price": 420}, {"info": "Qatar: $390", "price": 390}]}

def hotel_agent(state: TravelState):
    dest = state.get('destination', 'New York')
    logger.info(f"--- 🏨 HOTELS: {dest} ---")
    
    try:
        # Instead of a raw string, we create structured data for your Chainlit buttons
        # In a real app, you'd parse the search results, but for now, let's structure them:
        results = [
            {"name": f"Grand Central Hotel {dest}", "price": 250.0},
            {"name": f"Riverside Inn {dest}", "price": 150.0},
            {"name": f"City Center Suites", "price": 300.0}
        ]
        return {"hotel_options": results}
    except Exception as e:
        logger.error(f"Hotel Search Error: {e}")
        return {"hotel_options": [{"name": "Standard Stay", "price": 200.0}]}

def supervisor_node(state: TravelState):
    # Safely get values, defaulting to 0.0 if None or missing
    total = state.get("total_budget") or 0.0
    f_price = state.get("selected_flight_price") or 0.0
    h_price = state.get("selected_hotel_price") or 0.0
    
    # Calculate and round to 2 decimal places
    remaining = round(total - (f_price + h_price), 2)
    
    logger.info(f"--- 🧠 BUDGET CHECK: Total ${total} | Spent ${f_price + h_price} | Remaining ${remaining} ---")
    
    # Return the key to update the state
    return {"remaining_budget": remaining}

def activity_agent(state: TravelState):
    logger.info(f"--- 🎭 ACTIVITIES: {state['destination']} ---")
    try:
        res = search_tool.run(f"top attractions in {state['destination']}")
        return {"activities": [res]}
    except Exception as e:
        logger.error(f"Activity Search Error: {e}")
        return {"activities": ["General sightseeing"]}

def budget_warning_node(state: TravelState):
    logger.warning(f"--- ⚠️ OVER BUDGET: ${abs(state.get('remaining_budget', 0))} ---")
    return {}