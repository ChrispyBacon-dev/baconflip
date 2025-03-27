# BaconFlip - LiteLLM Discord Bot

This is a customizable Discord chat bot built with Python using the `Nextcord` library. It connects to various Large Language Models (LLMs) via a `liteLLM` proxy instance, allowing flexible AI interactions.

Core features include:
*   LLM chat interaction triggered by `@mention`, configured name (`BOT_TRIGGER_NAME`), or direct replies.
*   Conversation history persistence using Redis.
*   Configurable system prompt to define the bot's personality.
*   Channel muting (`!mute`/`!unmute`) controlled by a designated admin user.
*   Basic fun commands (`!roll`, `!coinflip`, `!choose`, `!avatar`, `!8ball` with LLM).
*   Welcome messages for new users.
*   Deployment via Docker Compose.

## Setup and Installation

**Prerequisites:**
*   Docker & Docker Compose installed.
*   Python 3.8+ (if running locally without Docker).
*   A running `liteLLM` proxy instance accessible from where the bot will run.
*   A Discord Bot Application created via the [Discord Developer Portal](https://discord.com/developers/applications).

**Steps:**

1.  **Clone the Repository:**
    ```bash
    git clone <your-repo-url>
    cd baconflip-bot
    ```
2.  **Create Discord Bot Application:**
    *   Go to the [Discord Developer Portal](https://discord.com/developers/applications).
    *   Create a "New Application".
    *   Go to the "Bot" tab, click "Add Bot".
    *   **Copy the Bot Token.**
    *   Enable **Privileged Gateway Intents**: `SERVER MEMBERS INTENT` and `MESSAGE CONTENT INTENT`.
    *   Use the "OAuth2" -> "URL Generator" to create an invite link with the `bot` scope and necessary permissions (e.g., Read/Send Messages, Read History). Invite the bot to your server.
3.  **Configure Environment Variables:**
    *   Copy `.env.example` to `.env`:
        ```bash
        cp .env.example .env
        ```
    *   **Edit the `.env` file** with your actual values:
        *   `DISCORD_BOT_TOKEN`: Your bot token from the dev portal.
        *   `BOT_TRIGGER_NAME`: The name the bot should listen for (e.g., `baconflip`). **This does not change the bot's Discord username.**
        *   `ADMIN_USER_ID`: Your Discord User ID (enable Developer Mode in Discord settings, right-click your name -> Copy User ID). Required for `!mute`/`!unmute`.
        *   `LITELLM_API_BASE`: The full URL of your running LiteLLM instance (e.g., `http://192.168.1.100:8000/`).
        *   `LLM_MODEL`: The default model string for LiteLLM (e.g., `gemini/gemini-pro`).
        *   `LLM_SYSTEM_PROMPT`: Define the bot's personality here.
        *   (Optional) Configure `COMMAND_PREFIX`, `WELCOME_CHANNEL_ID`, `REDIS_PASSWORD`, `HISTORY_LENGTH`, `LITELLM_API_KEY`.
4.  **Build and Run with Docker Compose:**
    ```bash
    docker-compose up --build -d
    ```
    *   `--build`: Rebuilds the image if code changes.
    *   `-d`: Runs in detached mode (background).
5.  **Check Logs:**
    ```bash
    docker-compose logs -f baconflip-bot  # View bot logs
    docker-compose logs -f redis-baconflip # View Redis logs (if needed)
    ```
    Press `Ctrl+C` to stop viewing logs.
6.  **Stopping:**
    ```bash
    docker-compose down         # Stops and removes containers
    docker-compose down -v      # Stops containers AND removes the Redis data volume
    ```

## Usage

*   **Start Chat:** Mention the bot (`@BotName <query>`) or use its configured name (`<BOT_TRIGGER_NAME> <query>`) at the start of your message.
*   **Continue Chat:** Reply directly to the bot's previous message.
*   **Commands:** Use the configured prefix (`!`, by default) for commands like `!roll`, `!8ball`, etc.
*   **Help:** Use `@BotName help` or `<BOT_TRIGGER_NAME> help`.
*   **Admin:** Use `!mute` or `!unmute` in a channel (requires being the configured `ADMIN_USER_ID`).

## Customization

*   **Personality:** Modify `LLM_SYSTEM_PROMPT` in `.env`.
*   **Trigger Name:** Change `BOT_TRIGGER_NAME` in `.env`.
*   **LLM Model:** Change `LLM_MODEL` in `.env` (ensure it's supported by your LiteLLM setup).
*   **Commands:** Add/modify commands in the `bot/cogs/` directory. Remember to load new cogs in `bot.py`.

## Project Structure

```text
baconflip-bot/
├── bot/                  # Main bot code
│   ├── __init__.py
│   ├── bot.py            # Core bot logic, event handlers
│   ├── cogs/             # Command modules (Cogs)
│   │   ├── __init__.py
│   │   ├── fun_cog.py
│   │   └── admin_cog.py
│   └── utils/            # Utility functions
│       ├── __init__.py
│       └── history.py    # Redis interactions (history, mute)
├── .env.example          # Example environment variables file <<< CONFIGURE THIS
├── .gitignore
├── Dockerfile            # Docker image definition for the bot
├── docker-compose.yml    # Docker Compose setup for bot and Redis
├── README.md             # This file
└── requirements.txt      # Python dependencies
