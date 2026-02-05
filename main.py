import discord
# import keep_alive # ローカル環境などファイルがない場合のためにtry-exceptで囲みます
import matplotlib
matplotlib.use('Agg') # サーバー上でグラフを描くための設定
import matplotlib.pyplot as plt
import io
import pandas as pd
from discord.ext import commands, tasks
from discord import app_commands, ui
import aiosqlite
import datetime
import random
import uuid
import asyncio
import logging
import traceback
import math
import contextlib
import os
from typing import Optional, List, Dict
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler

# keep_aliveの安全なインポート
try:
    import keep_alive
except ImportError:
    keep_alive = None

GEKIATSU = "<:b_069:1438962326463054008>"

# --- 環境変数とロギング ---
load_dotenv() 

# トークン取得
raw_token = os.getenv("DISCORD_TOKEN")
if raw_token:
    TOKEN = str(raw_token).strip().replace('"', '').replace("'", "")
else:
    TOKEN = None

# ロギング設定
LOG_FORMAT = '%(asctime)s:%(levelname)s:%(name)s: %(message)s'
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

if not TOKEN:
    logging.error("DISCORD_TOKEN is missing. Please check your Environment Variables or .env file.")
else:
    logging.info("DISCORD_TOKEN loaded successfully.")

# ログファイルの設定
file_handler = RotatingFileHandler(
    'lumen_bank.log',
    maxBytes=5*1024*1024,
    backupCount=3,
    encoding='utf-8'
)
file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
logger = logging.getLogger('LumenBank')
logger.addHandler(file_handler)


# --- 設定管理・権限チェックシステム ---

class ConfigManager:
    def __init__(self, bot):
        self.bot = bot
        self.vc_reward_per_min: int = 10
        self.role_wages: Dict[int, int] = {}       
        self.admin_roles: Dict[int, str] = {}      

    async def reload(self):
        async with self.bot.get_db() as db:
            async with db.execute("SELECT value FROM server_config WHERE key = 'vc_reward'") as cursor:
                row = await cursor.fetchone()
                if row: self.vc_reward_per_min = int(row['value'])
            
            async with db.execute("SELECT role_id, amount FROM role_wages") as cursor:
                rows = await cursor.fetchall()
                self.role_wages = {r['role_id']: r['amount'] for r in rows}

            async with db.execute("SELECT role_id, perm_level FROM admin_roles") as cursor:
                rows = await cursor.fetchall()
                self.admin_roles = {r['role_id']: r['perm_level'] for r in rows}
        logger.info("Configuration and Permissions reloaded.")

def has_permission(required_level: str):
    async def predicate(interaction: discord.Interaction) -> bool:
        if await interaction.client.is_owner(interaction.user):
            return True
        
        user_role_ids = [role.id for role in interaction.user.roles]
        admin_roles = interaction.client.config.admin_roles
        
        # 権限レベルの強さ定義
        levels = ["SUPREME_GOD", "GODDESS", "ADMIN"]
        try:
            req_index = levels.index(required_level)
        except ValueError:
            req_index = len(levels) # 未知のレベル

        for r_id in user_role_ids:
            if r_id in admin_roles:
                user_level = admin_roles[r_id]
                try:
                    user_index = levels.index(user_level)
                    if user_index <= req_index: # インデックスが小さいほど偉い
                        return True
                except ValueError:
                    continue
        
        raise app_commands.AppCommandError(f"この操作には '{required_level}' 以上の権限が必要です。")
    return app_commands.check(predicate)

class BankDatabase:
    def __init__(self, db_path="lumen_bank_v4.db"):
        self.db_path = db_path
    async def setup(self, conn):
        # 高速化設定
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")

        # 1. 口座・取引
        await conn.execute("""CREATE TABLE IF NOT EXISTS accounts (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 0 CHECK(balance >= 0), 
            total_earned INTEGER DEFAULT 0
        )""")

        await conn.execute("""CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER REFERENCES accounts(user_id),
            receiver_id INTEGER REFERENCES accounts(user_id),
            amount INTEGER,
            type TEXT,
            batch_id TEXT,
            month_tag TEXT,
            description TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")

        # 2. 設定・権限
        await conn.execute("CREATE TABLE IF NOT EXISTS server_config (key TEXT PRIMARY KEY, value TEXT)")
        await conn.execute("CREATE TABLE IF NOT EXISTS role_wages (role_id INTEGER PRIMARY KEY, amount INTEGER NOT NULL)")
        await conn.execute("CREATE TABLE IF NOT EXISTS admin_roles (role_id INTEGER PRIMARY KEY, perm_level TEXT)")
        
        # ユーザーごとの設定
        await conn.execute("""CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY, 
            dm_salary_enabled INTEGER DEFAULT 1
        )""")

        # 3. VC関連
        await conn.execute("CREATE TABLE IF NOT EXISTS voice_stats (user_id INTEGER PRIMARY KEY, total_seconds INTEGER DEFAULT 0)")
        await conn.execute("CREATE TABLE IF NOT EXISTS voice_tracking (user_id INTEGER PRIMARY KEY, join_time TEXT)")
        
        await conn.execute("""CREATE TABLE IF NOT EXISTS temp_vcs (
            channel_id INTEGER PRIMARY KEY,
            guild_id INTEGER,
            owner_id INTEGER,
            expire_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")

        await conn.execute("CREATE TABLE IF NOT EXISTS reward_channels (channel_id INTEGER PRIMARY KEY)")

        # 4. インデックス
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trans_receiver ON transactions (receiver_id, created_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_temp_vc_expire ON temp_vcs (expire_at)")

        await conn.commit()

    
# --- UI: VC内操作パネル  ---
class VCControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="招待するメンバーを選択...", min_values=1, max_values=10, row=0)
    async def invite_users(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        
        channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel):
            return await interaction.followup.send("❌ ここはボイスチャンネルではありません。", ephemeral=True)

        perms = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            stream=True,
            use_voice_activation=True,
            send_messages=True,
            read_message_history=True
        )

        added_users = []
        for member in select.values:
            if member.bot: continue
            await channel.set_permissions(member, overwrite=perms)
            added_users.append(member.display_name)

        await interaction.followup.send(f"✅ 以下のメンバーを招待しました:\n{', '.join(added_users)}", ephemeral=True)
        await channel.send(f"👋 {interaction.user.mention} が {', '.join([m.mention for m in select.values])} を招待しました。")

    @discord.ui.button(label="メンバーの権限を剥奪(追放)", style=discord.ButtonStyle.danger, row=1)
    async def kick_user_menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = RemoveUserView()
        await interaction.response.send_message("権限を剥奪するメンバーを選んでください。", view=view, ephemeral=True)


class RemoveUserView(discord.ui.View):
    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="権限を剥奪するメンバーを選択...", min_values=1, max_values=10)
    async def remove_users(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        
        removed_names = []
        for member in select.values:
            if member.id == interaction.user.id: continue
            if member.bot: continue
            
            await channel.set_permissions(member, overwrite=None)
            
            if member.voice and member.voice.channel.id == channel.id:
                await member.move_to(None)
            
            removed_names.append(member.display_name)

        if removed_names:
            await interaction.followup.send(f"🚫 以下のメンバーの権限を剥奪しました:\n{', '.join(removed_names)}", ephemeral=True)
        else:
            await interaction.followup.send("❌ 対象を選択してください（自分自身は削除できません）。", ephemeral=True)


# --- UI: プラン選択メニュー  ---
class PlanSelect(discord.ui.Select):
    def __init__(self, prices: dict):
        self.prices = prices
        options = [
            discord.SelectOption(
                label="6時間プラン", 
                description=f"{prices.get('6', 5000):,} Ru - ちょっとした作業や会議に", 
                value="6", emoji="🕐"
            ),
            discord.SelectOption(
                label="12時間プラン", 
                description=f"{prices.get('12', 10000):,} Ru - 半日じっくり", 
                value="12", emoji="🕓"
            ),
            discord.SelectOption(
                label="24時間プラン", 
                description=f"{prices.get('24', 30000):,} Ru - 丸一日貸切", 
                value="24", emoji="🕛"
            ),
        ]
        super().__init__(placeholder="利用プランを選択してください...", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        user = interaction.user
        bot = interaction.client

        async with bot.get_db() as db:
            async with db.execute("SELECT channel_id FROM temp_vcs WHERE owner_id = ?", (user.id,)) as cursor:
                existing_vc = await cursor.fetchone()
            if existing_vc:
                return await interaction.followup.send("❌ あなたは既に一時VCを作成しています。", ephemeral=True)

        hours = int(self.values[0])
        price = self.prices.get(str(hours), 5000)

        async with bot.get_db() as db:
            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (user.id,)) as cursor:
                row = await cursor.fetchone()
                current_bal = row['balance'] if row else 0

            if current_bal < price:
                return await interaction.followup.send(f"❌ 残高不足です。\n必要: {price:,} Ru / 所持: {current_bal:,} Ru", ephemeral=True)

            month_tag = datetime.datetime.now().strftime("%Y-%m")
            
            await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (price, user.id))
            await db.execute(
                "INSERT INTO transactions (sender_id, receiver_id, amount, type, description, month_tag) VALUES (?, 0, ?, 'VC_CREATE', ?, ?)",
                (user.id, price, f"一時VC作成 ({hours}時間)", month_tag)
            )
            await db.commit()

        try:
            guild = interaction.guild
            category = interaction.channel.category
            
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
                user: discord.PermissionOverwrite(
                    view_channel=True, connect=True, speak=True, stream=True, 
                    use_voice_activation=True, send_messages=True, read_message_history=True,
                    move_members=True, mute_members=True
                ),
                guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
            }

            channel_name = f"🔒 {user.display_name}の部屋"
            if not category:
                new_vc = await guild.create_voice_channel(name=channel_name, overwrites=overwrites, user_limit=2)
            else:
                new_vc = await guild.create_voice_channel(name=channel_name, category=category, overwrites=overwrites, user_limit=2)

            expire_dt = datetime.datetime.now() + datetime.timedelta(hours=hours)
            async with bot.get_db() as db:
                await db.execute(
                    "INSERT INTO temp_vcs (channel_id, guild_id, owner_id, expire_at) VALUES (?, ?, ?, ?)",
                    (new_vc.id, guild.id, user.id, expire_dt)
                )
                await db.commit()

            await new_vc.send(
                f"{user.mention} ようこそ！\nこのパネルを使って、友達を招待したり権限を管理できます。\n(時間が来るとこのチャンネルは自動消滅します)", 
                view=VCControlView()
            )

            await interaction.followup.send(f"✅ 作成完了: {new_vc.mention}\n期限: {expire_dt.strftime('%m/%d %H:%M')}\n招待機能はチャンネル内のパネルを使用してください。", ephemeral=True)

        except Exception as e:
            logger.error(f"VC Create Error: {e}")
            await interaction.followup.send("❌ VC作成中にエラーが発生しました。", ephemeral=True)


class VCPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="一時VCを作成する", style=discord.ButtonStyle.success, custom_id="create_temp_vc_btn", emoji="🔒")
    async def create_vc_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot = interaction.client
        prices = {}
        async with bot.get_db() as db:
            async with db.execute("SELECT key, value FROM server_config WHERE key IN ('vc_price_6', 'vc_price_12', 'vc_price_24')") as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    prices[row['key'].replace('vc_price_', '')] = int(row['value'])

        if '6' not in prices: prices['6'] = 5000
        if '12' not in prices: prices['12'] = 10000
        if '24' not in prices: prices['24'] = 30000

        view = discord.ui.View()
        view.add_item(PlanSelect(prices))
        await interaction.response.send_message("利用する時間プランを選択してください。", view=view, ephemeral=True)


# --- Cog: PrivateVCManager ---
class PrivateVCManager(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_expiration_task.start()

    def cog_unload(self):
        self.check_expiration_task.cancel()

    @tasks.loop(minutes=1)
    async def check_expiration_task(self):
        now = datetime.datetime.now()
        try:
            async with self.bot.get_db() as db:
                async with db.execute("SELECT channel_id, guild_id FROM temp_vcs WHERE expire_at < ?", (now,)) as cursor:
                    expired_vcs = await cursor.fetchall()

                if not expired_vcs: return

                for row in expired_vcs:
                    c_id = row['channel_id']
                    channel = self.bot.get_channel(c_id)
                    if channel:
                        try:
                            await channel.delete(reason="Temp VC Expired")
                        except: pass
                    await db.execute("DELETE FROM temp_vcs WHERE channel_id = ?", (c_id,))
                await db.commit()
        except Exception as e:
            logger.error(f"Expiration Check Error: {e}")

    @check_expiration_task.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="一時vcパネル作成", description="【管理者】内容をカスタマイズしてVC作成パネルを設置します")
    @app_commands.describe(
        title="パネルのタイトル",
        description="パネルの説明文（\\nで改行）",
        price_6h="6時間プランの価格",
        price_12h="12時間プランの価格",
        price_24h="24時間プランの価格"
    )
    @has_permission("ADMIN")
    async def deploy_panel(
        self, 
        interaction: discord.Interaction, 
        title: str = "🔒 プライベート一時VC作成パネル", 
        description: str = None, 
        price_6h: int = 5000, 
        price_12h: int = 10000, 
        price_24h: int = 30000
    ):
        
        await interaction.response.defer(ephemeral=True)

        if description is None:
            description = (
                "権限のある人以外からは見えない、プライベートな一時VCを作成できます。\n\n"
                "**🔒 プライバシー**\n招待した人以外は見えません\n"
                "**🛡 料金システム**\n作成時に自動引き落とし\n"
                f"**⏰ 料金プラン**\n"
                f"• **6時間**: {price_6h:,} Ru\n"
                f"• **12時間**: {price_12h:,} Ru\n"
                f"• **24時間**: {price_24h:,} Ru"
            )
        else:
            description = description.replace("\\n", "\n")

        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('vc_price_6', ?)", (str(price_6h),))
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('vc_price_12', ?)", (str(price_12h),))
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('vc_price_24', ?)", (str(price_24h),))
            await db.commit()

        embed = discord.Embed(title=title, description=description, color=0x2b2d31)
        embed.set_footer(text=f"Last Updated: {datetime.datetime.now().strftime('%Y/%m/%d %H:%M')}")
        
        await interaction.channel.send(embed=embed, view=VCPanel())
        await interaction.followup.send("✅ 設定を保存し、パネルを設置しました。", ephemeral=True)

