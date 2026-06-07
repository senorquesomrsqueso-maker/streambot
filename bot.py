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
    stats_col = db.creadores_stats
    logger.info("✅ Conexión a MongoDB preparada.")
except Exception as e:
    logger.error(f"❌ Error crítico al conectar a MongoDB: {e}")

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
def extraer_usuario_tiktok(input_str: str) -> str:
    """Extrae el usuario puro ya sea de un link o texto con @"""
    # Si manda un link completo, busca lo que está después del @
    match = re.search(r'@([a-zA-Z0-9_.-]+)', input_str)
    if match:
        return match.group(1)
    # Si manda solo texto, le quita el @ por si acaso
    return input_str.replace("@", "").strip()

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
            reporte = await reportes_col.find_one({"_id": ObjectId(self.reporte_id)})
            if not reporte or reporte.get("estado") != "pendiente":
                await interaction.response.send_message("⚠️ Este reporte ya fue procesado o no existe.", ephemeral=True)
                return

            await reportes_col.update_one({"_id": ObjectId(self.reporte_id)}, {"$set": {"estado": "aprobado"}})
            
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
        
        try:
            horas_float = float(self.horas.value.replace(',', '.'))
            vistas_int = int(self.vistas.value)
            puntos_totales = math.floor(horas_float * 1) + (math.floor(vistas_int / 10) * 2)
        except ValueError:
            await interaction.followup.send("❌ **Error:** Escribe solo números en 'Horas' (ej: 2.5) y 'Vistas' (ej: 40).", ephemeral=True)
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
            
            await interaction.followup.send(f"✅ Estadísticas enviadas. Recibirás **{puntos_totales} Puntos** si se aprueba.", ephemeral=True)

            helpers_channel = bot.get_channel(int(os.getenv('CHANNEL_HELPERS_ID')))
            if helpers_channel:
                embed = discord.Embed(title="📋 Nuevo Reporte (Pendiente)", color=discord.Color.purple())
                embed.add_field(name="Creador", value=f"{interaction.user.mention} (@{self.tiktok_username})", inline=False)
                embed.add_field(name="Horas Transmitidas", value=f"{horas_float}h", inline=True)
                embed.add_field(name="Audiencia Promedio", value=f"{vistas_int} views", inline=True)
                embed.add_field(name="🎯 Puntos a Recibir", value=f"**{puntos_totales} Puntos**", inline=True)
                embed.add_field(name="Enlace de Evidencia", value=self.link_prueba.value, inline=False)
                if self.link_prueba.value.startswith("http"):
                    embed.set_image(url=self.link_prueba.value)

                await helpers_channel.send(embed=embed, view=HelperReviewView(str(result.inserted_id)))
        except Exception as e:
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
    
    while True:
        try:
            streamer = await streamers_col.find_one({"username": username_clean, "active": True})
            if not streamer:
                # Si el usuario fue borrado por un admin, el hilo se rompe y deja de monitorear
                logger.info(f"Monitoreo finalizado para @{username_clean}")
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
            logger.info(f"🔴 @{username_clean} inició Stream!")
            
            channel = bot.get_channel(int(os.getenv('CHANNEL_START_ID')))
            if channel:
                await channel.send(f"🔴 **¡Anuncio de Stream!** <@{discord_user_id}> está EN VIVO en TikTok.\n🔗 https://tiktok.com/@{username_clean}/live")

        @client.on(DisconnectEvent)
        async def on_disconnect(event: DisconnectEvent):
            if not active_streams.get(username_clean):
                return

            active_streams[username_clean] = False
            logger.info(f"⏹️ @{username_clean} terminó su Stream.")
            
            channel = bot.get_channel(int(os.getenv('CHANNEL_END_ID')))
            if channel:
                await channel.send(f"⚠️ El stream de **@{username_clean}** ha finalizado.")
            
            try:
                user = await bot.fetch_user(discord_user_id)
                await user.send(
                    f"👋 ¡Tu directo en **@{username_clean}** ha terminado! Registra tus puntos.",
                    view=BotonDMView(username_clean)
                )
            except Exception as e:
                pass

        try:
            await client.start()
        except Exception as e:
            await asyncio.sleep(120)

