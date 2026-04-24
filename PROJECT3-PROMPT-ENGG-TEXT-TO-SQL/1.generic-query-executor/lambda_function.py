"""
Generic Query Executor Lambda
=============================

This Lambda function acts as a **database access layer** for all application
Lambdas. It executes parameterized SQL queries against an RDS PostgreSQL
database using credentials stored securely in AWS Secrets Manager.

Architecture Role
-----------------
Business Lambda (Customers, Orders, etc.)
    -> Generic Query Executor Lambda (this file)
        -> RDS PostgreSQL

Why this design?
----------------
- Centralized database access & security
- Reusable query execution logic
- Simplified IAM and secrets handling
- Connection pooling for performance optimization
"""

import json
import os
import boto3
import logging
import traceback
from botocore.exceptions import ClientError
import psycopg2
from psycopg2 import pool
from typing import Dict, Any, Optional

# ---------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------------------------------------------------
# Global State (Reused Across Lambda Invocations)
# ---------------------------------------------------------

# PostgreSQL connection pool (created once per warm Lambda)
connection_pool = None

# Cached DB credentials to avoid repeated Secrets Manager calls
cached_credentials = None

# ---------------------------------------------------------
# Secrets Manager Integration
# ---------------------------------------------------------

def get_secret(secret_name: str) -> Dict[str, Any]:
    """
    Retrieve database credentials from AWS Secrets Manager.

    This function:
    - Fetches credentials only once per Lambda container
    - Caches them in memory for subsequent invocations

    Args
    ----
    secret_name : str
        Name of the secret in AWS Secrets Manager

    Returns
    -------
    Dict[str, Any]
        Parsed JSON secret containing DB credentials

    Raises
    ------
    Exception
        If secret retrieval or parsing fails
    """
    global cached_credentials

    # Return cached credentials if already loaded
    if cached_credentials:
        logger.info(f"Using cached credentials for secret: {secret_name}")
        return cached_credentials

    logger.info(f"Fetching credentials from Secrets Manager, secret: {secret_name}, region: {os.environ.get('AWS_REGION', 'us-east-1')}")

    # Create Secrets Manager client
    session = boto3.session.Session()
    client = session.client(
        service_name="secretsmanager",
        region_name=os.environ.get("AWS_REGION", "us-east-1")
    )

    try:
        response = client.get_secret_value(SecretId=secret_name)
        logger.info(f"Successfully retrieved secret: {secret_name}")

    except ClientError as e:
        # Explicit handling of Secrets Manager failure scenarios
        error_code = e.response["Error"]["Code"]
        logger.error(f"Failed to retrieve secret: {secret_name}, error_code: {error_code}, error: {str(e)}, trace: {traceback.format_exc()}")

        if error_code == "DecryptionFailureException":
            raise Exception("Secrets Manager cannot decrypt the secret")
        elif error_code == "InternalServiceErrorException":
            raise Exception("Secrets Manager internal service error")
        elif error_code == "InvalidParameterException":
            raise Exception("Invalid parameter while fetching secret")
        elif error_code == "InvalidRequestException":
            raise Exception("Invalid request to Secrets Manager")
        elif error_code == "ResourceNotFoundException":
            raise Exception(f"Secret '{secret_name}' not found")
        else:
            raise Exception(f"Unexpected Secrets Manager error: {str(e)}")

    # Secrets Manager returns secrets either as string or binary
    if "SecretString" in response:
        secret = json.loads(response["SecretString"])
        cached_credentials = secret  # Cache for reuse
        logger.info(f"Secret parsed and cached, secret: {secret_name}")
        return secret

    logger.error(f"Unsupported secret format (binary), secret: {secret_name}")
    raise Exception("Unsupported secret format (binary secret)")


# ---------------------------------------------------------
# Database Connection Pool Management
# ---------------------------------------------------------

def get_db_connection():
    """
    Acquire a database connection from the pool.

    Behavior
    --------
    - Initializes a PostgreSQL connection pool on first invocation
    - Reuses pooled connections for subsequent calls

    Returns
    -------
    psycopg2.connection
        Active database connection
    """
    global connection_pool

    if connection_pool is None:
        logger.info("Initializing database connection pool")

        # Secret name must be provided via environment variable
        secret_name = os.environ.get("DB_SECRET_NAME")
        if not secret_name:
            logger.error("DB_SECRET_NAME environment variable not set")
            raise Exception("DB_SECRET_NAME environment variable is not set")

        # Fetch DB credentials securely
        credentials = get_secret(secret_name)

        db_host = credentials.get("host", os.environ.get("DB_HOST"))
        db_name = credentials.get("dbname", "postgres")
        db_user = credentials.get("username", os.environ.get("DB_USER"))
        db_port = credentials.get("port", os.environ.get("DB_PORT", "5432"))

        logger.info(f"Creating connection pool, host: {db_host}, database: {db_name}, user: {db_user}, port: {db_port}")

        try:
            # Initialize PostgreSQL connection pool
            connection_pool = psycopg2.pool.SimpleConnectionPool(
                minconn=1,
                maxconn=10,
                host=db_host,
                database=db_name,
                user=db_user,
                password=credentials["password"],  # MUST come from secret
                port=db_port
            )

            logger.info(f"Connection pool created successfully, host: {db_host}, database: {db_name}")

        except Exception as e:
            logger.error(f"Failed to create connection pool, host: {db_host}, database: {db_name}, error: {str(e)}, type: {type(e).__name__}, trace: {traceback.format_exc()}")
            raise

    logger.info("Acquiring connection from pool")

    try:
        conn = connection_pool.getconn()
        logger.info(f"Connection acquired successfully, connection_id: {id(conn)}")
        return conn
    except Exception as e:
        logger.error(f"Failed to acquire connection from pool, error: {str(e)}, type: {type(e).__name__}, trace: {traceback.format_exc()}")
        raise


