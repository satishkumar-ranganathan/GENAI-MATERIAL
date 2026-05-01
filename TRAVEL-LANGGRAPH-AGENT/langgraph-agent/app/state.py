import operator
from typing import Annotated, List, TypedDict, Optional

class TravelState(TypedDict):
    # Search Inputs
    origin: str
    destination: str
    travel_date_input: str
    travel_date_formatted: str
    origin_iata: str
    destination_iata: str
    
    # Financials (Crucial for Supervisor Node)
    total_budget: float        # The user's initial limit
    remaining_budget: float    # total - (flight + hotel)
    selected_flight_price: Optional[float]
    selected_hotel_price: Optional[float]
    
    # Result Lists
    flight_options: List[dict]
    hotel_options: List[dict]
    activities: List[str]
    
    # Message History for LangGraph
    messages: Annotated[List[dict], operator.add]