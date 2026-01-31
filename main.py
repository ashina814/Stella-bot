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
                new_vc = await guild.create_voice_channel(name=channel_name, category=category, overwrites=overwrites, user_limit=5)

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

    @app_commands.command(name="deploy_vc_panel", description="【管理者】内容をカスタマイズしてVC作成パネルを設置します")
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

# --- Cog: Economy (残高・送金) ---
class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Botの応答速度を確認します")
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"🏓 Pong! Latency: `{latency}ms`")

    @app_commands.command(name="balance", description="残高を確認します")
    async def balance(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)

        # ターゲット決定
        target = member or interaction.user
        
        # 権限チェック (自分以外を見る場合)
        if target.id != interaction.user.id:
            has_perm = False
            if await self.bot.is_owner(interaction.user):
                has_perm = True
            else:
                user_role_ids = [role.id for role in interaction.user.roles]
                admin_roles = self.bot.config.admin_roles
                for r_id in user_role_ids:
                    if r_id in admin_roles and admin_roles[r_id] in ["SUPREME_GOD", "GODDESS"]:
                        has_perm = True
                        break
            
            if not has_perm:
                return await interaction.followup.send("❌ 他人の口座を参照する権限がありません。", ephemeral=True)

        # データ取得
        async with self.bot.get_db() as db:
            async with db.execute(
                "SELECT balance, total_earned FROM accounts WHERE user_id = ?", (target.id,)
            ) as cursor:
                row = await cursor.fetchone()
                bal = row['balance'] if row else 0
                earned = row['total_earned'] if row else 0
        
        # デザイン
        embed = discord.Embed(
            title="🏛 ルーメン口座照会",
            color=0xFFD700 # Gold
        )
        embed.set_author(name=f"{target.display_name} 様の口座情報", icon_url=target.display_avatar.url)
        embed.add_field(name="💰 現在の残高", value=f"**{bal:,}** L", inline=False)
        embed.add_field(name="📈 累計獲得額", value=f"{earned:,} L", inline=False)
        
        date_str = datetime.datetime.now().strftime("%Y/%m/%d")
        embed.set_footer(text=f"Server: {interaction.guild.name} | {date_str}")
        embed.set_thumbnail(url=target.display_avatar.url)
        
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="transfer", description="送金処理（DM通知付き）")
    async def transfer(self, interaction: discord.Interaction, receiver: discord.Member, amount: int):
        await interaction.response.defer(ephemeral=True) 
        
        # 1. 入力値の安全性チェック
        if amount <= 0:
            return await interaction.followup.send("❌ 1 Ru 以上を指定してください。", ephemeral=True)
        if amount > 10000000: # 上限設定（例: 1000万）
            return await interaction.followup.send("❌ 1回の送金上限は 10,000,000 Ru です。", ephemeral=True)
            
        if receiver.id == interaction.user.id:
            return await interaction.followup.send("❌ 自分自身には送金できません。", ephemeral=True)
        if receiver.bot:
            return await interaction.followup.send("❌ Botには送金できません。", ephemeral=True)

        sender = interaction.user
        month_tag = datetime.datetime.now().strftime("%Y-%m")

        try:
            async with self.bot.get_db() as db:
                # ★修正: begin() を削除し、try-except ブロックに変更
                try:
                    # 送信者の口座を作成（無い場合）
                    await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance) VALUES (?, 0)", (sender.id,))
                    
                    # 残高を減らす（残高不足なら更新件数が0になる）
                    cursor = await db.execute(
                        "UPDATE accounts SET balance = balance - ? WHERE user_id = ? AND balance >= ?", 
                        (amount, sender.id, amount)
                    )
                    
                    # 更新された行数が0なら「残高不足」
                    if cursor.rowcount == 0:
                        async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (sender.id,)) as c:
                            row = await c.fetchone()
                            curr = row['balance'] if row else 0
                        # ここでロールバックする必要はないが、処理を中断
                        return await interaction.followup.send(f"❌ 残高が足りません。\n(送金額: {amount:,} L / 現在: {curr:,} L)", ephemeral=True)

                    # 相手の口座を作成 & 振り込む
                    await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance) VALUES (?, 0)", (receiver.id,))
                    await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (amount, receiver.id))
                    
                    # 履歴保存
                    await db.execute(
                        "INSERT INTO transactions (sender_id, receiver_id, amount, type, description, month_tag) VALUES (?, ?, ?, 'TRANSFER', ?, ?)",
                        (sender.id, receiver.id, amount, f"{sender.display_name}からの送金", month_tag)
                    )
                    
                    # ★ここで確定（コミット）
                    await db.commit()

                except Exception as db_err:
                    # DB操作中にエラーが出たら取り消す
                    await db.rollback()
                    raise db_err

            # --- 成功後の処理 ---

            # DM通知
            dm_status = ""
            try:
                embed = discord.Embed(
                    title="💰 送金を受け取りました",
                    description=f"**{interaction.guild.name}** であなたに送金がありました。",
                    color=discord.Color.green()
                )
                embed.add_field(name="差出人", value=sender.display_name)
                embed.add_field(name="金額", value=f"{amount:,} L")
                embed.set_footer(text="Lumen Bank System")
                await receiver.send(embed=embed)
            except:
                dm_status = "（相手の設定によりDM未送信）"

            await interaction.followup.send(f"✅ 送金成功: {receiver.mention} へ {amount:,} L 送りました。{dm_status}", ephemeral=True)
            

        except Exception as e:
            logger.error(f"Transfer Error: {e}")
            await interaction.followup.send("❌ エラーが発生しました。管理者に連絡してください。", ephemeral=True)


    @app_commands.command(name="history", description="直近の全ての入出金履歴を表示します")
    async def history(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True) # 自分のみ
        async with self.bot.get_db() as db:
            query = "SELECT * FROM transactions WHERE sender_id = ? OR receiver_id = ? ORDER BY created_at DESC LIMIT 10"
            async with db.execute(query, (interaction.user.id, interaction.user.id)) as cursor:
                rows = await cursor.fetchall()
        
        if not rows: return await interaction.followup.send("取引履歴はありません。", ephemeral=True)

        embed = discord.Embed(title="📜 取引履歴明細", color=discord.Color.blue())
        for r in rows:
            is_sender = r['sender_id'] == interaction.user.id
            emoji = "📤 送金" if is_sender else "📥 受取"
            amount_str = f"{'-' if is_sender else '+'}{r['amount']:,} L"
            
            # 相手の名前解決
            if r['sender_id'] == 0 or r['receiver_id'] == 0:
                target_name = "システム (Fee/Reward)"
            else:
                target_id = r['receiver_id'] if is_sender else r['sender_id']
                target_name = f"<@{target_id}>"

            embed.add_field(
                name=f"{r['created_at'][5:16]} | {emoji}",
                value=f"金額: **{amount_str}**\n相手: {target_name}\n内容: `{r['description']}`",
                inline=False
            )
        await interaction.followup.send(embed=embed, ephemeral=True)


