import psycopg2
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict
import openai
from langchain_openai import ChatOpenAI
from langchain.schema import HumanMessage
import json
import os
from psycopg2 import sql
from datetime import datetime
import logging
import re
import hashlib

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Database Schema Management API")

config_path = "./config/config.json"
table_context_path = "./config/table.txt"
try:
    with open(config_path, "r") as f:
        CONFIG = json.load(f)
    required_keys = ["domains", "sql_queries", "prompts"]
    missing_keys = [key for key in required_keys if key not in CONFIG]
    if missing_keys:
        raise ValueError(f"Missing required keys in config.json: {', '.join(missing_keys)}")
except FileNotFoundError:
    raise Exception(f"config.json not found at {config_path}. Please ensure it exists.")
except json.JSONDecodeError:
    raise Exception(f"Invalid JSON in config.json at {config_path}. Please check the file format.")
except ValueError as e:
    raise Exception(str(e))
except Exception as e:
    raise Exception(f"Error loading config.json: {str(e)}")

try:
    with open(table_context_path, "r") as f:
        lines = f.readlines()
        if not lines or not lines[0].startswith("# HASH:"):
            raise Exception("Invalid or missing table_context.txt")
        TABLE_CONTEXT = "\n".join(lines[1:]).strip()
    logger.info(f"Loaded table context from {table_context_path}")
except FileNotFoundError:
    raise Exception(f"table_context.txt not found at {table_context_path}. Run generate_context.py to create it.")
except Exception as e:
    raise Exception(f"Error loading table_context.txt: {str(e)}")

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8501"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class DBConfig(BaseModel):
    dbname: str
    user: str
    password: str
    host: str
    port: str


class ColumnSchema(BaseModel):
    column_name: str
    data_type: str
    description: Optional[str]


class TableSchema(BaseModel):
    table_name: str
    table_description: Optional[str]
    columns: List[ColumnSchema]


class GenerateDescriptionRequest(BaseModel):
    table_name: str
    schema_name: str
    domain: str


class UpdateDescriptionRequest(BaseModel):
    schema_name: str
    table_name: str
    table_description: Optional[str] = None
    column_descriptions: Optional[dict] = None


class QueryRequest(BaseModel):
    query: str
    domain: Optional[str] = None  # Added to support description generation
    execute: Optional[bool] = False


class ExecuteQueryRequest(BaseModel):
    db_config: DBConfig
    query: str


class SummarizeQueryRequest(BaseModel):
    results: List[dict]


def create_audit_table(db_config: DBConfig):
    try:
        conn = psycopg2.connect(**db_config.dict())
        cursor = conn.cursor()
        create_table_query = """
        CREATE TABLE IF NOT EXISTS ecommerce.schema_audit (
            id SERIAL PRIMARY KEY,
            table_name VARCHAR(255) NOT NULL,
            event_type VARCHAR(50) NOT NULL,
            changes JSONB NOT NULL,
            event_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """
        cursor.execute(create_table_query)
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating audit table: {str(e)}")


