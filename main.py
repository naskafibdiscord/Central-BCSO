import asyncio
import io
import json
import os
import re
import time
import unicodedata

import discord
from discord import app_commands
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

load_dotenv()

DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "data.json")


# ---------- Stockage (data.json) ----------
def load():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)


def default_responses():
    return [
        {
            "id": "acceptee",
            "name": "Candidature acceptée",
            "text": (
                "Bonjour,\n"
                "Nous avons le plaisir de vous informer que votre candidature pour rejoindre le BCSO a été acceptée !\n"
                "Votre profil et vos motivations ont retenu toute notre attention. Nous vous invitons à vous manifester "
                "dès que possible afin d'organiser votre intégration et de planifier vos premières étapes au sein du service.\n"
                "Félicitations et bienvenue parmi nous !\n"
                "Cordialement,\n"
                "L'équipe de direction du BCSO"
            ),
        },
        {
            "id": "refusee",
            "name": "Candidature refusée",
            "text": (
                "Bonjour,\n"
                "Nous vous remercions pour votre intérêt envers le BCSO.\n"
                "Après étude de votre dossier, nous avons le regret de vous informer que votre candidature n'a pas été "
                "retenue pour cette session. Les places étant limitées, nous avons dû effectuer une sélection stricte.\n"
                "Nous vous souhaitons une bonne continuation et vous encourageons à postuler de nouveau lors de prochains recrutements.\n"
                "Cordialement,\n"
                "La direction du BCSO"
            ),
        },
    ]


def default_config():
    return {
        "title": "🎫 Support",
        "description": "Choisissez un type de ticket dans le menu ci-dessous.",
        "types": [],
        "categoryId": None,
        "staffRoleId": None,
        "responses": default_responses(),
        "blacklist": {},
        "welcome": {},
    }


def get_guild(guild_id):
    g = default_config()
    g.update(load().get(str(guild_id), {}))
    return g


def update_guild(guild_id, fn):
    db = load()
    g = default_config()
    g.update(db.get(str(guild_id), {}))
    fn(g)
    db[str(guild_id)] = g
    save(db)
    return g


def slug(text):
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "ticket"


# ---------- Panel (menu déroulant) ----------
class TicketSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(
            custom_id="ticket_select",
            placeholder="Choisissez un type de ticket",
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        g = get_guild(guild.id)

        # Réinitialise le menu pour pouvoir re-sélectionner la même option
        if g["types"]:
            try:
                await interaction.message.edit(view=build_panel_view(g))
            except discord.HTTPException:
                pass

        type_id = self.values[0]
        ticket_type = next((t for t in g["types"] if t["id"] == type_id), None)
        if not ticket_type:
            return await interaction.followup.send("❌ Ce type n'existe plus.", ephemeral=True)

        topic = f"ticket:{interaction.user.id}:{type_id}"
        existing = discord.utils.find(lambda c: c.topic == topic, guild.text_channels)
        if existing:
            return await interaction.followup.send(
                f"❌ Tu as déjà un ticket ouvert : {existing.mention}", ephemeral=True
            )

        perms = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: perms,
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, manage_channels=True
            ),
        }
        staff_role = guild.get_role(g["staffRoleId"]) if g["staffRoleId"] else None
        if staff_role:
            overwrites[staff_role] = perms

        category = guild.get_channel(g["categoryId"]) if g["categoryId"] else None

        try:
            channel = await guild.create_text_channel(
                name=f"{slug(ticket_type['name'])}-{slug(interaction.user.name)}"[:90],
                category=category,
                topic=topic,
                overwrites=overwrites,
            )
        except discord.HTTPException:
            return await interaction.followup.send(
                "❌ Impossible de créer le ticket (vérifie la permission « Gérer les salons » du bot).",
                ephemeral=True,
            )

        embed = discord.Embed(
            title=f"{ticket_type['emoji']} {ticket_type['name']}".strip(),
            description=f"Bienvenue {interaction.user.mention}, explique ta demande. Le staff va te répondre.",
            color=0x57F287,
        )
        await channel.send(
            content=staff_role.mention if staff_role else None,
            embed=embed,
            view=CloseView(),
        )
        await interaction.followup.send(f"✅ Ticket créé : {channel.mention}", ephemeral=True)


def build_panel_view(g):
    options = [
        discord.SelectOption(
            label=t["name"],
            value=t["id"],
            description=t["description"] or None,
            emoji=t["emoji"] or None,
        )
        for t in g["types"]
    ]
    view = discord.ui.View(timeout=None)
    view.add_item(TicketSelect(options))
    return view


def build_panel_embed(g):
    return discord.Embed(title=g["title"], description=g["description"], color=0x5865F2)


