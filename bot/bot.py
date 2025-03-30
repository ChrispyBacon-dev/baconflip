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
import re # <-- Add re for regex parsing
try:
    import git # <-- Add GitPython import
except ImportError:
    print("GitPython not found. Please install it: pip install GitPython", file=sys.stderr)
    git = None # Set to None if import fails

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
# Suppress GitPython info logs unless needed for debugging
logging.getLogger('git').setLevel(logging.WARNING)
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

GITHUB_REPO_URL = "https://github.com/ChrispyBacon-dev/baconflip" # <-- Add Repo URL

# --- Git Version Function ---
def get_git_version():
    """Generates a version string using Git metadata: ver.MAJOR.MINOR.PATCH-BRANCH."""
    if not git:
        logger.error("GitPython not installed, cannot determine version.")
        return "ver. unknown (GitPython missing)"

    try:
        # Search parent directories to find the .git folder
        repo = git.Repo(search_parent_directories=True)
        repo_dir = repo.git_dir # Get the actual .git dir path for logging
        logger.info(f"Found Git repository at: {repo_dir}")

        # --- Branch ---
        branch_name = "unknown-branch"
        try:
            if repo.head.is_detached:
                # Use short commit hash if in detached HEAD state
                branch_name = f"detached-{repo.head.object.hexsha[:7]}"
                logger.warning(f"Git HEAD is detached, using commit hash: {branch_name}")
            else:
                branch_name = repo.active_branch.name
                logger.debug(f"Current Git branch: {branch_name}")
        except TypeError as e:
             logger.warning(f"Could not determine branch name (possibly unborn branch?): {e}")
             # Attempt fallback (might still fail if truly no commits/branch yet)
             try: branch_name = repo.git.rev_parse('--abbrev-ref', 'HEAD').strip()
             except Exception: branch_name = "unborn-branch" # Final fallback


        # --- Tag and Patch Count ---
        major_minor = "0.0" # Default if no tags
        patch = "0"       # Default if no tags/commits

        try:
            # Use git describe to find the nearest tag matching v*.*
            # --dirty adds '-dirty' suffix if the working tree has modifications
            description = repo.git.describe('--tags', '--long', '--match', 'v*.*', '--dirty')
            logger.debug(f"Git describe output: {description}")

            # Example: v0.2-15-gabcdef1 or v0.2-15-gabcdef1-dirty
            match = re.match(r'^(v\d+\.\d+)-(\d+)-g([0-9a-f]+)(-dirty)?$', description)
            if match:
                major_minor = match.group(1)[1:] # Remove the leading 'v' -> 0.2
                patch = match.group(2)           # -> 15
                is_dirty = bool(match.group(4))  # Check if '-dirty' suffix exists
                logger.debug(f"Parsed from tag: MAJOR.MINOR={major_minor}, PATCH={patch}, Dirty={is_dirty}")
                if is_dirty:
                    branch_name += "-dirty" # Append dirty status to branch
                    logger.info("Working directory is dirty.")
            else:
                 logger.warning(f"Could not parse git describe output: {description}. Using defaults.")
                 # Fallback: Maybe it's exactly on a tag?
                 try:
                     exact_tag = repo.git.describe('--tags', '--exact-match', '--match', 'v*.*', '--dirty')
                     match_exact = re.match(r'^(v\d+\.\d+)(-dirty)?$', exact_tag)
                     if match_exact:
                         major_minor = match_exact.group(1)[1:]
                         patch = "0" # Exactly on tag means 0 commits since tag
                         is_dirty = bool(match_exact.group(2))
                         logger.info(f"Exactly on tag: {exact_tag}, Parsed: MAJOR.MINOR={major_minor}, Dirty={is_dirty}")
                         if is_dirty: branch_name += "-dirty"
                     else: raise ValueError("Exact tag format mismatch") # Force fallback
                 except Exception:
                    logger.warning("Not exactly on a tag matching v*.*, counting all commits as patch.")
                    # Count total commits if describe failed parsing (e.g., very early history)
                    patch = str(repo.git.rev_list('--count', 'HEAD'))


        except git.GitCommandError as e:
            # This often happens if no tags like v*.* are found
            logger.warning(f"Git describe command failed (likely no 'v*.*' tags found): {e}")
            logger.info("Falling back to counting total commits for patch number.")
            # Fallback: Use 0.0 as base and count total commits as PATCH
            major_minor = "0.0"
            try:
                patch = str(repo.git.rev_list('--count', 'HEAD'))
                # Check dirty status separately if describe failed
                if repo.is_dirty():
                    branch_name += "-dirty"
                    logger.info("Working directory is dirty (checked separately).")
            except git.GitCommandError as count_e:
                 logger.error(f"Failed to count commits: {count_e}")
                 patch = "err" # Indicate error counting commits

        except Exception as e:
            logger.error(f"Unexpected error during git describe parsing: {e}", exc_info=True)
            major_minor = "err"
            patch = "err"

        # --- Construct Final Version ---
        version_string = f"ver. {major_minor}.{patch}-{branch_name}"
        logger.info(f"Determined Version: {version_string}")
        return version_string

    except git.InvalidGitRepositoryError:
        logger.error("Script is not running within a Git repository.")
        return "ver. unknown (not a Git repo)"
    except Exception as e:
        logger.error(f"An unexpected error occurred while getting Git version: {e}", exc_info=True)
        return "ver. error (unknown)"

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