class TransferConfirmView(discord.ui.View):
    def __init__(self, bot, sender, receiver, amount, message):
        super().__init__(timeout=60)
        self.bot = bot
        self.sender = sender
        self.receiver = receiver
        self.amount = amount
        self.msg = message
        self.processed = False

    async def on_timeout(self):
        if not self.processed:
            for child in self.children:
                child.disabled = True
            try:
                await self.message.edit(content="⏰ 時間切れです。", view=self)
            except:
                pass

    @discord.ui.button(label="✅ 送金を実行する", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.processed: return
        self.processed = True
        
        if interaction.user.id != self.sender.id:
            return await interaction.response.send_message("❌ 操作権限がありません。", ephemeral=True)

        await interaction.response.defer()
        
        month_tag = datetime.datetime.now().strftime("%Y-%m")
        sender_new_bal = 0
        receiver_new_bal = 0

        async with self.bot.get_db() as db:
            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (self.sender.id,)) as c:
                row = await c.fetchone()
                if not row or row['balance'] < self.amount:
                    return await interaction.followup.send("❌ 残高が不足しています。", ephemeral=True)

            try:
                await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (self.amount, self.sender.id))
                
                await db.execute("""
                    INSERT INTO accounts (user_id, balance, total_earned) VALUES (?, ?, 0)
                    ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance
                """, (self.receiver.id, self.amount))
                
                await db.execute("""
                    INSERT INTO transactions (sender_id, receiver_id, amount, type, description, month_tag)
                    VALUES (?, ?, ?, 'TRANSFER', ?, ?)
                """, (self.sender.id, self.receiver.id, self.amount, self.msg, month_tag))
                
                async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (self.sender.id,)) as c:
                    sender_new_bal = (await c.fetchone())['balance']
                async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (self.receiver.id,)) as c:
                    receiver_new_bal = (await c.fetchone())['balance']

                await db.commit()
                
                self.stop()
                await interaction.edit_original_response(content=f"✅ {self.receiver.mention} へ {self.amount:,} Ru 送金しました。", embed=None, view=None)

                try:
                    notify = True
                    async with db.execute("SELECT dm_salary_enabled FROM user_settings WHERE user_id = ?", (self.receiver.id,)) as c:
                        res = await c.fetchone()
                        if res and res['dm_salary_enabled'] == 0: notify = False
                    
                    if notify:
                        embed = discord.Embed(title="💰 Ru_men受取通知", color=discord.Color.green())
                        embed.add_field(name="送金者", value=self.sender.mention, inline=False)
                        embed.add_field(name="受取額", value=f"**{self.amount:,} Ru**", inline=False)
                        embed.add_field(name="メッセージ", value=f"`{self.msg}`", inline=False)
                        embed.timestamp = datetime.datetime.now()
                        await self.receiver.send(embed=embed)
                except:
                    pass

                log_ch_id = None
                async with db.execute("SELECT value FROM server_config WHERE key = 'currency_log_id'") as c:
                    row = await c.fetchone()
                    if row: log_ch_id = int(row['value'])
                
                if log_ch_id:
                    channel = self.bot.get_channel(log_ch_id)
                    if channel:
                        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        log_embed = discord.Embed(title="💸 送金ログ", color=0xFFD700)
                        log_embed.description = f"{self.sender.mention} ➔ {self.receiver.mention}"
                        log_embed.add_field(name="金額", value=f"**{self.amount:,} Ru**", inline=True)
                        log_embed.add_field(name="備考", value=self.msg, inline=True)
                        log_embed.add_field(name="処理後残高", value=f"送: {sender_new_bal:,} Ru\n受: {receiver_new_bal:,} Ru", inline=False)
                        log_embed.set_footer(text=f"Time: {now_str}")
                        await channel.send(embed=log_embed)

            except Exception as e:
                await db.rollback()
                await interaction.followup.send(f"❌ エラーが発生しました: {e}", ephemeral=True)

    @discord.ui.button(label="❌ キャンセル", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.processed: return
        self.processed = True
        
        if interaction.user.id != self.sender.id:
            return await interaction.response.send_message("❌ 操作権限がありません。", ephemeral=True)

        self.stop()
        await interaction.response.edit_message(content="❌ 送金をキャンセルしました。", embed=None, view=None)


# --- Cog: Economy (残高・送金) ---
class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ping", description="【管理者】Botの応答速度を確認します")
    @has_permission("ADMIN")
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"🏓 Pong! Latency: `{latency}ms`", ephemeral=True)

    @app_commands.command(name="残高確認", description="現在の所持金を確認します")
    async def balance(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        target = member or interaction.user
        
        if target.id != interaction.user.id:
            if not await self.check_admin_permission(interaction.user):
                return await interaction.followup.send("❌ 他人の口座を参照する権限がありません。", ephemeral=True)

        async with self.bot.get_db() as db:
            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (target.id,)) as cursor:
                row = await cursor.fetchone()
                bal = row['balance'] if row else 0
        
        embed = discord.Embed(title="🏛 ルーメン銀行 口座照会", color=0xFFD700)
        embed.set_author(name=f"{target.display_name} 様", icon_url=target.display_avatar.url)
        embed.add_field(name="💰 現在の残高", value=f"**{bal:,} Ru**", inline=False)
        embed.set_footer(text=f"Elysion Economy System")
        embed.set_thumbnail(url=target.display_avatar.url)
        
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="送金", description="他のユーザーにRuを送金します")
    @app_commands.describe(receiver="送金相手", amount="送金額", message="相手へのメッセージ（任意）")
    async def transfer(self, interaction: discord.Interaction, receiver: discord.Member, amount: int, message: str = "送金"):
        if amount <= 0: return await interaction.response.send_message("❌ 1 Ru 以上を指定してください。", ephemeral=True)
        if amount > 10000000: return await interaction.response.send_message("❌ 1回の送金上限は 10,000,000 Ru です。", ephemeral=True)
        if receiver.id == interaction.user.id: return await interaction.response.send_message("❌ 自分自身には送金できません。", ephemeral=True)
        if receiver.bot: return await interaction.response.send_message("❌ Botには送金できません。", ephemeral=True)

        embed = discord.Embed(title="⚠️ 送金確認", description="以下の内容で送金しますか？", color=discord.Color.orange())
        embed.add_field(name="👤 送金先", value=receiver.mention, inline=True)
        embed.add_field(name="💰 金額", value=f"**{amount:,} Ru**", inline=True)
        embed.add_field(name="💬 メッセージ", value=f"`{message}`", inline=False)
        
        view = TransferConfirmView(self.bot, interaction.user, receiver, amount, message)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="履歴", description="直近10件の入出金履歴を表示します")
    async def history(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            query = "SELECT * FROM transactions WHERE sender_id = ? OR receiver_id = ? ORDER BY created_at DESC LIMIT 10"
            async with db.execute(query, (interaction.user.id, interaction.user.id)) as cursor:
                rows = await cursor.fetchall()
        
        if not rows: return await interaction.followup.send("取引履歴はありません。", ephemeral=True)

        embed = discord.Embed(title="📜 取引履歴明細", color=discord.Color.blue())
        for r in rows:
            is_sender = r['sender_id'] == interaction.user.id
            emoji = "📤 送金" if is_sender else "📥 受取"
            amount_str = f"{'-' if is_sender else '+'}{r['amount']:,} Ru"
            
            target_id = r['receiver_id'] if is_sender else r['sender_id']
            target_name = f"<@{target_id}>" if target_id != 0 else "システム"

            embed.add_field(
                name=f"{r['created_at'][5:16]} | {emoji}",
                value=f"金額: **{amount_str}**\n相手: {target_name}\n内容: `{r['description']}`",
                inline=False
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def check_admin_permission(self, user):
        if await self.bot.is_owner(user): return True
        user_role_ids = [role.id for role in user.roles]
        admin_roles = self.bot.config.admin_roles
        for r_id in user_role_ids:
            if r_id in admin_roles and admin_roles[r_id] in ["SUPREME_GOD", "GODDESS"]:
                return True
        return False


class Salary(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="給与通知設定", description="給与支給時のDM明細通知をON/OFFします")
    @app_commands.describe(status="ON: 通知を受け取る / OFF: 通知しない")
    @app_commands.choices(status=[
        app_commands.Choice(name="ON (通知する)", value=1),
        app_commands.Choice(name="OFF (通知しない)", value=0)
    ])
    async def toggle_dm(self, interaction: discord.Interaction, status: int):
        async with self.bot.get_db() as db:
            await db.execute("""
                INSERT INTO user_settings (user_id, dm_salary_enabled) 
                VALUES (?, ?) 
                ON CONFLICT(user_id) DO UPDATE SET dm_salary_enabled = excluded.dm_salary_enabled
            """, (interaction.user.id, status))
            await db.commit()
        
        msg = "✅ 今後、給与明細は **DMで通知されます**。" if status == 1 else "🔕 今後、給与明細の **DM通知は行われません**。"
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="一括給与", description="【最高神】全役職の給与を合算支給し、明細をDM送信します")
    @has_permission("SUPREME_GOD")
    async def distribute_all(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        now = datetime.datetime.now()
        month_tag = now.strftime("%Y-%m")
        batch_id = str(uuid.uuid4())[:8]
        
        wage_dict = {}
        dm_prefs = {}
        async with self.bot.get_db() as db:
            async with db.execute("SELECT role_id, amount FROM role_wages") as c:
                async for r in c: wage_dict[int(r['role_id'])] = int(r['amount'])
            async with db.execute("SELECT user_id, dm_salary_enabled FROM user_settings") as c:
                async for r in c: dm_prefs[int(r['user_id'])] = bool(r['dm_salary_enabled'])

        if not wage_dict:
            return await interaction.followup.send("⚠️ 給与設定が見つかりません。")
        
        count = 0
        total_payout = 0
        role_summary = {}
        payout_data_list = []

        members = interaction.guild.members if interaction.guild.chunked else [m async for m in interaction.guild.fetch_members()]

        async with self.bot.get_db() as db:
            for member in members:
                if member.bot: continue
                
                matching = [(wage_dict[r.id], r) for r in member.roles if r.id in wage_dict]
                if not matching: continue
                
                member_total = sum(w for w, _ in matching)
                
                await db.execute("""
                    INSERT INTO accounts (user_id, balance, total_earned) VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET 
                    balance = balance + excluded.balance, total_earned = total_earned + excluded.total_earned
                """, (member.id, member_total, member_total))
                
                await db.execute("""
                    INSERT INTO transactions (sender_id, receiver_id, amount, type, batch_id, month_tag, description)
                    VALUES (0, ?, ?, 'SALARY', ?, ?, ?)
                """, (member.id, member_total, batch_id, month_tag, f"{month_tag} 給与"))

                count += 1
                total_payout += member_total
                for w, r in matching:
                    if r.id not in role_summary: role_summary[r.id] = {"mention": r.mention, "count": 0, "amount": 0}
                    role_summary[r.id]["count"] += 1
                    role_summary[r.id]["amount"] += w

                if dm_prefs.get(member.id, True):
                    payout_data_list.append((member, member_total, matching))

            await db.commit()

        sent_dm = 0
        for m, total, matching in payout_data_list:
            try:
                embed = self.create_salary_slip_embed(m, total, matching, month_tag)
                await m.send(embed=embed)
                sent_dm += 1
            except: pass

        await interaction.followup.send(f"💰 **一括支給完了** (ID: `{batch_id}`)\n人数: {count}名 / 総額: {total_payout:,} Ru\n通知送信: {sent_dm}名")
        await self.send_salary_log(interaction, batch_id, total_payout, count, role_summary, now)

    def create_salary_slip_embed(self, member, total, matching, month_tag):
        sorted_matching = sorted(matching, key=lambda x: x[0], reverse=True)
        main_role = sorted_matching[0][1]
        
        embed = discord.Embed(
            title="💰 月給支給のお知らせ",
            description=f"**{month_tag}** の月給が支給されました！",
            color=0x00FF00,
            timestamp=datetime.datetime.now()
        )
        
        embed.add_field(name="💵 支給総額", value=f"**{total:,} Ru**", inline=False)
        
        formula = " + ".join([f"{w:,}" for w, r in sorted_matching])
        embed.add_field(name="🧮 計算式", value=f"{formula} = **{total:,} Ru**", inline=False)
        
        breakdown = "\n".join([f"{i+1}. {r.name}: {w:,} Ru" for i, (w, r) in enumerate(sorted_matching)])
        embed.add_field(name="📊 給与内訳", value=breakdown, inline=False)
        
        embed.add_field(name="🏆 メインロール", value=main_role.name, inline=True)
        embed.add_field(name="🔢 適用ロール数", value=f"{len(matching)}個", inline=True)
        embed.add_field(name="📅 支給月", value=month_tag, inline=True)

        if len(matching) > 1:
            embed.add_field(
                name="⚠️ 複数ロール適用", 
                value="あなたは複数の給与対象ロールを持っているため、全ての給与が合算されて支給されています。", 
                inline=False
            )
        
        embed.set_footer(text="給与計算についてご質問がありましたら管理者にお声がけください")
        return embed

    @app_commands.command(name="給与一覧", description="現在設定されている役職ごとの給与テーブルを表示します")
    async def list_wages(self, interaction: discord.Interaction):
        async with self.bot.get_db() as db:
            async with db.execute("SELECT role_id, amount FROM role_wages ORDER BY amount DESC") as cursor:
                rows = await cursor.fetchall()
        
        if not rows:
            return await interaction.response.send_message("⚠️ 給与設定はまだ登録されていません。", ephemeral=True)
        
        embed = discord.Embed(title="📋 給与テーブル設定一覧", color=discord.Color.blue())
        text = ""
        for row in rows:
            role = interaction.guild.get_role(int(row['role_id']))
            role_str = role.mention if role else f"不明なロール(`{row['role_id']}`)"
            text += f"{role_str}: **{row['amount']:,} Ru**\n"
        
        embed.description = text
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="一括給与取り消し", description="【最高神】識別ID(Batch ID)を指定して給与支給を取り消します")
    @has_permission("SUPREME_GOD")
    async def salary_rollback(self, interaction: discord.Interaction, batch_id: str):
        await interaction.response.defer(ephemeral=True)
        
        async with self.bot.get_db() as db:
            async with db.execute("SELECT receiver_id, amount FROM transactions WHERE batch_id = ? AND type = 'SALARY'", (batch_id,)) as cursor:
                rows = await cursor.fetchall()
            
            if not rows:
                return await interaction.followup.send(f"❌ ID `{batch_id}` の給与データが見つかりません。", ephemeral=True)
            
            try:
                for row in rows:
                    await db.execute("""
                        UPDATE accounts SET balance = balance - ?, total_earned = total_earned - ? 
                        WHERE user_id = ?
                    """, (row['amount'], row['amount'], row['receiver_id']))
                
                await db.execute("DELETE FROM transactions WHERE batch_id = ?", (batch_id,))
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error(f"Rollback Error: {e}")
                return await interaction.followup.send("❌ エラーが発生しました。")

        await interaction.followup.send(f"↩️ **ロールバック完了**\nID: `{batch_id}` の支給を回収しました。")

    async def send_salary_log(self, interaction, batch_id, total, count, breakdown, timestamp):
        log_ch_id = None
        async with self.bot.get_db() as db:
            async with db.execute("SELECT value FROM server_config WHERE key = 'salary_log_id'") as c:
                row = await c.fetchone()
                if row: log_ch_id = int(row['value'])
        
        if not log_ch_id: return
        channel = self.bot.get_channel(log_ch_id)
        if not channel: return

        embed = discord.Embed(title="給与一斉送信ログ", color=0xFFD700, timestamp=timestamp)
        embed.add_field(name="実行者", value=interaction.user.mention, inline=True)
        embed.add_field(name="総額 / 人数", value=f"**{total:,} Ru** / {count}名", inline=True)
        
        breakdown_text = "\n".join([f"✅ {d['mention']}: {d['amount']:,} Ru ({d['count']}名)" for d in breakdown.values()])
        if breakdown_text:
            embed.add_field(name="ロール別内訳", value=breakdown_text, inline=False)
        
        embed.set_footer(text=f"BatchID: {batch_id}")
        await channel.send(embed=embed)


class Jackpot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ticket_price = 5000
        self.sponsor_cut = 0.10
        self.employee_cut = 0.10
        self.limit_per_round = 30
        self.max_number = 999
        self.seed_money = 1000000
        self.sponsor_name_display = "滝" 
        self.employee_role_name = "賭博従者"

    async def init_db(self):
        async with self.bot.get_db() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS lottery_tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    number INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS server_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await db.commit()

    @app_commands.command(name="ジャックポット状況", description="現在の賞金総額と自分の番号を確認します")
    async def status(self, interaction: discord.Interaction):
        await self.init_db()
        
        async with self.bot.get_db() as db:
            async with db.execute("SELECT value FROM server_config WHERE key = 'jackpot_pool'") as c:
                row = await c.fetchone()
                pool = int(row['value']) if row else 0

            async with db.execute("SELECT number FROM lottery_tickets WHERE user_id = ? ORDER BY number", (interaction.user.id,)) as c:
                my_tickets = await c.fetchall()
                my_numbers = [f"{row['number']:03d}" for row in my_tickets]

            async with db.execute("SELECT COUNT(*) as total FROM lottery_tickets") as c:
                sold_count = (await c.fetchone())['total']

        embed = discord.Embed(title="🎟️ エリュシオン・ジャンボ宝くじ", color=0xffd700)
        embed.description = (
            "3桁の番号(000-999)が当選番号と一致すれば賞金獲得！\n"
            "当選者なしの場合、賞金は**全額キャリーオーバー**されます。\n"
        )
        
        embed.add_field(name="💰 現在の賞金総額", value=f"**{pool:,} Ru**", inline=False)
        embed.add_field(name="👑 公認スポンサー", value=f"**{self.sponsor_name_display}** 様", inline=True)
        embed.add_field(name="🎫 発行済み枚数", value=f"{sold_count:,} 枚", inline=True)
        embed.add_field(name="📅 当選確率", value="1 / 1000", inline=True)

        if my_numbers:
            ticket_str = ", ".join(my_numbers)
            if len(ticket_str) > 500: ticket_str = ticket_str[:500] + "..."
            embed.add_field(name=f"🎫 あなたの番号 ({len(my_numbers)}枚)", value=f"`{ticket_str}`", inline=False)
        else:
            embed.add_field(name="🎫 あなたの番号", value="未購入", inline=False)
        
        embed.set_footer(text=f"上限: {self.limit_per_round}枚/人 | 当選時、賞金の10%は従業員に分配されます")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="ジャックポット購入", description="ランダムな3桁の番号が付与されます (1枚 5,000 Ru)")
    @app_commands.describe(amount="購入枚数")
    async def buy(self, interaction: discord.Interaction, amount: int):
        if amount <= 0: return await interaction.response.send_message("1枚以上指定してください。", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        total_cost = self.ticket_price * amount

        async with self.bot.get_db() as db:
            async with db.execute("SELECT COUNT(*) as count FROM lottery_tickets WHERE user_id = ?", (user.id,)) as c:
                current_count = (await c.fetchone())['count']
                if current_count + amount > self.limit_per_round:
                    return await interaction.followup.send(f"❌ 購入上限です (残り: {self.limit_per_round - current_count}枚)", ephemeral=True)

            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (user.id,)) as c:
                row = await c.fetchone()
                if not row or row['balance'] < total_cost:
                    return await interaction.followup.send("❌ 資金不足です。", ephemeral=True)

            try:
                async with db.execute("SELECT value FROM server_config WHERE key = 'jackpot_sponsor_id'") as c:
                    s_row = await c.fetchone()
                    sponsor_id = int(s_row['value']) if s_row else 0

                await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (total_cost, user.id))
                
                sponsor_reward = int(total_cost * self.sponsor_cut)
                if sponsor_id > 0:
                    await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (sponsor_reward, sponsor_id))
                
                to_pool = total_cost - sponsor_reward
                await db.execute("""
                    INSERT INTO server_config (key, value) VALUES ('jackpot_pool', ?) 
                    ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + ?
                """, (to_pool, to_pool))

                new_tickets = []
                my_numbers = []
                for _ in range(amount):
                    num = random.randint(0, self.max_number)
                    new_tickets.append((user.id, num))
                    my_numbers.append(f"{num:03d}")
                
                await db.executemany("INSERT INTO lottery_tickets (user_id, number) VALUES (?, ?)", new_tickets)
                await db.commit()

                num_display = ", ".join(my_numbers)
                await interaction.followup.send(f"✅ **{amount}枚** 購入しました！\n獲得番号: `{num_display}`\n(売上の10%はスポンサーへ還元されました)", ephemeral=True)

            except Exception as e:
                await db.rollback()
                traceback.print_exc()
                await interaction.followup.send("❌ システムエラーが発生しました。", ephemeral=True)

    @app_commands.command(name="ジャックポット抽選", description="【管理者】当選番号を決定します")
    @app_commands.describe(panic_release="Trueの場合、購入済みチケットから強制的に当選者を選びます")
    @app_commands.default_permissions(administrator=True)
    async def draw(self, interaction: discord.Interaction, panic_release: bool = False):
        await interaction.response.defer()
        
        async with self.bot.get_db() as db:
            async with db.execute("SELECT value FROM server_config WHERE key = 'jackpot_pool'") as c:
                row = await c.fetchone()
                current_pool = int(row['value']) if row else 0
            
            async with db.execute("SELECT value FROM server_config WHERE key = 'jackpot_sponsor_id'") as c:
                s_row = await c.fetchone()
                sponsor_id = int(s_row['value']) if s_row else 0

        winning_number = random.randint(0, self.max_number)
        winners = []
        is_panic = False

        async with self.bot.get_db() as db:
            if panic_release:
                async with db.execute("SELECT user_id, number FROM lottery_tickets") as c:
                    all_sold = await c.fetchall()
                if not all_sold: return await interaction.followup.send("⚠️ チケットが売れていません。")
                
                is_panic = True
                lucky = random.choice(all_sold)
                winning_number = lucky['number']
                winners = [t for t in all_sold if t['number'] == winning_number]
            else:
                async with db.execute("SELECT user_id FROM lottery_tickets WHERE number = ?", (winning_number,)) as c:
                    winners = await c.fetchall()

            winning_str = f"{winning_number:03d}"
            
            embed = discord.Embed(title="🎰 エリュシオン・ジャンボ 抽選会", color=0xffd700)
            embed.add_field(name="🎯 当選番号", value=f"<h1>**{winning_str}**</h1>", inline=False)

            if len(winners) > 0:
                total_employee_reward = int(current_pool * self.employee_cut)
                winner_pool = current_pool - total_employee_reward
                
                guild = interaction.guild
                employee_role = discord.utils.get(guild.roles, name=self.employee_role_name)
                
                emp_msg = ""
                if employee_role and len(employee_role.members) > 0:
                    pay_per_emp = total_employee_reward // len(employee_role.members)
                    for member in employee_role.members:
                        await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (pay_per_emp, member.id))
                    
                    emp_msg = f"\n(賞金の10% **{total_employee_reward:,} Ru** が\n従業員 **{len(employee_role.members)}名** に給与として分配されました)"
                else:
                    winner_pool += total_employee_reward
                    emp_msg = "\n(従業員不在のため、カット分は賞金に還元されました)"

                prize_per_winner = winner_pool // len(winners)
                winner_mentions = []
                for w in winners:
                    uid = w['user_id']
                    await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (prize_per_winner, uid))
                    winner_mentions.append(f"<@{uid}>")
                
                sponsor_msg = ""
                if sponsor_id > 0:
                    await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (self.seed_money, sponsor_id))
                    await db.execute("UPDATE server_config SET value = ? WHERE key = 'jackpot_pool'", (str(self.seed_money),))
                    sponsor_msg = f"\n(スポンサー {self.sponsor_name_display} から次回開催費 **{self.seed_money:,} Ru** を徴収しました)"
                else:
                    await db.execute("UPDATE server_config SET value = '0' WHERE key = 'jackpot_pool'")

                await db.execute("DELETE FROM lottery_tickets")
                await db.commit()

                desc = "キャリーオーバー放出！"
                if is_panic: desc = "🚨 **パニック・リリース発動！強制放出！** 🚨"
                
                embed.description = f"🎉 **{len(winners)}名** の当選者が出ました！{desc}"
                embed.add_field(name="💰 1人あたりの賞金", value=f"**{prize_per_winner:,} Ru** (手取り)", inline=False)
                
                mentions = " ".join(list(set(winner_mentions)))
                if len(mentions) > 1000: mentions = f"{len(winners)}名の当選者"
                embed.add_field(name="🏆 当選者一覧", value=mentions, inline=False)
                
                footer = f"おめでとうございます！{sponsor_msg}{emp_msg}"
                if len(footer) > 2000: footer = footer[:2000] + "..."
                embed.set_footer(text=footer)
                embed.color = 0xff00ff 

            else:
                await db.execute("DELETE FROM lottery_tickets")
                await db.commit()
                embed.description = "💀 **当選者なし...**"
                embed.add_field(name="💸 キャリーオーバー", value=f"**{current_pool:,} Ru** は次回に持ち越されます！", inline=False)
                embed.color = 0x2f3136

        await interaction.followup.send(content="@everyone", embed=embed)

    @app_commands.command(name="ジャックポット設定", description="【管理者】スポンサーを設定(売上10%還元 / 当選時100万徴収)")
    @app_commands.default_permissions(administrator=True)
    async def set_sponsor(self, interaction: discord.Interaction, user: discord.User):
        async with self.bot.get_db() as db:
            await db.execute("""
                INSERT INTO server_config (key, value) VALUES ('jackpot_sponsor_id', ?)
                ON CONFLICT(key) DO UPDATE SET value = ?
            """, (str(user.id), str(user.id)))
            await db.commit()
        
        await interaction.response.send_message(f"✅ ジャックポットのスポンサーを {user.mention} (滝) に設定しました。\n・チケット売上の**10%**が還元されます。\n・当選者が出た場合、**100万Ru**が徴収されます。", ephemeral=True)


