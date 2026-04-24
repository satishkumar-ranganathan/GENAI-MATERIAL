import streamlit as st

# Create tabs for different functionalities
tab_names = ["Schema Generator", "Ask a Query", "Audit Tracking"]
tabs = st.tabs(tab_names)
schema_tab, query_tab, audit_tab = tabs

st.success("Hey it success")
st.error("Hey it success")
with schema_tab:
    col1, col2 = st.columns(2)
    with col1:
        dbname = st.text_input("Database Name", value="123")
        user = st.text_input("User", value="abc")
        password = st.text_input("Password", type="password", value="")
    with col2:
        host = st.text_input("Host", value="")
        port = st.text_input("Port", value="")
        option = st.selectbox(
            "How would you like to be contacted?",
            ("Email", "Home phone", "Mobile phone"),
        )