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
bot = commands.Bot(command_prefix="!", intents=intents)
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

def get_queue(guild_id):
    return SONG_QUEUES.setdefault(guild_id, deque())

async def search_youtube(query):
    if not query.startswith("http"):
        query = f"ytsearch:{query}"

    results = []

    with yt_dlp.YoutubeDL({
        'format': 'bestaudio/best',
        'quiet': True,
        'noplaylist': False,
        'extract_flat': True,
    }) as ydl:
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

    with yt_dlp.YoutubeDL({'format': 'bestaudio', 'cookiefile': 'cookies.txt'}) as ydl:
        info = ydl.extract_info(url, download=False)
        audio_url = info['url']

    # Apply bassboost if enabled for this guild
    bassboost_level = BASSBOOST_LEVELS.get(guild_id, 0)

    options = '-vn'
    if bassboost_level > 0:
        # Cap level to avoid distortion
        gain = min(bassboost_level, 100)
        options += f' -af "bass=g={gain},dynaudnorm=f=200"'


    ffmpeg_opts = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': options
    }

    source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_opts)
    player = discord.PCMVolumeTransformer(source)

    def after_play(err):
        if err:
            print(f"Error in playback: {err}")
        fut = asyncio.run_coroutine_threadsafe(play_next(voice_client, guild_id, interaction), bot.loop)
        try:
            fut.result()
        except Exception as e:
            print(f"Next song error: {e}")

    voice_client.play(player, after=after_play)

    # ✅ Update "Now Playing" message in last known channel
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


@tree.command(name="play", description="Play a song from YouTube")
@app_commands.describe(query="YouTube URL or search term")
async def play(interaction: discord.Interaction, query: str):
    try:
        await interaction.response.defer()  # Defer immediately

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

        if not interaction.guild.voice_client:
            vc = await voice.channel.connect()
            await play_next(vc, interaction.guild_id, interaction)

        if len(results) > 1:
            await interaction.followup.send(f"✅ Added **{len(results)} songs** from playlist to the queue.")
        else:
            await interaction.followup.send(f"✅ Added to queue: {results[0][1]}")

    except Exception as e:
        try:
            # If not already deferred, try sending directly
            await interaction.followup.send(f"❌ Error: {e}")
        except discord.errors.InteractionResponded:
            print("Interaction already responded. Error:", e)




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


@tree.command(name="bassboost", description="Set bass boost level (1–100, or 0 to disable)")
@app_commands.describe(level="Bass boost level: 0 to 100")
async def bassboost(interaction: discord.Interaction, level: int):
    if not 0 <= level <= 100:
        await interaction.response.send_message("❌ Please enter a level between 0 and 100.", ephemeral=True)
        return

    BASSBOOST_LEVELS[interaction.guild_id] = level
    if level == 0:
        await interaction.response.send_message("🎛️ Bass boost has been **disabled**.")
    else:
        await interaction.response.send_message(f"🎛️ Bass boost level set to **{level}**.")



@bot.event
async def on_ready():
    activity = discord.Activity(type=discord.ActivityType.streaming, name="/playing ....")
    await bot.change_presence(status=discord.Status.online, activity=activity)
    await tree.sync()
    print(f"✅ Logged in as {bot.user}!")

bot.run(TOKEN)
