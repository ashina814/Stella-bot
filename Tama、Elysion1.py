import discord
from discord.ext import commands, tasks
from discord import app_commands
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
base_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(base_dir, 'Elysion1.env')

load_dotenv(env_path)

# トークン取得（NameError対策として確実に文字列処理を行う）
raw_token = os.getenv("DISCORD_TOKEN")
if raw_token:
    # 引用符や改行を徹底的に除去
    TOKEN = str(raw_token).strip().replace('"', '').replace("'", "")
else:
    TOKEN = None

# ロギング設定
LOG_FORMAT = '%(asctime)s:%(levelname)s:%(name)s: %(message)s'
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

if not TOKEN:
    logging.error(f"DISCORD_TOKEN is missing. Tried to load from: {env_path}")
else:
    # 成功時に長さを表示（デバッグ用）
    logging.info(f"DISCORD_TOKEN loaded successfully. (Length: {len(TOKEN)})")

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
    """DBから設定を読み込み、メモリに保持。1,000人規模のアクセスでもDB負荷を最小化。"""
    def __init__(self, bot):
        self.bot = bot
        self.vc_reward_per_min: int = 10
        self.role_wages: Dict[int, int] = {}       
        self.admin_roles: Dict[int, str] = {}      

    async def reload(self):
        """DBから最新設定をキャッシュに読み込む（起動時・設定変更時に呼び出し）"""
        async with self.bot.get_db() as db:
            # VC報酬額
            async with db.execute("SELECT value FROM server_config WHERE key = 'vc_reward'") as cursor:
                row = await cursor.fetchone()
                if row: self.vc_reward_per_min = int(row['value'])
            
            # 給与設定
            async with db.execute("SELECT role_id, amount FROM role_wages") as cursor:
                rows = await cursor.fetchall()
                self.role_wages = {r['role_id']: r['amount'] for r in rows}

            # 管理権限ロール
            async with db.execute("SELECT role_id, perm_level FROM admin_roles") as cursor:
                rows = await cursor.fetchall()
                self.admin_roles = {r['role_id']: r['perm_level'] for r in rows}
        logger.info("Configuration and Permissions reloaded.")

