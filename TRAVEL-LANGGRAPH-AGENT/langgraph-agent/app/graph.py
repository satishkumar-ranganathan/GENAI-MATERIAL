from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from app.nodes import flight_agent, hotel_agent, input_processor_node
from app.state import TravelState


builder = StateGraph(TravelState)

builder.add_node("processor", input_processor_node)
builder.add_node("flights", flight_agent)
builder.add_node("hotels", hotel_agent)

builder.set_entry_point("processor")
builder.add_edge("processor", "flights")
builder.add_edge("flights", "hotels")
builder.add_edge("hotels", END)

memory = MemorySaver()
graph = builder.compile(checkpointer=memory)
