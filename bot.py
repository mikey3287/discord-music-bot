# bot.py
import os
import time
import asyncio
import platform
from collections import deque
from datetime import timedelta

import discord
from discord.ext import commands
from discord import app_commands

import yt_dlp
from dotenv import load_dotenv
import imageio_ffmpeg as ffmpeg
import aiohttp

# ---------- Basic Setup ----------
os.environ["FFMPEG_BINARY"] = ffmpeg.get_ffmpeg_exe()
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# ---------- Opus Load (macOS/Linux helper) ----------
if not discord.opus.is_loaded():
    system = platform.system()
    if system == "Darwin":  # macOS
        possible_paths = [
            "/opt/homebrew/opt/opus/lib/libopus.dylib",  # Apple Silicon / M1/M2
            "/usr/local/opt/opus/lib/libopus.dylib"      # Intel Macs
        ]
        for p in possible_paths:
            if os.path.exists(p):
                discord.opus.load_opus(p)
                break
        else:
            raise OSError("Opus not found. Try: brew install opus")
    elif system == "Linux":
        try:
            discord.opus.load_opus("libopus.so.0")
        except Exception:
            discord.opus.load_opus("libopus.so")
    else:
        raise OSError(f"Unsupported OS: {system}")

# ---------- State ----------
SONG_QUEUES = {}            # {guild_id: deque([(url, title, duration), ...])}
CURRENT_PLAYERS = {}        # {guild_id: PCMVolumeTransformer}
CURRENT_TRACK = {}          # {guild_id: {"url","title","duration","started_at","seek_offset"}}
PENDING_RESTART = {}        # {guild_id: {"elapsed": float}}
BASSBOOST_LEVELS = {}       # {guild_id: 0..5}

NOW_PLAYING_CHANNELS = {}   # {guild_id: channel_id}
NOW_PLAYING_MESSAGES = {}   # {guild_id: message_obj}

def get_queue(guild_id):
    return SONG_QUEUES.setdefault(guild_id, deque())

# ---------- yt-dlp helpers ----------
async def search_youtube(query: str):
    """Returns list of (url, title, duration) from url/playlist/search."""
    if not query.startswith("http"):
        query = f"ytsearch:{query}"

    results = []
    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "noplaylist": False,
        "extract_flat": True,
    }

    def _extract(q):
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(q, download=False)

    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, lambda: _extract(query))
    except Exception as e:
        print(f"yt-dlp error: {e}; retrying once...")
        await asyncio.sleep(1)
        info = await loop.run_in_executor(None, lambda: _extract(query))

    entries = info["entries"] if "entries" in info else [info]
    for entry in entries:
        if entry and entry.get("url") and entry.get("title"):
            results.append((entry["url"], entry["title"], entry.get("duration", 0) or 0))
    return results

def fmt_time(seconds: float) -> str:
    try:
        seconds = int(max(0, round(seconds)))
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
    except Exception:
        return "?:??"

def make_progress_bar(elapsed: float, total: float, width: int = 16) -> str:
    if not total or total <= 0:
        return "─" * width
    ratio = max(0.0, min(1.0, float(elapsed) / float(total)))
    pos = int(ratio * (width - 1))
    bar = []
    for i in range(width):
        bar.append("●" if i == pos else "─")
    return "".join(bar)

def build_now_playing_embed(guild_id: int) -> discord.Embed:
    """Builds the playlist embed with current + next up."""
    q = get_queue(guild_id)
    track = CURRENT_TRACK.get(guild_id)
    bass = BASSBOOST_LEVELS.get(guild_id, 0)
    vol = 100
    if CURRENT_PLAYERS.get(guild_id):
        try:
            vol = int(round(CURRENT_PLAYERS[guild_id].volume * 100))
        except Exception:
            vol = 100

    embed = discord.Embed(
        title="🎶 Now Playing",
        color=discord.Color.blurple()
    )
    # Current
    if track:
        title = track.get("title") or "Unknown title"
        url = track.get("url") or "Unknown URL"
        duration = track.get("duration") or 0
        started_at = track.get("started_at") or time.monotonic()
        seek = float(track.get("seek_offset", 0.0))
        elapsed = (time.monotonic() - started_at) + seek
        elapsed = max(0.0, min(elapsed, max(0.0, duration - 0.5) if duration else elapsed))

        progress = make_progress_bar(elapsed, duration)
        embed.add_field(
            name=title,
            value=f"{url}\n`{fmt_time(elapsed)} {progress} {fmt_time(duration) if duration else '?:??'}`",
            inline=False
        )
    else:
        embed.add_field(name="Nothing playing", value="Use `/play <url or search>`", inline=False)

    # Next up (first 10)
    if q:
        lines = []
        for i, item in enumerate(list(q)[:10], 1):
            u, t, d = item[0], item[1], (item[2] if len(item) > 2 else 0)
            lines.append(f"**{i}.** {t}  ·  `{fmt_time(d) if d else '?:??'}`")
        embed.add_field(name="▶️ Next Up", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="▶️ Next Up", value="_Queue is empty_", inline=False)

    # Small footer/status
    status_bits = [f"Vol: {vol}%", f"Bass: {bass}"]
    embed.set_footer(text=" | ".join(status_bits))
    return embed

