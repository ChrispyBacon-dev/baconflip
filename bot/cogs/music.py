# --- bot/cogs/music.py ---

import nextcord
import nextcord.ui # <<< Import UI module
from nextcord.ext import commands
import asyncio
import yt_dlp
import logging
import functools
from collections import deque
from typing import TYPE_CHECKING, Union # For type hinting and Union

# --- Type Hinting Forward Reference ---
if TYPE_CHECKING:
    from __main__ import Bot # Assuming your main bot class is named Bot
    # Forward declare classes used in type hints before definition
    class GuildMusicState: pass
    class MusicCog: pass

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

# --- Song Class ---
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

# Forward declare classes needed for type hints before they are fully defined
class GuildMusicState: pass
class MusicCog: pass

# --- Music Player View ---
class MusicPlayerView(nextcord.ui.View):
    def __init__(self, music_cog: 'MusicCog', guild_id: int, timeout=None):
        super().__init__(timeout=timeout)
        self.music_cog = music_cog
        self.guild_id = guild_id
        self._update_buttons()

    def _get_state(self) -> Union['GuildMusicState', None]: # Use Union for forward ref
        if self.music_cog:
             return self.music_cog.guild_states.get(self.guild_id)
        return None

    def _update_buttons(self):
        state = self._get_state()
        vc = state.voice_client if state else None
        is_connected = state and vc and vc.is_connected()
        is_playing = is_connected and vc.is_playing()
        is_paused = is_connected and vc.is_paused()
        is_active = is_playing or is_paused
        has_queue = state and state.queue

        pause_resume_button: nextcord.ui.Button | None = nextcord.utils.get(self.children, custom_id="music_pause_resume")
        skip_button: nextcord.ui.Button | None = nextcord.utils.get(self.children, custom_id="music_skip")
        stop_button: nextcord.ui.Button | None = nextcord.utils.get(self.children, custom_id="music_stop")
        queue_button: nextcord.ui.Button | None = nextcord.utils.get(self.children, custom_id="music_queue")

        if not is_connected or not state:
            for button in [pause_resume_button, skip_button, stop_button, queue_button]:
                if button: button.disabled = True
            return

        if pause_resume_button:
            pause_resume_button.disabled = not is_active
            if is_paused: pause_resume_button.label = "Resume"; pause_resume_button.emoji = "▶️"; pause_resume_button.style = nextcord.ButtonStyle.green
            else: pause_resume_button.label = "Pause"; pause_resume_button.emoji = "⏸️"; pause_resume_button.style = nextcord.ButtonStyle.secondary
        if skip_button: skip_button.disabled = not is_active or not has_queue
        if stop_button: stop_button.disabled = not is_active
        if queue_button: queue_button.disabled = False

    async def interaction_check(self, interaction: nextcord.Interaction) -> bool:
        state = self._get_state()
        if not interaction.user or not isinstance(interaction.user, nextcord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("You must be in a voice channel.", ephemeral=True); return False
        if not state or not state.voice_client or not state.voice_client.is_connected() or state.voice_client.channel != interaction.user.voice.channel:
            await interaction.response.send_message("You must be in the bot's voice channel.", ephemeral=True); return False
        return True

    @nextcord.ui.button(label="Pause", emoji="⏸️", style=nextcord.ButtonStyle.secondary, custom_id="music_pause_resume")
    async def pause_resume_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        state = self._get_state()
        if not state or not state.voice_client or not state.voice_client.is_connected():
             await interaction.response.defer(ephemeral=True); return await interaction.followup.send("Bot not connected.", ephemeral=True)
        vc = state.voice_client; action_taken = None
        if vc.is_paused(): vc.resume(); action_taken = "Resumed"
        elif vc.is_playing(): vc.pause(); action_taken = "Paused"
        else: await interaction.response.send_message("Nothing is playing.", ephemeral=True); return
        self._update_buttons()
        await interaction.response.defer()
        try:
             await interaction.edit_original_message(view=self)
             await interaction.followup.send(f"Playback {action_taken}.", ephemeral=True)
        except nextcord.NotFound: logger.warning(f"Failed edit pause/resume (guild {self.guild_id}), msg deleted?"); await interaction.followup.send(f"Playback {action_taken}, controls msg missing.", ephemeral=True)
        except Exception as e: logger.error(f"Error edit pause/resume: {e}"); await interaction.followup.send(f"Playback {action_taken}, controls update failed.", ephemeral=True)

    @nextcord.ui.button(label="Skip", emoji="⏭️", style=nextcord.ButtonStyle.secondary, custom_id="music_skip")
    async def skip_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        state = self._get_state()
        if not state or not state.voice_client or not state.voice_client.is_connected() or not (state.voice_client.is_playing() or state.voice_client.is_paused()): return await interaction.response.send_message("Nothing to skip.", ephemeral=True)
        if not state.queue: return await interaction.response.send_message("Queue empty, cannot skip.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        skipped_title = state.current_song.title if state.current_song else "current song"
        state.voice_client.stop(); logger.info(f"[{self.guild_id}] Skipped via button by {interaction.user}")
        await interaction.followup.send(f"Skipped **{skipped_title}**.", ephemeral=True)

    @nextcord.ui.button(label="Stop", emoji="⏹️", style=nextcord.ButtonStyle.danger, custom_id="music_stop")
    async def stop_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        state = self._get_state()
        if not state or not state.voice_client or not state.voice_client.is_connected() or not (state.voice_client.is_playing() or state.voice_client.is_paused()): return await interaction.response.send_message("Nothing to stop.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        logger.info(f"[{self.guild_id}] Stopped via button by {interaction.user}")
        await state.stop_playback() # Handles view stop & message update
        await interaction.followup.send("Playback stopped & queue cleared.", ephemeral=True)

    @nextcord.ui.button(label="Queue", emoji="📜", style=nextcord.ButtonStyle.secondary, custom_id="music_queue")
    async def queue_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        state = self._get_state()
        if not state: return await interaction.response.send_message("Bot state error.", ephemeral=True)
        try:
            if not self.music_cog: return await interaction.response.send_message("Internal cog error.", ephemeral=True)
            q_embed = await self.music_cog.build_queue_embed(state)
            if q_embed: await interaction.response.send_message(embed=q_embed, ephemeral=True)
            else: await interaction.response.send_message("Queue empty & nothing playing.", ephemeral=True)
        except Exception as e: logger.error(f"Error queue button (guild {self.guild_id}): {e}", exc_info=True); await interaction.response.send_message("Error displaying queue.", ephemeral=True)

    async def on_timeout(self):
        logger.debug(f"View timed out/stopped for guild {self.guild_id}")
        for item in self.children:
            if isinstance(item, nextcord.ui.Button): item.disabled = True
        state = self._get_state()
        # Only attempt edit if view hasn't already been explicitly cleared/stopped elsewhere
        if state and state.current_player_view is self and state.current_player_message_id and state.last_command_channel_id:
            try:
                channel = self.music_cog.bot.get_channel(state.last_command_channel_id)
                if channel and isinstance(channel, nextcord.TextChannel):
                    msg = await channel.fetch_message(state.current_player_message_id)
                    if msg and msg.components: await msg.edit(view=self) # Show disabled buttons
            except (nextcord.NotFound, nextcord.Forbidden, AttributeError) as e: logger.warning(f"Failed edit on view timeout (guild {self.guild_id}): {e}")


# --- Guild Music State ---
class GuildMusicState:
    """Manages music state for a single guild."""
    def __init__(self, bot: commands.Bot, guild_id: int):
        self.bot = bot; self.guild_id = guild_id
        self.queue = deque(); self.voice_client: nextcord.VoiceClient | None = None
        self.current_song: Song | None = None; self.volume = 0.5
        self.play_next_song = asyncio.Event(); self._playback_task: asyncio.Task | None = None
        self._lock = asyncio.Lock(); self.last_command_channel_id: int | None = None
        self.current_player_message_id: int | None = None
        self.current_player_view: MusicPlayerView | None = None

    def _create_now_playing_embed(self, song: Song | None) -> nextcord.Embed | None:
         if not song: return None
         embed = nextcord.Embed(title="Now Playing", color=nextcord.Color.green())
         embed.description = f"**[{song.title}]({song.webpage_url})**"
         embed.add_field(name="Duration", value=song.format_duration(), inline=True)
         requester = song.requester
         if requester: embed.add_field(name="Requested by", value=requester.mention, inline=True); embed.set_thumbnail(url=requester.display_avatar.url if requester.display_avatar else None)
         return embed

    async def _update_player_message(self, embed: nextcord.Embed | None = None, view: nextcord.ui.View | None = None, content: str | None = None):
        log_prefix = f"[{self.guild_id}] PlayerMsg:"
        if not self.last_command_channel_id: logger.warning(f"{log_prefix} Cannot update, channel ID missing."); return
        channel = self.bot.get_channel(self.last_command_channel_id)
        if not channel or not isinstance(channel, nextcord.TextChannel): logger.warning(f"{log_prefix} Cannot find/use channel {self.last_command_channel_id}."); self.current_player_message_id = None; self.current_player_view = None; return
        message_to_edit = None
        if self.current_player_message_id:
            try: message_to_edit = await channel.fetch_message(self.current_player_message_id); logger.debug(f"{log_prefix} Found existing msg {self.current_player_message_id}")
            except nextcord.NotFound: logger.warning(f"{log_prefix} Msg {self.current_player_message_id} not found."); self.current_player_message_id = None
            except nextcord.Forbidden: logger.error(f"{log_prefix} Perm error fetching msg {self.current_player_message_id}."); self.current_player_message_id = None; return
            except Exception as e: logger.error(f"{log_prefix} Error fetching msg {self.current_player_message_id}: {e}")
        try:
            if message_to_edit: await message_to_edit.edit(content=content, embed=embed, view=view); logger.debug(f"{log_prefix} Edited msg {self.current_player_message_id}.")
            elif embed or view: new_message = await channel.send(content=content, embed=embed, view=view); self.current_player_message_id = new_message.id; self.current_player_view = view; logger.info(f"{log_prefix} Sent new msg {self.current_player_message_id}.")
            else: logger.debug(f"{log_prefix} No content/view & no existing msg.")
        except nextcord.Forbidden: logger.error(f"{log_prefix} Perm error send/edit in {channel.name}."); self.current_player_message_id = None; self.current_player_view = None
        except nextcord.HTTPException as e: logger.error(f"{log_prefix} HTTP error send/edit: {e}");
        except Exception as e: logger.error(f"{log_prefix} Unexpected error updating msg: {e}", exc_info=True)

    async def _playback_loop(self):
        await self.bot.wait_until_ready()
        logger.info(f"[{self.guild_id}] Playback loop starting.")
        music_cog: MusicCog | None = self.bot.get_cog("Music")
        if not music_cog: logger.critical(f"[{self.guild_id}] MusicCog instance not found!"); return

        while True:
            self.play_next_song.clear()
            log_prefix = f"[{self.guild_id}] Loop:"
            logger.debug(f"{log_prefix} Top of loop.")
            song_to_play = None; vc_valid = False

            if self.voice_client and self.voice_client.is_connected():
                 vc_valid = True
                 if self.voice_client.is_playing() or self.voice_client.is_paused(): await self.play_next_song.wait(); logger.debug(f"{log_prefix} Resuming after active VC wait."); continue
            else: # VC not connected
                logger.warning(f"{log_prefix} VC disconnected."); async with self._lock: if self.current_song: self.queue.appendleft(self.current_song); self.current_song = None
                if self.current_player_view: self.current_player_view.stop(); self.current_player_view = None
                # Use create_task to avoid blocking if loop is awaited elsewhere
                self.bot.loop.create_task(self._update_player_message(content="*Bot disconnected.*", embed=None, view=None)); self.current_player_message_id = None
                return

            if vc_valid: # Get Song
                async with self._lock:
                    if self.queue: song_to_play = self.queue.popleft(); self.current_song = song_to_play; logger.info(f"{log_prefix} Popped '{song_to_play.title}'. Q: {len(self.queue)}")
                    else: # Queue empty
                        if self.current_song: # Just finished
                             logger.info(f"{log_prefix} Playback finished (queue empty).")
                             finished_embed = self._create_now_playing_embed(self.current_song);
                             if finished_embed: finished_embed.title = "Finished Playing"
                             if self.current_player_view: self.current_player_view.stop()
                             disabled_view = self.current_player_view
                             if disabled_view: # Visually disable buttons
                                 for item in disabled_view.children:
                                     if isinstance(item, nextcord.ui.Button): item.disabled = True
                             # Update message async
                             self.bot.loop.create_task(self._update_player_message(content="*Queue finished.*", embed=finished_embed, view=disabled_view))
                             self.current_song = None; self.current_player_view = None # Clear state view ref
                        else: logger.debug(f"{log_prefix} Queue remains empty.")

            if not song_to_play: # Wait if queue empty
                logger.info(f"{log_prefix} Queue empty. Waiting."); await self.play_next_song.wait(); logger.info(f"{log_prefix} Event received."); continue

            # Play Song
            logger.info(f"{log_prefix} Attempting play: {song_to_play.title}")
            source = None; play_successful = False
            try:
                if not self.voice_client or not self.voice_client.is_connected(): logger.warning(f"{log_prefix} VC disconnected before play."); async with self._lock: self.queue.appendleft(song_to_play); self.current_song = None; continue
                if self.voice_client.is_playing() or self.voice_client.is_paused(): logger.error(f"{log_prefix} RACE?: VC active before play call."); async with self._lock: self.queue.appendleft(song_to_play); self.current_song = None; await self.play_next_song.wait(); continue

                original_source = nextcord.FFmpegPCMAudio(song_to_play.source_url, before_options=FFMPEG_BEFORE_OPTIONS, options=FFMPEG_OPTIONS)
                source = nextcord.PCMVolumeTransformer(original_source, volume=self.volume)
                self.voice_client.play(source, after=lambda e: self._handle_after_play(e))
                play_successful = True; logger.info(f"{log_prefix} play() called for {song_to_play.title}")

                # --- Create/Update Player Message ---
                logger.debug(f"{log_prefix} Updating player message for '{song_to_play.title}'.")
                now_playing_embed = self._create_now_playing_embed(song_to_play)
                if self.current_player_view and not self.current_player_view.is_finished(): logger.debug(f"{log_prefix} Stopping previous view."); self.current_player_view.stop()
                logger.debug(f"{log_prefix} Creating new View instance.")
                try:
                    # Pass the cog instance correctly
                    self.current_player_view = MusicPlayerView(music_cog, self.guild_id)
                    logger.debug(f"{log_prefix} View instance created. Calling _update_player_message.")
                except Exception as e_view: logger.error(f"{log_prefix} FAILED TO CREATE VIEW: {e_view}", exc_info=True); self.current_player_view = None
                if self.current_player_view:
                    await self._update_player_message(embed=now_playing_embed, view=self.current_player_view, content=None)
                    logger.debug(f"{log_prefix} _update_player_message call finished. Current msg ID: {self.current_player_message_id}")
                else: logger.warning(f"{log_prefix} Skipping msg update, view creation failed.")
                # ----------------------------------

            except (nextcord.errors.ClientException, ValueError, TypeError) as e: logger.error(f"{log_prefix} Client/Value/Type Err play '{song_to_play.title}': {e}", exc_info=False); await self._notify_channel_error(f"Error playing '{song_to_play.title}'. Skipping."); self.current_song = None # Log less verbose exc_info
            except Exception as e: logger.error(f"{log_prefix} Unexpected Err play '{song_to_play.title}': {e}", exc_info=True); await self._notify_channel_error(f"Unexpected error playing '{song_to_play.title}'. Skipping."); self.current_song = None

            if play_successful: logger.debug(f"{log_prefix} Waiting for event..."); await self.play_next_song.wait(); logger.debug(f"{log_prefix} Event received for '{song_to_play.title}'.")
            else: logger.debug(f"{log_prefix} Play fail, loop continues."); await asyncio.sleep(0.1)

    def _handle_after_play(self, error):
        log_prefix = f"[{self.guild_id}] AfterPlay:"
        if error: logger.error(f"{log_prefix} Playback error: {error!r}", exc_info=error); asyncio.run_coroutine_threadsafe(self._notify_channel_error(f"Playback error: {error}. Skipping."), self.bot.loop)
        else: logger.debug(f"{log_prefix} Song finished successfully.")
        logger.debug(f"{log_prefix} Setting play_next_song event.")
        self.bot.loop.call_soon_threadsafe(self.play_next_song.set)

    def start_playback_loop(self):
        if self._playback_task is None or self._playback_task.done():
            logger.info(f"[{self.guild_id}] Starting playback loop task.")
            self._playback_task = self.bot.loop.create_task(self._playback_loop())
            self._playback_task.add_done_callback(self._handle_loop_completion)
        else: logger.debug(f"[{self.guild_id}] Playback loop already running.")
        if self.queue and not self.play_next_song.is_set():
             if self.voice_client and not self.voice_client.is_playing(): logger.debug(f"[{self.guild_id}] Setting event (queue not empty, VC not playing)."); self.play_next_song.set()

    def _handle_loop_completion(self, task: asyncio.Task):
        guild_id = self.guild_id; log_prefix = f"[{guild_id}] LoopComplete:"
        try:
            if task.cancelled(): logger.info(f"{log_prefix} Loop cancelled.")
            elif task.exception(): exc = task.exception(); logger.error(f"{log_prefix} Loop error:", exc_info=exc); asyncio.run_coroutine_threadsafe(self._notify_channel_error(f"Music loop error: {exc}."), self.bot.loop); self.bot.loop.create_task(self.cleanup())
            else: logger.info(f"{log_prefix} Loop finished gracefully.")
        except Exception as e: logger.error(f"{log_prefix} Error in completion handler: {e}", exc_info=True)
        # Use getattr to safely access cog and check guild_states
        music_cog = getattr(self.bot, 'get_cog', lambda n: None)("Music")
        if music_cog and guild_id in music_cog.guild_states:
            self._playback_task = None; logger.debug(f"{log_prefix} Task ref cleared.")
        else:
            logger.debug(f"{log_prefix} State no longer exists, task ref not cleared.")


    async def stop_playback(self):
        log_prefix = f"[{self.guild_id}] StopPlayback:"
        view_to_stop = None; message_id_to_clear = None
        async with self._lock:
            self.queue.clear()
            vc = self.voice_client
            if vc and vc.is_connected() and (vc.is_playing() or vc.is_paused()): logger.info(f"{log_prefix} Stopping track."); vc.stop()
            self.current_song = None; logger.info(f"{log_prefix} Queue cleared.")
            view_to_stop = self.current_player_view; message_id_to_clear = self.current_player_message_id
            self.current_player_view = None; self.current_player_message_id = None
            if not self.play_next_song.is_set(): logger.debug(f"{log_prefix} Setting event."); self.play_next_song.set()

        if view_to_stop and not view_to_stop.is_finished(): view_to_stop.stop(); logger.debug(f"{log_prefix} Stopped view.")
        if message_id_to_clear and self.last_command_channel_id:
            logger.debug(f"{log_prefix} Scheduling msg clear/update.")
            disabled_view = view_to_stop
            if disabled_view:
                 for item in disabled_view.children:
                     if isinstance(item, nextcord.ui.Button): item.disabled = True
            # Use create_task to avoid blocking
            self.bot.loop.create_task(self._update_player_message(content="*Playback stopped.*", embed=None, view=disabled_view))

    async def cleanup(self):
        guild_id = self.guild_id; log_prefix = f"[{guild_id}] Cleanup:"
        logger.info(f"{log_prefix} Starting.")
        await self.stop_playback()
        task = self._playback_task
        if task and not task.done(): logger.info(f"{log_prefix} Cancelling loop task."); task.cancel();
        try: await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError): pass
        except Exception as e: logger.error(f"{log_prefix} Error awaiting task cancel: {e}", exc_info=True)
        self._playback_task = None
        vc = self.voice_client
        if vc and vc.is_connected(): logger.info(f"{log_prefix} Disconnecting VC.");
        try: await vc.disconnect(force=True)
        except Exception as e: logger.error(f"{log_prefix} Error disconnecting VC: {e}", exc_info=True)
        self.voice_client = None; self.current_song = None
        self.current_player_view = None; self.current_player_message_id = None
        logger.info(f"{log_prefix} Finished.")


    async def _notify_channel_error(self, message: str):
        if not self.last_command_channel_id: logger.warning(f"[{self.guild_id}] No channel ID for error."); return
        try:
            channel = self.bot.get_channel(self.last_command_channel_id)
            if channel and isinstance(channel, nextcord.abc.Messageable): embed = nextcord.Embed(title="Music Bot Error", description=message, color=nextcord.Color.red()); await channel.send(embed=embed); logger.debug(f"[{self.guild_id}] Sent error notification.")
            else: logger.warning(f"[{self.guild_id}] Cannot find channel {self.last_command_channel_id} for error.")
        except Exception as e: logger.error(f"[{self.guild_id}] Failed sending error notification: {e}", exc_info=True)

# --- Music Cog ---
class MusicCog(commands.Cog, name="Music"):
    """Commands for playing music in voice channels."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot; self.guild_states: dict[int, GuildMusicState] = {}
        self.ydl = yt_dlp.YoutubeDL(YDL_OPTS)

    def get_guild_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self.guild_states: self.guild_states[guild_id] = GuildMusicState(self.bot, guild_id)
        return self.guild_states[guild_id]

    async def build_queue_embed(self, state: GuildMusicState) -> nextcord.Embed | None:
         log_prefix = f"[{state.guild_id}] QueueEmbed:"; logger.debug(f"{log_prefix} Building.")
         async with state._lock: current_song = state.current_song; queue_copy = list(state.queue)
         if not current_song and not queue_copy: logger.debug(f"{log_prefix} Empty."); return None
         embed = nextcord.Embed(title="Music Queue", color=nextcord.Color.blurple()); current_display = "Nothing currently playing."; total_queue_duration = 0
         if current_song:
             status_icon = "❓"
             if state.voice_client and state.voice_client.is_connected(): status_icon = "▶️ Playing" if state.voice_client.is_playing() else ("⏸️ Paused" if state.voice_client.is_paused() else "⏹️ Stopped/Idle")
             requester_mention = current_song.requester.mention if current_song.requester else "Unknown"
             current_display = f"{status_icon}: **[{current_song.title}]({current_song.webpage_url})** `[{current_song.format_duration()}]` - Req by {requester_mention}"
         embed.add_field(name="Now Playing", value=current_display, inline=False)
         if queue_copy:
             queue_list_strings = []; current_length = 0; char_limit = 950; songs_shown = 0; max_songs_to_list = 20
             for i, song in enumerate(queue_copy):
                 if song.duration: try: total_queue_duration += int(song.duration) except (ValueError, TypeError): pass
                 if songs_shown < max_songs_to_list:
                     requester_name = song.requester.display_name if song.requester else "Unknown"
                     song_line = f"`{i+1}.` [{song.title}]({song.webpage_url}) `[{song.format_duration()}]` - Req by {requester_name}\n"
                     if current_length + len(song_line) <= char_limit: queue_list_strings.append(song_line); current_length += len(song_line); songs_shown += 1
                     else: remaining = len(queue_copy) - i; if remaining > 0: queue_list_strings.append(f"\n...and {remaining} more song{'s' if remaining != 1 else ''}."); break
             if songs_shown == max_songs_to_list and len(queue_copy) > max_songs_to_list: remaining = len(queue_copy) - max_songs_to_list; queue_list_strings.append(f"\n...and {remaining} more song{'s' if remaining != 1 else ''}.")
             total_dur_str = Song(None,None,None,total_queue_duration,None).format_duration() if total_queue_duration > 0 else "N/A"
             queue_header = f"Up Next ({len(queue_copy)} song{'s' if len(queue_copy) != 1 else ''}, Total Duration: {total_dur_str})"
             queue_value = "".join(queue_list_strings).strip()
             if not queue_value and len(queue_copy) > 0: queue_value = f"Queue contains {len(queue_copy)} song(s)..."
             if queue_value: embed.add_field(name=queue_header, value=queue_value, inline=False)
             else: embed.add_field(name="Up Next", value="No songs in queue.", inline=False)
         else: embed.add_field(name="Up Next", value="No songs in queue.", inline=False)
         total_songs_in_system = len(queue_copy) + (1 if current_song else 0); volume_percent = int(state.volume * 100) if hasattr(state, 'volume') else "N/A"
         embed.set_footer(text=f"Total songs: {total_songs_in_system} | Volume: {volume_percent}%"); logger.debug(f"{log_prefix} Finished building."); return embed

    # --- Extraction methods ---
    async def _process_entry(self, entry_data: dict, requester: nextcord.Member) -> Song | None:
        log_prefix = f"[{self.bot.user.id or 'Bot'}] EntryProc:";
        if not entry_data: logger.warning(f"{log_prefix} Empty entry data."); return None
        entry_title = entry_data.get('title', entry_data.get('id', 'N/A'))
        if entry_data.get('_type') == 'url' and 'url' in entry_data and 'formats' not in entry_data:
            try: logger.debug(f"{log_prefix} Flat entry, re-extracting: {entry_title}"); loop=asyncio.get_event_loop(); opts=YDL_OPTS.copy(); opts['noplaylist']=True; opts['extract_flat']=False; ydl=yt_dlp.YoutubeDL(opts); part=functools.partial(ydl.extract_info, entry_data['url'], download=False); full=await loop.run_in_executor(None, part);
            if not full: logger.warning(f"{log_prefix} Re-extract fail: {entry_data['url']}"); return None
            entry_data=full; entry_title=entry_data.get('title', entry_data.get('id', 'N/A')); logger.debug(f"{log_prefix} Re-extract OK: {entry_title}")
            except Exception as e: logger.error(f"{log_prefix} Re-extract error: {e}", exc_info=True); return None
        logger.debug(f"{log_prefix} Processing: {entry_title}"); url=None
        if 'url' in entry_data and entry_data.get('protocol') in ('http','https') and entry_data.get('acodec') != 'none': url=entry_data['url']; logger.debug(f"{log_prefix} Using pre-selected url.")
        elif 'formats' in entry_data:
             formats=entry_data.get('formats',[]); fmt=None; pref=['opus','vorbis','aac']
             for c in pref:
                 for f in formats: if(f.get('url') and f.get('protocol') in ('https','http') and f.get('acodec')==c and f.get('vcodec')=='none'): fmt=f; logger.debug(f"{log_prefix} Found preferred: {c}"); break
                 if fmt: break
             if not fmt:
                 for f in formats: fid=f.get('format_id','').lower(); note=f.get('format_note','').lower(); if((('bestaudio' in fid or 'bestaudio' in note) or fid=='bestaudio') and f.get('url') and f.get('protocol') in ('https','http') and f.get('acodec')!='none'): fmt=f; logger.debug(f"{log_prefix} Found 'bestaudio'."); break
             if not fmt:
                 for f in formats: if(f.get('url') and f.get('protocol') in ('https','http') and f.get('acodec')!='none' and f.get('vcodec')=='none'): fmt=f; logger.debug(f"{log_prefix} Using fallback audio-only."); break
             if not fmt:
                 for f in formats: if(f.get('url') and f.get('protocol') in ('https','http') and f.get('acodec')!='none'): fmt=f; logger.warning(f"{log_prefix} Using last resort audio."); break
             if fmt: url=fmt.get('url'); logger.debug(f"{log_prefix} Selected format ID {fmt.get('format_id')}")
             else: logger.warning(f"{log_prefix} No suitable HTTP/S format.")
        elif 'requested_formats' in entry_data and not url: req=entry_data.get('requested_formats'); if req: fmt=req[0]; if fmt.get('url') and fmt.get('protocol') in ('https','http'): url=fmt.get('url'); logger.debug(f"{log_prefix} Using requested_formats url.")
        logger.debug(f"{log_prefix} Final url: {'Yes' if url else 'No'}")
        if not url: logger.warning(f"{log_prefix} No URL for: {entry_title}. Skipping."); return None
        try: wurl=entry_data.get('webpage_url') or entry_data.get('original_url','N/A'); song=Song(url, entry_data.get('title','Unknown'), wurl, entry_data.get('duration'), requester); logger.debug(f"{log_prefix} Created Song: {song.title}"); return song
        except Exception as e: logger.error(f"{log_prefix} Error creating Song for {entry_title}: {e}", exc_info=True); return None

    async def _extract_info(self, query: str, requester: nextcord.Member) -> tuple[str | None, list[Song]]:
        log_prefix = f"[{self.bot.user.id or 'Bot'}] YTDL:"; logger.info(f"{log_prefix} Extracting: '{query}' (Req by {requester.name})"); songs=[]; pl_title=None
        try:
            loop=asyncio.get_event_loop(); part_np=functools.partial(self.ydl.extract_info, query, download=False, process=False); data=await loop.run_in_executor(None, part_np)
            if not data: logger.warning(f"{log_prefix} No data (initial)."); return "err_nodata",[]
            entries=data.get('entries')
            if entries:
                pl_title=data.get('title','Unk Playlist'); logger.info(f"{log_prefix} Playlist: '{pl_title}'. Processing..."); pc=0; oc=0
                for entry in entries: oc+=1; if entry: s=await self._process_entry(entry, requester); if s: songs.append(s); pc+=1
                logger.info(f"{log_prefix} Playlist done. Added {pc}/{oc}.")
            else:
                logger.info(f"{log_prefix} Single entry. Re-extracting w/ proc...");
                try: part_p=functools.partial(self.ydl.extract_info, query, download=False); pdata=await loop.run_in_executor(None, part_p)
                if not pdata: logger.warning(f"{log_prefix} Re-extract no data."); return "err_nodata_reextract",[]
                song=await self._process_entry(pdata, requester)
                if song: songs.append(song); logger.info(f"{log_prefix} Single OK: {song.title}")
                else: logger.warning(f"{log_prefix} Single process failed."); return "err_process_single_failed",[]
                except yt_dlp.utils.DownloadError as e1: logger.error(f"{log_prefix} Single DL Error: {e1}"); err=str(e1).lower(); c='ds'; if "unsupported" in err: c='unsupported'; elif "unavailable" in err: c='unavailable'; elif "private" in err: c='private'; elif "age" in err: c='age_restricted'; elif "webpage" in err: c='network'; return f"err_{c}",[]
                except Exception as e1: logger.error(f"{log_prefix} Single Extract Error: {e1}", exc_info=True); return "err_extraction_single",[]
            return pl_title, songs
        except yt_dlp.utils.DownloadError as e0: logger.error(f"{log_prefix} Initial DL Error: {e0}"); err=str(e0).lower(); c='di'; if "unsupported" in err: c='unsupported'; elif "webpage" in err: c='network'; return f"err_{c}",[]
        except Exception as e0: logger.error(f"{log_prefix} Initial Extract Error: {e0}", exc_info=True); return "err_extraction_initial",[]

    # --- Listener ---
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: nextcord.Member, before: nextcord.VoiceState, after: nextcord.VoiceState):
        if not member.guild: return; gid=member.guild.id; state=self.guild_states.get(gid);
        if not state: return
        bchan=state.voice_client.channel if state.voice_client and state.voice_client.is_connected() else None
        if member.id == self.bot.user.id:
            if before.channel and not after.channel: logger.warning(f"[{gid}] Bot disconnected."); await state.cleanup();
            if gid in self.guild_states: del self.guild_states[gid]; logger.info(f"[{gid}] State removed.")
            elif before.channel and after.channel and before.channel != after.channel: logger.info(f"[{gid}] Bot moved.");
            if state.voice_client: state.voice_client.channel=after.channel
        elif bchan:
            uleft=before.channel == bchan and after.channel != bchan; ujoined=before.channel != bchan and after.channel == bchan
            vstates=bchan.voice_states; balone=len(vstates)==1 and self.bot.user.id in vstates
            if uleft and balone:
                logger.info(f"[{gid}] Bot alone, pausing.");
                if state.voice_client and state.voice_client.is_playing(): state.voice_client.pause();
                if state.current_player_view: state.current_player_view._update_buttons(); self.bot.loop.create_task(state._update_player_message(view=state.current_player_view))
            elif ujoined and state.voice_client and state.voice_client.is_paused() and len(vstates)>1:
                logger.info(f"[{gid}] User joined, resuming."); state.voice_client.resume();
                if state.current_player_view: state.current_player_view._update_buttons(); self.bot.loop.create_task(state._update_player_message(view=state.current_player_view))

    # --- Commands ---
    @commands.command(name='play', aliases=['p'], help="Plays songs from URL, search, or playlist.")
    @commands.guild_only()
    async def play_command(self, ctx: commands.Context, *, query: str):
        if not ctx.guild: return; state=self.get_guild_state(ctx.guild.id); state.last_command_channel_id=ctx.channel.id; logp=f"[{ctx.guild.id}] PlayCmd:"
        logger.info(f"{logp} Play cmd: '{query}' by {ctx.author.name}")
        if not state.voice_client or not state.voice_client.is_connected():
            if ctx.author.voice and ctx.author.voice.channel: logger.info(f"{logp} Joining."); await ctx.invoke(self.join_command); state=self.get_guild_state(ctx.guild.id);
            if not state.voice_client or not state.voice_client.is_connected(): logger.warning(f"{logp} Join fail."); return
            else: logger.info(f"{logp} Join OK."); state.last_command_channel_id=ctx.channel.id
            else: return await ctx.send("You need VC for me to join.")
        elif not ctx.author.voice or ctx.author.voice.channel != state.voice_client.channel: return await ctx.send(f"Need to be in {state.voice_client.channel.mention}.")
        pl_title=None; songs_add=[]; err_code=None; task=asyncio.create_task(ctx.trigger_typing())
        try: res=await self._extract_info(query, ctx.author);
        if isinstance(res[0],str) and res[0].startswith("err_"): err_code=res[0][4:]
        else: pl_title, songs_add=res
        except Exception as e: logger.error(f"{logp} Extract exception: {e}", exc_info=True); err_code="internal_extract"
        finally: if task and not task.done(): try: task.cancel() except: pass
        if err_code: map={'unsupported':"Unsupported URL/service.",'unavailable':"Video/playlist unavailable.",'private':"Video/playlist private.",'age_restricted':"Cannot play age-restricted.",'network':"Network error fetching.",'download_initial':"Error fetch initial.",'download_single':"Error fetch track.",'nodata':"Couldn't find data.",'nodata_reextract':"No data re-fetch.",'process_single_failed':"Failed process track.",'extraction_initial':"Error proc initial.",'extraction_single':"Error proc track.",'internal_extraction':"Internal fetch error."}; msg=map.get(err_code,"Unknown fetch error."); return await ctx.send(msg)
        if not songs_add: return await ctx.send(f"Playlist '{pl_title}', no songs added." if pl_title else "No playable songs found.")
        logger.debug(f"{logp} Extracted {len(songs_add)} songs. Titles: {[s.title[:30]+'...' for s in songs_add]}")
        added=0; start_pos=0; was_empty=False
        async with state._lock: was_empty=not state.queue and not state.current_song; start_pos=max(1,len(state.queue)+(1 if state.current_song else 0)); state.queue.extend(songs_add); added=len(songs_add); logger.info(f"{logp} Added {added}. New Q: {len(state.queue)}")
        if added>0:
            try:
                if not was_empty: # Send confirm only if adding to existing
                    emb=nextcord.Embed(color=nextcord.Color.blue()); first=songs_add[0]
                    if pl_title and added>1: emb.title="Playlist Queued"; link=query if query.startswith('http') else None; desc=f"**[{pl_title}]({link})**" if link else f"**{pl_title}**"; emb.description=f"Added **{added}** songs from {desc}."
                    elif added==1: emb.title="Added to Queue"; emb.description=f"[{first.title}]({first.webpage_url})"; emb.add_field(name="Position", value=f"#{start_pos}", inline=True)
                    else: emb=None
                    if emb: foot=f"Req by {ctx.author.display_name}"; icon=ctx.author.display_avatar.url if ctx.author.display_avatar else None; emb.set_footer(text=foot, icon_url=icon); await ctx.send(embed=emb, delete_after=15.0)
                else: await ctx.message.add_reaction('✅') # React if starting playback
            except Exception as e: logger.error(f"{logp} Feedback error: {e}", exc_info=True)
        if added>0: logger.debug(f"{logp} Ensuring loop active."); state.start_playback_loop()
        logger.debug(f"{logp} Play cmd finished.")

    @commands.command(name='join',aliases=['connect','j'],help="Connects bot to your VC.")
    @commands.guild_only()
    async def join_command(self, ctx:commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel: return await ctx.send("Need VC.")
        if not ctx.guild: return; chan=ctx.author.voice.channel; state=self.get_guild_state(ctx.guild.id); state.last_command_channel_id=ctx.channel.id
        async with state._lock:
            if state.voice_client and state.voice_client.is_connected():
                if state.voice_client.channel == chan: await ctx.send(f"Already in {chan.mention}.")
                else: try: await state.voice_client.move_to(chan); await ctx.send(f"Moved.") except Exception as e: await ctx.send(f"Move Err: {e}")
            else:
                try: state.voice_client=await chan.connect(); await ctx.send(f"Connected."); state.start_playback_loop()
                except Exception as e: await ctx.send(f"Connect Err: {e}"); logger.error(f"[{ctx.guild.id}] VC Connect Err: {e}", exc_info=True); del self.guild_states[ctx.guild.id]

    @commands.command(name='leave',aliases=['disconnect','dc','stopbot'],help="Disconnects bot.")
    @commands.guild_only()
    async def leave_command(self, ctx:commands.Context):
        if not ctx.guild: return; state=self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected(): return await ctx.send("Not connected.")
        logger.info(f"[{ctx.guild.id}] Leave cmd by {ctx.author.name}."); await ctx.message.add_reaction('👋'); await state.cleanup()
        if ctx.guild.id in self.guild_states: del self.guild_states[ctx.guild.id]; logger.info(f"[{ctx.guild.id}] State removed.")

    @commands.command(name='skip',aliases=['s','next'],help="Skips current song (use button).")
    @commands.guild_only()
    async def skip_command(self, ctx:commands.Context):
        if not ctx.guild: return; state=self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected(): return await ctx.send("Not connected.")
        vc=state.voice_client; if not vc.is_playing() and not vc.is_paused(): return await ctx.send("Nothing playing.")
        if not state.queue: return await ctx.send("Queue empty.")
        logger.info(f"[{ctx.guild.id}] Skip cmd by {ctx.author.name}."); vc.stop(); await ctx.message.add_reaction('⏭️')

    @commands.command(name='stop',help="Stops playback (use button).")
    @commands.guild_only()
    async def stop_command(self, ctx:commands.Context):
        if not ctx.guild: return; state=self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected(): return await ctx.send("Not connected.")
        if not state.current_song and not state.queue: return await ctx.send("Nothing to stop.")
        logger.info(f"[{ctx.guild.id}] Stop cmd by {ctx.author.name}."); await state.stop_playback(); await ctx.message.add_reaction('⏹️')

    @commands.command(name='pause',help="Pauses song (use button).")
    @commands.guild_only()
    async def pause_command(self, ctx:commands.Context):
        if not ctx.guild: return; state=self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected(): return await ctx.send("Not connected.")
        vc=state.voice_client; if vc.is_paused(): return await ctx.send("Already paused.")
        if not vc.is_playing(): return await ctx.send("Nothing playing.")
        vc.pause(); logger.info(f"[{ctx.guild.id}] Pause cmd by {ctx.author.name}."); await ctx.message.add_reaction('⏸️')

    @commands.command(name='resume',aliases=['unpause'],help="Resumes song (use button).")
    @commands.guild_only()
    async def resume_command(self, ctx:commands.Context):
        if not ctx.guild: return; state=self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected(): return await ctx.send("Not connected.")
        vc=state.voice_client; if vc.is_playing(): return await ctx.send("Already playing.")
        if not vc.is_paused(): return await ctx.send("Nothing paused.")
        vc.resume(); logger.info(f"[{ctx.guild.id}] Resume cmd by {ctx.author.name}."); await ctx.message.add_reaction('▶️')

    @commands.command(name='queue',aliases=['q','nowplaying','np'],help="Shows queue (use button).")
    @commands.guild_only()
    async def queue_command(self, ctx:commands.Context):
         if not ctx.guild: return; state=self.guild_states.get(ctx.guild.id)
         if not state: return await ctx.send("Not active.")
         state.last_command_channel_id=ctx.channel.id # Update channel
         embed=await self.build_queue_embed(state)
         if embed: await ctx.send(embed=embed)
         else: await ctx.send("Queue empty & nothing playing.")

    @commands.command(name='volume',aliases=['vol'],help="Changes volume (0-100).")
    @commands.guild_only()
    async def volume_command(self, ctx:commands.Context, *, volume:int):
        if not ctx.guild: return; state=self.guild_states.get(ctx.guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected(): return await ctx.send("Not connected.")
        if not 0<=volume<=100: return await ctx.send("Volume 0-100.")
        new_vol=volume/100.0; state.volume=new_vol
        if state.voice_client.source and isinstance(state.voice_client.source, nextcord.PCMVolumeTransformer): state.voice_client.source.volume=new_vol
        await ctx.send(f"Volume set to **{volume}%**.")

    # --- Error Handler ---
    async def cog_command_error(self, ctx:commands.Context, error):
        logp=f"[{ctx.guild.id if ctx.guild else 'DM'}] CogErr:"; state=self.guild_states.get(ctx.guild.id) if ctx.guild else None
        if state and hasattr(ctx,'channel'): state.last_command_channel_id=ctx.channel.id
        if isinstance(error, commands.CommandNotFound): return
        elif isinstance(error, commands.CheckFailure): logger.warning(f"{logp} Check fail: {error}"); await ctx.send("No permission.")
        elif isinstance(error, commands.MissingRequiredArgument): await ctx.send(f"Missing arg: `{error.param.name}`.")
        elif isinstance(error, commands.BadArgument): await ctx.send(f"Invalid argument type.")
        elif isinstance(error, commands.CommandInvokeError):
            orig=error.original
            if isinstance(orig, nextcord.HTTPException) and orig.code==50035 and 'embeds.0.fields' in str(orig.text): logger.warning(f"{logp} Embed length error."); await ctx.send("Queue too long."); return
            logger.error(f"{logp} Invoke err '{ctx.command.qualified_name}': {orig.__class__.__name__}: {orig}", exc_info=orig)
            if isinstance(orig, nextcord.errors.ClientException): await ctx.send(f"Voice Error: {orig}")
            else: await ctx.send(f"Internal error running `{ctx.command.name}`.")
        else: logger.error(f"{logp} Unhandled err '{ctx.command.qualified_name}': {type(error).__name__}: {error}", exc_info=error)


# --- Setup Function ---
def setup(bot: commands.Bot):
    OPUS_PATH = '/usr/lib/x86_64-linux-gnu/libopus.so.0' # Adjust if needed
    try:
        if not nextcord.opus.is_loaded(): logger.info(f"Opus not loaded. Trying path: {OPUS_PATH}"); nextcord.opus.load_opus(OPUS_PATH);
        if nextcord.opus.is_loaded(): logger.info("Opus loaded successfully.")
        else: logger.critical("Opus load attempt finished, but is_loaded() is false.")
        else: logger.info("Opus was already loaded.")
    except Exception as e: logger.critical(f"CRITICAL: Opus load failed: {e}", exc_info=True)
    try: bot.add_cog(MusicCog(bot)); logger.info("MusicCog added to bot successfully.")
    except Exception as e: logger.critical(f"CRITICAL: Failed to add MusicCog to bot: {e}", exc_info=True)

# --- End of File ---