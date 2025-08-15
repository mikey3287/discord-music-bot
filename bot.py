import os
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp
import asyncio
from collections import deque
import discord.opus
from datetime import timedelta
from config import ALLOWED_USERS
import discord
import platform
import imageio_ffmpeg as ffmpeg
import aiohttp  # ⬅ add this at the top of the file
import time

CURRENT_TRACK = {}     # {guild_id: {"url": str, "title": str, "duration": int|None, "started_at": float, "seek_offset": float}}
PENDING_RESTART = {}   # {guild_id: {"elapsed": float}}


os.environ["FFMPEG_BINARY"] = ffmpeg.get_ffmpeg_exe()



# Replace this path if different on your Mac
if not discord.opus.is_loaded():
    system = platform.system()
    if system == "Darwin":  # macOS
        possible_paths = [
            "/opt/homebrew/opt/opus/lib/libopus.dylib",  # Apple Silicon / M1/M2
            "/usr/local/opt/opus/lib/libopus.dylib"      # Intel Macs
        ]
        for path in possible_paths:
            if os.path.exists(path):
                discord.opus.load_opus(path)
                break
        else:
            raise OSError("Opus library not found on macOS. Try running: brew install opus")
    elif system == "Linux":  # Render or other Linux
        discord.opus.load_opus('libopus.so.0')
    else:
        raise OSError(f"Unsupported OS: {system}")



load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree
bot.now_playing_messages = {}
bot.now_playing_channels = {}
BASSBOOST_ENABLED = {}  # {guild_id: True/False}
BASSBOOST_LEVELS = {}  # {guild_id: int level (0–100)}


YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': False,
    'quiet': True,
    'extract_flat': 'in_playlist'
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

SONG_QUEUES = {}
CURRENT_PLAYERS = {}

def get_queue(guild_id):
    return SONG_QUEUES.setdefault(guild_id, deque())

import asyncio

async def search_youtube(query):
    if not query.startswith("http"):
        query = f"ytsearch:{query}"

    results = []

    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'noplaylist': False,
        'extract_flat': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(query, download=False)
        except Exception as e:
            print(f"yt-dlp extraction error: {e}, retrying...")
            await asyncio.sleep(1)
            info = ydl.extract_info(query, download=False)

        if 'entries' in info:
            entries = info['entries']
        else:
            entries = [info]

        for entry in entries:
            if entry and entry.get('url') and entry.get('title'):
                results.append((entry['url'], entry['title'], entry.get('duration', 0)))

    return results


async def play_next(voice_client, guild_id, interaction):
    queue = get_queue(guild_id)
    if not queue:
        await voice_client.disconnect()
        return

    url, title, duration = queue.popleft()

    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'quiet': True,
        'noplaylist': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        audio_url = info['url']

    bassboost_level = BASSBOOST_LEVELS.get(guild_id, 0)

    # Build ffmpeg opts (no seek here; this is the start of the track)
    options = '-vn'
    if bassboost_level > 0:
        gain = min(bassboost_level, 5)
        options += f' -af "bass=g={gain},volume=1"'

    ffmpeg_opts = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -protocol_whitelist "file,http,https,tcp,tls,crypto"',
        'options': options
    }

    source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_opts)
    player = discord.PCMVolumeTransformer(source, volume=1)
    CURRENT_PLAYERS[guild_id] = player

    # Record track start time so we can seek later
    CURRENT_TRACK[guild_id] = {
        "url": url,
        "title": title,
        "duration": duration or 0,
        "started_at": time.monotonic(),
        "seek_offset": 0.0
    }

    def after_play(err):
        if err:
            print(f"Error in playback: {err}")

        # If a filter-change restart is pending, do that instead of advancing the queue
        if PENDING_RESTART.get(guild_id):
            fut = asyncio.run_coroutine_threadsafe(
                restart_same_track(voice_client, guild_id, interaction),
                bot.loop
            )
            try:
                fut.result()
            except Exception as e:
                print(f"Restart same track error: {e}")
            return

        # Normal advance to next song
        fut = asyncio.run_coroutine_threadsafe(play_next(voice_client, guild_id, interaction), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print(f"Next song error: {e}")

    voice_client.play(player, after=after_play)

    # --- update "Now Playing" message (unchanged) ---
    channel_id = bot.now_playing_channels.get(guild_id)
    channel = bot.get_channel(channel_id) if channel_id else None
    if channel:
        if guild_id in bot.now_playing_messages:
            try:
                await bot.now_playing_messages[guild_id].edit(content=f"🎶 Now playing: **{title}**")
            except discord.NotFound:
                message = await channel.send(f"🎶 Now playing: **{title}**")
                bot.now_playing_messages[guild_id] = message
        else:
            message = await channel.send(f"🎶 Now playing: **{title}**")
            bot.now_playing_messages[guild_id] = message


async def restart_same_track(voice_client, guild_id, interaction):
    """Called from after_play when a bassboost change requested a restart."""
    try:
        pending = PENDING_RESTART.pop(guild_id, None)
        if not pending:
            return

        track = CURRENT_TRACK.get(guild_id)
        if not track:
            return

        # Re-resolve a fresh audio_url because YouTube CDNs hand out short-lived URLs
        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'quiet': True,
            'noplaylist': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(track["url"], download=False)
            audio_url = info['url']

        bassboost_level = BASSBOOST_LEVELS.get(guild_id, 0)

        # Seek to elapsed time BEFORE input (faster + accurate for HTTP)
        elapsed = max(0.0, float(pending["elapsed"]))
        before_opts = f'-ss {elapsed} -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -protocol_whitelist "file,http,https,tcp,tls,crypto"'

        options = '-vn'
        if bassboost_level > 0:
            gain = min(bassboost_level, 5)
            options += f' -af "bass=g={gain},volume=1"'

        ffmpeg_opts = {
            'before_options': before_opts,
            'options': options
        }

        source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_opts)
        player = discord.PCMVolumeTransformer(source, volume=CURRENT_PLAYERS.get(guild_id, type("x", (), {"volume":1})) .volume if CURRENT_PLAYERS.get(guild_id) else 1)
        CURRENT_PLAYERS[guild_id] = player

        # Update track timing (we jumped forward)
        track["seek_offset"] = elapsed
        track["started_at"] = time.monotonic()

        def after_play(err):
            if err:
                print(f"Error in playback (restart): {err}")
            fut = asyncio.run_coroutine_threadsafe(play_next(voice_client, guild_id, interaction), bot.loop)
            try:
                fut.result()
            except Exception as e:
                print(f"Next song error: {e}")

        voice_client.play(player, after=after_play)

        # Keep the same "Now Playing" message; title unchanged

    except Exception as e:
        print(f"restart_same_track exception: {e}")
        # If restart fails, just go to next track to avoid deadlock
        fut = asyncio.run_coroutine_threadsafe(play_next(voice_client, guild_id, interaction), bot.loop)
        fut.result()



