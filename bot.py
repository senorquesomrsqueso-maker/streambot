import os
import asyncio
import re
import aiohttp
import discord
import logging
import certifi
from discord.ext import commands
from discord import app_commands
from motor.motor_asyncio import AsyncIOMotorClient
from bson.objectid import ObjectId
from TikTokLive import TikTokLiveClient
from TikTokLive.events import ConnectEvent, DisconnectEvent
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# SISTEMA DE LOGS PROFESIONAL (Para Render)
# ==========================================
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("StreamBot")

# --- CONFIGURACIÓN DE DISCORD ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --- CONEXIÓN DE BASE DE DATOS ---
try:
    db_client = AsyncIOMotorClient(os.getenv('MONGO_URI'), tlsCAFile=certifi.where())
    db = db_client.bot_database
    streamers_col = db.streamers
    logger.info("✅ Conexión a MongoDB preparada.")
except Exception as e:
    logger.error(f"❌ Error crítico al conectar a MongoDB: {e}")

# ==========================================
# MANEJADOR GLOBAL DE ERRORES (Evita que Discord diga "No responde")
# ==========================================
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logger.error(f"Error en el comando /{interaction.command.name}: {error}")
    mensaje = "❌ Ocurrió un error interno. Los administradores ya han sido notificados."
    if not interaction.response.is_done():
        await interaction.response.send_message(mensaje, ephemeral=True)
    else:
        await interaction.followup.send(mensaje, ephemeral=True)

# ==========================================
# FUNCIONES DE VALIDACIÓN DE TIKTOK
# ==========================================
async def validate_tiktok_user(username: str):
    """Verifica si el usuario tiene un formato válido y si la cuenta existe en TikTok."""
    # 1. Verificar caracteres inválidos
    if not re.match(r'^[a-zA-Z0-9_.-]{2,24}$', username):
        return False, "El nombre de usuario contiene espacios o caracteres no permitidos."
    
    # 2. Verificar que el perfil exista realmente (búsqueda web)
    url = f"https://www.tiktok.com/@{username}"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 404:
                    return False, "La cuenta no existe o está baneada (Error 404)."
                # Si devuelve 200, la cuenta existe.
                return True, None
    except Exception as e:
        logger.error(f"Error al validar en TikTok web: {e}")
        return False, "No nos pudimos conectar con los servidores de TikTok para validar."

# ==========================================
# FASE 3 Y 4: SITEMA DE REVISIÓN PARA HELPERS
# ==========================================
class HelperReviewView(discord.ui.View):
    def __init__(self, reporte_id: str):
        super().__init__(timeout=None)
        self.reporte_id = reporte_id

    @discord.ui.button(label="Aprobar ✅", style=discord.ButtonStyle.success)
    async def aprobar(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await db.reportes.update_one({"_id": ObjectId(self.reporte_id)}, {"$set": {"estado": "aprobado"}})
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content=f"🟢 **Reporte Aprobado por {interaction.user.mention}**", view=self)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error base de datos: {e}", ephemeral=True)

    @discord.ui.button(label="Rechazar ❌", style=discord.ButtonStyle.danger)
    async def rechazar(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await db.reportes.update_one({"_id": ObjectId(self.reporte_id)}, {"$set": {"estado": "rechazado"}})
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content=f"🔴 **Reporte Rechazado por {interaction.user.mention}**", view=self)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error base de datos: {e}", ephemeral=True)

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
        await interaction.response.defer(ephemeral=True)
        
        try:
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
            
            await interaction.followup.send("✅ Tus estadísticas han sido enviadas a revisión por los Helpers.", ephemeral=True)

            helpers_channel = bot.get_channel(int(os.getenv('CHANNEL_HELPERS_ID')))
            if helpers_channel:
                embed = discord.Embed(title="📋 Nuevo Reporte de Stream", color=discord.Color.purple())
                embed.add_field(name="Creador", value=f"{interaction.user.mention} (@{self.tiktok_username})", inline=False)
                embed.add_field(name="Horas Transmitidas", value=self.horas.value, inline=True)
                embed.add_field(name="Audiencia Promedio", value=self.vistas.value, inline=True)
                embed.add_field(name="Donaciones", value=self.donaciones.value or "N/A", inline=True)
                embed.add_field(name="Enlace de Evidencia", value=self.link_prueba.value, inline=False)
                
                if self.link_prueba.value.startswith("http"):
                    embed.set_image(url=self.link_prueba.value)

                await helpers_channel.send(embed=embed, view=HelperReviewView(str(result.inserted_id)))
        except Exception as e:
            logger.error(f"Error al guardar modal: {e}")
            await interaction.followup.send(f"❌ No se pudo guardar el reporte.", ephemeral=True)

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
    username_clean = username.replace("@", "").strip()
    logger.info(f"Iniciando hilo de monitoreo para @{username_clean}")
    
    while True:
        try:
            streamer = await streamers_col.find_one({"username": username_clean, "active": True})
            if not streamer:
                logger.info(f"Monitoreo desactivado para @{username_clean}, cerrando hilo.")
                break
        except Exception as e:
            logger.error(f"Error DB en monitoreo para @{username_clean}: {e}")
            await asyncio.sleep(60)
            continue

        client = TikTokLiveClient(unique_id=username_clean)

        @client.on(ConnectEvent)
        async def on_connect(event: ConnectEvent):
            logger.info(f"🔴 @{username_clean} acaba de iniciar Stream!")
            channel = bot.get_channel(int(os.getenv('CHANNEL_START_ID')))
            if channel:
                await channel.send(f"🔴 **¡Anuncio de Stream!** <@{discord_user_id}> está EN VIVO en TikTok.\n🔗 https://tiktok.com/@{username_clean}/live")

        @client.on(DisconnectEvent)
        async def on_disconnect(event: DisconnectEvent):
            logger.info(f"⏹️ @{username_clean} terminó su Stream.")
            channel = bot.get_channel(int(os.getenv('CHANNEL_END_ID')))
            if channel:
                await channel.send(f"⚠️ El stream de **@{username_clean}** ha finalizado.")
            
            try:
                user = await bot.fetch_user(discord_user_id)
                await user.send(
                    f"👋 ¡Tu directo en **@{username_clean}** ha terminado! Registra tus estadísticas abajo.",
                    view=BotonDMView(username_clean)
                )
            except Exception as e:
                logger.error(f"No se pudo enviar DM a {discord_user_id}: {e}")

        try:
            await client.start()
        except Exception as e:
            # Silenciamos errores menores de conexión para no saturar los logs
            await asyncio.sleep(120)

