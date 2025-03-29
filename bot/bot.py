# --- bot.py ---

import nextcord
from nextcord.ext import commands
import os
import sys
import logging
import asyncio
import aiohttp
from dotenv import load_dotenv
import json

# Import utility functions (assuming history/mute are in history.py)
from .utils import history as redis_utils

# --- Setup Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout)
        # logging.FileHandler("bot.log") # Optional
    ]
)
logging.getLogger('nextcord').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Load Environment Variables ---
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
COMMAND_PREFIX = os.getenv('COMMAND_PREFIX', '!')
WELCOME_CHANNEL_ID = int(os.getenv('WELCOME_CHANNEL_ID')) if os.getenv('WELCOME_CHANNEL_ID') else None
WELCOME_MESSAGE_FORMAT = os.getenv('WELCOME_MESSAGE', "Welcome {mention} to {server}! Enjoy your stay.")

LITELLM_API_BASE = os.getenv('LITELLM_API_BASE')
LITELLM_API_KEY = os.getenv('LITELLM_API_KEY')
LLM_MODEL = os.getenv('LLM_MODEL', 'gemini/gemini-pro')
LLM_SYSTEM_PROMPT = os.getenv('LLM_SYSTEM_PROMPT', "You are a helpful assistant.")
SYSTEM_PROMPT_MESSAGE = {"role": "system", "content": LLM_SYSTEM_PROMPT}

try:
    ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID'))
    logger.info(f"Admin User ID set to: {ADMIN_USER_ID}")
except (TypeError, ValueError):
    ADMIN_USER_ID = None
    logger.warning("ADMIN_USER_ID not set or invalid. Admin commands may be disabled.")

# Load Bot Trigger Name
BOT_TRIGGER_NAME = os.getenv('BOT_TRIGGER_NAME', '').strip()
if not BOT_TRIGGER_NAME:
    logger.warning("BOT_TRIGGER_NAME not set! Defaulting to 'baconflip'.")
    BOT_TRIGGER_NAME = 'baconflip' # Fallback default
BOT_TRIGGER_NAME_LOWER = BOT_TRIGGER_NAME.lower()
logger.info(f"Configured Bot Trigger Name: '{BOT_TRIGGER_NAME}' (Listening for this name)")

# --- Basic Checks ---
if not DISCORD_BOT_TOKEN: logger.critical("DISCORD_BOT_TOKEN missing."); sys.exit(1)
if not LITELLM_API_BASE: logger.critical("LITELLM_API_BASE missing."); sys.exit(1)
if WELCOME_CHANNEL_ID is None: logger.warning("WELCOME_CHANNEL_ID not set.")

# --- Intents ---
intents = nextcord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

# --- Initialize Bot and HTTP Session ---
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)
http_session = None

