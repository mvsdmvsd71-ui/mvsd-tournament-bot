import os
import re
import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
FOUNDER_ROLE_ID = int(os.getenv("FOUNDER_ROLE_ID", "0") or 0)
MODERATOR_ROLE_ID = int(os.getenv("MODERATOR_ROLE_ID", "0") or 0)
CHAMPION_ROLE_ID = int(os.getenv("CHAMPION_ROLE_ID", "0") or 0)
DATABASE_PATH = os.getenv("DATABASE_PATH", "mvsd_tournament.sqlite3")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("mvsd-tournament")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

RULES = [
    ("1. Hile ve üçüncü taraf yazılımlar", "Hile, script, aimbot, ESP, silent aim, hitbox extender, speed, fly ve benzeri üçüncü taraf yazılımlar yasaktır. İhlal, kanıt ve turnuva kuralları değerlendirilerek diskalifiye ve kalıcı turnuva yasağıyla sonuçlanabilir."),
    ("2. Makro kullanımı", "Otomatik ateş etme, zıplama veya hareket avantajı sağlayan makrolar yasaktır."),
    ("3. Exploit ve harita istismarı", "Harita dışına çıkmak, kapı/duvar arkası hatalarını kötüye kullanmak ve exploit kullanmak yasaktır."),
    ("4. Hesap eşleşmesi", "Turnuvaya kaydedilen Roblox hesabı ile maçta kullanılan hesap aynı olmalıdır. Smurf veya yan hesap kullanımı yasaktır."),
    ("5. Maça zamanında katılım", "Maç saatinden itibaren 10 dakika içinde belirlenen VIP sunucuda hazır bulunmayan taraf, hakem incelemesi sonrası hükmen mağlup sayılabilir."),
    ("6. Skor kanıtı", "Kazanan oyuncu maç sonunda oyun sonu zafer ekranının ekran görüntüsünü (SS) skor bildirimiyle birlikte sunmalıdır."),
    ("7. Davranış ve kararlar", "Discord ve oyun içi hakaret, küfür, taciz veya kışkırtma diskalifiye sebebi olabilir. Hakem kararları itiraz prosedürü çerçevesinde değerlendirilir; turnuva yöneticileri nihai kararı verir."),
]


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(""" CREATE TABLE IF NOT EXISTS tournaments ( id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', created_by INTEGER NOT NULL, created_at TEXT NOT NULL ); CREATE TABLE IF NOT EXISTS matches ( id INTEGER PRIMARY KEY AUTOINCREMENT, tournament_id INTEGER NOT NULL, round_name TEXT NOT NULL, channel_id INTEGER, player1_id INTEGER NOT NULL, player1_name TEXT NOT NULL, player2_id INTEGER NOT NULL, player2_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'scheduled', score TEXT, winner_id INTEGER, created_at TEXT NOT NULL, completed_at TEXT, FOREIGN KEY(tournament_id) REFERENCES tournaments(id) ); CREATE TABLE IF NOT EXISTS score_reports ( id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER NOT NULL, submitter_id INTEGER NOT NULL, opponent_id INTEGER NOT NULL, roblox_username TEXT NOT NULL, score TEXT NOT NULL, screenshot_url TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', message_id INTEGER, channel_id INTEGER NOT NULL, created_at TEXT NOT NULL, reviewed_at TEXT, FOREIGN KEY(match_id) REFERENCES matches(id) ); CREATE INDEX IF NOT EXISTS idx_matches_channel ON matches(channel_id); CREATE INDEX IF NOT EXISTS idx_reports_status ON score_reports(status); """)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    staff_ids = {FOUNDER_ROLE_ID, MODERATOR_ROLE_ID}
    return any(role.id in staff_ids for role in member.roles if role.id)


def staff_mentions(guild: Optional[discord.Guild]) -> str:
    if guild is None:
        return "@Founder @Moderator"
    parts = []
    for role_id, fallback in ((FOUNDER_ROLE_ID, "@Founder"), (MODERATOR_ROLE_ID, "@Moderator")):
        role = guild.get_role(role_id) if role_id else None
        parts.append(role.mention if role else fallback)
    return " ".join(parts)


def score_is_valid(score: str) -> bool:
    # Accept common formats such as 5-3, 5:3, or 5 – 3.
    return bool(re.fullmatch(r"\s*\d{1,3}\s*[-:–]\s*\d{1,3}\s*", score))


