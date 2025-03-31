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
logger.setLevel(logging.DEBUG) # <<< ENABLE DEBUG LOGGING FOR THIS COG

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
        await self.bot.wait_until_ready()
        logger.info(f"[{self.guild_id}] Playback loop starting.") # Log loop start

        while True:
            self.play_next_song.clear()
            log_prefix = f"[{self.guild_id}] Loop:" # Prefix for loop logs
            logger.debug(f"{log_prefix} Top of loop, play_next_song cleared.")
            song_to_play = None

            async with self._lock:
                logger.debug(f"{log_prefix} Lock acquired for queue check.")
                queue_is_empty = not self.queue
                if not queue_is_empty:
                    song_to_play = self.queue.popleft()
                    self.current_song = song_to_play
                    logger.info(f"{log_prefix} Popped '{song_to_play.title}'. Queue size: {len(self.queue)}")
                else:
                     logger.debug(f"{log_prefix} Queue is empty.")
                     self.current_song = None
                # LOCK RELEASED HERE
            logger.debug(f"{log_prefix} Lock released after queue check.")


            if queue_is_empty:
                logger.info(f"{log_prefix} Queue empty, pausing loop. Waiting for play_next_song event...")
                # --- Temporarily Disable Inactivity Timeout ---
                # try:
                #     # await asyncio.wait_for(self.play_next_song.wait(), timeout=300.0) # Wait 5 mins
                # except asyncio.TimeoutError:
                #      # Check condition again after timeout ONLY IF VC still exists
                #      if self.voice_client and not self.queue and not self.voice_client.is_playing():
                #          logger.info(f"[{self.guild_id}] No activity timeout. Cleaning up.")
                #          # Need to handle cleanup carefully to avoid race conditions
                #          # self.bot.loop.create_task(self.cleanup_from_inactivity())
                #          return # Exit the loop
                await self.play_next_song.wait() # Wait indefinitely for now
                # --- End Temporary Disable ---
                logger.info(f"{log_prefix} play_next_song event received while paused. Continuing loop.")
                continue # Re-check queue

            # --- Check Voice Client ---
            logger.debug(f"{log_prefix} Checking voice client state.")
            if not self.voice_client or not self.voice_client.is_connected():
                logger.warning(f"{log_prefix} Voice client disconnected. Stopping loop.")
                if song_to_play: # Put song back if popped but VC died
                    logger.warning(f"{log_prefix} Putting '{song_to_play.title}' back in queue.")
                    async with self._lock: self.queue.appendleft(song_to_play)
                self.current_song = None # Ensure current song is cleared
                return # Exit loop

            # --- Play Song ---
            if song_to_play:
                logger.info(f"{log_prefix} Attempting to play: {song_to_play.title}")
                source = None
                try:
                    logger.debug(f"{log_prefix} Creating FFmpegOpusAudio source for URL: {song_to_play.source_url}")
                    original_source = await nextcord.FFmpegOpusAudio.from_probe(
                        song_to_play.source_url,
                        before_options=FFMPEG_BEFORE_OPTIONS,
                        options=FFMPEG_OPTIONS,
                        method='fallback'
                    )
                    source = nextcord.PCMVolumeTransformer(original_source, volume=self.volume)
                    logger.debug(f"{log_prefix} Source created successfully.")

                    # Check VC status right before playing
                    if not self.voice_client or not self.voice_client.is_connected():
                         logger.warning(f"{log_prefix} VC disconnected just before calling play(). Aborting play for '{song_to_play.title}'.")
                         async with self._lock: self.queue.appendleft(song_to_play) # Put song back
                         self.current_song = None # Clear current song
                         self.play_next_song.set() # Allow loop to continue and potentially exit/recheck
                         continue # Skip to next loop iteration

                    # Play the source
                    self.voice_client.play(source, after=lambda e: self._handle_after_play(e))
                    logger.info(f"{log_prefix} voice_client.play() called successfully for {song_to_play.title}")

                    # --- Wait for song to finish or be skipped ---
                    logger.debug(f"{log_prefix} Waiting for play_next_song event (song completion/skip)...")
                    await self.play_next_song.wait()
                    logger.debug(f"{log_prefix} play_next_song event received after playback attempt for '{song_to_play.title}'.")
                    # ---

                except nextcord.errors.ClientException as e:
                    logger.error(f"{log_prefix} ClientException playing {song_to_play.title}: {e}")
                    await self._notify_channel_error(f"Error playing '{song_to_play.title}': {e}")
                    self.play_next_song.set() # Signal to continue loop
                except yt_dlp.utils.DownloadError as e: # Less likely here, more in extraction
                     logger.error(f"{log_prefix} Download Error during playback attempt for {song_to_play.title}: {e}")
                     await self._notify_channel_error(f"Download error for '{song_to_play.title}'.")
                     self.play_next_song.set() # Signal to continue loop
                except Exception as e:
                    logger.error(f"{log_prefix} Unexpected error during playback of {song_to_play.title}: {e}", exc_info=True)
                    await self._notify_channel_error(f"Unexpected error playing '{song_to_play.title}'.")
                    self.play_next_song.set() # Signal to continue loop
                finally:
                    # This block executes whether play succeeded, failed, or was skipped
                    logger.debug(f"{log_prefix} Playback block for '{song_to_play.title if song_to_play else 'None'}' finished.")
                    # self.current_song is cleared at the start of the next iteration if queue becomes empty
            else:
                 # This case shouldn't be reached if queue_is_empty check is correct
                 logger.warning(f"{log_prefix} Reached play block but song_to_play is None. Waiting to avoid tight loop.")
                 await self.play_next_song.wait() # Wait to avoid cpu spin

    def _handle_after_play(self, error):
        """Callback function run after a song finishes or errors."""
        log_prefix = f"[{self.guild_id}] After Play Callback: "
        if error:
            # Log the specific error object received by the callback using repr
            logger.error(f"{log_prefix}Playback error encountered: {error!r}", exc_info=error)
        else:
            logger.debug(f"{log_prefix}Song finished playing successfully.")

        # Signal the playback loop that it can proceed.
        logger.debug(f"{log_prefix}Setting play_next_song event.")
        # Use call_soon_threadsafe as this callback might be from a different thread (FFmpeg)
        self.bot.loop.call_soon_threadsafe(self.play_next_song.set)


    def start_playback_loop(self):
        """Starts the playback loop task if not already running."""
        if self._playback_task is None or self._playback_task.done():
            logger.info(f"[{self.guild_id}] Starting playback loop task.")
            self._playback_task = self.bot.loop.create_task(self._playback_loop())
            self._playback_task.add_done_callback(self._handle_loop_completion)
        else:
             logger.debug(f"[{self.guild_id}] Playback loop task already running or starting.")
        # Ensure the event is set if there are songs waiting and the loop might be paused
        if self.queue and not self.play_next_song.is_set():
             logger.debug(f"[{self.guild_id}] start_playback_loop: Setting play_next_song event as queue is not empty.")
             self.play_next_song.set()

    def _handle_loop_completion(self, task: asyncio.Task):
        """Callback for when the playback loop task finishes (error or natural exit)."""
        try:
            # Check if the task raised an exception
            if task.cancelled():
                 logger.info(f"[{self.guild_id}] Playback loop task was cancelled.")
            elif task.exception():
                logger.error(f"[{self.guild_id}] Playback loop task exited with error:", exc_info=task.exception())
            else:
                logger.info(f"[{self.guild_id}] Playback loop task finished gracefully (e.g., due to cleanup).")
        except asyncio.CancelledError:
             # This can happen if the callback itself is interrupted during shutdown
             logger.info(f"[{self.guild_id}] _handle_loop_completion was cancelled.")
        except Exception as e:
             logger.error(f"[{self.guild_id}] Error in _handle_loop_completion itself: {e}", exc_info=True)
        # Reset task variable so it can be restarted if needed (e.g., by join/play)
        # Important: Don't automatically restart here, let commands do that.
        self._playback_task = None
        logger.debug(f"[{self.guild_id}] Playback task reference cleared.")


    async def stop_playback(self):
        """Stops playback and clears the queue."""
        async with self._lock:
            self.queue.clear()
            vc = self.voice_client
            if vc and vc.is_playing():
                logger.info(f"[{self.guild_id}] Stopping currently playing track via stop_playback.")
                vc.stop() # This will trigger the 'after' callback
            self.current_song = None
            logger.info(f"[{self.guild_id}] Queue cleared by stop_playback.")
            # If the loop is waiting, wake it up so it sees the empty queue and pauses/exits.
            if not self.play_next_song.is_set():
                logger.debug(f"[{self.guild_id}] Setting play_next_song event in stop_playback.")
                self.play_next_song.set()

    async def cleanup(self):
        """Cleans up resources (disconnects VC, stops loop)."""
        guild_id = self.guild_id
        log_prefix = f"[{guild_id}] Cleanup:"
        logger.info(f"{log_prefix} Starting cleanup.")

        # Stop playback and clear queue first
        await self.stop_playback()

        # Cancel the loop task properly
        if self._playback_task and not self._playback_task.done():
            logger.info(f"{log_prefix} Cancelling playback loop task.")
            self._playback_task.cancel()
            try:
                await self._playback_task # Allow cancellation to process
            except asyncio.CancelledError:
                logger.debug(f"{log_prefix} Playback task cancelled successfully.")
            except Exception as e:
                logger.error(f"{log_prefix} Error awaiting cancelled playback task: {e}", exc_info=True)
        # Ensure task reference is cleared even if await failed
        self._playback_task = None

        # Disconnect voice client
        vc = self.voice_client
        if vc and vc.is_connected():
            logger.info(f"{log_prefix} Disconnecting voice client.")
            try:
                await vc.disconnect(force=True)
                logger.info(f"{log_prefix} Voice client disconnected.")
            except Exception as e:
                 logger.error(f"{log_prefix} Error disconnecting voice client: {e}", exc_info=True)
        self.voice_client = None
        self.current_song = None
        logger.info(f"{log_prefix} Cleanup finished.")
        # State removal from parent dict should happen where cleanup is called from (e.g., leave command, listener)


    async def cleanup_from_inactivity(self):
        """Cleanup specifically triggered by inactivity timeout."""
        # Note: This is currently disabled in _playback_loop for debugging
        logger.info(f"[{self.guild_id}] Initiating cleanup due to inactivity.")
        await self.cleanup()
        # Remove state from parent cog dictionary after cleanup
        if self.guild_id in self.bot.get_cog("Music").guild_states:
            del self.bot.get_cog("Music").guild_states[self.guild_id]
            logger.info(f"[{self.guild_id}] Music state removed after inactivity cleanup.")


    async def _notify_channel_error(self, message: str):
        """Helper to try and send error messages to a relevant channel."""
        # Placeholder - Needs a way to store/retrieve the last command's channel context
        logger.warning(f"[{self.guild_id}] Channel Error Notification (Not Sent): {message}")


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

    # --- _extract_song_info (Keep the detailed logging version from previous steps) ---
    async def _extract_song_info(self, query: str) -> dict | None:
        """Extracts song info using yt-dlp in an executor."""
        log_prefix = f"[{self.bot.user.id or 'Bot'}] YTDL:"
        logger.debug(f"{log_prefix} Attempting to extract info for query: '{query}'")
        try:
            loop = asyncio.get_event_loop()
            partial = functools.partial(self.ydl.extract_info, query, download=False, process=False)
            data = await loop.run_in_executor(None, partial)
            logger.debug(f"{log_prefix} Raw YTDL data (first 500 chars): {str(data)[:500]}")

            if not data:
                logger.warning(f"{log_prefix} yt-dlp returned no data for query: {query}")
                return None

            if 'entries' in data:
                entry = data['entries'][0]
                logger.debug(f"{log_prefix} Query was a playlist, using first entry: {entry.get('title', 'N/A')}")
            else:
                entry = data

            if not entry:
                 logger.warning(f"{log_prefix} Could not find a valid entry in yt-dlp data for: {query}")
                 return None

            logger.debug(f"{log_prefix} Processing IE result for entry...")
            try:
                if entry:
                    processed_entry = self.ydl.process_ie_result(entry, download=False)
                    logger.debug(f"{log_prefix} Processed entry keys: {processed_entry.keys() if processed_entry else 'None'}")
                else:
                    logger.warning(f"{log_prefix} Entry was None before processing IE result.")
                    processed_entry = None
            except Exception as process_err:
                logger.error(f"{log_prefix} Error processing IE result: {process_err}", exc_info=True)
                processed_entry = entry

            if not processed_entry:
                 logger.error(f"{log_prefix} Processed entry became None. Cannot proceed.")
                 return None

            audio_url = None
            logger.debug(f"{log_prefix} Checking 'url' in processed_entry: {processed_entry.get('url')}")
            if 'url' in processed_entry:
                audio_url = processed_entry['url']
            elif 'formats' in processed_entry:
                 formats = processed_entry.get('formats', [])
                 logger.debug(f"{log_prefix} 'url' not found directly, checking {len(formats)} formats...")
                 formats_to_log = min(len(formats), 5)
                 for i in range(formats_to_log):
                     f = formats[i]
                     logger.debug(f"  Format {i}: acodec={f.get('acodec')}, vcodec={f.get('vcodec')}, url_present={bool(f.get('url'))}, format_note={f.get('format_note')}, protocol={f.get('protocol')}")

                 for f in formats:
                    if f.get('acodec') == 'opus' and f.get('vcodec') == 'none' and f.get('url'): audio_url = f.get('url'); break
                 if not audio_url:
                    for f in formats:
                        if f.get('acodec') == 'vorbis' and f.get('vcodec') == 'none' and f.get('url'): audio_url = f.get('url'); break
                 if not audio_url:
                     for f in formats:
                         if f.get('acodec') == 'aac' and f.get('vcodec') == 'none' and f.get('url'): audio_url = f.get('url'); break
                 if not audio_url:
                     for f in formats:
                          if ('bestaudio' in f.get('format_id', '').lower() or 'bestaudio' in f.get('format_note', '').lower()) and f.get('url'):
                               audio_url = f['url']
                               logger.debug(f"{log_prefix} Found potential bestaudio URL in formats.")
                               break
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
                'duration': processed_entry.get('duration'),
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
            return {'error': 'download'}
        except Exception as e:
            logger.error(f"{log_prefix} Unexpected error extracting info for '{query}': {e}", exc_info=True)
            return {'error': 'extraction'}

    # --- Listener for Voice State Updates (Keep version from previous steps) ---
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: nextcord.Member, before: nextcord.VoiceState, after: nextcord.VoiceState):
        """Handles bot disconnection or empty/joined channels."""
        if not member.guild: return

        guild_id = member.guild.id
        state = self.guild_states.get(guild_id)

        if not state:
            return

        # --- Bot's own state changes ---
        if member.id == self.bot.user.id:
            if before.channel and not after.channel:
                logger.warning(f"[{guild_id}] Bot was disconnected from voice channel {before.channel.name}. Cleaning up music state.")
                await state.cleanup()
                if guild_id in self.guild_states:
                     del self.guild_states[guild_id]
                     logger.info(f"[{guild_id}] Removed music state after bot disconnect.")
            elif before.channel and after.channel and before.channel != after.channel:
                 logger.info(f"[{guild_id}] Bot moved from {before.channel.name} to {after.channel.name}.")
                 if state.voice_client: state.voice_client.channel = after.channel

        # --- Other users' state changes in the bot's channel ---
        elif state.voice_client and state.voice_client.is_connected():
            current_bot_channel = state.voice_client.channel
            if before.channel == current_bot_channel and after.channel != current_bot_channel:
                 logger.debug(f"[{guild_id}] User {member.name} left bot channel {before.channel.name}.")
                 if len(current_bot_channel.members) == 1 and self.bot.user in current_bot_channel.members:
                     logger.info(f"[{guild_id}] Bot is now alone in {current_bot_channel.name}. Pausing playback.")
                     if state.voice_client.is_playing():
                         state.voice_client.pause()
                     # state.start_inactivity_timer() # Optional TODO

            elif before.channel != current_bot_channel and after.channel == current_bot_channel:
                 logger.debug(f"[{guild_id}] User {member.name} joined bot channel {after.channel.name}.")
                 if state.voice_client.is_paused():
                     logger.info(f"[{guild_id}] User joined, resuming paused playback.")
                     state.voice_client.resume()
                 # state.cancel_inactivity_timer() # Optional TODO


    # --- Music Commands ---

    # --- join_command (Keep version from previous steps) ---
    @commands.command(name='join', aliases=['connect', 'j'], help="Connects the bot to your current voice channel.")
    @commands.guild_only()
    async def join_command(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send("You need to be in a voice channel to use this command.")

        channel = ctx.author.voice.channel
        state = self.get_guild_state(ctx.guild.id)

        async with state._lock:
            if state.voice_client and state.voice_client.is_connected():
                if state.voice_client.channel == channel:
                    await ctx.send(f"I'm already connected to {channel.mention}.")
                else:
                    try:
                        await state.voice_client.move_to(channel)
                        await ctx.send(f"Moved to {channel.mention}.")
                        logger.info(f"[{ctx.guild.id}] Moved to voice channel: {channel.name}")
                    except asyncio.TimeoutError:
                        await ctx.send(f"Timed out trying to move to {channel.mention}.")
                    except Exception as e:
                         await ctx.send(f"Error moving channels: {e}")
                         logger.error(f"[{ctx.guild.id}] Error moving VC: {e}", exc_info=True)
            else:
                try:
                    state.voice_client = await channel.connect()
                    await ctx.send(f"Connected to {channel.mention}.")
                    logger.info(f"[{ctx.guild.id}] Connected to voice channel: {channel.name}")
                    state.start_playback_loop()
                except asyncio.TimeoutError:
                    await ctx.send(f"Timed out connecting to {channel.mention}.")
                    logger.warning(f"[{ctx.guild.id}] Timeout connecting to {channel.name}.")
                    if ctx.guild.id in self.guild_states: del self.guild_states[ctx.guild.id]
                except nextcord.errors.ClientException as e:
                     await ctx.send(f"Unable to connect: {e}. Maybe check my permissions?")
                     logger.warning(f"[{ctx.guild.id}] ClientException on connect: {e}")
                     if ctx.guild.id in self.guild_states: del self.guild_states[ctx.guild.id]
                except Exception as e:
                    await ctx.send(f"An error occurred connecting: {e}")
                    logger.error(f"[{ctx.guild.id}] Error connecting to VC: {e}", exc_info=True)
                    if ctx.guild.id in self.guild_states: del self.guild_states[ctx.guild.id]

    # --- leave_command (Keep version from previous steps) ---
    @commands.command(name='leave', aliases=['disconnect', 'dc', 'fuckoff'], help="Disconnects the bot from the voice channel.")
    @commands.guild_only()
    async def leave_command(self, ctx: commands.Context):
        state = self.guild_states.get(ctx.guild.id)

        if not state or not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected to any voice channel.")

        logger.info(f"[{ctx.guild.id}] Leave command initiated by {ctx.author.name}.")
        await state.cleanup()

        if ctx.guild.id in self.guild_states:
            del self.guild_states[ctx.guild.id]
            logger.info(f"[{ctx.guild.id}] Removed music state after leave command.")

        await ctx.message.add_reaction('👋')

    # --- play_command (Use the version with extraction moved out of typing) ---
    @commands.command(name='play', aliases=['p'], help="Plays a song from a URL or search query, or adds it to the queue.")
    @commands.guild_only()
    async def play_command(self, ctx: commands.Context, *, query: str):
        state = self.get_guild_state(ctx.guild.id)
        log_prefix = f"[{ctx.guild.id}] PlayCmd:"
        logger.info(f"{log_prefix} User {ctx.author.name} initiated play with query: {query}")

        # 1. Connection checks
        if not state.voice_client or not state.voice_client.is_connected():
            if ctx.author.voice and ctx.author.voice.channel:
                 logger.info(f"{log_prefix} Bot not connected. Invoking join command for channel {ctx.author.voice.channel.name}.")
                 await ctx.invoke(self.join_command)
                 state = self.get_guild_state(ctx.guild.id)
                 if not state.voice_client or not state.voice_client.is_connected():
                      logger.warning(f"{log_prefix} Failed to join VC after invoking join_command.")
                      return
                 else:
                      logger.info(f"{log_prefix} Successfully joined VC after invoke.")
            else:
                logger.warning(f"{log_prefix} Failed: User not in VC and bot not connected.")
                return await ctx.send("You need to be in a voice channel for me to join.")
        elif ctx.author.voice and ctx.author.voice.channel != state.voice_client.channel:
             logger.warning(f"{log_prefix} Failed: User in different VC ({ctx.author.voice.channel.name}) than bot ({state.voice_client.channel.name}).")
             return await ctx.send(f"You must be in the same voice channel ({state.voice_client.channel.mention}) as me to add songs.")
        elif not ctx.author.voice or ctx.author.voice.channel != state.voice_client.channel:
              logger.warning(f"{log_prefix} Failed: User not in bot's VC.")
              return await ctx.send(f"You need to be in {state.voice_client.channel.mention} to add songs.")
        else:
             logger.info(f"{log_prefix} Bot already connected to {state.voice_client.channel.name}. Proceeding.")

        # --- Extraction Phase ---
        song_info = None
        logger.debug(f"{log_prefix} Entering extraction phase.")
        typing_task = asyncio.create_task(ctx.trigger_typing()) # Start typing

        try:
            logger.debug(f"{log_prefix} Now calling _extract_song_info (outside ctx.typing)...")
            song_info = await self._extract_song_info(query)
            logger.debug(f"{log_prefix} _extract_song_info call finished.")

        except Exception as e:
            logger.error(f"{log_prefix} Exception occurred DURING _extract_song_info call: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred while trying to fetch the song information.")
            typing_task.cancel()
            return
        finally:
            if not typing_task.done():
                 typing_task.cancel()

        # --- Process Extraction Result ---
        logger.debug(f"{log_prefix} Processing result from _extract_song_info. Result: {song_info}")
        if not song_info:
            logger.warning(f"{log_prefix} _extract_song_info returned None or empty for query: {query}")
            return await ctx.send("Could not retrieve valid song information. The URL might be invalid, private, or the service unavailable.")
        if song_info.get('error'):
             error_type = song_info['error']
             logger.warning(f"{log_prefix} _extract_song_info returned error: {error_type} for query: {query}")
             if error_type == 'unsupported': msg = "Sorry, I don't support that URL or service."
             elif error_type == 'unavailable': msg = "That video is unavailable (maybe private or deleted)."
             elif error_type == 'download': msg = "There was an error trying to access the song data (check logs for details)."
             elif error_type == 'age_restricted': msg = "Sorry, I can't play age-restricted content."
             else: msg = "An unknown error occurred while fetching the song."
             return await ctx.send(msg)

        # --- Create Song Object ---
        logger.debug(f"{log_prefix} Creating Song object...")
        try:
            song = Song(
                source_url=song_info.get('source_url'),
                title=song_info.get('title', 'Unknown Title'),
                webpage_url=song_info.get('webpage_url', query),
                duration=song_info.get('duration'),
                requester=ctx.author
            )
            if not song.source_url or not song.title:
                 logger.error(f"{log_prefix} Failed to create valid Song object (missing URL or Title). Info: {song_info}")
                 return await ctx.send("Failed to process song information (missing critical data).")
        except Exception as e:
             logger.error(f"{log_prefix} Error creating Song object: {e}. Info: {song_info}", exc_info=True)
             return await ctx.send("An internal error occurred preparing the song.")

        # --- Add to Queue (Minimal Lock Scope) ---
        logger.debug(f"{log_prefix} Attempting to acquire lock to add Song to queue.")
        added_successfully = False
        song_title_for_embed = song.title
        song_url_for_embed = song.webpage_url
        song_duration_for_embed = song.format_duration()
        requester_name = song.requester.display_name
        requester_icon = song.requester.display_avatar.url
        queue_pos = 0

        async with state._lock:
            logger.debug(f"{log_prefix} Lock acquired. Adding to queue.")
            state.queue.append(song)
            queue_pos = len(state.queue)
            added_successfully = True
            logger.info(f"{log_prefix} Added '{song_title_for_embed}' to queue at position {queue_pos}. Queue size now: {len(state.queue)}")
            # --- LOCK RELEASED HERE ---
        logger.debug(f"{log_prefix} Lock released.")

        # --- Send Feedback Message (Outside Lock) ---
        if added_successfully:
            logger.debug(f"{log_prefix} Preparing 'Added to Queue' embed.")
            try:
                is_now_playing = (not state.current_song and queue_pos == 1)
                embed = nextcord.Embed(
                    title="Now Playing" if is_now_playing else "Added to Queue",
                    description=f"[{song_title_for_embed}]({song_url_for_embed})",
                    color=nextcord.Color.green()
                )
                embed.add_field(name="Duration", value=song_duration_for_embed, inline=True)
                if not is_now_playing:
                    embed.add_field(name="Position", value=f"#{queue_pos}", inline=True)
                embed.set_footer(text=f"Requested by {requester_name}", icon_url=requester_icon)

                await ctx.send(embed=embed)
                logger.debug(f"{log_prefix} Sent 'Added to Queue' embed.")
            except nextcord.HTTPException as e:
                 logger.error(f"{log_prefix} Failed to send 'Added to Queue' message: {e}")
            except Exception as e:
                 logger.error(f"{log_prefix} Unexpected error sending embed: {e}", exc_info=True)
        else:
             logger.error(f"{log_prefix} Failed to add song to queue (logic error?).")

        # --- Ensure loop starts ---
        if added_successfully:
            logger.debug(f"{log_prefix} Ensuring playback loop is started.")
            state.start_playback_loop()
            logger.debug(f"{log_prefix} play_command finished successfully.")
        else:
             logger.warning(f"{log_prefix} play_command finished WITHOUT adding song.")


    # --- skip_command (Keep version from previous steps) ---
    @commands.command(name='skip', aliases=['s'], help="Skips the currently playing song.")
    @commands.guild_only()
    async def skip_command(self, ctx: commands.Context):
        state = self.guild_states.get(ctx.guild.id)

        if not state or not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected to a voice channel.")
        if not state.current_song:
            return await ctx.send("There's nothing playing to skip.")

        logger.info(f"[{ctx.guild.id}] Skip requested by {ctx.author.name} for '{state.current_song.title}'.")
        state.voice_client.stop()
        await ctx.message.add_reaction('⏭️')

    # --- stop_command (Keep version from previous steps) ---
    @commands.command(name='stop', help="Stops playback completely and clears the queue.")
    @commands.guild_only()
    async def stop_command(self, ctx: commands.Context):
        state = self.guild_states.get(ctx.guild.id)

        if not state or not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected or not playing anything.")

        logger.info(f"[{ctx.guild.id}] Stop requested by {ctx.author.name}.")
        await state.stop_playback()
        await ctx.send("Playback stopped and queue cleared.")
        await ctx.message.add_reaction('⏹️')

    # --- pause_command (Keep version from previous steps) ---
    @commands.command(name='pause', help="Pauses the currently playing song.")
    @commands.guild_only()
    async def pause_command(self, ctx: commands.Context):
        state = self.guild_states.get(ctx.guild.id)

        if not state or not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected.")
        if not state.voice_client.is_playing():
             if state.voice_client.is_paused():
                  return await ctx.send("Playback is already paused.")
             else:
                  return await ctx.send("Nothing is actively playing to pause.")
        state.voice_client.pause()
        logger.info(f"[{ctx.guild.id}] Playback paused by {ctx.author.name}.")
        await ctx.message.add_reaction('⏸️')

    # --- resume_command (Keep version from previous steps) ---
    @commands.command(name='resume', aliases=['unpause'], help="Resumes a paused song.")
    @commands.guild_only()
    async def resume_command(self, ctx: commands.Context):
        state = self.guild_states.get(ctx.guild.id)

        if not state or not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected.")
        if not state.voice_client.is_paused():
            if state.voice_client.is_playing():
                 return await ctx.send("Playback is already playing.")
            else:
                 return await ctx.send("Nothing is currently paused.")
        state.voice_client.resume()
        logger.info(f"[{ctx.guild.id}] Playback resumed by {ctx.author.name}.")
        await ctx.message.add_reaction('▶️')

    # --- queue_command (Keep version from previous steps) ---
    @commands.command(name='queue', aliases=['q', 'playlist'], help="Shows the current song queue.")
    @commands.guild_only()
    async def queue_command(self, ctx: commands.Context):
        state = self.guild_states.get(ctx.guild.id)

        if not state:
             return await ctx.send("I haven't played anything in this server yet.")

        async with state._lock:
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
                max_display = 10
                total_queue_duration = 0
                queue_copy = list(state.queue)
                for i, song in enumerate(queue_copy[:max_display]):
                     queue_list.append(f"`{i+1}.` [{song.title}]({song.webpage_url}) `[{song.format_duration()}]` - Req by {song.requester.display_name}")
                     if song.duration:
                         try: total_queue_duration += int(song.duration)
                         except (ValueError, TypeError): pass # Ignore if duration invalid

                if len(queue_copy) > max_display:
                    queue_list.append(f"\n...and {len(queue_copy) - max_display} more.")
                    for song in queue_copy[max_display:]:
                         if song.duration:
                             try: total_queue_duration += int(song.duration)
                             except (ValueError, TypeError): pass

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

    # --- volume_command (Keep version from previous steps) ---
    @commands.command(name='volume', aliases=['vol'], help="Changes the player volume (0-100).")
    @commands.guild_only()
    async def volume_command(self, ctx: commands.Context, *, volume: int):
        state = self.guild_states.get(ctx.guild.id)

        if not state or not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected to a voice channel.")

        if not 0 <= volume <= 100:
            return await ctx.send("Volume must be between 0 and 100.")

        new_volume = volume / 100.0
        state.volume = new_volume
        logger.debug(f"[{ctx.guild.id}] State volume set to {new_volume}")

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

        if isinstance(error, commands.CheckFailure):
             if isinstance(error, commands.GuildOnly):
                  logger.warning(f"{log_prefix} GuildOnly command '{ctx.command.name}' used in DM by {ctx.author.name}.")
                  return
             logger.warning(f"{log_prefix} Check failed for '{ctx.command.name}' by {ctx.author.name}: {error}")
             return

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
                await ctx.send(f"Voice Error: {original}")
            elif isinstance(original, yt_dlp.utils.DownloadError):
                 await ctx.send("Error fetching song data (maybe unavailable or network issue?).")
            else:
                await ctx.send(f"An internal error occurred while running `{ctx.command.name}`.")
        else:
            logger.warning(f"{log_prefix} Unhandled error type in cog_command_error for '{ctx.command.name}': {type(error).__name__}: {error}")
            # Optional: Re-raise for global handler
            # raise error


# --- setup function (Keep the manual opus load version) ---
def setup(bot: commands.Bot):
    """Adds the MusicCog to the bot."""
    OPUS_PATH = '/usr/lib/x86_64-linux-gnu/libopus.so.0' # Confirmed path

    try:
        if not nextcord.opus.is_loaded():
            logger.info(f"Opus not auto-loaded. Attempting manual load from: {OPUS_PATH}")
            nextcord.opus.load_opus(OPUS_PATH)
            if nextcord.opus.is_loaded():
                 logger.info("Opus manually loaded successfully.")
            else:
                 logger.critical("Manual Opus load attempt finished, but is_loaded() is still false.")
        else:
            logger.info("Opus library was already loaded automatically.")

    except nextcord.opus.OpusNotLoaded as e:
        logger.critical(f"CRITICAL: Manual Opus load failed using path '{OPUS_PATH}'. Error: {e}. "
                        "Ensure the path is correct and the library file is valid and has correct permissions inside the container.")
    except Exception as e:
         logger.critical(f"CRITICAL: An unexpected error occurred during manual Opus load attempt: {e}", exc_info=True)

    bot.add_cog(MusicCog(bot))
    logger.info("MusicCog added to bot.")

# --- Ensure no other code follows this function in the file ---