# ---------- Bouton fermer ----------
class CloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Fermer le ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = get_guild(interaction.guild_id)
        parts = (interaction.channel.topic or "").split(":")
        owner_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        is_staff = (
            (g["staffRoleId"] and interaction.user.get_role(g["staffRoleId"]) is not None)
            or interaction.user.guild_permissions.manage_channels
        )
        if interaction.user.id != owner_id and not is_staff:
            return await interaction.response.send_message("❌ Tu ne peux pas fermer ce ticket.", ephemeral=True)

        await interaction.response.send_message("🔒 Ticket fermé, suppression dans 5 secondes...")
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete()
        except discord.HTTPException:
            pass


# ---------- Modal apparence ----------
class ApparenceModal(discord.ui.Modal, title="Apparence du panel"):
    def __init__(self, g):
        super().__init__()
        self.titre = discord.ui.TextInput(label="Titre", default=g["title"], max_length=256)
        self.desc = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            default=g["description"],
            max_length=4000,
        )
        self.add_item(self.titre)
        self.add_item(self.desc)

    async def on_submit(self, interaction: discord.Interaction):
        def apply(g):
            g["title"] = self.titre.value
            g["description"] = self.desc.value

        update_guild(interaction.guild_id, apply)
        await interaction.response.send_message(
            "✅ Apparence mise à jour. Refais `/ticket_config envoyer` pour mettre à jour le panel.",
            ephemeral=True,
        )


# ---------- Commande /ticket_config ----------
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
class TicketConfig(app_commands.Group, name="ticket_config", description="Configurer le système de tickets"):

    @app_commands.command(name="apparence", description="Modifier le titre et la description du panel")
    async def apparence(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ApparenceModal(get_guild(interaction.guild_id)))

    @app_commands.command(name="type_ajouter", description="Ajouter un type de ticket au menu")
    @app_commands.describe(nom="Ex : Recrutement", description="Petite description sous le nom", emoji="Ex : 📝")
    async def type_ajouter(
        self,
        interaction: discord.Interaction,
        nom: app_commands.Range[str, 1, 80],
        description: app_commands.Range[str, 0, 100] = "",
        emoji: str = "",
    ):
        g = get_guild(interaction.guild_id)
        if len(g["types"]) >= 25:
            return await interaction.response.send_message("❌ Maximum 25 types.", ephemeral=True)
        new_type = {
            "id": format(int(time.time() * 1000), "x"),
            "name": nom,
            "description": description,
            "emoji": emoji,
        }
        update_guild(interaction.guild_id, lambda x: x["types"].append(new_type))
        await interaction.response.send_message(
            f"✅ Type **{nom}** ajouté. Refais `/ticket_config envoyer` pour mettre à jour le panel.",
            ephemeral=True,
        )

    @app_commands.command(name="type_retirer", description="Retirer un type de ticket")
    @app_commands.describe(nom="Type à retirer")
    async def type_retirer(self, interaction: discord.Interaction, nom: str):
        g = get_guild(interaction.guild_id)
        if not any(t["id"] == nom for t in g["types"]):
            return await interaction.response.send_message("❌ Type introuvable.", ephemeral=True)
        update_guild(interaction.guild_id, lambda x: x.update(types=[t for t in x["types"] if t["id"] != nom]))
        await interaction.response.send_message(
            "🗑️ Type retiré. Refais `/ticket_config envoyer` pour mettre à jour le panel.", ephemeral=True
        )

    @type_retirer.autocomplete("nom")
    async def type_retirer_autocomplete(self, interaction: discord.Interaction, current: str):
        g = get_guild(interaction.guild_id)
        return [
            app_commands.Choice(name=t["name"][:100], value=t["id"])
            for t in g["types"]
            if current.lower() in t["name"].lower()
        ][:25]

    @app_commands.command(name="categorie", description="Catégorie où les tickets seront créés")
    @app_commands.describe(categorie="La catégorie")
    async def categorie(self, interaction: discord.Interaction, categorie: discord.CategoryChannel):
        update_guild(interaction.guild_id, lambda x: x.update(categoryId=categorie.id))
        await interaction.response.send_message(
            f"✅ Les tickets seront créés dans **{categorie.name}**.", ephemeral=True
        )

    @app_commands.command(name="role_staff", description="Rôle qui peut voir et gérer les tickets")
    @app_commands.describe(role="Le rôle staff")
    async def role_staff(self, interaction: discord.Interaction, role: discord.Role):
        update_guild(interaction.guild_id, lambda x: x.update(staffRoleId=role.id))
        await interaction.response.send_message(f"✅ Rôle staff : {role.mention}.", ephemeral=True)

    @app_commands.command(name="envoyer", description="Envoyer le panel de tickets dans un salon")
    @app_commands.describe(salon="Salon du panel")
    async def envoyer(self, interaction: discord.Interaction, salon: discord.TextChannel):
        g = get_guild(interaction.guild_id)
        if not g["types"]:
            return await interaction.response.send_message(
                "❌ Ajoute au moins un type avec `/ticket_config type_ajouter`.", ephemeral=True
            )
        await salon.send(embed=build_panel_embed(g), view=build_panel_view(g))
        await interaction.response.send_message(f"✅ Panel envoyé dans {salon.mention}.", ephemeral=True)

    @app_commands.command(name="voir", description="Voir la configuration actuelle")
    async def voir(self, interaction: discord.Interaction):
        g = get_guild(interaction.guild_id)
        embed = discord.Embed(title="⚙️ Configuration des tickets", color=0x5865F2)
        embed.add_field(name="Titre", value=g["title"], inline=False)
        embed.add_field(name="Description", value=g["description"][:1000], inline=False)
        types = "\n".join(f"{t['emoji']} {t['name']}".strip() for t in g["types"]) or "Aucun"
        embed.add_field(name="Types", value=types, inline=False)
        embed.add_field(name="Catégorie", value=f"<#{g['categoryId']}>" if g["categoryId"] else "Non définie")
        embed.add_field(name="Rôle staff", value=f"<@&{g['staffRoleId']}>" if g["staffRoleId"] else "Non défini")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------- Réponses types : /reponse_config et /reponse ----------
