import nextcord
from nextcord.ext import commands
import random
import re
import logging
import asyncio # Needed for typing indicator

logger = logging.getLogger(__name__)

class FunCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name='roll', help='Rolls dice. Format: NdN (e.g., 2d6, d20). Defaults to 1d6.')
    async def roll(self, ctx, dice_str: str = '1d6'):
        """Rolls dice based on the NdN format."""
        match = re.match(r'(\d*)d(\d+)', dice_str.lower())
        if not match:
            await ctx.send("Invalid format! Use NdN (e.g., `!roll 2d6`, `!roll d20`).")
            return

        num_dice = int(match.group(1)) if match.group(1) else 1
        num_sides = int(match.group(2))

        if num_dice <= 0 or num_sides <= 0:
            await ctx.send("Number of dice and sides must be positive.")
            return
        if num_dice > 100: # Add a limit to prevent abuse
             await ctx.send("Whoa there, partner! Let's keep it under 100 dice.")
             return
        if num_sides > 1000: # Limit sides too
             await ctx.send("That's a lot of sides! Try something 1000 or less.")
             return

        rolls = [random.randint(1, num_sides) for _ in range(num_dice)]
        total = sum(rolls)

        if num_dice == 1:
            await ctx.send(f"{ctx.author.mention} rolled a **{total}** (1d{num_sides})")
        else:
            rolls_str = ', '.join(map(str, rolls))
            await ctx.send(f"{ctx.author.mention} rolled **{total}** ({num_dice}d{num_sides}: [{rolls_str}])")

    @commands.command(name='coinflip', help='Flips a coin.')
    async def coinflip(self, ctx):
        """Flips a coin."""
        result = random.choice(["Heads", "Tails"])
        await ctx.send(f"{ctx.author.mention}, it's **{result}!**")

    @commands.command(name='8ball', aliases=['eightball'], help='Ask the Magic 8-Ball a question using AI.')
    async def eight_ball(self, ctx, *, question: str):
        """Provides a random Magic 8-Ball style response using the LLM."""
        if not question:
            await ctx.send("You need to ask the Magic 8-Ball a question!")
            return

        # --- LLM Integration ---
        # Show typing indicator while waiting for the LLM
        async with ctx.typing():
            # Craft the prompt for the LLM to act like an 8-Ball
            llm_prompt = (
                f"You are playing the role of a classic Magic 8-Ball. "
                f"Respond to the following user question with a short, cryptic answer in the typical Magic 8-Ball style "
                f"(examples: 'It is certain.', 'Outlook not so good.', 'Cannot predict now.'). "
                f"Keep your answer under 10 words and do not break character. Do not reference the user's question in your answer."
                f"\n\nUser Question: \"{question}\""
            )

            answer = "Reply hazy, try again." # Default fallback
            try:
                # Check if the get_llm_response function exists on the bot object
                if hasattr(self.bot, 'get_llm_response') and callable(self.bot.get_llm_response):
                    # Call the LLM function attached to the bot instance
                    # Pass use_history=False to prevent using/saving chat history for this command
                    llm_response = await self.bot.get_llm_response(
                        query_content=llm_prompt,
                        channel_id=ctx.channel.id, # Needed for potential logging within get_llm_response
                        user_id=ctx.author.id,     # Needed for potential logging
                        use_history=False          # IMPORTANT: Isolate 8-ball calls
                    )
                    if llm_response is not None:
                        answer = llm_response
                else:
                    logger.error("get_llm_response not found on bot object. Is it defined and attached in on_ready?")
                    answer = "My sources say no (Internal Error)."

            except Exception as e:
                 logger.error(f"Error calling LLM for 8ball command: {e}", exc_info=True)
                 answer = "Don't count on it (Internal Error)."
        # --- End LLM Integration ---

        # Display the answer using an embed
        embed = nextcord.Embed(title="🎱 Magic 8-Ball AI 🎱", color=nextcord.Color.dark_purple())
        # Use the original user question here for context in the embed
        embed.add_field(name="Your Question", value=f"```{question}```", inline=False)
        # Display the LLM's generated answer
        embed.add_field(name="Answer", value=f"**{answer}**", inline=False)
        embed.set_footer(text=f"Asked by {ctx.author.display_name}")
        await ctx.send(embed=embed)

    @commands.command(name='choose', help='Randomly chooses from a list of options.')
    async def choose(self, ctx, *, choices: str):
        """Randomly chooses between multiple options provided."""
        options = [choice.strip() for choice in choices.split('|') if choice.strip()] # Split by '|'
        if len(options) < 2:
            await ctx.send("Please provide at least two options separated by `|` (e.g., `!choose pizza | burger | salad`).")
            return

        chosen_option = random.choice(options)
        await ctx.send(f"{ctx.author.mention}, out of `{', '.join(options)}`...\nI choose **{chosen_option}**!")

    @commands.command(name='avatar', help='Displays the avatar of a user.')
    async def avatar(self, ctx, member: nextcord.Member = None):
        """Shows the avatar URL of the specified member or the command author."""
        if member is None:
            member = ctx.author # Default to the person who used the command

        avatar_url = member.display_avatar.url

        embed = nextcord.Embed(title=f"{member.display_name}'s Avatar", color=member.color)
        embed.set_image(url=avatar_url)
        embed.set_footer(text=f"Requested by {ctx.author.display_name}")
        await ctx.send(embed=embed)

# This function is required for the cog to be loaded
def setup(bot):
    bot.add_cog(FunCog(bot))
    logger.info("FunCog loaded successfully.")

