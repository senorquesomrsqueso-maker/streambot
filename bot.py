import os
import asyncio
import discord
from discord.ext import commands
from motor.motor_asyncio import AsyncIOMotorClient
from bson.objectid import ObjectId
from TikTokLive import TikTokLiveClient
from TikTokLive.events import LiveStartEvent, LiveEndEvent
from dotenv import load_dotenv

load_dotenv()

# --- CONFIGURACIÓN DE DISCORD ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --- CONEXIÓN DE BASE DE DATOS ---
db_client = AsyncIOMotorClient(os.getenv('MONGO_URI'))
db = db_client.bot_database
streamers_col = db.streamers

# ==========================================
# FASE 3 Y 4: SITEMA DE REVISIÓN PARA HELPERS
# ==========================================
class HelperReviewView(discord.ui.View):
    def __init__(self, reporte_id: str):
        super().__init__(timeout=None)
        self.reporte_id = reporte_id

    @discord.ui.button(label="Aprobar ✅", style=discord.ButtonStyle.success)
    async def aprobar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await db.reportes.update_one({"_id": ObjectId(self.reporte_id)}, {"$set": {"estado": "aprobado"}})
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=f"🟢 **Reporte Aprobado por {interaction.user.mention}**", view=self)

    @discord.ui.button(label="Rechazar ❌", style=discord.ButtonStyle.danger)
    async def rechazar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await db.reportes.update_one({"_id": ObjectId(self.reporte_id)}, {"$set": {"estado": "rechazado"}})
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=f"🔴 **Reporte Rechazado por {interaction.user.mention}**", view=self)

# ==========================================
# FASE 2: FORMULARIO EN MENSAGE DIRECTO (MODAL)
# ==========================================
class ReporteStatsModal(discord.ui.Modal):
    def __init__(self, tiktok_username: str):
        super().__init__(title=f'Reporte: @{tiktok_username}')
        self.tiktok_username = tiktok_username

    horas = discord.ui.TextInput(label='Horas Totales de Stream', placeholder='Ej: 3.5')
    vistas = discord.ui.TextInput(label='Promedio de Espectadores', placeholder='Ej: 45')
    donaciones = discord.ui.TextInput(label='Regalos Recibidos', placeholder='Ej: 1200 monedas / Ninguno', required=False)
    link_prueba = discord.ui.TextInput(label='Enlace de Captura (Imgur/Discord)', style=discord.TextStyle.paragraph, placeholder='Pega el link de la imagen de tus stats aquí')

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("✅ Tus estadísticas han sido enviadas a revisión por los Helpers.", ephemeral=True)
        
        # Guardamos en la base de datos
        result = await db.reportes.insert_one({
            "usuario_discord": interaction.user.name,
            "id_discord": interaction.user.id,
            "tiktok": self.tiktok_username,
            "horas": self.horas.value,
            "vistas": self.vistas.value,
            "donaciones": self.donaciones.value,
            "prueba": self.link_prueba.value,
            "estado": "pendiente"
        })

        # Enviamos el panel de control al canal secreto de los Helpers
        helpers_channel = bot.get_channel(int(os.getenv('CHANNEL_HELPERS_ID')))
        if helpers_channel:
            embed = discord.Embed(title="📋 Nuevo Reporte de Stream para Verificar", color=discord.Color.purple())
            embed.add_field(name="Creador", value=f"{interaction.user.mention} (@{self.tiktok_username})", inline=False)
            embed.add_field(name="Horas Transmitidas", value=self.horas.value, inline=True)
            embed.add_field(name="Audiencia Promedio", value=self.vistas.value, inline=True)
            embed.add_field(name="Donaciones", value=self.donaciones.value or "N/A", inline=True)
            embed.add_field(name="Enlace de Evidencia", value=self.link_prueba.value, inline=False)
            
            if self.link_prueba.value.startswith("http"):
                embed.set_image(url=self.link_prueba.value)

            await helpers_channel.send(embed=embed, view=HelperReviewView(str(result.inserted_id)))

class BotonDMView(discord.ui.View):
    def __init__(self, tiktok_username: str):
        super().__init__(timeout=None)
        self.tiktok_username = tiktok_username

    @discord.ui.button(label="📝 Enviar Datos del Live", style=discord.ButtonStyle.green)
    async def abrir_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ReporteStatsModal(self.tiktok_username))

# ==========================================
# FASE 1: MONITOREO EN VIVO (TIKTOK)
# ==========================================
async def start_monitoring(username, discord_user_id):
    """Monitorea de forma asíncrona a un creador sin congelar Discord"""
    client = TikTokLiveClient(unique_id=username)

    @client.on(LiveStartEvent)
    async def on_live_start(event: LiveStartEvent):
        channel = bot.get_channel(int(os.getenv('CHANNEL_START_ID')))
        if channel:
            await channel.send(f"🔴 **¡Anuncio de Stream!** El creador <@{discord_user_id}> está EN VIVO en TikTok.\n🔗 https://tiktok.com/@{username}/live")

    @client.on(LiveEndEvent)
    async def on_live_end(event: LiveEndEvent):
        # Alerta en el canal de cierres
        channel = bot.get_channel(int(os.getenv('CHANNEL_END_ID')))
        if channel:
            await channel.send(f"⚠️ El stream de **@{username}** ha finalizado. Estadísticas solicitadas en privado.")
        
        # Enviar el botón interactivo al DM del creador
        try:
            user = await bot.fetch_user(discord_user_id)
            await user.send(
                f"👋 ¡Tu directo en **@{username}** ha terminado! Presiona el botón de abajo para registrar tus estadísticas de hoy.",
                view=BotonDMView(username)
            )
        except Exception as e:
            print(f"No se pudo enviar DM al usuario {discord_user_id}: {e}")

    try:
        await client.start()
    except Exception as e:
        print(f"Error en la conexión con TikTok para @{username}: {e}")

# ==========================================
# EVENTOS Y COMANDOS DE INICIO
# ==========================================
@bot.event
async def on_ready():
    print(f'🤖 Bot de Streaming Líder activo como {bot.user}')
    
    # PASO EXTRA PROFESIONAL: Si el bot se reinicia en Render, vuelve a activar todos los monitores guardados
    cursor = streamers_col.find({"active": True})
    async for streamer in cursor:
        asyncio.create_task(start_monitoring(streamer["username"], streamer["discord_user_id"]))
        print(f"🔄 Re-activado monitoreo automático para: @{streamer['username']}")

@bot.command()
async def register(ctx, tiktok_username: str):
    """Permite a los creadores enlazar su cuenta de TikTok con su Discord"""
    username_clean = tiktok_username.replace("@", "").strip()
    
    # Guardamos la relación Discord-ID <-> TikTok-User en la base de datos
    await streamers_col.update_one(
        {"username": username_clean},
        {"$set": {"username": username_clean, "discord_user_id": ctx.author.id, "active": True}},
        upsert=True
    )
    
    # Encendemos el monitor en segundo plano para este creador inmediatamente
    asyncio.create_task(start_monitoring(username_clean, ctx.author.id))
    await ctx.send(f"✅ ¡Registro Exitoso! Hola {ctx.author.mention}, tu TikTok `@{username_clean}` está siendo monitoreado 24/7.")

bot.run(os.getenv('DISCORD_TOKEN'))
