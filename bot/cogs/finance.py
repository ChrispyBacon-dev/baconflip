import nextcord
from nextcord.ext import commands
import os
import logging
from alpha_vantage.timeseries import TimeSeries
from alpha_vantage.cryptocurrencies import CryptoCurrencies
from alpha_vantage.fundamentaldata import FundamentalData

# Assume logger is configured similarly to main.py
logger = logging.getLogger('discord')

# --- Helper Function for Error Embeds ---
def create_error_embed(title: str, description: str) -> nextcord.Embed:
    """Creates a standardized error embed."""
    return nextcord.Embed(title=title, description=description, color=nextcord.Color.red())

# --- Finance Cog ---
class FinanceCog(commands.Cog):
    """Commands to fetch stock market and cryptocurrency data."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.command_prefix = getattr(bot, 'command_prefix', '?') # Fallback prefix
        self.api_key = os.getenv('ALPHA_VANTAGE_API_KEY')

        if not self.api_key:
            logger.error("ALPHA_VANTAGE_API_KEY not found in environment variables. Finance commands will fail.")
            # You might want to disable commands or the cog here if the key is missing
            self.ts = None
            self.cc = None
            self.fd = None
        else:
            logger.info("Alpha Vantage API Key loaded.")
            # Initialize Alpha Vantage clients
            # output_format='pandas' is useful but requires pandas dependency.
            # 'json' is simpler for direct parsing without extra libraries.
            try:
                self.ts = TimeSeries(key=self.api_key, output_format='json')
                self.cc = CryptoCurrencies(key=self.api_key, output_format='json')
                self.fd = FundamentalData(key=self.api_key, output_format='json')
                logger.info("Alpha Vantage clients initialized successfully.")
            except Exception as e:
                logger.error(f"Failed to initialize Alpha Vantage clients: {e}")
                self.ts = None
                self.cc = None
                self.fd = None # Ensure clients are None if initialization fails


    async def cog_check(self, ctx: commands.Context) -> bool:
        """Check if the API key is available before running commands."""
        if not self.api_key or not self.ts or not self.cc or not self.fd:
            # Check if ctx has send method before calling it
            if hasattr(ctx, 'send') and callable(ctx.send):
            await ctx.send(embed=create_error_embed(
                "API Key Missing",
                "The Alpha Vantage API key is not configured. Finance commands are disabled."
            ))
            else:
                # Log if ctx cannot send (e.g., check called outside command context)
                logger.warning("cog_check failed for FinanceCog: API Key missing, but context lacks send method.")
            return False # Prevent command execution
        return True # Allow command execution


    @commands.command(name='stock', help="Gets price data for a stock symbol. Usage: `stock <SYMBOL>`")
    async def stock(self, ctx: commands.Context, symbol: str):
        """Fetches the latest quote for a given stock symbol."""
        if not symbol:
            await ctx.send(embed=create_error_embed("Missing Argument", f"Usage: `{self.command_prefix}stock <SYMBOL>`"))
            return

        symbol = symbol.upper()
        # Send initial message and store it
        processing_message = await ctx.send(f"⏳ Fetching stock data for **{symbol}**...")

        try:
            # Use get_quote_endpoint for current price data
            data, meta_data = self.ts.get_quote_endpoint(symbol=symbol)

            if not data:
                 # Edit the original message
                await processing_message.edit(content=None, embed=create_error_embed(
                    "Data Not Found",
                    f"Could not retrieve quote data for symbol **{symbol}**. It might be invalid or delisted."
                ))
                return

            # Extract relevant fields (keys might vary slightly, check Alpha Vantage docs)
            price = data.get('05. price')
            change = data.get('09. change')
            change_percent = data.get('10. change percent')
            volume = data.get('06. volume')
            last_trading_day = data.get('07. latest trading day')

            # Ensure essential data is present
            if price is None or change is None or change_percent is None:
                 await processing_message.edit(content=None, embed=create_error_embed(
                    "Data Error",
                    f"Received incomplete data for **{symbol}**. Price or change information is missing."
                ))
                 return

            embed = nextcord.Embed(
                title=f"Stock Info: {symbol}",
                description=f"Latest data from Alpha Vantage ({last_trading_day or 'N/A'})",
                color=nextcord.Color.green() if float(change) >= 0 else nextcord.Color.red()
            )
            embed.add_field(name="Price", value=f"${float(price):.2f}", inline=True)
            embed.add_field(name="Change", value=f"{float(change):+.2f} ({change_percent})", inline=True) # Added + sign
            embed.add_field(name="Volume", value=f"{int(volume):,}" if volume else 'N/A', inline=True)

            embed.set_footer(text="Data provided by Alpha Vantage. Free plan has usage limits.")
             # Edit the original message
            await processing_message.edit(content=None, embed=embed)
            logger.info(f"Stock command executed by {ctx.author.name} for symbol {symbol}.")

        except ValueError as ve: # Often indicates rate limiting or invalid input format for the API library
             await processing_message.edit(content=None, embed=create_error_embed(
                 "API Error",
                 f"Failed to fetch data for **{symbol}**. Possible reasons:\n"
                 f"- Invalid symbol format.\n"
                 f"- API rate limit reached (Free tier: 25 requests/day, 5/minute). Please wait.\n"
                 #f"```\n{ve}\n```" # Avoid showing raw errors to users unless debugging
             ))
             logger.warning(f"Alpha Vantage API ValueError for symbol {symbol}: {ve}")
        except Exception as e:
            await processing_message.edit(content=None, embed=create_error_embed(
                "Error",
                f"An unexpected error occurred while fetching data for **{symbol}**."
            ))
            logger.error(f"Error fetching stock data for {symbol}: {e}", exc_info=True)


    @commands.command(name='crypto', help="Gets crypto exchange rate. Usage: `crypto <CRYPTO> [FIAT]`")
    async def crypto(self, ctx: commands.Context, crypto_symbol: str, fiat_symbol: str = 'USD'):
        """Fetches the exchange rate for a cryptocurrency pair (e.g., BTC to USD)."""
        if not crypto_symbol:
            await ctx.send(embed=create_error_embed("Missing Argument", f"Usage: `{self.command_prefix}crypto <CRYPTO_SYMBOL> [FIAT_SYMBOL]`"))
            return

        crypto_symbol = crypto_symbol.upper()
        fiat_symbol = fiat_symbol.upper()
        pair = f"{crypto_symbol}/{fiat_symbol}"

        # Send initial message and store it
        processing_message = await ctx.send(f"⏳ Fetching exchange rate for **{pair}**...")

        try: # <<< START OF TRY BLOCK
            data, _ = self.cc.get_digital_currency_exchange_rate(
                from_currency=crypto_symbol,
                to_currency=fiat_symbol
            )

            if not data or 'Realtime Currency Exchange Rate' not in data:
                 # Edit the original message
                await processing_message.edit(content=None, embed=create_error_embed(
                    "Data Not Found",
                    f"Could not retrieve exchange rate for **{pair}**. Check if symbols are valid."
                ))
                return

            rate_data = data['Realtime Currency Exchange Rate']
            from_code = rate_data.get('1. From_Currency Code')
            from_name = rate_data.get('2. From_Currency Name')
            to_code = rate_data.get('3. To_Currency Code')
            to_name = rate_data.get('4. To_Currency Name')
            exchange_rate = rate_data.get('5. Exchange Rate')
            last_refreshed = rate_data.get('6. Last Refreshed')
            # bid_price = rate_data.get('8. Bid Price') # Sometimes available
            # ask_price = rate_data.get('9. Ask Price') # Sometimes available

            if not all([from_code, to_code, exchange_rate]):
                await processing_message.edit(content=None, embed=create_error_embed(
                    "Data Error",
                    f"Received incomplete data for **{pair}**. Key information missing."
                ))
                return

            embed = nextcord.Embed(
                title=f"Crypto Exchange Rate: {from_name} ({from_code}) to {to_name} ({to_code})",
                description=f"Last refreshed: {last_refreshed or 'N/A'}",
                color=nextcord.Color.gold() # Or choose another color
            )
            embed.add_field(name="Exchange Rate", value=f"1 {from_code} = {float(exchange_rate):.8f} {to_code}", inline=False) # Show more decimal places for crypto

            # Add bid/ask if available
            # if bid_price and ask_price:
            #     embed.add_field(name="Bid Price", value=f"{float(bid_price):.8f} {to_code}", inline=True)
            #     embed.add_field(name="Ask Price", value=f"{float(ask_price):.8f} {to_code}", inline=True)

            embed.set_footer(text="Data provided by Alpha Vantage. Free plan has usage limits.")
            # Edit the original message
            await processing_message.edit(content=None, embed=embed)
            logger.info(f"Crypto command executed by {ctx.author.name} for pair {pair}.")

        # --- ADDED ERROR HANDLING ---
        except ValueError as ve: # Rate limiting or invalid symbols
             await processing_message.edit(content=None, embed=create_error_embed(
                 "API Error",
                 f"Failed to fetch data for **{pair}**. Possible reasons:\n"
                 f"- Invalid symbol(s).\n"
                 f"- API rate limit reached (Free tier: 25 requests/day, 5/minute). Please wait.\n"
                 #f"```\n{ve}\n```"
             ))
             logger.warning(f"Alpha Vantage API ValueError for pair {pair}: {ve}")
        except Exception as e:
            await processing_message.edit(content=None, embed=create_error_embed(
                "Error",
                f"An unexpected error occurred while fetching data for **{pair}**."
            ))
            logger.error(f"Error fetching crypto data for {pair}: {e}", exc_info=True)
        # --- END OF ADDED ERROR HANDLING ---

# --- Setup Function ---
def setup(bot: commands.Bot):
    """Adds the FinanceCog to the bot."""
    # Check if API key exists before adding the cog
    if not os.getenv('ALPHA_VANTAGE_API_KEY'):
        logger.error("ALPHA_VANTAGE_API_KEY not found. FinanceCog will not be loaded.")
    else:
        bot.add_cog(FinanceCog(bot))
        logger.info("FinanceCog added.")
        try:
        try: # <<< START OF TRY BLOCK
            data, _ = self.cc.get_digital_currency_exchange_rate(
                from_currency=crypto_symbol,
                to_currency=fiat_symbol
            )

            if not data or 'Realtime Currency Exchange Rate' not in data:
                await ctx.message.edit(content=None, embed=create_error_embed(
                 # Edit the original message
                await processing_message.edit(content=None, embed=create_error_embed(
                    "Data Not Found",
                    f"Could not retrieve exchange rate for **{pair}**. Check if symbols are valid."
                ))
                return

            rate_data = data['Realtime Currency Exchange Rate']
            from_code = rate_data.get('1. From_Currency Code')
            from_name = rate_data.get('2. From_Currency Name')
            to_code = rate_data.get('3. To_Currency Code')
            to_name = rate_data
            to_name = rate_data.get('4. To_Currency Name')
            exchange_rate = rate_data.get('5. Exchange Rate')
            last_refreshed = rate_data.get('6. Last Refreshed')
            # bid_price = rate_data.get('8. Bid Price') # Sometimes available
            # ask_price = rate_data.get('9. Ask Price') # Sometimes available