# --- Get and Store Version String Early ---
# Store it on the bot object so commands/cogs can access it if needed
bot.version_string = get_git_version()
logger.info(f"Bot Version set to: {bot.version_string}")


# --- LLM Helper Functions ---
# ... (rest of your LLM functions remain the same) ...
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
                            # Try splitting at newline first, then space, then hard split
                            best_split = llm_response.rfind('\n', 0, split_point)
                            if best_split == -1 or best_split < split_point * 0.75: # Prefer newline if it's reasonably close
                                best_split = llm_response.rfind(' ', 0, split_point)
                            if best_split == -1 or best_split < split_point * 0.5: # Don't split on space if it's too early
                                best_split = split_point # Hard split if no good newline/space found

                            parts.append(llm_response[:best_split])
                            llm_response = llm_response[best_split:].lstrip() # Remove leading space after split

                         if parts: # Ensure we have something to send
                             reply_msg = await message.reply(parts[0], mention_author=False)
                             for part in parts[1:]:
                                 await asyncio.sleep(0.2) # Small delay between parts
                                 await message.channel.send(part)
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

        # Get all commands, including the new 'about' command
        # Sort commands alphabetically for consistency
        all_commands = sorted(bot.commands, key=lambda cmd: cmd.name)

        for cmd in all_commands:
            if not cmd.hidden:
                is_admin = cmd.cog_name == "AdminCog" # Assuming Admin commands are in AdminCog
                help_text += f"`{COMMAND_PREFIX}{cmd.name}`"
                if cmd.signature: help_text += f" `{cmd.signature}`"
                help_text += f" - {cmd.help or 'No description.'}"
                if is_admin: help_text += " (Admin Only)"
                help_text += "\n"

        help_text += f"\n**Other:**\n"
        help_text += f"`@{bot.user.name} help` or `{BOT_TRIGGER_NAME} help` - This message.\n"
        help_text += f"`@{bot.user.name} clear` or `{BOT_TRIGGER_NAME} clear` - Clears *your* chat history for this channel.\n" # Clarified scope
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
         if not os.path.isdir(cog_dir): logger.warning(f"Cog dir not found: {cog_dir}"); continue # Changed to warning
         for fn in os.listdir(cog_dir):
             if fn.endswith('.py') and fn != '__init__.py':
                 mod_path = f'{cog_dir.replace("./", "").replace("/", ".")}.{fn[:-3]}'
                 try: bot.load_extension(mod_path); loaded.append(mod_path)
                 except Exception as e: logger.error(f'Cog load fail: {mod_path}: {e}', exc_info=True); failed.append(mod_path)
    logger.info(f"Bot '{bot.user.name}' ({bot.user.id}) Ready.")
    logger.info(f"Prefix: {COMMAND_PREFIX}, Trigger Name: '{BOT_TRIGGER_NAME}'")
    logger.info(f"Version: {bot.version_string}") # <-- Log version on ready too
    logger.info(f"LiteLLM: {LITELLM_API_BASE}, Model: {LLM_MODEL}")
    logger.info(f"Loaded Cogs: {loaded}"); logger.error(f"Failed Cogs: {failed}") if failed else None
    logger.info(f"Admin ID: {ADMIN_USER_ID}") if ADMIN_USER_ID else logger.warning("Admin ID not set.")
    logger.info(f"Welcome Chan: {WELCOME_CHANNEL_ID}") if WELCOME_CHANNEL_ID else logger.info("Welcome msgs disabled.")
    try: # Set presence
        status_txt = f"v{bot.version_string.split('-')[0][5:]} | {COMMAND_PREFIX}help" # e.g., v0.2.15 | !help
        await bot.change_presence(activity=nextcord.Game(name=status_txt))
        logger.info(f"Presence set to: Playing {status_txt}")
    except Exception as e: logger.warning(f"Presence set fail: {e}")