@tree.command(name="play", description="Play a song from YouTube")
@app_commands.describe(query="YouTube URL or search term")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    try:
        voice = interaction.user.voice
        if not voice or not voice.channel:
            await interaction.followup.send("❌ You must be in a voice channel.")
            return

        results = await search_youtube(query)
        if not results:
            await interaction.followup.send("❌ No results found.")
            return

        queue = get_queue(interaction.guild_id)
        queue.extend(results)

        # ✅ Set channel for updates
        bot.now_playing_channels[interaction.guild_id] = interaction.channel.id

        if not interaction.guild.voice_client:
            vc = await voice.channel.connect()
            await play_next(vc, interaction.guild_id, interaction)

        if len(results) > 1:
            await interaction.followup.send(f"✅ Added **{len(results)} songs** from playlist to the queue.")
        else:
            await interaction.followup.send(f"✅ Added to queue: {results[0][1]}")

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")


@tree.command(name="pause", description="Pause the music")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Paused.")
    else:
        await interaction.response.send_message("❌ Nothing is playing.")

@tree.command(name="resume", description="Resume the music")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Resumed.")
    else:
        await interaction.response.send_message("❌ Nothing is paused.")

@tree.command(name="stop", description="Stop the music and leave voice")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        get_queue(interaction.guild_id).clear()
        await interaction.response.send_message("🛑 Stopped and left channel.")
    else:
        await interaction.response.send_message("❌ Bot not in a voice channel.")

from datetime import timedelta

@tree.command(name="reset", description="Reset the bot: stop music, clear queue, leave voice, and clear status")
async def reset(interaction: discord.Interaction):
    from config import ALLOWED_USERS
    if interaction.user.id not in ALLOWED_USERS:
        await interaction.response.send_message("❌ You are not allowed to reset the bot.")
        return
    # ... rest of your reset logic ...

    await interaction.response.defer(thinking=False)  # Prevent timeout

    guild_id = interaction.guild_id
    vc = interaction.guild.voice_client

    if vc:
        vc.stop()
        await vc.disconnect()

    # Clear song queue
    queue = get_queue(guild_id)
    queue.clear()

    # Delete "Now Playing" message
    if guild_id in bot.now_playing_messages:
        try:
            await bot.now_playing_messages[guild_id].delete()
        except discord.HTTPException:
            pass
        del bot.now_playing_messages[guild_id]

    await interaction.followup.send("🔄 Bot has been reset: stopped music, cleared queue, and left voice.")

@bot.tree.command(name="shutdown", description="Shut down the bot (admin only)")
async def shutdown(interaction: discord.Interaction):
    from config import ALLOWED_USERS
    if interaction.user.id not in ALLOWED_USERS:
        await interaction.response.send_message("❌ You are not allowed to shut me down.")
        return

    await interaction.response.send_message("🛑 Bot is shutting down...")
    await bot.close()


