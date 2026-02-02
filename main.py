import discord
import keep_alive
import matplotlib
matplotlib.use('Agg') # サーバー上でグラフを描くための設定
import matplotlib.pyplot as plt
import io
import pandas as pd
from discord.ext import commands, tasks
from discord import app_commands, ui
import aiosqlite
import datetime
import uuid
import asyncio
import logging
import contextlib
import os
from typing import Optional, List, Dict
from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler

# --- 環境変数とロギング ---
# load_dotenv() の中身を空にすることで、標準の「.env」を探し、
# なければKoyebなどのシステム環境変数を直接見に行くようになります。
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
    # どこから読み込もうとしたかの特定パスを出さないようにし、汎用性を高めます
    logging.error("DISCORD_TOKEN is missing. Please check your Environment Variables or .env file.")
else:
    logging.info("DISCORD_TOKEN loaded successfully.")

# ログファイルの設定
# ※Koyebの無料枠では再起動で消えますが、動作自体に支障はありません。
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
        
        # ★ ここを追加！ ユーザーごとの設定（DM通知のON/OFFなど）を保存するテーブル
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

        # 4. インデックス（検索を速くする）
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

        # 権限設定: 見れる、入れる、喋れる、書ける
        perms = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            stream=True,
            use_voice_activation=True,
            send_messages=True,          # インチャ許可
            read_message_history=True    # 履歴許可
        )

        added_users = []
        for member in select.values:
            if member.bot: continue
            await channel.set_permissions(member, overwrite=perms)
            added_users.append(member.display_name)

        await interaction.followup.send(f"✅ 以下のメンバーを招待しました:\n{', '.join(added_users)}", ephemeral=True)
        # VC内にも通知
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
            # 自分自身やBotは消せないようにする
            if member.id == interaction.user.id: continue
            if member.bot: continue
            
            # 権限をリセット（Defaultに戻す＝見えなくなる）
            await channel.set_permissions(member, overwrite=None)
            
            # もしVCに入っていたら切断させる
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
        # メニューの作成
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

        # 1. 既存VCチェック
        async with bot.get_db() as db:
            async with db.execute("SELECT channel_id FROM temp_vcs WHERE owner_id = ?", (user.id,)) as cursor:
                existing_vc = await cursor.fetchone()
            if existing_vc:
                return await interaction.followup.send("❌ あなたは既に一時VCを作成しています。", ephemeral=True)

        hours = int(self.values[0])
        price = self.prices.get(str(hours), 5000)

        # 2. 残高チェック & 支払い
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
            await db.commit() # 確定

        # 3. VC作成処理
        try:
            guild = interaction.guild
            category = interaction.channel.category
            
            # 基本: 全員アクセス不可
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
                # オーナー: チャンネル管理権限を持たせない設定
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

            # DB登録
            expire_dt = datetime.datetime.now() + datetime.timedelta(hours=hours)
            async with bot.get_db() as db:
                await db.execute(
                    "INSERT INTO temp_vcs (channel_id, guild_id, owner_id, expire_at) VALUES (?, ?, ?, ?)",
                    (new_vc.id, guild.id, user.id, expire_dt)
                )
                await db.commit()

            # パネル送信
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


# --- Cog: PrivateVCManager (修正版: タイムアウト対策済み) ---
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

        # DB書き込み (commitを使用)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('vc_price_6', ?)", (str(price_6h),))
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('vc_price_12', ?)", (str(price_12h),))
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('vc_price_24', ?)", (str(price_24h),))
            await db.commit()

        # Embed作成
        embed = discord.Embed(title=title, description=description, color=0x2b2d31)
        embed.set_footer(text=f"Last Updated: {datetime.datetime.now().strftime('%Y/%m/%d %H:%M')}")
        
        # パネル送信
        await interaction.channel.send(embed=embed, view=VCPanel())
        # 完了通知 (defer済みなので followup)
        await interaction.followup.send("✅ 設定を保存し、パネルを設置しました。", ephemeral=True)

