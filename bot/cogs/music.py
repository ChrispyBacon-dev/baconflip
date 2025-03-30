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

logger = logging.getLogger(__name__)

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
        minutes, seconds = divmod(self.duration, 60)
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
        while True:
            self.play_next_song.clear() # Reset event for the current iteration
            song_to_play = None

            async with self._lock: # Ensure queue access is safe
                if not self.queue:
                    logger.info(f"[{self.guild_id}] Queue empty. Playback loop pausing.")
                    self.current_song = None
                    # Optional: Add auto-disconnect logic here after a timeout
                    await self.play_next_song.wait() # Wait until signaled to check queue again
                    continue # Re-check queue after being woken up

                song_to_play = self.queue.popleft()
                self.current_song = song_to_play

            if not self.voice_client or not self.voice_client.is_connected():
                logger.warning(f"[{self.guild_id}] Voice client disconnected unexpectedly. Stopping loop.")
                self.current_song = None # Clear current song as we can't play it
                # Attempt to cleanup state? Or let leave command handle it.
                return # Exit the loop if VC is gone

            if song_to_play:
                logger.info(f"[{self.guild_id}] Now playing: {song_to_play.title}")
                source = None
                try:
                    # Create the audio source. FFmpeg is used here.
                    original_source = await nextcord.FFmpegOpusAudio.from_probe(
                        song_to_play.source_url,
                        before_options=FFMPEG_BEFORE_OPTIONS,
                        options=FFMPEG_OPTIONS,
                        method='fallback' # Use fallback if probe fails initially
                    )
                    source = nextcord.PCMVolumeTransformer(original_source, volume=self.volume)

                    # Play the source. The 'after' callback is crucial for the loop.
                    self.voice_client.play(source, after=lambda e: self._handle_after_play(e))

                    # Notify channel (Optional, find the channel context)
                    # await self.notify_channel(f"Now playing: **{song_to_play.title}** [{song_to_play.format_duration()}] requested by {song_to_play.requester.mention}")

                    await self.play_next_song.wait() # Wait until 'after' callback signals completion or skip

                except nextcord.errors.ClientException as e:
                    logger.error(f"[{self.guild_id}] Error playing {song_to_play.title}: {e}")
                    # Handle cases like already playing, etc.
                    # Maybe skip to next?
                    self.play_next_song.set() # Signal to continue loop
                except yt_dlp.utils.DownloadError as e:
                     logger.error(f"[{self.guild_id}] Download Error for {song_to_play.title}: {e}")
                     # Notify channel about error?
                     self.play_next_song.set() # Signal to continue loop
                except Exception as e:
                    logger.error(f"[{self.guild_id}] Unexpected error during playback of {song_to_play.title}: {e}", exc_info=True)
                    # Maybe attempt to play next?
                    self.play_next_song.set() # Signal to continue loop
                finally:
                    # Ensure current song is cleared if loop exits or skips prematurely
                    # (handled by 'after' callback or next loop iteration)
                    pass

    def _handle_after_play(self, error):
        """Callback function run after a song finishes or errors."""
        if error:
            logger.error(f"[{self.guild_id}] Playback error: {error}", exc_info=error)
        else:
            logger.debug(f"[{self.guild_id}] Song finished playing.")

        # Signal the playback loop that it can proceed to the next song
        self.bot.loop.call_soon_threadsafe(self.play_next_song.set)

    def start_playback_loop(self):
        """Starts the playback loop task if not already running."""
        if self._playback_task is None or self._playback_task.done():
            logger.info(f"[{self.guild_id}] Starting playback loop.")
            self._playback_task = self.bot.loop.create_task(self._playback_loop())
        else:
             logger.debug(f"[{self.guild_id}] Playback loop already running.")
        # Ensure the event is set if there are songs waiting and the loop was paused
        if self.queue and not self.play_next_song.is_set():
             self.play_next_song.set()


    async def stop_playback(self):
        """Stops playback and clears the queue."""
        async with self._lock:
            self.queue.clear()
            if self.voice_client and self.voice_client.is_playing():
                self.voice_client.stop() # This will trigger the 'after' callback
            self.current_song = None
            # Cancel the loop task properly if needed, or let it pause via play_next_song.wait()
            # if self._playback_task and not self._playback_task.done():
            #     self._playback_task.cancel()
            #     self._playback_task = None
            logger.info(f"[{self.guild_id}] Playback stopped and queue cleared.")
            self.play_next_song.set() # Wake up loop if waiting

    async def cleanup(self):
        """Cleans up resources (disconnects VC, stops loop)."""
        logger.info(f"[{self.guild_id}] Cleaning up music state.")
        await self.stop_playback() # Clear queue and stop player
        if self._playback_task and not self._playback_task.done():
            self._playback_task.cancel()
            try:
                await self._playback_task # Allow cancellation to process
            except asyncio.CancelledError:
                logger.debug(f"[{self.guild_id}] Playback task cancelled successfully.")
            except Exception as e:
                logger.error(f"[{self.guild_id}] Error during playback task cancellation: {e}", exc_info=True)
            self._playback_task = None

        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect(force=True)
            logger.info(f"[{self.guild_id}] Disconnected voice client during cleanup.")
        self.voice_client = None
        self.current_song = None


