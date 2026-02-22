import discord
# import keep_alive # 必要な場合はコメントアウトを外してください
import matplotlib
matplotlib.use('Agg') # サーバー上でグラフを描くための設定
import matplotlib.pyplot as plt
import io
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

# numpyは必須ではないが、あれば使う設定
try:
    import numpy as np
except ImportError:
    np = None

# keep_aliveの安全なインポート
try:
    import keep_alive
except ImportError:
    keep_alive = None

GEKIATSU = "<:b_069:1438962326463054008>" # 必要であればこの絵文字IDも変更してください

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
    'stella_bank.log',
    maxBytes=5*1024*1024,
    backupCount=3,
    encoding='utf-8'
)
file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
logger = logging.getLogger('StellaBank')
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
    def __init__(self, db_path="stella_bank_v1.db"):
        self.db_path = db_path

    async def setup(self, conn):
        # 高速化設定
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        # ▼ 追加: 外部キー制約を有効化（これを入れないとREFERENCESが機能しません）
        await conn.execute("PRAGMA foreign_keys = ON") 

        # 1. 口座・取引
        await conn.execute("""CREATE TABLE IF NOT EXISTS accounts (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 0 CHECK(balance >= 0), 
            total_earned INTEGER DEFAULT 0
        )""")

        # ▼▼▼ 追加: これがないと「システム(ID:0)」からの送金でエラーになります ▼▼▼
        await conn.execute("INSERT OR IGNORE INTO accounts (user_id, balance, total_earned) VALUES (0, 0, 0)")
        # ▲▲▲ ここまで ▲▲▲

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
        
        
        # ▼ 月間対応の新しいテーブルを作成
        await conn.execute("""CREATE TABLE IF NOT EXISTS voice_stats (
            user_id INTEGER, 
            month TEXT, 
            total_seconds INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, month)
        )""")
        
        await conn.execute("CREATE TABLE IF NOT EXISTS voice_tracking (user_id INTEGER PRIMARY KEY, join_time TEXT)")
        
        # ▼ 抜け落ちていた部分（元のまま残します）
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
        
        # 5. ショップ・スロット・統計
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shop_items (
                role_id TEXT,
                shop_id TEXT,
                price INTEGER,
                description TEXT,
                item_type TEXT DEFAULT 'rental',
                max_per_user INTEGER DEFAULT 0,
                PRIMARY KEY (role_id, shop_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shop_subscriptions (
                user_id INTEGER,
                role_id INTEGER,
                expiry_date TEXT,
                PRIMARY KEY (user_id, role_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ticket_inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                shop_id TEXT,
                item_key TEXT,
                item_name TEXT,
                purchased_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                used_at DATETIME,
                used_by INTEGER
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS lottery_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                number INTEGER
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS slot_states (
                user_id INTEGER PRIMARY KEY,
                spins_since_win INTEGER DEFAULT 0
            )
        """)
        await conn.execute("CREATE TABLE IF NOT EXISTS daily_stats (date TEXT PRIMARY KEY, total_balance INTEGER)")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_issuers (
                user_id INTEGER PRIMARY KEY,
                total_shares INTEGER DEFAULT 0,
                is_listed INTEGER DEFAULT 1
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_holdings (
                user_id INTEGER,
                issuer_id INTEGER,
                amount INTEGER,
                avg_cost REAL,
                PRIMARY KEY (user_id, issuer_id)
            )
        """)
        await conn.execute("CREATE TABLE IF NOT EXISTS market_config (key TEXT PRIMARY KEY, value TEXT)")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_play_counts (
                user_id INTEGER,
                game TEXT,
                date TEXT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, game, date)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_play_exemptions (
                user_id INTEGER,
                game TEXT,
                date TEXT,
                PRIMARY KEY (user_id, game, date)
            )
        """)
        await conn.commit()

# --- UI: VC内操作パネル ---
class VCControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="招待するメンバーを選択...", min_values=1, max_values=10, row=0, custom_id="vc_invite_select")
    async def invite_users(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        
        channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel):
            return await interaction.followup.send("❌ ここはボイスチャンネルではありません。", ephemeral=True)

        perms = discord.PermissionOverwrite(
            view_channel=True, connect=True, speak=True, stream=True,
            use_voice_activation=True, send_messages=True, read_message_history=True
        )

        added_users = []
        for member in select.values:
            if member.bot: continue
            await channel.set_permissions(member, overwrite=perms)
            added_users.append(member.display_name)

        if not added_users:
            return await interaction.followup.send("❌ 招待できるメンバーがいませんでした。", ephemeral=True)

        await interaction.followup.send(f"✅ 以下のメンバーを招待しました:\n{', '.join(added_users)}", ephemeral=True)
        await channel.send(f"👋 {interaction.user.mention} が {', '.join([m.mention for m in select.values if not m.bot])} を招待しました。")

    @discord.ui.button(label="メンバーの権限を剥奪(追放)", style=discord.ButtonStyle.danger, row=1, custom_id="vc_kick_btn")
    async def kick_user_menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = RemoveUserView()
        await interaction.response.send_message("権限を剥奪するメンバーを選んでください。", view=view, ephemeral=True)


class RemoveUserView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="権限を剥奪するメンバーを選択...", min_values=1, max_values=10, custom_id="vc_remove_select")
    async def remove_users(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel

        removed_names = []
        for member in select.values:
            if member.id == interaction.user.id: continue
            if member.bot: continue
            await channel.set_permissions(member, overwrite=None)
            if member.voice and member.voice.channel and member.voice.channel.id == channel.id:
                await member.move_to(None)
            removed_names.append(member.display_name)

        if removed_names:
            await interaction.followup.send(f"🚫 以下のメンバーの権限を剥奪しました:\n{', '.join(removed_names)}", ephemeral=True)
        else:
            await interaction.followup.send("❌ 対象を選択してください（自分自身は削除できません）。", ephemeral=True)


# --- UI: プラン選択メニュー ---
class PlanSelect(discord.ui.Select):
    def __init__(self, prices: dict):
        self.prices = prices
        options = [
            discord.SelectOption(label="6時間プラン",  description=f"{prices.get('6',  5000):,} Stell - ちょっとした作業や会議に", value="6",  emoji="🕐"),
            discord.SelectOption(label="12時間プラン", description=f"{prices.get('12', 10000):,} Stell - 半日じっくり",             value="12", emoji="🕓"),
            discord.SelectOption(label="24時間プラン", description=f"{prices.get('24', 30000):,} Stell - 丸一日貸切",               value="24", emoji="🕛"),
        ]
        super().__init__(placeholder="利用プランを選択してください...", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        user = interaction.user
        bot = interaction.client

        # ★修正①: 孤立レコードをクリーンアップしてから既存チェック
        async with bot.get_db() as db:
            async with db.execute("SELECT channel_id FROM temp_vcs WHERE owner_id = ?", (user.id,)) as cursor:
                existing = await cursor.fetchone()

            if existing:
                # チャンネルが実際に存在するか確認
                real_channel = bot.get_channel(existing['channel_id'])
                if real_channel is None:
                    # 実在しない → 孤立レコードなので削除してOK
                    await db.execute("DELETE FROM temp_vcs WHERE owner_id = ?", (user.id,))
                    await db.commit()
                else:
                    return await interaction.followup.send("❌ あなたは既に一時VCを作成しています。", ephemeral=True)

        hours = int(self.values[0])
        price = self.prices.get(str(hours), 5000)

        async with bot.get_db() as db:
            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (user.id,)) as cursor:
                row = await cursor.fetchone()
                current_bal = row['balance'] if row else 0

            if current_bal < price:
                return await interaction.followup.send(
                    f"❌ 残高不足です。\n必要: {price:,} Stell / 所持: {current_bal:,} Stell", ephemeral=True
                )

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
            await interaction.followup.send(
                f"✅ 作成完了: {new_vc.mention}\n期限: {expire_dt.strftime('%m/%d %H:%M')}\n招待機能はチャンネル内のパネルを使用してください。",
                ephemeral=True
            )

        except Exception as e:
            logger.error(f"VC Create Error: {e}")
            # ★VC作成失敗したら引き落とした分を返金
            async with bot.get_db() as db:
                await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (price, user.id))
                await db.commit()
            await interaction.followup.send("❌ VC作成中にエラーが発生しました。料金を返金しました。", ephemeral=True)


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

        if '6'  not in prices: prices['6']  = 30000
        if '12' not in prices: prices['12'] = 50000
        if '24' not in prices: prices['24'] = 80000

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
                async with db.execute("SELECT channel_id, guild_id FROM temp_vcs") as cursor:
                    all_vcs = await cursor.fetchall()

                if not all_vcs: return

                for row in all_vcs:
                    c_id = row['channel_id']
                    channel = self.bot.get_channel(c_id)

                    # ★修正①: チャンネルが存在しない（手動削除済み）or 期限切れ → どちらも削除
                    if channel is None:
                        await db.execute("DELETE FROM temp_vcs WHERE channel_id = ?", (c_id,))
                    else:
                        async with db.execute("SELECT expire_at FROM temp_vcs WHERE channel_id = ?", (c_id,)) as c:
                            rec = await c.fetchone()
                        if rec:
                            expire_at = datetime.datetime.fromisoformat(str(rec['expire_at']))
                            if now >= expire_at:
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

    @app_commands.command(name="一時vcパネル作成", description="内容をカスタマイズしてVC作成パネルを設置します")
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
        title: str = "アパホテル",
        description: str = None,
        price_6h: int = 5000,
        price_12h: int = 10000,
        price_24h: int = 30000
    ):
        await interaction.response.defer(ephemeral=True)

        if description is None:
            description = (
                "権限のある人以外からは見えない、プライベートな一時VCを作成できます。ようこそアパホテルへ\n\n"
                "**🔒 プライバシー**\n招待した人以外は見えません\n"
                "**🛡 料金システム**\n作成時に自動引き落とし\n"
                f"**⏰ 料金プラン**\n"
                f"• **6時間**: {price_6h:,} Stell\n"
                f"• **12時間**: {price_12h:,} Stell\n"
                f"• **24時間**: {price_24h:,} Stell"
            )
        else:
            description = description.replace("\\n", "\n")

        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('vc_price_6', ?)",  (str(price_6h),))
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
                await interaction.edit_original_response(content=f"✅ {self.receiver.mention} へ {self.amount:,} Stell 送金しました。", embed=None, view=None)

                try:
                    notify = True
                    async with db.execute("SELECT dm_salary_enabled FROM user_settings WHERE user_id = ?", (self.receiver.id,)) as c:
                        res = await c.fetchone()
                        if res and res['dm_salary_enabled'] == 0: notify = False
                    
                    if notify:
                        embed = discord.Embed(title="💰 Stell受取通知", color=discord.Color.green())
                        embed.add_field(name="送金者", value=self.sender.mention, inline=False)
                        embed.add_field(name="受取額", value=f"**{self.amount:,} Stell**", inline=False)
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
                        log_embed.add_field(name="金額", value=f"**{self.amount:,} Stell**", inline=True)
                        log_embed.add_field(name="備考", value=self.msg, inline=True)
                        log_embed.add_field(name="処理後残高", value=f"送: {sender_new_bal:,} Stell\n受: {receiver_new_bal:,} Stell", inline=False)
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

# --- Cog: Economy (残高・送金・ランキング・資金操作) ---
class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Botの応答速度を確認します")
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
        
        embed = discord.Embed(title="🏛 ステラ銀行 口座照会", color=0xFFD700)
        embed.set_author(name=f"{target.display_name} 様", icon_url=target.display_avatar.url)
        embed.add_field(name="💰 現在の残高", value=f"**{bal:,} Stell**", inline=False)
        embed.set_footer(text=f"Stella Economy System")
        embed.set_thumbnail(url=target.display_avatar.url)
        
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="送金", description="他のユーザーにStellを送金します")
    @app_commands.describe(receiver="送金相手", amount="送金額", message="相手へのメッセージ（任意）")
    async def transfer(self, interaction: discord.Interaction, receiver: discord.Member, amount: int, message: str = "送金"):
        if amount <= 0: return await interaction.response.send_message("❌ 1 Stell 以上を指定してください。", ephemeral=True)
        if amount > 10000000: return await interaction.response.send_message("❌ 1回の送金上限は 10,000,000 Stell です。", ephemeral=True)
        if receiver.id == interaction.user.id: return await interaction.response.send_message("❌ 自分自身には送金できません。", ephemeral=True)
        if receiver.bot: return await interaction.response.send_message("❌ Botには送金できません。", ephemeral=True)

        embed = discord.Embed(title="⚠️ 送金確認", description="以下の内容で送金しますか？", color=discord.Color.orange())
        embed.add_field(name="👤 送金先", value=receiver.mention, inline=True)
        embed.add_field(name="💰 金額", value=f"**{amount:,} Stell**", inline=True)
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
            amount_str = f"{'-' if is_sender else '+'}{r['amount']:,} Stell"
            
            target_id = r['receiver_id'] if is_sender else r['sender_id']
            target_name = f"<@{target_id}>" if target_id != 0 else "システム"

            embed.add_field(
                name=f"{r['created_at'][5:16]} | {emoji}",
                value=f"金額: **{amount_str}**\n相手: {target_name}\n内容: `{r['description']}`",
                inline=False
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="今日の残り回数", description="今日のギャンブル残り回数を確認します")
    async def check_remaining(self, interaction: discord.Interaction):
        # ↓ ここから下の行は、すべて半角スペース4つ分（またはTab1回分）右にズラす
        _, remaining_chinchiro = await check_daily_limit(self.bot, interaction.user.id, "chinchiro")
        _, remaining_slot = await check_daily_limit(self.bot, interaction.user.id, "slot")

        embed = discord.Embed(title="🎲 本日のギャンブル残り回数", color=0x2b2d31)
        embed.add_field(
            name="🎲 チンチロ",
            value=f"残り **{min(remaining_chinchiro, 10)} / 10** 回" if remaining_chinchiro < 99999 else "✨ 制限解除中",
            inline=True
        )
        embed.add_field(
            name="🎰 スロット",
            value=f"残り **{min(remaining_slot, 10)} / 10** 回" if remaining_slot < 99999 else "✨ 制限解除中",
            inline=True
        )
        embed.set_footer(text="制限は毎日0時にリセットされます")
        await interaction.response.send_message(embed=embed, ephemeral=True)


    # === 追加機能1: 所持金ランキング ===
    @app_commands.command(name="ランキング", description="サーバー内の大富豪トップ10を表示します")
    async def ranking(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        async with self.bot.get_db() as db:
            # システムアカウント(ID:0)を除外し、残高が多い順に取得 (退出者やBotを飛ばせるように少し多めに取得)
            async with db.execute("SELECT user_id, balance FROM accounts WHERE user_id != 0 ORDER BY balance DESC LIMIT 30") as cursor:
                rows = await cursor.fetchall()

        if not rows:
            return await interaction.followup.send("まだデータがありません。")

        embed = discord.Embed(title="🏆 ステラ長者番付 トップ10", color=0xFFD700)
        embed.description = "サーバー内の大富豪ランキングです。\n\n"
        
        rank = 1
        for row in rows:
            if rank > 10: break
            
            member = interaction.guild.get_member(row['user_id'])
            # 退出済みのメンバーやBotはランキングから除外
            if not member or member.bot:
                continue
            
            medal = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else f"**{rank}.**"
            embed.description += f"{medal} **{member.display_name}**\n┗ 💰 **{row['balance']:,} Stell**\n\n"
            rank += 1

        embed.set_footer(text=f"実行者: {interaction.user.display_name} | Top 10 Richest Citizens")
        await interaction.followup.send(embed=embed)

    # === 追加機能2: 資金の直接操作 ===
    @app_commands.command(name="資金操作", description="【最高神】指定したユーザーの所持金を直接増減させます")
    @app_commands.describe(
        target="操作対象のユーザー",
        action="増やすか、減らすか",
        amount="金額",
        reason="理由（ログに残ります）"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="➕ 増やす (Mint)", value="add"),
        app_commands.Choice(name="➖ 減らす (Burn)", value="remove")
    ])
    @has_permission("SUPREME_GOD")
    async def manipulate_funds(self, interaction: discord.Interaction, target: discord.Member, action: str, amount: int, reason: str = "システム操作"):
        if amount <= 0:
            return await interaction.response.send_message("❌ 1以上の金額を指定してください。", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        month_tag = datetime.datetime.now().strftime("%Y-%m")

        async with self.bot.get_db() as db:
            # 対象の口座が存在しない場合は作成
            await db.execute("""
                INSERT INTO accounts (user_id, balance, total_earned) VALUES (?, 0, 0)
                ON CONFLICT(user_id) DO NOTHING
            """, (target.id,))

            if action == "add":
                # 資金追加
                await db.execute("UPDATE accounts SET balance = balance + ?, total_earned = total_earned + ? WHERE user_id = ?", (amount, amount, target.id))
                # ログ追加 (システム(0)から対象へ)
                await db.execute("""
                    INSERT INTO transactions (sender_id, receiver_id, amount, type, description, month_tag)
                    VALUES (0, ?, ?, 'SYSTEM_ADD', ?, ?)
                """, (target.id, amount, f"【運営付与】{reason}", month_tag))
                msg = f"✅ {target.mention} に **{amount:,} Stell** を付与しました。\n理由: `{reason}`"
            
            else:
                # 資金削減 (現在の残高を取得してマイナスにならないよう調整)
                async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (target.id,)) as c:
                    row = await c.fetchone()
                    current_bal = row['balance'] if row else 0
                
                actual_deduction = min(amount, current_bal)
                
                await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (actual_deduction, target.id))
                # ログ追加 (対象からシステム(0)へ)
                await db.execute("""
                    INSERT INTO transactions (sender_id, receiver_id, amount, type, description, month_tag)
                    VALUES (?, 0, ?, 'SYSTEM_REMOVE', ?, ?)
                """, (target.id, actual_deduction, f"【運営没収】{reason}", month_tag))
                
                msg = f"✅ {target.mention} から **{actual_deduction:,} Stell** を没収しました。\n理由: `{reason}`"

            await db.commit()
            
        # 通貨ログチャンネルに通知を送る
        embed = discord.Embed(title="⚙️ 運営資金操作ログ", color=0xff0000 if action == "remove" else 0x00ff00)
        embed.add_field(name="対象", value=target.mention, inline=True)
        embed.add_field(name="操作", value="➕ 付与" if action == "add" else "➖ 没収", inline=True)
        embed.add_field(name="金額", value=f"**{amount:,} S**" if action == "add" else f"**{actual_deduction:,} S**", inline=True)
        embed.add_field(name="理由", value=reason, inline=False)
        embed.add_field(name="実行者", value=interaction.user.mention, inline=False)
        embed.timestamp = datetime.datetime.now()

        log_ch_id = None
        async with self.bot.get_db() as db:
            async with db.execute("SELECT value FROM server_config WHERE key = 'currency_log_id'") as c:
                row = await c.fetchone()
                if row: log_ch_id = int(row['value'])
        if log_ch_id:
            channel = self.bot.get_channel(log_ch_id)
            if channel: await channel.send(embed=embed)

        await interaction.followup.send(msg, ephemeral=True)

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

    @app_commands.command(name="通貨通知設定", description="通貨交換時のDM明細通知をON/OFFします")
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
        
        msg = "✅ 今後、お金の明細は **DMで通知されます**。" if status == 1 else "🔕 今後、給与明細の **DM通知は行われません**。"
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="一括給与", description="全役職の給与を合算支給し、明細をDM送信します")
    @has_permission("SUPREME_GOD")
    async def distribute_all(self, interaction: discord.Interaction):
        # 処理が長引く可能性があるため、タイムアウトを回避（最大15分猶予）
        await interaction.response.defer()
        
        now = datetime.datetime.now()
        month_tag = now.strftime("%Y-%m")
        batch_id = str(uuid.uuid4())[:8]
        
        # --- 1. データ準備 ---
        wage_dict = {}
        dm_prefs = {}
        async with self.bot.get_db() as db:
            async with db.execute("SELECT role_id, amount FROM role_wages") as c:
                async for r in c: wage_dict[int(r['role_id'])] = int(r['amount'])
            async with db.execute("SELECT user_id, dm_salary_enabled FROM user_settings") as c:
                async for r in c: dm_prefs[int(r['user_id'])] = bool(r['dm_salary_enabled'])

        if not wage_dict:
            return await interaction.followup.send("⚠️ 給与設定が見つかりません。")
        
        # メンバーリスト取得
        members = interaction.guild.members if interaction.guild.chunked else [m async for m in interaction.guild.fetch_members()]

        # --- 2. 計算処理（メモリ上で処理） ---
        count = 0
        total_payout = 0
        role_summary = {}
        payout_data_list = []

        # DB一括書き込み用のリスト
        account_updates = []
        transaction_inserts = []

        for member in members:
            if member.bot: continue
            
            matching = [(wage_dict[r.id], r) for r in member.roles if r.id in wage_dict]
            if not matching: continue
            
            member_total = sum(w for w, _ in matching)
            
            # DB書き込み用データをリストに追加 (SQLのパラメータ順に合わせる)
            # accounts: user_id, balance, total_earned
            account_updates.append((member.id, member_total, member_total))
            
            # transactions: sender, receiver, amount, type, batch_id, month, desc
            transaction_inserts.append((
                0, member.id, member_total, 'SALARY', batch_id, month_tag, f"{month_tag} 給与"
            ))

            count += 1
            total_payout += member_total
            
            # 集計用ロジック
            for w, r in matching:
                if r.id not in role_summary: role_summary[r.id] = {"mention": r.mention, "count": 0, "amount": 0}
                role_summary[r.id]["count"] += 1
                role_summary[r.id]["amount"] += w

            if dm_prefs.get(member.id, True):
                payout_data_list.append((member, member_total, matching))

        # --- 3. DB一括書き込み (高速化の肝) ---
        if account_updates:
            async with self.bot.get_db() as db:
                try:
                    # executemanyを使って1回の通信で全員分書き込む
                    await db.executemany("""
                        INSERT INTO accounts (user_id, balance, total_earned) VALUES (?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET 
                        balance = balance + excluded.balance, total_earned = total_earned + excluded.total_earned
                    """, account_updates)

                    await db.executemany("""
                        INSERT INTO transactions (sender_id, receiver_id, amount, type, batch_id, month_tag, description)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, transaction_inserts)

                    await db.commit()
                except Exception as e:
                    await db.rollback()
                    return await interaction.followup.send(f"❌ DBエラーが発生しました: {e}")
        else:
             return await interaction.followup.send("⚠️ 給与対象者がいませんでした。")

        # --- 4. DM送信 (レート制限対策付き) ---
        sent_dm = 0
        for m, total, matching in payout_data_list:
            try:
                embed = self.create_salary_slip_embed(m, total, matching, month_tag)
                await m.send(embed=embed)
                sent_dm += 1
                # Discord APIのレート制限（BAN）回避のため、5件ごとに1秒休む
                if sent_dm % 5 == 0: 
                    await asyncio.sleep(1) 
            except:
                pass

        await interaction.followup.send(f"💰 **一括支給完了** (ID: `{batch_id}`)\n人数: {count}名 / 総額: {total_payout:,} Stell\n通知送信: {sent_dm}名")
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
        
        embed.add_field(name="💵 支給総額", value=f"**{total:,} Stell**", inline=False)
        
        formula = " + ".join([f"{w:,}" for w, r in sorted_matching])
        embed.add_field(name="🧮 計算式", value=f"{formula} = **{total:,} Stell**", inline=False)
        
        breakdown = "\n".join([f"{i+1}. {r.name}: {w:,} Stell" for i, (w, r) in enumerate(sorted_matching)])
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
            text += f"{role_str}: **{row['amount']:,} Stell**\n"
        
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
        embed.add_field(name="総額 / 人数", value=f"**{total:,} Stell** / {count}名", inline=True)
        
        breakdown_text = "\n".join([f"✅ {d['mention']}: {d['amount']:,} Stell ({d['count']}名)" for d in breakdown.values()])
        if breakdown_text:
            embed.add_field(name="ロール別内訳", value=breakdown_text, inline=False)
        
        embed.set_footer(text=f"BatchID: {batch_id}")
        await channel.send(embed=embed)

class Jackpot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.code_price = 5000
        self.pool_addition = 3000   # 5000のうち、金庫に入る額
        self.stella_pocket = 2000   # 5000のうち、消滅する額（インフレ対策）
        self.stella_tax_rate = 0.20 # 当選時のステラの手数料（20%回収）
        self.limit_per_round = 30
        self.max_number = 999
        self.seed_money = 300000    # 初期資金（100万から30万に減額してインフレ抑制）

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

    @app_commands.command(name="金庫状況", description="ステラの秘密の金庫の状況と、所持している解除コードを確認します")
    async def status(self, interaction: discord.Interaction):
        await self.init_db()
        
        async with self.bot.get_db() as db:
            async with db.execute("SELECT value FROM server_config WHERE key = 'jackpot_pool'") as c:
                row = await c.fetchone()
                pool = int(row['value']) if row else self.seed_money

            async with db.execute("SELECT number FROM lottery_tickets WHERE user_id = ? ORDER BY number", (interaction.user.id,)) as c:
                my_codes = await c.fetchall()
                my_numbers = [f"{row['number']:03d}" for row in my_codes]

            async with db.execute("SELECT COUNT(*) as total FROM lottery_tickets") as c:
                sold_count = (await c.fetchone())['total']

        embed = discord.Embed(title="🔐 ステラの秘密の金庫", color=0xff00ff)
        embed.description = (
            "「ふふっ、私の裏金庫が気になるの？ どうせあんたたちには開けられないわよ♡」\n\n"
            "3桁のハッキングコード(000-999)が正解と一致すれば、金庫の中身を強奪！\n"
            "失敗した場合は**全額キャリーオーバー**されます。\n"
        )
        
        embed.add_field(name="💰 現在の保管額", value=f"**{pool:,} Stell**", inline=False)
        embed.add_field(name="💻 発行済みコード数", value=f"{sold_count:,} 個", inline=True)
        embed.add_field(name="📅 ロック解除確率", value="1 / 1000", inline=True)

        if my_numbers:
            code_str = ", ".join(my_numbers)
            if len(code_str) > 500: code_str = code_str[:500] + "..."
            embed.add_field(name=f"🔑 あなたの解除コード ({len(my_numbers)}個)", value=f"`{code_str}`", inline=False)
        else:
            embed.add_field(name="🔑 あなたの解除コード", value="未所持", inline=False)
        
        embed.set_footer(text=f"コード代({self.code_price}S)のうち、{self.stella_pocket}Sはステラのお小遣いとして消滅します")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="ハッキングコード生成", description="金庫の解除コードを生成します (1回 5,000 Stell)")
    @app_commands.describe(amount="生成回数")
    async def buy(self, interaction: discord.Interaction, amount: int):
        if amount <= 0: return await interaction.response.send_message("1回以上指定してください。", ephemeral=True)
        
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        total_cost = self.code_price * amount
        total_pool_add = self.pool_addition * amount
        total_burn = self.stella_pocket * amount

        async with self.bot.get_db() as db:
            async with db.execute("SELECT COUNT(*) as count FROM lottery_tickets WHERE user_id = ?", (user.id,)) as c:
                current_count = (await c.fetchone())['count']
                if current_count + amount > self.limit_per_round:
                    return await interaction.followup.send(f"ステラ「ちょっと、ガッツきすぎよ！ 上限は {self.limit_per_round}回 までだからね！」\n(残り: {self.limit_per_round - current_count}回)", ephemeral=True)

            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (user.id,)) as c:
                row = await c.fetchone()
                if not row or row['balance'] < total_cost:
                    return await interaction.followup.send("ステラ「…お金ないじゃん。貧乏人は帰って。」", ephemeral=True)

            try:
                # ユーザーからお金を引き落とし
                await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (total_cost, user.id))
                
                # プール追加分のみ金庫へ。残りの burn 分はどこにも足さず「消滅（インフレ対策）」させる
                await db.execute("""
                    INSERT INTO server_config (key, value) VALUES ('jackpot_pool', ?) 
                    ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + ?
                """, (total_pool_add, total_pool_add))

                new_codes = []
                my_numbers = []
                for _ in range(amount):
                    num = random.randint(0, self.max_number)
                    new_codes.append((user.id, num))
                    my_numbers.append(f"{num:03d}")
                
                await db.executemany("INSERT INTO lottery_tickets (user_id, number) VALUES (?, ?)", new_codes)
                await db.commit()

                num_display = ", ".join(my_numbers)
                msg = (
                    f"ステラ「はい、ハッキングコードよ。どうせ当たらないんだから無駄遣いね♡\n"
                    f"（小声）ふふっ、{total_burn:,} Stell は私のお小遣いっと…♪」\n\n"
                    f"✅ **{amount}個** 生成しました！\n獲得コード: `{num_display}`\n"
                    f"(購入代金のうち、金庫に **{total_pool_add:,} S** 追加されました)"
                )
                await interaction.followup.send(msg, ephemeral=True)

            except Exception as e:
                await db.rollback()
                traceback.print_exc()
                await interaction.followup.send("❌ システムエラーが発生しました。", ephemeral=True)

    @app_commands.command(name="金庫解除", description="【管理者】金庫のロック解除処理を実行します")
    @app_commands.describe(panic_release="Trueの場合、発行済みコードの中から強制的に正解を選びます(特大還元祭)")
    @app_commands.default_permissions(administrator=True)
    async def draw(self, interaction: discord.Interaction, panic_release: bool = False):
        await interaction.response.defer()
        
        async with self.bot.get_db() as db:
            async with db.execute("SELECT value FROM server_config WHERE key = 'jackpot_pool'") as c:
                row = await c.fetchone()
                current_pool = int(row['value']) if row else self.seed_money
                if current_pool < self.seed_money: current_pool = self.seed_money

        winning_number = random.randint(0, self.max_number)
        winners = []
        is_panic = False

        async with self.bot.get_db() as db:
            if panic_release:
                async with db.execute("SELECT user_id, number FROM lottery_tickets") as c:
                    all_sold = await c.fetchall()
                if not all_sold: return await interaction.followup.send("⚠️ コードが一つも生成されていません。")
                
                is_panic = True
                lucky = random.choice(all_sold)
                winning_number = lucky['number']
                winners = [t for t in all_sold if t['number'] == winning_number]
            else:
                async with db.execute("SELECT user_id FROM lottery_tickets WHERE number = ?", (winning_number,)) as c:
                    winners = await c.fetchall()

            winning_str = f"{winning_number:03d}"
            
            embed = discord.Embed(title="🚨 ステラ金庫 ハッキング判定", color=0xffd700)
            embed.add_field(name="🎯 正解コード", value=f"<h1>**{winning_str}**</h1>", inline=False)

            if len(winners) > 0:
                # 【インフレ対策】ステラの手数料天引き (消滅するお金)
                stella_tax = int(current_pool * self.stella_tax_rate)
                actual_prize_pool = current_pool - stella_tax
                
                prize_per_winner = actual_prize_pool // len(winners)
                winner_mentions = []
                for w in winners:
                    uid = w['user_id']
                    await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (prize_per_winner, uid))
                    winner_mentions.append(f"<@{uid}>")
                
                # プールを初期資金(30万)にリセット
                await db.execute("UPDATE server_config SET value = ? WHERE key = 'jackpot_pool'", (str(self.seed_money),))

                await db.execute("DELETE FROM lottery_tickets")
                await db.commit()

                desc = f"ステラ「う、嘘でしょ！？ 私の金庫が…開けられた！？\n……し、しょーがないわね。ヘソクリにしてた分 {self.stella_tax_rate*100}%({stella_tax:,} S) は私が頂くから！」"
                if is_panic: desc = f"ステラ「ちょ、ちょっとシステムエラー！？ なんで勝手に開いてるのよ！！ 泥棒ー！！\nせ、せめて次の競馬代 {self.stella_tax_rate*100}%({stella_tax:,} S) だけでも確保しなきゃ…！」\n🚨 **パニック・リリース発動！強制放出！** 🚨"
                
                embed.description = f"{desc}\n\n🎉 **{len(winners)}名** のハッカーが金庫破りに成功しました！"
                embed.add_field(name="💰 1人あたりの獲得額", value=f"**{prize_per_winner:,} Stell** (手数料引抜き後)", inline=False)
                
                mentions = " ".join(list(set(winner_mentions)))
                if len(mentions) > 1000: mentions = f"{len(winners)}名の当選者"
                embed.add_field(name="🏆 成功者一覧", value=mentions, inline=False)
                
                embed.set_footer(text=f"金庫の残高はシステムによって{self.seed_money:,} Stellにリセットされました。")
                embed.color = 0xff00ff 

            else:
                await db.execute("DELETE FROM lottery_tickets")
                await db.commit()
                embed.description = "ステラ「あーっはっは！ ざぁこ♡ 誰一人開けられないじゃない！ このお金はぜーんぶ私のものね！」\n\n💀 **金庫破り失敗...**"
                embed.add_field(name="💸 キャリーオーバー", value=f"現在の **{current_pool:,} Stell** は次回に持ち越されます！", inline=False)
                embed.color = 0x2f3136

        await interaction.followup.send(content="@everyone", embed=embed)