async def find_match_for_channel(channel_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT m.*, t.name AS tournament_name FROM matches m JOIN tournaments t ON t.id=m.tournament_id WHERE m.channel_id=? ORDER BY m.id DESC LIMIT 1",
            (channel_id,),
        ).fetchone()


class ScoreApprovalView(discord.ui.View):
    def __init__(self, report_id: int):
        super().__init__(timeout=None)
        self.report_id = report_id
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label == "Onayla":
                item.custom_id = f"mvsd:score:approve:{report_id}"
            elif isinstance(item, discord.ui.Button) and item.label == "Reddet":
                item.custom_id = f"mvsd:score:reject:{report_id}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        with db() as conn:
            report = conn.execute("SELECT * FROM score_reports WHERE id=?", (self.report_id,)).fetchone()
        if not report or report["status"] != "pending":
            await interaction.response.send_message("Bu skor bildirimi artık beklemede değil.", ephemeral=True)
            return False
        if interaction.user.id != report["opponent_id"] and not (isinstance(interaction.user, discord.Member) and is_staff(interaction.user)):
            await interaction.response.send_message("Bu bildirimi yalnızca rakip oyuncu veya yetkili inceleyebilir.", ephemeral=True)
            return False
        return True

@discord.ui.button(label="Onayla", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        with db() as conn:
            report = conn.execute("SELECT * FROM score_reports WHERE id=?", (self.report_id,)).fetchone()
            if not report or report["status"] != "pending":
                await interaction.response.send_message("Bu bildirim zaten işlenmiş.", ephemeral=True)
                return
            match = conn.execute("SELECT * FROM matches WHERE id=?", (report["match_id"],)).fetchone()
            if not match or match["status"] == "completed":
                conn.execute("UPDATE score_reports SET status='cancelled', reviewed_at=? WHERE id=?", (now_iso(), self.report_id))
                await interaction.response.send_message("Maç artık sonuç kabul etmiyor; yetkiliyle iletişime geç.", ephemeral=True)
                return
            conn.execute("UPDATE score_reports SET status='approved', reviewed_at=? WHERE id=?", (now_iso(), self.report_id))
            conn.execute("UPDATE matches SET status='completed', score=?, winner_id=?, completed_at=? WHERE id=?",
                         (report["score"], report["submitter_id"], now_iso(), report["match_id"]))
        for child in self.children:
            child.disabled = True
        embed = discord.Embed(title="✅ Skor onaylandı", color=discord.Color.green(), description=f"Maç **#{report['match_id']}** sonucu kaydedildi.")
        embed.add_field(name="Skor", value=report["score"], inline=True)
        embed.add_field(name="Kazanan bildirici", value=f"<@{report['submitter_id']}>", inline=True)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("Skor tabloya işlendi.", ephemeral=True)

@discord.ui.button(label="Reddet", style=discord.ButtonStyle.danger, emoji="❌")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        with db() as conn:
            report = conn.execute("SELECT * FROM score_reports WHERE id=?", (self.report_id,)).fetchone()
            if not report or report["status"] != "pending":
                await interaction.response.send_message("Bu bildirim zaten işlenmiş.", ephemeral=True)
                return
            conn.execute("UPDATE score_reports SET status='disputed', reviewed_at=? WHERE id=?", (now_iso(), self.report_id))
            conn.execute("UPDATE matches SET status='disputed' WHERE id=?", (report["match_id"],))
        for child in self.children:
            child.disabled = True
        embed = discord.Embed(title="⚠️ Skor itirazı açıldı", color=discord.Color.orange(), description=f"Maç **#{report['match_id']}** sonucu reddedildi. Hakem incelemesi gerekiyor.")
        embed.add_field(name="Bildirilen skor", value=report["score"], inline=True)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(f"🚨 Skor reddedildi. Hakem ekibi bilgilendirildi. {staff_mentions(interaction.guild)}", allowed_mentions=discord.AllowedMentions(roles=True), ephemeral=False)


@bot.event
async def on_ready():
    log.info("Bot ready as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")


async def setup_hook():
    init_db()
    # Re-register persistent approval views for reports still awaiting a decision.
    with db() as conn:
        report_ids = [row[0] for row in conn.execute("SELECT id FROM score_reports WHERE status='pending'").fetchall()]
    for report_id in report_ids:
        bot.add_view(ScoreApprovalView(report_id))
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info("Synced %s commands to test/server guild %s", len(synced), GUILD_ID)
        else:
            synced = await bot.tree.sync()
            log.info("Synced %s global commands", len(synced))
    except Exception:
        log.exception("Slash command sync failed")


@bot.tree.command(name="kurallar", description="Genel MVSD turnuva kurallarını gösterir")
async def kurallar(interaction: discord.Interaction):
    embed = discord.Embed(title="🏆 MVSD Turnuva Kuralları", color=discord.Color.blurple(), description="Turnuvaya katılan tüm oyuncular aşağıdaki kurallara uymalıdır.")
    for title, body in RULES:
        embed.add_field(name=title, value=body, inline=False)
    embed.set_footer(text="MVSD Tournament Manager • Kurallar turnuva yöneticilerince uygulanır")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="turnuva-olustur", description="Yeni bir turnuva kaydı oluşturur (yetkili)")
