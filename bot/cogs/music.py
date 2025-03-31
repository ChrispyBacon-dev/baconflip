# --- bot/cogs/music.py ---

import nextcord
import nextcord.ui
from nextcord.ext import commands
import asyncio
import yt_dlp
import logging
import functools
from collections import deque
from typing import TYPE_CHECKING, Union, Optional, List # Added List

# --- Type Hinting Forward Reference ---
if TYPE_CHECKING:
    from __main__ import Bot
    # Forward declare classes used in type hints before definition
    class GuildMusicState: pass
    class MusicCog: pass
    class MusicPlayerView: pass

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
    'noplaylist': False,          # Allow playlists by default, process items individually later
    'nocheckcertificate': True,
    'ignoreerrors': True,         # Skip unavailable videos in playlists
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',  # Bind to all interfaces to avoid potential issues
    'extract_flat': 'in_playlist', # Faster playlist extraction, get individual URLs later if needed
    'force_generic_extractor': True, # Sometimes helps with problematic URLs
}

# Configure Logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG) # Set to INFO for less verbose logging in production

# --- DM Helper ---
async def _send_dm_or_log(user: nextcord.Member, message: Optional[str] = None, embed: Optional[nextcord.Embed] = None):
    """Attempts to send a DM, logs failure."""
    if not user:
        logger.warning("Attempted to send DM but user object was None.")
        return
    try:
        if message or embed: # Ensure there's something to send
            await user.send(content=message, embed=embed)
            logger.debug(f"Sent DM to {user.name} ({user.id}).")
    except nextcord.Forbidden:
        logger.warning(f"Could not send DM to {user.name} ({user.id}). DMs might be disabled or bot blocked.")
    except nextcord.HTTPException as e:
        logger.error(f"HTTP error sending DM to {user.name} ({user.id}): {e}")
    except Exception as e:
        logger.error(f"Unexpected error sending DM to {user.name} ({user.id}): {e}", exc_info=True)


# --- Song Class ---
class Song:
    """Represents a song to be played."""
    def __init__(self, source_url: str, title: str, webpage_url: str, duration: Optional[int], requester: Optional[nextcord.Member]):
        self.source_url: str = source_url
        self.title: str = title
        self.webpage_url: str = webpage_url
        self.duration: Optional[int] = duration # Store as int if available
        self.requester: Optional[nextcord.Member] = requester

    def format_duration(self) -> str:
        """Formats the duration into HH:MM:SS or MM:SS."""
        if self.duration is None:
            return "N/A"
        try:
            duration_int = int(self.duration)
            if duration_int < 0: return "N/A" # Handle potential negative durations
        except (ValueError, TypeError):
            return "N/A"

        mins, secs = divmod(duration_int, 60)
        hrs, mins = divmod(mins, 60)

        if hrs > 0:
            return f"{hrs:02d}:{mins:02d}:{secs:02d}"
        else:
            return f"{mins:02d}:{secs:02d}"