async def send_or_edit_now_playing(guild_id: int):
    """Create or update the single playlist embed for this guild."""
    channel_id = NOW_PLAYING_CHANNELS.get(guild_id)
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return

    embed = build_now_playing_embed(guild_id)

    # Try to edit existing, else send a new one
    msg = NOW_PLAYING_MESSAGES.get(guild_id)
    if msg:
        try:
            await msg.edit(embed=embed, content=None)
            return
        except (discord.NotFound, discord.HTTPException):
            NOW_PLAYING_MESSAGES.pop(guild_id, None)

    try:
        new_msg = await channel.send(embed=embed)
        NOW_PLAYING_MESSAGES[guild_id] = new_msg
    except discord.HTTPException:
        pass

# ---------- Playback Core ----------
async def _get_audio_url(original_url: str) -> str:
    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "quiet": True,
        "noplaylist": True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(original_url, download=False)
        return info["url"]

async def play_next(voice_client: discord.VoiceClient, guild_id: int, interaction: discord.Interaction | None):
    q = get_queue(guild_id)
    if not q:
        # queue finished
        CURRENT_TRACK.pop(guild_id, None)
        CURRENT_PLAYERS.pop(guild_id, None)
        await send_or_edit_now_playing(guild_id)
        await asyncio.sleep(1)
        try:
            await voice_client.disconnect()
        except Exception:
            pass
        return

    url, title, duration = q.popleft()
    audio_url = await asyncio.get_running_loop().run_in_executor(None, lambda: yt_dlp.YoutubeDL({"quiet": True})._op_download)  # dummy to warm thread pool (optional)
    audio_url = await _get_audio_url(url)

    bassboost_level = BASSBOOST_LEVELS.get(guild_id, 0)

    # Build ffmpeg opts for fresh start
    options = "-vn"
    if bassboost_level > 0:
        gain = min(bassboost_level, 5)
        options += f' -af "bass=g={gain},volume=1"'

    ffmpeg_opts = {
        "before_options": '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -protocol_whitelist "file,http,https,tcp,tls,crypto"',
        "options": options
    }

    source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_opts)
    player = discord.PCMVolumeTransformer(source, volume=CURRENT_PLAYERS.get(guild_id, type("x", (), {"volume":1})).volume if CURRENT_PLAYERS.get(guild_id) else 1)
    CURRENT_PLAYERS[guild_id] = player

    # Record track start
    CURRENT_TRACK[guild_id] = {
        "url": url,
        "title": title,
        "duration": duration or 0,
        "started_at": time.monotonic(),
        "seek_offset": 0.0
    }

    # Update the playlist embed right away
    await send_or_edit_now_playing(guild_id)

    def after_play(err):
        if err:
            print(f"Error in playback: {err}")

        # If a filter-change restart is pending, handle that instead of advancing
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

        fut = asyncio.run_coroutine_threadsafe(
            play_next(voice_client, guild_id, interaction),
            bot.loop
        )
        try:
            fut.result()
        except Exception as e:
            print(f"Next song error: {e}")

    voice_client.play(player, after=after_play)