@app_commands.describe(ad="Turnuvanın adı")
async def turnuva_olustur(interaction: discord.Interaction, ad: app_commands.Range[str, 3, 80]):
    if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
        await interaction.response.send_message("Bu komut yalnızca yetkililer içindir.", ephemeral=True)
        return
    with db() as conn:
        cur = conn.execute("INSERT INTO tournaments(name, status, created_by, created_at) VALUES(?, 'open', ?, ?)", (ad, interaction.user.id, now_iso()))
        tournament_id = cur.lastrowid
    embed = discord.Embed(title="🏆 Turnuva oluşturuldu", color=discord.Color.green(), description=f"**{ad}** turnuvası oluşturuldu.")
    embed.add_field(name="Turnuva ID", value=str(tournament_id), inline=True)
    embed.add_field(name="Durum", value="Kayıt açık", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="mac-olustur", description="Turnuva için oyuncular arasında maç oluşturur (yetkili)")
@app_commands.describe(turnuva_id="Turnuva ID", tur="Örn. Round 1 / Semi Final / Final", oyuncu1="Birinci Discord oyuncusu", roblox1="Birinci oyuncunun kayıtlı Roblox adı", oyuncu2="İkinci Discord oyuncusu", roblox2="İkinci oyuncunun kayıtlı Roblox adı", mac_kanali="Maç için kullanılacak metin kanalı")
async def mac_olustur(interaction: discord.Interaction, turnuva_id: app_commands.Range[int, 1, 999999], tur: app_commands.Range[str, 2, 40], oyuncu1: discord.Member, roblox1: app_commands.Range[str, 3, 20], oyuncu2: discord.Member, roblox2: app_commands.Range[str, 3, 20], mac_kanali: discord.TextChannel):
    if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
        await interaction.response.send_message("Bu komut yalnızca yetkililer içindir.", ephemeral=True)
        return
    if oyuncu1.id == oyuncu2.id:
        await interaction.response.send_message("Bir maçta aynı oyuncu iki taraf olamaz.", ephemeral=True)
        return
    with db() as conn:
        tournament = conn.execute("SELECT * FROM tournaments WHERE id=?", (turnuva_id,)).fetchone()
        if not tournament or tournament["status"] != "open":
            await interaction.response.send_message("Turnuva bulunamadı veya kayıt/maç durumu kapalı.", ephemeral=True)
            return
        cur = conn.execute("INSERT INTO matches(tournament_id, round_name, channel_id, player1_id, player1_name, player2_id, player2_name, status, created_at) VALUES(?,?,?,?,?,?,?,'scheduled',?)",
                           (turnuva_id, tur, mac_kanali.id, oyuncu1.id, roblox1, oyuncu2.id, roblox2, now_iso()))
        match_id = cur.lastrowid
    embed = discord.Embed(title=f"⚔️ Maç #{match_id}", color=discord.Color.blurple(), description=f"**Turnuva:** {tournament['name']}\n**Tur:** {tur}")
    embed.add_field(name="Oyuncu 1", value=f"{oyuncu1.mention}\nRoblox: `{roblox1}`", inline=True)
    embed.add_field(name="Oyuncu 2", value=f"{oyuncu2.mention}\nRoblox: `{roblox2}`", inline=True)
    embed.add_field(name="Maç kanalı", value=mac_kanali.mention, inline=False)
    embed.set_footer(text="Skor bildirimi bu maç kanalında yapılmalıdır.")
    await interaction.response.send_message(embed=embed)
    try:
        await mac_kanali.send(embed=embed)
    except discord.Forbidden:
        log.warning("Cannot post match %s to channel %s", match_id, mac_kanali.id)


