# control.py
import discord
from helper import reply_safe, send_or_edit_now_playing

class PlayerControls(discord.ui.View):
    """Persistent control panel for music playback."""
    def __init__(
        self, *, bot: discord.Client,
        now_playing_channels: dict, now_playing_messages: dict,
        current_players: dict, current_track: dict, queue_getter,
        bass_levels: dict, treble_levels: dict, vocal_levels: dict,
        allowed_mentions: discord.AllowedMentions | None = None
    ):
        super().__init__(timeout=None)  # persistent
        self.bot = bot
        self.now_playing_channels = now_playing_channels
        self.now_playing_messages = now_playing_messages
        self.current_players = current_players
        self.current_track = current_track
        self.get_queue = queue_getter
        self.bass_levels = bass_levels
        self.treble_levels = treble_levels
        self.vocal_levels = vocal_levels
        self.allowed_mentions = allowed_mentions or discord.AllowedMentions.none()

    # ---- helpers ----
    async def _refresh(self, guild_id: int):
        await send_or_edit_now_playing(
            guild_id,
            bot=self.bot,
            now_playing_channels=self.now_playing_channels,
            now_playing_messages=self.now_playing_messages,
            current_players=self.current_players,
            current_track=self.current_track,
            queue_getter=self.get_queue,
            bass_levels=self.bass_levels,
            treble_levels=self.treble_levels,
            vocal_levels=self.vocal_levels,
            allowed_mentions=self.allowed_mentions,
            view=self,  # keep controls visible
        )

    def _vc(self, interaction: discord.Interaction):
        g = interaction.guild
        return g.voice_client if g else None

    # ---- Buttons (custom_id is REQUIRED for persistent views) ----

    @discord.ui.button(label="Pause/Resume", style=discord.ButtonStyle.primary, emoji="⏯️", custom_id="controls:toggle")
    async def btn_toggle(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        vc = self._vc(interaction)
        if not vc:
            return await reply_safe(interaction, "❌ Not connected.", ephemeral=True)
        if vc.is_paused():
            vc.resume()
            await reply_safe(interaction, "▶️ Resumed.", ephemeral=True)
        elif vc.is_playing():
            vc.pause()
            await reply_safe(interaction, "⏸️ Paused.", ephemeral=True)
        else:
            await reply_safe(interaction, "ℹ️ Nothing to play.", ephemeral=True)
        await self._refresh(interaction.guild_id)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭️", custom_id="controls:skip")
    async def btn_skip(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        vc = self._vc(interaction)
        if vc and vc.is_playing():
            vc.stop()  # after callback will start next
            await reply_safe(interaction, "⏭️ Skipping…", ephemeral=True)
            await self._refresh(interaction.guild_id)
        else:
            await reply_safe(interaction, "❌ Nothing is playing.", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹️", custom_id="controls:stop")
    async def btn_stop(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        vc = self._vc(interaction)
        if vc:
            try:
                vc.stop()
                await vc.disconnect(force=True)
            except Exception:
                pass
        gid = interaction.guild_id
        self.get_queue(gid).clear()
        self.current_track.pop(gid, None)
        self.current_players.pop(gid, None)
        await reply_safe(interaction, "🛑 Stopped & cleared.", ephemeral=True)
        await self._refresh(gid)

    @discord.ui.button(label="- Vol", style=discord.ButtonStyle.secondary, emoji="🔉", custom_id="controls:vol_down")
    async def btn_vol_down(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        player = self.current_players.get(interaction.guild_id)
        if not player:
            return await reply_safe(interaction, "❌ No player.", ephemeral=True)
        newv = max(0.0, round(player.volume - 0.1, 2))
        player.volume = newv
        await reply_safe(interaction, f"🔉 Volume: {int(newv*100)}%", ephemeral=True)
        await self._refresh(interaction.guild_id)

    @discord.ui.button(label="+ Vol", style=discord.ButtonStyle.secondary, emoji="🔊", custom_id="controls:vol_up")
    async def btn_vol_up(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        player = self.current_players.get(interaction.guild_id)
        if not player:
            return await reply_safe(interaction, "❌ No player.", ephemeral=True)
        newv = min(1.0, round(player.volume + 0.1, 2))
        player.volume = newv
        await reply_safe(interaction, f"🔊 Volume: {int(newv*100)}%", ephemeral=True)
        await self._refresh(interaction.guild_id)

    @discord.ui.button(label="Queue", style=discord.ButtonStyle.secondary, emoji="📋", custom_id="controls:queue")
    async def btn_queue(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        await self._refresh(interaction.guild_id)
        await reply_safe(interaction, "📋 Now Playing refreshed.", ephemeral=True)