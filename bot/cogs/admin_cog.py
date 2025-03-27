# --- bot/cogs/admin_cog.py ---

import nextcord
from nextcord.ext import commands
import os
import logging
import asyncio # Might be needed for delays, good practice to have if doing async ops

# Use relative import '..' to go up one directory from cogs/ to bot/ then down to utils/
# Assumes your utils file containing Redis functions is named history.py
from ..utils import history as redis_utils

logger = logging.getLogger(__name__)

# --- Admin Check ---
# Load ADMIN_USER_ID at the module level for the check function
try:
    # Convert to int immediately to catch errors early
    ADMIN_ID = int(os.getenv('ADMIN_USER_ID'))
    logger.info(f"AdminCog: Admin ID loaded as {ADMIN_ID}")
except (TypeError, ValueError):
    ADMIN_ID = None
    logger.warning("AdminCog: ADMIN_USER_ID not set or invalid in .env. Admin commands will be disabled.")

def is_admin():
    """Decorator check: Ensures the command invoker is the defined admin user."""
    async def predicate(ctx: commands.Context) -> bool:
        # Check if ADMIN_ID was loaded correctly
        if ADMIN_ID is None:
            logger.warning(f"Admin check failed for command '{ctx.command.qualified_name}': ADMIN_USER_ID not configured.")
            # Optional: Send message directly, though CheckFailure is usually handled globally
            # await ctx.send("Admin commands are disabled because ADMIN_USER_ID is not set.")
            return False # Explicitly deny if admin isn't set

        # Check if the author's ID matches the loaded admin ID
        is_admin_user = ctx.author.id == ADMIN_ID
        if not is_admin_user:
             logger.warning(f"Unauthorized attempt: User {ctx.author.name} ({ctx.author.id}) tried to use admin command '{ctx.command.qualified_name}'.")
        return is_admin_user
    # Return the check created by commands.check()
    return commands.check(predicate)