def return_db_connection(conn):
    """
    Return a database connection back to the pool.

    This ensures connections are reused efficiently and
    prevents connection leaks.
    """
    global connection_pool

    if connection_pool and conn:
        logger.info(f"Returning connection to pool, connection_id: {id(conn)}")
        connection_pool.putconn(conn)


# ---------------------------------------------------------
# Core Query Execution Logic
# ---------------------------------------------------------

def execute_query(
    query: str,
    params: Optional[tuple] = None,
    fetch: bool = True
) -> Dict[str, Any]:
    """
    Execute a SQL query against PostgreSQL.

    Supports:
    - SELECT queries (fetch=True)
    - INSERT / UPDATE / DELETE (fetch=False or RETURNING)

    Args
    ----
    query : str
        SQL query with parameter placeholders (%s)
    params : tuple, optional
        Query parameters
    fetch : bool
        Whether to fetch and return query results

    Returns
    -------
    Dict[str, Any]
        {
            "success": bool,
            "rowcount": int,
            "data": list | None,
            "error": str (optional)
        }
    """
    conn = None
    cursor = None

    logger.info(f"Executing query: {query}, params: {params}, fetch: {fetch}")

    try:
        # Acquire DB connection
        conn = get_db_connection()
        cursor = conn.cursor()

        logger.info(f"Executing SQL statement: {query}")

        # Execute parameterized query (prevents SQL injection)
        cursor.execute(query, params)

        logger.info(f"SQL statement executed, query: {query}, rowcount: {cursor.rowcount}")

        result = {
            "success": True,
            "rowcount": cursor.rowcount
        }

        # Handle SELECT / RETURNING queries
        if fetch and cursor.description:
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()

            # Convert rows into list of dictionaries
            result["data"] = [dict(zip(columns, row)) for row in rows]

            logger.info(f"Query results fetched, query: {query}, rowcount: {cursor.rowcount}, result_count: {len(result['data'])}, columns: {columns}")
        else:
            result["data"] = None

        # Always commit the transaction after successful execution
        conn.commit()
        logger.info(f"Transaction committed, query: {query}, rowcount: {cursor.rowcount}")

        return result

    except psycopg2.Error as e:
        # Roll back transaction on DB errors
        if conn:
            conn.rollback()
            logger.info(f"Transaction rolled back, query: {query}")

        logger.error(f"Database error during query execution, query: {query}, params: {params}, error: {str(e)}, error_code: {getattr(e, 'pgcode', None)}, type: {type(e).__name__}, trace: {traceback.format_exc()}")

        return {
            "success": False,
            "error": str(e),
            "error_code": getattr(e, "pgcode", None)
        }

    except Exception as e:
        if conn:
            conn.rollback()
            logger.info(f"Transaction rolled back, query: {query}")

        logger.error(f"Unexpected error during query execution, query: {query}, params: {params}, error: {str(e)}, type: {type(e).__name__}, trace: {traceback.format_exc()}")

        return {
            "success": False,
            "error": str(e)
        }

    finally:
        # Always clean up resources
        if cursor:
            logger.info(f"Closing cursor for query: {query}")
            cursor.close()
        if conn:
            return_db_connection(conn)


# ---------------------------------------------------------
# Lambda Entry Point
# ---------------------------------------------------------

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AWS Lambda entry point.

    Expected Event Payload
    ----------------------
    {
        "query": "SELECT * FROM demo.customers WHERE customer_id = %s",
        "params": ["CUST001"],
        "fetch": true
    }

    Environment Variables
    ---------------------
    - DB_SECRET_NAME : Secrets Manager secret name
    - AWS_REGION     : AWS region (optional)

    Returns
    -------
    API Gateway–compatible HTTP response
    """

    logger.info("Lambda invocation started")

    try:
        # Support both API Gateway and direct Lambda invocation
        if isinstance(event.get("body"), str):
            logger.info("Parsing API Gateway request body")
            body = json.loads(event["body"])
        else:
            logger.info("Using direct Lambda invocation payload")
            body = event

        query = body.get("query")
        params = body.get("params")
        fetch = body.get("fetch", True)

        logger.info(f"Request parsed, query: {query}, params: {params}, fetch: {fetch}")

        # Input validation
        if not query:
            logger.warning("Query validation failed - query is required")
            return {
                "statusCode": 400,
                "headers": {
                    "Content-Type": "application/json",
                    "Access-Control-Allow-Origin": "*"
                },
                "body": json.dumps({
                    "success": False,
                    "error": "Query is required"
                })
            }

        # Convert params list to tuple for psycopg2
        if params and isinstance(params, list):
            params = tuple(params)
            logger.info(f"Converted params list to tuple, param_count: {len(params)}")

        # Execute SQL query
        result = execute_query(query, params, fetch)

        status_code = 200 if result["success"] else 500

        logger.info(f"Lambda invocation completed, status_code: {status_code}, success: {result['success']}, rowcount: {result.get('rowcount')}")

        return {
            "statusCode": status_code,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*"
            },
            "body": json.dumps(result, default=str)
        }

    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error, error: {str(e)}, type: {type(e).__name__}, trace: {traceback.format_exc()}")

        return {
            "statusCode": 400,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*"
            },
            "body": json.dumps({
                "success": False,
                "error": f"Invalid JSON payload: {str(e)}"
            })
        }

    except Exception as e:
        logger.error(f"Unhandled exception in lambda_handler, error: {str(e)}, type: {type(e).__name__}, trace: {traceback.format_exc()}")

        return {
            "statusCode": 500,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*"
            },
            "body": json.dumps({
                "success": False,
                "error": str(e)
            })
        }