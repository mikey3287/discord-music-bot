# bot.py
import os
import time
import asyncio
import platform
from collections import deque

import discord
from discord.ext import commands, tasks
from discord import app_commands

import yt_dlp
from dotenv import load_dotenv
import imageio_ffmpeg as ffmpeg
import aiohttp

from helper import reply_safe

from helper import (
    fmt_time,
    search_youtube,
    safe_defer,
    build_filter_chain,
    send_or_edit_now_playing,
    delete_now_playing_message,
)

# ---------- Basic Setup ----------
os.environ["FFMPEG_BINARY"] = ffmpeg.get_ffmpeg_exe()
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# Never ping anyone in bot replies
ALLOWED_NONE = discord.AllowedMentions.none()

# ---------- Opus Load (macOS/Linux helper) ----------
if not discord.opus.is_loaded():
    system = platform.system()
    if system == "Darwin":  # macOS
        possible_paths = [
            "/opt/homebrew/opt/opus/lib/libopus.dylib",  # Apple Silicon
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
TREBLEBOOST_LEVELS = {}     # {guild_id: 0..5}
VOCALBOOST_LEVELS = {}      # {guild_id: 0..5}

NOW_PLAYING_CHANNELS = {}   # {guild_id: channel_id}
NOW_PLAYING_MESSAGES = {}   # {guild_id: message_obj}

def get_queue(guild_id: int) -> deque:
    return SONG_QUEUES.setdefault(guild_id, deque())

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
        await send_or_edit_now_playing(
            guild_id,
            bot=bot,
            now_playing_channels=NOW_PLAYING_CHANNELS,
            now_playing_messages=NOW_PLAYING_MESSAGES,
            current_players=CURRENT_PLAYERS,
            current_track=CURRENT_TRACK,
            queue_getter=get_queue,
            bass_levels=BASSBOOST_LEVELS,
            treble_levels=TREBLEBOOST_LEVELS,
            vocal_levels=VOCALBOOST_LEVELS,
            allowed_mentions=ALLOWED_NONE,
        )
        await asyncio.sleep(1)
        try:
            await voice_client.disconnect()
        except Exception:
            pass
        return

    url, title, duration = q.popleft()
    audio_url = await _get_audio_url(url)

    options = build_filter_chain(
        guild_id,
        BASSBOOST_LEVELS,
        TREBLEBOOST_LEVELS,
        VOCALBOOST_LEVELS,
    )
    ffmpeg_opts = {
        "before_options": '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -protocol_whitelist "file,http,https,tcp,tls,crypto"',
        "options": options
    }

    source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_opts)
    prev = CURRENT_PLAYERS.get(guild_id)
    volume = prev.volume if prev else 1.0
    player = discord.PCMVolumeTransformer(source, volume=volume)
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
    await send_or_edit_now_playing(
        guild_id,
        bot=bot,
        now_playing_channels=NOW_PLAYING_CHANNELS,
        now_playing_messages=NOW_PLAYING_MESSAGES,
        current_players=CURRENT_PLAYERS,
        current_track=CURRENT_TRACK,
        queue_getter=get_queue,
        bass_levels=BASSBOOST_LEVELS,
        treble_levels=TREBLEBOOST_LEVELS,
        vocal_levels=VOCALBOOST_LEVELS,
        allowed_mentions=ALLOWED_NONE,
    )

    def after_play(err):
        if err:
            print(f"Error in playback: {err}")

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

        audio_url = await _get_audio_url(track["url"])

        elapsed = max(0.0, float(pending["elapsed"]))
        before_opts = f'-ss {elapsed} -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -protocol_whitelist "file,http,https,tcp,tls,crypto"'

        options = build_filter_chain(
            guild_id,
            BASSBOOST_LEVELS,
            TREBLEBOOST_LEVELS,
            VOCALBOOST_LEVELS,
        )
        ffmpeg_opts = {"before_options": before_opts, "options": options}

        source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_opts)
        vol = CURRENT_PLAYERS.get(guild_id).volume if CURRENT_PLAYERS.get(guild_id) else 1
        player = discord.PCMVolumeTransformer(source, volume=vol)
        CURRENT_PLAYERS[guild_id] = player

        # Update timing (we jumped forward)
        track["seek_offset"] = elapsed
        track["started_at"] = time.monotonic()

        await send_or_edit_now_playing(
            guild_id,
            bot=bot,
            now_playing_channels=NOW_PLAYING_CHANNELS,
            now_playing_messages=NOW_PLAYING_MESSAGES,
            current_players=CURRENT_PLAYERS,
            current_track=CURRENT_TRACK,
            queue_getter=get_queue,
            bass_levels=BASSBOOST_LEVELS,
            treble_levels=TREBLEBOOST_LEVELS,
            vocal_levels=VOCALBOOST_LEVELS,
            allowed_mentions=ALLOWED_NONE,
        )

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
async def play_cmd(interaction: discord.Interaction, query: str):
    deferred = await safe_defer(interaction)
    voice = interaction.user.voice
    if not voice or not voice.channel:
        msg = "❌ You must be in a voice channel."
        if deferred:
            await interaction.followup.send(msg, allowed_mentions=ALLOWED_NONE)
        else:
            await interaction.channel.send(msg, allowed_mentions=ALLOWED_NONE)
        return

    try:
        results = await search_youtube(query)
        if not results:
            if deferred:
                await interaction.followup.send("❌ No results found.", allowed_mentions=ALLOWED_NONE)
            else:
                await interaction.channel.send("❌ No results found.", allowed_mentions=ALLOWED_NONE)
            return

        NOW_PLAYING_CHANNELS[interaction.guild_id] = interaction.channel.id

        q = get_queue(interaction.guild_id)
        q.extend(results)

        if not interaction.guild.voice_client:
            vc = await voice.channel.connect()
            await play_next(vc, interaction.guild_id, interaction)
        else:
            await send_or_edit_now_playing(
                interaction.guild_id,
                bot=bot,
                now_playing_channels=NOW_PLAYING_CHANNELS,
                now_playing_messages=NOW_PLAYING_MESSAGES,
                current_players=CURRENT_PLAYERS,
                current_track=CURRENT_TRACK,
                queue_getter=get_queue,
                bass_levels=BASSBOOST_LEVELS,
                treble_levels=TREBLEBOOST_LEVELS,
                vocal_levels=VOCALBOOST_LEVELS,
                allowed_mentions=ALLOWED_NONE,
            )

        msg = (
            f"✅ Added **{len(results)}** tracks to the queue."
            if len(results) > 1
            else f"✅ Added to queue: **{results[0][1]}**"
        )
        if deferred:
            await interaction.followup.send(msg, allowed_mentions=ALLOWED_NONE)
        else:
            await interaction.channel.send(msg, allowed_mentions=ALLOWED_NONE)

    except Exception as e:
        err = f"❌ Error: `{e}`"
        if deferred:
            await interaction.followup.send(err, allowed_mentions=ALLOWED_NONE)
        else:
            await interaction.channel.send(err, allowed_mentions=ALLOWED_NONE)

