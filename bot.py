import os
import asyncio
import re
import aiohttp
import discord
import logging
import certifi
import math
from discord.ext import commands, tasks
from discord import app_commands
from motor.motor_asyncio import AsyncIOMotorClient
from bson.objectid import ObjectId
from TikTokLive import TikTokLiveClient
from TikTokLive.events import ConnectEvent, DisconnectEvent
from dotenv import load_dotenv

load_dotenv()
active_streams = {}

# ==========================================
# 1. SISTEMA DE LOGS PROFESIONAL
# ==========================================
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("StreamBot")

# ==========================================
# 2. CONFIGURACIÓN DE DISCORD Y BASE DE DATOS
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

try:
    db_client = AsyncIOMotorClient(os.getenv('MONGO_URI'), tlsCAFile=certifi.where())
    db = db_client.bot_database
    streamers_col = db.streamers
    reportes_col = db.reportes
    stats_col = db.creadores_stats # NUEVA COLECCIÓN PARA PUNTAJES
    logger.info("✅ Conexión a MongoDB preparada.")
except Exception as e:
    logger.error(f"❌ Error crítico al conectar a MongoDB: {e}")

# Manejador global de errores
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logger.error(f"Error en el comando /{interaction.command.name}: {error}")
    mensaje = "❌ Ocurrió un error interno. Los administradores ya han sido notificados."
    if not interaction.response.is_done():
        await interaction.response.send_message(mensaje, ephemeral=True)
    else:
        await interaction.followup.send(mensaje, ephemeral=True)

# ==========================================
# 3. FUNCIONES DE UTILIDAD (TikTok)
# ==========================================
async def validate_tiktok_user(username: str):
    if not re.match(r'^[a-zA-Z0-9_.-]{2,24}$', username):
        return False, "El nombre de usuario contiene espacios o caracteres no permitidos."
    
    url = f"https://www.tiktok.com/@{username}"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 404:
                    return False, "La cuenta no existe o está baneada (Error 404)."
                return True, None
    except Exception as e:
        logger.error(f"Error al validar en TikTok web: {e}")
        return False, "No nos pudimos conectar con los servidores de TikTok para validar."

# ==========================================
# 4. SISTEMA DE REVISIÓN Y APROBACIÓN (STAFF)
# ==========================================
class HelperReviewView(discord.ui.View):
    def __init__(self, reporte_id: str):
        super().__init__(timeout=None)
        self.reporte_id = reporte_id

    @discord.ui.button(label="Aprobar y Sumar Puntos ✅", style=discord.ButtonStyle.success)
    async def aprobar(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            # 1. Buscar el reporte original
            reporte = await reportes_col.find_one({"_id": ObjectId(self.reporte_id)})
            if not reporte or reporte.get("estado") != "pendiente":
                await interaction.response.send_message("⚠️ Este reporte ya fue procesado o no existe.", ephemeral=True)
                return

            # 2. Actualizar estado del reporte
            await reportes_col.update_one({"_id": ObjectId(self.reporte_id)}, {"$set": {"estado": "aprobado"}})
            
            # 3. Sumar los puntos al creador en la Leaderboard
            puntos = reporte.get("puntos_calculados", 0)
            tiktok_user = reporte.get("tiktok")
            
            await stats_col.update_one(
                {"tiktok": tiktok_user},
                {
                    "$inc": {"total_puntos": puntos, "total_streams": 1, "total_horas": float(reporte.get("horas", 0))},
                    "$set": {"discord_user_id": reporte.get("id_discord")}
                },
                upsert=True
            )

            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content=f"🟢 **Reporte Aprobado por {interaction.user.mention}** (+{puntos} pts para @{tiktok_user})", view=self)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error base de datos: {e}", ephemeral=True)

    @discord.ui.button(label="Rechazar ❌", style=discord.ButtonStyle.danger)
    async def rechazar(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await reportes_col.update_one({"_id": ObjectId(self.reporte_id)}, {"$set": {"estado": "rechazado"}})
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content=f"🔴 **Reporte Rechazado por {interaction.user.mention}** (No se sumaron puntos)", view=self)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error base de datos: {e}", ephemeral=True)