# --- 色定義 ---
def ansi(text, color_code): return f"\x1b[{color_code}m{text}\x1b[0m"
def gold(t): return ansi(t, "1;33")
def red(t): return ansi(t, "1;31")
def green(t): return ansi(t, "1;32")
def pink(t): return ansi(t, "1;35")
def gray(t): return ansi(t, "1;30")

class Omikuji(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cost = 300
        
        self.FORTUNES = [
            {"name": "【 大 吉 】", "rate": 4,  "payout": 1500, "color": gold, "msg": "「…へぇ、やるじゃない。今日は私の隣に座る？」"},
            {"name": "【 中 吉 】", "rate": 20, "payout": 500,  "color": green, "msg": "「悪くないわね。調子に乗らない程度に頑張りなさい。」"},
            {"name": "【 小 吉 】", "rate": 20, "payout": 300,  "color": green, "msg": "「普通。損はしてないんだから感謝しなさいよ。」"},
            {"name": "【 末 吉 】", "rate": 20, "payout": 100,  "color": gray,  "msg": "「微妙ね。ま、あんたにはお似合いかも。」"},
            {"name": "【　凶　】", "rate": 25, "payout": 0,    "color": red,   "msg": "「プッ、ざまぁないわね。日頃の行いが悪いんじゃなくって？」"},
            {"name": "【 大 凶 】", "rate": 11, "payout": 0,    "color": red,   "msg": "「あはは！ 最高に無様！ 近寄らないで、不幸が移るわ。」"}
        ]

    @app_commands.command(name="おみくじ", description="ルメンちゃんが今日の運勢を占います (1回 300 Ru)")
    async def omikuji(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user = interaction.user

        async with self.bot.get_db() as db:
            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (user.id,)) as c:
                row = await c.fetchone()
                if not row or row['balance'] < self.cost:
                    return await interaction.followup.send("ルメン「300Ruすら持ってないの？ 帰って。」", ephemeral=True)

            await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (self.cost, user.id))

            rand = random.randint(1, 100)
            current = 0
            result = self.FORTUNES[-1]
            
            for f in self.FORTUNES:
                current += f["rate"]
                if rand <= current:
                    result = f
                    break
            
            payout = result["payout"]
            profit = payout - self.cost
            
            if profit >= 0:
                await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (payout, user.id))
            else:
                if payout > 0:
                    await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (payout, user.id))
                
                loss_amount = abs(profit)
                jp_feed = int(loss_amount * 0.20)
                
                if jp_feed > 0:
                    await db.execute("""
                        INSERT INTO server_config (key, value) VALUES ('jackpot_pool', ?) 
                        ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + ?
                    """, (jp_feed, jp_feed))

            await db.commit()

        embed = discord.Embed(color=0x2f3136)
        if payout >= 500: embed.color = 0xffd700
        elif payout == 0: embed.color = 0xff0000

        frame_color = result["color"]
        draw_txt = (
            f"```ansi\n"
            f"{frame_color('┏━━━━━━━━━━━━━━━┓')}\n"
            f"{frame_color('┃')}   {result['name']}   {frame_color('┃')}\n"
            f"{frame_color('┗━━━━━━━━━━━━━━━┛')}\n"
            f"```"
        )

        res_str = f"**{payout} Ru** (収支: {profit:+d} Ru)"
        if profit < 0:
             res_str += f"\n(💸 負け分の20%はJP賞金へ)"

        embed.description = f"{draw_txt}\n{result['msg']}\n\n{res_str}"
        embed.set_footer(text=f"{user.display_name} の運勢")

        await interaction.followup.send(embed=embed)
        