async def restart_same_track(voice_client: discord.VoiceClient, guild_id: int, interaction: discord.Interaction | None):
    """Rebuild ffmpeg with seek + new filters for current song."""
    try:
        pending = PENDING_RESTART.pop(guild_id, None)
        if not pending:
            return
        track = CURRENT_TRACK.get(guild_id)
        if not track:
            return

        # fresh audio_url (YouTube cdn urls can be short-lived)
        audio_url = await _get_audio_url(track["url"])
        bassboost_level = BASSBOOST_LEVELS.get(guild_id, 0)

        elapsed = max(0.0, float(pending["elapsed"]))
        before_opts = f'-ss {elapsed} -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -protocol_whitelist "file,http,https,tcp,tls,crypto"'

        options = "-vn"
        if bassboost_level > 0:
            gain = min(bassboost_level, 5)
            options += f' -af "bass=g={gain},volume=1"'

        ffmpeg_opts = {
            "before_options": before_opts,
            "options": options
        }

        source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_opts)
        # keep the same volume if available
        vol = CURRENT_PLAYERS.get(guild_id).volume if CURRENT_PLAYERS.get(guild_id) else 1
        player = discord.PCMVolumeTransformer(source, volume=vol)
        CURRENT_PLAYERS[guild_id] = player

        # Update timing (we jumped forward)
        track["seek_offset"] = elapsed
        track["started_at"] = time.monotonic()

        # Update embed to reflect the new filter/position
        await send_or_edit_now_playing(guild_id)

        def after_play(err):
            if err:
                print(f"Error in playback (restart): {err}")
            fut = asyncio.run_coroutine_threadsafe(
                play_next(voice_client, guild_id, interaction),
                bot.loop
            )
            try:
                fut.result()
            except Exception as e:
                print(f"Next song error: {e}")

        voice_client.play(player, after=after_play)

    except Exception as e:
        print(f"restart_same_track exception: {e}")
        fut = asyncio.run_coroutine_threadsafe(play_next(voice_client, guild_id, interaction), bot.loop)
        fut.result()

# ---------- Commands ----------
@tree.command(name="play", description="Play a song or playlist from YouTube (URL or search).")
@app_commands.describe(query="YouTube URL/playlist or search terms")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    voice = interaction.user.voice
    if not voice or not voice.channel:
        await interaction.followup.send("❌ You must be in a voice channel.")
        return

    try:
        results = await search_youtube(query)
        if not results:
            await interaction.followup.send("❌ No results found.")
            return

        # Remember where to keep the playlist embed updated
        NOW_PLAYING_CHANNELS[interaction.guild_id] = interaction.channel.id

        q = get_queue(interaction.guild_id)
        q.extend(results)

        if not interaction.guild.voice_client:
            vc = await voice.channel.connect()
            await play_next(vc, interaction.guild_id, interaction)
        else:
            # refresh the embed since queue changed
            await send_or_edit_now_playing(interaction.guild_id)

        if len(results) > 1:
            await interaction.followup.send(f"✅ Added **{len(results)}** tracks to the queue.")
        else:
            await interaction.followup.send(f"✅ Added to queue: **{results[0][1]}**")

    except Exception as e:
        await interaction.followup.send(f"❌ Error: `{e}`")

@tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.followup.send("⏭️ Skipping…")
    else:
        await interaction.followup.send("❌ Nothing is playing.")
    await send_or_edit_now_playing(interaction.guild_id)

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

@tree.command(name="stop", description="Stop and clear the queue")
async def stop(interaction: discord.Interaction):
    await interaction.response.defer()
    guild_id = interaction.guild_id
    vc = interaction.guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect()

    get_queue(guild_id).clear()
    CURRENT_TRACK.pop(guild_id, None)
    CURRENT_PLAYERS.pop(guild_id, None)
    await interaction.followup.send("🛑 Stopped and cleared the queue.")
    await send_or_edit_now_playing(guild_id)

@tree.command(name="volume", description="Set playback volume (0-100%)")
@app_commands.describe(level="0 to 100")
async def volume(interaction: discord.Interaction, level: int):
    if not 0 <= level <= 100:
        await interaction.response.send_message("❌ Enter a volume between 0 and 100.", ephemeral=True)
        return
    player = CURRENT_PLAYERS.get(interaction.guild_id)
    if not player:
        await interaction.response.send_message("❌ No music is playing.", ephemeral=True)
        return
    player.volume = level / 100.0
    await interaction.response.send_message(f"🔊 Volume set to **{level}%**.")
    await send_or_edit_now_playing(interaction.guild_id)