# ==========================================
# 5. FORMULARIO DE REPORTE DE CREADORES
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
        
        # VALIDACIÓN MATEMÁTICA Y CÁLCULO DE PUNTOS
        try:
            horas_float = float(self.horas.value.replace(',', '.'))
            vistas_int = int(self.vistas.value)
            
            # Fórmula: (Horas * 1) + (Floor(Viewers / 10) * 2)
            puntos_totales = math.floor(horas_float * 1) + (math.floor(vistas_int / 10) * 2)
        except ValueError:
            await interaction.followup.send("❌ **Error:** Asegúrate de escribir solo números en 'Horas' (ej: 2.5) y 'Vistas' (ej: 40). No uses letras.", ephemeral=True)
            return

        try:
            result = await reportes_col.insert_one({
                "usuario_discord": interaction.user.name,
                "id_discord": interaction.user.id,
                "tiktok": self.tiktok_username,
                "horas": horas_float,
                "vistas": vistas_int,
                "puntos_calculados": puntos_totales,
                "donaciones": self.donaciones.value,
                "prueba": self.link_prueba.value,
                "estado": "pendiente"
            })
            
            await interaction.followup.send(f"✅ Tus estadísticas han sido enviadas. Calculamos un total de **{puntos_totales} Puntos**. Los Helpers están revisando tu captura.", ephemeral=True)

            helpers_channel = bot.get_channel(int(os.getenv('CHANNEL_HELPERS_ID')))
            if helpers_channel:
                embed = discord.Embed(title="📋 Nuevo Reporte de Stream (Pendiente)", color=discord.Color.purple())
                embed.add_field(name="Creador", value=f"{interaction.user.mention} (@{self.tiktok_username})", inline=False)
                embed.add_field(name="Horas Transmitidas", value=f"{horas_float}h", inline=True)
                embed.add_field(name="Audiencia Promedio", value=f"{vistas_int} views", inline=True)
                embed.add_field(name="🎯 Puntos a Recibir", value=f"**{puntos_totales} Puntos**", inline=True)
                embed.add_field(name="Donaciones", value=self.donaciones.value or "N/A", inline=False)
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
# 6. MONITOREO DE STREAMS EN VIVO
# ==========================================
async def start_monitoring(username, discord_user_id):
    username_clean = username.replace("@", "").strip()
    logger.info(f"Iniciando hilo de monitoreo para @{username_clean}")
    
    while True:
        try:
            streamer = await streamers_col.find_one({"username": username_clean, "active": True})
            if not streamer:
                logger.info(f"Monitoreo desactivado para @{username_clean}")
                break
        except Exception as e:
            await asyncio.sleep(60)
            continue

        client = TikTokLiveClient(unique_id=username_clean)

        @client.on(ConnectEvent)
        async def on_connect(event: ConnectEvent):
            if active_streams.get(username_clean):
                return

            active_streams[username_clean] = True
            logger.info(f"🔴 @{username_clean} acaba de iniciar Stream!")
            
            channel = bot.get_channel(int(os.getenv('CHANNEL_START_ID')))
            if channel:
                await channel.send(f"🔴 **¡Anuncio de Stream!** <@{discord_user_id}> está EN VIVO en TikTok.\n🔗 https://tiktok.com/@{username_clean}/live")

        @client.on(DisconnectEvent)
        async def on_disconnect(event: DisconnectEvent):
            if not active_streams.get(username_clean):
                return

            active_streams[username_clean] = False
            logger.info(f"⏹️ @{username_clean} terminó su Stream oficialmente.")
            
            channel = bot.get_channel(int(os.getenv('CHANNEL_END_ID')))
            if channel:
                await channel.send(f"⚠️ El stream de **@{username_clean}** ha finalizado.")
            
            try:
                user = await bot.fetch_user(discord_user_id)
                await user.send(
                    f"👋 ¡Tu directo en **@{username_clean}** ha terminado! Presiona el botón para registrar tus puntos.",
                    view=BotonDMView(username_clean)
                )
            except Exception as e:
                logger.error(f"No se pudo enviar DM a {discord_user_id}: {e}")

        try:
            await client.start()
        except Exception as e:
            await asyncio.sleep(120)