# --- Cog: VoiceSystem  ---
class VoiceSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.target_vc_ids = set() 
        self.is_ready_processed = False

    async def reload_targets(self):
        try:
            async with self.bot.get_db() as db:
                async with db.execute("SELECT channel_id FROM reward_channels") as cursor:
                    rows = await cursor.fetchall()
            
            self.target_vc_ids = {row['channel_id'] for row in rows}
            logger.info(f"Loaded {len(self.target_vc_ids)} reward VC targets.")
        except Exception as e:
            logger.error(f"Failed to load reward channels: {e}")

    def is_active(self, state):
        return (
            state and 
            state.channel and 
            state.channel.id in self.target_vc_ids and  
            not state.self_deaf and 
            not state.deaf
        )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot: return
        now = datetime.datetime.now()
        was_active, is_now_active = self.is_active(before), self.is_active(after)

        if not was_active and is_now_active:
            try:
                async with self.bot.get_db() as db:
                    await db.execute(
                        "INSERT OR REPLACE INTO voice_tracking (user_id, join_time) VALUES (?,?)", 
                        (member.id, now.isoformat())
                    )
                    await db.commit()
            except Exception as e:
                logger.error(f"Voice Tracking Error: {e}")

        elif was_active and not is_now_active:
            await self._process_reward(member, now)

    async def _process_reward(self, member_or_id, now):
        user_id = member_or_id.id if isinstance(member_or_id, discord.Member) else member_or_id
        try:
            async with self.bot.get_db() as db:
                async with db.execute("SELECT join_time FROM voice_tracking WHERE user_id =?", (user_id,)) as cursor:
                    row = await cursor.fetchone()
                if not row: return

                try:
                    join_time = datetime.datetime.fromisoformat(row['join_time'])
                    sec = int((now - join_time).total_seconds())
                    
                    if sec < 60:
                        reward = 0
                    else:
                        reward = (sec * 50) // 60 

                    if reward > 0:
                        month_tag = now.strftime("%Y-%m")
                        
                        await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance, total_earned) VALUES (0, 0, 0)")
                        await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance, total_earned) VALUES (?, 0, 0)", (user_id,))
                        
                        await db.execute(
                            "UPDATE accounts SET balance = balance +?, total_earned = total_earned +? WHERE user_id =?", 
                            (reward, reward, user_id)
                        )
                        await db.execute("INSERT OR IGNORE INTO voice_stats (user_id) VALUES (?)", (user_id,))
                        await db.execute("UPDATE voice_stats SET total_seconds = total_seconds +? WHERE user_id =?", (sec, user_id))
                        
                        await db.execute(
                            "INSERT INTO transactions (sender_id, receiver_id, amount, type, description, month_tag) VALUES (0, ?, ?, 'VC_REWARD', 'VC活動報酬', ?)",
                            (user_id, reward, month_tag)
                        )
                    
                    await db.execute("DELETE FROM voice_tracking WHERE user_id =?", (user_id,))
                    await db.commit()

                    if reward > 0:
                        embed = discord.Embed(title="🎙 VC報酬精算", color=discord.Color.blue(), timestamp=now)
                        embed.add_field(name="ユーザー", value=f"<@{user_id}>")
                        embed.add_field(name="付与額", value=f"{reward:,} L")
                        embed.add_field(name="滞在時間", value=f"{sec // 60}分")
                        # 修正: send_admin_log -> send_bank_log
                        await self.bot.send_bank_log('currency_log_id', embed)

                except Exception as db_err:
                    await db.rollback()
                    raise db_err

        except Exception as e:
            logger.error(f"Voice Reward Process Error [{user_id}]: {e}")

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready_processed: return
        self.is_ready_processed = True
        
        await self.reload_targets()

        await asyncio.sleep(10)
        now = datetime.datetime.now()
        
        try:
            async with self.bot.get_db() as db:
                async with db.execute("SELECT user_id FROM voice_tracking") as cursor:
                    tracked_users = await cursor.fetchall()
                
                for row in tracked_users:
                    u_id = row['user_id']
                    is_active_now = False
                    for guild in self.bot.guilds:
                        member = guild.get_member(u_id)
                        if member and self.is_active(member.voice):
                            is_active_now = True
                            break
                    
                    if not is_active_now:
                        await self._process_reward(u_id, now)
        except Exception as e:
            logger.error(f"Recovery Error: {e}")

class VoiceHistory(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="vc記録", description="【女神以上】指定したユーザーのVC累計滞在時間を画像で表示します")
    @app_commands.describe(member="確認したいユーザー")
    @has_permission("GODDESS")
    async def vc_history(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()

        async with self.bot.get_db() as db:
            async with db.execute("SELECT total_seconds FROM voice_stats WHERE user_id = ?", (member.id,)) as cursor:
                row = await cursor.fetchone()
                total_seconds = row['total_seconds'] if row else 0

        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        
        img = Image.new('RGB', (600, 300), color=(44, 47, 51))
        draw = ImageDraw.Draw(img)
        
        try:
            font_main = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 40)
            font_sub = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 25)
        except:
            font_main = ImageFont.load_default()
            font_sub = ImageFont.load_default()

        draw.text((40, 40), f"VC STATS: {member.display_name}", fill=(255, 255, 255), font=font_sub)
        draw.text((40, 100), f"{hours} hours {minutes} mins", fill=(0, 255, 127), font=font_main)
        draw.text((40, 180), f"Total Seconds: {total_seconds:,}s", fill=(185, 187, 190), font=font_sub)
        
        draw.rectangle([40, 240, 560, 245], fill=(114, 137, 218))

        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='PNG')
        img_byte_arr.seek(0)
        
        file = discord.File(fp=img_byte_arr, filename=f"vc_stats_{member.id}.png")
        
        embed = discord.Embed(title="📊 VC滞在記録照会", color=0x7289da)
        embed.set_image(url=f"attachment://vc_stats_{member.id}.png")
        embed.set_footer(text=f"Requested by {interaction.user.display_name}")
        
        await interaction.followup.send(embed=embed, file=file)

# --- Cog: InterviewSystem  ---
class InterviewSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="面接通過", description="指定ユーザー or 同じVCのメンバー全員にロールと初期資金を付与します")
    @app_commands.describe(
        role="付与するロール",
        amount="初期付与額（デフォルト: 10,000）",
        target="対象ユーザー（指定しない場合は、あなたと同じVCにいる全員が対象になります）"
    )
    @has_permission("ADMIN")
    async def pass_interview(
        self, 
        interaction: discord.Interaction, 
        role: discord.Role, 
        amount: int = 10000, 
        target: Optional[discord.Member] = None
    ):
        await interaction.response.defer()

        exclude_role_id = None
        async with self.bot.get_db() as db:
            async with db.execute("SELECT value FROM server_config WHERE key = 'exclude_role_id'") as cursor:
                row = await cursor.fetchone()
                if row:
                    exclude_role_id = int(row['value'])

        targets = []
        skipped_names = []

        if target:
            targets.append(target)
            mode_text = f"{target.mention} を"
        else:
            if interaction.user.voice and interaction.user.voice.channel:
                channel = interaction.user.voice.channel
                raw_members = channel.members
                
                for m in raw_members:
                    if exclude_role_id and any(r.id == exclude_role_id for r in m.roles):
                        skipped_names.append(m.display_name)
                        continue
                    targets.append(m)

                mode_text = f"VC **{channel.name}** のメンバー (除外あり)"
            else:
                return await interaction.followup.send("❌ 対象を指定するか、ボイスチャンネルに参加した状態で実行してください。", ephemeral=True)

        if not targets:
            msg = "❌ 対象となるメンバーがいませんでした。"
            if skipped_names:
                msg += f"\n(除外されたメンバー: {', '.join(skipped_names)})"
            return await interaction.followup.send(msg, ephemeral=True)

        success_members = []
        error_logs = []
        month_tag = datetime.datetime.now().strftime("%Y-%m")

        async with self.bot.get_db() as db:
            try:
                await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance, total_earned) VALUES (0, 0, 0)")

                for member in targets:
                    if member.bot: continue
                    
                    try:
                        if role not in member.roles:
                            await member.add_roles(role, reason="面接通過コマンドによる付与")
                        
                        await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance) VALUES (?, 0)", (member.id,))
                        await db.execute(
                            "UPDATE accounts SET balance = balance + ?, total_earned = total_earned + ? WHERE user_id = ?", 
                            (amount, amount, member.id)
                        )
                        
                        await db.execute(
                            "INSERT INTO transactions (sender_id, receiver_id, amount, type, description, month_tag) VALUES (0, ?, ?, 'BONUS', ?, ?)",
                            (member.id, amount, f"面接通過祝い: {role.name}", month_tag)
                        )
                        
                        success_members.append(member)
                        
                    except discord.Forbidden:
                        error_logs.append(f"⚠️ {member.display_name}: 権限不足でロールを付与できませんでした")
                    except Exception as e:
                        error_logs.append(f"❌ {member.display_name}: エラーが発生しました ({e})")
                        logger.error(f"Interview Command Error [{member.id}]: {e}")
                
                await db.commit()

            except Exception as db_err:
                await db.rollback()
                logger.error(f"Interview Transaction Error: {db_err}")
                return await interaction.followup.send("❌ データベースエラーが発生しました。", ephemeral=True)

        embed = discord.Embed(title="🌸 面接通過処理完了", color=discord.Color.pink())
        embed.add_field(name="対象範囲", value=mode_text, inline=False)
        embed.add_field(name="付与ロール", value=role.mention, inline=True)
        embed.add_field(name="支給額", value=f"{amount:,} L", inline=True)
        
        result_text = f"✅ 成功: {len(success_members)}名"
        if skipped_names:
            result_text += f"\n⛔ 除外(説明者): {len(skipped_names)}名"
            
        embed.add_field(name="処理結果", value=result_text, inline=False)
        if error_logs:
            embed.add_field(name="エラーログ", value="\n".join(error_logs[:5]), inline=False)

        await interaction.followup.send(embed=embed)

        log_ch_id = None
        async with self.bot.get_db() as db:
            async with db.execute("SELECT value FROM server_config WHERE key = 'interview_log_id'") as c:
                row = await c.fetchone()
                if row: log_ch_id = int(row['value'])

        if log_ch_id:
            channel = self.bot.get_channel(log_ch_id)
            if channel:
                log_embed = discord.Embed(title="面接通過 一括結果", color=0xFFD700, timestamp=datetime.datetime.now())
                log_embed.add_field(name="実行者", value=interaction.user.mention, inline=False)
                log_embed.add_field(name="対象数", value=f"{len(targets)}名", inline=True)
                log_embed.add_field(name="成功", value=f"{len(success_members)}名", inline=True)
                log_embed.add_field(name="付与ロール", value=role.mention, inline=False)
                log_embed.add_field(name="付与額", value=f"{amount:,} Ru", inline=False)
                
                success_text = "\n".join([f"・{m.mention} ({m.display_name})" for m in success_members])
                if len(success_text) > 1000:
                    success_text = success_text[:950] + "\n...他多数"
                
                if success_text:
                    log_embed.add_field(name="✅ 合格者一覧", value=success_text, inline=False)
                
                if skipped_names:
                    log_embed.add_field(name="⛔ スキップ(説明者等)", value=", ".join(skipped_names), inline=False)
                
                if error_logs:
                    log_embed.add_field(name="⚠️ エラー", value="\n".join(error_logs[:5]), inline=False)

                await channel.send(embed=log_embed)


# --- 1行サイコロ ---
CYBER_DICE = {
    1: "[ ⚀ ]", 2: "[ ⚁ ]", 3: "[ ⚂ ]",
    4: "[ ⚃ ]", 5: "[ ⚄ ]", 6: "[ ⚅ ]", "?": "[ 🎲 ]"
}

