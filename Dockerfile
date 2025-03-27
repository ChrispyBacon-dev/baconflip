# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set environment variables for Python
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set the working directory in the container
WORKDIR /app

# Install system dependencies if needed (e.g., for hiredis or other C extensions)
# RUN apt-get update && apt-get install -y --no-install-recommends gcc build-essential && rm -rf /var/lib/apt/lists/*

# Install uvloop and project requirements
COPY requirements.txt .
# Consider using --no-cache-dir for smaller final image size
RUN pip install --upgrade pip && \
    pip install uvloop && \
    pip install -r requirements.txt

# Copy the bot code into the container
COPY ./bot /app/bot

# Command to run the bot using uvloop
CMD ["python", "-m", "bot.bot"]
