import os
import nextcord
from nextcord.ext import commands
# Import yfinance - REMEMBER TO pip install yfinance
import yfinance as yf
import datetime
import logging
# Assuming your embed helper is still in the same place
from bot.utils.embeds import create_error_embed

logger = logging.getLogger(__name__)

# --- Helper Function for Timestamp Conversion ---
def format_yf_timestamp(timestamp: int | None) -> str:
    """Converts a Yahoo Finance Unix timestamp to a readable string."""
    if timestamp is None:
        return "N/A"
    try:
        # Assume timestamp is UTC
        dt_object = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
        # Format as desired (e.g., YYYY-MM-DD HH:MM:SS UTC)
        return dt_object.strftime('%Y-%m-%d %H:%M:%S %Z')
    except Exception as e:
        logger.warning(f"Could not format timestamp '{timestamp}': {e}")
        return "Invalid Date"
# --- End Helper ---


class FinanceCog(commands.Cog):
    """
    Commands for fetching financial data (stocks, crypto) using yfinance.
    Note: yfinance relies on scraping Yahoo Finance and may break if Yahoo changes its site.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.command_prefix = os.getenv('BOT_PREFIX', '?') # Get from env or default to '?'
        logger.info("FinanceCog initialized using yfinance.")
        # No API key needed for yfinance

    @commands.command(name='stock', help="Gets stock quote. Usage: `stock <SYMBOL>`")
    async def stock(self, ctx: commands.Context, *, symbol: str):
        """Fetches the latest quote for a given stock symbol using yfinance."""
        if not symbol:
            await ctx.send(embed=create_error_embed("Missing Argument", f"Please provide a stock symbol.\nUsage: `{self.command_prefix}stock <SYMBOL>`"))
            return

        symbol = symbol.strip().upper()
        processing_message = await ctx.send(f"⏳ Fetching quote for **{symbol}** using Yahoo Finance...")

        try:
            ticker = yf.Ticker(symbol)
            # .info dictionary contains the data
            info = ticker.info

            # --- Data Extraction from yfinance .info ---
            # Use .get() extensively as not all fields are guaranteed for every ticker type
            current_price = info.get('currentPrice') or info.get('regularMarketPrice')
            prev_close = info.get('previousClose') or info.get('regularMarketPreviousClose')
            volume = info.get('volume') or info.get('regularMarketVolume')
            market_state = info.get('marketState', 'N/A') # e.g., REGULAR, PRE, POST
            currency = info.get('currency', '')
            short_name = info.get('shortName', symbol) # Use symbol as fallback
            long_name = info.get('longName')
            market_time_ts = info.get('regularMarketTime') # Unix timestamp

            # --- Data Validation and Calculation ---
            price_f, change_f, change_percent_f, volume_i = None, None, None, None
            valid = True

            if current_price is not None:
                try:
                    price_f = float(current_price)
                except (ValueError, TypeError):
                    logger.warning(f"Could not convert current price '{current_price}' to float for {symbol}.")
                    valid = False
            else:
                # If we don't even have a price, it's likely a bad symbol or delisted
                logger.warning(f"Missing current price data for {symbol}. Info: {info}")
                valid = False

            if valid and prev_close is not None:
                try:
                    prev_close_f = float(prev_close)
                    if price_f is not None: # Ensure price was valid too
                        change_f = price_f - prev_close_f
                        if prev_close_f != 0: # Avoid division by zero
                            change_percent_f = change_f / prev_close_f
                        else:
                            change_percent_f = 0.0 # Or handle as N/A if preferred
                except (ValueError, TypeError):
                    logger.warning(f"Could not convert previous close '{prev_close}' to float for {symbol}.")
                    # Allow proceeding without change figures if only prev_close fails
            # else: change_f and change_percent_f remain None

            if volume is not None:
                try:
                    volume_i = int(volume)
                except (ValueError, TypeError):
                    logger.warning(f"Could not convert volume '{volume}' to int for {symbol}.")
            # --- End Validation ---

            if not valid:
                 await processing_message.edit(content=None, embed=create_error_embed(
                    "Data Not Found",
                    f"Could not retrieve essential price data for **{symbol}**. It might be delisted, invalid, or Yahoo Finance unavailable.\nCheck the symbol or try again later."
                ))
                 return

            # Use long name if available, otherwise short name
            display_name = long_name if long_name else short_name

            embed = nextcord.Embed(
                title=f"Stock Quote: {display_name} ({symbol})",
                description=f"Market Status: **{market_state}**\nLast Update: {format_yf_timestamp(market_time_ts)}",
                # Set color based on change
                color=nextcord.Color.green() if (change_f is not None and change_f >= 0) else nextcord.Color.red()
            )

            # Format currency symbol correctly if available
            price_display = f"{currency}{price_f:,.2f}" if price_f is not None else 'N/A'
            embed.add_field(name="Price", value=price_display, inline=True)

            # Format change and percentage with signs, handling None
            change_display = f"{change_f:+.2f}" if change_f is not None else "N/A"
            change_percent_display = f"({change_percent_f:+.2%})" if change_percent_f is not None else ""
            embed.add_field(name="Change", value=f"{change_display} {change_percent_display}".strip(), inline=True)

            embed.add_field(name="Volume", value=f"{volume_i:,}" if volume_i is not None else 'N/A', inline=True)

            # Add other potentially interesting fields if they exist
            day_low = info.get('dayLow')
            day_high = info.get('dayHigh')
            if day_low is not None and day_high is not None:
                embed.add_field(name="Day Range", value=f"{currency}{day_low:,.2f} - {currency}{day_high:,.2f}", inline=True)

            market_cap = info.get('marketCap')
            if market_cap is not None:
                 embed.add_field(name="Market Cap", value=f"{currency}{market_cap:,}", inline=True)

            # Add an empty field if needed to align the last row (max 3 inline per row)
            field_count = len(embed.fields)
            if field_count % 3 == 2: # If 2 fields on the last row, add a spacer
                 embed.add_field(name="\u200b", value="\u200b", inline=True) # Invisible character spacer

            embed.set_footer(text="Data provided by Yahoo Finance via yfinance (unofficial).")
            await processing_message.edit(content=None, embed=embed)
            logger.info(f"yfinance Stock command executed by {ctx.author.name} for symbol {symbol}.")

        except Exception as e:
            # Catch potential network errors, ticker not found errors from yfinance, etc.
            await processing_message.edit(content=None, embed=create_error_embed(
                "Error Fetching Data",
                f"An error occurred while fetching data for **{symbol}** from Yahoo Finance.\n"
                f"Details: `{e}`\n(The symbol might be invalid, delisted, or Yahoo Finance unavailable)."
            ))
            logger.error(f"Unexpected error in yfinance stock command for symbol {symbol}: {e}", exc_info=True)


    @commands.command(name='crypto', help="Gets crypto exchange rate. Usage: `crypto <CRYPTO> [FIAT]`")
    async def crypto(self, ctx: commands.Context, crypto_symbol: str, fiat_symbol: str = 'USD'):
        """Fetches the exchange rate for a cryptocurrency pair using yfinance."""
        if not crypto_symbol:
            await ctx.send(embed=create_error_embed("Missing Argument", f"Please provide a crypto symbol.\nUsage: `{self.command_prefix}crypto <CRYPTO_SYMBOL> [FIAT_SYMBOL]`"))
            return

        crypto_symbol = crypto_symbol.strip().upper()
        fiat_symbol = fiat_symbol.strip().upper()
        # Construct the ticker format Yahoo Finance expects (e.g., BTC-USD)
        yahoo_ticker = f"{crypto_symbol}-{fiat_symbol}"
        pair = f"{crypto_symbol}/{fiat_symbol}" # For display

        processing_message = await ctx.send(f"⏳ Fetching exchange rate for **{pair}** ({yahoo_ticker}) using Yahoo Finance...")

        try:
            ticker = yf.Ticker(yahoo_ticker)
            info = ticker.info

            # --- Data Extraction from yfinance .info for Crypto ---
            current_price = info.get('currentPrice') or info.get('regularMarketPrice')
            prev_close = info.get('previousClose') or info.get('regularMarketPreviousClose')
            volume = info.get('volume') or info.get('regularMarketVolume') # Often 24h volume for crypto
            volume_24h = info.get('volume24Hr') # Sometimes specifically available
            currency = info.get('currency', fiat_symbol) # Target currency
            from_currency = info.get('fromCurrency', crypto_symbol) # Base crypto
            market_time_ts = info.get('regularMarketTime') # Unix timestamp
            short_name = info.get('shortName', pair) # Use pair as fallback
            long_name = info.get('longName')
            market_cap = info.get('marketCap')
            day_low = info.get('dayLow')
            day_high = info.get('dayHigh')
            bid = info.get('bid')
            ask = info.get('ask')


            # --- Data Validation and Calculation ---
            price_f, change_f, change_percent_f, volume_i, bid_f, ask_f = None, None, None, None, None, None
            valid = True

            if current_price is not None:
                try:
                    price_f = float(current_price)
                except (ValueError, TypeError):
                    logger.warning(f"Could not convert current price '{current_price}' to float for {yahoo_ticker}.")
                    valid = False
            else:
                logger.warning(f"Missing current price data for {yahoo_ticker}. Info: {info}")
                valid = False

            # Calculate change if possible
            if valid and prev_close is not None:
                try:
                    prev_close_f = float(prev_close)
                    if price_f is not None:
                        change_f = price_f - prev_close_f
                        if prev_close_f != 0:
                            change_percent_f = change_f / prev_close_f
                        else: change_percent_f = 0.0
                except (ValueError, TypeError): pass # Non-fatal if change calc fails

            # Get volume (prefer specific 24h if available)
            vol_source = volume_24h if volume_24h is not None else volume
            if vol_source is not None:
                try: volume_i = int(vol_source)
                except (ValueError, TypeError): pass

            # Get bid/ask if available
            if bid is not None:
                try: bid_f = float(bid)
                except (ValueError, TypeError): pass
            if ask is not None:
                try: ask_f = float(ask)
                except (ValueError, TypeError): pass
            # --- End Validation ---

            if not valid:
                 await processing_message.edit(content=None, embed=create_error_embed(
                    "Data Not Found",
                    f"Could not retrieve essential price data for **{pair}** ({yahoo_ticker}).\n"
                    f"Check if the symbol format is correct (e.g., BTC-USD) or try again later."
                 ))
                 return

            display_name = long_name if long_name else short_name

            embed = nextcord.Embed(
                title=f"Crypto Rate: {display_name}",
                description=f"Last Update: {format_yf_timestamp(market_time_ts)}",
                color=nextcord.Color.gold() # Or color based on change if preferred
                # color=nextcord.Color.green() if (change_f is not None and change_f >= 0) else nextcord.Color.red()
            )

            # Use significant digits appropriate for crypto
            price_display = f"**{price_f:,.8f}** {currency}" if price_f is not None else 'N/A'
            embed.add_field(name=f"1 {from_currency} =", value=price_display, inline=False) # Main rate field

            # Add change if available
            if change_f is not None and change_percent_f is not None:
                 change_display = f"{change_f:+.8f} {currency} ({change_percent_f:+.2%})"
                 embed.add_field(name="Change (24h)", value=change_display, inline=False)

            # Add Bid/Ask if available
            if bid_f is not None:
                embed.add_field(name="Bid", value=f"{bid_f:,.8f} {currency}", inline=True)
            if ask_f is not None:
                 embed.add_field(name="Ask", value=f"{ask_f:,.8f} {currency}", inline=True)

             # Spacer if only one of bid/ask exists to align next row
            if (bid_f is not None) ^ (ask_f is not None) and (day_low is not None or day_high is not None or volume_i is not None or market_cap is not None):
                 embed.add_field(name="\u200b", value="\u200b", inline=True)

            # Add other info
            if day_low is not None and day_high is not None:
                embed.add_field(name="Day Range", value=f"{day_low:,.8f} - {day_high:,.8f} {currency}", inline=True)
            if volume_i is not None:
                embed.add_field(name="Volume", value=f"{volume_i:,}", inline=True)
            if market_cap is not None:
                 embed.add_field(name="Market Cap", value=f"{currency}{market_cap:,}", inline=True)

            embed.set_footer(text="Data provided by Yahoo Finance via yfinance (unofficial).")
            await processing_message.edit(content=None, embed=embed)
            logger.info(f"yfinance Crypto command executed by {ctx.author.name} for pair {pair} ({yahoo_ticker}).")


        except Exception as e:
            # Catch potential network errors, ticker not found errors from yfinance, etc.
            await processing_message.edit(content=None, embed=create_error_embed(
                "Error Fetching Data",
                f"An error occurred while fetching data for **{pair}** ({yahoo_ticker}) from Yahoo Finance.\n"
                f"Details: `{e}`\n(Check symbol format, e.g., BTC-USD. The pair might not be listed on Yahoo Finance)."
            ))
            logger.error(f"Unexpected error in yfinance crypto command for pair {pair} ({yahoo_ticker}): {e}", exc_info=True)


# This function is REQUIRED for the cog to be loaded by nextcord
def setup(bot):
    """Adds the FinanceCog (using yfinance) to the bot."""
    try:
        # Ensure yfinance is installed
        import yfinance
    except ImportError:
        logger.error("`yfinance` library not found. Please install it (`pip install yfinance`) to use FinanceCog.")
        # Optionally raise an error to prevent loading if yfinance is mandatory
        raise ImportError("`yfinance` library not found. FinanceCog cannot be loaded.")

    try:
        cog = FinanceCog(bot)
        bot.add_cog(cog)
        logger.info("FinanceCog (using yfinance) loaded successfully.")
    except Exception as e:
        logger.error(f"Failed to load FinanceCog (yfinance): {e}", exc_info=True)
        # raise e # Optional: prevent bot start if cog fails