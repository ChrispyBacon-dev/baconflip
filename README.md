 # BaconFlip - Your Personality-Driven, LiteLLM-Powered Discord Bot

[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Docker Image Size (latest semver)](https://img.shields.io/docker/image-size/ChrispyBacon/baconflip/latest)](https://hub.docker.com/r/ChristpyBacon/baconflip)  <!-- Replace with your DockerHub info -->
[![GitHub Stars](https://img.shields.io/github/stars/ChrispyBacon-dev/BaconFlip?style=social)](https://github.com/ChrispyBacon-dev/BaconFlip) <!-- Replace with your Github info -->

**Tired of generic Discord bots? Ready to unleash the full power of cutting-edge AI with your *own* unique twist?**

BaconFlip isn't just another chat bot; it's a highly customizable framework built with Python (`Nextcord`) designed to connect seamlessly to virtually **any Large Language Model (LLM)** via a `liteLLM` proxy. Whether you want to chat with GPT-4o, Gemini, Claude, Llama, or your own local models, BaconFlip provides the bridge.

## Why Check Out BaconFlip?

*   🧠 **Universal LLM Access:** Stop being locked into one AI provider. `liteLLM` lets you switch models easily.
*   🎭 **Deep Personality Customization:** Define your bot's unique character, quirks, and speaking style with a simple `LLM_SYSTEM_PROMPT` in the config. Want a flirty bacon bot? A stoic philosopher? A pirate captain? Go wild!
*   💬 **Real Conversations:** Thanks to Redis-backed memory, BaconFlip remembers recent interactions per-user, leading to more natural and engaging follow-up conversations.
*   🚀 **Easy Docker Deployment:** Get the bot (and its Redis dependency) running quickly and reliably using Docker Compose.
*   🔧 **Flexible Interaction:** Engage the bot via `@mention`, its configurable name (`BOT_TRIGGER_NAME`), or simply by replying to its messages.
*   🎉 **Fun & Dynamic Features:** Includes LLM-powered commands like `!8ball` and unique, AI-generated welcome messages alongside standard utilities.
*   ⚙️ **Solid Foundation:** Built with modern Python practices (`asyncio`, Cogs) making it a great base for adding your own features.

## Core Features Include:

*   LLM chat interaction (via Mention, Name Trigger, or Reply)
*   Redis-backed conversation history
*   Configurable system prompt for personality
*   Admin-controlled channel muting (`!mute`/`!unmute`)
*   Standard + LLM-generated welcome messages (`!testwelcome` included)
*   Fun commands: `!roll`, `!coinflip`, `!choose`, `!avatar`, `!8ball` (LLM)
*   Docker Compose deployment setup

---

## Setup and Installation

**Prerequisites:**
*   Docker & Docker Compose installed.
*   Python 3.8+ (if running locally without Docker).
*   A running `liteLLM` proxy instance accessible from where the bot will run. [Project on Github](https://github.com/BerriAI/litellm)
*   A Discord Bot Application created via the [Discord Developer Portal](https://discord.com/developers/applications).

**Steps:**

1.  **Clone the Repository:**
    ```bash
    git clone https://github.com/ChrispyBacon-dev/baconflip.git
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
```
## Contributing

Contributions are welcome! Please feel free to submit pull requests with bug fixes, new features, or improvements to the documentation.

1.  Fork the repository.
2.  Create a new branch for your feature.
3.  Make your changes.
4.  Submit a pull request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

*   [Nextcord](https://github.com/nextcord/nextcord): For providing the Discord API wrapper.
*   [LiteLLM](https://github.com/BerriAI/litellm): For enabling easy access to multiple LLMs.
*   [Redis](https://redis.io/): For providing the in-memory data store for conversation history.