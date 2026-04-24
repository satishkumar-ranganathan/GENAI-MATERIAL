import psycopg2
from pydantic import BaseModel
import json
import os
import logging
import hashlib
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DBConfig(BaseModel):
    dbname: str
    user: str
    password: str
    host: str
    port: str

config_path = "./config/config.json"
table_context_path = "./config/table.txt"
try:
    with open(config_path, "r") as f:
        CONFIG = json.load(f)
    required_keys = ["domains", "sql_queries"]
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
        logger.error(f"Error fetching schema for {schema_name}.{table_name}: {str(e)}")
        return [], None

def generate_table_context(db_config: DBConfig):
    try:
        table_context = []
        for domain, details in CONFIG["domains"].items():
            for table in details["tables"]:
                schema_name, table_name = table.split('.')
                _, table_description = fetch_table_schema(db_config, schema_name, table_name)
                description = (
                    table_description or
                    details.get("table_descriptions", {}).get(table, "No description available")
                )
                table_context.append(f"{table}: {description}")
        context_str = "\n".join(table_context)
        context_hash = hashlib.md5(context_str.encode()).hexdigest()
        update_needed = True
        if os.path.exists(table_context_path):
            try:
                with open(table_context_path, "r") as f:
                    lines = f.readlines()
                    if lines and lines[0].startswith("# HASH:"):
                        cached_hash = lines[0].strip().split(" ")[-1]
                        if cached_hash == context_hash:
                            update_needed = False
                            logger.info(f"table_context.txt is up-to-date at {table_context_path}")
                            return "\n".join(lines[1:]).strip()
            except Exception as e:
                logger.warning(f"Error reading table_context.txt: {str(e)}")
        if update_needed:
            try:
                os.makedirs(os.path.dirname(table_context_path), exist_ok=True)
                with open(table_context_path, "w") as f:
                    f.write(f"# HASH: {context_hash}\n{context_str}")
                logger.info(f"Updated table_context.txt at {table_context_path}")
            except Exception as e:
                logger.error(f"Error writing table_context.txt: {str(e)}")
                return context_str
        return context_str
    except Exception as e:
        logger.error(f"Error generating table context: {str(e)}")
        raise Exception(f"Failed to generate table context: {str(e)}")

if __name__ == "__main__":
    default_db_config = DBConfig(
        dbname="olist_ecommerce",
        user="root",
        password="",
        host="localhost",
        port="5432"
    )
    try:
        context = generate_table_context(default_db_config)
        print(f"Generated table context:\n{context}")
    except Exception as e:
        print(f"Error: {str(e)}")