# --- Viewクラス群 ---
class ChinchiroPVPApplyView(discord.ui.View):
    def __init__(self, cog, challenger, opponent, bet):
        super().__init__(timeout=60)
        self.cog = cog
        self.challenger = challenger
        self.opponent = opponent
        self.bet = bet
        self.message = None

    async def on_timeout(self):
        if self.message:
            try:
                for child in self.children: child.disabled = True
                await self.message.edit(content="⏰ 時間切れ。", view=self)
            except: pass

    @discord.ui.button(label="受けて立つ！", style=discord.ButtonStyle.danger, emoji="⚔️")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.opponent:
            return await interaction.response.send_message("関係ない人は触らないで！", ephemeral=True)
        await interaction.response.defer()
        self.stop()
        await self.cog.start_pvp_game(interaction, self.challenger, self.opponent, self.bet)

    @discord.ui.button(label="逃げる", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.opponent: return
        await interaction.response.edit_message(content=f"💨 {self.opponent.display_name} は逃亡しました。", view=None, embed=None)
        self.stop()

class ChinchiroTurnView(discord.ui.View):
    def __init__(self, current_player, turn_count):
        super().__init__(timeout=60)
        self.current_player = current_player
        self.action = None
        if turn_count >= 3:
            for child in self.children:
                if getattr(child, "label", "") == "振り直す":
                    child.disabled = True
                    child.label = "ラストチャンス"
                    child.style = discord.ButtonStyle.danger

    @discord.ui.button(label="確定", style=discord.ButtonStyle.success, emoji="🔒")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.current_player: return
        await interaction.response.defer()
        self.action = "confirm"
        self.stop()

    @discord.ui.button(label="振り直す", style=discord.ButtonStyle.secondary, emoji="🎲")
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.current_player: return
        await interaction.response.defer()
        self.action = "retry"
        self.stop()

# --- Bot本体 ---

class Chinchiro(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last_played = {}
        self.loss_streak = {}

    def get_lumen_dialogue(self, situation, user_name, amount=0):
        is_rare_dere = random.randint(1, 100) == 1

        dialogues = {
            "intro_normal": [
                f"「{user_name}、今日も貢ぎに来たの？」",
                "「準備はいい？ 骨までしゃぶってあげる。」",
                "「ふふ、その怯えた顔…たまらないわね。」"
            ],
            "intro_rich": [
                f"「あら {user_name}様♡ 今日はいくら溶かしてくださるの？」",
                "「素敵な靴ね。私の靴舐める権利、賭けてみる？」"
            ],
            "intro_poor": [
                "「…その小銭で遊ぶ気？ 臭いから寄らないで。」",
                "「時間の無駄よ。出直しなさい。」"
            ],
            "scavenge": [
                "「…惨めね。見てて興奮しちゃう。」",
                "「ほら、拾いなさいよ。地べたがお似合いよ。」",
                "「あはは！ その必死な顔！」"
            ],
            "win": [
                "「チッ…運だけはいいみたいね。」",
                "「…へぇ、やるじゃない。少しは見直してあげる。」",
                "「調子に乗らないでよ？ 次は倍にして奪うから。」"
            ],
            "win_big": [
                "「はぁ！？ …い、イカサマじゃないでしょうね！？」",
                "「くっ…覚えてなさいよ…！ 絶対に取り返すんだから！」"
            ],
            "lose": [
                "「あはは♡ 無様ね！」",
                "「養分ご苦労様♡」",
                "「ねえどんな気持ち？ 大切なお金が消える音、聞こえた？」"
            ],
            "lose_big": [
                "「ゾクゾクするわ…その絶望した顔、最高よ♡」",
                "「もう終わり？ つまらないわね。」"
            ],
            "warning": [
                "「ちょっと、目が血走ってるわよ？」",
                "「手が震えてる。…少し頭冷やしたら？」",
                "「ガツガツしないで。余裕のない男は嫌われるわよ？」"
            ]
        }

        if is_rare_dere:
            return pink(f"「…{user_name}、無理だけはしないでね。…べ、別にあんたの心配なんてしてないわよ！」")

        if situation == "intro":
            if amount >= 1000000: return random.choice(dialogues["intro_rich"])
            if amount < 3000: return random.choice(dialogues["intro_poor"])
            return random.choice(dialogues["intro_normal"])
        
        return random.choice(dialogues.get(situation, dialogues["intro_normal"]))

    def get_roll_result(self):
        dice = [random.randint(1, 6) for _ in range(3)]
        dice.sort()
        
        if dice == [1, 1, 1]: return dice, 111, "【極】ピンゾロ", 5, "🔥 神 降 臨 🔥", True
        if dice[0] == dice[1] == dice[2]: return dice, 100 + dice[0], f"嵐 ({dice[0]})", 3, "💪 激 強", True
        if dice == [4, 5, 6]: return dice, 90, "シゴロ (4-5-6)", 2, "✨ 勝利確定", False
        if dice == [1, 2, 3]: return dice, -1, "ヒフミ (1-2-3)", -2, "💩 倍 払 い", False
        
        if dice[0] == dice[1]: return dice, dice[2], f"{dice[2]} の目", 1, "😐 通 常", False
        if dice[1] == dice[2]: return dice, dice[0], f"{dice[0]} の目", 1, "😐 通 常", False
        if dice[0] == dice[2]: return dice, dice[1], f"{dice[1]} の目", 1, "😐 通 常", False
        
        return dice, 0, "役なし (目なし)", 0, "💀 没収対象", False

    def get_cyber_dice_string(self, dice_list):
        return "  ".join([CYBER_DICE.get(num, CYBER_DICE["?"]) for num in dice_list])

    def render_hud(self, player_name, dice_list, status, color_mode="blue"):
        c_frame = blue
        if color_mode == "red": c_frame = red
        elif color_mode == "gold": c_frame = yellow
        elif color_mode == "pink": c_frame = pink
        
        c_stat_text = white
        if "リーチ" in status: c_stat_text = red
        elif "神" in status: c_stat_text = yellow
        elif "勝利" in status: c_stat_text = yellow

        dice_row = self.get_cyber_dice_string(dice_list)
        dice_centered = dice_row.center(26 - 3)
        
        hud = (
            f"```ansi\n"
            f"{c_frame('┏━━━━━━━━━━━━━━━━━━━━━━━┓')}\n"
            f"{c_frame('┃')} {white(player_name.center(21))} {c_frame('┃')}\n"
            f"{c_frame('┣━━━━━━━━━━━━━━━━━━━━━━━┫')}\n"
            f"{c_frame('┃')} {dice_centered} {c_frame('┃')}\n"
            f"{c_frame('┣━━━━━━━━━━━━━━━━━━━━━━━┫')}\n"
            f"{c_frame('┃')} {c_stat_text(status.center(21))} {c_frame('┃')}\n"
            f"{c_frame('┗━━━━━━━━━━━━━━━━━━━━━━━┛')}\n"
            f"```"
        )
        return hud

    async def play_animation(self, msg, embed, field_idx, player_name, final_dice, rank_text, score, is_super):
        try:
            rand_dice = [random.randint(1,6) for _ in range(3)]
            hud = self.render_hud(player_name, rand_dice, "回転中...", "blue")
            embed.set_field_at(field_idx, name=f"🎲 {player_name}", value=hud, inline=False)
            await msg.edit(embed=embed)
            await asyncio.sleep(0.8)

            if score >= 90 or final_dice[0] == final_dice[1]:
                reach_dice = [final_dice[0], final_dice[1], random.randint(1,6)]
                hud = self.render_hud(player_name, reach_dice, "!!! リーチ !!!", "red")
                embed.set_field_at(field_idx, name=f"⚠️ {player_name} チャンス", value=hud, inline=False)
                await msg.edit(embed=embed)
                await asyncio.sleep(1.0)
            
            res_color = "blue"
            if is_super: res_color = "gold"
            elif score >= 90: res_color = "gold"
            elif score == -1: res_color = "red"
            
            final_hud = self.render_hud(player_name, final_dice, rank_text, res_color)
            embed.set_field_at(field_idx, name=f"🏁 {player_name} (確定)", value=final_hud, inline=False)
            await msg.edit(embed=embed)
        except Exception:
            pass

    async def check_balance(self, user, amount):
        async with self.bot.get_db() as db:
            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (user.id,)) as c:
                row = await c.fetchone()
                return row and row['balance'] >= amount

    @app_commands.command(name="チンチロ", description="ルメンちゃんと勝負。")
    async def chinchiro(self, interaction: discord.Interaction, bet: int):
        if bet < 100: return await interaction.response.send_message("100Ruから。", ephemeral=True)
        
        now = datetime.datetime.now()
        last_time = self.last_played.get(interaction.user.id)
        if last_time:
            delta = (now - last_time).total_seconds()
            if delta < 3.0: 
                warning_msg = self.get_lumen_dialogue("warning", interaction.user.display_name)
                return await interaction.response.send_message(warning_msg, ephemeral=True)
        
        streak = self.loss_streak.get(interaction.user.id, 0)
        if streak >= 6:
            msg = await interaction.response.send_message(f"ルメン「…{streak}連敗中よ？ 頭を冷やしてきなさい。」\n(深呼吸中... ⏳ 5秒)", ephemeral=True)
            await asyncio.sleep(5)
            self.loss_streak[interaction.user.id] = 3
            return

        self.last_played[interaction.user.id] = now

        if not await self.check_balance(interaction.user, bet):
            return await interaction.response.send_message("資金不足。", ephemeral=True)

        await interaction.response.defer()
        
        async with self.bot.get_db() as db:
            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (interaction.user.id,)) as c:
                row = await c.fetchone()
                current_bal = row['balance'] if row else 0

        opening_line = self.get_lumen_dialogue("intro", interaction.user.display_name, current_bal)
        
        embed = discord.Embed(title="🍵 エリュシオン賭博", description=opening_line, color=0x2f3136)
        embed.add_field(name="親：ルメン", value=self.render_hud("ルメン", ["?", "?", "?"], "待機中..."), inline=False)
        embed.add_field(name=f"子：{interaction.user.display_name}", value="準備中...", inline=False)
        msg = await interaction.followup.send(embed=embed)

        p_dice, p_score, p_name, p_mult, p_rank, p_super = self.get_roll_result()
        if p_score == 0:
             p_dice, p_score, p_name, p_mult, p_rank, p_super = self.get_roll_result()

        phud = self.render_hud("ルメン", p_dice, p_name, "gold" if p_super else "blue")
        embed.set_field_at(0, name="親：ルメン (確定)", value=phud, inline=False)
        await msg.edit(embed=embed)
        
        if p_score >= 90:
             return await self.settle_pve(msg, embed, interaction.user, bet, -p_mult if p_mult > 0 else -1)

        u_res = await self.run_player_turn(msg, embed, 1, interaction.user)
        u_score, u_mult = u_res["score"], u_res["mult"]

        final_mult = 1
        if u_score > p_score: 
            final_mult = max(u_mult, abs(p_mult) if p_mult < 0 else 1)
        elif u_score < p_score: 
            final_mult = -max(p_mult, abs(u_mult) if u_mult < 0 else 1)
        else:
            final_mult = 0 
            
        await self.settle_pve(msg, embed, interaction.user, bet, final_mult)

    @app_commands.command(name="チンチロ対戦", description="【PVP】1vs1の心理戦。")
    async def pvp_chinchiro(self, interaction: discord.Interaction, opponent: discord.Member, bet: int):
        if opponent.bot or opponent == interaction.user: return await interaction.response.send_message("対戦相手が必要です。", ephemeral=True)
        if bet < 500: return await interaction.response.send_message("対戦は500Ruから。", ephemeral=True)
        if not await self.check_balance(interaction.user, bet) or not await self.check_balance(opponent, bet):
            return await interaction.response.send_message("どちらかの資金が不足しています。", ephemeral=True)

        view = ChinchiroPVPApplyView(self, interaction.user, opponent, bet)
        await interaction.response.send_message(f"{opponent.mention}！\n{interaction.user.mention} から **{bet:,} Ru** の勝負を挑まれました！", view=view)
        view.message = await interaction.original_response()

    async def start_pvp_game(self, interaction, challenger, opponent, bet):
        embed = discord.Embed(title="⚔️ PVP CHINCHIRO", color=0xff0000)
        hud_1 = self.render_hud(challenger.display_name, ["?", "?", "?"], "待機中...")
        hud_2 = self.render_hud(opponent.display_name, ["?", "?", "?"], "待機中...")
        embed.add_field(name=f"1P: {challenger.display_name}", value=hud_1, inline=False)
        embed.add_field(name=f"2P: {opponent.display_name}", value=hud_2, inline=False)
        
        msg = interaction.message 
        await msg.edit(content=None, embed=embed, view=None)

        c_res = await self.run_player_turn(msg, embed, 0, challenger)
        o_res = await self.run_player_turn(msg, embed, 1, opponent)
        await self.settle_pvp(msg, embed, challenger, opponent, bet, c_res, o_res)

    async def run_player_turn(self, msg, embed, field_idx, player):
        best_res = {"score": -999, "mult": 1, "dice": [1,2,3], "name": "役なし", "is_super": False}
        
        for try_num in range(1, 4):
            dice, score, name, mult, rank, is_super = self.get_roll_result()
            await self.play_animation(msg, embed, field_idx, player.display_name, dice, name, score, is_super)
            
            if score >= 90 or score == -1 or try_num == 3:
                best_res = {"score": score, "mult": mult, "dice": dice, "name": name, "is_super": is_super}
                break
            
            view = ChinchiroTurnView(player, try_num)
            await msg.edit(view=view)
            await view.wait()
            
            if view.action == "confirm":
                best_res = {"score": score, "mult": mult, "dice": dice, "name": name, "is_super": is_super}
                await msg.edit(view=None)
                break
            else:
                await msg.edit(view=None)
                continue
        
        return best_res

    async def settle_pve(self, msg, embed, user, bet, multiplier):
        async with self.bot.get_db() as db:
            if multiplier > 0:
                win_amt = bet * multiplier
                tax = int(win_amt * 0.1)
                final = win_amt - tax
                await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (final, user.id))
                
                self.loss_streak[user.id] = 0
                
                embed.color = 0xffd700
                res_str = f"🎉 **WIN! +{final:,} Ru** (x{multiplier})"
                
                comment_key = "win_big" if multiplier >= 3 else "win"
                comment = self.get_lumen_dialogue(comment_key, user.display_name)
                embed.description = comment

            elif multiplier < 0:
                loss = bet * abs(multiplier)
                async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (user.id,)) as c:
                    curr = (await c.fetchone())['balance']
                actual_loss = min(loss, curr)
                
                await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (actual_loss, user.id))
                
                jackpot_feed = int(actual_loss * 0.05)
                
                if jackpot_feed > 0:
                    await db.execute("""
                        INSERT INTO server_config (key, value) VALUES ('jackpot_pool', ?) 
                        ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + ?
                    """, (jackpot_feed, jackpot_feed))

                self.loss_streak[user.id] = self.loss_streak.get(user.id, 0) + 1

                embed.color = 0xff0000
                res_str = f"💀 **LOSE... -{actual_loss:,} Ru** (x{abs(multiplier)})"
                
                if jackpot_feed > 0:
                     res_str += f"\n(💸 負け額の一部 **{jackpot_feed:,} Ru** がジャックポットへ吸い込まれました...)"

                comment_key = "lose_big" if abs(multiplier) >= 2 else "lose"
                comment = self.get_lumen_dialogue(comment_key, user.display_name)
                embed.description = comment
            
            else:
                embed.color = 0x808080
                res_str = "🤝 **DRAW** (返金)"
                embed.description = "「…つまらないわね。もう一回やる？」"

            await db.commit()
            
        embed.add_field(name="最終結果", value=res_str, inline=False)
        await msg.edit(embed=embed, view=None)
        
    async def settle_pvp(self, msg, embed, p1, p2, bet, r1, r2):
        s1, m1 = r1["score"], r1["mult"]
        s2, m2 = r2["score"], r2["mult"]
        
        winner = None
        payout_mult = 1
        
        if s1 >= s2:
            winner = p1
            loser = p2
            payout_mult = max(m1 if m1 > 0 else 1, abs(m2) if m2 < 0 else 1)
        else:
            winner = p2
            loser = p1
            payout_mult = max(m2 if m2 > 0 else 1, abs(m1) if m1 < 0 else 1)
            
        async with self.bot.get_db() as db:
            total_move = bet * payout_mult
            
            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (loser.id,)) as c:
                l_bal = (await c.fetchone())['balance']
                actual_move = min(total_move, l_bal)
            
            tax = int(actual_move * 0.1)
            prize = actual_move - tax
            
            await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (actual_move, loser.id))
            await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (prize, winner.id))
            await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = 0", (tax,))
            await db.commit()
            
            res_hud = (
                f"```ansi\n"
                f"{yellow('┏━━━━━━━━━━━━━━━━━━━━━━━━━━┓')}\n"
                f"{yellow('┃')}   👑  {white('WINNER')}  👑   {yellow('┃')}\n"
                f"{yellow('┃')}   {blue(winner.display_name.center(20))}   {yellow('┃')}\n"
                f"{yellow('┃')} {green('+' + f'{prize:,}'.center(16) + 'Ru')} {yellow('┃')}\n"
                f"{yellow('┗━━━━━━━━━━━━━━━━━━━━━━━━━━┛')}\n"
                f"```"
            )
            desc = res_hud + f"\n決まり手: **x{payout_mult}** (税: {tax:,})"
            
            embed.title = "🏆 決 着"
            embed.description = desc
            embed.color = 0xffd700
            embed.clear_fields()
            
            embed.add_field(name=f"1P: {p1.display_name}", value=f"{r1['name']} ({r1['score']})", inline=True)
            embed.add_field(name=f"2P: {p2.display_name}", value=f"{r2['name']} ({r2['score']})", inline=True)
            
            await msg.edit(embed=embed, view=None)

    @app_commands.command(name="ゴミ拾い", description="所持金が500Ru以下の時だけ使えます。")
    async def scavenge(self, interaction: discord.Interaction):
        async with self.bot.get_db() as db:
            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (interaction.user.id,)) as c:
                row = await c.fetchone()
                bal = row['balance'] if row else 0
            
            if bal > 500:
                return await interaction.response.send_message("「まだ持ってるでしょ？ 欲張らないで。」", ephemeral=True)
            
            amount = random.randint(500, 1500)
            await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (amount, interaction.user.id))
            await db.commit()
            
            msg_text = self.get_lumen_dialogue("scavenge", interaction.user.display_name)
            
            if random.randint(1, 20) == 1:
                msg_text = f"「…はぁ。仕方ないわね。\nこれ、私が落としたことにしといてあげる。」\n(ルメンがそっぽを向きながら **{amount} Ru** を投げ捨てた！)"

            await interaction.response.send_message(f"{msg_text}\n\n🗑️ 公園で空き缶を拾って **{amount} Ru** になりました。")