# --- Music Player View ---
# (Keep the MusicPlayerView class exactly as it was in the previous 'working' version)
# ... (MusicPlayerView class code - no changes needed here for DM feature) ...
class MusicPlayerView(nextcord.ui.View):
    """Persistent view for music player controls."""
    def __init__(self, music_cog: 'MusicCog', guild_id: int, timeout: Optional[float] = None): # Default timeout is 180s, None is persistent
        super().__init__(timeout=timeout)
        self.music_cog: 'MusicCog' = music_cog
        self.guild_id: int = guild_id
        self._update_buttons() # Initial button state update

    def _get_state(self) -> Optional['GuildMusicState']:
        """Safely gets the GuildMusicState."""
        if not self.music_cog:
            return None
        return self.music_cog.guild_states.get(self.guild_id)

    def _update_buttons(self):
        """Updates the enabled/disabled state and appearance of buttons based on player state."""
        state = self._get_state()
        vc = state.voice_client if state else None

        is_connected = state and vc and vc.is_connected()
        is_playing = is_connected and vc.is_playing()
        is_paused = is_connected and vc.is_paused()
        is_active = is_playing or is_paused
        has_queue = bool(state and state.queue) # Check if queue is not empty

        # Get button references safely
        pause_resume_btn: Optional[nextcord.ui.Button] = nextcord.utils.get(self.children, custom_id="music_pause_resume")
        skip_btn: Optional[nextcord.ui.Button] = nextcord.utils.get(self.children, custom_id="music_skip")
        stop_btn: Optional[nextcord.ui.Button] = nextcord.utils.get(self.children, custom_id="music_stop")
        queue_btn: Optional[nextcord.ui.Button] = nextcord.utils.get(self.children, custom_id="music_queue")

        # Disable all if not connected or no state
        if not is_connected or not state:
            for btn in [pause_resume_btn, skip_btn, stop_btn, queue_btn]:
                if btn: btn.disabled = True
            return

        # Pause/Resume Button
        if pause_resume_btn:
            pause_resume_btn.disabled = not is_active
            if is_paused:
                pause_resume_btn.label = "Resume"
                pause_resume_btn.emoji = "▶️"
                pause_resume_btn.style = nextcord.ButtonStyle.green
            else:
                pause_resume_btn.label = "Pause"
                pause_resume_btn.emoji = "⏸️"
                pause_resume_btn.style = nextcord.ButtonStyle.secondary

        # Skip Button: Enabled if active
        if skip_btn:
            skip_btn.disabled = not is_active # Allow skipping current song even if queue is empty

        # Stop Button: Enabled if active
        if stop_btn:
            stop_btn.disabled = not is_active

        # Queue Button: Always enabled when connected
        if queue_btn:
            queue_btn.disabled = False # Generally keep queue button enabled if connected

    async def interaction_check(self, interaction: nextcord.Interaction) -> bool:
        """Checks if the interaction user is allowed to use the controls."""
        state = self._get_state()

        # Check if user is in a voice channel
        if not interaction.user or not isinstance(interaction.user, nextcord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
            # Using ephemeral response for interaction check failure
            await interaction.response.send_message("You need to be in a voice channel to use music controls.", ephemeral=True)
            return False

        # Check if the bot is connected and in the same channel
        if not state or not state.voice_client or not state.voice_client.is_connected() or state.voice_client.channel != interaction.user.voice.channel:
            await interaction.response.send_message("You need to be in the same voice channel as the bot.", ephemeral=True)
            return False

        return True # Interaction is valid

    # --- Button Handlers (pause_resume, skip, stop, queue) ---
    # These mostly use interaction.followup.send(..., ephemeral=True), which is already user-specific
    # No changes needed in the button handlers themselves for the DM feature.
    @nextcord.ui.button(label="Pause", emoji="⏸️", style=nextcord.ButtonStyle.secondary, custom_id="music_pause_resume")
    async def pause_resume_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        state = self._get_state()
        if not state or not state.voice_client or not state.voice_client.is_connected():
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send("The bot is not connected to voice.", ephemeral=True)
            return

        vc = state.voice_client
        action_taken = None

        if vc.is_paused():
            vc.resume()
            action_taken = "Resumed"
        elif vc.is_playing():
            vc.pause()
            action_taken = "Paused"
        else:
            await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)
            return

        self._update_buttons()
        await interaction.response.defer() # Defer *before* editing original message

        try:
            await interaction.edit_original_message(view=self)
            if action_taken:
                # Ephemeral followup is good here
                await interaction.followup.send(f"Playback {action_taken}.", ephemeral=True)
        except nextcord.NotFound:
            logger.warning(f"Failed to edit original player message (NotFound) on pause/resume (Guild ID: {self.guild_id})")
            if action_taken:
                 await interaction.followup.send(f"Playback {action_taken}, but the controls message seems to be missing.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error editing player message on pause/resume (Guild ID: {self.guild_id}): {e}")
            if action_taken:
                 await interaction.followup.send(f"Playback {action_taken}, but failed to update controls.", ephemeral=True)

    @nextcord.ui.button(label="Skip", emoji="⏭️", style=nextcord.ButtonStyle.secondary, custom_id="music_skip")
    async def skip_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        state = self._get_state()
        if not state or not state.voice_client or not state.voice_client.is_connected() or not (state.voice_client.is_playing() or state.voice_client.is_paused()):
            return await interaction.response.send_message("Nothing is playing to skip.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        current_title = state.current_song.title if state.current_song else "the current track"
        logger.info(f"[Guild {self.guild_id}] Song '{current_title}' skipped via button by {interaction.user}")
        state.voice_client.stop()

        await interaction.followup.send(f"Skipped **{current_title}**.", ephemeral=True)

    @nextcord.ui.button(label="Stop", emoji="⏹️", style=nextcord.ButtonStyle.danger, custom_id="music_stop")
    async def stop_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        state = self._get_state()
        if not state or not state.voice_client or not state.voice_client.is_connected() or not (state.voice_client.is_playing() or state.voice_client.is_paused()):
            return await interaction.response.send_message("Nothing is playing to stop.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        logger.info(f"[Guild {self.guild_id}] Playback stopped via button by {interaction.user}")

        await state.stop_playback()

        await interaction.followup.send("Playback stopped and queue cleared.", ephemeral=True)

    @nextcord.ui.button(label="Queue", emoji="📜", style=nextcord.ButtonStyle.secondary, custom_id="music_queue")
    async def queue_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        state = self._get_state()
        if not state:
            return await interaction.response.send_message("Music player state not found.", ephemeral=True)
        if not self.music_cog:
             return await interaction.response.send_message("Music cog instance not found.", ephemeral=True)

        try:
            queue_embed = await self.music_cog.build_queue_embed(state)
            if queue_embed:
                # Send queue embed ephemerally in response to button
                await interaction.response.send_message(embed=queue_embed, ephemeral=True)
            else:
                await interaction.response.send_message("The queue is empty and nothing is playing.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error building or sending queue embed (Guild ID: {self.guild_id}): {e}", exc_info=True)
            await interaction.response.send_message("Sorry, an error occurred while trying to display the queue.", ephemeral=True)

    async def on_timeout(self):
        logger.debug(f"MusicPlayerView timed out or stopped (Guild ID: {self.guild_id})")
        state = self._get_state()

        for item in self.children:
            if isinstance(item, nextcord.ui.Button):
                item.disabled = True

        if state and state.current_player_view is self:
            if state.current_player_message_id and state.last_command_channel_id:
                try:
                    channel = self.music_cog.bot.get_channel(state.last_command_channel_id)
                    if channel and isinstance(channel, nextcord.TextChannel):
                        message = await channel.fetch_message(state.current_player_message_id)
                        if message and message.components:
                            logger.debug(f"Editing message {state.current_player_message_id} on timeout to show disabled view.")
                            await message.edit(view=self)
                except (nextcord.NotFound, nextcord.Forbidden, AttributeError) as e:
                    logger.warning(f"Failed to edit message on view timeout (Guild ID: {self.guild_id}): {e}.")
                except Exception as e_inner:
                     logger.error(f"Unexpected error editing message on view timeout (Guild ID: {self.guild_id}): {e_inner}", exc_info=True)
            state.current_player_view = None
# --- End of MusicPlayerView ---


# --- Guild Music State ---
class GuildMusicState:
    """Manages music playback state for a single guild."""
    def __init__(self, bot: commands.Bot, guild_id: int):
        self.bot: commands.Bot = bot
        self.guild_id: int = guild_id
        self.queue: deque[Song] = deque()
        self.voice_client: Optional[nextcord.VoiceClient] = None
        self.current_song: Optional[Song] = None
        self.volume: float = 0.5
        self.play_next_song: asyncio.Event = asyncio.Event()
        self._playback_task: Optional[asyncio.Task] = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self.last_command_channel_id: Optional[int] = None # Channel where last music command was used OR where player message is
        self.current_player_message_id: Optional[int] = None
        self.current_player_view: Optional[MusicPlayerView] = None

    def _create_now_playing_embed(self, song: Optional[Song]) -> Optional[nextcord.Embed]:
        """Creates the 'Now Playing' embed."""
        # (Keep this function as is)
        if not song:
            return None

        embed = nextcord.Embed(title="Now Playing", color=nextcord.Color.green())
        embed.description = f"**[{song.title}]({song.webpage_url})**"
        embed.add_field(name="Duration", value=song.format_duration(), inline=True)

        requester = song.requester
        if requester:
            embed.add_field(name="Requested by", value=requester.mention, inline=True)
            if requester.display_avatar:
                embed.set_thumbnail(url=requester.display_avatar.url)
        else:
             embed.add_field(name="Requested by", value="Unknown", inline=True)

        return embed

    async def _update_player_message(self, *, embed: Optional[nextcord.Embed] = None, view: Optional[nextcord.ui.View] = None, content: Optional[str] = None):
        """Edits the existing player message or sends a new one IN THE CHANNEL."""
        # --- THIS FUNCTION REMAINS UNCHANGED ---
        # It is responsible for the main persistent message in the channel.
        log_prefix = f"[Guild {self.guild_id}] PlayerMsg:"
        channel_id = self.last_command_channel_id

        if not channel_id:
            logger.warning(f"{log_prefix} Cannot update player message: No command channel ID stored.")
            return

        channel = self.bot.get_channel(channel_id)
        if not channel or not isinstance(channel, nextcord.TextChannel):
            logger.warning(f"{log_prefix} Cannot update player message: Channel ID {channel_id} not found or not a text channel.")
            self.current_player_message_id = None
            self.current_player_view = None
            return

        message_to_edit = None
        message_id = self.current_player_message_id

        if message_id:
            try:
                message_to_edit = await channel.fetch_message(message_id)
                logger.debug(f"{log_prefix} Found existing message {message_id}")
            except nextcord.NotFound:
                logger.warning(f"{log_prefix} Player message {message_id} not found (likely deleted).")
                self.current_player_message_id = None
                message_to_edit = None
            except nextcord.Forbidden:
                logger.error(f"{log_prefix} Lacking permissions to fetch player message {message_id}.")
                self.current_player_message_id = None
                return
            except Exception as e:
                logger.error(f"{log_prefix} Error fetching player message {message_id}: {e}", exc_info=True)
                message_to_edit = None

        try:
            if message_to_edit:
                await message_to_edit.edit(content=content, embed=embed, view=view)
                logger.debug(f"{log_prefix} Edited message {message_id}.")
            elif embed or view or content:
                new_message = await channel.send(content=content, embed=embed, view=view)
                self.current_player_message_id = new_message.id
                if isinstance(view, MusicPlayerView):
                    self.current_player_view = view
                logger.info(f"{log_prefix} Sent new player message {new_message.id}.")
            else:
                logger.debug(f"{log_prefix} No content, embed, or view provided; nothing to send/edit.")

        except nextcord.Forbidden:
            logger.error(f"{log_prefix} Lacking permissions to send/edit player message in channel {channel_id}.")
            self.current_player_message_id = None
            self.current_player_view = None
        except nextcord.HTTPException as e:
            logger.error(f"{log_prefix} HTTP error sending/editing player message: {e}", exc_info=False)
            if e.status == 404 and message_to_edit:
                logger.warning(f"{log_prefix} Message {message_id} was deleted before edit could complete.")
                self.current_player_message_id = None
                self.current_player_view = None
        except Exception as e:
            logger.error(f"{log_prefix} Unexpected error updating player message: {e}", exc_info=True)
    # --- End _update_player_message ---


    async def _playback_loop(self):
        """The main loop that handles dequeuing songs and playing them."""
        await self.bot.wait_until_ready()
        log_prefix = f"[Guild {self.guild_id}] PlaybackLoop:"
        logger.info(f"{log_prefix} Starting.")

        music_cog: Optional['MusicCog'] = self.bot.get_cog("Music")
        if not music_cog:
            logger.critical(f"{log_prefix} MusicCog instance not found! Cannot proceed.")
            return

        while True:
            self.play_next_song.clear()
            logger.debug(f"{log_prefix} Loop top, event cleared.")
            song_to_play: Optional[Song] = None
            vc_ok = False

            # --- Check Voice Client State ---
            if self.voice_client and self.voice_client.is_connected():
                 vc_ok = True
                 if self.voice_client.is_playing() or self.voice_client.is_paused():
                     logger.debug(f"{log_prefix} VC active, waiting for play_next_song event...")
                     await self.play_next_song.wait()
                     logger.debug(f"{log_prefix} Resuming loop after VC became idle.")
                     continue
            else:
                # --- Handle Unexpected VC Disconnection ---
                logger.warning(f"{log_prefix} Voice client is not connected.")
                async with self._lock:
                    if self.current_song:
                        logger.warning(f"{log_prefix} Re-queuing '{self.current_song.title}' due to disconnect.")
                        self.queue.appendleft(self.current_song)
                        self.current_song = None

                if self.current_player_view:
                    logger.debug(f"{log_prefix} Stopping player view due to disconnect.")
                    self.current_player_view.stop()
                    # Try update message in channel
                    self.bot.loop.create_task(self._update_player_message(content="*Bot disconnected from voice.*", embed=None, view=None))
                    self.current_player_view = None

                self.current_player_message_id = None
                logger.info(f"{log_prefix} Exiting loop due to disconnect.")
                return


            # --- Get Next Song ---
            if vc_ok:
                async with self._lock:
                    if self.queue:
                        song_to_play = self.queue.popleft()
                        self.current_song = song_to_play
                        logger.info(f"{log_prefix} Popped '{song_to_play.title}'. Queue length: {len(self.queue)}")
                    else:
                        # --- Handle Empty Queue ---
                        if self.current_song:
                             logger.info(f"{log_prefix} Queue empty after '{self.current_song.title}' finished.")
                             finished_embed = self._create_now_playing_embed(self.current_song)
                             if finished_embed:
                                 finished_embed.title = "Finished Playing"

                             disabled_view = self.current_player_view
                             if disabled_view:
                                 disabled_view.stop()
                                 for item in disabled_view.children:
                                     if isinstance(item, nextcord.ui.Button): item.disabled = True

                             # Update message in channel
                             self.bot.loop.create_task(self._update_player_message(content="*Queue finished.*", embed=finished_embed, view=disabled_view))

                             self.current_song = None
                             self.current_player_view = None

                        else:
                             logger.debug(f"{log_prefix} Queue remains empty.")


            # --- Wait or Play ---
            if not song_to_play:
                logger.info(f"{log_prefix} Queue is empty. Waiting for play_next_song event...")
                await self.play_next_song.wait()
                logger.info(f"{log_prefix} Event received, restarting loop.")
                continue

            # --- Play the Song ---
            logger.info(f"{log_prefix} Attempting to play: {song_to_play.title}")
            audio_source = None
            play_success = False
            try:
                if not self.voice_client or not self.voice_client.is_connected():
                    logger.warning(f"{log_prefix} VC disconnected before play could start. Re-queuing '{song_to_play.title}'.")
                    async with self._lock:
                        self.queue.appendleft(song_to_play)
                        self.current_song = None
                    continue

                if self.voice_client.is_playing() or self.voice_client.is_paused():
                    logger.error(f"{log_prefix} Race condition? VC became active unexpectedly. Re-queuing '{song_to_play.title}'.")
                    async with self._lock:
                        self.queue.appendleft(song_to_play)
                        self.current_song = None
                    await self.play_next_song.wait()
                    continue

                original_source = nextcord.FFmpegPCMAudio(song_to_play.source_url, before_options=FFMPEG_BEFORE_OPTIONS, options=FFMPEG_OPTIONS)
                audio_source = nextcord.PCMVolumeTransformer(original_source, volume=self.volume)

                self.voice_client.play(audio_source, after=lambda e: self._handle_after_play(e))
                play_success = True
                logger.info(f"{log_prefix} Called voice_client.play() for '{song_to_play.title}'.")

                # --- Update Player Message and View in Channel ---
                logger.debug(f"{log_prefix} Updating player message in channel for '{song_to_play.title}'.")
                now_playing_embed = self._create_now_playing_embed(song_to_play)

                if self.current_player_view and not self.current_player_view.is_finished():
                    logger.debug(f"{log_prefix} Stopping previous player view.")
                    self.current_player_view.stop()
                    self.current_player_view = None

                logger.debug(f"{log_prefix} Creating new MusicPlayerView.")
                try:
                    self.current_player_view = MusicPlayerView(music_cog, self.guild_id)
                    logger.debug(f"{log_prefix} New view created. Updating message in channel.")
                    # --- Call to _update_player_message (posts/edits in channel) ---
                    await self._update_player_message(embed=now_playing_embed, view=self.current_player_view, content=None)
                    logger.debug(f"{log_prefix} _update_player_message call finished. Current msg ID: {self.current_player_message_id}")
                except Exception as e_view:
                    logger.error(f"{log_prefix} Failed to create or update player view: {e_view}", exc_info=True)
                    self.current_player_view = None
                    await self._update_player_message(embed=now_playing_embed, view=None, content=None) # Update channel message without view


            except (nextcord.errors.ClientException, ValueError, TypeError) as e:
                logger.error(f"{log_prefix} Playback error (Client/Value/Type) for '{song_to_play.title}': {e}", exc_info=False)
                # Use _notify_channel_error for background errors
                await self._notify_channel_error(f"Error playing '{song_to_play.title}'. Skipping.")
                async with self._lock: self.current_song = None
            except Exception as e:
                logger.error(f"{log_prefix} Unexpected error during playback setup for '{song_to_play.title}': {e}", exc_info=True)
                await self._notify_channel_error(f"An unexpected error occurred while trying to play '{song_to_play.title}'. Skipping.")
                async with self._lock: self.current_song = None

            # --- Wait for Song End ---
            if play_success:
                logger.debug(f"{log_prefix} Waiting for play_next_song event (song '{song_to_play.title}' is playing)...")
                await self.play_next_song.wait()
                logger.debug(f"{log_prefix} Event received for '{song_to_play.title}'.")
            else:
                logger.debug(f"{log_prefix} Playback setup failed, continuing loop shortly.")
                await asyncio.sleep(0.1)


    def _handle_after_play(self, error: Optional[Exception]):
        """Callback executed after a song finishes playing or errors during playback."""
        log_prefix = f"[Guild {self.guild_id}] AfterPlayCallback:"
        if error:
            logger.error(f"{log_prefix} Playback error reported: {error!r}", exc_info=error)
            # Schedule notification (goes to channel via _notify_channel_error)
            # Could try DMing requester here if needed:
            # user = self.current_song.requester if self.current_song else None
            # asyncio.run_coroutine_threadsafe(_send_dm_or_log(user, f"Playback error occurred for '{self.current_song.title}': {error}. Skip."), self.bot.loop)
            # Sticking to channel notification for now:
            asyncio.run_coroutine_threadsafe(self._notify_channel_error(f"Playback error occurred: {error}. Skipping to next."), self.bot.loop)
        else:
            logger.debug(f"{log_prefix} Song finished successfully.")

        logger.debug(f"{log_prefix} Setting play_next_song event.")
        self.bot.loop.call_soon_threadsafe(self.play_next_song.set)


    def start_playback_loop(self):
        """Starts the playback loop task if it's not already running."""
        # (Keep this function as is)
        log_prefix = f"[Guild {self.guild_id}]"
        if self._playback_task is None or self._playback_task.done():
            logger.info(f"{log_prefix} Starting playback loop task.")
            self._playback_task = self.bot.loop.create_task(self._playback_loop())
            self._playback_task.add_done_callback(self._handle_loop_completion)
        else:
            logger.debug(f"{log_prefix} Playback loop task is already running.")

        if self.queue and not self.play_next_song.is_set():
             if self.voice_client and self.voice_client.is_connected() and not self.voice_client.is_playing() and not self.voice_client.is_paused():
                 logger.debug(f"{log_prefix} Setting play_next_song event (queue not empty, VC idle).")
                 self.play_next_song.set()

    def _handle_loop_completion(self, task: asyncio.Task):
        """Callback executed when the playback loop task finishes."""
        # (Keep this function as is)
        guild_id = self.guild_id
        log_prefix = f"[Guild {guild_id}] LoopCompletion:"
        try:
            if task.cancelled():
                logger.info(f"{log_prefix} Playback loop task was cancelled.")
            elif task.exception():
                exc = task.exception()
                logger.error(f"{log_prefix} Playback loop task failed with exception:", exc_info=exc)
                # Notify channel about the loop error
                error_message = f"Music playback loop encountered an error: {exc}. Please try playing again."
                asyncio.run_coroutine_threadsafe(self._notify_channel_error(error_message), self.bot.loop)
                # Schedule cleanup
                self.bot.loop.create_task(self.cleanup())
            else:
                logger.info(f"{log_prefix} Playback loop task finished gracefully.")
        except Exception as e:
            logger.error(f"{log_prefix} Error within _handle_loop_completion itself: {e}", exc_info=True)

        cog_getter = getattr(self.bot, 'get_cog', lambda n: None)
        cog = cog_getter("Music")
        if cog and guild_id in cog.guild_states and cog.guild_states[guild_id] is self:
             if self._playback_task is task:
                self._playback_task = None
                logger.debug(f"{log_prefix} Playback task reference cleared.")
        else:
            logger.debug(f"{log_prefix} State or Cog no longer exists or task mismatch; task reference not cleared from this instance.")


    async def stop_playback(self):
        """Stops the current song, clears the queue, and resets state."""
        # (Keep this function as is - it updates the message in the channel)
        log_prefix = f"[Guild {self.guild_id}] StopPlayback:"
        logger.info(f"{log_prefix} Initiating stop.")

        view_to_stop = None
        message_id_to_clear = None

        async with self._lock:
            self.queue.clear()
            logger.debug(f"{log_prefix} Queue cleared.")

            vc = self.voice_client
            if vc and vc.is_connected() and (vc.is_playing() or vc.is_paused()):
                logger.info(f"{log_prefix} Stopping voice client playback.")
                vc.stop()

            self.current_song = None
            logger.debug(f"{log_prefix} Current song cleared.")

            view_to_stop = self.current_player_view
            message_id_to_clear = self.current_player_message_id

            self.current_player_view = None
            self.current_player_message_id = None

            if not self.play_next_song.is_set():
                logger.debug(f"{log_prefix} Setting play_next_song event to prevent loop waiting.")
                self.play_next_song.set()

        if view_to_stop and not view_to_stop.is_finished():
            logger.debug(f"{log_prefix} Stopping player view instance.")
            view_to_stop.stop()

            for item in view_to_stop.children:
                if isinstance(item, nextcord.ui.Button): item.disabled = True

            if message_id_to_clear and self.last_command_channel_id:
                logger.debug(f"{log_prefix} Scheduling player message update to show stopped state.")
                # Updates message in channel
                self.bot.loop.create_task(self._update_player_message(content="*Playback stopped.*", embed=None, view=view_to_stop))
            else:
                 logger.debug(f"{log_prefix} No message ID or channel to update for stopped state.")


    async def cleanup(self):
        """Comprehensive cleanup: stops playback, cancels loop, disconnects VC, resets state."""
        # (Keep this function as is)
        guild_id = self.guild_id
        log_prefix = f"[Guild {guild_id}] Cleanup:"
        logger.info(f"{log_prefix} Starting cleanup process.")

        await self.stop_playback() # Updates channel message

        task = self._playback_task
        if task and not task.done():
            logger.info(f"{log_prefix} Cancelling playback loop task.")
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
                logger.debug(f"{log_prefix} Playback loop task cancellation processed.")
            except asyncio.CancelledError:
                logger.debug(f"{log_prefix} Playback loop task successfully cancelled.")
            except asyncio.TimeoutError:
                logger.warning(f"{log_prefix} Timeout waiting for playback loop task to cancel.")
            except Exception as e:
                logger.error(f"{log_prefix} Error occurred while awaiting loop task cancellation: {e}", exc_info=True)
        self._playback_task = None

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
        self.current_player_view = None
        self.current_player_message_id = None

        logger.info(f"{log_prefix} Cleanup finished.")


    async def _notify_channel_error(self, message: str):
        """Sends an error message embed to the last used command channel."""
        # --- THIS FUNCTION REMAINS UNCHANGED --- Sends to channel.
        channel_id = self.last_command_channel_id
        guild_id = self.guild_id
        if not channel_id:
            logger.warning(f"[Guild {guild_id}] Cannot send error notification: No command channel ID stored.")
            return

        try:
            channel = self.bot.get_channel(channel_id)
            if channel and isinstance(channel, nextcord.abc.Messageable):
                embed = nextcord.Embed(title="Music Error", description=message, color=nextcord.Color.red())
                await channel.send(embed=embed, delete_after=30.0)
                logger.debug(f"[Guild {guild_id}] Sent error notification to channel {channel_id}.")
            else:
                logger.warning(f"[Guild {guild_id}] Cannot find channel {channel_id} to send error notification.")
        except nextcord.Forbidden:
             logger.error(f"[Guild {guild_id}] Lacking permissions to send error notification in channel {channel_id}.")
        except Exception as e:
            logger.error(f"[Guild {guild_id}] Failed to send error notification: {e}", exc_info=True)
# --- End GuildMusicState ---


# --- Music Cog ---
class MusicCog(commands.Cog, name="Music"):
    """Commands for playing music in voice channels."""

    def __init__(self, bot: commands.Bot):
        self.bot: commands.Bot = bot
        self.guild_states: dict[int, GuildMusicState] = {}
        try:
            self.ydl = yt_dlp.YoutubeDL(YDL_OPTS)
        except Exception as e:
             logger.critical(f"Failed to initialize YoutubeDL: {e}", exc_info=True)
             raise RuntimeError("YoutubeDL failed to initialize, MusicCog cannot function.") from e

    def get_guild_state(self, guild_id: int) -> GuildMusicState:
        """Gets or creates the GuildMusicState for a guild."""
        # (Keep as is)
        if guild_id not in self.guild_states:
            logger.info(f"[Guild {guild_id}] Creating new GuildMusicState.")
            self.guild_states[guild_id] = GuildMusicState(self.bot, guild_id)
        return self.guild_states[guild_id]

    async def build_queue_embed(self, state: GuildMusicState) -> Optional[nextcord.Embed]:
         """Builds the queue information embed."""
         # (Keep this function as is)
         log_prefix = f"[Guild {state.guild_id}] QueueEmbed:"
         logger.debug(f"{log_prefix} Building queue embed.")

         async with state._lock:
             current_song = state.current_song
             queue_copy = list(state.queue)

         if not current_song and not queue_copy:
             logger.debug(f"{log_prefix} Queue and current song are empty.")
             return None

         embed = nextcord.Embed(title="Queue", color=nextcord.Color.blurple())
         now_playing_value = "Nothing playing."
         queue_duration_secs = 0

         if current_song:
             player_icon = "❓"
             if state.voice_client and state.voice_client.is_connected():
                 if state.voice_client.is_playing(): player_icon = "▶️ Playing"
                 elif state.voice_client.is_paused(): player_icon = "⏸️ Paused"
                 else: player_icon = "⏹️ Idle"

             requester_mention = current_song.requester.mention if current_song.requester else "Unknown"
             now_playing_value = (
                 f"{player_icon}: **[{current_song.title}]({current_song.webpage_url})** "
                 f"`[{current_song.format_duration()}]` Req: {requester_mention}"
             )
         embed.add_field(name="Now Playing", value=now_playing_value, inline=False)

         if queue_copy:
             queue_lines = []
             current_length = 0
             char_limit = 950
             max_list_display = 15
             songs_shown = 0

             for i, song in enumerate(queue_copy):
                 if song.duration:
                     try: queue_duration_secs += int(song.duration)
                     except (ValueError, TypeError): pass

                 if songs_shown < max_list_display:
                     requester_name = song.requester.display_name if song.requester else "Unknown"
                     line = (
                         f"`{i + 1}.` [{song.title}]({song.webpage_url}) "
                         f"`[{song.format_duration()}]` R: {requester_name}\n"
                     )
                     if current_length + len(line) <= char_limit:
                         queue_lines.append(line)
                         current_length += len(line)
                         songs_shown += 1
                     else:
                         remaining_count = len(queue_copy) - i
                         if remaining_count > 0:
                              queue_lines.append(f"\n*...and {remaining_count} more.*")
                         break

             if songs_shown == max_list_display and len(queue_copy) > max_list_display:
                 remaining_count = len(queue_copy) - max_list_display
                 queue_lines.append(f"\n*...and {remaining_count} more.*")

             total_duration_str = Song(None, None, None, queue_duration_secs, None).format_duration() if queue_duration_secs > 0 else "N/A"
             queue_header = f"Up Next ({len(queue_copy)} song{'s' if len(queue_copy) != 1 else ''}, Total: {total_duration_str})"

             queue_value = "".join(queue_lines).strip()
             if not queue_value and len(queue_copy) > 0:
                 queue_value = f"{len(queue_copy)} songs in queue..."

             if queue_value:
                 embed.add_field(name=queue_header, value=queue_value, inline=False)
             else:
                 embed.add_field(name="Up Next", value="Queue is empty.", inline=False)
         else:
             embed.add_field(name="Up Next", value="Queue is empty.", inline=False)

         total_songs = len(queue_copy) + (1 if current_song else 0)
         volume_percent = int(state.volume * 100)
         embed.set_footer(text=f"Total Songs: {total_songs} | Volume: {volume_percent}%")

         logger.debug(f"{log_prefix} Embed built successfully.")
         return embed

    # --- Extraction Methods ---
    # (_process_entry and _extract_info should be the versions that fixed stream extraction)
    async def _process_entry(self, entry_data: dict, requester: nextcord.Member) -> Optional[Song]:
        """Processes a single entry from yt-dlp result, potentially re-extracting and processing if needed."""
        bot_id = self.bot.user.id if self.bot.user else 'Bot'
        log_prefix = f"[{bot_id}] EntryProcessing:"

        if not entry_data:
            logger.warning(f"{log_prefix} Received empty entry data.")
            return None
        title = entry_data.get('title', entry_data.get('id', 'N/A'))

        if entry_data.get('_type') == 'url' and 'url' in entry_data and 'formats' not in entry_data and 'entries' not in entry_data:
            logger.debug(f"{log_prefix} Flat entry detected for '{title}'. Re-extracting with processing.")
            try:
                loop = asyncio.get_event_loop()
                ydl_opts_single = YDL_OPTS.copy()
                ydl_opts_single['noplaylist'] = True
                ydl_opts_single['extract_flat'] = False
                ydl_single = yt_dlp.YoutubeDL(ydl_opts_single)
                partial_extract = functools.partial(ydl_single.extract_info, entry_data['url'], download=False)
                full_entry_data = await loop.run_in_executor(None, partial_extract)
                if not full_entry_data:
                    logger.warning(f"{log_prefix} Re-extraction failed for URL: {entry_data['url']}")
                    return None
                entry_data = full_entry_data
                title = entry_data.get('title', entry_data.get('id', 'N/A'))
                logger.debug(f"{log_prefix} Re-extraction successful for '{title}'.")
            except Exception as e:
                logger.error(f"{log_prefix} Error during re-extraction for '{title}': {e}", exc_info=True)
                return None

        processed_data = None
        try:
             logger.debug(f"{log_prefix} Running process_ie_result for '{title}'...")
             processed_data = self.ydl.process_ie_result(entry_data, download=False)
             if not processed_data:
                  logger.warning(f"{log_prefix} process_ie_result returned None for '{title}'.")
                  return None
             logger.debug(f"{log_prefix} process_ie_result completed.")
        except Exception as process_err:
             logger.error(f"{log_prefix} Error during process_ie_result for '{title}': {process_err}", exc_info=True)
             return None

        logger.debug(f"{log_prefix} Searching for stream URL in processed data for: '{title}'")
        stream_url = None
        entry_to_search = processed_data

        if 'url' in entry_to_search and entry_to_search.get('protocol') in ('http', 'https') and entry_to_search.get('acodec') != 'none':
            stream_url = entry_to_search['url']
            logger.debug(f"{log_prefix} Using pre-selected stream URL from processed data.")
        elif 'formats' in entry_to_search:
            formats = entry_to_search.get('formats', [])
            best_format = None
            audio_preference = ['opus', 'aac', 'vorbis', 'mp4a', 'mp3']
            for codec in audio_preference:
                for f in formats:
                    if (f.get('url') and f.get('protocol') in ('https', 'http') and f.get('acodec') == codec and f.get('vcodec') == 'none'):
                        best_format = f; logger.debug(f"{log_prefix} Found preferred audio-only format: {codec} (ID: {f.get('format_id', 'N/A')})"); break
                if best_format: break
            if not best_format:
                for f in formats:
                    format_id = f.get('format_id', '').lower(); format_note = f.get('format_note', '').lower()
                    if (('bestaudio' in format_id or 'bestaudio' in format_note) and f.get('url') and f.get('protocol') in ('https', 'http') and f.get('acodec') != 'none'):
                         best_format = f; logger.debug(f"{log_prefix} Found format marked 'bestaudio' (ID: {f.get('format_id', 'N/A')})."); break
            if not best_format:
                for f in formats:
                    if (f.get('url') and f.get('protocol') in ('https', 'http') and f.get('acodec') != 'none' and f.get('vcodec') == 'none'):
                        best_format = f; logger.debug(f"{log_prefix} Using fallback audio-only format (ID: {f.get('format_id', 'N/A')})."); break
            if not best_format:
                for f in formats:
                     if (f.get('url') and f.get('protocol') in ('https', 'http') and f.get('acodec') != 'none'):
                         best_format = f; logger.warning(f"{log_prefix} Using last resort format (might include video) (ID: {f.get('format_id', 'N/A')})."); break
            if best_format:
                stream_url = best_format.get('url')
                logger.debug(f"{log_prefix} Selected stream URL from format ID {best_format.get('format_id', 'N/A')}.")
            else: logger.warning(f"{log_prefix} No suitable HTTP/S audio stream format found for '{title}'.")
        elif 'requested_formats' in entry_to_search and not stream_url:
             req_formats = entry_to_search.get('requested_formats')
             if req_formats:
                 fmt = req_formats[0]
                 if fmt.get('url') and fmt.get('protocol') in ('https', 'http'):
                     stream_url = fmt.get('url'); logger.debug(f"{log_prefix} Using stream URL from 'requested_formats'.")

        logger.debug(f"{log_prefix} Final stream URL found: {'Yes' if stream_url else 'No'}")
        if not stream_url:
            logger.warning(f"{log_prefix} Could not determine a stream URL for '{title}'. Skipping entry.")
            return None
        try:
            webpage_url = processed_data.get('webpage_url') or processed_data.get('original_url', 'N/A')
            duration_sec = processed_data.get('duration')
            duration_int: Optional[int] = None
            if duration_sec is not None:
                try: duration_int = int(duration_sec)
                except (ValueError, TypeError): duration_int = None
            song = Song(source_url=stream_url, title=processed_data.get('title', 'Unknown Title'), webpage_url=webpage_url, duration=duration_int, requester=requester)
            logger.debug(f"{log_prefix} Successfully created Song object for: {song.title}")
            return song
        except Exception as e:
            logger.error(f"{log_prefix} Error creating Song object for '{title}': {e}", exc_info=True)
            return None

    async def _extract_info(self, query: str, requester: nextcord.Member) -> tuple[Optional[str], List[Song]]:
        """Extracts info using yt-dlp, handling playlists and single videos."""
        bot_id = self.bot.user.id if self.bot.user else 'Bot'
        log_prefix = f"[{bot_id}] YTDLExtraction:"
        logger.info(f"{log_prefix} Starting extraction for query: '{query}' (Requester: {requester.name})")
        songs_found: List[Song] = []
        playlist_title: Optional[str] = None
        error_code: Optional[str] = None
        try:
            loop = asyncio.get_event_loop()
            partial_extract_initial = functools.partial(self.ydl.extract_info, query, download=False, process=False)
            initial_data = await loop.run_in_executor(None, partial_extract_initial)
            if not initial_data:
                logger.warning(f"{log_prefix} Initial extraction returned no data for query: {query}")
                return "err_nodata", []
            if 'entries' in initial_data and initial_data.get('entries'):
                playlist_title = initial_data.get('title', 'Unknown Playlist')
                entries = initial_data['entries']
                logger.info(f"{log_prefix} Detected playlist: '{playlist_title}' with {len(entries)} potential entries. Processing...")
                processed_count = 0
                original_count = len(entries)
                for entry in entries:
                    if entry:
                        song = await self._process_entry(entry, requester)
                        if song:
                            songs_found.append(song)
                            processed_count += 1
                        else: logger.warning(f"{log_prefix} Failed to process playlist entry: {entry.get('title', entry.get('id', 'Unknown ID'))}")
                    else: original_count -= 1
                logger.info(f"{log_prefix} Playlist processing finished. Added {processed_count}/{original_count} valid songs.")
                if not songs_found: error_code = "err_playlist_empty_or_fail"
            else:
                logger.info(f"{log_prefix} Detected single entry. Processing directly...")
                song = await self._process_entry(initial_data, requester)
                if song:
                    songs_found.append(song)
                    logger.info(f"{log_prefix} Successfully processed single entry: {song.title}")
                else:
                    logger.warning(f"{log_prefix} Failed to process single entry.")
                    error_code = "err_process_single_failed"

            if error_code: return error_code, []
            else: return playlist_title, songs_found
        except yt_dlp.utils.DownloadError as e:
            error_message = str(e).lower(); logger.error(f"{log_prefix} DownloadError during extraction: {e}")
            err_type = 'download_generic'
            if "unsupported url" in error_message: err_type = 'unsupported'
            elif "video unavailable" in error_message: err_type = 'unavailable'
            elif "private video" in error_message: err_type = 'private'
            elif "age restricted" in error_message: err_type = 'age_restricted'
            elif "could not extract" in error_message: err_type = 'extract_failed'
            elif "network error" in error_message or "webpage" in error_message: err_type = 'network'
            return f"err_{err_type}", []
        except Exception as e:
            logger.error(f"{log_prefix} Unexpected error during extraction: {e}", exc_info=True)
            return "err_extraction_unexpected", []
    # --- End Extraction Methods ---


    # --- Listener ---
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: nextcord.Member, before: nextcord.VoiceState, after: nextcord.VoiceState):
        """Handles voice state changes, like bot disconnects or users leaving/joining."""
        # (Keep this listener as is)
        if not member.guild: return
        guild_id = member.guild.id
        state = self.guild_states.get(guild_id)
        if not state or not state.voice_client: return

        bot_voice_channel = state.voice_client.channel if state.voice_client.is_connected() else None
        log_prefix = f"[Guild {guild_id}] VoiceStateUpdate:"

        if member.id == self.bot.user.id:
            if before.channel and not after.channel:
                logger.warning(f"{log_prefix} Bot was disconnected from voice channel {before.channel.name}.")
                await state.cleanup()
                if guild_id in self.guild_states:
                    del self.guild_states[guild_id]; logger.info(f"{log_prefix} GuildMusicState removed.")
            elif before.channel and after.channel and before.channel != after.channel:
                logger.info(f"{log_prefix} Bot moved from {before.channel.name} to {after.channel.name}.")
                if state.voice_client: state.voice_client.channel = after.channel
            elif not before.channel and after.channel:
                 logger.info(f"{log_prefix} Bot joined voice channel {after.channel.name}.")
        elif bot_voice_channel:
            user_left_bot_channel = before.channel == bot_voice_channel and after.channel != bot_voice_channel
            user_joined_bot_channel = before.channel != bot_voice_channel and after.channel == bot_voice_channel
            current_human_members = [m for m in bot_voice_channel.members if not m.bot]
            is_bot_alone = len(current_human_members) == 0

            if user_left_bot_channel and is_bot_alone:
                logger.info(f"{log_prefix} Last user left ({member.name}). Bot is alone in {bot_voice_channel.name}. Pausing.")
                if state.voice_client.is_playing():
                    state.voice_client.pause()
                    if state.current_player_view:
                        state.current_player_view._update_buttons()
                        self.bot.loop.create_task(state._update_player_message(view=state.current_player_view))
            elif user_joined_bot_channel and state.voice_client.is_paused() and len(current_human_members) > 0:
                 logger.info(f"{log_prefix} User {member.name} joined. Resuming playback.")
                 state.voice_client.resume()
                 if state.current_player_view:
                     state.current_player_view._update_buttons()
                     self.bot.loop.create_task(state._update_player_message(view=state.current_player_view))
    # --- End Listener ---


    # --- Commands ---
    @commands.command(name='play', aliases=['p'], help="Plays a song or adds it/playlist to the queue.")
    @commands.guild_only()
    async def play_command(self, ctx: commands.Context, *, query: str):
        """Plays audio from a URL or search query, or adds a playlist."""
        if not ctx.guild: return
        state = self.get_guild_state(ctx.guild.id)
        state.last_command_channel_id = ctx.channel.id
        log_prefix = f"[Guild {ctx.guild.id}] PlayCmd:"
        logger.info(f"{log_prefix} Received play command for '{query}' from {ctx.author.name}")

        # --- Ensure Bot is Connected ---
        if not state.voice_client or not state.voice_client.is_connected():
            if ctx.author.voice and ctx.author.voice.channel:
                logger.info(f"{log_prefix} Bot not connected. Attempting to join {ctx.author.voice.channel.name}.")
                try:
                    # Use join_command directly (it now sends DMs for feedback)
                    await self.join_command(ctx)
                    state = self.guild_states.get(ctx.guild.id)
                    if not state or not state.voice_client or not state.voice_client.is_connected():
                        logger.warning(f"{log_prefix} Failed to join voice channel after automatic attempt.")
                        return
                    else:
                         logger.info(f"{log_prefix} Successfully joined voice channel.")
                         state.last_command_channel_id = ctx.channel.id
                except Exception as e:
                     logger.error(f"{log_prefix} Error occurred invoking join command: {e}", exc_info=True)
                     await _send_dm_or_log(ctx.author, "An error occurred while trying to join the voice channel.")
                     return
            else:
                # DM the user they need to be in VC
                await _send_dm_or_log(ctx.author, "You need to be in a voice channel for me to join.")
                return
        # --- Ensure User is in the Same VC ---
        elif not ctx.author.voice or ctx.author.voice.channel != state.voice_client.channel:
             # DM the user they need to be in same VC
             await _send_dm_or_log(ctx.author, f"You need to be in the same voice channel as me ({state.voice_client.channel.mention}).")
             return

        # --- Extract Info ---
        await ctx.trigger_typing()
        playlist_title: Optional[str] = None
        songs_to_add: List[Song] = []
        error_code: Optional[str] = None
        try:
            result_tuple = await self._extract_info(query, ctx.author)
            error_code, songs_found_or_title = result_tuple[0], result_tuple[1]
            if isinstance(error_code, str) and error_code.startswith("err_"): songs_to_add = []
            else: playlist_title = error_code; songs_to_add = songs_found_or_title; error_code = None
        except Exception as e:
            logger.error(f"{log_prefix} Unexpected exception during _extract_info call: {e}", exc_info=True)
            error_code = "err_internal_extract"

        # --- Handle Extraction Errors ---
        if error_code:
            error_map = { # Same error map as before
                'nodata': "Could not find any data for your query.",
                'playlist_empty_or_fail': f"Could not add any songs from the playlist '{playlist_title}'. They might be unavailable or private.",
                'process_single_failed': "Failed to process the requested track. It might be unsupported or unavailable.",
                'unsupported': "This URL or video format is not supported.",
                'unavailable': "This video is unavailable.", 'private': "This video is private.",
                'age_restricted': "This video is age-restricted.", 'extract_failed': "Failed to extract information for this item.",
                'network': "A network error occurred while fetching information.", 'download_generic': "An error occurred while trying to access the media.",
                'extraction_unexpected': "An unexpected error occurred during information extraction.",
                'internal_extract': "An internal error occurred while processing your request."
            }
            error_message = error_map.get(error_code.replace("err_", ""), "An unknown error occurred during track lookup.")
            logger.warning(f"{log_prefix} Extraction failed. Code: {error_code}")
            # Send error via DM
            await _send_dm_or_log(ctx.author, error_message)
            return

        if not songs_to_add:
            logger.warning(f"{log_prefix} Extraction succeeded but found no playable songs for query: {query}")
            # Send feedback via DM
            await _send_dm_or_log(ctx.author, f"Couldn't find any playable songs for '{query}'.")
            return

        # --- Add Songs to Queue ---
        logger.debug(f"{log_prefix} Extracted {len(songs_to_add)} songs.")
        added_count = 0; start_position = 0; was_queue_empty = False
        async with state._lock:
            was_queue_empty = not state.queue and not state.current_song
            start_position = len(state.queue) + (1 if state.current_song else 0) + 1
            state.queue.extend(songs_to_add)
            added_count = len(songs_to_add)
            logger.info(f"{log_prefix} Added {added_count} songs. New queue length: {len(state.queue)}")

        # --- Send Feedback ---
        if added_count > 0:
            try:
                if not was_queue_empty: # Queue wasn't empty, send DM confirmation
                    feedback_embed = nextcord.Embed(color=nextcord.Color.blue())
                    first_song = songs_to_add[0]
                    if playlist_title and added_count > 1:
                        feedback_embed.title = "Playlist Queued"
                        playlist_link = query if query.startswith('http') else None
                        playlist_desc = f"**[{playlist_title}]({playlist_link})**" if playlist_link else f"**{playlist_title}**"
                        feedback_embed.description = f"Added **{added_count}** songs from {playlist_desc} to the server queue."
                    elif added_count == 1:
                        feedback_embed.title = "Added to Queue"
                        feedback_embed.description = f"[{first_song.title}]({first_song.webpage_url})"
                        feedback_embed.add_field(name="Position", value=f"#{start_position}", inline=True)
                        feedback_embed.add_field(name="Duration", value=first_song.format_duration(), inline=True)
                    else:
                         feedback_embed.title = "Songs Queued"
                         feedback_embed.description = f"Added **{added_count}** songs to the server queue."

                    requester_name = ctx.author.display_name
                    requester_icon = ctx.author.display_avatar.url if ctx.author.display_avatar else None
                    feedback_embed.set_footer(text=f"Requested by {requester_name}", icon_url=requester_icon)
                    # --- Send feedback embed via DM ---
                    await _send_dm_or_log(ctx.author, embed=feedback_embed)
                else:
                    # If queue was empty, just react to the command message in channel
                    await ctx.message.add_reaction('✅')
            except Exception as e:
                logger.error(f"{log_prefix} Failed to send feedback DM/reaction: {e}", exc_info=True)

        # --- Ensure Playback Starts/Continues ---
        if added_count > 0:
            logger.debug(f"{log_prefix} Ensuring playback loop is running.")
            state.start_playback_loop()

        logger.debug(f"{log_prefix} Play command finished processing.")


    @commands.command(name='join', aliases=['connect', 'j'], help="Connects the bot to your current voice channel.")
    @commands.guild_only()
    async def join_command(self, ctx: commands.Context):
        """Connects the bot to the voice channel the command invoker is in."""
        if not ctx.guild: return
        state = self.get_guild_state(ctx.guild.id)
        state.last_command_channel_id = ctx.channel.id

        if not ctx.author.voice or not ctx.author.voice.channel:
            # Send DM
            await _send_dm_or_log(ctx.author, "You need to be in a voice channel for me to join.")
            return

        target_channel = ctx.author.voice.channel
        log_prefix = f"[Guild {ctx.guild.id}] JoinCmd:"

        async with state._lock:
            current_vc = state.voice_client
            if current_vc and current_vc.is_connected():
                if current_vc.channel == target_channel:
                    # Send DM
                    await _send_dm_or_log(ctx.author, f"I'm already in {target_channel.mention}.")
                else:
                    try:
                        await current_vc.move_to(target_channel)
                        # Send DM
                        await _send_dm_or_log(ctx.author, f"Moved to {target_channel.mention}.")
                        logger.info(f"{log_prefix} Moved VC to {target_channel.name}")
                    except asyncio.TimeoutError:
                         logger.error(f"{log_prefix} Timeout moving VC to {target_channel.name}")
                         # Send DM
                         await _send_dm_or_log(ctx.author, "Timed out trying to move channels.")
                    except Exception as e:
                        logger.error(f"{log_prefix} Error moving VC to {target_channel.name}: {e}", exc_info=True)
                        # Send DM
                        await _send_dm_or_log(ctx.author, f"Couldn't move to your channel: {e}")
            else:
                try:
                    logger.info(f"{log_prefix} Attempting to connect to {target_channel.name}")
                    state.voice_client = await target_channel.connect()
                    # Send DM
                    await _send_dm_or_log(ctx.author, f"Connected to {target_channel.mention}.")
                    logger.info(f"{log_prefix} Successfully connected.")
                    state.start_playback_loop()
                except asyncio.TimeoutError:
                    logger.error(f"{log_prefix} Timeout connecting to {target_channel.name}")
                    # Send DM
                    await _send_dm_or_log(ctx.author, f"Timed out trying to connect to {target_channel.mention}.")
                    if ctx.guild.id in self.guild_states: del self.guild_states[ctx.guild.id]
                except nextcord.errors.ClientException as e:
                     logger.error(f"{log_prefix} ClientException connecting to {target_channel.name}: {e}", exc_info=True)
                     # Send DM
                     await _send_dm_or_log(ctx.author, f"Error connecting: {e}")
                     if ctx.guild.id in self.guild_states: del self.guild_states[ctx.guild.id]
                except Exception as e:
                    logger.error(f"{log_prefix} Unexpected error connecting to {target_channel.name}: {e}", exc_info=True)
                    # Send DM
                    await _send_dm_or_log(ctx.author, "An unexpected error occurred while trying to connect.")
                    if ctx.guild.id in self.guild_states: del self.guild_states[ctx.guild.id]


    @commands.command(name='leave', aliases=['disconnect', 'dc', 'stopbot'], help="Disconnects the bot from voice and clears the queue.")
    @commands.guild_only()
    async def leave_command(self, ctx: commands.Context):
        """Disconnects the bot, stops playback, and clears state."""
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)
        log_prefix = f"[Guild {ctx.guild.id}] LeaveCmd:"

        if not state or not state.voice_client or not state.voice_client.is_connected():
            # Send DM if trying to leave when not connected
            await _send_dm_or_log(ctx.author, "I'm not connected to a voice channel.")
            return

        logger.info(f"{log_prefix} Received leave command from {ctx.author.name}.")
        await ctx.message.add_reaction('👋') # Keep reaction in channel

        await state.cleanup() # Cleanup handles stopping etc.

        if ctx.guild.id in self.guild_states:
            del self.guild_states[ctx.guild.id]
            logger.info(f"{log_prefix} GuildMusicState removed after cleanup.")
        # Maybe send a confirmation DM? Optional.
        # await _send_dm_or_log(ctx.author, "Disconnected from the voice channel.")


    @commands.command(name='skip', aliases=['s', 'next'], help="Skips the current song.")
    @commands.guild_only()
    async def skip_command(self, ctx: commands.Context):
        """Skips the currently playing song."""
        # Keep using reaction for low spam
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)

        if not state or not state.voice_client or not state.voice_client.is_connected():
            await _send_dm_or_log(ctx.author, "I'm not connected or playing anything.")
            return
        vc = state.voice_client
        if not vc.is_playing() and not vc.is_paused():
            await _send_dm_or_log(ctx.author, "Nothing is currently playing to skip.")
            return

        logger.info(f"[Guild {ctx.guild.id}] Skip command received from {ctx.author.name}.")
        vc.stop()
        await ctx.message.add_reaction('⏭️')


    @commands.command(name='stop', help="Stops playback completely and clears the queue.")
    @commands.guild_only()
    async def stop_command(self, ctx: commands.Context):
        """Stops the player and clears the song queue."""
        # Keep using reaction for low spam
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)

        if not state or not state.voice_client or not state.voice_client.is_connected():
            await _send_dm_or_log(ctx.author, "I'm not connected or playing anything.")
            return
        if not state.current_song and not state.queue:
            await _send_dm_or_log(ctx.author, "Nothing to stop - the player is idle and the queue is empty.")
            return

        logger.info(f"[Guild {ctx.guild.id}] Stop command received from {ctx.author.name}.")
        await state.stop_playback() # Handles stopping VC, clearing queue, updating channel message
        await ctx.message.add_reaction('⏹️')
        # Confirmation DM is optional, as main player message updates
        # await _send_dm_or_log(ctx.author, "Playback stopped and queue cleared.")


    @commands.command(name='pause', help="Pauses the current song.")
    @commands.guild_only()
    async def pause_command(self, ctx: commands.Context):
        """Pauses the currently playing song."""
        # Keep using reaction for low spam
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected():
            await _send_dm_or_log(ctx.author, "I'm not connected or playing anything.")
            return
        vc = state.voice_client
        if vc.is_paused():
            await _send_dm_or_log(ctx.author, "Playback is already paused.")
            return
        if not vc.is_playing():
            await _send_dm_or_log(ctx.author, "Nothing is currently playing to pause.")
            return

        vc.pause()
        logger.info(f"[Guild {ctx.guild.id}] Pause command received from {ctx.author.name}.")
        await ctx.message.add_reaction('⏸️')
        if state.current_player_view: # Update buttons in channel message
            state.current_player_view._update_buttons()
            await state._update_player_message(view=state.current_player_view)


    @commands.command(name='resume', aliases=['unpause'], help="Resumes the paused song.")
    @commands.guild_only()
    async def resume_command(self, ctx: commands.Context):
        """Resumes playback if it was paused."""
        # Keep using reaction for low spam
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected():
            await _send_dm_or_log(ctx.author, "I'm not connected.")
            return
        vc = state.voice_client
        if vc.is_playing():
            await _send_dm_or_log(ctx.author, "Playback is already playing.")
            return
        if not vc.is_paused():
            await _send_dm_or_log(ctx.author, "Nothing is currently paused.")
            return

        vc.resume()
        logger.info(f"[Guild {ctx.guild.id}] Resume command received from {ctx.author.name}.")
        await ctx.message.add_reaction('▶️')
        if state.current_player_view: # Update buttons in channel message
            state.current_player_view._update_buttons()
            await state._update_player_message(view=state.current_player_view)


    @commands.command(name='queue', aliases=['q', 'nowplaying', 'np'], help="Shows the current song queue.")
    @commands.guild_only()
    async def queue_command(self, ctx: commands.Context):
         """Displays the current queue and now playing information."""
         # Keep sending queue to channel
         if not ctx.guild: return
         state = self.guild_states.get(ctx.guild.id)
         if not state:
             await ctx.send("The music player is not active in this server.") # Keep in channel
             return

         state.last_command_channel_id = ctx.channel.id
         embed = await self.build_queue_embed(state)
         if embed:
             await ctx.send(embed=embed) # Keep in channel
         else:
             await ctx.send("The queue is empty and nothing is currently playing.") # Keep in channel


    @commands.command(name='volume', aliases=['vol'], help="Changes the player volume (0-100).")
    @commands.guild_only()
    async def volume_command(self, ctx: commands.Context, *, volume: int):
        """Sets the playback volume."""
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected():
            await _send_dm_or_log(ctx.author, "I'm not connected to voice.") # Send DM
            return
        if not 0 <= volume <= 100:
            await _send_dm_or_log(ctx.author, "Please provide a volume level between 0 and 100.") # Send DM
            return

        new_volume_float = volume / 100.0
        state.volume = new_volume_float

        if state.voice_client.source and isinstance(state.voice_client.source, nextcord.PCMVolumeTransformer):
            state.voice_client.source.volume = new_volume_float
            # Send DM confirmation
            await _send_dm_or_log(ctx.author, f"Volume set to **{volume}%**.")
        else:
             # Send DM confirmation
             await _send_dm_or_log(ctx.author, f"Volume set to **{volume}%**. It will apply to the next song.")
        logger.info(f"[Guild {ctx.guild.id}] Volume set to {volume}% by {ctx.author.name}.")


    # --- Error Handler ---
    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Handles errors specific to commands within this cog, sending feedback via DM."""
        log_prefix = f"[Guild {ctx.guild.id if ctx.guild else 'DM'}] CogCmdErrorHandler:"
        state = self.guild_states.get(ctx.guild.id) if ctx.guild else None
        if state and isinstance(ctx.channel, nextcord.abc.GuildChannel):
            state.last_command_channel_id = ctx.channel.id

        if isinstance(error, commands.CommandNotFound): return # Ignore

        error_message = None # Prepare message for DM

        if isinstance(error, commands.CheckFailure):
            logger.warning(f"{log_prefix} Check failed for command '{ctx.command.qualified_name if ctx.command else 'N/A'}': {error}")
            error_message = "You don't have the necessary permissions or conditions met to use this command."
        elif isinstance(error, commands.MissingRequiredArgument):
            error_message = f"Oops! You missed an argument: `{error.param.name}`. Use `?help {ctx.command.qualified_name}` for details."
        elif isinstance(error, commands.BadArgument):
            error_message = f"Invalid argument provided. Use `?help {ctx.command.qualified_name}` for details."
        elif isinstance(error, commands.GuildNotFound):
             error_message = "This command can only be used in a server."
        elif isinstance(error, commands.CommandInvokeError):
            original_error = error.original
            cmd_name = ctx.command.qualified_name if ctx.command else 'unknown command'
            if isinstance(original_error, nextcord.HTTPException) and original_error.code == 50035 and 'embeds.0.fields' in str(original_error.text).lower():
                logger.warning(f"{log_prefix} Embed length error likely from queue display.")
                # Queue command sends to channel anyway, so maybe send this error there too? Or DM?
                await ctx.send("The queue is too long to display fully!") # Keep this specific one in channel?
                return # Don't proceed to DM for this specific case
            elif isinstance(original_error, nextcord.errors.ClientException):
                 logger.error(f"{log_prefix} Voice ClientException during '{cmd_name}': {original_error}", exc_info=False)
                 error_message = f"A voice-related error occurred: {original_error}"
            else:
                logger.error(f"{log_prefix} Error invoking command '{cmd_name}': {original_error.__class__.__name__}: {original_error}", exc_info=original_error)
                error_message = f"An internal error occurred while running the `{cmd_name}` command. Please let the bot owner know."
        else:
            cmd_name = ctx.command.qualified_name if ctx.command else 'unknown command'
            logger.error(f"{log_prefix} Unhandled error type '{type(error).__name__}' for command '{cmd_name}': {error}", exc_info=error)
            error_message = f"An unexpected error occurred: {type(error).__name__}"

        # --- Send the determined error message via DM ---
        if error_message and ctx.author: # Check author exists
            await _send_dm_or_log(ctx.author, message=error_message)
        elif error_message:
             logger.warning(f"{log_prefix} Could not DM error message as ctx.author was not available.")
# --- End Error Handler ---


# --- Setup Function ---
def setup(bot: commands.Bot):
    """Adds the MusicCog to the bot."""
    # (Using the corrected manual Opus load from previous step)
    OPUS_PATH = '/usr/lib/x86_64-linux-gnu/libopus.so.0' # Confirmed path

    try:
        if not nextcord.opus.is_loaded():
            logger.info(f"Opus not loaded. Attempting to load manually from: {OPUS_PATH}")
            try:
                if nextcord.opus.load_opus(OPUS_PATH):
                     logger.info("Opus library loaded successfully from manual path.")
                else:
                     logger.critical(f"CRITICAL: nextcord.opus.load_opus({OPUS_PATH}) returned False. Voice will not work.")
                     raise commands.ExtensionError(f"Opus load returned False for path: {OPUS_PATH}")
            except nextcord.opus.OpusNotLoaded:
                 logger.critical(f"CRITICAL: Opus library not found or failed to load at specified path: {OPUS_PATH}. Voice will not work.")
                 raise commands.ExtensionError(f"Opus library not found or failed to load at: {OPUS_PATH}")
            except Exception as e_opus:
                 logger.critical(f"CRITICAL: Unexpected error loading Opus from {OPUS_PATH}: {e_opus}", exc_info=True)
                 raise commands.ExtensionError(f"Unexpected error loading Opus from {OPUS_PATH}: {e_opus}") from e_opus
        else:
            logger.info("Opus library already loaded.")
    except Exception as e:
        logger.critical(f"CRITICAL: An unexpected error occurred during Opus check/load setup: {e}", exc_info=True)
        if isinstance(e, commands.ExtensionError): raise e
        else: raise commands.ExtensionError(f"Opus check/load setup failed: {e}") from e

    try:
        bot.add_cog(MusicCog(bot))
        logger.info("MusicCog added successfully.")
    except Exception as e:
        logger.critical(f"CRITICAL: Failed to add MusicCog to the bot: {e}", exc_info=True)
        raise commands.ExtensionFailed(name="bot.cogs.music", original=e) from e
# --- End Setup Function ---

# --- End of File ---