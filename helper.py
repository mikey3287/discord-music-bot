# helper.py
import time
import asyncio
from collections import deque
from typing import Callable, Dict, Any, List, Tuple

import discord
import yt_dlp

from config import load_theme_data
from datetime import datetime


# -------------------------------------------------
# TIME & PROGRESS BAR
# -------------------------------------------------

def fmt_time(seconds: float) -> str:
    try:
        seconds = int(max(0, round(seconds)))
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
    except Exception:
        return "?:??"


def make_progress_bar(elapsed: float, total: float, width: int = 16) -> str:
    """Progress bar that changes the dot based on theme."""
    if not total or total <= 0:
        return "─" * width

    theme = load_theme_data().get("mode", "normal")

    # ------ Theme emoji mapping ------
    dot = "🟢"  # default
    if theme == "christmas":
        dot = "🎄"
    elif theme == "neon":
        dot = "💜"
    elif theme == "winter":
        dot = "❄️"
    elif theme == "pastel":
        dot = "🌸"
    elif theme == "dark":
        dot = "⚫"
    elif theme == "custom":
        dot = "🎨"

    ratio = max(0.0, min(1.0, float(elapsed) / float(total)))
    pos = int(ratio * (width - 1))

    chars = ["─"] * width
    chars[pos] = dot
    return "".join(chars)


# -------------------------------------------------
# SAFE MESSAGE FUNCTIONS
# -------------------------------------------------

async def safe_defer(interaction: discord.Interaction, *, ephemeral: bool = False) -> bool:
    if interaction.response.is_done():
        return True
    try:
        await interaction.response.defer(ephemeral=ephemeral)
        return True
    except (discord.NotFound, discord.HTTPException):
        return False


async def reply_safe(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool = False,
    allowed_mentions: discord.AllowedMentions | None = None,
):
    """Reply safely; fallback to channel message if interaction expired."""
    if allowed_mentions is None:
        allowed_mentions = discord.AllowedMentions.none()

    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                content, ephemeral=ephemeral, allowed_mentions=allowed_mentions
            )
        else:
            await interaction.followup.send(
                content, ephemeral=ephemeral, allowed_mentions=allowed_mentions
            )
    except Exception:
        try:
            await interaction.channel.send(content, allowed_mentions=allowed_mentions)
        except Exception:
            pass


# -------------------------------------------------
# YOUTUBE SEARCH
# -------------------------------------------------

async def search_youtube(query: str) -> List[Tuple[str, str, int]]:
    """Returns list of (url, title, duration)."""
    if not query.startswith("http"):
        query = f"ytsearch:{query}"

    results: List[Tuple[str, str, int]] = []

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
    except Exception:
        await asyncio.sleep(1)
        info = await loop.run_in_executor(None, lambda: _extract(query))

    entries = info["entries"] if "entries" in info else [info]

    for entry in entries:
        if entry and entry.get("url") and entry.get("title"):
            results.append(
                (entry["url"], entry["title"], entry.get("duration", 0) or 0)
            )

    return results


# -------------------------------------------------
# AUDIO FILTERS
# -------------------------------------------------

def build_filter_chain(
    guild_id: int,
    bass_levels: Dict[int, int],
    treble_levels: Dict[int, int],
    vocal_levels: Dict[int, int],
) -> str:
    bass = bass_levels.get(guild_id, 0)
    treb = treble_levels.get(guild_id, 0)
    vocal = vocal_levels.get(guild_id, 0)

    filters = []

    if bass > 0:
        filters.append(f"bass=g={min(bass,5)}")

    if treb > 0:
        filters.append(f"treble=g={min(treb,5)}")

    if vocal > 0:
        filters.append(
            f"equalizer=f=3000:width_type=h:width=2000:g={min(vocal,5)}"
        )

    if not filters:
        return "-vn"

    return f'-vn -af "{",".join(filters)},volume=1"'


# -------------------------------------------------
# THEMING ENGINE
# -------------------------------------------------

def get_theme_color_and_title() -> tuple[discord.Color, str, str]:
    """Returns embed color, title, and theme mode."""
    data = load_theme_data()
    mode = data.get("mode", "normal")
    custom_hex = data.get("custom_color", "#ffffff")

    if mode == "christmas":
        return discord.Color.red(), "🎄 Now Playing — Christmas Edition 🎁", mode
    if mode == "winter":
        return discord.Color.blue(), "❄️ Now Playing — Winter Edition", mode
    if mode == "neon":
        return discord.Color.magenta(), "💜 Now Playing — Neon Mode", mode
    if mode == "pastel":
        return discord.Color.from_str("#ffb3ba"), "🌸 Now Playing — Pastel Mode", mode
    if mode == "dark":
        return discord.Color.dark_gray(), "🌑 Now Playing — Dark Mode", mode
    if mode == "custom":
        try:
            return discord.Color.from_str(custom_hex), "🎨 Now Playing — Custom Theme", mode
        except:
            return discord.Color.blurple(), "🎨 Now Playing", mode

    return discord.Color.blurple(), "🎶 Now Playing", mode


