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
# 3. FUNCIONES DE UTILIDAD
# ==========================================
def extraer_usuario_tiktok(input_str: str) -> str:
    match = re.search(r'@([a-zA-Z0-9_.-]+)', input_str)
    if match:
        return match.group(1)
    return input_str.replace("@", "").strip()

async def validate_tiktok_user(username: str):
    if not re.match(r'^[a-zA-Z0-9_.-]{2,24}$', username):
        return False, "El nombre de usuario contiene caracteres no permitidos."
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
        return False, "No nos pudimos conectar con los servidores de TikTok."

# ¡NUEVA FUNCIÓN! Asegura que el bot encuentre el canal, incluso desde DMs
async def obtener_canal_seguro(bot, channel_id):
    if not channel_id:
        return None
    channel_id = int(channel_id)
    canal = bot.get_channel(channel_id)
    if not canal:
        try:
            canal = await bot.fetch_channel(channel_id)
        except Exception as e:
            logger.error(f"No se pudo obtener el canal {channel_id}: {e}")
            return None
    return canal

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

            # Cambiar estado a aprobado
            await reportes_col.update_one({"_id": ObjectId(self.reporte_id)}, {"$set": {"estado": "aprobado"}})
            
            puntos = reporte.get("puntos_calculados", 0)
            tiktok_user = reporte.get("tiktok")
            
            # Sumar a la Leaderboard
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
            await interaction.response.edit_message(content=f"🟢 **Reporte Aprobado por {interaction.user.mention}** (+{puntos} pts para @{tiktok_user} sumados a la Leaderboard)", view=self)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error base de datos: {e}", ephemeral=True)

    @discord.ui.button(label="Denegar ❌", style=discord.ButtonStyle.danger)
    async def rechazar(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await reportes_col.update_one({"_id": ObjectId(self.reporte_id)}, {"$set": {"estado": "rechazado"}})
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content=f"🔴 **Reporte Denegado por {interaction.user.mention}** (No se sumaron puntos)", view=self)
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
    link_prueba = discord.ui.TextInput(label='Enlace de Captura (Drive/Imgur/Discord)', style=discord.TextStyle.paragraph, placeholder='Pega el link de Google Drive, Imgur, etc. aquí')

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            horas_float = float(self.horas.value.replace(',', '.'))
            vistas_int = int(self.vistas.value)
            # FÓRMULA DE PUNTOS
            puntos_totales = math.floor(horas_float * 1) + (math.floor(vistas_int / 10) * 2)
        except ValueError:
            await interaction.followup.send("❌ **Error:** Escribe solo números en 'Horas' (ej: 2.5) y 'Vistas' (ej: 40).", ephemeral=True)
            return

        try:
            # 1. Guardamos en Base de Datos
            result = await reportes_col.insert_one({
                "usuario_discord": interaction.user.name,
                "id_discord": interaction.user.id,
                "tiktok": self.tiktok_username,
                "horas": horas_float,
                "vistas": vistas_int,
                "puntos_calculados": puntos_totales,
                "prueba": self.link_prueba.value,
                "estado": "pendiente"
            })
            
            # 2. Mensaje de confirmación al usuario (en su DM)
            await interaction.followup.send(
                f"✅ **¡Datos enviados con éxito!**\nTu reporte de `@{self.tiktok_username}` ya está siendo evaluado por nuestro equipo de Staff. Te avisaremos pronto.", 
                ephemeral=True
            )

            # 3. Enviar al canal de Staff (con búsqueda segura)
            helpers_channel = await obtener_canal_seguro(bot, os.getenv('CHANNEL_HELPERS_ID'))
            if helpers_channel:
                embed = discord.Embed(title="📋 Nuevo Reporte a Evaluar", color=discord.Color.purple())
                embed.add_field(name="Creador", value=f"{interaction.user.mention} (@{self.tiktok_username})", inline=False)
                embed.add_field(name="Horas Transmitidas", value=f"{horas_float}h", inline=True)
                embed.add_field(name="Audiencia Promedio", value=f"{vistas_int} views", inline=True)
                embed.add_field(name="🎯 Puntos Calculados", value=f"**{puntos_totales} Puntos**", inline=False)
                embed.add_field(name="Enlace de Evidencia", value=f"[Haz clic aquí para ver la prueba]({self.link_prueba.value})\n`{self.link_prueba.value}`", inline=False)
                
                # Si el link es directo a imagen, lo muestra
                if self.link_prueba.value.endswith(('.png', '.jpg', '.jpeg', '.webp')):
                    embed.set_image(url=self.link_prueba.value)

                await helpers_channel.send(embed=embed, view=HelperReviewView(str(result.inserted_id)))
            else:
                logger.error("No se encontró el canal de HELPERS para enviar el reporte.")
        except Exception as e:
            await interaction.followup.send(f"❌ Ocurrió un error al procesar tu reporte. Contacta al Staff.", ephemeral=True)
            logger.error(f"Error procesando reporte: {e}")

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
                break
        except Exception as e:
            await asyncio.sleep(60)
            continue

        client = TikTokLiveClient(unique_id=username_clean)

        @client.on(ConnectEvent)
        async def on_connect(event: ConnectEvent):
            if active_streams.get(username_clean): return
            active_streams[username_clean] = True
            logger.info(f"🔴 @{username_clean} inició Stream!")
            
            channel = await obtener_canal_seguro(bot, os.getenv('CHANNEL_START_ID'))
            if channel:
                await channel.send(f"🔴 **¡Anuncio de Stream!** <@{discord_user_id}> está EN VIVO en TikTok.\n🔗 https://tiktok.com/@{username_clean}/live")

        @client.on(DisconnectEvent)
        async def on_disconnect(event: DisconnectEvent):
            if not active_streams.get(username_clean): return
            active_streams[username_clean] = False
            logger.info(f"⏹️ @{username_clean} terminó su Stream.")
            
            channel = await obtener_canal_seguro(bot, os.getenv('CHANNEL_END_ID'))
            if channel:
                await channel.send(f"⚠️ El stream de **@{username_clean}** ha finalizado.")
            
            try:
                user = await bot.fetch_user(discord_user_id)
                await user.send(
                    f"👋 ¡Tu directo en **@{username_clean}** ha terminado!\nPor favor, ingresa los datos para sumar tus puntos.",
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
    canal = await obtener_canal_seguro(bot, os.getenv('CHANNEL_LEADERBOARD_ID'))
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

    # IMPORTANTE: Reemplaza este ID por el ID real de tu servidor (BloodStrike LATAM o el de pruebas)
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
# 9. COMANDOS DEL BOT
# ==========================================

@bot.tree.command(name="register", description="Enlaza tu cuenta de TikTok al bot (Usa tu usuario o Link)")
async def register(interaction: discord.Interaction, tiktok_input: str):
    await interaction.response.defer(ephemeral=True) 
    
    es_staff = interaction.permissions.manage_messages
    
    if not es_staff:
        usuario_existente = await streamers_col.find_one({"discord_user_id": interaction.user.id})
        if usuario_existente:
            await interaction.followup.send(f"⚠️ **Ya estás registrado.** Solo puedes vincular una cuenta (`@{usuario_existente['username']}`).", ephemeral=True)
            return

    username_clean = extraer_usuario_tiktok(tiktok_input)
    es_valido, razon_error = await validate_tiktok_user(username_clean)
    
    if not es_valido:
        await interaction.followup.send(f"⚠️ **Error:** `{razon_error}`", ephemeral=True)
        return

    try:
        await streamers_col.update_one(
            {"username": username_clean}, 
            {"$set": {"username": username_clean, "discord_user_id": interaction.user.id, "active": True}}, 
            upsert=True
        )
        asyncio.create_task(start_monitoring(username_clean, interaction.user.id))
        await interaction.followup.send(f"✅ **¡Registrado!**\nYa estamos monitoreando a `@{username_clean}`.", ephemeral=True)

        # Enviar log usando obtener_canal_seguro
        log_channel = await obtener_canal_seguro(bot, os.getenv('CHANNEL_LOG_REGISTER_ID'))
        if log_channel:
            embed = discord.Embed(title="🆕 Nuevo Creador Registrado", color=discord.Color.green())
            embed.add_field(name="Usuario de Discord", value=interaction.user.mention, inline=False)
            embed.add_field(name="Cuenta de TikTok", value=f"🔗 [@{username_clean}](https://www.tiktok.com/@{username_clean})", inline=False)
            if es_staff:
                embed.set_footer(text="⚙️ Registrado manualmente por Staff")
            await log_channel.send(embed=embed)

    except Exception as e:
        logger.error(f"Error al registrar: {e}")
        await interaction.followup.send("❌ Hubo un error al registrarte.", ephemeral=True)

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

@bot.tree.command(name="lista_creadores", description="[STAFF] Muestra los creadores monitoreados actualmente")
@app_commands.default_permissions(manage_messages=True)
async def lista_creadores(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        creadores = await streamers_col.find({"active": True}).to_list(length=100)
        if not creadores:
            await interaction.followup.send("⚠️ No hay creadores activos.")
            return

        texto_lista = ""
        for c in creadores:
            texto_lista += f"📱 **@{c['username']}** (<@{c['discord_user_id']}>)\n"
            
        embed = discord.Embed(title="📋 Creadores Monitoreados", description=texto_lista, color=discord.Color.green())
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Error BD: {e}")

@bot.tree.command(name="eliminar_creador", description="[STAFF] Borra la cuenta de un creador")
@app_commands.default_permissions(manage_messages=True)
async def eliminar_creador(interaction: discord.Interaction, tiktok_input: str):
    await interaction.response.defer()
    username_clean = extraer_usuario_tiktok(tiktok_input)
    try:
        resultado = await streamers_col.delete_one({"username": username_clean})
        if username_clean in active_streams: del active_streams[username_clean]
        if resultado.deleted_count > 0:
            await interaction.followup.send(f"🗑️ El creador `@{username_clean}` ha sido eliminado.")
        else:
            await interaction.followup.send(f"⚠️ El creador `@{username_clean}` no estaba registrado.")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="check", description="[STAFF] Revisa si alguien está en vivo ahora mismo")
@app_commands.default_permissions(manage_messages=True)
async def check(interaction: discord.Interaction, tiktok_input: str):
    username_clean = extraer_usuario_tiktok(tiktok_input)
    estado_live = active_streams.get(username_clean, False)
    if estado_live:
        await interaction.response.send_message(f"🟢 **@{username_clean}** está **EN VIVO**.")
    else:
        await interaction.response.send_message(f"🔴 **@{username_clean}** está **APAGADO**.")

# Servidor Dummy para Render
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
class DummyServer(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"Bot OK")
threading.Thread(target=lambda: HTTPServer(('0.0.0.0', int(os.getenv('PORT', 8080))), DummyServer).serve_forever(), daemon=True).start()

bot.run(os.getenv('DISCORD_TOKEN'))
