import traceback

import psycopg2
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
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
from fastapi.middleware.cors import CORSMiddleware
from functools import lru_cache
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Database Schema Management API")

# Global cache for generated descriptions to avoid redundant LLM calls
DESCRIPTION_CACHE = {}
QUERY_CACHE = {}

# Load configuration
config_path = "./config/config.json"
table_context_path = "./config/table.txt"
logger.info(f"Starting configuration loading from {config_path}")
try:
    logger.info(f"Attempting to load config from {config_path}")
    with open(config_path, "r") as f:
        CONFIG = json.load(f)
    logger.info("Config file loaded successfully")

    required_keys = ["domains", "sql_queries", "prompts"]
    logger.info(f"Checking for required keys: {required_keys}")
    missing_keys = [key for key in required_keys if key not in CONFIG]
    if missing_keys:
        logger.error(f"Missing required keys in config.json: {', '.join(missing_keys)}")
        raise ValueError(f"Missing required keys in config.json: {', '.join(missing_keys)}")
    logger.info("All required keys found in config")
except FileNotFoundError:
    logger.error(f"config.json not found at {config_path}")
    raise Exception(f"config.json not found at {config_path}. Please ensure it exists.")
except json.JSONDecodeError:
    logger.error(f"Invalid JSON in config.json at {config_path}")
    raise Exception(f"Invalid JSON in config.json at {config_path}. Please check the file format.")
except ValueError as e:
    logger.error(f"ValueError in config loading: {str(e)}")
    raise Exception(str(e))
except Exception as e:
    logger.error(f"Unexpected error loading config.json: {str(e)}")
    raise Exception(f"Error loading config.json: {str(e)}")

# Load table context
logger.info(f"Starting table context loading from {table_context_path}")
try:
    logger.info(f"Attempting to load table context from {table_context_path}")
    with open(table_context_path, "r") as f:
        lines = f.readlines()
        logger.info(f"Read {len(lines)} lines from table context file")
        if not lines or not lines[0].startswith("# HASH:"):
            logger.error("Invalid or missing table_context.txt - no hash header found")
            raise Exception("Invalid or missing table_context.txt")
        TABLE_CONTEXT = "\n".join(lines[1:]).strip()
    logger.info(f"Loaded table context from {table_context_path} successfully")
except FileNotFoundError:
    logger.error(f"table_context.txt not found at {table_context_path}")
    raise Exception(f"table_context.txt not found at {table_context_path}. Run generate_context.py to create it.")
except Exception as e:
    logger.error(f"Error loading table_context.txt: {str(e)}")
    raise Exception(f"Error loading table_context.txt: {str(e)}")

# CORS middleware
logger.info("Setting up CORS middleware")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8501"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger.info("CORS middleware configured")


# Pydantic models (unchanged)
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
    domain: Optional[str] = None
    execute: Optional[bool] = False


class ExecuteQueryRequest(BaseModel):
    db_config: DBConfig
    query: str


class SummarizeQueryRequest(BaseModel):
    results: Optional[List[Dict[str, Any]]] = None
    prompt: Optional[str] = None
    user_question: Optional[str] = None
    rephrased_question: Optional[str] = None


# Utility functions
def get_cache_key(func_name: str, *args) -> str:
    """Generate a cache key for function calls"""
    logger.debug(f"Generating cache key for function: {func_name} with args: {args}")
    cache_key = hashlib.md5(f"{func_name}_{str(args)}".encode()).hexdigest()
    logger.debug(f"Generated cache key: {cache_key}")
    return cache_key


@lru_cache(maxsize=128)
def get_llm_client():
    """Cached LLM client to avoid recreation"""
    logger.info("Creating LLM client")
    openai.api_key = os.getenv("OPENAI_API_KEY")
    if not openai.api_key:
        logger.error("OPENAI_API_KEY environment variable not set")
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY environment variable not set")
    logger.info("LLM client created successfully")
    return ChatOpenAI(model_name="gpt-4-turbo", seed=42,temperature=0.0)