# --- 送金確認用のボタン ---
class TransferConfirmView(discord.ui.View):
    def __init__(self, bot, sender, receiver, amount):
        super().__init__(timeout=60)
        self.bot = bot
        self.sender = sender
        self.receiver = receiver
        self.amount = amount
        self.processed = False

    @discord.ui.button(label="✅ 送金する", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.processed: return
        self.processed = True
        
        # ボタンを押した後の処理（ローディング表示）
        await interaction.response.defer(ephemeral=True)
        
        sender_new_bal = 0
        receiver_new_bal = 0
        month_tag = datetime.datetime.now().strftime("%Y-%m")

        try:
            async with self.bot.get_db() as db:
                try:
                    # 1. 残高を減らす
                    await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance) VALUES (?, 0)", (self.sender.id,))
                    cursor = await db.execute(
                        "UPDATE accounts SET balance = balance - ? WHERE user_id = ? AND balance >= ?", 
                        (self.amount, self.sender.id, self.amount)
                    )
                    
                    if cursor.rowcount == 0:
                        return await interaction.followup.send(f"❌ 残高が足りません。", ephemeral=True)

                    # 2. 残高を増やす
                    await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance) VALUES (?, 0)", (self.receiver.id,))
                    await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (self.amount, self.receiver.id))
                    
                    # 3. 履歴保存
                    await db.execute(
                        "INSERT INTO transactions (sender_id, receiver_id, amount, type, description, month_tag) VALUES (?, ?, ?, 'TRANSFER', ?, ?)",
                        (self.sender.id, self.receiver.id, self.amount, f"{self.sender.display_name}からの送金", month_tag)
                    )
                    
                    # ログ用データ取得
                    async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (self.sender.id,)) as c:
                        sender_new_bal = (await c.fetchone())['balance']
                    async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (self.receiver.id,)) as c:
                        receiver_new_bal = (await c.fetchone())['balance']

                    await db.commit()

                except Exception as db_err:
                    await db.rollback()
                    raise db_err

            # 完了メッセージ（ボタンを無効化して更新）
            await interaction.edit_original_response(content=f"✅ 送金成功: {self.receiver.mention} へ {self.amount:,} L 送りました。", embed=None, view=None)
            
            # ログ出力
            log_ch_id = None
            async with self.bot.get_db() as db:
                async with db.execute("SELECT value FROM server_config WHERE key = 'currency_log_id'") as c:
                    row = await c.fetchone()
                    if row: log_ch_id = int(row['value'])
            
            if log_ch_id:
                channel = self.bot.get_channel(log_ch_id)
                if channel:
                    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC+09:00")
                    embed = discord.Embed(title="送金ログ", color=0xFFD700, timestamp=datetime.datetime.now())
                    embed.set_author(name="ElysionBOT", icon_url=self.bot.user.display_avatar.url)
                    embed.description = f"{self.sender.mention} から {self.receiver.mention} へ **{self.amount:,} Ru** 送金されました。"
                    embed.add_field(name="メモ", value="なし", inline=False)
                    embed.add_field(
                        name="残高", 
                        value=f"送金者: {sender_new_bal:,} Ru\n受取者: {receiver_new_bal:,} Ru", 
                        inline=False
                    )
                    embed.add_field(name="実行時刻", value=now_str, inline=False)
                    await channel.send(embed=embed)

        except Exception as e:
            logger.error(f"Transfer Error: {e}")
            await interaction.followup.send("❌ エラーが発生しました。", ephemeral=True)

    @discord.ui.button(label="❌ キャンセル", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.processed = True
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

    # --- 1. 残高確認 (デザイン修正) ---
    @app_commands.command(name="残高確認", description="現在の所持金を確認します")
    async def balance(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        target = member or interaction.user
        
        # 権限チェック (他人の口座を見る場合)
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

    # --- 2. 送金コマンド (確認ボタン呼び出し) ---
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
        
        # 下記で定義する View を呼び出す
        view = TransferConfirmView(self.bot, interaction.user, receiver, amount, message)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # --- 3. 取引履歴 (Ru表記へ修正) ---
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

class TransferConfirmView(discord.ui.View):
    def __init__(self, bot, sender, receiver, amount, message):
        super().__init__(timeout=60)
        self.bot = bot
        self.sender = sender
        self.receiver = receiver
        self.amount = amount
        self.msg = message

    @discord.ui.button(label="送金を実行する", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        async with self.bot.get_db() as db:
            # 送金元の残高チェック
            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (self.sender.id,)) as c:
                row = await c.fetchone()
                if not row or row['balance'] < self.amount:
                    return await interaction.followup.send("❌ 残高が不足しています。", ephemeral=True)

            try:
                # 送金処理
                await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (self.amount, self.sender.id))
                await db.execute("""
                    INSERT INTO accounts (user_id, balance) VALUES (?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance
                """, (self.receiver.id, self.amount))
                
                # 履歴保存
                await db.execute("""
                    INSERT INTO transactions (sender_id, receiver_id, amount, type, description)
                    VALUES (?, ?, ?, 'TRANSFER', ?)
                """, (self.sender.id, self.receiver.id, self.amount, self.msg))
                
                await db.commit()
                self.stop()
                await interaction.followup.send(f"✅ {self.receiver.mention} へ {self.amount:,} Ru 送金しました。", ephemeral=True)

                # ★ 受取通知 DM (画像 1000004644.png の再現)
                try:
                    # DM通知設定を確認（Salaryで追加した設定を流用）
                    async with db.execute("SELECT dm_salary_enabled FROM user_settings WHERE user_id = ?", (self.receiver.id,)) as c:
                        res = await c.fetchone()
                        if res and res['dm_salary_enabled'] == 0: return # 通知OFFなら送らない

                    embed = discord.Embed(title="💰 Ru_men受取通知", color=discord.Color.green())
                    embed.add_field(name="送金者", value=self.sender.mention, inline=False)
                    embed.add_field(name="受取額", value=f"**{self.amount:,} Ru**", inline=False)
                    embed.add_field(name="メッセージ", value=f"`{self.msg}`", inline=False)
                    embed.timestamp = datetime.datetime.now()
                    
                    await self.receiver.send(embed=embed)
                except:
                    pass # DMが閉鎖されている場合は無視

            except Exception as e:
                await db.rollback()
                await interaction.followup.send(f"❌ エラーが発生しました: {e}", ephemeral=True)

class Salary(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # --- 1. 給与通知設定コマンド ---
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

    # --- 2. 一括給与支給コマンド (明細生成・DM送信対応) ---
    @app_commands.command(name="一括給与", description="【最高神】全役職の給与を合算支給し、明細をDM送信します")
    @has_permission("SUPREME_GOD")
    async def distribute_all(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        now = datetime.datetime.now()
        month_tag = now.strftime("%Y-%m")
        batch_id = str(uuid.uuid4())[:8]
        
        # 設定の読み込み
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
        payout_data_list = [] # DM送信用のデータ保持用

        # メンバーリストの取得
        members = interaction.guild.members if interaction.guild.chunked else [m async for m in interaction.guild.fetch_members()]

        async with self.bot.get_db() as db:
            for member in members:
                if member.bot: continue
                
                # 該当ロールを抽出
                matching = [(wage_dict[r.id], r) for r in member.roles if r.id in wage_dict]
                if not matching: continue
                
                member_total = sum(w for w, _ in matching)
                
                # DB更新
                await db.execute("""
                    INSERT INTO accounts (user_id, balance, total_earned) VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET 
                    balance = balance + excluded.balance, total_earned = total_earned + excluded.total_earned
                """, (member.id, member_total, member_total))
                
                await db.execute("""
                    INSERT INTO transactions (sender_id, receiver_id, amount, type, batch_id, month_tag, description)
                    VALUES (0, ?, ?, 'SALARY', ?, ?, ?)
                """, (member.id, member_total, batch_id, month_tag, f"{month_tag} 給与"))

                # ログ・内訳用集計
                count += 1
                total_payout += member_total
                for w, r in matching:
                    if r.id not in role_summary: role_summary[r.id] = {"mention": r.mention, "count": 0, "amount": 0}
                    role_summary[r.id]["count"] += 1
                    role_summary[r.id]["amount"] += w

                # DM送信対象であればリストに追加
                if dm_prefs.get(member.id, True): # デフォルトはON
                    payout_data_list.append((member, member_total, matching))

            await db.commit()

        # DM送信実行
        sent_dm = 0
        for m, total, matching in payout_data_list:
            try:
                embed = self.create_salary_slip_embed(m, total, matching, month_tag)
                await m.send(embed=embed)
                sent_dm += 1
            except: pass # DM拒否設定などはスルー

        await interaction.followup.send(f"💰 **一括支給完了** (ID: `{batch_id}`)\n人数: {count}名 / 総額: {total_payout:,} Ru\n通知送信: {sent_dm}名")
        await self.send_salary_log(interaction, batch_id, total_payout, count, role_summary, now)

    # --- 3. 給与明細作成 (画像再現ロジック) ---
    def create_salary_slip_embed(self, member, total, matching, month_tag):
        # 金額の高い順に並び替え
        sorted_matching = sorted(matching, key=lambda x: x[0], reverse=True)
        main_role = sorted_matching[0][1] # 一番高い給与のロール
        
        embed = discord.Embed(
            title="💰 月給支給のお知らせ",
            description=f"**{month_tag}** の月給が支給されました！",
            color=0x00FF00, # 画像に合わせた緑色
            timestamp=datetime.datetime.now()
        )
        
        embed.add_field(name="💵 支給総額", value=f"**{total:,} Ru**", inline=False)
        
        # 計算式の作成 (例: 500,000 + 50,000...)
        formula = " + ".join([f"{w:,}" for w, r in sorted_matching])
        embed.add_field(name="🧮 計算式", value=f"{formula} = **{total:,} Ru**", inline=False)
        
        # 内訳の作成
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

    # --- 4. 給与一覧表示 ---
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

    # --- 5. ロールバックコマンド ---
    @app_commands.command(name="一括給与取り消し", description="【最高神】識別ID(Batch ID)を指定して給与支給を取り消します")
    @has_permission("SUPREME_GOD")
    async def salary_rollback(self, interaction: discord.Interaction, batch_id: str):
        await interaction.response.defer(ephemeral=True)
        
        async with self.bot.get_db() as db:
            async with db.execute("SELECT receiver_id, amount FROM transactions WHERE batch_id = ? AND type = 'SALARY'", (batch_id,)) as cursor:
                rows = await cursor.fetchall()
            
            if not rows:
                return await interaction.followup.send(f"❌ ID `{batch_id}` の給与データが見つかりません。", ephemeral=True)
            
            total_reverted = sum(row['amount'] for row in rows)
            count = len(rows)
            
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

    # --- 6. 共通: ログ送信 ---
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

    

# --- Cog: VoiceSystem  ---
class VoiceSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # 1つのIDではなく、複数のIDを保持するセット(集合)に変更
        self.target_vc_ids = set() 
        self.is_ready_processed = False

    async def reload_targets(self):
        """DBから報酬対象のVCリストを再読み込みする"""
        try:
            async with self.bot.get_db() as db:
                async with db.execute("SELECT channel_id FROM reward_channels") as cursor:
                    rows = await cursor.fetchall()
            
            self.target_vc_ids = {row['channel_id'] for row in rows}
            # ログに読み込み数を表示
            logger.info(f"Loaded {len(self.target_vc_ids)} reward VC targets.")
        except Exception as e:
            logger.error(f"Failed to load reward channels: {e}")

    def is_active(self, state):
        """対象リストに含まれるVCにいて、かつミュートしていないか判定"""
        return (
            state and 
            state.channel and 
            state.channel.id in self.target_vc_ids and  # リストに含まれているかチェック
            not state.self_deaf and 
            not state.deaf
        )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot: return
        now = datetime.datetime.now()
        was_active, is_now_active = self.is_active(before), self.is_active(after)

        # 報酬対象エリアに入った（またはミュート解除した）
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

        # 報酬対象エリアから出た（またはミュートした）
        elif was_active and not is_now_active:
            await self._process_reward(member, now)

    async def _process_reward(self, member_or_id, now):
        user_id = member_or_id.id if isinstance(member_or_id, discord.Member) else member_or_id
        try:
            async with self.bot.get_db() as db:
                # まず入室時間を取得
                async with db.execute("SELECT join_time FROM voice_tracking WHERE user_id =?", (user_id,)) as cursor:
                    row = await cursor.fetchone()
                if not row: return

                # ★修正: db.begin() を削除し、手動コミットへ変更
                try:
                    join_time = datetime.datetime.fromisoformat(row['join_time'])
                    sec = int((now - join_time).total_seconds())
                    
                    # 1分未満は切り捨て
                    if sec < 60:
                        reward = 0
                    else:
                        reward = (sec * 50) // 60 

                    if reward > 0:
                        month_tag = now.strftime("%Y-%m")
                        
                        # 1. システム口座(ID:0)を確実に作る（エラー回避）
                        await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance, total_earned) VALUES (0, 0, 0)")

                        # 2. ユーザーの口座を作る
                        await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance, total_earned) VALUES (?, 0, 0)", (user_id,))
                        
                        # 3. 残高と統計を更新
                        await db.execute(
                            "UPDATE accounts SET balance = balance +?, total_earned = total_earned +? WHERE user_id =?", 
                            (reward, reward, user_id)
                        )
                        await db.execute("INSERT OR IGNORE INTO voice_stats (user_id) VALUES (?)", (user_id,))
                        await db.execute("UPDATE voice_stats SET total_seconds = total_seconds +? WHERE user_id =?", (sec, user_id))
                        
                        # 4. 取引履歴（システムからユーザーへ）
                        await db.execute(
                            "INSERT INTO transactions (sender_id, receiver_id, amount, type, description, month_tag) VALUES (0, ?, ?, 'VC_REWARD', 'VC活動報酬', ?)",
                            (user_id, reward, month_tag)
                        )
                    
                    # 5. 追跡データを削除（報酬0でも削除する）
                    await db.execute("DELETE FROM voice_tracking WHERE user_id =?", (user_id,))
                    
                    # ★最後にコミット
                    await db.commit()

                    # ログ出力（コミット成功後）
                    if reward > 0:
                        embed = discord.Embed(title="🎙 VC報酬精算", color=discord.Color.blue(), timestamp=now)
                        embed.add_field(name="ユーザー", value=f"<@{user_id}>")
                        embed.add_field(name="付与額", value=f"{reward:,} L")
                        embed.add_field(name="滞在時間", value=f"{sec // 60}分")
                        await self.bot.send_admin_log(embed)

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
        
        # 再起動時のリカバリー処理
        try:
            async with self.bot.get_db() as db:
                async with db.execute("SELECT user_id FROM voice_tracking") as cursor:
                    tracked_users = await cursor.fetchall()
                
                for row in tracked_users:
                    u_id = row['user_id']
                    
                    # 現在サーバーにいて、かつ「対象のVCリストのどれか」にいるか確認
                    is_active_now = False
                    for guild in self.bot.guilds:
                        member = guild.get_member(u_id)
                        if member and self.is_active(member.voice):
                            is_active_now = True
                            break
                    
                    # 落ちていた間に抜けてしまっていたら精算
                    if not is_active_now:
                        await self._process_reward(u_id, now)
        except Exception as e:
            logger.error(f"Recovery Error: {e}")

class VoiceHistory(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # --- VC記録確認コマンド (女神以上) ---
    @app_commands.command(name="vc記録", description="【女神以上】指定したユーザーのVC累計滞在時間を画像で表示します")
    @app_commands.describe(member="確認したいユーザー")
    @has_permission("GODDESS") # 以前作成した権限チェック（女神 = index 1, 最高神 = 0 が実行可能）
    async def vc_history(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()

        # 1. データベースから累計秒数を取得
        async with self.bot.get_db() as db:
            async with db.execute("SELECT total_seconds FROM voice_stats WHERE user_id = ?", (member.id,)) as cursor:
                row = await cursor.fetchone()
                total_seconds = row['total_seconds'] if row else 0

        # 2. 時間の計算 (秒 -> 時間・分)
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        
        # 3. 画像の生成 (Pillowを使用)
        # 600x300のダークテーマなカードを作成
        img = Image.new('RGB', (600, 300), color=(44, 47, 51)) # Discord風の背景色
        draw = ImageDraw.Draw(img)
        
        # フォント設定 (サーバー内のパスに合わせて調整が必要な場合があります)
        try:
            # Linux標準のフォントパス例
            font_main = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 40)
            font_sub = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 25)
        except:
            font_main = ImageFont.load_default()
            font_sub = ImageFont.load_default()

        # テキストの描画
        draw.text((40, 40), f"VC STATS: {member.display_name}", fill=(255, 255, 255), font=font_sub)
        draw.text((40, 100), f"{hours} hours {minutes} mins", fill=(0, 255, 127), font=font_main)
        draw.text((40, 180), f"Total Seconds: {total_seconds:,}s", fill=(185, 187, 190), font=font_sub)
        
        # 下部に装飾ライン
        draw.rectangle([40, 240, 560, 245], fill=(114, 137, 218))

        # 画像をバイナリとして保存
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='PNG')
        img_byte_arr.seek(0)
        
        # 4. ファイルとして送信
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

        # 1. 除外ロール（説明者ロール）のIDを取得
        exclude_role_id = None
        async with self.bot.get_db() as db:
            async with db.execute("SELECT value FROM server_config WHERE key = 'exclude_role_id'") as cursor:
                row = await cursor.fetchone()
                if row:
                    exclude_role_id = int(row['value'])

        targets = []
        skipped_names = [] # 除外された人の名前リスト

        # 2. 対象者の決定ロジック
        if target:
            targets.append(target)
            mode_text = f"{target.mention} を"
        else:
            # 一括指定の場合
            if interaction.user.voice and interaction.user.voice.channel:
                channel = interaction.user.voice.channel
                raw_members = channel.members
                
                for m in raw_members:
                    # 除外ロールを持っているか確認
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

        # 3. 一括処理実行
        success_members = [] # 成功したメンバーオブジェクトを保存
        error_logs = []
        month_tag = datetime.datetime.now().strftime("%Y-%m")

        async with self.bot.get_db() as db:
            try:
                # 0. システム口座(ID:0)を確実に作る
                await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance, total_earned) VALUES (0, 0, 0)")

                for member in targets:
                    if member.bot: continue
                    
                    try:
                        # A. ロール付与
                        if role not in member.roles:
                            await member.add_roles(role, reason="面接通過コマンドによる付与")
                        
                        # B. お金付与
                        await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance) VALUES (?, 0)", (member.id,))
                        await db.execute(
                            "UPDATE accounts SET balance = balance + ?, total_earned = total_earned + ? WHERE user_id = ?", 
                            (amount, amount, member.id)
                        )
                        
                        # 取引履歴
                        await db.execute(
                            "INSERT INTO transactions (sender_id, receiver_id, amount, type, description, month_tag) VALUES (0, ?, ?, 'BONUS', ?, ?)",
                            (member.id, amount, f"面接通過祝い: {role.name}", month_tag)
                        )
                        
                        success_members.append(member) # 成功リストに追加
                        
                    except discord.Forbidden:
                        error_logs.append(f"⚠️ {member.display_name}: 権限不足でロールを付与できませんでした")
                    except Exception as e:
                        error_logs.append(f"❌ {member.display_name}: エラーが発生しました ({e})")
                        logger.error(f"Interview Command Error [{member.id}]: {e}")
                
                # ★最後にコミット
                await db.commit()

            except Exception as db_err:
                await db.rollback()
                logger.error(f"Interview Transaction Error: {db_err}")
                return await interaction.followup.send("❌ データベースエラーが発生しました。", ephemeral=True)

        # 4. コマンド実行者への結果報告
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

        # 5. ★追加部分：専用ログチャンネルへの出力
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
                
                # 成功者リスト（最大文字数対策で一部のみ表示）
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

