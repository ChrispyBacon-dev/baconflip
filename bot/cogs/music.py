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
    # --- Playlist Settings ---
    'noplaylist': False,       # Allow processing playlists
    'playlistend': 50,         # Limit playlist items fetched (adjust as needed)
    # -----------------------
    'nocheckcertificate': True,
    'ignoreerrors': False,     # False needed to process playlist items individually after potential errors
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',  # bind to ipv4 since ipv6 addresses cause issues sometimes
}

# Configure logger for this cog
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG) # Enable DEBUG logging for this cog

class Song:
    """Represents a song to be played."""
    def __init__(self, source_url, title, webpage_url, duration, requester):
        self.source_url = source_url
        self.title = title
        self.webpage_url = webpage_url
        self.duration = duration
        self.requester = requester

    def format_duration(self):
        """Formats duration seconds into MM:SS or HH:MM:SS"""
        if self.duration is None: return "N/A"
        try: duration_int = int(self.duration)
        except (ValueError, TypeError): return "N/A"
        minutes, seconds = divmod(duration_int, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours > 0 else f"{minutes:02d}:{seconds:02d}"

class GuildMusicState:
    """Manages music state for a single guild."""
    def __init__(self, bot: commands.Bot, guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.queue = deque()
        self.voice_client: nextcord.VoiceClient | None = None
        self.current_song: Song | None = None
        self.volume = 0.5
        self.play_next_song = asyncio.Event()
        self._playback_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

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
                queue_is_empty = not self.queue
                if not queue_is_empty:
                    song_to_play = self.queue.popleft()
                    self.current_song = song_to_play
                    logger.info(f"{log_prefix} Popped '{song_to_play.title}'. Queue size: {len(self.queue)}")
                else:
                     logger.debug(f"{log_prefix} Queue is empty.")
                     self.current_song = None
            logger.debug(f"{log_prefix} Lock released after queue check.")

            if queue_is_empty:
                logger.info(f"{log_prefix} Queue empty, pausing loop. Waiting for play_next_song event...")
                # --- Inactivity Timeout (Currently Disabled for Debugging) ---
                # try:
                #     await asyncio.wait_for(self.play_next_song.wait(), timeout=300.0) # 5 mins
                # except asyncio.TimeoutError:
                #      if self.voice_client and not self.queue and not self.voice_client.is_playing():
                #          logger.info(f"[{self.guild_id}] No activity timeout. Cleaning up.")
                #          # Ensure cleanup is handled safely - potentially schedule it
                #          self.bot.loop.create_task(self.cleanup_from_inactivity())
                #          return # Exit the loop
                await self.play_next_song.wait() # Wait indefinitely
                # --- End Inactivity Timeout ---
                logger.info(f"{log_prefix} play_next_song event received while paused. Continuing loop.")
                continue # Re-check queue

            logger.debug(f"{log_prefix} Checking voice client state.")
            if not self.voice_client or not self.voice_client.is_connected():
                logger.warning(f"{log_prefix} Voice client disconnected. Stopping loop.")
                if song_to_play:
                    logger.warning(f"{log_prefix} Putting '{song_to_play.title}' back in queue.")
                    async with self._lock: self.queue.appendleft(song_to_play)
                self.current_song = None
                return # Exit loop

            if song_to_play:
                logger.info(f"{log_prefix} Attempting to play: {song_to_play.title}")
                source = None
                try:
                    logger.debug(f"{log_prefix} Creating FFmpegPCMAudio source for URL: {song_to_play.source_url}")
                    original_source = nextcord.FFmpegPCMAudio(
                        song_to_play.source_url,
                        before_options=FFMPEG_BEFORE_OPTIONS,
                        options=FFMPEG_OPTIONS
                    )
                    source = nextcord.PCMVolumeTransformer(original_source, volume=self.volume)
                    logger.debug(f"{log_prefix} Source created successfully (PCMVolumeTransformer wrapping FFmpegPCMAudio).")

                    if not self.voice_client or not self.voice_client.is_connected():
                         logger.warning(f"{log_prefix} VC disconnected just before calling play(). Aborting play for '{song_to_play.title}'.")
                         async with self._lock: self.queue.appendleft(song_to_play)
                         self.current_song = None
                         self.play_next_song.set()
                         continue

                    self.voice_client.play(source, after=lambda e: self._handle_after_play(e))
                    logger.info(f"{log_prefix} voice_client.play() called successfully for {song_to_play.title}")

                    logger.debug(f"{log_prefix} Waiting for play_next_song event (song completion/skip)...")
                    await self.play_next_song.wait()
                    logger.debug(f"{log_prefix} play_next_song event received after playback attempt for '{song_to_play.title}'.")

                except nextcord.errors.ClientException as e:
                    logger.error(f"{log_prefix} ClientException playing {song_to_play.title}: {e}")
                    await self._notify_channel_error(f"Error playing '{song_to_play.title}': {e}")
                    self.play_next_song.set()
                except yt_dlp.utils.DownloadError as e:
                     logger.error(f"{log_prefix} Download Error during playback attempt for {song_to_play.title}: {e}")
                     await self._notify_channel_error(f"Download error for '{song_to_play.title}'.")
                     self.play_next_song.set()
                except Exception as e:
                    logger.error(f"{log_prefix} Unexpected error during playback of {song_to_play.title}: {e}", exc_info=True)
                    await self._notify_channel_error(f"Unexpected error playing '{song_to_play.title}'.")
                    self.play_next_song.set()
                finally:
                    logger.debug(f"{log_prefix} Playback block for '{song_to_play.title if song_to_play else 'None'}' finished.")
            else:
                 logger.warning(f"{log_prefix} Reached play block but song_to_play is None. Waiting to avoid tight loop.")
                 await self.play_next_song.wait()

    def _handle_after_play(self, error):
        """Callback function run after a song finishes or errors."""
        log_prefix = f"[{self.guild_id}] After Play Callback: "
        if error:
            logger.error(f"{log_prefix}Playback error encountered: {error!r}", exc_info=error)
        else:
            logger.debug(f"{log_prefix}Song finished playing successfully.")
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
        if self.queue and not self.play_next_song.is_set():
             logger.debug(f"[{self.guild_id}] start_playback_loop: Setting play_next_song event as queue is not empty.")
             self.play_next_song.set()

    def _handle_loop_completion(self, task: asyncio.Task):
        """Callback for when the playback loop task finishes."""
        try:
            if task.cancelled():
                 logger.info(f"[{self.guild_id}] Playback loop task was cancelled.")
            elif task.exception():
                logger.error(f"[{self.guild_id}] Playback loop task exited with error:", exc_info=task.exception())
            else:
                logger.info(f"[{self.guild_id}] Playback loop task finished gracefully.")
        except asyncio.CancelledError:
             logger.info(f"[{self.guild_id}] _handle_loop_completion was cancelled.")
        except Exception as e:
             logger.error(f"[{self.guild_id}] Error in _handle_loop_completion itself: {e}", exc_info=True)
        self._playback_task = None
        logger.debug(f"[{self.guild_id}] Playback task reference cleared.")

    async def stop_playback(self):
        """Stops playback and clears the queue."""
        async with self._lock:
            self.queue.clear()
            vc = self.voice_client
            if vc and vc.is_playing():
                logger.info(f"[{self.guild_id}] Stopping currently playing track via stop_playback.")
                vc.stop()
            self.current_song = None
            logger.info(f"[{self.guild_id}] Queue cleared by stop_playback.")
            if not self.play_next_song.is_set():
                logger.debug(f"[{self.guild_id}] Setting play_next_song event in stop_playback.")
                self.play_next_song.set()

    async def cleanup(self):
        """Cleans up resources (disconnects VC, stops loop)."""
        guild_id = self.guild_id
        log_prefix = f"[{guild_id}] Cleanup:"
        logger.info(f"{log_prefix} Starting cleanup.")
        await self.stop_playback()
        if self._playback_task and not self._playback_task.done():
            logger.info(f"{log_prefix} Cancelling playback loop task.")
            self._playback_task.cancel()
            try: await self._playback_task
            except asyncio.CancelledError: logger.debug(f"{log_prefix} Playback task cancelled successfully.")
            except Exception as e: logger.error(f"{log_prefix} Error awaiting cancelled playback task: {e}", exc_info=True)
        self._playback_task = None
        vc = self.voice_client
        if vc and vc.is_connected():
            logger.info(f"{log_prefix} Disconnecting voice client.")
            try: await vc.disconnect(force=True)
            except Exception as e: logger.error(f"{log_prefix} Error disconnecting voice client: {e}", exc_info=True)
        self.voice_client = None
        self.current_song = None
        logger.info(f"{log_prefix} Cleanup finished.")

    async def cleanup_from_inactivity(self):
        """Cleanup specifically triggered by inactivity timeout."""
        logger.info(f"[{self.guild_id}] Initiating cleanup due to inactivity.")
        await self.cleanup()
        music_cog = self.bot.get_cog("Music")
        if music_cog and self.guild_id in music_cog.guild_states:
            del music_cog.guild_states[self.guild_id]
            logger.info(f"[{self.guild_id}] Music state removed after inactivity cleanup.")

    async def _notify_channel_error(self, message: str):
        """Helper to try and send error messages to a relevant channel."""
        logger.warning(f"[{self.guild_id}] Channel Error Notification (Not Sent): {message}")


class MusicCog(commands.Cog, name="Music"):
    """Commands for playing music in voice channels."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_states: dict[int, GuildMusicState] = {}
        self.ydl = yt_dlp.YoutubeDL(YDL_OPTS)

    def get_guild_state(self, guild_id: int) -> GuildMusicState:
        """Gets or creates the music state for a guild."""
        if guild_id not in self.guild_states:
            logger.debug(f"Creating new GuildMusicState for guild {guild_id}")
            self.guild_states[guild_id] = GuildMusicState(self.bot, guild_id)
        return self.guild_states[guild_id]

    async def _extract_song_info(self, query: str) -> list[dict] | dict | None:
        """Extracts song info using yt-dlp. Returns a list for playlists, dict for single."""
        log_prefix = f"[{self.bot.user.id or 'Bot'}] YTDL:"
        logger.debug(f"{log_prefix} Attempting to extract info for query: '{query}'")
        try:
            loop = asyncio.get_event_loop()
            partial = functools.partial(self.ydl.extract_info, query, download=False, process=True) # process=True helps with playlists
            data = await loop.run_in_executor(None, partial)
            logger.debug(f"{log_prefix} Raw YTDL data type: {type(data)}. Keys (if dict): {data.keys() if isinstance(data, dict) else 'N/A'}")

            if not data:
                logger.warning(f"{log_prefix} yt-dlp returned no data for query: {query}")
                return None

            # --- Handle Playlists ---
            if 'entries' in data:
                entries = data.get('entries')
                if not entries:
                     logger.warning(f"{log_prefix} Playlist found but has no entries: {query}")
                     return {'error': 'empty_playlist'}

                logger.info(f"{log_prefix} Playlist detected with {len(entries)} potential items (limit: {YDL_OPTS.get('playlistend', 'N/A')}). Processing...")
                song_infos = []
                processed_count = 0
                error_count = 0

                for entry in entries:
                    if not entry:
                        error_count += 1
                        continue

                    audio_url = None
                    if 'url' in entry: audio_url = entry['url']
                    elif 'formats' in entry:
                         formats = entry.get('formats', [])
                         for f in formats: if f.get('acodec') == 'opus' and f.get('vcodec') == 'none' and f.get('url'): audio_url = f.get('url'); break
                         if not audio_url: for f in formats: if f.get('acodec') == 'vorbis' and f.get('vcodec') == 'none' and f.get('url'): audio_url = f.get('url'); break
                         if not audio_url: for f in formats: if f.get('acodec') == 'aac' and f.get('vcodec') == 'none' and f.get('url'): audio_url = f.get('url'); break
                         if not audio_url: for f in formats: if ('bestaudio' in f.get('format_id', '').lower() or 'bestaudio' in f.get('format_note', '').lower()) and f.get('url'): audio_url = f['url']; break

                    if audio_url:
                        song_infos.append({
                            'source_url': audio_url,
                            'title': entry.get('title', 'Unknown Title'),
                            'webpage_url': entry.get('webpage_url', 'N/A'),
                            'duration': entry.get('duration'),
                            'uploader': entry.get('uploader', 'Unknown Uploader')
                        })
                        processed_count += 1
                    else:
                        error_count += 1
                        logger.warning(f"{log_prefix} Could not find audio URL for playlist item: {entry.get('title', entry.get('id', 'Unknown ID'))}")

                logger.info(f"{log_prefix} Finished processing playlist: {processed_count} songs added, {error_count} errors.")
                if not song_infos: return {'error': 'playlist_extraction_failed'}
                return song_infos

            # --- Handle Single Track ---
            else:
                logger.debug(f"{log_prefix} Single track detected. Processing...")
                entry = data
                try:
                    processed_entry = self.ydl.process_ie_result(entry, download=False) if entry else None
                except Exception as process_err:
                    logger.error(f"{log_prefix} Error processing IE result for single: {process_err}", exc_info=True)
                    processed_entry = entry

                if not processed_entry:
                     logger.error(f"{log_prefix} Processed entry became None for single track.")
                     return None

                audio_url = None
                if 'url' in processed_entry: audio_url = processed_entry['url']
                elif 'formats' in processed_entry:
                    formats = processed_entry.get('formats', [])
                    for f in formats: if f.get('acodec') == 'opus' and f.get('vcodec') == 'none' and f.get('url'): audio_url = f.get('url'); break
                    if not audio_url: for f in formats: if f.get('acodec') == 'vorbis' and f.get('vcodec') == 'none' and f.get('url'): audio_url = f.get('url'); break
                    if not audio_url: for f in formats: if f.get('acodec') == 'aac' and f.get('vcodec') == 'none' and f.get('url'): audio_url = f.get('url'); break
                    if not audio_url: for f in formats: if ('bestaudio' in f.get('format_id', '').lower() or 'bestaudio' in f.get('format_note', '').lower()) and f.get('url'): audio_url = f['url']; break
                    if not audio_url:
                        requested_formats = processed_entry.get('requested_formats')
                        if requested_formats and isinstance(requested_formats, list) and len(requested_formats) > 0: audio_url = requested_formats[0].get('url')

                logger.debug(f"{log_prefix} Final single audio URL: {audio_url}")
                if not audio_url:
                    logger.error(f"{log_prefix} Could not extract playable URL for single: {processed_entry.get('title', query)}")
                    return None

                single_info = {
                    'source_url': audio_url,
                    'title': processed_entry.get('title', 'Unknown Title'),
                    'webpage_url': processed_entry.get('webpage_url', query),
                    'duration': processed_entry.get('duration'),
                    'uploader': processed_entry.get('uploader', 'Unknown Uploader')
                }
                logger.debug(f"{log_prefix} Successfully extracted single song info: {single_info.get('title')}")
                return single_info

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


    @commands.Cog.listener()
    async def on_voice_state_update(self, member: nextcord.Member, before: nextcord.VoiceState, after: nextcord.VoiceState):
        """Handles bot disconnection or empty/joined channels."""
        if not member.guild: return

        guild_id = member.guild.id
        state = self.guild_states.get(guild_id)

        if not state: return

        # Bot's own state changes
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

        # Other users' state changes in the bot's channel
        elif state.voice_client and state.voice_client.is_connected():
            current_bot_channel = state.voice_client.channel
            if before.channel == current_bot_channel and after.channel != current_bot_channel:
                 logger.debug(f"[{guild_id}] User {member.name} left bot channel {before.channel.name}.")
                 if len(current_bot_channel.members) == 1 and self.bot.user in current_bot_channel.members:
                     logger.info(f"[{guild_id}] Bot is now alone in {current_bot_channel.name}. Pausing playback.")
                     if state.voice_client.is_playing(): state.voice_client.pause()

            elif before.channel != current_bot_channel and after.channel == current_bot_channel:
                 logger.debug(f"[{guild_id}] User {member.name} joined bot channel {after.channel.name}.")
                 if state.voice_client.is_paused():
                     logger.info(f"[{guild_id}] User joined, resuming paused playback.")
                     state.voice_client.resume()


    # --- Music Commands ---

    @commands.command(name='join', aliases=['connect', 'j'], help="Connects the bot to your current voice channel.")
    @commands.guild_only()
    async def join_command(self, ctx: commands.Context):
        """Connects the bot to the voice channel the command user is in."""
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send("You need to be in a voice channel to use this command.")

        channel = ctx.author.voice.channel
        state = self.get_guild_state(ctx.guild.id)

        async with state._lock:
            if state.voice_client and state.voice_client.is_connected():
                if state.voice_client.channel == channel: await ctx.send(f"I'm already in {channel.mention}.")
                else:
                    try:
                        await state.voice_client.move_to(channel); await ctx.send(f"Moved to {channel.mention}.")
                        logger.info(f"[{ctx.guild.id}] Moved to VC: {channel.name}")
                    except asyncio.TimeoutError: await ctx.send(f"Timeout moving to {channel.mention}.")
                    except Exception as e: await ctx.send(f"Error moving: {e}"); logger.error(f"[{ctx.guild.id}] Error moving: {e}", exc_info=True)
            else:
                try:
                    state.voice_client = await channel.connect(); await ctx.send(f"Connected to {channel.mention}.")
                    logger.info(f"[{ctx.guild.id}] Connected to VC: {channel.name}")
                    state.start_playback_loop()
                except asyncio.TimeoutError: await ctx.send(f"Timeout connecting to {channel.mention}."); logger.warning(f"[{ctx.guild.id}] Timeout connecting."); await self._cleanup_failed_connect(ctx.guild.id)
                except nextcord.errors.ClientException as e: await ctx.send(f"Unable to connect: {e}"); logger.warning(f"[{ctx.guild.id}] ClientException on connect: {e}"); await self._cleanup_failed_connect(ctx.guild.id)
                except Exception as e: await ctx.send(f"Error connecting: {e}"); logger.error(f"[{ctx.guild.id}] Error connecting: {e}", exc_info=True); await self._cleanup_failed_connect(ctx.guild.id)

    async def _cleanup_failed_connect(self, guild_id: int):
         """Removes potentially partial state if connection fails."""
         if guild_id in self.guild_states:
              logger.debug(f"Cleaning up failed connection state for guild {guild_id}")
              try:
                   # Ensure VC is None if cleanup is needed
                   if self.guild_states[guild_id].voice_client:
                        await self.guild_states[guild_id].voice_client.disconnect(force=True)
                   del self.guild_states[guild_id]
              except Exception as e:
                   logger.error(f"Error during failed connection cleanup for {guild_id}: {e}")


    @commands.command(name='leave', aliases=['disconnect', 'dc', 'fuckoff'], help="Disconnects the bot from the voice channel.")
    @commands.guild_only()
    async def leave_command(self, ctx: commands.Context):
        """Disconnects the bot from its current voice channel in the guild."""
        state = self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected(): return await ctx.send("I'm not connected.")
        logger.info(f"[{ctx.guild.id}] Leave command initiated by {ctx.author.name}.")
        await state.cleanup()
        if ctx.guild.id in self.guild_states: del self.guild_states[ctx.guild.id]; logger.info(f"[{ctx.guild.id}] Removed music state after leave.")
        await ctx.message.add_reaction('👋')


    @commands.command(name='play', aliases=['p'], help="Plays a song or playlist from a URL/search.")
    @commands.guild_only()
    async def play_command(self, ctx: commands.Context, *, query: str):
        """Plays audio from a URL/search, handling playlists."""
        state = self.get_guild_state(ctx.guild.id)
        log_prefix = f"[{ctx.guild.id}] PlayCmd:"
        logger.info(f"{log_prefix} User {ctx.author.name} initiated play with query: {query}")

        # 1. Connection checks
        if not state.voice_client or not state.voice_client.is_connected():
            if ctx.author.voice and ctx.author.voice.channel:
                 logger.info(f"{log_prefix} Bot not connected. Invoking join.")
                 await ctx.invoke(self.join_command)
                 state = self.get_guild_state(ctx.guild.id) # Re-fetch state
                 if not state.voice_client or not state.voice_client.is_connected(): logger.warning(f"{log_prefix} Failed join after invoke."); return
                 else: logger.info(f"{log_prefix} Joined VC after invoke.")
            else: logger.warning(f"{log_prefix} Failed: User not in VC, bot not connected."); return await ctx.send("You need to be in a VC.")
        elif not ctx.author.voice or ctx.author.voice.channel != state.voice_client.channel:
             logger.warning(f"{log_prefix} Failed: User not in bot's VC."); return await ctx.send(f"You need to be in {state.voice_client.channel.mention}.")
        else: logger.info(f"{log_prefix} Bot connected to {state.voice_client.channel.name}.")

        # --- Extraction Phase ---
        all_song_info = None
        logger.debug(f"{log_prefix} Entering extraction.")
        typing_task = asyncio.create_task(ctx.trigger_typing())
        try:
            logger.debug(f"{log_prefix} Calling _extract_song_info...")
            all_song_info = await self._extract_song_info(query)
            logger.debug(f"{log_prefix} _extract_song_info finished.")
        except Exception as e:
            logger.error(f"{log_prefix} Exception DURING _extract_song_info: {e}", exc_info=True); await ctx.send("Error fetching info."); typing_task.cancel(); return
        finally:
            if not typing_task.done(): typing_task.cancel()

        # --- Process Extraction Result ---
        logger.debug(f"{log_prefix} Processing result type: {type(all_song_info)}. Result: {str(all_song_info)[:300]}...")
        if not all_song_info: logger.warning(f"{log_prefix} No info returned."); return await ctx.send("Could not get info.")
        if isinstance(all_song_info, dict) and all_song_info.get('error'):
             error_type = all_song_info['error']; logger.warning(f"{log_prefix} Extractor error: {error_type}"); msg_map = {'unsupported': "Unsupported URL/service.", 'unavailable': "Video/playlist unavailable.", 'download': "Error accessing data.", 'age_restricted': "Can't play age-restricted.", 'empty_playlist': "Playlist is empty.", 'playlist_extraction_failed': "Failed to extract songs."}; return await ctx.send(msg_map.get(error_type, "Unknown fetch error."))

        # --- Prepare List of Songs ---
        songs_to_add = []
        if isinstance(all_song_info, list): # Playlist
             logger.debug(f"{log_prefix} Processing playlist ({len(all_song_info)} items).")
             for info in all_song_info:
                 try: song = Song(info.get('source_url'), info.get('title'), info.get('webpage_url'), info.get('duration'), ctx.author); songs_to_add.append(song) if song.source_url and song.title else logger.warning(f"{log_prefix} Skipping playlist item: {info}")
                 except Exception as e: logger.error(f"{log_prefix} Error processing playlist item: {e}. Info: {info}", exc_info=True)
             if not songs_to_add: logger.warning(f"{log_prefix} No valid songs from playlist."); return await ctx.send("No valid songs processed from playlist.")
        elif isinstance(all_song_info, dict): # Single
             logger.debug(f"{log_prefix} Processing single song.")
             try: song = Song(all_song_info.get('source_url'), all_song_info.get('title'), all_song_info.get('webpage_url'), all_song_info.get('duration'), ctx.author); songs_to_add.append(song) if song.source_url and song.title else logger.error(f"{log_prefix} Invalid single song data."); return await ctx.send("Failed processing song data.")
             except Exception as e: logger.error(f"{log_prefix} Error creating single Song: {e}.", exc_info=True); return await ctx.send("Error preparing song.")
        else: logger.error(f"{log_prefix} Unexpected info type: {type(all_song_info)}"); return await ctx.send("Internal error after fetch.")

        # --- Add Song(s) to Queue ---
        if not songs_to_add: logger.warning(f"{log_prefix} No songs to add."); return
        logger.debug(f"{log_prefix} Acquiring lock to add {len(songs_to_add)} song(s).")
        start_queue_pos = 0; added_count = len(songs_to_add)
        async with state._lock: start_queue_pos = len(state.queue) + 1; state.queue.extend(songs_to_add); current_queue_size = len(state.queue); logger.info(f"{log_prefix} Added {added_count} song(s) at #{start_queue_pos}. Queue size: {current_queue_size}")
        logger.debug(f"{log_prefix} Lock released.")

        # --- Send Feedback Message ---
        logger.debug(f"{log_prefix} Preparing feedback embed.")
        try:
            first_song = songs_to_add[0]; is_now_playing = (not state.current_song and start_queue_pos == 1)
            embed = nextcord.Embed(color=nextcord.Color.green())
            if added_count == 1: embed.title = "Now Playing" if is_now_playing else "Added to Queue"; embed.description = f"[{first_song.title}]({first_song.webpage_url})"; embed.add_field(name="Duration", value=first_song.format_duration(), inline=True); embed.add_field(name="Position", value=f"#{start_queue_pos}", inline=True) if not is_now_playing else None
            else: embed.title = "Playlist Added"; embed.description = f"Added **{added_count}** songs starting at #{start_queue_pos}."; embed.add_field(name="First Song", value=f"[{first_song.title}]({first_song.webpage_url})", inline=False)
            embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url); await ctx.send(embed=embed)
            logger.debug(f"{log_prefix} Sent feedback embed.")
        except Exception as e: logger.error(f"{log_prefix} Error sending feedback: {e}", exc_info=True)

        # --- Ensure loop starts ---
        logger.debug(f"{log_prefix} Ensuring playback loop started."); state.start_playback_loop(); logger.debug(f"{log_prefix} play_command finished.")


    @commands.command(name='skip', aliases=['s'], help="Skips the current song.")
    @commands.guild_only()
    async def skip_command(self, ctx: commands.Context):
        """Skips the current song."""
        state = self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected(): return await ctx.send("Not connected.")
        if not state.current_song: return await ctx.send("Nothing playing to skip.")
        logger.info(f"[{ctx.guild.id}] Skip requested by {ctx.author.name} for '{state.current_song.title}'.")
        state.voice_client.stop() # Triggers 'after' callback
        await ctx.message.add_reaction('⏭️')

    @commands.command(name='stop', help="Stops playback and clears queue.")
    @commands.guild_only()
    async def stop_command(self, ctx: commands.Context):
        """Stops music, clears queue, stays connected."""
        state = self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected(): return await ctx.send("Not connected or playing.")
        logger.info(f"[{ctx.guild.id}] Stop requested by {ctx.author.name}.")
        await state.stop_playback()
        await ctx.send("Playback stopped, queue cleared."); await ctx.message.add_reaction('⏹️')

    @commands.command(name='pause', help="Pauses playback.")
    @commands.guild_only()
    async def pause_command(self, ctx: commands.Context):
        """Pauses current audio."""
        state = self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected(): return await ctx.send("Not connected.")
        if state.voice_client.is_paused(): return await ctx.send("Already paused.")
        if not state.voice_client.is_playing(): return await ctx.send("Nothing playing to pause.")
        state.voice_client.pause(); logger.info(f"[{ctx.guild.id}] Paused by {ctx.author.name}."); await ctx.message.add_reaction('⏸️')

    @commands.command(name='resume', aliases=['unpause'], help="Resumes playback.")
    @commands.guild_only()
    async def resume_command(self, ctx: commands.Context):
        """Resumes paused audio."""
        state = self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected(): return await ctx.send("Not connected.")
        if not state.voice_client.is_paused(): return await ctx.send("Not paused.")
        state.voice_client.resume(); logger.info(f"[{ctx.guild.id}] Resumed by {ctx.author.name}."); await ctx.message.add_reaction('▶️')

    @commands.command(name='queue', aliases=['q', 'playlist'], help="Shows the music queue.")
    @commands.guild_only()
    async def queue_command(self, ctx: commands.Context):
        """Displays the current song queue."""
        state = self.guild_states.get(ctx.guild.id)
        if not state: return await ctx.send("Not playing anything.")

        async with state._lock:
            if not state.current_song and not state.queue: return await ctx.send("Queue empty, nothing playing.")

            embed = nextcord.Embed(title="Music Queue", color=nextcord.Color.blurple()); current_display = "Nothing playing."
            if state.current_song: song = state.current_song; status = "▶️ Playing" if state.voice_client and state.voice_client.is_playing() else "⏸️ Paused"; current_display = f"{status}: **[{song.title}]({song.webpage_url})** `[{song.format_duration()}]` - Req by {song.requester.mention}"
            embed.add_field(name="Now Playing", value=current_display, inline=False)

            if state.queue:
                q_list = []; max_disp = 10; tot_dur = 0; q_copy = list(state.queue)
                for i, song in enumerate(q_copy[:max_disp]): q_list.append(f"`{i+1}.` [{song.title}]({song.webpage_url}) `[{song.format_duration()}]` - Req by {song.requester.display_name}"); tot_dur += int(song.duration or 0)
                if len(q_copy) > max_disp: q_list.append(f"\n...and {len(q_copy) - max_disp} more."); [tot_dur := tot_dur + int(s.duration or 0) for s in q_copy[max_disp:]] # Sum remaining duration
                tot_dur_str = Song(None,None,None,tot_dur,None).format_duration() if tot_dur > 0 else "N/A"
                embed.add_field(name=f"Up Next ({len(q_copy)} song{'s' if len(q_copy) != 1 else ''}, Total: {tot_dur_str})", value="\n".join(q_list) or "Queue empty.", inline=False)
            else: embed.add_field(name="Up Next", value="Queue empty.", inline=False)

            tot_songs = len(state.queue) + (1 if state.current_song else 0); embed.set_footer(text=f"Total: {tot_songs} | Vol: {int(state.volume * 100)}%"); await ctx.send(embed=embed)

    @commands.command(name='volume', aliases=['vol'], help="Changes player volume (0-100).")
    @commands.guild_only()
    async def volume_command(self, ctx: commands.Context, *, volume: int):
        """Sets player volume."""
        state = self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected(): return await ctx.send("Not connected.")
        if not 0 <= volume <= 100: return await ctx.send("Volume: 0-100.")
        state.volume = new_vol = volume / 100.0; logger.debug(f"[{ctx.guild.id}] State vol set: {new_vol}")
        if state.voice_client.source and isinstance(state.voice_client.source, nextcord.PCMVolumeTransformer): state.voice_client.source.volume = new_vol; logger.info(f"[{ctx.guild.id}] Vol adjusted: {volume}%")
        else: logger.info(f"[{ctx.guild.id}] Vol pre-set: {volume}%")
        await ctx.send(f"Volume set: **{volume}%**.")


    # --- Error Handling ---
    async def cog_command_error(self, ctx: commands.Context, error):
        """Local error handler for MusicCog commands."""
        log_prefix = f"[{ctx.guild.id if ctx.guild else 'DM'}] MusicCog Error:"
        if isinstance(error, commands.CheckFailure):
             if isinstance(error, commands.GuildOnly): logger.warning(f"{log_prefix} GuildOnly cmd '{ctx.command.name}' in DM."); return
             logger.warning(f"{log_prefix} Check failed for '{ctx.command.name}': {error}"); return
        elif isinstance(error, commands.MissingRequiredArgument): logger.debug(f"{log_prefix} Missing arg for '{ctx.command.name}': {error.param.name}"); await ctx.send(f"Forgot arg: `{error.param.name}`. Use `?help {ctx.command.name}`.")
        elif isinstance(error, commands.BadArgument): logger.debug(f"{log_prefix} Bad arg for '{ctx.command.name}': {error}"); await ctx.send(f"Invalid arg type. Use `?help {ctx.command.name}`.")
        elif isinstance(error, commands.CommandInvokeError):
            orig = error.original; logger.error(f"{log_prefix} Invoke error '{ctx.command.name}': {orig.__class__.__name__}: {orig}", exc_info=orig)
            if isinstance(orig, nextcord.errors.ClientException): await ctx.send(f"Voice Error: {orig}")
            elif isinstance(orig, yt_dlp.utils.DownloadError): await ctx.send("Error fetching song data.")
            else: await ctx.send(f"Internal error running `{ctx.command.name}`.")
        else: logger.warning(f"{log_prefix} Unhandled error type '{ctx.command.name}': {type(error).__name__}: {error}")


# --- setup function ---
def setup(bot: commands.Bot):
    """Adds the MusicCog to the bot."""
    OPUS_PATH = '/usr/lib/x86_64-linux-gnu/libopus.so.0' # Confirmed path
    try:
        if not nextcord.opus.is_loaded():
            logger.info(f"Opus not auto-loaded. Attempting manual load: {OPUS_PATH}")
            nextcord.opus.load_opus(OPUS_PATH)
            if nextcord.opus.is_loaded(): logger.info("Opus loaded successfully.")
            else: logger.critical("Manual Opus load finished, but is_loaded() is false.")
        else: logger.info("Opus library already loaded.")
    except nextcord.opus.OpusNotLoaded as e: logger.critical(f"CRITICAL: Manual Opus load failed: {e}. Path: '{OPUS_PATH}'.")
    except Exception as e: logger.critical(f"CRITICAL: Unexpected error during Opus load: {e}", exc_info=True)

    bot.add_cog(MusicCog(bot))
    logger.info("MusicCog added to bot.")
# --- End of File ---