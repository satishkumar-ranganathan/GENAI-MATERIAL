from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from app.state import TravelState
from app.nodes import *

def route(state: TravelState):
    if state["remaining_budget"] < 0: return "warn"
    return "proceed"

builder = StateGraph(TravelState)
builder.add_node("processor", input_processor_node)
builder.add_node("flights", flight_agent)
builder.add_node("hotels", hotel_agent)
builder.add_node("supervisor", supervisor_node)
builder.add_node("budget_warning", budget_warning_node)
builder.add_node("activities", activity_agent)

builder.set_entry_point("processor")
builder.add_edge("processor", "flights")
builder.add_edge("flights", "hotels")
builder.add_edge("hotels", "supervisor")

builder.add_conditional_edges("supervisor", route, {"warn": "budget_warning", "proceed": "activities"})
builder.add_edge("activities", END)
builder.add_edge("budget_warning", END)

memory = MemorySaver()
graph = builder.compile(checkpointer=memory, interrupt_after=["hotels", "budget_warning"])