@tree.command(name="bassboost", description="Set bass boost (0-5) — reapplies to the current song without skipping")
@app_commands.describe(level="0 to 5")
async def bassboost(interaction: discord.Interaction, level: int):
    await interaction.response.defer()
    if not 0 <= level <= 5:
        await interaction.followup.send("❌ Enter a level between 0 and 5.")
        return

    guild_id = interaction.guild_id
    BASSBOOST_LEVELS[guild_id] = level

    vc = interaction.guild.voice_client
    track = CURRENT_TRACK.get(guild_id)

    if vc and (vc.is_playing() or vc.is_paused()) and track:
        started_at = track.get("started_at") or time.monotonic()
        seek_offset = float(track.get("seek_offset", 0.0))
        elapsed = (time.monotonic() - started_at) + seek_offset
        # Clamp to duration - 1s when known
        duration = track.get("duration") or 0
        if duration:
            elapsed = min(max(0.0, elapsed), max(0.0, duration - 1))
        PENDING_RESTART[guild_id] = {"elapsed": elapsed}
        vc.stop()  # triggers restart_same_track via after callback
        msg = f"🎛️ Bass boost {'disabled' if level == 0 else f'set to **{level}**'} — reapplied to the current song."
    else:
        msg = f"🎛️ Bass boost {'disabled' if level == 0 else f'set to **{level}**'}. (No track playing.)"

    await interaction.followup.send(msg)
    await send_or_edit_now_playing(guild_id)

@tree.command(name="queue", description="Show the queue in an embed (same as Now Playing)")
async def queue_cmd(interaction: discord.Interaction):
    # Remember this channel for future auto-updates, then render
    NOW_PLAYING_CHANNELS[interaction.guild_id] = interaction.channel.id
    await send_or_edit_now_playing(interaction.guild_id)
    await interaction.response.send_message("📋 Queue/Now Playing updated above.", ephemeral=True)

@tree.command(name="debug", description="Check bot status and connection")
async def debug(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    vc = interaction.guild.voice_client
    guild_id = interaction.guild_id
    q = get_queue(guild_id)

    def fmt_time(seconds: float) -> str:
        try:
            seconds = int(max(0, round(seconds)))
            m, s = divmod(seconds, 60)
            h, m = divmod(m, 60)
            return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
        except Exception:
            return "?:??"

    embed = discord.Embed(title="Bot Debug Info", color=discord.Color.dark_red())

    # Voice info
    if vc and vc.is_connected():
        embed.add_field(name="Voice Status", value=f"Connected to: {vc.channel.name}", inline=False)
        embed.add_field(name="Is Playing?", value=str(vc.is_playing()), inline=True)
        embed.add_field(name="Is Paused?", value=str(vc.is_paused()), inline=True)
    else:
        embed.add_field(name="Voice Status", value="Not connected", inline=False)

    # Current song from CURRENT_TRACK (preferred)
    track = CURRENT_TRACK.get(guild_id)
    if track:
        elapsed = (time.monotonic() - (track.get("started_at") or time.monotonic())) + float(track.get("seek_offset", 0.0))
        details = f"{track.get('title')}\n{track.get('url')}\n⏱ {fmt_time(elapsed)} / {fmt_time(track.get('duration') or 0)}"
        embed.add_field(name="Current Song", value=details, inline=False)
    else:
        embed.add_field(name="Current Song", value="No song playing", inline=False)

    # Network check on current or next URL
    check_url = None
    if track and isinstance(track.get("url"), str) and track["url"].startswith(("http://", "https://")):
        check_url = track["url"]
    elif q and isinstance(q[0][0], str) and q[0][0].startswith(("http://", "https://")):
        check_url = q[0][0]

    try:
        if check_url:
            async with aiohttp.ClientSession() as session:
                async with session.head(check_url, timeout=5) as resp:
                    embed.add_field(name="Network Status", value=f"URL reachable: {resp.status}", inline=False)
        else:
            embed.add_field(name="Network Status", value="No HTTP URL to check", inline=False)
    except Exception as e:
        embed.add_field(name="Network Status", value=f"Error: {e}", inline=False)

    embed.add_field(name="Queue Length", value=str(len(q)), inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)

# ---------- Lifecycle ----------
@bot.event
async def on_ready():
    activity = discord.Activity(type=discord.ActivityType.listening, name="/play")
    await bot.change_presence(status=discord.Status.online, activity=activity)
    await tree.sync()
    print(f"✅ Logged in as {bot.user}!")
    print("successfully finished startup")

bot.run(TOKEN)