# --- Cog Definition ---
class AdminCog(commands.Cog, name="Admin Commands"): # Added a Cog name
    """Commands restricted to the bot administrator."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if ADMIN_ID is None:
            logger.error("AdminCog initialized, but ADMIN_USER_ID is not set. Commands in this cog require it.")

    @commands.command(name='mute', help='(Admin Only) Mutes LLM name/mention/reply responses in this channel.')
    @is_admin() # Apply the custom admin check
    @commands.guild_only() # Ensure command is not used in DMs
    async def mute_channel(self, ctx: commands.Context):
        """(Admin Only) Mutes the bot's LLM responses in the current channel."""
        channel_id = ctx.channel.id
        try:
            success = await redis_utils.set_channel_mute(channel_id, muted=True)
            if success:
                await ctx.send(f"🔇 Okay, I will now ignore name/mention/reply triggers in `{ctx.channel.name}` (except for `help`/`clear`). Use `!unmute` here to re-enable.")
            else:
                await ctx.send("⚠️ There was an error trying to mute this channel in Redis.")
        except Exception as e:
            logger.error(f"Error executing mute command: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred while trying to mute.")


    @commands.command(name='unmute', help='(Admin Only) Unmutes LLM name/mention/reply responses in this channel.')
    @is_admin() # Apply the custom admin check
    @commands.guild_only() # Ensure command is not used in DMs
    async def unmute_channel(self, ctx: commands.Context):
        """(Admin Only) Unmutes the bot's LLM responses in the current channel."""
        channel_id = ctx.channel.id
        try:
            success = await redis_utils.set_channel_mute(channel_id, muted=False)
            if success:
                await ctx.send(f"🔊 Okay, I will resume responding to name/mention/reply triggers in `{ctx.channel.name}`.")
            else:
                await ctx.send("⚠️ There was an error trying to unmute this channel in Redis.")
        except Exception as e:
            logger.error(f"Error executing unmute command: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred while trying to unmute.")


    @commands.command(name='testwelcome', help='(Admin Only) Simulates the welcome message sequence.')
    @is_admin()
    @commands.guild_only()
    # Allow optional member argument to test with different users, defaults to command author
    async def test_welcome(self, ctx: commands.Context, member_to_test: nextcord.Member = None):
        """(Admin Only) Triggers the welcome message logic for testing purposes."""

        if member_to_test is None:
            member_to_test = ctx.author # Use the person running the command as the test member

        logger.info(f"Admin {ctx.author.name} initiated welcome test for user {member_to_test.name}")

        # --- Get Welcome Channel ---
        welcome_channel_id_str = os.getenv('WELCOME_CHANNEL_ID')
        if not welcome_channel_id_str:
            await ctx.send("⚠️ Welcome channel ID is not configured in `.env`. Cannot test.")
            return

        try:
            welcome_channel_id = int(welcome_channel_id_str)
            welcome_channel = ctx.guild.get_channel(welcome_channel_id)
            if not welcome_channel:
                await ctx.send(f"⚠️ Cannot find the configured welcome channel (ID: {welcome_channel_id}).")
                return
            # Check bot permissions in that channel
            if not welcome_channel.permissions_for(ctx.guild.me).send_messages:
                 await ctx.send(f"⚠️ I don't have permission to send messages in the welcome channel (`{welcome_channel.name}`).")
                 return

        except ValueError:
             await ctx.send("⚠️ Welcome channel ID in `.env` is not a valid number.")
             return
        except Exception as e:
             logger.error(f"Error getting welcome channel for test: {e}", exc_info=True)
             await ctx.send("An error occurred while trying to find the welcome channel.")
             return

        await ctx.send(f"Simulating welcome for **{member_to_test.display_name}** in {welcome_channel.mention}...")

        # --- Mimic on_member_join Logic ---
        # 1. Standard Welcome
        try:
            std_format = os.getenv('WELCOME_MESSAGE', "Welcome {mention} to {server}!")
            std_message = std_format.format(
                mention=member_to_test.mention,
                user=member_to_test.display_name, # Use display_name
                server=ctx.guild.name
            )
            await welcome_channel.send(f"**(Test Run - Standard)**\n{std_message}")
            logger.info(f"Test: Sent standard welcome for {member_to_test.name}")
        except Exception as e:
            logger.error(f"Test: Error sending standard welcome: {e}", exc_info=True)
            await ctx.send(f"⚠️ Error sending standard welcome part: {e}")

        # 2. LLM Welcome
        try:
            # Check if the get_llm_response method exists and is callable
            if hasattr(self.bot, 'get_llm_response') and callable(self.bot.get_llm_response):
                system_prompt_snippet = os.getenv('LLM_SYSTEM_PROMPT', 'Default Persona')[:100] + "..."
                welcome_prompt = (
                    f"A user named '{member_to_test.display_name}' is being welcomed to the server '{ctx.guild.name}'.\n"
                    f"In your persona (currently: {system_prompt_snippet}), generate a short, unique, "
                    f"and welcoming message (2-3 sentences max) directly addressing them. "
                    f"Make it flavorful and engaging."
                )

                llm_welcome = await self.bot.get_llm_response(
                    query_content=welcome_prompt,
                    channel_id=welcome_channel.id, # For potential logging inside get_llm_response
                    user_id=member_to_test.id, # Use the simulated member's ID
                    use_history=False # Do not use/save conversation history
                )

                if llm_welcome:
                    # Optional short delay before sending LLM message
                    # await asyncio.sleep(1)
                    await welcome_channel.send(f"**(Test Run - LLM)**\n{llm_welcome}")
                    logger.info(f"Test: Sent LLM welcome for {member_to_test.name}")
                else:
                    logger.warning(f"Test: LLM returned no welcome message.")
                    await ctx.send("ℹ️ LLM returned no welcome message for the test.")
            else:
                logger.warning("Test: get_llm_response not found on bot.")
                await ctx.send("⚠️ LLM function not available for test.")
        except Exception as e:
             logger.error(f"Test: Error generating/sending LLM welcome: {e}", exc_info=True)
             await ctx.send(f"⚠️ Error during LLM welcome part: {e}")

        await ctx.send("✅ Welcome test sequence complete.")


    # Cog-specific error handler for commands in AdminCog
    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        # Check if the error occurred in a command belonging to this specific cog
        if ctx.cog is not self:
            return # Ignore errors from other cogs

        # Extract the original error if it's wrapped
        original_error = getattr(error, "original", error)

        if isinstance(original_error, commands.CheckFailure):
            # This catches errors from @is_admin() or @commands.guild_only() etc.
            logger.warning(f"Check failed for admin command '{ctx.command.qualified_name}' by {ctx.author.name}: {original_error}")
            await ctx.send("⛔ Sorry, you don't have permission to use this command or it cannot be used here.")
        elif isinstance(original_error, commands.NoPrivateMessage):
            await ctx.send("🔒 This command can only be used in a server channel.")
        else:
            # Log other errors specific to commands in this cog if not handled globally
             logger.error(f"Unhandled error in AdminCog command '{ctx.command.qualified_name}': {error}", exc_info=True)
             # Optionally send a generic error message to the user
             # await ctx.send("An unexpected error occurred in the admin command.")


# Setup function required by Nextcord to load the cog
def setup(bot: commands.Bot):
    # Add the cog to the bot
    bot.add_cog(AdminCog(bot))
    logger.info("AdminCog loaded successfully.")
