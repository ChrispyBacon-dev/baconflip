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
        self.last_command_channel_id: int | None = None # Store for errors/notifications

    # --- MODIFIED Playback Loop ---
    async def _playback_loop(self):
        """The main loop that plays songs from the queue."""
        await self.bot.wait_until_ready()
        logger.info(f"[{self.guild_id}] Playback loop starting.")

        while True:
            self.play_next_song.clear()
            log_prefix = f"[{self.guild_id}] Loop:"
            logger.debug(f"{log_prefix} Top of loop, play_next_song cleared.")
            song_to_play = None
            vc_valid = False # Flag to check if VC is usable

            # --- Check Voice Client State Early ---
            if self.voice_client and self.voice_client.is_connected():
                 vc_valid = True
                 # Also check if it's already playing - helps prevent immediate error later
                 if self.voice_client.is_playing():
                      logger.debug(f"{log_prefix} VC is already playing. Waiting for song end signal.")
                      # Wait here for the signal, otherwise we might pop prematurely
                      await self.play_next_song.wait()
                      logger.debug(f"{log_prefix} play_next_song event received after waiting because VC was playing.")
                      # Loop will continue and re-evaluate state
                      continue
                 elif self.voice_client.is_paused():
                      logger.debug(f"{log_prefix} VC is paused. Waiting for signal.")
                      await self.play_next_song.wait()
                      logger.debug(f"{log_prefix} play_next_song event received while paused.")
                      continue # Re-evaluate state (e.g., resume might have happened)

            elif not self.voice_client or not self.voice_client.is_connected():
                logger.warning(f"{log_prefix} Voice client seems disconnected at top of loop. Checking queue before exiting.")
                # Check if there was a song ready to play - put it back
                async with self._lock:
                     # Check current_song first as it might have been popped but not yet played
                     song_to_put_back = self.current_song
                     if song_to_put_back:
                          logger.warning(f"{log_prefix} Putting current song '{song_to_put_back.title}' back in queue due to VC disconnect.")
                          self.queue.appendleft(song_to_put_back)
                          self.current_song = None
                logger.warning(f"{log_prefix} VC disconnected, stopping loop.")
                # Loop should exit naturally or cleanup should handle it via task completion
                return # Exit loop

            # --- Get Song (if VC is valid) ---
            if vc_valid:
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

            # --- Wait if Queue Empty ---
            if not song_to_play:
                logger.info(f"{log_prefix} Queue empty or VC invalid, pausing loop. Waiting for play_next_song event...")
                await self.play_next_song.wait()
                logger.info(f"{log_prefix} play_next_song event received while queue was empty. Continuing loop.")
                continue # Re-check queue and VC state

            # --- Play Song ---
            logger.info(f"{log_prefix} Attempting to play: {song_to_play.title}")
            source = None
            play_attempted = False
            try:
                # Re-verify VC right before creating source and playing
                if not self.voice_client or not self.voice_client.is_connected():
                     logger.warning(f"{log_prefix} VC disconnected just before creating source for '{song_to_play.title}'.")
                     # Put song back and let loop cycle
                     async with self._lock: self.queue.appendleft(song_to_play); self.current_song = None
                     continue # Go back to top of loop

                if self.voice_client.is_playing():
                     # This safeguard should ideally not be hit due to the check at the top
                     logger.error(f"{log_prefix} CRITICAL RACE CONDITION?: VC is playing just before calling play() for '{song_to_play.title}'. Check loop logic.")
                     # Put song back and wait for the real 'after' callback
                     async with self._lock: self.queue.appendleft(song_to_play); self.current_song = None
                     await self.play_next_song.wait() # Wait for signal from the *actual* playing song
                     continue

                logger.debug(f"{log_prefix} Creating FFmpegPCMAudio source for URL snippet: {song_to_play.source_url[:50]}...")
                if not song_to_play.source_url or not isinstance(song_to_play.source_url, str):
                     raise ValueError(f"Invalid source_url for song '{song_to_play.title}': {song_to_play.source_url}")

                original_source = nextcord.FFmpegPCMAudio(
                    song_to_play.source_url,
                    before_options=FFMPEG_BEFORE_OPTIONS,
                    options=FFMPEG_OPTIONS,
                )
                source = nextcord.PCMVolumeTransformer(original_source, volume=self.volume)
                logger.debug(f"{log_prefix} Source created successfully.")

                # --- THE PLAY CALL ---
                self.voice_client.play(source, after=lambda e: self._handle_after_play(e))
                play_attempted = True
                # ---------------------

                logger.info(f"{log_prefix} voice_client.play() called successfully for {song_to_play.title}")

            except (nextcord.errors.ClientException, ValueError) as e:
                # Error creating source or calling play (e.g., invalid URL, *maybe* already playing if race condition happened)
                logger.error(f"{log_prefix} Client/Value Exception preparing or starting play for {song_to_play.title}: {e}", exc_info=True)
                await self._notify_channel_error(f"Error preparing to play '{song_to_play.title}': {e}. Skipping.")
                # CRITICAL: DO NOT set play_next_song here. Let loop cycle or wait for correct 'after'.
                self.current_song = None # Clear current song as it failed to start
                play_attempted = False # Ensure we don't wait below
                # Continue to top of loop without waiting
            except Exception as e:
                logger.error(f"{log_prefix} Unexpected error during playback preparation of {song_to_play.title}: {e}", exc_info=True)
                await self._notify_channel_error(f"Unexpected error preparing '{song_to_play.title}'. Skipping.")
                # CRITICAL: DO NOT set play_next_song here.
                self.current_song = None # Clear current song as it failed to start
                play_attempted = False # Ensure we don't wait below
                # Continue to top of loop without waiting

            # --- Wait for song to finish ONLY if play was successfully initiated ---
            if play_attempted:
                 logger.debug(f"{log_prefix} Waiting for play_next_song event (song completion/skip)...")
                 await self.play_next_song.wait()
                 logger.debug(f"{log_prefix} play_next_song event received after playback attempt for '{song_to_play.title}'.")
            else:
                 # If play wasn't attempted (e.g., error before play call), don't wait here.
                 # Loop continues immediately to the top to re-evaluate state.
                 logger.debug(f"{log_prefix} Play not attempted for '{song_to_play.title}', loop continues without waiting.")
                 # Add a small sleep to prevent potential tight loop if errors happen consecutively
                 await asyncio.sleep(0.1)


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
        if self.queue and not self.play_next_song.is_set():
             # Check if VC is NOT playing AND loop might be waiting inside wait()
             if self.voice_client and not self.voice_client.is_playing():
                  logger.debug(f"[{self.guild_id}] start_playback_loop: Setting play_next_song event as queue is not empty and VC not playing.")
                  self.play_next_song.set()


    def _handle_loop_completion(self, task: asyncio.Task):
        """Callback for when the playback loop task finishes (error or natural exit)."""
        guild_id = self.guild_id
        log_prefix = f"[{guild_id}] LoopComplete:"
        try:
            if task.cancelled():
                 logger.info(f"{log_prefix} Playback loop task was cancelled.")
                 # Schedule cleanup if cancellation happened unexpectedly while connected
                 if self.voice_client and self.voice_client.is_connected():
                     logger.info(f"{log_prefix} Loop cancelled, scheduling cleanup.")
                     self.bot.loop.create_task(self.cleanup())

            elif task.exception():
                exc = task.exception()
                logger.error(f"{log_prefix} Playback loop task exited with error:", exc_info=exc)
                asyncio.run_coroutine_threadsafe(
                    self._notify_channel_error(f"The music playback loop encountered an unexpected error: {exc}. Please try playing again."),
                    self.bot.loop
                )
                # Schedule cleanup after loop error
                self.bot.loop.create_task(self.cleanup())

            else:
                logger.info(f"{log_prefix} Playback loop task finished gracefully (e.g., via leave/stop).")
                # State removal should happen in the command/event that triggered the stop

        except asyncio.CancelledError:
             logger.info(f"{log_prefix} _handle_loop_completion itself was cancelled.")
        except Exception as e:
             logger.error(f"{log_prefix} Error in _handle_loop_completion: {e}", exc_info=True)

        # Reset task variable only if state still exists (cleanup might remove it)
        if guild_id in self.bot.get_cog("Music").guild_states:
            self._playback_task = None
            logger.debug(f"{log_prefix} Playback task reference cleared.")
        else:
            logger.debug(f"{log_prefix} State was removed, not clearing task reference.")


    async def stop_playback(self):
        """Stops playback and clears the queue."""
        async with self._lock:
            self.queue.clear()
            vc = self.voice_client
            # Stop playback ONLY if connected and actually playing/paused
            if vc and vc.is_connected() and (vc.is_playing() or vc.is_paused()):
                logger.info(f"[{self.guild_id}] Stopping currently playing track via stop_playback.")
                vc.stop() # This will trigger the 'after' callback which sets play_next_song
            self.current_song = None
            logger.info(f"[{self.guild_id}] Queue cleared by stop_playback.")
            # If the loop is waiting, wake it up so it sees the empty queue and pauses/stops
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
        task = self._playback_task
        if task and not task.done():
            logger.info(f"{log_prefix} Cancelling playback loop task.")
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
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
        # self.current_song should be None after stop_playback, ensure it
        self.current_song = None
        logger.info(f"{log_prefix} Cleanup finished.")
        # Actual removal of state from MusicCog.guild_states dictionary should happen
        # in the calling context (e.g., leave command, disconnect listener) after cleanup returns.


    async def _notify_channel_error(self, message: str):
        """Helper to send error messages to the last known command channel."""
        if not self.last_command_channel_id:
            logger.warning(f"[{self.guild_id}] Cannot send error, no last_command_channel_id stored.")
            return

        try:
            channel = self.bot.get_channel(self.last_command_channel_id)
            if channel and isinstance(channel, nextcord.abc.Messageable):
                 # Use embed for better visibility
                 embed = nextcord.Embed(title="Music Bot Error", description=message, color=nextcord.Color.red())
                 await channel.send(embed=embed)
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
        self.ydl = yt_dlp.YoutubeDL(YDL_OPTS)

    def get_guild_state(self, guild_id: int) -> GuildMusicState:
        """Gets or creates the music state for a guild."""
        if guild_id not in self.guild_states:
            logger.debug(f"Creating new GuildMusicState for guild {guild_id}")
            self.guild_states[guild_id] = GuildMusicState(self.bot, guild_id)
        return self.guild_states[guild_id]

    # --- Helper Function to Process a Single Entry ---
    async def _process_entry(self, entry_data: dict, requester: nextcord.Member) -> Song | None:
        """Processes a single yt-dlp entry dictionary into a Song object."""
        log_prefix = f"[{self.bot.user.id or 'Bot'}] EntryProc:"
        if not entry_data:
            logger.warning(f"{log_prefix} Received empty entry data.")
            return None

        entry_title_for_logs = entry_data.get('title', entry_data.get('id', 'N/A'))

        # Re-extraction logic for flat entries remains the same
        if entry_data.get('_type') == 'url' and 'url' in entry_data and 'formats' not in entry_data:
            try:
                logger.debug(f"{log_prefix} Flat entry detected, re-extracting full info for: {entry_title_for_logs}")
                loop = asyncio.get_event_loop()
                single_opts = YDL_OPTS.copy()
                single_opts['noplaylist'] = True
                single_opts['extract_flat'] = False
                # Create a new YDL instance for thread safety if needed, though functools.partial should be okay
                single_ydl = yt_dlp.YoutubeDL(single_opts)
                partial = functools.partial(single_ydl.extract_info, entry_data['url'], download=False)
                full_entry_data = await loop.run_in_executor(None, partial)

                if not full_entry_data:
                    logger.warning(f"{log_prefix} Re-extraction returned no data for {entry_data['url']}")
                    return None
                entry_data = full_entry_data
                entry_title_for_logs = entry_data.get('title', entry_data.get('id', 'N/A'))
                logger.debug(f"{log_prefix} Re-extraction successful for {entry_title_for_logs}")

            except yt_dlp.utils.DownloadError as e:
                 logger.warning(f"{log_prefix} DownloadError re-extracting flat entry {entry_data.get('url')}: {e}. Skipping.")
                 return None
            except Exception as e:
                logger.error(f"{log_prefix} Error re-extracting flat entry {entry_data.get('url')}: {e}", exc_info=True)
                return None

        # --- Format Selection Logic ---
        logger.debug(f"{log_prefix} Processing entry: {entry_title_for_logs}")
        audio_url = None

        # Check if yt-dlp provided a pre-selected URL (common after process=True)
        if 'url' in entry_data and entry_data.get('protocol') in ('http', 'https') and entry_data.get('acodec') != 'none':
            audio_url = entry_data['url']
            logger.debug(f"{log_prefix} Using pre-selected 'url': {audio_url[:50]}...")
        elif 'formats' in entry_data:
             formats = entry_data.get('formats', [])
             logger.debug(f"{log_prefix} Found {len(formats)} formats, searching for best audio...")
             best_audio_format = None
             preferred_codecs = ['opus', 'vorbis', 'aac']

             # Find best preferred codec
             for codec in preferred_codecs:
                 for f in formats:
                     # Ensure it has a URL, correct codec, no video, and http/https protocol
                     if (f.get('url') and f.get('protocol') in ('https', 'http') and
                         f.get('acodec') == codec and f.get('vcodec') == 'none'):
                         # Optional: Could check bitrate here if needed
                         best_audio_format = f
                         logger.debug(f"{log_prefix} Found preferred audio format: {codec} (ID: {f.get('format_id')})")
                         break
                 if best_audio_format: break

             # Fallback: look for format notes/ids matching 'bestaudio'
             if not best_audio_format:
                 for f in formats:
                      format_id = f.get('format_id', '').lower()
                      format_note = f.get('format_note', '').lower()
                      if ((('bestaudio' in format_id or 'bestaudio' in format_note) or format_id == 'bestaudio')
                          and f.get('url') and f.get('protocol') in ('https', 'http') and f.get('acodec') != 'none'):
                           best_audio_format = f
                           logger.debug(f"{log_prefix} Found format matching 'bestaudio' id/note (ID: {f.get('format_id')})")
                           break

             # Generic fallback: first usable http/https audio stream
             if not best_audio_format:
                  for f in formats:
                      if (f.get('url') and f.get('protocol') in ('https', 'http')
                          and f.get('acodec') != 'none' and f.get('vcodec') == 'none'): # Prefer audio only if possible
                          best_audio_format = f
                          logger.debug(f"{log_prefix} Using first available audio-only format as fallback (ID: {f.get('format_id')}).")
                          break
             # Last resort fallback: any http/https audio
             if not best_audio_format:
                  for f in formats:
                      if (f.get('url') and f.get('protocol') in ('https', 'http')
                          and f.get('acodec') != 'none'):
                          best_audio_format = f
                          logger.warning(f"{log_prefix} Using first available audio format (might include video stream) as last resort fallback (ID: {f.get('format_id')}).")
                          break

             if best_audio_format:
                 audio_url = best_audio_format.get('url')
                 logger.debug(f"{log_prefix} Selected audio format: ID {best_audio_format.get('format_id')}, Codec {best_audio_format.get('acodec')}")
             else:
                  logger.warning(f"{log_prefix} No suitable HTTP/HTTPS audio format found in 'formats' array.")

        # Check requested_formats as a last resort (less common)
        elif 'requested_formats' in entry_data and not audio_url:
             requested_formats = entry_data.get('requested_formats')
             if requested_formats:
                 first_req_format = requested_formats[0]
                 if first_req_format.get('url') and first_req_format.get('protocol') in ('https', 'http'):
                     audio_url = first_req_format.get('url')
                     logger.debug(f"{log_prefix} Found usable audio URL in 'requested_formats'.")

        # --- Create Song Object ---
        logger.debug(f"{log_prefix} Final audio URL determined: {'Yes' if audio_url else 'No'}")
        if not audio_url:
            logger.warning(f"{log_prefix} Could not extract playable audio URL for: {entry_title_for_logs}. Skipping.")
            return None

        try:
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


    # --- MODIFIED Function to Extract Info (Handles Single/Playlist with re-processing) ---
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
                logger.warning(f"{log_prefix} yt-dlp returned no data for query: {query} (initial call)")
                return "err_nodata", [] # Return specific error code

            # --- Check for Playlist ---
            entries_to_process = data.get('entries')
            if entries_to_process:
                playlist_title = data.get('title', 'Unknown Playlist')
                logger.info(f"{log_prefix} Playlist detected: '{playlist_title}'. Processing entries...")
                if not isinstance(entries_to_process, list):
                    logger.debug(f"{log_prefix} Entries is a generator, iterating...")
                else:
                    logger.debug(f"{log_prefix} Entries is a list, iterating...")

                processed_count = 0
                original_count = 0
                for entry in entries_to_process:
                    original_count += 1
                    if entry:
                        song = await self._process_entry(entry, requester)
                        if song: songs.append(song); processed_count += 1
                    else: logger.warning(f"{log_prefix} Found a null entry in playlist data, skipping.")

                logger.info(f"{log_prefix} Finished processing playlist '{playlist_title}'. Added {processed_count} valid songs out of {original_count} entries.")
                # If count is 0, playlist_title is still set, play_command will handle feedback

            # --- Handle Single Video/Search Result (Re-extract with processing) ---
            else:
                logger.info(f"{log_prefix} Single entry/search result. Re-extracting with processing enabled...")
                try:
                    # Re-extract *with* processing enabled (default)
                    partial_process = functools.partial(self.ydl.extract_info, query, download=False)
                    processed_data = await loop.run_in_executor(None, partial_process)

                    if not processed_data:
                         logger.warning(f"{log_prefix} Re-extraction for single entry yielded no data.")
                         return "err_nodata_reextract", []

                    song = await self._process_entry(processed_data, requester)
                    if song:
                        songs.append(song)
                        logger.info(f"{log_prefix} Successfully processed single entry: {song.title}")
                    else:
                        logger.warning(f"{log_prefix} Failed to process single entry after re-extraction.")
                        return "err_process_single_failed", []

                except yt_dlp.utils.DownloadError as e_single:
                     logger.error(f"{log_prefix} YTDL DownloadError during single re-extraction for '{query}': {e_single}")
                     # Map specific errors
                     err_str = str(e_single).lower(); code = 'download_single'
                     if "unsupported url" in err_str: code = 'unsupported'
                     elif "video unavailable" in err_str: code = 'unavailable'
                     elif "private video" in err_str: code = 'private'
                     elif "confirm your age" in err_str: code = 'age_restricted'
                     elif "unable to download webpage" in err_str: code = 'network'
                     return f"err_{code}", []
                except Exception as e_single:
                     logger.error(f"{log_prefix} Unexpected error during single re-extraction for '{query}': {e_single}", exc_info=True)
                     return "err_extraction_single", []

            # --- Final Return ---
            return playlist_title, songs

        except yt_dlp.utils.DownloadError as e_initial:
            # Handle errors during the *initial* (no-process) extraction
            logger.error(f"{log_prefix} YTDL DownloadError during initial extraction for '{query}': {e_initial}")
            err_str = str(e_initial).lower(); code = 'download_initial'
            # Map specific errors if needed (less likely to be detailed here)
            if "unsupported url" in err_str: code = 'unsupported'
            elif "unable to download webpage" in err_str: code = 'network'
            # Add more mappings based on observed errors
            return f"err_{code}", []
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

        bot_vc_channel = state.voice_client.channel if state.voice_client and state.voice_client.is_connected() else None

        # Bot's own state changes
        if member.id == self.bot.user.id:
            if before.channel and not after.channel:
                logger.warning(f"[{guild_id}] Bot was disconnected from VC {before.channel.name}. Cleaning up state.")
                await state.cleanup()
                if guild_id in self.guild_states:
                     del self.guild_states[guild_id]
                     logger.info(f"[{guild_id}] Removed music state after bot disconnect.")
            elif before.channel and after.channel and before.channel != after.channel:
                 logger.info(f"[{guild_id}] Bot moved from {before.channel.name} to {after.channel.name}.")
                 if state.voice_client: state.voice_client.channel = after.channel

        # Other users' state changes in the bot's channel
        elif bot_vc_channel:
            # User leaves bot's channel
            if before.channel == bot_vc_channel and after.channel != bot_vc_channel:
                 logger.debug(f"[{guild_id}] User {member.name} left bot channel {bot_vc_channel.name}.")
                 if len(bot_vc_channel.members) == 1 and self.bot.user in bot_vc_channel.members:
                     logger.info(f"[{guild_id}] Bot is now alone in {bot_vc_channel.name}. Pausing playback.")
                     if state.voice_client and state.voice_client.is_playing():
                         state.voice_client.pause()
                     # TODO: Consider adding inactivity timer start here

            # User joins bot's channel
            elif before.channel != bot_vc_channel and after.channel == bot_vc_channel:
                 logger.debug(f"[{guild_id}] User {member.name} joined bot channel {bot_vc_channel.name}.")
                 if state.voice_client and state.voice_client.is_paused() and len(bot_vc_channel.members) > 1:
                     logger.info(f"[{guild_id}] User joined, resuming paused playback.")
                     state.voice_client.resume()
                 # TODO: Consider cancelling inactivity timer here

    # --- Music Commands ---

    @commands.command(name='join', aliases=['connect', 'j'], help="Connects the bot to your current voice channel.")
    @commands.guild_only()
    async def join_command(self, ctx: commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send("You need to be in a voice channel to use this command.")
        if not ctx.guild: return # Should be guaranteed by guild_only, but safety check

        channel = ctx.author.voice.channel
        state = self.get_guild_state(ctx.guild.id)
        state.last_command_channel_id = ctx.channel.id

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
                        logger.warning(f"[{ctx.guild.id}] Timeout moving to {channel.name}.")
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

    @commands.command(name='leave', aliases=['disconnect', 'dc', 'stopbot'], help="Disconnects the bot and clears the queue.")
    @commands.guild_only()
    async def leave_command(self, ctx: commands.Context):
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)

        if not state or not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected to any voice channel.")

        logger.info(f"[{ctx.guild.id}] Leave command initiated by {ctx.author.name}.")
        await ctx.message.add_reaction('👋')

        await state.cleanup()

        if ctx.guild.id in self.guild_states:
            del self.guild_states[ctx.guild.id]
            logger.info(f"[{ctx.guild.id}] Removed music state after leave command.")
        else:
             logger.info(f"[{ctx.guild.id}] Music state was already removed before leave command finished.")


    # --- Updated play_command ---
    @commands.command(name='play', aliases=['p'], help="Plays songs from a URL, search query, or playlist.")
    @commands.guild_only()
    async def play_command(self, ctx: commands.Context, *, query: str):
        if not ctx.guild: return
        state = self.get_guild_state(ctx.guild.id)
        state.last_command_channel_id = ctx.channel.id
        log_prefix = f"[{ctx.guild.id}] PlayCmd:"
        logger.info(f"{log_prefix} User {ctx.author.name} initiated play with query: {query}")

        # Connection checks
        if not state.voice_client or not state.voice_client.is_connected():
            if ctx.author.voice and ctx.author.voice.channel:
                 logger.info(f"{log_prefix} Bot not connected. Invoking join command for channel {ctx.author.voice.channel.name}.")
                 await ctx.invoke(self.join_command)
                 state = self.get_guild_state(ctx.guild.id) # Re-get state
                 if not state.voice_client or not state.voice_client.is_connected():
                      logger.warning(f"{log_prefix} Failed to join VC after invoking join_command.")
                      return # join_command should have sent message
                 else:
                      logger.info(f"{log_prefix} Successfully joined VC after invoke.")
                      state.last_command_channel_id = ctx.channel.id # Re-set channel ID
            else:
                logger.warning(f"{log_prefix} Failed: User not in VC and bot not connected.")
                return await ctx.send("You need to be in a voice channel for me to join.")
        elif not ctx.author.voice or ctx.author.voice.channel != state.voice_client.channel:
             logger.warning(f"{log_prefix} Failed: User not in bot's VC ({state.voice_client.channel.name}).")
             return await ctx.send(f"You need to be in {state.voice_client.channel.mention} to add songs.")
        else:
             logger.info(f"{log_prefix} Bot already connected to {state.voice_client.channel.name}. Proceeding.")

        # Extraction Phase
        playlist_title = None
        songs_to_add = []
        extraction_error_code = None
        logger.debug(f"{log_prefix} Entering extraction phase.")
        typing_task = asyncio.create_task(ctx.trigger_typing())

        try:
            logger.debug(f"{log_prefix} Calling _extract_info...")
            result = await self._extract_info(query, ctx.author)

            if isinstance(result[0], str) and result[0].startswith("err_"):
                 extraction_error_code = result[0][4:]
                 playlist_title = None; songs_to_add = []
                 logger.warning(f"{log_prefix} _extract_info returned error code: {extraction_error_code}")
            else:
                 playlist_title, songs_to_add = result
                 logger.debug(f"{log_prefix} _extract_info finished. Found {len(songs_to_add)} songs. Playlist: {playlist_title}")

        except Exception as e:
            logger.error(f"{log_prefix} Exception during _extract_info call: {e}", exc_info=True)
            extraction_error_code = "internal_extraction"
        finally:
             if typing_task and not typing_task.done():
                  try: typing_task.cancel()
                  except asyncio.CancelledError: pass

        # Process Extraction Result
        if extraction_error_code:
             error_map = {
                 'unsupported': "Sorry, I don't support that URL or service.",
                 'unavailable': "That video/playlist seems unavailable (maybe private or deleted).",
                 'private': "That video/playlist is private and I can't access it.",
                 'age_restricted': "Sorry, I can't play age-restricted content.",
                 'network': "I couldn't connect to the source to get the details.",
                 'download_initial': "Error fetching initial data.",
                 'download_single': "Error fetching data for the single track.",
                 'nodata': "Couldn't find any data for the query.",
                 'nodata_reextract': "Couldn't find data when re-fetching single track info.",
                 'process_single_failed': "Failed to process the single track after fetching.",
                 'extraction_initial': "Error processing initial data.",
                 'extraction_single': "Error processing single track data.",
                 'internal_extraction': "An internal error occurred fetching information."
             }
             error_message = error_map.get(extraction_error_code, "An unknown error occurred while fetching.")
             return await ctx.send(error_message)

        if not songs_to_add:
            if playlist_title:
                 logger.warning(f"{log_prefix} Playlist '{playlist_title}' yielded no valid songs.")
                 return await ctx.send(f"Found playlist '{playlist_title}', but couldn't add any playable songs from it.")
            else:
                 logger.warning(f"{log_prefix} _extract_info returned no songs for query: {query}")
                 return await ctx.send("Could not find any playable songs for your query.")

        # Add to Queue
        added_count = 0
        queue_start_pos = 0

        logger.debug(f"{log_prefix} Attempting to acquire lock to add {len(songs_to_add)} Song(s).")
        async with state._lock:
            logger.debug(f"{log_prefix} Lock acquired.")
            queue_start_pos = len(state.queue) + (1 if state.current_song else 0)
            if queue_start_pos == 0: queue_start_pos = 1
            state.queue.extend(songs_to_add)
            added_count = len(songs_to_add)
            logger.info(f"{log_prefix} Added {added_count} songs. Queue size now: {len(state.queue)}")
        logger.debug(f"{log_prefix} Lock released.")

        # Send Feedback Message
        if added_count > 0:
            logger.debug(f"{log_prefix} Preparing feedback embed.")
            try:
                is_first_song_now_playing = (not state.current_song and queue_start_pos == 1)
                embed = nextcord.Embed(color=nextcord.Color.green())
                first_song = songs_to_add[0]

                if playlist_title and added_count > 1:
                    embed.title = "Playlist Added"
                    pl_link = query if query.startswith('http') else None
                    pl_desc = f"**[{playlist_title}]({pl_link})**" if pl_link else f"**{playlist_title}**"
                    embed.description = f"Added **{added_count}** songs from playlist {pl_desc}"
                    embed.add_field(name="First Song Queued", value=f"`{queue_start_pos}.` [{first_song.title}]({first_song.webpage_url}) `[{first_song.format_duration()}]`", inline=False)
                    if is_first_song_now_playing:
                         embed.add_field(name="\u200B", value="▶️ Now Playing the first song!", inline=False)
                elif added_count == 1:
                    embed.title = "Now Playing" if is_first_song_now_playing else "Added to Queue"
                    embed.description = f"[{first_song.title}]({first_song.webpage_url})"
                    embed.add_field(name="Duration", value=first_song.format_duration(), inline=True)
                    if not is_first_song_now_playing:
                        embed.add_field(name="Position", value=f"#{queue_start_pos}", inline=True)

                requester_name = ctx.author.display_name
                requester_icon = ctx.author.display_avatar.url if ctx.author.display_avatar else None
                embed.set_footer(text=f"Requested by {requester_name}", icon_url=requester_icon)
                await ctx.send(embed=embed)
                logger.debug(f"{log_prefix} Sent feedback embed.")

            except nextcord.HTTPException as e:
                 logger.error(f"{log_prefix} Failed to send feedback message: {e}")
            except Exception as e:
                 logger.error(f"{log_prefix} Unexpected error sending embed: {e}", exc_info=True)

        # Ensure loop starts/continues
        if added_count > 0:
            logger.debug(f"{log_prefix} Ensuring playback loop is started/signaled.")
            state.start_playback_loop()
            logger.debug(f"{log_prefix} play_command finished successfully.")
        else:
             logger.warning(f"{log_prefix} play_command finished WITHOUT adding songs.")


    @commands.command(name='skip', aliases=['s', 'next'], help="Skips the currently playing song.")
    @commands.guild_only()
    async def skip_command(self, ctx: commands.Context):
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)
        if not state: return await ctx.send("I'm not active in this server.")
        state.last_command_channel_id = ctx.channel.id

        if not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected to a voice channel.")

        vc = state.voice_client
        if not vc.is_playing() and not vc.is_paused():
             return await ctx.send("There's nothing playing or paused to skip.")
        if not state.current_song:
             # If playing/paused but no current_song, stop the player directly
             logger.warning(f"[{ctx.guild.id}] Skip called while VC active but current_song is None. Stopping VC.")
             vc.stop()
             await ctx.message.add_reaction('⏭️')
             return

        logger.info(f"[{ctx.guild.id}] Skip requested by {ctx.author.name} for '{state.current_song.title}'.")
        vc.stop() # Triggers 'after' callback -> play_next_song.set() -> loop advances
        await ctx.message.add_reaction('⏭️')


    # --- MODIFIED stop_command ---
    @commands.command(name='stop', help="Stops playback completely and clears the queue.")
    @commands.guild_only()
    async def stop_command(self, ctx: commands.Context):
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)

        if not state:
             return await ctx.send("I'm not active in this server.")
        state.last_command_channel_id = ctx.channel.id

        if not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected or not playing anything.")
        if not state.current_song and not state.queue:
             return await ctx.send("Nothing to stop (queue is empty and nothing is playing).")

        # --- CORRECTED LOGGING ---
        logger.info(f"[{ctx.guild.id}] Stop requested by {ctx.author.name}.")
        # -------------------------
        await state.stop_playback() # Clears queue and stops player
        await ctx.send("Playback stopped and queue cleared.")
        await ctx.message.add_reaction('⏹️')


    @commands.command(name='pause', help="Pauses the currently playing song.")
    @commands.guild_only()
    async def pause_command(self, ctx: commands.Context):
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)
        if not state: return await ctx.send("I'm not active in this server.")
        state.last_command_channel_id = ctx.channel.id

        if not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected.")
        if not state.voice_client.is_playing():
             if state.voice_client.is_paused(): return await ctx.send("Playback is already paused.")
             else: return await ctx.send("Nothing is actively playing to pause.")

        state.voice_client.pause()
        logger.info(f"[{ctx.guild.id}] Playback paused by {ctx.author.name}.")
        await ctx.message.add_reaction('⏸️')


    @commands.command(name='resume', aliases=['unpause'], help="Resumes a paused song.")
    @commands.guild_only()
    async def resume_command(self, ctx: commands.Context):
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)
        if not state: return await ctx.send("I'm not active in this server.")
        state.last_command_channel_id = ctx.channel.id

        if not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected.")
        if not state.voice_client.is_paused():
            if state.voice_client.is_playing(): return await ctx.send("Playback is already playing.")
            else: return await ctx.send("Nothing is currently paused.")

        state.voice_client.resume()
        logger.info(f"[{ctx.guild.id}] Playback resumed by {ctx.author.name}.")
        await ctx.message.add_reaction('▶️')


    # --- MODIFIED queue_command ---
    @commands.command(name='queue', aliases=['q', 'nowplaying', 'np'], help="Shows the current song queue.")
    @commands.guild_only()
    async def queue_command(self, ctx: commands.Context):
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)
        if not state:
             return await ctx.send("I haven't played anything in this server yet.")
        state.last_command_channel_id = ctx.channel.id

        async with state._lock:
            current_song = state.current_song
            queue_copy = list(state.queue)

        if not current_song and not queue_copy:
            return await ctx.send("The queue is empty and nothing is playing.")

        embed = nextcord.Embed(title="Music Queue", color=nextcord.Color.blurple())
        current_display = "Nothing currently playing."
        total_queue_duration = 0

        # Display Current Song
        if current_song:
            status_icon = "❓"
            if state.voice_client and state.voice_client.is_connected():
                 if state.voice_client.is_playing(): status_icon = "▶️ Playing"
                 elif state.voice_client.is_paused(): status_icon = "⏸️ Paused"
                 else: status_icon = "⏹️ Stopped/Idle"
            requester_mention = current_song.requester.mention if current_song.requester else "Unknown"
            current_display = f"{status_icon}: **[{current_song.title}]({current_song.webpage_url})** `[{current_song.format_duration()}]` - Req by {requester_mention}"
        embed.add_field(name="Now Playing", value=current_display, inline=False)

        # Display Queue (with character limit)
        if queue_copy:
            queue_list_strings = []
            current_length = 0
            char_limit = 950 # Safely below 1024
            songs_shown = 0
            max_songs_to_list = 20 # Limit number of entries listed

            for i, song in enumerate(queue_copy):
                if song.duration:
                    try: total_queue_duration += int(song.duration)
                    except (ValueError, TypeError): pass

                # Only format and add if within limits
                if songs_shown < max_songs_to_list:
                    requester_name = song.requester.display_name if song.requester else "Unknown"
                    song_line = f"`{i+1}.` [{song.title}]({song.webpage_url}) `[{song.format_duration()}]` - Req by {requester_name}\n"

                    if current_length + len(song_line) <= char_limit:
                        queue_list_strings.append(song_line)
                        current_length += len(song_line)
                        songs_shown += 1
                    else:
                        # Stop adding detailed entries if limit reached
                        remaining_songs = len(queue_copy) - i
                        if remaining_songs > 0:
                             queue_list_strings.append(f"\n...and {remaining_songs} more song{'s' if remaining_songs != 1 else ''}.")
                        break # Exit loop once limit is hit

            # If loop finished but max_songs_to_list was hit before char_limit
            if songs_shown == max_songs_to_list and len(queue_copy) > max_songs_to_list:
                 remaining_songs = len(queue_copy) - max_songs_to_list
                 queue_list_strings.append(f"\n...and {remaining_songs} more song{'s' if remaining_songs != 1 else ''}.")


            total_dur_str = Song(None,None,None,total_queue_duration,None).format_duration() if total_queue_duration > 0 else "N/A"
            queue_header = f"Up Next ({len(queue_copy)} song{'s' if len(queue_copy) != 1 else ''}, Total Duration: {total_dur_str})"
            queue_value = "".join(queue_list_strings).strip()
            if not queue_value and len(queue_copy) > 0: # Handle edge case where first song is too long
                 queue_value = f"Queue contains {len(queue_copy)} song(s), but the first is too long to display."

            # Ensure value is not empty before adding field
            if queue_value:
                 embed.add_field(name=queue_header, value=queue_value, inline=False)
            else: # If queue_value is somehow still empty (e.g. queue_copy was empty)
                 embed.add_field(name="Up Next", value="No songs in queue.", inline=False)

        else:
             embed.add_field(name="Up Next", value="No songs in queue.", inline=False)

        total_songs_in_system = len(queue_copy) + (1 if current_song else 0)
        volume_percent = int(state.volume * 100) if hasattr(state, 'volume') else "N/A"
        embed.set_footer(text=f"Total songs: {total_songs_in_system} | Volume: {volume_percent}%")
        await ctx.send(embed=embed)


    @commands.command(name='volume', aliases=['vol'], help="Changes the player volume (0-100).")
    @commands.guild_only()
    async def volume_command(self, ctx: commands.Context, *, volume: int):
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)
        if not state: return await ctx.send("I'm not active in this server.")
        state.last_command_channel_id = ctx.channel.id

        if not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected to a voice channel.")

        if not 0 <= volume <= 100:
            return await ctx.send("Volume must be between 0 and 100.")

        new_volume_float = volume / 100.0
        state.volume = new_volume_float
        logger.debug(f"[{ctx.guild.id}] State volume set to {new_volume_float}")

        if state.voice_client.source and isinstance(state.voice_client.source, nextcord.PCMVolumeTransformer):
            state.voice_client.source.volume = new_volume_float
            logger.info(f"[{ctx.guild.id}] Volume adjusted live to {volume}% by {ctx.author.name}.")
            await ctx.send(f"Volume changed to **{volume}%**.")
        else:
             logger.info(f"[{ctx.guild.id}] Volume pre-set to {volume}% by {ctx.author.name} (will apply to next song).")
             await ctx.send(f"Volume set to **{volume}%**. It will apply to the next song played.")


    # --- Error Handling for Music Commands ---
    async def cog_command_error(self, ctx: commands.Context, error):
        """Local error handler specifically for commands in this Cog."""
        log_prefix = f"[{ctx.guild.id if ctx.guild else 'DM'}] MusicCog Error:"
        state = self.guild_states.get(ctx.guild.id) if ctx.guild else None

        if state and hasattr(ctx, 'channel'):
             state.last_command_channel_id = ctx.channel.id

        if isinstance(error, commands.CommandNotFound):
            return # Let bot handle this

        elif isinstance(error, commands.CheckFailure):
             if isinstance(error, commands.GuildOnly):
                  logger.warning(f"{log_prefix} GuildOnly command '{ctx.command.qualified_name}' used in DM by {ctx.author}.")
                  return
             logger.warning(f"{log_prefix} Check failed for '{ctx.command.qualified_name}' by {ctx.author}: {error}")
             await ctx.send("You don't have permission for this command.")

        elif isinstance(error, commands.MissingRequiredArgument):
             logger.debug(f"{log_prefix} Missing argument for '{ctx.command.qualified_name}': {error.param.name}")
             await ctx.send(f"Missing argument: `{error.param.name}`. Use `?help {ctx.command.qualified_name}`.")

        elif isinstance(error, commands.BadArgument):
             logger.debug(f"{log_prefix} Bad argument for '{ctx.command.qualified_name}': {error}")
             await ctx.send(f"Invalid argument type. Check `?help {ctx.command.qualified_name}`.")

        elif isinstance(error, commands.CommandInvokeError):
            original = error.original
            # Special handling for the queue embed length error
            if isinstance(original, nextcord.HTTPException) and original.code == 50035 and 'embeds.0.fields' in str(original.text):
                 logger.warning(f"{log_prefix} Embed field length error for command '{ctx.command.qualified_name}': {original}")
                 await ctx.send("The queue is too long to display fully in the embed.")
                 return # Don't log the full error below for this specific case

            logger.error(f"{log_prefix} Error invoking command '{ctx.command.qualified_name}': {original.__class__.__name__}: {original}", exc_info=original)
            if isinstance(original, nextcord.errors.ClientException):
                await ctx.send(f"Voice Error: {original}")
            elif isinstance(original, yt_dlp.utils.DownloadError):
                 await ctx.send("Error fetching song/playlist data (unavailable, private, network?).")
            elif isinstance(original, asyncio.TimeoutError):
                 await ctx.send("An operation timed out. Please try again.")
            else:
                await ctx.send(f"An internal error occurred running `{ctx.command.name}`.")
        else:
            logger.error(f"{log_prefix} Unhandled error in cog_command_error for '{ctx.command.qualified_name}': {type(error).__name__}: {error}", exc_info=error)
            # raise error # Optionally re-raise for global handler