class Slot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last_played = {} 
        self.loss_streak = {} 

        self.SYMBOLS = {
            "DIAMOND": "💎",
            "SEVEN":   "7️⃣",
            "WILD":    "🃏",
            "BELL":    "🔔",
            "CHERRY":  "🍒",
            "MISS":    "💨"
        }
        
        self.MODES = {
            "1": { 
                "probs": [
                    ("DIAMOND", 3, 100), ("SEVEN", 50, 20), ("WILD", 100, 10),
                    ("BELL", 800, 5), ("CHERRY", 1800, 2), ("MISS", 7247, 0)
                ], 
                "ceiling": 1000, "name": "設定1 (回収)" 
            },
            "2": { 
                "probs": [
                    ("DIAMOND", 5, 100), ("SEVEN", 60, 20), ("WILD", 120, 10),
                    ("BELL", 850, 5), ("CHERRY", 1900, 2), ("MISS", 7065, 0)
                ], 
                "ceiling": 900, "name": "設定2 (弱回収)" 
            },
            "3": { 
                "probs": [
                    ("DIAMOND", 8, 100), ("SEVEN", 70, 20), ("WILD", 150, 10),
                    ("BELL", 900, 5), ("CHERRY", 2000, 2), ("MISS", 6872, 0)
                ], 
                "ceiling": 800, "name": "設定3 (遊び)" 
            },
            "4": { 
                "probs": [
                    ("DIAMOND", 12, 100), ("SEVEN", 100, 20), ("WILD", 200, 10),
                    ("BELL", 1000, 5), ("CHERRY", 2100, 2), ("MISS", 6588, 0)
                ], 
                "ceiling": 600, "name": "設定4 (通常)" 
            },
            "5": { 
                "probs": [
                    ("DIAMOND", 20, 100), ("SEVEN", 150, 20), ("WILD", 300, 10),
                    ("BELL", 1100, 5), ("CHERRY", 2200, 2), ("MISS", 6230, 0)
                ], 
                "ceiling": 500, "name": "設定5 (優良)" 
            },
            "6": { 
                "probs": [
                    ("DIAMOND", 40, 100), ("SEVEN", 300, 20), ("WILD", 500, 10),
                    ("BELL", 1200, 5), ("CHERRY", 2300, 2), ("MISS", 5660, 0)
                ], 
                "ceiling": 300, "name": "設定6 (極)" 
            },
            "L": { 
                "probs": [
                    ("DIAMOND", 0, 100), ("SEVEN", 0, 20), ("WILD", 0, 10), 
                    ("BELL", 0, 5), ("CHERRY", 500, 2), ("MISS", 9500, 0)
                ], 
                "ceiling": 99999, "name": "設定L (虚無)" 
            }
        }

    def get_lumen_comment(self, situation, **kwargs):
        user = kwargs.get('user', '貴方')
        
        if random.randint(1, 100) == 1:
            return pink(f"「…{user}、あんまり根詰めちゃだめよ。…べ、別に心配なんてしてないけど！」")

        dialogues = {
            "start_normal": [
                "「さあ、回しなさい。運命のレバーを。」",
                "「私のためにRuを増やしてくれるのかしら？」",
                "「…ふふ、いい顔してるわね。」",
                "「今日はどのくらい貢いでくれるの？」"
            ],
            "start_deep": [
                "「…あら、目が血走ってるわよ？ 引くに引けないの？」",
                "「あと少しかもしれないわね…ふふ、地獄の底まで付き合ってあげる。」",
                "「やめないわよね？ ここまで来て逃げるなんて、ありえないもの。」",
                "「泥沼ねぇ…ゾクゾクしちゃう。」"
            ],
            "win_small": [
                "「はい、小銭。」",
                "「チッ…減らないわね。」",
                "「遊びはこれからよ。」",
                "「ま、ジュース代くらいにはなるんじゃない？」"
            ],
            "win_mid": [
                "「あら、やるじゃない。」",
                "「ふん、まぐれよ。」",
                "「…少しは楽しませてくれるのね。」"
            ],
            "win_big": [
                "「…生意気ね。次は全部奪ってやるんだから。」",
                "「7が揃った…ですって…？ 認めないわよ！」",
                "「調子に乗らないでよ？ これは私が貸してあげただけなんだから！」"
            ],
            "win_god": [
                "「あ…あっ♡ …すごい…壊れちゃうっ…///」",
                "「嘘…こんなの…計算外よ…///」",
                "「ぅぅ…負けたわ…今日はあんたの好きにしていいわよ…///」"
            ],
            "lose": [
                "「養分ご苦労様♡」",
                "「あはは！ その絶望した顔、ゾクゾクするわ！」",
                "「ねえ、どんな気持ち？ 大切なお金が消える音。」",
                "「もっと歪んだ顔が見たいわ…♡」"
            ],
            "lumen_save": [
                "「…もう、見てられないわね！ 特別よ！？」",
                "「今回だけなんだからね！ …勘違いしないでよ！」",
                "「チッ…仕方ないわね。私の『権限』で書き換えてあげる。」"
            ],
            "ceiling_hit": [
                "「…はぁ。無様ね。見てられないから当ててあげる。」",
                "「ほら、餌よ。…これでまた地獄へ落ちなさい。」",
                "「私の慈悲に感謝することね。」"
            ]
        }
        return random.choice(dialogues.get(situation, dialogues["start_normal"]))

    async def init_slot_db(self):
        async with self.bot.get_db() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS slot_states (
                    user_id INTEGER PRIMARY KEY,
                    spins_since_win INTEGER DEFAULT 0
                )
            """)
            await db.commit()

    async def get_current_mode(self):
        mode = "4"
        try:
            async with self.bot.get_db() as db:
                async with db.execute("SELECT value FROM server_config WHERE key = 'slot_mode'") as cursor:
                    row = await cursor.fetchone()
                    if row: mode = row['value']
        except: pass
        return mode

    async def spin_slot(self, user_id, mode_key):
        await self.init_slot_db()
        mode_data = self.MODES.get(mode_key, self.MODES["4"])
        ceiling_max = mode_data["ceiling"]
        is_ceiling = False
        current_spins = 0

        async with self.bot.get_db() as db:
            async with db.execute("SELECT spins_since_win FROM slot_states WHERE user_id = ?", (user_id,)) as c:
                row = await c.fetchone()
                current_spins = row['spins_since_win'] if row else 0

            if current_spins >= ceiling_max:
                is_ceiling = True
                outcome_name = "SEVEN" if random.random() < 0.9 else "DIAMOND"
            else:
                outcome_name = "MISS"
                rand = random.randint(1, 10000)
                current_weight = 0
                for name, weight, _ in mode_data["probs"]:
                    current_weight += weight
                    if rand <= current_weight:
                        outcome_name = name
                        break

            payout_mult = 0
            if outcome_name in ["SEVEN", "WILD", "DIAMOND"]:
                new_spins = 0
                for n, _, p in mode_data["probs"]:
                    if n == outcome_name: payout_mult = p
            else:
                new_spins = current_spins + 1
                if outcome_name != "MISS":
                    for n, _, p in mode_data["probs"]:
                        if n == outcome_name: payout_mult = p

            await db.execute("INSERT OR REPLACE INTO slot_states (user_id, spins_since_win) VALUES (?, ?)", (user_id, new_spins))
            await db.commit()

            return outcome_name, payout_mult, is_ceiling, current_spins

    def generate_grid(self, outcome_name, force_reach=False):
        grid = [[self.SYMBOLS["MISS"] for _ in range(3)] for _ in range(3)]
        deco_symbols = [v for k, v in self.SYMBOLS.items() if k != "DIAMOND"]
        for r in range(3):
            for c in range(3):
                grid[r][c] = random.choice(deco_symbols)
        if outcome_name != "MISS":
            sym = self.SYMBOLS[outcome_name]
            grid[1] = [sym, sym, sym]
        else:
            if force_reach or random.random() < 0.15: 
                target = random.choice(list(self.SYMBOLS.values()))
                grid[1] = [target, target, self.SYMBOLS["MISS"]]
            else:
                grid[1][0] = random.choice(deco_symbols)
                grid[1][1] = random.choice([s for s in deco_symbols if s != grid[1][0]])
                grid[1][2] = random.choice(deco_symbols)
        return grid

    def render_slot_screen(self, grid, status_msg="SPINNING...", color_mode="blue"):
        c_frame = blue
        c_text = white
        if color_mode == "red": c_frame = red
        elif color_mode == "gold": c_frame = yellow
        elif color_mode == "black": c_frame = lambda x: f"\x1b[1;30m{x}\x1b[0m"
        elif color_mode == "pink": c_frame = pink
        
        row_top = "   ".join(grid[0])
        row_mid = "   ".join(grid[1])
        row_btm = "   ".join(grid[2])
        screen = (
            f"```ansi\n"
            f"{c_frame('┏━━━━━━━━━━━━━━━━━━━━━━━┓')}\n"
            f"{c_frame('┃')}  {c_text(row_top.center(19))}  {c_frame('┃')}\n"
            f"{c_frame('┣━━━━━━━━━━━━━━━━━━━━━━━┫')} \n"
            f"{c_frame('┃')}▶ {white(row_mid.center(19))} ◀{c_frame('┃')} \n"
            f"{c_frame('┣━━━━━━━━━━━━━━━━━━━━━━━┫')} \n"
            f"{c_frame('┃')}  {c_text(row_btm.center(19))}  {c_frame('┃')}\n"
            f"{c_frame('┗━━━━━━━━━━━━━━━━━━━━━━━┛')}\n"
            f"{c_frame(status_msg.center(25))}\n"
            f"```"
        )
        return screen

    @app_commands.command(name="スロット設定", description="【管理者】スロットの設定を変更します")
    @app_commands.describe(mode="設定値 (1-6, L)")
    @app_commands.default_permissions(administrator=True)
    async def config_slot(self, interaction: discord.Interaction, mode: str):
        if mode not in self.MODES: return await interaction.response.send_message("設定値が無効です。", ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('slot_mode', ?)", (mode,))
            await db.commit()
        await interaction.response.send_message(f"✅ 設定を **{self.MODES[mode]['name']}** に変更しました。", ephemeral=True)

    @app_commands.command(name="スロット", description="さ、引きなさい。")
    @app_commands.describe(bet="賭け金 (100 Ru 〜)")
    async def slot(self, interaction: discord.Interaction, bet: int):
        if bet < 100: return await interaction.response.send_message("100Ruから。", ephemeral=True)

        now = datetime.datetime.now()
        last_time = self.last_played.get(interaction.user.id)
        if last_time and (now - last_time).total_seconds() < 3.5:
            return await interaction.response.send_message("ルメン「目が回るわ…落ち着きなさい。」", ephemeral=True)
        self.last_played[interaction.user.id] = now
        
        streak = self.loss_streak.get(interaction.user.id, 0)
        if streak >= 10:
             await interaction.response.send_message(f"ルメン「…{streak}連敗中よ？ 少し頭を冷やしてきたら？」\n(深呼吸中... ⏳ 5秒)", ephemeral=True)
             await asyncio.sleep(5)
             self.loss_streak[interaction.user.id] = 5
             return

        try:
            await interaction.response.defer()
            user = interaction.user
            async with self.bot.get_db() as db:
                async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (user.id,)) as c:
                    row = await c.fetchone()
                    if not row or row['balance'] < bet:
                        return await interaction.followup.send("ルメン「お金、足りないみたいよ？ 出直してらっしゃい。」")
                await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (bet, user.id))
                await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = 0", (bet,))
                await db.commit()

            current_mode_key = await self.get_current_mode()
            outcome_name, multiplier, is_ceiling_hit, spins_now = await self.spin_slot(user.id, current_mode_key)
            
            is_freeze = (outcome_name == "DIAMOND" and random.random() < 0.33)
            is_respin = (outcome_name in ["WILD", "SEVEN", "DIAMOND"] and random.random() < 0.20)
            
            is_lumen_save = False
            if outcome_name == "MISS" and not is_ceiling_hit:
                if random.random() < 0.001:
                    is_lumen_save = True
                    outcome_name = "SEVEN"
                    multiplier = 20
            
            is_lumen_cutin = False
            
            final_grid = self.generate_grid(outcome_name)
            
            ceiling_max = self.MODES[current_mode_key]["ceiling"]
            is_deep = spins_now >= (ceiling_max * 0.8)

            start_msg = self.get_lumen_comment("start_deep" if is_deep else "start_normal", user=user.display_name)
            if is_ceiling_hit: start_msg = self.get_lumen_comment("ceiling_hit")

            embed = discord.Embed(title="🎰 エリュシオン・スロット", color=0x2f3136)

            if is_freeze:
                await asyncio.sleep(1.0)
                embed.color = 0x000000
                embed.description = "```\n \n \n \n \n```"
                await interaction.followup.send(embed=embed)
                msg = await interaction.original_response()
                await asyncio.sleep(2.5)
                embed.description = "```\n\n     プ チ ュ ン …\n\n```"
                await msg.edit(embed=embed)
                await asyncio.sleep(2.0)
                final_display = final_grid
                flash_col = "gold"
            
            else:
                aura = "purple" if is_deep else "blue"
                status_txt = f"HAMARI: {spins_now}G" if is_deep else "SPINNING..."
                
                embed.description = self.render_slot_screen(self.generate_grid("MISS"), status_txt, aura)
                embed.set_footer(text=f"現在の回転数: {spins_now}G")
                await interaction.followup.send(content=start_msg, embed=embed)
                msg = await interaction.original_response()
                await asyncio.sleep(0.5)

                disp = [row[:] for row in final_grid]
                disp[0], disp[1], disp[2] = ["🌀"]*3, ["🌀"]*3, ["🌀"]*3
                
                disp[1][0] = final_grid[1][0]
                if is_respin or is_lumen_save: 
                     disp[1][0] = self.SYMBOLS["MISS"] if is_lumen_save else final_grid[1][0]
                
                embed.description = self.render_slot_screen(disp, "STOPPING...", aura)
                await msg.edit(embed=embed)
                await asyncio.sleep(0.7)

                disp[1][1] = final_grid[1][1]
                if is_lumen_save: disp[1][1] = self.SYMBOLS["MISS"]

                is_reach = disp[1][0] == disp[1][1]
                
                if is_reach and not is_lumen_save and random.random() < 0.20:
                    is_lumen_cutin = True

                mid_status = "!!!" if is_reach else "..."
                if is_lumen_cutin: mid_status = "LUMEN IS WATCHING..."
                
                mid_color = aura
                if is_reach: mid_color = "red"
                if is_lumen_cutin: mid_color = "pink"

                embed.description = self.render_slot_screen(disp, mid_status, mid_color)
                await msg.edit(embed=embed)
                
                wait_time = 0.5
                if is_reach: wait_time = 1.0
                if is_lumen_cutin: wait_time = 1.5
                await asyncio.sleep(wait_time)

                if is_respin:
                    temp = self.generate_grid("MISS", force_reach=True)
                    temp[1][0], temp[1][1] = final_grid[1][0], final_grid[1][1]
                    embed.description = self.render_slot_screen(temp, "...", aura)
                    await msg.edit(embed=embed)
                    await asyncio.sleep(1.0)
                    revival = self.render_slot_screen(temp, "!!! GLITCH !!!", "red")
                    embed.description = f"{revival}\n🛑 **キュイン！再始動！！** 🛑"
                    await msg.edit(embed=embed)
                    await asyncio.sleep(1.5)
                
                elif is_lumen_save:
                    miss_grid = self.generate_grid("MISS")
                    embed.description = self.render_slot_screen(miss_grid, "LOSE...", "blue")
                    await msg.edit(embed=embed)
                    await asyncio.sleep(1.5)
                    embed.color = 0xff69b4 
                    lumen_txt = self.render_slot_screen(miss_grid, "⚡ LUMEN PANIC ⚡", "pink")
                    save_msg = self.get_lumen_comment("lumen_save")
                    embed.description = f"{lumen_txt}\n{pink(save_msg)}"
                    await msg.edit(embed=embed)
                    await asyncio.sleep(2.0)
                
                final_display = final_grid
                flash_col = "gold" if multiplier > 0 else aura
                if is_lumen_save: flash_col = "pink"

            final_screen = self.render_slot_screen(final_display, "WINNER!!" if multiplier > 0 else "LOSE...", flash_col)
            embed.description = final_screen
            
            if multiplier > 0:
                payout = bet * multiplier
                async with self.bot.get_db() as db:
                    await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (payout, user.id))
                    await db.commit()
                self.loss_streak[user.id] = 0

                if is_lumen_save:
                    comment = "💕 **LUMEN SAVE!!** 💕\n「貸しにしておくわよ！」"
                    color = 0xff69b4
                elif outcome_name == "DIAMOND":
                    comment = self.get_lumen_comment("win_god")
                    color = 0xffffff
                    res_txt = "**PREMIUM JACKPOT**"
                elif outcome_name in ["SEVEN"]:
                    comment = self.get_lumen_comment("win_big")
                    color = 0xffd700
                    res_txt = "**BIG WIN**"
                elif outcome_name in ["WILD"]:
                    comment = self.get_lumen_comment("win_mid")
                    color = 0xff00ff
                    res_txt = "**SUPER WIN**"
                else:
                    comment = self.get_lumen_comment("win_small")
                    color = 0x00ff00
                    res_txt = "**WIN**"

                if is_ceiling_hit:
                    comment = self.get_lumen_comment("ceiling_hit")
                    res_txt += " (天井到達)"

                embed.clear_fields()
                embed.add_field(name=res_txt if 'res_txt' in locals() else "WIN", value=f"**+{payout:,} Ru**", inline=False)
                embed.color = color
            else:
                charge = int(bet * 0.05)
                if charge > 0:
                    async with self.bot.get_db() as db:
                        await db.execute("""
                            INSERT INTO server_config (key, value) VALUES ('jackpot_pool', ?) 
                            ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + ?
                        """, (charge, charge))
                        await db.commit()
                
                self.loss_streak[user.id] = self.loss_streak.get(user.id, 0) + 1
                comment = self.get_lumen_comment("lose")
                embed.color = 0x2f3136
                embed.clear_fields()
                if charge > 0:
                    embed.set_footer(text=f"現在の回転数: {spins_now}G | 負け額の一部はJPへ")

            embed.description += f"\n\n{comment}"
            await msg.edit(content=None, embed=embed)

        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(f"❌ エラー: `{e}`", ephemeral=True)


class ServerStats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if not self.daily_log_task.is_running():
            self.daily_log_task.start()

    def cog_unload(self):
        self.daily_log_task.cancel()

    async def get_citizen_balances(self):
        guild = self.bot.guilds[0]
        if not guild.chunked:
            await guild.chunk()

        async with self.bot.get_db() as db:
            god_role_ids = [r_id for r_id, level in self.bot.config.admin_roles.items() if level == "SUPREME_GOD"]
            citizen_role_id = None
            active_threshold_days = 30
            async with db.execute("SELECT key, value FROM server_config") as cursor:
                async for row in cursor:
                    if row['key'] == 'citizen_role_id': citizen_role_id = int(row['value'])
                    elif row['key'] == 'active_threshold_days': active_threshold_days = int(row['value'])

            cutoff = datetime.datetime.now() - datetime.timedelta(days=active_threshold_days)
            sql = "SELECT DISTINCT sender_id, receiver_id FROM transactions WHERE created_at > ?"
            async with db.execute(sql, (cutoff,)) as cursor:
                rows = await cursor.fetchall()
                active_user_ids = {r[0] for r in rows} | {r[1] for r in rows}

            async with db.execute("SELECT user_id, balance FROM accounts") as cursor:
                user_balances = {row['user_id']: row['balance'] for row in await cursor.fetchall()}

        balances = []
        for member in guild.members:
            if member.bot or any(role.id in god_role_ids for role in member.roles): continue
            if citizen_role_id and not any(role.id == citizen_role_id for role in member.roles): continue
            if member.id not in active_user_ids: continue
            balances.append(user_balances.get(member.id, 0))
        
        return balances, active_threshold_days

    @tasks.loop(hours=24)
    async def daily_log_task(self):
        try:
            balances, _ = await self.get_citizen_balances()
            total = sum(balances)
            async with self.bot.get_db() as db:
                await db.execute("CREATE TABLE IF NOT EXISTS daily_stats (date TEXT PRIMARY KEY, total_balance INTEGER)")
                await db.execute("INSERT OR REPLACE INTO daily_stats (date, total_balance) VALUES (?, ?)", 
                                 (datetime.datetime.now().strftime("%Y-%m-%d"), total))
                await db.commit()
        except Exception as e:
            logger.error(f"Daily Log Error: {e}")

    @app_commands.command(name="経済グラフ", description="【管理者】詳細な格差判定と経済レポートを表示")
    @has_permission("ADMIN")
    async def economy_graph(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        try:
            balances, active_days = await self.get_citizen_balances()
            current_total = sum(balances)
            citizen_count = len(balances)
            
            gini_val = 0.0
            if balances and current_total > 0:
                s_bal = sorted(balances)
                n = len(balances)
                gini_val = (2 * sum((i + 1) * v for i, v in enumerate(s_bal)) / (n * current_total)) - (n + 1) / n

            async with self.bot.get_db() as db:
                await db.execute("""CREATE TABLE IF NOT EXISTS last_stats_report (
                    id INTEGER PRIMARY KEY, total_balance INTEGER, gini_val REAL, timestamp DATETIME
                )""")
                cutoff_24h = datetime.datetime.now() - datetime.timedelta(days=1)
                async with db.execute("SELECT COUNT(*) FROM transactions WHERE created_at > ?", (cutoff_24h,)) as c:
                    tx_count = (await c.fetchone())[0]
                async with db.execute("SELECT total_balance, gini_val, timestamp FROM last_stats_report WHERE id = 1") as c:
                    last_report = await c.fetchone()
                async with db.execute("SELECT date, total_balance FROM daily_stats ORDER BY date ASC") as c:
                    history = await c.fetchall()

            if gini_val < 0.20: gini_lv, gini_color = "🕊️ ユートピア", 0x00ffff
            elif gini_val < 0.30: gini_lv, gini_color = "🟢 平穏", 0x00ff00
            elif gini_val < 0.40: gini_lv, gini_color = "🟡 普通", 0xffff00
            elif gini_val < 0.50: gini_lv, gini_color = "🟠 警戒", 0xffa500
            elif gini_val < 0.60: gini_lv, gini_color = "🔴 危険", 0xff4500
            else: gini_lv, gini_color = "💀 崩壊", 0x000000

            if last_report:
                diff_total = current_total - last_report['total_balance']
                rate = (diff_total / last_report['total_balance'] * 100) if last_report['total_balance'] > 0 else 0
                inflation_text = f"{'📈' if diff_total >= 0 else '📉'} **{diff_total:+,} L** ({rate:+.2f}%)"
                diff_gini = gini_val - last_report['gini_val']
                gini_trend = "🔺拡大" if diff_gini > 0.005 else "🔻改善" if diff_gini < -0.005 else "➡️維持"
            else:
                inflation_text = "🔰 初回データ"; gini_trend = "ー"

            async with self.bot.get_db() as db:
                await db.execute("""INSERT OR REPLACE INTO last_stats_report (id, total_balance, gini_val, timestamp) 
                                 VALUES (1, ?, ?, ?)""", (current_total, gini_val, datetime.datetime.now()))
                await db.commit()

            plt.figure(figsize=(10, 5))
            try:
                dates = [r['date'][5:] for r in history]
                totals = [r['total_balance'] for r in history]
                plt.plot(dates, totals, marker='o', color='#00b0f4', linewidth=2)
                plt.title('Economy Growth History'); plt.grid(True, alpha=0.3)
                buf = io.BytesIO(); plt.savefig(buf, format='png'); buf.seek(0)
                file = discord.File(buf, filename="economy_graph.png")
            finally:
                plt.close()

            activity_ratio = tx_count / max(1, citizen_count)
            tx_comment = "🔥 過熱" if activity_ratio >= 1.0 else "🏃 活発" if activity_ratio >= 0.5 else "🚶 微動"
            
            embed = discord.Embed(title="📊 ルーメン経済レポート", color=gini_color, timestamp=datetime.datetime.now())
            embed.add_field(name="🔄 活発度", value=f"{tx_comment} ({activity_ratio:.2f} tx/人)", inline=False)
            embed.add_field(name="💹 資産総額変化", value=inflation_text, inline=True)
            embed.add_field(name="⚖️ 格差レベル", value=f"**{gini_lv}** ({gini_trend})\n係数: `{gini_val:.3f}`", inline=True)
            embed.add_field(name=f"💰 アクティブ総資産 ({citizen_count}名)", value=f"**{current_total:,} L**", inline=False)
            embed.set_image(url="attachment://economy_graph.png")
            
            await interaction.followup.send(embed=embed, file=file)

        except Exception as e:
            logger.error(f"Economy Graph Error: {e}")
            await interaction.followup.send(f"❌ レポート生成失敗: {e}")


class ShopPurchaseView(discord.ui.View):
    def __init__(self, bot, role_id, price, shop_id):
        super().__init__(timeout=None)
        self.bot = bot
        self.role_id = role_id
        self.price = price
        self.shop_id = shop_id

    @discord.ui.button(label="このロールを購入する (30日間)", style=discord.ButtonStyle.green, emoji="🛒")
    async def buy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        user = interaction.user
        role = interaction.guild.get_role(self.role_id)

        if not role:
            return await interaction.followup.send("❌ この商品は現在取り扱われていません。", ephemeral=True)

        if role in user.roles:
            return await interaction.followup.send(f"✅ すでに **{role.name}** を持っています。\n期限切れになってから再度購入してください。", ephemeral=True)

        async with self.bot.get_db() as db:
            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (user.id,)) as cursor:
                row = await cursor.fetchone()
                balance = row['balance'] if row else 0

            if balance < self.price:
                return await interaction.followup.send(f"❌ お金が足りません。\n(価格: {self.price:,} L / 所持金: {balance:,} L)", ephemeral=True)

            try:
                await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (self.price, user.id))
                
                month_tag = datetime.datetime.now().strftime("%Y-%m")
                await db.execute(
                    "INSERT INTO transactions (sender_id, receiver_id, amount, type, description, month_tag) VALUES (?, 0, ?, 'SHOP', ?, ?)",
                    (user.id, self.price, f"購入: {role.name} (Shop: {self.shop_id})", month_tag)
                )

                expiry_date = datetime.datetime.now() + datetime.timedelta(days=30)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS shop_subscriptions (
                        user_id INTEGER,
                        role_id INTEGER,
                        expiry_date TEXT,
                        PRIMARY KEY (user_id, role_id)
                    )
                """)
                await db.execute(
                    "INSERT OR REPLACE INTO shop_subscriptions (user_id, role_id, expiry_date) VALUES (?, ?, ?)",
                    (user.id, role.id, expiry_date.strftime("%Y-%m-%d %H:%M:%S"))
                )
                
                await db.commit()

            except Exception as e:
                await db.rollback()
                return await interaction.followup.send(f"❌ エラーが発生しました: {e}", ephemeral=True)

        try:
            await user.add_roles(role, reason=f"ショップ購入({self.shop_id})")
            expiry_str = expiry_date.strftime('%Y/%m/%d')
            await interaction.followup.send(f"🎉 **購入完了！**\n**{role.name}** を購入しました。\n有効期限: **{expiry_str}** まで\n(-{self.price:,} L)", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("⚠️ 購入処理は完了しましたが、権限不足でロールを付与できませんでした。", ephemeral=True)


# --- 商品選択メニュー ---
class ShopSelect(discord.ui.Select):
    def __init__(self, bot, items, shop_id):
        self.bot = bot
        self.shop_id = shop_id
        options = []
        for item in items:
            role = item['role_obj']
            price = item['price']
            desc = item['desc'] or "説明なし"
            options.append(discord.SelectOption(
                label=f"{role.name} ({price:,} L)",
                description=f"[30日] {desc}"[:90], 
                value=str(role.id),
                emoji="🏷️"
            ))
        super().__init__(placeholder="購入したい商品を選択してください...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        role_id = int(self.values[0])
        price = 0
        
        async with self.bot.get_db() as db:
            async with db.execute("SELECT price FROM shop_items WHERE role_id = ? AND shop_id = ?", (str(role_id), self.shop_id)) as cursor:
                row = await cursor.fetchone()
                if row: price = row['price']
        
        view = ShopPurchaseView(self.bot, role_id, price, self.shop_id)
        role = interaction.guild.get_role(role_id)
        
        embed = discord.Embed(title="🛒 購入確認 (30日レンタル)", description=f"以下のロールを購入しますか？", color=role.color)
        embed.add_field(name="商品名", value=role.mention, inline=False)
        embed.add_field(name="価格", value=f"**{price:,} L** / 30日間", inline=False)
        embed.add_field(name="有効期限", value="購入日から30日間（自動解除）", inline=False)
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class ShopPanelView(discord.ui.View):
    def __init__(self, bot, items, shop_id):
        super().__init__(timeout=None)
        self.add_item(ShopSelect(bot, items, shop_id))


# --- Cog本体 ---
class ShopSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_subscription_expiry.start()

    def cog_unload(self):
        self.check_subscription_expiry.cancel()

    @tasks.loop(hours=1)
    async def check_subscription_expiry(self):
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        expired_rows = []
        async with self.bot.get_db() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS shop_subscriptions (
                    user_id INTEGER,
                    role_id INTEGER,
                    expiry_date TEXT,
                    PRIMARY KEY (user_id, role_id)
                )
            """)
            async with db.execute("SELECT user_id, role_id FROM shop_subscriptions WHERE expiry_date < ?", (now_str,)) as cursor:
                expired_rows = await cursor.fetchall()
        
        if not expired_rows: return

        guild = self.bot.guilds[0]
        async with self.bot.get_db() as db:
            for row in expired_rows:
                user_id = row['user_id']
                role_id = row['role_id']
                member = guild.get_member(user_id)
                role = guild.get_role(role_id)
                
                if member and role:
                    try:
                        if role in member.roles:
                            await member.remove_roles(role, reason="ショップ有効期限切れ")
                            try:
                                await member.send(f"⏳ **有効期限切れ**\nロール **{role.name}** の有効期限（30日）が終了しました。")
                            except: pass
                    except: pass
                
                await db.execute("DELETE FROM shop_subscriptions WHERE user_id = ? AND role_id = ?", (user_id, role_id))
            await db.commit()

    @check_subscription_expiry.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()


    # ▼▼▼ 1. 商品登録 ▼▼▼
    @app_commands.command(name="ショップ_商品登録", description="【最高神】ショップにロールを出品します")
    @app_commands.rename(shop_id="ショップid", role="商品ロール", price="価格", description="説明文")
    @app_commands.describe(
        shop_id="配置するショップのID（例: main, dark など。好きな英数字）",
        role="販売するロール",
        price="30日間の価格 (Lumen)",
        description="商品の説明文（パネルに表示されます）"
    )
    @has_permission("SUPREME_GOD")
    async def shop_add(self, interaction: discord.Interaction, shop_id: str, role: discord.Role, price: int, description: str = None):
        await interaction.response.defer(ephemeral=True)
        if price < 0: return await interaction.followup.send("価格は0以上にしてください。", ephemeral=True)

        async with self.bot.get_db() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS shop_items (
                    role_id TEXT,
                    shop_id TEXT,
                    price INTEGER,
                    description TEXT,
                    PRIMARY KEY (role_id, shop_id)
                )
            """)
            await db.execute(
                "INSERT OR REPLACE INTO shop_items (role_id, shop_id, price, description) VALUES (?, ?, ?, ?)",
                (str(role.id), shop_id, price, description)
            )
            await db.commit()
            
        await interaction.followup.send(f"✅ ショップ(`{shop_id}`) に **{role.name}** ({price:,} L) を登録しました。", ephemeral=True)


    # ▼▼▼ 2. 商品削除 ▼▼▼
    @app_commands.command(name="ショップ_商品削除", description="【最高神】ショップから商品を取り下げます")
    @app_commands.rename(shop_id="ショップid", role="削除ロール")
    @app_commands.describe(shop_id="削除したい商品があるショップID", role="削除するロール")
    @has_permission("SUPREME_GOD")
    async def shop_remove(self, interaction: discord.Interaction, shop_id: str, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("DELETE FROM shop_items WHERE role_id = ? AND shop_id = ?", (str(role.id), shop_id))
            await db.commit()
        await interaction.followup.send(f"🗑️ ショップ(`{shop_id}`) から **{role.name}** を削除しました。", ephemeral=True)


    # ▼▼▼ 3. パネル設置 ▼▼▼
    @app_commands.command(name="ショップ_パネル設置", description="【最高神】指定したIDのショップパネルを設置します")
    @app_commands.rename(shop_id="ショップid", title="タイトル", content="本文", image_url="画像url")
    @app_commands.describe(
        shop_id="表示するショップID（登録時に決めたもの）", 
        title="パネルのタイトル", 
        content="パネルの本文（説明文）", 
        image_url="画像のURL（あれば）"
    )
    @has_permission("SUPREME_GOD")
    async def shop_panel(self, interaction: discord.Interaction, shop_id: str, title: str = "🛒 ルーメンショップ", content: str = "欲しいロールを選択してください！", image_url: str = None):
        await interaction.response.defer()
        
        items = []
        async with self.bot.get_db() as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS shop_items (
                    role_id TEXT,
                    shop_id TEXT,
                    price INTEGER,
                    description TEXT,
                    PRIMARY KEY (role_id, shop_id)
                )
            """)
            async with db.execute("SELECT * FROM shop_items WHERE shop_id = ?", (shop_id,)) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    role = interaction.guild.get_role(int(row['role_id']))
                    if role:
                        items.append({'role_obj': role, 'price': row['price'], 'desc': row['description']})
        
        if not items:
            return await interaction.followup.send(f"❌ ショップID `{shop_id}` には商品が登録されていません。\n先に `/ショップ_商品登録` で商品を登録してください。", ephemeral=True)

        embed = discord.Embed(title=title, description=content, color=discord.Color.gold())
        if image_url: embed.set_image(url=image_url)
        
        embed.add_field(name="💳 システム", value="30日間の買い切り制\n(期限が来ると自動解除)", inline=False)
        
        item_list_text = ""
        for item in items:
            item_list_text += f"• **{item['role_obj'].mention}**: `{item['price']:,} L`\n"
        embed.add_field(name="📦 商品ラインナップ", value=item_list_text, inline=False)

        view = ShopPanelView(self.bot, items, shop_id)
        await interaction.followup.send(embed=embed, view=view)

# --- 3. 管理者ツール (修正版) ---
class AdminTools(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ログ出力先決定", description="各ログの出力先を設定します")
    @app_commands.choices(log_type=[
        discord.app_commands.Choice(name="通貨ログ (送金など)", value="currency_log_id"),
        discord.app_commands.Choice(name="給与ログ (一斉支給)", value="salary_log_id"),
        discord.app_commands.Choice(name="面接ログ (合格通知)", value="interview_log_id")
    ])
    @has_permission("SUPREME_GOD")
    async def config_log_channel(self, interaction: discord.Interaction, log_type: str, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES (?, ?)", (log_type, str(channel.id)))
            await db.commit()
        await self.bot.config.reload()
        await interaction.followup.send(f"✅ **{channel.mention}** をログ出力先に設定しました。", ephemeral=True)

    @app_commands.command(name="面接の除外ロール設定", description="【最高神】面接コマンドでスキップするロール（説明者など）を設定")
    @has_permission("SUPREME_GOD")
    async def config_exclude_role(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('exclude_role_id', ?)", (str(role.id),))
            await db.commit()
        await self.bot.config.reload()
        await interaction.followup.send(f"✅ 面接時に **{role.name}** を持つメンバーを除外（スキップ）するように設定しました。", ephemeral=True)

    @app_commands.command(name="管理者権限設定", description="【オーナー用】管理権限ロールを登録・更新します")
    async def config_set_admin(self, interaction: discord.Interaction, role: discord.Role, level: str):
        await interaction.response.defer(ephemeral=True)
        if not await self.bot.is_owner(interaction.user):
            return await interaction.followup.send("オーナーのみ実行可能です。", ephemeral=True)
        
        valid_levels = ["SUPREME_GOD", "GODDESS", "ADMIN"]
        if level not in valid_levels:
             return await interaction.followup.send(f"レベルは {valid_levels} のいずれかである必要があります。", ephemeral=True)

        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO admin_roles (role_id, perm_level) VALUES (?, ?)", (role.id, level))
            await db.commit()
        await self.bot.config.reload()
        await interaction.followup.send(f"✅ {role.mention} を `{level}` に設定しました。", ephemeral=True)

    @app_commands.command(name="給与額設定", description="【最高神】役職ごとの給与額を設定します")
    @has_permission("SUPREME_GOD")
    async def config_set_wage(self, interaction: discord.Interaction, role: discord.Role, amount: int):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO role_wages (role_id, amount) VALUES (?, ?)", (role.id, amount))
            await db.commit()
        await self.bot.config.reload()
        await interaction.followup.send(f"✅ 設定を更新しました。", ephemeral=True)

    @app_commands.command(name="vc報酬追加", description="【最高神】報酬対象のVCを追加します")
    @has_permission("SUPREME_GOD")
    async def add_reward_vc(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR IGNORE INTO reward_channels (channel_id) VALUES (?)", (channel.id,))
            await db.commit()
        
        vc_cog = self.bot.get_cog("VoiceSystem")
        if vc_cog: await vc_cog.reload_targets()
        await interaction.followup.send(f"✅ {channel.mention} を報酬対象に追加しました。", ephemeral=True)

    @app_commands.command(name="vc報酬解除", description="【最高神】報酬対象のVCを解除します")
    @has_permission("SUPREME_GOD")
    async def remove_reward_vc(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("DELETE FROM reward_channels WHERE channel_id = ?", (channel.id,))
            await db.commit()

        vc_cog = self.bot.get_cog("VoiceSystem")
        if vc_cog: await vc_cog.reload_targets()
        await interaction.followup.send(f"🗑️ {channel.mention} を報酬対象から除外しました。", ephemeral=True)

    @app_commands.command(name="vc報酬リスト", description="【最高神】報酬対象のVC一覧を表示します")
    @has_permission("SUPREME_GOD")
    async def list_reward_vcs(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            async with db.execute("SELECT channel_id FROM reward_channels") as cursor:
                rows = await cursor.fetchall()
        
        if not rows: return await interaction.followup.send("報酬対象のVCは設定されていません。", ephemeral=True)
        channels_text = "\n".join([f"• <#{row['channel_id']}>" for row in rows])
        embed = discord.Embed(title="🎙 報酬対象VC一覧", description=channels_text, color=discord.Color.green())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="経済集計ロール付与", description="【最高神】経済統計の対象とする「市民ロール」を設定します")
    @has_permission("SUPREME_GOD")
    async def config_citizen_role(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('citizen_role_id', ?)", (str(role.id),))
            await db.commit()
        await self.bot.config.reload()
        await interaction.followup.send(f"✅ 経済統計の対象を **{role.name}** を持つメンバーに限定しました。", ephemeral=True)

    @app_commands.command(name="経済集計アクティブ判定期間", description="【最高神】経済統計に含める「アクティブ期間（日数）」を設定します")
    @app_commands.describe(days="この日数以内に取引がない人は、市民ロールを持っていても計算から除外されます（推奨: 30）")
    @has_permission("SUPREME_GOD")
    async def config_active_days(self, interaction: discord.Interaction, days: int):
        await interaction.response.defer(ephemeral=True)
        if days < 1:
            return await interaction.followup.send("❌ 1日以上を設定してください。", ephemeral=True)
            
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('active_threshold_days', ?)", (str(days),))
            await db.commit()
        await self.bot.config.reload()
        await interaction.followup.send(f"✅ 過去 **{days}日間** に取引がないメンバーを、経済統計から除外するように設定しました。", ephemeral=True)


# --- Bot 本体 ---
class LumenBankBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.voice_states = True
        intents.message_content = True
        
        super().__init__(
            command_prefix="!", 
            intents=intents,
            help_command=None
        )
        
        self.db_path = "lumen_bank_v4.db"
        self.db_manager = BankDatabase(self.db_path)
        self.config = ConfigManager(self)

    @contextlib.asynccontextmanager
    async def get_db(self):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("PRAGMA busy_timeout = 5000")
            yield db

    async def setup_hook(self):
        async with self.get_db() as db:
            await self.db_manager.setup(db)
            # ジャックポット用
            await db.execute("""CREATE TABLE IF NOT EXISTS jackpot_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                ticket_id TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""")
            # 統計レポート用
            await db.execute("""CREATE TABLE IF NOT EXISTS last_stats_report (
                id INTEGER PRIMARY KEY, 
                total_balance INTEGER, 
                gini_val REAL, 
                timestamp DATETIME
            )""")
            await db.commit()
        
        await self.config.reload()
        
        if 'VCPanel' in globals():
            self.add_view(VCPanel())
        
        await self.add_cog(Economy(self))
        await self.add_cog(Salary(self))
        await self.add_cog(AdminTools(self))
        await self.add_cog(ServerStats(self))
        await self.add_cog(ShopSystem(self))
        
        await self.add_cog(VoiceSystem(self))
        await self.add_cog(PrivateVCManager(self))
        await self.add_cog(VoiceHistory(self))
        await self.add_cog(InterviewSystem(self))
        
        await self.add_cog(Chinchiro(self))
        await self.add_cog(Jackpot(self))
        await self.add_cog(Slot(self))
        await self.add_cog(Omikuji(self))
        
        if not self.backup_db_task.is_running():
            self.backup_db_task.start()
        
        await self.tree.sync()
        logger.info("LumenBank System: Setup complete and All Cogs Synced.")

    async def send_bank_log(self, log_key: str, embed: discord.Embed):
        """
        指定されたキー（currency_log_id, salary_log_id 等）の設定を読み込み、
        対応するチャンネルへログを送信します。
        """
        async with self.get_db() as db:
            async with db.execute("SELECT value FROM server_config WHERE key = ?", (log_key,)) as c:
                row = await c.fetchone()
                if row:
                    try:
                        channel_id = int(row['value'])
                        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
                        if channel:
                            await channel.send(embed=embed)
                    except Exception as e:
                        logger.error(f"Log Send Error ({log_key}): {e}")

    @tasks.loop(hours=24)
    async def backup_db_task(self):
        import shutil
        import datetime
        backup_name = f"backup_{datetime.datetime.now().strftime('%Y%m%d')}.db"
        try:
            shutil.copy2(self.db_path, backup_name)
            logger.info(f"Auto Backup Success: {backup_name}")
        except Exception as e:
            logger.error(f"Backup Failure: {e}")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print("--- Lumen Bank System Online ---")

# --- 実行ブロック ---
if __name__ == "__main__":
    if not TOKEN:
        logging.error("DISCORD_TOKEN is missing")
    else:
        # ボットの起動
        bot = LumenBankBot()
        bot.run(TOKEN)
