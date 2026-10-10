import asyncio
import json
import os
import re
import time
import unicodedata

import discord
from discord import app_commands
from dotenv import load_dotenv

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


# ---------- Bot ----------
class TicketBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.tree.add_command(TicketConfig())
        self.tree.add_command(ReponseConfig())
        self.tree.add_command(reponse)
        # Vues persistantes : les menus/boutons marchent encore après un redémarrage
        placeholder = [discord.SelectOption(label="placeholder", value="placeholder")]
        v = discord.ui.View(timeout=None)
        v.add_item(TicketSelect(placeholder))
        self.add_view(v)
        self.add_view(CloseView())
        await self.tree.sync()

    async def on_ready(self):
        print(f"✅ Connecté en tant que {self.user}")


bot = TicketBot()
bot.run(os.getenv("TOKEN"))
