# 🚀 GenAI SQL Assistant – Enterprise Architecture (Lambda + LLM + Chainlit + DB Executor)

## 📌 Overview

This project is an **end-to-end GenAI-powered Natural Language to SQL system** built using AWS Lambda, OpenAI LLM, PostgreSQL, and a Chainlit chat interface.

It allows users to ask questions in natural language (e.g., *“Who is the most expensive customer order?”*) and get:

- 🔹 Generated SQL (LLM-powered)
- 🔹 Executed database results
- 🔹 Human-readable business response

---

## 🧠 Core Idea

Instead of writing SQL manually:
User → Natural Language Question
↓
LLM (Intent + SQL Generation)
↓
Validated SQL Query
↓
Execution Service (Lambda / DB Executor)
↓
Formatted Business Response

---

## 🏗️ High-Level Architecture
            ┌─────────────────────────────┐
            │        Chainlit UI          │
            │  (Chat Interface / Bot)     │
            └─────────────┬───────────────┘
                          │ HTTPS POST
                          ▼
    ┌────────────────────────────────────────┐
    │        AWS Lambda (Main Orchestrator) │
    │--------------------------------------│
    │  1. Intent Detection (LLM)           │
    │  2. SQL Generation (LLM)             │
    │  3. SQL Validation Layer             │
    │  4. Query Execution Call             │
    │  5. Response Formatting (LLM)        │
    └─────────────┬────────────────────────┘
                  │ Invoke
                  ▼
    ┌────────────────────────────────────────┐
    │  Generic Query Executor Lambda        │
    │--------------------------------------│
    │  - PostgreSQL Connection Pool        │
    │  - Secrets Manager Integration       │
    │  - SQL Execution                     │
    └─────────────┬────────────────────────┘
                  │
                  ▼
    ┌────────────────────────────────────────┐
    │         PostgreSQL Database           │
    │  (customers + orders schema)         │
    └────────────────────────────────────────┘

---

## ⚙️ Services Breakdown

### 1. LLM Layer (OpenAI GPT)
Responsible for:
- Intent classification (SELECT / INSERT / UPDATE / DELETE)
- SQL generation from natural language
- Business-friendly response formatting

Example:
Input: "Who placed the most expensive order?"
Output SQL:
SELECT o.order_id, o.total_amount
FROM demo.orders o
ORDER BY o.total_amount DESC
LIMIT 1;

---

### 2. ⚡ AWS Lambda (Main Orchestrator)

Handles:
- Request parsing
- Secret retrieval from AWS Secrets Manager
- LLM orchestration
- SQL validation
- Response formatting

Key Features:
- Structured logging (UTC timestamps)
- Error tracing with stack traces
- Cached secrets for performance

---

### 3. 🔐 AWS Secrets Manager

Stores securely:
- OPENAI_API_KEY
- Database credentials
- Lambda function references
- Model configuration

Secret Name:
llm-secrets


---

### 4. 🗄️ Generic Query Executor Lambda

Responsible for:
- Executing SQL queries
- Connecting to PostgreSQL (RDS)
- Returning structured results

Features:
- Connection pooling
- Secure credential fetching
- Timeout handling
- Error reporting

---

### 5. 🐘 PostgreSQL Database

Schema:
demo.customers
demo.orders

Relationships:
customers.customer_id → orders.customer_id


---

### 6. 💬 Chainlit Chat UI

Frontend chat interface:
- Sends user query to Lambda URL
- Receives structured response
- Displays natural language output

Example Flow:
User: Who is dvsgenai?
Bot: dvsgenai is a customer with ID CUST001...

---

## 🔁 End-to-End Flow Example

### User Query:
What is the most expensive order?

### Step 1 – Intent Detection:
SELECT

### Step 2 – SQL Generation:
```sql
SELECT o.order_id, o.total_amount
FROM demo.orders o
ORDER BY o.total_amount DESC
LIMIT 1;
```
###  Step 3 – Execution Result:
ORD0000454 | 4996.60

### Step 4 – Final Response:
The most expensive order is Order ID ORD0000454 with a total amount of $4,996.60.