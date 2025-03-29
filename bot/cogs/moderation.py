import nextcord
from nextcord.ext import commands
import logging
import os

# Assume logger is configured similarly to main.py
logger = logging.getLogger('discord')
# Load Admin User ID from environment for checks
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', 0)) # Default to 0 if not set

class ModerationCog(commands.Cog):
    """Basic moderation commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.command_prefix = getattr(bot, 'command_prefix', '?') # Fallback prefix

    # --- Permission Checks ---
    async def cog_check(self, ctx: commands.Context):
        """Check if the user is in a guild for most commands here."""
        if not ctx.guild:
            raise commands.NoPrivateMessage("Moderation commands cannot be used in DMs.")
        return True # Proceed if in a guild

    # Example basic command structure (can be expanded)
    @commands.command(name="ping_mod")
    @commands.has_permissions(kick_members=True) # Example permission check
    async def ping_mod(self, ctx: commands.Context):
        """A simple command to check if the cog is loaded (requires kick perms)."""
        await ctx.send(f"Moderation Cog Pong! (Prefix: {self.command_prefix})")

    @commands.command(name="admin_only_test")
    async def admin_only_test(self, ctx: commands.Context):
        """A command only the bot admin can run."""
        if ctx.author.id != ADMIN_USER_ID:
            await ctx.send("You do not have permission to use this command.")
            return
        await ctx.send(f"Hello Admin User {ctx.author.mention}!")

# Setup function to load the cog (standard practice)
def setup(bot: commands.Bot):
    bot.add_cog(ModerationCog(bot))
    logger.info("ModerationCog loaded successfully.") # Log success on load