# ==========================================
# EVENTOS Y COMANDOS PRINCIPALES
# ==========================================
@bot.event
async def on_ready():
    logger.info(f'🤖 Bot activo y logueado como {bot.user}')
    
    # Sincronización en tu servidor
    GUILD_ID = discord.Object(id=1465461057261670636) 
    
    try:
        logger.info("🔄 Sincronizando comandos Slash...")
        bot.tree.copy_global_to(guild=GUILD_ID)
        await bot.tree.sync(guild=GUILD_ID)
        logger.info("✅ ¡Comandos registrados!")
    except Exception as e:
        logger.error(f"❌ Error al sincronizar comandos: {e}")
    
    # Cargar Base de Datos
    try:
        logger.info("🔍 Recuperando streamers activos de la base de datos...")
        cursor = streamers_col.find({"active": True})
        async for streamer in cursor:
            asyncio.create_task(start_monitoring(streamer["username"], streamer["discord_user_id"]))
    except Exception as e:
        logger.error(f"🔴 MONGODB ERROR: No se cargaron los monitores. Detalles: {e}")

@bot.tree.command(name="register", description="Verifica y enlaza tu cuenta de TikTok al bot")
async def register(interaction: discord.Interaction, tiktok_username: str):
    # 1. Pone al bot a "pensar" para que Discord no lance error de tiempo
    await interaction.response.defer(ephemeral=True) 
    
    username_clean = tiktok_username.replace("@", "").strip()
    logger.info(f"Usuario {interaction.user.name} solicitó registro para @{username_clean}")
    
    # 2. VALIDACIÓN ESTRICTA
    es_valido, razon_error = await validate_tiktok_user(username_clean)
    
    if not es_valido:
        logger.warning(f"Validación fallida para @{username_clean} - Razón: {razon_error}")
        await interaction.followup.send(f"⚠️ **No pudimos registrar tu cuenta.**\n**Razón:** `{razon_error}`\nAsegúrate de escribir bien tu usuario, sin la '@'.", ephemeral=True)
        return

    # 3. SI EXISTE, GUARDAR EN LA BASE DE DATOS
    try:
        await streamers_col.update_one(
            {"username": username_clean}, 
            {"$set": {"username": username_clean, "discord_user_id": interaction.user.id, "active": True}}, 
            upsert=True
        )
        
        # 4. INICIAR MONITOREO DE INMEDIATO
        asyncio.create_task(start_monitoring(username_clean, interaction.user.id))
        
        logger.info(f"✅ Registro exitoso para @{username_clean}")
        await interaction.followup.send(f"✅ **¡Perfil Verificado y Registrado!**\nTu cuenta `@{username_clean}` ha sido enlazada a tu perfil de Discord y ya estamos monitoreando tus directos.", ephemeral=True)
        
    except Exception as e:
        logger.error(f"Error al guardar registro en BD: {e}")
        await interaction.followup.send("❌ Tu perfil de TikTok es válido, pero hubo un error en nuestra base de datos. Avisa a los administradores.", ephemeral=True)

# Hilo falso para que Render no moleste con los puertos
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
class DummyServer(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"Bot OK")
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.getenv('PORT', 8080))), DummyServer).serve_forever(), daemon=True).start()

bot.run(os.getenv('DISCORD_TOKEN'))
