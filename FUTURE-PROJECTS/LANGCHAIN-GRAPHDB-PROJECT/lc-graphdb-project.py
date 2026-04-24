# pip install neo4j langchain-neo4j langchain-openai fastapi
from langchain_neo4j import GraphCypherQAChain, Neo4jGraph
import chainlit as cl
from langchain_openai import ChatOpenAI
from pydantic import BaseModel
import os
os.environ["OPENAI_API_KEY"] = 'sk-proj-bAiHdIB3iDGG-WxyfhKLkncWZ684qLbO0Ioa7w8LVXQXH686vftfV-2mXrup8yuZcCgA'
NEO4J_URI = "bolt://52.91.25.198:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "effort-boys-events"
graph = Neo4jGraph(url=NEO4J_URI, username=NEO4J_USER, password=NEO4J_PASSWORD,enhanced_schema=True,)
from langchain_core.prompts.prompt import PromptTemplate
CYPHER_GENERATION_TEMPLATE = """Task:Generate Cypher statement to query a graph database.
Instructions:
Use only the provided relationship types and properties in the schema.
Do not use any other relationship types or properties that are not provided.
Schema:
{schema}
Note: Do not include any explanations or apologies in your responses.
Do not respond to any questions that might ask anything else than for you to construct a Cypher statement.
Do not include any text except the generated Cypher statement.
Examples: Here are a few examples of generated Cypher statements for particular questions:
# Total no of bookings per airline?
MATCH (u:User)-[:BOOKED_FLIGHT]->(f:Flight)
RETURN f.airline, count(*) as totalbooking
The question is:
{question}
Can you generate human generated format so that everyone understand naturally
"""
CYPHER_GENERATION_PROMPT = PromptTemplate(
    input_variables=["schema", "question"], template=CYPHER_GENERATION_TEMPLATE
)
chain = GraphCypherQAChain.from_llm(
    ChatOpenAI(temperature=0),
    graph=graph,
    verbose=True,
    cypher_prompt=CYPHER_GENERATION_PROMPT,
    return_direct=True,
    validate_cypher=True,
    use_function_response=True,
    allow_dangerous_requests=True,
)
# from fastapi import FastAPI
# # API app
# app = FastAPI()
# # Request schema
# class QueryRequest(BaseModel):
#     query: str
# # API endpoint
# @app.post("/ask")
# def ask_graph(request: QueryRequest):
#     result = chain.invoke({"query": request.query})
#     return {
#         "query": request.query,
#         "result": result
#     }
from langchain_core.prompts import PromptTemplate
from langchain_openai import OpenAI
llm = OpenAI()
outputprompt = PromptTemplate.from_template("""
You are a response formatter.
Your job is to convert database results into a clean and human-readable format.
Rules:
- Never return raw JSON or dictionaries.
- Present the result in a pretty readable format.
- If the result contains a count, write it as a sentence.
- If the result contains multiple items, present them as a numbered list.
- If the result is empty, say "No results found".
- Only return the formatted answer.
Examples:
Input:
{{'totalAirlines': 8}}
Output:
Total airlines are 8.
Input:
{{'totalUsers': 10}}
Output:
Total users are 10.
Input:
[{{'h.name': 'Jackton Grand Hotel'}}, {{'h.name': 'North Johnhaven Grand Hotel'}}]
Output:
Hotels found:
1. Jackton Grand Hotel
2. North Johnhaven Grand Hotel
Input:
[{{'name': 'Emirates'}}, {{'name': 'Qatar Airways'}}, {{'name': 'Lufthansa'}}]
Output:
Airlines found:
1. Emirates
2. Qatar Airways
3. Lufthansa
Now format the following result in a clean and pretty way:
{input}
""")
outputchain = outputprompt | llm
# Chat handler
@cl.on_message
async def main(message: cl.Message):
    query = message.content
    result = chain.invoke({"query": query})
    finalresult = outputchain.invoke({"input": result})
    print(finalresult)
    await cl.Message(
        content=str(finalresult)
    ).send()