def has_permission(required_level: str):
    """
    動的な権限チェック用デコレータ。
    SUPREME_GOD（最高神）は全ての管理コマンドをパスします。
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        # Botオーナーは常に全権限をパス
        if await interaction.client.is_owner(interaction.user):
            return True
        
        user_role_ids = [role.id for role in interaction.user.roles]
        admin_roles = interaction.client.config.admin_roles
        
        for r_id in user_role_ids:
            if r_id in admin_roles:
                level = admin_roles[r_id]
                if level == "SUPREME_GOD": return True
                if level == required_level: return True
        
        raise app_commands.AppCommandError(f"この操作には '{required_level}' 以上の権限が必要です。")
    return app_commands.check(predicate)

# --- データベース管理クラス (VC作成機能を排除) ---

class BankDatabase:
    """口座・取引・設定の永続化を担当。"""
    def __init__(self, db_path="lumen_bank_v4.db"):
        self.db_path = db_path

    async def setup(self, conn):
        """起動時に堅牢なデータ構造を構築する"""
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")

        # 1. 口座・取引履歴
        await conn.execute("""CREATE TABLE IF NOT EXISTS accounts (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 0 CHECK(balance >= 0), 
            total_earned INTEGER DEFAULT 0
        )""")

        await conn.execute("""CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER,
            receiver_id INTEGER,
            amount INTEGER,
            type TEXT,          -- 'TRANSFER', 'SALARY', 'VC_REWARD'
            batch_id TEXT,
            month_tag TEXT,
            description TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")

        # 2. サーバー設定・権限
        await conn.execute("CREATE TABLE IF NOT EXISTS server_config (key TEXT PRIMARY KEY, value TEXT)")
        await conn.execute("CREATE TABLE IF NOT EXISTS role_wages (role_id INTEGER PRIMARY KEY, amount INTEGER NOT NULL)")
        await conn.execute("CREATE TABLE IF NOT EXISTS admin_roles (role_id INTEGER PRIMARY KEY, perm_level TEXT)")

        # 3. VC統計・トラッキング
        await conn.execute("CREATE TABLE IF NOT EXISTS voice_stats (user_id INTEGER PRIMARY KEY, total_seconds INTEGER DEFAULT 0)")
        await conn.execute("CREATE TABLE IF NOT EXISTS voice_tracking (user_id INTEGER PRIMARY KEY, join_time TEXT)")

        # 4. 高速検索用インデックス
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trans_receiver ON transactions (receiver_id, created_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trans_sender ON transactions (sender_id, created_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trans_month ON transactions (month_tag, type)")

        await conn.commit()

# --- Cog: Economy (DB設定 & 動的権限版) ---
class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Botの応答速度を確認します")
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"🏓 Pong! Latency: `{latency}ms`")

    @app_commands.command(name="balance", description="残高を確認します（他人の照会は管理職のみ）")
    async def balance(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        # 1. 自分以外の残高を見ようとしているかチェック
        is_viewing_others = member is not None and member.id != interaction.user.id
        
        # 2. 他人の残高を見る場合のみ権限チェックを行う
        if is_viewing_others:
            # 権限がない場合はエラーを出して終了（ephemeral=Trueでこっそり通知）
            # has_permissionデコレータと同じロジックを関数内で実行します
            has_perm = False
            if await self.bot.is_owner(interaction.user):
                has_perm = True
            else:
                user_role_ids = [role.id for role in interaction.user.roles]
                # GODDESS（女神）以上の権限があるか確認
                for r_id in user_role_ids:
                    level = self.bot.config.admin_roles.get(r_id)
                    if level in ["SUPREME_GOD", "GODDESS"]:
                        has_perm = True
                        break
            
            if not has_perm:
                return await interaction.response.send_message(
                    "❌ 他人の残高を照会する権限がありません。", 
                    ephemeral=True
                )

        # 3. 実行（自分または許可された相手の情報取得）
        # ephemeral=True にすることで、コマンドの応答が自分以外には見えなくなります
        await interaction.response.defer(ephemeral=True)
        target = member or interaction.user

        async with self.bot.get_db() as db:
            async with db.execute(
                "SELECT balance FROM accounts WHERE user_id = ?", (target.id,)
            ) as cursor:
                row = await cursor.fetchone()
                bal = row['balance'] if row else 0
        
        embed = discord.Embed(title="🏦 ルーメン口座照会", color=discord.Color.gold())
        embed.add_field(name="ユーザー", value=target.mention)
        embed.add_field(name="残高", value=f"**{bal:,}** ルーメン")
        embed.set_thumbnail(url=target.display_avatar.url)
        
        await interaction.followup.send(embed=embed)


    @app_commands.command(name="transfer", description="送金処理（DM通知付き）")
    async def transfer(self, interaction: discord.Interaction, receiver: discord.Member, amount: int):
        await interaction.response.defer()
        if amount <= 0: return await interaction.followup.send("1以上を指定してください。", ephemeral=True)
        if receiver.id == interaction.user.id: return await interaction.followup.send("自分自身には送金できません。", ephemeral=True)
        if receiver.bot: return await interaction.followup.send("Botには送金できません。", ephemeral=True)

        try:
            async with self.bot.get_db() as db:
                async with db.begin():
                    await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance) VALUES (?, 0)", (interaction.user.id,))
                    async with db.execute("SELECT balance FROM accounts WHERE user_id = ?", (interaction.user.id,)) as cursor:
                        row = await cursor.fetchone()
                        current_bal = row['balance'] if row else 0
                        
                    if current_bal < amount:
                        return await interaction.followup.send(f"残高が足りません。(現在: {current_bal:,}L)", ephemeral=True)

                    await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (amount, interaction.user.id))
                    await db.execute("INSERT OR IGNORE INTO accounts (user_id, balance) VALUES (?, 0)", (receiver.id,))
                    await db.execute("UPDATE accounts SET balance = balance + ? WHERE user_id = ?", (amount, receiver.id))
                    
                    month_tag = datetime.datetime.now().strftime("%Y-%m")
                    await db.execute(
                        "INSERT INTO transactions (sender_id, receiver_id, amount, type, description, month_tag) VALUES (?, ?, ?, 'TRANSFER', ?, ?)",
                        (interaction.user.id, receiver.id, amount, f"{interaction.user.display_name}からの送金", month_tag)
                    )

            # --- ここからDM通知処理を追加 ---
            dm_notice = ""
            try:
                embed = discord.Embed(
                    title="💰 送金を受け取りました",
                    description=f"**{interaction.guild.name}** であなたに送金がありました。",
                    color=discord.Color.green()
                )
                embed.add_field(name="差出人", value=interaction.user.display_name)
                embed.add_field(name="金額", value=f"{amount:,} L")
                embed.set_footer(text="Lumen Bank System")
                
                await receiver.send(embed=embed)
                dm_notice = "（通知DMを送信しました）"
            except discord.Forbidden:
                # 相手がDM拒否設定の場合、ここに来るが無視して続行
                dm_notice = "（相手がDMを拒否しているため通知は送られませんでした）"
            except Exception as e:
                logger.error(f"DM Send Error: {e}")
                dm_notice = "（DM通知中にエラーが発生しました）"
            # -------------------------------

            await interaction.followup.send(f"✅ {receiver.mention} へ {amount:,}L 送金しました。{dm_notice}")

        except Exception as e:
            logger.error(f"Transfer Error: {e}")
            await interaction.followup.send("❌ 送金に失敗しました。", ephemeral=True)


    @app_commands.command(name="user_info", description="【女神以上】詳細な情報を表示します")
    @has_permission("GODDESS") # DB版の動的権限チェック
    async def user_info(self, interaction: discord.Interaction, member: discord.Member):
        async with self.bot.get_db() as db:
            async with db.execute("SELECT balance, total_earned FROM accounts WHERE user_id =?", (member.id,)) as cursor:
                acc = await cursor.fetchone()
            async with db.execute("SELECT total_seconds FROM voice_stats WHERE user_id =?", (member.id,)) as cursor:
                v_row = await cursor.fetchone()
        
        balance = acc['balance'] if acc else 0
        total_earned = acc['total_earned'] if acc else 0
        vc_sec = v_row['total_seconds'] if v_row else 0
        
        embed = discord.Embed(title=f"🔍 ユーザー情報: {member.display_name}", color=discord.Color.blue())
        embed.add_field(name="残高", value=f"{balance:,} L", inline=True)
        embed.add_field(name="累計獲得", value=f"{total_earned:,} L", inline=True)
        embed.add_field(name="VC時間", value=f"{vc_sec // 60}分", inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="history", description="直近の全ての入出金履歴を表示します")
    async def history(self, interaction: discord.Interaction):
        await interaction.response.defer()
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
            target = f"<@{r['receiver_id'] if is_sender else r['sender_id']}>"
            if not is_sender and r['sender_id'] == 0: target = "システム"

            embed.add_field(
                name=f"{r['created_at'][5:16]} | {emoji}",
                value=f"金額: **{amount_str}** / 相手: {target}\n種別: `{r['type']}`",
                inline=False
            )
        await interaction.followup.send(embed=embed)