@tree.command(name="skip", description="Skip the current song")
async def skip_cmd(interaction: discord.Interaction):
    deferred = await safe_defer(interaction)
    vc = interaction.guild.voice_client
    msg = "⏭️ Skipping…" if (vc and vc.is_playing()) else "❌ Nothing is playing."
    if vc and vc.is_playing():
        vc.stop()
    if deferred:
        await interaction.followup.send(msg, allowed_mentions=ALLOWED_NONE)
    else:
        await interaction.channel.send(msg, allowed_mentions=ALLOWED_NONE)
    await send_or_edit_now_playing(
        interaction.guild_id,
        bot=bot,
        now_playing_channels=NOW_PLAYING_CHANNELS,
        now_playing_messages=NOW_PLAYING_MESSAGES,
        current_players=CURRENT_PLAYERS,
        current_track=CURRENT_TRACK,
        queue_getter=get_queue,
        bass_levels=BASSBOOST_LEVELS,
        treble_levels=TREBLEBOOST_LEVELS,
        vocal_levels=VOCALBOOST_LEVELS,
        allowed_mentions=ALLOWED_NONE,
    )

@tree.command(name="pause", description="Pause the music")
async def pause_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Paused.", allowed_mentions=ALLOWED_NONE)
    else:
        await interaction.response.send_message("❌ Nothing is playing.", allowed_mentions=ALLOWED_NONE)

