import os
from dotenv import load_dotenv

import logging
import sys

# Configure logging to output to stdout for CloudWatch
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger("travel-agent")

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY")
DUFFEL_ACCESS_TOKEN = os.getenv("DUFFEL_ACCESS_TOKEN")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")

# LangGraph checkpoint persistence.
# Use CHECKPOINTER_TYPE=postgres and DATABASE_URL or LANGGRAPH_POSTGRES_URI in ECS.
CHECKPOINTER_TYPE = os.getenv("CHECKPOINTER_TYPE", "memory").lower()
LANGGRAPH_POSTGRES_URI = os.getenv("LANGGRAPH_POSTGRES_URI") or os.getenv("DATABASE_URL")
LANGGRAPH_POSTGRES_SETUP = os.getenv("LANGGRAPH_POSTGRES_SETUP", "false").lower() == "true"
LANGGRAPH_POSTGRES_POOL_MODE = os.getenv("LANGGRAPH_POSTGRES_POOL_MODE", "null").lower()
LANGGRAPH_POSTGRES_POOL_MIN_SIZE = int(os.getenv("LANGGRAPH_POSTGRES_POOL_MIN_SIZE", "1"))
LANGGRAPH_POSTGRES_POOL_MAX_SIZE = int(os.getenv("LANGGRAPH_POSTGRES_POOL_MAX_SIZE", "5"))
LANGGRAPH_POSTGRES_POOL_MAX_IDLE = float(os.getenv("LANGGRAPH_POSTGRES_POOL_MAX_IDLE", "300"))
LANGGRAPH_POSTGRES_POOL_MAX_LIFETIME = float(os.getenv("LANGGRAPH_POSTGRES_POOL_MAX_LIFETIME", "300"))
LANGGRAPH_POSTGRES_POOL_TIMEOUT = float(os.getenv("LANGGRAPH_POSTGRES_POOL_TIMEOUT", "30"))