class Chinchiro(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.dice_emojis = ["", "⚀", "⚁", "⚂", "⚃", "⚄", "⚅"]

    def roll_dice(self):
        dice = [random.randint(1, 6) for _ in range(3)]
        dice.sort()
        if dice[0] == dice[1] == dice[2]: return dice, 100 + dice[0], f"嵐 ({dice[0]})"
        if dice == [4, 5, 6]: return dice, 90, "シゴロ"
        if dice == [1, 2, 3]: return dice, -1, "ヒフミ"
        if dice[0] == dice[1]: return dice, dice[2], f"{dice[2]}の目"
        if dice[1] == dice[2]: return dice, dice[0], f"{dice[0]}の目"
        if dice[0] == dice[2]: return dice, dice[1], f"{dice[1]}の目"
        return dice, 0, "目なし"

    @app_commands.command(name="チンチロ", description="Ruを賭けて勝負！(※女神の加護で親が絶対有利だよ♡)")
    async def chinchiro(self, interaction: discord.Interaction, bet: int):
        if bet < 100: return await interaction.response.send_message("最低100Ruからだよ、ケチらないでよね？", ephemeral=True)
        
        await interaction.response.defer()
        user = interaction.user

        async with self.bot.get_db() as db:
            async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (user.id,)) as c:
                row = await c.fetchone()
                if not row or row['balance'] < bet:
                    return await interaction.followup.send("お財布空っぽじゃん、ざぁこ♡ 稼いできてから出直してよ。")

        embed = discord.Embed(title="🎲 チンチロリン・Elysion", color=0x2f3136)
        embed.set_author(name=f"{user.display_name}の挑戦♡", icon_url=user.display_avatar.url)
        embed.add_field(name="賭け金", value=f"{bet:,} Ru", inline=False)
        msg = await interaction.followup.send(embed=embed)

        # --- 親の番 ---
        embed.add_field(name="親(Bot)の出目", value="くるくる...", inline=True)
        await asyncio.sleep(1.5)
        p_dice, p_score, p_name = self.roll_dice()
        p_str = " ".join([self.dice_emojis[d] for d in p_dice])
        embed.set_field_at(1, name="親(Bot)の出目", value=f"**{p_name}**\n`{p_str}`", inline=True)
        await msg.edit(embed=embed)

        if p_score >= 90:
            return await self.process_result(interaction, msg, embed, user, bet, "lose", "親の強役だもん！文句ある？全部没収ね♡")

        # --- 子の番 ---
        embed.add_field(name="あなたの出目", value="くるくる...", inline=True)
        await asyncio.sleep(1.5)
        u_dice, u_score, u_name = self.roll_dice()
        u_str = " ".join([self.dice_emojis[d] for d in u_dice])
        embed.set_field_at(2, name="あなたの出目", value=f"**{u_name}**\n`{u_str}`", inline=True)
        await msg.edit(embed=embed)

        # --- 判定ロジック ---
        if u_score == -1: # ヒフミ
            await self.process_result(interaction, msg, embed, user, bet, "lose_double", "えっ、ヒフミ！？うわぁ…ダッサ♡ おバカさんには「2倍」払ってもらうね！")
        elif u_score > p_score:
            res_type = "win_triple" if u_score > 100 else "win_double" if u_score == 90 else "win"
            await self.process_result(interaction, msg, embed, user, bet, res_type, "チッ…運だけはいいんだね。ほら、女神への奉納金を引いた残額だよ。ありがたく受け取りなよ。")
        elif u_score == p_score:
            await self.process_result(interaction, msg, embed, user, bet, "lose", "同点は親の勝ちだよ！ルールも知らないの？Ruは美味しく焼却してあげるね♡")
        else:
            await self.process_result(interaction, msg, embed, user, bet, "lose", "はい負けー♡ そのRuはElysionのゴミ箱行きでーす！お疲れ様♡")

    async def process_result(self, interaction, msg, embed, user, bet, result, comment):
        multiplier = 0
        if "win_triple" in result: multiplier = 3
        elif "win_double" in result: multiplier = 2
        elif "win" in result: multiplier = 1
        elif "lose_double" in result: multiplier = -2
        else: multiplier = -1

        async with self.bot.get_db() as db:
            if multiplier > 0:
                raw_payout = bet * multiplier
                tax = int(raw_payout * 0.1) # 10%焼却
                final_payout = raw_payout - tax
                await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (final_payout, user.id))
                embed.color = 0x00ff00
                res_msg = f"**結果: +{final_payout:,} Ru** (奉納金: {tax} Ru)"
            else:
                loss_amount = bet * abs(multiplier)
                await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (loss_amount, user.id))
                await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = 0", (loss_amount,))
                embed.color = 0xff0000
                res_msg = f"**結果: -{loss_amount:,} Ru**"

            await db.execute("""
                INSERT INTO transactions (sender_id, receiver_id, amount, type, description)
                VALUES (?, ?, ?, 'GAMBLE', 'チンチロ勝負')
            """, (user.id if multiplier < 0 else 0, 0 if multiplier < 0 else user.id, abs(bet * multiplier)))
            await db.commit()

        embed.description = f"{res_msg}\n\n{comment}"
        await msg.edit(embed=embed)