# ... (on_disconnect, on_resumed, on_member_join remain the same) ...
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
                    system_prompt_snippet = LLM_SYSTEM_PROMPT[:100] + ("..." if len(LLM_SYSTEM_PROMPT)>100 else "") # Use global prompt
                    welcome_prompt = (
                        f"A new user named '{member.display_name}' just joined the server '{member.guild.name}'.\n"
                        f"In your persona (currently: {system_prompt_snippet}), generate a short, unique, "
                        f"and welcoming message (2-3 sentences max) directly addressing them. "
                        f"Make it flavorful and engaging. Do not mention your persona explicitly." # Added instruction
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
    if message.author == bot.user or message.author.bot: return # Ignore self and other bots
    if isinstance(message.channel, nextcord.DMChannel):
        # Basic response for DMs, maybe guide them to help or server
        if message.content.startswith(COMMAND_PREFIX): # Allow commands in DMs if applicable
             await bot.process_commands(message)
        # else: # Optional: Respond to non-command DMs
        #    await message.channel.send(f"Hi {message.author.mention}! Please interact with me in a server channel or use `{COMMAND_PREFIX}help`.")
        return # Stop processing after handling DM

    if not bot.user: return # Bot not fully ready

    # --- Check Mute Status ---
    is_muted = False # Default value / Fail safe
    try:
        is_muted = await redis_utils.is_channel_muted(message.channel.id)
    except Exception as e:
        logger.error(f"Failed to check mute status for {message.channel.id}: {e}")
        # If check fails, is_muted remains False (the failsafe value)

    # --- Priority 1: Check if it's a reply TO THE BOT ---
    is_reply_to_bot = False
    if message.reference and message.reference.resolved and isinstance(message.reference.resolved, nextcord.Message) and message.reference.resolved.author == bot.user:
        is_reply_to_bot = True
        logger.debug(f"{message.author.name} replied to bot in channel {message.channel.id}.")
        if is_muted:
            logger.info(f"Ignoring reply from {message.author.name} in muted channel {message.channel.id}")
            return # Explicitly stop if muted
        query = message.content.strip()
        await _trigger_llm_response(message, query)
        return # Handled as a reply

    # --- Priority 2: Check Triggers (Mention or Name at the start) ---
    trigger_used, content_after_trigger = None, None
    if not is_reply_to_bot: # Only check triggers if not already handled as a reply
        msg_strip = message.content.strip()
        msg_strip_lower = msg_strip.lower()

        # Check for direct @mention
        actual_mention = None
        if msg_strip.startswith(f'<@!{bot.user.id}>'): actual_mention = f'<@!{bot.user.id}>'
        elif msg_strip.startswith(f'<@{bot.user.id}>'): actual_mention = f'<@{bot.user.id}>'

        if actual_mention:
            trigger_used = actual_mention
            content_after_trigger = msg_strip[len(trigger_used):].strip()
            logger.debug(f"Triggered by mention from {message.author.name}")
        # Check for BOT_TRIGGER_NAME at the start, followed by space or punctuation or end of string
        elif BOT_TRIGGER_NAME_LOWER and msg_strip_lower.startswith(BOT_TRIGGER_NAME_LOWER):
            name_len = len(BOT_TRIGGER_NAME_LOWER)
            # Ensure it's either the full message or followed by a separator
            if len(msg_strip_lower) == name_len or (len(msg_strip_lower) > name_len and not msg_strip_lower[name_len].isalnum()):
                 # Find the actual end of the trigger name (case-insensitive match might differ slightly)
                 idx = name_len
                 # Consume following non-alphanumeric chars (space, comma, etc.)
                 while idx < len(msg_strip) and not msg_strip[idx].isalnum() and not msg_strip[idx].isspace():
                    idx += 1
                 # Consume any spaces after punctuation
                 while idx < len(msg_strip) and msg_strip[idx].isspace():
                    idx +=1

                 trigger_used = msg_strip[:idx] # The part that was actually typed
                 content_after_trigger = msg_strip[idx:].strip()
                 logger.debug(f"Triggered by name '{trigger_used.strip()}' from {message.author.name}")


        if trigger_used is not None: # A trigger was definitely used
            # Handle special trigger commands first
            content_lower = content_after_trigger.lower() if content_after_trigger else ""
            if content_lower == 'help':
                if await send_help_dm(message.author): await message.add_reaction('✅') # DM sent
                else: await message.add_reaction('❌') # DM failed
                return # Handled help request
            elif content_lower == 'clear':
                # Add confirmation? Maybe not needed for personal history clear.
                cleared = await redis_utils.clear_history(message.channel.id, message.author.id)
                if cleared: await message.reply("Your conversation history with me in this channel has been cleared.", mention_author=False)
                else: await message.reply("Could not clear history (maybe none exists or error occurred).", mention_author=False)
                return # Handled clear request

            # Now, handle the LLM trigger if the channel isn't muted
            if is_muted:
                logger.info(f"Ignoring trigger '{trigger_used.strip()}' from {message.author.name} in muted channel {message.channel.id}")
                return # Explicitly stop if muted
            await _trigger_llm_response(message, content_after_trigger)
            return # Handled as trigger/LLM call

    # --- Priority 3: Process Prefix Commands ---
    # Only process if not a reply to the bot and no trigger was used
    if not is_reply_to_bot and trigger_used is None:
        if not is_muted:
            await bot.process_commands(message)
            # logger.debug(f"Processing potential command: {message.content[:50]}") # Optional debug
        else: # Channel is muted
            ctx = await bot.get_context(message)
            # Check if it's a command AND if the user is the Admin OR if the command's cog is AdminCog (more flexible)
            is_admin_command = False
            if ctx and ctx.command:
                is_admin_user = ADMIN_USER_ID and ctx.author.id == ADMIN_USER_ID
                is_admin_cog = ctx.cog and ctx.cog.__class__.__name__ == 'AdminCog'
                # Allow if user is admin OR if the command belongs to the Admin Cog (requires check in cog too)
                # For simplicity here, let's primarily rely on ADMIN_USER_ID for overrides in muted channels.
                # You might refine this logic based on specific command needs.
                if is_admin_user:
                    logger.info(f"Allowing admin command '{ctx.command.name}' by admin user {ctx.author.name} in muted channel {message.channel.id}.")
                    await bot.process_commands(message) # Process the command
                elif is_admin_cog: # Maybe log attempt by non-admin
                     logger.warning(f"Non-admin user {ctx.author.name} attempted AdminCog command '{ctx.command.name}' in muted channel {message.channel.id}.")
                     # Do NOT process the command
                else: # Normal command attempt in muted channel
                    logger.debug(f"Ignoring prefix command '{ctx.invoked_with}' from {message.author.name} in muted channel {message.channel.id}.")
            # else: logger.debug(f"Ignoring non-command message in muted channel {message.channel.id}.") # Reduce noise

# --- New ?about Command ---
@bot.command(name='about', help="Shows bot version and source code link.")
async def about_command(ctx: commands.Context):
    """Displays the bot's version information and a link to its source code."""
    version_info = bot.version_string or "ver. unknown (error retrieving)" # Fallback
    embed = nextcord.Embed(
        title=f"{bot.user.name} - About",
        description=(
            f"**Version:** {version_info}\n\n"
            f"I'm an open-source bot! Find my code here:\n{GITHUB_REPO_URL}"
        ),
        color=nextcord.Color.blue()
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url) # Add bot's avatar
    await ctx.send(embed=embed)


# --- Global Command Error Handler ---
@bot.event
async def on_command_error(ctx, error):
    """Global prefix command error handler."""
    if isinstance(error, commands.CommandNotFound):
        logger.debug(f"Command not found: {ctx.message.content}")
        # Optionally, send a subtle hint or react
        # await ctx.message.add_reaction('❓')
        return # Don't clutter chat for typos
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Oops! You missed the `{error.param.name}` argument. Use `{COMMAND_PREFIX}help {ctx.command.name}` for details.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"Hmm, that wasn't the right type of argument. Check `{COMMAND_PREFIX}help {ctx.command.name}`.")
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Take a breath! This command is on cooldown. Try again in {error.retry_after:.1f}s.", delete_after=5)
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send(f"You don't have the required permissions: `{', '.join(error.missing_perms)}`")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send(f"I can't do that because I lack permissions: `{', '.join(error.missing_perms)}`")
    elif isinstance(error, commands.CheckFailure):
        # This catches custom checks like @commands.is_owner() or specific role checks
        logger.warning(f"Check failed for command '{ctx.command.name}' by user {ctx.author.name} ({ctx.author.id})")
        await ctx.send("You do not have permission to use this command.") # Generic check failure message
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.send("This command can only be used in a server channel, not in DMs.")
    elif isinstance(error, commands.CommandInvokeError):
        original = error.original
        logger.error(f'Unhandled error in command {ctx.command.name}: {original.__class__.__name__}: {original}', exc_info=True)
        await ctx.send("An unexpected error occurred while running that command. The developers have been notified (probably).")
    else:
        # Catch-all for other errors derived from commands.CommandError
        logger.error(f'Unhandled command error type: {type(error)} for command {ctx.command}: {error}', exc_info=True)
        await ctx.send("An unexpected error occurred.")