class MusicCog(commands.Cog, name="Music"):
    """Commands for playing music in voice channels."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_states: dict[int, GuildMusicState] = {} # Guild ID -> State
        self.ydl = yt_dlp.YoutubeDL(YDL_OPTS) # Reusable YTDL instance

    def get_guild_state(self, guild_id: int) -> GuildMusicState:
        """Gets or creates the music state for a guild."""
        if guild_id not in self.guild_states:
            self.guild_states[guild_id] = GuildMusicState(self.bot, guild_id)
        return self.guild_states[guild_id]

    async def _extract_song_info(self, query: str) -> dict | None:
        """Extracts song info using yt-dlp in an executor."""
        try:
            # Run blocking IO in executor
            loop = asyncio.get_event_loop()
            partial = functools.partial(self.ydl.extract_info, query, download=False)
            data = await loop.run_in_executor(None, partial)

            if not data:
                logger.warning(f"yt-dlp returned no data for query: {query}")
                return None

            # If it's a playlist, extract_info returns a dict with 'entries'
            if 'entries' in data:
                # Take the first item from the playlist if noplaylist=True wasn't effective
                # or handle playlist logic differently if desired
                entry = data['entries'][0]
                logger.debug(f"Query was a playlist, using first entry: {entry.get('title', 'N/A')}")
            else:
                # If it's a single video
                entry = data

            if not entry:
                 logger.warning(f"Could not find a valid entry in yt-dlp data for: {query}")
                 return None

            # Try to get the best audio stream URL
            audio_url = None
            if 'url' in entry: # Often the direct stream URL is here for bestaudio
                audio_url = entry['url']
            elif 'formats' in entry: # Fallback: search formats
                 for f in entry['formats']:
                    # Prioritize opus, then vorbis, then aac, then best audio overall
                    if f.get('acodec') == 'opus' and f.get('vcodec') == 'none':
                        audio_url = f['url']
                        break
                 if not audio_url:
                    for f in entry['formats']:
                        if f.get('acodec') == 'vorbis' and f.get('vcodec') == 'none':
                            audio_url = f['url']
                            break
                 if not audio_url:
                     for f in entry['formats']:
                         if f.get('acodec') == 'aac' and f.get('vcodec') == 'none':
                             audio_url = f['url']
                             break
                 if not audio_url: # Last resort: highest quality audio format found
                     best_audio = None
                     # Try processing formats to find the best audio URL explicitly marked by ytdl
                     try:
                         processed_info = self.ydl.process_ie_result(entry, download=False)
                         best_audio = processed_info.get('requested_formats')
                     except Exception as process_err:
                         logger.warning(f"Could not re-process formats: {process_err}")

                     if best_audio:
                         audio_url = best_audio[0].get('url')
                     elif entry.get('url'): # Final fallback to entry URL if exists and no formats worked
                          audio_url = entry['url']

            if not audio_url:
                logger.error(f"Could not extract playable audio URL for: {entry.get('title', query)}")
                return None

            return {
                'source_url': audio_url,
                'title': entry.get('title', 'Unknown Title'),
                'webpage_url': entry.get('webpage_url', query),
                'duration': entry.get('duration'), # In seconds
                'uploader': entry.get('uploader', 'Unknown Uploader')
            }

        except yt_dlp.utils.DownloadError as e:
            logger.error(f"YTDL DownloadError for '{query}': {e}")
            # Check for specific error types if needed
            if "Unsupported URL" in str(e): return {'error': 'unsupported'}
            if "Video unavailable" in str(e): return {'error': 'unavailable'}
            return {'error': 'download'}
        except Exception as e:
            logger.error(f"Unexpected error extracting info for '{query}': {e}", exc_info=True)
            return {'error': 'extraction'}


    # --- Listener for Voice State Updates (Optional but Recommended) ---
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: nextcord.Member, before: nextcord.VoiceState, after: nextcord.VoiceState):
        """Handles bot disconnection or empty channels."""
        if member.id != self.bot.user.id:
            # Handle users leaving/joining if needed (e.g., pause if channel empty)
            # Check if someone left the bot's channel and the bot is now alone
            if before.channel and self.bot.user in before.channel.members and len(before.channel.members) == 1:
                state = self.get_guild_state(member.guild.id)
                # Ensure the state and voice client are valid and the channel matches
                if state and state.voice_client and state.voice_client.channel == before.channel:
                    logger.info(f"[{member.guild.id}] Last user left channel {before.channel.name}. Pausing playback.")
                    if state.voice_client.is_playing():
                        state.voice_client.pause()
                    # Optional TODO: Add a timer here to leave after X minutes of being alone?

            # Check if someone joined the bot's channel while it was paused (potentially due to being alone)
            elif after.channel and self.bot.user in after.channel.members and len(after.channel.members) > 1:
                 state = self.get_guild_state(member.guild.id)
                 # Ensure the state and voice client are valid and the channel matches
                 if state and state.voice_client and state.voice_client.channel == after.channel:
                     if state.voice_client.is_paused():
                          logger.info(f"[{member.guild.id}] User joined channel {after.channel.name}. Resuming playback.")
                          state.voice_client.resume()

            # If the update wasn't for the bot, we don't need to process bot disconnect logic below
            return # <-- This is the line that caused the error, now correctly indented

        # --- Code below only runs if the voice state update IS for the bot ---
        state = self.get_guild_state(member.guild.id) # Get state safely

        # Bot was disconnected (kicked, moved, channel deleted, etc.)
        if before.channel is not None and after.channel is None:
            logger.warning(f"[{member.guild.id}] Bot was disconnected from voice channel {before.channel.name}. Cleaning up.")
            await state.cleanup() # Stop loop, clear queue, cleanup state
            if member.guild.id in self.guild_states:
                 del self.guild_states[member.guild.id] # Remove state entry


    # --- Music Commands ---

    @commands.command(name='join', aliases=['connect', 'j'], help="Connects the bot to your current voice channel.")
    async def join_command(self, ctx: commands.Context):
        """Connects the bot to the voice channel the command user is in."""
        if ctx.author.voice is None or ctx.author.voice.channel is None:
            return await ctx.send("You need to be in a voice channel to use this command.")

        channel = ctx.author.voice.channel
        state = self.get_guild_state(ctx.guild.id)

        async with state._lock: # Protect connection state changes
            if state.voice_client and state.voice_client.is_connected():
                if state.voice_client.channel == channel:
                    await ctx.send(f"I'm already connected to {channel.mention}.")
                else:
                    try:
                        await state.voice_client.move_to(channel)
                        await ctx.send(f"Moved to {channel.mention}.")
                        logger.info(f"[{ctx.guild.id}] Moved to voice channel: {channel.name}")
                    except asyncio.TimeoutError:
                        await ctx.send("Timed out trying to move channels.")
                    except Exception as e:
                         await ctx.send(f"Error moving channels: {e}")
                         logger.error(f"[{ctx.guild.id}] Error moving VC: {e}", exc_info=True)
            else:
                try:
                    state.voice_client = await channel.connect()
                    await ctx.send(f"Connected to {channel.mention}.")
                    logger.info(f"[{ctx.guild.id}] Connected to voice channel: {channel.name}")
                    # Automatically start the playback loop after connecting if needed
                    state.start_playback_loop()
                except asyncio.TimeoutError:
                    await ctx.send(f"Timed out connecting to {channel.mention}.")
                except nextcord.errors.ClientException as e:
                     await ctx.send(f"Unable to connect: {e}. Maybe I'm already connected elsewhere?")
                     logger.warning(f"[{ctx.guild.id}] ClientException on connect: {e}")
                except Exception as e:
                    await ctx.send(f"An error occurred connecting: {e}")
                    logger.error(f"[{ctx.guild.id}] Error connecting to VC: {e}", exc_info=True)
                    # Clean up partial state if connection failed badly
                    if ctx.guild.id in self.guild_states:
                        await self.guild_states[ctx.guild.id].cleanup()
                        del self.guild_states[ctx.guild.id]

    @commands.command(name='leave', aliases=['disconnect', 'dc', 'fuckoff'], help="Disconnects the bot from the voice channel.")
    async def leave_command(self, ctx: commands.Context):
        """Disconnects the bot from its current voice channel in the guild."""
        state = self.get_guild_state(ctx.guild.id)

        if not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected to any voice channel.")

        logger.info(f"[{ctx.guild.id}] Leave command initiated by {ctx.author.name}.")
        await state.cleanup() # Handles stopping playback, disconnecting, and cleaning state vars

        if ctx.guild.id in self.guild_states:
            del self.guild_states[ctx.guild.id] # Remove state entry after cleanup

        await ctx.send("Disconnected from the voice channel.")

    @commands.command(name='play', aliases=['p'], help="Plays a song from a URL or search query, or adds it to the queue.")
    async def play_command(self, ctx: commands.Context, *, query: str):
        """Plays audio from a URL (YouTube, SoundCloud, etc.) or searches YouTube."""
        state = self.get_guild_state(ctx.guild.id)

        # 1. Ensure bot is connected (or connect it)
        if not state.voice_client or not state.voice_client.is_connected():
            if ctx.author.voice and ctx.author.voice.channel:
                 logger.info(f"[{ctx.guild.id}] Play requested, connecting to {ctx.author.voice.channel.name} first.")
                 await ctx.invoke(self.join_command) # Attempt to join user's channel
                 # Re-fetch state in case join_command created it
                 state = self.get_guild_state(ctx.guild.id)
                 if not state.voice_client or not state.voice_client.is_connected(): # Check if join succeeded
                      return await ctx.send("Failed to join your voice channel. Cannot play.")
            else:
                return await ctx.send("You need to be in a voice channel, or I need to be connected already.")
        elif ctx.author.voice and ctx.author.voice.channel != state.voice_client.channel:
            # Optional: Prevent playing if user is in a different channel?
             return await ctx.send(f"You must be in the same voice channel ({state.voice_client.channel.mention}) to add songs.")

        # 2. Extract Song Info
        async with ctx.typing():
            song_info = await self._extract_song_info(query)

            if not song_info:
                return await ctx.send("Could not retrieve song information. The URL might be invalid or the service unavailable.")
            if song_info.get('error'):
                 error_type = song_info['error']
                 if error_type == 'unsupported': msg = "Sorry, I don't support that URL or service."
                 elif error_type == 'unavailable': msg = "That video is unavailable."
                 elif error_type == 'download': msg = "There was an error trying to access the song data."
                 else: msg = "An unknown error occurred while fetching the song."
                 return await ctx.send(msg)


            song = Song(
                source_url=song_info['source_url'],
                title=song_info['title'],
                webpage_url=song_info['webpage_url'],
                duration=song_info['duration'],
                requester=ctx.author
            )

        # 3. Add to Queue and Signal Playback Loop
        async with state._lock:
            state.queue.append(song)
            queue_pos = len(state.queue)

            embed = nextcord.Embed(
                title="Added to Queue",
                description=f"[{song.title}]({song.webpage_url})",
                color=nextcord.Color.green()
            )
            embed.add_field(name="Duration", value=song.format_duration(), inline=True)
            embed.add_field(name="Position", value=f"#{queue_pos}", inline=True)
            embed.set_footer(text=f"Requested by {song.requester.display_name}", icon_url=song.requester.display_avatar.url)
            await ctx.send(embed=embed)

            # Ensure the playback loop is running and signal it
            state.start_playback_loop() # Starts if not running
            # No need to explicitly set event here, start_playback_loop handles it or the loop will pick it up
            # state.play_next_song.set() # Avoid setting event directly unless needed for specific cases


    @commands.command(name='skip', aliases=['s'], help="Skips the currently playing song.")
    async def skip_command(self, ctx: commands.Context):
        """Skips the current song and plays the next one in the queue."""
        state = self.get_guild_state(ctx.guild.id)

        if not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected to a voice channel.")
        if not state.voice_client.is_playing() and not state.current_song: # Check if actually playing or about to play
            return await ctx.send("I'm not playing anything right now.")

        logger.info(f"[{ctx.guild.id}] Skip requested by {ctx.author.name}.")
        state.voice_client.stop() # Triggers the 'after' callback, which starts the next song via play_next_song.set()
        await ctx.message.add_reaction('⏭️') # Indicate success

    @commands.command(name='stop', help="Stops playback completely and clears the queue.")
    async def stop_command(self, ctx: commands.Context):
        """Stops the music, clears the queue, but stays connected."""
        state = self.get_guild_state(ctx.guild.id)

        if not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected to a voice channel.")

        logger.info(f"[{ctx.guild.id}] Stop requested by {ctx.author.name}.")
        await state.stop_playback() # Handles stopping player and clearing queue
        await ctx.send("Playback stopped and queue cleared.")
        await ctx.message.add_reaction('⏹️')

    @commands.command(name='pause', help="Pauses the currently playing song.")
    async def pause_command(self, ctx: commands.Context):
        """Pauses the current audio playback."""
        state = self.get_guild_state(ctx.guild.id)

        if not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected.")
        if not state.voice_client.is_playing():
            return await ctx.send("I'm not playing anything.")
        if state.voice_client.is_paused():
            return await ctx.send("Playback is already paused.")

        state.voice_client.pause()
        logger.info(f"[{ctx.guild.id}] Playback paused by {ctx.author.name}.")
        await ctx.message.add_reaction('⏸️')

    @commands.command(name='resume', aliases=['unpause'], help="Resumes a paused song.")
    async def resume_command(self, ctx: commands.Context):
        """Resumes audio playback if paused."""
        state = self.get_guild_state(ctx.guild.id)

        if not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected.")
        # Allow resume even if nothing was actively 'playing' but paused state exists
        if not state.voice_client.is_paused():
            return await ctx.send("Playback is not paused.")

        state.voice_client.resume()
        logger.info(f"[{ctx.guild.id}] Playback resumed by {ctx.author.name}.")
        await ctx.message.add_reaction('▶️')

    @commands.command(name='queue', aliases=['q', 'playlist'], help="Shows the current song queue.")
    async def queue_command(self, ctx: commands.Context):
        """Displays the list of songs waiting to be played."""
        state = self.get_guild_state(ctx.guild.id)

        async with state._lock: # Ensure queue isn't modified while reading
            if not state.current_song and not state.queue:
                return await ctx.send("The queue is empty and nothing is playing.")

            embed = nextcord.Embed(title="Music Queue", color=nextcord.Color.blurple())
            current_display = "Nothing currently playing."
            if state.current_song:
                song = state.current_song
                current_display = f"▶️ **[{song.title}]({song.webpage_url})** `[{song.format_duration()}]` - Req by {song.requester.mention}"
            embed.add_field(name="Now Playing", value=current_display, inline=False)

            if state.queue:
                queue_list = []
                max_display = 10 # Limit display to prevent huge messages
                for i, song in enumerate(list(state.queue)[:max_display]):
                     queue_list.append(f"`{i+1}.` [{song.title}]({song.webpage_url}) `[{song.format_duration()}]` - Req by {song.requester.display_name}")

                if len(state.queue) > max_display:
                    queue_list.append(f"\n...and {len(state.queue) - max_display} more.")

                embed.add_field(name="Up Next", value="\n".join(queue_list) or "No songs in queue.", inline=False)
            else:
                 embed.add_field(name="Up Next", value="No songs in queue.", inline=False)

            embed.set_footer(text=f"Total songs: {len(state.queue) + (1 if state.current_song else 0)}")
            await ctx.send(embed=embed)

    @commands.command(name='volume', aliases=['vol'], help="Changes the player volume (0-100).")
    async def volume_command(self, ctx: commands.Context, *, volume: int):
        """Sets the volume of the music player."""
        state = self.get_guild_state(ctx.guild.id)

        if not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send("I'm not connected to a voice channel.")

        if not 0 <= volume <= 100:
            return await ctx.send("Volume must be between 0 and 100.")

        new_volume = volume / 100.0
        state.volume = new_volume

        # If actively playing, adjust the source volume transformer
        if state.voice_client.source and isinstance(state.voice_client.source, nextcord.PCMVolumeTransformer):
            state.voice_client.source.volume = new_volume
            logger.info(f"[{ctx.guild.id}] Volume adjusted to {volume}% by {ctx.author.name} (active player).")
        else:
             logger.info(f"[{ctx.guild.id}] Volume set to {volume}% by {ctx.author.name} (will apply to next song).")


        await ctx.send(f"Volume set to **{volume}%**.")


    # --- Error Handling for Music Commands ---
    @play_command.error
    @join_command.error
    @leave_command.error
    @skip_command.error
    @stop_command.error
    @pause_command.error
    @resume_command.error
    @queue_command.error
    @volume_command.error
    async def music_command_error(self, ctx: commands.Context, error):
        """Local error handler for music commands."""
        # Check if it's a CheckFailure (like user not in VC) handled by the command itself
        if isinstance(error, commands.CheckFailure):
            # Errors like MissingPermissions, NoPrivateMessage etc. will be caught by the global handler
            # Specific checks within commands usually send their own message.
             logger.debug(f"Music command check failure: {error}")
             # await ctx.send(f"Check failed: {error}") # Optional: generic check fail message
             return # Usually handled in command or global handler needed

        elif isinstance(error, commands.MissingRequiredArgument):
             await ctx.send(f"You forgot an argument: `{error.param.name}`. Check `!help {ctx.command.name}`.")
        elif isinstance(error, commands.BadArgument):
             await ctx.send(f"Invalid argument type provided. Check `!help {ctx.command.name}`.")
        elif isinstance(error, commands.CommandInvokeError):
            # Errors raised within the command's execution
            original = error.original
            logger.error(f"Error invoking command {ctx.command.name}: {original.__class__.__name__}: {original}", exc_info=original)
            # Provide more specific feedback for common voice errors if possible
            if isinstance(original, nextcord.errors.ClientException):
                await ctx.send(f"Voice Error: {original}")
            else:
                await ctx.send("An internal error occurred while processing your request.")
        else:
            # Let the global handler deal with other common errors
            # Log it here for music-specific context if needed
            logger.warning(f"Unhandled error in music command {ctx.command.name}: {error} (Type: {type(error)})")
            # Re-raise it for the global handler if you want it to respond
            # raise error


def setup(bot: commands.Bot):
    """Adds the MusicCog to the bot."""
    # Ensure opus is loaded before adding the cog that uses voice
    if not nextcord.opus.is_loaded():
        try:
            # Try loading opus. You might need to specify the path if it's not found automatically.
            # e.g., nextcord.opus.load_opus('/usr/lib/libopus.so.0')
            nextcord.opus.load_opus()
            logger.info("Opus library loaded successfully.")
        except nextcord.opus.OpusNotLoaded:
            logger.critical("Opus library could not be loaded. Music playback will fail. "
                          "Ensure libopus is installed and potentially specify its path.")
            # Optionally prevent loading the cog if opus fails
            # return
    bot.add_cog(MusicCog(bot))
    logger.info("MusicCog loaded successfully.")