@tree.command(name="resume", description="Resume the music")
async def resume_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ Resumed.", allowed_mentions=ALLOWED_NONE)
    else:
        await interaction.response.send_message("❌ Nothing is paused.", allowed_mentions=ALLOWED_NONE)

@tree.command(name="stop", description="Stop and clear the queue")
async def stop_cmd(interaction: discord.Interaction):
    deferred = await safe_defer(interaction)
    guild_id = interaction.guild_id
    vc = interaction.guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect()

    get_queue(guild_id).clear()
    CURRENT_TRACK.pop(guild_id, None)
    CURRENT_PLAYERS.pop(guild_id, None)

    if deferred:
        await interaction.followup.send("🛑 Stopped and cleared the queue.", allowed_mentions=ALLOWED_NONE)
    else:
        await interaction.channel.send("🛑 Stopped and cleared the queue.", allowed_mentions=ALLOWED_NONE)

    await send_or_edit_now_playing(
        guild_id,
        bot=bot,
        now_playing_channels=NOW_PLAYING_CHANNELS,
        now_playing_messages=NOW_PLAYING_MESSAGES,
        current_players=CURRENT_PLAYERS,
        current_track=CURRENT_TRACK,
        queue_getter=get_queue,
        bass_levels=BASSBOOST_LEVELS,
        treble_levels=TREBLEBOOST_LEVELS,
        vocal_levels=VOCALBOOST_LEVELS,
        allowed_mentions=ALLOWED_NONE,
    )

@tree.command(name="volume", description="Set playback volume (0-100%)")
@app_commands.describe(level="0 to 100")
async def volume_cmd(interaction: discord.Interaction, level: int):
    if not 0 <= level <= 100:
        await interaction.response.send_message("❌ Enter a volume between 0 and 100.", ephemeral=True, allowed_mentions=ALLOWED_NONE)
        return
    player = CURRENT_PLAYERS.get(interaction.guild_id)
    if not player:
        await interaction.response.send_message("❌ No music is playing.", ephemeral=True, allowed_mentions=ALLOWED_NONE)
        return
    player.volume = level / 100.0
    await interaction.response.send_message(f"🔊 Volume set to **{level}%**.", allowed_mentions=ALLOWED_NONE)
    await send_or_edit_now_playing(
        interaction.guild_id,
        bot=bot,
        now_playing_channels=NOW_PLAYING_CHANNELS,
        now_playing_messages=NOW_PLAYING_MESSAGES,
        current_players=CURRENT_PLAYERS,
        current_track=CURRENT_TRACK,
        queue_getter=get_queue,
        bass_levels=BASSBOOST_LEVELS,
        treble_levels=TREBLEBOOST_LEVELS,
        vocal_levels=VOCALBOOST_LEVELS,
        allowed_mentions=ALLOWED_NONE,
    )

