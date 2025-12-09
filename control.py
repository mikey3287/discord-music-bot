# control.py
import discord
from helper import reply_safe, send_or_edit_now_playing

class PlayerControls(discord.ui.View):

    def __init__(
        self, *, bot, 
        now_playing_channels, now_playing_messages,
        current_players, current_track, queue_getter,
        bass_levels, treble_levels, vocal_levels,
        allowed_mentions=None
    ):
        super().__init__(timeout=None)
        self.bot = bot
        self.now_playing_channels = now_playing_channels
        self.now_playing_messages = now_playing_messages
        self.current_players = current_players
        self.current_track = current_track
        self.queue_getter = queue_getter
        self.bass_levels = bass_levels
        self.treble_levels = treble_levels
        self.vocal_levels = vocal_levels
        self.allowed_mentions = allowed_mentions or discord.AllowedMentions.none()

    async def _refresh(self, guild_id):
        await send_or_edit_now_playing(
            guild_id,
            bot=self.bot,
            now_playing_channels=self.now_playing_channels,
            now_playing_messages=self.now_playing_messages,
            current_players=self.current_players,
            current_track=self.current_track,
            queue_getter=self.queue_getter,
            bass_levels=self.bass_levels,
            treble_levels=self.treble_levels,
            vocal_levels=self.vocal_levels,
            allowed_mentions=self.allowed_mentions,
            view=self
        )

    def _vc(self, interaction):
        return interaction.guild.voice_client if interaction.guild else None

    @discord.ui.button(label="Pause/Resume", emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="controls:toggle")
    async def toggle_btn(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        vc = self._vc(interaction)
        if not vc:
            return await reply_safe(interaction, "❌ Not connected.")
        if vc.is_paused():
            vc.resume()
            await reply_safe(interaction, "▶️ Resumed.")
        elif vc.is_playing():
            vc.pause()
            await reply_safe(interaction, "⏸️ Paused.")
        else:
            await reply_safe(interaction, "Nothing playing.")
        await self._refresh(interaction.guild_id)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="controls:skip")
    async def skip_btn(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        vc = self._vc(interaction)
        if vc and vc.is_playing():
            vc.stop()
            await reply_safe(interaction, "⏭️ Skipping…")
        else:
            await reply_safe(interaction, "❌ Nothing is playing.")
        await self._refresh(interaction.guild_id)

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="controls:stop")
    async def stop_btn(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        vc = self._vc(interaction)
        gid = interaction.guild_id
        if vc:
            vc.stop()
            await vc.disconnect(force=True)

        self.queue_getter(gid).clear()
        self.current_track.pop(gid, None)
        self.current_players.pop(gid, None)

        await reply_safe(interaction, "🛑 Stopped and cleared.")
        await self._refresh(gid)

    @discord.ui.button(label="Vol -", emoji="🔉", style=discord.ButtonStyle.secondary, custom_id="controls:vol_down")
    async def vol_down(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        player = self.current_players.get(interaction.guild_id)
        if not player:
            return await reply_safe(interaction, "❌ No player.")
        player.volume = max(0, player.volume - 0.1)
        await reply_safe(interaction, f"🔉 Volume: {int(player.volume*100)}%")
        await self._refresh(interaction.guild_id)

    @discord.ui.button(label="Vol +", emoji="🔊", style=discord.ButtonStyle.secondary, custom_id="controls:vol_up")
    async def vol_up(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        player = self.current_players.get(interaction.guild_id)
        if not player:
            return await reply_safe(interaction, "❌ No player.")
        player.volume = min(1, player.volume + 0.1)
        await reply_safe(interaction, f"🔊 Volume: {int(player.volume*100)}%")
        await self._refresh(interaction.guild_id)

    @discord.ui.button(label="Queue", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="controls:queue")
    async def queue_btn(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        await self._refresh(interaction.guild_id)
        await reply_safe(interaction, "📋 Queue refreshed.")