def fetch_audit_logs(db_config: DBConfig, limit: int = 100, offset: int = 0):
    try:
        conn = psycopg2.connect(**db_config.dict())
        cursor = conn.cursor()
        query = """
        SELECT id, table_name, event_type, changes, event_date
        FROM ecommerce.schema_audit
        ORDER BY event_date DESC
        LIMIT %s OFFSET %s;
        """
        cursor.execute(query, (limit, offset))
        results = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        formatted_results = [dict(zip(columns, row)) for row in results]

        cursor.execute("SELECT COUNT(*) FROM ecommerce.schema_audit;")
        total_count = cursor.fetchone()[0]

        cursor.close()
        conn.close()
        return {"logs": formatted_results, "total_count": total_count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching audit logs: {str(e)}")


def fetch_table_schema(db_config: DBConfig, schema_name: str, table_name: str) -> tuple:
    try:
        conn = psycopg2.connect(**db_config.dict())
        cursor = conn.cursor()
        cursor.execute(CONFIG["sql_queries"]["schema_query"], (schema_name, table_name))
        schema = [
            {"column_name": col, "data_type": dtype, "description": desc if desc else None}
            for col, dtype, desc in cursor.fetchall()
        ]
        cursor.execute(CONFIG["sql_queries"]["table_description_query"], (schema_name, table_name))
        table_description = cursor.fetchone()
        table_description = table_description[0] if table_description and table_description[0] else None
        cursor.close()
        conn.close()
        return schema, table_description
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching schema for {schema_name}.{table_name}: {str(e)}")


def validate_columns(db_config: DBConfig, query: str, table: str) -> dict:
    try:
        schema_name, table_name = table.split('.')
        schema, _ = fetch_table_schema(db_config, schema_name, table_name)
        column_names = {col["column_name"] for col in schema}
        found_columns = set(re.findall(r'\b\w+\b(?=\s*[,>\=<])|\b\w+\b(?=\s*\))', query))
        missing_columns = found_columns - column_names - {'AVG', 'SELECT', 'FROM', 'WHERE', 'AND', 'OR'}
        if missing_columns:
            return {
                "valid": False,
                "missing_columns": list(missing_columns),
                "suggestion": f"Columns {missing_columns} not found in {table}. Check table schema or use ecommerce.order_payments for payment-related columns."
            }
        return {"valid": True}
    except Exception as e:
        return {"valid": False, "suggestion": f"Error validating columns for {table}: {str(e)}"}


def update_descriptions(db_config: DBConfig, schema_name: str, table_name: str,
                        table_description: Optional[str], column_descriptions: Optional[dict]):
    try:
        conn = psycopg2.connect(**db_config.dict())
        cursor = conn.cursor()
        create_audit_table(db_config)
        current_schema, current_table_description = fetch_table_schema(db_config, schema_name, table_name)
        current_column_descriptions = {col["column_name"]: col["description"] for col in current_schema}
        if table_description and table_description != current_table_description:
            query = sql.SQL("COMMENT ON TABLE {}.{} IS %s;").format(
                sql.Identifier(schema_name),
                sql.Identifier(table_name)
            )
            logger.info(f"Executing SQL: {query.as_string(conn)} with description: {table_description}")
            cursor.execute(query, (table_description,))
            audit_changes = {
                "old_description": current_table_description,
                "new_description": table_description
            }
            cursor.execute(
                """
                INSERT INTO ecommerce.schema_audit (table_name, event_type, changes, event_date)
                VALUES (%s, %s, %s, %s);
                """,
                (f"{schema_name}.{table_name}", "UPDATE_TABLE_DESCRIPTION", json.dumps(audit_changes), datetime.now())
            )
        if column_descriptions:
            for column_name, description in column_descriptions.items():
                current_desc = current_column_descriptions.get(column_name)
                if description != current_desc:
                    query = sql.SQL("COMMENT ON COLUMN {}.{}.{} IS %s;").format(
                        sql.Identifier(schema_name),
                        sql.Identifier(table_name),
                        sql.Identifier(column_name)
                    )
                    logger.info(f"Executing SQL: {query.as_string(conn)} with description: {description}")
                    cursor.execute(query, (description,))
                    audit_changes = {
                        "column_name": column_name,
                        "old_description": current_desc,
                        "new_description": description
                    }
                    cursor.execute(
                        """
                        INSERT INTO ecommerce.schema_audit (table_name, event_type, changes, event_date)
                        VALUES (%s, %s, %s, %s);
                        """,
                        (f"{schema_name}.{table_name}", "UPDATE_COLUMN_DESCRIPTION", json.dumps(audit_changes),
                         datetime.now())
                    )
        conn.commit()
        cursor.close()
        conn.close()
        return {"status": "success", "message": f"Descriptions updated for {schema_name}.{table_name}"}
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"Error updating descriptions for {schema_name}.{table_name}: {str(e)}")


def execute_query(db_config: DBConfig, query: str, tables: List[str]) -> dict:
    try:
        for table in tables:
            validation_result = validate_columns(db_config, query, table)
            if not validation_result["valid"]:
                raise HTTPException(status_code=400, detail=validation_result["suggestion"])
        conn = psycopg2.connect(**db_config.dict())
        cursor = conn.cursor()
        cursor.execute(query)
        results = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        formatted_results = [dict(zip(columns, row)) for row in results]
        cursor.close()
        conn.close()
        return {"results": formatted_results}
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"Error executing query: {str(e)}. Ensure columns and tables are correct (e.g., use ecommerce.order_payments for payment_value).")