# --- LLM Helper Functions ---
async def get_llm_response(query_content: str, channel_id: int, user_id: int, use_history: bool = True) -> str | None:
    """Gets response from LiteLLM, managing history."""
    global http_session
    if not http_session: logger.error("AIOHTTP session not initialized."); return "Connection unavailable."

    api_url = f"{LITELLM_API_BASE.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if LITELLM_API_KEY: headers["Authorization"] = f"Bearer {LITELLM_API_KEY}"

    try:
        conversation_history = []
        if use_history: conversation_history = await redis_utils.get_history(channel_id, user_id)

        user_message = {"role": "user", "content": query_content}
        messages_payload = [SYSTEM_PROMPT_MESSAGE] + conversation_history + [user_message]

        # Default parameters, adjusted for potential persona
        llm_params = {"temperature": 0.75, "max_tokens": 500} # Default for conversation
        if not use_history: # Params for short, one-off tasks like 8ball/welcome
            llm_params["temperature"] = 0.85 # Higher temp for creativity
            llm_params["max_tokens"] = 150  # Increased slightly for welcome
            logger.debug("Using non-history LLM parameters (creative response expected).")

        data = {"model": LLM_MODEL, "messages": messages_payload, **llm_params}
        logger.debug(f"Sending to LiteLLM ({api_url}) w/ {len(messages_payload)} msgs (History: {use_history}). Params: {llm_params}")

        async with http_session.post(api_url, headers=headers, json=data, timeout=60) as response:
            if response.status == 200:
                response_data = await response.json()
                try:
                    llm_response_content = response_data['choices'][0]['message']['content'].strip()
                    if not llm_response_content: logger.warning("LLM returned empty."); return "Reply hazy..." if not use_history else "Empty response."
                    if use_history: await redis_utils.add_to_history(channel_id, user_id, query_content, llm_response_content)
                    log_suffix = f" for user {user_id} in {channel_id}" + (" (No History)" if not use_history else "")
                    logger.info(f"LLM success{log_suffix}")
                    return llm_response_content
                except (KeyError, IndexError, TypeError) as e: logger.error(f"LLM parse error: {e}. Resp: {response_data}", exc_info=True); return "Cannot predict..." if not use_history else "AI response parse error."
            else:
                error_text = await response.text(); logger.error(f"LiteLLM API Error: {response.status} - {error_text}")
                if response.status == 401: return "No auth (401)." if not use_history else "AI Auth Error."
                elif response.status == 429: return "Too busy (429)." if not use_history else "AI Rate Limit."
                else: return f"Bad outlook ({response.status})." if not use_history else f"AI Error ({response.status})."
    except aiohttp.ClientConnectorError as e: logger.error(f"LLM Connect Err: {api_url}: {e}", exc_info=True); return "No connection." if not use_history else "Couldn't connect to AI."
    except asyncio.TimeoutError: logger.error(f"LLM Timeout: {api_url}"); return "Timeout." if not use_history else "AI request timed out."
    except Exception as e: logger.error(f"LLM interaction error: {e}", exc_info=True); return "Error." if not use_history else "Unexpected error."

async def _trigger_llm_response(message: nextcord.Message, query: str):
    """Internal helper to handle LLM calls triggered by triggers/replies."""
    if not query: logger.debug(f"Ignoring trigger from {message.author.name}, empty query."); return

    async with message.channel.typing():
        try:
            if hasattr(bot, 'get_llm_response') and callable(bot.get_llm_response):
                llm_response = await bot.get_llm_response(query, message.channel.id, message.author.id, use_history=True)
                if llm_response:
                    # Message splitting logic
                    if len(llm_response) <= 2000: await message.reply(llm_response, mention_author=False)
                    else:
                         parts = []
                         while len(llm_response) > 0:
                            split_point = min(len(llm_response), 2000)
                            best_split = llm_response.rfind('\n', 0, split_point)
                            if best_split == -1: best_split = llm_response.rfind(' ', 0, split_point)
                            if best_split == -1 or best_split < 1000: best_split = split_point
                            parts.append(llm_response[:best_split])
                            llm_response = llm_response[best_split:].lstrip()
                         reply_msg = await message.reply(parts[0], mention_author=False)
                         for part in parts[1:]: await message.channel.send(part)
            else: logger.error("get_llm_response missing."); await message.reply("LLM connection error.", mention_author=False)
        except Exception as e: logger.error(f"LLM trigger error: {e}", exc_info=True); await message.reply("Processing error.", mention_author=False)

# --- General Helper Functions ---
async def send_help_dm(user: nextcord.Member):
    """Sends help message via DM."""
    try:
        help_text = f"Hello {user.mention}! Interaction Guide:\n\n"
        help_text += "**LLM Chat:**\n"
        help_text += f"- Start: `@{bot.user.name} <query>` or `{BOT_TRIGGER_NAME} <query>`.\n"
        help_text += f"- Continue: Simply **reply** to my last message.\n\n"
        help_text += f"**Commands ({COMMAND_PREFIX}):**\n"
        for cog_name, cog in bot.cogs.items():
             cmds = cog.get_commands()
             if cmds:
                 is_admin = isinstance(cog, commands.Cog) and cog.__class__.__name__ == "AdminCog"
                 for cmd in cmds:
                     if not cmd.hidden:
                          help_text += f"`{COMMAND_PREFIX}{cmd.name}`"
                          if cmd.signature: help_text += f" `{cmd.signature}`"
                          help_text += f" - {cmd.help or 'No description.'}"
                          if is_admin: help_text += " (Admin Only)"
                          help_text += "\n"
        help_text += f"\n**Other:**\n"
        help_text += f"`@{bot.user.name} help` or `{BOT_TRIGGER_NAME} help` - This message.\n"
        help_text += f"`@{bot.user.name} clear` or `{BOT_TRIGGER_NAME} clear` - Clears *your* chat history...\n"
        await user.send(help_text); logger.info(f"Sent help DM to {user.name}"); return True
    except nextcord.Forbidden: logger.warning(f"Cannot send help DM to {user.name}. DMs disabled?"); return False
    except Exception as e: logger.error(f"Error sending help DM: {e}", exc_info=True); return False

