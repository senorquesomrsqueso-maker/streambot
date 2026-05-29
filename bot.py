import os
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from motor.motor_asyncio import AsyncIOMotorClient
from bson.objectid import ObjectId
from TikTokLive import TikTokLiveClient
from TikTokLive.events import ConnectEvent, DisconnectEvent
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
    """Monitorea en bucle infinito de forma asíncrona a un creador sin congelar Discord"""
    username_clean = username.replace("@", "").strip()
    
    while True:
        streamer = await streamers_col.find_one({"username": username_clean, "active": True})
        if not streamer:
            break

        client = TikTokLiveClient(unique_id=username_clean)

        @client.on(ConnectEvent)
        async def on_connect(event: ConnectEvent):
            channel = bot.get_channel(int(os.getenv('CHANNEL_START_ID')))
            if channel:
                await channel.send(f"🔴 **¡Anuncio de Stream!** El creador <@{discord_user_id}> está EN VIVO en TikTok.\n🔗 https://tiktok.com/@{username_clean}/live")

        @client.on(DisconnectEvent)
        async def on_disconnect(event: DisconnectEvent):
            channel = bot.get_channel(int(os.getenv('CHANNEL_END_ID')))
            if channel:
                await channel.send(f"⚠️ El stream de **@{username_clean}** ha finalizado. Estadísticas solicitadas en privado.")
            
            try:
                user = await bot.fetch_user(discord_user_id)
                await user.send(
                    f"👋 ¡Tu directo en **@{username_clean}** ha terminado! Presiona el botón de abajo para registrar tus estadísticas de hoy.",
                    view=BotonDMView(username_clean)
                )
            except Exception as e:
                print(f"No se pudo enviar DM al usuario {discord_user_id}: {e}")

        try:
            await client.start()
        except Exception:
            await asyncio.sleep(180)

# ==========================================
# EVENTOS Y COMANDOS DE INICIO
# ==========================================
@bot.event
async def on_ready():
    # Sincroniza los comandos '/' (Slash) con Discord
    await bot.tree.sync()
    print(f'🤖 Bot de Streaming Líder activo como {bot.user}')
    
    cursor = streamers_col.find({"active": True})
    async for streamer in cursor:
        asyncio.create_task(start_monitoring(streamer["username"], streamer["discord_user_id"]))
        print(f"🔄 Re-activado monitoreo automático para: @{streamer['username']}")

# NUEVO COMANDO SLASH (/)
@bot.tree.command(name="register", description="Enlaza tu cuenta de TikTok con tu usuario de Discord")
@app_commands.describe(tiktok_username="Escribe tu nombre de usuario de TikTok (sin el @)")
async def register(interaction: discord.Interaction, tiktok_username: str):
    username_clean = tiktok_username.replace("@", "").strip()
    
    # 1. Guardamos en DB
    await streamers_col.update_one(
        {"username": username_clean},
        {"$set": {"username": username_clean, "discord_user_id": interaction.user.id, "active": True}},
        upsert=True
    )
    
    # 2. Encendemos monitor
    asyncio.create_task(start_monitoring(username_clean, interaction.user.id))
    
    # 3. Aviso oculto (efímero) solo para el usuario
    await interaction.response.send_message(f"✅ ¡Registro Exitoso! Tu cuenta `@{username_clean}` está vinculada y siendo monitoreada.", ephemeral=True)

    # 4. Aviso público en el canal de Staff
    staff_channel = bot.get_channel(int(os.getenv('CHANNEL_STAFF_ID')))
    if staff_channel:
        await staff_channel.send(f"🆕 **Nuevo Registro:** El usuario {interaction.user.mention} acaba de registrar la cuenta de TikTok **@{username_clean}**.")

# Hilo falso para que Render no moleste con los puertos
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
class DummyServer(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"Bot OK")
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.getenv('PORT', 8080))), DummyServer).serve_forever(), daemon=True).start()

bot.run(os.getenv('DISCORD_TOKEN'))