# ==========================================
# 7. TAREA AUTOMÁTICA (CICLO DE 3 DÍAS)
# ==========================================
@tasks.loop(hours=72)
async def reporte_leaderboard_ciclico():
    canal_id = os.getenv('CHANNEL_LEADERBOARD_ID')
    if not canal_id: return
    canal = bot.get_channel(int(canal_id))
    if not canal: return

    try:
        top_creadores = await stats_col.find().sort("total_puntos", -1).limit(10).to_list(length=10)
        if not top_creadores: return

        embed = discord.Embed(title="📅 REPORTE DE ACTIVIDAD (Últimos 3 Días)", color=discord.Color.green())
        texto_lista = ""
        for index, creador in enumerate(top_creadores, 1):
            texto_lista += f"**{index}. @{creador['tiktok']}** ➔ {creador.get('total_puntos', 0)} Puntos\n"
            
        embed.add_field(name="Ranking General", value=texto_lista)
        await canal.send(embed=embed)
    except Exception as e:
        logger.error(f"Error reporte 3 días: {e}")

@reporte_leaderboard_ciclico.before_loop
async def before_reporte():
    await bot.wait_until_ready()

# ==========================================
# 8. EVENTO DE INICIO DEL BOT
# ==========================================
@bot.event
async def on_ready():
    logger.info(f'🤖 Bot activo como {bot.user}')
    if not reporte_leaderboard_ciclico.is_running():
        reporte_leaderboard_ciclico.start()

    GUILD_ID = discord.Object(id=1465461057261670636) 
    try:
        bot.tree.copy_global_to(guild=GUILD_ID)
        await bot.tree.sync(guild=GUILD_ID)
        logger.info("✅ Comandos registrados!")
    except Exception as e:
        logger.error(f"❌ Error al sincronizar: {e}")
    
    try:
        cursor = streamers_col.find({"active": True})
        async for streamer in cursor:
            asyncio.create_task(start_monitoring(streamer["username"], streamer["discord_user_id"]))
    except Exception as e:
        pass

# ==========================================
# 9. COMANDOS DEL BOT (DIVIDIDOS Y LIMPIOS)
# ==========================================