# --- 色定義 ---
def ansi(text, color_code): return f"\x1b[{color_code}m{text}\x1b[0m"
def gold(t): return ansi(t, "1;33")
def red(t): return ansi(t, "1;31")
def green(t): return ansi(t, "1;32")
def pink(t): return ansi(t, "1;35")
def gray(t): return ansi(t, "1;30")
def blue(t): return ansi(t, "1;34")
def yellow(t): return ansi(t, "1;33")
def white(t): return ansi(t, "1;37")

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

    @app_commands.command(name="おみくじ", description="ステラちゃんが今日の運勢を占います (1回 300 Stell)")
    async def omikuji(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user = interaction.user

        async with self.bot.get_db() as db:
            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (user.id,)) as c:
                row = await c.fetchone()
                if not row or row['balance'] < self.cost:
                    return await interaction.followup.send("ステラ「300Stellすら持ってないの？ 帰って。」", ephemeral=True)

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

        res_str = f"**{payout} Stell** (収支: {profit:+d} Stell)"
        if profit < 0:
             res_str += f"\n(💸 負け分の20%はJP賞金へ)"

        embed.description = f"{draw_txt}\n{result['msg']}\n\n{res_str}"
        embed.set_footer(text=f"{user.display_name} の運勢")

        await interaction.followup.send(embed=embed)
        
# --- Cog: VoiceSystem (改良版) ---
class VoiceSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.target_vc_ids = set() 
        self.is_ready_processed = False
        self.locks = {} # ユーザーごとのロック {user_id: asyncio.Lock()}
        self.reward_rate = 50 # 基本レート (Stell/分)

    def get_lock(self, user_id):
        if user_id not in self.locks:
            self.locks[user_id] = asyncio.Lock()
        return self.locks[user_id]

    async def reload_targets(self):
        try:
            async with self.bot.get_db() as db:
                # 報酬対象VCの読み込み
                async with db.execute("SELECT channel_id FROM reward_channels") as cursor:
                    rows = await cursor.fetchall()
                self.target_vc_ids = {row['channel_id'] for row in rows}
                
                # 報酬レートの読み込み (設定がなければデフォルト50)
                async with db.execute("SELECT value FROM server_config WHERE key = 'vc_reward_rate'") as cursor:
                    row = await cursor.fetchone()
                    if row: self.reward_rate = int(row['value'])
            
            logger.info(f"Loaded {len(self.target_vc_ids)} reward VCs. Rate: {self.reward_rate}/min")
        except Exception as e:
            logger.error(f"Failed to load voice config: {e}")

    # インフレ対策コマンド: 報酬レートの変更
    @app_commands.command(name="vc報酬レート設定", description="VC報酬の基本レート(1分あたり)を変更します")
    @has_permission("ADMIN")
    async def set_vc_rate(self, interaction: discord.Interaction, amount: int):
        if amount < 0: return await interaction.response.send_message("❌ 0以上にしてください。", ephemeral=True)
        
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('vc_reward_rate', ?)", (str(amount),))
            await db.commit()
        
        self.reward_rate = amount
        await interaction.response.send_message(f"✅ VC報酬レートを **{amount} Stell / 分** に変更しました。\n(インフレ時は下げ、キャンペーン時は上げてください)", ephemeral=True)

    def is_active(self, state):
        # 判定強化: サーバーミュート/自己ミュート/サーバー拒否/自己拒否 すべてチェック
        return (
            state and 
            state.channel and 
            state.channel.id in self.target_vc_ids and  
            not state.self_deaf and not state.deaf and # 聞けない状態はNG
            not state.self_mute and not state.mute     # ★追加: 喋れない状態(ミュート)もNGにするならこれを入れる
        )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot: return
        
        # ロックを取得して同時実行を防ぐ
        async with self.get_lock(member.id):
            now = datetime.datetime.now()
            was_active, is_now_active = self.is_active(before), self.is_active(after)

            # 入室 (または条件達成)
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

            # 退室 (または条件未達)
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
                        # 設定されたレートを使って計算
                        # reward_rate は "1分あたりの額" なので、秒数にかけて 60 で割る
                        reward = int(self.reward_rate * (sec / 60))

                    if reward > 0:
                        month_tag = now.strftime("%Y-%m")
                        
                        
                        await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance, total_earned) VALUES (0, 0, 0)")
                        await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance, total_earned) VALUES (?, 0, 0)", (user_id,))
                        
                        await db.execute(
                            "UPDATE accounts SET balance = balance +?, total_earned = total_earned +? WHERE user_id =?", 
                            (reward, reward, user_id)
                        )
                        
                        # ▼▼ ここから ▼▼
                        await db.execute(
                            "INSERT OR IGNORE INTO voice_stats (user_id, month, total_seconds) VALUES (?, ?, 0)", 
                            (user_id, month_tag)
                        )
                        await db.execute(
                            "UPDATE voice_stats SET total_seconds = total_seconds + ? WHERE user_id = ? AND month = ?", 
                            (sec, user_id, month_tag)
                        )
                    # reward=0でも追跡レコードは必ず消す（★修正②）
                    await db.execute("DELETE FROM voice_tracking WHERE user_id = ?", (user_id,))
                    await db.commit()  # ★修正①: commitを追加


                except Exception as db_err:
                    await db.rollback()
                    raise db_err

        except Exception as e:
            logger.error(f"Voice Reward Process Error [{user_id}]: {e}")

    # (on_ready は元のまま)
    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready_processed: return
        self.is_ready_processed = True
        await self.reload_targets()
        