# -------------------------------------------------
# EMBED BUILDER
# -------------------------------------------------

def build_now_playing_embed(
    guild_id: int,
    *,
    current_players: Dict[int, discord.PCMVolumeTransformer],
    current_track: Dict[int, Dict[str, Any]],
    queue_getter: Callable[[int], deque],
    bass_levels: Dict[int, int],
    treble_levels: Dict[int, int],
    vocal_levels: Dict[int, int],
    bot: discord.Client,
) -> discord.Embed:

    q = queue_getter(guild_id)
    track = current_track.get(guild_id)

    # Volume
    vol = 100
    if current_players.get(guild_id):
        try:
            vol = int(round(current_players[guild_id].volume * 100))
        except:
            vol = 100

    # Theme (color + title)
    color, title_text, theme_mode = get_theme_color_and_title()
    embed = discord.Embed(title=title_text, color=color)

    # ------------------
    # NOW PLAYING FIELD
    # ------------------
    if track:
        song_title = track.get("title") or "Unknown Title"
        duration = track.get("duration") or 0
        started_at = track.get("started_at", time.monotonic())
        seek = float(track.get("seek_offset", 0.0))

        elapsed = (time.monotonic() - started_at) + seek
        elapsed = max(0, min(elapsed, duration))

        bar = make_progress_bar(elapsed, duration)

        embed.add_field(
            name=song_title,
            value=f"`{fmt_time(elapsed)} {bar} {fmt_time(duration)}`",
            inline=False,
        )
    else:
        embed.add_field(
            name="Nothing Playing",
            value="Use `/play <song>` to start music.",
            inline=False,
        )

    # ------------------
    # QUEUE FIELD
    # ------------------
    if q:
        lines = []
        for i, item in enumerate(list(q)[:10], 1):
            url, title, dur = item
            lines.append(f"**{i}.** {title} — `{fmt_time(dur)}`")
        embed.add_field(name="▶️ Next Up", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="▶️ Next Up", value="_Queue empty_", inline=False)

    # ------------------
    # FOOTER
    # ------------------
    playlist_len = len(q)

    footer = f"Vol: {vol}% | Bass: {bass_levels.get(guild_id,0)} | Treble: {treble_levels.get(guild_id,0)} | Vocal: {vocal_levels.get(guild_id,0)}"

    if playlist_len > 0:
        footer += f" | Playlist: {playlist_len} songs"

    if theme_mode == "christmas":
        footer += " 🎄"
    if theme_mode == "winter":
        footer += " ❄️"

    embed.set_footer(text=footer)

    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)

    return embed


# -------------------------------------------------
# MESSAGE SENDER / EDITOR
# -------------------------------------------------

async def send_or_edit_now_playing(
    guild_id: int,
    *,
    bot: discord.Client,
    now_playing_channels: Dict[int, int],
    now_playing_messages: Dict[int, discord.Message],
    current_players: Dict[int, discord.PCMVolumeTransformer],
    current_track: Dict[int, Dict[str, Any]],
    queue_getter: Callable[[int], deque],
    bass_levels: Dict[int, int],
    treble_levels: Dict[int, int],
    vocal_levels: Dict[int, int],
    allowed_mentions: discord.AllowedMentions | None = None,
    view: discord.ui.View | None = None,
):
    """Creates or edits the Now Playing embed."""

    channel_id = now_playing_channels.get(guild_id)
    if not channel_id:
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        return

    embed = build_now_playing_embed(
        guild_id,
        current_players=current_players,
        current_track=current_track,
        queue_getter=queue_getter,
        bass_levels=bass_levels,
        treble_levels=treble_levels,
        vocal_levels=vocal_levels,
        bot=bot,
    )

    msg = now_playing_messages.get(guild_id)

    if msg:
        try:
            await msg.edit(embed=embed, view=view)
            return
        except (discord.NotFound, discord.HTTPException):
            now_playing_messages.pop(guild_id, None)

    # Create new message
    try:
        new_msg = await channel.send(embed=embed, allowed_mentions=allowed_mentions, view=view)
        now_playing_messages[guild_id] = new_msg
    except discord.HTTPException:
        pass


async def delete_now_playing_message(
    guild_id: int,
    *,
    now_playing_messages: Dict[int, discord.Message],
):
    """Deletes embed on /reset."""
    msg = now_playing_messages.pop(guild_id, None)
    if not msg:
        return
    try:
        await msg.delete()
    except:
        pass