@bot.tree.command(name="skor-tablosu", description="Turnuva maçlarını, skorlarını ve tur durumunu gösterir")
@app_commands.describe(turnuva_id="İsteğe bağlı turnuva ID")
async def skor_tablosu(interaction: discord.Interaction, turnuva_id: Optional[app_commands.Range[int, 1, 999999]] = None):
    query = "SELECT m.*, t.name AS tournament_name FROM matches m JOIN tournaments t ON t.id=m.tournament_id"
    params = ()
    if turnuva_id is not None:
        query += " WHERE m.tournament_id=?"
        params = (turnuva_id,)
    query += " ORDER BY m.tournament_id DESC, m.id DESC LIMIT 20"
    with db() as conn:
        rows = conn.execute(query, params).fetchall()
    if not rows:
        await interaction.response.send_message("Henüz maç kaydı bulunmuyor.", ephemeral=True)
        return
    embed = discord.Embed(title="📊 MVSD Turnuva Skor Tablosu", color=discord.Color.blurple(), description="Son 20 maç kaydı")
    for row in rows:
        status_map = {"scheduled": "🕒 Planlandı", "pending": "🟡 Skor onayı bekliyor", "completed": "✅ Tamamlandı", "disputed": "🚨 Hakem incelemesi", "cancelled": "⛔ İptal"}
        score = row["score"] or "Henüz skor yok"
        winner = f"\nKazanan: <@{row['winner_id']}>" if row["winner_id"] else ""
        value = f"**Tur:** {row['round_name']}\n<@{row['player1_id']}> (`{row['player1_name']}`) vs <@{row['player2_id']}> (`{row['player2_name']}`)\n**Skor:** {score}\n**Durum:** {status_map.get(row['status'], row['status'])}{winner}"
        embed.add_field(name=f"#{row['id']} • {row['tournament_name']}", value=value, inline=False)
    embed.set_footer(text="Skorlar yalnızca rakip onayından veya yetkili kararından sonra kesinleşir.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="hakem-cagir", description="Bulunduğun maç kanalı için hakem çağırır")
@app_commands.describe(aciklama="İsteğe bağlı kısa açıklama")
async def hakem_cagir(interaction: discord.Interaction, aciklama: Optional[app_commands.Range[str, 1, 300]] = None):
    if not interaction.channel or not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("Bu komut bir maç metin kanalında kullanılmalıdır.", ephemeral=True)
        return
    match = await find_match_for_channel(interaction.channel.id)
    if not match:
        await interaction.response.send_message("Bu kanal herhangi bir maça bağlı değil. Yetkiliden maç kanalı kurmasını iste.", ephemeral=True)
        return
    embed = discord.Embed(title="🚨 Hakem Çağrısı", color=discord.Color.red(), description=f"{interaction.user.mention} bir hakem talep ediyor!\n**Maç:** #{match['id']}\n**Tur:** {match['round_name']}")
    if aciklama:
        embed.add_field(name="Açıklama", value=aciklama, inline=False)
    await interaction.response.send_message(content=staff_mentions(interaction.guild), embed=embed, allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False))