@tree.command(name="bassboost", description="Set bass boost (0-5) — reapplies to the current song without skipping")
@app_commands.describe(level="0 to 5")
async def bassboost_cmd(interaction: discord.Interaction, level: int):
    deferred = await safe_defer(interaction)
    if not 0 <= level <= 5:
        msg = "❌ Enter a level between 0 and 5."
        if deferred:
            await interaction.followup.send(msg, allowed_mentions=ALLOWED_NONE)
        else:
            await interaction.channel.send(msg, allowed_mentions=ALLOWED_NONE)
        return

    guild_id = interaction.guild_id
    BASSBOOST_LEVELS[guild_id] = level

    vc = interaction.guild.voice_client
    track = CURRENT_TRACK.get(guild_id)

    if vc and (vc.is_playing() or vc.is_paused()) and track:
        started_at = track.get("started_at") or time.monotonic()
        seek_offset = float(track.get("seek_offset", 0.0))
        elapsed = (time.monotonic() - started_at) + seek_offset
        duration = track.get("duration") or 0
        if duration:
            elapsed = min(max(0.0, elapsed), max(0.0, duration - 1))
        PENDING_RESTART[guild_id] = {"elapsed": elapsed}
        vc.stop()
        msg = f"🎛️ Bass boost {'disabled' if level == 0 else f'set to **{level}**'} — reapplied."
    else:
        msg = f"🎛️ Bass boost {'disabled' if level == 0 else f'set to **{level}**'}. (No track playing.)"

    if deferred:
        await interaction.followup.send(msg, allowed_mentions=ALLOWED_NONE)
    else:
        await interaction.channel.send(msg, allowed_mentions=ALLOWED_NONE)

    await send_or_edit_now_playing(
        guild_id,
        bot=bot,
        now_playing_channels=NOW_PLAYING_CHANNELS,
        now_playing_messages=NOW_PLAYING_MESSAGES,
        current_players=CURRENT_PLAYERS,
        current_track=CURRENT_TRACK,
        queue_getter=get_queue,
        bass_levels=BASSBOOST_LEVELS,
        treble_levels=TREBLEBOOST_LEVELS,
        vocal_levels=VOCALBOOST_LEVELS,
        allowed_mentions=ALLOWED_NONE,
    )

@tree.command(name="trebleboost", description="Set treble boost (0-5) — reapplies to the current song without skipping")
@app_commands.describe(level="0 to 5")
async def trebleboost_cmd(interaction: discord.Interaction, level: int):
    deferred = await safe_defer(interaction)
    if not 0 <= level <= 5:
        msg = "❌ Enter a level between 0 and 5."
        if deferred:
            await interaction.followup.send(msg, allowed_mentions=ALLOWED_NONE)
        else:
            await interaction.channel.send(msg, allowed_mentions=ALLOWED_NONE)
        return

    guild_id = interaction.guild_id
    TREBLEBOOST_LEVELS[guild_id] = level

    vc = interaction.guild.voice_client
    track = CURRENT_TRACK.get(guild_id)
    if vc and (vc.is_playing() or vc.is_paused()) and track:
        started_at = track.get("started_at") or time.monotonic()
        seek_offset = float(track.get("seek_offset", 0.0))
        elapsed = (time.monotonic() - started_at) + seek_offset
        duration = track.get("duration") or 0
        if duration:
            elapsed = min(max(0.0, elapsed), max(0.0, duration - 1))
        PENDING_RESTART[guild_id] = {"elapsed": elapsed}
        vc.stop()
        msg = f"✨ Treble boost {'disabled' if level == 0 else f'set to **{level}**'} — reapplied."
    else:
        msg = f"✨ Treble boost {'disabled' if level == 0 else f'set to **{level}**'}. (No track playing.)"

    if deferred:
        await interaction.followup.send(msg, allowed_mentions=ALLOWED_NONE)
    else:
        await interaction.channel.send(msg, allowed_mentions=ALLOWED_NONE)

    await send_or_edit_now_playing(
        guild_id,
        bot=bot,
        now_playing_channels=NOW_PLAYING_CHANNELS,
        now_playing_messages=NOW_PLAYING_MESSAGES,
        current_players=CURRENT_PLAYERS,
        current_track=CURRENT_TRACK,
        queue_getter=get_queue,
        bass_levels=BASSBOOST_LEVELS,
        treble_levels=TREBLEBOOST_LEVELS,
        vocal_levels=VOCALBOOST_LEVELS,
        allowed_mentions=ALLOWED_NONE,
    )

