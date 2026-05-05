import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict


class TravelState(TypedDict, total=False):
    # User inputs
    origin: str
    destination: str
    travel_date_input: str
    travel_date_formatted: str
    origin_iata: str
    destination_iata: str

    # Budget and selections
    total_budget: float
    remaining_budget: float
    selected_flight_price: Optional[float]
    selected_flight_info: Optional[str]
    selected_hotel_price: Optional[float]
    selected_hotel_name: Optional[str]
    hotel_skipped: bool

    # Results
    flight_options: List[Dict[str, Any]]
    hotel_options: List[Dict[str, Any]]
    activities: List[Dict[str, Any]]

    # Booking
    booking_reference: str
    is_booked: bool

    # Operational metadata
    data_source_notes: List[str]
    messages: Annotated[List[dict], operator.add]