def parse_llm_json_response(response_content: str) -> dict:
    """Unified JSON parsing for LLM responses with robust multi-line SQL handling"""
    logger.debug(f"Parsing LLM JSON response: {response_content[:200]}...")

    # First try to find JSON in code blocks
    json_match = re.search(r'```json\n(\{.*?\})\n```', response_content, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
        logger.debug(f"Found JSON in code block: {json_str[:100]}...")

        try:

            # Find all string values that contain newlines
            def escape_sql_newlines(match):
                full_match = match.group(0)
                key = match.group(1)
                value = match.group(2)
                # Escape newlines and other control characters in the SQL value
                escaped_value = value.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t').replace('"', '\\"')
                return f'"{key}": "{escaped_value}"'

            # Pattern to match key-value pairs where value contains newlines
            pattern = r'"([^"]+)":\s*"([^"]*(?:\n[^"]*)*)"'
            cleaned_json_str = re.sub(pattern, escape_sql_newlines, json_str, flags=re.DOTALL)

            logger.debug(f"Cleaned JSON: {cleaned_json_str[:200]}...")
            result = json.loads(cleaned_json_str)
            logger.debug("Successfully parsed cleaned JSON from code block")

            # Unescape the SQL in the result
            for key, value in result.items():
                if isinstance(value, str) and ('\\n' in value or '\\t' in value):
                    result[key] = value.replace('\\n', '\n').replace('\\r', '\r').replace('\\t', '\t').replace('\\"',
                                                                                                               '"')

            return result

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse cleaned JSON: {str(e)}")

            # Fallback: Try to manually extract and reconstruct the JSON
            try:
                # Extract individual fields manually
                result = {}

                # Look for common fields
                fields_to_extract = ['query', 'final_query', 'error', 'decomposition_needed', 'sub_questions',
                                     'sub_queries']

                for field in fields_to_extract:
                    # Pattern to match field with potentially multi-line value
                    field_pattern = rf'"{field}":\s*"([^"]*(?:\n[^"]*)*)"'
                    field_match = re.search(field_pattern, json_str, re.DOTALL)
                    if field_match:
                        result[field] = field_match.group(1)
                    else:
                        # Try without quotes (for boolean/array values)
                        field_pattern_unquoted = r'"' + field + r'":\s*([^,\}]+)'
                        field_match_unquoted = re.search(field_pattern_unquoted, json_str)
                        if field_match_unquoted:
                            value = field_match_unquoted.group(1).strip()
                            if value == 'true':
                                result[field] = True
                            elif value == 'false':
                                result[field] = False
                            elif value.startswith('[') and value.endswith(']'):
                                # Try to parse as array
                                try:
                                    result[field] = json.loads(value)
                                except:
                                    result[field] = value
                            else:
                                result[field] = value

                if result:
                    logger.debug("Successfully parsed JSON using manual extraction")
                    return result
                else:
                    raise json.JSONDecodeError("No fields extracted", json_str, 0)

            except Exception as e2:
                logger.error(f"Manual extraction also failed: {str(e2)}")
                logger.error(f"Failed to parse JSON from LLM response: {json_str}")
                raise HTTPException(status_code=500, detail=f"Error parsing LLM response: {str(e)}")
    else:
        # Try direct JSON parsing as fallback
        logger.debug("No JSON code block found, trying direct parsing")
        try:
            result = json.loads(response_content)
            logger.debug("Successfully parsed JSON directly")
            return result
        except json.JSONDecodeError as e:
            logger.error(f"No valid JSON found in LLM response: {response_content}")
            raise HTTPException(status_code=500, detail="No valid JSON found in LLM response")


# Database functions (optimized)
def create_audit_table(db_config: DBConfig):
    logger.info("Creating audit table if it doesn't exist")
    try:
        logger.debug(f"Connecting to database: {db_config.host}:{db_config.port}/{db_config.dbname}")
        conn = psycopg2.connect(**db_config.dict())
        cursor = conn.cursor()
        logger.debug("Database connection established for audit table creation")

        create_table_query = """
        CREATE TABLE IF NOT EXISTS ecommerce.schema_audit (
            id SERIAL PRIMARY KEY,
            table_name VARCHAR(255) NOT NULL,
            event_type VARCHAR(50) NOT NULL,
            changes JSONB NOT NULL,
            event_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """
        logger.debug("Executing audit table creation query")
        cursor.execute(create_table_query)
        conn.commit()
        logger.info("Audit table created/verified successfully")
        cursor.close()
        conn.close()
        logger.debug("Database connection closed after audit table creation")
    except Exception as e:
        logger.error(f"Error creating audit table: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error creating audit table: {str(e)}")


def fetch_audit_logs(db_config: DBConfig, limit: int = 100, offset: int = 0):
    logger.info(f"Fetching audit logs with limit={limit}, offset={offset}")
    try:
        logger.debug(f"Connecting to database for audit logs: {db_config.host}:{db_config.port}/{db_config.dbname}")
        conn = psycopg2.connect(**db_config.dict())
        cursor = conn.cursor()
        logger.debug("Database connection established for audit logs fetch")

        query = """
        SELECT id, table_name, event_type, changes, event_date
        FROM ecommerce.schema_audit
        ORDER BY event_date DESC
        LIMIT %s OFFSET %s;
        """
        logger.debug(f"Executing audit logs query with limit={limit}, offset={offset}")
        cursor.execute(query, (limit, offset))
        results = cursor.fetchall()
        logger.info(f"Retrieved {len(results)} audit log records")

        columns = [desc[0] for desc in cursor.description]
        formatted_results = [dict(zip(columns, row)) for row in results]
        logger.debug("Formatted audit log results")

        logger.debug("Getting total count of audit logs")
        cursor.execute("SELECT COUNT(*) FROM ecommerce.schema_audit;")
        total_count = cursor.fetchone()[0]
        logger.info(f"Total audit log count: {total_count}")

        cursor.close()
        conn.close()
        logger.debug("Database connection closed after audit logs fetch")
        return {"logs": formatted_results, "total_count": total_count}
    except Exception as e:
        logger.error(f"Error fetching audit logs: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching audit logs: {str(e)}")


from functools import lru_cache
import psycopg2
import json
from fastapi import HTTPException

def fetch_table_schema_cached(db_config_str: str, schema_name: str, table_name: str) -> tuple:
    """Cached version of fetch_table_schema to avoid repeated DB calls.
       Returns: (schema: List[dict], table_description: str, sample_rows: List[dict])
    """
    logger.debug(f"Fetching cached table schema for {schema_name}.{table_name}")
    db_config = json.loads(db_config_str)

    try:
        logger.debug(
            f"Connecting to database for schema fetch: {db_config['host']}:{db_config['port']}/{db_config['dbname']}"
        )
        conn = psycopg2.connect(**db_config)
        cursor = conn.cursor()
        logger.debug("Database connection established for schema fetch")

        # Fetch schema
        logger.debug(f"Executing schema query for {schema_name}.{table_name}")
        cursor.execute(CONFIG["sql_queries"]["schema_query"], (schema_name, table_name))
        schema_results = cursor.fetchall()
        logger.info(f"Retrieved {len(schema_results)} columns for {schema_name}.{table_name}")

        # Initialize schema dict
        schema = [
            {
                "column_name": col,
                "data_type": dtype,
                "description": desc if desc else None,
                "sample_values": []  # Placeholder
            }
            for col, dtype, desc in schema_results
        ]

        # Fetch table description
        logger.debug(f"Executing table description query for {schema_name}.{table_name}")
        cursor.execute(CONFIG["sql_queries"]["table_description_query"], (schema_name, table_name))
        table_description_result = cursor.fetchone()
        table_description = table_description_result[0] if table_description_result and table_description_result[0] else None
        logger.debug(f"Table description retrieved: {'Yes' if table_description else 'No'}")

        # Fetch 3 sample rows as dict
        sample_query = f'SELECT * FROM "{schema_name}"."{table_name}" LIMIT 3'
        logger.debug(f"Executing sample data query: {sample_query}")
        cursor.execute(sample_query)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        sample_data = [dict(zip(columns, row)) for row in rows]
        logger.info(f"Retrieved {len(sample_data)} sample rows from {schema_name}.{table_name}")

        # Map sample values per column
        for col_entry in schema:
            col_name = col_entry["column_name"]
            col_entry["sample_values"] = [
                row.get(col_name) for row in sample_data if col_name in row
            ]

        # Cleanup
        cursor.close()
        conn.close()
        logger.debug("Database connection closed after schema fetch")

        return schema, table_description, sample_data

    except Exception as e:
        logger.error(f"Error fetching schema for {schema_name}.{table_name}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching schema for {schema_name}.{table_name}: {str(e)}")


def fetch_table_schema(db_config: DBConfig, schema_name: str, table_name: str) -> tuple:
    """Wrapper for cached schema fetching"""
    logger.debug(f"Fetching table schema for {schema_name}.{table_name}")
    result = fetch_table_schema_cached(json.dumps(db_config.dict(), sort_keys=True), schema_name, table_name)
    logger.debug(f"Schema fetch completed for {schema_name}.{table_name}")
    return result


def validate_columns(db_config: DBConfig, query: str, tables: List[str]) -> dict:
    """Optimized column validation for multi-table queries"""
    logger.info(f"Validating columns for query against tables: {tables}")
    logger.debug(f"Query: {query}")
    try:
        # Collect all valid columns from all tables
        all_valid_columns = set()
        table_columns = {}

        for table in tables:
            logger.debug(f"Fetching schema for table: {table}")
            schema_name, table_name = table.split('.')
            schema, _,_ = fetch_table_schema(db_config, schema_name, table_name)
            table_column_set = {col["column_name"].lower() for col in schema}
            table_columns[table] = table_column_set
            all_valid_columns.update(table_column_set)
            logger.debug(f"Table {table} columns: {table_column_set}")

        logger.debug(f"All valid columns across tables: {all_valid_columns}")

        # Extract potential column references from query
        found_identifiers = set(re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b', query.lower()))
        logger.debug(f"Found identifiers in query: {found_identifiers}")

        # Extended SQL keywords and functions to filter out
        sql_keywords = {
            'select', 'from', 'where', 'and', 'or', 'avg', 'count', 'sum', 'max', 'min',
            'group', 'by', 'order', 'having', 'limit', 'distinct', 'as', 'join', 'inner',
            'left', 'right', 'outer', 'on', 'union', 'case', 'when', 'then', 'else', 'end',
            'with', 'asc', 'desc', 'null', 'not', 'is', 'in', 'exists', 'like', 'between',
            'date_trunc', 'lag', 'over', 'window', 'partition', 'row_number', 'rank',
            'dense_rank', 'first_value', 'last_value', 'lead', 'extract', 'cast', 'coalesce',
            'nullif', 'greatest', 'least', 'abs', 'ceil', 'floor', 'round', 'trunc',
            'month', 'year', 'day', 'hour', 'minute', 'second', 'now', 'current_date',
            'current_time', 'current_timestamp', 'interval', 'true', 'false'
        }

        # Extract table names and aliases from the query
        table_names = set()
        table_aliases = set()

        for table in tables:
            schema_name, table_name = table.split('.')
            table_names.add(table_name.lower())
            table_names.add(schema_name.lower())

        # Find table aliases (letters after table names in FROM/JOIN clauses)
        alias_patterns = [
            r'FROM\s+\w+\.\w+\s+([a-zA-Z_][a-zA-Z0-9_]*)',
            r'JOIN\s+\w+\.\w+\s+([a-zA-Z_][a-zA-Z0-9_]*)',
        ]

        for pattern in alias_patterns:
            aliases = re.findall(pattern, query, re.IGNORECASE)
            table_aliases.update([alias.lower() for alias in aliases])

        logger.debug(f"Identified table names: {table_names}")
        logger.debug(f"Identified table aliases: {table_aliases}")

        # Filter out SQL keywords, table names, aliases, and string literals
        potential_columns = found_identifiers - sql_keywords - table_names - table_aliases

        # Remove string literals and category values (things in quotes)
        string_literals = set(re.findall(r"'([^']*)'", query.lower()))
        potential_columns = potential_columns - string_literals

        logger.debug(f"Potential columns after filtering: {potential_columns}")

        # Check if potential columns exist in any of the tables
        missing_columns = potential_columns - all_valid_columns

        # Filter out obvious non-column identifiers (like aggregated column names)
        actual_missing = set()
        for col in missing_columns:
            # Skip if it looks like an alias or derived column
            if col in ['total_sales', 'number_of_orders', 'sales_difference', 'previous_month_sales']:
                continue
            # Skip single letters that are likely aliases
            if len(col) == 1:
                continue
            actual_missing.add(col)

        logger.debug(f"Actually missing columns: {actual_missing}")

        if actual_missing:
            logger.warning(f"Validation failed - missing columns: {actual_missing}")
            return {
                "valid": False,
                "missing_columns": list(actual_missing),
                "suggestion": f"Columns {actual_missing} not found in any of the tables {tables}. Available columns: {dict(table_columns)}"
            }

        logger.info("Column validation passed")
        return {"valid": True}

    except Exception as e:
        logger.error(f"Error validating columns for {tables}: {str(e)}")
        return {"valid": False, "suggestion": f"Error validating columns for {tables}: {str(e)}"}


def update_descriptions(db_config: DBConfig, schema_name: str, table_name: str,
                        table_description: Optional[str], column_descriptions: Optional[dict]):
    logger.info(f"Updating descriptions for {schema_name}.{table_name}")
    conn = None
    cursor = None
    try:
        logger.debug(
            f"Connecting to database for description update: {db_config.host}:{db_config.port}/{db_config.dbname}")
        conn = psycopg2.connect(**db_config.dict())
        # Set autocommit to see changes immediately
        conn.autocommit = False
        cursor = conn.cursor()
        logger.debug("Database connection established for description update")

        logger.debug("Creating audit table if needed")
        create_audit_table(db_config)

        logger.debug("Fetching current schema and descriptions")
        current_schema, current_table_description,_ = fetch_table_schema(db_config, schema_name, table_name)
        print("update_descriptions:current_schema:", current_schema)
        current_column_descriptions = {col["column_name"]: col["description"] for col in current_schema}
        logger.debug(f"Current table description: {'Yes' if current_table_description else 'No'}")
        logger.debug(
            f"Current column descriptions count: {len([d for d in current_column_descriptions.values() if d])}")

        changes_made = False
        failed_updates = []

        # Update table description
        if table_description and table_description != current_table_description:
            logger.info(f"Updating table description for {schema_name}.{table_name}")
            try:
                query = sql.SQL("COMMENT ON TABLE {}.{} IS %s;").format(
                    sql.Identifier(schema_name),
                    sql.Identifier(table_name)
                )
                logger.info(f"Executing SQL: {query.as_string(conn)} with description: {table_description[:50]}...")
                cursor.execute(query, (table_description,))

                # Commit this change to make it visible
                conn.commit()

                # Verify the update with a new cursor
                verify_cursor = conn.cursor()
                verify_query = """
                SELECT obj_description(c.oid)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = %s
                """
                verify_cursor.execute(verify_query, (schema_name, table_name))
                new_desc = verify_cursor.fetchone()
                new_desc = new_desc[0] if new_desc else None
                verify_cursor.close()

                if new_desc == table_description:
                    logger.info("Table description update verified successfully")

                    # Record in audit log
                    audit_changes = {
                        "old_description": current_table_description,
                        "new_description": table_description
                    }
                    logger.debug("Recording table description change in audit log")
                    cursor.execute(
                        """
                        INSERT INTO ecommerce.schema_audit (table_name, event_type, changes, event_date)
                        VALUES (%s, %s, %s, %s);
                        """,
                        (f"{schema_name}.{table_name}", "UPDATE_TABLE_DESCRIPTION", json.dumps(audit_changes),
                         datetime.now())
                    )
                    conn.commit()
                    changes_made = True
                else:
                    logger.error(
                        f"Table description update failed - expected: {table_description[:50]}..., got: {new_desc[:50] if new_desc else 'None'}")
                    failed_updates.append(f"Table description update failed")

            except Exception as e:
                logger.error(f"Error updating table description: {str(e)}")
                conn.rollback()
                failed_updates.append(f"Table description: {str(e)}")

        # Update column descriptions
        if column_descriptions:
            logger.info(f"Processing {len(column_descriptions)} column description updates")

            # Debug: Check if column names exist in the table
            logger.debug(f"Columns to update: {list(column_descriptions.keys())}")
            logger.debug(f"Existing columns: {list(current_column_descriptions.keys())}")

            for column_name, description in column_descriptions.items():
                # Check if column exists
                if column_name not in current_column_descriptions:
                    logger.error(f"Column {column_name} does not exist in table {schema_name}.{table_name}")
                    failed_updates.append(f"Column {column_name} does not exist")
                    continue

                current_desc = current_column_descriptions.get(column_name)

                # Skip if description is the same
                if description == current_desc:
                    logger.debug(f"No change needed for column {column_name}")
                    continue

                logger.info(f"Updating column description for {schema_name}.{table_name}.{column_name}")
                logger.debug(f"Current description: {current_desc}")
                logger.debug(f"New description: {description}")

                try:
                    # First verify the column exists
                    check_column_query = """
                    SELECT EXISTS (
                        SELECT 1 
                        FROM pg_attribute a
                        JOIN pg_class c ON a.attrelid = c.oid
                        JOIN pg_namespace n ON c.relnamespace = n.oid
                        WHERE n.nspname = %s
                        AND c.relname = %s
                        AND a.attname = %s
                        AND a.attnum > 0
                        AND NOT a.attisdropped
                    )
                    """
                    cursor.execute(check_column_query, (schema_name, table_name, column_name))
                    column_exists = cursor.fetchone()[0]

                    if not column_exists:
                        logger.error(f"Column {column_name} not found in database")
                        failed_updates.append(f"Column {column_name} not found in database")
                        continue

                    # Update the column description
                    query = sql.SQL("COMMENT ON COLUMN {}.{}.{} IS %s;").format(
                        sql.Identifier(schema_name),
                        sql.Identifier(table_name),
                        sql.Identifier(column_name)
                    )
                    logger.info(f"Executing SQL: {query.as_string(conn)} with description: {description[:50]}...")
                    cursor.execute(query, (description,))

                    # Commit immediately to make change visible
                    conn.commit()

                    # Verify the update with a fresh cursor
                    verify_cursor = conn.cursor()
                    verify_query = """
                    SELECT col_description(c.oid, a.attnum)
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    JOIN pg_attribute a ON a.attrelid = c.oid
                    WHERE n.nspname = %s 
                    AND c.relname = %s
                    AND a.attname = %s
                    """
                    verify_cursor.execute(verify_query, (schema_name, table_name, column_name))
                    result = verify_cursor.fetchone()
                    new_desc = result[0] if result else None
                    verify_cursor.close()

                    if new_desc == description:
                        logger.info(f"Column description update verified for {column_name}")

                        # Record in audit log
                        audit_changes = {
                            "column_name": column_name,
                            "old_description": current_desc,
                            "new_description": description
                        }
                        logger.debug(f"Recording column description change in audit log for {column_name}")
                        cursor.execute(
                            """
                            INSERT INTO ecommerce.schema_audit (table_name, event_type, changes, event_date)
                            VALUES (%s, %s, %s, %s);
                            """,
                            (f"{schema_name}.{table_name}", "UPDATE_COLUMN_DESCRIPTION",
                             json.dumps(audit_changes), datetime.now())
                        )
                        conn.commit()
                        changes_made = True
                    else:
                        logger.error(
                            f"Column {column_name} description update verification failed - "
                            f"expected: {description[:50]}..., got: {new_desc[:50] if new_desc else 'None'}")
                        failed_updates.append(f"Column {column_name} verification failed")

                except Exception as e:
                    logger.error(f"Error updating column {column_name}: {str(e)}")
                    logger.error(f"Full traceback: ", exc_info=True)
                    conn.rollback()
                    failed_updates.append(f"Column {column_name}: {str(e)}")

        # Clear cache for this table
        if changes_made:
            logger.debug("Clearing cached table schema")
            # fetch_table_schema_cached.cache_clear()
            logger.info("All changes committed and cache cleared")

        if failed_updates:
            return {
                "status": "partial_success" if changes_made else "error",
                "message": f"{'Some' if changes_made else 'Failed to update'} descriptions for {schema_name}.{table_name}",
                "failed_updates": failed_updates
            }
        elif changes_made:
            return {"status": "success", "message": f"All descriptions updated for {schema_name}.{table_name}"}
        else:
            return {"status": "success", "message": "No changes needed"}

    except Exception as e:
        logger.error(f"Error updating descriptions for {schema_name}.{table_name}: {str(e)}")
        logger.error(f"Full traceback: ", exc_info=True)
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500,
                            detail=f"Error updating descriptions for {schema_name}.{table_name}: {str(e)}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
        logger.debug("Database connection closed after description update")


def execute_query(db_config: DBConfig, query: str, tables: List[str]) -> dict:
    logger.info(f"Executing query against tables: {tables}")
    logger.debug(f"Query: {query}")
    try:
        # # Validate columns for all tables at once
        # logger.debug("Starting column validation for all tables")
        # validation_result = validate_columns(db_config, query, tables)
        # if not validation_result["valid"]:
        #     logger.error(f"Column validation failed: {validation_result['suggestion']}")
        #     raise HTTPException(status_code=400, detail=validation_result["suggestion"])
        # logger.info("Column validation passed for all tables")

        logger.debug(
            f"Connecting to database for query execution: {db_config.host}:{db_config.port}/{db_config.dbname}")
        conn = psycopg2.connect(**db_config.dict())
        cursor = conn.cursor()
        logger.debug("Database connection established for query execution")

        logger.info("Executing query")
        cursor.execute(query)
        results = cursor.fetchall()
        logger.info(f"Query executed successfully, retrieved {len(results)} rows")

        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        logger.debug(f"Query returned {len(columns)} columns: {columns}")
        formatted_results = [dict(zip(columns, row)) for row in results]
        logger.debug("Results formatted successfully")

        cursor.close()
        conn.close()
        logger.debug("Database connection closed after query execution")
        return {"results": formatted_results}
    except Exception as e:
        logger.error(f"Error executing query: {str(e)}")
        raise HTTPException(status_code=500,
                            detail=f"Error executing query: {str(e)}")


def summarize_results(results: List[dict], prompt: Optional[str] = None, user_question: Optional[str] = None,
                      rephrased_question: Optional[str] = None) -> str:
    logger.info(f"Summarizing results - {len(results)} records")
    logger.debug(f"User question: {user_question}")
    logger.debug(f"Rephrased question: {rephrased_question}")
    try:
        logger.debug("Getting LLM client for summarization")
        llm = get_llm_client()

        if prompt:
            logger.debug("Using custom prompt for summarization")
            summary_prompt = prompt
        else:
            # Limit result size to avoid token limits
            limited_results = results[:100] if len(results) > 100 else results
            logger.debug(f"Using default prompt with {len(limited_results)} results (limited from {len(results)})")

            # Build context about the user's question
            question_context = ""
            if user_question:
                question_context += f"Original User Question: {user_question}\n"
            if rephrased_question:
                question_context += f"Rephrased Question: {rephrased_question}\n"

            summary_prompt = f"""
            {question_context}
            Query Results: {json.dumps(limited_results, indent=2, default=str)}

            Analyze the query results in the context of the user's question and provide insights that directly answer their question:

            - If the question is about trends (going up/down, increasing/decreasing), analyze the data chronologically and identify patterns, trends, or changes over time.
            - If the question is about comparisons, highlight the differences and what they mean.
            - If the question is about performance or metrics, focus on the key performance indicators and what they reveal.
            - If the question asks "why" something is happening, look for patterns, correlations, or anomalies in the data that could explain the phenomenon.

            Key Analysis Points:
            - Total number of records: {len(results)}
            - Identify trends, patterns, or significant changes in the data
            - Highlight any outliers or notable data points
            - Connect the findings directly to the user's question
            - If asking about decline/growth, show the progression over time
            - Provide actionable insights or explanations based on the data

            Structure your response to:
            1. Directly address the user's question first
            2. Support your answer with specific data points from the results
            3. Highlight the most important insights
            4. Keep it concise but comprehensive

            Return the summary as plain text that clearly answers the user's question.
            """

        logger.debug("Invoking LLM for summarization")
        response = llm.invoke([HumanMessage(content=summary_prompt)])
        summary = response.content.strip()
        logger.info("Summarization completed successfully")
        logger.debug(f"Summary length: {len(summary)} characters")
        return summary
    except Exception as e:
        logger.error(f"Error summarizing results: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error summarizing results: {str(e)}")


def generate_table_description(table_name: str, schema_name: str, domain: str) -> str:
    logger.info(f"Generating table description for {schema_name}.{table_name} in domain {domain}")
    cache_key = get_cache_key("table_desc", table_name, schema_name, domain)
    if cache_key in DESCRIPTION_CACHE:
        logger.info(f"Using cached table description for {schema_name}.{table_name}")
        return DESCRIPTION_CACHE[cache_key]

    try:
        logger.debug("Getting LLM client for table description generation")
        llm = get_llm_client()

        domain_context = CONFIG["domains"].get(domain, {}).get("context", "No domain context available")
        logger.debug(f"Using domain context: {domain_context[:100]}...")

        prompt = CONFIG["prompts"]["table_description"].format(
            domain_context=domain_context,
            schema_name=schema_name,
            table_name=table_name
        )
        logger.debug("Formatted prompt for table description")

        logger.debug("Invoking LLM for table description")
        response = llm.invoke([HumanMessage(content=prompt)])
        description = response.content.strip()
        logger.info(f"Table description generated successfully for {schema_name}.{table_name}")
        logger.debug(f"Description length: {len(description)} characters")

        DESCRIPTION_CACHE[cache_key] = description
        logger.debug("Table description cached")
        return description
    except Exception as e:
        logger.error(f"Error generating description for {schema_name}.{table_name}: {str(e)}")
        raise HTTPException(status_code=500,
                            detail=f"Error generating description for {schema_name}.{table_name}: {str(e)}")


def generate_column_description(table_description:str, column_name: str, data_type: str, table_name: str, schema_name: str,
                                domain: str, sample_data:List[Any]) -> str:
    logger.info(f"Generating column description for {schema_name}.{table_name}.{column_name}")
    cache_key = get_cache_key("col_desc", column_name, data_type, table_name, schema_name, domain)
    if cache_key in DESCRIPTION_CACHE:
        logger.info(f"Using cached column description for {schema_name}.{table_name}.{column_name}")
        return DESCRIPTION_CACHE[cache_key]

    try:
        logger.info("Getting LLM client for column description generation")
        llm = get_llm_client()

        domain_context = CONFIG["domains"].get(domain, {}).get("context", "No domain context available")
        logger.info(f"Using domain context for column description")

        prompt = CONFIG["prompts"]["column_description"].format(
            table_description = table_description,
            domain_context=domain_context,
            schema_name=schema_name,
            table_name=table_name,
            column_name=column_name,
            data_type=data_type,
            sample_values = ",".join([str(value) for value in sample_data])
        )
        logger.info(f"Formatted prompt for column {column_name} description.")

        logger.debug("Invoking LLM for column description")
        response = llm.invoke([HumanMessage(content=prompt)])
        description = response.content.strip()
        logger.info(f"Column description generated successfully for {schema_name}.{table_name}.{column_name}")
        logger.debug(f"Description length: {len(description)} characters")

        DESCRIPTION_CACHE[cache_key] = description
        logger.debug("Column description cached")
        return description
    except Exception as e:
        logger.error(f"Error generating column description for {column_name}: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Error generating column description for {column_name}: {traceback.format_exc()}")


def validate_query(query: str) -> str:
    logger.info("Validating and potentially reformatting query")
    logger.debug(f"Input query: {query}")
    cache_key = get_cache_key("validate_query", query)
    if cache_key in QUERY_CACHE:
        logger.info(f"Using cached query validation")
        return QUERY_CACHE[cache_key]

    try:
        logger.debug("Getting LLM client for query validation")
        llm = get_llm_client()

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
        logger.debug("Formatted validation prompt")

        logger.debug("Invoking LLM for query validation")
        response = llm.invoke([HumanMessage(content=validation_prompt)])
        response_content = response.content.strip()
        logger.info(f"LLM response for validate_query: {response_content}")

        result = parse_llm_json_response(response_content)
        validated_query = result["query"]
        logger.info("Query validation completed successfully")
        logger.debug(f"Validated query: {validated_query}")

        QUERY_CACHE[cache_key] = validated_query
        logger.debug("Validated query cached")
        return validated_query
    except Exception as e:
        logger.error(f"Error validating query: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error validating query: {str(e)}")


def extract_table_names(query: str) -> List[str]:
    """Optimized table name extraction using regex patterns"""
    logger.info("Extracting table names from query")
    logger.debug(f"Query for table extraction: {query}")
    try:
        # First try regex pattern matching for common SQL patterns
        patterns = [
            r'FROM\s+([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*)',
            r'JOIN\s+([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*)',
            r'UPDATE\s+([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*)',
            r'INSERT\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*)'
        ]
        logger.debug(f"Using {len(patterns)} regex patterns for table extraction")

        found_tables = set()
        for i, pattern in enumerate(patterns):
            matches = re.findall(pattern, query, re.IGNORECASE)
            logger.debug(f"Pattern {i + 1} found {len(matches)} matches: {matches}")
            found_tables.update(matches)

        logger.debug(f"Regex extraction found {len(found_tables)} tables: {found_tables}")
        if found_tables:
            logger.info(f"Table extraction successful via regex: {list(found_tables)}")
            return list(found_tables)

        # Fallback to LLM if regex doesn't find tables
        logger.info("Regex extraction failed, falling back to LLM")
        cache_key = get_cache_key("extract_tables", query)
        if cache_key in QUERY_CACHE:
            logger.info("Using cached table extraction result")
            return QUERY_CACHE[cache_key]

        logger.debug("Getting LLM client for table extraction")
        llm = get_llm_client()
        prompt = f"""
        Analyze the query: '{query}'
        Extract all table names referenced, including schema names if present (e.g., ecommerce.customers).
        Use the following context to identify valid tables:
        {TABLE_CONTEXT}
        Return a JSON object: {{"tables": ["schema.table", ...]}}.
        Ensure the output is strictly a JSON object, wrapped in ```json\n{{...}}\n```, with no additional text or explanations.
        """
        logger.debug("Formatted table extraction prompt")

        logger.debug("Invoking LLM for table extraction")
        response = llm.invoke([HumanMessage(content=prompt)])
        response_content = response.content.strip()
        logger.info(f"LLM response for extract_table_names: {response_content}")

        result = parse_llm_json_response(response_content)
        tables = result.get("tables", [])
        logger.info(f"LLM table extraction completed: {tables}")

        QUERY_CACHE[cache_key] = tables
        logger.debug("Table extraction result cached")
        return tables
    except Exception as e:
        logger.error(f"Error in extract_table_names: {str(e)}")
        return []


def validate_user_question(question: str) -> Dict[str, str]:
    logger.info("Validating user question")
    logger.debug(f"User question: {question}")
    try:
        # Check cache first
        cache_key = get_cache_key("validate_question", question)
        if cache_key in QUERY_CACHE:
            logger.info("Using cached question validation result")
            return QUERY_CACHE[cache_key]

        logger.debug("Getting LLM client for question validation")
        llm = get_llm_client()

        # Let the LLM handle all validation logic
        validation_prompt = f"""
        You are validating questions for an e-commerce database query system. Analyze the following input: '{question}'

        Context about the database:
        {TABLE_CONTEXT}

        Validation rules:
        1. GREETINGS/OUT OF CONTEXT: If the input is:
           - Just a greeting (hello, hi, hey, good morning, etc.)
           - A simple acknowledgment (yes, no, ok, thanks, bye)
           - Completely unrelated to e-commerce (weather, jokes, general knowledge)
           - Too vague to form a database query
           Then return: {{"error": "The input appears to be a greeting or out of context for the e-commerce domain. Please provide a relevant question related to the database."}}

        2. VALID E-COMMERCE QUESTIONS include anything about:
           - Orders, products, customers, payments, reviews, sellers
           - Geographic data (cities, states, countries) in the context of orders/customers
           - Financial metrics (revenue, costs, prices, totals, freight)
           - Analytical queries (top N, trends, comparisons, counts, averages)
           - Time-based analysis (monthly, yearly, date ranges)
           - Category or status analysis

        3. If the question is valid but has minor issues:
           - Fix spelling and grammar errors
           - Clarify ambiguous phrasing
           - PRESERVE exact database terminology (don't change underscores, keep technical terms)
           - Examples: "health_beauty" stays "health_beauty", "order_id" stays "order_id"

        Examples of VALID questions (should be accepted):
        - "List top 5 cities by order value"
        - "Show me highest selling products"
        - "What's the average freight cost?"
        - "hi what are the top selling categories" (has greeting but also valid query)

        Examples of INVALID inputs (should be rejected):
        - "Hello"
        - "Thank you"
        - "What's the weather today?"
        - "Tell me a joke"
        - "Yes"

        Analyze the input and return ONLY a JSON object:
        - If valid: {{"rephrased_question": "..."}} (can be same as original if no rephrasing needed)
        - If invalid: {{"error": "The input appears to be a greeting or out of context for the e-commerce domain. Please provide a relevant question related to the database."}}

        Important: Even if the input starts with a greeting but contains a valid e-commerce question, treat it as VALID and just rephrase to remove the greeting.

        The output MUST be a valid JSON object wrapped in ```json\n{{...}}\n```
        """

        logger.debug("Invoking LLM for question validation")
        response = llm.invoke([HumanMessage(content=validation_prompt)])
        response_content = response.content.strip()
        logger.info(f"LLM response for validate_user_question: {response_content}")

        result = parse_llm_json_response(response_content)
        logger.info("Question validation completed successfully")

        QUERY_CACHE[cache_key] = result
        logger.debug("Question validation result cached")
        return result

    except Exception as e:
        logger.error(f"Error validating user question: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error validating user question: {str(e)}")


def extract_table_names_from_question(question: str) -> List[str]:
    """Extract table names from natural language question"""
    logger.info("Extracting table names from natural language question")
    logger.info(f"Question for table extraction: {question}")
    cache_key = get_cache_key("extract_tables_question", question)
    if cache_key in QUERY_CACHE:
        logger.info("Using cached table extraction from question")
        return QUERY_CACHE[cache_key]

    try:
        logger.info("Getting LLM client for table extraction from question")
        llm = get_llm_client()
        prompt = f"""
        You are a database assistant. Your task is to analyze the following user question and identify which tables from the provided table context are most relevant for answering the question.

        User Question:
        '{question}'

        STRICT SELECTION RULES:
        1. ONLY select tables that are listed in the TABLE CONTEXT below.
        2. DO NOT guess, invent, assume, or modify table names. Use only the tables that are exactly present in the table context.
        3. DO NOT include any table that is not listed, even if it seems related.
        4. If a concept in the question does not map to any table in the context, do NOT select any table for that concept.
        5. If no table is relevant or matches the user's question, return an empty list.
        6. Tables must be returned as fully schema-qualified names (e.g., ecommerce.customers).

        TABLE CONTEXT (choose only from these tables):
        {TABLE_CONTEXT}

        Output requirements:
        - Return a single JSON object with a key "tables" and a list of relevant schema-qualified table names as the value.
        - The output must be formatted as follows: ```json\n{{"tables": ["schema.table", ...]}}\n```
        - If no valid tables are found, return: ```json\n{{"tables": []}}\n```
        - Do NOT include any explanation, notes, or formatting outside the JSON code block.

        Your output should be only the JSON object, nothing else.
        """
        logger.info(f"Formatted table extraction from question prompt:{prompt}")

        logger.info("Invoking LLM for table extraction from question")
        response = llm.invoke([HumanMessage(content=prompt)])
        response_content = response.content.strip()
        logger.info(f"LLM response for extract_table_names_from_question: {response_content}")

        result = parse_llm_json_response(response_content)
        tables = result.get("tables", [])
        logger.info(f"Extracted {len(tables)} tables from question: {tables}")

        # Validate tables against config
        valid_tables = sum([CONFIG["domains"][d]["tables"] for d in CONFIG["domains"]], [])
        logger.debug(f"Validating against {len(valid_tables)} valid tables")
        invalid_tables = [t for t in tables if t not in valid_tables]
        if invalid_tables:
            logger.error(f"Invalid tables referenced: {invalid_tables}")
            raise HTTPException(status_code=400, detail=f"Invalid tables referenced: {invalid_tables}")
        logger.info("All extracted tables are valid")

        QUERY_CACHE[cache_key] = tables
        logger.debug("Table extraction from question result cached")
        return tables
    except Exception as e:
        logger.error(f"Error in extract_table_names_from_question: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error extracting table names: {str(e)}")


def retrieve_table_schemas(db_config: DBConfig, tables: List[str], domain: str) -> dict:
    """Optimized table schema retrieval with batch processing"""
    logger.info(f"Retrieving table schemas for {len(tables)} tables in domain {domain}")
    logger.debug(f"Tables to retrieve: {tables}")
    try:
        if not tables:
            logger.warning("No tables provided for schema retrieval")
            return {
                "error": "The query cannot be served as no tables could be identified from the question."
            }

        table_info = []
        for i, table in enumerate(tables):
            logger.info(f"Processing table {i + 1}/{len(tables)}: {table}")
            schema_name, table_name = table.split('.')
            logger.debug(f"Schema: {schema_name}, Table: {table_name}")

            # Validate table exists in config
            if not any(table in CONFIG["domains"][d]["tables"] for d in CONFIG["domains"]):
                logger.error(f"Invalid table not found in config: {table}")
                raise HTTPException(status_code=400, detail=f"Invalid table: {table}")

            logger.debug(f"Fetching schema for {schema_name}.{table_name}")
            schema, table_description, sample_data = fetch_table_schema(db_config, schema_name, table_name)
            logger.debug(f"Retrieved {len(schema)} columns, table description: {'Yes' if table_description else 'No'}")

            # Generate missing descriptions only if needed
            if not table_description:
                logger.info(f"Generating missing table description for {table_name}")
                table_description = generate_table_description(table_name, schema_name, domain)

            # Count columns needing descriptions
            columns_needing_desc = [col for col in schema if not col["description"]]
            logger.debug(f"{len(columns_needing_desc)} columns need descriptions")

            for j, column in enumerate(schema):
                if not column["description"]:
                    logger.debug(f"Generating description for column {j + 1}: {column['column_name']}")
                    column["description"] = generate_column_description(
                        column["column_name"], column["data_type"], table_name, schema_name, domain
                    )

            table_info_item = {
                "table_name": table,
                "table_description": table_description,
                "columns": [
                    {
                        "column_name": col["column_name"],
                        "data_type": col["data_type"],
                        "description": col["description"]
                    } for col in schema
                ]
            }
            table_info.append(table_info_item)
            logger.info(f"Completed processing table {table}")

        logger.info(f"Successfully retrieved schemas for {len(table_info)} tables")
        return {"tables": tables, "table_info": table_info}
    except Exception as e:
        logger.error(f"Error in retrieve_table_schemas: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error retrieving table schemas: {str(e)}")


def validate_and_fix_sql(query: str, schema_context: str,user_question="") -> str:
    """Validate and fix SQL syntax issues, particularly for PostgreSQL"""
    logger.info("Validating and fixing SQL query")
    logger.debug(f"Original query: {query}")

    try:
        llm = get_llm_client()

        # # Build schema context for validation
        # schema_context = ""
        # for table_info in table_schemas:
        #     table_name = table_info["table_name"]
        #     columns = table_info["columns"]
        #     schema_context += f"Table: {table_name}\nColumns: "
        #     schema_context += ", ".join([f"{col['column_name']} ({col['data_type']})" for col in columns])
        #     schema_context += "\n\n"

        validation_prompt = f"""
        # Prompt for PostgreSQL SQL Query Validation and Correction
        ## Role
        You are a PostgreSQL SQL expert with deep knowledge of PostgreSQL syntax, best practices, and query optimization.

        ## Tasks
        - Validate and correct a given PostgreSQL SQL query for syntax errors, aliasing issues, and adherence to PostgreSQL-specific best practices.
        - Ensure the corrected query preserves the original intent and logic.
        - Use the provided database schema to validate table and column references.
        - Output only the corrected SQL query without explanations or comments.

        ## Context
        - **Database Schema:**
          {schema_context}
          (The schema provides table names, column names, data types, and relationships for validation.)
        - **Query to Validate:**
          {query}
          (The user-provided SQL query to be corrected.)
        - **Validation Checklist:**
          1. **Reserved Keywords as Aliases**
             - Identify if aliases for tables or CTEs use PostgreSQL reserved keywords (e.g., `or`, `and`, `select`, `from`, `where`, `as`, `in`, `is`, `null`, `not`, `order`, `group`, `having`, `union`, `case`, `when`, `then`, `else`, `end`).
             - Correct by:
               - Renaming to meaningful, non-reserved aliases (e.g., `or` -> `ord`, `and` -> `a`, `order` -> `o`, `is` -> `i1`, `in` -> `i`).
               - Or wrapping in double quotes (e.g., `"or"`).
             - Prefer renaming over quoting for clarity unless explicitly required.
          2. **Alias Reference Validation**
             - Ensure every column reference uses the correct table or CTE alias (e.g., `c.column_name`, not `customers.column_name`).
             - Verify all aliases defined in `FROM`, `JOIN`, or `WITH` clauses are consistently used.
             - Validate CTE references by exact name match.
          3. **Column Existence**
             - Confirm every referenced column exists in the specified table or CTE per the schema.
             - Correct typos or non-existent columns (e.g., replace with correct column names or use placeholder `unknown_column` if unresolvable).
             - Use fully-qualified references (e.g., `alias.column_name` or `cte_name.column_name`).
          4. **Join Conditions**
             - Ensure every `JOIN` has a valid `ON` clause.
             - Verify join columns exist in their respective tables and use correct aliases.
          5. **CTE (WITH Clause) Validation**
             - Ensure CTE names are not reserved keywords (rename if necessary).
             - Confirm CTE column references match their definitions.
             - Validate all CTE references in the main query.
          6. **PostgreSQL-Specific Rules**
             - Use `DATE_TRUNC('unit', column)` for date truncation (e.g., `DATE_TRUNC('month', created_at)`). Avoid `DATEPART` or `DATETRUNC`.
             - Use `column::type` for casting (e.g., `amount::integer`). Avoid `CAST()`.
             - Use `||` for string concatenation. Avoid `+` or `CONCAT()`.
             - Use `LIMIT n` instead of `TOP n`.
             - Use `INTERVAL 'n unit'` for date arithmetic (e.g., `created_at + INTERVAL '1 month'`). Avoid `DATEADD`.
          7. **Ambiguous Column References**
             - Prefix all columns with table or CTE aliases in queries with multiple tables to avoid ambiguity.
             - In `GROUP BY`, use either fully-qualified `alias.column_name` or positional integers (e.g., `GROUP BY 1, 2`).

        ## Instructions
        - Correct all issues identified in the validation checklist.
        - Maintain the original query’s logic and structure unless corrections require restructuring (e.g., fixing invalid joins).
        - Ensure the query is valid and executable in PostgreSQL.
        - Use the provided schema to validate table names, column names, and relationships.
        - If a column or table is invalid and cannot be resolved from the schema, replace with a placeholder (e.g., `unknown_column`) and ensure the query remains syntactically valid.
        - Do not include explanations, comments, or reasoning in the output.

        ## Output
        - Strictly the corrected SQL query.
        - No additional text, comments, or explanations.
        """

        response = llm.invoke([HumanMessage(content=validation_prompt)])
        fixed_query = response.content.strip()

        # Remove any markdown code blocks if present
        if fixed_query.startswith("```sql"):
            fixed_query = fixed_query[6:]
        if fixed_query.startswith("```"):
            fixed_query = fixed_query[3:]
        if fixed_query.endswith("```"):
            fixed_query = fixed_query[:-3]

        fixed_query = fixed_query.strip()
        logger.info("SQL query validated and fixed")
        logger.debug(f"Fixed query: {fixed_query}")

        return fixed_query

    except Exception as e:
        logger.error(f"Error validating SQL: {str(e)}")
        # Return original query if validation fails
        return query

# def validate_and_fix_sql(query: str, schema_context: str, user_question:str) -> str:
#     """Validate and iteratively fix SQL syntax issues for PostgreSQL using LLM, modular pipeline."""
#
#     print("Inside validate_and_fix_sql")
#     logger.info("Validating and fixing SQL query")
#     logger.info(f"Original query: {query}")
#
#     try:
#         llm = get_llm_client()
#         stages = [
#             # 1. Reserved Keyword Alias
#             f"""
#             You are a PostgreSQL SQL expert.
#             Task: Review the SQL query. If any table or CTE alias uses a reserved PostgreSQL keyword (e.g., or, and, select, from, where, as, in, is, null, not, order, group, having, union, case, when, then, else, end), rename the alias to a non-reserved, meaningful name. Example: or → ord, and → a, order → o. Prefer renaming to quoting. Output only the modified SQL query, no explanations.
#             Input Query:
#             {query}
#             """,
#
#             # 2. Alias Reference Consistency
#             f"""
#             You are a PostgreSQL SQL expert.
#             Task: Ensure every column reference uses the correct table or CTE alias. All aliases defined in FROM, JOIN, or WITH clauses must be used consistently. Correct any inconsistencies and output only the fixed SQL query.
#             Input Query:
#             {{QUERY}}
#             """,
#            f"""
#                 You are a PostgreSQL SQL expert.
#
#                 Context:
#                 - User Question: {user_question}
#
#                 Database Schema:
#                 {schema_context}
#
#                 Task:
#                 - Review the provided SQL query and compare every referenced column against the schema above.
#                 - If all referenced columns exist, return the query unchanged.
#                 - If any referenced column does not exist in the schema:
#                     - Rewrite the query so it answers the user question as closely as possible using only columns present in the schema.
#                     - Omit or adapt query logic as needed (for example, remove SELECT expressions, GROUP BY, or ORDER BY on missing columns).
#                 - Do NOT invent or assume any columns not present in the schema.
#                 - The output must be a valid, executable PostgreSQL Query
#
#                 Instructions:
#                 - Do not provide explanations or comments—output only the SQL query or JSON error.
#                 - Maintain the user’s intent as much as possible using only available schema.
#
#                 Input Query:
#                 {{QUERY}}
#                 """,
#
#             # 4. Join Conditions
#             """
#             You are a PostgreSQL SQL expert.
#             Task: Ensure every JOIN clause has a valid ON condition and all join columns exist in their respective tables and use correct aliases. If any join is invalid, correct it as needed. Output only the fixed SQL query.
#             Input Query:
#             {QUERY}
#             """,
#
#             # 5. CTE Validation
#             """
#             You are a PostgreSQL SQL expert.
#             Task: Ensure all WITH (CTE) names are not reserved keywords and are referenced correctly in the main query. Correct any issues with CTE naming or usage. Output only the fixed SQL query.
#             Input Query:
#             {QUERY}
#             """,
#
#             # 6. PostgreSQL Best Practices
#             """
#             You are a PostgreSQL SQL expert.
#             Task: Review for PostgreSQL best practices:
#             - Use DATE_TRUNC('unit', column) for date truncation.
#             - Use column::type for type casting, not CAST().
#             - Use || for string concatenation, not + or CONCAT().
#             - Use LIMIT n instead of TOP n.
#             - Use INTERVAL 'n unit' for date arithmetic.
#             Correct any issues and output only the fixed SQL query.
#             Input Query:
#             {QUERY}
#             """,
#
#             # 7. Ambiguous Column Reference
#             """
#             You are a PostgreSQL SQL expert.
#             Task: In queries with multiple tables, ensure all columns are prefixed with the correct table or CTE alias to avoid ambiguity. In GROUP BY, use fully qualified names or positional integers. Correct any issues and output only the fixed SQL query.
#             Input Query:
#             {QUERY}
#             """
#         ]
#
#         current_query = query
#         for i, stage_prompt in enumerate(stages):
#             # Replace {QUERY} placeholder with the latest version of the query
#             prompt = stage_prompt.replace("{QUERY}", current_query)
#             # logger.info(f"Stage {i+1} prompt:\n{prompt}")
#             response = llm.invoke([HumanMessage(content=prompt)])
#             fixed_query = response.content.strip()
#             logger.info(f"Stage {i+1} fixed_query:\n{fixed_query}")
#
#             # Remove any markdown code blocks if present
#             if fixed_query.startswith("```sql"):
#                 fixed_query = fixed_query[6:]
#             if fixed_query.startswith("```"):
#                 fixed_query = fixed_query[3:]
#             if fixed_query.endswith("```"):
#                 fixed_query = fixed_query[:-3]
#             fixed_query = fixed_query.strip()
#
#             # If the query did not change, move on; else, continue refining
#             if fixed_query and fixed_query != current_query:
#                 logger.info(f"Stage {i+1} produced changes to the query.")
#                 current_query = fixed_query
#             else:
#                 logger.debug(f"Stage {i+1} did not change the query.")
#
#         logger.info("SQL query validated and fixed")
#         logger.debug(f"Final fixed query: {current_query}")
#
#         return current_query
#
#     except Exception as e:
#         logger.error(f"Error validating SQL: {str(e)}")
#         # Return original query if validation fails
#         return query

def decompose_question(rephrased_question: str, tables: List[str], table_schemas: List[dict]) -> dict:
    """Decompose complex questions into sub-queries with structured schema grounding (JSON format)"""
    logger.info("Decomposing question into sub-queries")
    logger.debug(f"Question: {rephrased_question}")
    logger.debug(f"Tables involved: {tables}")

    cache_key = get_cache_key("decompose_question", rephrased_question, str(tables))
    if cache_key in QUERY_CACHE:
        logger.info("Using cached question decomposition")
        return QUERY_CACHE[cache_key]

    try:
        logger.debug("Getting LLM client for question decomposition")
        llm = get_llm_client()

        logger.debug("Building schema context as JSON")
        schema_context_dict = []
        for table_info in table_schemas:
            schema_context_dict.append({
                "table_name": table_info["table_name"],
                "table_description": table_info["table_description"],
                "columns": [
                    {
                        "column_name": col["column_name"],
                        "data_type": col["data_type"],
                        "description": col["description"]
                    }
                    for col in table_info["columns"]
                ]
            })
        schema_context = json.dumps(schema_context_dict, indent=2)
        result = []
        for table in schema_context_dict:
            table_name = table['table_name']
            columns = [col['column_name'] for col in table['columns']]
            result.append({
                "table_name": table_name,
                "columns": columns
            })
        schema_context_lite = json.dumps(result, indent=2)
        logger.debug(f"Schema context built: {len(schema_context)} characters")

        prompt = """
                # PostgreSQL Query Decomposition

                ## Role
                PostgreSQL SQL expert that analyzes user questions, determines if decomposition is needed, and generates executable SELECT queries using provided schemas.

                ## Tasks
                - Analyze user questions for complexity (simple vs complex)
                - Generate valid PostgreSQL SELECT queries using only specified tables/columns
                - For complex questions only: break into sub-questions with corresponding sub-queries
                - Return structured JSON responses

                ## Context
                **Input Variables:**
                - User's data question: {rephrased_question}
                - Available tables: {tables}
                - Database schema: {schema_context}

                **Question Types:**
                - **Simple**: Direct data requests that can be answered in one step (e.g., "Show all customers in New York")
                - **Complex**: Multi-step questions requiring logical breakdown (e.g., "Show top customers by revenue in each region with more than 10 orders")

                ## Instructions/Guidelines 

                ### PostgreSQL Syntax Rules
                ```sql
                -- Schema-qualified names
                ecommerce.orders

                -- Time grouping
                DATE_TRUNC('month', created_at)

                -- Type casting
                amount::integer

                -- Case-insensitive matching
                ILIKE '%pattern%'

                -- String concatenation
                first_name || ' ' || last_name

                -- String aggregation
                STRING_AGG(column, ', ')
                ```

                ### Schema Constraints
                - **CRITICAL**: Use ONLY tables/columns explicitly listed in the provided schema
                - **NEVER** assume or invent columns that don't exist
                - **VERIFY** every column reference against the schema before generating queries
                - For missing relationships: Return error instead of assuming connections

                **Before generating any query:**
                1. List all required columns for the question
                2. Verify each column exists in the provided schema
                3. If ANY required column is missing, return missing data response
                4. Only proceed if ALL columns are confirmed to exist

                **Example of Missing Data Response:**
                Question: "Find top performing items by category"
                Required: performance_metric in items table, category_id in items table
                Schema Check: performance_metric NOT found in schema.items
                Response: Return missing_elements error, NOT a query with invented columns

                ### Query Construction

                **When NOT to Decompose (Simple):**
                - Single table queries with basic filtering/sorting
                - Straightforward joins between 2-3 tables
                - Basic aggregations (SUM, COUNT, AVG) on one dataset
                - Direct data retrieval without complex logic

                **When to Decompose (Complex):**
                - Multi-step calculations requiring intermediate results
                - Questions needing multiple aggregations across different groupings
                - Filtering that depends on results from other queries
                - Top-N queries within categories
                - Questions with "and then" or "for each" logic

                **Process:**
                1. **Analyze** question for above criteria
                2. **Simple**: Write direct SELECT query
                3. **Complex**: Break into logical sub-questions → create sub-queries → combine with CTEs
                4. **Validate** all references against schema

                ## Output

                ### Simple Questions
                ```json
                {{
                  "decomposition_needed": false,
                  "final_query": "SELECT column1, column2 FROM schema.table WHERE condition;"
                }}
                ```

                ### Simple Questions with Missing Data
                ```json
                {{
                  "decomposition_needed": false,
                  "error": "Required data not available in schema",
                  "missing_elements": ["table_name.column_name"],
                  "final_query": null
                }}
                ```

                ### Complex Questions
                ```json
                {{
                  "decomposition_needed": true,
                  "sub_questions": ["Sub-question 1", "Sub-question 2"],
                  "sub_queries": ["SELECT ...", "SELECT ..."],
                  "final_query": "WITH cte1 AS (...), cte2 AS (...) SELECT ... FROM cte1 JOIN cte2 ..."
                }}
                ```

                ### Complex Questions with Missing Data
                ```json
                {{
                  "decomposition_needed": true,
                  "sub_questions": ["Sub-question 1", "Sub-question 2 (data not available)"],
                  "sub_queries": ["SELECT ...", null],
                  "missing_elements": ["table_name.column_name"],
                  "final_query": null,
                  "error": "Cannot complete query due to missing schema elements"
                }}
                ```

                ### Requirements
                - All queries must be valid PostgreSQL SELECT statements
                - Use schema-qualified table names
                - Return only JSON, no explanations
                - Handle missing schema data gracefully
                - If the question asks for “quantity” or any other column that is NOT in the schema, you must stop and return the JSON error, even if you expect that column to exist.
                - NEVER use columns that do not appear in the explicit schema.
                """.format(
            rephrased_question=rephrased_question,
            tables=", ".join(tables),
            schema_context=schema_context
        )

        # prompt = f"""
        # You are a PostgreSQL SQL expert.
        #
        # Your task:
        # - Generate executable SELECT queries (and decompositions, if needed) for the user’s question,
        # - But ONLY use tables and columns exactly as provided in the schema context.
        #
        # User Question:
        # {rephrased_question}
        #
        # Tables in Scope:
        # {tables}
        #
        # Provided Schema:
        # {schema_context}
        #
        # CRITICAL RULES
        #
        # 1. NEVER invent or assume columns or tables.
        #    - You may only use the columns explicitly listed for each table in the provided schema context.
        #    - If a column mentioned in the question (or required for a typical solution) is missing from the schema, you MUST stop and return a JSON error as shown below.
        #
        # 2. Column Check (MUST-DO)
        #    - Before writing any SQL, list each table and the columns you will reference in your query.
        #    - For each column, check if it appears in the provided schema.
        #    - If ANY required column is missing, DO NOT write any SQL. Instead, return ONLY this JSON:
        # {{
        #   "decomposition_needed": false,
        #   "final_query": null,
        #   "error": "Required data not available in schema",
        #   "missing_elements": ["table.column", ...]
        # }}
        #
        # 3. Query Output
        #    - If ALL referenced columns exist, write a valid PostgreSQL SELECT (or CTE) query using only the allowed tables and columns.
        #    - Use schema-qualified table names.
        #    - Return a JSON object as shown in the examples below.
        #
        # OUTPUT FORMAT
        #
        # On Missing Columns
        # {{
        #   "decomposition_needed": false,
        #   "final_query": null,
        #   "error": "Required data not available in schema",
        #   "missing_elements": ["table.column"]
        # }}
        #
        # On Success
        # {{
        #   "decomposition_needed": false,
        #   "final_query": "SELECT ... FROM ... WHERE ...;"
        # }}
        #
        # (or decomposition structure, as needed)
        #
        # - Do not explain your reasoning or output anything except the JSON object.
        #
        # IMPORTANT
        #
        # - If the question asks for “quantity” or any other column that is NOT in the schema, you must stop and return the JSON error, even if you expect that column to exist.
        # - NEVER use columns that do not appear in the explicit schema.
        # """

        logger.debug("Stage 1: Sending prompt to LLM")
        response = llm.invoke([HumanMessage(content=prompt)])
        response_content = response.content.strip()
        logger.info("LLM response received")

        result = parse_llm_json_response(response_content)
        decomposition_needed = result.get("decomposition_needed", False)

        if decomposition_needed:
            sub_questions = result.get("sub_questions", [])
            sub_queries = result.get("sub_queries", [])
            final_query = result.get("final_query", "")

            logger.info(f"Stage 2: Validating and fixing {len(sub_queries)} sub-queries")

            fixed_sub_queries = [validate_and_fix_sql(q, schema_context_lite,sub_questions[index]) for index,q in enumerate(sub_queries)]
            final_query = validate_and_fix_sql(final_query, schema_context_lite,rephrased_question) if final_query else ""

            sub_queries = fixed_sub_queries
            all_queries = sub_queries + ([final_query] if final_query else [])
        else:
            sub_questions = []
            sub_queries = []
            final_query = result.get("final_query", "")
            final_query = validate_and_fix_sql(final_query, schema_context_lite,rephrased_question) if final_query else ""
            all_queries = [final_query] if final_query else []

        logger.info("Stage 3: Final validation of all queries")
        valid_tables = sum([CONFIG["domains"][d]["tables"] for d in CONFIG["domains"]], [])
        dangerous_keywords = ["DROP", "DELETE", "INSERT", "UPDATE", "CREATE", "ALTER", "TRUNCATE", "EXEC", "EXECUTE"]

        for idx, query in enumerate(all_queries):
            if not query:
                continue

            query_upper = query.strip().upper()

            # if not (query_upper.startswith("SELECT") or query_upper.startswith("WITH")):
            #     logger.error(f"Query {idx} doesn't start with SELECT or WITH")
            #     raise HTTPException(status_code=400, detail=f"Query {idx} must start with SELECT or WITH")

            for keyword in dangerous_keywords:
                if re.search(rf'\b{keyword}\b', query_upper):
                    if keyword in ["DROP", "DELETE", "UPDATE", "CREATE", "ALTER", "TRUNCATE"] and \
                            re.search(rf'\b{keyword}\s+(TABLE|DATABASE|SCHEMA|INDEX|VIEW)\b', query_upper):
                        logger.error(f"Dangerous operation '{keyword}' found in query {idx}")
                        raise HTTPException(status_code=400,
                                            detail=f"Query {idx} contains forbidden operation: {keyword}")

            query_tables = extract_table_names(query)
            invalid_tables = [t for t in query_tables if t not in valid_tables]
            if invalid_tables:
                logger.error(f"Invalid tables in query {idx}: {invalid_tables}")
                raise HTTPException(status_code=400, detail=f"Invalid tables in query {idx}: {invalid_tables}")

        logger.info("All generated queries validated successfully")

        formatted_result = {
            "sub_questions": sub_questions,
            "sub_queries": sub_queries,
            "final_query": final_query,
            "decomposition_needed": decomposition_needed
        }

        QUERY_CACHE[cache_key] = formatted_result
        logger.debug("Question decomposition result cached")
        return formatted_result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in decompose_question: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error decomposing question: {str(e)}")


# API endpoints (optimized)
@app.post("/schema/extract", response_model=List[TableSchema])
async def extract_schema(db_config: DBConfig, domain: str, table: Optional[str] = None):
    logger.info(f"Extract schema endpoint called - domain: {domain}, table: {table}")
    if domain not in CONFIG["domains"]:
        logger.error(f"Invalid domain requested: {domain}")
        raise HTTPException(status_code=400, detail=f"Invalid domain: {domain}")

    table_list = [table] if table else CONFIG["domains"][domain]["tables"]
    logger.info(f"Processing {len(table_list)} tables")

    if table and table not in CONFIG["domains"][domain]["tables"]:
        logger.error(f"Invalid table requested: {table}")
        raise HTTPException(status_code=400, detail=f"Invalid table: {table}")

    output_schemas = []
    for i, table in enumerate(table_list):
        logger.info(f"Processing table {i + 1}/{len(table_list)}: {table}")
        schema_name, table_name = table.split('.')
        schema, table_description, sample_data_dict = fetch_table_schema(db_config, schema_name, table_name)

        # Only generate descriptions if they don't exist
        if not table_description:
            logger.info(f"Generating table description for {table_name}")
            table_description = generate_table_description(table_name, schema_name, domain)

        # Batch generate column descriptions for columns without descriptions
        columns_needing_descriptions = [col for col in schema if not col["description"]]
        logger.info(f"Generating descriptions for {len(columns_needing_descriptions)} columns")
        for column in columns_needing_descriptions:
            column["description"] = generate_column_description(
                table_description,column["column_name"], column["data_type"], table_name, schema_name, domain, column["sample_values"]
            )


        output_schema = {
            "table_name": f"{schema_name}.{table_name}",
            "table_description": table_description,
            "columns": [ColumnSchema(**col) for col in schema]
        }
        output_schemas.append(output_schema)
        logger.debug(f"Completed processing table {table}")

    logger.info(f"Extract schema endpoint completed - returned {len(output_schemas)} schemas")
    return output_schemas


@app.post("/description/generate", response_model=dict)
async def generate_description(request: GenerateDescriptionRequest):
    logger.info(
        f"Generate description endpoint called for {request.schema_name}.{request.table_name} in domain {request.domain}")
    if request.domain not in CONFIG["domains"]:
        logger.error(f"Invalid domain in generate description: {request.domain}")
        raise HTTPException(status_code=400, detail=f"Invalid domain: {request.domain}")
    if f"{request.schema_name}.{request.table_name}" not in CONFIG["domains"][request.domain]["tables"]:
        logger.error(f"Invalid table in generate description: {request.schema_name}.{request.table_name}")
        raise HTTPException(status_code=400, detail=f"Invalid table: {request.schema_name}.{request.table_name}")

    description = generate_table_description(request.table_name, request.schema_name, request.domain)
    result = {"table_name": f"{request.schema_name}.{request.table_name}", "description": description}
    logger.info("Generate description endpoint completed successfully")
    return result


@app.post("/description/generate_full", response_model=TableSchema)
async def generate_full_description(db_config: DBConfig, request: GenerateDescriptionRequest):
    logger.info(
        f"Generate full description endpoint called for {request.schema_name}.{request.table_name} in domain {request.domain}")
    if request.domain not in CONFIG["domains"]:
        logger.error(f"Invalid domain in generate full description: {request.domain}")
        raise HTTPException(status_code=400, detail=f"Invalid domain: {request.domain}")
    if f"{request.schema_name}.{request.table_name}" not in CONFIG["domains"][request.domain]["tables"]:
        logger.error(f"Invalid table in generate full description: {request.schema_name}.{request.table_name}")
        raise HTTPException(status_code=400, detail=f"Invalid table: {request.schema_name}.{request.table_name}")

    schema, table_description, sample_data_dict = fetch_table_schema(db_config, request.schema_name, request.table_name)

    # Only generate if missing
    if not table_description:
        logger.info("Generating missing table description")
        table_description = generate_table_description(request.table_name, request.schema_name, request.domain)

    # Batch process columns without descriptions
    columns_needing_desc = [col for col in schema if not col["description"]]
    logger.info(f"Generating descriptions for {len(columns_needing_desc)} columns")
    for column in schema:
        if not column["description"]:
            column["description"] = generate_column_description(
                table_description, column["column_name"], column["data_type"], request.table_name, request.schema_name, request.domain, column["sample_values"]
            )

    result = {
        "table_name": f"{request.schema_name}.{request.table_name}",
        "table_description": table_description,
        "columns": [ColumnSchema(**col) for col in schema]
    }
    logger.info("Generate full description endpoint completed successfully")
    return result


@app.post("/description/update", response_model=dict)
async def update_description(db_config: DBConfig, request: UpdateDescriptionRequest):
    logger.info(f"Update description endpoint called for {request.schema_name}.{request.table_name}")
    if f"{request.schema_name}.{request.table_name}" not in sum(
            [CONFIG["domains"][d]["tables"] for d in CONFIG["domains"]], []):
        logger.error(f"Invalid table in update description: {request.schema_name}.{request.table_name}")
        raise HTTPException(status_code=400, detail=f"Invalid table: {request.schema_name}.{request.table_name}")

    result = update_descriptions(
        db_config, request.schema_name, request.table_name,
        request.table_description, request.column_descriptions
    )
    logger.info("Update description endpoint completed successfully")
    return result


@app.post("/query/process_user_query", response_model=dict)
async def process_user_query(db_config: DBConfig, request: QueryRequest):
    """Optimized main query processing endpoint"""
    logger.info(f"Process user query endpoint called with query: {request.query}")
    logger.debug(f"Request domain: {request.domain}, execute: {request.execute}")
    try:
        start_time = time.time()
        logger.info("Starting user query processing")

        # Step 1: Validate user question (cached)
        logger.info("Step 1: Validating user question")
        validation_result = validate_user_question(request.query)
        if "error" in validation_result:
            logger.warning(f"User question validation failed: {validation_result['error']}")
            return {"error": validation_result["error"]}

        rephrased_question = validation_result["rephrased_question"]
        logger.info(f"Question validated and rephrased: {rephrased_question}")
        logger.info(f"Validation took {time.time() - start_time:.2f}s")

        # Step 2: Extract table names (cached)
        step2_start = time.time()
        logger.info("Step 2: Extracting table names from question")
        tables = extract_table_names_from_question(rephrased_question)
        logger.info(f"Extracted {len(tables)} tables: {tables}")
        logger.info(f"Table extraction took {time.time() - step2_start:.2f}s")

        # Step 3: Determine domain
        logger.info("Step 3: Determining domain")
        domain = request.domain
        if not domain:
            logger.debug("No domain specified, auto-detecting from tables")
            for d in CONFIG["domains"]:
                if any(t in CONFIG["domains"][d]["tables"] for t in tables):
                    domain = d
                    logger.info(f"Auto-detected domain: {domain}")
                    break
            if not domain:
                domain = list(CONFIG["domains"].keys())[0]
                logger.info(f"Defaulting to first domain: {domain}")
        else:
            logger.info(f"Using specified domain: {domain}")

        # Step 4: Retrieve table schemas (with caching)
        step4_start = time.time()
        logger.info("Step 4: Retrieving table schemas")
        schema_result = retrieve_table_schemas(db_config, tables, domain)
        if "error" in schema_result:
            logger.warning(f"Schema retrieval failed: {schema_result['error']}")
            return schema_result
        logger.info(f"Schema retrieval took {time.time() - step4_start:.2f}s")

        table_info = schema_result["table_info"]
        logger.debug(f"Retrieved schema info for {len(table_info)} tables")

        # Step 5: Decompose question (cached)
        step5_start = time.time()
        logger.info("Step 5: Decomposing question into queries")
        decomposition_result = decompose_question(rephrased_question, tables, table_info)
        if "error" in decomposition_result:
            logger.warning(f"Question decomposition failed: {decomposition_result['error']}")
            return decomposition_result
        logger.info(f"Question decomposition took {time.time() - step5_start:.2f}s")

        total_time = time.time() - start_time
        response = {
            "rephrased_question": rephrased_question,
            "tables": tables,
            "table_info": table_info,
            "sub_questions": decomposition_result["sub_questions"],
            "sub_queries": decomposition_result["sub_queries"],
            "final_query": decomposition_result["final_query"],
            "processing_time": total_time
        }

        logger.info(f"Process user query completed successfully in {total_time:.2f}s")
        logger.info(f"Response from process_user_query: {response}")
        return response

    except Exception as e:
        logger.error(f"Error in process_user_query: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing user query: {str(e)}")


@app.post("/query/execute", response_model=dict)
async def execute_query_endpoint(request: ExecuteQueryRequest):
    """Optimized query execution"""
    logger.info("Execute query endpoint called")
    logger.debug(f"Query to execute: {request.query}")
    print(request)
    tables = extract_table_names(request.query)
    logger.info(f"Extracted {len(tables)} tables for execution: {tables}")
    result = execute_query(request.db_config, request.query, tables)
    logger.info("Query execution completed successfully")
    return result


@app.post("/query/summarize", response_model=dict)
async def summarize_query(request: SummarizeQueryRequest):
    logger.info("Summarize query endpoint called")
    try:
        if request.results is not None:
            logger.info(f"Summarizing {len(request.results)} results")
            summary = summarize_results(
                request.results,
                prompt=request.prompt,
                user_question=request.user_question,
                rephrased_question=request.rephrased_question
            )
        elif request.prompt is not None:
            logger.info("Summarizing with custom prompt")
            summary = summarize_results(
                [],
                prompt=request.prompt,
                user_question=request.user_question,
                rephrased_question=request.rephrased_question
            )
        else:
            logger.error("Neither results nor prompt provided for summarization")
            raise HTTPException(status_code=422, detail="Either 'results' or 'prompt' must be provided")

        logger.info("Summarization completed successfully")
        return {"summary": summary}
    except Exception as e:
        logger.error(f"Error summarizing query results: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error summarizing query results: {str(e)}")


@app.post("/audit/logs", response_model=dict)
async def get_audit_logs(db_config: DBConfig, limit: int = 100, offset: int = 0):
    logger.info(f"Get audit logs endpoint called with limit={limit}, offset={offset}")
    result = fetch_audit_logs(db_config, limit, offset)
    logger.info(f"Audit logs retrieved successfully - {len(result['logs'])} logs, total: {result['total_count']}")
    return result


# Cache management endpoints
@app.post("/cache/clear")
async def clear_cache():
    """Clear all caches"""
    logger.info("Clear cache endpoint called")
    global DESCRIPTION_CACHE, QUERY_CACHE
    desc_cache_size = len(DESCRIPTION_CACHE)
    query_cache_size = len(QUERY_CACHE)

    DESCRIPTION_CACHE.clear()
    QUERY_CACHE.clear()
    # fetch_table_schema_cached.cache_clear()
    get_llm_client.cache_clear()

    logger.info(f"All caches cleared - description: {desc_cache_size}, query: {query_cache_size}")
    return {"status": "success", "message": "All caches cleared"}


@app.get("/cache/stats")
async def get_cache_stats():
    """Get cache statistics"""
    logger.info("Cache stats endpoint called")
    stats = {
        "description_cache_size": len(DESCRIPTION_CACHE),
        "query_cache_size": len(QUERY_CACHE),
        "schema_cache_info": fetch_table_schema_cached.cache_info(),
        "llm_client_cache_info": get_llm_client.cache_info()
    }
    logger.info(f"Cache stats retrieved: {stats}")
    return stats


# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check endpoint"""
    logger.info("Health check endpoint called")
    try:
        # Test OpenAI API key
        if not os.getenv("OPENAI_API_KEY"):
            logger.error("Health check failed - OPENAI_API_KEY not set")
            return {"status": "unhealthy", "error": "OPENAI_API_KEY not set"}

        # Test config loading
        if not CONFIG or not TABLE_CONTEXT:
            logger.error("Health check failed - Configuration not loaded properly")
            return {"status": "unhealthy", "error": "Configuration not loaded properly"}

        cache_stats = {
            "description_cache_size": len(DESCRIPTION_CACHE),
            "query_cache_size": len(QUERY_CACHE)
        }

        logger.info(f"Health check passed - cache stats: {cache_stats}")
        return {
            "status": "healthy",
            "cache_stats": cache_stats
        }
    except Exception as e:
        logger.error(f"Health check failed with exception: {str(e)}")
        return {"status": "unhealthy", "error": str(e)}


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting FastAPI application")
    uvicorn.run(app, host="0.0.0.0", port=8002)