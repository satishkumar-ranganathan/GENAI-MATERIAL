from fastapi import FastAPI, Body, HTTPException
from app.graph import graph
from app.config import logger

app = FastAPI(title="Travel Agent API")

@app.post("/chat")
async def chat(payload: dict = Body(...)):
    thread_id = payload.get("thread_id", "session_1")
    config = {"configurable": {"thread_id": thread_id}}
    action = payload.get("action")
    data = payload.get("data", {})

    logger.info(f"REQUEST | Action: {action} | Thread: {thread_id}")

    try:
        if action == "start":
            # Initial run: Start the graph with the provided travel details
            graph.invoke(data, config)
        
        elif action == "select_prices":
            # Commit the flight or hotel selection to the checkpoint first
            graph.update_state(config, data)
            # Re-run from current position to update remaining_budget
            graph.invoke(None, config)

        elif action == "fix_budget":
            # 1. Update the total_budget in the checkpointer
            graph.update_state(config, data)
            # 2. Force the graph pointer back to the supervisor node
            graph.update_state(config, {}, as_node="supervisor")
            # 3. Re-run so the supervisor recalculates the new math
            graph.invoke(None, config)
            
        else:
            raise HTTPException(status_code=400, detail="Invalid action provided")

        # Always pull the fresh values after the graph finishes its turn
        final_state = graph.get_state(config).values
        logger.info(f"SUCCESS | Thread: {thread_id} | Budget State: {final_state.get('remaining_budget')}")
        return final_state

    except Exception as e:
        logger.error(f"FATAL ERROR | Thread: {thread_id} | Error: {str(e)}")
        # Shifting left on error visibility: return the actual error for easier debugging
        raise HTTPException(status_code=500, detail=f"Graph Execution Failed: {str(e)}")

@app.get("/health")
def health():
    return {"status": "healthy"}