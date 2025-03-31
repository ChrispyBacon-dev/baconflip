# --- bot/cogs/music.py ---

import nextcord
import nextcord.ui # <<< Import UI module
from nextcord.ext import commands
import asyncio
import yt_dlp
import logging
import functools
from collections import deque
from typing import TYPE_CHECKING # For type hinting MusicCog in View

# --- Type Hinting Forward Reference ---
if TYPE_CHECKING:
    from __main__ import Bot # Assuming your main bot class is named Bot

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
    'noplaylist': False,
    'nocheckcertificate': True,
    'ignoreerrors': True,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'extract_flat': 'in_playlist',
    'force_generic_extractor': True,
}

# Configure Logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# --- Song Class (Unchanged) ---
class Song:
    """Represents a song to be played."""
    def __init__(self, source_url, title, webpage_url, duration, requester):
        self.source_url = source_url
        self.title = title
        self.webpage_url = webpage_url
        self.duration = duration
        self.requester = requester

    def format_duration(self):
        if self.duration is None: return "N/A"
        try: duration_int = int(self.duration)
        except (ValueError, TypeError): return "N/A"
        minutes, seconds = divmod(duration_int, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours > 0 else f"{minutes:02d}:{seconds:02d}"

# Forward declare MusicCog for type hints in View
class MusicCog: pass

# --- Music Player View ---
class MusicPlayerView(nextcord.ui.View):
    def __init__(self, music_cog: 'MusicCog', guild_id: int, timeout=None): # Keep timeout=None for persistent-like behavior until stopped
        super().__init__(timeout=timeout)
        self.music_cog = music_cog
        self.guild_id = guild_id
        self._update_buttons() # Set initial button state

    # Helper to safely get the current guild state
    def _get_state(self) -> 'GuildMusicState' | None: # Use forward reference string
        # Access guild_states from the cog instance passed during init
        if self.music_cog:
             return self.music_cog.guild_states.get(self.guild_id)
        return None

    # Helper to update button states based on player status
    def _update_buttons(self):
        state = self._get_state()
        vc = state.voice_client if state else None
        is_connected = state and vc and vc.is_connected()
        is_playing = is_connected and vc.is_playing()
        is_paused = is_connected and vc.is_paused()
        is_active = is_playing or is_paused # Actively playing or paused
        has_queue = state and state.queue # Check if there are songs upcoming

        # --- Find buttons by custom_id ---
        pause_resume_button: nextcord.ui.Button | None = nextcord.utils.get(self.children, custom_id="music_pause_resume")
        skip_button: nextcord.ui.Button | None = nextcord.utils.get(self.children, custom_id="music_skip")
        stop_button: nextcord.ui.Button | None = nextcord.utils.get(self.children, custom_id="music_stop")
        queue_button: nextcord.ui.Button | None = nextcord.utils.get(self.children, custom_id="music_queue")
        # Add other buttons here if needed

        # Disable all if state is invalid or bot disconnected
        if not is_connected or not state:
            for button in [pause_resume_button, skip_button, stop_button, queue_button]:
                if button: button.disabled = True
            return

        # Pause/Resume Button Logic
        if pause_resume_button:
            pause_resume_button.disabled = not is_active # Disabled if neither playing nor paused
            if is_paused:
                pause_resume_button.label = "Resume"
                pause_resume_button.emoji = "▶️"
                pause_resume_button.style = nextcord.ButtonStyle.green
            else:
                pause_resume_button.label = "Pause"
                pause_resume_button.emoji = "⏸️"
                pause_resume_button.style = nextcord.ButtonStyle.secondary

        # Skip Button Logic (disable if nothing active or queue empty)
        if skip_button:
            # Can skip if playing/paused AND there's something next in queue
            skip_button.disabled = not is_active or not has_queue

        # Stop Button Logic (disable if nothing active)
        if stop_button:
            stop_button.disabled = not is_active

        # Queue button is generally always available if connected
        if queue_button:
            queue_button.disabled = False

    # --- Interaction Check ---
    async def interaction_check(self, interaction: nextcord.Interaction) -> bool:
        """Generic check: User must be in the bot's VC to use controls."""
        state = self._get_state()
        # Check if interaction user is in a voice channel
        if not interaction.user or not isinstance(interaction.user, nextcord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("You must be in a voice channel to use music controls.", ephemeral=True)
            return False
        # Check if bot is connected and user is in the same channel
        if not state or not state.voice_client or not state.voice_client.is_connected() or state.voice_client.channel != interaction.user.voice.channel:
            await interaction.response.send_message("You must be in the same voice channel as the bot.", ephemeral=True)
            return False
        return True # User is in the correct channel

    # --- Button Definitions and Callbacks ---
    @nextcord.ui.button(label="Pause", emoji="⏸️", style=nextcord.ButtonStyle.secondary, custom_id="music_pause_resume")
    async def pause_resume_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        state = self._get_state()
        if not state or not state.voice_client or not state.voice_client.is_connected():
             # Defer before sending error in case check failed somehow
             await interaction.response.defer(ephemeral=True)
             return await interaction.followup.send("Error: Bot is not connected.", ephemeral=True)

        vc = state.voice_client
        action_taken = None
        if vc.is_paused():
            vc.resume()
            action_taken = "Resumed"
        elif vc.is_playing():
            vc.pause()
            action_taken = "Paused"
        else: # Neither playing nor paused (should be disabled, but check again)
            await interaction.response.send_message("Nothing is playing to pause/resume.", ephemeral=True)
            return

        # Update button state immediately for visual feedback
        self._update_buttons()
        # Defer update first, then edit original, then send confirmation
        await interaction.response.defer(ephemeral=False) # Defer (no need for ephemeral defer here)
        try:
             await interaction.edit_original_message(view=self)
             # Send confirmation *after* editing the view
             await interaction.followup.send(f"Playback {action_taken}.", ephemeral=True)
        except nextcord.NotFound:
             logger.warning(f"Failed to edit player message for pause/resume (guild {self.guild_id}), message likely deleted.")
             await interaction.followup.send(f"Playback {action_taken}, but couldn't update controls message.", ephemeral=True)
        except Exception as e:
             logger.error(f"Error editing message on pause/resume: {e}")
             await interaction.followup.send(f"Playback {action_taken}, but failed to update controls message.", ephemeral=True)

    @nextcord.ui.button(label="Skip", emoji="⏭️", style=nextcord.ButtonStyle.secondary, custom_id="music_skip")
    async def skip_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        state = self._get_state()
        # Re-check conditions even though button should be disabled
        if not state or not state.voice_client or not state.voice_client.is_connected() or not (state.voice_client.is_playing() or state.voice_client.is_paused()):
            return await interaction.response.send_message("Nothing is playing to skip.", ephemeral=True)
        if not state.queue:
             return await interaction.response.send_message("Queue is empty, cannot skip.", ephemeral=True)

        await interaction.response.defer(ephemeral=True) # Defer response
        skipped_title = state.current_song.title if state.current_song else "current song"
        state.voice_client.stop() # Triggers the loop to play next
        logger.info(f"[{self.guild_id}] Song skipped via button by {interaction.user}")
        await interaction.followup.send(f"Skipped **{skipped_title}**.", ephemeral=True)
        # Loop handles updating the message when the next song starts

    @nextcord.ui.button(label="Stop", emoji="⏹️", style=nextcord.ButtonStyle.danger, custom_id="music_stop")
    async def stop_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        state = self._get_state()
        # Re-check conditions
        if not state or not state.voice_client or not state.voice_client.is_connected() or not (state.voice_client.is_playing() or state.voice_client.is_paused()):
             return await interaction.response.send_message("Nothing is playing to stop.", ephemeral=True)

        await interaction.response.defer(ephemeral=True) # Defer response
        logger.info(f"[{self.guild_id}] Playback stopped via button by {interaction.user}")
        # stop_playback now handles view stopping and message clearing
        await state.stop_playback()
        await interaction.followup.send("Playback stopped and queue cleared.", ephemeral=True)
        # View should be stopped and message cleared by stop_playback


    @nextcord.ui.button(label="Queue", emoji="📜", style=nextcord.ButtonStyle.secondary, custom_id="music_queue")
    async def queue_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        state = self._get_state()
        if not state:
             return await interaction.response.send_message("Error: Bot state not found.", ephemeral=True)

        # Generate the queue embed using the cog's helper method
        try:
            # Ensure the cog instance is available
            if not self.music_cog:
                 return await interaction.response.send_message("Internal error: Cog reference missing.", ephemeral=True)

            q_embed = await self.music_cog.build_queue_embed(state) # Use the helper
            if q_embed:
                await interaction.response.send_message(embed=q_embed, ephemeral=True)
            else:
                await interaction.response.send_message("The queue is empty and nothing is playing.", ephemeral=True)
        except Exception as e:
             logger.error(f"Error generating queue embed for button (guild {self.guild_id}): {e}", exc_info=True)
             await interaction.response.send_message("Error displaying the queue.", ephemeral=True)


    async def on_timeout(self):
        # This disables buttons if the view times out.
        # Since we use timeout=None, this ideally shouldn't be called unless manually stopped.
        logger.debug(f"MusicPlayerView timed out or was stopped for guild {self.guild_id}")
        for item in self.children:
            if isinstance(item, nextcord.ui.Button): item.disabled = True

        # Try to edit the original message to show disabled buttons
        state = self._get_state()
        if state and state.current_player_message_id and state.last_command_channel_id:
            try:
                channel = self.music_cog.bot.get_channel(state.last_command_channel_id)
                if channel and isinstance(channel, nextcord.TextChannel):
                    msg = await channel.fetch_message(state.current_player_message_id)
                    # Only edit if the view hasn't already been removed
                    if msg and msg.components: # Check if components still exist
                         await msg.edit(view=self) # Edit with the current (disabled) view state
            except (nextcord.NotFound, nextcord.Forbidden, AttributeError) as e:
                logger.warning(f"Failed to edit message on view timeout/stop for guild {self.guild_id}: {e}")
        # Don't clear message ID here, stop_playback or cleanup handles that

# --- Guild Music State ---
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
        self.last_command_channel_id: int | None = None
        # --- Player Message Attributes ---
        self.current_player_message_id: int | None = None
        self.current_player_view: MusicPlayerView | None = None
        # --------------------------------

    # --- Helper to create Now Playing embed ---
    def _create_now_playing_embed(self, song: Song | None) -> nextcord.Embed | None:
         if not song: return None
         embed = nextcord.Embed(title="Now Playing", color=nextcord.Color.green())
         embed.description = f"**[{song.title}]({song.webpage_url})**"
         embed.add_field(name="Duration", value=song.format_duration(), inline=True)
         requester = song.requester
         if requester:
              embed.add_field(name="Requested by", value=requester.mention, inline=True)
              # Use display_avatar which falls back gracefully
              embed.set_thumbnail(url=requester.display_avatar.url if requester.display_avatar else None)
         # TODO: Consider adding song thumbnail?
         # if song_thumbnail_url: embed.set_thumbnail(url=song_thumbnail_url)
         return embed

    # --- Helper to update or send player message ---
    async def _update_player_message(self, embed: nextcord.Embed | None = None, view: nextcord.ui.View | None = None, content: str | None = None):
        """Sends or edits the persistent player message."""
        log_prefix = f"[{self.guild_id}] PlayerMsg:"
        if not self.last_command_channel_id:
            logger.warning(f"{log_prefix} Cannot update player message, channel ID missing.")
            return

        channel = self.bot.get_channel(self.last_command_channel_id)
        if not channel or not isinstance(channel, nextcord.TextChannel):
            logger.warning(f"{log_prefix} Cannot find/use channel {self.last_command_channel_id}.")
            self.current_player_message_id = None # Invalidate message ID
            self.current_player_view = None
            return

        message_to_edit = None
        # --- Attempt to Fetch Existing Message ---
        if self.current_player_message_id:
            try:
                message_to_edit = await channel.fetch_message(self.current_player_message_id)
                logger.debug(f"{log_prefix} Found existing player message {self.current_player_message_id}")
            except nextcord.NotFound:
                logger.warning(f"{log_prefix} Player message {self.current_player_message_id} not found. Sending new.")
                self.current_player_message_id = None
            except nextcord.Forbidden:
                 logger.error(f"{log_prefix} Permission error fetching message {self.current_player_message_id}.")
                 self.current_player_message_id = None # Cannot manage this message anymore
                 return
            except Exception as e:
                 logger.error(f"{log_prefix} Error fetching message {self.current_player_message_id}: {e}")
                 # Keep ID for potential retry, but log error

        # --- Send or Edit ---
        try:
            if message_to_edit:
                # If view is None explicitly, remove components
                await message_to_edit.edit(content=content, embed=embed, view=view)
                logger.debug(f"{log_prefix} Edited player message {self.current_player_message_id}.")
            elif embed or view: # Only send if there's content
                new_message = await channel.send(content=content, embed=embed, view=view)
                self.current_player_message_id = new_message.id
                self.current_player_view = view # Store the view associated with this new message
                logger.info(f"{log_prefix} Sent new player message {self.current_player_message_id}.")
            else:
                 logger.debug(f"{log_prefix} No content to send/edit and no existing message.")

        except nextcord.Forbidden:
            logger.error(f"{log_prefix} Permission error sending/editing in channel {channel.name}.")
            self.current_player_message_id = None # Cannot manage
            self.current_player_view = None
        except nextcord.HTTPException as e:
            logger.error(f"{log_prefix} HTTP error sending/editing player message: {e}")
            # If it's a 404 on edit, the message was likely deleted between fetch and edit
            if e.status == 404 and message_to_edit:
                logger.warning(f"{log_prefix} Message {self.current_player_message_id} deleted before edit could complete.")
                self.current_player_message_id = None
                self.current_player_view = None
        except Exception as e:
             logger.error(f"{log_prefix} Unexpected error updating player message: {e}", exc_info=True)


    # --- Modified Playback Loop ---
    async def _playback_loop(self):
        await self.bot.wait_until_ready()
        logger.info(f"[{self.guild_id}] Playback loop starting.")
        music_cog: MusicCog | None = self.bot.get_cog("Music") # Get cog instance once
        if not music_cog:
             logger.critical(f"[{self.guild_id}] CRITICAL: MusicCog instance not found in bot. Loop cannot function.")
             return

        while True:
            self.play_next_song.clear()
            log_prefix = f"[{self.guild_id}] Loop:"
            logger.debug(f"{log_prefix} Top of loop, play_next_song cleared.")
            song_to_play = None
            vc_valid = False

            # Check VC State
            if self.voice_client and self.voice_client.is_connected():
                 vc_valid = True
                 if self.voice_client.is_playing() or self.voice_client.is_paused():
                      logger.debug(f"{log_prefix} VC is active. Waiting for song end signal.")
                      await self.play_next_song.wait()
                      logger.debug(f"{log_prefix} play_next_song event received while VC active.")
                      continue # Re-evaluate state
            else: # VC not connected
                logger.warning(f"{log_prefix} Voice client disconnected at loop top.")
                async with self._lock: # Put back potential current song
                    if self.current_song: self.queue.appendleft(self.current_song); self.current_song = None
                # Cleanup Player Message if VC disconnects
                logger.debug(f"{log_prefix} Cleaning up player message due to VC disconnect.")
                if self.current_player_view: self.current_player_view.stop(); self.current_player_view = None
                # Schedule update task as this might be called from different contexts
                self.bot.loop.create_task(self._update_player_message(content="*Bot disconnected.*", embed=None, view=None))
                self.current_player_message_id = None
                return # Exit loop

            # Get Song
            if vc_valid:
                async with self._lock:
                    if self.queue:
                        song_to_play = self.queue.popleft()
                        self.current_song = song_to_play
                        logger.info(f"{log_prefix} Popped '{song_to_play.title}'. Queue: {len(self.queue)}")
                    else: # Queue is empty
                        if self.current_song: # A song just finished, queue is now empty
                             logger.info(f"{log_prefix} Playback finished (queue empty).")
                             finished_song_embed = self._create_now_playing_embed(self.current_song)
                             if finished_song_embed: finished_song_embed.title = "Finished Playing" # Update title
                             # Stop the view and update message to show finished state with disabled buttons
                             if self.current_player_view: self.current_player_view.stop()
                             disabled_view = self.current_player_view # Get reference before clearing
                             if disabled_view: # Ensure buttons are disabled visually
                                 for item in disabled_view.children:
                                     if isinstance(item, nextcord.ui.Button): item.disabled = True
                             self.bot.loop.create_task(self._update_player_message(content="*Queue finished.*", embed=finished_song_embed, view=disabled_view))
                             self.current_song = None
                             self.current_player_view = None
                             # Keep message ID until potentially overwritten by next play
                        else:
                             # Queue was already empty and no song was playing
                             logger.debug(f"{log_prefix} Queue remains empty.")

            # Wait if Queue Empty
            if not song_to_play:
                logger.info(f"{log_prefix} Queue empty. Waiting for event.")
                await self.play_next_song.wait()
                logger.info(f"{log_prefix} play_next_song received while queue was empty.")
                continue # Re-check queue

            # Play Song
            logger.info(f"{log_prefix} Attempting to play: {song_to_play.title}")
            source = None
            play_successful = False
            try:
                # Re-verify VC
                if not self.voice_client or not self.voice_client.is_connected():
                     logger.warning(f"{log_prefix} VC disconnected before playing '{song_to_play.title}'.")
                     async with self._lock: self.queue.appendleft(song_to_play); self.current_song = None
                     continue
                if self.voice_client.is_playing() or self.voice_client.is_paused():
                    logger.error(f"{log_prefix} RACE?: VC active before play call for '{song_to_play.title}'.")
                    async with self._lock: self.queue.appendleft(song_to_play); self.current_song = None
                    await self.play_next_song.wait(); continue

                # Create Source and Play
                original_source = nextcord.FFmpegPCMAudio(song_to_play.source_url, before_options=FFMPEG_BEFORE_OPTIONS, options=FFMPEG_OPTIONS)
                source = nextcord.PCMVolumeTransformer(original_source, volume=self.volume)
                self.voice_client.play(source, after=lambda e: self._handle_after_play(e))
                play_successful = True
                logger.info(f"{log_prefix} voice_client.play() called for {song_to_play.title}")

                # --- Create/Update Player Message ---
                now_playing_embed = self._create_now_playing_embed(song_to_play)
                # Stop previous view if it exists and wasn't stopped
                if self.current_player_view and not self.current_player_view.is_finished():
                     logger.debug(f"{log_prefix} Stopping previous player view.")
                     self.current_player_view.stop()
                # Create a new view instance for the new song
                self.current_player_view = MusicPlayerView(music_cog, self.guild_id)
                await self._update_player_message(embed=now_playing_embed, view=self.current_player_view, content=None) # Clear any previous content
                # ----------------------------------

            except (nextcord.errors.ClientException, ValueError, TypeError) as e:
                logger.error(f"{log_prefix} Exception preparing/starting play for {song_to_play.title}: {e}", exc_info=True)
                await self._notify_channel_error(f"Error playing '{song_to_play.title}': {e}. Skipping.")
                self.current_song = None
            except Exception as e:
                logger.error(f"{log_prefix} Unexpected error during playback prep for {song_to_play.title}: {e}", exc_info=True)
                await self._notify_channel_error(f"Unexpected error preparing '{song_to_play.title}'. Skipping.")
                self.current_song = None

            # Wait for song end/skip only if play was successful
            if play_successful:
                 logger.debug(f"{log_prefix} Waiting for play_next_song event...")
                 await self.play_next_song.wait()
                 logger.debug(f"{log_prefix} play_next_song event received for '{song_to_play.title}'.")
            else:
                 logger.debug(f"{log_prefix} Play failed, loop continues without waiting.")
                 await asyncio.sleep(0.1)


    # --- Modified Stop/Cleanup ---
    async def stop_playback(self):
        """Stops playback, clears queue, and cleans up the player message."""
        log_prefix = f"[{self.guild_id}] StopPlayback:"
        async with self._lock:
            self.queue.clear()
            vc = self.voice_client
            if vc and vc.is_connected() and (vc.is_playing() or vc.is_paused()):
                logger.info(f"{log_prefix} Stopping track.")
                vc.stop() # Triggers after callback -> sets event
            self.current_song = None
            logger.info(f"{log_prefix} Queue cleared.")

            # --- Prepare message/view cleanup data (release lock before async msg update) ---
            view_to_stop = self.current_player_view
            message_id_to_clear = self.current_player_message_id
            self.current_player_view = None
            self.current_player_message_id = None
            # --------------------------------------------------------------------

            if not self.play_next_song.is_set():
                logger.debug(f"{log_prefix} Setting play_next_song event.")
                self.play_next_song.set()

        # --- Now perform message/view cleanup outside the lock ---
        if view_to_stop and not view_to_stop.is_finished():
            view_to_stop.stop()
            logger.debug(f"{log_prefix} Stopped player view.")
        if message_id_to_clear and self.last_command_channel_id:
            logger.debug(f"{log_prefix} Scheduling player message clear/update.")
            # Ensure buttons are disabled on the view being passed
            if view_to_stop:
                 for item in view_to_stop.children:
                     if isinstance(item, nextcord.ui.Button): item.disabled = True
            self.bot.loop.create_task(
                self._update_player_message(content="*Playback stopped.*", embed=None, view=view_to_stop) # Show disabled buttons briefly
            )
        # -----------------------------------------------------

    async def cleanup(self):
        """Cleans up resources (disconnects VC, stops loop, cleans player message)."""
        guild_id = self.guild_id
        log_prefix = f"[{guild_id}] Cleanup:"
        logger.info(f"{log_prefix} Starting cleanup.")

        # Stop playback (handles queue, vc.stop, view stop, message clear)
        await self.stop_playback()

        # Cancel the loop task
        task = self._playback_task
        if task and not task.done():
            logger.info(f"{log_prefix} Cancelling playback loop task.")
            task.cancel()
            try: await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError): pass
            except Exception as e: logger.error(f"{log_prefix} Error awaiting cancelled task: {e}", exc_info=True)
        self._playback_task = None

        # Disconnect voice client
        vc = self.voice_client
        if vc and vc.is_connected():
            logger.info(f"{log_prefix} Disconnecting voice client.")
            try: await vc.disconnect(force=True)
            except Exception as e: logger.error(f"{log_prefix} Error disconnecting VC: {e}", exc_info=True)
        self.voice_client = None
        self.current_song = None

        # Ensure view/message IDs are cleared (stop_playback should handle this)
        self.current_player_view = None
        self.current_player_message_id = None

        logger.info(f"{log_prefix} Cleanup finished.")
        # State dictionary removal happens in calling context


    async def _notify_channel_error(self, message: str):
        """Helper to send error messages (now using embeds)."""
        if not self.last_command_channel_id:
            logger.warning(f"[{self.guild_id}] Cannot send error, channel ID missing.")
            return
        try:
            channel = self.bot.get_channel(self.last_command_channel_id)
            if channel and isinstance(channel, nextcord.abc.Messageable):
                 embed = nextcord.Embed(title="Music Bot Error", description=message, color=nextcord.Color.red())
                 await channel.send(embed=embed)
                 logger.debug(f"[{self.guild_id}] Sent error notification to channel {self.last_command_channel_id}")
            else:
                 logger.warning(f"[{self.guild_id}] Cannot find/use channel {self.last_command_channel_id} for error.")
        except Exception as e:
             logger.error(f"[{self.guild_id}] Failed to send error notification: {e}", exc_info=True)


# --- Music Cog ---
# Use class MusicCog(commands.Cog) to allow type hint in MusicPlayerView
class MusicCog(commands.Cog, name="Music"):
    """Commands for playing music in voice channels."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_states: dict[int, GuildMusicState] = {}
        self.ydl = yt_dlp.YoutubeDL(YDL_OPTS)
        # If using persistent views, would add self.bot.add_view here in on_ready or init

    def get_guild_state(self, guild_id: int) -> GuildMusicState:
        """Gets or creates the music state for a guild."""
        if guild_id not in self.guild_states:
            logger.debug(f"Creating new GuildMusicState for guild {guild_id}")
            self.guild_states[guild_id] = GuildMusicState(self.bot, guild_id)
        return self.guild_states[guild_id]

    # --- Helper to build Queue Embed (extracted logic) ---
    async def build_queue_embed(self, state: GuildMusicState) -> nextcord.Embed | None:
         """Builds the queue embed message."""
         log_prefix = f"[{state.guild_id}] QueueEmbed:"
         logger.debug(f"{log_prefix} Building queue embed.")
         async with state._lock:
             current_song = state.current_song
             queue_copy = list(state.queue)

         if not current_song and not queue_copy:
             logger.debug(f"{log_prefix} Queue/NowPlaying empty.")
             return None

         embed = nextcord.Embed(title="Music Queue", color=nextcord.Color.blurple())
         current_display = "Nothing currently playing."
         total_queue_duration = 0

         if current_song:
             status_icon = "❓"
             if state.voice_client and state.voice_client.is_connected():
                  if state.voice_client.is_playing(): status_icon = "▶️ Playing"
                  elif state.voice_client.is_paused(): status_icon = "⏸️ Paused"
                  else: status_icon = "⏹️ Stopped/Idle"
             requester_mention = current_song.requester.mention if current_song.requester else "Unknown"
             current_display = f"{status_icon}: **[{current_song.title}]({current_song.webpage_url})** `[{current_song.format_duration()}]` - Req by {requester_mention}"
         embed.add_field(name="Now Playing", value=current_display, inline=False)

         if queue_copy:
             queue_list_strings = []
             current_length = 0
             char_limit = 950; songs_shown = 0; max_songs_to_list = 20
             for i, song in enumerate(queue_copy):
                 if song.duration:
                     try: total_queue_duration += int(song.duration)
                     except (ValueError, TypeError): pass
                 if songs_shown < max_songs_to_list:
                     requester_name = song.requester.display_name if song.requester else "Unknown"
                     song_line = f"`{i+1}.` [{song.title}]({song.webpage_url}) `[{song.format_duration()}]` - Req by {requester_name}\n"
                     if current_length + len(song_line) <= char_limit:
                         queue_list_strings.append(song_line)
                         current_length += len(song_line); songs_shown += 1
                     else:
                         remaining = len(queue_copy) - i
                         if remaining > 0: queue_list_strings.append(f"\n...and {remaining} more song{'s' if remaining != 1 else ''}.")
                         break
             if songs_shown == max_songs_to_list and len(queue_copy) > max_songs_to_list:
                  remaining = len(queue_copy) - max_songs_to_list
                  queue_list_strings.append(f"\n...and {remaining} more song{'s' if remaining != 1 else ''}.")

             total_dur_str = Song(None,None,None,total_queue_duration,None).format_duration() if total_queue_duration > 0 else "N/A"
             queue_header = f"Up Next ({len(queue_copy)} song{'s' if len(queue_copy) != 1 else ''}, Total Duration: {total_dur_str})"
             queue_value = "".join(queue_list_strings).strip()
             if not queue_value and len(queue_copy) > 0: queue_value = f"Queue contains {len(queue_copy)} song(s)..."
             if queue_value: embed.add_field(name=queue_header, value=queue_value, inline=False)
             else: embed.add_field(name="Up Next", value="No songs in queue.", inline=False)
         else:
             embed.add_field(name="Up Next", value="No songs in queue.", inline=False)

         total_songs_in_system = len(queue_copy) + (1 if current_song else 0)
         volume_percent = int(state.volume * 100) if hasattr(state, 'volume') else "N/A"
         embed.set_footer(text=f"Total songs: {total_songs_in_system} | Volume: {volume_percent}%")
         logger.debug(f"{log_prefix} Finished building embed.")
         return embed

    # --- Extraction methods (_process_entry, _extract_info) remain unchanged ---
    # ... (paste _process_entry and _extract_info from previous correct version here) ...
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


    # --- Listener for Voice State Updates (Unchanged) ---
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
                await state.cleanup() # Cleanup handles message/view now
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
                 # Check if bot is alone (only bot member left)
                 # Use len(channel.voice_states) which correctly counts members with voice states in channel
                 if len(bot_vc_channel.voice_states) == 1 and self.bot.user.id in bot_vc_channel.voice_states:
                     logger.info(f"[{guild_id}] Bot is now alone in {bot_vc_channel.name}. Pausing playback.")
                     if state.voice_client and state.voice_client.is_playing():
                         state.voice_client.pause()
                         # Update buttons via view if possible
                         if state.current_player_view:
                              state.current_player_view._update_buttons()
                              self.bot.loop.create_task(state._update_player_message(view=state.current_player_view)) # Update message async

            # User joins bot's channel
            elif before.channel != bot_vc_channel and after.channel == bot_vc_channel:
                 logger.debug(f"[{guild_id}] User {member.name} joined bot channel {bot_vc_channel.name}.")
                 # If bot was paused due to being alone, resume playback
                 # Check voice_states count > 1
                 if state.voice_client and state.voice_client.is_paused() and len(bot_vc_channel.voice_states) > 1:
                     logger.info(f"[{guild_id}] User joined, resuming paused playback.")
                     state.voice_client.resume()
                     # Update buttons via view if possible
                     if state.current_player_view:
                         state.current_player_view._update_buttons()
                         self.bot.loop.create_task(state._update_player_message(view=state.current_player_view)) # Update message async


    # --- Music Commands ---

    # --- play_command (Modified Feedback) ---
    @commands.command(name='play', aliases=['p'], help="Plays songs from a URL, search query, or playlist.")
    @commands.guild_only()
    async def play_command(self, ctx: commands.Context, *, query: str):
        if not ctx.guild: return
        state = self.get_guild_state(ctx.guild.id)
        state.last_command_channel_id = ctx.channel.id # CRITICAL: Set channel ID
        log_prefix = f"[{ctx.guild.id}] PlayCmd:"
        logger.info(f"{log_prefix} User {ctx.author.name} initiated play with query: {query}")

        # --- Connection checks ---
        if not state.voice_client or not state.voice_client.is_connected():
            if ctx.author.voice and ctx.author.voice.channel:
                 logger.info(f"{log_prefix} Bot not connected. Joining {ctx.author.voice.channel.name}.")
                 await ctx.invoke(self.join_command)
                 state = self.get_guild_state(ctx.guild.id) # Re-get state
                 if not state.voice_client or not state.voice_client.is_connected():
                      logger.warning(f"{log_prefix} Failed to join VC after invoke.")
                      return # join_command should have sent message
                 else:
                      logger.info(f"{log_prefix} Successfully joined VC after invoke.")
                      state.last_command_channel_id = ctx.channel.id # Ensure channel ID is set
            else:
                logger.warning(f"{log_prefix} User not in VC and bot not connected.")
                return await ctx.send("You need to be in a voice channel for me to join.")
        elif not ctx.author.voice or ctx.author.voice.channel != state.voice_client.channel:
             logger.warning(f"{log_prefix} User not in bot's VC ({state.voice_client.channel.name}).")
             return await ctx.send(f"You need to be in {state.voice_client.channel.mention} to add songs.")
        else:
             logger.info(f"{log_prefix} Bot already connected to {state.voice_client.channel.name}.")

        # --- Extraction Phase ---
        playlist_title = None; songs_to_add = []; extraction_error_code = None
        logger.debug(f"{log_prefix} Entering extraction phase.")
        typing_task = asyncio.create_task(ctx.trigger_typing())
        try:
            logger.debug(f"{log_prefix} Calling _extract_info...")
            result = await self._extract_info(query, ctx.author)
            if isinstance(result[0], str) and result[0].startswith("err_"):
                 extraction_error_code = result[0][4:]
            else: playlist_title, songs_to_add = result
            logger.debug(f"{log_prefix} _extract_info finished. Error: {extraction_error_code}, Songs: {len(songs_to_add)}, PL: {playlist_title}")
        except Exception as e: logger.error(f"{log_prefix} Exception during _extract_info: {e}", exc_info=True); extraction_error_code = "internal_extraction"
        finally:
             if typing_task and not typing_task.done():
                  try: typing_task.cancel()
                  except asyncio.CancelledError: pass

        # --- Process Extraction Result ---
        if extraction_error_code:
             # ... (error mapping - same as before) ...
             error_map = {'unsupported': "Sorry, I don't support that URL or service.", 'unavailable': "That video/playlist seems unavailable (maybe private or deleted).", 'private': "That video/playlist is private and I can't access it.", 'age_restricted': "Sorry, I can't play age-restricted content.", 'network': "I couldn't connect to the source to get the details.", 'download_initial': "Error fetching initial data.", 'download_single': "Error fetching data for the single track.", 'nodata': "Couldn't find any data for the query.", 'nodata_reextract': "Couldn't find data when re-fetching single track info.", 'process_single_failed': "Failed to process the single track after fetching.", 'extraction_initial': "Error processing initial data.", 'extraction_single': "Error processing single track data.", 'internal_extraction': "An internal error occurred fetching information."}
             error_message = error_map.get(extraction_error_code, "An unknown error occurred while fetching.")
             return await ctx.send(error_message)
        if not songs_to_add:
            if playlist_title: return await ctx.send(f"Found playlist '{playlist_title}', but couldn't add any playable songs.")
            else: return await ctx.send("Could not find any playable songs for your query.")

        # --- Add to Queue ---
        added_count = 0; queue_start_pos = 0
        async with state._lock:
            was_empty_before_add = not state.queue and not state.current_song # Check *before* adding
            queue_start_pos = len(state.queue) + (1 if state.current_song else 0); queue_start_pos = max(1, queue_start_pos)
            state.queue.extend(songs_to_add)
            added_count = len(songs_to_add)
            logger.info(f"{log_prefix} Added {added_count} songs. Queue size: {len(state.queue)}")

        # --- Simplified Feedback ---
        if added_count > 0:
            try:
                # Only send a confirmation if songs were added to an *already active* queue/player
                if not was_empty_before_add:
                    embed = nextcord.Embed(color=nextcord.Color.blue())
                    first_song = songs_to_add[0]
                    if playlist_title and added_count > 1:
                        embed.title = "Playlist Queued"
                        pl_link = query if query.startswith('http') else None; pl_desc = f"**[{playlist_title}]({pl_link})**" if pl_link else f"**{playlist_title}**"
                        embed.description = f"Added **{added_count}** songs from {pl_desc}."
                    elif added_count == 1:
                        embed.title = "Added to Queue"
                        embed.description = f"[{first_song.title}]({first_song.webpage_url})"
                        embed.add_field(name="Position", value=f"#{queue_start_pos}", inline=True)
                    else: # Should not happen
                         embed = None

                    if embed:
                        requester_name = ctx.author.display_name; requester_icon = ctx.author.display_avatar.url if ctx.author.display_avatar else None
                        embed.set_footer(text=f"Requested by {requester_name}", icon_url=requester_icon)
                        await ctx.send(embed=embed, delete_after=15.0) # Delete after short time
                else:
                     # If queue was empty, just react - player UI will appear soon
                     await ctx.message.add_reaction('✅')

            except Exception as e: logger.error(f"{log_prefix} Error sending simplified feedback: {e}", exc_info=True)

        # --- Ensure loop starts/continues ---
        if added_count > 0:
            logger.debug(f"{log_prefix} Ensuring playback loop is started/signaled.")
            state.start_playback_loop()
        logger.debug(f"{log_prefix} play_command finished.")

    # --- Commands below can optionally be removed if buttons are preferred ---
    # --- They are kept here for now but might conflict if not used carefully ---

    @commands.command(name='join', aliases=['connect', 'j'], help="Connects the bot to your current voice channel.")
    @commands.guild_only()
    async def join_command(self, ctx: commands.Context):
        # ... (join command logic - unchanged) ...
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send("You need to be in a voice channel to use this command.")
        if not ctx.guild: return
        channel = ctx.author.voice.channel
        state = self.get_guild_state(ctx.guild.id)
        state.last_command_channel_id = ctx.channel.id
        async with state._lock:
            if state.voice_client and state.voice_client.is_connected():
                if state.voice_client.channel == channel: await ctx.send(f"I'm already in {channel.mention}.")
                else:
                    try: await state.voice_client.move_to(channel); await ctx.send(f"Moved to {channel.mention}.")
                    except Exception as e: await ctx.send(f"Error moving: {e}"); logger.error(f"[{ctx.guild.id}] Error moving VC: {e}", exc_info=True)
            else:
                try: state.voice_client = await channel.connect(); await ctx.send(f"Connected to {channel.mention}."); state.start_playback_loop()
                except Exception as e: await ctx.send(f"Error connecting: {e}"); logger.error(f"[{ctx.guild.id}] Error connecting VC: {e}", exc_info=True); del self.guild_states[ctx.guild.id]

    @commands.command(name='leave', aliases=['disconnect', 'dc', 'stopbot'], help="Disconnects the bot and clears the queue.")
    @commands.guild_only()
    async def leave_command(self, ctx: commands.Context):
        # ... (leave command logic - unchanged, calls cleanup) ...
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected(): return await ctx.send("Not connected.")
        logger.info(f"[{ctx.guild.id}] Leave initiated by {ctx.author.name}.")
        await ctx.message.add_reaction('👋')
        await state.cleanup()
        if ctx.guild.id in self.guild_states: del self.guild_states[ctx.guild.id]; logger.info(f"[{ctx.guild.id}] State removed after leave.")

    @commands.command(name='skip', aliases=['s', 'next'], help="Skips the currently playing song (use button preferably).")
    @commands.guild_only()
    async def skip_command(self, ctx: commands.Context):
        # ... (skip command logic - unchanged, just stops vc) ...
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected(): return await ctx.send("Not connected.")
        vc = state.voice_client
        if not vc.is_playing() and not vc.is_paused(): return await ctx.send("Nothing playing to skip.")
        if not state.queue: return await ctx.send("Queue empty, cannot skip.")
        logger.info(f"[{ctx.guild.id}] Skip requested via cmd by {ctx.author.name}.")
        vc.stop(); await ctx.message.add_reaction('⏭️')

    @commands.command(name='stop', help="Stops playback completely (use button preferably).")
    @commands.guild_only()
    async def stop_command(self, ctx: commands.Context):
        # ... (stop command logic - unchanged, calls stop_playback) ...
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected(): return await ctx.send("Not connected.")
        if not state.current_song and not state.queue: return await ctx.send("Nothing to stop.")
        logger.info(f"[{ctx.guild.id}] Stop requested via cmd by {ctx.author.name}.")
        await state.stop_playback(); await ctx.message.add_reaction('⏹️')

    @commands.command(name='pause', help="Pauses the currently playing song (use button preferably).")
    @commands.guild_only()
    async def pause_command(self, ctx: commands.Context):
        # ... (pause command logic - unchanged) ...
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected(): return await ctx.send("Not connected.")
        if state.voice_client.is_paused(): return await ctx.send("Already paused.")
        if not state.voice_client.is_playing(): return await ctx.send("Nothing playing.")
        state.voice_client.pause(); logger.info(f"[{ctx.guild.id}] Paused via cmd by {ctx.author.name}."); await ctx.message.add_reaction('⏸️')

    @commands.command(name='resume', aliases=['unpause'], help="Resumes a paused song (use button preferably).")
    @commands.guild_only()
    async def resume_command(self, ctx: commands.Context):
        # ... (resume command logic - unchanged) ...
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected(): return await ctx.send("Not connected.")
        if state.voice_client.is_playing(): return await ctx.send("Already playing.")
        if not state.voice_client.is_paused(): return await ctx.send("Nothing paused.")
        state.voice_client.resume(); logger.info(f"[{ctx.guild.id}] Resumed via cmd by {ctx.author.name}."); await ctx.message.add_reaction('▶️')

    # --- queue_command uses helper ---
    @commands.command(name='queue', aliases=['q', 'nowplaying', 'np'], help="Shows the current song queue (use button preferably).")
    @commands.guild_only()
    async def queue_command(self, ctx: commands.Context):
         if not ctx.guild: return
         state = self.guild_states.get(ctx.guild.id)
         if not state: return await ctx.send("Not active.")
         state.last_command_channel_id = ctx.channel.id # Update channel potentially

         embed = await self.build_queue_embed(state)
         if embed: await ctx.send(embed=embed)
         else: await ctx.send("Queue empty and nothing playing.")

    @commands.command(name='volume', aliases=['vol'], help="Changes the player volume (0-100).")
    @commands.guild_only()
    async def volume_command(self, ctx: commands.Context, *, volume: int):
        # ... (volume command logic - unchanged) ...
        if not ctx.guild: return
        state = self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected(): return await ctx.send("Not connected.")
        if not 0 <= volume <= 100: return await ctx.send("Volume must be 0-100.")
        new_vol = volume / 100.0; state.volume = new_vol
        if state.voice_client.source and isinstance(state.voice_client.source, nextcord.PCMVolumeTransformer): state.voice_client.source.volume = new_vol
        await ctx.send(f"Volume set to **{volume}%**.")

    # --- Error Handler (Unchanged) ---
    async def cog_command_error(self, ctx: commands.Context, error):
        # ... (error handler logic - unchanged) ...
        log_prefix = f"[{ctx.guild.id if ctx.guild else 'DM'}] MusicCog Error:"
        state = self.guild_states.get(ctx.guild.id) if ctx.guild else None
        if state and hasattr(ctx, 'channel'): state.last_command_channel_id = ctx.channel.id
        if isinstance(error, commands.CommandNotFound): return
        elif isinstance(error, commands.CheckFailure): logger.warning(f"{log_prefix} Check failed for '{ctx.command.qualified_name}' by {ctx.author}: {error}"); await ctx.send("No permission.")
        elif isinstance(error, commands.MissingRequiredArgument): await ctx.send(f"Missing arg: `{error.param.name}`.")
        elif isinstance(error, commands.BadArgument): await ctx.send(f"Invalid argument type.")
        elif isinstance(error, commands.CommandInvokeError):
            original = error.original
            if isinstance(original, nextcord.HTTPException) and original.code == 50035 and 'embeds.0.fields' in str(original.text): logger.warning(f"{log_prefix} Embed length error: {original}"); await ctx.send("Queue too long to display.") ; return
            logger.error(f"{log_prefix} Invoke error '{ctx.command.qualified_name}': {original.__class__.__name__}: {original}", exc_info=original)
            if isinstance(original, nextcord.errors.ClientException): await ctx.send(f"Voice Error: {original}")
            else: await ctx.send(f"Internal error running `{ctx.command.name}`.")
        else: logger.error(f"{log_prefix} Unhandled error '{ctx.command.qualified_name}': {type(error).__name__}: {error}", exc_info=error)


# --- Setup Function (Unchanged) ---
def setup(bot: commands.Bot):
    # ... (opus loading logic - unchanged) ...
    OPUS_PATH = '/usr/lib/x86_64-linux-gnu/libopus.so.0'
    try:
        if not nextcord.opus.is_loaded():
            logger.info(f"Opus not loaded. Trying path: {OPUS_PATH}")
            nextcord.opus.load_opus(OPUS_PATH)
            if nextcord.opus.is_loaded(): logger.info("Opus loaded successfully.")
            else: logger.critical("Opus load attempt finished, but is_loaded() is false.")
        else: logger.info("Opus was already loaded.")
    except Exception as e: logger.critical(f"CRITICAL: Opus load failed: {e}", exc_info=True)

    try:
        bot.add_cog(MusicCog(bot))
        logger.info("MusicCog added to bot successfully.")
    except Exception as e:
         logger.critical(f"CRITICAL: Failed to add MusicCog to bot: {e}", exc_info=True)

# --- End of File ---