@tree.command(name="vocalboost", description="Set vocal boost (0-5) — reapplies to the current song without skipping")
@app_commands.describe(level="0 to 5")
async def vocalboost_cmd(interaction: discord.Interaction, level: int):
    deferred = await safe_defer(interaction)
    if not 0 <= level <= 5:
        msg = "❌ Enter a level between 0 and 5."
        if deferred:
            await interaction.followup.send(msg, allowed_mentions=ALLOWED_NONE)
        else:
            await interaction.channel.send(msg, allowed_mentions=ALLOWED_NONE)
        return

    guild_id = interaction.guild_id
    VOCALBOOST_LEVELS[guild_id] = level

    vc = interaction.guild.voice_client
    track = CURRENT_TRACK.get(guild_id)
    if vc and (vc.is_playing() or vc.is_paused()) and track:
        started_at = track.get("started_at") or time.monotonic()
        seek_offset = float(track.get("seek_offset", 0.0))
        elapsed = (time.monotonic() - started_at) + seek_offset
        duration = track.get("duration") or 0
        if duration:
            elapsed = min(max(0.0, elapsed), max(0.0, duration - 1))
        PENDING_RESTART[guild_id] = {"elapsed": elapsed}
        vc.stop()
        msg = f"🎤 Vocal boost {'disabled' if level == 0 else f'set to **{level}**'} — reapplied."
    else:
        msg = f"🎤 Vocal boost {'disabled' if level == 0 else f'set to **{level}**'}. (No track playing.)"

    if deferred:
        await interaction.followup.send(msg, allowed_mentions=ALLOWED_NONE)
    else:
        await interaction.channel.send(msg, allowed_mentions=ALLOWED_NONE)

    await send_or_edit_now_playing(
        guild_id,
        bot=bot,
        now_playing_channels=NOW_PLAYING_CHANNELS,
        now_playing_messages=NOW_PLAYING_MESSAGES,
        current_players=CURRENT_PLAYERS,
        current_track=CURRENT_TRACK,
        queue_getter=get_queue,
        bass_levels=BASSBOOST_LEVELS,
        treble_levels=TREBLEBOOST_LEVELS,
        vocal_levels=VOCALBOOST_LEVELS,
        allowed_mentions=ALLOWED_NONE,
    )

@tree.command(name="queue", description="Show the queue in an embed (same as Now Playing)")
async def queue_cmd(interaction: discord.Interaction):
    deferred = await safe_defer(interaction, ephemeral=True)
    NOW_PLAYING_CHANNELS[interaction.guild_id] = interaction.channel.id
    await send_or_edit_now_playing(
        interaction.guild_id,
        bot=bot,
        now_playing_channels=NOW_PLAYING_CHANNELS,
        now_playing_messages=NOW_PLAYING_MESSAGES,
        current_players=CURRENT_PLAYERS,
        current_track=CURRENT_TRACK,
        queue_getter=get_queue,
        bass_levels=BASSBOOST_LEVELS,
        treble_levels=TREBLEBOOST_LEVELS,
        vocal_levels=VOCALBOOST_LEVELS,
        allowed_mentions=ALLOWED_NONE,
    )
    if deferred:
        await interaction.followup.send("📋 Queue/Now Playing updated above.", ephemeral=True, allowed_mentions=ALLOWED_NONE)
    else:
        await interaction.channel.send("📋 Queue/Now Playing updated above.", allowed_mentions=ALLOWED_NONE)

@tree.command(name="debug", description="Check bot status and connection")
async def debug_cmd(interaction: discord.Interaction):
    deferred = await safe_defer(interaction, ephemeral=True)
    vc = interaction.guild.voice_client
    guild_id = interaction.guild_id
    q = get_queue(guild_id)

    embed = discord.Embed(title="Bot Debug Info", color=discord.Color.dark_red())

    if vc and vc.is_connected():
        embed.add_field(name="Voice Status", value=f"Connected to: {vc.channel.name}", inline=False)
        embed.add_field(name="Is Playing?", value=str(vc.is_playing()), inline=True)
        embed.add_field(name="Is Paused?", value=str(vc.is_paused()), inline=True)
    else:
        embed.add_field(name="Voice Status", value="Not connected", inline=False)

    track = CURRENT_TRACK.get(guild_id)
    if track:
        elapsed = (time.monotonic() - (track.get("started_at") or time.monotonic())) + float(track.get("seek_offset", 0.0))
        details = f"{track.get('title')}\n⏱ {fmt_time(elapsed)} / {fmt_time(track.get('duration') or 0)}"
        embed.add_field(name="Current Song", value=details, inline=False)
    else:
        embed.add_field(name="Current Song", value="No song playing", inline=False)

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

    if deferred:
        await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=ALLOWED_NONE)
    else:
        await interaction.channel.send(embed=embed, allowed_mentions=ALLOWED_NONE)

