# =========================================================
# IMPORTS
# =========================================================
import json
import os
import boto3
import logging
import traceback
from datetime import datetime
from openai import OpenAI
from datetime import datetime, timezone

# =========================================================
# LOGGING CONFIG
# =========================================================
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def log(level, message, **kwargs):
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "message": message,
        **kwargs
    }
    getattr(logger, level.lower())(json.dumps(log_entry))

# =========================================================
# GLOBAL CACHE
# =========================================================
cached_secrets = None
client = None
lambda_client = boto3.client("lambda")

# =========================================================
# DATABASE SCHEMA
# =========================================================
SCHEMA = """
Database: PostgreSQL
Schema: demo

Tables:

1. customers
- customer_id (VARCHAR, PRIMARY KEY)
- customer_name (VARCHAR)
- email (VARCHAR)
- city (VARCHAR)
- state (VARCHAR)
- created_at (TIMESTAMP)

2. orders
- order_id (VARCHAR, PRIMARY KEY)
- customer_id (VARCHAR, FOREIGN KEY -> customers.customer_id)
- order_date (TIMESTAMP)
- status (VARCHAR)
- total_amount (NUMERIC)

Relationships:
- orders.customer_id = customers.customer_id (many-to-one)
"""

# =========================================================
# PROMPTS
# =========================================================
SQL_PROMPT = """
You are an expert PostgreSQL query generator.

Your task is to convert a natural language question into a SAFE SQL query.

=====================
STRICT RULES
=====================
1. ALWAYS use schema name: demo
2. ALWAYS use table aliases:
   - customers → c
   - orders → o
3. ALWAYS qualify column names (e.g., c.customer_name)
4. LIMIT results to 50 unless aggregation is used
5. Use ORDER BY when relevant (e.g., latest → order_date DESC)

=====================
CASE INSENSITIVITY RULES
=====================
- ALWAYS assume user input may have mixed case
- For string comparisons, prefer case-insensitive matching
- Use LOWER() on both column and value when filtering text fields

Examples:
- c.city = 'New York' ❌
- LOWER(c.city) = LOWER('New York') ✅

OR use ILIKE for pattern matching:
- c.city ILIKE 'new york'

=====================
BUSINESS LOGIC
=====================
- If query involves both customers and orders → MUST use JOIN
- Join condition: o.customer_id = c.customer_id
- "total", "revenue", "sum" → use SUM(o.total_amount)
- "count", "how many" → use COUNT(*)
- "latest" → ORDER BY o.order_date DESC

=====================
OUTPUT FORMAT (CRITICAL)
=====================
- Return ONLY raw SQL query
- DO NOT include markdown (no ```sql)
- DO NOT include explanations
- DO NOT include comments
- DO NOT include extra text

=====================
SCHEMA
=====================
{schema}

=====================
USER QUESTION
=====================
{question}
"""

INTENT_PROMPT = """
Classify the user request into one of the following:

1. SELECT
2. INSERT
3. UPDATE
4. DELETE

User Input:
{question}

Return ONLY the intent.
"""

FORMAT_PROMPT = """
You are a business analyst.

Convert SQL result into a human-friendly response.

User Question:
{question}

SQL Result:
{result}

Rules:
- Be concise
- Highlight key insights
- If empty → say "No data found"
- If aggregation → explain meaning clearly
"""

# =========================================================
# SECRETS MANAGER
# =========================================================
def get_secrets():
    global cached_secrets

    if cached_secrets:
        return cached_secrets

    try:
        secret_name = os.environ.get("SECRET_NAME")
        region_name = os.environ.get("AWS_REGION_NAME", "us-east-1")

        if not secret_name:
            raise ValueError("SECRET_NAME not set")

        log("INFO", "Fetching secrets", secret_name=secret_name)

        sm_client = boto3.client("secretsmanager", region_name=region_name)
        response = sm_client.get_secret_value(SecretId=secret_name)

        cached_secrets = json.loads(response["SecretString"])
        return cached_secrets

    except Exception as e:
        log("ERROR", "Secrets Manager failure", error=str(e), trace=traceback.format_exc())
        raise Exception("Secrets retrieval failed")

# =========================================================
# INIT CLIENT
# =========================================================
def init_clients():
    global client

    if client is None:
        secrets = get_secrets()
        client = OpenAI(api_key=secrets["OPENAI_API_KEY"])
        log("INFO", "OpenAI client initialized")