# --- 1. 給与関連 (Salary) ---
class Salary(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="salary_distribute_all", description="【最高神】一括給与支給")
    @has_permission("SUPREME_GOD") # DB版の動的権限チェック
    async def distribute_all(self, interaction: discord.Interaction):
        await interaction.response.defer()
        now = datetime.datetime.now()
        month_tag = now.strftime("%Y-%m")
        batch_id = str(uuid.uuid4())[:8]
        
        # DBから動的に取得した給与設定を使用
        wage_dict = self.bot.config.role_wages 
        
        count, total_amount = 0, 0
        account_updates, transaction_records = [], []

        try:
            members = interaction.guild.members if interaction.guild.chunked else [m async for m in interaction.guild.fetch_members()]

            for member in members:
                if member.bot: continue
                # ロールIDで判定し、最大額を適用
                matching_wages = [wage_dict[r.id] for r in member.roles if r.id in wage_dict]
                if not matching_wages: continue
                
                wage = max(matching_wages)
                account_updates.append((member.id, wage, wage))
                transaction_records.append((0, member.id, wage, 'SALARY', batch_id, month_tag, f"{month_tag} 給与"))
                count += 1
                total_amount += wage

            if not account_updates:
                return await interaction.followup.send("対象となる役職を持つメンバーがいませんでした。")

            async with self.bot.get_db() as db:
                async with db.begin():
                    await db.executemany("""
                        INSERT INTO accounts (user_id, balance, total_earned) VALUES (?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET 
                        balance = balance + excluded.balance,
                        total_earned = total_earned + excluded.total_earned
                    """, account_updates)
                    await db.executemany("""
                        INSERT INTO transactions (sender_id, receiver_id, amount, type, batch_id, month_tag, description)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, transaction_records)

            await interaction.followup.send(f"💰 **一括支給完了**\n対象: {count}名\n総額: {total_amount:,} L\n識別ID: `{batch_id}`")
        except Exception as e:
            logger.error(f"Salary Error: {e}")
            await interaction.followup.send("❌ 支給中にエラーが発生しました。", ephemeral=True)

    @app_commands.command(name="admin_salary_rollback", description="【最高神】給与支給の取消")
    @has_permission("SUPREME_GOD")
    async def salary_rollback(self, interaction: discord.Interaction, batch_id: str):
        await interaction.response.defer()
        try:
            async with self.bot.get_db() as db:
                async with db.execute(
                    "SELECT receiver_id, amount FROM transactions WHERE batch_id = ? AND type = 'SALARY'", (batch_id,)
                ) as cursor:
                    records = await cursor.fetchall()
                if not records: return await interaction.followup.send(f"❌ バッチID `{batch_id}` は見つかりません。")

                async with db.begin():
                    for r in records:
                        await db.execute("UPDATE accounts SET balance = balance - ? WHERE user_id = ?", (r['amount'], r['receiver_id']))
                    await db.execute("DELETE FROM transactions WHERE batch_id = ?", (batch_id,))
            await interaction.followup.send(f"✅ バッチID `{batch_id}` の給与を取り消しました。")
        except Exception as e:
            logger.error(f"Rollback Error: {e}")
            await interaction.followup.send("❌ 取消失敗。残高不足のユーザーがいる可能性があります。", ephemeral=True)

    @app_commands.command(name="salary_diagnosis", description="自身の給与内訳を確認")
    async def salary_diagnosis(self, interaction: discord.Interaction):
        wage_dict = self.bot.config.role_wages
        wages = [wage_dict[r.id] for r in interaction.user.roles if r.id in wage_dict]
        total = max(wages) if wages else 0
        await interaction.response.send_message(f"🧾 現在の役職給与設定は **{total:,} L** です。")

    @app_commands.command(name="admin_economy_stats", description="【最高神】経済レポート")
    @has_permission("SUPREME_GOD")
    async def economy_stats(self, interaction: discord.Interaction):
        await interaction.response.defer()
        month_tag = datetime.datetime.now().strftime("%Y-%m")
        async with self.bot.get_db() as db:
            async with db.execute("SELECT SUM(amount) as t FROM transactions WHERE month_tag = ? AND type = 'SALARY'", (month_tag,)) as c:
                s_total = (await c.fetchone())['t'] or 0
            async with db.execute("SELECT SUM(amount) as t FROM transactions WHERE month_tag = ? AND type = 'VC_REWARD'", (month_tag,)) as c:
                v_total = (await c.fetchone())['t'] or 0

        embed = discord.Embed(title=f"📊 {month_tag} 経済レポート", color=discord.Color.dark_green())
        embed.add_field(name="合計発行量", value=f"**{s_total + v_total:,} L**", inline=False)
        await interaction.followup.send(embed=embed)

# --- Cog: VoiceSystem (1時間3000L & 監査ログ対応) ---
class VoiceSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.target_vc_id = 1459226569431056417 
        self.is_ready_processed = False

    def is_active(self, state):
        """報酬対象の状態か判定（対象VCにいて、かつスピーカーミュートでない）"""
        return (
            state and 
            state.channel and 
            state.channel.id == self.target_vc_id and 
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
                logger.error(f"Voice Tracking Start Error [{member.id}]: {e}")

        elif was_active and not is_now_active:
            await self._process_reward(member, now)

    async def _process_reward(self, member_or_id, now):
        user_id = member_or_id.id if isinstance(member_or_id, discord.Member) else member_or_id
        try:
            async with self.bot.get_db() as db:
                async with db.execute(
                    "SELECT join_time FROM voice_tracking WHERE user_id =?", (user_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                if not row: return

                async with db.begin():
                    join_time = datetime.datetime.fromisoformat(row['join_time'])
                    sec = int((now - join_time).total_seconds())
                    
                    # 1時間3000L = 1分50L の固定レート計算
                    reward = (sec * 50) // 60 

                    if reward > 0:
                        month_tag = now.strftime("%Y-%m")
                        await db.execute("INSERT OR IGNORE INTO accounts (user_id) VALUES (?)", (user_id,))
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

                # --- 監査ログ送信 ---
                if reward > 0:
                    embed = discord.Embed(title="🎙 VC報酬精算", color=discord.Color.blue(), timestamp=now)
                    embed.add_field(name="ユーザー", value=f"<@{user_id}>")
                    embed.add_field(name="付与額", value=f"{reward:,} L")
                    embed.add_field(name="滞在時間", value=f"{sec // 60}分")
                    await self.bot.send_admin_log(embed)

        except Exception as e:
            logger.error(f"Voice Reward Process Error [{user_id}]: {e}")

    @commands.Cog.listener()
    async def on_ready(self):
        if self.is_ready_processed: return
        self.is_ready_processed = True
        await asyncio.sleep(10)
        now = datetime.datetime.now()
        try:
            async with self.bot.get_db() as db:
                async with db.execute("SELECT user_id FROM voice_tracking") as cursor:
                    tracked_users = await cursor.fetchall()
                for row in tracked_users:
                    u_id = row['user_id']
                    if not any(self.is_active(g.get_member(u_id).voice) for g in self.bot.guilds if g.get_member(u_id)):
                        await self._process_reward(u_id, now)
        except Exception as e:
            logger.error(f"Recovery Error: {e}")

# --- 3. 管理者ツール (AdminTools: ログ設定 & 一時VC削除) ---
class AdminTools(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="config_set_log_channel", description="【最高神】監査ログ（証拠）の出力先を設定します")
    @has_permission("SUPREME_GOD")
    async def set_log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        async with self.bot.get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO server_config (key, value) VALUES ('log_channel_id', ?)", 
                (str(channel.id),)
            )
            await db.commit()
        await interaction.response.send_message(f"✅ 以降、全ての重要ログを {channel.mention} に送信します。")

    @app_commands.command(name="config_set_admin", description="【オーナー用】管理権限ロールを登録・更新します")
    async def config_set_admin(self, interaction: discord.Interaction, role: discord.Role, level: str):
        if not await self.bot.is_owner(interaction.user):
            return await interaction.response.send_message("オーナーのみ実行可能です。", ephemeral=True)
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO admin_roles (role_id, perm_level) VALUES (?, ?)", (role.id, level.upper()))
            await db.commit()
        await self.bot.config.reload()
        
        # ログ送信
        embed = discord.Embed(title="⚖️ 権限設定変更", color=discord.Color.red())
        embed.add_field(name="ロール", value=role.mention)
        embed.add_field(name="レベル", value=level.upper())
        await self.bot.send_admin_log(embed)
        await interaction.response.send_message(f"✅ {role.mention} を `{level}` に設定しました。")

    @app_commands.command(name="config_set_wage", description="【最高神】役職ごとの給与額を設定します")
    @has_permission("SUPREME_GOD")
    async def config_set_wage(self, interaction: discord.Interaction, role: discord.Role, amount: int):
        async with self.bot.get_db() as db:
            await db.execute("INSERT OR REPLACE INTO role_wages (role_id, amount) VALUES (?, ?)", (role.id, amount))
            await db.commit()
        await self.bot.config.reload()
        
        # ログ送信
        embed = discord.Embed(title="💰 給与設定更新", color=discord.Color.orange())
        embed.add_field(name="対象ロール", value=role.mention)
        embed.add_field(name="設定額", value=f"{amount:,} L")
        await self.bot.send_admin_log(embed)
        await interaction.response.send_message(f"✅ 設定を更新しました。")


# --- Bot 本体: LumenBankBot  ---
class LumenBankBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True          # 1,000人規模のメンバー取得に必須
        intents.voice_states = True     # VC報酬計算に必須
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        
        self.db_path = "lumen_bank_v4.db"
        self.db_manager = BankDatabase(self.db_path)
        self.config = ConfigManager(self)

    @contextlib.asynccontextmanager
    async def get_db(self):
        """DB接続の共通ゲートウェイ"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            yield db

    async def setup_hook(self):
        # 1. データベースの初期化
        async with self.get_db() as db:
            await self.db_manager.setup(db)
        
        # 2. 設定キャッシュ読み込み
        await self.config.reload()
        
        # 3. Cogの追加（一時VCがないため、関連Cogのみ）
        await self.add_cog(Economy(self))
        await self.add_cog(Salary(self))
        await self.add_cog(VoiceSystem(self))
        await self.add_cog(AdminTools(self))
        
        # 4. バックアップタスク開始
        self.backup_db_task.start()
        
        # 5. コマンド同期（TempVCViewの登録は削除済み）
        await self.tree.sync()
        logger.info("LumenBank System: Setup complete and Synced.")

    async def send_admin_log(self, embed: discord.Embed):
        """指定されたチャンネルへ監査ログを飛ばす重要メソッド"""
        async with self.get_db() as db:
            async with db.execute("SELECT value FROM server_config WHERE key = 'log_channel_id'") as c:
                row = await c.fetchone()
                if row:
                    channel = self.get_channel(int(row['value']))
                    if channel:
                        await channel.send(embed=embed)

    @tasks.loop(hours=24)
    async def backup_db_task(self):
        """24時間ごとにDBのコピーを作成し、資産データを死守する"""
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

# --- 実行部分 ---
if __name__ == "__main__":
    if not TOKEN:
        logger.error("DISCORD_TOKEN is missing in .env")
    else:
        bot = LumenBankBot()
        bot.run(TOKEN)