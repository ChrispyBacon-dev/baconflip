import os
import nextcord
from nextcord.ext import commands
from alpha_vantage.timeseries import TimeSeries
from alpha_vantage.cryptocurrencies import CryptoCurrencies
from bot.utils.embeds import create_error_embed # Assuming you have this helper
import logging

logger = logging.getLogger(__name__)

class FinanceCog(commands.Cog):
    """Commands for fetching financial data (stocks, crypto)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Determine command prefix dynamically or use a fixed one if preferred
        # Example: Get prefix from bot instance if it stores it
        # self.command_prefix = getattr(bot, 'command_prefix', '!') # Default to '!' if not found
        # Or if you pass it during cog setup:
        # self.command_prefix = bot.config.get('prefix', '!') # Assuming config is available
        # For now, let's hardcode for the help message example, replace as needed
        self.command_prefix = os.getenv('BOT_PREFIX', '!') # Get from env or default

        api_key = os.getenv('ALPHA_VANTAGE_API_KEY')
        if not api_key:
            logger.error("ALPHA_VANTAGE_API_KEY not found in environment variables.")
            # Consider how to handle this: raise error, log & disable, etc.
            # For now, we'll raise an error to prevent the cog from loading improperly.
            raise ValueError("Alpha Vantage API key is missing. FinanceCog cannot be loaded.")

        # Initialize Alpha Vantage clients
        try:
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
            data, meta_data = self.ts.get_quote_endpoint(symbol=symbol)

            # More robust check for valid data
            if not data or 'Global Quote' not in data or not data['Global Quote']:
                # Check if it might be an API error message
                if data and 'Error Message' in data:
                     error_msg = data['Error Message']
                     await processing_message.edit(content=None, embed=create_error_embed(
                        "API Error",
                        f"Failed to fetch data for **{symbol}**: {error_msg}\n(Check symbol validity or API limits)"
                    ))
                     logger.warning(f"API Error for symbol {symbol}: {error_msg}")
                elif data and 'Note' in data: # Handle API limit messages
                     note_msg = data['Note']
                     await processing_message.edit(content=None, embed=create_error_embed(
                        "API Limit Note",
                        f"Note regarding **{symbol}**: {note_msg}\n(This usually indicates reaching the free tier limit)"
                    ))
                     logger.warning(f"API Note for symbol {symbol}: {note_msg}")
                else:
                    await processing_message.edit(content=None, embed=create_error_embed(
                        "Data Not Found",
                        f"Could not retrieve valid quote data for **{symbol}**. Check if the symbol is correct or try again later."
                    ))
                    logger.warning(f"No valid 'Global Quote' data found for symbol: {symbol}. Received: {data}")
                return

            quote = data['Global Quote']

            # Extract data safely using .get() with defaults
            price_str = quote.get('05. price')
            change_str = quote.get('09. change')
            change_percent_str = quote.get('10. change percent', '0%')
            volume_str = quote.get('06. volume')
            last_trading_day = quote.get('07. latest trading day')
            symbol_returned = quote.get('01. symbol', symbol) # Use returned symbol if available

            # --- Data Validation and Conversion ---
            price_f, change_f, change_percent, volume_i = None, None, 0.0, None
            conversion_errors = []

            try:
                if price_str: price_f = float(price_str)
            except (ValueError, TypeError): conversion_errors.append("price")
            try:
                if change_str: change_f = float(change_str)
            except (ValueError, TypeError): conversion_errors.append("change")
            try:
                # Clean up percentage string before conversion
                if change_percent_str:
                   cleaned_percent_str = change_percent_str.rstrip('%').strip()
                   change_percent = float(cleaned_percent_str) / 100.0
            except (ValueError, TypeError): conversion_errors.append("change percent")
            try:
                 if volume_str: volume_i = int(volume_str)
            except (ValueError, TypeError): conversion_errors.append("volume")

            if conversion_errors:
                error_details = ", ".join(conversion_errors)
                await processing_message.edit(content=None, embed=create_error_embed(
                    "Data Format Error",
                    f"Received unexpected data format for **{symbol_returned}** (fields: {error_details}).\nQuote data: `{quote}`"
                ))
                logger.error(f"Data format/conversion error for {symbol_returned}. Errors in: {error_details}. Raw quote: {quote}")
                return
            # --- End Data Validation ---

            embed = nextcord.Embed(
                title=f"Stock Quote: {symbol_returned}",
                description=f"Latest Trading Day: {last_trading_day or 'N/A'}",
                # Set color based on change
                color=nextcord.Color.green() if (change_f is not None and change_f >= 0) else nextcord.Color.red()
            )

            embed.add_field(name="Price", value=f"${price_f:,.2f}" if price_f is not None else 'N/A', inline=True)

            # Format change and percentage with signs
            change_display = f"{change_f:+.2f}" if change_f is not None else "N/A" # Always show sign (+/-)
            change_percent_display = f"({change_percent:+.2%})" if change_percent is not None else "" # Always show sign (+/-)
            embed.add_field(name="Change", value=f"{change_display} {change_percent_display}".strip(), inline=True)

            embed.add_field(name="Volume", value=f"{volume_i:,}" if volume_i is not None else 'N/A', inline=True)

            embed.set_footer(text="Data provided by Alpha Vantage. Free plan limits apply.")
            await processing_message.edit(content=None, embed=embed)
            logger.info(f"Stock command executed by {ctx.author.name} for symbol {symbol}. Result: {symbol_returned}")

        except ValueError as ve: # Catches errors from alpha_vantage library itself (e.g., invalid API key format, though unlikely here)
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
            # --- FIX: Complete the logger.error call ---
            logger.error(f"Unexpected error in stock command for symbol {symbol}: {e}", exc_info=True)
            # --- END FIX ---


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
            data, _ = self.cc.get_digital_currency_exchange_rate(
                from_currency=crypto_symbol,
                to_currency=fiat_symbol
            )

            # Check for valid data or specific API error messages
            if not data or 'Realtime Currency Exchange Rate' not in data:
                if data and 'Error Message' in data:
                     error_msg = data['Error Message']
                     await processing_message.edit(content=None, embed=create_error_embed(
                        "API Error",
                        f"Failed to fetch data for **{pair}**: {error_msg}\n(Check symbol validity or API limits)"
                    ))
                     logger.warning(f"API Error for pair {pair}: {error_msg}")
                elif data and 'Note' in data: # Handle API limit messages
                     note_msg = data['Note']
                     await processing_message.edit(content=None, embed=create_error_embed(
                        "API Limit Note",
                        f"Note regarding **{pair}**: {note_msg}\n(This usually indicates reaching the free tier limit)"
                    ))
                     logger.warning(f"API Note for pair {pair}: {note_msg}")
                else:
                    await processing_message.edit(content=None, embed=create_error_embed(
                        "Data Not Found",
                        f"Could not retrieve valid exchange rate data for **{pair}**. Check if symbols are correct or try again later."
                    ))
                    logger.warning(f"No valid 'Realtime Currency Exchange Rate' data for {pair}. Received: {data}")
                return

            rate_data = data['Realtime Currency Exchange Rate']

            # Extract data safely
            from_code = rate_data.get('1. From_Currency Code')
            from_name = rate_data.get('2. From_Currency Name')
            to_code = rate_data.get('3. To_Currency Code')
            to_name = rate_data.get('4. To_Currency Name')
            exchange_rate_str = rate_data.get('5. Exchange Rate')
            last_refreshed = rate_data.get('6. Last Refreshed')
            time_zone = rate_data.get('7. Time Zone')
            bid_price_str = rate_data.get('8. Bid Price') # Added
            ask_price_str = rate_data.get('9. Ask Price') # Added


            # --- Data Validation and Conversion ---
            exchange_rate_f, bid_price_f, ask_price_f = None, None, None
            conversion_errors = []

            if not all([from_code, from_name, to_code, to_name, exchange_rate_str]):
                await processing_message.edit(content=None, embed=create_error_embed(
                    "Incomplete Data",
                    f"Received incomplete data structure for **{pair}**. Key information missing.\nData: `{rate_data}`"
                ))
                logger.error(f"Incomplete data received for {pair}. Raw data: {rate_data}")
                return

            try:
                if exchange_rate_str: exchange_rate_f = float(exchange_rate_str)
            except (ValueError, TypeError): conversion_errors.append("exchange rate")
            try:
                if bid_price_str: bid_price_f = float(bid_price_str)
            except (ValueError, TypeError): pass # Bid/Ask might be missing, don't error out
            try:
                if ask_price_str: ask_price_f = float(ask_price_str)
            except (ValueError, TypeError): pass # Bid/Ask might be missing, don't error out

            if "exchange rate" in conversion_errors: # Only error if the main rate is bad
                error_details = ", ".join(conversion_errors)
                await processing_message.edit(content=None, embed=create_error_embed(
                    "Data Format Error",
                    f"Received unexpected data format for **{pair}** (fields: {error_details}).\nRate data: `{rate_data}`"
                ))
                logger.error(f"Data format/conversion error for {pair}. Errors in: {error_details}. Raw rate data: {rate_data}")
                return
            # --- End Data Validation ---

            embed = nextcord.Embed(
                title=f"Crypto Exchange Rate: {from_name} ({from_code}) to {to_name} ({to_code})",
                description=f"Last Refreshed: {last_refreshed or 'N/A'} ({time_zone or 'N/A'})",
                color=nextcord.Color.gold() # Or use a crypto-specific color
            )

            if exchange_rate_f is not None:
                 # Display with reasonable precision for crypto
                embed.add_field(name="Exchange Rate", value=f"1 {from_code} = **{exchange_rate_f:,.8f}** {to_code}", inline=False)
            else:
                 embed.add_field(name="Exchange Rate", value="N/A", inline=False)

            # Add Bid/Ask if available
            if bid_price_f is not None:
                embed.add_field(name="Bid Price", value=f"{bid_price_f:,.8f} {to_code}", inline=True)
            if ask_price_f is not None:
                 embed.add_field(name="Ask Price", value=f"{ask_price_f:,.8f} {to_code}", inline=True)


            embed.set_footer(text="Data provided by Alpha Vantage. Free plan limits apply.")
            await processing_message.edit(content=None, embed=embed)
            logger.info(f"Crypto command executed by {ctx.author.name} for pair {pair}.")

        except ValueError as ve: # From alpha_vantage client
             await processing_message.edit(content=None, embed=create_error_embed(
                "API Request Error",
                f"There was an issue configuring the request for **{pair}**.\nDetails: {ve}"
            ))
             logger.warning(f"Alpha Vantage client ValueError for pair {pair}: {ve}")