class VoiceHistory(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="vc記録", description="今月のVC累計滞在時間を確認します")
    @app_commands.describe(
        member="確認したいユーザー（省略すると自分）",
        role="このロールを持つ全員の一覧を表示（管理者専用）"
    )
    async def vc_history(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
        role: Optional[discord.Role] = None
    ):
        await interaction.response.defer(ephemeral=True)

        current_month = datetime.datetime.now().strftime("%Y-%m")
        is_admin = await interaction.client.is_owner(interaction.user) or any(
            r.id in interaction.client.config.admin_roles and
            interaction.client.config.admin_roles[r.id] in ["SUPREME_GOD", "GODDESS"]
            for r in interaction.user.roles
        )

        # --- ロール指定（管理者専用） ---
        if role is not None:
            if not is_admin:
                return await interaction.followup.send("❌ ロール指定は管理者のみ使用できます。", ephemeral=True)

            targets = [m for m in role.members if not m.bot]
            if not targets:
                return await interaction.followup.send(f"❌ {role.mention} にメンバーがいません。", ephemeral=True)

            async with self.bot.get_db() as db:
                async with db.execute(
                    "SELECT user_id, total_seconds FROM voice_stats WHERE month = ?",
                    (current_month,)
                ) as cursor:
                    rows = await cursor.fetchall()
                    vc_data = {r['user_id']: r['total_seconds'] for r in rows}

            # 時間順にソート
            results = sorted(
                [(m, vc_data.get(m.id, 0)) for m in targets],
                key=lambda x: x[1],
                reverse=True
            )

            embed = discord.Embed(
                title=f"📊 VC滞在記録一覧 ({current_month})",
                description=f"ロール: {role.mention} ({len(targets)}名)",
                color=0x7289da
            )

            lines = []
            for i, (m, sec) in enumerate(results):
                h = sec // 3600
                mins = (sec % 3600) // 60
                rank = f"`{i+1}.`"
                lines.append(f"{rank} **{m.display_name}** ── {h}時間 {mins}分")

            # embedの文字数制限対策で分割
            chunk = ""
            for line in lines:
                if len(chunk) + len(line) > 1000:
                    embed.add_field(name="\u200b", value=chunk, inline=False)
                    chunk = ""
                chunk += line + "\n"
            if chunk:
                embed.add_field(name="\u200b", value=chunk, inline=False)

            embed.set_footer(text=f"Requested by {interaction.user.display_name}")
            return await interaction.followup.send(embed=embed, ephemeral=True)

        # --- ユーザー個別 ---
        # 他人を見ようとしたら管理者チェック
        target = member or interaction.user
        if target.id != interaction.user.id and not is_admin:
            return await interaction.followup.send("❌ 他のユーザーの記録を見る権限がありません。", ephemeral=True)

        async with self.bot.get_db() as db:
            async with db.execute(
                "SELECT total_seconds FROM voice_stats WHERE user_id = ? AND month = ?",
                (target.id, current_month)
            ) as cursor:
                row = await cursor.fetchone()
                total_seconds = row['total_seconds'] if row else 0

        h = total_seconds // 3600
        mins = (total_seconds % 3600) // 60
        sec = total_seconds % 60

        embed = discord.Embed(
            title=f"🎙️ VC滞在記録 ({current_month})",
            color=0x7289da
        )
        embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
        embed.add_field(name="⏱️ 今月の累計", value=f"**{h}時間 {mins}分 {sec}秒**", inline=False)
        embed.add_field(name="📐 合計秒数", value=f"{total_seconds:,} 秒", inline=True)
        embed.set_footer(text=f"Requested by {interaction.user.display_name}")

        await interaction.followup.send(embed=embed, ephemeral=True)



# --- 1行サイコロ ---
CYBER_DICE = {
    1: "[ ⚀ ]", 2: "[ ⚁ ]", 3: "[ ⚂ ]",
    4: "[ ⚃ ]", 5: "[ ⚄ ]", 6: "[ ⚅ ]", "?": "[ 🎲 ]"
}

# ==========================================
#  セスタ・チンチロ (PvE & PvP 完全統合版)
# ==========================================

# --- ターン制御用 View ---
class ChinchiroTurnView(discord.ui.View):
    def __init__(self, current_player, turn_count):
        super().__init__(timeout=60)
        self.current_player = current_player
        self.action = None
        # 3回目は強制確定なのでボタン変更
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

