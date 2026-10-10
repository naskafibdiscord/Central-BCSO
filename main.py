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


def default_config():
    return {
        "title": "🎫 Support",
        "description": "Choisissez un type de ticket dans le menu ci-dessous.",
        "types": [],
        "categoryId": None,
        "staffRoleId": None,
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


# ---------- Bot ----------
class TicketBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.tree.add_command(TicketConfig())
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