# --- Setup Function ---
def setup(bot: commands.Bot):
    """Adds the MusicCog to the bot."""
    # Opus Loading
    try:
        if not nextcord.opus.is_loaded():
            logger.info("Opus library not loaded. Attempting to load...")
            try:
                 nextcord.opus.load_opus()
                 logger.info("Opus library loaded successfully using default load_opus().")
            except nextcord.opus.OpusNotLoaded:
                 logger.warning("Default Opus load failed. Trying common paths...")
                 opus_paths = [
                     '/usr/lib/x86_64-linux-gnu/libopus.so.0',
                     '/usr/lib/libopus.so.0',
                     'libopus-0.x64.dll',
                     'libopus-0.x86.dll',
                     'libopus.0.dylib',
                     'opus' # Sometimes just 'opus' works on PATH
                 ]
                 loaded = False
                 for path in opus_paths:
                     try:
                         nextcord.opus.load_opus(path)
                         logger.info(f"Opus manually loaded successfully from: {path}")
                         loaded = True
                         break
                     except nextcord.opus.OpusNotFound: continue # Try next path
                     except nextcord.opus.OpusLoadError as e: logger.error(f"Error loading Opus from {path}: {e}")
                     except Exception as e: logger.error(f"Unexpected error loading Opus from {path}: {e}")

                 if not loaded:
                      logger.critical("CRITICAL: Failed to load Opus library. Voice functionality WILL NOT work.")
                      # Consider raising an error to prevent cog loading if Opus is mandatory
                      # raise RuntimeError("Could not load libopus.")
        else:
            logger.info("Opus library was already loaded.")
    except Exception as e:
         logger.critical(f"CRITICAL: Unexpected error during Opus loading check: {e}", exc_info=True)

    # Add Cog
    try:
        bot.add_cog(MusicCog(bot))
        logger.info("MusicCog added to bot successfully.")
    except Exception as e:
         logger.critical(f"CRITICAL: Failed to add MusicCog to bot: {e}", exc_info=True)

# --- End of File ---