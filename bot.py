import os
import discord
from discord.ext import commands
from discord import app_commands
from motor.motor_asyncio import AsyncIOMotorClient
from TikTokLive import TikTokLiveClient
from TikTokLive.types.events import LiveEndEvent, LiveStartEvent
from dotenv import load_dotenv

load_dotenv()

# --- CONFIGURACIÓN ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

db_client = AsyncIOMotorClient(os.getenv('mongodb+srv://senorquesomrsqueso_db_user:mrsqueso123@mrqueso.ffmnkfy.mongodb.net/?appName=mrqueso'))
db = db_client.bot_database
streamers_col = db.streamers

# --- FORMULARIO PROFESIONAL (MODAL) ---
class ReporteStatsModal(discord.ui.Modal, title='Reporte de Stream'):
    horas = discord.ui.TextInput(label='Horas de Stream', placeholder='Ej: 3.5')
    vistas = discord.ui.TextInput(label='Promedio de Vistas', placeholder='Ej: 150')
    link_prueba = discord.ui.TextInput(label='Link de Captura (Imgur/Discord)', style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        # Aquí guardamos los datos en MongoDB
        await db.reportes.insert_one({
            "usuario": interaction.user.name,
            "horas": self.horas.value,
            "vistas": self.vistas.value,
            "prueba": self.link_prueba.value
        })
        await interaction.response.send_message("✅ ¡Stats recibidas y guardadas!", ephemeral=True)

# --- LÓGICA DE TIKTOK ---
async def start_monitoring(username):
    client = TikTokLiveClient(unique_id=username)

    @client.on(LiveStartEvent)
    async def on_live_start(event: LiveStartEvent):
        channel = bot.get_channel(int(os.getenv('CHANNEL_START_ID')))
        await channel.send(f"🔴 **¡{username} está en vivo!** \n🔗 https://tiktok.com/@{username}/live")

    @client.on(LiveEndEvent)
    async def on_live_end(event: LiveEndEvent):
        channel = bot.get_channel(int(os.getenv('CHANNEL_END_ID')))
        await channel.send(f"⚠️ El stream de **{username}** terminó. \n📩 Revisa tus mensajes directos para enviar tus stats.")
        
        # Enviar DM al creador (Asumiendo que tenemos su ID de Discord en la DB)
        # Aquí iría lógica para buscar al usuario y enviarle el Modal
        
    client.run()

# --- COMANDOS ---
@bot.command()
async def add(ctx, username: str):
    await streamers_col.update_one({"user": username}, {"$set": {"active": True}}, upsert=True)
    await ctx.send(f"✅ Monitoreando a @{username}")

bot.run(os.getenv('MTUwOTY5Nzc3NzQzNTY3MjY3Ng.GF28tF.FypyUnCsndI8VbR_jz4zk1ODHWDQuutyPLXDn4'))
