import operator
from typing import Annotated, List, TypedDict, Optional

class TravelState(TypedDict):
    origin: str
    destination: str
    travel_date_input: str
    travel_date_formatted: str
    origin_iata: str
    destination_iata: str
    total_budget: float
    remaining_budget: float
    selected_flight_price: Optional[float] # Changed to Optional for checking logic
    selected_hotel_price: Optional[float]  # Changed to Optional
    flight_options: List[dict]
    hotel_options: List[dict]
    activities: List[str]
    messages: Annotated[List[dict], operator.add]