@bot.tree.command(name="skor-bildir", description="Maç skorunu ve zafer ekranı görselini bildirir")
@app_commands.describe(roblox_kullanici_adi="Turnuvaya kayıtlı Roblox kullanıcı adın", skor="Örn. 5-3", ss_gorseli="Zafer ekranının ekran görüntüsü")
async def skor_bildir(interaction: discord.Interaction, roblox_kullanici_adi: app_commands.Range[str, 3, 20], skor: app_commands.Range[str, 3, 15], ss_gorseli: discord.Attachment):
    if not interaction.channel:
        await interaction.response.send_message("Bu komut bir maç kanalında kullanılmalıdır.", ephemeral=True)
        return
    if not score_is_valid(skor):
        await interaction.response.send_message("Skor formatı geçersiz. Örnek: `5-3`.", ephemeral=True)
        return
    if ss_gorseli.content_type and not ss_gorseli.content_type.startswith("image/"):
        await interaction.response.send_message("Lütfen zafer ekranını resim dosyası olarak yükle.", ephemeral=True)
        return
    if not ss_gorseli.content_type and not ss_gorseli.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        await interaction.response.send_message("Dosya resim gibi görünmüyor. PNG, JPG veya WEBP yükle.", ephemeral=True)
        return
    match = await find_match_for_channel(interaction.channel.id)
    if not match:
        await interaction.response.send_message("Bu kanal kayıtlı bir maç kanalı değil.", ephemeral=True)
        return
    if match["status"] == "completed":
        await interaction.response.send_message("Bu maçın skoru zaten kesinleşmiş.", ephemeral=True)
        return
    if interaction.user.id not in (match["player1_id"], match["player2_id"]):
        await interaction.response.send_message("Bu maçta kayıtlı oyuncu olmadığın için skor bildiremezsin.", ephemeral=True)
        return
    expected_name = match["player1_name"] if interaction.user.id == match["player1_id"] else match["player2_name"]
    if roblox_kullanici_adi.casefold() != expected_name.casefold():
        await interaction.response.send_message(f"Roblox kullanıcı adın maç kaydıyla eşleşmiyor. Kayıtlı ad: `{expected_name}`", ephemeral=True)
        return
    opponent_id = match["player2_id"] if interaction.user.id == match["player1_id"] else match["player1_id"]
    with db() as conn:
        pending = conn.execute("SELECT id FROM score_reports WHERE match_id=? AND status='pending'", (match["id"],)).fetchone()
        if pending:
            await interaction.response.send_message("Bu maç için zaten bir skor bildirimi bekliyor.", ephemeral=True)
            return
        cur = conn.execute("INSERT INTO score_reports(match_id, submitter_id, opponent_id, roblox_username, score, screenshot_url, status, channel_id, created_at) VALUES(?,?,?,?,?,?,'pending',?,?)",
                           (match["id"], interaction.user.id, opponent_id, roblox_kullanici_adi, skor.strip(), ss_gorseli.url, interaction.channel.id, now_iso()))
        report_id = cur.lastrowid
        conn.execute("UPDATE matches SET status='pending' WHERE id=?", (match["id"],))
    embed = discord.Embed(title="🧾 Skor Onayı Bekleniyor", color=discord.Color.gold(), description=f"Rakip skoru **{skor.strip()}** olarak bildirdi. Onaylıyor musun?")
    embed.add_field(name="Maç", value=f"#{match['id']} • {match['round_name']}", inline=True)
    embed.add_field(name="Skoru bildiren", value=interaction.user.mention, inline=True)
    embed.add_field(name="Rakip onayı", value=f"<@{opponent_id}>", inline=True)
    embed.set_image(url=ss_gorseli.url)
    embed.set_footer(text=f"Bildirim ID: {report_id} • Rakip onaylarsa skor kesinleşir")
    view = ScoreApprovalView(report_id)
    await interaction.response.send_message("Skor bildirimi oluşturuldu.", ephemeral=True)
    message = await interaction.channel.send(content=f"<@{opponent_id}>", embed=embed, view=view, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
    with db() as conn:
        conn.execute("UPDATE score_reports SET message_id=? WHERE id=?", (message.id, report_id))
    bot.add_view(view)


@bot.tree.command(name="sampiyon-ver", description="Final galibiyeti doğrulandıktan sonra şampiyon rolü verir")
@app_commands.describe(kullanici="Şampiyon ilan edilecek kullanıcı", mac_id="Tamamlanmış final maçının ID'si")
async def sampiyon_ver(interaction: discord.Interaction, kullanici: discord.Member, mac_id: app_commands.Range[int, 1, 999999]):
    if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
        await interaction.response.send_message("Bu komut yalnızca yetkililer içindir.", ephemeral=True)
        return
    if not CHAMPION_ROLE_ID:
        await interaction.response.send_message("Şampiyon rolü ayarlanmamış. `.env` dosyasındaki `CHAMPION_ROLE_ID` değerini yapılandır.", ephemeral=True)
        return
    with db() as conn:
        match = conn.execute("SELECT * FROM matches WHERE id=?", (mac_id,)).fetchone()
    if not match or match["status"] != "completed":
        await interaction.response.send_message("Belirtilen maç bulunamadı veya sonucu kesinleşmedi.", ephemeral=True)
        return
    if match["round_name"].casefold() != "final":
        await interaction.response.send_message("Şampiyon rolü yalnızca `Final` turu tamamlanmış bir maçtan sonra verilebilir.", ephemeral=True)
        return
    if match["winner_id"] != kullanici.id:
        await interaction.response.send_message("Seçilen kullanıcı, final maçının kayıtlı kazananı değil.", ephemeral=True)
        return
    role = interaction.guild.get_role(CHAMPION_ROLE_ID) if interaction.guild else None
    if not role:
        await interaction.response.send_message("Şampiyon rolü bu sunucuda bulunamadı. Rol ID'sini kontrol et.", ephemeral=True)
        return
    try:
        await kullanici.add_roles(role, reason=f"MVSD final şampiyonu • Maç #{mac_id}")
    except discord.Forbidden:
        await interaction.response.send_message("Bot bu rolü veremiyor. Bot rolünü şampiyon rolünün üstüne taşı ve Manage Roles iznini kontrol et.", ephemeral=True)
        return
    embed = discord.Embed(title="🏆 MVSD TOURNAMENT CHAMPION", color=discord.Color.gold(), description=f"Tebrikler {kullanici.mention}! Final maçını kazanarak **Tournament Champion** oldun.")
    embed.add_field(name="Final maçı", value=f"#{mac_id}", inline=True)
    embed.add_field(name="Skor", value=match["score"] or "Kayıtlı", inline=True)
    embed.set_footer(text="MVSD Tournament Manager")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="mac-bilgisi", description="Bir maçın ayrıntılarını gösterir")