def summarize_results(results: List[dict]) -> str:
    try:
        openai.api_key = os.getenv("OPENAI_API_KEY")
        if not openai.api_key:
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY environment variable not set")
        llm = ChatOpenAI(model_name="gpt-4", temperature=0.2)
        summary_prompt = f"""
        Summarize the query results: {json.dumps(results, indent=2)}
        - Highlight key insights (e.g., row count, significant values).
        - Keep it concise and user-friendly.
        Return the summary as plain text.
        """
        response = llm.invoke([HumanMessage(content=summary_prompt)])
        return response.content.strip()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error summarizing results: {str(e)}")


def generate_table_description(table_name: str, schema_name: str, domain: str) -> str:
    try:
        openai.api_key = os.getenv("OPENAI_API_KEY")
        if not openai.api_key:
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY environment variable not set")
        llm = ChatOpenAI(model_name="gpt-4", temperature=0.2)
        prompt = CONFIG["prompts"]["table_description"].format(
            domain_context=CONFIG["domains"].get(domain, {}).get("context", "No domain context available"),
            schema_name=schema_name,
            table_name=table_name
        )
        response = llm.invoke([HumanMessage(content=prompt)])
        return response.content.strip()
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"Error generating description for {schema_name}.{table_name}: {str(e)}")


def generate_column_description(column_name: str, data_type: str, table_name: str, schema_name: str,
                                domain: str) -> str:
    try:
        openai.api_key = os.getenv("OPENAI_API_KEY")
        llm = ChatOpenAI(model_name="gpt-4", temperature=0.2)
        prompt = CONFIG["prompts"]["column_description"].format(
            domain_context=CONFIG["domains"].get(domain, {}).get("context", "No domain context available"),
            schema_name=schema_name,
            table_name=table_name,
            column_name=column_name,
            data_type=data_type
        )
        response = llm.invoke([HumanMessage(content=prompt)])
        return response.content.strip()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating column description for {column_name}: {str(e)}")


