# helper.py
import time
import asyncio
from collections import deque
from typing import Callable, Dict, Any

import discord
import yt_dlp

# ---------- Small utils ----------

def fmt_time(seconds: float) -> str:
    try:
        seconds = int(max(0, round(seconds)))
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
    except Exception:
        return "?:??"

def make_progress_bar(elapsed: float, total: float, width: int = 16) -> str:
    """Progress bar with colored dot."""
    if not total or total <= 0:
        return "─" * width
    ratio = max(0.0, min(1.0, float(elapsed) / float(total)))
    pos = int(ratio * (width - 1))
    chars = ["─"] * width
    if width:
        chars[pos] = "🟢"  # change to 🔵 / 🔴 / 🎵 etc.
    return "".join(chars)

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
    """Reply safely; falls back to channel.send if the token is invalid."""
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

# ---------- YouTube Search ----------

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
    except Exception:
        await asyncio.sleep(1)
        info = await loop.run_in_executor(None, lambda: _extract(query))

    entries = info["entries"] if "entries" in info else [info]
    for entry in entries:
        if entry and entry.get("url") and entry.get("title"):
            results.append((entry["url"], entry["title"], entry.get("duration", 0) or 0))
    return results

# ---------- Audio filter building ----------

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
        g = min(bass, 5)
        filters.append(f"bass=g={g}")
    if treb > 0:
        g = min(treb, 5)
        filters.append(f"treble=g={g}")
    if vocal > 0:
        g = min(vocal, 5)
        filters.append(f"equalizer=f=3000:width_type=h:width=2000:g={g}")  # presence boost

    if not filters:
        return "-vn"
    return f'-vn -af "{",".join(filters)},volume=1"'

# ---------- Embeds & UI ----------

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

    vol = 100
    if current_players.get(guild_id):
        try:
            vol = int(round(current_players[guild_id].volume * 100))
        except Exception:
            vol = 100

    embed = discord.Embed(title="🎶 Now Playing", color=discord.Color.blurple())

    if track:
        title = track.get("title") or "Unknown title"
        duration = track.get("duration") or 0
        started_at = track.get("started_at") or time.monotonic()
        seek = float(track.get("seek_offset", 0.0))
        elapsed = (time.monotonic() - started_at) + seek
        elapsed = max(0.0, min(elapsed, max(0.0, duration - 0.5) if duration else elapsed))

        bar = make_progress_bar(elapsed, duration)
        embed.add_field(
            name=title,
            value=f"`{fmt_time(elapsed)} {bar} {fmt_time(duration) if duration else '?:??'}`",
            inline=False
        )
    else:
        embed.add_field(name="Nothing playing", value="Use `/play <url or search>`", inline=False)

    if q:
        lines = []
        for i, item in enumerate(list(q)[:10], 1):
            _u, t, d = item[0], item[1], (item[2] if len(item) > 2 else 0)
            lines.append(f"**{i}.** {t}  ·  `{fmt_time(d) if d else '?:??'}`")
        embed.add_field(name="▶️ Next Up", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="▶️ Next Up", value="_Queue is empty_", inline=False)

    bass = bass_levels.get(guild_id, 0)
    treb = treble_levels.get(guild_id, 0)
    voc  = vocal_levels.get(guild_id, 0)
    embed.set_footer(text=" | ".join([f"Vol: {vol}%", f"Bass: {bass}", f"Treble: {treb}", f"Vocal: {voc}"]))

    if bot.user and bot.user.display_avatar:
        embed.set_thumbnail(url=bot.user.display_avatar.url)

    return embed

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
    view: discord.ui.View | None = None,   # << allow attaching a View
):
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
            await msg.edit(embed=embed, content=None, view=view)
            return
        except (discord.NotFound, discord.HTTPException):
            now_playing_messages.pop(guild_id, None)

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
    msg = now_playing_messages.pop(guild_id, None)
    if not msg:
        return
    try:
        await msg.delete()
    except (discord.NotFound, discord.HTTPException):
        pass