@tree.command(name="bassboost", description="Set bass boost level (0 to 5)")
@app_commands.describe(level="Bass boost level: 0 to 5")
async def bassboost(interaction: discord.Interaction, level: int):
    await interaction.response.defer(ephemeral=False, thinking=False)  # avoids 10062 errors

    if not 0 <= level <= 5:
        await interaction.followup.send("❌ Please enter a level between 0 and 5.")
        return

    guild_id = interaction.guild_id
    BASSBOOST_LEVELS[guild_id] = level

    vc = interaction.guild.voice_client
    track = CURRENT_TRACK.get(guild_id)

    if vc and (vc.is_playing() or vc.is_paused()) and track:
        # Compute elapsed time in current track
        # elapsed = time since started + any previous seek_offset
        started_at = track.get("started_at") or time.monotonic()
        seek_offset = float(track.get("seek_offset", 0.0))
        elapsed = (time.monotonic() - started_at) + seek_offset
        # If we know duration, clamp
        duration = track.get("duration") or 0
        if duration:
            elapsed = min(max(0.0, elapsed), max(0.0, duration - 1))

        # Flag a same-track restart, then stop current decoder.
        PENDING_RESTART[guild_id] = {"elapsed": elapsed}
        vc.stop()  # Triggers after; after will see PENDING_RESTART and restart the SAME track
    # else: nothing playing; next song (when started) will use new bass

    if level == 0:
        await interaction.followup.send("🎛️ Bass boost has been **disabled** (no skip).")
    else:
        await interaction.followup.send(f"🎛️ Bass boost set to **{level}** and reapplied to the **current song**.")




@bot.tree.command(name="queue", description="Show the current music queue")
async def queue_cmd(interaction: discord.Interaction):
    q = get_queue(interaction.guild_id)
    if not q:
        await interaction.response.send_message("The queue is currently empty.")
        return

    # Works if queue items are dicts; your code stores tuples, so use tuple path
    try:
        message = "\n".join(f"{idx+1}. {item['title']}" for idx, item in enumerate(q))
    except (TypeError, KeyError):
        message = "\n".join(f"{idx+1}. {title}" for idx, (_, title, *_) in enumerate(q))

    await interaction.response.send_message(f"**Current Queue:**\n{message}")


@tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("⏭️ Skipping song...")
    else:
        await interaction.response.send_message("❌ Nothing is playing.")    


@tree.command(name="volume", description="Set the playback volume (0-100%)")
@app_commands.describe(level="Volume level from 0 to 100")
async def volume(interaction: discord.Interaction, level: int):
    if not 0 <= level <= 100:
        await interaction.response.send_message("❌ Please enter a volume between 0 and 100.", ephemeral=True)
        return

    player = CURRENT_PLAYERS.get(interaction.guild_id)
    if not player:
        await interaction.response.send_message("❌ No music is currently playing.", ephemeral=True)
        return

    player.volume = level / 100  # convert percent to 0.0–1.0
    await interaction.response.send_message(f"🔊 Volume set to **{level}%**.")
       

@tree.command(name="debug", description="Check bot status and connection")
async def debug(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    q = get_queue(interaction.guild_id)

    embed = discord.Embed(title="Bot Debug Info", color=discord.Color.blurple())

    # Voice connection
    if vc and vc.is_connected():
        embed.add_field(name="Voice Status", value=f"Connected to: {vc.channel.name}", inline=False)
        embed.add_field(name="Is Playing?", value=str(vc.is_playing()), inline=True)
        embed.add_field(name="Is Paused?", value=str(vc.is_paused()), inline=True)
    else:
        embed.add_field(name="Voice Status", value="Not connected", inline=False)

    # Current song
    if vc and vc.is_playing() and q:
        embed.add_field(
            name="Current Song",
            value=f"{q[0]['title']}\n{q[0]['url']}",
            inline=False
        )
    elif vc and vc.is_playing():
        embed.add_field(name="Current Song", value="Unknown (playing directly)", inline=False)
    else:
        embed.add_field(name="Current Song", value="No song playing", inline=False)

    # Network check
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            if q:
                url = q[0]['url']
                async with session.head(url, timeout=5) as resp:
                    embed.add_field(name="Network Status", value=f"URL reachable: {resp.status}", inline=False)
            else:
                embed.add_field(name="Network Status", value="No URL to check", inline=False)
    except Exception as e:
        embed.add_field(name="Network Status", value=f"Error: {e}", inline=False)

    await interaction.response.send_message(embed=embed)
     

@bot.event
async def on_ready():
    activity = discord.Activity(type=discord.ActivityType.streaming, name="/playing ....")
    await bot.change_presence(status=discord.Status.online, activity=activity)
    await tree.sync()
    print(f"✅ Logged in as {bot.user}!")
    print("successfully finished startup")

bot.run(TOKEN)