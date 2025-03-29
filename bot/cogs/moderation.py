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

    def
