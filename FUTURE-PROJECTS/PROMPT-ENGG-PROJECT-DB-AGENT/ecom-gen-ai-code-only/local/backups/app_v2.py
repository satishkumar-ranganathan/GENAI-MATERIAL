import streamlit as st
import requests
import json
import os
import math

# Load configuration with robust error handling
config_path = "./config/config.json"
try:
    with open(config_path, "r") as f:
        CONFIG = json.load(f)
    required_keys = ["domains", "sql_queries", "prompts"]
    missing_keys = [key for key in required_keys if key not in CONFIG]
    if missing_keys:
        st.error(f"Missing required keys in config.json: {', '.join(missing_keys)}")
        st.stop()
except FileNotFoundError:
    st.error(f"config.json not found at {config_path}. Please ensure it exists.")
    st.stop()
except json.JSONDecodeError:
    st.error(f"Invalid JSON in config.json at {config_path}. Please check the file format.")
    st.stop()
except Exception as e:
    st.error(f"Error loading config.json: {str(e)}")
    st.stop()

# Streamlit app configuration
st.set_page_config(page_title="Database Schema Manager", layout="wide")

# Custom CSS for button styling
st.markdown("""
<style>
/* General button styling */
div.stButton > button {
    border-radius: 5px;
    padding: 8px 16px;
    font-weight: 500;
    margin-right: 10px;
}

/* Blue buttons for actions */
div.stButton > button[kind="primary"] {
    background-color: #007bff;
    color: white;
    border: 1px solid #007bff;
}
div.stButton > button[kind="primary"]:hover {
    background-color: #0056b3;
    border: 1px solid #0056b3;
}

/* Green button for Save */
div.stButton > button[kind="save"] {
    background-color: #28a745;
    color: white;
    border: 1px solid #28a745;
}
div.stButton > button[kind="save"]:hover {
    background-color: #218838;
    border: 1px solid #218838;
}

/* Red button for Cancel */
div.stButton > button[kind="cancel"] {
    background-color: #dc3545;
    color: white;
    border: 1px solid #dc3545;
}
div.stButton > button[kind="cancel"]:hover {
    background-color: #c82333;
    border: 1px solid #c82333;
}

/* Align buttons side by side */
div.stButton {
    display: inline-block;
}
</style>
""", unsafe_allow_html=True)

# Initialize session state
if 'schemas' not in st.session_state:
    st.session_state.schemas = []
if 'error' not in st.session_state:
    st.session_state.error = ''
if 'edit_table' not in st.session_state:
    st.session_state.edit_table = None
if 'edit_descriptions' not in st.session_state:
    st.session_state.edit_descriptions = {}
if 'messages' not in st.session_state:
    st.session_state.messages = {
        "Schema Generator": {"success": None, "error": None},
        "Ask a Query": {"success": None, "error": None},
        "Audit Tracking": {"success": None, "error": None}
    }
if 'audit_logs' not in st.session_state:
    st.session_state.audit_logs = []
if 'audit_page' not in st.session_state:
    st.session_state.audit_page = 1
if 'audit_total_count' not in st.session_state:
    st.session_state.audit_total_count = 0
if 'query_results' not in st.session_state:
    st.session_state.query_results = {
        "rephrased_question": "",
        "tables": [],
        "table_info": [],
        "sub_questions": [],
        "sub_queries": [],
        "final_query": "",
        "results": [],
        "summary": "",
        "sub_query_results": [],
        "debug_final_prompt": ""
    }
if 'query_processed' not in st.session_state:
    st.session_state.query_processed = False
if 'show_execution_options' not in st.session_state:
    st.session_state.show_execution_options = False
if 'active_tab' not in st.session_state:
    st.session_state.active_tab = "Schema Generator"

# API base URL
API_BASE_URL = "http://localhost:8002"

# Pagination settings
PAGE_SIZE = 100

# Format JSONB changes for display
def format_audit_changes(changes):
    if "column_name" in changes:
        return f"Column: {changes['column_name']}, Old: {changes.get('old_description', 'None')}, New: {changes.get('new_description', 'None')}"
    return f"Old: {changes.get('old_description', 'None')}, New: {changes.get('new_description', 'None')}"

