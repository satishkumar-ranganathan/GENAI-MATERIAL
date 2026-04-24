# Use the official Python 3.13 slim image for a small footprint
FROM python:3.13-slim

# Set environment variables
# Prevents Python from writing pyc files to disc
ENV PYTHONDONTWRITEBYTECODE=1
# Prevents Python from buffering stdout and stderr
ENV PYTHONUNBUFFERED=1
# Set the port for Chainlit
ENV PORT=8000

# Install system dependencies (build-essentials if you have C-based python libs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create and set the working directory
WORKDIR /app

# Create a non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Install Python dependencies
# Copy only requirements first to leverage Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Set ownership of the app directory to the non-root user
RUN chown -R appuser:appuser /app

# Switch to the non-root user
USER appuser

# Expose the port Chainlit runs on
EXPOSE 8000

# Command to run the application
# Use --host 0.0.0.0 to ensure it's reachable in the container network
CMD ["chainlit", "run", "app.py", "--host", "0.0.0.0", "--port", "8000"]