@tree.command(name="reset", description="Hard reset: stop audio, clear queue, remove Now Playing UI & filters")
async def reset_cmd(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    print(f"[RESET] invoked for guild {guild_id}")

    # Try to acknowledge immediately (ephemeral)
    acknowledged = False
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message("♻️ Resetting…", ephemeral=True, allowed_mentions=ALLOWED_NONE)
            acknowledged = True
            print("[RESET] sent initial ephemeral ack")
    except Exception as e:
        print(f"[RESET] initial ack failed: {e}")

    # ---- Stop & disconnect voice ----
    try:
        vc = interaction.guild.voice_client
        if vc:
            print("[RESET] stopping and disconnecting voice")
            vc.stop()
            await vc.disconnect(force=True)
    except Exception as e:
        print(f"[RESET] voice disconnect error: {e}")

    # ---- Clear all state for this guild ----
    try:
        get_queue(guild_id).clear()
        for m in (CURRENT_TRACK, CURRENT_PLAYERS, PENDING_RESTART,
                  BASSBOOST_LEVELS, TREBLEBOOST_LEVELS, VOCALBOOST_LEVELS):
            m.pop(guild_id, None)

        # Delete the Now Playing message if it exists
        await delete_now_playing_message(guild_id, now_playing_messages=NOW_PLAYING_MESSAGES)
        NOW_PLAYING_CHANNELS.pop(guild_id, None)
        print("[RESET] cleared queue, filters, messages, channels")
    except Exception as e:
        print(f"[RESET] state clear error: {e}")

    # ---- Final confirmation ----
    final_msg = "✅ Reset complete: disconnected, cleared queue/filters, and removed Now Playing."
    try:
        if acknowledged:
            await interaction.followup.send(final_msg, ephemeral=True, allowed_mentions=ALLOWED_NONE)
        else:
            # If we couldn't ack earlier, try a normal response first…
            if not interaction.response.is_done():
                await interaction.response.send_message(final_msg, ephemeral=True, allowed_mentions=ALLOWED_NONE)
            else:
                # …or last resort: post to the channel (works even if the interaction token died)
                await interaction.channel.send(final_msg, allowed_mentions=ALLOWED_NONE)
        print("[RESET] sent final confirmation")
    except Exception as e:
        print(f"[RESET] final confirm failed: {e}")


# ---------- Moving progress bar refresh ----------
@tasks.loop(seconds=2)
async def _progress_tick():
    for guild_id in list(NOW_PLAYING_MESSAGES.keys()):
        track = CURRENT_TRACK.get(guild_id)
        guild = bot.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if not track or not vc or not (vc.is_playing() or vc.is_paused()):
            continue
        try:
            await send_or_edit_now_playing(
                guild_id,
                bot=bot,
                now_playing_channels=NOW_PLAYING_CHANNELS,
                now_playing_messages=NOW_PLAYING_MESSAGES,
                current_players=CURRENT_PLAYERS,
                current_track=CURRENT_TRACK,
                queue_getter=get_queue,
                bass_levels=BASSBOOST_LEVELS,
                treble_levels=TREBLEBOOST_LEVELS,
                vocal_levels=VOCALBOOST_LEVELS,
                allowed_mentions=ALLOWED_NONE,
            )
        except Exception:
            pass

# ---------- Lifecycle ----------
@bot.event
async def on_ready():
    activity = discord.Activity(type=discord.ActivityType.listening, name="/play")
    await bot.change_presence(status=discord.Status.online, activity=activity)
    await tree.sync()
    if not _progress_tick.is_running():
        _progress_tick.start()
    print(f"✅ Logged in as {bot.user}!")
    print("successfully finished startup")

bot.run(TOKEN)