# Fetch audit logs from API with pagination
def fetch_audit_logs(db_config, page=1):
    tab = "Audit Tracking"
    st.session_state.messages[tab]["error"] = None
    st.session_state.messages[tab]["success"] = None
    offset = (page - 1) * PAGE_SIZE
    payload = db_config
    with st.spinner(f"Fetching audit logs (Page {page})..."):
        try:
            response = requests.post(
                f"{API_BASE_URL}/audit/logs?limit={PAGE_SIZE}&offset={offset}",
                json=payload,
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            data = response.json()
            formatted_logs = [
                {
                    "ID": log["id"],
                    "Table Name": log["table_name"],
                    "Event Type": log["event_type"],
                    "Changes": format_audit_changes(log["changes"]),
                    "Event Date": log["event_date"]
                }
                for log in data["logs"]
            ]
            st.session_state.audit_logs = formatted_logs
            st.session_state.audit_total_count = data["total_count"]
            st.session_state.audit_page = page
            st.session_state.messages[tab]["success"] = f"Audit logs fetched successfully (Page {page})"
        except requests.exceptions.HTTPError as e:
            error_detail = e.response.json().get("detail", str(e.response.text)) if e.response else str(e)
            st.session_state.messages[tab]["error"] = f"Error fetching audit logs: {str(e)}\nDetail: {error_detail}"
        except Exception as e:
            st.session_state.messages[tab]["error"] = f"Error fetching audit logs: {str(e)}"
        st.rerun()

# Fetch schemas from API
def fetch_schemas(db_config, domain):
    tab = "Schema Generator"
    st.session_state.messages[tab]["error"] = None
    st.session_state.messages[tab]["success"] = None
    payload = db_config
    with st.spinner("Fetching schemas..."):
        try:
            response = requests.post(
                f"{API_BASE_URL}/schema/extract?domain={domain}",
                json=payload,
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            st.session_state.schemas = response.json()
            st.session_state.messages[tab]["success"] = f"Schemas fetched successfully for domain {domain}"
        except requests.exceptions.HTTPError as e:
            error_detail = e.response.json().get("detail", str(e.response.text)) if e.response else str(e)
            st.session_state.messages[tab]["error"] = f"Error fetching schemas: {str(e)}\nDetail: {error_detail}"
        except Exception as e:
            st.session_state.messages[tab]["error"] = f"Error fetching schemas: {str(e)}"
        st.rerun()

# Generate full table and column descriptions
def generate_full_description(db_config, schema_name, table_name, domain):
    tab = "Schema Generator"
    st.session_state.messages[tab]["error"] = None
    st.session_state.messages[tab]["success"] = None
    payload = {
        "db_config": db_config,
        "request": {
            "schema_name": schema_name,
            "table_name": table_name,
            "domain": domain
        }
    }
    with st.spinner(f"Generating descriptions for {schema_name}.{table_name}..."):
        try:
            response = requests.post(
                f"{API_BASE_URL}/description/generate_full",
                json=payload,
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            data = response.json()
            st.session_state.schemas = [
                data if s["table_name"] == f"{schema_name}.{table_name}" else s
                for s in st.session_state.schemas
            ] + ([data] if not any(
                s["table_name"] == f"{schema_name}.{table_name}" for s in st.session_state.schemas) else [])
            st.session_state.messages[tab]["success"] = f"Descriptions generated successfully for {schema_name}.{table_name}"
        except requests.exceptions.HTTPError as e:
            error_detail = e.response.json().get("detail", str(e.response.text)) if e.response else str(e)
            st.session_state.messages[tab]["error"] = f"Error generating descriptions: {str(e)}\nDetail: {error_detail}"
        except Exception as e:
            st.session_state.messages[tab]["error"] = f"Error generating descriptions: {str(e)}"
        st.rerun()

# Update descriptions in PostgreSQL
def update_descriptions(schema_name, table_name, db_config, table_description, column_descriptions):
    tab = "Schema Generator"
    st.session_state.messages[tab]["error"] = None
    st.session_state.messages[tab]["success"] = None
    payload = {
        "db_config": db_config,
        "request": {
            "schema_name": schema_name,
            "table_name": table_name,
            "table_description": table_description,
            "column_descriptions": column_descriptions
        }
    }
    with st.spinner(f"Updating descriptions for {schema_name}.{table_name}..."):
        try:
            response = requests.post(
                f"{API_BASE_URL}/description/update",
                json=payload,
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            data = response.json()
            st.session_state.schemas = [
                {
                    **s,
                    "table_description": table_description,
                    "columns": [
                        {**col, "description": column_descriptions.get(col["column_name"], col["description"])}
                        for col in s["columns"]
                    ]
                } if s["table_name"] == f"{schema_name}.{table_name}" else s
                for s in st.session_state.schemas
            ]
            st.session_state.edit_table = None
            st.session_state.edit_descriptions = {}
            st.session_state.messages[tab]["success"] = data["message"]
        except requests.exceptions.HTTPError as e:
            error_detail = e.response.json().get("detail", str(e.response.text)) if e.response else str(e)
            st.session_state.messages[tab]["error"] = f"Error updating descriptions: {str(e)}\nDetail: {error_detail}"
        except Exception as e:
            st.session_state.messages[tab]["error"] = f"Error updating descriptions: {str(e)}"
        st.rerun()

# Process query in Ask a Query tab
def process_query(db_config, query_input, domain):
    tab = "Ask a Query"
    st.session_state.messages[tab]["error"] = None
    st.session_state.messages[tab]["success"] = None
    st.session_state.query_processed = False
    st.session_state.show_execution_options = False
    with st.spinner("Processing query..."):
        try:
            response = requests.post(
                f"{API_BASE_URL}/query/process_user_query",
                json={"db_config": db_config, "request": {"query": query_input, "domain": domain, "execute": False}},
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            query_data = response.json()

            if "error" in query_data:
                st.session_state.messages[tab]["error"] = query_data["error"]
                return

            st.session_state.query_results = {
                "rephrased_question": query_data.get("rephrased_question", query_input),
                "tables": query_data.get("tables", []),
                "table_info": query_data.get("table_info", []),
                "sub_questions": query_data.get("sub_questions", []),
                "sub_queries": query_data.get("sub_queries", []),
                "final_query": query_data.get("final_query", ""),
                "results": [],
                "summary": "",
                "sub_query_results": [],
                "debug_final_prompt": ""
            }
            st.session_state.query_processed = True
            st.session_state.messages[tab]["success"] = "Query processed successfully"
        except requests.exceptions.HTTPError as e:
            error_detail = e.response.json().get("detail", str(e.response.text)) if e.response else str(e)
            st.session_state.messages[tab]["error"] = f"Error processing query: {str(e)}\nDetail: {error_detail}"
        except Exception as e:
            st.session_state.messages[tab]["error"] = f"Error processing query: {str(e)}"

# Execute queries based on user selection
def execute_queries(db_config, execution_option):
    tab = "Ask a Query"
    st.session_state.messages[tab]["error"] = None
    st.session_state.messages[tab]["success"] = None
    with st.spinner("Executing queries..."):
        try:
            if execution_option == "Execute final query and summarize":
                response = requests.post(
                    f"{API_BASE_URL}/query/execute",
                    json={"db_config": db_config, "query": st.session_state.query_results["final_query"]},
                    headers={"Content-Type": "application/json"}
                )
                response.raise_for_status()
                query_result = response.json()

                summary_response = requests.post(
                    f"{API_BASE_URL}/query/summarize",
                    json={"results": query_result["results"]},
                    headers={"Content-Type": "application/json"}
                )
                summary_response.raise_for_status()
                summary = summary_response.json().get("summary", "")

                st.session_state.query_results["results"] = query_result["results"]
                st.session_state.query_results["summary"] = summary
                st.session_state.query_results["sub_query_results"] = []
                st.session_state.query_results["debug_final_prompt"] = ""

            elif execution_option == "Execute all sub-queries and summarize":
                sub_query_results = []
                for sub_question, sub_query in zip(st.session_state.query_results["sub_questions"], st.session_state.query_results["sub_queries"]):
                    response = requests.post(
                        f"{API_BASE_URL}/query/execute",
                        json={"db_config": db_config, "query": sub_query},
                        headers={"Content-Type": "application/json"}
                    )
                    response.raise_for_status()
                    sub_query_result = response.json()

                    summary_response = requests.post(
                        f"{API_BASE_URL}/query/summarize",
                        json={"results": sub_query_result["results"]},
                        headers={"Content-Type": "application/json"}
                    )
                    summary_response.raise_for_status()
                    sub_summary = summary_response.json().get("summary", "")

                    sub_query_results.append({
                        "sub_question": sub_question,
                        "sub_query": sub_query,
                        "results": sub_query_result["results"],
                        "summary": sub_summary
                    })

                st.session_state.query_results["sub_query_results"] = sub_query_results

                prompt = (
                    f"Original Question: {st.session_state.query_results['rephrased_question']}\n\n"
                    f"Sub-Questions and Summaries:\n"
                    + "\n".join(
                        f"- Sub-Question: {result['sub_question']}\n  Summary: {result['summary']}"
                        for result in sub_query_results
                    )
                    + "\n\nProvide a final summary that integrates the sub-question summaries to answer the original question."
                )

                st.session_state.query_results["debug_final_prompt"] = prompt

                final_summary_response = requests.post(
                    f"{API_BASE_URL}/query/summarize",
                    json={"prompt": prompt},
                    headers={"Content-Type": "application/json"}
                )
                final_summary_response.raise_for_status()
                final_summary = final_summary_response.json().get("summary", "")

                st.session_state.query_results["results"] = []
                st.session_state.query_results["summary"] = final_summary
                st.session_state.query_results["sub_query_results"] = sub_query_results

            st.session_state.show_execution_options = False
            st.session_state.messages[tab]["success"] = "Queries executed and summarized successfully"
        except requests.exceptions.HTTPError as e:
            error_detail = e.response.json().get("detail", str(e.response.text)) if e.response else str(e)
            st.session_state.messages[tab]["error"] = f"Error executing queries: {str(e)}\nDetail: {error_detail}"
        except Exception as e:
            st.session_state.messages[tab]["error"] = f"Error executing queries: {str(e)}"

# Main app
def main():
    st.title("Database Schema Manager")

    # Create tabs
    tab_names = ["Schema Generator", "Ask a Query", "Audit Tracking"]
    tabs = st.tabs(tab_names)
    schema_tab, query_tab, audit_tab = tabs

    # Schema Generator Tab
    with schema_tab:
        st.session_state.active_tab = "Schema Generator"

        # Display messages
        if st.session_state.messages["Schema Generator"]["success"]:
            st.success(st.session_state.messages["Schema Generator"]["success"])
            st.session_state.messages["Schema Generator"]["success"] = None
        if st.session_state.messages["Schema Generator"]["error"]:
            st.error(st.session_state.messages["Schema Generator"]["error"])
            st.session_state.messages["Schema Generator"]["error"] = None
        if st.session_state.error:
            st.error(st.session_state.error)
            st.session_state.error = ''

        # Database connection inputs
        st.header("Database Connection")
        col1, col2 = st.columns(2)
        with col1:
            dbname = st.text_input("Database Name", value="olist_ecommerce")
            user = st.text_input("User", value="root")
            password = st.text_input("Password", type="password", value="")
        with col2:
            host = st.text_input("Host", value="localhost")
            port = st.text_input("Port", value="5432")
            domain = st.selectbox("Domain", list(CONFIG["domains"].keys()))

        db_config = {
            "dbname": dbname,
            "user": user,
            "password": password,
            "host": host,
            "port": port
        }

        # Fetch schemas button
        if st.button("Fetch Schemas", key="fetch_schemas", type="primary"):
            if not all([dbname, user, host, port]):
                st.session_state.messages["Schema Generator"]["error"] = "Please fill in all required fields (Database Name, User, Host, Port)."
            elif domain not in CONFIG["domains"]:
                st.session_state.messages["Schema Generator"]["error"] = f"Invalid domain: {domain}. Please select a valid domain from config.json."
            else:
                fetch_schemas(db_config, domain)

        # Generate table description
        st.header("Generate Table Description")
        table_options = [table for table in CONFIG["domains"].get(domain, {}).get("tables", [])]
        selected_table = st.selectbox("Select Table to Generate Description", [""] + table_options,
                                      key="generate_table_select")
        if selected_table and st.button("Generate Description for Selected Table", key="generate_selected",
                                       type="primary"):
            schema_name, table_name = selected_table.split('.')
            generate_full_description(db_config, schema_name, table_name, domain)

        # Display schemas
        for schema in st.session_state.schemas:
            st.header(schema["table_name"])
            schema_name, table_name = schema["table_name"].split('.')

            if st.session_state.edit_table == schema["table_name"]:
                with st.form(key=f"edit_form_{schema['table_name']}"):
                    st.subheader("Edit Descriptions")
                    table_description = st.text_area(
                        "Table Description",
                        value=st.session_state.edit_descriptions.get("table_description",
                                                                    schema["table_description"] or ""),
                        key=f"table_desc_{schema['table_name']}"
                    )
                    column_descriptions = {}
                    for col in schema["columns"]:
                        col_desc = st.text_area(
                            f"{col['column_name']} ({col['data_type']})",
                            value=st.session_state.edit_descriptions.get("columns", {}).get(col["column_name"],
                                                                                           col["description"] or ""),
                            key=f"col_desc_{schema['table_name']}_{col['column_name']}"
                        )
                        column_descriptions[col["column_name"]] = col_desc

                    col_save, col_cancel = st.columns(2)
                    with col_save:
                        if st.form_submit_button("Save", type="save"):
                            update_descriptions(schema_name, table_name, db_config, table_description,
                                               column_descriptions)
                    with col_cancel:
                        if st.form_submit_button("Cancel", type="cancel"):
                            st.session_state.edit_table = None
                            st.session_state.edit_descriptions = {}
                            st.rerun()
            else:
                st.write(f"**Description:** {schema['table_description'] or 'No description'}")
                st.subheader("Columns")
                for col in schema["columns"]:
                    st.write(f"- {col['column_name']} ({col['data_type']}): {col['description'] or 'No description'}")

                col_edit, col_update, col_generate = st.columns(3)
                with col_edit:
                    if st.button("Edit Descriptions", key=f"edit_{schema['table_name']}", type="primary"):
                        st.session_state.edit_table = schema["table_name"]
                        st.session_state.edit_descriptions = {
                            "table_description": schema["table_description"],
                            "columns": {col["column_name"]: col["description"] for col in schema["columns"]}
                        }
                        st.rerun()
                with col_update:
                    if st.button("Update Descriptions", key=f"update_{schema['table_name']}", type="primary"):
                        column_descriptions = {col["column_name"]: col["description"] or "" for col in schema["columns"]}
                        update_descriptions(schema_name, table_name, db_config,
                                           schema["table_description"] or "", column_descriptions)
                with col_generate:
                    if not schema["table_description"] and st.button("Generate Description",
                                                                    key=f"generate_{schema['table_name']}",
                                                                    type="primary"):
                        generate_full_description(db_config, schema_name, table_name, domain)

    # Ask a Query Tab
    with query_tab:
        st.session_state.active_tab = "Ask a Query"

        # Display messages
        if st.session_state.messages["Ask a Query"]["success"]:
            st.success(st.session_state.messages["Ask a Query"]["success"])
            st.session_state.messages["Ask a Query"]["success"] = None
        if st.session_state.messages["Ask a Query"]["error"]:
            st.error(st.session_state.messages["Ask a Query"]["error"])
            st.session_state.messages["Ask a Query"]["error"] = None
        if st.session_state.error:
            st.error(st.session_state.error)
            st.session_state.error = ''

        # Query input and buttons
        st.header("Ask a Query")
        query_input = st.text_area("Enter your question about the database", height=150)
        col_submit, col_execute = st.columns(2)
        with col_submit:
            if st.button("Submit Query", key="submit_query", type="primary"):
                if not query_input.strip():
                    st.session_state.messages["Ask a Query"]["error"] = "Please enter a query."
                else:
                    process_query(db_config, query_input, domain)
        with col_execute:
            if st.button("Execute Query", key="execute_query", type="primary", disabled=not st.session_state.query_processed):
                st.session_state.show_execution_options = not st.session_state.show_execution_options
                st.session_state.query_results["results"] = []
                st.session_state.query_results["summary"] = ""
                st.session_state.query_results["sub_query_results"] = []
                st.session_state.query_results["debug_final_prompt"] = ""

        # Debug outputs (toggleable for development)
        if st.session_state.query_results["debug_final_prompt"]:
            with st.expander("Debug: Final Summary Prompt"):
                st.write(st.session_state.query_results["debug_final_prompt"])
        if st.session_state.query_results["sub_query_results"]:
            with st.expander("Debug: Sub-Query Results"):
                st.write(st.session_state.query_results["sub_query_results"])

        # Execution options
        if st.session_state.show_execution_options and st.session_state.query_processed:
            execution_option = st.radio(
                "Select execution option:",
                ["Execute final query and summarize", "Execute all sub-queries and summarize"],
                key="execution_option"
            )
            if st.button("Execute Selected Option", key="execute_selected", type="primary"):
                execute_queries(db_config, execution_option)

        # Display query results
        if st.session_state.query_results["rephrased_question"]:
            st.subheader("Rephrased Question")
            st.write(st.session_state.query_results["rephrased_question"])
            st.subheader("Extracted Tables")
            st.write(f"Tables extracted: {', '.join(st.session_state.query_results['tables']) if st.session_state.query_results['tables'] else 'None'}")
            st.subheader("Extracted Columns")
            for table_info_entry in st.session_state.query_results["table_info"]:
                st.write(f"**{table_info_entry['table_name']}**: {table_info_entry['table_description'] or 'No description'}")
                for col in table_info_entry["columns"]:
                    st.write(f"- {col['column_name']} ({col['data_type']}): {col['description'] or 'No description'}")
            if st.session_state.query_results["sub_questions"]:
                st.subheader("Decomposed Sub-Questions and Queries")
                for sub_question, sub_query in zip(st.session_state.query_results["sub_questions"], st.session_state.query_results["sub_queries"]):
                    st.write(f"**Sub-Question:** {sub_question}")
                    st.code(sub_query, language="sql")
            st.subheader("Final Query")
            st.code(st.session_state.query_results["final_query"], language="sql")

            if st.session_state.query_results["sub_query_results"]:
                st.subheader("Sub-Query Results and Summaries")
                for idx, result in enumerate(st.session_state.query_results["sub_query_results"], 1):
                    st.write(f"**Sub-Question {idx}:** {result['sub_question']}")
                    st.code(result["sub_query"], language="sql")
                    st.write(f"**Results:**")
                    if result["results"]:
                        st.dataframe(result["results"])
                    else:
                        st.write("No results returned.")
                    st.write(f"**Summary:** {result['summary']}")
                st.subheader("Final Summary")
                st.write(st.session_state.query_results["summary"])
            elif st.session_state.query_results["results"]:
                st.subheader("Query Results")
                if st.session_state.query_results["results"]:
                    st.dataframe(st.session_state.query_results["results"])
                else:
                    st.write("No results returned.")
                st.subheader("Summary")
                st.write(st.session_state.query_results["summary"])

    # Audit Tracking Tab
    with audit_tab:
        st.session_state.active_tab = "Audit Tracking"

        # Display messages
        if st.session_state.messages["Audit Tracking"]["success"]:
            st.success(st.session_state.messages["Audit Tracking"]["success"])
            st.session_state.messages["Audit Tracking"]["success"] = None
        if st.session_state.messages["Audit Tracking"]["error"]:
            st.error(st.session_state.messages["Audit Tracking"]["error"])
            st.session_state.messages["Audit Tracking"]["error"] = None
        if st.session_state.error:
            st.error(st.session_state.error)
            st.session_state.error = ''

        # Audit logs section
        st.header("Audit Tracking")
        if st.button("Refresh Audit Logs", key="refresh_audit", type="primary"):
            fetch_audit_logs(db_config, page=1)

        if st.session_state.audit_logs:
            st.subheader("Audit Logs")
            st.dataframe(st.session_state.audit_logs)

            total_pages = max(1, math.ceil(st.session_state.audit_total_count / PAGE_SIZE))
            col_prev, col_page_info, col_next = st.columns([1, 3, 1])
            with col_prev:
                if st.button("Previous", key="audit_prev", type="primary", disabled=st.session_state.audit_page <= 1):
                    fetch_audit_logs(db_config, page=st.session_state.audit_page - 1)
            with col_page_info:
                st.write(f"Page {st.session_state.audit_page} of {total_pages} (Total Records: {st.session_state.audit_total_count})")
            with col_next:
                if st.button("Next", key="audit_next", type="primary", disabled=st.session_state.audit_page >= total_pages):
                    fetch_audit_logs(db_config, page=st.session_state.audit_page + 1)
        else:
            st.write("No audit logs available. Click 'Refresh Audit Logs' to fetch logs.")

if __name__ == "__main__":
    main()