# =========================================================
# LLM FUNCTIONS
# =========================================================
def detect_intent(user_query: str):
    try:
        init_clients()
        secrets = get_secrets()

        log("INFO", "Detecting intent", query=user_query)

        response = client.chat.completions.create(
            model=secrets.get("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": INTENT_PROMPT.format(question=user_query)}],
            temperature=0
        )

        intent = response.choices[0].message.content.strip().upper()

        log("INFO", "Intent detected", intent=intent)
        return intent

    except Exception as e:
        log("ERROR", "Intent detection failed", error=str(e), trace=traceback.format_exc())
        raise Exception("Intent detection failed")


def generate_sql(user_query: str, intent: str):
    try:
        init_clients()
        secrets = get_secrets()

        log("INFO", "Generating SQL", intent=intent)

        prompt = f"You MUST generate a {intent} SQL query.\n\n" + \
                 SQL_PROMPT.format(schema=SCHEMA, question=user_query)

        response = client.chat.completions.create(
            model=secrets.get("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )

        sql = response.choices[0].message.content.strip()

        log("INFO", "SQL generated", sql=sql)
        return sql

    except Exception as e:
        log("ERROR", "SQL generation failed", error=str(e), trace=traceback.format_exc())
        raise Exception("SQL generation failed")


def format_result(user_query: str, result: dict):
    try:
        init_clients()
        secrets = get_secrets()

        log("INFO", "Formatting response")

        response = client.chat.completions.create(
            model=secrets.get("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{
                "role": "user",
                "content": FORMAT_PROMPT.format(
                    question=user_query,
                    result=json.dumps(result)
                )
            }],
            temperature=0.3
        )

        return response.choices[0].message.content

    except Exception as e:
        log("ERROR", "Response formatting failed", error=str(e), trace=traceback.format_exc())
        raise Exception("Formatting failed")

# =========================================================
# VALIDATION
# =========================================================
def validate_sql(query: str, intent: str):
    try:
        log("INFO", "Validating SQL", intent=intent)

        upper = query.upper()

        if not upper.startswith(intent):
            raise ValueError(f"Query must start with {intent}")

        return query

    except Exception as e:
        log("ERROR", "SQL validation failed", error=str(e), sql=query)
        raise Exception("SQL validation failed")

# =========================================================
# EXECUTION
# =========================================================
def execute_sql(query: str):
    try:
        secrets = get_secrets()

        log("INFO", "Invoking query executor", function=secrets["QUERY_EXECUTOR_FUNCTION"])

        payload = {
            "query": query,
            "params": [],
            "fetch": True
        }

        response = lambda_client.invoke(
            FunctionName=secrets["QUERY_EXECUTOR_FUNCTION"],
            InvocationType="RequestResponse",
            Payload=json.dumps(payload)
        )

        raw = json.loads(response["Payload"].read())

        if isinstance(raw.get("body"), str):
            result = json.loads(raw["body"])
        else:
            result = raw

        log("INFO", "Query executed", success=result.get("success"))

        return result

    except Exception as e:
        log("ERROR", "Query execution failed", error=str(e), trace=traceback.format_exc())
        raise Exception("Execution failed")

# =========================================================
# HANDLER
# =========================================================
def lambda_handler(event, context):

    request_id = context.aws_request_id if context else "local"

    log("INFO", "Request received", request_id=request_id, event=event)

    try:
        # Parse input
        if isinstance(event.get("body"), str):
            body = json.loads(event["body"])
        elif event.get("queryStringParameters"):
            body = event["queryStringParameters"]
        else:
            body = event

        user_query = body.get("query")

        if not user_query:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "query is required"})
            }

        # FLOW
        intent = detect_intent(user_query)
        sql = generate_sql(user_query, intent)
        validate_sql(sql, intent)
        result = execute_sql(sql)

        if not result.get("success"):
            return {
                "statusCode": 500,
                "body": json.dumps({
                    "error": "Query execution failed",
                    "details": result
                })
            }

        final_response = format_result(user_query, result)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "request_id": request_id,
                "intent": intent,
                "sql": sql,
                "response": final_response
            })
        }

    except Exception as e:
        log("ERROR", "Unhandled error", error=str(e), trace=traceback.format_exc())

        return {
            "statusCode": 500,
            "body": json.dumps({
                "request_id": request_id,
                "error": str(e)
            })
        }