# --- COMANDO PARA REGISTRAR CREADOR ---
@bot.tree.command(name="register", description="Enlaza tu cuenta de TikTok al bot (Usa tu usuario o Link)")
async def register(interaction: discord.Interaction, tiktok_input: str):
    await interaction.response.defer(ephemeral=True) 
    
    # Aquí funciona la nueva magia para limpiar el link o el arroba
    username_clean = extraer_usuario_tiktok(tiktok_input)
    
    es_valido, razon_error = await validate_tiktok_user(username_clean)
    if not es_valido:
        await interaction.followup.send(f"⚠️ **Error al registrar:** `{razon_error}`\nAsegúrate de que la cuenta existe.", ephemeral=True)
        return

    try:
        await streamers_col.update_one(
            {"username": username_clean}, 
            {"$set": {"username": username_clean, "discord_user_id": interaction.user.id, "active": True}}, 
            upsert=True
        )
        asyncio.create_task(start_monitoring(username_clean, interaction.user.id))
        await interaction.followup.send(f"✅ **¡Registrado!**\nYa estamos monitoreando a `@{username_clean}`.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send("❌ Hubo un error en nuestra base de datos.", ephemeral=True)

# --- COMANDOS PARA LEADERBOARDS (STAFF) ---
@bot.tree.command(name="leaderboard_general", description="[STAFF] Muestra el Top 10 de creadores con más puntos")
@app_commands.default_permissions(manage_messages=True)
async def leaderboard_general(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        top_creadores = await stats_col.find().sort("total_puntos", -1).limit(10).to_list(length=10)
        if not top_creadores:
            await interaction.followup.send("📊 Todavía no hay creadores registrados en la Leaderboard.")
            return

        embed = discord.Embed(title="🏆 TOP 10 CREADORES 🏆", color=discord.Color.gold())
        texto_lista = ""
        for index, c in enumerate(top_creadores, 1):
            medalla = "🥇" if index == 1 else "🥈" if index == 2 else "🥉" if index == 3 else f"{index}."
            texto_lista += f"{medalla} **@{c['tiktok']}** - {c.get('total_puntos', 0)} pts\n"
            
        embed.description = texto_lista
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="leaderboard_individual", description="[STAFF] Revisa el total de un solo creador")
@app_commands.default_permissions(manage_messages=True)
async def leaderboard_individual(interaction: discord.Interaction, tiktok_input: str):
    await interaction.response.defer()
    username_clean = extraer_usuario_tiktok(tiktok_input)
    
    try:
        creador = await stats_col.find_one({"tiktok": username_clean})
        if not creador:
            await interaction.followup.send(f"❌ `@{username_clean}` no tiene estadísticas aún.")
            return

        discord_user = f"<@{creador.get('discord_user_id')}>" if creador.get('discord_user_id') else "No enlazado"
        embed = discord.Embed(title=f"📊 Stats de @{username_clean}", color=discord.Color.blue())
        embed.add_field(name="Usuario Discord", value=discord_user, inline=False)
        embed.add_field(name="Puntos Totales", value=f"🏆 **{creador.get('total_puntos', 0)} pts**", inline=True)
        embed.add_field(name="Directos Totales", value=f"📹 {creador.get('total_streams', 0)}", inline=True)
        
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

# --- COMANDOS ADMINISTRATIVOS Y DE GESTIÓN (STAFF) ---
@bot.tree.command(name="lista_creadores", description="[STAFF] Muestra los creadores monitoreados actualmente")
@app_commands.default_permissions(manage_messages=True)
async def lista_creadores(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        creadores = await streamers_col.find({"active": True}).to_list(length=100)
        if not creadores:
            await interaction.followup.send("⚠️ No hay creadores activos en el bot en este momento.")
            return

        texto_lista = ""
        for c in creadores:
            texto_lista += f"📱 **@{c['username']}** (Discord: <@{c['discord_user_id']}>)\n"
            
        embed = discord.Embed(title="📋 Creadores Monitoreados", description=texto_lista, color=discord.Color.green())
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Error BD: {e}")

@bot.tree.command(name="eliminar_creador", description="[STAFF] Borra la cuenta de un creador del bot (deja de avisar)")
@app_commands.default_permissions(manage_messages=True)
async def eliminar_creador(interaction: discord.Interaction, tiktok_input: str):
    await interaction.response.defer()
    username_clean = extraer_usuario_tiktok(tiktok_input)
    
    try:
        # Se elimina de la base de datos (El hilo de monitoreo se apagará solo)
        resultado = await streamers_col.delete_one({"username": username_clean})
        
        # También lo sacamos del candado manual si estaba encendido
        if username_clean in active_streams:
            del active_streams[username_clean]
            
        if resultado.deleted_count > 0:
            await interaction.followup.send(f"🗑️ El creador `@{username_clean}` ha sido **eliminado** del monitoreo exitosamente.")
        else:
            await interaction.followup.send(f"⚠️ El creador `@{username_clean}` no estaba registrado.")
    except Exception as e:
        await interaction.followup.send(f"❌ Error al eliminar: {e}")

@bot.tree.command(name="check", description="[STAFF] Revisa si el bot detecta a alguien en vivo ahora mismo")
@app_commands.default_permissions(manage_messages=True)
async def check(interaction: discord.Interaction, tiktok_input: str):
    username_clean = extraer_usuario_tiktok(tiktok_input)
    
    # Consulta rápida al candado (diccionario)
    estado_live = active_streams.get(username_clean, False)
    
    if estado_live:
        await interaction.response.send_message(f"🟢 El bot confirma que **@{username_clean}** está **EN VIVO** en este momento.")
    else:
        await interaction.response.send_message(f"🔴 Según los sensores del bot, **@{username_clean}** está **APAGADO** o no ha sido detectado aún.")

# Servidor Dummy para Render
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
class DummyServer(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"Bot OK")
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.getenv('PORT', 8080))), DummyServer).serve_forever(), daemon=True).start()

bot.run(os.getenv('DISCORD_TOKEN'))