def render_response(text, member):
    """Place la mention à côté de « Bonjour » (ou à la place de {user} si présent)."""
    if "{user}" in text:
        return text.replace("{user}", member.mention)
    match = re.search(r"bonjour", text, re.IGNORECASE)
    if match:
        return text[: match.end()] + " " + member.mention + text[match.end():]
    return f"{member.mention}\n{text}"


async def response_choices(interaction: discord.Interaction, current: str):
    g = get_guild(interaction.guild_id)
    return [
        app_commands.Choice(name=r["name"][:100], value=r["id"])
        for r in g["responses"]
        if current.lower() in r["name"].lower()
    ][:25]


class ReponseModal(discord.ui.Modal):
    def __init__(self, existing=None):
        super().__init__(title="Modifier la réponse" if existing else "Nouvelle réponse")
        self.existing_id = existing["id"] if existing else None
        self.nom = discord.ui.TextInput(
            label="Nom de la réponse",
            placeholder="Ex : Candidature acceptée",
            default=existing["name"] if existing else None,
            max_length=50,
        )
        self.texte = discord.ui.TextInput(
            label="Texte du message",
            style=discord.TextStyle.paragraph,
            placeholder="Bonjour,\n...",
            default=existing["text"] if existing else None,
            max_length=1900,
        )
        self.add_item(self.nom)
        self.add_item(self.texte)

    async def on_submit(self, interaction: discord.Interaction):
        g = get_guild(interaction.guild_id)
        if not self.existing_id and len(g["responses"]) >= 25:
            return await interaction.response.send_message("❌ Maximum 25 réponses.", ephemeral=True)

        def apply(x):
            if self.existing_id:
                for r in x["responses"]:
                    if r["id"] == self.existing_id:
                        r["name"] = self.nom.value
                        r["text"] = self.texte.value
            else:
                x["responses"].append(
                    {"id": format(int(time.time() * 1000), "x"), "name": self.nom.value, "text": self.texte.value}
                )

        update_guild(interaction.guild_id, apply)
        await interaction.response.send_message(
            f"✅ Réponse **{self.nom.value}** enregistrée. Utilise `/reponse` pour l'envoyer.", ephemeral=True
        )


