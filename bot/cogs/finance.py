import os
import nextcord
from nextcord.ext import commands
from alpha_vantage.timeseries import TimeSeries
from alpha_vantage.cryptocurrencies import CryptoCurrencies
# Assuming you have this helper in bot.utils.embeds
# If not, you'll need to create a similar function or replace its calls
from bot.utils.embeds import create_error_embed
import logging

logger = logging.getLogger(__name__)

class FinanceCog(commands.Cog):
    """Commands for fetching financial data (stocks, crypto)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Determine command prefix dynamically or use a fixed one if preferred
        self.command_prefix = os.getenv('BOT_PREFIX', '?') # Get from env or default to '?'

        api_key = os.getenv('ALPHA_VANTAGE_API_KEY')
        if not api_key:
            logger.error("ALPHA_VANTAGE_API_KEY not found in environment variables.")
            # Consider how to handle this: raise error, log & disable, etc.
            # For now, we'll raise an error to prevent the cog from loading improperly.
            raise ValueError("Alpha Vantage API key is missing. FinanceCog cannot be loaded.")

        # Initialize Alpha Vantage clients
        try:
            # Consider adding 'premium=True' if you have a paid plan
            self.ts = TimeSeries(key=api_key, output_format='json')
            self.cc = CryptoCurrencies(key=api_key, output_format='json')
            logger.info("FinanceCog initialized with Alpha Vantage clients.")
        except Exception as e:
            logger.error(f"Failed to initialize Alpha Vantage clients: {e}", exc_info=True)
            # Re-raise or handle appropriately if initialization fails
            raise RuntimeError(f"Failed to initialize Alpha Vantage clients: {e}")

    @commands.command(name='stock', help="Gets stock quote. Usage: `stock <SYMBOL>`")
    async def stock(self, ctx: commands.Context, *, symbol: str):
        """Fetches the latest quote for a given stock symbol."""
        if not symbol:
            await ctx.send(embed=create_error_embed("Missing Argument", f"Please provide a stock symbol.\nUsage: `{self.command_prefix}stock <SYMBOL>`"))
            return

        symbol = symbol.strip().upper() # Clean up input
        processing_message = await ctx.send(f"⏳ Fetching quote for **{symbol}**...")

        try:
            # Use Alpha Vantage's get_quote_endpoint
            # The library returns data and meta_data, but for quotes, the data dict often contains everything needed.
            data, meta_data = self.ts.get_quote_endpoint(symbol=symbol)

            # Check if the primary data key is present
            if not data or 'Global Quote' not in data:
                 # Check if it might be an API error message
                if data and 'Error Message' in data:
                     error_msg = data['Error Message']
                     await processing_message.edit(content=None, embed=create_error_embed(
                        "API Error",
                        f"Failed to fetch data for **{symbol}**: {error_msg}\n(Check symbol validity or API limits)"
                    ))
                     logger.warning(f"API Error for symbol {symbol}: {error_msg}. Received: {data}")
                elif data and 'Note' in data: # Handle API limit messages
                     note_msg = data['Note']
                     await processing_message.edit(content=None, embed=create_error_embed(
                        "API Limit Note",
                        f"Note regarding **{symbol}**: {note_msg}\n(This usually indicates reaching the free tier limit)"
                    ))
                     logger.warning(f"API Note for symbol {symbol}: {note_msg}. Received: {data}")
                else:
                    await processing_message.edit(content=None, embed=create_error_embed(
                        "Data Not Found",
                        f"Could not retrieve valid quote data for **{symbol}**. Check if the symbol is correct or try again later."
                    ))
                    # Log the structure if it's unexpected but not an error/note
                    logger.warning(f"No 'Global Quote' key found for symbol: {symbol}. Received: {data}")
                return # Exit if no valid quote data structure

            quote = data.get('Global Quote') # Use .get() for safety

            # Check if the 'Global Quote' dictionary itself is empty or None
            if not quote:
                 await processing_message.edit(content=None, embed=create_error_embed(
                    "Empty Quote Data",
                    f"Received an empty quote data structure for **{symbol}**. Check symbol or API status."
                ))
                 logger.warning(f"'Global Quote' data is empty or None for symbol: {symbol}. Received data: {data}")
                 return

            # Extract data safely using .get() with defaults
            price_str = quote.get('05. price')
            change_str = quote.get('09. change')
            change_percent_str = quote.get('10. change percent') # Get raw string first
            volume_str = quote.get('06. volume')
            last_trading_day = quote.get('07. latest trading day')
            symbol_returned = quote.get('01. symbol', symbol) # Use returned symbol if available

            # --- Data Validation and Conversion ---
            price_f, change_f, change_percent_f, volume_i = None, None, None, None
            conversion_errors = []

            try:
                if price_str is not None: price_f = float(price_str)
                else: conversion_errors.append("price (missing)")
            except (ValueError, TypeError): conversion_errors.append(f"price ('{price_str}')")

            try:
                if change_str is not None: change_f = float(change_str)
                else: conversion_errors.append("change (missing)")
            except (ValueError, TypeError): conversion_errors.append(f"change ('{change_str}')")

            try:
                # Clean up percentage string before conversion
                if change_percent_str is not None:
                   cleaned_percent_str = change_percent_str.rstrip('%').strip()
                   change_percent_f = float(cleaned_percent_str) / 100.0 # Store as float (e.g., 0.01 for 1%)
                else: conversion_errors.append("change percent (missing)")
            except (ValueError, TypeError): conversion_errors.append(f"change percent ('{change_percent_str}')")

            try:
                 if volume_str is not None: volume_i = int(volume_str)
                 else: conversion_errors.append("volume (missing)")
            except (ValueError, TypeError): conversion_errors.append(f"volume ('{volume_str}')")

            # Only halt if essential data (price) failed conversion
            if price_f is None or change_f is None or change_percent_f is None:
                error_details = ", ".join(conversion_errors)
                await processing_message.edit(content=None, embed=create_error_embed(
                    "Data Format Error",
                    f"Received unexpected or missing essential data format for **{symbol_returned}**.\nIssues with: {error_details}.\nRaw quote data: `{quote}`"
                ))
                logger.error(f"Essential data format/conversion error for {symbol_returned}. Errors: {error_details}. Raw quote: {quote}")
                return
            # --- End Data Validation ---

            embed = nextcord.Embed(
                title=f"Stock Quote: {symbol_returned}",
                description=f"Latest Trading Day: {last_trading_day or 'N/A'}",
                # Set color based on change (handle None case)
                color=nextcord.Color.green() if (change_f is not None and change_f >= 0) else nextcord.Color.red()
            )

            embed.add_field(name="Price", value=f"${price_f:,.2f}" if price_f is not None else 'N/A', inline=True)

            # Format change and percentage with signs, handling None
            change_display = f"{change_f:+.2f}" if change_f is not None else "N/A" # Always show sign (+/-)
            change_percent_display = f"({change_percent_f:+.2%})" if change_percent_f is not None else "" # Format as percentage, show sign
            embed.add_field(name="Change", value=f"{change_display} {change_percent_display}".strip(), inline=True)

            embed.add_field(name="Volume", value=f"{volume_i:,}" if volume_i is not None else 'N/A', inline=True)

            embed.set_footer(text="Data provided by Alpha Vantage. Free plan limits apply.")
            await processing_message.edit(content=None, embed=embed)
            logger.info(f"Stock command executed by {ctx.author.name} for symbol {symbol}. Result: {symbol_returned}")

        except ValueError as ve: # Catches errors from alpha_vantage library itself
             await processing_message.edit(content=None, embed=create_error_embed(
                "API Request Error",
                f"There was an issue configuring the request for **{symbol}**.\nDetails: {ve}"
            ))
             logger.warning(f"Alpha Vantage client ValueError for symbol {symbol}: {ve}")
        except Exception as e:
            # Catch potential network errors, unexpected API responses, etc.
            await processing_message.edit(content=None, embed=create_error_embed(
                "Unexpected Error",
                f"An error occurred while fetching data for **{symbol}**.\nPlease try again later or contact support if it persists."
            ))
            # Log the full traceback for debugging
            logger.error(f"Unexpected error in stock command for symbol {symbol}: {e}", exc_info=True)


    # --- CORRECTED crypto FUNCTION ---
    @commands.command(name='crypto', help="Gets crypto exchange rate. Usage: `crypto <CRYPTO> [FIAT]`")
    async def crypto(self, ctx: commands.Context, crypto_symbol: str, fiat_symbol: str = 'USD'):
        """Fetches the exchange rate for a cryptocurrency pair (e.g., BTC to USD)."""
        if not crypto_symbol:
            await ctx.send(embed=create_error_embed("Missing Argument", f"Please provide a crypto symbol.\nUsage: `{self.command_prefix}crypto <CRYPTO_SYMBOL> [FIAT_SYMBOL]`"))
            return

        crypto_symbol = crypto_symbol.strip().upper()
        fiat_symbol = fiat_symbol.strip().upper()
        pair = f"{crypto_symbol}/{fiat_symbol}"

        processing_message = await ctx.send(f"⏳ Fetching exchange rate for **{pair}**...")

        try:
            # The crypto endpoint in the library directly returns the rate dictionary (or error/note)
            data, _ = self.cc.get_digital_currency_exchange_rate(
                from_currency=crypto_symbol,
                to_currency=fiat_symbol
            )

            # 1. Check for explicit API errors/notes first
            if data and 'Error Message' in data:
                 error_msg = data['Error Message']
                 await processing_message.edit(content=None, embed=create_error_embed(
                    "API Error",
                    f"Failed to fetch data for **{pair}**: {error_msg}\n(Check symbol validity or API limits)"
                ))
                 logger.warning(f"API Error for pair {pair}: {error_msg}. Received: {data}")
                 return # Exit after error
            elif data and 'Note' in data: # Handle API limit messages
                 note_msg = data['Note']
                 await processing_message.edit(content=None, embed=create_error_embed(
                    "API Limit Note",
                    f"Note regarding **{pair}**: {note_msg}\n(This usually indicates reaching the free tier limit)"
                ))
                 logger.warning(f"API Note for pair {pair}: {note_msg}. Received: {data}")
                 return # Exit after note

            # 2. Check if core data exists (using a key we know should be in a successful response)
            #    If the API call worked, 'data' itself IS the rate data dictionary.
            elif data and '5. Exchange Rate' in data: # Check for a key that MUST exist on success
                rate_data = data # Use the received dictionary directly

                # Extract data safely using the CORRECT keys ('1.', '2.', etc.)
                from_code = rate_data.get('1. From_Currency Code')
                from_name = rate_data.get('2. From_Currency Name')
                to_code = rate_data.get('3. To_Currency Code')
                to_name = rate_data.get('4. To_Currency Name')
                exchange_rate_str = rate_data.get('5. Exchange Rate')
                last_refreshed = rate_data.get('6. Last Refreshed')
                time_zone = rate_data.get('7. Time Zone')
                bid_price_str = rate_data.get('8. Bid Price')
                ask_price_str = rate_data.get('9. Ask Price')

                # --- Data Validation and Conversion ---
                exchange_rate_f, bid_price_f, ask_price_f = None, None, None
                conversion_errors = []

                # Check if essential fields were extracted correctly
                if not all([from_code, from_name, to_code, to_name, exchange_rate_str]):
                    await processing_message.edit(content=None, embed=create_error_embed(
                        "Incomplete Data",
                        f"Received incomplete data structure for **{pair}**. Key information missing.\nData: `{rate_data}`"
                    ))
                    logger.error(f"Incomplete data received for {pair}. Raw data: {rate_data}")
                    return

                try:
                    if exchange_rate_str: exchange_rate_f = float(exchange_rate_str)
                    else: conversion_errors.append("exchange rate (missing)")
                except (ValueError, TypeError): conversion_errors.append(f"exchange rate ('{exchange_rate_str}')")

                # Bid/Ask are optional, only log conversion errors if they exist but are invalid
                try:
                    if bid_price_str is not None: bid_price_f = float(bid_price_str)
                except (ValueError, TypeError):
                    logger.warning(f"Could not convert bid price '{bid_price_str}' to float for {pair}.")
                    pass # Don't add to conversion_errors as it's optional

                try:
                    if ask_price_str is not None: ask_price_f = float(ask_price_str)
                except (ValueError, TypeError):
                    logger.warning(f"Could not convert ask price '{ask_price_str}' to float for {pair}.")
                    pass # Don't add to conversion_errors as it's optional

                # Only error out if the MAIN exchange rate failed conversion
                if "exchange rate" in conversion_errors or exchange_rate_f is None:
                    error_details = ", ".join(conversion_errors)
                    await processing_message.edit(content=None, embed=create_error_embed(
                        "Data Format Error",
                        f"Received unexpected data format for **{pair}** (field: exchange rate).\nIssues: {error_details}\nRate data: `{rate_data}`"
                    ))
                    logger.error(f"Data format/conversion error for {pair}. Errors in: {error_details}. Raw rate data: {rate_data}")
                    return
                # --- End Data Validation ---

                # --- Build Embed (using corrected data) ---
                embed = nextcord.Embed(
                    title=f"Crypto Exchange Rate: {from_name} ({from_code}) to {to_name} ({to_code})",
                    description=f"Last Refreshed: {last_refreshed or 'N/A'} ({time_zone or 'UTC'})", # Default TZ if missing
                    color=nextcord.Color.gold() # Or use a crypto-specific color
                )

                if exchange_rate_f is not None:
                     # Display with reasonable precision for crypto (e.g., 8 decimal places)
                    embed.add_field(name="Exchange Rate", value=f"1 {from_code} = **{exchange_rate_f:,.8f}** {to_code}", inline=False)
                else:
                     # This case should ideally not be reached due to validation above, but added for safety
                     embed.add_field(name="Exchange Rate", value="N/A", inline=False)

                # Add Bid/Ask if successfully converted
                if bid_price_f is not None:
                    embed.add_field(name="Bid Price", value=f"{bid_price_f:,.8f} {to_code}", inline=True)
                if ask_price_f is not None:
                     embed.add_field(name="Ask Price", value=f"{ask_price_f:,.8f} {to_code}", inline=True)

                # Add a spacer field if both bid and ask are present for better layout on desktop
                if bid_price_f is not None and ask_price_f is not None:
                    # You might need to adjust the number of inline fields per row in Discord's embed limitations
                     pass # Embed handles layout automatically, often 3 inline fields max per row

                embed.set_footer(text="Data provided by Alpha Vantage. Free plan limits apply.")
                await processing_message.edit(content=None, embed=embed)
                logger.info(f"Crypto command executed by {ctx.author.name} for pair {pair}.")

            # 3. If data exists but isn't an error/note and doesn't have the core rate key
            elif data: # Catch cases where data is received but not in expected format
                 await processing_message.edit(content=None, embed=create_error_embed(
                    "Unexpected Data Format",
                    f"Received an unexpected data structure for **{pair}**. Cannot find exchange rate.\nCheck logs for details."
                ))
                 # Log the actual received data for debugging why it wasn't recognized
                 logger.warning(f"Unrecognized data structure for {pair}. Missing '5. Exchange Rate' key. Received: {data}")
                 return # Exit

            # 4. If data is None or empty
            else:
                 await processing_message.edit(content=None, embed=create_error_embed(
                    "Data Not Found",
                    f"Could not retrieve any data for **{pair}**. Check if symbols are correct or the API is down."
                ))
                 logger.warning(f"No data received from API for {pair}. Received: {data}")
                 return # Exit

        except ValueError as ve: # From alpha_vantage client errors (e.g., invalid key format, though unlikely here)
             await processing_message.edit(content=None, embed=create_error_embed(
                "API Configuration Error",
                f"There was an issue configuring the request for **{pair}**.\nDetails: {ve}"
            ))
             logger.warning(f"Alpha Vantage client ValueError for pair {pair}: {ve}")
        except Exception as e:
            # Catch potential network errors, timeouts, other unexpected issues
            await processing_message.edit(content=None, embed=create_error_embed(
                "Unexpected Error",
                f"An error occurred while fetching data for **{pair}**.\nPlease try again later or contact support if it persists."
            ))
            logger.error(f"Unexpected error in crypto command for pair {pair}: {e}", exc_info=True)


# This function is REQUIRED for the cog to be loaded by nextcord
def setup(bot):
    """Adds the FinanceCog to the bot."""
    # Ensure you have the create_error_embed helper function available
    # If it's in bot.utils.embeds, this import at the top should work.
    # If not, you might need to adjust the import path or define the function here/elsewhere.
    if not hasattr(bot, 'utils') or not hasattr(bot.utils, 'embeds') or not hasattr(bot.utils.embeds, 'create_error_embed'):
         # A basic fallback if the helper is missing, replace with your actual implementation
         global create_error_embed
         def create_error_embed(title, description):
             logger.warning("Using basic fallback create_error_embed. Define bot.utils.embeds.create_error_embed for proper formatting.")
             return nextcord.Embed(title=f"❌ Error: {title}", description=description, color=nextcord.Color.red())

    try:
        cog = FinanceCog(bot)
        bot.add_cog(cog)
        logger.info("FinanceCog loaded successfully.")
    except Exception as e:
        logger.error(f"Failed to load FinanceCog: {e}", exc_info=True)
        # Depending on your bot's structure, you might want to prevent the bot from starting
        # or just log the error and continue without the cog.