@app_commands.describe(mac_id="Maç ID'si")
async def mac_bilgisi(interaction: discord.Interaction, mac_id: app_commands.Range[int, 1, 999999]):
    with db() as conn:
        row = conn.execute("SELECT m.*, t.name AS tournament_name FROM matches m JOIN tournaments t ON t.id=m.tournament_id WHERE m.id=?", (mac_id,)).fetchone()
    if not row:
        await interaction.response.send_message("Bu ID ile maç bulunamadı.", ephemeral=True)
        return
    status_map = {"scheduled": "Planlandı", "pending": "Skor onayı bekliyor", "completed": "Tamamlandı", "disputed": "Hakem incelemesinde", "cancelled": "İptal"}
    embed = discord.Embed(title=f"⚔️ Maç #{row['id']}", color=discord.Color.blurple())
    embed.add_field(name="Turnuva", value=row["tournament_name"], inline=True)
    embed.add_field(name="Tur", value=row["round_name"], inline=True)
    embed.add_field(name="Durum", value=status_map.get(row["status"], row["status"]), inline=True)
    embed.add_field(name="Oyuncular", value=f"<@{row['player1_id']}> (`{row['player1_name']}`)\nvs\n<@{row['player2_id']}> (`{row['player2_name']}`)", inline=False)
    embed.add_field(name="Skor", value=row["score"] or "Henüz kesinleşmedi", inline=True)
    if row["winner_id"]:
        embed.add_field(name="Kazanan", value=f"<@{row['winner_id']}>", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="turnuva-kapat", description="Bir turnuvanın yeni maç oluşturulmasını durdurur (yetkili)")
@app_commands.describe(turnuva_id="Turnuva ID")
async def turnuva_kapat(interaction: discord.Interaction, turnuva_id: app_commands.Range[int, 1, 999999]):
    if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
        await interaction.response.send_message("Bu komut yalnızca yetkililer içindir.", ephemeral=True)
        return
    with db() as conn:
        cur = conn.execute("UPDATE tournaments SET status='closed' WHERE id=? AND status='open'", (turnuva_id,))
    if cur.rowcount == 0:
        await interaction.response.send_message("Açık turnuva bulunamadı. ID'yi kontrol et.", ephemeral=True)
        return
    await interaction.response.send_message(f"Turnuva **#{turnuva_id}** kapatıldı.")


bot.setup_hook = setup_hook

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN bulunamadı. .env dosyasını oluşturup bot tokenını ekle.")

bot.run(TOKEN)
