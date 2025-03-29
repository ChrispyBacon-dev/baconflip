import nextcord
from nextcord.ext import commands
import time
from datetime import datetime
import logging

# Assume logger is configured similarly to main.py
logger = logging.getLogger('discord')

class InfoCog(commands.Cog):
    """Commands for displaying information."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Get the command prefix from the bot instance if possible, otherwise use default
        self.command_prefix = getattr(bot, 'command_prefix', '?') # Fallback prefix

    @commands.command(name='ping', help="Shows the bot's latency.")
    async def ping(self, ctx: commands.Context):
        """Calculates and displays the bot's latency."""
        start_time = time.monotonic()
        message = await ctx.send("Pinging...")
        end_time = time.monotonic()
        latency = (end_time - start_time) * 1000
        await message.edit(content=f"Pong! Latency: {latency:.2f}ms\nAPI Latency: {self.bot.latency * 1000:.2f}ms")
        logger.info(f"Ping command executed by {ctx.author.name}. Latency: {latency:.2f}ms")

    @commands.command(name='serverinfo', help="Displays information about the server.")
    @commands.guild_only()
    async def serverinfo(self, ctx: commands.Context):
        """Provides detailed information about the current server."""
        guild = ctx.guild
        if not guild:
            await ctx.send("This command can only be used in a server.")
            return

        embed = nextcord.Embed(title=f"Server Info: {guild.name}", color=nextcord.Color.blue())
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.add_field(name="ID", value=guild.id, inline=True)
        embed.add_field(name="Owner", value=guild.owner.mention if guild.owner else 'Unknown', inline=True)
        embed.add_field(name="Members", value=guild.member_count, inline=True)

        embed.add_field(name="Text Channels", value=len(guild.text_channels), inline=True)
        embed.add_field(name="Voice Channels", value=len(guild.voice_channels), inline=True)
        embed.add_field(name="Roles", value=len(guild.roles), inline=True)

        embed.add_field(name="Created At", value=guild.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"), inline=False)
        # You can add boost level, features etc. if desired guild.premium_tier, guild.features

        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        logger.info(f"Serverinfo command executed by {ctx.author.name} for guild {guild.name} ({guild.id}).")

    @commands.command(name='userinfo', help="Displays information about a user (or yourself). Usage: `userinfo [@user]`")
    @commands.guild_only() # Needs guild to get member object easily
    async def userinfo(self, ctx: commands.Context, member: nextcord.Member = None):
        """Provides detailed information about a specified member or the command user."""
        if member is None:
            member = ctx.author # Default to the person who invoked the command

        embed = nextcord.Embed(title=f"User Info: {member.display_name}", color=member.color) # Use member's top role color
        embed.set_thumbnail(url=member.display_avatar.url)

        embed.add_field(name="Full Name", value=f"{member.name}#{member.discriminator}", inline=True)
        embed.add_field(name="ID", value=member.id, inline=True)
        embed.add_field(name="Nickname", value=member.nick or 'None', inline=True)

        embed.add_field(name="Joined Server", value=member.joined_at.strftime("%Y-%m-%d %H:%M:%S UTC") if member.joined_at else 'Unknown', inline=False)
        embed.add_field(name="Account Created", value=member.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"), inline=False)

        # Roles (limit display if too many)
        roles = [role.mention for role in member.roles[1:]] # Exclude @everyone
        role_str = ', '.join(roles) if roles else 'None'
        if len(role_str) > 1024: # Embed field value limit
            role_str = role_str[:1020] + "..."
        embed.add_field(name=f"Roles ({len(roles)})", value=role_str, inline=False)

        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        logger.info(f"Userinfo command executed by {ctx.author.name} for user {member.name} ({member.id}).")

    @commands.command(name='avatar', help="Shows a user's avatar. Usage: `avatar [@user]`")
    async def avatar(self, ctx: commands.Context, user: nextcord.User = None):
        """Displays the avatar of the specified user or the command user."""
        if user is None:
            user = ctx.author

        embed = nextcord.Embed(title=f"{user.name}'s Avatar", color=nextcord.Color.green())
        embed.set_image(url=user.display_avatar.url)
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)
        logger.info(f"Avatar command executed by {ctx.author.name} for user {user.name} ({user.id}).")

# This function is needed by nextcord to load the cog
def setup(bot: commands.Bot):
    bot.add_cog(InfoCog(bot))
    logger.info("InfoCog loaded successfully.")

