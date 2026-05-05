from fastapi import Body, FastAPI, HTTPException

from app.booking_store import get_booking, save_booking
from app.config import logger
from app.graph import graph
from app.nodes import activity_agent, booking_node, budget_check_node


app = FastAPI(title="Travel Agent API")


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _state(thread_id: str) -> dict:
    return dict(graph.get_state(_config(thread_id)).values or {})


def _merge_state(thread_id: str, updates: dict) -> dict:
    config = _config(thread_id)
    graph.update_state(config, updates)
    return _state(thread_id)


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
            graph.invoke(initial_state, config)

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

            current.update(booking_node(current))
            _merge_state(thread_id, current)
            save_booking(current["booking_reference"], _state(thread_id))

        elif action == "retrieve":
            reference = (payload.get("reference") or thread_id or "").upper()
            stored = get_booking(reference)
            if stored:
                return stored
            current = _state(thread_id)
            if not current:
                raise HTTPException(status_code=404, detail=f"No booking found for {reference}")

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

        return _state(thread_id)

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Backend error")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/health")
def health():
    return {"status": "healthy"}
