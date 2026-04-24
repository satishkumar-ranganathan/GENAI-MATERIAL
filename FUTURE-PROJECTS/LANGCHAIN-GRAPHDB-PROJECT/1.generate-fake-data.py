# Make sure you are install required packages 
# Colab !pip install neo4j faker

from neo4j import GraphDatabase
import random
from faker import Faker
import datetime
# Initialize Faker for generating realistic names, cities, and emails
fake = Faker()
# Neo4j connection
NEO4J_URI = "bolt://3.87.23.184:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "trainers-mountain-garage"
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
# Config: how many entities to generate
NUM_USERS = 10
NUM_CITIES = 5
NUM_FLIGHTS = 15
NUM_HOTELS = 8
NUM_BOOKINGS = 20
# Helper functions to generate random times
def random_datetime(start, end):
    return start + datetime.timedelta(seconds=random.randint(0, int((end - start).total_seconds())))
# Step 1: Create Cities
cities = [fake.city() for _ in range(NUM_CITIES)]
with driver.session() as session:
    for city in cities:
        session.run("MERGE (c:City {name:$name})", name=city)
# Step 2: Create Airlines (real names)
real_airlines = ["Emirates", "IndiGo", "Flydubai", "Lufthansa", "Qatar Airways", "Etihad", "Singapore Airlines", "Air India"]
with driver.session() as session:
    for airline in real_airlines:
        session.run("MERGE (a:Airline {name:$name})", name=airline)
# Step 3: Create Users
users = [{"userId": f"U{i}", "name": fake.name(), "email": fake.email()} for i in range(NUM_USERS)]
with driver.session() as session:
    for u in users:
        session.run("MERGE (u:User {userId:$userId, name:$name, email:$email})", **u)
# Step 4: Create Flights
flights = []
start_date = datetime.datetime.now()
end_date = start_date + datetime.timedelta(days=30)
with driver.session() as session:
    for i in range(NUM_FLIGHTS):
        from_city = random.choice(cities)
        to_city = random.choice([c for c in cities if c != from_city])
        airline = random.choice(real_airlines)  # pick from real airline names
        dep_time = random_datetime(start_date, end_date)
        arr_time = dep_time + datetime.timedelta(hours=random.randint(2, 12))
        flight_id = f"F{i}"
        flights.append(flight_id)
        session.run(
            """
            MERGE (f:Flight {flightId:$flightId, airline:$airline, fromCity:$fromCity, toCity:$toCity, departureTime:$depTime, arrivalTime:$arrTime})
            """,
            flightId=flight_id, airline=airline, fromCity=from_city, toCity=to_city,
            depTime=dep_time.isoformat(), arrTime=arr_time.isoformat()
        )

# Step 5: Create Hotels
hotels = []
with driver.session() as session:
    for i in range(NUM_HOTELS):
        city = random.choice(cities)
        hotel_id = f"H{i}"
        hotels.append(hotel_id)
        session.run(
            "MERGE (h:Hotel {hotelId:$hotelId, name:$name, city:$city, stars:$stars})",
            hotelId=hotel_id, name=f"{city} Grand Hotel", city=city, stars=random.randint(3, 5)
        )
# Step 6: Create Bookings (link Users -> Flights -> Hotels)
with driver.session() as session:
    for i in range(NUM_BOOKINGS):
        user = random.choice(users)["userId"]
        flight = random.choice(flights)
        hotel = random.choice(hotels)
        booking_id = f"B{i}"
        booking_date = (start_date - datetime.timedelta(days=random.randint(1, 10))).date().isoformat()
        status = random.choice(["Confirmed", "Pending", "Cancelled"])
        session.run(
            """
            MERGE (b:Booking {bookingId:$bookingId, bookingDate:$bookingDate, status:$status})
            WITH b
            MATCH (u:User {userId:$user}), (f:Flight {flightId:$flight}), (h:Hotel {hotelId:$hotel})
            MERGE (u)-[:HAS_BOOKING]->(b)
            MERGE (b)-[:INCLUDES_FLIGHT]->(f)
            MERGE (b)-[:INCLUDES_HOTEL]->(h)
            MERGE (u)-[:BOOKED_FLIGHT]->(f)
            MERGE (u)-[:BOOKED_HOTEL]->(h)
            """,
            bookingId=booking_id, bookingDate=booking_date, status=status,
            user=user, flight=flight, hotel=hotel
        )
print("Dynamic travel data generated successfully!")
driver.close()