def validate_query(query: str) -> str:
    try:
        openai.api_key = os.getenv("OPENAI_API_KEY")
        if not openai.api_key:
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY environment variable not set")
        llm = ChatOpenAI(model_name="gpt-4", temperature=0.2)
        validation_prompt = f"""
        Validate the following query or question: '{query}'
        - If it's a valid SQL query with schema-qualified table names (e.g., ecommerce.customers), return it unchanged.
        - If it's a natural language question or a SQL query without schema names, rephrase it into a valid SQL query using schema-qualified table names from the context below.
        - Select tables that contain the relevant columns (e.g., for 'payment_value' or 'payment_type', use ecommerce.order_payments).
        - Avoid using SELECT *; instead, specify relevant columns (e.g., order_id, payment_value).
        - If it's invalid or unsafe (e.g., DROP, DELETE), suggest a safe alternative or clarify the intent.
        - Use the following table context to select the correct tables:
        {TABLE_CONTEXT}
        Return a JSON object: {{"query": "..."}}.
        Ensure the output is strictly a JSON object, wrapped in ```json\n{{...}}\n```, with no additional text or explanations.
        """
        print("###########################")
        print(f"Step1: validation_prompt {validation_prompt}")
        response = llm.invoke([HumanMessage(content=validation_prompt)])
        response_content = response.content.strip()
        logger.info(f"LLM response for validate_query: {response_content}")
        json_match = re.search(r'```json\n(\{.*?\})\n```', response_content, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
            try:
                result = json.loads(json_str)
                validated_query = result["query"]
                print("###########################")
                print(f"output of Step1 : {validated_query}")
                return validated_query
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse JSON from LLM response: {json_str}")
                raise HTTPException(status_code=500, detail=f"Error parsing validated query: {str(e)}")
        else:
            logger.error(f"No JSON found in LLM response: {response_content}")
            raise HTTPException(status_code=500, detail="No JSON found in validated query response")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error validating query: {str(e)}")


def extract_table_names(query: str) -> List[str]:
    try:
        openai.api_key = os.getenv("OPENAI_API_KEY")
        if not openai.api_key:
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY environment variable not set")
        llm = ChatOpenAI(model_name="gpt-4", temperature=0.2)
        prompt = f"""
        Analyze the query: '{query}'
        Extract all table names referenced, including schema names if present (e.g., ecommerce.customers).
        Use the following context to identify valid tables:
        {TABLE_CONTEXT}
        Return a JSON object: {{"tables": ["schema.table", ...]}}.
        Ensure the output is strictly a JSON object, wrapped in ```json\n{{...}}\n```, with no additional text or explanations.
        """
        print("###########################")
        print(f"Step2: extract_table_names {prompt}")
        response = llm.invoke([HumanMessage(content=prompt)])
        response_content = response.content.strip()
        logger.info(f"LLM response for extract_table_names: {response_content}")
        if not response_content:
            logger.warning("Empty LLM response for table extraction")
            return []
        json_match = re.search(r'```json\n(\{.*?\})\n```', response_content, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
            try:
                result = json.loads(json_str)
                print("###########################")
                print(f"Step2: extract_table_names output {result}")
                return result.get("tables", [])
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse JSON from LLM response: {json_str}")
                return []
        else:
            logger.error(f"No JSON found in LLM response: {response_content}")
            return []
    except Exception as e:
        logger.error(f"Error in extract_table_names: {str(e)}")
        return []


def decompose_query(query: str) -> dict:
    try:
        openai.api_key = os.getenv("OPENAI_API_KEY")
        if not openai.api_key:
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY environment variable not set")
        llm = ChatOpenAI(model_name="gpt-4", temperature=0.2)
        prompt = f"""
        Analyze the SQL query: '{query}'
        - If it's simple (e.g., single SELECT without subqueries), return it as is with no sub-queries.
        - If it's complex (e.g., multiple joins, subqueries), decompose it into simpler sub-queries.
        - Ensure all table names in sub-queries and final query are schema-qualified (e.g., ecommerce.order_payments).
        - Do not use placeholders (e.g., ?) in sub-queries; ensure they are executable SQL.
        - Use the following table context to select the correct tables:
        {TABLE_CONTEXT}
        Return a JSON object: {{"sub_queries": ["...", "..."], "final_query": "..."}}.
        Ensure the output is strictly a JSON object, wrapped in ```json\n{{...}}\n```, with no additional text or explanations.
        """
        print("###########################")
        print(f"Step4: decompose_query {prompt}")
        response = llm.invoke([HumanMessage(content=prompt)])
        response_content = response.content.strip()
        logger.info(f"LLM response for decompose_query: {response_content}")
        print(f"Step4: decompose_query output {response_content}")
        print("###########################")
        json_match = re.search(r'```json\n(\{.*?\})\n```', response_content, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
            try:
                result = json.loads(json_str)
                return result
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse JSON from LLM response: {json_str}")
                return {"sub_queries": [], "final_query": query}
        else:
            logger.error(f"No JSON found in LLM response: {response_content}")
            return {"sub_queries": [], "final_query": query}
    except Exception as e:
        logger.error(f"Error in decompose_query: {str(e)}")
        return {"sub_queries": [], "final_query": query}


def validate_user_question(question: str) -> Dict[str, str]:
    try:
        openai.api_key = os.getenv("OPENAI_API_KEY")
        if not openai.api_key:
            raise Exception("OPENAI_API_KEY environment variable not set")
        llm = ChatOpenAI(model_name="gpt-4", temperature=0.2)

        # Step 1: Validate and rephrase user question
        greeting_keywords = {'hello', 'hi', 'hey', 'good morning', 'good afternoon', 'good evening', 'thank you', 'thanks', 'bye'}
        question_lower = question.lower().strip()
        if any(keyword in question_lower for keyword in greeting_keywords) or len(question_lower.split()) < 3:
            return {"error": "The input appears to be a greeting or out of context for the e-commerce domain. Please provide a relevant question related to the database."}

        validation_prompt = f"""
        Validate the following user question: '{question}'
        - If the question is a greeting or unrelated to the e-commerce domain, return a JSON object: {{"error": "The question is out of context for the e-commerce domain."}}
        - If the question contains spelling or grammatical errors, rephrase it into a clear, grammatically correct question while preserving the original intent.
        - Ensure the question is relevant to the e-commerce domain using the following table context:
        {TABLE_CONTEXT}
        - Return a JSON object: {{"rephrased_question": "..."}} or {{"error": "..."}}.
        - The output MUST be a valid JSON object wrapped in triple backticks with the 'json' language identifier, like this:
        ```json
        {{"rephrased_question": "your rephrased question here"}}
        ```
        - Do NOT include any additional text, explanations, or comments outside the JSON object and backticks.
        """
        response = llm.invoke([HumanMessage(content=validation_prompt)])
        response_content = response.content.strip()
        logger.info(f"LLM response for validate_user_question: {response_content}")

        # Try regex parsing first
        json_match = re.search(r'```json\n(\{.*?\})\n```', response_content, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
            try:
                result = json.loads(json_str)
                return result
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse JSON from LLM response: {json_str}")
                raise HTTPException(status_code=500, detail=f"Error parsing validated question: {str(e)}")
        else:
            # Fallback: Try parsing response_content as JSON directly
            logger.warning(f"No JSON backticks found in LLM response, attempting direct JSON parsing: {response_content}")
            try:
                result = json.loads(response_content)
                return result
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse direct JSON from LLM response: {response_content}")
                raise HTTPException(status_code=500, detail=f"No JSON found in validated question response: {str(e)}")
    except Exception as e:
        logger.error(f"Error validating user question: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error validating user question: {str(e)}")

def extract_table_names_from_question(question: str) -> List[str]:
    try:
        openai.api_key = os.getenv("OPENAI_API_KEY")
        if not openai.api_key:
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY environment variable not set")
        llm = ChatOpenAI(model_name="gpt-4", temperature=0.2)
        prompt = f"""
        Analyze the following user question: '{question}'
        - Identify all table names referenced in the question that are relevant to the e-commerce domain.
        - Use schema-qualified table names (e.g., ecommerce.customers) based on the following table context:
        {TABLE_CONTEXT}
        - If no tables can be identified or the question is unclear, return an empty list.
        - Return a JSON object: {{"tables": ["schema.table", ...]}}.
        Ensure the output is strictly a JSON object, wrapped in ```json\n{{...}}\n```, with no additional text or explanations.
        """
        response = llm.invoke([HumanMessage(content=prompt)])
        response_content = response.content.strip()
        logger.info(f"LLM response for extract_table_names_from_question: {response_content}")
        if not response_content:
            logger.warning("Empty LLM response for table extraction")
            return []
        json_match = re.search(r'```json\n(\{.*?\})\n```', response_content, re.DOTALL)
        if not json_match:
            logger.error(f"No JSON found in LLM response: {response_content}")
            raise HTTPException(status_code=500, detail="No JSON found in table extraction response")
        json_str = json_match.group(1)
        try:
            result = json.loads(json_str)
            tables = result.get("tables", [])
            # Validate tables against CONFIG
            valid_tables = sum([CONFIG["domains"][d]["tables"] for d in CONFIG["domains"]], [])
            invalid_tables = [t for t in tables if t not in valid_tables]
            if invalid_tables:
                logger.error(f"Invalid tables referenced: {invalid_tables}")
                raise HTTPException(status_code=400, detail=f"Invalid tables referenced: {invalid_tables}")
            return tables
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON from LLM response: {json_str}")
            raise HTTPException(status_code=500, detail=f"Error parsing table extraction response: {str(e)}")
    except Exception as e:
        logger.error(f"Error in extract_table_names_from_question: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error extracting table names: {str(e)}")


def retrieve_table_schemas(db_config: DBConfig, tables: List[str], domain: str) -> dict:
    try:
        # Step 2: Check if tables were identified
        if not tables:
            return {
                "error": "The query cannot be served as no tables could be identified from the question."
            }

        # Step 3: Retrieve columns, data types, and descriptions for each table
        table_info = []
        for table in tables:
            schema_name, table_name = table.split('.')
            # Validate table against CONFIG
            if not any(table in CONFIG["domains"][d]["tables"] for d in CONFIG["domains"]):
                raise HTTPException(status_code=400, detail=f"Invalid table: {table}")

            schema, table_description = fetch_table_schema(db_config, schema_name, table_name)
            # Generate table description if missing
            if not table_description:
                table_description = generate_table_description(table_name, schema_name, domain)

            # Generate column descriptions if missing
            for column in schema:
                if not column["description"]:
                    column["description"] = generate_column_description(
                        column["column_name"], column["data_type"], table_name, schema_name, domain
                    )

            table_info.append({
                "table_name": table,
                "table_description": table_description,
                "columns": [
                    {
                        "column_name": col["column_name"],
                        "data_type": col["data_type"],
                        "description": col["description"]
                    } for col in schema
                ]
            })

        return {"tables": tables, "table_info": table_info}

    except Exception as e:
        logger.error(f"Error in retrieve_table_schemas: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error retrieving table schemas: {str(e)}")

def decompose_question(rephrased_question: str, tables: List[str], table_schemas: List[dict]) -> dict:
    try:
        openai.api_key = os.getenv("OPENAI_API_KEY")
        if not openai.api_key:
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY environment variable not set")
        llm = ChatOpenAI(model_name="gpt-4", temperature=0.2)

        # Prepare schema context for the prompt
        schema_context = ""
        for table_info in table_schemas:
            table_name = table_info["table_name"]
            table_description = table_info["table_description"]
            columns = table_info["columns"]
            schema_context += f"Table: {table_name}\nDescription: {table_description}\nColumns:\n"
            for col in columns:
                schema_context += f"- {col['column_name']} ({col['data_type']}): {col['description']}\n"
            schema_context += "\n"

        prompt = f"""
        Analyze the following user question: '{rephrased_question}'
        - Determine if the question requires multiple SQL queries (e.g., involves multiple tables or distinct analyses).
        - If multiple queries are needed, decompose the question into simpler sub-questions, each addressing a specific part of the query.
        - For each sub-question (or the single question if no decomposition is needed), generate a valid SQL SELECT query.
        - Use schema-qualified table names (e.g., ecommerce.customers) and relevant columns based on the provided schema:
        {schema_context}
        - Ensure queries are safe (SELECT only, no DROP, DELETE, UPDATE, INSERT).
        - Use the following table context to validate table names:
        {TABLE_CONTEXT}
        - If the question cannot be translated into valid SQL, return a JSON object: {{"error": "The question cannot be translated into a valid SQL query."}}
        - Return a JSON object: {{"sub_questions": ["...", "..."], "sub_queries": ["...", "..."], "final_query": "..."}}.
        - If no decomposition is needed, set sub_questions and sub_queries to empty lists and provide the single query in final_query.
        Ensure the output is strictly a JSON object, wrapped in ```json\n{{...}}\n```, with no additional text or explanations.
        """
        response = llm.invoke([HumanMessage(content=prompt)])
        response_content = response.content.strip()
        logger.info(f"LLM response for decompose_question: {response_content}")
        json_match = re.search(r'```json\n(\{.*?\})\n```', response_content, re.DOTALL)
        if not json_match:
            logger.error(f"No JSON found in LLM response: {response_content}")
            raise HTTPException(status_code=500, detail="No JSON found in question decomposition response")
        json_str = json_match.group(1)
        try:
            result = json.loads(json_str)
            # Validate table names in generated queries
            valid_tables = sum([CONFIG["domains"][d]["tables"] for d in CONFIG["domains"]], [])
            for query in result.get("sub_queries", []) + [result.get("final_query", "")]:
                if query:
                    query_tables = extract_table_names(query)  # Assumes extract_table_names from original code
                    invalid_tables = [t for t in query_tables if t not in valid_tables]
                    if invalid_tables:
                        raise HTTPException(status_code=400, detail=f"Invalid tables in generated query: {invalid_tables}")
                    # Ensure query is SELECT only
                    if not query.strip().upper().startswith("SELECT"):
                        raise HTTPException(status_code=400, detail="Generated query is not a safe SELECT query")
            return result
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON from LLM response: {json_str}")
            raise HTTPException(status_code=500, detail=f"Error parsing question decomposition response: {str(e)}")
    except Exception as e:
        logger.error(f"Error in decompose_question: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error decomposing question: {str(e)}")



@app.post("/schema/extract", response_model=List[TableSchema])
async def extract_schema(db_config: DBConfig, domain: str, table: Optional[str] = None):
    if domain not in CONFIG["domains"]:
        raise HTTPException(status_code=400, detail=f"Invalid domain: {domain}")
    table_list = [table] if table else CONFIG["domains"][domain]["tables"]
    if table and table not in CONFIG["domains"][domain]["tables"]:
        raise HTTPException(status_code=400, detail=f"Invalid table: {table}")
    output_schemas = []
    for table in table_list:
        schema_name, table_name = table.split('.')
        schema, table_description = fetch_table_schema(db_config, schema_name, table_name)
        if not table_description:
            table_description = generate_table_description(table_name, schema_name, domain)
        for column in schema:
            if not column["description"]:
                column["description"] = generate_column_description(
                    column["column_name"], column["data_type"], table_name, schema_name, domain
                )
        output_schemas.append({
            "table_name": f"{schema_name}.{table_name}",
            "table_description": table_description,
            "columns": [ColumnSchema(**col) for col in schema]
        })
    return output_schemas


@app.post("/description/generate", response_model=dict)
async def generate_description(request: GenerateDescriptionRequest):
    if request.domain not in CONFIG["domains"]:
        raise HTTPException(status_code=400, detail=f"Invalid domain: {request.domain}")
    if f"{request.schema_name}.{request.table_name}" not in CONFIG["domains"][request.domain]["tables"]:
        raise HTTPException(status_code=400, detail=f"Invalid table: {request.schema_name}.{request.table_name}")
    description = generate_table_description(request.table_name, request.schema_name, request.domain)
    return {"table_name": f"{request.schema_name}.{request.table_name}", "description": description}


@app.post("/description/generate_full", response_model=TableSchema)
async def generate_full_description(db_config: DBConfig, request: GenerateDescriptionRequest):
    if request.domain not in CONFIG["domains"]:
        raise HTTPException(status_code=400, detail=f"Invalid domain: {request.domain}")
    if f"{request.schema_name}.{request.table_name}" not in CONFIG["domains"][request.domain]["tables"]:
        raise HTTPException(status_code=400, detail=f"Invalid table: {request.schema_name}.{request.table_name}")
    schema, table_description = fetch_table_schema(db_config, request.schema_name, request.table_name)
    if not table_description:
        table_description = generate_table_description(request.table_name, request.schema_name, request.domain)
    for column in schema:
        if not column["description"]:
            column["description"] = generate_column_description(
                column["column_name"], column["data_type"], request.table_name, request.schema_name, request.domain
            )
    return {
        "table_name": f"{request.schema_name}.{request.table_name}",
        "table_description": table_description,
        "columns": [ColumnSchema(**col) for col in schema]
    }


@app.post("/description/update", response_model=dict)
async def update_description(db_config: DBConfig, request: UpdateDescriptionRequest):
    if f"{request.schema_name}.{request.table_name}" not in sum(
            [CONFIG["domains"][d]["tables"] for d in CONFIG["domains"]], []):
        raise HTTPException(status_code=400, detail=f"Invalid table: {request.schema_name}.{request.table_name}")
    result = update_descriptions(
        db_config, request.schema_name, request.table_name,
        request.table_description, request.column_descriptions
    )
    return result


@app.post("/query/process_user_query", response_model=dict)
async def process_user_query(db_config: DBConfig, request: QueryRequest):
    try:
        # Step 1: Validate and rephrase the user question
        validation_result = validate_user_question(request.query)
        if "error" in validation_result:
            return {"error": validation_result["error"]}
        rephrased_question = validation_result["rephrased_question"]

        # Step 2: Extract table names from the rephrased question
        tables = extract_table_names_from_question(rephrased_question)

        # Step 3: Retrieve table schemas
        # Use request.domain if provided, else infer from CONFIG
        domain = request.domain
        if not domain:
            for d in CONFIG["domains"]:
                if any(t in CONFIG["domains"][d]["tables"] for t in tables):
                    domain = d
                    break
            if not domain:
                domain = list(CONFIG["domains"].keys())[0]  # Fallback to first domain
        schema_result = retrieve_table_schemas(db_config, tables, domain)
        if "error" in schema_result:
            return schema_result
        table_info = schema_result["table_info"]

        # Step 4: Decompose question and generate SQL queries
        decomposition_result = decompose_question(rephrased_question, tables, table_info)
        if "error" in decomposition_result:
            return decomposition_result

        # Prepare response
        response = {
            "rephrased_question": rephrased_question,
            "tables": tables,
            "table_info": table_info,
            "sub_questions": decomposition_result["sub_questions"],
            "sub_queries": decomposition_result["sub_queries"],
            "final_query": decomposition_result["final_query"]
        }
        logger.info(f"response from process_user_query: {response}")
        return response

    except Exception as e:
        logger.error(f"Error in process_user_query: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error processing user query: {str(e)}")


@app.post("/query/execute", response_model=dict)
async def execute_query_endpoint(request: ExecuteQueryRequest):
    tables = extract_table_names(request.query)
    return execute_query(request.db_config, request.query, tables)


@app.post("/query/summarize", response_model=dict)
async def summarize_query(request: SummarizeQueryRequest):
    try:
        summary = summarize_results(request.results)
        return {"summary": summary}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error summarizing query results: {str(e)}")


@app.post("/audit/logs", response_model=dict)
async def get_audit_logs(db_config: DBConfig, limit: int = 100, offset: int = 0):
    return fetch_audit_logs(db_config, limit, offset)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8002)