# --- Bot Events ---
@bot.event
async def on_ready():
    """On bot ready."""
    global http_session
    if http_session is None: http_session = aiohttp.ClientSession()
    bot.get_llm_response = get_llm_response # Attach for Cogs
    try: # Redis check
        redis_utils.initialize_redis_pool(); redis_client = await redis_utils.get_redis_client(); await redis_client.ping(); logger.info("Redis connected.")
    except Exception as e: logger.critical(f"Redis connection failed: {e}. History/Mute may fail.", exc_info=True)
    # Load Cogs
    logger.info("Loading Cogs..."); loaded, failed = [], []; cog_dirs = ['./bot/cogs']
    for cog_dir in cog_dirs:
         if not os.path.isdir(cog_dir): logger.error(f"Cog dir not found: {cog_dir}"); continue
         for fn in os.listdir(cog_dir):
             if fn.endswith('.py') and fn != '__init__.py':
                 mod_path = f'{cog_dir.replace("./", "").replace("/", ".")}.{fn[:-3]}'
                 try: bot.load_extension(mod_path); loaded.append(mod_path)
                 except Exception as e: logger.error(f'Cog load fail: {mod_path}: {e}', exc_info=True); failed.append(mod_path)
    logger.info(f"Bot '{bot.user.name}' ({bot.user.id}) Ready.")
    logger.info(f"Prefix: {COMMAND_PREFIX}, Trigger Name: '{BOT_TRIGGER_NAME}'")
    logger.info(f"LiteLLM: {LITELLM_API_BASE}, Model: {LLM_MODEL}")
    logger.info(f"Loaded Cogs: {loaded}"); logger.error(f"Failed Cogs: {failed}") if failed else None
    logger.info(f"Admin ID: {ADMIN_USER_ID}") if ADMIN_USER_ID else logger.warning("Admin ID not set.")
    logger.info(f"Welcome Chan: {WELCOME_CHANNEL_ID}") if WELCOME_CHANNEL_ID else logger.info("Welcome msgs disabled.")
    try: # Set presence
        #status_txt = f"'{BOT_TRIGGER_NAME}' or @mention"
        #await bot.change_presence(activity=nextcord.Activity(type=nextcord.ActivityType.listening, name=status_txt)); logger.info(f"Presence: Listening to {status_txt}")
    pass # Add pass if the try block becomes empty
    except Exception as e: logger.warning(f"Presence set fail: {e}")

@bot.event
async def on_disconnect(): logger.warning("Bot disconnected.")
@bot.event
async def on_resumed(): logger.info("Bot resumed.")

