# Lightweight Python image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy only the requirements first (to reuse the Docker layer cache)
COPY requirements.txt .

# Install dependencies once
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the code
COPY . .

# Start the bot
CMD ["python", "agent.py"]
