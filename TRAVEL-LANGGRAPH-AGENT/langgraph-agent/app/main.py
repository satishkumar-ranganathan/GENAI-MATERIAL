from fastapi import FastAPI, Body
from app.graph import graph

app = FastAPI()

@app.post("/chat")
async def chat(payload: dict = Body(...)):
    thread_id = payload.get("thread_id", "session_1")
    config = {"configurable": {"thread_id": thread_id}}
    action = payload.get("action") # "start", "select_prices", or "fix_budget"

    if action == "start":
        inputs = payload.get("data") # {origin, destination, date, budget}
        graph.invoke(inputs, config)
    
    elif action == "select_prices":
        # Replaces your input("\nChosen flight price: ")
        graph.update_state(config, payload.get("data"))
        graph.invoke(None, config)

    elif action == "fix_budget":
        # Replaces your budget deficit choice logic
        graph.update_state(config, payload.get("data"))
        graph.update_state(config, {}, as_node="supervisor")
        graph.invoke(None, config)

    return graph.get_state(config).values