@bot.event
async def on_member_join(member: nextcord.Member):
    """Handle member join with standard and LLM welcome messages."""
    if WELCOME_CHANNEL_ID and member.guild:
        channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
        if channel:
            # --- 1. Send Standard Welcome Message ---
            try:
                std_message = WELCOME_MESSAGE_FORMAT.format(
                    mention=member.mention,
                    user=member.display_name, # Use display_name
                    server=member.guild.name
                )
                await channel.send(std_message)
                logger.info(f"Sent standard welcome message for {member.name} in {channel.name}")
            except nextcord.Forbidden: logger.error(f"Permissions error sending standard welcome in {channel.name}"); return
            except Exception as e: logger.error(f"Error sending standard welcome message: {e}", exc_info=True) # Continue to LLM

            # --- 2. Attempt LLM Welcome Message ---
            try:
                if hasattr(bot, 'get_llm_response') and callable(bot.get_llm_response):
                    logger.debug(f"Attempting LLM welcome for {member.name}")
                    system_prompt_snippet = LLM_SYSTEM_PROMPT[:100] + "..." # Use global prompt
                    welcome_prompt = (
                        f"A new user named '{member.display_name}' just joined the server '{member.guild.name}'.\n"
                        f"In your persona (currently: {system_prompt_snippet}), generate a short, unique, "
                        f"and welcoming message (2-3 sentences max) directly addressing them. "
                        f"Make it flavorful and engaging."
                    )
                    llm_welcome_message = await bot.get_llm_response(
                        welcome_prompt, channel.id, member.id, use_history=False
                    )
                    if llm_welcome_message:
                        # await asyncio.sleep(1) # Optional delay
                        await channel.send(llm_welcome_message)
                        logger.info(f"Sent LLM welcome message for {member.name}")
                    else: logger.warning(f"LLM returned no welcome message for {member.name}.")
                else: logger.warning("get_llm_response missing; cannot generate LLM welcome.")
            except Exception as e: logger.error(f"Error generating/sending LLM welcome: {e}", exc_info=True)
        else: logger.error(f"Welcome channel ID {WELCOME_CHANNEL_ID} not found in {member.guild.name}.")

@bot.event
async def on_message(message: nextcord.Message):
    """Handle incoming messages for triggers, replies, and commands."""
    if message.author == bot.user: return
    if isinstance(message.channel, nextcord.DMChannel): await bot.process_commands(message); return
    if not bot.user: return

    # --- Check Mute Status ---
    is_muted = False # Default value / Fail safe
    try:
        # Check Redis for mute status
        is_muted = await redis_utils.is_channel_muted(message.channel.id)
    except Exception as e:
        logger.error(f"Failed to check mute status for {message.channel.id}: {e}")
        # If check fails, is_muted remains False (the failsafe value)

    # --- Priority 1: Check if it's a reply TO THE BOT ---
    is_reply_to_bot = False
    if message.reference and message.reference.resolved and message.reference.resolved.author == bot.user:
        is_reply_to_bot = True; logger.debug(f"{message.author.name} replied to bot.")
        if is_muted: logger.info(f"Ignoring reply in muted channel {message.channel.id}"); return
        query = message.content.strip(); await _trigger_llm_response(message, query); return

    # --- Priority 2: Check Triggers (Mention or Name at the start) ---
    trigger_used, content_after_trigger = None, None
    if not is_reply_to_bot:
        # BOT_TRIGGER_NAME_LOWER is global
        msg_strip = message.content.strip(); msg_strip_lower = msg_strip.lower()
        actual_mention = None
        if msg_strip.startswith(f'<@!{bot.user.id}>'): actual_mention = f'<@!{bot.user.id}>'
        elif msg_strip.startswith(f'<@{bot.user.id}>'): actual_mention = f'<@{bot.user.id}>'

        if actual_mention:
            trigger_used = actual_mention; content_after_trigger = msg_strip[len(trigger_used):].strip()
        elif msg_strip_lower.startswith(BOT_TRIGGER_NAME_LOWER):
            name_len = len(BOT_TRIGGER_NAME_LOWER)
            if len(msg_strip_lower) == name_len:
                trigger_used = msg_strip[:name_len]
                content_after_trigger = ""
            elif msg_strip_lower[name_len:].startswith((' ', ',', ';', ':', '?','!')):
                # <<< CORRECTED LINES HERE >>>
                idx = name_len # Assign index first
                # Then start the while loop on a new line
                while idx < len(msg_strip) and \
                      (msg_strip[idx].isspace() or msg_strip[idx] in (',', ';', ':', '?', '!')):
                    idx += 1
                # <<< END CORRECTION >>>
                trigger_used = msg_strip[:idx]
                content_after_trigger = msg_strip[idx:].strip()

        if trigger_used is not None:
            content_lower = content_after_trigger.lower()
            if content_lower == 'help':
                if await send_help_dm(message.author): await message.add_reaction('✅')
                else: await message.reply("Help DM failed.", mention_author=False); return
            if content_lower == 'clear':
                if await redis_utils.clear_history(message.channel.id, message.author.id): await message.reply("History cleared.", mention_author=False)
                else: await message.reply("Error clearing history.", mention_author=False); return
            if is_muted: logger.info(f"Ignoring trigger '{trigger_used.strip()}' in muted chan {message.channel.id}"); return
            await _trigger_llm_response(message, content_after_trigger); return

    # --- Priority 3: Process Prefix Commands ---
    if not is_reply_to_bot and trigger_used is None:
        if not is_muted: await bot.process_commands(message)
        else: # Muted: only allow admin commands
            ctx = await bot.get_context(message)
            if ctx and ctx.command and ctx.cog and ctx.cog.__class__.__name__ == 'AdminCog':
                 if ADMIN_USER_ID and ctx.author.id == ADMIN_USER_ID: logger.info(f"Allowing admin cmd '{ctx.command.name}' in muted chan."); await bot.process_commands(message)
                 else: logger.warning(f"Non-admin {ctx.author.name} tried admin cmd '{ctx.command.name}' in muted chan.")
            elif ctx and ctx.command: logger.debug(f"Ignoring prefix cmd '{ctx.invoked_with}' in muted chan.")
            else: logger.debug(f"Ignoring non-cmd message in muted chan.")

