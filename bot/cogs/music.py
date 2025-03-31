# --- bot/cogs/music.py ---

import nextcord
import nextcord.ui
from nextcord.ext import commands
import asyncio
import yt_dlp
import logging
import functools
from collections import deque
from typing import TYPE_CHECKING, Union

# --- Type Hinting Forward Reference ---
if TYPE_CHECKING:
    from __main__ import Bot
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
        self.source_url = source_url; self.title = title; self.webpage_url = webpage_url
        self.duration = duration; self.requester = requester

    def format_duration(self):
        if self.duration is None: return "N/A"
        try: duration_int = int(self.duration)
        except (ValueError, TypeError): return "N/A"
        mins, secs = divmod(duration_int, 60); hrs, mins = divmod(mins, 60)
        return f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs > 0 else f"{mins:02d}:{secs:02d}"

# Forward declare classes
class GuildMusicState: pass
class MusicCog: pass

# --- Music Player View ---
class MusicPlayerView(nextcord.ui.View):
    def __init__(self, music_cog: 'MusicCog', guild_id: int, timeout=None):
        super().__init__(timeout=timeout); self.music_cog = music_cog; self.guild_id = guild_id
        self._update_buttons()

    def _get_state(self) -> Union['GuildMusicState', None]:
        return self.music_cog.guild_states.get(self.guild_id) if self.music_cog else None

    def _update_buttons(self):
        state = self._get_state(); vc = state.voice_client if state else None
        conn = state and vc and vc.is_connected(); play = conn and vc.is_playing()
        paused = conn and vc.is_paused(); active = play or paused; has_q = state and state.queue
        pr_btn: nextcord.ui.Button | None = nextcord.utils.get(self.children, custom_id="music_pause_resume")
        sk_btn: nextcord.ui.Button | None = nextcord.utils.get(self.children, custom_id="music_skip")
        st_btn: nextcord.ui.Button | None = nextcord.utils.get(self.children, custom_id="music_stop")
        qu_btn: nextcord.ui.Button | None = nextcord.utils.get(self.children, custom_id="music_queue")
        if not conn or not state:
            for btn in [pr_btn, sk_btn, st_btn, qu_btn]:
                if btn: btn.disabled = True
            return
        if pr_btn: pr_btn.disabled = not active;
        if paused: pr_btn.label="Resume"; pr_btn.emoji="▶️"; pr_btn.style=nextcord.ButtonStyle.green
        else: pr_btn.label="Pause"; pr_btn.emoji="⏸️"; pr_btn.style=nextcord.ButtonStyle.secondary
        if sk_btn: sk_btn.disabled = not active or not has_q
        if st_btn: st_btn.disabled = not active
        if qu_btn: qu_btn.disabled = False

    async def interaction_check(self, interaction: nextcord.Interaction) -> bool:
        state = self._get_state()
        if not interaction.user or not isinstance(interaction.user, nextcord.Member) or not interaction.user.voice or not interaction.user.voice.channel: await interaction.response.send_message("Need VC.", ephemeral=True); return False
        if not state or not state.voice_client or not state.voice_client.is_connected() or state.voice_client.channel != interaction.user.voice.channel: await interaction.response.send_message("Need same VC.", ephemeral=True); return False
        return True

    @nextcord.ui.button(label="Pause", emoji="⏸️", style=nextcord.ButtonStyle.secondary, custom_id="music_pause_resume")
    async def pause_resume_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        state = self._get_state()
        if not state or not state.voice_client or not state.voice_client.is_connected(): await interaction.response.defer(ephemeral=True); return await interaction.followup.send("Not connected.", ephemeral=True)
        vc = state.voice_client; action = None
        if vc.is_paused(): vc.resume(); action = "Resumed"
        elif vc.is_playing(): vc.pause(); action = "Paused"
        else: await interaction.response.send_message("Nothing playing.", ephemeral=True); return
        self._update_buttons(); await interaction.response.defer()
        try: await interaction.edit_original_message(view=self); await interaction.followup.send(f"Playback {action}.", ephemeral=True)
        except nextcord.NotFound: logger.warning(f"Edit fail pause/resume (gid {self.guild_id})"); await interaction.followup.send(f"Playback {action}, controls msg missing.", ephemeral=True)
        except Exception as e: logger.error(f"Edit err pause/resume: {e}"); await interaction.followup.send(f"Playback {action}, controls update fail.", ephemeral=True)

    @nextcord.ui.button(label="Skip", emoji="⏭️", style=nextcord.ButtonStyle.secondary, custom_id="music_skip")
    async def skip_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        state = self._get_state()
        if not state or not state.voice_client or not state.voice_client.is_connected() or not (state.voice_client.is_playing() or state.voice_client.is_paused()): return await interaction.response.send_message("Nothing to skip.", ephemeral=True)
        if not state.queue: return await interaction.response.send_message("Queue empty.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        title = state.current_song.title if state.current_song else "current"; state.voice_client.stop(); logger.info(f"[{self.guild_id}] Skipped via button by {interaction.user}")
        await interaction.followup.send(f"Skipped **{title}**.", ephemeral=True)

    @nextcord.ui.button(label="Stop", emoji="⏹️", style=nextcord.ButtonStyle.danger, custom_id="music_stop")
    async def stop_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        state = self._get_state()
        if not state or not state.voice_client or not state.voice_client.is_connected() or not (state.voice_client.is_playing() or state.voice_client.is_paused()): return await interaction.response.send_message("Nothing to stop.", ephemeral=True)
        await interaction.response.defer(ephemeral=True); logger.info(f"[{self.guild_id}] Stopped via button by {interaction.user}")
        await state.stop_playback(); await interaction.followup.send("Playback stopped & queue cleared.", ephemeral=True)

    @nextcord.ui.button(label="Queue", emoji="📜", style=nextcord.ButtonStyle.secondary, custom_id="music_queue")
    async def queue_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        state = self._get_state();
        if not state: return await interaction.response.send_message("State error.", ephemeral=True)
        try:
            if not self.music_cog: return await interaction.response.send_message("Cog error.", ephemeral=True)
            emb = await self.music_cog.build_queue_embed(state)
            if emb: await interaction.response.send_message(embed=emb, ephemeral=True)
            else: await interaction.response.send_message("Queue empty.", ephemeral=True)
        except Exception as e: logger.error(f"Queue button err (gid {self.guild_id}): {e}", exc_info=True); await interaction.response.send_message("Err display queue.", ephemeral=True)

    async def on_timeout(self):
        logger.debug(f"View timeout/stopped (gid {self.guild_id})")
        for item in self.children:
            if isinstance(item, nextcord.ui.Button): item.disabled = True
        state = self._get_state()
        if state and state.current_player_view is self and state.current_player_message_id and state.last_command_channel_id:
            try: chan = self.music_cog.bot.get_channel(state.last_command_channel_id);
            if chan and isinstance(chan, nextcord.TextChannel): msg = await chan.fetch_message(state.current_player_message_id);
            if msg and msg.components: await msg.edit(view=self)
            except (nextcord.NotFound, nextcord.Forbidden, AttributeError) as e: logger.warning(f"Failed edit on view timeout (gid {self.guild_id}): {e}")

# --- Guild Music State ---
class GuildMusicState:
    """Manages music state for a single guild."""
    def __init__(self, bot: commands.Bot, guild_id: int):
        self.bot = bot; self.guild_id = guild_id; self.queue = deque(); self.voice_client: nextcord.VoiceClient | None = None
        self.current_song: Song | None = None; self.volume = 0.5; self.play_next_song = asyncio.Event(); self._playback_task: asyncio.Task | None = None
        self._lock = asyncio.Lock(); self.last_command_channel_id: int | None = None; self.current_player_message_id: int | None = None
        self.current_player_view: MusicPlayerView | None = None

    def _create_now_playing_embed(self, song: Song | None) -> nextcord.Embed | None:
         if not song: return None; emb = nextcord.Embed(title="Now Playing", color=nextcord.Color.green()); emb.description = f"**[{song.title}]({song.webpage_url})**"
         emb.add_field(name="Duration", value=song.format_duration(), inline=True); req = song.requester
         if req: emb.add_field(name="Requested by", value=req.mention, inline=True); emb.set_thumbnail(url=req.display_avatar.url if req.display_avatar else None)
         return emb

    async def _update_player_message(self, embed: nextcord.Embed | None = None, view: nextcord.ui.View | None = None, content: str | None = None):
        logp = f"[{self.guild_id}] PlayerMsg:"; chan_id = self.last_command_channel_id
        if not chan_id: logger.warning(f"{logp} No channel ID."); return
        chan = self.bot.get_channel(chan_id)
        if not chan or not isinstance(chan, nextcord.TextChannel): logger.warning(f"{logp} Bad channel {chan_id}."); self.current_player_message_id=None; self.current_player_view=None; return
        msg_edit = None; msg_id = self.current_player_message_id
        if msg_id:
            try: msg_edit = await chan.fetch_message(msg_id); logger.debug(f"{logp} Found msg {msg_id}")
            except nextcord.NotFound: logger.warning(f"{logp} Msg {msg_id} not found."); self.current_player_message_id=None
            except nextcord.Forbidden: logger.error(f"{logp} Perm fetch {msg_id}."); self.current_player_message_id=None; return
            except Exception as e: logger.error(f"{logp} Err fetch {msg_id}: {e}")
        try:
            if msg_edit: await msg_edit.edit(content=content, embed=embed, view=view); logger.debug(f"{logp} Edited {msg_id}.")
            elif embed or view: new_msg = await chan.send(content=content, embed=embed, view=view); self.current_player_message_id = new_msg.id; self.current_player_view = view; logger.info(f"{logp} Sent new {new_msg.id}.")
            else: logger.debug(f"{logp} Nothing to send/edit.")
        except nextcord.Forbidden: logger.error(f"{logp} Perm send/edit."); self.current_player_message_id=None; self.current_player_view=None
        except nextcord.HTTPException as e: logger.error(f"{logp} HTTP err send/edit: {e}"); if e.status==404 and msg_edit: logger.warning(f"{logp} Msg {msg_id} deleted before edit."); self.current_player_message_id=None; self.current_player_view=None
        except Exception as e: logger.error(f"{logp} Unexpected err update msg: {e}", exc_info=True)

    async def _playback_loop(self):
        await self.bot.wait_until_ready(); logger.info(f"[{self.guild_id}] Playback loop starting.")
        music_cog: MusicCog | None = self.bot.get_cog("Music");
        if not music_cog: logger.critical(f"[{self.guild_id}] MusicCog instance not found!"); return

        while True:
            self.play_next_song.clear(); logp = f"[{self.guild_id}] Loop:"; logger.debug(f"{logp} Top.")
            song_play = None; vc_ok = False

            if self.voice_client and self.voice_client.is_connected():
                 vc_ok = True;
                 if self.voice_client.is_playing() or self.voice_client.is_paused(): await self.play_next_song.wait(); logger.debug(f"{logp} Resume after active VC wait."); continue
            else: # VC not connected Corrected block
                logger.warning(f"{logp} VC disconnected.") # Semicolon removed
                async with self._lock: # async with on new line
                    if self.current_song:
                        self.queue.appendleft(self.current_song)
                        self.current_song = None
                if self.current_player_view:
                    self.current_player_view.stop()
                    self.current_player_view = None
                self.bot.loop.create_task(self._update_player_message(content="*Bot disconnected.*", embed=None, view=None))
                self.current_player_message_id = None
                return # Exit loop

            if vc_ok: # Get Song
                async with self._lock:
                    if self.queue: song_play=self.queue.popleft(); self.current_song=song_play; logger.info(f"{logp} Popped '{song_play.title}'. Q:{len(self.queue)}")
                    else: # Queue empty
                        if self.current_song: # Just finished
                             logger.info(f"{logp} Queue empty after song.")
                             fin_emb = self._create_now_playing_embed(self.current_song);
                             if fin_emb: fin_emb.title="Finished Playing"
                             if self.current_player_view: self.current_player_view.stop()
                             dis_view = self.current_player_view
                             if dis_view:
                                 for item in dis_view.children:
                                     if isinstance(item, nextcord.ui.Button): item.disabled=True
                             self.bot.loop.create_task(self._update_player_message(content="*Queue finished.*", embed=fin_emb, view=dis_view))
                             self.current_song=None; self.current_player_view=None
                        else: logger.debug(f"{logp} Queue remains empty.")

            if not song_play: # Wait if queue empty
                logger.info(f"{logp} Queue empty. Waiting."); await self.play_next_song.wait(); logger.info(f"{logp} Event received."); continue

            # Play Song
            logger.info(f"{logp} Attempting play: {song_play.title}"); src=None; ok=False
            try:
                if not self.voice_client or not self.voice_client.is_connected(): logger.warning(f"{logp} VC disconnected before play."); async with self._lock: self.queue.appendleft(song_play); self.current_song=None; continue
                if self.voice_client.is_playing() or self.voice_client.is_paused(): logger.error(f"{logp} RACE?: VC active."); async with self._lock: self.queue.appendleft(song_play); self.current_song=None; await self.play_next_song.wait(); continue
                orig_src=nextcord.FFmpegPCMAudio(song_play.source_url, before_options=FFMPEG_BEFORE_OPTIONS, options=FFMPEG_OPTIONS); src=nextcord.PCMVolumeTransformer(orig_src, volume=self.volume)
                self.voice_client.play(src, after=lambda e: self._handle_after_play(e)); ok=True; logger.info(f"{logp} play() called.")
                logger.debug(f"{logp} Updating player message for '{song_play.title}'."); np_emb=self._create_now_playing_embed(song_play)
                if self.current_player_view and not self.current_player_view.is_finished(): logger.debug(f"{logp} Stopping previous view."); self.current_player_view.stop()
                logger.debug(f"{logp} Creating new View.");
                try: self.current_player_view = MusicPlayerView(music_cog, self.guild_id); logger.debug(f"{logp} View created. Updating msg.")
                except Exception as e_v: logger.error(f"{logp} VIEW CREATE FAIL: {e_v}", exc_info=True); self.current_player_view=None
                if self.current_player_view: await self._update_player_message(embed=np_emb, view=self.current_player_view, content=None); logger.debug(f"{logp} Update msg call finished. MsgID: {self.current_player_message_id}")
                else: logger.warning(f"{logp} Skip msg update, view fail.")
            except (nextcord.errors.ClientException, ValueError, TypeError) as e: logger.error(f"{logp} Client/Val/Type Err '{song_play.title}': {e}", exc_info=False); await self._notify_channel_error(f"Error playing '{song_play.title}'. Skipping."); self.current_song=None
            except Exception as e: logger.error(f"{logp} Unexpected Err '{song_play.title}': {e}", exc_info=True); await self._notify_channel_error(f"Unexpected error playing '{song_play.title}'. Skipping."); self.current_song=None
            if ok: logger.debug(f"{logp} Waiting event..."); await self.play_next_song.wait(); logger.debug(f"{logp} Event received '{song_play.title}'.")
            else: logger.debug(f"{logp} Play fail, loop continues."); await asyncio.sleep(0.1)

    def _handle_after_play(self, error):
        logp = f"[{self.guild_id}] AfterPlay:";
        if error: logger.error(f"{logp} Error: {error!r}", exc_info=error); asyncio.run_coroutine_threadsafe(self._notify_channel_error(f"Playback error: {error}. Skip."), self.bot.loop)
        else: logger.debug(f"{logp} OK.")
        logger.debug(f"{logp} Setting event.")
        self.bot.loop.call_soon_threadsafe(self.play_next_song.set)

    def start_playback_loop(self):
        if self._playback_task is None or self._playback_task.done(): logger.info(f"[{self.guild_id}] Starting loop task."); self._playback_task=self.bot.loop.create_task(self._playback_loop()); self._playback_task.add_done_callback(self._handle_loop_completion)
        else: logger.debug(f"[{self.guild_id}] Loop already running.")
        if self.queue and not self.play_next_song.is_set():
             if self.voice_client and not self.voice_client.is_playing(): logger.debug(f"[{self.guild_id}] Setting event (Q not empty, VC idle)."); self.play_next_song.set()

    def _handle_loop_completion(self, task: asyncio.Task):
        gid=self.guild_id; logp=f"[{gid}] LoopComplete:"
        try:
            if task.cancelled(): logger.info(f"{logp} Cancelled.")
            elif task.exception(): exc=task.exception(); logger.error(f"{logp} Error:", exc_info=exc); asyncio.run_coroutine_threadsafe(self._notify_channel_error(f"Loop error: {exc}."), self.bot.loop); self.bot.loop.create_task(self.cleanup())
            else: logger.info(f"{logp} Finished gracefully.")
        except Exception as e: logger.error(f"{logp} Handler error: {e}", exc_info=True)
        cog=getattr(self.bot,'get_cog',lambda n:None)("Music");
        if cog and gid in cog.guild_states: self._playback_task=None; logger.debug(f"{logp} Task ref cleared.")
        else: logger.debug(f"{logp} State gone, ref not cleared.")

    async def stop_playback(self):
        logp=f"[{self.guild_id}] StopPlayback:"; v_stop=None; mid_clear=None
        async with self._lock:
            self.queue.clear(); vc=self.voice_client
            if vc and vc.is_connected() and (vc.is_playing() or vc.is_paused()): logger.info(f"{logp} Stopping track."); vc.stop()
            self.current_song=None; logger.info(f"{logp} Queue cleared.")
            v_stop=self.current_player_view; mid_clear=self.current_player_message_id
            self.current_player_view=None; self.current_player_message_id=None
            if not self.play_next_song.is_set(): logger.debug(f"{logp} Setting event."); self.play_next_song.set()
        if v_stop and not v_stop.is_finished(): v_stop.stop(); logger.debug(f"{logp} Stopped view.")
        if mid_clear and self.last_command_channel_id:
            logger.debug(f"{logp} Scheduling msg clear."); dis_view=v_stop
            if dis_view:
                 for item in dis_view.children:
                     if isinstance(item, nextcord.ui.Button): item.disabled=True
            self.bot.loop.create_task(self._update_player_message(content="*Playback stopped.*", embed=None, view=dis_view))

    async def cleanup(self):
        gid=self.guild_id; logp=f"[{gid}] Cleanup:"; logger.info(f"{logp} Starting.")
        await self.stop_playback(); task=self._playback_task
        if task and not task.done(): logger.info(f"{logp} Cancelling loop."); task.cancel();
        try: await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError): pass
        except Exception as e: logger.error(f"{logp} Err awaiting cancel: {e}", exc_info=True)
        self._playback_task=None; vc=self.voice_client
        if vc and vc.is_connected(): logger.info(f"{logp} Disconnecting VC.");
        try: await vc.disconnect(force=True)
        except Exception as e: logger.error(f"{logp} Err disconnecting VC: {e}", exc_info=True)
        self.voice_client=None; self.current_song=None; self.current_player_view=None; self.current_player_message_id=None
        logger.info(f"{logp} Finished.")

    async def _notify_channel_error(self, message: str):
        cid=self.last_command_channel_id; gid=self.guild_id
        if not cid: logger.warning(f"[{gid}] No channel ID for error."); return
        try: chan=self.bot.get_channel(cid);
        if chan and isinstance(chan, nextcord.abc.Messageable): emb=nextcord.Embed(title="Music Error", description=message, color=nextcord.Color.red()); await chan.send(embed=emb); logger.debug(f"[{gid}] Sent error notification.")
        else: logger.warning(f"[{gid}] Cannot find channel {cid} for error.")
        except Exception as e: logger.error(f"[{gid}] Failed sending error notification: {e}", exc_info=True)

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
         logp=f"[{state.guild_id}] QueueEmbed:"; logger.debug(f"{logp} Building.")
         async with state._lock: cs=state.current_song; q_copy=list(state.queue)
         if not cs and not q_copy: logger.debug(f"{logp} Empty."); return None
         emb=nextcord.Embed(title="Queue", color=nextcord.Color.blurple()); display="Nothing playing."; q_dur=0
         if cs:
             icon="❓";
             if state.voice_client and state.voice_client.is_connected(): icon="▶️ Playing" if state.voice_client.is_playing() else ("⏸️ Paused" if state.voice_client.is_paused() else "⏹️ Idle")
             req=cs.requester.mention if cs.requester else "Unk"; display=f"{icon}: **[{cs.title}]({cs.webpage_url})** `[{cs.format_duration()}]` Req:{req}"
         emb.add_field(name="Now Playing", value=display, inline=False)
         if q_copy:
             qls=[]; clen=0; clim=950; shown=0; max_list=20
             for i, song in enumerate(q_copy):
                 if song.duration: try: q_dur+=int(song.duration) except: pass
                 if shown < max_list:
                     req=song.requester.display_name if song.requester else "Unk"; line=f"`{i+1}.` [{song.title}]({song.webpage_url}) `[{song.format_duration()}]` R:{req}\n"
                     if clen+len(line)<=clim: qls.append(line); clen+=len(line); shown+=1
                     else: rem=len(q_copy)-i; if rem>0: qls.append(f"\n...+{rem} more."); break
             if shown==max_list and len(q_copy)>max_list: rem=len(q_copy)-max_list; qls.append(f"\n...+{rem} more.")
             dur_str=Song(None,None,None,q_dur,None).format_duration() if q_dur>0 else "N/A"; header=f"Up Next ({len(q_copy)} song{'s'[:len(q_copy)^1]}, Total:{dur_str})"
             q_val="".join(qls).strip();
             if not q_val and len(q_copy)>0: q_val=f"Queue:{len(q_copy)} songs..."
             if q_val: emb.add_field(name=header, value=q_val, inline=False)
             else: emb.add_field(name="Up Next", value="Empty.", inline=False)
         else: emb.add_field(name="Up Next", value="Empty.", inline=False)
         total=len(q_copy)+(1 if cs else 0); vol=int(state.volume*100) if hasattr(state,'volume') else "N/A"; emb.set_footer(text=f"Total:{total}|Vol:{vol}%"); logger.debug(f"{logp} Built."); return emb

    # --- Extraction methods ---
    async def _process_entry(self, entry_data: dict, requester: nextcord.Member) -> Song | None:
        logp=f"[{self.bot.user.id or 'Bot'}] EntryProc:";
        if not entry_data: logger.warning(f"{logp} Empty entry."); return None
        title=entry_data.get('title',entry_data.get('id','N/A'))
        if entry_data.get('_type')=='url' and 'url' in entry_data and 'formats' not in entry_data:
            try: logger.debug(f"{logp} Flat, re-extract: {title}"); loop=asyncio.get_event_loop(); opts=YDL_OPTS.copy(); opts['noplaylist']=True; opts['extract_flat']=False; ydl=yt_dlp.YoutubeDL(opts); part=functools.partial(ydl.extract_info, entry_data['url'], download=False); full=await loop.run_in_executor(None, part);
            if not full: logger.warning(f"{logp} Re-extract fail: {entry_data['url']}"); return None
            entry_data=full; title=entry_data.get('title', entry_data.get('id','N/A')); logger.debug(f"{logp} Re-extract OK: {title}")
            except Exception as e: logger.error(f"{logp} Re-extract error: {e}", exc_info=True); return None
        logger.debug(f"{logp} Processing: {title}"); url=None
        if 'url' in entry_data and entry_data.get('protocol') in ('http','https') and entry_data.get('acodec')!='none': url=entry_data['url']; logger.debug(f"{logp} Using pre-selected url.")
        elif 'formats' in entry_data:
             formats=entry_data.get('formats',[]); fmt=None; pref=['opus','vorbis','aac']
             for c in pref:
                 for f in formats: if(f.get('url') and f.get('protocol') in ('https','http') and f.get('acodec')==c and f.get('vcodec')=='none'): fmt=f; logger.debug(f"{logp} Found pref: {c}"); break
                 if fmt: break
             if not fmt:
                 for f in formats: fid=f.get('format_id','').lower(); note=f.get('format_note','').lower(); if((('bestaudio' in fid or 'bestaudio' in note) or fid=='bestaudio') and f.get('url') and f.get('protocol') in ('https','http') and f.get('acodec')!='none'): fmt=f; logger.debug(f"{logp} Found 'bestaudio'."); break
             if not fmt:
                 for f in formats: if(f.get('url') and f.get('protocol') in ('https','http') and f.get('acodec')!='none' and f.get('vcodec')=='none'): fmt=f; logger.debug(f"{logp} Using fallback audio-only."); break
             if not fmt:
                 for f in formats: if(f.get('url') and f.get('protocol') in ('https','http') and f.get('acodec')!='none'): fmt=f; logger.warning(f"{logp} Using last resort audio."); break
             if fmt: url=fmt.get('url'); logger.debug(f"{logp} Selected fmt ID {fmt.get('format_id')}")
             else: logger.warning(f"{logp} No suitable HTTP/S format.")
        elif 'requested_formats' in entry_data and not url: req=entry_data.get('requested_formats'); if req: fmt=req[0]; if fmt.get('url') and fmt.get('protocol') in ('https','http'): url=fmt.get('url'); logger.debug(f"{logp} Using requested_formats url.")
        logger.debug(f"{logp} Final url: {'Yes' if url else 'No'}")
        if not url: logger.warning(f"{logp} No URL for: {title}. Skip."); return None
        try: wurl=entry_data.get('webpage_url') or entry_data.get('original_url','N/A'); song=Song(url, entry_data.get('title','Unknown'), wurl, entry_data.get('duration'), requester); logger.debug(f"{logp} Created Song: {song.title}"); return song
        except Exception as e: logger.error(f"{logp} Error create Song: {e}", exc_info=True); return None

    async def _extract_info(self, query: str, requester: nextcord.Member) -> tuple[str | None, list[Song]]:
        logp=f"[{self.bot.user.id or 'Bot'}] YTDL:"; logger.info(f"{logp} Extracting: '{query}' (Req:{requester.name})"); songs=[]; pl_title=None
        try:
            loop=asyncio.get_event_loop(); part_np=functools.partial(self.ydl.extract_info, query, download=False, process=False); data=await loop.run_in_executor(None, part_np)
            if not data: logger.warning(f"{logp} No data (initial)."); return "err_nodata",[]
            entries=data.get('entries')
            if entries:
                pl_title=data.get('title','Unk Playlist'); logger.info(f"{logp} Playlist: '{pl_title}'. Proc..."); pc=0; oc=0
                for entry in entries: oc+=1; if entry: s=await self._process_entry(entry, requester); if s: songs.append(s); pc+=1
                logger.info(f"{logp} PL done. Add {pc}/{oc}.")
            else:
                logger.info(f"{logp} Single entry. Re-extract w/ proc...");
                try: part_p=functools.partial(self.ydl.extract_info, query, download=False); pdata=await loop.run_in_executor(None, part_p)
                if not pdata: logger.warning(f"{logp} Re-extract no data."); return "err_nodata_reextract",[]
                song=await self._process_entry(pdata, requester)
                if song: songs.append(song); logger.info(f"{logp} Single OK: {song.title}")
                else: logger.warning(f"{logp} Single process fail."); return "err_process_single_failed",[]
                except yt_dlp.utils.DownloadError as e1: logger.error(f"{logp} Single DL Err: {e1}"); err=str(e1).lower(); c='ds'; if "unsupported" in err: c='unsupported'; elif "unavailable" in err: c='unavailable'; elif "private" in err: c='private'; elif "age" in err: c='age_restricted'; elif "webpage" in err: c='network'; return f"err_{c}",[]
                except Exception as e1: logger.error(f"{logp} Single Extract Err: {e1}", exc_info=True); return "err_extraction_single",[]
            return pl_title, songs
        except yt_dlp.utils.DownloadError as e0: logger.error(f"{logp} Initial DL Err: {e0}"); err=str(e0).lower(); c='di'; if "unsupported" in err: c='unsupported'; elif "webpage" in err: c='network'; return f"err_{c}",[]
        except Exception as e0: logger.error(f"{logp} Initial Extract Err: {e0}", exc_info=True); return "err_extraction_initial",[]

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
            if uleft and balone: logger.info(f"[{gid}] Bot alone, pausing.");
            if state.voice_client and state.voice_client.is_playing(): state.voice_client.pause();
            if state.current_player_view: state.current_player_view._update_buttons(); self.bot.loop.create_task(state._update_player_message(view=state.current_player_view))
            elif ujoined and state.voice_client and state.voice_client.is_paused() and len(vstates)>1: logger.info(f"[{gid}] User joined, resuming."); state.voice_client.resume();
            if state.current_player_view: state.current_player_view._update_buttons(); self.bot.loop.create_task(state._update_player_message(view=state.current_player_view))

    # --- Commands ---
    @commands.command(name='play', aliases=['p'], help="Plays songs.")
    @commands.guild_only()
    async def play_command(self, ctx: commands.Context, *, query: str):
        if not ctx.guild: return; state=self.get_guild_state(ctx.guild.id); state.last_command_channel_id=ctx.channel.id; logp=f"[{ctx.guild.id}] PlayCmd:"
        logger.info(f"{logp} Play cmd: '{query}' by {ctx.author.name}")
        if not state.voice_client or not state.voice_client.is_connected():
            if ctx.author.voice and ctx.author.voice.channel: logger.info(f"{logp} Joining."); await ctx.invoke(self.join_command); state=self.get_guild_state(ctx.guild.id);
            if not state.voice_client or not state.voice_client.is_connected(): logger.warning(f"{logp} Join fail."); return
            else: logger.info(f"{logp} Join OK."); state.last_command_channel_id=ctx.channel.id
            else: return await ctx.send("Need VC.")
        elif not ctx.author.voice or ctx.author.voice.channel != state.voice_client.channel: return await ctx.send(f"Need same VC.")
        pl_title=None; songs_add=[]; err_code=None; task=asyncio.create_task(ctx.trigger_typing())
        try: res=await self._extract_info(query, ctx.author);
        if isinstance(res[0],str) and res[0].startswith("err_"): err_code=res[0][4:]
        else: pl_title, songs_add=res
        except Exception as e: logger.error(f"{logp} Extract exception: {e}", exc_info=True); err_code="internal_extract"
        finally: if task and not task.done(): try: task.cancel() except: pass
        if err_code: map={'unsupported':"Unsupported.",'unavailable':"Unavailable.",'private':"Private.",'age_restricted':"Age restrict.",'network':"Network err.",'download_initial':"Err fetch initial.",'download_single':"Err fetch track.",'nodata':"No data.",'nodata_reextract':"No data re-fetch.",'process_single_failed':"Fail process track.",'extraction_initial':"Err proc initial.",'extraction_single':"Err proc track.",'internal_extraction':"Internal fetch err."}; msg=map.get(err_code,"Unk fetch err."); return await ctx.send(msg)
        if not songs_add: return await ctx.send(f"Playlist '{pl_title}', no songs." if pl_title else "No songs found.")
        logger.debug(f"{logp} Extracted {len(songs_add)}. Titles: {[s.title[:30]+'...' for s in songs_add]}")
        added=0; start_pos=0; was_empty=False
        async with state._lock: was_empty=not state.queue and not state.current_song; start_pos=max(1,len(state.queue)+(1 if state.current_song else 0)); state.queue.extend(songs_add); added=len(songs_add); logger.info(f"{logp} Added {added}. New Q: {len(state.queue)}")
        if added>0:
            try:
                if not was_empty:
                    emb=nextcord.Embed(color=nextcord.Color.blue()); first=songs_add[0]
                    if pl_title and added>1: emb.title="Playlist Queued"; link=query if query.startswith('http') else None; desc=f"**[{pl_title}]({link})**" if link else f"**{pl_title}**"; emb.description=f"Added **{added}** from {desc}."
                    elif added==1: emb.title="Added to Queue"; emb.description=f"[{first.title}]({first.webpage_url})"; emb.add_field(name="Pos", value=f"#{start_pos}", inline=True)
                    else: emb=None
                    if emb: foot=f"Req by {ctx.author.display_name}"; icon=ctx.author.display_avatar.url if ctx.author.display_avatar else None; emb.set_footer(text=foot, icon_url=icon); await ctx.send(embed=emb, delete_after=15.0)
                else: await ctx.message.add_reaction('✅')
            except Exception as e: logger.error(f"{logp} Feedback error: {e}", exc_info=True)
        if added>0: logger.debug(f"{logp} Ensuring loop."); state.start_playback_loop()
        logger.debug(f"{logp} Play cmd finished.")

    @commands.command(name='join',aliases=['connect','j'],help="Connects bot.")
    @commands.guild_only()
    async def join_command(self, ctx:commands.Context):
        if not ctx.author.voice or not ctx.author.voice.channel: return await ctx.send("Need VC.")
        if not ctx.guild: return; chan=ctx.author.voice.channel; state=self.get_guild_state(ctx.guild.id); state.last_command_channel_id=ctx.channel.id
        async with state._lock:
            if state.voice_client and state.voice_client.is_connected():
                if state.voice_client.channel == chan: await ctx.send(f"Already in.")
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

    @commands.command(name='skip',aliases=['s','next'],help="Skips song (use button).")
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
        if not nextcord.opus.is_loaded():
            logger.info(f"Opus not loaded. Trying path: {OPUS_PATH}")
            nextcord.opus.load_opus(OPUS_PATH)
        # Re-check after load attempt
        if nextcord.opus.is_loaded():
             logger.info("Opus loaded successfully.")
        else:
             logger.critical("Opus load attempt finished, but is_loaded() is still false.")
        # Removed redundant `else:` block that was causing issues
    except nextcord.opus.OpusNotLoaded:
         logger.critical(f"CRITICAL: Opus library not found at path '{OPUS_PATH}' or failed to load.")
    except Exception as e:
         logger.critical(f"CRITICAL: Opus load failed: {e}", exc_info=True)

    try:
        bot.add_cog(MusicCog(bot))
        logger.info("MusicCog added to bot successfully.")
    except Exception as e:
         logger.critical(f"CRITICAL: Failed to add MusicCog to bot: {e}", exc_info=True)

# --- End of File ---