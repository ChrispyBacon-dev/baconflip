import os
import nextcord
from nextcord.ext import commands
import yfinance as yf
import datetime
import logging
# Required for charting
import matplotlib
matplotlib.use('Agg') # Use non-interactive backend suitable for bots
import matplotlib.pyplot as plt
import pandas as pd
import io # For handling image data in memory

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
    Commands for fetching financial data (stocks, crypto) using yfinance,
    including a small trend chart.
    Note: yfinance relies on scraping Yahoo Finance and may break if Yahoo changes its site.
    Requires: yfinance, matplotlib, pandas
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.command_prefix = os.getenv('BOT_PREFIX', '?') # Get from env or default to '?'
        logger.info("FinanceCog initialized using yfinance (with charting).")
        # No API key needed for yfinance

    async def generate_trend_chart(self, history_df: pd.DataFrame, ticker_symbol: str, period_str: str) -> io.BytesIO | None:
        """Generates a small trend chart from historical data."""
        if history_df.empty or 'Close' not in history_df.columns or len(history_df) < 2:
            logger.warning(f"Not enough data to generate chart for {ticker_symbol} ({period_str}).")
            return None

        try:
            plt.style.use('dark_background') # Use dark theme suitable for Discord embeds
            fig, ax = plt.subplots(figsize=(4, 1.5), dpi=100) # Small figure size, decent resolution

            close_prices = history_df['Close']
            start_price = close_prices.iloc[0]
            end_price = close_prices.iloc[-1]

            # Determine line color based on trend
            line_color = 'lime' if end_price >= start_price else 'red'

            # Plot the closing prices
            ax.plot(history_df.index, close_prices, color=line_color, linewidth=1.5)

            # --- Minimalist Customization ---
            ax.axis('off') # Hide axes, ticks, and labels for a clean look
            fig.tight_layout(pad=0.1) # Reduce padding around the plot

            # --- Save to In-Memory Buffer ---
            buf = io.BytesIO()
            # Save as PNG, make background transparent, fit plot tightly
            plt.savefig(buf, format='png', transparent=True, bbox_inches='tight', pad_inches=0.05)
            plt.close(fig) # IMPORTANT: Close the plot to free memory
            buf.seek(0) # Reset buffer position to the beginning for reading
            logger.info(f"Successfully generated trend chart for {ticker_symbol} ({period_str}).")
            return buf

        except Exception as e:
            logger.error(f"Failed to generate trend chart for {ticker_symbol}: {e}", exc_info=True)
            plt.close('all') # Ensure any dangling plots are closed on error
            return None


    @commands.command(name='stock', help="Gets stock quote + 5d trend. Usage: `stock <SYMBOL>`")
    async def stock(self, ctx: commands.Context, *, symbol: str):
        """Fetches the latest quote and a 5-day trend chart for a stock symbol using yfinance."""
        if not symbol:
            await ctx.send(embed=create_error_embed("Missing Argument", f"Please provide a stock symbol.\nUsage: `{self.command_prefix}stock <SYMBOL>`"))
            return

        symbol = symbol.strip().upper()
        processing_message = await ctx.send(f"⏳ Fetching quote & trend for **{symbol}** using Yahoo Finance...")

        chart_file = None # Initialize chart file variable
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info # Fetch basic info

            # --- Data Extraction from yfinance .info ---
            current_price = info.get('currentPrice') or info.get('regularMarketPrice')
            prev_close = info.get('previousClose') or info.get('regularMarketPreviousClose')
            volume = info.get('volume') or info.get('regularMarketVolume')
            market_state = info.get('marketState', 'N/A')
            currency = info.get('currency', '')
            short_name = info.get('shortName', symbol)
            long_name = info.get('longName')
            market_time_ts = info.get('regularMarketTime')
            day_low = info.get('dayLow')
            day_high = info.get('dayHigh')
            market_cap = info.get('marketCap')

             # --- Fetch History for Chart ---
            # Use a short period and appropriate interval (e.g., 1h for 5d, 1d for 1mo)
            # Adjust interval based on period to avoid getting too much/too little data
            chart_period = "5d"
            chart_interval = "60m" # Hourly data for 5 days seems reasonable
            history = ticker.history(period=chart_period, interval=chart_interval)

            # --- Generate Chart ---
            if not history.empty:
                chart_buffer = await self.generate_trend_chart(history, symbol, chart_period)
                if chart_buffer:
                    chart_file = nextcord.File(chart_buffer, filename="trend.png")
            else:
                logger.warning(f"No historical data returned for {symbol} ({chart_period} / {chart_interval}). Cannot generate chart.")


            # --- Data Validation and Calculation (Price, Change, Volume) ---
            price_f, change_f, change_percent_f, volume_i = None, None, None, None
            valid_price = True

            if current_price is not None:
                try: price_f = float(current_price)
                except (ValueError, TypeError): valid_price = False
            else: valid_price = False

            if not valid_price:
                logger.warning(f"Missing or invalid current price data for {symbol}. Info: {info}")
                await processing_message.edit(content=None, embed=create_error_embed(
                    "Data Not Found",
                    f"Could not retrieve essential price data for **{symbol}**. It might be delisted, invalid, or Yahoo Finance unavailable.\nCheck the symbol or try again later."
                ), file=None) # Ensure file is None on error edit
                return

            # Calculate change if possible
            if prev_close is not None:
                try:
                    prev_close_f = float(prev_close)
                    change_f = price_f - prev_close_f
                    change_percent_f = (change_f / prev_close_f) if prev_close_f != 0 else 0.0
                except (ValueError, TypeError): pass # Non-fatal

            # Convert volume
            if volume is not None:
                try: volume_i = int(volume)
                except (ValueError, TypeError): pass
            # --- End Validation ---

            # Use long name if available, otherwise short name
            display_name = long_name if long_name else short_name

            embed = nextcord.Embed(
                title=f"Stock Quote: {display_name} ({symbol})",
                description=f"Market Status: **{market_state}**\nLast Update: {format_yf_timestamp(market_time_ts)}",
                color=nextcord.Color.green() if (change_f is not None and change_f >= 0) else nextcord.Color.red()
            )

            price_display = f"{currency}{price_f:,.2f}" if price_f is not None else 'N/A'
            embed.add_field(name="Price", value=price_display, inline=True)

            change_display = f"{change_f:+.2f}" if change_f is not None else "N/A"
            change_percent_display = f"({change_percent_f:+.2%})" if change_percent_f is not None else ""
            embed.add_field(name="Change", value=f"{change_display} {change_percent_display}".strip(), inline=True)

            embed.add_field(name="Volume", value=f"{volume_i:,}" if volume_i is not None else 'N/A', inline=True)

            if day_low is not None and day_high is not None:
                embed.add_field(name="Day Range", value=f"{currency}{day_low:,.2f} - {currency}{day_high:,.2f}", inline=True)

            if market_cap is not None:
                 embed.add_field(name="Market Cap", value=f"{currency}{market_cap:,}", inline=True)

            # Add spacer if needed
            field_count = len(embed.fields)
            if field_count == 5: # Price, Change, Vol, Range, Cap = 5 -> Add spacer
                 embed.add_field(name="\u200b", value="\u200b", inline=True)

            # Set image if chart was generated
            if chart_file:
                embed.set_image(url=f"attachment://{chart_file.filename}") # Link embed image to attachment name
                embed.set_footer(text=f"Data provided by Yahoo Finance via yfinance. Trend: {chart_period}.")
            else:
                embed.set_footer(text="Data provided by Yahoo Finance via yfinance. (Chart unavailable)")

            # Edit the message, sending the file ALONG with the embed
            await processing_message.edit(content=None, embed=embed, file=chart_file)
            logger.info(f"yfinance Stock command executed by {ctx.author.name} for symbol {symbol}.")

        except Exception as e:
            await processing_message.edit(content=None, embed=create_error_embed(
                "Error Fetching Data",
                f"An error occurred while fetching data for **{symbol}** from Yahoo Finance.\n"
                f"Details: `{e}`\n(The symbol might be invalid, delisted, or Yahoo Finance unavailable)."
            ), file=None) # Ensure file is None on error edit
            logger.error(f"Unexpected error in yfinance stock command for symbol {symbol}: {e}", exc_info=True)
            # Explicitly close any potentially open matplotlib plots in case of unexpected error
            plt.close('all')


    @commands.command(name='crypto', help="Gets crypto rate + 5d trend. Usage: `crypto <CRYPTO> [FIAT]`")
    async def crypto(self, ctx: commands.Context, crypto_symbol: str, fiat_symbol: str = 'USD'):
        """Fetches the exchange rate and a 5-day trend chart for a cryptocurrency pair using yfinance."""
        if not crypto_symbol:
            await ctx.send(embed=create_error_embed("Missing Argument", f"Please provide a crypto symbol.\nUsage: `{self.command_prefix}crypto <CRYPTO_SYMBOL> [FIAT_SYMBOL]`"))
            return

        crypto_symbol = crypto_symbol.strip().upper()
        fiat_symbol = fiat_symbol.strip().upper()
        yahoo_ticker = f"{crypto_symbol}-{fiat_symbol}"
        pair = f"{crypto_symbol}/{fiat_symbol}"

        processing_message = await ctx.send(f"⏳ Fetching rate & trend for **{pair}** ({yahoo_ticker}) using Yahoo Finance...")

        chart_file = None # Initialize chart file variable
        try:
            ticker = yf.Ticker(yahoo_ticker)
            info = ticker.info # Fetch basic info

            # --- Data Extraction for Crypto ---
            current_price = info.get('currentPrice') or info.get('regularMarketPrice')
            prev_close = info.get('previousClose') or info.get('regularMarketPreviousClose')
            volume_24h = info.get('volume24Hr') or info.get('volume') # Prefer 24hr if available
            currency = info.get('currency', fiat_symbol)
            from_currency = info.get('fromCurrency', crypto_symbol)
            market_time_ts = info.get('regularMarketTime')
            short_name = info.get('shortName', pair)
            long_name = info.get('longName')
            market_cap = info.get('marketCap')
            day_low = info.get('dayLow')
            day_high = info.get('dayHigh')
            bid = info.get('bid')
            ask = info.get('ask')

            # --- Fetch History for Chart ---
            chart_period = "5d"
            chart_interval = "90m" # Slightly larger interval for potentially more volatile crypto? Test this.
            history = ticker.history(period=chart_period, interval=chart_interval)

             # --- Generate Chart ---
            if not history.empty:
                chart_buffer = await self.generate_trend_chart(history, yahoo_ticker, chart_period)
                if chart_buffer:
                    chart_file = nextcord.File(chart_buffer, filename="trend.png")
            else:
                logger.warning(f"No historical data returned for {yahoo_ticker} ({chart_period} / {chart_interval}). Cannot generate chart.")


            # --- Data Validation and Calculation ---
            price_f, change_f, change_percent_f, volume_i, bid_f, ask_f = None, None, None, None, None, None
            valid_price = True

            if current_price is not None:
                try: price_f = float(current_price)
                except (ValueError, TypeError): valid_price = False
            else: valid_price = False

            if not valid_price:
                logger.warning(f"Missing or invalid current price data for {yahoo_ticker}. Info: {info}")
                await processing_message.edit(content=None, embed=create_error_embed(
                    "Data Not Found",
                    f"Could not retrieve essential price data for **{pair}** ({yahoo_ticker}).\n"
                    f"Check if the symbol format is correct (e.g., BTC-USD) or try again later."
                 ), file=None) # Ensure file is None on error edit
                return

            # Calculate change
            if prev_close is not None:
                try:
                    prev_close_f = float(prev_close)
                    change_f = price_f - prev_close_f
                    change_percent_f = (change_f / prev_close_f) if prev_close_f != 0 else 0.0
                except (ValueError, TypeError): pass

            # Volume
            if volume_24h is not None:
                try: volume_i = int(volume_24h)
                except (ValueError, TypeError): pass

            # Bid/Ask
            if bid is not None:
                try: bid_f = float(bid)
                except (ValueError, TypeError): pass
            if ask is not None:
                try: ask_f = float(ask)
                except (ValueError, TypeError): pass
            # --- End Validation ---

            display_name = long_name if long_name else short_name

            embed = nextcord.Embed(
                title=f"Crypto Rate: {display_name}",
                description=f"Last Update: {format_yf_timestamp(market_time_ts)}",
                color=nextcord.Color.gold()
                # color=nextcord.Color.green() if (change_f is not None and change_f >= 0) else nextcord.Color.red() # Optional: Color by trend
            )

            price_display = f"**{price_f:,.8f}** {currency}" if price_f is not None else 'N/A'
            embed.add_field(name=f"1 {from_currency} =", value=price_display, inline=False)

            if change_f is not None and change_percent_f is not None:
                 change_display = f"{change_f:+.8f} {currency} ({change_percent_f:+.2%})"
                 embed.add_field(name="Change (24h)", value=change_display, inline=False)

            if bid_f is not None:
                embed.add_field(name="Bid", value=f"{bid_f:,.8f} {currency}", inline=True)
            if ask_f is not None:
                 embed.add_field(name="Ask", value=f"{ask_f:,.8f} {currency}", inline=True)

            # Spacer needed if: Change exists AND (only Bid or only Ask exists)
            if change_f is not None and ((bid_f is not None) ^ (ask_f is not None)):
                 embed.add_field(name="\u200b", value="\u200b", inline=True)


            if day_low is not None and day_high is not None:
                embed.add_field(name="Day Range", value=f"{day_low:,.8f} - {day_high:,.8f} {currency}", inline=True)
            if volume_i is not None:
                # Display volume using the crypto symbol if possible
                vol_unit = from_currency if price_f else '' # Display volume in crypto units if price exists
                embed.add_field(name="Volume (24h)", value=f"{volume_i:,} {vol_unit}".strip(), inline=True)

            if market_cap is not None:
                 embed.add_field(name="Market Cap", value=f"{currency}{market_cap:,}", inline=True)


            # Set image if chart was generated
            if chart_file:
                embed.set_image(url=f"attachment://{chart_file.filename}")
                embed.set_footer(text=f"Data provided by Yahoo Finance via yfinance. Trend: {chart_period}.")
            else:
                 embed.set_footer(text="Data provided by Yahoo Finance via yfinance. (Chart unavailable)")


            await processing_message.edit(content=None, embed=embed, file=chart_file)
            logger.info(f"yfinance Crypto command executed by {ctx.author.name} for pair {pair} ({yahoo_ticker}).")


        except Exception as e:
            await processing_message.edit(content=None, embed=create_error_embed(
                "Error Fetching Data",
                f"An error occurred while fetching data for **{pair}** ({yahoo_ticker}) from Yahoo Finance.\n"
                f"Details: `{e}`\n(Check symbol format, e.g., BTC-USD. The pair might not be listed on Yahoo Finance)."
            ), file=None) # Ensure file is None on error edit
            logger.error(f"Unexpected error in yfinance crypto command for pair {pair} ({yahoo_ticker}): {e}", exc_info=True)
            # Explicitly close any potentially open matplotlib plots
            plt.close('all')


# This function is REQUIRED for the cog to be loaded by nextcord
def setup(bot):
    """Adds the FinanceCog (using yfinance + matplotlib) to the bot."""
    # Ensure required libraries are installed
    missing_libs = []
    try:
        import yfinance
    except ImportError:
        missing_libs.append("yfinance")
    try:
        import matplotlib
    except ImportError:
        missing_libs.append("matplotlib")
    try:
        import pandas
    except ImportError:
        missing_libs.append("pandas")

    if missing_libs:
        libs_str = ", ".join(f"`{lib}`" for lib in missing_libs)
        logger.error(f"Missing required libraries for FinanceCog: {libs_str}. Please install them (`pip install {' '.join(missing_libs)}`)")
        raise ImportError(f"Missing required libraries for FinanceCog: {libs_str}")

    try:
        cog = FinanceCog(bot)
        bot.add_cog(cog)
        logger.info("FinanceCog (using yfinance + charting) loaded successfully.")
    except Exception as e:
        logger.error(f"Failed to load FinanceCog (yfinance + charting): {e}", exc_info=True)
        # raise e # Optional: prevent bot start if cog fails