@bot.event
async def on_command_error(ctx, error):
    """Global prefix command error handler."""
    if isinstance(error, commands.CommandNotFound): logger.debug(f"Cmd not found: {ctx.message.content}"); return
    elif isinstance(error, commands.MissingRequiredArgument): await ctx.send(f"Missing: `{error.param.name}`.")
    elif isinstance(error, commands.BadArgument): await ctx.send(f"Bad arg. Use `{COMMAND_PREFIX}help {ctx.command.name}`.")
    elif isinstance(error, commands.CommandOnCooldown): await ctx.send(f"Cooldown! Try in {error.retry_after:.1f}s.")
    elif isinstance(error, commands.MissingPermissions): await ctx.send(f"You lack permissions: `{', '.join(error.missing_perms)}`")
    elif isinstance(error, commands.BotMissingPermissions): await ctx.send(f"I lack permissions: `{', '.join(error.missing_perms)}`")
    elif isinstance(error, commands.CheckFailure): await ctx.send("Permission denied for this command.") # Catches @is_admin etc.
    elif isinstance(error, commands.NoPrivateMessage): await ctx.send("Use this in a server channel.")
    else: logger.error(f'Unhandled command error: {ctx.command}: {error}', exc_info=True); await ctx.send("Unexpected error.")

# --- Main Execution ---
async def close_sessions():
    """Gracefully close sessions."""
    if http_session and not http_session.closed: await http_session.close(); logger.info("AIOHTTP session closed.")
    # Optional: await redis_utils.close_redis_pool()

def main():
    """Bot entry point."""
    loop = None
    try:
        try: import uvloop; uvloop.install(); logger.info("Using uvloop.")
        except ImportError: logger.info("Using default asyncio loop.")
        logger.info("Starting bot...")
        bot.run(DISCORD_BOT_TOKEN) # Handles loop internally
    except nextcord.LoginFailure: logger.critical("Login failed: Invalid Discord Bot Token.")
    except Exception as e: logger.critical(f"Fatal error: {e}", exc_info=True)
    finally: # Cleanup attempt even if bot.run fails/stops
        logger.info("Shutdown sequence.")
        try: loop = asyncio.get_running_loop()
        except RuntimeError: loop = None
        if loop and loop.is_running(): loop.run_until_complete(close_sessions())
        else: asyncio.run(close_sessions()) # Fallback

if __name__ == "__main__":
     for dp in ["bot", "bot/cogs", "bot/utils"]: os.makedirs(dp, exist_ok=True); init = os.path.join(dp, "__init__.py"); open(init, 'a').close()
     main()
