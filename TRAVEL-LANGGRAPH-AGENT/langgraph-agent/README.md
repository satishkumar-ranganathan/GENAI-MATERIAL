# Travel LangGraph Agent

Production-oriented AI travel planning backend for a two-service deployment:

- `langgraph-agent`: FastAPI + LangGraph backend on port `8000`
- `chat-assistant`: Chainlit UI on port `8000`

The UI calls the backend through `LLM_AGENT_URL`, so in ECS this should point to the backend service discovery name, internal ALB URL, or private service IP.

## Workflow

1. User provides source, destination, date, and total budget in Chainlit.
2. Backend normalizes the date and resolves IATA airport codes.
3. Backend searches live flights with Duffel.
4. Backend searches live hotels with SerpAPI Google Hotels.
5. User selects one flight.
6. User either selects a hotel or skips hotel planning.
7. Backend calculates remaining budget.
8. If budget is valid, backend searches live sightseeing recommendations with SerpAPI.
9. User reviews the itinerary and explicitly confirms.
10. Only after confirmation does the backend create a `TRV-XXXXXX` reference.
11. User can retrieve the booking with `/retrieve TRV-XXXXXX`.

If Duffel or SerpAPI keys are missing or an upstream API fails, the backend returns controlled demo fallback data and includes `data_source_notes` so the UI can disclose it. For production delivery, configure real keys and monitor those notes.

## Backend Environment

Create `langgraph-agent/.env` or configure these in ECS task definition secrets/environment:

```bash
OPENAI_API_KEY=
LLM_MODEL=gpt-4o
SERPAPI_API_KEY=
DUFFEL_ACCESS_TOKEN=
BOOKING_STORE_PATH=/tmp/travel_bookings.json
```

LangGraph `MemorySaver` remains the primary state store for active conversations. Every active conversation is keyed by the `thread_id` passed from the UI, and it is available as long as the backend ECS task is running.

The booking store is only a secondary lookup for completed bookings after the user confirms. For booking retrieval across ECS task restarts, mount an EFS volume and set `BOOKING_STORE_PATH` to a file path on that volume. For a higher scale production system, replace the file store with DynamoDB or Postgres.

## UI Environment

Create `chat-assistant/.env` or configure these in ECS:

```bash
OPENAI_API_KEY=
LLM_MODEL=gpt-4o-mini
LLM_AGENT_URL=http://<backend-service-or-alb>:8000/chat
```

## Local Run

Backend:

```bash
cd langgraph-agent
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

UI:

```bash
cd chat-assistant
pip install -r requirements.txt
chainlit run app.py --host 0.0.0.0 --port 8000
```

Run the UI on a different local port if both services are on one machine.

## Docker Build

Backend:

```bash
cd langgraph-agent
docker build -t travel-langgraph-agent .
```

UI:

```bash
cd chat-assistant
docker build -t travel-chat-assistant .
```

## ECS Notes

- Put the backend and UI in the same VPC/security group path.
- Allow the UI task to reach backend port `8000`.
- Set `LLM_AGENT_URL` in the UI task to the backend service endpoint.
- Use Secrets Manager or SSM Parameter Store for API keys.
- Configure CloudWatch logs for both services.
- Add health checks:
  - Backend: `GET /health`
  - UI: Chainlit root path or container health check at the load balancer level

## Recommended Production Improvements

- Replace file-based booking storage with DynamoDB or Postgres.
- Add authentication before exposing booking retrieval publicly.
- Add request/response tracing with a correlation ID.
- Add unit tests with mocked Duffel and SerpAPI responses.
- Add retry/backoff for upstream provider failures.
