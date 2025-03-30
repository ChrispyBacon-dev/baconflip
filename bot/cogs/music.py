# --- bot/cogs/music.py ---

import nextcord
from nextcord.ext import commands
import asyncio
import yt_dlp
import logging
import functools
from collections import deque

# --- Suppress Noise/Info from yt-dlp ---
yt_dlp.utils.bug_reports_message = lambda: ''

# --- FFmpeg Options ---
# -reconnect 1: Enable reconnection
# -reconnect_streamed 1: Enable reconnection for streamed media
# -reconnect_delay_max 5: Max delay in seconds before reconnecting
# -nostdin: Prevents FFmpeg from reading stdin, which can cause issues
FFMPEG_BEFORE_OPTIONS = '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
FFMPEG_OPTIONS = '-vn' # -vn: Disable video recording

# --- YTDL Options ---
# format: bestaudio/best -> Prefers audio-only, falls back to best quality stream
# noplaylist: True -> If a playlist URL is given, only download the first video
# default_search: auto -> Allows searching YouTube if input isn't a URL
# quiet: True -> Suppress console output from yt-dlp
# no_warnings: True -> Suppress warnings
# source_address: 0.0.0.0 -> Helps with potential IP binding issues (IPv4)
YDL_OPTS = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',  # bind to ipv4 since ipv6 addresses cause issues sometimes
}

# Configure root logger or specific logger for more detail
# logging.basicConfig(level=logging.DEBUG) # Uncomment for maximum detail globally
# Or configure just this cog's logger
logger = logging.getLogger(__name__)
# To see DEBUG messages from this cog specifically:
# logger.setLevel(logging.DEBUG) # Uncomment if needed for deep debugging

