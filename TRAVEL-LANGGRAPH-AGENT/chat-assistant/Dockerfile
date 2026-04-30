# Use a slim version for faster ECS startup
FROM python:3.11-slim

# Prevent Python from buffering stdout/stderr (Crucial for CloudWatch)
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install system dependencies if needed (e.g., for certain networking tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy only requirements first to leverage Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Chainlit default port
EXPOSE 8000

# Start Chainlit - we use the -h flag to listen on all interfaces for ECS
CMD ["chainlit", "run", "app.py", "--host", "0.0.0.0", "--port", "8000"]