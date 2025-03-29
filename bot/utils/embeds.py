import nextcord
import datetime

def create_error_embed(title: str, description: str) -> nextcord.Embed:
    """Creates a standardized error embed."""
    embed = nextcord.Embed(
        title=f"❌ Error: {title}",
        description=description,
        color=nextcord.Color.red(),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    # You could add a footer, author, etc. here if desired
    # embed.set_footer(text="Something went wrong.")
    return embed

def create_success_embed(title: str, description: str) -> nextcord.Embed:
    """Creates a standardized success embed."""
    embed = nextcord.Embed(
        title=f"✅ Success: {title}",
        description=description,
        color=nextcord.Color.green(),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    return embed

def create_info_embed(title: str, description: str) -> nextcord.Embed:
    """Creates a standardized informational embed."""
    embed = nextcord.Embed(
        title=f"ℹ️ Info: {title}",
        description=description,
        color=nextcord.Color.blue(),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    return embed

# Add more embed helper functions here as needed, e.g., for specific commands
