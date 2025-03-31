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
FFMPEG_BEFORE_OPTIONS = '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
FFMPEG_OPTIONS = '-vn'

# --- YTDL Options ---
# format: bestaudio/best -> Prefers audio-only, falls back to best quality stream
# noplaylist: False -> ALLOW processing playlist URLs
# ignoreerrors: True -> Skip unavailable videos in playlists instead of failing
# extract_flat: 'in_playlist' -> Faster initial playlist parsing
# default_search: auto -> Allows searching YouTube if input isn't a URL
# quiet: True -> Suppress console output from yt-dlp
# no_warnings: True -> Suppress warnings
# source_address: 0.0.0.0 -> Helps with potential IP binding issues (IPv4)
# force_generic_extractor: True -> Can help with some URL types like YT Music
YDL_OPTS = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': False,  # <<< ALLOW PLAYLISTS
    'nocheckcertificate': True,
    'ignoreerrors': True,  # <<< SKIP ERRORS IN PLAYLISTS
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'extract_flat': 'in_playlist', # <<< FASTER PLAYLIST EXTRACTION
    'force_generic_extractor': True, # <<< HELPS WITH YT MUSIC LINKS
}

# Configure Logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG) # Set to INFO or WARNING for less verbose logs

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
             return "N/A"

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
        # Store the channel ID where the last command was invoked for error reporting
        self.last_command_channel_id: int | None = None

    async def _playback_loop(self):
        """The main loop that plays songs from the queue."""
        await self.bot.wait_until_ready()
        logger.info(f"[{self.guild_id}] Playback loop starting.")

        while True:
            self.play_next_song.clear()
            log_prefix = f"[{self.guild_id}] Loop:"
            logger.debug(f"{log_prefix} Top of loop, play_next_song cleared.")
            song_to_play = None

            async with self._lock:
                logger.debug(f"{log_prefix} Lock acquired for queue check.")
                if self.queue:
                    song_to_play = self.queue.popleft()
                    self.current_song = song_to_play
                    logger.info(f"{log_prefix} Popped '{song_to_play.title}'. Queue size: {len(self.queue)}")
                else:
                     logger.debug(f"{log_prefix} Queue is empty.")
                     self.current_song = None
                # LOCK RELEASED HERE
            logger.debug(f"{log_prefix} Lock released after queue check.")


            if not song_to_play:
                logger.info(f"{log_prefix} Queue empty, pausing loop. Waiting for play_next_song event...")
                # --- Consider adding inactivity timeout here ---
                await self.play_next_song.wait() # Wait indefinitely until a song is added or skip/stop happens
                logger.info(f"{log_prefix} play_next_song event received while paused. Continuing loop.")
                continue # Re-check queue

            # --- Check Voice Client ---
            logger.debug(f"{log_prefix} Checking voice client state.")
            if not self.voice_client or not self.voice_client.is_connected():
                logger.warning(f"{log_prefix} Voice client disconnected unexpectedly. Stopping loop.")
                # Put song back if popped but VC died
                logger.warning(f"{log_prefix} Putting '{song_to_play.title}' back in queue.")
                async with self._lock: self.queue.appendleft(song_to_play)
                self.current_song = None # Ensure current song is cleared
                # Trigger cleanup through the task handler
                if self._playback_task and not self._playback_task.done():
                     self._playback_task.cancel() # Will trigger cleanup in _handle_loop_completion
                return # Exit loop


            # --- Play Song ---
            logger.info(f"{log_prefix} Attempting to play: {song_to_play.title}")
            source = None
            try:
                logger.debug(f"{log_prefix} Creating FFmpegPCMAudio source for URL snippet: {song_to_play.source_url[:50]}...")
                # Ensure source_url is valid before creating source
                if not song_to_play.source_url or not isinstance(song_to_play.source_url, str):
                     raise ValueError(f"Invalid source_url for song '{song_to_play.title}': {song_to_play.source_url}")

                original_source = nextcord.FFmpegPCMAudio(
                    song_to_play.source_url,
                    before_options=FFMPEG_BEFORE_OPTIONS,
                    options=FFMPEG_OPTIONS,
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

            except (nextcord.errors.ClientException, ValueError) as e: # Added ValueError for invalid source_url
                logger.error(f"{log_prefix} Client/Value Exception playing {song_to_play.title}: {e}", exc_info=True)
                await self._notify_channel_error(f"Error preparing to play '{song_to_play.title}': {e}")
                self.play_next_song.set() # Signal to continue loop (skip this song)
            except Exception as e:
                logger.error(f"{log_prefix} Unexpected error during playback of {song_to_play.title}: {e}", exc_info=True)
                await self._notify_channel_error(f"Unexpected error playing '{song_to_play.title}'. Please check logs.")
                self.play_next_song.set() # Signal to continue loop (skip this song)
            finally:
                logger.debug(f"{log_prefix} Playback block for '{song_to_play.title}' finished.")
                # current_song is cleared at the start of the next iteration if queue becomes empty

    def _handle_after_play(self, error):
        """Callback function run after a song finishes or errors."""
        log_prefix = f"[{self.guild_id}] After Play Callback: "
        if error:
            logger.error(f"{log_prefix}Playback error encountered: {error!r}", exc_info=error)
            # Try notifying the channel about the playback error
            asyncio.run_coroutine_threadsafe(
                self._notify_channel_error(f"Error during playback: {error}. Skipping to next song if available."),
                self.bot.loop
            )
        else:
            logger.debug(f"{log_prefix}Song finished playing successfully.")

        # Signal the playback loop that it can proceed.
        logger.debug(f"{log_prefix}Setting play_next_song event.")
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
        # Check queue length directly here, current_song might still be set briefly after finishing
        if self.queue and not self.play_next_song.is_set():
             logger.debug(f"[{self.guild_id}] start_playback_loop: Setting play_next_song event as queue is not empty.")
             self.play_next_song.set()

    def _handle_loop_completion(self, task: asyncio.Task):
        """Callback for when the playback loop task finishes (error or natural exit)."""
        guild_id = self.guild_id
        log_prefix = f"[{guild_id}] LoopComplete:"
        try:
            # Check if the task raised an exception
            if task.cancelled():
                 logger.info(f"{log_prefix} Playback loop task was cancelled.")
                 # Cancellation often implies cleanup is needed/intended
                 # Ensure cleanup happens if VC is still connected
                 if self.voice_client and self.voice_client.is_connected():
                     logger.info(f"{log_prefix} Loop cancelled, initiating cleanup.")
                     # Use create_task to avoid blocking the callback handler
                     self.bot.loop.create_task(self.cleanup())
                 else:
                     logger.info(f"{log_prefix} Loop cancelled, VC already disconnected or cleanup underway.")

            elif task.exception():
                exc = task.exception()
                logger.error(f"{log_prefix} Playback loop task exited with error:", exc_info=exc)
                # Attempt to notify channel about the loop failure
                asyncio.run_coroutine_threadsafe(
                    self._notify_channel_error(f"The music playback loop encountered an unexpected error: {exc}. Please try playing again."),
                    self.bot.loop
                )
                # Initiate cleanup after loop error
                self.bot.loop.create_task(self.cleanup())

            else:
                logger.info(f"{log_prefix} Playback loop task finished gracefully (e.g., stopped/cleaned up).")
                # Ensure state is removed if cleanup didn't handle it (should be rare)
                if guild_id in self.bot.get_cog("Music").guild_states and (not self.voice_client or not self.voice_client.is_connected()):
                     logger.warning(f"{log_prefix} Loop ended gracefully but state still exists with no VC. Removing state.")
                     del self.bot.get_cog("Music").guild_states[guild_id]

        except asyncio.CancelledError:
             logger.info(f"{log_prefix} _handle_loop_completion itself was cancelled.")
        except Exception as e:
             logger.error(f"{log_prefix} Error in _handle_loop_completion: {e}", exc_info=True)

        # Reset task variable ONLY IF cleanup hasn't removed the state object entirely
        # This allows commands to restart the loop if needed
        if guild_id in self.bot.get_cog("Music").guild_states:
            self._playback_task = None
            logger.debug(f"{log_prefix} Playback task reference cleared.")
        else:
             logger.debug(f"{log_prefix} State was removed, not clearing task reference (object may be gone).")


    async def stop_playback(self):
        """Stops playback and clears the queue."""
        async with self._lock:
            self.queue.clear()
            vc = self.voice_client
            if vc and vc.is_playing():
                logger.info(f"[{self.guild_id}] Stopping currently playing track via stop_playback.")
                vc.stop() # This will trigger the 'after' callback which sets play_next_song
            self.current_song = None
            logger.info(f"[{self.guild_id}] Queue cleared by stop_playback.")
            # If the loop is waiting, wake it up so it sees the empty queue and pauses
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

        # Cancel the loop task properly if it's still running
        if self._playback_task and not self._playback_task.done():
            logger.info(f"{log_prefix} Cancelling playback loop task.")
            self._playback_task.cancel()
            try:
                # Give the task a moment to process cancellation
                await asyncio.wait_for(self._playback_task, timeout=5.0)
            except asyncio.CancelledError:
                logger.debug(f"{log_prefix} Playback task cancelled successfully during cleanup await.")
            except asyncio.TimeoutError:
                 logger.warning(f"{log_prefix} Timeout waiting for playback task cancellation.")
            except Exception as e:
                logger.error(f"{log_prefix} Error awaiting cancelled playback task: {e}", exc_info=True)
        self._playback_task = None # Ensure reference is cleared

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
        self.current_song = None # Should be cleared by stop_playback, but ensure it here
        logger.info(f"{log_prefix} Cleanup finished.")

        # State removal from parent dict happens where cleanup is called from
        # (e.g., leave command, listener, loop completion handler)


    async def _notify_channel_error(self, message: str):
        """Helper to send error messages to the last known command channel."""
        if not self.last_command_channel_id:
            logger.warning(f"[{self.guild_id}] Cannot send error, no last_command_channel_id stored.")
            return

        try:
            channel = self.bot.get_channel(self.last_command_channel_id)
            if channel and isinstance(channel, nextcord.abc.Messageable):
                 await channel.send(f"⚠️ Music Error: {message}")
                 logger.debug(f"[{self.guild_id}] Sent error notification to channel {self.last_command_channel_id}")
            else:
                 logger.warning(f"[{self.guild_id}] Could not find or send to channel {self.last_command_channel_id}.")
        except nextcord.HTTPException as e:
             logger.error(f"[{self.guild_id}] Failed to send error notification to channel {self.last_command_channel_id}: {e}")
        except Exception as e:
             logger.error(f"[{self.guild_id}] Unexpected error sending error notification: {e}", exc_info=True)


class MusicCog(commands.Cog, name="Music"):
    """Commands for playing music in voice channels."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_states: dict[int, GuildMusicState] = {} # Guild ID -> State
        # Create one YTDL instance per cog, reusing options
        self.ydl = yt_dlp.YoutubeDL(YDL_OPTS)

    def get_guild_state(self, guild_id: int) -> GuildMusicState:
        """Gets or creates the music state for a guild."""
        if guild_id not in self.guild_states:
            logger.debug(f"Creating new GuildMusicState for guild {guild_id}")
            self.guild_states[guild_id] = GuildMusicState(self.bot, guild_id)
        return self.guild_states[guild_id]

    # --- NEW Helper Function to Process a Single Entry ---
    async def _process_entry(self, entry_data: dict, requester: nextcord.Member) -> Song | None:
        """Processes a single yt-dlp entry dictionary into a Song object."""
        log_prefix = f"[{self.bot.user.id or 'Bot'}] EntryProc:"
        if not entry_data:
            logger.warning(f"{log_prefix} Received empty entry data.")
            return None

        entry_title_for_logs = entry_data.get('title', entry_data.get('id', 'N/A'))

        # If using extract_flat, we might need to re-extract full info
        # Check if it has a URL but lacks format info, indicating it's likely flat
        if entry_data.get('_type') == 'url' and 'url' in entry_data and 'formats' not in entry_data:
            try:
                logger.debug(f"{log_prefix} Flat entry detected, re-extracting full info for: {entry_title_for_logs}")
                loop = asyncio.get_event_loop()
                # Use temporary YDL instance with NO playlist processing for this single URL
                # Use a copy of base opts but ensure noplaylist is True
                single_opts = YDL_OPTS.copy()
                single_opts['noplaylist'] = True
                single_opts['extract_flat'] = False # Ensure we get formats
                single_ydl = yt_dlp.YoutubeDL(single_opts)

                # Extract full info, process=True is default and needed here
                partial = functools.partial(single_ydl.extract_info, entry_data['url'], download=False)
                full_entry_data = await loop.run_in_executor(None, partial)

                if not full_entry_data:
                    logger.warning(f"{log_prefix} Re-extraction returned no data for {entry_data['url']}")
                    return None

                # Use the newly extracted full data
                entry_data = full_entry_data
                entry_title_for_logs = entry_data.get('title', entry_data.get('id', 'N/A')) # Update title for logs
                logger.debug(f"{log_prefix} Re-extraction successful for {entry_title_for_logs}")

            except yt_dlp.utils.DownloadError as e:
                 logger.warning(f"{log_prefix} DownloadError re-extracting flat entry {entry_data.get('url')}: {e}. Skipping.")
                 return None # Skip this entry
            except Exception as e:
                logger.error(f"{log_prefix} Error re-extracting flat entry {entry_data.get('url')}: {e}", exc_info=True)
                return None # Skip this entry

        # --- Now process the (potentially re-extracted) entry data ---
        logger.debug(f"{log_prefix} Processing entry: {entry_title_for_logs}")

        # --- Find Audio URL ---
        audio_url = None

        # yt-dlp might have already processed and selected the best format URL
        if 'url' in entry_data and ('formats' not in entry_data or entry_data.get('acodec') != 'none'):
            audio_url = entry_data['url']
            logger.debug(f"{log_prefix} Found 'url' likely pre-selected by yt-dlp: {audio_url[:50]}...")
        elif 'formats' in entry_data:
             formats = entry_data.get('formats', [])
             logger.debug(f"{log_prefix} Found {len(formats)} formats, searching for best audio...")

             best_audio_format = None
             # Prioritize opus, vorbis, aac directly if available and valid
             preferred_codecs = ['opus', 'vorbis', 'aac']
             for codec in preferred_codecs:
                 for f in formats:
                     if f.get('acodec') == codec and f.get('vcodec') == 'none' and f.get('url') and f.get('protocol') in ('https', 'http'):
                         best_audio_format = f
                         logger.debug(f"{log_prefix} Found preferred audio format: {codec} via direct check.")
                         break
                 if best_audio_format: break

             # Fallback: look for format notes/ids or just take the first http/https audio stream
             if not best_audio_format:
                 for f in formats:
                      format_id = f.get('format_id', '').lower()
                      format_note = f.get('format_note', '').lower()
                      if ('bestaudio' in format_id or 'bestaudio' in format_note) and f.get('url') and f.get('protocol') in ('https', 'http'):
                           best_audio_format = f
                           logger.debug(f"{log_prefix} Found format matching 'bestaudio' id/note.")
                           break

             # Generic fallback: first format that has audio and a usable URL
             if not best_audio_format:
                  for f in formats:
                      if f.get('acodec') != 'none' and f.get('url') and f.get('protocol') in ('https', 'http'):
                          best_audio_format = f
                          logger.debug(f"{log_prefix} Using first available audio format as fallback.")
                          break

             if best_audio_format:
                 audio_url = best_audio_format.get('url')
                 logger.debug(f"{log_prefix} Selected audio format: ID {best_audio_format.get('format_id')}, Codec {best_audio_format.get('acodec')}, VCodec {best_audio_format.get('vcodec')}")
             else:
                  logger.warning(f"{log_prefix} No suitable audio format found in 'formats' array.")

        # Check requested_formats as a last resort (less common now)
        elif not audio_url:
             requested_formats = entry_data.get('requested_formats')
             if requested_formats and isinstance(requested_formats, list) and len(requested_formats) > 0:
                 # Typically the first one is the best match based on YDL_OPTS
                 first_req_format = requested_formats[0]
                 if first_req_format.get('url') and first_req_format.get('protocol') in ('https', 'http'):
                     audio_url = first_req_format.get('url')
                     logger.debug(f"{log_prefix} Found audio URL in 'requested_formats'.")

        logger.debug(f"{log_prefix} Final audio URL determined: {'Yes' if audio_url else 'No'}")

        if not audio_url:
            logger.warning(f"{log_prefix} Could not extract playable audio URL for: {entry_title_for_logs}. Skipping.")
            return None

        try:
            # Ensure webpage_url is sensible (use original_url if webpage_url is missing)
            webpage_url = entry_data.get('webpage_url') or entry_data.get('original_url', 'N/A')

            song = Song(
                source_url=audio_url,
                title=entry_data.get('title', 'Unknown Title'),
                webpage_url=webpage_url,
                duration=entry_data.get('duration'),
                requester=requester
            )
            logger.debug(f"{log_prefix} Successfully created Song object for: {song.title}")
            return song
        except Exception as e:
            logger.error(f"{log_prefix} Error creating Song object for {entry_title_for_logs}: {e}", exc_info=True)
            return None
    ##################################
# --- Modified Function to Extract Info (Handles Single/Playlist with re-processing for singles) ---
    async def _extract_info(self, query: str, requester: nextcord.Member) -> tuple[str | None, list[Song]]:
        """
        Extracts info using yt-dlp. Handles single videos and playlists.
        For single videos, it ensures full processing for better format selection.
        Returns: (playlist_title_or_error_code, list_of_songs)
        Returns (error_code, []) on major failure or if no valid songs found.
        """
        log_prefix = f"[{self.bot.user.id or 'Bot'}] YTDL:"
        logger.info(f"{log_prefix} Attempting info extraction requested by {requester.name} for query: '{query}'")
        songs = []
        playlist_title = None

        try:
            loop = asyncio.get_event_loop()
            # Initial call with process=False (efficient for playlists with extract_flat)
            partial_no_process = functools.partial(self.ydl.extract_info, query, download=False, process=False)
            data = await loop.run_in_executor(None, partial_no_process)

            if not data:
                logger.warning(f"{log_prefix} yt-dlp returned no data for query: {query}")
                return "err_nodata", [] # Return specific error code

            # --- Check for Playlist ---
            entries_to_process = data.get('entries')
            if entries_to_process: # Checks if 'entries' key exists and is not None/empty
                playlist_title = data.get('title', 'Unknown Playlist')
                logger.info(f"{log_prefix} Playlist detected: '{playlist_title}'. Processing entries...")

                # Handle potential generator (common with process=False)
                if not isinstance(entries_to_process, list):
                    logger.debug(f"{log_prefix} Entries is a generator, iterating...")
                    # Process generator directly
                else:
                    logger.debug(f"{log_prefix} Entries is a list, iterating...")

                processed_count = 0
                original_count = 0

                for entry in entries_to_process:
                    original_count += 1
                    if entry:
                        # Pass the potentially flat entry to _process_entry
                        # _process_entry handles re-extraction if necessary
                        song = await self._process_entry(entry, requester)
                        if song:
                            songs.append(song)
                            processed_count += 1
                            # await asyncio.sleep(0.05) # Optional delay
                    else:
                        logger.warning(f"{log_prefix} Found a null entry in playlist data, skipping.")

                logger.info(f"{log_prefix} Finished processing playlist '{playlist_title}'. Added {processed_count} valid songs out of {original_count} entries.")
                if processed_count == 0 and original_count > 0:
                     logger.warning(f"{log_prefix} No valid songs could be processed from the playlist.")
                     # Playlist title is known, but no songs were added

            # --- Handle Single Video/Search Result (Re-extract with processing) ---
            else:
                logger.info(f"{log_prefix} Single entry detected or search result. Re-extracting with processing enabled...")
                try:
                    # Re-extract *without* process=False to let yt-dlp select formats properly
                    # Use the same YDL instance
                    partial_process = functools.partial(self.ydl.extract_info, query, download=False) # process=True is default
                    processed_data = await loop.run_in_executor(None, partial_process)

                    if not processed_data:
                         logger.warning(f"{log_prefix} Re-extraction for single entry yielded no data.")
                         # Map error based on original data if possible, otherwise generic
                         # (This case is less likely if the first call returned data)
                         return "err_nodata_reextract", []

                    # Now process the *fully processed* data using the same helper
                    song = await self._process_entry(processed_data, requester)
                    if song:
                        songs.append(song)
                        logger.info(f"{log_prefix} Successfully processed single entry: {song.title}")
                    else:
                        logger.warning(f"{log_prefix} Failed to process single entry even after re-extraction.")
                        # If _process_entry failed on processed data, it's likely unplayable/bad format
                        return "err_process_single_failed", []

                except yt_dlp.utils.DownloadError as e_single:
                     # Handle errors during the second (processing) extraction
                     logger.error(f"{log_prefix} YTDL DownloadError during single entry re-extraction for '{query}': {e_single}")
                     err_str = str(e_single).lower()
                     # Map specific errors
                     if "unsupported url" in err_str: error_code = 'unsupported'
                     elif "video unavailable" in err_str: error_code = 'unavailable'
                     elif "private video" in err_str: error_code = 'private'
                     elif "confirm your age" in err_str: error_code = 'age_restricted'
                     elif "unable to download webpage" in err_str: error_code = 'network'
                     else: error_code = 'download_single'
                     return f"err_{error_code}", []
                except Exception as e_single:
                     logger.error(f"{log_prefix} Unexpected error during single entry re-extraction for '{query}': {e_single}", exc_info=True)
                     return "err_extraction_single", []

            # --- Final Return ---
            # Return playlist title (if any) and the list of songs
            # If songs list is empty but playlist_title is set, play_command handles feedback
            return playlist_title, songs

        except yt_dlp.utils.DownloadError as e_initial:
            # Handle errors during the *initial* (no-process) extraction
            logger.error(f"{log_prefix} YTDL DownloadError during initial extraction for '{query}': {e_initial}")
            err_str = str(e_initial).lower()
            # Map specific errors
            if "unsupported url" in err_str: error_code = 'unsupported'
            # ... (add other specific error checks if needed for initial call) ...
            else: error_code = 'download_initial'
            return f"err_{error_code}", []
        except Exception as e_initial:
            logger.error(f"{log_prefix} Unexpected error during initial extraction for '{query}': {e_initial}", exc_info=True)
            return "err_extraction_initial", []

    # --- Listener for Voice State Updates ---
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: nextcord.Member, before: nextcord.VoiceState, after: nextcord.VoiceState):
        if not member.guild: return
        guild_id = member.guild.id
        state = self.guild_states.get(guild_id)
        if not state: return

        # Store the bot's current channel if connected
        bot_vc_channel = state.voice_client.channel if state.voice_client and state.voice_client.is_connected() else None

        # --- Bot's own state changes ---
        if member.id == self.bot.user.id:
            if before.channel and not after.channel:
                logger.warning(f"[{guild_id}] Bot was disconnected from voice channel {before.channel.name}. Cleaning up music state.")
                # Ensure cleanup happens and state is removed
                await state.cleanup()
                if guild_id in self.guild_states:
                     del self.guild_states[guild_id]
                     logger.info(f"[{guild_id}] Removed music state after bot disconnect.")
            elif before.channel and after.channel and before.channel != after.channel:
                 logger.info(f"[{guild_id}] Bot moved from {before.channel.name} to {after.channel.name}.")
                 # Update the channel reference in the voice client if it exists
                 if state.voice_client: state.voice_client.channel = after.channel

        # --- Other users' state changes in the bot's channel ---
        elif bot_vc_channel: # Only care if the bot is actually in a channel
            # User leaves the bot's channel
            if before.channel == bot_vc_channel and after.channel != bot_vc_channel:
                 logger.debug(f"[{guild_id}] User {member.name} left bot channel {bot_vc_channel.name}.")
                 # Check if bot is now alone (only bot member left)
                 if len(bot_vc_channel.members) == 1 and self.bot.user in bot_vc_channel.members:
                     logger.info(f"[{guild_id}] Bot is now alone in {bot_vc_channel.name}. Pausing playback.")
                     if state.voice_client and state.voice_client.is_playing():
                         state.voice_client.pause()
                     # Optional: Start inactivity timer here
                     # state.start_inactivity_timer()

            # User joins the bot's channel
            elif before.channel != bot_vc_channel and after.channel == bot_vc_channel:
                 logger.debug(f"[{guild_id}] User {member.name} joined bot channel {bot_vc_channel.name}.")
                 # If bot was paused due to being alone, resume playback
                 if state.voice_client and state.voice_client.is_paused() and len(bot_vc_channel.members) > 1:
                     logger.info(f"[{guild_id}] User joined, resuming paused playback.")
                     state.voice_client.resume()
                 # Optional: Cancel inactivity timer here
                 # state.cancel_inactivity_timer()

    # --- Music Commands ---

    @commands.command(name='join', aliases=['connect', 'j'], help="Connects the bot to your current voice channel.")
    @commands.guild_only()
    async def join_command(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send("You need to be in a voice channel to use this command.")

        channel = ctx.author.voice.channel
        state = self.get_guild_state(ctx.guild.id)
        state.last_command_channel_id = ctx.channel.id # Store for errors

        async with state._lock: # Ensure operations on voice_client are safe
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
                        logger.warning(f"[{ctx.guild.id}] Timeout moving to {channel.name}.")
                    except Exception as e:
                         await ctx.send(f"Error moving channels: {e}")
                         logger.error(f"[{ctx.guild.id}] Error moving VC: {e}", exc_info=True)
            else:
                try:
                    state.voice_client = await channel.connect()
                    await ctx.send(f"Connected to {channel.mention}.")
                    logger.info(f"[{ctx.guild.id}] Connected to voice channel: {channel.name}")
                    # Start the playback loop once connected
                    state.start_playback_loop()
                except asyncio.TimeoutError:
                    await ctx.send(f"Timed out connecting to {channel.mention}.")
                    logger.warning(f"[{ctx.guild.id}] Timeout connecting to {channel.name}.")
                    # Clean up the potentially partially created state
                    if ctx.guild.id in self.guild_states: del self.guild_states[ctx.guild.id]
                except nextcord.errors.ClientException as e:
                     await ctx.send(f"Unable to connect: {e}. Maybe check my permissions?")
                     logger.warning(f"[{ctx.guild.id}] ClientException on connect: {e}")
                     if ctx.guild.id in self.guild_states: del self.guild_states[ctx.guild.id]
                except Exception as e:
                    await ctx.send(f"An error occurred connecting: {e}")
                    logger.error(f"[{ctx.guild.id}] Error connecting to VC: {e}", exc_info=True)
                    if ctx.guild.id in self.guild_states: del self.guild_states[ctx.guild.id]

    @commands.command(name='leave', aliases=['disconnect', 'dc', 'stopbot'], help="Disconnects the bot and clears the queue.")
    @commands.guild_only()
    async def leave_command(self, ctx: commands.Context):
        state = self.guild_states.get(ctx.guild.id)

        if not state or not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected to any voice channel.")

        logger.info(f"[{ctx.guild.id}] Leave command initiated by {ctx.author.name}.")
        await ctx.message.add_reaction('👋') # React before potential delay from cleanup

        # Perform cleanup
        await state.cleanup()

        # Remove state from the global dictionary *after* cleanup
        if ctx.guild.id in self.guild_states:
            del self.guild_states[ctx.guild.id]
            logger.info(f"[{ctx.guild.id}] Removed music state after leave command.")
        else:
             # This might happen if cleanup itself removed the state due to an error
             logger.info(f"[{ctx.guild.id}] Music state was already removed before leave command finished.")

        # Confirm disconnect (optional, reaction might be enough)
        # await ctx.send("Disconnected and cleared queue.")


    # --- Updated play_command ---
    @commands.command(name='play', aliases=['p'], help="Plays songs from a URL, search query, or playlist.")
    @commands.guild_only()
    async def play_command(self, ctx: commands.Context, *, query: str):
        state = self.get_guild_state(ctx.guild.id)
        state.last_command_channel_id = ctx.channel.id # Store for errors/notifications
        log_prefix = f"[{ctx.guild.id}] PlayCmd:"
        logger.info(f"{log_prefix} User {ctx.author.name} initiated play with query: {query}")

        # --- Connection checks ---
        if not state.voice_client or not state.voice_client.is_connected():
            if ctx.author.voice and ctx.author.voice.channel:
                 logger.info(f"{log_prefix} Bot not connected. Invoking join command for channel {ctx.author.voice.channel.name}.")
                 await ctx.invoke(self.join_command)
                 # Re-get state in case it was created by join_command
                 state = self.get_guild_state(ctx.guild.id)
                 if not state.voice_client or not state.voice_client.is_connected():
                      logger.warning(f"{log_prefix} Failed to join VC after invoking join_command.")
                      # Don't send here, join_command likely sent a message
                      return
                 else:
                      logger.info(f"{log_prefix} Successfully joined VC after invoke.")
                      state.last_command_channel_id = ctx.channel.id # Re-set channel ID after join
            else:
                logger.warning(f"{log_prefix} Failed: User not in VC and bot not connected.")
                return await ctx.send("You need to be in a voice channel for me to join.")
        # Check if user is in the *same* channel as the bot
        elif not ctx.author.voice or ctx.author.voice.channel != state.voice_client.channel:
             logger.warning(f"{log_prefix} Failed: User not in bot's VC ({state.voice_client.channel.name}).")
             return await ctx.send(f"You need to be in {state.voice_client.channel.mention} to add songs.")
        else:
             logger.info(f"{log_prefix} Bot already connected to {state.voice_client.channel.name}. Proceeding.")

        # --- Extraction Phase ---
        playlist_title = None
        songs_to_add = []
        extraction_error_code = None
        logger.debug(f"{log_prefix} Entering extraction phase.")
        typing_task = asyncio.create_task(ctx.trigger_typing())

        try:
            logger.debug(f"{log_prefix} Calling _extract_info...")
            # Pass the requester (ctx.author) to be stored in Song objects
            result = await self._extract_info(query, ctx.author)
            # Check if the first element indicates an error code
            if isinstance(result[0], str) and result[0].startswith("err_"):
                 extraction_error_code = result[0][4:] # Get code without "err_"
                 playlist_title = None
                 songs_to_add = []
                 logger.warning(f"{log_prefix} _extract_info returned error code: {extraction_error_code}")
            else:
                 playlist_title, songs_to_add = result
                 logger.debug(f"{log_prefix} _extract_info finished. Found {len(songs_to_add)} songs. Playlist: {playlist_title}")

        except Exception as e:
            logger.error(f"{log_prefix} Exception occurred DURING _extract_info call: {e}", exc_info=True)
            await ctx.send("An unexpected error occurred while fetching the song/playlist information.")
            extraction_error_code = "internal" # Mark as internal error
        finally:
             # Ensure typing stops
             if typing_task and not typing_task.done():
                  try: typing_task.cancel()
                  except asyncio.CancelledError: pass # Ignore if already cancelled

        # --- Process Extraction Result ---
        if extraction_error_code:
             # Handle specific YTDL errors reported back from _extract_info
             error_map = {
                 'unsupported': "Sorry, I don't support that URL or service.",
                 'unavailable': "That video/playlist seems unavailable (maybe private or deleted).",
                 'private': "That video/playlist is private and I can't access it.",
                 'age_restricted': "Sorry, I can't play age-restricted content.",
                 'network': "I couldn't connect to the source to get the video/playlist details.",
                 'download': "There was an error trying to access the song data.",
                 'extraction': "An error occurred while processing the information.",
                 'internal': "An internal error occurred during information fetching." # Handled above
             }
             error_message = error_map.get(extraction_error_code, "An unknown error occurred while fetching the song/playlist.")
             return await ctx.send(error_message)

        if not songs_to_add:
            # Check if it was a playlist but yielded no songs
            if playlist_title:
                 logger.warning(f"{log_prefix} Playlist '{playlist_title}' processed but no valid/playable songs found.")
                 return await ctx.send(f"Found the playlist '{playlist_title}', but couldn't add any playable songs from it (they might be unavailable, private, or unsupported).")
            else:
                 # Single query/search yielded nothing
                 logger.warning(f"{log_prefix} _extract_info returned no songs for query: {query}")
                 return await ctx.send("Could not find any playable songs for your query. Try a different search or check the link.")

        # --- Add to Queue (Minimal Lock Scope) ---
        added_count = 0
        queue_start_pos = 0 # The queue position where the *first* new song will be

        logger.debug(f"{log_prefix} Attempting to acquire lock to add {len(songs_to_add)} Song(s) to queue.")
        async with state._lock:
            logger.debug(f"{log_prefix} Lock acquired. Adding to queue.")
            # Calculate position before adding
            queue_start_pos = len(state.queue) + (1 if state.current_song else 0)
            if queue_start_pos == 0: queue_start_pos = 1 # Position is 1-based

            state.queue.extend(songs_to_add) # Use extend for multiple items
            added_count = len(songs_to_add)
            logger.info(f"{log_prefix} Added {added_count} songs. Queue size now: {len(state.queue)}")
            # --- LOCK RELEASED HERE ---
        logger.debug(f"{log_prefix} Lock released.")

        # --- Send Feedback Message (Outside Lock) ---
        if added_count > 0:
            logger.debug(f"{log_prefix} Preparing feedback embed.")
            try:
                # Determine if the *first* added song will play immediately
                # This happens if nothing was playing AND the queue was empty before adding
                is_first_song_now_playing = (not state.current_song and queue_start_pos == 1)

                embed = nextcord.Embed(color=nextcord.Color.green())
                first_song = songs_to_add[0] # We know there's at least one

                if playlist_title and added_count > 1:
                    # Playlist Embed
                    embed.title = "Playlist Added"
                    # Try to make playlist title a link if query was a valid URL
                    pl_link = query if query.startswith('http') else None
                    pl_description = f"**[{playlist_title}]({pl_link})**" if pl_link else f"**{playlist_title}**"
                    embed.description = f"Added **{added_count}** songs from playlist {pl_description}"
                    # Show the first song added
                    embed.add_field(
                        name="First Song Queued",
                        value=f"`{queue_start_pos}.` [{first_song.title}]({first_song.webpage_url}) `[{first_song.format_duration()}]`",
                        inline=False
                    )
                    # Mention if the first song is playing now
                    if is_first_song_now_playing:
                         embed.add_field(name="\u200B", value="▶️ Now Playing the first song!", inline=False) # Use zero-width space for spacing

                elif added_count == 1:
                    # Single Song Embed
                    embed.title = "Now Playing" if is_first_song_now_playing else "Added to Queue"
                    embed.description = f"[{first_song.title}]({first_song.webpage_url})"
                    embed.add_field(name="Duration", value=first_song.format_duration(), inline=True)
                    if not is_first_song_now_playing:
                        embed.add_field(name="Position", value=f"#{queue_start_pos}", inline=True)
                else:
                    # Should not happen if added_count > 0, but handle just in case
                     logger.error(f"{log_prefix} Logic error: added_count is {added_count} but trying to send feedback.")
                     return # Avoid sending confusing message

                embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
                await ctx.send(embed=embed)
                logger.debug(f"{log_prefix} Sent feedback embed.")

            except nextcord.HTTPException as e:
                 logger.error(f"{log_prefix} Failed to send feedback message: {e}")
            except Exception as e:
                 logger.error(f"{log_prefix} Unexpected error sending embed: {e}", exc_info=True)
        # No explicit message if added_count is 0, handled earlier


        # --- Ensure loop starts/continues ---
        if added_count > 0:
            logger.debug(f"{log_prefix} Ensuring playback loop is started/signaled.")
            state.start_playback_loop() # Will start if needed, or set event if loop was waiting
            logger.debug(f"{log_prefix} play_command finished successfully.")
        else:
             # Logged earlier if no songs found, just note completion here
             logger.warning(f"{log_prefix} play_command finished WITHOUT adding songs.")


    @commands.command(name='skip', aliases=['s', 'next'], help="Skips the currently playing song.")
    @commands.guild_only()
    async def skip_command(self, ctx: commands.Context):
        state = self.guild_states.get(ctx.guild.id)
        state.last_command_channel_id = ctx.channel.id

        if not state or not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected to a voice channel.")
        if not state.current_song:
            # Check if playing but somehow current_song is None (shouldn't happen often)
            if state.voice_client.is_playing() or state.voice_client.is_paused():
                 logger.warning(f"[{ctx.guild.id}] Skip called while VC playing/paused but current_song is None. Stopping VC.")
                 state.voice_client.stop() # Stop the player directly
                 await ctx.message.add_reaction('⏭️')
                 return
            else:
                 return await ctx.send("There's nothing playing to skip.")
        if not state.voice_client.is_playing() and not state.voice_client.is_paused():
            return await ctx.send("There's nothing playing or paused to skip.")


        logger.info(f"[{ctx.guild.id}] Skip requested by {ctx.author.name} for '{state.current_song.title}'.")
        state.voice_client.stop() # Triggers 'after' callback -> play_next_song.set() -> loop advances
        await ctx.message.add_reaction('⏭️')
        # Optional: Send message confirming skip
        # await ctx.send(f"Skipped **{state.current_song.title}**.")


    @commands.command(name='stop', help="Stops playback completely and clears the queue.")
    @commands.guild_only()
    async def stop_command(self, ctx: commands.Context):
        state = self.guild_states.get(ctx.guild.id)
        state.last_command_channel_id = ctx.channel.id

        if not state or not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected or not playing anything.")
        # Check if actually doing something
        if not state.current_song and not state.queue:
             return await ctx.send("Nothing to stop (queue is empty and nothing is playing).")


        logger.info(f"[{ctx.guild_id}] Stop requested by {ctx.author.name}.")
        await state.stop_playback() # Clears queue and stops player
        await ctx.send("Playback stopped and queue cleared.")
        await ctx.message.add_reaction('⏹️')


    @commands.command(name='pause', help="Pauses the currently playing song.")
    @commands.guild_only()
    async def pause_command(self, ctx: commands.Context):
        state = self.guild_states.get(ctx.guild.id)
        state.last_command_channel_id = ctx.channel.id

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


    @commands.command(name='resume', aliases=['unpause'], help="Resumes a paused song.")
    @commands.guild_only()
    async def resume_command(self, ctx: commands.Context):
        state = self.guild_states.get(ctx.guild.id)
        state.last_command_channel_id = ctx.channel.id

        if not state or not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected.")
        if not state.voice_client.is_paused():
            if state.voice_client.is_playing():
                 return await ctx.send("Playback is already playing.")
            else: # Not playing, not paused -> nothing to resume
                 return await ctx.send("Nothing is currently paused.")

        state.voice_client.resume()
        logger.info(f"[{ctx.guild.id}] Playback resumed by {ctx.author.name}.")
        await ctx.message.add_reaction('▶️')


    @commands.command(name='queue', aliases=['q', 'nowplaying', 'np'], help="Shows the current song queue.")
    @commands.guild_only()
    async def queue_command(self, ctx: commands.Context):
        state = self.guild_states.get(ctx.guild.id)
        state.last_command_channel_id = ctx.channel.id

        if not state:
             return await ctx.send("I haven't played anything in this server yet.")

        # Use lock for reading queue and current song to ensure consistency
        async with state._lock:
            current_song = state.current_song
            queue_copy = list(state.queue) # Create copy inside lock

        if not current_song and not queue_copy:
            return await ctx.send("The queue is empty and nothing is playing.")

        embed = nextcord.Embed(title="Music Queue", color=nextcord.Color.blurple())
        current_display = "Nothing currently playing."
        total_queue_duration = 0 # Duration of songs *in the queue* only

        # Display Current Song
        if current_song:
            # Determine status based on VC state (check VC exists first)
            status_icon = "❓" # Unknown status if VC disconnected somehow
            if state.voice_client and state.voice_client.is_connected():
                 if state.voice_client.is_playing(): status_icon = "▶️ Playing"
                 elif state.voice_client.is_paused(): status_icon = "⏸️ Paused"
                 else: status_icon = "⏹️ Stopped/Idle" # Should ideally not happen if current_song is set

            current_display = f"{status_icon}: **[{current_song.title}]({current_song.webpage_url})** `[{current_song.format_duration()}]` - Req by {current_song.requester.mention}"
        embed.add_field(name="Now Playing", value=current_display, inline=False)

        # Display Queue
        if queue_copy:
            queue_list_strings = []
            max_display = 10 # Max songs to show details for

            for i, song in enumerate(queue_copy):
                 if i < max_display:
                     queue_list_strings.append(f"`{i+1}.` [{song.title}]({song.webpage_url}) `[{song.format_duration()}]` - Req by {song.requester.display_name}")
                 # Calculate total duration for all songs in queue
                 if song.duration:
                     try: total_queue_duration += int(song.duration)
                     except (ValueError, TypeError): pass # Ignore if duration invalid

            if len(queue_copy) > max_display:
                queue_list_strings.append(f"\n...and {len(queue_copy) - max_display} more.")

            total_dur_str = Song(None,None,None,total_queue_duration,None).format_duration() if total_queue_duration > 0 else "N/A"
            queue_header = f"Up Next ({len(queue_copy)} song{'s' if len(queue_copy) != 1 else ''}, Total Duration: {total_dur_str})"

            embed.add_field(
                name=queue_header,
                value="\n".join(queue_list_strings) or "Queue is empty.", # Fallback message
                inline=False
            )
        else:
             embed.add_field(name="Up Next", value="No songs in queue.", inline=False)

        total_songs_in_system = len(queue_copy) + (1 if current_song else 0)
        embed.set_footer(text=f"Total songs: {total_songs_in_system} | Volume: {int(state.volume * 100)}%")
        await ctx.send(embed=embed)


    @commands.command(name='volume', aliases=['vol'], help="Changes the player volume (0-100).")
    @commands.guild_only()
    async def volume_command(self, ctx: commands.Context, *, volume: int):
        state = self.guild_states.get(ctx.guild.id)
        state.last_command_channel_id = ctx.channel.id

        if not state or not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected to a voice channel.")

        if not 0 <= volume <= 100:
            return await ctx.send("Volume must be between 0 and 100.")

        new_volume_float = volume / 100.0
        state.volume = new_volume_float # Store the volume setting (0.0 to 1.0)
        logger.debug(f"[{ctx.guild.id}] State volume set to {new_volume_float}")

        # Adjust live volume if playing and using PCMVolumeTransformer
        if state.voice_client.source and isinstance(state.voice_client.source, nextcord.PCMVolumeTransformer):
            state.voice_client.source.volume = new_volume_float
            logger.info(f"[{ctx.guild.id}] Volume adjusted live to {volume}% by {ctx.author.name}.")
            await ctx.send(f"Volume changed to **{volume}%**.")
        else:
             # If not playing, or source isn't the volume transformer type,
             # the volume will be applied when the next song starts
             logger.info(f"[{ctx.guild.id}] Volume pre-set to {volume}% by {ctx.author.name} (will apply to next song).")
             await ctx.send(f"Volume set to **{volume}%**. It will apply to the next song played.")


    # --- Error Handling for Music Commands ---
    async def cog_command_error(self, ctx: commands.Context, error):
        """Local error handler specifically for commands in this Cog."""
        log_prefix = f"[{ctx.guild.id if ctx.guild else 'DM'}] MusicCog Error:"
        state = self.guild_states.get(ctx.guild.id) if ctx.guild else None

        # Update last channel ID even on error, if possible
        if state and hasattr(ctx, 'channel'):
             state.last_command_channel_id = ctx.channel.id

        # Simplify handling and log appropriately
        if isinstance(error, commands.CommandNotFound):
            # This cog shouldn't handle CommandNotFound, let the bot handle it
            return # Don't log or respond here

        elif isinstance(error, commands.CheckFailure):
             if isinstance(error, commands.GuildOnly):
                  logger.warning(f"{log_prefix} GuildOnly command '{ctx.command.qualified_name}' used in DM by {ctx.author}.")
                  # No response needed as check prevents command running
                  return
             # Handle other checks if you add them (e.g., permissions)
             logger.warning(f"{log_prefix} Check failed for '{ctx.command.qualified_name}' by {ctx.author}: {error}")
             await ctx.send("You don't have the necessary permissions or context to use this command.")

        elif isinstance(error, commands.MissingRequiredArgument):
             logger.debug(f"{log_prefix} Missing argument for '{ctx.command.qualified_name}': {error.param.name}")
             await ctx.send(f"You missed the `{error.param.name}` argument. Use `?help {ctx.command.qualified_name}` for details.")

        elif isinstance(error, commands.BadArgument):
             logger.debug(f"{log_prefix} Bad argument for '{ctx.command.qualified_name}': {error}")
             await ctx.send(f"Invalid argument provided. Check `?help {ctx.command.qualified_name}`.")

        elif isinstance(error, commands.CommandInvokeError):
            original = error.original
            logger.error(f"{log_prefix} Error invoking command '{ctx.command.qualified_name}': {original.__class__.__name__}: {original}", exc_info=original)
            # Provide user-friendly messages for common underlying errors
            if isinstance(original, nextcord.errors.ClientException):
                await ctx.send(f"Voice Error: {original}")
            elif isinstance(original, yt_dlp.utils.DownloadError):
                 # Should ideally be caught by _extract_info now, but catch here as fallback
                 await ctx.send("Error fetching song/playlist data. It might be unavailable, private, or there could be network issues.")
            elif isinstance(original, asyncio.TimeoutError):
                 await ctx.send("An operation timed out. Please try again.")
            else:
                # Generic error for unexpected issues during command execution
                await ctx.send(f"An internal error occurred while running the `{ctx.command.name}` command. Please check the bot logs or contact the administrator.")
        else:
            # Log any other error types that weren't caught
            logger.error(f"{log_prefix} Unhandled error type in cog_command_error for '{ctx.command.qualified_name}': {type(error).__name__}: {error}", exc_info=error)
            # Optionally re-raise for a global error handler
            # raise error


# --- Setup Function ---
def setup(bot: commands.Bot):
    """Adds the MusicCog to the bot."""
    # --- Opus Loading ---
    # Try loading opus library. If it fails, log critical error.
    try:
        if not nextcord.opus.is_loaded():
            # Try default load first
            try:
                 nextcord.opus.load_opus()
                 logger.info("Opus library loaded successfully (default method).")
            except nextcord.opus.OpusNotLoaded:
                 logger.warning("Default Opus load failed. Trying common paths...")
                 # If default fails, try specific paths (adjust if necessary for your OS/environment)
                 opus_paths = [
                     '/usr/lib/x86_64-linux-gnu/libopus.so.0', # Common Debian/Ubuntu path
                     '/usr/lib/libopus.so.0',                 # Other Linux path
                     'libopus-0.x64.dll',                     # Windows 64-bit
                     'libopus-0.x86.dll',                     # Windows 32-bit
                     'libopus.0.dylib',                       # macOS
                     # Add other potential paths here
                 ]
                 loaded = False
                 for path in opus_paths:
                     try:
                         nextcord.opus.load_opus(path)
                         logger.info(f"Opus manually loaded successfully from: {path}")
                         loaded = True
                         break
                     except nextcord.opus.OpusNotFound:
                         logger.debug(f"Opus not found at path: {path}")
                     except nextcord.opus.OpusLoadError as e:
                         logger.error(f"Error loading Opus from {path}: {e}")
                     except Exception as e:
                          logger.error(f"Unexpected error loading Opus from {path}: {e}")

                 if not loaded:
                      logger.critical("CRITICAL: Failed to load Opus library from any known path. Voice functionality will NOT work.")
                      # You might want to prevent the cog from loading entirely if Opus fails
                      # raise OpusLoadError("Could not load libopus.") # Or similar custom exception
        else:
            logger.info("Opus library was already loaded.")

    except Exception as e:
         # Catch any unexpected errors during the loading process itself
         logger.critical(f"CRITICAL: An unexpected error occurred during Opus loading check: {e}", exc_info=True)

    # --- Add Cog ---
    try:
        bot.add_cog(MusicCog(bot))
        logger.info("MusicCog added to bot successfully.")
    except Exception as e:
         logger.critical(f"CRITICAL: Failed to add MusicCog to bot: {e}", exc_info=True)

# --- End of File ---