class Song:
    """Represents a song to be played."""
    def __init__(self, source_url, title, webpage_url, duration, requester):
        self.source_url = source_url # Direct audio stream URL
        self.title = title
        self.webpage_url = webpage_url # Original URL (e.g., YouTube link)
        self.duration = duration # In seconds
        self.requester = requester # Member who requested the song

    def format_duration(self):
        """Formats duration seconds into MM:SS or HH:MM:SS"""
        if self.duration is None:
            return "N/A"
        try:
             duration_int = int(self.duration)
        except (ValueError, TypeError):
             return "N/A" # Handle cases where duration isn't a valid number

        minutes, seconds = divmod(duration_int, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        else:
            return f"{minutes:02d}:{seconds:02d}"

class GuildMusicState:
    """Manages music state for a single guild."""
    def __init__(self, bot: commands.Bot, guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.queue = deque()
        self.voice_client: nextcord.VoiceClient | None = None
        self.current_song: Song | None = None
        self.volume = 0.5 # Default volume (50%)
        self.play_next_song = asyncio.Event() # Event to signal next song should play
        self._playback_task: asyncio.Task | None = None # Task running the playback loop
        self._lock = asyncio.Lock() # To prevent race conditions

    async def _playback_loop(self):
        """The main loop that plays songs from the queue."""
        await self.bot.wait_until_ready() # Ensure bot is fully ready before loop starts

        while True:
            self.play_next_song.clear() # Reset event for the current iteration
            song_to_play = None

            async with self._lock: # Ensure queue access is safe
                if not self.queue:
                    logger.info(f"[{self.guild_id}] Queue empty. Playback loop pausing.")
                    self.current_song = None
                    # Optional: Add auto-disconnect logic here after a timeout
                    # Example: Wait for 5 minutes, then cleanup if still paused and queue empty
                    try:
                        await asyncio.wait_for(self.play_next_song.wait(), timeout=300.0) # Wait 5 mins
                    except asyncio.TimeoutError:
                         if not self.queue and not self.voice_client.is_playing(): # Check condition again after timeout
                             logger.info(f"[{self.guild_id}] No activity for 5 minutes. Cleaning up.")
                             # Need to run cleanup in a task as it can disconnect
                             self.bot.loop.create_task(self.cleanup_from_inactivity())
                             return # Exit the loop

                    continue # Re-check queue after being woken up or timed out

                # Get the next song ONLY if the queue is not empty
                song_to_play = self.queue.popleft()
                self.current_song = song_to_play
                logger.info(f"[{self.guild_id}] Popped '{song_to_play.title}' from queue. Queue size: {len(self.queue)}")


            if not self.voice_client or not self.voice_client.is_connected():
                logger.warning(f"[{self.guild_id}] Voice client disconnected unexpectedly. Stopping loop.")
                self.current_song = None # Clear current song as we can't play it
                # Attempt to cleanup state? Or let leave command handle it.
                # Put song back in queue?
                if song_to_play: # If we popped a song but VC disconnected before playing
                    async with self._lock: self.queue.appendleft(song_to_play) # Put it back at the front
                return # Exit the loop if VC is gone

            if song_to_play:
                logger.info(f"[{self.guild_id}] Now playing: {song_to_play.title} requested by {song_to_play.requester.name}")
                source = None
                try:
                    # Create the audio source. FFmpeg is used here.
                    logger.debug(f"[{self.guild_id}] Creating FFmpegOpusAudio source for URL: {song_to_play.source_url}")
                    original_source = await nextcord.FFmpegOpusAudio.from_probe(
                        song_to_play.source_url,
                        before_options=FFMPEG_BEFORE_OPTIONS,
                        options=FFMPEG_OPTIONS,
                        method='fallback' # Use fallback if probe fails initially
                    )
                    source = nextcord.PCMVolumeTransformer(original_source, volume=self.volume)
                    logger.debug(f"[{self.guild_id}] Source created successfully.")

                    # Play the source. The 'after' callback is crucial for the loop.
                    self.voice_client.play(source, after=lambda e: self._handle_after_play(e))
                    logger.debug(f"[{self.guild_id}] voice_client.play() called for {song_to_play.title}")

                    # Notify channel (Optional, find the channel context)
                    # await self.notify_channel(f"Now playing: **{song_to_play.title}** [{song_to_play.format_duration()}] requested by {song_to_play.requester.mention}")

                    await self.play_next_song.wait() # Wait until 'after' callback signals completion or skip
                    logger.debug(f"[{self.guild_id}] play_next_song event set. Loop continues.")


                except nextcord.errors.ClientException as e:
                    logger.error(f"[{self.guild_id}] ClientException playing {song_to_play.title}: {e}")
                    # Handle cases like already playing, etc.
                    await self._notify_channel_error(f"Error playing {song_to_play.title}: {e}")
                    self.play_next_song.set() # Signal to continue loop
                except yt_dlp.utils.DownloadError as e:
                     logger.error(f"[{self.guild_id}] Download Error during playback for {song_to_play.title}: {e}")
                     await self._notify_channel_error(f"Download error for {song_to_play.title}.")
                     self.play_next_song.set() # Signal to continue loop
                except Exception as e:
                    logger.error(f"[{self.guild_id}] Unexpected error during playback of {song_to_play.title}: {e}", exc_info=True)
                    await self._notify_channel_error(f"Unexpected error playing {song_to_play.title}.")
                    self.play_next_song.set() # Signal to continue loop
                finally:
                    # Ensure current song is cleared if loop exits or skips prematurely
                    # (handled by 'after' callback or next loop iteration)
                    logger.debug(f"[{self.guild_id}] Playback attempt for {song_to_play.title} finished (or errored out).")
                    # Current song is cleared at the start of the next loop iteration or on error in 'after'

    def _handle_after_play(self, error):
        """Callback function run after a song finishes or errors."""
        log_prefix = f"[{self.guild_id}] After Play Callback: "
        if error:
            logger.error(f"{log_prefix}Playback error encountered: {error}", exc_info=error)
            # Potential TODO: Try to notify a channel about the error? Requires context.
            # Find relevant channel from bot state? Difficult from callback.
        else:
            logger.debug(f"{log_prefix}Song finished playing successfully.")

        # Regardless of error, signal the playback loop that it can proceed.
        logger.debug(f"{log_prefix}Setting play_next_song event.")
        self.bot.loop.call_soon_threadsafe(self.play_next_song.set)
        # Note: self.current_song is cleared at the start of the next loop iteration.


    def start_playback_loop(self):
        """Starts the playback loop task if not already running."""
        if self._playback_task is None or self._playback_task.done():
            logger.info(f"[{self.guild_id}] Starting playback loop task.")
            self._playback_task = self.bot.loop.create_task(self._playback_loop())
            # Handle potential errors during task creation itself if necessary
            self._playback_task.add_done_callback(self._handle_loop_completion)
        else:
             logger.debug(f"[{self.guild_id}] Playback loop task already running or starting.")
        # Ensure the event is set if there are songs waiting and the loop was paused/just started
        if self.queue and not self.play_next_song.is_set():
             logger.debug(f"[{self.guild_id}] Setting play_next_song event as queue is not empty.")
             self.play_next_song.set()

    def _handle_loop_completion(self, task: asyncio.Task):
        """Callback for when the playback loop task finishes (error or natural exit)."""
        try:
            # Check if the task raised an exception
            if task.exception():
                logger.error(f"[{self.guild_id}] Playback loop task exited with error:", exc_info=task.exception())
            else:
                logger.info(f"[{self.guild_id}] Playback loop task finished gracefully.")
        except asyncio.CancelledError:
             logger.info(f"[{self.guild_id}] Playback loop task was cancelled.")
        except Exception as e:
             logger.error(f"[{self.guild_id}] Error in _handle_loop_completion itself: {e}", exc_info=True)
        # Reset task variable so it can be restarted
        self._playback_task = None


    async def stop_playback(self):
        """Stops playback and clears the queue."""
        async with self._lock:
            self.queue.clear()
            if self.voice_client and self.voice_client.is_playing():
                logger.info(f"[{self.guild_id}] Stopping currently playing track.")
                self.voice_client.stop() # This will trigger the 'after' callback
            self.current_song = None
            logger.info(f"[{self.guild_id}] Playback stopped and queue cleared by command.")
            # If the loop is waiting, wake it up so it sees the empty queue and pauses/exits.
            if not self.play_next_song.is_set():
                self.play_next_song.set()

    async def cleanup(self):
        """Cleans up resources (disconnects VC, stops loop)."""
        guild_id = self.guild_id # Store locally in case self changes during async ops
        logger.info(f"[{guild_id}] Cleaning up music state.")
        # Stop playback and clear queue first
        await self.stop_playback()

        # Cancel the loop task properly
        if self._playback_task and not self._playback_task.done():
            logger.info(f"[{guild_id}] Cancelling playback loop task.")
            self._playback_task.cancel()
            try:
                await self._playback_task # Allow cancellation to process
            except asyncio.CancelledError:
                logger.debug(f"[{guild_id}] Playback task cancelled successfully during cleanup.")
            except Exception as e:
                logger.error(f"[{guild_id}] Error awaiting cancelled playback task: {e}", exc_info=True)
        self._playback_task = None

        # Disconnect voice client
        vc = self.voice_client
        if vc and vc.is_connected():
            logger.info(f"[{guild_id}] Disconnecting voice client during cleanup.")
            try:
                await vc.disconnect(force=True)
                logger.info(f"[{guild_id}] Voice client disconnected.")
            except Exception as e:
                 logger.error(f"[{guild_id}] Error disconnecting voice client: {e}", exc_info=True)
        self.voice_client = None
        self.current_song = None
        # Optional: Remove state from parent cog dictionary if called from leave command context
        # This part should be handled in the leave command itself after calling cleanup.

    async def cleanup_from_inactivity(self):
        """Cleanup specifically triggered by inactivity timeout."""
        logger.info(f"[{self.guild_id}] Initiating cleanup due to inactivity.")
        # Potentially notify a channel? Requires storing channel context earlier.
        await self.cleanup()
        # Remove state from parent cog dictionary
        # Requires access to the cog instance, maybe pass it during init?
        # Or handle removal in the cog's listener/command that calls this.


    async def _notify_channel_error(self, message: str):
        """Helper to try and send error messages to a relevant channel."""
        # This is tricky as we don't always have context (ctx) here
        # We could try finding the last used text channel for the bot in this guild
        # or store the channel from the last command. For now, just log.
        logger.warning(f"[{self.guild_id}] Channel Notification Placeholder: {message}")
        # Example (needs channel stored):
        # if self.last_text_channel:
        #    try: await self.last_text_channel.send(message)
        #    except Exception: logger.error("Failed to send error notification.")


class MusicCog(commands.Cog, name="Music"):
    """Commands for playing music in voice channels."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_states: dict[int, GuildMusicState] = {} # Guild ID -> State
        self.ydl = yt_dlp.YoutubeDL(YDL_OPTS) # Reusable YTDL instance

    def get_guild_state(self, guild_id: int) -> GuildMusicState:
        """Gets or creates the music state for a guild."""
        if guild_id not in self.guild_states:
            logger.debug(f"Creating new GuildMusicState for guild {guild_id}")
            self.guild_states[guild_id] = GuildMusicState(self.bot, guild_id)
        return self.guild_states[guild_id]

    async def _extract_song_info(self, query: str) -> dict | None:
        """Extracts song info using yt-dlp in an executor."""
        # Determine guild ID for logging prefix (if possible, otherwise use bot ID)
        # This method doesn't have guild context directly, using bot ID as fallback prefix
        log_prefix = f"[{self.bot.user.id or 'Bot'}] YTDL:"
        logger.debug(f"{log_prefix} Attempting to extract info for query: '{query}'") # Log query
        try:
            # Run blocking IO in executor
            loop = asyncio.get_event_loop()
            # process=False first to get basic data, then process if needed
            partial = functools.partial(self.ydl.extract_info, query, download=False, process=False)
            data = await loop.run_in_executor(None, partial)
            # --- Log raw data ---
            logger.debug(f"{log_prefix} Raw YTDL data (first 500 chars): {str(data)[:500]}")
            # ---

            if not data:
                logger.warning(f"{log_prefix} yt-dlp returned no data for query: {query}")
                return None

            # If it's a playlist, extract_info returns a dict with 'entries'
            if 'entries' in data:
                entry = data['entries'][0]
                logger.debug(f"{log_prefix} Query was a playlist, using first entry: {entry.get('title', 'N/A')}")
            else:
                entry = data

            if not entry:
                 logger.warning(f"{log_prefix} Could not find a valid entry in yt-dlp data for: {query}")
                 return None

            # --- Re-process to get formats and potential direct URL ---
            logger.debug(f"{log_prefix} Processing IE result for entry...")
            try:
                # Ensure entry is not None before processing
                if entry:
                    processed_entry = self.ydl.process_ie_result(entry, download=False)
                    logger.debug(f"{log_prefix} Processed entry keys: {processed_entry.keys() if processed_entry else 'None'}")
                else: # Should not happen if check above passed, but safety first
                    logger.warning(f"{log_prefix} Entry was None before processing IE result.")
                    processed_entry = None
            except Exception as process_err:
                logger.error(f"{log_prefix} Error processing IE result: {process_err}", exc_info=True)
                processed_entry = entry # Fallback to original entry if processing fails

            # Ensure processed_entry is not None before proceeding
            if not processed_entry:
                 logger.error(f"{log_prefix} Processed entry became None. Cannot proceed.")
                 return None

            # Try to get the best audio stream URL
            audio_url = None
            logger.debug(f"{log_prefix} Checking 'url' in processed_entry: {processed_entry.get('url')}")
            if 'url' in processed_entry: # Often the direct stream URL is here after processing
                audio_url = processed_entry['url']
            elif 'formats' in processed_entry:
                 formats = processed_entry.get('formats', [])
                 logger.debug(f"{log_prefix} 'url' not found directly, checking {len(formats)} formats...")
                 formats_to_log = min(len(formats), 5)
                 for i in range(formats_to_log):
                     f = formats[i]
                     logger.debug(f"  Format {i}: acodec={f.get('acodec')}, vcodec={f.get('vcodec')}, url_present={bool(f.get('url'))}, format_note={f.get('format_note')}, protocol={f.get('protocol')}")

                 # Prioritize opus, then vorbis, then aac with valid URLs
                 for f in formats:
                    if f.get('acodec') == 'opus' and f.get('vcodec') == 'none' and f.get('url'): audio_url = f.get('url'); break
                 if not audio_url:
                    for f in formats:
                        if f.get('acodec') == 'vorbis' and f.get('vcodec') == 'none' and f.get('url'): audio_url = f.get('url'); break
                 if not audio_url:
                     for f in formats:
                         if f.get('acodec') == 'aac' and f.get('vcodec') == 'none' and f.get('url'): audio_url = f.get('url'); break
                 # Fallback for generic 'bestaudio'
                 if not audio_url:
                     for f in formats:
                          if ('bestaudio' in f.get('format_id', '').lower() or 'bestaudio' in f.get('format_note', '').lower()) and f.get('url'):
                               audio_url = f['url']
                               logger.debug(f"{log_prefix} Found potential bestaudio URL in formats.")
                               break
                 # Fallback: requested_formats (less common)
                 if not audio_url:
                     requested_formats = processed_entry.get('requested_formats')
                     if requested_formats and isinstance(requested_formats, list) and len(requested_formats) > 0:
                         audio_url = requested_formats[0].get('url')
                         logger.debug(f"{log_prefix} Found URL in requested_formats.")

            logger.debug(f"{log_prefix} Final audio URL determined: {audio_url}")

            if not audio_url:
                logger.error(f"{log_prefix} Could not extract playable audio URL for: {processed_entry.get('title', query)}")
                return None

            final_info = {
                'source_url': audio_url,
                'title': processed_entry.get('title', 'Unknown Title'),
                'webpage_url': processed_entry.get('webpage_url', query),
                'duration': processed_entry.get('duration'), # In seconds
                'uploader': processed_entry.get('uploader', 'Unknown Uploader')
            }
            logger.debug(f"{log_prefix} Successfully extracted song info: {final_info.get('title')}")
            return final_info

        except yt_dlp.utils.DownloadError as e:
            logger.error(f"{log_prefix} YTDL DownloadError for '{query}': {e}")
            err_str = str(e).lower()
            if "unsupported url" in err_str: return {'error': 'unsupported'}
            if "video unavailable" in err_str: return {'error': 'unavailable'}
            if "confirm your age" in err_str: return {'error': 'age_restricted'}
            # Add more specific checks if needed
            return {'error': 'download'}
        except Exception as e:
            logger.error(f"{log_prefix} Unexpected error extracting info for '{query}': {e}", exc_info=True)
            return {'error': 'extraction'}


    # --- Listener for Voice State Updates ---
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: nextcord.Member, before: nextcord.VoiceState, after: nextcord.VoiceState):
        """Handles bot disconnection or empty/joined channels."""
        if not member.guild: return # Ignore DM updates if any happen

        guild_id = member.guild.id
        state = self.guild_states.get(guild_id) # Use get to avoid creating state if not needed

        # If no state exists for this guild, ignore the update
        if not state:
            # logger.debug(f"Ignoring voice state update in {guild_id} (no active music state).")
            return

        # --- Bot's own state changes ---
        if member.id == self.bot.user.id:
            # Bot was disconnected (kicked, moved, channel deleted, etc.)
            if before.channel and not after.channel:
                logger.warning(f"[{guild_id}] Bot was disconnected from voice channel {before.channel.name}. Cleaning up music state.")
                await state.cleanup()
                if guild_id in self.guild_states: # Remove state entry after cleanup
                     del self.guild_states[guild_id]
                     logger.info(f"[{guild_id}] Removed music state after bot disconnect.")
            # Bot moved channels (less common to handle explicitly unless needed)
            elif before.channel and after.channel and before.channel != after.channel:
                 logger.info(f"[{guild_id}] Bot moved from {before.channel.name} to {after.channel.name}.")
                 # Update state's channel reference if needed, though voice_client usually handles this
                 if state.voice_client: state.voice_client.channel = after.channel

        # --- Other users' state changes in the bot's channel ---
        elif state.voice_client and state.voice_client.is_connected():
            current_bot_channel = state.voice_client.channel
            # User left the bot's channel
            if before.channel == current_bot_channel and after.channel != current_bot_channel:
                 logger.debug(f"[{guild_id}] User {member.name} left bot channel {before.channel.name}.")
                 # Check if bot is now alone (only member left is the bot itself)
                 if len(current_bot_channel.members) == 1 and self.bot.user in current_bot_channel.members:
                     logger.info(f"[{guild_id}] Bot is now alone in {current_bot_channel.name}. Pausing playback.")
                     if state.voice_client.is_playing():
                         state.voice_client.pause()
                     # Optional TODO: Start inactivity timer here in the state object
                     # state.start_inactivity_timer()

            # User joined the bot's channel
            elif before.channel != current_bot_channel and after.channel == current_bot_channel:
                 logger.debug(f"[{guild_id}] User {member.name} joined bot channel {after.channel.name}.")
                 # If bot was paused (potentially due to being alone), resume
                 if state.voice_client.is_paused():
                     logger.info(f"[{guild_id}] User joined, resuming paused playback.")
                     state.voice_client.resume()
                 # Optional TODO: Cancel inactivity timer if running
                 # state.cancel_inactivity_timer()


    # --- Music Commands ---

    @commands.command(name='join', aliases=['connect', 'j'], help="Connects the bot to your current voice channel.")
    @commands.guild_only()
    async def join_command(self, ctx: commands.Context):
        """Connects the bot to the voice channel the command user is in."""
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send("You need to be in a voice channel to use this command.")

        channel = ctx.author.voice.channel
        state = self.get_guild_state(ctx.guild.id)

        async with state._lock: # Protect connection state changes
            if state.voice_client and state.voice_client.is_connected():
                if state.voice_client.channel == channel:
                    await ctx.send(f"I'm already connected to {channel.mention}.")
                else: # Move if connected to a different channel
                    try:
                        await state.voice_client.move_to(channel)
                        await ctx.send(f"Moved to {channel.mention}.")
                        logger.info(f"[{ctx.guild.id}] Moved to voice channel: {channel.name}")
                    except asyncio.TimeoutError:
                        await ctx.send(f"Timed out trying to move to {channel.mention}.")
                    except Exception as e:
                         await ctx.send(f"Error moving channels: {e}")
                         logger.error(f"[{ctx.guild.id}] Error moving VC: {e}", exc_info=True)
            else: # Connect if not connected anywhere in this guild
                try:
                    state.voice_client = await channel.connect()
                    await ctx.send(f"Connected to {channel.mention}.")
                    logger.info(f"[{ctx.guild.id}] Connected to voice channel: {channel.name}")
                    # Automatically start the playback loop after connecting
                    state.start_playback_loop()
                except asyncio.TimeoutError:
                    await ctx.send(f"Timed out connecting to {channel.mention}.")
                    logger.warning(f"[{ctx.guild.id}] Timeout connecting to {channel.name}.")
                    # Clean up if connection failed partway
                    if ctx.guild.id in self.guild_states: del self.guild_states[ctx.guild.id]
                except nextcord.errors.ClientException as e:
                     await ctx.send(f"Unable to connect: {e}. Maybe check my permissions?")
                     logger.warning(f"[{ctx.guild.id}] ClientException on connect: {e}")
                     if ctx.guild.id in self.guild_states: del self.guild_states[ctx.guild.id]
                except Exception as e:
                    await ctx.send(f"An error occurred connecting: {e}")
                    logger.error(f"[{ctx.guild.id}] Error connecting to VC: {e}", exc_info=True)
                    if ctx.guild.id in self.guild_states: del self.guild_states[ctx.guild.id]


    @commands.command(name='leave', aliases=['disconnect', 'dc', 'fuckoff'], help="Disconnects the bot from the voice channel.")
    @commands.guild_only()
    async def leave_command(self, ctx: commands.Context):
        """Disconnects the bot from its current voice channel in the guild."""
        state = self.guild_states.get(ctx.guild.id) # Use get

        if not state or not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected to any voice channel.")

        logger.info(f"[{ctx.guild.id}] Leave command initiated by {ctx.author.name}.")
        await state.cleanup() # Handles stopping playback, disconnecting, and cleaning state vars

        if ctx.guild.id in self.guild_states: # Remove state entry after cleanup
            del self.guild_states[ctx.guild.id]
            logger.info(f"[{ctx.guild.id}] Removed music state after leave command.")

        await ctx.message.add_reaction('👋')


    @commands.command(name='play', aliases=['p'], help="Plays a song from a URL or search query, or adds it to the queue.")
    @commands.guild_only()
    async def play_command(self, ctx: commands.Context, *, query: str):
        """Plays audio from a URL (YouTube, SoundCloud, etc.) or searches YouTube."""
        state = self.get_guild_state(ctx.guild.id)
        logger.info(f"[{ctx.guild.id}] User {ctx.author.name} initiated play with query: {query}") # Log who and what

        # 1. Ensure bot is connected (or connect it)
        if not state.voice_client or not state.voice_client.is_connected():
            if ctx.author.voice and ctx.author.voice.channel:
                 logger.info(f"[{ctx.guild.id}] Play requested, connecting to {ctx.author.voice.channel.name} first.")
                 await ctx.invoke(self.join_command) # Attempt to join user's channel
                 state = self.get_guild_state(ctx.guild.id) # Re-fetch state AFTER join attempt
                 if not state.voice_client or not state.voice_client.is_connected(): # Check if join succeeded
                      logger.warning(f"[{ctx.guild.id}] Failed to join VC after invoking join_command.")
                      # No need to send message here, join_command likely did or failed silently
                      return # Stop processing if join failed
            else:
                logger.warning(f"[{ctx.guild.id}] Play command failed: User not in VC and bot not connected.")
                return await ctx.send("You need to be in a voice channel for me to join.")
        elif ctx.author.voice and ctx.author.voice.channel != state.voice_client.channel:
             logger.warning(f"[{ctx.guild.id}] Play command failed: User in different VC ({ctx.author.voice.channel.name}) than bot ({state.voice_client.channel.name}).")
             return await ctx.send(f"You must be in the same voice channel ({state.voice_client.channel.mention}) as me to add songs.")
        elif not ctx.author.voice or ctx.author.voice.channel != state.voice_client.channel:
             # Case where bot is connected, but user is not in the bot's channel (or any channel)
              logger.warning(f"[{ctx.guild.id}] Play command failed: User not in bot's VC.")
              return await ctx.send(f"You need to be in {state.voice_client.channel.mention} to add songs.")


        # 2. Extract Song Info
        logger.debug(f"[{ctx.guild.id}] Calling _extract_song_info for query: {query}")
        async with ctx.typing():
            song_info = await self._extract_song_info(query)

            # --- Log result of extraction ---
            logger.debug(f"[{ctx.guild.id}] _extract_song_info returned: {song_info}")
            # ---

            if not song_info:
                logger.warning(f"[{ctx.guild.id}] _extract_song_info failed to return info for query: {query}")
                return await ctx.send("Could not retrieve song information. The URL might be invalid, private, or the service unavailable.")
            if song_info.get('error'):
                 error_type = song_info['error']
                 logger.warning(f"[{ctx.guild.id}] _extract_song_info returned error: {error_type} for query: {query}")
                 if error_type == 'unsupported': msg = "Sorry, I don't support that URL or service."
                 elif error_type == 'unavailable': msg = "That video is unavailable (maybe private or deleted)."
                 elif error_type == 'download': msg = "There was an error trying to access the song data."
                 elif error_type == 'age_restricted': msg = "Sorry, I can't play age-restricted content."
                 else: msg = "An unknown error occurred while fetching the song."
                 return await ctx.send(msg)

            # --- Log before creating Song object ---
            logger.debug(f"[{ctx.guild.id}] Creating Song object with info: Title='{song_info.get('title')}', URL='{song_info.get('source_url')}'")
            # ---
            try:
                song = Song(
                    source_url=song_info['source_url'],
                    title=song_info['title'],
                    webpage_url=song_info['webpage_url'],
                    duration=song_info['duration'],
                    requester=ctx.author
                )
            except KeyError as e:
                 logger.error(f"[{ctx.guild.id}] Missing key in song_info dict: {e}. Info: {song_info}")
                 return await ctx.send("Failed to process song information after fetching.")
            except Exception as e:
                 logger.error(f"[{ctx.guild.id}] Error creating Song object: {e}. Info: {song_info}", exc_info=True)
                 return await ctx.send("An internal error occurred preparing the song.")


        # 3. Add to Queue and Signal Playback Loop
        async with state._lock:
            state.queue.append(song)
            queue_pos = len(state.queue)
            # --- Log adding to queue ---
            logger.info(f"[{ctx.guild.id}] Added '{song.title}' to queue at position {queue_pos}. Queue size now: {len(state.queue)}")
            # ---

            embed = nextcord.Embed(
                title="Added to Queue" if (state.current_song or queue_pos > 1) else "Now Playing",
                description=f"[{song.title}]({song.webpage_url})",
                color=nextcord.Color.green()
            )
            embed.add_field(name="Duration", value=song.format_duration(), inline=True)
            if queue_pos > 1 or state.current_song: # Only show position if not first in queue
                embed.add_field(name="Position", value=f"#{queue_pos}", inline=True)
            embed.set_footer(text=f"Requested by {song.requester.display_name}", icon_url=song.requester.display_avatar.url)

            try: # Send feedback message
                await ctx.send(embed=embed)
            except nextcord.HTTPException as e:
                 logger.error(f"[{ctx.guild.id}] Failed to send 'Added to Queue' message: {e}")


            # Ensure the playback loop is running and signal it if necessary
            state.start_playback_loop() # Starts if not running


    @commands.command(name='skip', aliases=['s'], help="Skips the currently playing song.")
    @commands.guild_only()
    async def skip_command(self, ctx: commands.Context):
        """Skips the current song and plays the next one in the queue."""
        state = self.guild_states.get(ctx.guild.id)

        if not state or not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected to a voice channel.")
        if not state.current_song: # Check if a song is loaded as current, even if paused
            return await ctx.send("There's nothing playing to skip.")

        # Optional: Add vote-skip logic here later if desired

        logger.info(f"[{ctx.guild.id}] Skip requested by {ctx.author.name} for '{state.current_song.title}'.")
        state.voice_client.stop() # Triggers the 'after' callback, which starts the next song via play_next_song.set()
        await ctx.message.add_reaction('⏭️') # Indicate success


    @commands.command(name='stop', help="Stops playback completely and clears the queue.")
    @commands.guild_only()
    async def stop_command(self, ctx: commands.Context):
        """Stops the music, clears the queue, but stays connected."""
        state = self.guild_states.get(ctx.guild.id)

        if not state or not state.voice_client or not state.voice_client.is_connected():
            # Maybe check if queue has items even if not connected? Unlikely useful.
            return await ctx.send("I'm not connected or not playing anything.")

        logger.info(f"[{ctx.guild.id}] Stop requested by {ctx.author.name}.")
        await state.stop_playback() # Handles stopping player and clearing queue
        await ctx.send("Playback stopped and queue cleared.")
        await ctx.message.add_reaction('⏹️')


    @commands.command(name='pause', help="Pauses the currently playing song.")
    @commands.guild_only()
    async def pause_command(self, ctx: commands.Context):
        """Pauses the current audio playback."""
        state = self.guild_states.get(ctx.guild.id)

        if not state or not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected.")
        if not state.voice_client.is_playing():
             # Check if it's already paused
             if state.voice_client.is_paused():
                  return await ctx.send("Playback is already paused.")
             else:
                  return await ctx.send("Nothing is actively playing to pause.")
        # If is_playing() is true, then is_paused() must be false, so pause it
        state.voice_client.pause()
        logger.info(f"[{ctx.guild.id}] Playback paused by {ctx.author.name}.")
        await ctx.message.add_reaction('⏸️')


    @commands.command(name='resume', aliases=['unpause'], help="Resumes a paused song.")
    @commands.guild_only()
    async def resume_command(self, ctx: commands.Context):
        """Resumes audio playback if paused."""
        state = self.guild_states.get(ctx.guild.id)

        if not state or not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected.")
        if not state.voice_client.is_paused():
            if state.voice_client.is_playing():
                 return await ctx.send("Playback is already playing.")
            else:
                 return await ctx.send("Nothing is currently paused.")
        # If is_paused() is true, resume it
        state.voice_client.resume()
        logger.info(f"[{ctx.guild.id}] Playback resumed by {ctx.author.name}.")
        await ctx.message.add_reaction('▶️')


    @commands.command(name='queue', aliases=['q', 'playlist'], help="Shows the current song queue.")
    @commands.guild_only()
    async def queue_command(self, ctx: commands.Context):
        """Displays the list of songs waiting to be played."""
        state = self.guild_states.get(ctx.guild.id)

        if not state:
             return await ctx.send("I haven't played anything in this server yet.")

        async with state._lock: # Ensure queue isn't modified while reading
            if not state.current_song and not state.queue:
                return await ctx.send("The queue is empty and nothing is playing.")

            embed = nextcord.Embed(title="Music Queue", color=nextcord.Color.blurple())
            current_display = "Nothing currently playing."
            if state.current_song:
                song = state.current_song
                status = "▶️ Playing" if state.voice_client and state.voice_client.is_playing() else "⏸️ Paused"
                current_display = f"{status}: **[{song.title}]({song.webpage_url})** `[{song.format_duration()}]` - Req by {song.requester.mention}"
            embed.add_field(name="Now Playing", value=current_display, inline=False)

            if state.queue:
                queue_list = []
                max_display = 10 # Limit display to prevent huge messages
                total_queue_duration = 0
                # Use list(state.queue) to create a temporary copy for iteration
                queue_copy = list(state.queue)
                for i, song in enumerate(queue_copy[:max_display]):
                     queue_list.append(f"`{i+1}.` [{song.title}]({song.webpage_url}) `[{song.format_duration()}]` - Req by {song.requester.display_name}")
                     if song.duration: total_queue_duration += int(song.duration) # Sum duration

                if len(queue_copy) > max_display:
                    queue_list.append(f"\n...and {len(queue_copy) - max_display} more.")
                    # Calculate remaining duration approx if needed
                    for song in queue_copy[max_display:]:
                         if song.duration: total_queue_duration += int(song.duration)

                # Format total duration
                total_dur_str = Song(None,None,None,total_queue_duration,None).format_duration() if total_queue_duration > 0 else "N/A"

                embed.add_field(
                    name=f"Up Next ({len(queue_copy)} song{'s' if len(queue_copy) != 1 else ''}, Total: {total_dur_str})",
                    value="\n".join(queue_list) or "No songs in queue.",
                    inline=False
                )
            else:
                 embed.add_field(name="Up Next", value="No songs in queue.", inline=False)

            total_songs = len(state.queue) + (1 if state.current_song else 0)
            embed.set_footer(text=f"Total songs: {total_songs} | Volume: {int(state.volume * 100)}%")
            await ctx.send(embed=embed)


    @commands.command(name='volume', aliases=['vol'], help="Changes the player volume (0-100).")
    @commands.guild_only()
    async def volume_command(self, ctx: commands.Context, *, volume: int):
        """Sets the volume of the music player."""
        state = self.guild_states.get(ctx.guild.id)

        if not state or not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected to a voice channel.")

        if not 0 <= volume <= 100:
            return await ctx.send("Volume must be between 0 and 100.")

        new_volume = volume / 100.0
        state.volume = new_volume
        logger.debug(f"[{ctx.guild.id}] State volume set to {new_volume}")


        # If actively playing, adjust the source volume transformer
        if state.voice_client.source and isinstance(state.voice_client.source, nextcord.PCMVolumeTransformer):
            state.voice_client.source.volume = new_volume
            logger.info(f"[{ctx.guild.id}] Volume adjusted to {volume}% by {ctx.author.name} (active player).")
        else:
             logger.info(f"[{ctx.guild.id}] Volume pre-set to {volume}% by {ctx.author.name} (will apply to next song).")


        await ctx.send(f"Volume set to **{volume}%**.")


    # --- Error Handling for Music Commands ---
    async def cog_command_error(self, ctx: commands.Context, error):
        """Local error handler specifically for commands in this Cog."""
        log_prefix = f"[{ctx.guild.id if ctx.guild else 'DM'}] MusicCog Error:"

        # Handle checks first
        if isinstance(error, commands.CheckFailure):
             if isinstance(error, commands.GuildOnly):
                  logger.warning(f"{log_prefix} GuildOnly command '{ctx.command.name}' used in DM by {ctx.author.name}.")
                  # Don't message back in DMs usually for guild only commands
                  return
             # Add other specific check failures if needed
             logger.warning(f"{log_prefix} Check failed for '{ctx.command.name}' by {ctx.author.name}: {error}")
             # Send a generic message or let global handler manage it
             # await ctx.send("You don't have permission or the conditions aren't met to use this command here.")
             return # Often handled by global handler better

        elif isinstance(error, commands.MissingRequiredArgument):
             logger.debug(f"{log_prefix} Missing argument for '{ctx.command.name}': {error.param.name}")
             await ctx.send(f"You forgot the `{error.param.name}` argument. Check `?help {ctx.command.name}`.")
        elif isinstance(error, commands.BadArgument):
             logger.debug(f"{log_prefix} Bad argument for '{ctx.command.name}': {error}")
             await ctx.send(f"Invalid argument type provided. Check `?help {ctx.command.name}`.")
        elif isinstance(error, commands.CommandInvokeError):
            original = error.original
            logger.error(f"{log_prefix} Error invoking command '{ctx.command.name}': {original.__class__.__name__}: {original}", exc_info=original)
            if isinstance(original, nextcord.errors.ClientException):
                await ctx.send(f"Voice Error: {original}") # Often permissions or connection issues
            elif isinstance(original, yt_dlp.utils.DownloadError):
                 # Handle specific download errors that might bubble up
                 await ctx.send("Error fetching song data (maybe unavailable or network issue?).")
            else:
                # Generic internal error for other exceptions raised within the command
                await ctx.send(f"An internal error occurred while running `{ctx.command.name}`.")
        else:
            # If not handled here, let the global handler try (or log if no global handler catches it)
            logger.warning(f"{log_prefix} Unhandled error type in cog_command_error for '{ctx.command.name}': {type(error).__name__}: {error}")
            # To pass it to the global handler, remove the cog_command_error handler
            # or re-raise the error here:
            # raise error


def setup(bot: commands.Bot):
    """Adds the MusicCog to the bot."""
    # --- Attempt Manual Opus Load ---
    # Define the path where libopus was found inside the container
    # Use the path identified via `find` or `ldconfig -p` in the container shell
    OPUS_PATH = '/usr/lib/x86_64-linux-gnu/libopus.so.0' # Confirmed path

    try:
        if not nextcord.opus.is_loaded():
            # Only attempt manual load if automatic loading failed
            logger.info(f"Opus not auto-loaded. Attempting manual load from: {OPUS_PATH}")
            nextcord.opus.load_opus(OPUS_PATH)

            # Verify if the manual load actually succeeded
            if nextcord.opus.is_loaded():
                 logger.info("Opus manually loaded successfully.")
            else:
                 # This case should ideally not happen if load_opus didn't raise an error,
                 # but check just in case.
                 logger.critical("Manual Opus load attempt finished, but is_loaded() is still false.")
        else:
            # If auto-load worked for some reason, log that.
            logger.info("Opus library was already loaded automatically (unexpected based on previous logs, but good).")

    except nextcord.opus.OpusNotLoaded as e:
        # This error means load_opus(OPUS_PATH) specifically failed.
        logger.critical(f"CRITICAL: Manual Opus load failed using path '{OPUS_PATH}'. Error: {e}. "
                        "Ensure the path is correct and the library file is valid and has correct permissions inside the container.")
        # Optionally, prevent loading the cog if opus fails definitively
        # raise commands.ExtensionError(f"Opus library failed to load from {OPUS_PATH}", original=e)
    except Exception as e:
         # Catch any other unexpected errors during the loading process
         logger.critical(f"CRITICAL: An unexpected error occurred during manual Opus load attempt: {e}", exc_info=True)
         # Optionally raise
         # raise commands.ExtensionError("Unexpected error loading Opus", original=e)
    # --- End Manual Opus Load Attempt ---

    # Add the cog to the bot. Playback depends on successful Opus loading above.
    bot.add_cog(MusicCog(bot))
    logger.info("MusicCog added to bot.")