# --- Cog: ServerStats (サーバー経済統計 & グラフ) ---
class ServerStats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.daily_log_task.start()

    def cog_unload(self):
        self.daily_log_task.cancel()

    # --- ヘルパー関数: 「アクティブな市民」の所持金リストを取得 ---
    async def get_citizen_balances(self):
        """
        以下の条件を全て満たす人の所持金リストを取得する
        1. 最高神ではない
        2. 市民ロールを持っている（設定がある場合）
        3. 直近〇〇日以内に取引履歴がある（設定がある場合）
        """
        guild = self.bot.guilds[0]
        
        # 1. 設定値の読み込み
        god_role_ids = []
        citizen_role_id = None
        active_threshold_days = 30 # デフォルトは30日（給料サイクル意識）
        
        async with self.bot.get_db() as db:
            # 最高神ロール
            for r_id, level in self.bot.config.admin_roles.items():
                if level == "SUPREME_GOD":
                    god_role_ids.append(r_id)
            
            # 各種設定ロード
            async with db.execute("SELECT key, value FROM server_config") as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    if row['key'] == 'citizen_role_id':
                        citizen_role_id = int(row['value'])
                    elif row['key'] == 'active_threshold_days':
                        active_threshold_days = int(row['value'])

        # 2. アクティブユーザーの特定（DBから高速判定）
        active_user_ids = set()
        cutoff_date = datetime.datetime.now() - datetime.timedelta(days=active_threshold_days)
        
        async with self.bot.get_db() as db:
            # 指定期間内に「送金した」or「受け取った」人をリストアップ
            sql = "SELECT DISTINCT sender_id, receiver_id FROM transactions WHERE created_at > ?"
            async with db.execute(sql, (cutoff_date,)) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    active_user_ids.add(row['sender_id'])
                    active_user_ids.add(row['receiver_id'])

        # 3. メンバーの選別
        balances = []
        
        # 所持金辞書
        user_balances = {}
        async with self.bot.get_db() as db:
            async with db.execute("SELECT user_id, balance FROM accounts") as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    user_balances[row['user_id']] = row['balance']

        if not guild.chunked:
            await guild.chunk()
            
        for member in guild.members:
            if member.bot: continue

            # A. 最高神は除外
            if any(role.id in god_role_ids for role in member.roles):
                continue
            
            # B. 市民ロールチェック
            if citizen_role_id:
                if not any(role.id == citizen_role_id for role in member.roles):
                    continue 

            # C. アクティブチェック（★ここが追加点）
            # 取引履歴にIDがない ＝ 期間中ずっと寝てた人
            if member.id not in active_user_ids:
                continue
            
            # 全条件クリア！
            bal = user_balances.get(member.id, 0)
            balances.append(bal)
        
        return balances, active_threshold_days # 日数も返す（表示用）

    # --- 毎日ログ ---
    @tasks.loop(hours=24)
    async def daily_log_task(self):
        now = datetime.datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        try:
            balances, _ = await self.get_citizen_balances()
            total_balance = sum(balances)
            
            async with self.bot.get_db() as db:
                await db.execute("CREATE TABLE IF NOT EXISTS daily_stats (date TEXT PRIMARY KEY, total_balance INTEGER)")
                await db.execute("INSERT OR REPLACE INTO daily_stats (date, total_balance) VALUES (?, ?)", (date_str, total_balance))
                await db.commit()
            logger.info(f"Daily Stats Logged: {date_str} = {total_balance:,} L")
        except Exception as e:
            logger.error(f"Daily Stats Error: {e}")

    @daily_log_task.before_loop
    async def before_daily_log(self):
        await self.bot.wait_until_ready()

    # --- グラフコマンド ---
    @app_commands.command(name="経済グラフ", description="【管理者】経済状況・インフレ率・格差・活発度を表示")
    @has_permission("ADMIN")
    async def economy_graph(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        
        # 1. 資産データ取得（アクティブ日数も受け取る）
        balances, active_days = await self.get_citizen_balances()
        current_total = sum(balances)
        citizen_count = len(balances)
        
        # 2. ジニ係数
        gini_val = 0.0
        gini_comment = "データなし"
        gini_text = "測定不能"
        
        if balances and current_total > 0:
            sorted_balances = sorted(balances)
            n = len(balances)
            numerator = 2 * sum((i + 1) * val for i, val in enumerate(sorted_balances))
            denominator = n * current_total
            gini_val = (numerator / denominator) - (n + 1) / n
            
            if gini_val < 0.2: gini_comment = "🟢 **超平等** (社会主義的ユートピア)"
            elif gini_val < 0.3: gini_comment = "🟢 **平和** (安定した経済)"
            elif gini_val < 0.4: gini_comment = "🟡 **普通** (よくある資本主義)"
            elif gini_val < 0.5: gini_comment = "🟠 **警戒** (富の集中が始まっています)"
            elif gini_val < 0.6: gini_comment = "🔴 **危険** (暴動が起きるレベル)"
            else: gini_comment = "💀 **崩壊** (王と奴隷の世界)"
            gini_text = f"{gini_val:.3f}"

        # 3. 経済の活発度
        tx_count = 0
        tx_comment = ""
        yesterday_time = datetime.datetime.now() - datetime.timedelta(days=1)
        
        async with self.bot.get_db() as db:
            await db.execute("CREATE TABLE IF NOT EXISTS daily_stats (date TEXT PRIMARY KEY, total_balance INTEGER)")
            await db.execute("INSERT OR REPLACE INTO daily_stats (date, total_balance) VALUES (?, ?)", (today_str, current_total))
            await db.commit()
            
            async with db.execute("SELECT date, total_balance FROM daily_stats ORDER BY date ASC") as cursor:
                rows = await cursor.fetchall()

            async with db.execute("SELECT COUNT(*) as cnt FROM transactions WHERE created_at > ?", (yesterday_time,)) as cursor:
                row = await cursor.fetchone()
                tx_count = row['cnt'] if row else 0

        if tx_count == 0: tx_comment = "💀 **死** (誰も使っていません)"
        elif tx_count < 5: tx_comment = "🧊 **停滞** (動きが鈍いです)"
        elif tx_count < 20: tx_comment = "🚶 **微動** (ボチボチです)"
        elif tx_count < 50: tx_comment = "🏃 **活発** (経済回ってます！)"
        else: tx_comment = "🔥 **過熱** (凄まじい取引量です)"

        # 4. インフレ率
        inflation_text = "データ不足"
        diff_amount = 0
        
        if len(rows) >= 2:
            today_data = rows[-1]
            prev_data = rows[-2]
            yesterday_total = prev_data['total_balance']
            today_total = today_data['total_balance']
            diff_amount = today_total - yesterday_total
            
            if yesterday_total > 0:
                rate = (diff_amount / yesterday_total) * 100
                sign = "+" if rate >= 0 else ""
                emoji = "📈" if rate > 0 else "📉" if rate < 0 else "➡️"
                inflation_text = f"{emoji} **{sign}{rate:.2f}%**"
            else:
                inflation_text = "♾️ (前回0)"
        else:
             inflation_text = "🔰 データ収集中"

        # 5. グラフ
        dates = [r['date'][5:] for r in rows]
        totals = [r['total_balance'] for r in rows]
        
        plt.figure(figsize=(10, 5))
        plt.plot(dates, totals, marker='o', linestyle='-', color='#00b0f4', linewidth=2)
        plt.title(f'Economy Growth ({rows[0]["date"]} - {today_str})')
        plt.grid(True, linestyle='--', alpha=0.7)
        plt.xticks(rotation=45)
        plt.tight_layout()
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        plt.close()
        file = discord.File(buf, filename="economy_graph.png")

        # 6. レポートEmbed
        embed = discord.Embed(title="📊 ルーメン経済レポート", color=discord.Color.blue(), timestamp=datetime.datetime.now())
        embed.set_thumbnail(url=interaction.guild.icon.url if interaction.guild.icon else None)
        
        embed.add_field(name="🔄 経済の回転 (24h取引数)", value=f"{tx_comment}\n(回数: `{tx_count}回`)", inline=False)
        embed.add_field(name="💹 経済成長率 (前回比)", value=f"# {inflation_text}", inline=True)
        embed.add_field(name="⚖️ 格差レベル (ジニ係数)", value=f"{gini_comment}\n(`{gini_text}`)", inline=True)
        
        # ターゲット説明をフッターに追加
        footer_text = f"※集計対象: 過去{active_days}日以内に取引がある市民"
        
        diff_sign = "+" if diff_amount >= 0 else ""
        embed.add_field(name=f"💰 アクティブ総資産 ({citizen_count}名)", value=f"{current_total:,} L\n({diff_sign}{diff_amount:,} L)", inline=False)

        embed.set_image(url="attachment://economy_graph.png")
        embed.set_footer(text=footer_text)

        await interaction.followup.send(embed=embed, file=file)


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
    @has_permission("SUPREME_GOD") # ここでメインファイルの has_permission を使います
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

# --- 3. 管理者ツール ---
class AdminTools(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ▼▼▼ 1. ログ出力先設定（3種類対応版） ▼▼▼
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

    # ▼▼▼ 2. 面接の除外ロール設定（★これが抜けてました！） ▼▼▼
    @app_commands.command(name="面接の除外ロール設定", description="【最高神】面接コマンドでスキップするロール（説明者など）を設定")
    @has_permission("SUPREME_GOD")
    async def config_exclude_role(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('exclude_role_id', ?)", (str(role.id),))
            await db.commit()
        await self.bot.config.reload()
        await interaction.followup.send(f"✅ 面接時に **{role.name}** を持つメンバーを除外（スキップ）するように設定しました。", ephemeral=True)

    #▼▼▼ 3. 管理者権限設定 ▼▼▼
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

    # ▼▼▼ 4. 給与額設定 ▼▼▼
    @app_commands.command(name="給与額設定", description="【最高神】役職ごとの給与額を設定します")
    @has_permission("SUPREME_GOD")
    async def config_set_wage(self, interaction: discord.Interaction, role: discord.Role, amount: int):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO role_wages (role_id, amount) VALUES (?, ?)", (role.id, amount))
            await db.commit()
        await self.bot.config.reload()
        await interaction.followup.send(f"✅ 設定を更新しました。", ephemeral=True)

    # ▼▼▼ 5. VC報酬設定エリア ▼▼▼
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
    # ▼▼▼ 追加: 市民ロール（集計対象）の設定 ▼▼▼
    @app_commands.command(name="経済集計ロール付与", description="【最高神】経済統計の対象とする「市民ロール」を設定します")
    @has_permission("SUPREME_GOD")
    async def config_citizen_role(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('citizen_role_id', ?)", (str(role.id),))
            await db.commit()
        await self.bot.config.reload()
        await interaction.followup.send(f"✅ 経済統計の対象を **{role.name}** を持つメンバーに限定しました。", ephemeral=True)
    # ▼▼▼ 追加: 経済統計の「アクティブ判定期間」を設定 ▼▼▼
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
        intents.members = True          # メンバー取得用
        intents.voice_states = True     # VC状態監視用
        intents.message_content = True  # メッセージコマンド用
        
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
            # 1. データの矛盾（幽霊ユーザーなど）を許さない設定
            await db.execute("PRAGMA foreign_keys = ON")
            # 2. DB混雑時に5秒間リトライする設定
            await db.execute("PRAGMA busy_timeout = 5000")
            yield db

    async def setup_hook(self):
        # データベースの初期セットアップ
        async with self.get_db() as db:
            await self.db_manager.setup(db)
        
        # 設定の読み込み
        await self.config.reload()
        
        # 永続的なView（ボタンなど）の登録
        self.add_view(VCPanel())
        
        # 各種機能（Cog）の読み込み
        # ★ ここをすべて self.add_cog に修正し、VoiceHistory を追加しました
        await self.add_cog(Economy(self))
        await self.add_cog(Salary(self))
        await self.add_cog(VoiceSystem(self))
        await self.add_cog(AdminTools(self))
        await self.add_cog(PrivateVCManager(self))
        await self.add_cog(InterviewSystem(self))
        await self.add_cog(ServerStats(self))
        await self.add_cog(ShopSystem(self))    # bot -> self に修正
        await self.add_cog(VoiceHistory(self))  
        await self.add_cog(Chinchiro(self)) # 【新機能】VC記録画像表示を追加
        
        # バックアップタスクの開始
        if not self.backup_db_task.is_running():
            self.backup_db_task.start()
        
        # Discord側へのコマンド同期
        await self.tree.sync()
        logger.info("LumenBank System: Setup complete and Synced.")

    async def send_admin_log(self, embed: discord.Embed):
        async with self.get_db() as db:
            async with db.execute("SELECT value FROM server_config WHERE key = 'log_channel_id'") as c:
                row = await c.fetchone()
                if row:
                    channel = self.get_channel(int(row['value']))
                    if channel:
                        await channel.send(embed=embed)

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
        logger.error("DISCORD_TOKEN is missing")
    else:
        # Flask(keep_alive)が必要な場合は有効に、不要ならコメントアウト
        # keep_alive.keep_alive() 
        
        bot = LumenBankBot()
        bot.run(TOKEN)