# --- Cog: Salary (給与) ---
class Salary(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ▼▼▼ 修正版: 一括給与支給コマンド ▼▼▼
    @app_commands.command(name="salary_distribute_all", description="【最高神】一括給与支給")
    @has_permission("SUPREME_GOD")
    async def distribute_all(self, interaction: discord.Interaction):
        await interaction.response.defer()
        now = datetime.datetime.now()
        month_tag = now.strftime("%Y-%m")
        # 識別ID（ロールバック用）を生成
        batch_id = str(uuid.uuid4())[:8]
        
        wage_dict = self.bot.config.role_wages 
        
        count, total_amount = 0, 0
        account_updates, transaction_records = [], []

        try:
            # メンバーリストを取得
            members = interaction.guild.members if interaction.guild.chunked else [m async for m in interaction.guild.fetch_members()]

            for member in members:
                if member.bot: continue
                # 設定された役職を持っているかチェック
                matching_wages = [wage_dict[r.id] for r in member.roles if r.id in wage_dict]
                if not matching_wages: continue
                
                # 一番高い給与を採用
                wage = max(matching_wages)
                
                # DB更新用データを作成
                account_updates.append((member.id, wage, wage))
                transaction_records.append((0, member.id, wage, 'SALARY', batch_id, month_tag, f"{month_tag} 給与"))
                count += 1
                total_amount += wage

            if not account_updates:
                return await interaction.followup.send("対象となる役職を持つメンバーがいませんでした。")

            # データベース処理（安全装置付き）
            async with self.bot.get_db() as db:
                try:
                    # 1. まずシステム口座（ID:0）を確実に作る（エラー回避）
                    await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance, total_earned) VALUES (0, 0, 0)")
                    
                    # 2. 全員の残高を更新
                    await db.executemany("""
                        INSERT INTO accounts (user_id, balance, total_earned) VALUES (?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET 
                        balance = balance + excluded.balance,
                        total_earned = total_earned + excluded.total_earned
                    """, account_updates)
                    
                    # 3. 取引履歴を記録
                    await db.executemany("""
                        INSERT INTO transactions (sender_id, receiver_id, amount, type, batch_id, month_tag, description)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, transaction_records)
                    
                    # 4. ここまでエラーがなければ確定（セーブ）
                    await db.commit()
                    
                except Exception as db_err:
                    # 途中でエラーが起きたら、変更を全部なかったことにする（ロールバック）
                    await db.rollback()
                    raise db_err

            await interaction.followup.send(f"💰 **一括支給完了**\n対象: {count}名\n総額: {total_amount:,} L\n識別ID: `{batch_id}`\n(※万が一間違えた場合は `/salary_rollback {batch_id}` で取り消せます)")
            
        except Exception as e:
            logger.error(f"Salary Error: {e}")
            await interaction.followup.send(f"❌ 支給中にエラーが発生しました: {e}", ephemeral=True)


    # ▼▼▼ 追加機能: 給与取り消し（ロールバック）コマンド ▼▼▼
    @app_commands.command(name="salary_rollback", description="【最高神】指定した識別ID(Batch ID)の給与支給を取り消します")
    @app_commands.describe(batch_id="取り消したい支給の識別ID（支給完了時に表示されます）")
    @has_permission("SUPREME_GOD")
    async def salary_rollback(self, interaction: discord.Interaction, batch_id: str):
        await interaction.response.defer(ephemeral=True)
        
        try:
            async with self.bot.get_db() as db:
                # 指定されたIDの給与データを検索
                async with db.execute("SELECT receiver_id, amount FROM transactions WHERE batch_id = ? AND type = 'SALARY'", (batch_id,)) as cursor:
                    rows = await cursor.fetchall()
                
                if not rows:
                    return await interaction.followup.send(f"❌ 指定されたID `{batch_id}` の給与データが見つかりません。", ephemeral=True)
                
                count = 0
                total_reverted = 0
                
                try:
                    # 1. 配ったお金を各ユーザーから回収する
                    for row in rows:
                        uid = row['receiver_id']
                        amt = row['amount']
                        # 残高と累計獲得額の両方から引く
                        await db.execute("UPDATE accounts SET balance = balance - ?, total_earned = total_earned - ? WHERE user_id = ?", (amt, amt, uid))
                        count += 1
                        total_reverted += amt
                    
                    # 2. 取引履歴を削除する（なかったことにする）
                    await db.execute("DELETE FROM transactions WHERE batch_id = ?", (batch_id,))
                    
                    # 3. 確定
                    await db.commit()
                    
                except Exception as db_err:
                    await db.rollback()
                    raise db_err

            # 完了報告
            await interaction.followup.send(f"↩️ **ロールバック完了**\n識別ID `{batch_id}` の支給を取り消しました。\n対象: {count}件\n回収額: {total_reverted:,} L", ephemeral=True)

        except Exception as e:
            logger.error(f"Rollback Error: {e}")
            await interaction.followup.send(f"❌ ロールバック中にエラーが発生しました: {e}", ephemeral=True)

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



# --- Cog: InterviewSystem  ---
class InterviewSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="面接通過", description="指定ユーザー or 同じVCのメンバー全員にロールと初期資金を付与します")
    @app_commands.describe(
        role="付与するロール",
        amount="初期付与額（デフォルト: 30,000）",
        target="対象ユーザー（指定しない場合は、あなたと同じVCにいる全員が対象になります）"
    )
    @has_permission("ADMIN")
    async def pass_interview(
        self, 
        interaction: discord.Interaction, 
        role: discord.Role, 
        amount: int = 30000, 
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
        skipped_members = [] # 除外された人のリスト

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
                        skipped_members.append(m.display_name)
                        continue
                    targets.append(m)

                mode_text = f"VC **{channel.name}** のメンバー (除外あり)"
            else:
                return await interaction.followup.send("❌ 対象を指定するか、ボイスチャンネルに参加した状態で実行してください。", ephemeral=True)

        if not targets:
            msg = "❌ 対象となるメンバーがいませんでした。"
            if skipped_members:
                msg += f"\n(除外されたメンバー: {', '.join(skipped_members)})"
            return await interaction.followup.send(msg, ephemeral=True)

        # 3. 一括処理実行
        success_count = 0
        error_logs = []
        month_tag = datetime.datetime.now().strftime("%Y-%m")

        async with self.bot.get_db() as db:
            # ★修正: db.begin() を削除し、手動管理へ
            try:
                # 0. システム口座(ID:0)を確実に作る（エラー回避）
                await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance, total_earned) VALUES (0, 0, 0)")

                for member in targets:
                    if member.bot: continue
                    
                    try:
                        # A. ロール付与
                        if role not in member.roles:
                            await member.add_roles(role, reason="面接通過コマンドによる付与")
                        
                        # B. お金付与
                        # 口座がなければ作る
                        await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance) VALUES (?, 0)", (member.id,))
                        
                        # 残高追加
                        await db.execute(
                            "UPDATE accounts SET balance = balance + ?, total_earned = total_earned + ? WHERE user_id = ?", 
                            (amount, amount, member.id)
                        )
                        
                        # 取引履歴
                        await db.execute(
                            "INSERT INTO transactions (sender_id, receiver_id, amount, type, description, month_tag) VALUES (0, ?, ?, 'BONUS', ?, ?)",
                            (member.id, amount, f"面接通過祝い: {role.name}", month_tag)
                        )
                        
                        success_count += 1
                        
                    except discord.Forbidden:
                        error_logs.append(f"⚠️ {member.display_name}: 権限不足でロールを付与できませんでした")
                    except Exception as e:
                        error_logs.append(f"❌ {member.display_name}: エラーが発生しました ({e})")
                        logger.error(f"Interview Command Error [{member.id}]: {e}")
                
                # ★最後にコミット（これで確定）
                await db.commit()

            except Exception as db_err:
                await db.rollback()
                logger.error(f"Interview Transaction Error: {db_err}")
                return await interaction.followup.send("❌ データベースエラーが発生しました。", ephemeral=True)

        # 4. 結果報告Embed
        embed = discord.Embed(title="🌸 面接通過処理完了", color=discord.Color.pink())
        embed.add_field(name="対象範囲", value=mode_text, inline=False)
        embed.add_field(name="付与ロール", value=role.mention, inline=True)
        embed.add_field(name="支給額", value=f"{amount:,} L", inline=True)
        
        # 結果の内訳
        result_text = f"✅ 成功: {success_count}名"
        if skipped_members:
            result_text += f"\n⛔ 除外(説明者): {len(skipped_members)}名"
            
        embed.add_field(name="処理結果", value=result_text, inline=False)
        
        if error_logs:
            embed.add_field(name="エラーログ", value="\n".join(error_logs[:5]), inline=False)

        await interaction.followup.send(embed=embed)

# --- Cog: ServerStats (サーバー経済統計 & グラフ) ---
class ServerStats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.daily_log_task.start()

    def cog_unload(self):
        self.daily_log_task.cancel()

    async def get_total_balance_excluding_gods(self):
        """最高神とシステム(ID:0)を除く、サーバー全体の総資産を計算"""
        guild = self.bot.guilds[0] # メインサーバーを取得
        
        # 1. 最高神のロールIDを特定
        god_role_ids = []
        for r_id, level in self.bot.config.admin_roles.items():
            if level == "SUPREME_GOD":
                god_role_ids.append(r_id)
        
        # 2. 除外対象（最高神ロール持ち & システム）をリストアップ
        exclude_user_ids = {0}
        
        # メンバー情報を確実に取得
        if not guild.chunked:
            await guild.chunk()
            
        for member in guild.members:
            # 最高神ロールを持っているかチェック
            if any(role.id in god_role_ids for role in member.roles):
                exclude_user_ids.add(member.id)

        # 3. DBから集計（一般市民の残高のみ合計）
        total = 0
        async with self.bot.get_db() as db:
            async with db.execute("SELECT user_id, balance FROM accounts") as cursor:
                rows = await cursor.fetchall()
                
            for row in rows:
                if row['user_id'] not in exclude_user_ids:
                    total += row['balance']
        
        return total

    @tasks.loop(hours=24)
    async def daily_log_task(self):
        """毎日データを自動記録"""
        now = datetime.datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        
        try:
            total_balance = await self.get_total_balance_excluding_gods()
            
            async with self.bot.get_db() as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS daily_stats (
                        date TEXT PRIMARY KEY,
                        total_balance INTEGER
                    )
                """)
                await db.execute(
                    "INSERT OR REPLACE INTO daily_stats (date, total_balance) VALUES (?, ?)",
                    (date_str, total_balance)
                )
                await db.commit()
            
            logger.info(f"Daily Stats Logged: {date_str} = {total_balance:,} L")
            
        except Exception as e:
            logger.error(f"Daily Stats Error: {e}")

    @daily_log_task.before_loop
    async def before_daily_log(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="economy_graph", description="一般市民の総資産推移をグラフ化します")
    async def economy_graph(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        # データを取得
        async with self.bot.get_db() as db:
            await db.execute("CREATE TABLE IF NOT EXISTS daily_stats (date TEXT PRIMARY KEY, total_balance INTEGER)")
            async with db.execute("SELECT date, total_balance FROM daily_stats ORDER BY date ASC") as cursor:
                rows = await cursor.fetchall()
        
        # データがまだ無いなら、今の瞬間を記録して表示
        if not rows:
            current_total = await self.get_total_balance_excluding_gods()
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            rows = [{'date': today, 'total_balance': current_total}]
            
            async with self.bot.get_db() as db:
                await db.execute("INSERT OR REPLACE INTO daily_stats (date, total_balance) VALUES (?, ?)", (today, current_total))
                await db.commit()

        # グラフ描画
        dates = [r['date'] for r in rows]
        balances = [r['total_balance'] for r in rows]

        plt.figure(figsize=(10, 6))
        plt.plot(dates, balances, marker='o', linestyle='-', color='b', label='Total Balance')
        plt.title('Server Economy (Excluding Gods)')
        plt.xlabel('Date')
        plt.ylabel('Total Balance (Lumen)')
        plt.grid(True)
        plt.xticks(rotation=45)
        plt.tight_layout()

        # 画像をDiscordに送る準備
        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        plt.close()

        file = discord.File(buf, filename="economy_graph.png")
        await interaction.followup.send(f"📊 **サーバー経済推移**\n現在の一般市民総資産: {balances[-1]:,} L", file=file)

# --- 3. 管理者ツール ---
class AdminTools(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="config_set_log_channel", description="【最高神】監査ログ（証拠）の出力先を設定します")
    @has_permission("SUPREME_GOD")
    async def set_log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        # タイムアウト対策
        await interaction.response.defer(ephemeral=True)
        
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO server_config (key, value) VALUES ('log_channel_id', ?)", (str(channel.id),))
            await db.commit()
        
        # deferした後は followup.send を使う
        await interaction.followup.send(f"✅ 以降、全ての重要ログを {channel.mention} に送信します。", ephemeral=True)

    @app_commands.command(name="config_set_admin", description="【オーナー用】管理権限ロールを登録・更新します")
    async def config_set_admin(self, interaction: discord.Interaction, role: discord.Role, level: str):
        # ここも先に待機中にする
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

    @app_commands.command(name="config_set_wage", description="【最高神】役職ごとの給与額を設定します")
    @has_permission("SUPREME_GOD")
    async def config_set_wage(self, interaction: discord.Interaction, role: discord.Role, amount: int):
        # ★エラーが出ていた箇所。deferを追加して修正★
        await interaction.response.defer(ephemeral=True)
        
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO role_wages (role_id, amount) VALUES (?, ?)", (role.id, amount))
            await db.commit()
        await self.bot.config.reload()
        
        await interaction.followup.send(f"✅ 設定を更新しました。", ephemeral=True)

    @app_commands.command(name="vc_reward_add", description="【最高神】報酬対象のVCを追加します")
    @has_permission("SUPREME_GOD")
    async def add_reward_vc(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        await interaction.response.defer(ephemeral=True)
        
        async with self.bot.get_db() as db:
            # 重複無視で挿入
            await db.execute("INSERT OR IGNORE INTO reward_channels (channel_id) VALUES (?)", (channel.id,))
            await db.commit()
        
        # VoiceSystemに即座に反映
        vc_cog = self.bot.get_cog("VoiceSystem")
        if vc_cog:
            await vc_cog.reload_targets()

        await interaction.followup.send(f"✅ {channel.mention} を報酬対象に追加しました。", ephemeral=True)

    @app_commands.command(name="vc_reward_remove", description="【最高神】報酬対象のVCを解除します")
    @has_permission("SUPREME_GOD")
    async def remove_reward_vc(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        await interaction.response.defer(ephemeral=True)

        async with self.bot.get_db() as db:
            await db.execute("DELETE FROM reward_channels WHERE channel_id = ?", (channel.id,))
            await db.commit()

        # VoiceSystemに即座に反映
        vc_cog = self.bot.get_cog("VoiceSystem")
        if vc_cog:
            await vc_cog.reload_targets()

        await interaction.followup.send(f"🗑️ {channel.mention} を報酬対象から除外しました。", ephemeral=True)

    @app_commands.command(name="vc_reward_list", description="【最高神】報酬対象のVC一覧を表示します")
    @has_permission("SUPREME_GOD")
    async def list_reward_vcs(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        async with self.bot.get_db() as db:
            async with db.execute("SELECT channel_id FROM reward_channels") as cursor:
                rows = await cursor.fetchall()
        
        if not rows:
            return await interaction.followup.send("報酬対象のVCは設定されていません。", ephemeral=True)

        # チャンネルリンクを作成して表示
        channels_text = "\n".join([f"• <#{row['channel_id']}>" for row in rows])
        embed = discord.Embed(title="🎙 報酬対象VC一覧", description=channels_text, color=discord.Color.green())
        await interaction.followup.send(embed=embed, ephemeral=True)

# --- Bot 本体 ---
class LumenBankBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True          
        intents.voice_states = True     
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        
        self.db_path = "lumen_bank_v4.db"
        self.db_manager = BankDatabase(self.db_path)
        self.config = ConfigManager(self)

    
    @contextlib.asynccontextmanager
    async def get_db(self):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            
            # --- ここが追加した「保険」です ---
            # 1. データの矛盾（幽霊ユーザーなど）を許さない設定
            await db.execute("PRAGMA foreign_keys = ON")
            
            # 2. DBが混雑していても、エラーで即死せずに5秒間待ってリトライする設定
            # これをここでやることで、全てのコマンドで「Botが止まる」のを防げます
            await db.execute("PRAGMA busy_timeout = 5000")
            # -------------------------------
            
            yield db

    async def setup_hook(self):
        async with self.get_db() as db:
            await self.db_manager.setup(db)
        
        await self.config.reload()
        
        # 永続的なViewを登録
        self.add_view(VCPanel())
        
        await self.add_cog(Economy(self))
        await self.add_cog(Salary(self))
        await self.add_cog(VoiceSystem(self))
        await self.add_cog(AdminTools(self))
        await self.add_cog(PrivateVCManager(self))
        await self.add_cog(InterviewSystem(self))
        await self.add_cog(ServerStats(self))
        self.backup_db_task.start()
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
        backup_name = f"backup_{datetime.datetime.now().strftime('%Y%m%d')}.db"
        try:
            shutil.copy2(self.db_path, backup_name)
            logger.info(f"Auto Backup Success: {backup_name}")
        except Exception as e:
            logger.error(f"Backup Failure: {e}")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print("--- Lumen Bank System Online ---")

if __name__ == "__main__":
    if not TOKEN:
        logger.error("DISCORD_TOKEN is missing")
    else:
        keep_alive.keep_alive()
        bot = LumenBankBot()
        bot.run(TOKEN)