# ==========================================
# 7. COMANDOS DE LEADERBOARD (NUEVO)
# ==========================================
@bot.tree.command(name="leaderboard_general", description="[STAFF] Muestra el Top 10 de creadores con más puntos")
@app_commands.default_permissions(manage_messages=True) # Solo staff
async def leaderboard_general(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        # Traer los top 10 ordenados por puntos descendentes
        top_creadores = await stats_col.find().sort("total_puntos", -1).limit(10).to_list(length=10)
        
        if not top_creadores:
            await interaction.followup.send("📊 Todavía no hay creadores registrados en la Leaderboard.")
            return

        embed = discord.Embed(title="🏆 TOP 10 CREADORES - LEADERBOARD 🏆", color=discord.Color.gold())
        
        texto_lista = ""
        for index, creador in enumerate(top_creadores, 1):
            medalla = "🥇" if index == 1 else "🥈" if index == 2 else "🥉" if index == 3 else f"{index}."
            texto_lista += f"{medalla} **@{creador['tiktok']}** - {creador.get('total_puntos', 0)} Puntos ({creador.get('total_streams', 0)} streams)\n"
            
        embed.description = texto_lista
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Error al cargar la leaderboard: {e}")

@bot.tree.command(name="leaderboard_individual", description="[STAFF] Revisa las estadísticas totales de un creador en específico")
@app_commands.default_permissions(manage_messages=True) # Solo staff
async def leaderboard_individual(interaction: discord.Interaction, tiktok_username: str):
    await interaction.response.defer()
    username_clean = tiktok_username.replace("@", "").strip()
    
    try:
        creador = await stats_col.find_one({"tiktok": username_clean})
        if not creador:
            await interaction.followup.send(f"❌ El creador `@{username_clean}` no tiene estadísticas registradas aún.")
            return

        discord_user = f"<@{creador.get('discord_user_id')}>" if creador.get('discord_user_id') else "No enlazado"

        embed = discord.Embed(title=f"📊 Estadísticas de @{username_clean}", color=discord.Color.blue())
        embed.add_field(name="Usuario Discord", value=discord_user, inline=False)
        embed.add_field(name="Puntos Totales", value=f"🏆 **{creador.get('total_puntos', 0)} pts**", inline=True)
        embed.add_field(name="Directos Totales", value=f"📹 {creador.get('total_streams', 0)}", inline=True)
        embed.add_field(name="Horas Transmitidas", value=f"⏱️ {creador.get('total_horas', 0)}h", inline=True)
        
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Error base de datos: {e}")

# ==========================================
# 8. TAREA AUTOMÁTICA (CICLO DE 3 DÍAS)
# ==========================================
@tasks.loop(hours=72)
async def reporte_leaderboard_ciclico():
    """Envía el top de creadores cada 3 días al canal especificado"""
    canal_id = os.getenv('CHANNEL_LEADERBOARD_ID')
    if not canal_id:
        logger.warning("No hay CHANNEL_LEADERBOARD_ID configurado para el reporte de 3 días.")
        return

    canal = bot.get_channel(int(canal_id))
    if not canal:
        return

    try:
        top_creadores = await stats_col.find().sort("total_puntos", -1).limit(10).to_list(length=10)
        if not top_creadores: return

        embed = discord.Embed(title="📅 REPORTE DE ACTIVIDAD (Últimos 3 Días)", description="Aquí está la actualización automática del Ranking de Creadores:", color=discord.Color.green())
        
        texto_lista = ""
        for index, creador in enumerate(top_creadores, 1):
            texto_lista += f"**{index}. @{creador['tiktok']}** ➔ {creador.get('total_puntos', 0)} Puntos\n"
            
        embed.add_field(name="Ranking General", value=texto_lista)
        await canal.send(embed=embed)
    except Exception as e:
        logger.error(f"Error enviando reporte de 3 días: {e}")

@reporte_leaderboard_ciclico.before_loop
async def before_reporte():
    await bot.wait_until_ready()

# ==========================================
# 9. INICIO DEL BOT Y REGISTRO
# ==========================================
@bot.event
async def on_ready():
    logger.info(f'🤖 Bot activo y logueado como {bot.user}')
    
    # Iniciar la tarea cíclica de 3 días
    if not reporte_leaderboard_ciclico.is_running():
        reporte_leaderboard_ciclico.start()

    GUILD_ID = discord.Object(id=1465461057261670636) 
    
    try:
        logger.info("🔄 Sincronizando comandos Slash...")
        bot.tree.copy_global_to(guild=GUILD_ID)
        await bot.tree.sync(guild=GUILD_ID)
        logger.info("✅ ¡Comandos registrados!")
    except Exception as e:
        logger.error(f"❌ Error al sincronizar comandos: {e}")
    
    try:
        logger.info("🔍 Recuperando streamers activos de la base de datos...")
        cursor = streamers_col.find({"active": True})
        async for streamer in cursor:
            asyncio.create_task(start_monitoring(streamer["username"], streamer["discord_user_id"]))
    except Exception as e:
        logger.error(f"🔴 MONGODB ERROR: No se cargaron los monitores.")

@bot.tree.command(name="register", description="Verifica y enlaza tu cuenta de TikTok al bot")
async def register(interaction: discord.Interaction, tiktok_username: str):
    await interaction.response.defer(ephemeral=True) 
    username_clean = tiktok_username.replace("@", "").strip()
    es_valido, razon_error = await validate_tiktok_user(username_clean)
    
    if not es_valido:
        await interaction.followup.send(f"⚠️ **No pudimos registrar tu cuenta.**\n**Razón:** `{razon_error}`", ephemeral=True)
        return

    try:
        await streamers_col.update_one(
            {"username": username_clean}, 
            {"$set": {"username": username_clean, "discord_user_id": interaction.user.id, "active": True}}, 
            upsert=True
        )
        asyncio.create_task(start_monitoring(username_clean, interaction.user.id))
        await interaction.followup.send(f"✅ **¡Perfil Verificado y Registrado!**\nYa estamos monitoreando tus directos.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send("❌ Hubo un error en nuestra base de datos.", ephemeral=True)

# Servidor Dummy para Render
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
class DummyServer(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"Bot OK")
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.getenv('PORT', 8080))), DummyServer).serve_forever(), daemon=True).start()

bot.run(os.getenv('DISCORD_TOKEN'))
