from fastapi import Body, FastAPI, HTTPException

from app.config import logger
from app.graph import graph
from app.nodes import activity_agent, booking_node, budget_check_node


app = FastAPI(title="Travel Agent API")


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _run_with_checkpoint_retry(operation_name: str, func):
    try:
        return func()
    except Exception as exc:
        message = str(exc).lower()
        is_connection_error = "connection is closed" in message or "ssl connection has been closed" in message
        if not is_connection_error:
            raise
        logger.warning("Retrying %s after stale Postgres checkpoint connection: %s", operation_name, exc)
        return func()


def _state(thread_id: str) -> dict:
    snapshot = _run_with_checkpoint_retry("get_state", lambda: graph.get_state(_config(thread_id)))
    return dict(snapshot.values or {})


def _merge_state(thread_id: str, updates: dict) -> dict:
    config = _config(thread_id)
    _run_with_checkpoint_retry("update_state", lambda: graph.update_state(config, updates))
    return _state(thread_id)


def _itinerary_response(state: dict) -> dict:
    flight_price = state.get("selected_flight_price") or 0.0
    hotel_price = state.get("selected_hotel_price") or 0.0
    total_budget = state.get("total_budget") or 0.0
    amount_planned = round(float(flight_price) + float(hotel_price), 2)
    booking_confirmed = bool(state.get("is_booked"))

    return {
        **state,
        "itinerary": {
            "booking_reference": state.get("booking_reference"),
            "booking_status": "confirmed" if booking_confirmed else "in_progress",
            "origin": state.get("origin"),
            "destination": state.get("destination"),
            "travel_date": state.get("travel_date_formatted") or state.get("travel_date_input"),
            "origin_iata": state.get("origin_iata"),
            "destination_iata": state.get("destination_iata"),
            "flight": {
                "summary": state.get("selected_flight_info"),
                "price": flight_price,
            },
            "hotel": {
                "name": state.get("selected_hotel_name"),
                "price": hotel_price,
                "skipped": bool(state.get("hotel_skipped")),
            },
            "financials": {
                "total_budget": total_budget,
                "amount_planned": amount_planned,
                "amount_paid": amount_planned if booking_confirmed else 0.0,
                "remaining_budget": state.get("remaining_budget"),
            },
            "activities": state.get("activities", []),
        },
    }


def _persist_booking_reference(reference: str, source_thread_id: str, state: dict) -> None:
    reference_state = {
        **state,
        "source_thread_id": source_thread_id,
        "thread_alias_type": "booking_reference",
    }
    _merge_state(reference, reference_state)


def _run_budget_and_activities(thread_id: str) -> dict:
    state = _state(thread_id)
    state.update(budget_check_node(state))
    _merge_state(thread_id, state)

    if state.get("remaining_budget", 0) < 0:
        return _state(thread_id)

    state = _state(thread_id)
    state.update(activity_agent(state))
    _merge_state(thread_id, state)
    return _state(thread_id)


@app.post("/chat")
async def chat(payload: dict = Body(...)):
    thread_id = payload.get("thread_id") or "session_1"
    action = payload.get("action")
    data = payload.get("data") or {}
    config = _config(thread_id)

    try:
        if action == "start":
            required = ["origin", "destination", "travel_date_input", "total_budget"]
            missing = [field for field in required if data.get(field) in (None, "", "unknown")]
            if missing:
                raise HTTPException(status_code=400, detail=f"Missing required fields: {', '.join(missing)}")

            initial_state = {
                **data,
                "selected_flight_price": None,
                "selected_flight_info": None,
                "selected_hotel_price": None,
                "selected_hotel_name": None,
                "hotel_skipped": False,
                "remaining_budget": float(data["total_budget"]),
                "is_booked": False,
                "booking_reference": "",
                "activities": [],
                "data_source_notes": [],
            }
            _run_with_checkpoint_retry("invoke", lambda: graph.invoke(initial_state, config))

        elif action == "select_prices":
            if not data:
                raise HTTPException(status_code=400, detail="Selection payload is empty")
            _merge_state(thread_id, data)
            current = _state(thread_id)
            if current.get("selected_flight_price") is not None and current.get("selected_hotel_price") is not None:
                _run_budget_and_activities(thread_id)

        elif action == "skip_hotel":
            current = _state(thread_id)
            if not current:
                raise HTTPException(status_code=404, detail="No active travel session found")
            if current.get("selected_flight_price") is None:
                raise HTTPException(status_code=400, detail="Select a flight before skipping hotel.")
            _merge_state(
                thread_id,
                {
                    "selected_hotel_price": 0.0,
                    "selected_hotel_name": "No hotel selected",
                    "hotel_skipped": True,
                },
            )
            _run_budget_and_activities(thread_id)

        elif action == "confirm_booking":
            current = _state(thread_id)
            if not current:
                raise HTTPException(status_code=404, detail="No active travel session found")
            if current.get("remaining_budget", 0) < 0:
                raise HTTPException(status_code=400, detail="Trip is over budget. Update budget before booking.")
            if current.get("selected_flight_price") is None:
                raise HTTPException(status_code=400, detail="Select a flight before booking.")
            if current.get("selected_hotel_price") is None and not current.get("hotel_skipped"):
                raise HTTPException(status_code=400, detail="Select or skip hotel before booking.")

            if not current.get("is_booked"):
                current.update(booking_node(current))
                _merge_state(thread_id, current)
            _persist_booking_reference(current["booking_reference"], thread_id, _state(thread_id))

        elif action == "retrieve":
            reference = (payload.get("reference") or thread_id or "").upper()
            current = _state(reference) or _state(thread_id)
            if not current:
                raise HTTPException(status_code=404, detail=f"No booking found for {reference}")
            return _itinerary_response(current)

        elif action == "fix_budget":
            if data.get("total_budget") is None:
                raise HTTPException(status_code=400, detail="total_budget is required")
            _merge_state(thread_id, {"total_budget": float(data["total_budget"])})
            _run_budget_and_activities(thread_id)

        elif action == "get_activities":
            current = _state(thread_id)
            if current and not current.get("activities"):
                current.update(activity_agent(current))
                _merge_state(thread_id, current)

        else:
            raise HTTPException(status_code=400, detail="Invalid action")

        return _itinerary_response(_state(thread_id))

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Backend error")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/health")
def health():
    return {"status": "healthy"}