# --- Main Execution ---
async def close_sessions():
    """Gracefully close sessions."""
    if http_session and not http_session.closed: await http_session.close(); logger.info("AIOHTTP session closed.")
    if hasattr(redis_utils, 'close_redis_pool'): await redis_utils.close_redis_pool(); logger.info("Redis pool closed.")

def main():
    """Bot entry point."""
    loop = None
    try:
        try: import uvloop; uvloop.install(); logger.info("Using uvloop.")
        except ImportError: logger.info("Using default asyncio loop.")
        logger.info("Starting bot...")
        bot.run(DISCORD_BOT_TOKEN) # Handles loop internally
    except nextcord.LoginFailure: logger.critical("Login failed: Invalid Discord Bot Token.")
    except git.InvalidGitRepositoryError as e: # Catch Git error during startup if needed
         logger.critical(f"Fatal Error: Not a Git repository or Git is unavailable. {e}")
    except Exception as e: logger.critical(f"Fatal error during bot startup: {e}", exc_info=True)
    finally: # Cleanup attempt even if bot.run fails/stops
        logger.info("Shutdown sequence initiating...")
        # Graceful shutdown using asyncio
        try:
            # Get the current event loop. If bot.run() completed successfully,
            # it might have already closed the loop. If it crashed, loop might exist.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None # No running loop

            if loop and loop.is_running():
                 # Create a task to close sessions and wait for it
                 shutdown_task = loop.create_task(close_sessions())
                 # Wait for the task to complete, with a timeout
                 loop.run_until_complete(asyncio.wait_for(shutdown_task, timeout=5.0))
                 logger.info("Shutdown task completed.")
                 # Give tasks spawned by bot.run() a moment to cancel/finish
                 # pending = asyncio.all_tasks(loop=loop) # Get tasks other than current
                 # loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True)) # Wait briefly
                 # loop.run_until_complete(loop.shutdown_asyncgens()) # Shutdown async generators
                 # loop.close() # Close the loop if we controlled it (bot.run usually does this)
                 # logger.info("Asyncio loop closed.")
            else:
                 # If no loop is running, run close_sessions in a new temporary loop
                 logger.info("No running asyncio loop found, running cleanup synchronously.")
                 asyncio.run(close_sessions())

        except Exception as e:
            logger.error(f"Error during shutdown cleanup: {e}", exc_info=True)
        logger.info("Shutdown complete.")

if __name__ == "__main__":
     # Ensure required directories exist
     for dp in ["bot", "bot/cogs", "bot/utils"]: os.makedirs(dp, exist_ok=True); init = os.path.join(dp, "__init__.py"); open(init, 'a').close()
     main()