# --- PVP 申し込み用 View ---
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
                embed = self.message.embeds[0]
                embed.description = "⏰ 時間切れ。興醒めね。"
                await self.message.edit(embed=embed, view=self)
            except: pass

    @discord.ui.button(label="受けて立つ！", style=discord.ButtonStyle.danger, emoji="⚔️")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.opponent:
            return await interaction.response.send_message("あんた関係ないでしょ。引っ込んでて。", ephemeral=True)
        
        # 受諾時の再チェック
        if not await self.cog.check_balance(self.opponent, self.bet):
             return await interaction.response.send_message("…お金、足りないみたいだけど？", ephemeral=True)
        if not await self.cog.check_balance(self.challenger, self.bet):
             return await interaction.response.send_message("あら、仕掛けた本人が文無しみたいよ？", ephemeral=True)

        await interaction.response.defer()
        self.stop()
        await self.cog.start_pvp_game(interaction, self.challenger, self.opponent, self.bet)

    @discord.ui.button(label="逃げる", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.opponent: return
        
        embed = interaction.message.embeds[0]
        embed.description = f"💨 {self.opponent.display_name} は逃げ出した。\nセスタ「…あらそう。賢明な判断ね（笑）」"
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

# --- 本体 (Cog) ---
import random
import datetime
import asyncio
import discord
from discord.ext import commands
from discord import app_commands

async def check_daily_limit(bot, user_id: int, game: str, limit: int = 10) -> tuple[bool, int]:
    """
    1日のプレイ回数を確認する。
    戻り値: (制限に引っかかったか, 今日の残り回数)
    引っかかった = True なら弾く
    """
    today = datetime.datetime.now().strftime("%Y-%m-%d")

    async with bot.get_db() as db:
        # 免除チェック
        async with db.execute(
            "SELECT 1 FROM daily_play_exemptions WHERE user_id = ? AND game = ? AND date = ?",
            (user_id, game, today)
        ) as c:
            if await c.fetchone():
                return False, 99999  # 制限なし

        # 今日の回数を取得
        async with db.execute(
            "SELECT count FROM daily_play_counts WHERE user_id = ? AND game = ? AND date = ?",
            (user_id, game, today)
        ) as c:
            row = await c.fetchone()
            current = row['count'] if row else 0

    remaining = limit - current
    if remaining <= 0:
        return True, 0
    return False, remaining


async def increment_daily_count(bot, user_id: int, game: str):
    """プレイ後に回数を+1する"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    async with bot.get_db() as db:
        await db.execute("""
            INSERT INTO daily_play_counts (user_id, game, date, count) VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id, game, date) DO UPDATE SET count = count + 1
        """, (user_id, game, today))
        await db.commit()

class Chinchiro(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last_played = {}
        self.play_counts = {} # セッション中のプレイ回数（湿度管理用）
        self.max_bet = 200000 # 賭け金上限
        self.tax_rate_pve = 0.15  # PvE 税率 5% (総合RTP 約85%)
        self.tax_rate_pvp = 0.05  # PvP 場所代 5%

    # --- セリフ管理 (完全版：メスガキ＋イースターエッグ＋ガチデレ) ---
    def get_cesta_dialogue(self, situation, user_name, amount=0, humidity=0, is_all_in=False):
        
        # 🥚 イースターエッグ 1: 煩悩ベット (108 Stell)
        if situation == "intro" and amount == 108:
            return "108Stell？ 煩悩の数？ …ホント、あんたって救いようのないバカだね♡ さっさとむしり取ってあげる！"

        # 🥚 イースターエッグ 2: 鯖主「釈迦」専用セリフ (50%で発生)
        if "釈迦" in user_name and random.random() < 0.5:
            shaka_lines = {
                "intro": "……あっ、鯖主。べ、別にサボってないわよ！ あんたの代わりにカモから巻き上げてやってるんだから感謝しなさいよね！",
                "win_big": "……チッ、鯖主権限で確率いじったでしょ！ ズルいズルい！ 運営の横暴だー！",
                "lose_big": "……っ！ しゃ、釈迦のくせに煩悩まみれで大負けしてんじゃん！ ダッサ！ 鯖主引退すれば？♡",
                "shomben_player": "はーい鯖主のションベンいただきましたー！ スクショして全体公開しよっかなー♡ ざぁこ♡"
            }
            if situation in shaka_lines:
                return shaka_lines[situation]

        # 🥚 イースターエッグ 3: 全額ベット（オールイン）時のガチデレ
        if is_all_in:
            if situation == "intro":
                return f"は！？ 全財産（{amount:,} Stell）賭けるって正気！？\n……バカ。もし一文無しになって、ここに来なくなったら……私、つまんないんだけど。\n……絶対勝ちなさいよ。応援してあげるから。"
            elif situation in ["win_small", "win_big"]:
                return "……っ！ よ、よかったぁ……。心臓止まるかと思った……。もうこんな無茶、絶対しちゃダメだからね！"
            elif situation in ["lose_normal", "lose_big"]:
                return "……バカ。ほんと、どうしようもないバカ……。ほら、ちょっとこっち来なさい。……今日だけは、慰めてあげるから。"

        # 湿度高め（常連・高回数プレイ時）のデレ
        if humidity >= 5 and random.random() < 0.3:
            heavy_lines = [
                f"…何度も何度も、そんなに私に構ってほしいの？ しょーがないなぁ…♡",
                f"ざぁこ♡ …って言いたいとこだけど、{user_name}の粘り強さだけは認めてあげなくもないわ。",
                "ねぇ、そろそろ休憩しない？ …べ、別に心配してるわけじゃないから！私が疲れただけ！",
                "あんたのお金、全部私が管理してあげよっか？ …なーんてね。冗談に決まってんじゃん。"
            ]
            return random.choice(heavy_lines)

        dialogues = {
            "intro_normal": [
                f"お金溶かしに来たの？ いいよ、相手してあげる。ざぁこ♡",
                f"ふーん、{user_name}か。すぐ泣きべそかくくせに、懲りないねぇ。",
                "はいはい、チンチロね。むしり取ってあげるから覚悟しなさいよね！"
            ],
            "intro_high": [
                f"…へぇ、{amount:,} Stell。あんたにしては度胸あるじゃん。",
                "ちょっと、本気？ …負けても泣かないって約束できるなら、受けてあげる。"
            ],
            "pvp_start": [
                "おっ、バカ同士の潰し合い？ 特等席で見させてもらうわ♡",
                "さぁ、どっちが私の養分になるのかなー？ 楽しみ！"
            ],
            "pvp_end": [
                "あーあ、負けた方はダッサいねー♡ 勝った方、場所代きっちり頂くわよ。",
                "はい決着！ …他人の不幸で食べるご飯って最高に美味しいよね。"
            ],
            "win_small": [ 
                "チッ…運だけはいいみたいね。調子乗んな！",
                "はいはい、勝ち分。…たかが一勝でドヤ顔しないでよね。",
                "…あっそ。次で全部取り返してあげるんだから。"
            ],
            "win_big": [ 
                "はぁ！？ ちょ、なんかイカサマしたでしょ！ …証拠がないから払うけど！",
                "…っ！ べ、別に悔しくなんてないし！ たまたまよ、たまたま！",
                "…やるじゃん。ちょっとだけ見直してあげなくもない…わよ。"
            ],
            "lose_normal": [ 
                "はい、全額没収ー！ ざぁこ♡ よわよわ♡",
                "あーあ、また溶かしちゃったね。私のお小遣いあざーっす♡",
                "弱すぎなんですけどー！ 出直してきな！"
            ],
            "lose_big": [ 
                "…っ！ ちょ、そんなに派手に負けて大丈夫なの！？",
                "…バカじゃないの！？ 加減ってものを知りなさいよ！",
                "あーあ…破産しても私は知らないからね。…ホントに大丈夫なの？"
            ],
            "draw_push": [
                "…同点？ チッ、今回は私の奢り（ノーカン）にしてあげるわ。感謝しなさいよね！",
                "引き分けかぁ。…今回は見逃してあげる。次こそむしり取るから！"
            ],
            "shomben_parent": [
                "……あっ。……い、今のノーカン！ ノーカンだから！！ 見てないでしょ！？",
                "ちょっ、サイコロ滑っただけだし！ ズルいズルい！！"
            ],
            "shomben_player": [
                "ダッサ！！ ションベンとかありえないんですけどー！ ざぁこ♡",
                "はーい盤外落下！ あんたホントに不器用だねー♡ はい没収！"
            ]
        }

        if situation == "intro":
            if amount >= 50000: return random.choice(dialogues["intro_high"])
            return random.choice(dialogues["intro_normal"])
        
        return random.choice(dialogues.get(situation, dialogues["lose_normal"]))

    # --- ダイス・描画ロジック ---
    def get_roll_result(self):
        # 3%の確率で「ションベン（盤外）」発生
        if random.random() < 0.03:
            return [0, 0, 0], -99, "ションベン", -1, "💦 盤外", False

        dice = [random.randint(1, 6) for _ in range(3)]
        dice.sort()
        
        if dice == [1, 1, 1]: return dice, 111, "【極】ピンゾロ", 5, "🔥 最 強 🔥", True
        if dice[0] == dice[1] == dice[2]: return dice, 100 + dice[0], f"嵐 ({dice[0]})", 3, "💪 強 い", True
        if dice == [4, 5, 6]: return dice, 90, "シゴロ (4-5-6)", 2, "✨ 勝ち確", False
        if dice == [1, 2, 3]: return dice, -1, "ヒフミ (1-2-3)", -2, " 倍 払 い", False
        
        if dice[0] == dice[1]: return dice, dice[2], f"{dice[2]} の目", 1, "😐 フツー", False
        if dice[1] == dice[2]: return dice, dice[0], f"{dice[0]} の目", 1, "😐 フツー", False
        if dice[0] == dice[2]: return dice, dice[1], f"{dice[1]} の目", 1, "😐 フツー", False
        
        return dice, 0, "役なし", 0, "💀 没収", False

    def get_cyber_dice_string(self, dice_list):
        if dice_list == [0, 0, 0]:
            return "×  ×  ×"
        # CYBER_DICE は外部で定義されている前提
        return "  ".join([CYBER_DICE.get(num, CYBER_DICE["?"]) for num in dice_list])

    def render_hud(self, player_name, dice_list, status, color_mode="blue"):
        c_frame = blue
        if color_mode == "red": c_frame = red
        elif color_mode == "gold": c_frame = yellow
        elif color_mode == "pink": c_frame = pink
        elif color_mode == "purple": c_frame = lambda x: f"\x1b[1;35m{x}\x1b[0m"
        
        c_stat_text = white
        if "リーチ" in status: c_stat_text = red
        elif "神" in status: c_stat_text = yellow
        elif "勝ち" in status: c_stat_text = yellow
        elif "盤外" in status: c_stat_text = red

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
            # ションベン時は即時結果表示
            if score == -99:
                final_hud = self.render_hud(player_name, final_dice, rank_text, "red")
                embed.set_field_at(field_idx, name=f"💦 {player_name}", value=final_hud, inline=False)
                await msg.edit(embed=embed)
                await asyncio.sleep(1.0)
                return

            rand_dice = [random.randint(1,6) for _ in range(3)]
            hud = self.render_hud(player_name, rand_dice, "回転中...", "blue")
            embed.set_field_at(field_idx, name=f"🎲 {player_name}", value=hud, inline=False)
            await msg.edit(embed=embed)
            await asyncio.sleep(0.8)

            if score >= 90 or final_dice[0] == final_dice[1]:
                reach_dice = [final_dice[0], final_dice[1], random.randint(1,6)]
                hud = self.render_hud(player_name, reach_dice, "!!! リーチ !!!", "red")
                embed.set_field_at(field_idx, name=f"⚠️ {player_name}", value=hud, inline=False)
                await msg.edit(embed=embed)
                await asyncio.sleep(1.0)
            
            res_color = "blue"
            if is_super: res_color = "gold"
            elif score >= 90: res_color = "gold"
            elif score == -1: res_color = "purple"
            elif score <= 0: res_color = "red"
            
            final_hud = self.render_hud(player_name, final_dice, rank_text, res_color)
            embed.set_field_at(field_idx, name=f"🏁 {player_name}", value=final_hud, inline=False)
            await msg.edit(embed=embed)
        except Exception:
            pass

    async def check_balance(self, user, amount):
        async with self.bot.get_db() as db:
            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (user.id,)) as c:
                row = await c.fetchone()
                return row and row['balance'] >= amount

    async def run_player_turn(self, msg, embed, field_idx, player):
        best_res = {"score": -999, "mult": 1, "dice": [1,2,3], "name": "役なし", "is_super": False}
        
        for try_num in range(1, 4):
            dice, score, name, mult, rank, is_super = self.get_roll_result()
            await self.play_animation(msg, embed, field_idx, player.display_name, dice, name, score, is_super)
            
            if score == -99: # ションベン
                return {"score": score, "mult": mult, "dice": dice, "name": name, "is_super": False}

            if score >= 90 or score == -1 or try_num == 3:
                best_res = {"score": score, "mult": mult, "dice": dice, "name": name, "is_super": is_super}
                break
            
            if score > 0:
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

    # ------------------------------------------------------------------
    #  PvE: 対セスタ
    # ------------------------------------------------------------------
    @app_commands.command(name="チンチロ", description="セスタと勝負。")
    async def chinchiro(self, interaction: discord.Interaction, bet: int):
        if bet < 100: 
            return await interaction.response.send_message(f"は？ {bet} Stell？ 小銭じゃつまんないんですけどー。100Stellからにしてよね、ざぁこ♡", ephemeral=True)
        if bet > self.max_bet:
            return await interaction.response.send_message(f"ちょっと！ 上限は **{self.max_bet:,} Stell** まで！ 私から全部巻き上げるつもり！？ …手加減しなさいよ！", ephemeral=True)

    # ▼ 日次制限チェック（ここを追加）
        is_over, remaining = await check_daily_limit(self.bot, interaction.user.id, "chinchiro")
        if is_over:
            return await interaction.response.send_message(
            "セスタ「今日はもう終わり。また明日いらっしゃい♡ 依存症は私でも面倒みきれないわ。」\n"
            "（本日の上限10回に達しました）",
            ephemeral=True
        )
    # ▲ ここまで

        now = datetime.datetime.now()
        last_time = self.last_played.get(interaction.user.id)
        
        if last_time and (now - last_time).total_seconds() > 1800:
            self.play_counts[interaction.user.id] = 0
        
        if last_time and (now - last_time).total_seconds() < 3.0: 
            return await interaction.response.send_message("ちょっと焦りすぎじゃない？ がっつきすぎでキモいんですけどー♡ 落ち着きなよ。", ephemeral=True)

        self.last_played[interaction.user.id] = now
        self.play_counts[interaction.user.id] = self.play_counts.get(interaction.user.id, 0) + 1
        humidity = self.play_counts[interaction.user.id]

        # 残高とオールイン判定の取得
        async with self.bot.get_db() as db:
            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (interaction.user.id,)) as c:
                row = await c.fetchone()
                if not row or row['balance'] < bet:
                    return await interaction.response.send_message("…は？ お金ないじゃん。私に貢ぐお金すら無くなっちゃったの？ ざぁこ♡ 出直してきな！", ephemeral=True)
                curr_balance = row['balance']

        is_all_in = (bet == curr_balance and bet >= 100)

        await interaction.response.defer()

        opening_line = self.get_cesta_dialogue("intro", interaction.user.display_name, bet, humidity, is_all_in)
        embed = discord.Embed(title="🎲 セスタの賭博", description=opening_line, color=0x2f3136)
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.add_field(name="親：セスタ", value=self.render_hud("セスタ", ["?", "?", "?"], "待機中..."), inline=False)
        embed.add_field(name=f"子：{interaction.user.display_name}", value="準備中...", inline=False)
        msg = await interaction.followup.send(embed=embed)

        # セスタのターン：役が出るまで最大3回振る
        p_score = 0
        for _ in range(3):
            p_dice, p_score, p_name, p_mult, p_rank, p_super = self.get_roll_result()
            if p_score != 0: # 役（目）が出たら終了
                break

        phud = self.render_hud("セスタ", p_dice, p_name, "gold" if p_super else ("red" if p_score <= 0 else "blue"))
        embed.set_field_at(0, name="親：セスタ (確定)", value=phud, inline=False)
        await msg.edit(embed=embed)
        
        if p_score >= 90:
             return await self.settle_pve(msg, embed, interaction.user, bet, -p_mult if p_mult > 0 else -1, humidity, p_score, 0, is_all_in)
        if p_score == -99:
             return await self.settle_pve(msg, embed, interaction.user, bet, 1, humidity, p_score, 0, is_all_in)

        u_res = await self.run_player_turn(msg, embed, 1, interaction.user)
        u_score, u_mult = u_res["score"], u_res["mult"]

        final_mult = 0
        if u_score == -99:
            final_mult = -1
        elif u_score == -1:
            final_mult = -2 
        elif u_score > p_score:
            final_mult = max(u_mult, abs(p_mult) if p_mult < 0 else 1)
        elif u_score < p_score:
            final_mult = -max(p_mult, abs(u_mult) if u_mult < 0 else 1)
        else:
            final_mult = 0 # 引き分けは0(返金)

        await self.settle_pve(msg, embed, interaction.user, bet, final_mult, humidity, p_score, u_score, is_all_in)

    async def settle_pve(self, msg, embed, user, bet, multiplier, humidity, p_score=0, u_score=0, is_all_in=False):
        async with self.bot.get_db() as db:
            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (user.id,)) as c:
                curr_balance = (await c.fetchone())['balance']

            if multiplier > 0:
                raw_win = bet * multiplier
                tax = int(raw_win * self.tax_rate_pve)
                final_profit = raw_win - tax
                
                await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (final_profit, user.id))
                
                embed.color = 0xffd700
                res_str = f"🎉 **WIN! +{final_profit:,} Stell**"
                if multiplier > 1: res_str += f" (x{multiplier})"
                res_str += f"\n(手数料: {tax:,} S)"
                
                if p_score == -99:
                    comment = self.get_cesta_dialogue("shomben_parent", user.display_name, 0, humidity, is_all_in)
                else:
                    comment_key = "win_big" if multiplier >= 3 else "win_small"
                    comment = self.get_cesta_dialogue(comment_key, user.display_name, 0, humidity, is_all_in)
                embed.description = comment

            elif multiplier < 0:
                loss_mult = abs(multiplier)
                loss_amount = bet * loss_mult
                actual_loss = min(loss_amount, curr_balance)
                
                await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (actual_loss, user.id))
                
                jp_feed = int(actual_loss * 0.05)
                if jp_feed > 0:
                    await db.execute("""
                        INSERT INTO server_config (key, value) VALUES ('jackpot_pool', ?) 
                        ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + ?
                    """, (jp_feed, jp_feed))

                embed.color = 0x2f3136
                res_str = f"💀 **LOSE... -{actual_loss:,} Stell**"
                if loss_mult > 1: res_str += f" (x{loss_mult} 倍払い)"
                
                if u_score == -99:
                    comment = self.get_cesta_dialogue("shomben_player", user.display_name, 0, humidity, is_all_in)
                else:
                    comment_key = "lose_big" if loss_mult >= 2 else "lose_normal"
                    comment = self.get_cesta_dialogue(comment_key, user.display_name, actual_loss, humidity, is_all_in)

                embed.description = comment

            else:
                embed.color = 0x808080
                res_str = f"🤝 **DRAW (引き分け)**\nベット額 {bet:,} Stell は返還されました。"
                embed.description = self.get_cesta_dialogue("draw_push", user.display_name, 0, humidity, is_all_in)
            
            await db.commit()
            await increment_daily_count(self.bot, user.id, "chinchiro")
            
        embed.add_field(name="最終結果", value=res_str, inline=False)
        await msg.edit(embed=embed, view=None)

    # ------------------------------------------------------------------
    #  PvP: 対人戦
    # ------------------------------------------------------------------
    @app_commands.command(name="チンチロ対戦", description="【PVP】他のユーザーと1vs1で勝負します。")
    @app_commands.describe(opponent="対戦相手", bet="賭け金")
    async def pvp_chinchiro(self, interaction: discord.Interaction, opponent: discord.Member, bet: int):
        if opponent.bot or opponent == interaction.user:
            return await interaction.response.send_message("…ねえ、バカなの？ 虚空に向かってチンチロするとかウケるんですけどー♡ ちゃんと相手を選びなよ。", ephemeral=True)
        if bet < 500: return await interaction.response.send_message("対戦は500Stellから。小銭の奪い合いとか見苦しいだけだからやめてよね。", ephemeral=True)
        if bet > self.max_bet: return await interaction.response.send_message(f"上限は {self.max_bet:,} Stell まで。どんだけ熱くなってんの？ 少しは落ち着きなよ。", ephemeral=True)

        if not await self.check_balance(interaction.user, bet):
             return await interaction.response.send_message("…あんた、お金ないじゃん。自分のお財布も確認できないの？ ざぁこ♡", ephemeral=True)
        if not await self.check_balance(opponent, bet):
             return await interaction.response.send_message("…相手がお金持ってないみたい。貧乏人同士で喧嘩しないでよ、みすぼらしいなぁ。", ephemeral=True)

        embed = discord.Embed(title="⚔️ 決闘の申し込み", description=f"{interaction.user.mention} が {opponent.mention} に勝負を挑んだわ。\n\n💰 **レート: {bet:,} Stell**", color=0xff0000)
        embed.set_thumbnail(url=opponent.display_avatar.url)
        embed.set_footer(text="受けるも逃げるも自由よ。…ま、逃げたら一生バカにしてあげるけどね♡")

        view = ChinchiroPVPApplyView(self, interaction.user, opponent, bet)
        await interaction.response.send_message(content=opponent.mention, embed=embed, view=view)
        view.message = await interaction.original_response()

    async def start_pvp_game(self, interaction, challenger, opponent, bet):
        async with self.bot.get_db() as db:
            await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (bet, challenger.id))
            await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (bet, opponent.id))
            await db.commit()

        embed = discord.Embed(title="⚔️ PVP CHINCHIRO", description=self.get_cesta_dialogue("pvp_start", ""), color=0x990000)
        hud_1 = self.render_hud(challenger.display_name, ["?", "?", "?"], "待機中...")
        hud_2 = self.render_hud(opponent.display_name, ["?", "?", "?"], "待機中...")
        embed.add_field(name=f"1P: {challenger.display_name}", value=hud_1, inline=False)
        embed.add_field(name=f"2P: {opponent.display_name}", value=hud_2, inline=False)
        
        msg = interaction.message
        await msg.edit(content=None, embed=embed, view=None)

        c_res = await self.run_player_turn(msg, embed, 0, challenger)
        o_res = await self.run_player_turn(msg, embed, 1, opponent)

        await self.settle_pvp(msg, embed, challenger, opponent, bet, c_res, o_res)

    async def settle_pvp(self, msg, embed, p1, p2, bet, r1, r2):
        s1, m1 = r1["score"], r1["mult"]
        s2, m2 = r2["score"], r2["mult"]
        
        winner = None
        loser = None
        payout_mult = 1
        is_draw = False

        if s1 == -99 and s2 == -99:
            is_draw = True
        elif s1 == -99:
            winner, loser = p2, p1
        elif s2 == -99:
            winner, loser = p1, p2
        elif s1 > s2:
            winner, loser = p1, p2
            payout_mult = max(m1 if m1 > 0 else 1, abs(m2) if m2 < 0 else 1)
        elif s2 > s1:
            winner, loser = p2, p1
            payout_mult = max(m2 if m2 > 0 else 1, abs(m1) if m1 < 0 else 1)
        else:
            is_draw = True

        async with self.bot.get_db() as db:
            if is_draw:
                await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (bet, p1.id))
                await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (bet, p2.id))
                
                desc = f"🤝 **DRAW** (返金)\n\nセスタ「…チッ、興醒め。とっとと帰りな。」"
                embed.color = 0x808080

            else:
                base_pot = bet * 2
                extra_take = 0
                
                if payout_mult > 1:
                    extra_needed = bet * (payout_mult - 1)
                    
                    async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (loser.id,)) as c:
                        l_bal = (await c.fetchone())['balance']
                    
                    extra_take = min(extra_needed, l_bal)
                    if extra_take > 0:
                        await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (extra_take, loser.id))
                
                total_win = base_pot + extra_take
                fee = int(total_win * self.tax_rate_pvp)
                final_payout = total_win - fee
                
                await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (final_payout, winner.id))
                
                cesta_msg = self.get_cesta_dialogue("pvp_end", "")
                
                res_hud = (
                    f"```ansi\n"
                    f"{yellow('┏━━━━━━━━━━━━━━━━━━━━━━━━━━┓')}\n"
                    f"{yellow('┃')}   👑  {white('WINNER')}  👑   {yellow('┃')}\n"
                    f"{yellow('┃')}   {blue(winner.display_name.center(20))}   {yellow('┃')}\n"
                    f"{yellow('┃')} {green('+' + f'{final_payout:,}'.center(16) + 'S')} {yellow('┃')}\n"
                    f"{yellow('┗━━━━━━━━━━━━━━━━━━━━━━━━━━┛')}\n"
                    f"```"
                )
                desc = f"{res_hud}\n決まり手: **x{payout_mult}** (場所代: {fee:,})\n\nセスタ「{cesta_msg}」"
                
                embed.title = "🏆 決 着"
                embed.description = desc
                embed.color = 0xffd700
            
            await db.commit()

            embed.clear_fields()
            embed.add_field(name=f"1P: {p1.display_name}", value=f"{r1['name']} ({r1['score']})", inline=True)
            embed.add_field(name=f"2P: {p2.display_name}", value=f"{r2['name']} ({r2['score']})", inline=True)
            
            await msg.edit(embed=embed, view=None)



            
    @app_commands.command(name="ゴミ拾い", description="所持金が500Stell以下の時だけ使えます。")
    async def scavenge(self, interaction: discord.Interaction):
        async with self.bot.get_db() as db:
            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (interaction.user.id,)) as c:
                row = await c.fetchone()
                bal = row['balance'] if row else 0
            
            if bal > 500:
                return await interaction.response.send_message("まだ持ってるでしょ？ 欲張らないで。", ephemeral=True)
            
            amount = random.randint(500, 1500)
            await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (amount, interaction.user.id))
            await db.commit()
            
            msg_text = self.get_stella_dialogue("scavenge", interaction.user.display_name)
            
            if random.randint(1, 20) == 1:
                msg_text = f"「…はぁ。仕方ないわね。\nこれ、私が落としたことにしといてあげる。」\n(セスタがそっぽを向きながら **{amount} Stell** を投げ捨てた！)"

            await interaction.response.send_message(f"{msg_text}\n\n🗑️ 公園で空き缶を拾って **{amount} Stell** になりました。")


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
            "1": { # 期待値: 約88.5% (しっかり回収)
                "probs": [
                    ("DIAMOND", 5, 100), ("SEVEN", 40, 20), ("WILD", 70, 10),
                    ("BELL", 500, 5), ("CHERRY", 2500, 2), ("MISS", 6885, 0)
                ], 
                "ceiling": 1200, "name": "設定1 (回収)" 
            },
            "2": { # 期待値: 約91.5% (弱回収)
                "probs": [
                    ("DIAMOND", 6, 100), ("SEVEN", 50, 20), ("WILD", 85, 10),
                    ("BELL", 600, 5), ("CHERRY", 2350, 2), ("MISS", 6909, 0)
                ], 
                "ceiling": 1000, "name": "設定2 (弱回収)" 
            },
            "3": { # 期待値: 約94.8% (遊びやすい)
                "probs": [
                    ("DIAMOND", 8, 100), ("SEVEN", 60, 20), ("WILD", 110, 10),
                    ("BELL", 700, 5), ("CHERRY", 2300, 2), ("MISS", 6822, 0)
                ], 
                "ceiling": 850, "name": "設定3 (遊び)" 
            },
            "4": { # 期待値: 約98.2% (トントン)
                "probs": [
                    ("DIAMOND", 10, 100), ("SEVEN", 75, 20), ("WILD", 140, 10),
                    ("BELL", 850, 5), ("CHERRY", 2250, 2), ("MISS", 6675, 0)
                ], 
                "ceiling": 700, "name": "設定4 (通常)" 
            },
            "5": { # 期待値: 約101.5% (微増インフレ)
                "probs": [
                    ("DIAMOND", 12, 100), ("SEVEN", 90, 20), ("WILD", 180, 10),
                    ("BELL", 1000, 5), ("CHERRY", 2200, 2), ("MISS", 6518, 0)
                ], 
                "ceiling": 550, "name": "設定5 (優良)" 
            },
            "6": { # 期待値: 約105.8% (夢の設定・制限解除だと少し危険)
                "probs": [
                    ("DIAMOND", 15, 100), ("SEVEN", 110, 20), ("WILD", 250, 10),
                    ("BELL", 1200, 5), ("CHERRY", 2150, 2), ("MISS", 6275, 0)
                ], 
                "ceiling": 400, "name": "設定6 (極)" 
            },
            "L": { # 期待値: 約10.0% (地獄)
                "probs": [
                    ("DIAMOND", 0, 100), ("SEVEN", 0, 20), ("WILD", 0, 10), 
                    ("BELL", 0, 5), ("CHERRY", 500, 2), ("MISS", 9500, 0)
                ], 
                "ceiling": 99999, "name": "設定L (虚無)" 
            }
        }

    def get_stella_comment(self, situation, **kwargs):
        user = kwargs.get('user', '貴方')
        
        if random.randint(1, 100) == 1:
            return pink(f"「…{user}、あんまり根詰めちゃだめよ。…べ、別に心配なんてしてないけど！」")

        dialogues = {
            "start_normal": [
                "「さあ、回しなさい。運命のレバーを。」",
                "「私のためにStellを増やしてくれるのかしら？」",
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
            "stella_save": [
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

    @app_commands.command(name="スロット設定", description="スロットの設定を変更します")
    @app_commands.describe(mode="設定値 (1-6, L)")
    @commands.is_owner()
    async def config_slot(self, interaction: discord.Interaction, mode: str):
        if mode not in self.MODES:
            return await interaction.response.send_message("設定値が無効です。", ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('slot_mode', ?)", (mode,))
            await db.commit()
        await interaction.response.send_message(f"✅ 設定を **{self.MODES[mode]['name']}** に変更しました。", ephemeral=True)

    @app_commands.command(name="スロット", description="さ、引きなさい。")
    @app_commands.describe(bet="賭け金 (100 Stell 〜)")
    async def slot(self, interaction: discord.Interaction, bet: int):
        if bet < 100: return await interaction.response.send_message("100Stellから。", ephemeral=True)
        if bet > 200000:return await interaction.response.send_message("…熱くなりすぎよ。賭け金は 200,000 Stell までにしておきなさい。", ephemeral=True)

             # ▼ 日次制限チェック（ここを追加）
        is_over, remaining = await check_daily_limit(self.bot, interaction.user.id, "slot")
        if is_over:
            return await interaction.response.send_message(
               "今日はもう閉店よ。また明日いらっしゃい♡\n"
               "（本日の上限10回に達しました）",
               ephemeral=True
           )
        
        now = datetime.datetime.now()
        last_time = self.last_played.get(interaction.user.id)
        if last_time and (now - last_time).total_seconds() < 3.5:
            return await interaction.response.send_message("目が回るわ…落ち着きなさい。", ephemeral=True)
        self.last_played[interaction.user.id] = now
        
        streak = self.loss_streak.get(interaction.user.id, 0)
        if streak >= 10:
             await interaction.response.send_message(f"…{streak}連敗中よ？ 少し頭を冷やしてきたら？\n(深呼吸中... ⏳ 5秒)", ephemeral=True)
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
                        return await interaction.followup.send("ステラ「お金、足りないみたいよ？ 出直してらっしゃい。」")
                await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (bet, user.id))
                await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = 0", (bet,))
                await db.commit()

            current_mode_key = await self.get_current_mode()
            outcome_name, multiplier, is_ceiling_hit, spins_now = await self.spin_slot(user.id, current_mode_key)
            
            is_freeze = (outcome_name == "DIAMOND" and random.random() < 0.33)
            is_respin = (outcome_name in ["WILD", "SEVEN", "DIAMOND"] and random.random() < 0.20)
            
            is_stella_save = False
            if outcome_name == "MISS" and not is_ceiling_hit:
                if random.random() < 0.001:
                    is_stella_save = True
                    outcome_name = "SEVEN"
                    multiplier = 20
            
            is_stella_cutin = False
            
            final_grid = self.generate_grid(outcome_name)
            
            ceiling_max = self.MODES[current_mode_key]["ceiling"]
            is_deep = spins_now >= (ceiling_max * 0.8)

            start_msg = self.get_stella_comment("start_deep" if is_deep else "start_normal", user=user.display_name)
            if is_ceiling_hit: start_msg = self.get_stella_comment("ceiling_hit")

            embed = discord.Embed(title="🎰 ステラ・スロット", color=0x2f3136)

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
                if is_respin or is_stella_save: 
                     disp[1][0] = self.SYMBOLS["MISS"] if is_stella_save else final_grid[1][0]
                
                embed.description = self.render_slot_screen(disp, "STOPPING...", aura)
                await msg.edit(embed=embed)
                await asyncio.sleep(0.7)

                disp[1][1] = final_grid[1][1]
                if is_stella_save: disp[1][1] = self.SYMBOLS["MISS"]

                is_reach = disp[1][0] == disp[1][1]
                
                if is_reach and not is_stella_save and random.random() < 0.20:
                    is_stella_cutin = True

                mid_status = "!!!" if is_reach else "..."
                if is_stella_cutin: mid_status = "STELLA IS WATCHING..."
                
                mid_color = aura
                if is_reach: mid_color = "red"
                if is_stella_cutin: mid_color = "pink"

                embed.description = self.render_slot_screen(disp, mid_status, mid_color)
                await msg.edit(embed=embed)
                
                wait_time = 0.5
                if is_reach: wait_time = 1.0
                if is_stella_cutin: wait_time = 1.5
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
                
                elif is_stella_save:
                    miss_grid = self.generate_grid("MISS")
                    embed.description = self.render_slot_screen(miss_grid, "LOSE...", "blue")
                    await msg.edit(embed=embed)
                    await asyncio.sleep(1.5)
                    embed.color = 0xff69b4 
                    lumen_txt = self.render_slot_screen(miss_grid, "⚡ STELLA PANIC ⚡", "pink")
                    save_msg = self.get_stella_comment("stella_save")
                    embed.description = f"{lumen_txt}\n{pink(save_msg)}"
                    await msg.edit(embed=embed)
                    await asyncio.sleep(2.0)
                
                final_display = final_grid
                flash_col = "gold" if multiplier > 0 else aura
                if is_stella_save: flash_col = "pink"

            final_screen = self.render_slot_screen(final_display, "WINNER!!" if multiplier > 0 else "LOSE...", flash_col)
            embed.description = final_screen
            
            if multiplier > 0:
                payout = bet * multiplier
                async with self.bot.get_db() as db:
                    await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (payout, user.id))
                    await db.commit()
                self.loss_streak[user.id] = 0

                if is_stella_save:
                    comment = "💕 **STELLA SAVE!!** 💕\n「貸しにしておくわよ！」"
                    color = 0xff69b4
                elif outcome_name == "DIAMOND":
                    comment = self.get_stella_comment("win_god")
                    color = 0xffffff
                    res_txt = "**PREMIUM JACKPOT**"
                elif outcome_name in ["SEVEN"]:
                    comment = self.get_stella_comment("win_big")
                    color = 0xffd700
                    res_txt = "**BIG WIN**"
                elif outcome_name in ["WILD"]:
                    comment = self.get_stella_comment("win_mid")
                    color = 0xff00ff
                    res_txt = "**SUPER WIN**"
                else:
                    comment = self.get_stella_comment("win_small")
                    color = 0x00ff00
                    res_txt = "**WIN**"

                if is_ceiling_hit:
                    comment = self.get_stella_comment("ceiling_hit")
                    res_txt += " (天井到達)"

                embed.clear_fields()
                embed.add_field(name=res_txt if 'res_txt' in locals() else "WIN", value=f"**+{payout:,} Stell**", inline=False)
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
                        await increment_daily_count(self.bot, interaction.user.id, "slot")
# ▲
                
                self.loss_streak[user.id] = self.loss_streak.get(user.id, 0) + 1
                comment = self.get_stella_comment("lose")
                embed.color = 0x2f3136
                embed.clear_fields()
                if charge > 0:
                    embed.set_footer(text=f"現在の回転数: {spins_now}G | 負け額の一部はJPへ")

            embed.description += f"\n\n{comment}"
            await msg.edit(content=None, embed=embed)

        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(f"❌ エラー: `{e}`", ephemeral=True)

# ==========================================
#  人間株式市場 (完全版: スター豪華演出 + 昇格システム)
# ==========================================

# --- 取引パネル (View) ---
class StockControlView(discord.ui.View):
    def __init__(self, cog, target_user: discord.Member):
        super().__init__(timeout=300)
        self.cog = cog
        self.target = target_user

    async def update_embed(self, interaction: discord.Interaction):
        # 1. DBから最新情報を取得
        star_role_id = None
        async with self.cog.bot.get_db() as db:
            # スターロールIDの確認
            async with db.execute("SELECT value FROM market_config WHERE key = 'star_role_id'") as c:
                row = await c.fetchone()
                if row: star_role_id = int(row['value'])

            # 発行株数の確認
            async with db.execute("SELECT total_shares FROM stock_issuers WHERE user_id = ?", (self.target.id,)) as c:
                row = await c.fetchone()
                if not row: return None 
                shares = row['total_shares']
            
            # 自分の保有状況の確認
            async with db.execute("SELECT amount, avg_cost FROM stock_holdings WHERE user_id = ? AND issuer_id = ?", (interaction.user.id, self.target.id)) as c:
                holding = await c.fetchone()
                my_amount = holding['amount'] if holding else 0
                my_avg = holding['avg_cost'] if holding else 0

        # 2. スター判定（ターゲットがスターロールを持っているか？）
        is_star = False
        if star_role_id:
            if any(r.id == star_role_id for r in self.target.roles):
                is_star = True

        current_price = self.cog.calculate_price(shares)
        
        # 3. 損益計算
        total_val = current_price * my_amount
        profit = total_val - (my_avg * my_amount)
        sign = "+" if profit >= 0 else ""

        # 4. デザインの分岐
        if is_star:
            # ★★★ スター用の豪華デザイン ★★★
            color = 0xFFD700 # ゴールド
            title = f"👑 {self.target.display_name} 👑"
            desc = "✨ **STAR MEMBER** ✨\n現在ランキング上位のスター銘柄です。\n価格変動が激しい可能性があります。"
            thumbnail_url = self.target.display_avatar.url
        else:
            # 通常デザイン（利益が出てれば緑、損失なら赤）
            color = 0x00ff00 if profit >= 0 else 0xff0000
            title = f"📈 {self.target.display_name} の銘柄"
            desc = "ボタンで売買できます（手数料: 10%）"
            thumbnail_url = self.target.display_avatar.url
        
        embed = discord.Embed(title=title, description=desc, color=color)
        embed.set_thumbnail(url=thumbnail_url)
        
        # 5. フィールド設定
        # スターの場合は少しリッチな装飾文字を使う
        icon_price = "💎" if is_star else "💰"
        icon_stock = "🏰" if is_star else "🏢"

        embed.add_field(name=f"{icon_price} 現在株価", value=f"**{current_price:,} S**", inline=True)
        embed.add_field(name=f"{icon_stock} 発行数", value=f"{shares:,} 株", inline=True)
        
        # 空白フィールドで段落調整
        embed.add_field(name="\u200b", value="\u200b", inline=True) 

        # 保有情報の表示
        embed.add_field(name="──────────", value="**あなたの保有状況**", inline=False)
        embed.add_field(name="🎒 保有数", value=f"{my_amount:,} 株", inline=True)
        
        # 損益表示（スターで色が固定されても、損益は文字色で見やすくする）
        profit_str = f"{sign}{int(profit):,} S"
        if profit >= 0:
            val_str = f"```ansi\n\u001b[1;32m{profit_str}\u001b[0m```" # 緑
        else:
            val_str = f"```ansi\n\u001b[1;31m{profit_str}\u001b[0m```" # 赤
            
        embed.add_field(name="📊 評価損益", value=val_str, inline=True)
        
        if is_star:
            embed.set_footer(text="★ スター銘柄: 2週間ごとの審査で入れ替わります")
        
        return embed

    # --- ボタン処理 ---
    @discord.ui.button(label="買う(1)", style=discord.ButtonStyle.success, emoji="🛒", row=0)
    async def buy_one(self, interaction, button): await self._trade(interaction, "buy", 1)

    @discord.ui.button(label="買う(10)", style=discord.ButtonStyle.success, emoji="📦", row=0)
    async def buy_ten(self, interaction, button): await self._trade(interaction, "buy", 10)

    @discord.ui.button(label="売る(1)", style=discord.ButtonStyle.danger, emoji="💸", row=1)
    async def sell_one(self, interaction, button): await self._trade(interaction, "sell", 1)

    @discord.ui.button(label="全売却", style=discord.ButtonStyle.danger, emoji="💥", row=1)
    async def sell_all(self, interaction, button):
        async with self.cog.bot.get_db() as db:
            async with db.execute("SELECT amount FROM stock_holdings WHERE user_id = ? AND issuer_id = ?", (interaction.user.id, self.target.id)) as c:
                row = await c.fetchone()
                amount = row['amount'] if row else 0
        if amount > 0: await self._trade(interaction, "sell", amount)
        else: await interaction.response.send_message("株を持っていません。", ephemeral=True)

    @discord.ui.button(label="更新", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)
    async def refresh(self, interaction, button):
        new_embed = await self.update_embed(interaction)
        if new_embed: await interaction.response.edit_message(embed=new_embed, view=self)

    async def _trade(self, interaction, type, amount):
        if type == "buy": msg, success = await self.cog.internal_buy(interaction.user, self.target, amount)
        else: msg, success = await self.cog.internal_sell(interaction.user, self.target, amount)
        
        if success:
            new_embed = await self.update_embed(interaction)
            await interaction.response.edit_message(embed=new_embed, view=self)
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


# --- 本体 (Cog) ---
class HumanStockMarket(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # --- 市場設定 ---
        self.base_price = 100       # 最低価格
        self.slope = 20             # 価格感応度（1株ごとの値上がり幅）
        self.trading_fee = 0.10     # 手数料10%
        self.issuer_fee = 0.05      # 発行者への還元5%
        
        self.promotion_cycle_task.start() # 昇格審査タスクを開始

    def cog_unload(self):
        self.promotion_cycle_task.cancel()

    # 価格計算式（ボンディングカーブ）
    def calculate_price(self, shares):
        return self.base_price + (shares * self.slope)

    async def init_market_db(self):
        async with self.bot.get_db() as db:
            await db.execute("CREATE TABLE IF NOT EXISTS stock_issuers (user_id INTEGER PRIMARY KEY, total_shares INTEGER DEFAULT 0, is_listed INTEGER DEFAULT 1)")
            await db.execute("CREATE TABLE IF NOT EXISTS stock_holdings (user_id INTEGER, issuer_id INTEGER, amount INTEGER, avg_cost REAL, PRIMARY KEY (user_id, issuer_id))")
            await db.execute("CREATE TABLE IF NOT EXISTS market_config (key TEXT PRIMARY KEY, value TEXT)")
            await db.commit()

    # --- 昇格・入れ替えシステム (2週間ごとのランキング集計) ---
    @tasks.loop(hours=1) # 1時間ごとにチェック
    async def promotion_cycle_task(self):
        await self.bot.wait_until_ready()
        now = datetime.datetime.now()
        
        async with self.bot.get_db() as db:
            # 次回の審査日時を取得
            async with db.execute("SELECT value FROM market_config WHERE key = 'next_promotion_date'") as c:
                row = await c.fetchone()
                if row:
                    next_date = datetime.datetime.fromisoformat(row['value'])
                else:
                    # 設定がない場合は現在時刻から2週間後をセット
                    next_date = now + datetime.timedelta(weeks=2)
                    await db.execute("INSERT OR REPLACE INTO market_config (key, value) VALUES ('next_promotion_date', ?)", (next_date.isoformat(),))
                    await db.commit()
                    return # 初回セット時はスキップ

        # 審査時刻を過ぎていたら実行
        if now >= next_date:
            await self.execute_promotion(now)

    async def execute_promotion(self, now):
        guild = self.bot.guilds[0] # メインサーバーを想定
        cast_role_id = None
        star_role_id = None
        log_ch_id = None

        # 設定読み込み
        async with self.bot.get_db() as db:
            async with db.execute("SELECT key, value FROM market_config") as c:
                async for row in c:
                    if row['key'] == 'cast_role_id': cast_role_id = int(row['value'])
                    elif row['key'] == 'star_role_id': star_role_id = int(row['value'])
                    elif row['key'] == 'promotion_log_id': log_ch_id = int(row['value'])
            
            # ランキング集計（株価が高い順 = 発行数が多い順）
            async with db.execute("SELECT user_id, total_shares FROM stock_issuers WHERE is_listed=1 ORDER BY total_shares DESC") as c:
                rankings = await c.fetchall()

        if not cast_role_id or not star_role_id:
            logger.error("Roles for Stock Market promotion are not set.")
            return

        cast_role = guild.get_role(cast_role_id)
        star_role = guild.get_role(star_role_id)
        if not cast_role or not star_role: return

        # 上位4名を特定
        top_4_ids = []
        promoted_members = []
        demoted_members = []

        # ランキング上位からループして、キャストロールを持っている人を探す
        for row in rankings:
            if len(top_4_ids) >= 4: break
            
            member = guild.get_member(row['user_id'])
            if member and cast_role in member.roles: # キャストロール所持者のみ対象
                top_4_ids.append(member.id)

        # 1. スターロールの付与と剥奪処理
        # 現在スターロールを持っている全員をチェック
        for member in star_role.members:
            if member.id not in top_4_ids:
                try:
                    await member.remove_roles(star_role, reason="株価ランキング圏外による降格")
                    demoted_members.append(member.display_name)
                except: pass
        
        # 新トップ4にスターロール付与
        for uid in top_4_ids:
            member = guild.get_member(uid)
            if member:
                if star_role not in member.roles:
                    try:
                        await member.add_roles(star_role, reason="株価ランキングTop4入り")
                        promoted_members.append(member.display_name)
                    except: pass

        # 次回の日程を更新 (2週間後)
        next_due = now + datetime.timedelta(weeks=2)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO market_config (key, value) VALUES ('next_promotion_date', ?)", (next_due.isoformat(),))
            await db.commit()

        # ログ・通知送信
        if log_ch_id:
            channel = self.bot.get_channel(log_ch_id)
            if channel:
                embed = discord.Embed(title="👑 キャスト選抜総選挙 結果発表", description="株価ランキングによるスター入れ替えが行われました。", color=discord.Color.gold())
                
                top_text = ""
                for i, uid in enumerate(top_4_ids):
                    m = guild.get_member(uid)
                    name = m.display_name if m else "Unknown"
                    share_val = 0
                    # 株価取得用
                    for r in rankings:
                        if r['user_id'] == uid:
                            share_val = self.calculate_price(r['total_shares'])
                            break
                    top_text += f"**{i+1}位**: {name} (株価: {share_val:,} S)\n"
                
                if not top_text: top_text = "該当者なし"

                embed.add_field(name="🏆 新スターメンバー (Top 4)", value=top_text, inline=False)
                
                if promoted_members:
                    embed.add_field(name="⬆️ 新規昇格", value=", ".join(promoted_members), inline=True)
                if demoted_members:
                    embed.add_field(name="⬇️ 降格", value=", ".join(demoted_members), inline=True)
                
                embed.set_footer(text=f"次回審査: {next_due.strftime('%Y/%m/%d %H:%M')}")
                await channel.send(embed=embed)


    # --- 内部処理: 購入 ---
    async def internal_buy(self, buyer, target, amount):
        if buyer.id == target.id: return ("❌ 自己売買は禁止です。", False)
        
        async with self.bot.get_db() as db:
            async with db.execute("SELECT total_shares FROM stock_issuers WHERE user_id = ?", (target.id,)) as c:
                row = await c.fetchone()
                if not row: return ("❌ 上場していません。", False)
                shares = row['total_shares']

            # 価格計算
            unit_price = self.calculate_price(shares)
            
            # 購入処理
            subtotal = unit_price * amount
            fee = int(subtotal * self.trading_fee)
            bonus = int(subtotal * self.issuer_fee)
            total = subtotal + fee + bonus

            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (buyer.id,)) as c:
                bal = await c.fetchone()
                if not bal or bal['balance'] < total: return (f"❌ 資金不足 (必要: {total:,} S)", False)

            try:
                # 資産移動
                await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (total, buyer.id))
                await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (bonus, target.id)) # 発行者へ還元
                
                # 保有データ更新
                async with db.execute("SELECT amount, avg_cost FROM stock_holdings WHERE user_id = ? AND issuer_id = ?", (buyer.id, target.id)) as c:
                    h = await c.fetchone()
                
                if h:
                    new_n = h['amount'] + amount
                    # 平均取得単価の更新
                    new_avg = ((h['amount'] * h['avg_cost']) + subtotal) / new_n
                    await db.execute("UPDATE stock_holdings SET amount = ?, avg_cost = ? WHERE user_id = ? AND issuer_id = ?", (new_n, new_avg, buyer.id, target.id))
                else:
                    await db.execute("INSERT INTO stock_holdings (user_id, issuer_id, amount, avg_cost) VALUES (?, ?, ?, ?)", (buyer.id, target.id, amount, unit_price))
                
                # 発行数増加（これにより次の人の購入価格が上がる）
                await db.execute("UPDATE stock_issuers SET total_shares = total_shares + ? WHERE user_id = ?", (amount, target.id))
                
                month = datetime.datetime.now().strftime("%Y-%m")
                await db.execute("INSERT INTO transactions (sender_id, receiver_id, amount, type, description, month_tag) VALUES (?, ?, ?, 'STOCK_BUY', ?, ?)",
                                 (buyer.id, 0, total, f"株購入: {target.display_name}", month))
                await db.commit()
                return (f"✅ 購入成功: {target.display_name} x{amount}株 (単価: {unit_price:,} S)", True)
            except Exception as e:
                await db.rollback()
                return (f"エラー: {e}", False)

    # --- 内部処理: 売却 ---
    async def internal_sell(self, seller, target, amount):
        async with self.bot.get_db() as db:
            async with db.execute("SELECT total_shares FROM stock_issuers WHERE user_id = ?", (target.id,)) as c:
                row = await c.fetchone()
                if not row: return ("❌ 上場していません。", False)
                shares = row['total_shares']

            async with db.execute("SELECT amount, avg_cost FROM stock_holdings WHERE user_id = ? AND issuer_id = ?", (seller.id, target.id)) as c:
                h = await c.fetchone()
                if not h or h['amount'] < amount: return ("❌ 保有数不足", False)

            # 現在価格で売却（売るときは少し安くなる＝スプレッド要素として、base_price計算を現在発行数ベースで行う）
            unit_price = self.calculate_price(shares)
            revenue = unit_price * amount
            
            try:
                new_n = h['amount'] - amount
                if new_n == 0: await db.execute("DELETE FROM stock_holdings WHERE user_id = ? AND issuer_id = ?", (seller.id, target.id))
                else: await db.execute("UPDATE stock_holdings SET amount = ? WHERE user_id = ? AND issuer_id = ?", (new_n, seller.id, target.id))
                
                await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (revenue, seller.id))
                # 発行数を減らす（価格が下がる）
                await db.execute("UPDATE stock_issuers SET total_shares = total_shares - ? WHERE user_id = ?", (amount, target.id))
                
                month = datetime.datetime.now().strftime("%Y-%m")
                await db.execute("INSERT INTO transactions (sender_id, receiver_id, amount, type, description, month_tag) VALUES (0, ?, ?, 'STOCK_SELL', ?, ?)",
                                 (seller.id, revenue, f"株売却: {target.display_name}", month))
                await db.commit()
                return (f"📉 売却成功: {revenue:,} S 受取", True)
            except Exception as e:
                await db.rollback()
                return (f"エラー: {e}", False)

    # --- コマンド類 ---

    @app_commands.command(name="株_キャスト設定", description="【管理者】上場可能な『キャスト』ロールを設定します")
    @has_permission("ADMIN")
    async def config_cast_role(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO market_config (key, value) VALUES ('cast_role_id', ?)", (str(role.id),))
            await db.commit()
        await interaction.followup.send(f"✅ 上場可能ロールを {role.mention} に設定しました。", ephemeral=True)

    @app_commands.command(name="株_スター設定", description="【管理者】ランキング上位に付与する『スター』ロールを設定します")
    @has_permission("ADMIN")
    async def config_star_role(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO market_config (key, value) VALUES ('star_role_id', ?)", (str(role.id),))
            await db.commit()
        await interaction.followup.send(f"✅ 上位報酬ロールを {role.mention} に設定しました。", ephemeral=True)

    @app_commands.command(name="株_結果ログ設定", description="【管理者】昇格・降格の結果を発表するチャンネルを設定します")
    @has_permission("ADMIN")
    async def config_promo_log(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO market_config (key, value) VALUES ('promotion_log_id', ?)", (str(channel.id),))
            await db.commit()
        await interaction.followup.send(f"✅ 結果発表先を {channel.mention} に設定しました。", ephemeral=True)

    @app_commands.command(name="株_上場", description="自分の株を上場します（キャスト限定）")
    async def ipo(self, interaction):
        await self.init_market_db()
        user = interaction.user

        # ロールチェック
        cast_role_id = None
        async with self.bot.get_db() as db:
            async with db.execute("SELECT value FROM market_config WHERE key = 'cast_role_id'") as c:
                row = await c.fetchone()
                if row: cast_role_id = int(row['value'])
        
        if not cast_role_id:
            return await interaction.response.send_message("❌ システムエラー: キャストロールが未設定です。管理者に連絡してください。", ephemeral=True)

        has_cast_role = any(r.id == cast_role_id for r in user.roles)
        if not has_cast_role:
             return await interaction.response.send_message("❌ 上場できるのは『キャスト』のみです。", ephemeral=True)

        async with self.bot.get_db() as db:
            try:
                await db.execute("INSERT INTO stock_issuers (user_id, total_shares) VALUES (?, 0)", (user.id,))
                await db.commit()
                await interaction.response.send_message(f"🎉 {user.mention} が株式市場に上場しました！\n誰でもこの株を売買して利益を狙えます。")
            except:
                await interaction.response.send_message("既に上場済みです。", ephemeral=True)

    @app_commands.command(name="株_取引パネル", description="株の売買パネルを開きます")
    async def open_panel(self, interaction: discord.Interaction, target: discord.Member):
        await self.init_market_db()
        view = StockControlView(self, target)
        embed = await view.update_embed(interaction)
        if embed: await interaction.response.send_message(embed=embed, view=view)
        else: await interaction.response.send_message("その人は上場していません。", ephemeral=True)

    @app_commands.command(name="株_ランキング", description="現在の株価ランキングと次回の審査日を表示します")
    async def ranking(self, interaction: discord.Interaction):
        await self.init_market_db()
        await interaction.response.defer()
        
        next_date_str = "未定"
        async with self.bot.get_db() as db:
            async with db.execute("SELECT user_id, total_shares FROM stock_issuers WHERE is_listed=1") as c: rows = await c.fetchall()
            async with db.execute("SELECT value FROM market_config WHERE key = 'next_promotion_date'") as c:
                row = await c.fetchone()
                if row:
                    dt = datetime.datetime.fromisoformat(row['value'])
                    next_date_str = dt.strftime("%m/%d %H:%M")

        data = []
        for r in rows:
            p = self.calculate_price(r['total_shares'])
            m = interaction.guild.get_member(r['user_id'])
            # 退室したメンバーなどは除外
            if not m: continue
            
            name = m.display_name
            data.append((name, p, r['total_shares']))
        
        # 株価順（=発行数順）にソート
        data.sort(key=lambda x: x[1], reverse=True)
        
        desc = f"📅 **次回審査: {next_date_str}**\n上位4名が『スター』に昇格します。\n\n"
        
        for i, d in enumerate(data[:10]):
            rank_icon = "👑" if i < 4 else f"{i+1}."
            bold = "**" if i < 4 else ""
            line = f"{rank_icon} {bold}{d[0]}{bold}: 株価 {d[1]:,} S (流通: {d[2]}株)\n"
            desc += line
            
        if len(data) > 10: desc += f"\n...他 {len(data)-10} 名"

        embed = discord.Embed(title="📊 キャスト株価ランキング", description=desc, color=discord.Color.gold())
        embed.set_footer(text="株を買うと価格が上がり、売ると下がります。推しをスターに押し上げよう！")
        await interaction.followup.send(embed=embed)

import io
import datetime
import matplotlib.pyplot as plt
import japanize_matplotlib # 日本語を表示するために必須です！

def generate_economy_dashboard(balances, history, flow_stats, type_breakdown, total_asset, avg_asset, active_citizens, active_days):
    """
    見やすさ重視・日本語解説付きの縦長ダッシュボード
    """
    plt.style.use('dark_background')
    
    # スマホ・Discordでそのまま読める縦長レイアウト
    fig = plt.figure(figsize=(10, 15))
    gs = fig.add_gridspec(3, 1, height_ratios=[1.2, 1.2, 1.0])

    # --- 1. 上段: マクロ経済推移 ---
    ax1 = fig.add_subplot(gs[0])
    try:
        dates = [r['date'][5:] for r in history]
        totals = [r['total_balance'] for r in history]
    except TypeError:
        dates = [r[0][5:] for r in history]
        totals = [r[1] for r in history]

    ax1.plot(dates, totals, marker='o', color='#00d2ff', linewidth=3)
    ax1.fill_between(dates, totals, color='#00d2ff', alpha=0.15)
    ax1.set_title(f"💰 サーバー全体の資金量推移 (総額: {total_asset:,} S)", fontweight='bold', fontsize=16, pad=15)
    ax1.grid(True, alpha=0.2, linestyle='--')
    if len(dates) > 10: ax1.set_xticks(dates[::max(1, len(dates)//7)])

    # --- 2. 中段: 資産分布（格差カーブ） ---
    ax2 = fig.add_subplot(gs[1])
    sorted_bal = sorted(balances)
    count = len(sorted_bal)
    x_users = list(range(1, count + 1))
    
    ax2.plot(x_users, sorted_bal, color='#f1c40f', linewidth=3)
    ax2.fill_between(x_users, sorted_bal, color='#f1c40f', alpha=0.2)
    ax2.set_title("⚖️ 市民の資産分布（格差カーブ）", fontweight='bold', fontsize=16, pad=15)
    ax2.set_xlabel("市民（左から右へ、資産が少ない順 → 多い順）", fontsize=12)
    ax2.set_ylabel("所持金 (S)", fontsize=12)
    ax2.grid(True, alpha=0.2, linestyle='--')

    # ジニ係数の計算と日本語での状況判定
    if total_asset > 0 and count > 0:
        gini = (2 * sum((i + 1) * v for i, v in enumerate(sorted_bal)) / (count * total_asset)) - (count + 1) / count
        
        # 0に近いほど平等、1に近いほど格差が大きい
        if gini < 0.3: status = "非常に平等な社会です 🕊️"
        elif gini < 0.4: status = "適度な競争がある正常な経済です 🏃"
        elif gini < 0.5: status = "少し格差が広がっています ⚠️"
        else: status = "深刻な格差社会です（一部への富の集中） 🚨"
    else:
        gini = 0
        status = "データなし"

    # グラフ内に判定結果を目立つように表示
    bbox_props = dict(boxstyle="round,pad=0.5", fc="#2b2d31", ec="#f1c40f", lw=2)
    ax2.text(0.05, 0.85, f"ジニ係数: {gini:.3f}\n【評価】 {status}", 
             transform=ax2.transAxes, fontsize=14, color='white', bbox=bbox_props)

    # --- 3. 下段: 日本語の経済サマリーテキスト ---
    ax3 = fig.add_subplot(gs[2])
    ax3.axis('off') # 枠線を消す
    
    net_flow = flow_stats['mint'] - flow_stats['burn']
    flow_sign = "+" if net_flow >= 0 else ""
    median_asset = int(sorted_bal[count//2]) if sorted_bal else 0
    turnover = (flow_stats['gdp'] / total_asset * 100) if total_asset else 0

    # スッキリと箇条書きでまとめる
    summary_text = (
        f"📋 【経済レポート】\n\n"
        f"👥 アクティブ市民数 : {active_citizens} 人\n"
        f"🏦 サーバー総資産   : {total_asset:,} S\n"
        f"📊 平均資産         : {int(avg_asset):,} S\n"
        f"🎯 中央値(一般的な層): {median_asset:,} S\n\n"
        f"💸 【24時間のお金の動き】\n"
        f"📥 発行額(Mint)     : {flow_stats['mint']:,} S\n"
        f"📤 回収額(Burn)     : {flow_stats['burn']:,} S\n"
        f"📈 差し引き増加量   : {flow_sign}{net_flow:,} S\n"
        f"🔄 流通量(GDP)      : {flow_stats['gdp']:,} S  (資金回転率: {turnover:.2f}%)\n"
    )

    ax3.text(0.1, 0.9, summary_text, transform=ax3.transAxes, fontsize=15, color='white', verticalalignment='top')

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100)
    buf.seek(0)
    plt.close(fig)
    return buf


class ServerStats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if not self.daily_log_task.is_running():
            self.daily_log_task.start()

    def cog_unload(self):
        self.daily_log_task.cancel()

    async def get_economic_details(self):
        """経済データを詳細に収集する"""
        guild = self.bot.guilds[0]
        if not guild.chunked: await guild.chunk()

        async with self.bot.get_db() as db:
            # 1. 設定読み込み
            god_role_ids = [r_id for r_id, level in self.bot.config.admin_roles.items() if level == "SUPREME_GOD"]
            citizen_role_id = None
            active_days = 30
            async with db.execute("SELECT key, value FROM server_config") as cursor:
                async for row in cursor:
                    if row['key'] == 'citizen_role_id': citizen_role_id = int(row['value'])
                    elif row['key'] == 'active_threshold_days': active_days = int(row['value'])

            # 2. アクティブユーザー判定 & 口座取得
            cutoff = datetime.datetime.now() - datetime.timedelta(days=active_days)
            async with db.execute("SELECT user_id, balance FROM accounts") as cursor:
                all_accounts = await cursor.fetchall()

            async with db.execute("SELECT DISTINCT sender_id FROM transactions WHERE created_at > ? UNION SELECT DISTINCT receiver_id FROM transactions WHERE created_at > ?", (cutoff, cutoff)) as cursor:
                rows = await cursor.fetchall()
                active_ids = {r[0] for r in rows}

            # 3. 24時間以内の動向分析
            cutoff_24h = datetime.datetime.now() - datetime.timedelta(days=1)
            flow_stats = {"mint": 0, "burn": 0, "transfer": 0, "gdp": 0}
            type_breakdown = {}

            query = "SELECT sender_id, receiver_id, amount, type FROM transactions WHERE created_at > ?"
            async with db.execute(query, (cutoff_24h,)) as cursor:
                async for row in cursor:
                    s_id, r_id, amt, t_type = row['sender_id'], row['receiver_id'], row['amount'], row['type']
                    flow_stats["gdp"] += amt
                    type_breakdown[t_type] = type_breakdown.get(t_type, 0) + amt
                    if s_id == 0: flow_stats["mint"] += amt
                    elif r_id == 0: flow_stats["burn"] += amt
                    else: flow_stats["transfer"] += amt

        # 4. 市民データのフィルタリング
        valid_balances = []
        for row in all_accounts:
            uid, bal = row['user_id'], row['balance']
            member = guild.get_member(uid)
            if not member or member.bot: continue
            if any(r.id in god_role_ids for r in member.roles): continue
            if citizen_role_id and not any(r.id == citizen_role_id for r in member.roles): continue
            if uid not in active_ids: continue
            valid_balances.append(bal)

        return valid_balances, flow_stats, type_breakdown, active_days

    @tasks.loop(hours=24)
    async def daily_log_task(self):
        try:
            balances, _, _, _ = await self.get_economic_details()
            total = sum(balances)
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            async with self.bot.get_db() as db:
                await db.execute("CREATE TABLE IF NOT EXISTS daily_stats (date TEXT PRIMARY KEY, total_balance INTEGER)")
                await db.execute("INSERT OR REPLACE INTO daily_stats (date, total_balance) VALUES (?, ?)", (today, total))
                await db.commit()
        except Exception as e:
            logger.error(f"Daily Log Error: {e}")

    @app_commands.command(name="経済グラフ", description="サーバー経済の詳細ダッシュボードを生成します（非同期生成）")
    @has_permission("ADMIN")
    async def economy_graph(self, interaction: discord.Interaction):
        # 処理開始を通知（これでタイムアウトを防ぐ）
        await interaction.response.defer()
        
        try:
            # 1. データの収集（DBアクセスは非同期で軽いのでそのまま）
            balances, flow_stats, type_breakdown, active_days = await self.get_economic_details()
            
            # データ加工
            balances.sort()
            count = len(balances)
            total_asset = sum(balances)
            avg_asset = total_asset / count if count > 0 else 0

            # 履歴データの取得
            async with self.bot.get_db() as db:
                async with db.execute("SELECT date, total_balance FROM daily_stats ORDER BY date ASC") as c:
                    history = await c.fetchall()

            # 2. 【重要】グラフ描画を別スレッドで実行
            # これにより、matplotlibがBot本体の動作を止めるのを防ぎます
            loop = asyncio.get_running_loop()
            buf = await loop.run_in_executor(
                None, 
                generate_economy_dashboard, 
                balances, history, flow_stats, type_breakdown, total_asset, avg_asset, count, active_days
            )

            # 3. 結果の送信
            file = discord.File(buf, filename="economy_dashboard.png")
            
            embed = discord.Embed(title="📊 ステラ経済ダッシュボード", color=0x2b2d31)
            embed.set_image(url="attachment://economy_dashboard.png")
            embed.set_footer(text=f"Generated in background thread | {datetime.datetime.now().strftime('%H:%M:%S')}")

            await interaction.followup.send(embed=embed, file=file)

        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(f"❌ レポート生成中にエラーが発生しました: {e}")

# --- 購入確認View ---
class ShopPurchaseView(discord.ui.View):
    def __init__(self, bot, role_id, price, shop_id, item_type, max_per_user):
        super().__init__(timeout=None)
        self.bot = bot
        self.role_id = role_id
        self.price = price
        self.shop_id = shop_id
        self.item_type = item_type          # 'rental' / 'permanent' / 'ticket'
        self.max_per_user = max_per_user

    def _button_label(self):
        if self.item_type == "rental":    return "購入する (30日間)"
        if self.item_type == "permanent": return "購入する (永続)"
        if self.item_type == "ticket":    return "購入する (引換券)"
        return "購入する"

    @discord.ui.button(style=discord.ButtonStyle.green, emoji="🛒")
    async def buy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # ボタンラベルを動的に設定できないのでdeferしてから処理
        await interaction.response.defer(ephemeral=True)
        user = interaction.user

        # --- チケット枚数上限チェック ---
        if self.item_type == "ticket" and self.max_per_user > 0:
            async with self.bot.get_db() as db:
                async with db.execute(
                    "SELECT COUNT(*) as cnt FROM ticket_inventory WHERE user_id = ? AND item_key = ? AND used_at IS NULL",
                    (user.id, self.role_id)
                ) as c:
                    row = await c.fetchone()
                    if row['cnt'] >= self.max_per_user:
                        return await interaction.followup.send(
                            f"❌ このチケットは1人 **{self.max_per_user}枚** までしか持てません。\n（未使用チケットを先に使ってください）",
                            ephemeral=True
                        )

        # --- ロール系: 既に持っているか確認 ---
        if self.item_type in ("rental", "permanent"):
            role = interaction.guild.get_role(self.role_id)
            if not role:
                return await interaction.followup.send("❌ この商品は現在取り扱われていません。", ephemeral=True)
            if role in user.roles:
                return await interaction.followup.send(
                    f"❌ すでに **{role.name}** を持っています。",
                    ephemeral=True
                )

        # --- 残高チェック ---
        async with self.bot.get_db() as db:
            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (user.id,)) as c:
                row = await c.fetchone()
                balance = row['balance'] if row else 0

        if balance < self.price:
            return await interaction.followup.send(
                f"❌ お金が足りません。\n(価格: {self.price:,} S / 所持金: {balance:,} S)",
                ephemeral=True
            )

        # --- 購入処理 ---
        month_tag = datetime.datetime.now().strftime("%Y-%m")
        try:
            async with self.bot.get_db() as db:
                await db.execute(
                    "UPDATE accounts SET balance = balance - ? WHERE user_id = ?",
                    (self.price, user.id)
                )
                await db.execute(
                    "INSERT INTO transactions (sender_id, receiver_id, amount, type, description, month_tag) VALUES (?, 0, ?, 'SHOP', ?, ?)",
                    (user.id, self.price, f"購入: Shop({self.shop_id}) item({self.role_id})", month_tag)
                )

                if self.item_type == "rental":
                    expiry_date = datetime.datetime.now() + datetime.timedelta(days=30)
                    await db.execute(
                        "INSERT OR REPLACE INTO shop_subscriptions (user_id, role_id, expiry_date) VALUES (?, ?, ?)",
                        (user.id, self.role_id, expiry_date.strftime("%Y-%m-%d %H:%M:%S"))
                    )

                elif self.item_type == "ticket":
                    # チケットをインベントリに追加
                    async with db.execute(
                        "SELECT description FROM shop_items WHERE role_id = ? AND shop_id = ?",
                        (str(self.role_id), self.shop_id)
                    ) as c:
                        item_row = await c.fetchone()
                        item_name = item_row['description'] if item_row else "チケット"
                    await db.execute(
                        "INSERT INTO ticket_inventory (user_id, shop_id, item_key, item_name) VALUES (?, ?, ?, ?)",
                        (user.id, self.shop_id, str(self.role_id), item_name)
                    )

                await db.commit()

        except Exception as e:
            await db.rollback()
            return await interaction.followup.send(f"❌ エラーが発生しました: {e}", ephemeral=True)

        # --- ロール付与 ---
        if self.item_type in ("rental", "permanent"):
            try:
                role = interaction.guild.get_role(self.role_id)
                await user.add_roles(role, reason=f"ショップ購入({self.shop_id})")
                if self.item_type == "rental":
                    expiry_str = expiry_date.strftime('%Y/%m/%d')
                    msg = f"🎉 **購入完了！**\n**{role.name}** を購入しました。\n有効期限: **{expiry_str}** まで\n(-{self.price:,} S)"
                else:
                    msg = f"🎉 **購入完了！**\n**{role.name}** を永続付与しました。\n(-{self.price:,} S)"
                await interaction.followup.send(msg, ephemeral=True)
            except discord.Forbidden:
                await interaction.followup.send("⚠️ 購入処理は完了しましたが、権限不足でロールを付与できませんでした。", ephemeral=True)

        elif self.item_type == "ticket":
            await interaction.followup.send(
                f"🎟️ **チケット購入完了！**\n**{item_name}** を1枚取得しました。\n"
                f"管理者が確認し次第、特典が付与されます。\n(-{self.price:,} S)",
                ephemeral=True
            )


# --- 商品選択メニュー ---
class ShopSelect(discord.ui.Select):
    def __init__(self, bot, items, shop_id):
        self.bot = bot
        self.shop_id = shop_id

        TYPE_EMOJI = {"rental": "⏳", "permanent": "♾️", "ticket": "🎟️"}
        TYPE_LABEL = {"rental": "30日", "permanent": "永続", "ticket": "引換券"}

        options = []
        for item in items:
            t = item['item_type']
            label = f"{item['name']} ({item['price']:,} S)"
            desc = f"[{TYPE_LABEL.get(t, '?')}] {item['desc'] or '説明なし'}"
            options.append(discord.SelectOption(
                label=label[:100],
                description=desc[:100],
                value=str(item['role_id']),
                emoji=TYPE_EMOJI.get(t, "🏷️")
            ))
        super().__init__(
            placeholder="購入したい商品を選択してください...",
            min_values=1, max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        role_id_str = self.values[0]
        async with self.bot.get_db() as db:
            async with db.execute(
                "SELECT * FROM shop_items WHERE role_id = ? AND shop_id = ?",
                (role_id_str, self.shop_id)
            ) as c:
                row = await c.fetchone()

        if not row:
            return await interaction.response.send_message("❌ 商品情報が取得できませんでした。", ephemeral=True)

        item_type = row['item_type'] or 'rental'
        price = row['price']
        max_per_user = row['max_per_user'] or 0
        role_id = int(role_id_str)

        TYPE_LABEL = {"rental": "30日レンタル", "permanent": "買い切り（永続）", "ticket": "引換券"}
        TYPE_EMOJI = {"rental": "⏳", "permanent": "♾️", "ticket": "🎟️"}

        if item_type in ("rental", "permanent"):
            role = interaction.guild.get_role(role_id)
            color = role.color if role else discord.Color.gold()
            name_str = role.mention if role else f"ID:{role_id}"
        else:
            color = discord.Color.purple()
            name_str = f"🎟️ {row['description'] or 'チケット'}"

        embed = discord.Embed(
            title=f"🛒 購入確認 ({TYPE_LABEL.get(item_type, '?')})",
            color=color
        )
        embed.add_field(name="商品", value=name_str, inline=False)
        embed.add_field(name="価格", value=f"**{price:,} Stell**", inline=True)
        embed.add_field(name="種別", value=f"{TYPE_EMOJI.get(item_type)} {TYPE_LABEL.get(item_type)}", inline=True)
        if item_type == "ticket" and max_per_user > 0:
            embed.add_field(name="所持上限", value=f"{max_per_user}枚まで", inline=True)

        view = ShopPurchaseView(self.bot, role_id, price, self.shop_id, item_type, max_per_user)
        # ボタンラベルをitem_typeに合わせて変更
        view.buy_button.label = view._button_label()
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
        async with self.bot.get_db() as db:
            async with db.execute(
                "SELECT user_id, role_id FROM shop_subscriptions WHERE expiry_date < ?", (now_str,)
            ) as cursor:
                expired_rows = await cursor.fetchall()

        if not expired_rows:
            return

        guild = self.bot.guilds[0]
        async with self.bot.get_db() as db:
            for row in expired_rows:
                member = guild.get_member(row['user_id'])
                role = guild.get_role(row['role_id'])
                if member and role and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="ショップ有効期限切れ")
                        try:
                            await member.send(f"⏳ **有効期限切れ**\nロール **{role.name}** の有効期限（30日）が終了しました。")
                        except:
                            pass
                    except:
                        pass
                await db.execute(
                    "DELETE FROM shop_subscriptions WHERE user_id = ? AND role_id = ?",
                    (row['user_id'], row['role_id'])
                )
            await db.commit()

    @check_subscription_expiry.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    # ▼▼▼ 1. 商品登録 ▼▼▼
    @app_commands.command(name="ショップ_商品登録", description="ショップに商品を登録します")
    @app_commands.rename(shop_id="ショップid", role="商品ロール", price="価格", description="説明文", item_type="種別", max_per_user="所持上限")
    @app_commands.describe(
        shop_id="配置するショップID（例: main）",
        role="対象のロール（チケットの場合は識別用に適当なロールを指定）",
        price="価格 (Stell)",
        description="商品説明文",
        item_type="rental=30日 / permanent=永続 / ticket=引換券",
        max_per_user="チケットの所持上限（0=無制限）"
    )
    @app_commands.choices(item_type=[
        app_commands.Choice(name="⏳ 期限付き (30日)", value="rental"),
        app_commands.Choice(name="♾️ 買い切り (永続)", value="permanent"),
        app_commands.Choice(name="🎟️ 引換券チケット", value="ticket"),
    ])
    @has_permission("SUPREME_GOD")
    async def shop_add(self, interaction: discord.Interaction, shop_id: str, role: discord.Role, price: int, description: str = None, item_type: str = "rental", max_per_user: int = 0):
        await interaction.response.defer(ephemeral=True)
        if price < 0:
            return await interaction.followup.send("❌ 価格は0以上にしてください。", ephemeral=True)

        async with self.bot.get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO shop_items (role_id, shop_id, price, description, item_type, max_per_user) VALUES (?, ?, ?, ?, ?, ?)",
                (str(role.id), shop_id, price, description, item_type, max_per_user)
            )
            await db.commit()

        TYPE_LABEL = {"rental": "30日", "permanent": "永続", "ticket": "引換券"}
        await interaction.followup.send(
            f"✅ ショップ(`{shop_id}`) に **{role.name}** ({price:,} S / {TYPE_LABEL.get(item_type)}) を登録しました。",
            ephemeral=True
        )

    # ▼▼▼ 2. 商品削除 ▼▼▼
    @app_commands.command(name="ショップ_商品削除", description="ショップから商品を取り下げます")
    @app_commands.rename(shop_id="ショップid", role="削除ロール")
    @app_commands.describe(shop_id="削除したい商品があるショップID", role="削除するロール")
    @has_permission("SUPREME_GOD")
    async def shop_remove(self, interaction: discord.Interaction, shop_id: str, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute(
                "DELETE FROM shop_items WHERE role_id = ? AND shop_id = ?",
                (str(role.id), shop_id)
            )
            await db.commit()
        await interaction.followup.send(f"🗑️ ショップ(`{shop_id}`) から **{role.name}** を削除しました。", ephemeral=True)

    # ▼▼▼ 3. パネル設置 ▼▼▼
    @app_commands.command(name="ショップ_パネル設置", description="指定したIDのショップパネルを設置します")
    @app_commands.rename(shop_id="ショップid", title="タイトル", content="本文", image_url="画像url")
    @app_commands.describe(shop_id="表示するショップID", title="パネルタイトル", content="パネル本文", image_url="画像URL（任意）")
    @has_permission("SUPREME_GOD")
    async def shop_panel(self, interaction: discord.Interaction, shop_id: str, title: str = "🛒 ステラショップ", content: str = "欲しい商品を選択してください！", image_url: str = None):
        await interaction.response.defer()

        async with self.bot.get_db() as db:
            async with db.execute(
                "SELECT * FROM shop_items WHERE shop_id = ?", (shop_id,)
            ) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            return await interaction.followup.send(f"❌ ショップID `{shop_id}` に商品がありません。", ephemeral=True)

        items = []
        TYPE_EMOJI = {"rental": "⏳", "permanent": "♾️", "ticket": "🎟️"}
        TYPE_LABEL = {"rental": "30日", "permanent": "永続", "ticket": "引換券"}
        item_list_text = ""

        for row in rows:
            role = interaction.guild.get_role(int(row['role_id']))
            if not role:
                continue
            t = row['item_type'] or 'rental'
            items.append({
                'role_id': int(row['role_id']),
                'name': role.name,
                'price': row['price'],
                'desc': row['description'],
                'item_type': t,
                'max_per_user': row['max_per_user'] or 0,
            })
            limit_str = f"（上限{row['max_per_user']}枚）" if t == "ticket" and row['max_per_user'] > 0 else ""
            item_list_text += f"{TYPE_EMOJI.get(t)} **{role.name}**: `{row['price']:,} S` [{TYPE_LABEL.get(t)}]{limit_str}\n"

        if not items:
            return await interaction.followup.send("❌ 有効な商品がありません。", ephemeral=True)

        embed = discord.Embed(title=title, description=content, color=discord.Color.gold())
        if image_url:
            embed.set_image(url=image_url)
        embed.add_field(name="📦 ラインナップ", value=item_list_text, inline=False)

        view = ShopPanelView(self.bot, items, shop_id)
        await interaction.followup.send(embed=embed, view=view)

    # ▼▼▼ 4. チケット確認（管理者向け） ▼▼▼
    @app_commands.command(name="チケット確認", description="【管理者】未使用チケットの一覧を確認します")
    @app_commands.describe(shop_id="対象のショップID（省略で全件）")
    @has_permission("GODDESS")
    async def ticket_list(self, interaction: discord.Interaction, shop_id: str = None):
        await interaction.response.defer(ephemeral=True)

        async with self.bot.get_db() as db:
            if shop_id:
                async with db.execute(
                    "SELECT * FROM ticket_inventory WHERE used_at IS NULL AND shop_id = ? ORDER BY purchased_at ASC",
                    (shop_id,)
                ) as c:
                    rows = await c.fetchall()
            else:
                async with db.execute(
                    "SELECT * FROM ticket_inventory WHERE used_at IS NULL ORDER BY purchased_at ASC"
                ) as c:
                    rows = await c.fetchall()

        if not rows:
            return await interaction.followup.send("✅ 未使用チケットはありません。", ephemeral=True)

        embed = discord.Embed(
            title=f"🎟️ 未使用チケット一覧",
            description=f"{len(rows)}件",
            color=discord.Color.purple()
        )

        for row in rows:
            purchased = row['purchased_at'][:16] if row['purchased_at'] else "不明"
            embed.add_field(
                name=f"ID:{row['id']} | {row['item_name']}",
                value=f"所持者: <@{row['user_id']}>\n購入日: {purchased}",
                inline=False
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ▼▼▼ 5. チケット使用済みにする（管理者向け） ▼▼▼
    @app_commands.command(name="チケット処理済み", description="【管理者】チケットを処理済みにします")
    @app_commands.describe(ticket_id="チケットID（/チケット確認 で確認できます）")
    @has_permission("GODDESS")
    async def ticket_use(self, interaction: discord.Interaction, ticket_id: int):
        await interaction.response.defer(ephemeral=True)

        async with self.bot.get_db() as db:
            async with db.execute(
                "SELECT * FROM ticket_inventory WHERE id = ?", (ticket_id,)
            ) as c:
                row = await c.fetchone()

            if not row:
                return await interaction.followup.send(f"❌ チケットID `{ticket_id}` が見つかりません。", ephemeral=True)
            if row['used_at']:
                return await interaction.followup.send(f"❌ チケットID `{ticket_id}` は既に処理済みです。", ephemeral=True)

            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await db.execute(
                "UPDATE ticket_inventory SET used_at = ?, used_by = ? WHERE id = ?",
                (now_str, interaction.user.id, ticket_id)
            )
            await db.commit()

        # 購入者にDM通知
        try:
            user = interaction.client.get_user(row['user_id']) or await interaction.client.fetch_user(row['user_id'])
            await user.send(
                f"🎟️ **チケット処理完了**\n"
                f"**{row['item_name']}** のチケット（ID: {ticket_id}）が処理されました。\n"
                f"特典付与をお待ちください。"
            )
        except:
            pass

        await interaction.followup.send(
            f"✅ チケットID `{ticket_id}` を処理済みにしました。\n"
            f"対象: <@{row['user_id']}> / 内容: **{row['item_name']}**",
            ephemeral=True
            )



# --- 3. 管理者ツール (整理版) ---
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

    @app_commands.command(name="給与額設定", description="役職ごとの給与額を設定します")
    @has_permission("SUPREME_GOD")
    async def config_set_wage(self, interaction: discord.Interaction, role: discord.Role, amount: int):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO role_wages (role_id, amount) VALUES (?, ?)", (role.id, amount))
            await db.commit()
        await self.bot.config.reload()
        await interaction.followup.send(f"✅ 設定を更新しました。", ephemeral=True)

    @app_commands.command(name="vc報酬追加", description="報酬対象のVCを追加します")
    @has_permission("SUPREME_GOD")
    async def add_reward_vc(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR IGNORE INTO reward_channels (channel_id) VALUES (?)", (channel.id,))
            await db.commit()
        
        vc_cog = self.bot.get_cog("VoiceSystem")
        if vc_cog: await vc_cog.reload_targets()
        await interaction.followup.send(f"✅ {channel.mention} を報酬対象に追加しました。", ephemeral=True)

    @app_commands.command(name="vc報酬解除", description="報酬対象のVCを解除します")
    @has_permission("SUPREME_GOD")
    async def remove_reward_vc(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("DELETE FROM reward_channels WHERE channel_id = ?", (channel.id,))
            await db.commit()

        vc_cog = self.bot.get_cog("VoiceSystem")
        if vc_cog: await vc_cog.reload_targets()
        await interaction.followup.send(f"🗑️ {channel.mention} を報酬対象から除外しました。", ephemeral=True)

    @app_commands.command(name="vc報酬リスト", description="報酬対象のVC一覧を表示します")
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

    @app_commands.command(name="経済集計ロール付与", description="経済統計の対象とする「市民ロール」を設定します")
    @has_permission("SUPREME_GOD")
    async def config_citizen_role(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('citizen_role_id', ?)", (str(role.id),))
            await db.commit()
        await self.bot.config.reload()
        await interaction.followup.send(f"✅ 経済統計の対象を **{role.name}** を持つメンバーに限定しました。", ephemeral=True)
        
    @app_commands.command(name="経済集計アクティブ判定期間", description="経済統計に含める「アクティブ期間（日数）」を設定します")
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


    @app_commands.command(name="ギャンブル制限解除", description="【管理者】指定ユーザーまたはロールの今日のプレイ制限を解除します")
    @app_commands.describe(
        target="対象ユーザー（ロールと同時指定不可）",
        role="対象ロール（そのロールの全員を解除）",
        game="解除するゲーム"
    )
    @app_commands.choices(game=[
        app_commands.Choice(name="チンチロ", value="chinchiro"),
        app_commands.Choice(name="スロット", value="slot"),
        app_commands.Choice(name="両方", value="all"),
    ])
    @has_permission("ADMIN")
    async def lift_play_limit(self, interaction: discord.Interaction, game: str, target: Optional[discord.Member] = None, role: Optional[discord.Role] = None):
        await interaction.response.defer(ephemeral=True)

        if not target and not role:
            return await interaction.followup.send("❌ ユーザーかロールのどちらかを指定してください。", ephemeral=True)
        if target and role:
            return await interaction.followup.send("❌ ユーザーとロールは同時に指定できません。", ephemeral=True)

        today = datetime.datetime.now().strftime("%Y-%m-%d")
        games = ["chinchiro", "slot"] if game == "all" else [game]

        # 対象メンバーリストを作成
        if target:
            members = [target]
        else:
            members = [m for m in role.members if not m.bot]
            if not members:
                return await interaction.followup.send(f"❌ {role.mention} にメンバーがいません。", ephemeral=True)

        async with self.bot.get_db() as db:
            for m in members:
                for g in games:
                    await db.execute("""
                        INSERT OR IGNORE INTO daily_play_exemptions (user_id, game, date)
                        VALUES (?, ?, ?)
                    """, (m.id, g, today))
            await db.commit()

        game_str = "チンチロ・スロット両方" if game == "all" else ("チンチロ" if game == "chinchiro" else "スロット")
        if target:
            msg = f"✅ {target.mention} の **{game_str}** の本日の制限を解除しました。"
        else:
            msg = f"✅ {role.mention} ({len(members)}名) の **{game_str}** の本日の制限を解除しました。"

        await interaction.followup.send(msg, ephemeral=True)
# --- 追加: 面接用のUIパネル ---
class InterviewPanelView(discord.ui.View):
    def __init__(self, bot, routes, probation_role_id):
        super().__init__(timeout=None)
        self.bot = bot
        self.routes = routes
        self.probation_role_id = probation_role_id
        self.selected_user = None

        # 対象者を選択するプルダウン
        self.add_item(InterviewUserSelect())

        # 登録されているルートボタンを動的に生成
        for slot, data in self.routes.items():
            btn = discord.ui.Button(
                label=data['desc'],
                emoji=data['emoji'],
                style=discord.ButtonStyle.primary,
                custom_id=f"eval_route_{slot}"
            )
            btn.callback = self.make_callback(slot, data)
            self.add_item(btn)

    def make_callback(self, slot, data):
        async def callback(interaction: discord.Interaction):
            if not self.selected_user:
                return await interaction.response.send_message("❌ 先に上のメニューから対象者(研修生)を選択してください。", ephemeral=True)

            await interaction.response.defer(ephemeral=True)
            member = interaction.guild.get_member(self.selected_user.id)
            if not member:
                return await interaction.followup.send("❌ 対象のユーザーがサーバーに見つかりません。", ephemeral=True)

            probation_role = interaction.guild.get_role(self.probation_role_id)
            new_role = interaction.guild.get_role(data['role_id'])
            bonus_amount = 30000
            month_tag = datetime.datetime.now().strftime("%Y-%m")

            try:
                # ロールの付け替え
                if probation_role and probation_role in member.roles:
                    await member.remove_roles(probation_role, reason="面接完了: 仮ロール削除")
                if new_role:
                    await member.add_roles(new_role, reason=f"面接完了: {data['desc']}ルート")

                # 祝金の付与
                async with self.bot.get_db() as db:
                    await db.execute("""
                        INSERT INTO accounts (user_id, balance, total_earned) VALUES (?, ?, 0)
                        ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance
                    """, (member.id, bonus_amount))
                    
                    await db.execute("""
                        INSERT INTO transactions (sender_id, receiver_id, amount, type, description, month_tag)
                        VALUES (0, ?, ?, 'BONUS', ?, ?)
                    """, (member.id, bonus_amount, f"面接合格: {data['desc']}", month_tag))
                    await db.commit()

                # ログ送信
                embed = discord.Embed(title="🌸 面接個別評価 完了", color=discord.Color.gold())
                embed.add_field(name="対象者", value=member.mention, inline=True)
                embed.add_field(name="決定ルート", value=f"{data['emoji']} {data['desc']}", inline=True)
                embed.add_field(name="付与ロール", value=new_role.mention if new_role else "なし", inline=False)
                embed.add_field(name="祝金", value=f"**{bonus_amount:,} Stell**", inline=False)
                embed.set_footer(text=f"担当面接官: {interaction.user.display_name}")

                log_ch_id = None
                async with self.bot.get_db() as db:
                    async with db.execute("SELECT value FROM server_config WHERE key = 'interview_log_id'") as c:
                        row = await c.fetchone()
                        if row: log_ch_id = int(row['value'])
                
                if log_ch_id:
                    log_ch = self.bot.get_channel(log_ch_id)
                    if log_ch: await log_ch.send(embed=embed)

                await interaction.followup.send(f"✅ **{member.display_name}** を **{data['desc']}** ルートで処理し、祝金を付与しました。", ephemeral=True)

            except Exception as e:
                logger.error(f"Interview Error: {e}")
                await interaction.followup.send(f"❌ 処理中にエラーが発生しました: {e}", ephemeral=True)

        return callback

# --- Cog: InterviewSystem (2段階評価システム) ---
class DynamicEvalView(discord.ui.View):
    def __init__(self, user_id, base_role_id, routes):
        super().__init__(timeout=None) # タイムアウトなしで2週間後でも押せるようにする
        
        # データベースに登録されているルートの数だけボタンを生成
        for slot, data in routes.items():
            btn = discord.ui.Button(
                label=data['desc'],
                emoji=data['emoji'],
                style=discord.ButtonStyle.primary,
                # custom_id に「ユーザーID」「剥奪する旧ロールID」「付与する新ロールID」を埋め込む（再起動対策）
                custom_id=f"eval_route:{user_id}:{base_role_id}:{data['role_id']}"
            )
            self.add_item(btn)

class InterviewSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # --- 1. 面接の基本設定 ---
    @app_commands.command(name="面接設定_ルート", description="【管理者】2週間後の評価分岐ルート(1〜5)を設定します")
    @app_commands.describe(slot="設定枠 (1~5)", role="付与するロール", emoji="ボタンの絵文字", description="ルート名（天使ルート等）")
    @app_commands.choices(slot=[app_commands.Choice(name=f"ルート {i}", value=i) for i in range(1, 6)])
    @has_permission("SUPREME_GOD")
    async def config_eval_branch(self, interaction: discord.Interaction, slot: int, role: discord.Role, emoji: str, description: str):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES (?, ?)", (f"branch_{slot}_role", str(role.id)))
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES (?, ?)", (f"branch_{slot}_emoji", emoji))
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES (?, ?)", (f"branch_{slot}_desc", description))
            await db.commit()
        await interaction.followup.send(f"✅ **ルート {slot}** を設定しました。\n{emoji} {description} ➡ {role.mention}", ephemeral=True)

    @app_commands.command(name="評価パネル送信先設定", description="【管理者】VC面接通過後、2週間後の評価パネルを送るチャンネルを設定します")
    @has_permission("SUPREME_GOD")
    async def config_eval_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('eval_channel_id', ?)", (str(channel.id),))
            await db.commit()
        await interaction.followup.send(f"✅ VC面接通過後の「評価待ちパネル」を {channel.mention} に送信するよう設定しました。", ephemeral=True)

    # --- 2. 除外ロールの管理 (複数対応) ---
    @app_commands.command(name="面接除外_追加", description="【管理者】VC一括合格の対象から外すロール(面接官など)を追加します")
    @has_permission("SUPREME_GOD")
    async def add_exclude_role(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            async with db.execute("SELECT value FROM server_config WHERE key = 'interview_exclude_roles'") as c:
                row = await c.fetchone()
                current = row['value'].split(',') if row and row['value'] else []
            
            if str(role.id) not in current:
                current.append(str(role.id))
                await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('interview_exclude_roles', ?)", (','.join(current),))
                await db.commit()
                await interaction.followup.send(f"✅ {role.mention} を除外ロールに追加しました。", ephemeral=True)
            else:
                await interaction.followup.send(f"⚠️ {role.mention} は既に除外ロールに登録されています。", ephemeral=True)

    @app_commands.command(name="面接除外_削除", description="【管理者】登録されている除外ロールを解除します")
    @has_permission("SUPREME_GOD")
    async def remove_exclude_role(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            async with db.execute("SELECT value FROM server_config WHERE key = 'interview_exclude_roles'") as c:
                row = await c.fetchone()
                current = row['value'].split(',') if row and row['value'] else []
            
            if str(role.id) in current:
                current.remove(str(role.id))
                await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('interview_exclude_roles', ?)", (','.join(current),))
                await db.commit()
                await interaction.followup.send(f"🗑️ {role.mention} を除外ロールから削除しました。", ephemeral=True)
            else:
                await interaction.followup.send(f"⚠️ {role.mention} は除外ロールに登録されていません。", ephemeral=True)

    @app_commands.command(name="面接除外_一覧", description="【管理者】現在登録されている除外ロールの一覧を確認します")
    @has_permission("ADMIN")
    async def list_exclude_roles(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            async with db.execute("SELECT value FROM server_config WHERE key = 'interview_exclude_roles'") as c:
                row = await c.fetchone()
                current = row['value'].split(',') if row and row['value'] else []

        if not current:
            return await interaction.followup.send("📝 除外ロールは登録されていません。", ephemeral=True)

        mentions = [f"<@&{role_id}>" for role_id in current]
        embed = discord.Embed(title="🛡️ 面接除外ロール一覧", description="\n".join(mentions), color=discord.Color.blue())
        await interaction.followup.send(embed=embed, ephemeral=True)


    # --- 3. 実行コマンド: VC一括面接 (Phase 1) ---
    @app_commands.command(name="面接_vc一括合格", description="【管理者】VC内の対象者を合格させ、2週間後の評価パネルを自動生成します")
    @app_commands.describe(target_role="変更前のロール(Aロール)", new_role="変更後のロール(Bロール)")
    @has_permission("ADMIN")
    async def pass_interview_vc(self, interaction: discord.Interaction, target_role: discord.Role, new_role: discord.Role):
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.response.send_message("❌ VCに参加してから実行してください。", ephemeral=True)
        
        channel = interaction.user.voice.channel
        await interaction.response.defer(ephemeral=True) # ★自分だけに表示

        exclude_roles = []
        eval_channel_id = None
        routes = {}

        # DBから設定を読み込む
        async with self.bot.get_db() as db:
            async with db.execute("SELECT value FROM server_config WHERE key = 'interview_exclude_roles'") as c:
                row = await c.fetchone()
                if row and row['value']: exclude_roles = [int(x) for x in row['value'].split(',')]
            
            async with db.execute("SELECT value FROM server_config WHERE key = 'eval_channel_id'") as c:
                row = await c.fetchone()
                if row: eval_channel_id = int(row['value'])

            for i in range(1, 6):
                async with db.execute("SELECT key, value FROM server_config WHERE key LIKE ?", (f"branch_{i}_%",)) as c:
                    rows = await c.fetchall()
                    data = {}
                    for r in rows:
                        if r['key'].endswith('_role'): data['role_id'] = int(r['value'])
                        elif r['key'].endswith('_emoji'): data['emoji'] = r['value']
                        elif r['key'].endswith('_desc'): data['desc'] = r['value']
                    if 'role_id' in data: routes[i] = data

        processed_members = []
        bonus_amount = 30000
        month_tag = datetime.datetime.now().strftime("%Y-%m")

        # 対象者のロール付け替えと祝金付与
        async with self.bot.get_db() as db:
            for member in channel.members:
                if member.bot: continue
                if any(r.id in exclude_roles for r in member.roles): continue
                if target_role not in member.roles: continue

                try:
                    await member.remove_roles(target_role, reason="面接一括合格: Aロール削除")
                    await member.add_roles(new_role, reason="面接一括合格: Bロール付与")
                    
                    await db.execute("""
                        INSERT INTO accounts (user_id, balance, total_earned) VALUES (?, ?, 0)
                        ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance
                    """, (member.id, bonus_amount))
                    
                    await db.execute("""
                        INSERT INTO transactions (sender_id, receiver_id, amount, type, description, month_tag)
                        VALUES (0, ?, ?, 'BONUS', '面接一括合格祝い', ?)
                    """, (member.id, bonus_amount, month_tag))
                    
                    processed_members.append(member)
                except Exception as e:
                    logger.error(f"Interview Error: {e}")
            await db.commit()

        if not processed_members:
            return await interaction.followup.send("⚠️ 対象となるメンバーがいませんでした。", ephemeral=True)

        # 実行者(自分)への結果報告（Ephemeral）
        embed = discord.Embed(title="🌸 VC面接 合格処理完了", color=discord.Color.brand_green())
        embed.add_field(name="処理人数", value=f"{len(processed_members)} 名", inline=False)
        embed.add_field(name="ロール変更", value=f"{target_role.mention} ➡ {new_role.mention}", inline=False)
        names = ", ".join([m.display_name for m in processed_members])
        embed.add_field(name="対象者", value=names[:1000], inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

        # 指定チャンネルへ評価パネル(備忘録)を送信
        if eval_channel_id and routes:
            eval_ch = self.bot.get_channel(eval_channel_id)
            if eval_ch:
                for member in processed_members:
                    view = DynamicEvalView(member.id, new_role.id, routes)
                    msg_embed = discord.Embed(
                        title=f"📋 評価待ち: {member.display_name}", 
                        description=f"現在のロール: {new_role.mention}\n2週間後、決定したルートのボタンを押してください。",
                        color=0x2b2d31
                    )
                    msg_embed.set_thumbnail(url=member.display_avatar.url)
                    await eval_ch.send(content=f"{member.mention}", embed=msg_embed, view=view)


    # --- 4. ボタンが押された時の処理 (Phase 2: 2週間後の評価) ---
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        # コンポーネント(ボタン)じゃなければ無視
        if interaction.type != discord.InteractionType.component: return
        
        custom_id = interaction.data.get("custom_id", "")
        # 面接の評価ボタンじゃなければ無視
        if not custom_id.startswith("eval_route:"): return

        # eval_route:{user_id}:{base_role_id}:{new_role_id} の形式で情報を抽出
        parts = custom_id.split(":")
        if len(parts) != 4: return
        
        target_id = int(parts[1])
        base_role_id = int(parts[2])
        new_role_id = int(parts[3])

        await interaction.response.defer(ephemeral=True)

        member = interaction.guild.get_member(target_id)
        if not member:
            return await interaction.followup.send("❌ ユーザーが既にサーバーにいないようです。", ephemeral=True)

        base_role = interaction.guild.get_role(base_role_id)
        new_role = interaction.guild.get_role(new_role_id)

        try:
            # ロールの付け替え (Bロールを剥奪して、C/Dロールを付与)
            if base_role and base_role in member.roles:
                await member.remove_roles(base_role, reason="2週間評価: Bロール剥奪")
            if new_role:
                await member.add_roles(new_role, reason="2週間評価: ルート確定ロール付与")

            # 押したボタンのあるメッセージを更新(ボタンを消して完了済みにする)
            completed_embed = interaction.message.embeds[0]
            completed_embed.color = discord.Color.gold()
            completed_embed.title = f"✅ 評価完了: {member.display_name}"
            completed_embed.description = f"決定ルート: {new_role.mention if new_role else '不明'}\n担当: {interaction.user.display_name}"
            
            # ビューを空にしてメッセージを更新
            await interaction.message.edit(embed=completed_embed, view=None)
            await interaction.followup.send(f"✅ {member.display_name} の評価を完了し、ロールを更新しました。", ephemeral=True)

        except Exception as e:
            logger.error(f"Eval Error: {e}")
            await interaction.followup.send("❌ ロールの変更中にエラーが発生しました。権限などを確認してください。", ephemeral=True)


# --- Bot 本体 ---
class CestaBankBot(commands.Bot):
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
        
        self.db_path = "stella_bank_v1.db"
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
        await self.add_cog(HumanStockMarket(self))

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
        logger.info("StellaBank System: Setup complete and All Cogs Synced.")

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
        import datetime
        import glob  # ファイル検索用
        import os    # ファイル削除用

        # 1. 新しいバックアップを作成
        backup_name = f"backup_{datetime.datetime.now().strftime('%Y%m%d')}.db"
        try:
            async with self.get_db() as db:
                await db.execute(f"VACUUM INTO '{backup_name}'")
            
            logger.info(f"Auto Backup Success: {backup_name}")

            # 2. 古いバックアップを削除 (最新3世代のみ残す)
            # "backup_*.db" に一致するファイルをすべて取得して、名前順(日付順)に並べる
            backups = sorted(glob.glob("backup_*.db"))
            
            # バックアップが3つより多い場合、古いものから削除する
            if len(backups) > 3:
                # リストの「後ろから3つ」を除いたもの（＝古いファイル）を対象にループ
                for old_bk in backups[:-3]:
                    try:
                        os.remove(old_bk) # ファイル削除
                        logger.info(f"Deleted old backup: {old_bk}")
                    except Exception as e:
                        logger.error(f"Failed to delete {old_bk}: {e}")

        except Exception as e:
            logger.error(f"Backup Failure: {e}")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print("--- Stella Bank System Online ---")
        
# --- 実行ブロック ---
if __name__ == "__main__":
    if not TOKEN:
        logging.error("DISCORD_TOKEN is missing")
    else:
        # ボットの起動
        bot = CestaBankBot()
        bot.run(TOKEN)