@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
class ReponseConfig(app_commands.Group, name="reponse_config", description="Configurer les réponses types"):

    @app_commands.command(name="ajouter", description="Créer une nouvelle réponse type")
    async def ajouter(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ReponseModal())

    @app_commands.command(name="modifier", description="Modifier une réponse type")
    @app_commands.describe(nom="Réponse à modifier")
    async def modifier(self, interaction: discord.Interaction, nom: str):
        g = get_guild(interaction.guild_id)
        r = next((r for r in g["responses"] if r["id"] == nom), None)
        if not r:
            return await interaction.response.send_message("❌ Réponse introuvable.", ephemeral=True)
        await interaction.response.send_modal(ReponseModal(r))

    @modifier.autocomplete("nom")
    async def modifier_autocomplete(self, interaction: discord.Interaction, current: str):
        return await response_choices(interaction, current)

    @app_commands.command(name="supprimer", description="Supprimer une réponse type")
    @app_commands.describe(nom="Réponse à supprimer")
    async def supprimer(self, interaction: discord.Interaction, nom: str):
        g = get_guild(interaction.guild_id)
        if not any(r["id"] == nom for r in g["responses"]):
            return await interaction.response.send_message("❌ Réponse introuvable.", ephemeral=True)
        update_guild(interaction.guild_id, lambda x: x.update(responses=[r for r in x["responses"] if r["id"] != nom]))
        await interaction.response.send_message("🗑️ Réponse supprimée.", ephemeral=True)

    @supprimer.autocomplete("nom")
    async def supprimer_autocomplete(self, interaction: discord.Interaction, current: str):
        return await response_choices(interaction, current)

    @app_commands.command(name="voir", description="Voir les réponses types enregistrées")
    async def voir(self, interaction: discord.Interaction):
        g = get_guild(interaction.guild_id)
        if not g["responses"]:
            return await interaction.response.send_message(
                "Aucune réponse. Crée-en une avec `/reponse_config ajouter`.", ephemeral=True
            )
        embed = discord.Embed(title="📨 Réponses types", color=0x5865F2)
        for r in g["responses"]:
            preview = r["text"][:200] + ("…" if len(r["text"]) > 200 else "")
            embed.add_field(name=r["name"], value=preview, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ResponseSelect(discord.ui.Select):
    def __init__(self, responses):
        super().__init__(
            placeholder="1️⃣ Choisis le texte à envoyer",
            options=[discord.SelectOption(label=r["name"][:100], value=r["id"]) for r in responses],
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_id = self.values[0]
        await interaction.response.defer()


class MemberSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="2️⃣ Choisis la personne à mentionner")

    async def callback(self, interaction: discord.Interaction):
        self.view.member = self.values[0]
        await interaction.response.defer()


class SendButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Envoyer", emoji="📨", style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not view.selected_id or not view.member:
            return await interaction.response.send_message(
                "❌ Choisis un texte ET une personne avant d'envoyer.", ephemeral=True
            )
        g = get_guild(interaction.guild_id)
        r = next((r for r in g["responses"] if r["id"] == view.selected_id), None)
        if not r:
            return await interaction.response.send_message("❌ Ce texte n'existe plus.", ephemeral=True)
        try:
            await interaction.channel.send(
                render_response(r["text"], view.member),
                allowed_mentions=discord.AllowedMentions(users=[view.member]),
            )
        except discord.HTTPException:
            return await interaction.response.send_message(
                "❌ Impossible d'envoyer ici (vérifie les permissions du bot dans ce salon).", ephemeral=True
            )
        view.stop()
        await interaction.response.edit_message(content="✅ Message envoyé !", view=None)


class ResponsePanel(discord.ui.View):
    def __init__(self, responses):
        super().__init__(timeout=300)
        self.selected_id = None
        self.member = None
        self.add_item(ResponseSelect(responses))
        self.add_item(MemberSelect())
        self.add_item(SendButton())


@app_commands.command(name="reponse", description="Ouvrir le panel pour envoyer une réponse type")
@app_commands.default_permissions(manage_messages=True)
@app_commands.guild_only()
async def reponse(interaction: discord.Interaction):
    g = get_guild(interaction.guild_id)
    if not g["responses"]:
        return await interaction.response.send_message(
            "❌ Aucune réponse. Crée-en une avec `/reponse_config ajouter`.", ephemeral=True
        )
    await interaction.response.send_message(
        "📨 **Réponses types** : choisis le texte, la personne à mentionner, puis clique sur Envoyer.",
        view=ResponsePanel(g["responses"]),
        ephemeral=True,
    )


# ---------- Blacklist : /blacklist, /unblacklist, /blacklist_liste ----------
BLACKLIST_ROLE_NAME = "blacklist"


async def get_blacklist_role(guild, create=True):
    role = discord.utils.find(lambda r: r.name.lower() == BLACKLIST_ROLE_NAME, guild.roles)
    if not role and create:
        role = await guild.create_role(name=BLACKLIST_ROLE_NAME, reason="Système de blacklist")
    return role


async def hide_channels(guild, role):
    """Refuse « Voir le salon » au rôle blacklist sur tous les salons et catégories."""
    count = 0
    for channel in guild.channels:
        overwrite = channel.overwrites_for(role)
        if overwrite.view_channel is False:
            continue
        overwrite.view_channel = False
        try:
            await channel.set_permissions(role, overwrite=overwrite, reason="Blacklist : salon masqué")
            count += 1
        except discord.HTTPException as e:
            print(f"❌ Impossible de masquer #{channel.name} : {e}")
    return count


def clean_id(text):
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


@app_commands.command(name="blacklist", description="Blacklister quelqu'un définitivement (même s'il quitte le serveur)")
@app_commands.describe(identifiant="ID Discord de la personne", raison="Raison (optionnel)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def blacklist(interaction: discord.Interaction, identifiant: str, raison: str = "Non précisée"):
    uid = clean_id(identifiant)
    if not uid:
        return await interaction.response.send_message("❌ Identifiant invalide (chiffres uniquement).", ephemeral=True)
    g = get_guild(interaction.guild_id)
    if str(uid) in g["blacklist"]:
        return await interaction.response.send_message("⚠️ Cette personne est déjà blacklistée.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    try:
        user = await interaction.client.fetch_user(uid)
    except discord.NotFound:
        return await interaction.followup.send("❌ Aucun compte Discord avec cet identifiant.", ephemeral=True)

    entry = {
        "name": str(user),
        "reason": raison[:500],
        "by": interaction.user.id,
        "date": time.strftime("%d/%m/%Y"),
    }
    update_guild(interaction.guild_id, lambda x: x["blacklist"].__setitem__(str(uid), entry))

    guild = interaction.guild
    member = guild.get_member(uid)
    if member is None:
        try:
            member = await guild.fetch_member(uid)
        except discord.NotFound:
            member = None

    try:
        role = await get_blacklist_role(guild)
        await hide_channels(guild, role)
    except discord.Forbidden:
        role = None

    if role is None:
        status = (
            "⚠️ Enregistré, mais je n'ai pas pu préparer le rôle : "
            "donne-moi « Gérer les rôles » et « Gérer les salons »."
        )
    elif member:
        try:
            await member.add_roles(role, reason=f"Blacklist par {interaction.user}")
            status = f"Le rôle **{role.name}** a été donné à {member.mention} : il ne voit plus aucun salon."
        except discord.Forbidden:
            status = (
                "⚠️ Enregistré, mais je n'ai pas pu donner le rôle : "
                "mon rôle doit être **au-dessus** du rôle blacklist."
            )
    else:
        status = "La personne n'est pas sur le serveur : le rôle lui sera donné automatiquement si elle revient."

    await interaction.followup.send(
        f"🚫 **{user}** (`{uid}`) est blacklisté(e).\nRaison : {entry['reason']}\n{status}", ephemeral=True
    )


@app_commands.command(name="unblacklist", description="Retirer quelqu'un de la blacklist")
@app_commands.describe(identifiant="ID Discord de la personne")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def unblacklist(interaction: discord.Interaction, identifiant: str):
    uid = clean_id(identifiant)
    g = get_guild(interaction.guild_id)
    if not uid or str(uid) not in g["blacklist"]:
        return await interaction.response.send_message("❌ Cette personne n'est pas blacklistée.", ephemeral=True)
    update_guild(interaction.guild_id, lambda x: x["blacklist"].pop(str(uid), None))

    member = interaction.guild.get_member(uid)
    role = await get_blacklist_role(interaction.guild, create=False)
    if member and role and role in member.roles:
        try:
            await member.remove_roles(role, reason=f"Retrait blacklist par {interaction.user}")
        except discord.Forbidden:
            pass
    await interaction.response.send_message(f"✅ `{uid}` a été retiré(e) de la blacklist.", ephemeral=True)


@app_commands.command(name="blacklist_liste", description="Voir toutes les personnes blacklistées")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def blacklist_liste(interaction: discord.Interaction):
    g = get_guild(interaction.guild_id)
    if not g["blacklist"]:
        return await interaction.response.send_message("La blacklist est vide.", ephemeral=True)
    lines = [
        f"• <@{uid}> (`{uid}`) — {e['reason']} _({e['date']})_" for uid, e in g["blacklist"].items()
    ]
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    await interaction.response.send_message(
        embed=discord.Embed(title=f"🚫 Blacklist ({len(lines)})", description=text, color=0xED4245),
        ephemeral=True,
    )


# ---------- Bienvenue : /bienvenue_config ----------
W_WIDTH, W_HEIGHT = 1000, 420


def welcome_defaults():
    return {
        "enabled": False,
        "channelId": None,
        "roleId": None,
        "color": "#5865F2",
        "hasBackground": False,
        "title": "Bienvenue sur {server} !",
        "message": (
            "Nous sommes ravis de t'accueillir parmi nous ! 🎉\n\n"
            "📜 Prends le temps de lire le règlement.\n"
            "💬 N'hésite pas à te présenter et à discuter avec la communauté.\n\n"
            "Tu es notre **membre n°{count}** !"
        ),
    }


def get_welcome(g):
    return {**welcome_defaults(), **g.get("welcome", {})}


def set_welcome(guild_id, **changes):
    def apply(x):
        x["welcome"] = {**welcome_defaults(), **x.get("welcome", {}), **changes}
    return update_guild(guild_id, apply)


def background_path(guild_id):
    return os.path.join(DATA_DIR, f"welcome_bg_{guild_id}.img")


def hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def get_font(size, bold=True):
    candidates = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"]
        if bold else
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf"]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)  # police intégrée à Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def _circle(img, center, diameter, fill):
    """Cercle anti-aliasé."""
    scale = 4
    mask = Image.new("L", (diameter * scale, diameter * scale), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, diameter * scale - 1, diameter * scale - 1), fill=255)
    mask = mask.resize((diameter, diameter), Image.LANCZOS)
    layer = fill if isinstance(fill, Image.Image) else Image.new("RGBA", (diameter, diameter), fill)
    img.paste(layer, (center[0] - diameter // 2, center[1] - diameter // 2), mask)


def make_welcome_card(avatar_bytes, bg_bytes, name, count, color, server):
    """Génère l'image de bienvenue (1000x420)."""
    W, H = W_WIDTH, W_HEIGHT
    accent = hex_to_rgb(color)
    light = tuple(min(255, int(c + (255 - c) * 0.5)) for c in accent)

    # --- Fond
    if bg_bytes:
        bg = Image.open(io.BytesIO(bg_bytes)).convert("RGB")
        bg = ImageOps.fit(bg, (W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(1.5))
        bg = Image.blend(bg, Image.new("RGB", (W, H), (6, 6, 12)), 0.58)
    else:
        top = Image.new("RGB", (W, H), tuple(int(c * 0.22) for c in accent))
        bottom = Image.new("RGB", (W, H), tuple(int(c * 0.05) + 4 for c in accent))
        bg = Image.composite(bottom, top, Image.linear_gradient("L").resize((W, H)))
    img = bg.convert("RGBA")

    # --- Halos lumineux
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((W // 2 - 220, -140, W // 2 + 220, 300), fill=accent + (170,))
    gd.ellipse((-180, H - 170, 260, H + 200), fill=accent + (80,))
    gd.ellipse((W - 260, -180, W + 180, 170), fill=accent + (80,))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(75)))

    # --- Cadre fin
    frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(frame).rounded_rectangle((16, 16, W - 17, H - 17), radius=30, outline=accent + (170,), width=3)
    img = Image.alpha_composite(img, frame)

    # --- Avatar (anneau couleur + liseré sombre)
    size, cx, cy = 190, W // 2, 125
    avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((size, size), Image.LANCZOS)
    _circle(img, (cx, cy), size + 30, accent + (255,))
    _circle(img, (cx, cy), size + 14, (10, 10, 18, 255))
    _circle(img, (cx, cy), size, avatar)

    # --- Textes
    draw = ImageDraw.Draw(img)

    def centered(text, y, font, fill, spacing=0, shadow=True):
        widths = [font.getlength(ch) for ch in text]
        total = sum(widths) + spacing * (len(text) - 1)
        x = (W - total) / 2
        for ch, w in zip(text, widths):
            if shadow:
                draw.text((x + 2, y + 3), ch, font=font, fill=(0, 0, 0, 255))
            draw.text((x, y), ch, font=font, fill=fill)
            x += w + spacing

    centered("BIENVENUE", 243, get_font(54), (255, 255, 255, 255), spacing=9)

    name = name if len(name) <= 24 else name[:23] + "…"
    fsize = 40
    while get_font(fsize).getlength(name) > W - 140 and fsize > 20:
        fsize -= 2
    centered(name, 310, get_font(fsize), light + (255,))

    # Séparateur
    draw.rounded_rectangle((W // 2 - 40, 366, W // 2 + 40, 369), radius=2, fill=accent + (255,))

    sub = f"Membre n°{count}  •  {server}"
    if get_font(22, bold=False).getlength(sub) > W - 140:
        sub = f"Membre n°{count}"
    centered(sub, 376, get_font(20, bold=False), (200, 202, 215, 255), shadow=False)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def fill_placeholders(text, member):
    return (
        text.replace("{user}", member.mention)
        .replace("{name}", member.display_name)
        .replace("{server}", member.guild.name)
        .replace("{count}", str(member.guild.member_count))
    )


async def build_welcome(member, w):
    avatar_bytes = await member.display_avatar.replace(size=256, format="png").read()
    bg_bytes = None
    if w["hasBackground"]:
        try:
            with open(background_path(member.guild.id), "rb") as f:
                bg_bytes = f.read()
        except OSError:
            pass
    card = await asyncio.to_thread(
        make_welcome_card, avatar_bytes, bg_bytes, member.display_name,
        member.guild.member_count, w["color"], member.guild.name,
    )
    embed = discord.Embed(
        title=fill_placeholders(w["title"], member)[:256],
        description=fill_placeholders(w["message"], member)[:4000],
        color=int(w["color"].lstrip("#"), 16),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_image(url="attachment://bienvenue.png")
    embed.set_footer(text=member.guild.name, icon_url=member.guild.icon.url if member.guild.icon else None)
    return embed, discord.File(io.BytesIO(card), filename="bienvenue.png")


async def send_welcome(member):
    if member.bot:
        return
    g = get_guild(member.guild.id)
    w = get_welcome(g)

    if w["roleId"]:
        role = member.guild.get_role(w["roleId"])
        if role:
            try:
                await member.add_roles(role, reason="Rôle automatique d'arrivée")
            except discord.HTTPException:
                pass

    if not w["enabled"] or not w["channelId"]:
        return
    channel = member.guild.get_channel(w["channelId"])
    if not channel:
        return
    try:
        embed, file = await build_welcome(member, w)
        await channel.send(
            content=f"👋 {member.mention}",
            embed=embed,
            file=file,
            allowed_mentions=discord.AllowedMentions(users=[member]),
        )
    except Exception as e:
        print(f"❌ Message de bienvenue impossible : {e}")


class WelcomeModal(discord.ui.Modal, title="Message de bienvenue"):
    def __init__(self, w):
        super().__init__()
        self.titre = discord.ui.TextInput(label="Titre", default=w["title"], max_length=200)
        self.texte = discord.ui.TextInput(
            label="Message ({user} {name} {server} {count})",
            style=discord.TextStyle.paragraph, default=w["message"], max_length=1500,
        )
        self.add_item(self.titre)
        self.add_item(self.texte)

    async def on_submit(self, interaction: discord.Interaction):
        set_welcome(interaction.guild_id, title=self.titre.value, message=self.texte.value)
        await interaction.response.send_message(
            "✅ Message mis à jour. Teste le rendu avec `/bienvenue_config test`.", ephemeral=True
        )


@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
class WelcomeConfig(app_commands.Group, name="bienvenue_config", description="Configurer le système de bienvenue"):

    @app_commands.command(name="salon", description="Salon où les messages de bienvenue sont envoyés (et active le système)")
    @app_commands.describe(salon="Le salon de bienvenue")
    async def salon(self, interaction: discord.Interaction, salon: discord.TextChannel):
        set_welcome(interaction.guild_id, channelId=salon.id, enabled=True)
        await interaction.response.send_message(f"✅ Bienvenue activée dans {salon.mention}.", ephemeral=True)

    @app_commands.command(name="message", description="Modifier le titre et le texte du message")
    async def message(self, interaction: discord.Interaction):
        await interaction.response.send_modal(WelcomeModal(get_welcome(get_guild(interaction.guild_id))))

    @app_commands.command(name="couleur", description="Couleur principale (ex : #FF5733)")
    @app_commands.describe(hexa="Code couleur hexadécimal, ex : #FF5733")
    async def couleur(self, interaction: discord.Interaction, hexa: str):
        hexa = hexa.strip()
        if not re.fullmatch(r"#?[0-9a-fA-F]{6}", hexa):
            return await interaction.response.send_message("❌ Format invalide. Exemple : `#FF5733`", ephemeral=True)
        set_welcome(interaction.guild_id, color="#" + hexa.lstrip("#").upper())
        await interaction.response.send_message("✅ Couleur mise à jour.", ephemeral=True)

    @app_commands.command(name="fond", description="Image de fond de la carte (sans image = retire le fond)")
    @app_commands.describe(image="Image de fond (idéal : 1000x420)")
    async def fond(self, interaction: discord.Interaction, image: discord.Attachment = None):
        if image is None:
            set_welcome(interaction.guild_id, hasBackground=False)
            return await interaction.response.send_message("🗑️ Fond retiré (dégradé par défaut).", ephemeral=True)
        if not (image.content_type or "").startswith("image/") or image.size > 8_000_000:
            return await interaction.response.send_message("❌ Envoie une image de moins de 8 Mo.", ephemeral=True)
        data = await image.read()
        try:
            await asyncio.to_thread(lambda: Image.open(io.BytesIO(data)).verify())
        except Exception:
            return await interaction.response.send_message("❌ Image illisible.", ephemeral=True)
        with open(background_path(interaction.guild_id), "wb") as f:
            f.write(data)
        set_welcome(interaction.guild_id, hasBackground=True)
        await interaction.response.send_message("✅ Fond enregistré. Teste avec `/bienvenue_config test`.", ephemeral=True)

    @app_commands.command(name="role_auto", description="Rôle donné automatiquement à l'arrivée (sans rôle = désactivé)")
    @app_commands.describe(role="Rôle à donner aux nouveaux membres")
    async def role_auto(self, interaction: discord.Interaction, role: discord.Role = None):
        set_welcome(interaction.guild_id, roleId=role.id if role else None)
        txt = f"✅ Les nouveaux membres recevront {role.mention}." if role else "🗑️ Rôle automatique désactivé."
        await interaction.response.send_message(txt, ephemeral=True)

    @app_commands.command(name="etat", description="Activer ou désactiver les messages de bienvenue")
    @app_commands.describe(actif="Activer ?")
    async def etat(self, interaction: discord.Interaction, actif: bool):
        set_welcome(interaction.guild_id, enabled=actif)
        await interaction.response.send_message("✅ Activé." if actif else "⏸️ Désactivé.", ephemeral=True)

    @app_commands.command(name="test", description="Prévisualiser le message de bienvenue (visible par toi seul)")
    async def test(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        w = get_welcome(get_guild(interaction.guild_id))
        embed, file = await build_welcome(interaction.user, w)
        await interaction.followup.send(content=f"👋 {interaction.user.mention}", embed=embed, file=file, ephemeral=True)

    @app_commands.command(name="voir", description="Voir la configuration de la bienvenue")
    async def voir(self, interaction: discord.Interaction):
        w = get_welcome(get_guild(interaction.guild_id))
        embed = discord.Embed(title="⚙️ Bienvenue", color=int(w["color"].lstrip("#"), 16))
        embed.add_field(name="État", value="✅ Activée" if w["enabled"] else "⏸️ Désactivée")
        embed.add_field(name="Salon", value=f"<#{w['channelId']}>" if w["channelId"] else "Non défini")
        embed.add_field(name="Rôle auto", value=f"<@&{w['roleId']}>" if w["roleId"] else "Aucun")
        embed.add_field(name="Couleur", value=w["color"])
        embed.add_field(name="Fond", value="Image perso" if w["hasBackground"] else "Dégradé")
        embed.add_field(name="Titre", value=w["title"][:200], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------- Bot ----------
class TicketBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True  # nécessaire pour détecter les arrivées (blacklist)
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Vues persistantes : les menus/boutons marchent encore après un redémarrage
        placeholder = [discord.SelectOption(label="placeholder", value="placeholder")]
        v = discord.ui.View(timeout=None)
        v.add_item(TicketSelect(placeholder))
        self.add_view(v)
        self.add_view(CloseView())
        # Vide les anciennes commandes globales (évite les doublons avec celles du serveur)
        await self.tree.sync()

    async def sync_guild(self, guild):
        # Commandes synchronisées directement sur le serveur : elles apparaissent tout de suite
        self.tree.clear_commands(guild=guild)
        self.tree.add_command(TicketConfig(), guild=guild)
        self.tree.add_command(ReponseConfig(), guild=guild)
        self.tree.add_command(reponse, guild=guild)
        self.tree.add_command(blacklist, guild=guild)
        self.tree.add_command(unblacklist, guild=guild)
        self.tree.add_command(blacklist_liste, guild=guild)
        self.tree.add_command(WelcomeConfig(), guild=guild)
        synced = await self.tree.sync(guild=guild)
        print(f"🔄 {guild.name} : {len(synced)} commandes -> {[c.name for c in synced]}")

    async def on_ready(self):
        print(f"✅ Connecté en tant que {self.user}")
        for guild in self.guilds:
            try:
                await self.sync_guild(guild)
            except Exception as e:
                print(f"❌ Synchronisation impossible sur {guild.name} : {e}")
            role = await get_blacklist_role(guild, create=False)
            if role:
                await hide_channels(guild, role)

    async def on_guild_channel_create(self, channel):
        # Tout nouveau salon est automatiquement caché au rôle blacklist
        role = await get_blacklist_role(channel.guild, create=False)
        if role:
            await hide_channels(channel.guild, role)

    async def on_member_join(self, member):
        # Blacklisté qui revient : le rôle est remis immédiatement
        if str(member.id) in get_guild(member.guild.id)["blacklist"]:
            try:
                role = await get_blacklist_role(member.guild)
                await member.add_roles(role, reason="Blacklist (retour sur le serveur)")
            except discord.HTTPException as e:
                print(f"❌ Impossible de remettre le rôle blacklist à {member}: {e}")
            return
        await send_welcome(member)

    async def on_member_update(self, before, after):
        # Si quelqu'un retire le rôle à un blacklisté, il est remis
        if str(after.id) not in get_guild(after.guild.id)["blacklist"]:
            return
        role = await get_blacklist_role(after.guild, create=False)
        if role and role in before.roles and role not in after.roles:
            try:
                await after.add_roles(role, reason="Blacklist (rôle retiré)")
            except discord.HTTPException:
                pass

    async def on_guild_join(self, guild):
        await self.sync_guild(guild)


bot = TicketBot()
bot.run(os.getenv("TOKEN"))
