# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set environment variables for Python
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set the working directory in the container
WORKDIR /app

# --- Install git ---
# GitPython requires the git executable to be installed in the container's OS.
# Combine update, install, and cleanup into one RUN layer to reduce image size.
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    # Clean up apt cache to keep the image smaller
    rm -rf /var/lib/apt/lists/*

# --- Install Python Dependencies ---
# Copy requirements first to leverage Docker layer caching if requirements don't change
COPY requirements.txt .
# Install uvloop and project requirements using --no-cache-dir for smaller final image
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir uvloop && \
    pip install --no-cache-dir -r requirements.txt

# --- Copy Application Code ---
# Copy the ENTIRE project context (including the .git directory) into the WORKDIR
# Make sure you run `docker build` from the ROOT of your project directory
# (the directory that contains your .git folder, bot folder, Dockerfile, requirements.txt etc.)
COPY . .

# --- Run Command ---
# Command to run the bot using module execution (assumes bot/bot.py exists)
CMD ["python", "-m", "bot.bot"]