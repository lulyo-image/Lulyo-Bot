"""
web_server.py — Lulyo Web : site web du bot (aucun exécutable, aucun téléchargement).

Tout est servi par le bot lui-même : page d'accueil, connexion avec les
identifiants envoyés en message privé par le bot, page de configuration à la
première connexion (changement de mot de passe obligatoire), paramètres du
compte (mot de passe, liaison Discord / Google), tableau de bord dont le
contenu dépend des permissions réelles de l'utilisateur sur le serveur, et une
page « Télé » (IPTV) utilisable aussi comme activité Discord.

Branchement dans bot.py — UNE ligne, après la création du bot et de la base :

    import web_server; web_server.attach(bot, db, permission_level=permission_level)

Ce module ajoute aussi :
  * /compte            — (tout le monde) reçoit ou régénère ses identifiants en MP
  * /site              — lien du site
  * /autoreply         — mot déclencheur -> réponse automatique du bot
  * /radio chaine:<numéro ou url>  — radio en vocal (niveau DJ)
  * /tv                — liste des chaînes / lien de la télé / activité Discord

Variables d'environnement (toutes optionnelles) :
    WEB_ENABLED=1                 0 pour tout désactiver
    WEB_HOST=0.0.0.0              interface d'écoute
    WEB_PORT=8080                 port HTTP
    WEB_PUBLIC_URL=https://...    adresse publique (sinon http://IP:PORT)
    WEB_SECRET=...                clé de signature des cookies (générée sinon)
    DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET   liaison « Se connecter avec Discord »
    GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET     liaison « Se connecter avec Google »
    WEB_CHANNELS_FILE=channels.json  chaînes TV / radios personnalisables
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import sys
import time
import unicodedata
from datetime import datetime, timezone
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import aiohttp
import discord
from aiohttp import web
from discord import app_commands

try:  # bots compagnons (vocal / radio / musique) — facultatif
    import voice_bot
except Exception:  # pragma: no cover - le site doit marcher sans ce module
    voice_bot = None  # type: ignore[assignment]

log = logging.getLogger("lulyo.web")

BRAND = "Lulyo"
BASE_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
#  Réglages
# --------------------------------------------------------------------------- #
def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def env_bool(name: str, default: bool = True) -> bool:
    raw = env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "oui", "yes", "on"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp() -> str:
    return utcnow().isoformat()


# --------------------------------------------------------------------------- #
#  Chaînes TV et radios (modifiables dans channels.json)
# --------------------------------------------------------------------------- #
DEFAULT_RADIOS = [
    {"nom": "France Inter", "url": "https://icecast.radiofrance.fr/franceinter-midfi.mp3"},
    {"nom": "France Info", "url": "https://icecast.radiofrance.fr/franceinfo-midfi.mp3"},
    {"nom": "FIP", "url": "https://icecast.radiofrance.fr/fip-midfi.mp3"},
    {"nom": "France Musique", "url": "https://icecast.radiofrance.fr/francemusique-midfi.mp3"},
    {"nom": "France Culture", "url": "https://icecast.radiofrance.fr/franceculture-midfi.mp3"},
    {"nom": "Mouv'", "url": "https://icecast.radiofrance.fr/mouv-midfi.mp3"},
    {"nom": "NRJ", "url": "http://cdn.nrjaudio.fm/audio1/fr/30001/mp3_128.mp3"},
    {"nom": "Nostalgie", "url": "http://cdn.nrjaudio.fm/audio1/fr/30601/mp3_128.mp3"},
    {"nom": "Chérie FM", "url": "http://cdn.nrjaudio.fm/audio1/fr/30201/mp3_128.mp3"},
    {"nom": "Rire & Chansons", "url": "http://cdn.nrjaudio.fm/audio1/fr/30401/mp3_128.mp3"},
    {"nom": "Skyrock", "url": "http://icecast.skyrock.net/s/natio_mp3_128k"},
    {"nom": "RTL", "url": "http://streaming.radio.rtl.fr/rtl-1-44-128"},
    {"nom": "Fun Radio", "url": "http://streaming.radio.funradio.fr/fun-1-44-128"},
    {"nom": "RMC", "url": "http://audio.bfmtv.com/rmcradio_128.mp3"},
    {"nom": "Radio Nova", "url": "https://novazz.ice.infomaniak.ch/novazz-128.mp3"},
]

CHANNELS_VERSION = 2

DEFAULT_TV = [
    {"nom": "France 24", "logo": "🌍",
     "url": "https://live.france24.com/hls/live/2037179-b/F24_FR_HI_HLS/master_5000.m3u8"},
    {"nom": "BFM TV", "logo": "📰",
     "url": "https://ncdn-live-bfm.pfd.sfr.net/shls/LIVE$BFM_TV/index.m3u8?end=END&start=LIVE"},
    {"nom": "BFM Business", "logo": "💼",
     "url": "https://ncdn-live-bfm.pfd.sfr.net/shls/LIVE$BFM_BUSINESS/index.m3u8?end=END&start=LIVE"},
    {"nom": "BFM Tech & Co", "logo": "💻",
     "url": "https://ncdn-live-bfm.pfd.sfr.net/shls/LIVE$BFM_TECHANDCO/index.m3u8?end=END&start=LIVE"},
    {"nom": "BFM 2", "logo": "📡",
     "url": "https://d1ib1gsg71oarf.cloudfront.net/v1/master/3722c60a815c199d9c0ef36c5b73da68a62b09d1/cc-scp7wda722jph/BFM2_FR.m3u8"},
    {"nom": "Arte", "logo": "🎭",
     "url": "https://artesimulcast.akamaized.net/hls/live/2030993/artelive_fr/index.m3u8"},
    {"nom": "TV5 Monde Info", "logo": "🗞️",
     "url": "https://ott.tv5monde.com/Content/HLS/Live/channel(info)/variant.m3u8"},
    {"nom": "Euronews (français)", "logo": "🇪🇺",
     "url": "https://2f6c5bf4.wurl.com/master/f36d25e7e52f1ba8d7e56eb859c636563214f541/UmxheHhUVi1ldV9FdXJvbmV3c0ZyYW5jYWlzX0hMUw/playlist.m3u8"},
    {"nom": "20 Minutes TV", "logo": "🗨️",
     "url": "https://live-20minutestv.digiteka.com/1961167769/index.m3u8"},
    {"nom": "DBM TV", "logo": "🎬",
     "url": "https://dbmtv.vedge.infomaniak.com/livecast/dbmtv/playlist.m3u8"},
    {"nom": "ADN TV+ (animés)", "logo": "🌸",
     "url": "https://d3b73b34o7cvkq.cloudfront.net/v1/master/3722c60a815c199d9c0ef36c5b73da68a62b09d1/cc-gz2sgqzp076kf/adn.m3u8"},
    {"nom": "Trace Urban", "logo": "🎵",
     "url": "https://channels.trace.plus/Traceprod/URBAN_FR_hd/index.m3u8"},
    {"nom": "Africa 24", "logo": "🌍",
     "url": "https://africa24.vedge.infomaniak.com/livecast/ik:africa24/manifest.m3u8"},
    {"nom": "France 24 (secours)", "logo": "🌍",
     "url": "https://static.france24.com/live/F24_FR_LO_HLS/live_web.m3u8"},
]


def channels_path() -> Path:
    raw = env("WEB_CHANNELS_FILE", "channels.json")
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


def write_channels(data: dict) -> None:
    try:
        channels_path().write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    except OSError:
        pass


def load_channels() -> dict:
    """Lit channels.json. Si le fichier date d'une version antérieure, la liste TV
    livrée par défaut (chaînes françaises) remplace l'ancienne, les radios
    personnalisées sont conservées."""
    path = channels_path()
    fresh = {"version": CHANNELS_VERSION, "radios": DEFAULT_RADIOS, "tv": DEFAULT_TV}
    if not path.exists():
        write_channels(fresh)
        return fresh
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.warning("channels.json illisible, valeurs par défaut utilisées")
        return fresh
    if int(data.get("version", 1)) < CHANNELS_VERSION:
        data = {"version": CHANNELS_VERSION,
                "radios": data.get("radios") or DEFAULT_RADIOS,
                "tv": DEFAULT_TV}
        write_channels(data)
        log.info("channels.json mis à jour : nouvelles chaînes françaises")
    return {"version": CHANNELS_VERSION,
            "radios": data.get("radios") or DEFAULT_RADIOS,
            "tv": data.get("tv") or DEFAULT_TV}


# --------------------------------------------------------------------------- #
#  État global
# --------------------------------------------------------------------------- #
class State:
    entry_point: Optional[dict] = None
    bot: Any = None
    db: Any = None
    permission_level: Optional[Callable] = None
    runner: Optional[web.AppRunner] = None
    site: Optional[web.TCPSite] = None
    started = False
    public_ip = ""
    sessions: dict[str, dict] = {}
    oauth_states: dict[str, dict] = {}
    autoreplies: dict[int, list[dict]] = {}
    radio_now: dict[int, dict] = {}
    mic: dict[int, dict] = {}


STATE = State()

LEVEL_NAMES = {0: "Membre", 1: "DJ", 2: "Staff", 3: "Administrateur", 4: "Propriétaire"}


async def level_of(member: Any, guild: Any) -> int:
    """Niveau interne de l'utilisateur (délégué à bot.py si disponible)."""
    if STATE.permission_level is not None:
        try:
            return int(await STATE.permission_level(member, guild))
        except Exception:
            log.debug("permission_level a échoué", exc_info=True)
    # secours : déduction depuis les permissions Discord
    if not isinstance(member, discord.Member):
        return 0
    if guild is not None and member.id == getattr(guild, "owner_id", 0):
        return 4
    perms = member.guild_permissions
    if perms.administrator:
        return 3
    if perms.manage_guild or perms.ban_members or perms.kick_members or perms.manage_messages:
        return 2
    return 0


# --------------------------------------------------------------------------- #
#  Base de données (tables propres au site)
# --------------------------------------------------------------------------- #
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS web_accounts (
           user_id     INTEGER PRIMARY KEY,
           username    TEXT NOT NULL UNIQUE,
           salt        TEXT NOT NULL,
           password    TEXT NOT NULL,
           must_change INTEGER NOT NULL DEFAULT 1,
           google_sub  TEXT,
           google_mail TEXT,
           discord_ok  INTEGER NOT NULL DEFAULT 0,
           created_at  TEXT NOT NULL,
           last_login  TEXT
       )""",
    """CREATE TABLE IF NOT EXISTS web_autoreplies (
           id         INTEGER PRIMARY KEY AUTOINCREMENT,
           guild_id   INTEGER NOT NULL,
           trigger    TEXT NOT NULL,
           response   TEXT NOT NULL,
           partial    INTEGER NOT NULL DEFAULT 1,
           created_by INTEGER,
           created_at TEXT NOT NULL
       )""",
    "CREATE INDEX IF NOT EXISTS idx_autoreplies_guild ON web_autoreplies(guild_id)",
    """CREATE TABLE IF NOT EXISTS web_messages (
           message_id INTEGER PRIMARY KEY,
           guild_id   INTEGER NOT NULL,
           channel_id INTEGER NOT NULL,
           user_id    INTEGER NOT NULL,
           webhook_id INTEGER,
           as_bot     INTEGER NOT NULL DEFAULT 0,
           created_at TEXT NOT NULL
       )""",
    "CREATE INDEX IF NOT EXISTS idx_webmsg_chan ON web_messages(channel_id)",
    """CREATE TABLE IF NOT EXISTS web_config (
           key   TEXT PRIMARY KEY,
           value TEXT NOT NULL
       )""",
]


async def setup_tables() -> None:
    for query in SCHEMA:
        await STATE.db.execute(query)


async def config_get(key: str, default: str = "") -> str:
    row = await STATE.db.fetchone("SELECT value FROM web_config WHERE key = ?", (key,))
    if row is None:
        return default
    try:
        return row["value"]
    except (TypeError, KeyError, IndexError):
        return row[0]


async def config_set(key: str, value: str) -> None:
    await STATE.db.execute(
        "INSERT INTO web_config(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def row_dict(row: Any) -> Optional[dict]:
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Mots de passe et comptes
# --------------------------------------------------------------------------- #
def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 240_000).hex()


def check_password(password: str, salt: str, expected: str) -> bool:
    return hmac.compare_digest(hash_password(password, salt), expected)


ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_password(length: int = 14) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def clean_username(raw: str) -> str:
    text = unicodedata.normalize("NFKD", raw or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-zA-Z0-9._-]", "", text).strip("._-").lower()
    return text or "membre"


def password_problem(password: str) -> Optional[str]:
    """Règles de mot de passe (strictes, comme demandé)."""
    if len(password) < 10:
        return "10 caractères minimum."
    if not re.search(r"[a-z]", password):
        return "il faut au moins une minuscule."
    if not re.search(r"[A-Z]", password):
        return "il faut au moins une majuscule."
    if not re.search(r"[0-9]", password):
        return "il faut au moins un chiffre."
    if not re.search(r"[^A-Za-z0-9]", password):
        return "il faut au moins un caractère spécial."
    if password.lower() in {"motdepasse", "password1!", "azertyuiop1!"}:
        return "mot de passe trop courant."
    return None


async def unique_username(base: str, user_id: int) -> str:
    name = clean_username(base)
    taken = await STATE.db.fetchone(
        "SELECT user_id FROM web_accounts WHERE username = ? AND user_id != ?", (name, user_id))
    if taken is None:
        return name
    return f"{name}{str(user_id)[-4:]}"


async def get_account(user_id: int) -> Optional[dict]:
    return row_dict(await STATE.db.fetchone(
        "SELECT * FROM web_accounts WHERE user_id = ?", (user_id,)))


async def ensure_account(user: discord.abc.User, reset: bool = False) -> tuple[dict, Optional[str]]:
    """Crée le compte si besoin. Retourne (compte, mot de passe en clair ou None)."""
    account = await get_account(user.id)
    if account is not None and not reset:
        return account, None
    password = new_password()
    salt = secrets.token_hex(16)
    digest = hash_password(password, salt)
    username = await unique_username(getattr(user, "name", "membre"), user.id)
    if account is None:
        await STATE.db.execute(
            "INSERT INTO web_accounts(user_id, username, salt, password, must_change, "
            "discord_ok, created_at) VALUES(?, ?, ?, ?, 1, 1, ?)",
            (user.id, username, salt, digest, stamp()))
    else:
        await STATE.db.execute(
            "UPDATE web_accounts SET salt = ?, password = ?, must_change = 1 WHERE user_id = ?",
            (salt, digest, user.id))
    account = await get_account(user.id)
    return account or {}, password


async def send_credentials(user: discord.abc.User, username: str, password: str,
                           motif: str = "") -> bool:
    url = public_url()
    embed = discord.Embed(
        title=f"🔐 Tes identifiants {BRAND}",
        description=(f"Voici ton accès **personnel** au site {BRAND}.\n"
                     f"Aucun fichier à télécharger : tout se passe dans le navigateur.\n\n"
                     f"🌐 **Site** : {url}"),
        color=0x5865F2, timestamp=utcnow())
    embed.add_field(name="Identifiant", value=f"`{username}`", inline=True)
    embed.add_field(name="Mot de passe provisoire", value=f"||`{password}`||", inline=True)
    embed.add_field(
        name="À la première connexion",
        value=("Le site t'emmène sur une page de configuration : tu devras choisir "
               "un **nouveau mot de passe** (10 caractères minimum, majuscule, "
               "minuscule, chiffre et caractère spécial) et le saisir **deux fois**."),
        inline=False)
    embed.add_field(
        name="Tes accès",
        value=("Le site lit automatiquement tes rôles Discord : tu vois exactement "
               "ce à quoi tu as droit, ni plus ni moins."),
        inline=False)
    if motif:
        embed.set_footer(text=motif)
    try:
        await user.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


# --------------------------------------------------------------------------- #
#  Adresse publique
# --------------------------------------------------------------------------- #
def host() -> str:
    return env("WEB_HOST", "0.0.0.0")


def port() -> int:
    try:
        return int(env("WEB_PORT", "8080"))
    except ValueError:
        return 8080


def public_url() -> str:
    raw = env("WEB_PUBLIC_URL")
    if raw:
        return raw.rstrip("/")
    ip = STATE.public_ip or "127.0.0.1"
    return f"http://{ip}:{port()}"


async def detect_public_ip() -> None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.ipify.org", timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status == 200:
                    STATE.public_ip = (await resp.text()).strip()
    except Exception:
        STATE.public_ip = ""


# --------------------------------------------------------------------------- #
#  Sessions (cookie signé + mémoire)
# --------------------------------------------------------------------------- #
COOKIE = "lulyo_session"
SESSION_TTL = 7 * 24 * 3600


def secret_key() -> str:
    value = env("WEB_SECRET")
    if value:
        return value
    if not getattr(STATE, "_secret", None):
        STATE._secret = secrets.token_hex(32)  # type: ignore[attr-defined]
    return STATE._secret  # type: ignore[attr-defined]


def sign(token: str) -> str:
    mac = hmac.new(secret_key().encode(), token.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{token}.{mac}"


def unsign(raw: str) -> Optional[str]:
    if not raw or "." not in raw:
        return None
    token, _, mac = raw.rpartition(".")
    if hmac.compare_digest(sign(token).rpartition(".")[2], mac):
        return token
    return None


def new_session(user_id: int, username: str, level: int = 0) -> str:
    token = secrets.token_urlsafe(32)
    STATE.sessions[token] = {"user_id": user_id, "username": username,
                             "level": level,          # staff = 2 et plus
                             "created": time.time(), "seen": time.time()}
    return token


def session_of(request: web.Request) -> Optional[dict]:
    token = unsign(request.cookies.get(COOKIE, ""))
    if not token:
        return None
    data = STATE.sessions.get(token)
    if data is None:
        return None
    if time.time() - data["created"] > SESSION_TTL:
        STATE.sessions.pop(token, None)
        return None
    data["seen"] = time.time()
    data["token"] = token
    return data


def embed_origins() -> list[str]:
    """Sites autorisés à afficher le site dans une iframe (GitHub Pages…)."""
    raw = env("WEB_EMBED_ORIGINS")
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


def secure_site() -> bool:
    return public_url().startswith("https://")


def set_cookie(response: web.Response, token: str) -> web.Response:
    # dans une iframe d'un autre domaine, un cookie SameSite=Lax n'est pas renvoyé :
    # on passe en None+Secure dès qu'une intégration est déclarée (HTTPS obligatoire).
    if embed_origins() and secure_site():
        response.set_cookie(COOKIE, sign(token), max_age=SESSION_TTL, httponly=True,
                            samesite="None", secure=True, path="/")
    else:
        response.set_cookie(COOKIE, sign(token), max_age=SESSION_TTL, httponly=True,
                            samesite="Lax", path="/")
    return response


# --------------------------------------------------------------------------- #
#  Permissions vues par le site
# --------------------------------------------------------------------------- #
ACCESS_BY_LEVEL = {
    0: ["accueil", "tv", "radio", "profil"],
    1: ["accueil", "tv", "radio", "profil", "musique"],
    2: ["accueil", "tv", "radio", "profil", "musique", "tickets", "moderation", "membres"],
    3: ["accueil", "tv", "radio", "profil", "musique", "tickets", "moderation", "membres",
        "configuration", "journaux"],
    4: ["accueil", "tv", "radio", "profil", "musique", "tickets", "moderation", "membres",
        "configuration", "journaux", "administration"],
}

ACCESS_LABELS = {
    "accueil": ("🏠", "Accueil", "Vue d'ensemble du serveur"),
    "tv": ("📺", "Télé", "Regarder la télévision en direct"),
    "radio": ("📻", "Radio", "Écouter la radio et la lancer en vocal"),
    "profil": ("👤", "Mon profil", "XP, économie, badges, paramètres du compte"),
    "musique": ("🎵", "Musique / DJ", "File d'attente, radio en vocal, contrôles"),
    "tickets": ("🎫", "Tickets", "Suivre et traiter les tickets d'assistance"),
    "moderation": ("🛡️", "Modération", "Sanctions, avertissements, historique"),
    "membres": ("👥", "Membres", "Liste des membres et de leurs rôles"),
    "configuration": ("⚙️", "Configuration", "Réglages du serveur"),
    "journaux": ("📜", "Journaux", "Logs et statistiques détaillées"),
    "administration": ("🧰", "Administration", "Contrôle total du bot"),
}


async def user_guilds(user_id: int) -> list[dict]:
    out = []
    for guild in list(getattr(STATE.bot, "guilds", ())):
        member = guild.get_member(user_id)
        if member is None:
            continue
        level = await level_of(member, guild)
        out.append({
            "id": guild.id, "name": guild.name,
            "icon": guild.icon.url if guild.icon else "",
            "level": level, "level_name": LEVEL_NAMES.get(level, "Membre"),
            "access": ACCESS_BY_LEVEL.get(level, ACCESS_BY_LEVEL[0]),
            "members": guild.member_count or 0,
        })
    out.sort(key=lambda g: (-g["level"], g["name"].lower()))
    return out


# --------------------------------------------------------------------------- #
#  Gabarits HTML
# --------------------------------------------------------------------------- #
CSS = """
:root{--bg:#0c0c0e;--bg2:#111114;--panel:#151518;--panel2:#1b1b20;
 --line:rgba(255,255,255,.08);--line2:rgba(255,255,255,.15);
 --tx:#f2efe9;--mute:#a09c93;--dim:#6f6c65;
 --acc:#ff6a3d;--acc2:#ffb089;--accbg:rgba(255,106,61,.12);--accln:rgba(255,106,61,.42);
 --ok:#7fd9a8;--err:#ff8585;
 --serif:'Fraunces','Iowan Old Style','Palatino Linotype',Georgia,serif;
 --sans:'Inter','Segoe UI',system-ui,-apple-system,sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
html{color-scheme:dark;scroll-behavior:smooth}
body{font-family:var(--sans);background:var(--bg);color:var(--tx);min-height:100vh;
 line-height:1.6;font-size:16px;letter-spacing:-.005em;-webkit-font-smoothing:antialiased}
::selection{background:var(--acc);color:#1a0f0a}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:#2a2a30;border-radius:10px;border:2px solid var(--bg)}
a{color:var(--acc2);text-decoration:none;transition:color .15s}
a:hover{color:#fff}
:focus-visible{outline:2px solid var(--acc);outline-offset:3px;border-radius:6px}
.bg{position:fixed;inset:0;z-index:-1;background:
 radial-gradient(1000px 620px at 6% -14%,rgba(255,106,61,.14),transparent 60%),
 radial-gradient(760px 480px at 104% 2%,rgba(255,176,137,.06),transparent 60%),var(--bg)}
.bg::after{content:'';position:absolute;inset:0;opacity:.07;mix-blend-mode:overlay;
 background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2' stitchTiles='stitch'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>")}
@keyframes rise{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}
.rise{animation:rise .85s cubic-bezier(.2,.7,.2,1) both;animation-delay:var(--d,0s)}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;scroll-behavior:auto!important}}

header.nav{display:flex;align-items:center;gap:18px;padding:16px 32px;
 border-bottom:1px solid var(--line);backdrop-filter:blur(14px) saturate(1.2);
 background:rgba(12,12,14,.72);position:sticky;top:0;z-index:20;flex-wrap:wrap}
.brand{display:inline-flex;align-items:center;font-family:var(--serif);font-weight:600;
 font-size:1.4rem;letter-spacing:-.02em;color:var(--tx);transition:opacity .15s}
.brand:hover{opacity:.82;color:var(--tx)}
.brand span::after{content:'.';color:var(--acc)}
.brand img{box-shadow:0 0 0 1px var(--line2)}
.nav nav{display:flex;gap:4px;flex-wrap:wrap;margin-left:auto;align-items:center}
.nav nav a{padding:8px 14px;border-radius:999px;color:var(--mute);font-size:.92rem;font-weight:500;
 transition:color .15s,background .15s}
.nav nav a:hover{color:var(--tx);background:rgba(255,255,255,.05)}
.nav nav a.cta{border:1px solid var(--line2);color:var(--tx);margin-left:8px;padding:8px 18px}
.nav nav a.cta:hover{background:var(--acc);border-color:var(--acc);color:#1a0f0a}

.wrap{max-width:1120px;margin:0 auto;padding:40px 28px 90px}

.hero2{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,.85fr);gap:56px;
 align-items:center;padding:64px 0 40px}
@media(max-width:900px){.hero2{grid-template-columns:1fr;gap:44px;padding-top:36px}}
.eyebrow{font-size:.78rem;letter-spacing:.16em;text-transform:uppercase;color:var(--acc);
 font-weight:600;margin-bottom:22px;display:flex;align-items:center;gap:12px}
.eyebrow::before{content:'';width:28px;height:1px;background:var(--acc)}
.hero2 h1{font-family:var(--serif);font-weight:500;font-size:clamp(2.5rem,5.6vw,4.4rem);
 line-height:1.04;letter-spacing:-.035em;margin-bottom:24px}
.hero2 h1 em{font-style:italic;color:var(--acc2);font-weight:400}
.lead{color:var(--mute);font-size:1.12rem;max-width:540px;margin-bottom:32px}
.cta-row{display:flex;gap:12px;flex-wrap:wrap}
.stats{display:flex;gap:44px;margin-top:48px;padding-top:26px;border-top:1px solid var(--line)}
.stat b{display:block;font-family:var(--serif);font-weight:500;font-size:2.5rem;line-height:1;
 letter-spacing:-.03em}
.stat small{display:block;margin-top:8px;font-size:.74rem;letter-spacing:.14em;
 text-transform:uppercase;color:var(--dim)}

.mock{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line2);
 border-radius:20px;box-shadow:0 40px 90px -40px rgba(0,0,0,.9),0 0 0 1px rgba(255,255,255,.02) inset;
 overflow:hidden;transform:rotate(1.2deg);transition:transform .5s cubic-bezier(.2,.7,.2,1)}
.mock:hover{transform:rotate(0)}
.mock-bar{display:flex;align-items:center;gap:7px;padding:14px 18px;border-bottom:1px solid var(--line);
 font-size:.76rem;color:var(--dim)}
.mock-bar i{width:10px;height:10px;border-radius:50%;background:#2c2c33;display:block}
.mock-bar span{margin-left:10px}
.mock-body{padding:22px 20px 24px;display:flex;flex-direction:column;gap:18px}
.mk-cmd{display:inline-block;font-family:ui-monospace,'SF Mono','Cascadia Code',Consolas,monospace;
 font-size:.88rem;background:var(--accbg);border:1px solid var(--accln);color:var(--acc2);
 padding:5px 11px;border-radius:8px}
.mk-bot{display:grid;grid-template-columns:38px 1fr;gap:6px 12px}
.mk-av{width:38px;height:38px;border-radius:50%;background:var(--accbg);border:1px solid var(--accln);
 display:grid;place-items:center;font-family:var(--serif);font-weight:600;color:var(--acc2);overflow:hidden}
.mk-av img{width:100%;height:100%;object-fit:cover;border-radius:0!important}
.mk-head{font-weight:600;font-size:.94rem;display:flex;align-items:center;gap:8px}
.mk-badge{font-size:.6rem;letter-spacing:.06em;background:#5865f2;color:#fff;padding:2px 6px;border-radius:4px}
.mk-card{grid-column:2;border-left:3px solid var(--acc);background:rgba(255,255,255,.035);
 border-radius:4px 10px 10px 4px;padding:14px 16px;font-size:.88rem}
.mk-card strong{display:block;margin-bottom:10px;font-weight:600}
.mk-card dt{font-size:.68rem;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);margin-top:10px}
.mk-card dd{font-family:ui-monospace,Consolas,monospace;color:var(--tx);margin-top:2px}

.feats{border-bottom:1px solid var(--line)}
.feat{display:grid;grid-template-columns:70px minmax(0,.9fr) minmax(0,1.3fr);gap:24px;
 padding:28px 6px;border-top:1px solid var(--line);align-items:baseline;transition:background .25s,padding .25s}
.feat:hover{background:linear-gradient(90deg,var(--accbg),transparent 70%);padding-left:18px}
.feat .n{font-family:var(--serif);font-style:italic;color:var(--acc);font-size:1.05rem}
.feat h3{font-family:var(--serif);font-weight:500;font-size:1.55rem;letter-spacing:-.02em;line-height:1.2}
.feat p{color:var(--mute)}
@media(max-width:760px){.feat{grid-template-columns:44px 1fr;gap:6px 14px}.feat p{grid-column:2}}

.steps{display:grid;gap:0;grid-template-columns:repeat(3,minmax(0,1fr));margin-top:6px}
@media(max-width:800px){.steps{grid-template-columns:1fr}}
.step{padding:6px 28px 6px 0;margin-right:28px;border-right:1px solid var(--line)}
.step:last-child{border:0;margin:0}
@media(max-width:800px){.step{border:0;margin:0 0 26px;padding:0}}
.step b{font-family:var(--serif);font-weight:400;font-size:3.1rem;line-height:1;color:var(--acc);
 display:block;margin-bottom:12px;letter-spacing:-.04em}
.step h3{font-size:1.05rem;margin-bottom:6px}
.step p{color:var(--mute);font-size:.94rem}
.step code{font-family:ui-monospace,Consolas,monospace;font-size:.88em;background:var(--accbg);
 border:1px solid var(--accln);color:var(--acc2);padding:1px 7px;border-radius:6px}

.btn{display:inline-flex;align-items:center;justify-content:center;gap:9px;padding:13px 24px;
 border-radius:999px;font-weight:600;border:1px solid transparent;cursor:pointer;font-size:.96rem;
 font-family:inherit;background:var(--acc);color:#1a0f0a;
 transition:transform .18s,background .2s,box-shadow .25s,border-color .2s}
.btn:hover{background:#ff7f57;color:#1a0f0a;transform:translateY(-1px);
 box-shadow:0 12px 32px -10px rgba(255,106,61,.65)}
.btn:active{transform:translateY(0)}
.btn.ghost{background:transparent;border-color:var(--line2);color:var(--tx)}
.btn.ghost:hover{background:rgba(255,255,255,.06);border-color:rgba(255,255,255,.3);color:var(--tx);box-shadow:none}
.btn.google{background:#f2efe9;color:#17171b}
.btn.google:hover{background:#fff;color:#17171b;box-shadow:0 10px 28px -12px rgba(255,255,255,.5)}
.btn.discord{background:#5865f2;color:#fff}
.btn.discord:hover{background:#6b77f5;color:#fff;box-shadow:0 12px 30px -10px rgba(88,101,242,.7)}
.btn.wide{display:flex;width:100%}

.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(265px,1fr))}
.card{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:24px;
 transition:border-color .25s,transform .25s,background .25s}
.grid>.card:hover{border-color:var(--line2);transform:translateY(-2px);background:var(--panel2)}
.card h3{font-family:var(--serif);font-weight:500;font-size:1.25rem;letter-spacing:-.015em;margin-bottom:8px}
.card p{color:var(--mute);font-size:.94rem}
.card .ico{display:grid;place-items:center;width:46px;height:46px;font-size:1.35rem;margin-bottom:16px;
 border-radius:13px;background:var(--accbg);border:1px solid var(--accln);filter:grayscale(1) brightness(1.6)}

.panel{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line2);
 border-radius:24px;padding:36px;max-width:450px;margin:48px auto;
 box-shadow:0 40px 100px -50px rgba(0,0,0,.95)}
.panel h2{font-family:var(--serif);font-weight:500;font-size:1.9rem;letter-spacing:-.025em;margin-bottom:6px}
.panel .sub{color:var(--mute);font-size:.93rem;margin-bottom:24px}
label{display:block;font-size:.78rem;color:var(--mute);margin:16px 0 7px;font-weight:600;
 letter-spacing:.06em;text-transform:uppercase}
select{appearance:none;-webkit-appearance:none;cursor:pointer;
 background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='8'><path d='M1 1l5 5 5-5' fill='none' stroke='%23a09c93' stroke-width='2'/></svg>");
 background-repeat:no-repeat;background-position:right 14px center;padding-right:38px!important}
select option{background:var(--panel);color:var(--tx);padding:8px}
select option:checked{background:#2a2a30}
input,select,textarea{width:100%;padding:13px 15px;border-radius:12px;background:var(--bg2);
 border:1px solid var(--line2);color:var(--tx);font-size:.97rem;font-family:inherit;
 transition:border-color .2s,box-shadow .2s}
input::placeholder,textarea::placeholder{color:var(--dim)}
input[type=checkbox],input[type=radio]{accent-color:var(--acc)}
textarea{resize:vertical;min-height:74px;line-height:1.55}
input:hover,select:hover,textarea:hover{border-color:rgba(255,255,255,.26)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--acc);
 box-shadow:0 0 0 4px var(--accbg)}
.msg{padding:12px 16px;border-radius:12px;font-size:.9rem;margin-bottom:16px}
.msg.err{background:rgba(255,133,133,.09);border:1px solid rgba(255,133,133,.38);color:#ffc2c2}
.msg.ok{background:rgba(127,217,168,.09);border:1px solid rgba(127,217,168,.35);color:#b6f0d1}
.msg.info{background:var(--accbg);border:1px solid var(--accln);color:#ffd2bd}
.sep{display:flex;align-items:center;gap:14px;margin:22px 0;color:var(--dim);font-size:.8rem;
 text-transform:uppercase;letter-spacing:.14em}
.sep::before,.sep::after{content:'';flex:1;height:1px;background:var(--line)}
.tag{display:inline-block;padding:4px 12px;border-radius:999px;font-size:.74rem;font-weight:500;
 background:var(--accbg);border:1px solid var(--accln);color:var(--acc2)}
.tag.gray{background:rgba(255,255,255,.05);border-color:var(--line2);color:var(--mute)}
h2.section{font-family:var(--serif);font-weight:500;font-size:2rem;letter-spacing:-.03em;
 line-height:1.15;margin:64px 0 24px}
ul.perm{list-style:none;display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
ul.perm li{font-size:.8rem;padding:5px 12px;border-radius:999px;background:rgba(255,255,255,.04);
 border:1px solid var(--line);color:var(--mute)}
.rules{font-size:.85rem;color:var(--mute);margin-top:12px;padding-left:18px}
.rules li{margin-top:3px}
.tvwrap{display:grid;gap:20px;grid-template-columns:minmax(0,1fr) 300px}
@media(max-width:860px){.tvwrap{grid-template-columns:1fr}}
video{width:100%;border-radius:18px;background:#000;aspect-ratio:16/9;border:1px solid var(--line)}
.chan{display:flex;align-items:center;gap:12px;padding:11px 13px;border-radius:12px;cursor:pointer;
 border:1px solid transparent;transition:background .15s,border-color .15s}
.chan:hover{background:rgba(255,255,255,.05)}
.chan.on{background:var(--accbg);border-color:var(--accln)}
.chan b{font-size:.93rem;font-weight:600}
.chan small{color:var(--dim);display:block;font-size:.76rem}
.list{max-height:520px;overflow:auto}
footer{border-top:1px solid var(--line);color:var(--dim);font-size:.84rem;padding:34px 32px;
 text-align:center}
footer a{color:var(--mute)}footer a:hover{color:var(--tx)}
.foot{max-width:1064px;margin:0 auto;display:flex;justify-content:space-between;gap:18px;
 flex-wrap:wrap;text-align:left}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th,td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:600;font-size:.72rem;text-transform:uppercase;letter-spacing:.12em}
tr:hover td{background:rgba(255,255,255,.02)}
"""


def logo_path() -> Path:
    """Logo du site : LOGO_FILE, sinon logo.png à côté de bot.py."""
    custom = env("WEB_LOGO_FILE")
    if custom:
        path = Path(custom).expanduser()
        if path.is_file():
            return path
    return Path(__file__).resolve().parent / "logo.png"


async def serve_logo(request: web.Request) -> web.StreamResponse:
    path = logo_path()
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


def logo_tag(size: int = 34, extra: str = "") -> str:
    """Balise <img> du logo, ou l'ancienne étoile si le fichier est absent."""
    if not logo_path().is_file():
        return "✦"
    return (f'<img src="/logo.png" alt="" width="{size}" height="{size}" '
            f'style="border-radius:9px;vertical-align:middle;{extra}">')


POLISH_CSS = """
/* ===================================================================== *
 *  Finition visuelle — matières, rythme typographique, micro-animations
 * ===================================================================== */
:root{
  --r-sm:10px; --r-md:14px; --r-lg:20px; --r-xl:28px;
  --shadow-s:0 1px 2px rgba(0,0,0,.4);
  --shadow-m:0 10px 30px -12px rgba(0,0,0,.65);
  --shadow-l:0 30px 70px -24px rgba(0,0,0,.8);
  --glow:0 0 0 1px rgba(255,255,255,.05), 0 0 40px -12px var(--accln);
  --ease:cubic-bezier(.22,1,.36,1);
}

/* --- fond : dégradés doux + grain, au lieu d'un aplat ---------------- */
.bg{position:fixed;inset:0;z-index:-2;
 background:
  radial-gradient(1200px 700px at 8% -12%, color-mix(in oklab, var(--acc) 22%, transparent), transparent 62%),
  radial-gradient(900px 600px at 96% 4%, color-mix(in oklab, var(--acc2) 16%, transparent), transparent 58%),
  radial-gradient(1000px 800px at 50% 115%, color-mix(in oklab, var(--acc) 12%, transparent), transparent 60%),
  var(--bg)}
.bg::after{content:'';position:absolute;inset:0;opacity:.5;pointer-events:none;
 background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='160' height='160' filter='url(%23n)' opacity='.035'/%3E%3C/svg%3E")}

/* --- typographie ------------------------------------------------------ */
body{-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
 letter-spacing:-.011em}
h1,h2,h3{text-wrap:balance}
p,li{text-wrap:pretty}
.section{position:relative;padding-left:18px}
.section::before{content:'';position:absolute;left:0;top:.18em;bottom:.18em;width:3px;
 border-radius:3px;background:linear-gradient(180deg,var(--acc),transparent)}
::selection{background:color-mix(in oklab, var(--acc) 45%, transparent);color:#fff}

/* --- barre de navigation --------------------------------------------- */
.nav{background:color-mix(in oklab, var(--bg) 72%, transparent);
 backdrop-filter:blur(16px) saturate(1.4);-webkit-backdrop-filter:blur(16px) saturate(1.4);
 border-bottom:1px solid var(--line)}
.nav nav a{position:relative;transition:color .2s var(--ease)}
.nav nav a::after{content:'';position:absolute;left:14px;right:14px;bottom:4px;height:2px;
 border-radius:2px;background:var(--acc);transform:scaleX(0);opacity:0;
 transition:transform .3s var(--ease),opacity .2s}
.nav nav a:hover::after{transform:scaleX(1);opacity:1}
.nav nav a:hover{text-decoration:none;color:var(--tx)}
.nav nav a.cta::after{display:none}

/* --- cartes : bord lumineux et relief --------------------------------- */
.card,.panel,.tile,.feat,.step{position:relative;isolation:isolate;
 transition:transform .35s var(--ease),border-color .3s,box-shadow .35s}
.card::before,.panel::before{content:'';position:absolute;inset:0;border-radius:inherit;
 padding:1px;pointer-events:none;z-index:-1;opacity:0;transition:opacity .35s;
 background:linear-gradient(140deg,color-mix(in oklab,var(--acc) 55%,transparent),
  transparent 42%,transparent 60%,color-mix(in oklab,var(--acc2) 35%,transparent));
 -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);
 -webkit-mask-composite:xor;mask-composite:exclude}
.card:hover::before,.panel:hover::before{opacity:1}
.card:hover{transform:translateY(-3px);box-shadow:var(--shadow-m)}
.grid>.card:hover,.feat:hover,.tile:hover{transform:translateY(-4px)}

/* --- boutons : relief, reflet au survol ------------------------------- */
.btn{position:relative;overflow:hidden;border-radius:var(--r-md);
 box-shadow:var(--shadow-s),inset 0 1px 0 rgba(255,255,255,.18);
 transition:transform .18s var(--ease),box-shadow .25s,filter .2s}
.btn::after{content:'';position:absolute;inset:0;transform:translateX(-120%);
 background:linear-gradient(100deg,transparent,rgba(255,255,255,.22),transparent);
 transition:transform .7s var(--ease)}
.btn:hover::after{transform:translateX(120%)}
.btn:hover{transform:translateY(-2px);box-shadow:var(--shadow-m),
 0 0 0 1px color-mix(in oklab,var(--acc) 40%,transparent)}
.btn:active{transform:translateY(0) scale(.985)}
.btn.ghost{box-shadow:none;backdrop-filter:blur(6px)}
.btn.ghost:hover{border-color:var(--accln);background:var(--accbg)}

/* --- champs ------------------------------------------------------------ */
input,select,textarea{transition:border-color .2s,box-shadow .25s,background .2s;
 box-shadow:inset 0 1px 2px rgba(0,0,0,.35)}
input:hover,select:hover,textarea:hover{border-color:var(--line2)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accln);
 box-shadow:0 0 0 3px color-mix(in oklab,var(--acc) 22%,transparent),
 inset 0 1px 2px rgba(0,0,0,.35)}
input::placeholder,textarea::placeholder{color:color-mix(in oklab,var(--mute) 70%,transparent)}
label{transition:color .2s}
form:focus-within label{color:color-mix(in oklab,var(--acc2) 70%,var(--mute))}

/* --- tableaux ---------------------------------------------------------- */
table{border-collapse:separate;border-spacing:0}
tbody tr,table tr{transition:background .2s}
table tr:hover{background:color-mix(in oklab,var(--acc) 6%,transparent)}
th{position:sticky;top:0;background:color-mix(in oklab,var(--panel) 92%,transparent);
 backdrop-filter:blur(8px);z-index:1}

/* --- apparition au défilement ----------------------------------------- */
.reveal{opacity:0;transform:translateY(22px) scale(.985);
 transition:opacity .7s var(--ease),transform .7s var(--ease)}
.reveal.seen{opacity:1;transform:none}

/* --- barre de défilement ----------------------------------------------- */
*{scrollbar-width:thin;scrollbar-color:color-mix(in oklab,var(--acc) 45%,transparent) transparent}
*::-webkit-scrollbar{width:11px;height:11px}
*::-webkit-scrollbar-track{background:transparent}
*::-webkit-scrollbar-thumb{border-radius:99px;border:3px solid transparent;
 background-clip:content-box;background-color:color-mix(in oklab,var(--acc) 38%,transparent)}
*::-webkit-scrollbar-thumb:hover{background-color:var(--acc)}

/* --- messages ---------------------------------------------------------- */
.msg{position:relative;padding-left:44px;border-radius:var(--r-md);
 animation:msgIn .5s var(--ease) both}
.msg::before{position:absolute;left:15px;top:50%;transform:translateY(-50%);font-size:1.05rem}
.msg.ok::before{content:'✓'}
.msg.err::before{content:'!'}
.msg.info::before{content:'i';font-style:italic;font-family:var(--serif)}
@keyframes msgIn{from{opacity:0;transform:translateY(-8px)}}

/* --- étiquettes --------------------------------------------------------- */
.tag{backdrop-filter:blur(6px);transition:transform .2s,border-color .2s}
.tag:hover{transform:translateY(-1px);border-color:var(--accln)}

/* --- page partenaires --------------------------------------------------- */
.collab-hero{text-align:center;padding:56px 0 34px;max-width:720px;margin:0 auto}
.collab-hero h1{font-family:var(--serif);font-weight:500;letter-spacing:-.03em;
 font-size:clamp(2.1rem,4.6vw,3.4rem);line-height:1.08;margin:12px 0}
.collab-hero .lead{color:var(--mute);font-size:1.03rem}
.collab-hero .eyebrow{justify-content:center;display:inline-flex}
.collab-grid{display:grid;gap:22px;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));
 align-items:start}
.collab-card{background:linear-gradient(180deg,var(--panel2),var(--panel));
 border:1px solid var(--line);border-radius:var(--r-lg);overflow:hidden;
 transition:transform .4s var(--ease),border-color .3s,box-shadow .4s}
.collab-card:hover{transform:translateY(-6px);border-color:var(--accln);
 box-shadow:var(--shadow-l)}
.collab-head{padding:20px 22px 14px}
.collab-head h3{font-family:var(--serif);font-weight:500;font-size:1.35rem;
 letter-spacing:-.02em}
.collab-head p{color:var(--mute);font-size:.9rem;margin-top:6px}
.collab-widget{padding:0 14px}
.collab-widget iframe{display:block;border-radius:var(--r-md);background:#1e1f22}
.collab-foot{padding:16px 22px 20px}
.collab-foot .btn{width:100%;justify-content:center}

/* --- confort ------------------------------------------------------------ */
html{scroll-behavior:smooth}
img{-webkit-user-drag:none}
@media (prefers-reduced-motion: reduce){
  .reveal{opacity:1;transform:none;transition:none}
  .btn::after{display:none}
  html{scroll-behavior:auto}
}
"""

POLISH_JS = """
(function(){
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  /* apparition progressive des blocs au défilement */
  const cibles = document.querySelectorAll(
    '.wrap .card, .wrap .feat, .wrap .step, .wrap .tile, .wrap .section, .reveal');
  const vu = new IntersectionObserver((entrees) => {
    entrees.forEach((e, i) => {
      if (!e.isIntersecting) return;
      setTimeout(() => e.target.classList.add('seen'), Math.min(i * 45, 220));
      vu.unobserve(e.target);
    });
  }, {rootMargin: '0px 0px -8% 0px', threshold: .08});
  cibles.forEach(el => { el.classList.add('reveal'); vu.observe(el); });

  /* la page d'accueil garde ses propres animations d'entrée */
  document.querySelectorAll('.hero2 .reveal, .rise').forEach(el => el.classList.add('seen'));

  /* léger relief qui suit la souris sur les cartes */
  document.querySelectorAll('.card, .collab-card').forEach(carte => {
    carte.addEventListener('pointermove', e => {
      const r = carte.getBoundingClientRect();
      carte.style.setProperty('--mx', ((e.clientX - r.left) / r.width * 100) + '%');
      carte.style.setProperty('--my', ((e.clientY - r.top) / r.height * 100) + '%');
    });
  });
})();
"""


ULTRA_CSS = """
/* ===================================================================== *
 *  Direction « presse / programme télé » : aplats, filets, ombres dures
 *  Aucun dégradé, aucun flou, aucune lueur.
 * ===================================================================== */
:root{
  --bg:#0f0e0c;--bg2:#141310;--panel:#171612;--panel2:#1d1b16;
  --line:rgba(241,236,226,.13);--line2:rgba(241,236,226,.28);
  --tx:#f1ece2;--mute:#aca698;--dim:#7a7468;
  --acc:#ff4d1c;--acc2:#ff8a5c;--accbg:rgba(255,77,28,.12);--accln:rgba(255,77,28,.6);
  --serif:'Bricolage Grotesque','Arial Narrow',system-ui,sans-serif;
  --sans:'Hanken Grotesk','Segoe UI',system-ui,sans-serif;
  --mono:'DM Mono',ui-monospace,Consolas,monospace;
  --r-sm:3px;--r-md:4px;--r-lg:6px;--r-xl:8px;
}
body{background:var(--bg);font-family:var(--sans);letter-spacing:0}
.bg{background:var(--bg)!important}
.bg::after{display:none}
.card::before,.panel::before,.btn::after{display:none!important}
::selection{background:var(--acc);color:#0f0e0c}

/* --- navigation : barre pleine largeur, filet dessous ------------------ */
header.nav{position:sticky;top:0;margin:0;width:auto;max-width:none;padding:14px 32px;
 border:0;border-bottom:1.5px solid var(--line2);border-radius:0;background:var(--bg);
 backdrop-filter:none;-webkit-backdrop-filter:none;box-shadow:none}
.brand{font-family:var(--serif);font-weight:800;font-size:1.7rem;letter-spacing:-.05em}
.brand img{border-radius:4px;box-shadow:none}
.nav nav a{font-family:var(--mono);font-size:.74rem;letter-spacing:.08em;text-transform:uppercase;
 border-radius:3px;padding:9px 12px;color:var(--mute)}
.nav nav a::after{left:12px;right:12px;bottom:3px;height:1.5px;border-radius:0}
.nav nav a:hover{background:none;color:var(--tx)}
.nav nav a.on{color:var(--acc);background:none}
.nav nav a.on::after{transform:scaleX(1);opacity:1}
.nav nav a.cta{background:var(--acc);color:#0f0e0c;border:0;margin-left:10px;padding:9px 16px;font-weight:500}
.nav nav a.cta:hover{background:var(--tx);color:#0f0e0c}
.burger{display:none;margin-left:auto;width:42px;height:42px;border-radius:3px;cursor:pointer;
 border:1.5px solid var(--line2);background:none;color:var(--tx);place-items:center}
@media(max-width:980px){
 header.nav{padding:12px 18px}
 .burger{display:grid}
 .nav nav{display:none;width:100%;flex-direction:column;align-items:stretch;gap:0;margin-top:10px;
  border-top:1.5px solid var(--line2)}
 .nav.open nav{display:flex}
 .nav nav a{padding:14px 4px;border-bottom:1px solid var(--line)}
 .nav nav a.cta{margin:12px 0 0;text-align:center}
}

/* --- titres de section : gros filet au-dessus -------------------------- */
h2.section{font-family:var(--serif);font-weight:800;font-size:clamp(2rem,4.6vw,3.5rem);
 letter-spacing:-.045em;line-height:1;margin:110px 0 30px;padding:18px 0 0;border-top:2px solid var(--tx)}
h2.section::before{display:none}
.wrap h2.section:first-child{margin-top:34px}

/* --- boutons : aplat + ombre décalée ----------------------------------- */
.btn{background:var(--acc);color:#0f0e0c;border:1.5px solid var(--tx);border-radius:4px;
 padding:13px 24px;font-weight:600;font-family:var(--sans);box-shadow:4px 4px 0 var(--tx);
 transition:transform .12s,box-shadow .12s,background .12s,color .12s}
.btn:hover{background:var(--acc);color:#0f0e0c;transform:translate(-2px,-2px);box-shadow:6px 6px 0 var(--tx)}
.btn:active{transform:translate(3px,3px);box-shadow:1px 1px 0 var(--tx)}
.btn.ghost{background:transparent;color:var(--tx);border-color:var(--line2);box-shadow:none;backdrop-filter:none}
.btn.ghost:hover{background:var(--tx);color:#0f0e0c;border-color:var(--tx);transform:none;box-shadow:none}
.btn.google{background:#f1ece2;color:#0f0e0c}
.btn.discord{background:#5865f2;color:#fff;border-color:#f1ece2}
.btn.discord:hover{background:#5865f2;color:#fff}

/* --- cartes, panneaux, tuiles ------------------------------------------ */
.card,.tile,.collab-card{background:var(--panel);border:1.5px solid var(--line2);border-radius:6px;
 backdrop-filter:none;box-shadow:none;transition:transform .14s,box-shadow .14s,border-color .14s}
.card:hover,.grid>.card:hover,.tile:hover,.collab-card:hover{transform:translate(-3px,-3px);
 box-shadow:6px 6px 0 var(--acc);border-color:var(--tx);background:var(--panel)}
.card h3{font-family:var(--serif);font-weight:700;font-size:1.35rem;letter-spacing:-.03em}
.card .ico{filter:none;border-radius:4px;background:var(--acc);border:0;color:#0f0e0c;box-shadow:none}
.panel{background:var(--panel);border:1.5px solid var(--line2);border-radius:6px;
 box-shadow:8px 8px 0 var(--line2);backdrop-filter:none}
.panel h2{font-family:var(--serif);font-weight:800;letter-spacing:-.04em;font-size:2.1rem}
.tag{font-family:var(--mono);font-size:.68rem;letter-spacing:.08em;text-transform:uppercase;
 border-radius:3px;background:var(--accbg);border:1px solid var(--accln);color:var(--acc2);
 padding:4px 9px;backdrop-filter:none}
.tag:hover{transform:none}
.tag.gray{background:none;border-color:var(--line2);color:var(--mute)}
ul.perm li{border-radius:3px;font-family:var(--mono);font-size:.7rem;background:none;
 border:1px solid var(--line2)}
label{font-family:var(--mono);font-size:.7rem;letter-spacing:.1em}
form:focus-within label{color:var(--acc2)}

/* --- champs ------------------------------------------------------------ */
input,select,textarea{background:var(--bg);border:1.5px solid var(--line2);border-radius:4px;
 padding:13px 15px;box-shadow:none}
input:hover,select:hover,textarea:hover{border-color:var(--tx)}
input:focus,select:focus,textarea:focus{border-color:var(--acc);box-shadow:3px 3px 0 var(--acc)}
select option{background:#171612;color:var(--tx)}
.msg{border-radius:4px;border-width:1.5px}
.msg.ok::before,.msg.err::before,.msg.info::before{font-family:var(--mono)}
.sep{font-family:var(--mono);font-size:.7rem}
th{font-family:var(--mono);font-size:.68rem;background:var(--bg)!important;backdrop-filter:none!important;
 border-bottom:1.5px solid var(--line2)}
table tr:hover{background:rgba(255,77,28,.06)}
video{border-radius:4px;border:1.5px solid var(--line2)}
.chan{border-radius:4px}
.chan.on{background:var(--acc);border-color:var(--acc);color:#0f0e0c}
.chan.on small{color:rgba(15,14,12,.7)}

/* --- accueil : en-tête ---------------------------------------------------- */
.hero2{display:block;padding:34px 0 0}
.meta-line{display:flex;flex-wrap:wrap;gap:10px 28px;align-items:center;padding:12px 0;
 border-top:1.5px solid var(--tx);border-bottom:1px solid var(--line);margin-bottom:34px;
 font-family:var(--mono);font-size:.74rem;letter-spacing:.1em;text-transform:uppercase;color:var(--mute)}
.meta-line .live{margin-left:auto;display:inline-flex;align-items:center;gap:9px;color:var(--tx)}
.meta-line .live i{width:9px;height:9px;border-radius:50%;background:var(--acc);display:block;
 animation:blink 1.4s steps(2,jump-none) infinite}
@keyframes blink{50%{opacity:.15}}
.hero2 h1{font-family:var(--serif);font-weight:800;font-size:clamp(3rem,9.4vw,8.6rem);
 line-height:.9;letter-spacing:-.06em;margin:0 0 44px;max-width:15ch}
.hero2 h1 em{font-style:normal;color:var(--acc);font-weight:800;background:none;animation:none;
 -webkit-text-fill-color:currentColor}
.hero-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,.9fr);gap:56px;align-items:start}
@media(max-width:900px){.hero-grid{grid-template-columns:1fr;gap:44px}}
.lead{font-size:1.2rem;line-height:1.6;color:var(--tx);opacity:.82;max-width:34em}
.stats{gap:0;border:0;border-top:1.5px solid var(--tx);margin-top:44px;padding:0;background:none;
 width:auto;border-radius:0}
.stat{padding:20px 44px 0 0}
.stat+.stat{border:0;padding-left:0}
.stat b{font-family:var(--serif);font-weight:800;font-size:3.6rem;letter-spacing:-.05em;
 color:var(--tx);background:none;-webkit-text-fill-color:currentColor}
.stat small{font-family:var(--mono);font-size:.68rem;color:var(--mute)}
.mock{background:var(--panel);border:1.5px solid var(--tx);border-radius:6px;
 box-shadow:10px 10px 0 var(--acc);transform:rotate(-1.4deg);backdrop-filter:none;
 transition:transform .2s}
.mock:hover{transform:rotate(0)}
.mock-bar{border-bottom:1.5px solid var(--line2);font-family:var(--mono);font-size:.7rem;
 letter-spacing:.06em;text-transform:uppercase}
.mock-bar i{background:none;border:1.5px solid var(--mute);width:10px;height:10px}
.mk-cmd{border-radius:3px;font-family:var(--mono)}
.mk-av{border-radius:4px;background:var(--acc);border:0}
.mk-badge{border-radius:3px;font-family:var(--mono)}
.mk-card{border-left:4px solid var(--acc);border-radius:0;background:var(--bg)}
@media(max-width:900px){.mock{transform:none}}

/* --- programme (chaînes réelles) --------------------------------------- */
.prog{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));column-gap:56px;
 border-top:1.5px solid var(--line2)}
.prog a{display:grid;grid-template-columns:44px minmax(0,1fr) auto;align-items:baseline;gap:14px;
 padding:20px 6px;border-bottom:1.5px solid var(--line);color:var(--tx);
 transition:background .12s,color .12s,padding .15s}
.prog a:hover{background:var(--tx);color:#0f0e0c;padding-left:16px}
.prog .no{font-family:var(--mono);font-size:.85rem;color:var(--acc)}
.prog b{font-family:var(--serif);font-weight:700;font-size:1.55rem;letter-spacing:-.035em;line-height:1.1}
.prog .dir{display:inline-flex;align-items:center;gap:8px;font-family:var(--mono);font-size:.68rem;
 letter-spacing:.1em;text-transform:uppercase;color:var(--mute)}
.prog .dir::before{content:'';width:7px;height:7px;border-radius:50%;background:var(--acc)}
.prog a:hover .dir,.prog a:hover .no{color:#0f0e0c}
.prog a:hover .dir::before{background:#0f0e0c}
.prog-more{margin-top:22px;font-family:var(--mono);font-size:.78rem;letter-spacing:.08em;text-transform:uppercase}
@media(max-width:800px){.prog{grid-template-columns:1fr}}

/* --- fonctions : index numéroté ---------------------------------------- */
.feats{border-bottom:1.5px solid var(--line2)}
.feat{grid-template-columns:70px minmax(0,1fr) minmax(0,1.2fr);padding:26px 8px;
 border-top:1.5px solid var(--line2);background:none;backdrop-filter:none;border-radius:0;
 transition:background .12s,color .12s,padding .15s}
.feat:hover{background:var(--acc);padding-left:20px;transform:none;color:#0f0e0c}
.feat .n{font-family:var(--mono);font-style:normal;font-size:.85rem;color:var(--acc)}
.feat h3{font-family:var(--serif);font-weight:700;font-size:clamp(1.5rem,2.6vw,2.1rem);letter-spacing:-.04em}
.feat p{color:var(--mute)}
.feat:hover .n,.feat:hover h3,.feat:hover p{color:#0f0e0c}

/* --- étapes ------------------------------------------------------------ */
.steps{gap:0;border-top:1.5px solid var(--line2)}
.step{margin:0;padding:26px 28px 6px 0;border:0;border-radius:0;background:none;border-right:1.5px solid var(--line)}
.step+.step{padding-left:28px}
.step:last-child{border-right:0}
.step b{font-family:var(--serif);font-weight:800;font-size:5.4rem;letter-spacing:-.07em;color:var(--acc);
 background:none;-webkit-text-fill-color:currentColor;margin-bottom:10px}
.step h3{font-family:var(--serif);font-weight:700;font-size:1.3rem;letter-spacing:-.03em}
.step code{border-radius:3px;font-family:var(--mono)}
@media(max-width:800px){.step,.step+.step{padding:22px 0;border-right:0;border-bottom:1.5px solid var(--line)}}

/* --- bloc final : aplat orange ----------------------------------------- */
.cta-band{margin-top:110px;padding:64px 44px;background:var(--acc);color:#0f0e0c;border:1.5px solid var(--tx);
 border-radius:6px;box-shadow:10px 10px 0 var(--tx)}
.cta-band h2{font-family:var(--serif);font-weight:800;font-size:clamp(2.4rem,6.4vw,5.4rem);
 line-height:.92;letter-spacing:-.06em;margin-bottom:16px;max-width:13ch}
.cta-band p{font-size:1.1rem;max-width:30em;margin-bottom:30px;color:#0f0e0c;opacity:.82}
.cta-band .btn{background:#0f0e0c;color:var(--tx);border-color:#0f0e0c;box-shadow:4px 4px 0 var(--tx)}
.cta-band .btn:hover{background:#0f0e0c;color:var(--tx)}
.cta-band .btn.ghost{background:transparent;color:#0f0e0c;border-color:#0f0e0c;box-shadow:none}
.cta-band .btn.ghost:hover{background:#0f0e0c;color:var(--tx)}

/* --- connexion / tableau de bord --------------------------------------- */
.auth{box-shadow:8px 8px 0 var(--acc)}
.auth-logo{width:60px;height:60px;display:grid;place-items:center;margin-bottom:20px;border-radius:4px;
 background:var(--acc);border:1.5px solid var(--tx)}
.dash-hero{margin:34px 0 8px;padding:0;border:0;border-top:2px solid var(--tx);background:none;border-radius:0}
.dash-hero small{font-family:var(--mono);font-size:.74rem;letter-spacing:.1em;color:var(--mute);
 display:block;padding:14px 0 18px;text-transform:uppercase}
.dash-hero h1{font-family:var(--serif);font-weight:800;font-size:clamp(2.6rem,7vw,5.6rem);
 line-height:.92;letter-spacing:-.06em}
.dash-hero h1 span{color:var(--acc)}
.dash-hero p{color:var(--mute);margin:18px 0 0;max-width:36em}

/* --- pied de page ------------------------------------------------------ */
footer{border-top:1.5px solid var(--tx);background:none;margin-top:60px;font-family:var(--mono);
 font-size:.74rem;letter-spacing:.04em}

@media(prefers-reduced-motion:reduce){.meta-line .live i{animation:none}.mock{transform:none}}
"""

ULTRA_JS = """
(function(){
  var nav = document.querySelector('header.nav');
  var burger = document.querySelector('.burger');
  if (burger && nav) burger.addEventListener('click', function(){
    var open = nav.classList.toggle('open');
    burger.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  var path = location.pathname.replace(/\\/$/, '') || '/';
  document.querySelectorAll('.nav nav a[href]').forEach(function(a){
    var p = a.getAttribute('href').split('#')[0].replace(/\\/$/, '');
    if (p && p === path && !a.classList.contains('cta')) a.classList.add('on');
  });
  if (!matchMedia('(prefers-reduced-motion: reduce)').matches)
    document.querySelectorAll('[data-count]').forEach(function(el){
      var to = parseInt(el.dataset.count, 10) || 0, t0 = null;
      if (to < 2) return;
      (function tick(t){
        if (t0 === null) t0 = t;
        var p = Math.min((t - t0) / 1100, 1);
        el.textContent = Math.round(to * (1 - Math.pow(1 - p, 3))).toLocaleString('fr-FR');
        if (p < 1) requestAnimationFrame(tick);
      })(performance.now());
    });
})();
"""


def page(title: str, body: str, session: Optional[dict] = None, wide: bool = False) -> web.Response:
    nav = ('<a href="/tv">Télé</a><a href="/collab">Partenaires</a>'
           '<a href="/#fonctions">Fonctions</a>')
    if session:
        nav += ('<a href="/client">Client Discord</a><a href="/dashboard">Tableau de bord</a>'
                + nav_extra(session)
                + ('<a href="/live">🎛️ Direct</a>' if is_sharer(session) else '')
                + ('<a href="/share">📤 Partage</a>' if is_sharer(session) else '')

                + ('<a href="/studio">🎨 Studio</a>' if is_owner(session) else '')
                + '<a href="/account">Mon compte</a><a href="/logout" class="cta">Déconnexion</a>')
    else:
        nav += nav_extra(None) + '<a href="/login" class="cta">Se connecter</a>'
    html = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0c0c0e">
<title>{title} · {BRAND}</title>
<link rel="icon" type="image/png" href="/logo.png">
<link rel="apple-touch-icon" href="/logo.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800&family=Hanken+Grotesk:wght@400;500;600&family=DM+Mono:wght@400;500&display=swap">
<style>{CSS}{CLIENT_CSS}{EDITOR_CSS}{MIC_CSS}{GUILD_CSS}{POLISH_CSS}{ULTRA_CSS}</style></head><body><div class="bg"></div>
<header class="nav"><a class="brand" href="/" title="Revenir à l'accueil">{logo_tag(32, "margin-right:11px;border-radius:4px")}<span>{BRAND}</span></a><button class="burger" type="button" aria-label="Menu" aria-expanded="false"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg></button><nav>{nav}</nav></header>
<div class="wrap" {'style="max-width:1320px"' if wide else ''}>{body}</div>
<footer><div class="foot"><span>{BRAND} — site servi directement par le bot. Aucun logiciel à installer.</span>
<span><a href="/confidentialite">Confidentialité</a> &nbsp;·&nbsp; <a href="/conditions">Conditions d'utilisation</a>
 &nbsp;·&nbsp; <a href="/therms">Règlement</a></span></div></footer>
<script>{POLISH_JS}</script>
<script>{ULTRA_JS}</script>
</body></html>"""
    return web.Response(text=html, content_type="text/html")



# --------------------------------------------------------------------------- #
#  Pages
# --------------------------------------------------------------------------- #
async def page_home(request: web.Request) -> web.Response:
    session = session_of(request)
    guilds = len(getattr(STATE.bot, "guilds", ()) or ())
    users = sum(g.member_count or 0 for g in getattr(STATE.bot, "guilds", ()) or ())
    guild_label = "serveurs" if guilds > 1 else "serveur"
    user_label = "membres" if users > 1 else "membre"
    arrow = ('<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M3 8h10M9 4l4 4-4 4" '
             'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>')
    feats = "".join(
        f'<div class="feat"><span class="n">{i:02d}</span><h3>{name}</h3><p>{desc}</p></div>'
        for i, (name, desc) in enumerate([
            ("Connexion par le bot", "Ton identifiant et ton mot de passe arrivent en message privé. Rien à télécharger, rien à installer."),
            ("Accès selon tes rôles", "Le site lit tes permissions Discord en direct et t'ouvre exactement ce à quoi tu as droit."),
            ("Télé en direct", "Les chaînes en streaming, depuis le site ou directement dans un salon vocal en activité Discord."),
            ("Radio en vocal", "/radio chaine:&lt;numéro ou url&gt; et le bot diffuse la station dans ton salon."),
            ("Réponses automatiques", "/autoreply : un mot déclencheur, une réponse écrite par toi."),
            ("Musique &amp; DJ", "File d'attente, playlists, favoris et contrôles à distance."),
        ], start=1))
    # textes modifiables depuis le studio (/studio)
    label = SITE_HOME.get("bouton") or ("Ouvrir le tableau de bord" if session
                                        else "Se connecter")
    target = SITE_HOME.get("bouton_url") or ("/dashboard" if session else "/login")
    cta_main = f'<a class="btn" href="{escape(target)}">{escape(label)} ' + arrow + '</a>'
    eyebrow = escape(SITE_HOME.get("eyebrow") or f"Tableau de bord {BRAND}")
    titre_perso = SITE_HOME.get("titre")
    hero_title = (escape(titre_perso).replace("\n", "<br>") if titre_perso
                  else "Ton serveur Discord,<br>piloté <em>depuis ton navigateur.</em>")
    hero_lead = (escape(SITE_HOME.get("sous_titre")) if SITE_HOME.get("sous_titre")
                 else "Connecte-toi avec les identifiants que le bot t'envoie en message "
                      "privé. Aucun exécutable, aucun téléchargement : une page web qui "
                      "s'adapte à tes permissions.")
    try:
        _tv = load_channels().get("tv", [])[:8]
    except Exception:
        _tv = []
    prog = "".join(
        f'<a href="/tv"><span class="no">{i:02d}</span><b>{escape(str(ch.get("nom", "")))}</b>'
        f'<span class="dir">Direct</span></a>' for i, ch in enumerate(_tv, start=1))
    prog_block = (f'<h2 class="section">Au programme</h2><div class="prog">{prog}</div>'
                  '<p class="prog-more"><a href="/tv">Toutes les chaînes →</a></p>') if prog else ""
    body = f"""
<section class="hero2">
  <div class="meta-line rise"><span>N°01</span><span>{eyebrow}</span>
    <span class="live"><i></i>En ligne</span></div>
  <h1 class="rise" style="--d:.06s">{hero_title}</h1>
  <div class="hero-grid">
    <div>
      <p class="lead rise" style="--d:.14s">{hero_lead}</p>
      <div class="cta-row rise" style="--d:.2s">{cta_main}<a class="btn ghost" href="/tv">Regarder la télé</a></div>
      <div class="stats rise" style="--d:.26s">
        <div class="stat"><b data-count="{guilds}">{guilds}</b><small>{guild_label}</small></div>
        <div class="stat"><b data-count="{users}">{users}</b><small>{user_label}</small></div>
      </div>
    </div>
    <div class="mock rise" style="--d:.18s" aria-hidden="true">
      <div class="mock-bar"><i></i><i></i><i></i><span>Message privé · {BRAND}</span></div>
      <div class="mock-body">
        <div><span class="mk-cmd">/compte</span></div>
        <div class="mk-bot">
          <div class="mk-av">{logo_tag(38, "border-radius:0")}</div>
          <div class="mk-head">{BRAND} <span class="mk-badge">BOT</span></div>
          <div class="mk-card"><strong>Ton accès est prêt</strong>
            <dl><dt>Identifiant</dt><dd>prenom.nom</dd>
            <dt>Mot de passe provisoire</dt><dd>••••••••••••</dd></dl></div>
        </div>
      </div>
    </div>
  </div>
</section>

{prog_block}

<h2 class="section" id="fonctions">Ce que tu peux faire</h2>
<div class="feats">{feats}</div>

<h2 class="section">Comment obtenir mes identifiants ?</h2>
<div class="steps">
  <div class="step"><b>1</b><h3>Tape la commande</h3>
    <p>Sur le serveur Discord, lance <code>/compte</code>.</p></div>
  <div class="step"><b>2</b><h3>Lis ton message privé</h3>
    <p>Le bot t'envoie ton identifiant et un mot de passe provisoire.</p></div>
  <div class="step"><b>3</b><h3>Choisis ton mot de passe</h3>
    <p>À la première connexion, tu définis le tien. Tu peux ensuite lier Discord et Google pour entrer en un clic.</p></div>
</div>

<section class="cta-band">
  <h2>Entre dans le salon.</h2>
  <p>Un compte, tes rôles Discord, et tout est là. Aucune installation.</p>
  <div class="cta-row">{cta_main}<a class="btn ghost" href="/tv">Regarder la télé</a></div>
</section>
"""
    return page("Accueil", body, session)


async def page_login(request: web.Request) -> web.Response:
    if session_of(request):
        raise web.HTTPFound("/dashboard")
    error = request.query.get("error", "")
    notice = request.query.get("ok", "")
    blocks = ""
    if error:
        blocks += f'<div class="msg err">{error}</div>'
    if notice:
        blocks += f'<div class="msg ok">{notice}</div>'
    oauth = ""
    if env("DISCORD_CLIENT_ID") and env("DISCORD_CLIENT_SECRET"):
        oauth += '<a class="btn discord wide" href="/oauth/discord/start" style="margin-bottom:10px">Continuer avec Discord</a>'
    if env("GOOGLE_CLIENT_ID") and env("GOOGLE_CLIENT_SECRET"):
        oauth += '<a class="btn google wide" href="/oauth/google/start">Continuer avec Google</a>'
    if oauth:
        oauth = f'<div class="sep">ou</div>{oauth}'
    body = f"""
<div class="panel auth">
  <div class="auth-logo">{logo_tag(40, "border-radius:3px")}</div>
  <h2>Connexion</h2>
  <p class="sub">Identifiants envoyés par le bot en message privé (commande <b>/compte</b>).</p>
  {blocks}
  <form method="post" action="/login">
    <label for="u">Identifiant</label>
    <input id="u" name="username" autocomplete="username" required>
    <label for="p">Mot de passe</label>
    <input id="p" name="password" type="password" autocomplete="current-password" required>
    <button class="btn wide" style="margin-top:20px" type="submit">Se connecter</button>
  </form>
  {oauth}
  <p class="sub" style="margin-top:20px">Mot de passe oublié ? Tape <b>/compte reset</b> sur Discord,
     le bot t'en renvoie un nouveau.</p>
</div>"""
    return page("Connexion", body)


async def do_login(request: web.Request) -> web.Response:
    data = await request.post()
    username = str(data.get("username", "")).strip().lower()
    password = str(data.get("password", ""))
    account = row_dict(await STATE.db.fetchone(
        "SELECT * FROM web_accounts WHERE username = ?", (username,)))
    await asyncio.sleep(0.25)  # anti force brute basique
    if account is None or not check_password(password, account["salt"], account["password"]):
        raise web.HTTPFound("/login?error=Identifiant ou mot de passe incorrect.")
    return await finish_login(account)


async def finish_login(account: dict) -> web.Response:
    await STATE.db.execute("UPDATE web_accounts SET last_login = ? WHERE user_id = ?",
                           (stamp(), account["user_id"]))
    token = new_session(account["user_id"], account["username"],
                        await session_level(account["user_id"]))
    target = "/setup" if account.get("must_change") else "/dashboard"
    return set_cookie(web.HTTPFound(target), token)  # type: ignore[arg-type]


async def page_logout(request: web.Request) -> web.Response:
    session = session_of(request)
    if session:
        STATE.sessions.pop(session.get("token", ""), None)
    response = web.HTTPFound("/")
    response.del_cookie(COOKIE, path="/")
    raise response


def require_session(request: web.Request) -> dict:
    session = session_of(request)
    if session is None:
        raise web.HTTPFound("/login?error=Connecte-toi pour continuer.")
    return session


async def page_setup(request: web.Request) -> web.Response:
    session = require_session(request)
    account = await get_account(session["user_id"])
    if account is None:
        raise web.HTTPFound("/logout")
    if not account.get("must_change"):
        raise web.HTTPFound("/dashboard")
    error = request.query.get("error", "")
    body = f"""
<div class="panel">
  <h2>Bienvenue, {account['username']}</h2>
  <p class="sub">Première connexion : choisis ton mot de passe définitif.</p>
  {f'<div class="msg err">{error}</div>' if error else ''}
  <div class="msg info">Le mot de passe provisoire envoyé par le bot ne fonctionnera plus
   après cette étape.</div>
  <form method="post" action="/setup">
    <label for="c">Mot de passe provisoire (reçu en MP)</label>
    <input id="c" name="current" type="password" required>
    <label for="n">Nouveau mot de passe</label>
    <input id="n" name="new1" type="password" required>
    <label for="n2">Confirme le nouveau mot de passe</label>
    <input id="n2" name="new2" type="password" required>
    <ul class="rules">
      <li>10 caractères minimum</li><li>une majuscule et une minuscule</li>
      <li>un chiffre</li><li>un caractère spécial (!?@#...)</li>
      <li>les deux saisies doivent être identiques</li>
    </ul>
    <button class="btn wide" style="margin-top:20px" type="submit">Valider et entrer</button>
  </form>
</div>"""
    return page("Configuration", body, session)


async def do_setup(request: web.Request) -> web.Response:
    session = require_session(request)
    account = await get_account(session["user_id"])
    if account is None:
        raise web.HTTPFound("/logout")
    data = await request.post()
    current = str(data.get("current", ""))
    new1, new2 = str(data.get("new1", "")), str(data.get("new2", ""))
    if not check_password(current, account["salt"], account["password"]):
        raise web.HTTPFound("/setup?error=Mot de passe provisoire incorrect.")
    if new1 != new2:
        raise web.HTTPFound("/setup?error=Les deux mots de passe ne sont pas identiques.")
    problem = password_problem(new1)
    if problem:
        raise web.HTTPFound(f"/setup?error=Mot de passe refusé : {problem}")
    if check_password(new1, account["salt"], account["password"]):
        raise web.HTTPFound("/setup?error=Choisis un mot de passe différent du provisoire.")
    salt = secrets.token_hex(16)
    await STATE.db.execute(
        "UPDATE web_accounts SET salt = ?, password = ?, must_change = 0 WHERE user_id = ?",
        (salt, hash_password(new1, salt), account["user_id"]))
    raise web.HTTPFound("/dashboard")


async def page_dashboard(request: web.Request) -> web.Response:
    session = require_session(request)
    account = await get_account(session["user_id"])
    if account is None:
        raise web.HTTPFound("/logout")
    if account.get("must_change"):
        raise web.HTTPFound("/setup")
    guilds = await user_guilds(account["user_id"])
    if not guilds:
        body = """<div class="panel"><h2>Aucun serveur en commun</h2>
        <p class="sub">Le bot ne te voit sur aucun serveur. Rejoins un serveur où
        Lulyo est présent, puis recharge cette page.</p></div>"""
        return page("Tableau de bord", body, session)
    blocks = []
    for guild in guilds:
        items = "".join(
            f'<li>{ACCESS_LABELS[a][0]} {ACCESS_LABELS[a][1]}</li>'
            for a in guild["access"] if a in ACCESS_LABELS)
        icon = (f'<img src="{guild["icon"]}" alt="" style="width:46px;height:46px;'
                f'border-radius:14px;vertical-align:middle;margin-right:12px">'
                if guild["icon"] else "🌐 ")
        blocks.append(f"""<div class="card">
  <h3>{icon}{guild['name']}</h3>
  <p><span class="tag">{guild['level_name']}</span>
     <span class="tag gray">{guild['members']} membres</span></p>
  <ul class="perm">{items}</ul>
  <p style="margin-top:16px"><a class="btn" href="/guild/{guild['id']}">Gérer ce serveur</a></p>
  </div>""")
    error = request.query.get("error", "")
    body = f"""
{f'<div class="msg err">{error}</div>' if error else ''}
<section class="dash-hero"><small>Tableau de bord</small>
<h1>Bonjour, <span>{account['username']}</span>.</h1>
<p>Tes accès sont calculés en direct à partir de tes rôles Discord : pas besoin qu'un administrateur t'ajoute à la main.</p></section>
<div class="grid" style="margin-top:20px">{''.join(blocks)}</div>
<h2 class="section">Raccourcis</h2>
<div class="grid">
 <div class="card"><span class="ico">📺</span><h3>Télé en direct</h3>
   <p>Les chaînes disponibles depuis le site ou dans Discord.</p>
   <p style="margin-top:12px"><a class="btn" href="/tv">Ouvrir la télé</a></p></div>
 <div class="card"><span class="ico">👤</span><h3>Mon compte</h3>
   <p>Changer le mot de passe, lier Discord et Google.</p>
   <p style="margin-top:12px"><a class="btn ghost" href="/account">Paramètres</a></p></div>
</div>"""
    return page("Tableau de bord", body, session)


async def page_account(request: web.Request) -> web.Response:
    session = require_session(request)
    account = await get_account(session["user_id"])
    if account is None:
        raise web.HTTPFound("/logout")
    if account.get("must_change"):
        raise web.HTTPFound("/setup")
    error, notice = request.query.get("error", ""), request.query.get("ok", "")
    discord_state = ("✅ lié" if account.get("discord_ok") else "❌ non lié")
    google_state = (f"✅ lié ({account.get('google_mail')})" if account.get("google_sub")
                    else "❌ non lié")
    dlink = ('<a class="btn discord" href="/oauth/discord/start">Lier Discord</a>'
             if env("DISCORD_CLIENT_ID") and env("DISCORD_CLIENT_SECRET")
             else '<span class="tag gray">OAuth Discord non configuré (.env)</span>')
    glink = ('<a class="btn google" href="/oauth/google/start">Lier Google</a>'
             if env("GOOGLE_CLIENT_ID") and env("GOOGLE_CLIENT_SECRET")
             else '<span class="tag gray">OAuth Google non configuré (.env)</span>')
    body = f"""
<h2 class="section">Paramètres du compte</h2>
{f'<div class="msg err">{error}</div>' if error else ''}
{f'<div class="msg ok">{notice}</div>' if notice else ''}
<div class="grid">
  <div class="card">
    <h3>🔑 Mot de passe</h3>
    <form method="post" action="/account/password">
      <label>Mot de passe actuel</label><input name="current" type="password" required>
      <label>Nouveau mot de passe</label><input name="new1" type="password" required>
      <label>Confirmation</label><input name="new2" type="password" required>
      <ul class="rules"><li>10 caractères, majuscule, minuscule, chiffre, spécial</li>
      <li>saisi deux fois à l'identique</li></ul>
      <button class="btn wide" style="margin-top:16px">Changer le mot de passe</button>
    </form>
  </div>
  <div class="card">
    <h3>🔗 Connexions rapides</h3>
    <p style="margin:12px 0">Discord : <b>{discord_state}</b></p>{dlink}
    <p style="margin:18px 0 12px">Google : <b>{google_state}</b></p>{glink}
    <p class="sub" style="margin-top:18px;color:#8d96ad;font-size:.85rem">
      Une fois lié, un clic suffit pour te connecter, sans taper de mot de passe.</p>
  </div>
  <div class="card">
    <h3>🪪 Identité</h3>
    <p>Identifiant : <b>{account['username']}</b></p>
    <p>Compte Discord : <b>{account['user_id']}</b></p>
    <p>Créé le : {str(account.get('created_at'))[:10]}</p>
    <p>Dernière connexion : {str(account.get('last_login') or '—')[:19].replace('T', ' ')}</p>
  </div>
</div>"""
    return page("Mon compte", body, session)


async def do_password(request: web.Request) -> web.Response:
    session = require_session(request)
    account = await get_account(session["user_id"])
    if account is None:
        raise web.HTTPFound("/logout")
    data = await request.post()
    current = str(data.get("current", ""))
    new1, new2 = str(data.get("new1", "")), str(data.get("new2", ""))
    if not check_password(current, account["salt"], account["password"]):
        raise web.HTTPFound("/account?error=Mot de passe actuel incorrect.")
    if new1 != new2:
        raise web.HTTPFound("/account?error=Les deux saisies diffèrent.")
    problem = password_problem(new1)
    if problem:
        raise web.HTTPFound(f"/account?error=Mot de passe refusé : {problem}")
    salt = secrets.token_hex(16)
    await STATE.db.execute(
        "UPDATE web_accounts SET salt = ?, password = ?, must_change = 0 WHERE user_id = ?",
        (salt, hash_password(new1, salt), account["user_id"]))
    raise web.HTTPFound("/account?ok=Mot de passe mis à jour.")


# --------------------------------------------------------------------------- #
#  OAuth Discord / Google
# --------------------------------------------------------------------------- #
def oauth_redirect(provider: str) -> str:
    return f"{public_url()}/oauth/{provider}/callback"


async def oauth_start(request: web.Request) -> web.Response:
    provider = request.match_info["provider"]
    session = session_of(request)
    state = secrets.token_urlsafe(24)
    STATE.oauth_states[state] = {"provider": provider, "time": time.time(),
                                 "user_id": session["user_id"] if session else None}
    if provider == "discord":
        if not env("DISCORD_CLIENT_ID"):
            raise web.HTTPFound("/login?error=OAuth Discord non configuré.")
        url = "https://discord.com/oauth2/authorize?" + urlencode({
            "client_id": env("DISCORD_CLIENT_ID"), "redirect_uri": oauth_redirect("discord"),
            "response_type": "code", "scope": "identify", "state": state, "prompt": "consent"})
    elif provider == "google":
        if not env("GOOGLE_CLIENT_ID"):
            raise web.HTTPFound("/login?error=OAuth Google non configuré.")
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": env("GOOGLE_CLIENT_ID"), "redirect_uri": oauth_redirect("google"),
            "response_type": "code", "scope": "openid email profile", "state": state,
            "access_type": "online"})
    else:
        raise web.HTTPNotFound()
    raise web.HTTPFound(url)


async def oauth_callback(request: web.Request) -> web.Response:
    provider = request.match_info["provider"]
    code = request.query.get("code", "")
    state = request.query.get("state", "")
    info = STATE.oauth_states.pop(state, None)
    if not code or info is None or info["provider"] != provider:
        raise web.HTTPFound("/login?error=Connexion externe expirée, réessaie.")
    try:
        if provider == "discord":
            profile = await fetch_discord_profile(code)
            discord_id = int(profile["id"])
            account = await get_account(discord_id)
            if account is None:
                raise web.HTTPFound(
                    "/login?error=Aucun compte : tape /compte sur Discord pour en créer un.")
            await STATE.db.execute("UPDATE web_accounts SET discord_ok = 1 WHERE user_id = ?",
                                   (discord_id,))
            if info["user_id"] and info["user_id"] != discord_id:
                raise web.HTTPFound("/account?error=Ce compte Discord appartient à quelqu'un d'autre.")
            return await finish_login(account)
        else:
            profile = await fetch_google_profile(code)
            sub, mail = profile.get("sub", ""), profile.get("email", "")
            if info["user_id"]:
                await STATE.db.execute(
                    "UPDATE web_accounts SET google_sub = ?, google_mail = ? WHERE user_id = ?",
                    (sub, mail, info["user_id"]))
                raise web.HTTPFound("/account?ok=Compte Google lié.")
            account = row_dict(await STATE.db.fetchone(
                "SELECT * FROM web_accounts WHERE google_sub = ?", (sub,)))
            if account is None:
                raise web.HTTPFound(
                    "/login?error=Ce compte Google n'est lié à aucun profil. "
                    "Connecte-toi puis lie-le dans Mon compte.")
            return await finish_login(account)
    except web.HTTPException:
        raise
    except Exception:
        log.exception("OAuth %s en échec", provider)
        raise web.HTTPFound("/login?error=Connexion externe impossible.")


async def fetch_discord_profile(code: str) -> dict:
    data = {"client_id": env("DISCORD_CLIENT_ID"), "client_secret": env("DISCORD_CLIENT_SECRET"),
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": oauth_redirect("discord")}
    async with aiohttp.ClientSession() as http:
        async with http.post("https://discord.com/api/oauth2/token", data=data) as resp:
            token = (await resp.json()).get("access_token")
        async with http.get("https://discord.com/api/users/@me",
                            headers={"Authorization": f"Bearer {token}"}) as resp:
            return await resp.json()


async def fetch_google_profile(code: str) -> dict:
    data = {"client_id": env("GOOGLE_CLIENT_ID"), "client_secret": env("GOOGLE_CLIENT_SECRET"),
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": oauth_redirect("google")}
    async with aiohttp.ClientSession() as http:
        async with http.post("https://oauth2.googleapis.com/token", data=data) as resp:
            token = (await resp.json()).get("access_token")
        async with http.get("https://openidconnect.googleapis.com/v1/userinfo",
                            headers={"Authorization": f"Bearer {token}"}) as resp:
            return await resp.json()



# --------------------------------------------------------------------------- #
#  Proxy HLS : corrige CORS, http/https mélangés et User-Agent refusé
# --------------------------------------------------------------------------- #
PROXY_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_allowed_hosts: set[str] = set()


def refresh_allowed_hosts() -> set[str]:
    """Hôtes autorisés : ceux des chaînes déclarées (+ ceux découverts dans les
    playlists de ces chaînes). Empêche d'utiliser le site comme proxy ouvert."""
    from urllib.parse import urlparse
    for entry in load_channels()["tv"] + load_channels()["radios"]:
        host = urlparse(entry.get("url", "")).hostname
        if host:
            _allowed_hosts.add(host.lower())
    return _allowed_hosts


def proxy_url(url: str) -> str:
    from urllib.parse import quote
    return "/tv/stream?u=" + quote(url, safe="")


def rewrite_playlist(text: str, base: str) -> str:
    from urllib.parse import urljoin, urlparse
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if stripped.startswith("#"):
            # réécrit les URI= (clés de chiffrement, pistes audio…)
            def sub(match):
                target = urljoin(base, match.group(1))
                host = urlparse(target).hostname
                if host:
                    _allowed_hosts.add(host.lower())
                return f'URI="{proxy_url(target)}"'
            out.append(re.sub(r'URI="([^"]+)"', sub, line))
            continue
        target = urljoin(base, stripped)
        host = urlparse(target).hostname
        if host:
            _allowed_hosts.add(host.lower())
        out.append(proxy_url(target))
    return "\n".join(out)


async def tv_proxy(request: web.Request) -> web.StreamResponse:
    from urllib.parse import urlparse
    url = request.query.get("u", "")
    if not url.startswith(("http://", "https://")):
        return web.Response(status=400, text="url invalide")
    host = (urlparse(url).hostname or "").lower()
    if host not in refresh_allowed_hosts():
        return web.Response(status=403, text="hôte non autorisé")
    headers = {"User-Agent": PROXY_UA, "Accept": "*/*",
               "Referer": f"https://{host}/", "Origin": f"https://{host}"}
    rng = request.headers.get("Range")
    if rng:
        headers["Range"] = rng
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)
    try:
        session = aiohttp.ClientSession(timeout=timeout)
        upstream = await session.get(url, headers=headers, allow_redirects=True)
    except Exception as exc:
        log.debug("proxy TV : %s", exc)
        return web.Response(status=502, text="flux injoignable")

    ctype = (upstream.headers.get("Content-Type") or "").lower()
    is_playlist = ("mpegurl" in ctype or url.split("?")[0].endswith((".m3u8", ".m3u")))
    cors = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"}

    if is_playlist:
        try:
            raw = await upstream.text(errors="ignore")
        finally:
            await session.close()
        body = rewrite_playlist(raw, str(upstream.url))
        return web.Response(text=body, headers=cors,
                            content_type="application/vnd.apple.mpegurl")

    response = web.StreamResponse(status=upstream.status, headers=cors)
    for key in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
        if upstream.headers.get(key):
            response.headers[key] = upstream.headers[key]
    await response.prepare(request)
    try:
        async for chunk in upstream.content.iter_chunked(64 * 1024):
            await response.write(chunk)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    except Exception:
        pass
    finally:
        await session.close()
    return response


# --------------------------------------------------------------------------- #
#  Télé
# --------------------------------------------------------------------------- #
TV_SCRIPT = """
const CH = __CHANNELS__;
let PROXY = true;
const SOURCES = [
  "/vendor/hls.js",
  "https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js",
  "https://unpkg.com/hls.js@1.5.17/dist/hls.min.js",
  "https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.17/hls.min.js"
];
const video = document.getElementById('player');
const title = document.getElementById('now');
let hls = null, current = 0;

function loadScript(i){
  return new Promise((ok, ko) => {
    if (i >= SOURCES.length) return ko();
    const s = document.createElement('script');
    s.src = SOURCES[i]; s.async = true;
    s.onload = () => ok();
    s.onerror = () => loadScript(i + 1).then(ok, ko);
    document.head.appendChild(s);
  });
}

async function ensureHls(){
  if (window.Hls) return true;
  try { await loadScript(0); } catch(e) { return false; }
  return !!window.Hls;
}

async function play(i){
  const c = CH[i]; if (!c) return;
  current = i;
  document.querySelectorAll('.chan').forEach((e,k)=>e.classList.toggle('on', k===i));
  title.textContent = c.nom + ' — chargement…';
  if (hls) { hls.destroy(); hls = null; }
  try { localStorage.setItem('lulyo_tv', i); } catch(e) {}

  const native = video.canPlayType('application/vnd.apple.mpegurl');
  const src = PROXY ? ('/tv/stream?u=' + encodeURIComponent(c.url)) : c.url;
  if (await ensureHls() && window.Hls.isSupported()) {
    hls = new Hls({ lowLatencyMode: true, manifestLoadingMaxRetry: 3 });
    hls.loadSource(src); hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      title.textContent = c.nom; video.play().catch(()=>{});
    });
    hls.on(Hls.Events.ERROR, (e, d) => {
      if (!d.fatal) return;
      if (d.type === 'networkError') { hls.startLoad(); return; }
      title.textContent = c.nom + ' — flux indisponible (change de chaîne ou édite channels.json)';
    });
  } else if (native) {
    video.src = src;
    video.play().catch(()=>{});
    title.textContent = c.nom;
  } else {
    title.textContent = c.nom + " — hls.js n'a pas pu être chargé (réseau/CDN bloqué). "
      + "Le direct fonctionne quand la page est ouverte hors aperçu.";
  }
}

document.querySelectorAll('.chan').forEach((el,i)=>el.addEventListener('click',()=>play(i)));
const tgl = document.getElementById('proxy');
if (tgl) tgl.addEventListener('change', e => { PROXY = e.target.checked; play(current); });
video.addEventListener('error', ()=>{ title.textContent = (CH[current]||{}).nom + ' — flux illisible'; });
let start = 0; try { start = parseInt(localStorage.getItem('lulyo_tv')||'0'); } catch(e) {}
play(isNaN(start) ? 0 : start);
"""


ACTIVITY_BOOT = """
<script type="module">
// SDK servi par notre domaine : dans une activité, Discord bloque les CDN externes.
try {
  const { DiscordSDK } = await import('/vendor/discord-sdk.mjs');
  const params = new URLSearchParams(location.search);
  const appId = params.get('client_id') || document.body.dataset.app || '';
  if (appId) {
    const sdk = new DiscordSDK(appId);
    await sdk.ready();          // fait disparaître l'écran de chargement de Discord
    console.log('Activité Lulyo prête');
  }
} catch (e) { console.warn('SDK Discord indisponible :', e); }
</script>"""


def tv_body(channels: list[dict], activity: bool = False) -> str:
    items = "".join(
        f'<div class="chan"><span style="font-size:1.4rem">{c.get("logo") or "📺"}</span>'
        f'<span><b>{c["nom"]}</b><small>Direct</small></span></div>' for c in channels)
    script = TV_SCRIPT.replace("__CHANNELS__", json.dumps(channels, ensure_ascii=False))
    return f"""
<h2 class="section">📺 Télévision en direct</h2>
<div class="tvwrap">
  <div><video id="player" controls playsinline autoplay muted></video>
    <p style="margin-top:12px;color:#aab3c9">En cours : <b id="now">—</b></p>
    <p style="margin-top:8px;color:#9aa3ba;font-size:.86rem">
      <label style="display:inline-flex;align-items:center;gap:8px;margin:0">
        <input type="checkbox" id="proxy" checked style="width:auto">
        Passer par le serveur (corrige les blocages CORS et les flux en http)</label></p>
    <p style="color:#79829a;font-size:.84rem;margin-top:6px">Les chaînes sont définies dans
      <code>channels.json</code> à côté de bot.py — ajoute les tiennes librement
      (flux HLS <code>.m3u8</code>).</p></div>
  <div class="card list">{items}</div>
</div>
{ACTIVITY_BOOT if activity else ''}
<script>{script}</script>"""


async def page_tv(request: web.Request) -> web.Response:
    channels = load_channels()["tv"]
    return page("Télé", tv_body(channels), session_of(request), wide=True)


# --------------------------------------------------------------------------- #
#  Bibliothèques servies depuis notre domaine
#  (dans une activité Discord, tout passe par le proxy <app_id>.discordsays.com :
#   un script chargé depuis un CDN externe serait bloqué)
# --------------------------------------------------------------------------- #
VENDOR_FILES = {
    "hls.js": [
        "https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js",
        "https://unpkg.com/hls.js@1.5.17/dist/hls.min.js",
        "https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.17/hls.min.js",
    ],
    # bundle ESM officiel (contient toutes ses dépendances)
    "discord-sdk.mjs": [
        "https://cdn.jsdelivr.net/npm/@discord/embedded-app-sdk@2/+esm",
        "https://cdn.jsdelivr.net/npm/@discord/embedded-app-sdk@2.5.0/+esm",
    ],
}


def vendor_dir() -> Path:
    path = Path(__file__).resolve().parent / ".vendor"
    path.mkdir(exist_ok=True)
    return path


async def serve_vendor(request: web.Request) -> web.StreamResponse:
    """Télécharge la bibliothèque une seule fois, puis la sert localement."""
    name = request.match_info["name"]
    sources = VENDOR_FILES.get(name)
    if not sources:
        raise web.HTTPNotFound()
    cached = vendor_dir() / name
    if not cached.is_file() or cached.stat().st_size < 1024:
        for url in sources:
            try:
                timeout = aiohttp.ClientTimeout(total=25)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as response:
                        if response.status != 200:
                            continue
                        cached.write_bytes(await response.read())
                log.info("bibliothèque %s téléchargée depuis %s", name, url)
                break
            except Exception as exc:
                log.debug("vendor %s : %s", url, exc)
    if not cached.is_file():
        raise web.HTTPBadGateway(text="bibliothèque indisponible")
    return web.FileResponse(cached, headers={
        "Content-Type": "application/javascript; charset=utf-8",
        "Cache-Control": "public, max-age=604800"})


# --------------------------------------------------------------------------- #
#  Point d'entrée de l'activité Discord
#  Depuis 2024, Discord refuse de lancer une activité tant que l'application n'a
#  pas de « Primary Entry Point command » (type 4). On l'enregistre nous-mêmes.
# --------------------------------------------------------------------------- #
ENTRY_POINT_TYPE = 4          # PRIMARY_ENTRY_POINT
ENTRY_HANDLER_LAUNCH = 2      # DISCORD_LAUNCH_ACTIVITY : Discord ouvre l'activité seul


def activity_command_name() -> str:
    raw = (env("WEB_ACTIVITY_COMMAND", "tele") or "tele").lower().strip()
    cleaned = re.sub(r"[^a-z0-9_-]", "", raw)[:32]
    return cleaned or "tele"


async def fetch_entry_point(bot: Any) -> Optional[dict]:
    """Récupère la commande de point d'entrée déjà déclarée sur l'application."""
    app_id = env("DISCORD_CLIENT_ID") or str(getattr(bot, "application_id", "") or "")
    if not app_id:
        return None
    route = discord.http.Route("GET", "/applications/{application_id}/commands",
                               application_id=app_id)
    try:
        for command in await bot.http.request(route) or []:
            if int(command.get("type", 1)) == ENTRY_POINT_TYPE:
                return {
                    "id": command.get("id"),
                    "name": command.get("name"),
                    "description": command.get("description") or "Activité",
                    "type": ENTRY_POINT_TYPE,
                    "handler": command.get("handler", ENTRY_HANDLER_LAUNCH),
                    "integration_types": command.get("integration_types", [0, 1]),
                    "contexts": command.get("contexts", [0, 1, 2]),
                }
    except Exception as exc:
        log.debug("Activité : lecture des commandes impossible (%s)", exc)
    return None


def patch_global_sync(bot: Any) -> None:
    """Évite l'erreur 50240 : discord.py remplace toutes les commandes d'un coup et
    oublie la commande de point d'entrée, que Discord refuse de voir disparaître.
    On la réinjecte, et on rejoue la synchronisation si elle a déjà échoué."""
    http = bot.http
    if getattr(http, "_lulyo_entry_patched", False):
        return
    original = http.bulk_upsert_global_commands

    async def wrapper(application_id, payload, *args, **kwargs):
        commands = list(payload or [])
        if STATE.entry_point and not any(
                int(c.get("type", 1)) == ENTRY_POINT_TYPE for c in commands):
            commands.append(STATE.entry_point)
        try:
            return await original(application_id, commands, *args, **kwargs)
        except discord.HTTPException as exc:
            if getattr(exc, "code", 0) != 50240:
                raise
            entry = STATE.entry_point or await fetch_entry_point(bot)
            if entry is None:
                raise
            STATE.entry_point = entry
            log.info("Activité : point d'entrée « /%s » réinjecté dans la "
                     "synchronisation", entry.get("name"))
            commands = [c for c in commands
                        if int(c.get("type", 1)) != ENTRY_POINT_TYPE] + [entry]
            return await original(application_id, commands, *args, **kwargs)

    http.bulk_upsert_global_commands = wrapper
    http._lulyo_entry_patched = True


async def ensure_entry_point(bot: Any) -> None:
    """Crée (ou met à jour) la commande qui lance l'activité."""
    if env("WEB_ACTIVITY_SETUP", "1").lower() in ("0", "false", "no", "non"):
        return
    app_id = env("DISCORD_CLIENT_ID") or str(getattr(bot, "application_id", "") or "")
    if not app_id:
        log.info("Activité : DISCORD_CLIENT_ID absent, point d'entrée non enregistré")
        return
    existing = await fetch_entry_point(bot)
    if existing is not None:
        STATE.entry_point = existing
        patch_global_sync(bot)
        log.info("Activité : point d'entrée « /%s » déjà en place", existing.get("name"))
        return

    name = activity_command_name()
    payload = {
        "name": name,
        "description": f"Regarder la télé {BRAND} ensemble dans un salon vocal",
        "type": ENTRY_POINT_TYPE,
        "handler": ENTRY_HANDLER_LAUNCH,
        "integration_types": [0, 1],   # serveur + compte utilisateur
        "contexts": [0, 1, 2],         # serveur, MP avec le bot, MP privés
    }
    route = discord.http.Route("POST", "/applications/{application_id}/commands",
                               application_id=app_id)
    try:
        await bot.http.request(route, json=payload)
        STATE.entry_point = dict(payload)
        patch_global_sync(bot)
        log.info("Activité : commande de point d'entrée « /%s » enregistrée", name)
    except discord.Forbidden:
        log.warning("Activité : accès refusé à l'enregistrement du point d'entrée")
    except discord.HTTPException as exc:
        detail = (getattr(exc, "text", "") or str(exc))[:200]
        if "ENTRY_POINT" in detail or "handler" in detail:
            log.warning("Activité : active d'abord « Enable Activities » dans le portail "
                        "développeur (Activities -> Settings). Détail : %s", detail)
        else:
            log.warning("Activité : point d'entrée refusé (%s)", detail)
    except Exception as exc:
        log.debug("Activité : %s", exc, exc_info=True)


async def page_tv_activity(request: web.Request) -> web.Response:
    """Page allégée destinée à l'iframe d'activité Discord."""
    channels = load_channels()["tv"]
    app_id = env("DISCORD_CLIENT_ID")
    html = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Télé {BRAND}</title><style>{CSS}
body{{padding:0}} .wrap{{padding:16px}} .list{{max-height:none}}</style></head>
<body data-app="{app_id}"><div class="bg"></div>
<div class="wrap" style="max-width:100%">
{tv_body(channels, activity=True)}</div></body></html>"""
    response = web.Response(text=html, content_type="text/html")
    # autorise l'intégration dans le client Discord
    response.headers["Content-Security-Policy"] = (
        "frame-ancestors https://*.discord.com https://discord.com "
        "https://*.discordsays.com")
    return response


async def api_channels(request: web.Request) -> web.Response:
    return web.json_response(load_channels())


async def api_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "brand": BRAND,
                              "guilds": len(getattr(STATE.bot, "guilds", ()) or ()),
                              "url": public_url()})


# --------------------------------------------------------------------------- #
#  Console de gestion d'un serveur
# --------------------------------------------------------------------------- #
class ApiError(web.HTTPException):
    """Erreur renvoyée en JSON aux appels /api/ (au lieu d'une redirection HTML)."""

    def __init__(self, message: str, status: int = 403):
        self.status_code = status
        super().__init__(text=json.dumps({"error": message}, ensure_ascii=False),
                         content_type="application/json")


async def guild_context(request: web.Request) -> tuple[dict, Any, discord.Member, int]:
    """Session + serveur + membre + niveau.

    Les pages sont redirigées, les appels /api/ reçoivent un JSON explicite :
    une redirection renvoyait du HTML au client, qui restait bloqué sur
    « Chargement… » sans afficher la moindre raison.
    """
    is_api = request.path.startswith("/api/")

    def stop(message: str, target: str, status: int = 403) -> None:
        if is_api:
            raise ApiError(message, status)
        raise web.HTTPFound(target)

    try:
        session = require_session(request)
    except web.HTTPException:
        if is_api:
            raise ApiError("Tu n'es pas connecté.", 401)
        raise
    account = await get_account(session["user_id"])
    if account is None:
        stop("Session expirée, reconnecte-toi.", "/logout", 401)
    if account.get("must_change"):
        stop("Change ton mot de passe avant de continuer.", "/setup", 401)
    try:
        guild_id = int(request.match_info["gid"])
    except (KeyError, ValueError):
        stop("Serveur invalide.", "/dashboard", 400)

    guild = STATE.bot.get_guild(guild_id)
    if guild is None:
        stop("Le bot n'est pas (ou plus) sur ce serveur.",
             "/dashboard?error=Serveur introuvable.", 404)

    member = guild.get_member(account["user_id"])
    if member is None:
        # le cache des membres peut être incomplet : on interroge Discord
        try:
            member = await guild.fetch_member(account["user_id"])
        except (discord.NotFound, discord.Forbidden):
            member = None
        except Exception:
            log.debug("fetch_member", exc_info=True)
            member = None
    if member is None:
        stop("Tu n'es pas membre de ce serveur, ou l'intent « Server Members » "
             "est desactive dans le portail developpeur.",
             "/dashboard?error=Tu n'es pas sur ce serveur.", 403)

    level = await level_of(member, guild)
    return session, guild, member, level


def escape(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def text_channels(guild: Any) -> list:
    return [c for c in guild.text_channels
            if c.permissions_for(guild.me).send_messages]


def voice_channels(guild: Any) -> list:
    return list(guild.voice_channels)


GUILD_CSS = """
@view-transition{navigation:auto}
header.nav{view-transition-name:site-nav}
::view-transition-old(root){animation:vtOut .16s ease both}
::view-transition-new(root){animation:vtIn .42s cubic-bezier(.2,.7,.2,1) both}
@keyframes vtOut{to{opacity:0;transform:translateY(-6px)}}
@keyframes vtIn{from{opacity:0;transform:translateY(12px)}}
@supports not (view-transition-name:none){.wrap{animation:vtIn .5s cubic-bezier(.2,.7,.2,1) both}}

.console{display:grid;grid-template-columns:262px minmax(0,1fr);gap:44px;align-items:start}
.side{position:sticky;top:92px}
.side-head{display:flex;align-items:center;gap:13px;padding:4px 10px 20px;margin-bottom:12px;
 border-bottom:1px solid var(--line)}
.side-head img,.side-head .gi{width:48px;height:48px;border-radius:15px;flex:none;object-fit:cover}
.gi{display:grid;place-items:center;background:var(--accbg);border:1px solid var(--accln);
 font-family:var(--serif);color:var(--acc2);font-size:1.3rem}
.side-head b{display:block;font-family:var(--serif);font-weight:500;font-size:1.12rem;line-height:1.2;
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:170px}
.side-head small{display:block;margin-top:3px;font-size:.7rem;letter-spacing:.14em;
 text-transform:uppercase;color:var(--acc)}
.side-list{position:relative;display:flex;flex-direction:column;gap:2px}
.side-ind{position:absolute;left:0;top:0;width:100%;height:42px;border-radius:11px;
 background:var(--accbg);border:1px solid var(--accln);pointer-events:none;z-index:0;opacity:0}
.console.ready .side-ind{opacity:1;
 transition:transform .5s cubic-bezier(.3,.85,.2,1),height .3s}
.side .si,.side-links a{position:relative;z-index:1;display:flex;align-items:center;gap:13px;
 height:42px;padding:0 14px;border-radius:11px;color:var(--mute);font-size:.94rem;font-weight:500;
 background:none;border:0;cursor:pointer;font-family:inherit;text-align:left;width:100%;
 transition:color .2s,background .2s;white-space:nowrap}
.side .si:hover,.side-links a:hover{color:var(--tx)}
.side-links a:hover{background:rgba(255,255,255,.05)}
.side .si.on{color:var(--tx)}
.side svg{width:18px;height:18px;flex:none;fill:none;stroke:currentColor;stroke-width:1.6;
 stroke-linecap:round;stroke-linejoin:round;opacity:.9}
.side .si.on svg{stroke:var(--acc2)}
.side-sep{height:1px;background:var(--line);margin:14px 8px}
.side-links{display:flex;flex-direction:column;gap:2px}

.stage{min-width:0}
.view{display:none}
.view.on{display:block;animation:viewIn .55s cubic-bezier(.2,.7,.2,1) both}
.view.out{display:block;animation:viewOut .15s ease both}
@keyframes viewIn{from{opacity:0;transform:translateY(18px)}}
@keyframes viewOut{to{opacity:0;transform:translateX(-14px)}}
.view.on .tile,.view.on .card{animation:viewIn .6s cubic-bezier(.2,.7,.2,1) both}
.view.on .tile:nth-child(2){animation-delay:.05s}.view.on .tile:nth-child(3){animation-delay:.1s}
.view.on .tile:nth-child(4){animation-delay:.15s}.view.on .card{animation-delay:.12s}
.view-head{margin-bottom:28px}
.view-head .eyebrow{margin-bottom:12px}
.view-head h2{font-family:var(--serif);font-weight:500;font-size:clamp(2rem,3.6vw,2.8rem);
 letter-spacing:-.03em;line-height:1.08}
.view-head p{color:var(--mute);margin-top:10px;max-width:580px}
.tiles{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));margin-bottom:16px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:22px;
 display:flex;flex-direction:column;transition:border-color .25s,background .25s,transform .25s}
.tile:hover{border-color:var(--line2);background:var(--panel2);transform:translateY(-2px)}
.tile small{font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;color:var(--dim)}
.tile b{font-family:var(--serif);font-weight:500;font-size:2.7rem;line-height:1;letter-spacing:-.03em;
 margin:12px 0 10px}
.tile b i{font-style:normal;font-family:var(--sans);font-size:1rem;color:var(--mute);margin-left:5px}
.tile b.sm{font-size:1.4rem;line-height:1.25;letter-spacing:-.015em;margin:12px 0 10px}
.tile span{color:var(--mute);font-size:.88rem}
.hint{color:var(--dim);font-size:.84rem;margin-top:12px}
.flash{transition:opacity .5s,transform .5s;margin-bottom:26px}
.flash.gone{opacity:0;transform:translateY(-8px)}
details.fold summary{cursor:pointer;color:var(--acc2);font-weight:500}

@media(max-width:940px){
 .console{grid-template-columns:minmax(0,1fr);gap:22px}
 .side{position:static;min-width:0}
 .side-head{padding-bottom:14px;margin-bottom:8px}
 .side-list,.side-links{flex-direction:row;overflow-x:auto;gap:6px;padding-bottom:4px}
 .side-ind{display:none}
 .side .si,.side-links a{width:auto;flex:none}
 .side .si.on{background:var(--accbg);box-shadow:0 0 0 1px var(--accln) inset}
 .side-sep{display:none}
 .side-links{margin-top:6px}
}
"""

GUILD_JS = """
(function(){
  var KEY='lulyo.view.__GID__';
  var items=[].slice.call(document.querySelectorAll('.si[data-view]'));
  var views={};
  [].forEach.call(document.querySelectorAll('.view'),function(v){views[v.dataset.view]=v});
  var con=document.querySelector('.console'), ind=document.querySelector('.side-ind');
  var reduce=window.matchMedia&&matchMedia('(prefers-reduced-motion: reduce)').matches;
  var cur=null, tok=0;
  function place(el){
    if(!ind||!el) return;
    ind.style.transform='translateY('+el.offsetTop+'px)';
    ind.style.height=el.offsetHeight+'px';
  }
  function show(name,instant){
    if(!views[name]) name=items[0].dataset.view;
    if(name===cur) return;
    var prev=cur?views[cur]:null; cur=name;
    items.forEach(function(b){
      var on=b.dataset.view===name;
      b.classList.toggle('on',on);
      b.setAttribute('aria-current',on?'page':'false');
      if(on) place(b);
    });
    try{localStorage.setItem(KEY,name)}catch(e){}
    if(history.replaceState) history.replaceState(null,'',location.pathname+location.search+'#'+name);
    var mine=++tok;
    function enter(){
      if(mine!==tok) return;
      Object.keys(views).forEach(function(k){views[k].classList.remove('on','out')});
      views[name].classList.add('on');
    }
    if(prev&&!instant&&!reduce){ prev.classList.add('out'); setTimeout(enter,150); } else enter();
    if(!instant){
      var top=con.getBoundingClientRect().top+window.pageYOffset-92;
      if(window.pageYOffset>top) window.scrollTo({top:top,behavior:reduce?'auto':'smooth'});
    }
  }
  items.forEach(function(b){ b.addEventListener('click',function(){show(b.dataset.view)}); });
  window.addEventListener('resize',function(){ var o=items.filter(function(b){return b.classList.contains('on')})[0]; place(o); });
  var params=new URLSearchParams(location.search), start=location.hash.slice(1);
  if(!views[start]){ start=params.has('q')&&views.membres?'membres':''; }
  if(!views[start]){ try{start=localStorage.getItem(KEY)||''}catch(e){} }
  show(start,true);
  requestAnimationFrame(function(){requestAnimationFrame(function(){con.classList.add('ready')})});
  if(params.has('ok')||params.has('error')){
    params.delete('ok'); params.delete('error');
    var qs=params.toString();
    if(history.replaceState) history.replaceState(null,'',location.pathname+(qs?'?'+qs:'')+'#'+cur);
    setTimeout(function(){
      [].forEach.call(document.querySelectorAll('.flash'),function(f){
        f.classList.add('gone'); setTimeout(function(){f.remove()},520);
      });
    },7000);
  }
})();
"""

SIDE_ICONS = {
    "apercu": '<rect x="3" y="3" width="7" height="7" rx="1.6"/><rect x="14" y="3" width="7" height="7" rx="1.6"/><rect x="3" y="14" width="7" height="7" rx="1.6"/><rect x="14" y="14" width="7" height="7" rx="1.6"/>',
    "parler": '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>',
    "moderation": '<path d="M12 3l8 3v6c0 4.5-3.3 7.8-8 9-4.7-1.2-8-4.5-8-9V6l8-3z"/>',
    "radio": '<circle cx="12" cy="12" r="2"/><path d="M7.8 7.8a6 6 0 0 0 0 8.4M16.2 7.8a6 6 0 0 1 0 8.4M4.9 4.9a10 10 0 0 0 0 14.2M19.1 4.9a10 10 0 0 1 0 14.2"/>',
    "membres": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.6-3.6 3.2-5.5 6.5-5.5s5.9 1.9 6.5 5.5M16 4.7a3.5 3.5 0 0 1 0 6.6M18 14.8c2 .6 3.3 2.3 3.5 5.2"/>',
    "configuration": '<path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/>',
    "mp": '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M3.5 7l8.5 6 8.5-6"/>',
    "annonces": '<path d="M3 11v2a1 1 0 0 0 1 1h3l8 4V6L7 10H4a1 1 0 0 0-1 1z"/><path d="M19 9a4 4 0 0 1 0 6"/>',
    "client": '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
    "retour": '<path d="M19 12H5M11 6l-6 6 6 6"/>',
}


def side_icon(name: str) -> str:
    return f'<svg viewBox="0 0 24 24" aria-hidden="true">{SIDE_ICONS[name]}</svg>'


async def page_guild(request: web.Request) -> web.Response:
    session, guild, member, level = await guild_context(request)
    access = ACCESS_BY_LEVEL.get(level, ACCESS_BY_LEVEL[0])
    error, notice = request.query.get("error", ""), request.query.get("ok", "")
    gid = guild.id

    online = sum(1 for m in guild.members if m.status is not discord.Status.offline) \
        if guild.members else 0
    radio_now = escape(STATE.radio_now.get(gid, {}).get("nom", "à l'arrêt"))
    boost = f"boost niveau {guild.premium_tier}" if guild.premium_tier else "aucun boost"

    # chaque entrée : (identifiant, libellé, icône, titre, sous-titre, contenu)
    views: list = []

    views.append(("apercu", "Aperçu", "apercu", "Vue d'ensemble",
                  "L'état de ton serveur en un coup d'œil.", f"""
<div class="tiles">
  <div class="tile"><small>Membres</small><b>{guild.member_count or 0}</b>
    <span>{online} connecté(s) · {len(guild.roles)} rôles</span></div>
  <div class="tile"><small>Salons écrits</small><b>{len(guild.text_channels)}</b>
    <span>{len(guild.voice_channels)} vocaux · {len(guild.categories)} catégories</span></div>
  <div class="tile"><small>Latence du bot</small><b>{round(STATE.bot.latency * 1000)}<i>ms</i></b>
    <span>{boost}</span></div>
  <div class="tile"><small>Radio</small><b class="sm">{radio_now}</b><span>en salon vocal</span></div>
</div>
<div class="card"><h3>Client Discord</h3>
  <p>Lis et écris dans tes salons depuis le navigateur.</p>
  <p style="margin-top:14px"><a class="btn" href="/client?guild={gid}">Ouvrir le client</a></p></div>"""))

    if "moderation" in access:
        options = "".join(f'<option value="{c.id}">#{escape(c.name)}</option>'
                          for c in text_channels(guild))
        views.append(("parler", "Parler", "parler", "Parler dans un salon",
                      "Le bot envoie ton message en son nom.", f"""
<div class="card"><form method="post" action="/guild/{gid}/say">
  <label>Salon</label><select name="channel">{options}</select>
  <label>Message</label><input name="content" maxlength="1900" required>
  <button class="btn" style="margin-top:18px">Envoyer</button>
</form></div>"""))

        views.append(("moderation", "Modération", "moderation", "Modération",
                      "Exclure, expulser, bannir ou rétablir un membre.", f"""
<div class="card"><form method="post" action="/guild/{gid}/moderate">
  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(190px,1fr))">
    <div><label>Membre (identifiant ou pseudo)</label><input name="target" required></div>
    <div><label>Action</label><select name="action">
      <option value="timeout">Exclusion temporaire</option>
      <option value="untimeout">Lever l'exclusion</option>
      <option value="kick">Expulser</option>
      <option value="ban">Bannir</option>
      <option value="unban">Débannir (identifiant)</option>
    </select></div>
    <div><label>Durée (minutes, exclusion)</label><input name="minutes" value="10"></div>
    <div><label>Raison</label><input name="reason" value="Via le site Lulyo"></div>
  </div>
  <button class="btn" style="margin-top:18px">Appliquer</button>
</form></div>"""))

    if "radio" in access and level >= 1:
        vopts = "".join(f'<option value="{c.id}">{escape(c.name)}</option>'
                        for c in voice_channels(guild))
        radios = load_channels()["radios"]
        ropts = "".join(f'<option value="{i}">{i}. {escape(r["nom"])}</option>'
                        for i, r in enumerate(radios, start=1))
        views.append(("radio", "Radio", "radio", "Radio en vocal",
                      f"Actuellement : {radio_now}.", f"""
<div class="card"><form method="post" action="/guild/{gid}/radio">
  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(190px,1fr))">
    <div><label>Salon vocal</label><select name="voice">{vopts}</select></div>
    <div><label>Station</label><select name="station">{ropts}</select></div>
    <div><label>ou URL d'un flux</label><input name="url" placeholder="https://…"></div>
  </div>
  <button class="btn" style="margin-top:18px">Lancer</button>
  <button class="btn ghost" style="margin-top:18px" formaction="/guild/{gid}/radio/stop">Arrêter</button>
</form></div>"""))

    if "membres" in access:
        query = request.query.get("q", "").lower()
        members = [m for m in guild.members
                   if not query or query in m.display_name.lower() or query in str(m.id)]
        members = sorted(members, key=lambda m: (-m.top_role.position, m.display_name.lower()))[:60]
        rows = "".join(
            f"<tr><td>{escape(m.display_name)}</td><td>{escape(m.name)}</td>"
            f"<td><code>{m.id}</code></td><td>{escape(m.top_role.name)}</td>"
            f"<td>{m.joined_at.strftime('%d/%m/%Y') if m.joined_at else '—'}</td></tr>"
            for m in members)
        views.append(("membres", "Membres", "membres", "Membres",
                      "Recherche par pseudo ou identifiant.", f"""
<div class="card">
  <form method="get"><label>Rechercher</label>
    <input name="q" value="{escape(query)}" placeholder="pseudo ou identifiant"></form>
  <div style="overflow:auto;margin-top:16px"><table>
    <tr><th>Affiché</th><th>Compte</th><th>Identifiant</th><th>Rôle</th><th>Arrivé</th></tr>
    {rows}</table></div>
  <p class="hint">60 premiers résultats.</p>
</div>"""))

    if "configuration" in access:
        try:
            settings = await STATE.db.get_settings(guild.id)  # type: ignore[attr-defined]
        except Exception:
            settings = {}
        rows = "".join(
            f"<tr><td><code>{escape(k)}</code></td><td>{escape(json.dumps(v, ensure_ascii=False))[:160]}</td></tr>"
            for k, v in sorted(settings.items())) or "<tr><td>—</td><td>Aucun réglage lisible</td></tr>"
        views.append(("configuration", "Configuration", "configuration", "Configuration",
                      "Lecture seule : utilise /config ou /setup sur Discord pour modifier.", f"""
<div class="card"><div style="overflow:auto;max-height:520px"><table>
  <tr><th>Clé</th><th>Valeur</th></tr>{rows}</table></div></div>"""))

    if "moderation" in access:
        roles_opts = "".join(
            f'<option value="{r.id}">@{escape(r.name)} ({len(r.members)})</option>'
            for r in sorted(guild.roles, key=lambda r: -r.position)
            if not r.is_default() and r.members)
        mass = (f"""
  <details class="fold" style="margin-top:24px"><summary>Envoi groupé à un rôle (Administrateur)</summary>
  <form method="post" action="/guild/{gid}/dm-role" style="margin-top:14px">
    <label>Rôle destinataire</label><select name="role">{roles_opts}</select>
    <label>Message</label>
    <textarea name="content" rows="4" maxlength="1800" required></textarea>
    <button class="btn" style="margin-top:16px">Envoyer à tout le rôle</button>
    <p class="hint">1 message par seconde&nbsp;; les membres qui refusent les MP sont simplement ignorés.</p>
  </form></details>""" if level >= 3 else "")
        views.append(("mp", "Messages privés", "mp", "Messages privés",
                      "Écris à un membre au nom du staff.", f"""
<div class="card">
  <form method="post" action="/guild/{gid}/dm">
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
      <div><label>Destinataire (identifiant ou pseudo)</label><input name="target" required></div>
      <div><label>Titre (facultatif)</label><input name="title" placeholder="Message du staff"></div>
    </div>
    <label>Message — Entrée pour aller à la ligne</label>
    <textarea name="content" rows="4" maxlength="1800" required></textarea>
    <button class="btn" style="margin-top:18px">Envoyer le MP</button>
    <p class="hint">Le MP est envoyé par le bot et indique que tu en es l'auteur.
      Si le membre refuse les MP, tu en es averti.</p>
  </form>{mass}
</div>"""))

    if voice_owner(member, guild):
        views.append(("micro", "Micro", "radio", "Micro en direct",
                      "Parle dans un salon vocal avec la voix du bot.",
                      mic_panel(guild)))

    nav = "".join(
        f'<button type="button" class="si" data-view="{vid}">{side_icon(ico)}{escape(label)}</button>'
        for vid, label, ico, _t, _s, _h in views)
    panels = "".join(
        f'<section class="view" data-view="{vid}"><div class="view-head">'
        f'<p class="eyebrow">{escape(guild.name)}</p><h2>{title}</h2><p>{sub}</p></div>{html}</section>'
        for vid, _l, _i, title, sub, html in views)

    extra = ""
    if "moderation" in access:
        extra += f'<a href="/guild/{gid}/annonce">{side_icon("annonces")}Annonces</a>'
    extra += (f'<a href="/client?guild={gid}">{side_icon("client")}Client Discord</a>'
              f'<a href="/dashboard">{side_icon("retour")}Tous mes serveurs</a>')

    initial = escape(guild.name[:1].upper() or "?")
    icon = (f'<img src="{guild.icon.url}" alt="">' if guild.icon else f'<span class="gi">{initial}</span>')
    flash = ""
    if error:
        flash += f'<div class="msg err flash">{escape(error)}</div>'
    if notice:
        flash += f'<div class="msg ok flash">{escape(notice)}</div>'

    body = f"""
<div class="console" data-guild="{gid}">
  <aside class="side">
    <div class="side-head">{icon}<div><b>{escape(guild.name)}</b><small>{escape(str(LEVEL_NAMES.get(level)))}</small></div></div>
    <nav class="side-list" aria-label="Sections du serveur"><span class="side-ind"></span>{nav}</nav>
    <div class="side-sep"></div>
    <div class="side-links">{extra}</div>
  </aside>
  <div class="stage">{flash}{panels}</div>
</div>
<script>{GUILD_JS.replace('__GID__', str(gid))}</script>"""
    return page(guild.name, body, session, wide=True)


def back(gid: int, ok: str = "", error: str = "") -> web.Response:
    query = urlencode({"ok": ok} if ok else {"error": error})
    raise web.HTTPFound(f"/guild/{gid}?{query}")


async def act_say(request: web.Request) -> web.Response:
    session, guild, member, level = await guild_context(request)
    if level < 2:
        back(guild.id, error="Accès refusé.")
    data = await request.post()
    channel = guild.get_channel(int(str(data.get("channel", "0")) or 0))
    content = str(data.get("content", "")).strip()[:1900]
    if channel is None or not content:
        back(guild.id, error="Salon ou message invalide.")
    try:
        await channel.send(content)
    except Exception as exc:
        back(guild.id, error=f"Envoi impossible ({type(exc).__name__}).")
    back(guild.id, ok=f"Message envoyé dans #{channel.name}.")


async def resolve_member(guild: Any, raw: str) -> Optional[discord.Member]:
    raw = raw.strip().strip("<@!>")
    if raw.isdigit():
        return guild.get_member(int(raw))
    lowered = raw.lower()
    for m in guild.members:
        if lowered in (m.name.lower(), m.display_name.lower()):
            return m
    return None


async def act_moderate(request: web.Request) -> web.Response:
    session, guild, member, level = await guild_context(request)
    if level < 2:
        back(guild.id, error="Accès refusé.")
    data = await request.post()
    action = str(data.get("action", ""))
    reason = f"{str(data.get('reason', '')).strip()[:300]} — par {member} (site Lulyo)"
    raw = str(data.get("target", "")).strip()
    try:
        minutes = max(1, min(40320, int(str(data.get("minutes", "10")) or 10)))
    except ValueError:
        minutes = 10

    if action == "unban":
        if not raw.isdigit():
            back(guild.id, error="Pour débannir, donne l'identifiant numérique.")
        try:
            await guild.unban(discord.Object(id=int(raw)), reason=reason)
        except Exception as exc:
            back(guild.id, error=f"Débannissement impossible ({type(exc).__name__}).")
        back(guild.id, ok=f"Identifiant {raw} débanni.")

    target = await resolve_member(guild, raw)
    if target is None:
        back(guild.id, error="Membre introuvable.")
    if target.id == member.id:
        back(guild.id, error="Tu ne peux pas te sanctionner toi-même.")
    target_level = await level_of(target, guild)
    if target_level >= level:
        back(guild.id, error="Ce membre a un niveau égal ou supérieur au tien.")
    if target.top_role >= guild.me.top_role:
        back(guild.id, error="Mon rôle est trop bas pour agir sur ce membre.")
    try:
        if action == "kick":
            await target.kick(reason=reason)
            done = f"{target} expulsé."
        elif action == "ban":
            await target.ban(reason=reason, delete_message_days=0)
            done = f"{target} banni."
        elif action == "untimeout":
            await target.timeout(None, reason=reason)
            done = f"Exclusion de {target} levée."
        else:
            from datetime import timedelta
            await target.timeout(timedelta(minutes=minutes), reason=reason)
            done = f"{target} exclu {minutes} min."
    except discord.Forbidden:
        back(guild.id, error="Discord refuse : permissions du bot insuffisantes.")
    except Exception as exc:
        back(guild.id, error=f"Action impossible ({type(exc).__name__}).")
    back(guild.id, ok=done)


def radio_bot_handles(channel: Any) -> bool:
    """Un bot radio dédié (TOKEN_RADIO) peut-il diffuser dans ce salon ?"""
    try:
        return voice_bot is not None and voice_bot.radio_handles(channel)
    except Exception:
        return False


async def radio_bot_stop(guild: Any) -> bool:
    """Coupe la radio du bot dédié s'il en diffusait une."""
    try:
        return bool(voice_bot is not None and await voice_bot.radio_stop(guild))
    except Exception:
        return False


async def act_radio(request: web.Request) -> web.Response:
    session, guild, member, level = await guild_context(request)
    if level < 1:
        back(guild.id, error="Réservé aux DJ.")
    data = await request.post()
    channel = guild.get_channel(int(str(data.get("voice", "0")) or 0))
    if channel is None:
        back(guild.id, error="Salon vocal invalide.")
    url = str(data.get("url", "")).strip()
    station = resolve_station(url or str(data.get("station", "1")))
    if station is None:
        back(guild.id, error="Station inconnue.")
    if radio_bot_handles(channel):
        ok, problem = await voice_bot.radio_play(channel, station["url"])
        if not ok:
            back(guild.id, error=f"Radio impossible : {problem}")
    else:
        try:
            client = guild.voice_client
            if client is None:
                client = await channel.connect(self_deaf=True)
            elif client.channel != channel:
                await client.move_to(channel)
            if client.is_playing() or client.is_paused():
                client.stop()
            client.play(discord.FFmpegOpusAudio(station["url"], executable=ffmpeg_path(),
                                                before_options=FFMPEG_BEFORE, options="-vn"))
        except Exception as exc:
            back(guild.id, error=f"Lecture impossible ({type(exc).__name__}). Vérifie FFmpeg.")
    STATE.radio_now[guild.id] = station
    back(guild.id, ok=f"{station['nom']} en direct dans {channel.name}.")


async def act_radio_stop(request: web.Request) -> web.Response:
    session, guild, member, level = await guild_context(request)
    if level < 1:
        back(guild.id, error="Réservé aux DJ.")
    await radio_bot_stop(guild)
    client = guild.voice_client
    if client is not None:
        client.stop()
        await client.disconnect(force=False)
    STATE.radio_now.pop(guild.id, None)
    back(guild.id, ok="Radio arrêtée.")




# --------------------------------------------------------------------------- #
#  Messages privés et annonces
# --------------------------------------------------------------------------- #
async def act_dm(request: web.Request) -> web.Response:
    session, guild, member, level = await guild_context(request)
    if level < 2:
        back(guild.id, error="Accès refusé.")
    data = await request.post()
    target = await resolve_member(guild, str(data.get("target", "")))
    content = str(data.get("content", "")).strip()[:1800]
    title = str(data.get("title", "")).strip()[:250] or "✉️ Message du staff"
    if target is None or not content:
        back(guild.id, error="Destinataire ou message invalide.")
    if target.bot:
        back(guild.id, error="On n'écrit pas à un bot.")
    embed = discord.Embed(title=title, description=content, color=0x5865F2,
                          timestamp=utcnow())
    embed.set_author(name=f"{member.display_name} · {guild.name}",
                     icon_url=member.display_avatar.url)
    embed.set_footer(text="Réponds sur le serveur : ce message vient du site Lulyo.")
    try:
        await target.send(embed=embed)
    except discord.Forbidden:
        back(guild.id, error=f"{target.display_name} refuse les messages privés.")
    except Exception as exc:
        back(guild.id, error=f"Envoi impossible ({type(exc).__name__}).")
    back(guild.id, ok=f"MP envoyé à {target.display_name}.")


async def act_dm_role(request: web.Request) -> web.Response:
    session, guild, member, level = await guild_context(request)
    if level < 3:
        back(guild.id, error="Réservé aux administrateurs.")
    data = await request.post()
    role = guild.get_role(int(str(data.get("role", "0")) or 0))
    content = str(data.get("content", "")).strip()[:1800]
    if role is None or not content:
        back(guild.id, error="Rôle ou message invalide.")
    embed = discord.Embed(title=f"📣 Message à @{role.name}", description=content,
                          color=role.color.value or 0x5865F2, timestamp=utcnow())
    embed.set_author(name=f"{member.display_name} · {guild.name}",
                     icon_url=member.display_avatar.url)
    sent = refused = 0
    for target in list(role.members):
        if target.bot:
            continue
        try:
            await target.send(embed=embed)
            sent += 1
        except Exception:
            refused += 1
        await asyncio.sleep(1.0)
    back(guild.id, ok=f"Envoyé à {sent} membre(s) · {refused} MP fermés.")


BUTTON_STYLES = {
    "blurple": discord.ButtonStyle.primary,
    "gris": discord.ButtonStyle.secondary,
    "vert": discord.ButtonStyle.success,
    "rouge": discord.ButtonStyle.danger,
}
ROLE_PREFIX = "lulyo:role:"


def clean_emoji(raw: str):
    """Accepte un émoji unicode ou personnalisé (<:nom:id>, <a:nom:id>, :nom:).

    Retourne None si c'est inutilisable : Discord refuse tout le message quand
    un émoji est invalide, mieux vaut publier le bouton sans émoji.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("<"):
        # forme stricte : <:nom:123456789> ou <a:nom:123456789>
        match = re.fullmatch(r"<(a?):([A-Za-z0-9_]{2,32}):(\d{15,25})>", raw)
        if not match:
            return None  # mention de rôle / d'utilisateur collée par erreur, etc.
        return discord.PartialEmoji(name=match.group(2), id=int(match.group(3)),
                                    animated=bool(match.group(1)))
    if raw.startswith(":") and raw.endswith(":"):
        # « :nom: » n'est pas exploitable par l'API, on cherche l'émoji du serveur
        return None
    # émoji unicode : 1 à 8 caractères, aucune lettre ni chiffre ASCII
    if len(raw) > 8 or re.search(r"[A-Za-z0-9]", raw):
        return None
    try:
        return discord.PartialEmoji(name=raw)
    except Exception:
        return None


def build_view(guild, items: list) -> "Optional[discord.ui.View]":
    """Boutons d'annonce : lien externe, raccourci vers un salon, ou rôle cliquable."""
    view = discord.ui.View(timeout=None)
    count = 0
    for item in items[:25]:
        label = str(item.get("label", "")).strip()[:80]
        kind = str(item.get("kind", "link"))
        target = str(item.get("target", "")).strip()
        raw_emoji = str(item.get("emoji", "")).strip()[:64]
        emoji = clean_emoji(raw_emoji)
        if emoji is None and raw_emoji.startswith(":") and raw_emoji.endswith(":"):
            wanted = raw_emoji.strip(":").lower()
            emoji = discord.utils.find(
                lambda e: e.name.lower() == wanted, getattr(guild, "emojis", ()))
        if not label:
            continue
        try:
            if kind == "link":
                if not target.startswith(("http://", "https://")):
                    continue
                button = discord.ui.Button(label=label, url=target, emoji=emoji,
                                           style=discord.ButtonStyle.link)
            elif kind == "channel":
                channel = guild.get_channel(int(target or 0))
                if channel is None:
                    continue
                button = discord.ui.Button(
                    label=label, emoji=emoji, style=discord.ButtonStyle.link,
                    url=f"https://discord.com/channels/{guild.id}/{channel.id}")
            elif kind == "role":
                role = guild.get_role(int(target or 0))
                if role is None:
                    continue
                style = BUTTON_STYLES.get(str(item.get("style", "blurple")),
                                          discord.ButtonStyle.primary)
                button = discord.ui.Button(label=label, emoji=emoji, style=style,
                                           custom_id=f"{ROLE_PREFIX}{role.id}")
            else:
                continue
        except (ValueError, TypeError):
            continue
        try:
            view.add_item(button)
        except Exception:
            log.debug("bouton ignoré : %s", label, exc_info=True)
            continue
        count += 1
    return view if count else None


async def on_role_button(interaction: discord.Interaction) -> None:
    """Clic sur un bouton de rôle — fonctionne même après un redémarrage du bot."""
    if interaction.type is not discord.InteractionType.component:
        return
    custom_id = (interaction.data or {}).get("custom_id", "")
    if not custom_id.startswith(ROLE_PREFIX) or interaction.guild is None:
        return
    try:
        role_id = int(custom_id[len(ROLE_PREFIX):])
    except ValueError:
        return
    role = interaction.guild.get_role(role_id)
    member = interaction.user
    if role is None or not isinstance(member, discord.Member):
        return await interaction.response.send_message("Rôle introuvable.", ephemeral=True)
    me = interaction.guild.me
    if role >= me.top_role or role.managed or not me.guild_permissions.manage_roles:
        return await interaction.response.send_message(
            "Je ne peux pas gérer ce rôle (il est au-dessus du mien).", ephemeral=True)
    try:
        if role in member.roles:
            await member.remove_roles(role, reason="Bouton Lulyo")
            await interaction.response.send_message(f"Rôle **{role.name}** retiré.",
                                                    ephemeral=True)
        else:
            await member.add_roles(role, reason="Bouton Lulyo")
            await interaction.response.send_message(f"Rôle **{role.name}** ajouté !",
                                                    ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("Permission refusée par Discord.",
                                                ephemeral=True)
    except Exception:
        log.debug("bouton de rôle", exc_info=True)


ANNOUNCE_JS = r"""
const CHANNELS = __CHANNELS__, ROLES = __ROLES__, EXISTING = __EXISTING__;
const COLORS = {blurple:'#5865f2', gris:'#4e5058', vert:'#248046', rouge:'#da373c'};
const opts = (list) => list.map(o => `<option value="${o[0]}">${o[1]}</option>`).join('');

function addRow(data){
  data = data || {};
  const div = document.createElement('div');
  div.className = 'btnrow';
  div.innerHTML = `
    <div class="bgrid">
      <div><label>Texte du bouton</label>
        <input class="b-label" maxlength="80" placeholder="Nos règles"></div>
      <div><label>Émoji (facultatif)</label>
        <input class="b-emoji" maxlength="64" placeholder="📘 ou <:nom:123456789>"></div>
      <div><label>Action</label>
        <select class="b-kind">
          <option value="link">🔗 Ouvrir un lien</option>
          <option value="channel">#️⃣ Aller dans un salon</option>
          <option value="role">🏷️ Donner / retirer un rôle</option>
        </select></div>
      <div class="w-url"><label>Adresse</label>
        <input class="b-url" placeholder="https://exemple.fr"></div>
      <div class="w-chan" style="display:none"><label>Salon</label>
        <select class="b-chan">${opts(CHANNELS)}</select></div>
      <div class="w-role" style="display:none"><label>Rôle</label>
        <select class="b-role">${opts(ROLES)}</select></div>
      <div class="w-style" style="display:none"><label>Couleur</label>
        <select class="b-style">
          <option value="blurple">Bleu (principal)</option>
          <option value="gris">Gris</option>
          <option value="vert">Vert</option>
          <option value="rouge">Rouge</option>
        </select></div>
      <div style="display:flex;align-items:flex-end">
        <button type="button" class="btn ghost b-del">🗑️ Retirer</button></div>
    </div>`;
  document.getElementById('buttons').appendChild(div);
  const kind = div.querySelector('.b-kind');
  if (data.label !== undefined){        // bouton existant : on le réaffiche
    div.querySelector('.b-label').value = data.label || '';
    div.querySelector('.b-emoji').value = data.emoji || '';
    kind.value = data.kind || 'link';
    if (data.kind === 'link') div.querySelector('.b-url').value = data.target || '';
    if (data.kind === 'channel') div.querySelector('.b-chan').value = data.target || '';
    if (data.kind === 'role') div.querySelector('.b-role').value = data.target || '';
    div.querySelector('.b-style').value = data.style || 'blurple';
  }
  const sync = () => {
    const k = kind.value;
    div.querySelector('.w-url').style.display = k === 'link' ? 'block' : 'none';
    div.querySelector('.w-chan').style.display = k === 'channel' ? 'block' : 'none';
    div.querySelector('.w-role').style.display = k === 'role' ? 'block' : 'none';
    div.querySelector('.w-style').style.display = k === 'role' ? 'block' : 'none';
    preview();
  };
  kind.addEventListener('change', sync);
  div.querySelector('.b-del').addEventListener('click', () => { div.remove(); preview(); });
  div.querySelectorAll('input,select').forEach(el => el.addEventListener('input', preview));
  sync();
}

function collect(){
  return [...document.querySelectorAll('.btnrow')].map(div => {
    const k = div.querySelector('.b-kind').value;
    return {
      label: div.querySelector('.b-label').value,
      emoji: div.querySelector('.b-emoji').value,
      kind: k,
      style: div.querySelector('.b-style').value,
      target: k === 'link' ? div.querySelector('.b-url').value
            : k === 'channel' ? div.querySelector('.b-chan').value
            : div.querySelector('.b-role').value
    };
  }).filter(b => b.label.trim());
}

function val(name){ const e = document.querySelector('[name=' + name + ']'); return e ? e.value : ''; }

function pings(){
  const out = [];
  const g = document.querySelector('[name=ping]');
  if (g && g.value) out.push('@' + g.value);
  document.querySelectorAll('input[name=ping_roles]:checked').forEach(el => {
    const label = el.closest('.rolepick');
    if (label) out.push(label.querySelector('span:last-of-type').textContent);
  });
  return out;
}

function preview(){
  const pv = document.getElementById('pv-ping');
  const list = pings();
  pv.innerHTML = list.map(p => `<span class="pvping">${p}</span>`).join(' ');
  pv.style.display = list.length ? 'block' : 'none';
  document.getElementById('pv-bar').style.background = val('color');
  document.getElementById('pv-title').textContent = val('title');
  document.getElementById('pv-desc').textContent = val('description') || 'Ton texte apparaîtra ici.';
  document.getElementById('pv-foot').textContent = val('footer');
  const img = val('image'), pvi = document.getElementById('pv-img');
  pvi.style.display = img ? 'block' : 'none'; if (img) pvi.src = img;
  document.getElementById('pv-btns').innerHTML = collect().map(b => {
    const isLink = b.kind !== 'role';
    const bg = isLink ? '#4e5058' : (COLORS[b.style] || '#5865f2');
    const txt = b.label.replace(/[<>&]/g, '');
    return `<span class="pvb" style="background:${bg}">${b.emoji || ''} ${txt}${isLink ? ' ↗' : ''}</span>`;
  }).join('');
  document.querySelector('[name=buttons_json]').value = JSON.stringify(collect());
}

const search = document.getElementById('rolesearch');
if (search) search.addEventListener('input', e => {
  const q = e.target.value.toLowerCase();
  document.querySelectorAll('#rolebox .rolepick').forEach(el => {
    el.style.display = el.textContent.toLowerCase().includes(q) ? 'flex' : 'none';
  });
});
document.querySelectorAll('input[name=ping_roles]').forEach(el =>
  el.addEventListener('change', preview));

document.getElementById('add').addEventListener('click', () => addRow());
(EXISTING || []).forEach(b => addRow(b));
document.querySelectorAll('#form input,#form select,#form textarea')
  .forEach(el => el.addEventListener('input', preview));
document.getElementById('form').addEventListener('submit', preview);
preview();
"""

PING_BLOCK = ('<div><label>Mention générale</label><select name="ping">'
              '<option value="">Aucune</option>'
              '<option value="everyone">@everyone</option>'
              '<option value="here">@here</option></select></div>')


def roles_picker(guild: Any) -> str:
    """Cases à cocher pour mentionner autant de rôles que voulu."""
    roles = [r for r in sorted(guild.roles, key=lambda r: -r.position)
             if not r.is_default()]
    if not roles:
        return ""
    items = "".join(
        f'<label class="rolepick"><input type="checkbox" name="ping_roles" '
        f'value="{r.id}"><span class="dot" style="background:'
        f'{"#" + format(r.color.value, "06x") if r.color.value else "#99aab5"}"></span>'
        f'<span>@{escape(r.name)}</span>'
        f'<small>{len(getattr(r, "members", ()))}</small></label>'
        for r in roles)
    return f"""
<label style="margin-top:6px">Rôles à mentionner (autant que tu veux)</label>
<input id="rolesearch" placeholder="Filtrer les rôles…" style="margin-bottom:8px">
<div class="rolebox" id="rolebox">{items}</div>
<p class="sub" style="color:#79829a;margin-top:6px">Les rôles cochés sont mentionnés au
 début du message. Un rôle non mentionnable par les membres l'est quand même : c'est le
 bot qui écrit.</p>"""


def parse_message_ref(raw: str) -> tuple[Optional[int], Optional[int]]:
    """Accepte un lien de message, « salon-message » ou un identifiant seul."""
    text = (raw or "").strip()
    if not text:
        return None, None
    lien = re.search(r"/channels/\d+/(\d+)/(\d+)", text)
    if lien:
        return int(lien.group(1)), int(lien.group(2))
    couple = re.fullmatch(r"(\d{2,25})\s*[-\s/]\s*(\d{2,25})", text)
    if couple:
        return int(couple.group(1)), int(couple.group(2))
    if text.isdigit():
        return None, int(text)
    return None, None


def view_to_rows(message: Any) -> list[dict]:
    """Retrouve les boutons d'un message pour les réafficher dans l'éditeur."""
    rows = []
    for line in getattr(message, "components", []) or []:
        for item in getattr(line, "children", []) or []:
            if getattr(item, "type", None) != discord.ComponentType.button:
                continue
            emoji = str(item.emoji) if getattr(item, "emoji", None) else ""
            url = getattr(item, "url", "") or ""
            custom = getattr(item, "custom_id", "") or ""
            if url and "/channels/" in url:
                kind, target = "channel", url.rstrip("/").split("/")[-1]
            elif url:
                kind, target = "link", url
            elif custom.startswith(ROLE_PREFIX):
                kind, target = "role", custom[len(ROLE_PREFIX):]
            else:
                continue
            style = {discord.ButtonStyle.primary: "blurple",
                     discord.ButtonStyle.secondary: "gris",
                     discord.ButtonStyle.success: "vert",
                     discord.ButtonStyle.danger: "rouge"}.get(
                         getattr(item, "style", None), "blurple")
            rows.append({"label": item.label or "", "emoji": emoji, "kind": kind,
                         "target": target, "style": style})
    return rows


async def load_message(guild: Any, channel_id: Optional[int],
                       message_id: int) -> tuple[Any, Any]:
    """Retrouve un message dans le salon donné, ou en cherchant partout."""
    salons = []
    if channel_id:
        found = guild.get_channel(int(channel_id))
        if found is not None:
            salons.append(found)
    if not salons:
        salons = list(guild.text_channels)[:60]
    for channel in salons:
        try:
            return channel, await channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden):
            continue
        except Exception:
            continue
    return None, None


async def page_announce(request: web.Request) -> web.Response:
    session, guild, member, level = await guild_context(request)
    if level < 2:
        back(guild.id, error="Accès refusé.")
    error, notice = request.query.get("error", ""), request.query.get("ok", "")
    channels = [[str(c.id), f"#{c.name}"] for c in text_channels(guild)]
    all_channels = [[str(c.id), f"#{c.name}"] for c in guild.text_channels]
    roles = [[str(r.id), f"@{r.name}"]
             for r in sorted(guild.roles, key=lambda r: -r.position)
             if not r.is_default() and not r.managed and r < guild.me.top_role]
    # ----- modification d'un message déjà publié -------------------------- #
    edit_ref = request.query.get("edit", "").strip()
    edit_channel, edit_id = parse_message_ref(edit_ref)
    courant, message_edite, boutons_existants = {}, None, []
    if edit_id:
        salon_trouve, message_edite = await load_message(guild, edit_channel, edit_id)
        if message_edite is None:
            error = error or ("Message introuvable. Donne son lien, ou vérifie que je "
                              "peux lire ce salon.")
            edit_id = None
        else:
            edit_channel = salon_trouve.id
            embed_source = message_edite.embeds[0] if message_edite.embeds else None
            courant = {
                "titre": (embed_source.title if embed_source else "") or "",
                "description": ((embed_source.description if embed_source else "")
                                or message_edite.content or ""),
                "couleur": (f"#{embed_source.color.value:06x}"
                            if embed_source and embed_source.color else "#5865f2"),
                "image": (embed_source.image.url if embed_source and embed_source.image
                          else ""),
                "thumbnail": (embed_source.thumbnail.url if embed_source
                              and embed_source.thumbnail else ""),
                "footer": (getattr(embed_source.footer, "text", "")
                           if embed_source else "") or "",
            }
            boutons_existants = view_to_rows(message_edite)

    options = "".join(
        f'<option value="{c[0]}"'
        f'{" selected" if edit_channel and str(edit_channel) == c[0] else ""}>'
        f'{escape(c[1])}</option>' for c in channels)
    ping = PING_BLOCK if level >= 3 else ""
    script = (ANNOUNCE_JS
              .replace("__CHANNELS__", json.dumps(all_channels, ensure_ascii=False))
              .replace("__ROLES__", json.dumps(roles, ensure_ascii=False))
              .replace("__EXISTING__", json.dumps(boutons_existants, ensure_ascii=False)))
    body = f"""
<h2 class="section">{'✏️ Modifier une annonce' if edit_id else '📢 Nouvelle annonce'} —
  {escape(guild.name)}</h2>
<div class="card" style="margin-bottom:16px">
  <form method="get" style="display:flex;gap:10px;flex-wrap:wrap;align-items:end">
    <div style="flex:1;min-width:260px"><label>Modifier un message déjà publié</label>
      <input name="edit" value="{escape(edit_ref)}"
             placeholder="Colle le lien du message (clic droit → Copier le lien)"></div>
    <button class="btn ghost">Charger</button>
    {'<a class="btn ghost" href="/guild/' + str(guild.id) + '/annonce">Nouvelle annonce</a>' if edit_id else ''}
  </form>
  <p class="sub" style="color:var(--mute);font-size:.85rem;margin-top:8px">
    Le texte, l'embed <b>et les boutons</b> sont rechargés : tu peux tout corriger,
    ajouter ou retirer des boutons, puis réenregistrer.</p>
</div>
{f'<div class="msg err">{escape(error)}</div>' if error else ''}
{f'<div class="msg ok">{escape(notice)}</div>' if notice else ''}
<div class="annwrap">
  <form id="form" method="post" action="/guild/{guild.id}/annonce" class="card">
    <input type="hidden" name="edit_message" value="{edit_id or ''}">
    <input type="hidden" name="edit_channel" value="{edit_channel or ''}">
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(190px,1fr))">
      <div><label>Salon de publication</label><select name="channel">{options}</select></div>
      <div><label>Couleur de l'embed</label>
        <input name="color" type="color" value="{escape(courant.get('couleur', '#5865f2'))}"
             style="height:46px;padding:4px"></div>
      {ping}
    </div>
    {roles_picker(guild)}
    <label>Titre</label><input name="title" maxlength="250" placeholder="📢 Annonce importante" value="{escape(courant.get('titre', ''))}">
    <label>Texte — Entrée pour aller à la ligne</label>
    <textarea name="description" rows="7" maxlength="3800" required
      placeholder="Première ligne&#10;Deuxième ligne&#10;&#10;**gras**, *italique*, [lien](https://…)">{escape(courant.get('description', ''))}</textarea>
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(190px,1fr))">
      <div><label>Image (URL)</label><input name="image" placeholder="https://…" value="{escape(courant.get('image', ''))}"></div>
      <div><label>Vignette (URL)</label><input name="thumbnail" placeholder="https://…" value="{escape(courant.get('thumbnail', ''))}"></div>
      <div><label>Bas de page</label><input name="footer" placeholder="Équipe du serveur" value="{escape(courant.get('footer', ''))}"></div>
    </div>

    <h3 style="margin:24px 0 10px">🔘 Boutons</h3>
    <div id="buttons"></div>
    <button type="button" id="add" class="btn ghost">➕ Ajouter un bouton</button>
    <input type="hidden" name="buttons_json" value="[]">
    <p class="sub" style="color:#79829a;margin-top:10px">Jusqu'à 25 boutons. Discord impose
      le gris aux boutons « lien » et « salon » ; la couleur ne s'applique qu'aux boutons de
      rôle, où un clic ajoute le rôle — et un second clic le retire.<br>
      Émoji : un caractère unicode (📘) ou un émoji personnalisé complet
      (<code>&lt;:nom:123456789&gt;</code>, obtenu en tapant <code>\\:nom:</code> dans
      Discord). Un émoji non reconnu est simplement ignoré, l'annonce part quand même.</p>

    <button class="btn" style="margin-top:20px">
      {'Enregistrer les modifications' if edit_id else "Publier l'annonce"}</button>
    <a class="btn ghost" href="/guild/{guild.id}" style="margin-top:20px">Retour</a>
  </form>

  <div class="card" style="align-self:start">
    <h3 style="margin-bottom:12px">👁️ Aperçu en direct</h3>
    <div id="pv-ping" style="display:none;margin-bottom:8px"></div>
    <div class="pv"><div class="pv-bar" id="pv-bar"></div>
      <div class="pv-body">
        <div class="pv-title" id="pv-title"></div>
        <div class="pv-desc" id="pv-desc"></div>
        <img id="pv-img" style="display:none" alt="">
        <div class="pv-foot" id="pv-foot"></div>
      </div></div>
    <div id="pv-btns" class="pv-btns"></div>
  </div>
</div>
<script>{script}</script>"""
    return page("Annonce", body, session, wide=True)


async def act_announce(request: web.Request) -> web.Response:
    session, guild, member, level = await guild_context(request)
    if level < 2:
        back(guild.id, error="Accès refusé.")
    data = await request.post()
    channel = guild.get_channel(int(str(data.get("channel", "0")) or 0))
    description = str(data.get("description", "")).replace("\\n", "\n").strip()[:3800]
    if channel is None or not description:
        raise web.HTTPFound(f"/guild/{guild.id}/annonce?error=Salon ou texte invalide.")
    try:
        color = int(str(data.get("color", "#5865f2")).lstrip("#"), 16)
    except ValueError:
        color = 0x5865F2
    embed = discord.Embed(description=description, color=color, timestamp=utcnow())
    title = str(data.get("title", "")).strip()[:250]
    if title:
        embed.title = title
    ignored = []
    for key, setter in (("image", embed.set_image), ("thumbnail", embed.set_thumbnail)):
        url = str(data.get(key, "")).strip()
        if not url:
            continue
        if url.startswith(("http://", "https://")):
            setter(url=url)
        else:
            ignored.append(key)
    footer = str(data.get("footer", "")).strip()[:2000]
    embed.set_footer(text=footer or f"Publié par {member.display_name}",
                     icon_url=guild.icon.url if guild.icon else None)
    ping = str(data.get("ping", "")) if level >= 3 else ""
    mention_all = {"everyone": "@everyone", "here": "@here"}.get(ping, "")

    wanted, pinged = [], []
    for raw in data.getall("ping_roles", []):
        try:
            role = guild.get_role(int(raw))
        except (TypeError, ValueError):
            continue
        if role is not None and not role.is_default():
            wanted.append(role)
            pinged.append(f"<@&{role.id}>")

    parts = [p for p in ([mention_all] + pinged) if p]
    content = " ".join(parts)[:1900]
    allowed = discord.AllowedMentions(everyone=bool(mention_all), users=False,
                                      roles=wanted if wanted else False)
    try:
        items = json.loads(str(data.get("buttons_json", "[]")) or "[]")
        if not isinstance(items, list):
            items = []
    except Exception:
        items = []
    view = build_view(guild, items)

    # ----- modification d'un message existant ----------------------------- #
    edit_id = str(data.get("edit_message", "")).strip()
    if edit_id.isdigit():
        salon_edit = str(data.get("edit_channel", "")).strip()
        cible, ancien = await load_message(
            guild, int(salon_edit) if salon_edit.isdigit() else None, int(edit_id))
        if ancien is None:
            raise web.HTTPFound(f"/guild/{guild.id}/annonce?error=Message introuvable.")
        if ancien.author.id != guild.me.id:
            raise web.HTTPFound(
                f"/guild/{guild.id}/annonce?error=Je ne peux modifier que mes propres "
                f"messages.")
        try:
            await ancien.edit(content=content or None, embed=embed,
                              view=view, allowed_mentions=allowed)
        except discord.HTTPException as exc:
            detail = (getattr(exc, "text", "") or str(exc)).replace("\n", " ")[:180]
            raise web.HTTPFound(
                f"/guild/{guild.id}/annonce?edit={edit_id}&error=Discord a refusé : {detail}")
        count = len(view.children) if view else 0
        raise web.HTTPFound(
            f"/guild/{guild.id}/annonce?edit={edit_id}"
            f"&ok=Message mis à jour dans #{cible.name} ({count} bouton(s)).")

    try:
        await channel.send(content=content or None, embed=embed, view=view,
                           allowed_mentions=allowed)
    except discord.Forbidden:
        raise web.HTTPFound(
            f"/guild/{guild.id}/annonce?error=Le bot ne peut pas écrire dans ce salon.")
    except discord.HTTPException as exc:
        detail = (getattr(exc, "text", "") or str(exc)).replace("\n", " ")[:180]
        raise web.HTTPFound(
            f"/guild/{guild.id}/annonce?error=Discord a refusé l'annonce : {detail}")
    except Exception as exc:
        raise web.HTTPFound(
            f"/guild/{guild.id}/annonce?error=Publication impossible "
            f"({type(exc).__name__} : {str(exc)[:140]})")
    count = len(view.children) if view else 0
    extra = (" — adresse invalide ignorée : " + ", ".join(ignored)) if ignored else ""
    if wanted:
        extra += f" — {len(wanted)} rôle(s) mentionné(s)"
    raise web.HTTPFound(
        f"/guild/{guild.id}/annonce?ok=Annonce publiée dans #{channel.name} "
        f"avec {count} bouton(s).{extra}")


# --------------------------------------------------------------------------- #
#  Client Discord web
# --------------------------------------------------------------------------- #
WEBHOOK_NAME = "Lulyo Web"
_webhooks: dict[int, Any] = {}


async def channel_webhook(channel: Any) -> Optional[Any]:
    """Webhook réutilisé pour parler « à la place » d'un membre."""
    hook = _webhooks.get(channel.id)
    if hook is not None:
        return hook
    me = channel.guild.me
    if not channel.permissions_for(me).manage_webhooks:
        return None
    try:
        for existing in await channel.webhooks():
            if existing.name == WEBHOOK_NAME and existing.token:
                _webhooks[channel.id] = existing
                return existing
        hook = await channel.create_webhook(name=WEBHOOK_NAME,
                                            reason="Client web Lulyo")
        _webhooks[channel.id] = hook
        return hook
    except Exception:
        return None


def visible_channels(guild: Any, member: Any) -> list:
    """Salons écrits que CE membre a le droit de voir."""
    out = []
    for channel in guild.text_channels:
        perms = channel.permissions_for(member)
        if perms.view_channel and perms.read_message_history:
            out.append(channel)
    out.sort(key=lambda c: ((c.category.position if c.category else -1), c.position))
    return out


async def api_client_channels(request: web.Request) -> web.Response:
    session, guild, member, level = await guild_context(request)
    data = []
    for channel in visible_channels(guild, member):
        perms = channel.permissions_for(member)
        data.append({"id": str(channel.id), "name": channel.name,
                     "category": channel.category.name if channel.category else "",
                     "topic": (channel.topic or "")[:120],
                     "can_send": bool(perms.send_messages),
                     "nsfw": bool(channel.is_nsfw())})
    return web.json_response({
        "guild": {"id": str(guild.id), "name": guild.name,
                  "icon": guild.icon.url if guild.icon else ""},
        "me": {"name": member.display_name, "avatar": member.display_avatar.url,
               "level": LEVEL_NAMES.get(level, "Membre"), "staff": level >= 2},
        "channels": data})


def serialize_message(message: Any) -> dict:
    """Message prêt à afficher : mentions résolues, embeds et fichiers détaillés."""
    embeds = []
    for embed in message.embeds[:3]:
        embeds.append({
            "author": getattr(embed.author, "name", None),
            "title": embed.title,
            "description": (embed.description or "")[:800],
            "color": f"#{embed.color.value:06x}" if embed.color else "#5865f2",
            "image": (embed.image.url if embed.image else
                      (embed.thumbnail.url if embed.thumbnail else None)),
            "footer": getattr(embed.footer, "text", None),
            "fields": [[f.name, (f.value or "")[:200]] for f in embed.fields[:5]],
        })
    files = []
    for att in message.attachments[:4]:
        ctype = (att.content_type or "")
        files.append({"url": att.url, "name": att.filename,
                      "image": ctype.startswith("image/")})
    try:
        text = message.clean_content
    except Exception:
        text = message.content or ""
    return {
        "id": str(message.id),
        "author": message.author.display_name,
        "avatar": message.author.display_avatar.url,
        "bot": bool(message.author.bot),
        "content": text or "",
        "time": message.created_at.astimezone().strftime("%d/%m %H:%M"),
        "attachments": files,
        "embeds": embeds,
    }


async def api_client_messages(request: web.Request) -> web.Response:
    session, guild, member, level = await guild_context(request)
    channel = guild.get_channel(int(request.match_info["cid"]))
    if channel is None or channel not in visible_channels(guild, member):
        return web.json_response({"error": "Salon inaccessible."}, status=403)
    try:
        limit = max(1, min(80, int(request.query.get("limit", "50"))))
    except ValueError:
        limit = 50
    rows = await STATE.db.fetchall(
        "SELECT message_id, user_id FROM web_messages WHERE channel_id = ?", (channel.id,))
    owners = {}
    for row in rows:
        item = row_dict(row) or {}
        owners[int(item.get("message_id", 0))] = int(item.get("user_id", 0))
    out = []
    try:
        async for message in channel.history(limit=limit):
            item = serialize_message(message)
            item["mine"] = owners.get(message.id) == member.id
            item["web"] = message.id in owners
            out.append(item)
    except discord.Forbidden:
        return web.json_response({"error": "Le bot ne peut pas lire ce salon."}, status=403)
    out.reverse()
    return web.json_response({"messages": out})


MAX_UPLOAD = 8 * 1024 * 1024  # 8 Mo, limite Discord sans boost


async def remember_message(message_id: int, guild_id: int, channel_id: int,
                           user_id: int, webhook_id: Optional[int], as_bot: bool) -> None:
    await STATE.db.execute(
        "INSERT OR REPLACE INTO web_messages(message_id, guild_id, channel_id, user_id, "
        "webhook_id, as_bot, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
        (message_id, guild_id, channel_id, user_id, webhook_id, int(as_bot), stamp()))


async def message_owner(message_id: int) -> Optional[dict]:
    return row_dict(await STATE.db.fetchone(
        "SELECT * FROM web_messages WHERE message_id = ?", (message_id,)))


async def api_client_send(request: web.Request) -> web.Response:
    session, guild, member, level = await guild_context(request)
    channel = guild.get_channel(int(request.match_info["cid"]))
    if channel is None or channel not in visible_channels(guild, member):
        return web.json_response({"error": "Salon inaccessible."}, status=403)
    if not channel.permissions_for(member).send_messages:
        return web.json_response({"error": "Tu n'as pas le droit d'écrire ici."}, status=403)

    content, as_bot, files = "", False, []
    if request.content_type.startswith("multipart/"):
        reader = await request.multipart()
        async for part in reader:
            if part.name == "content":
                content = (await part.text()).strip()[:1900]
            elif part.name == "as_bot":
                as_bot = (await part.text()) in ("1", "true", "on")
            elif part.name == "file" and part.filename:
                data = await part.read(decode=False)
                if len(data) > MAX_UPLOAD:
                    return web.json_response(
                        {"error": "Fichier trop lourd (8 Mo maximum)."}, status=413)
                files.append(discord.File(io.BytesIO(data), filename=part.filename[:90]))
                if len(files) >= 5:
                    break
    else:
        payload = await request.json()
        content = str(payload.get("content", "")).strip()[:1900]
        as_bot = bool(payload.get("as_bot"))
    if not content and not files:
        return web.json_response({"error": "Message vide."}, status=400)
    if as_bot and level < 2:
        return web.json_response({"error": "Parler en tant que bot est réservé au staff."},
                                 status=403)

    now = time.monotonic()
    holder = STATE.sessions.get(session.get("token", ""), {})
    if now - holder.get("last_msg", 0) < 1.0:
        return web.json_response({"error": "Doucement…"}, status=429)
    holder["last_msg"] = now

    mentions = discord.AllowedMentions(
        everyone=member.guild_permissions.mention_everyone, roles=False, users=True)
    try:
        if as_bot:
            sent = await channel.send(content=content or None, files=files,
                                      allowed_mentions=mentions)
            await remember_message(sent.id, guild.id, channel.id, member.id, None, True)
        else:
            hook = await channel_webhook(channel)
            if hook is not None:
                sent = await hook.send(content=content or None, files=files,
                                       username=member.display_name[:80],
                                       avatar_url=member.display_avatar.url,
                                       allowed_mentions=mentions, wait=True)
                await remember_message(sent.id, guild.id, channel.id, member.id,
                                       hook.id, False)
            else:
                sent = await channel.send(f"**{member.display_name}** : {content}",
                                          files=files, allowed_mentions=mentions)
                await remember_message(sent.id, guild.id, channel.id, member.id, None, True)
    except discord.Forbidden:
        return web.json_response({"error": "Le bot ne peut pas écrire ici."}, status=403)
    except Exception as exc:
        return web.json_response({"error": f"Échec ({type(exc).__name__})."}, status=500)
    return web.json_response({"ok": True})


async def api_client_edit(request: web.Request) -> web.Response:
    session, guild, member, level = await guild_context(request)
    channel = guild.get_channel(int(request.match_info["cid"]))
    mid = int(request.match_info["mid"])
    if channel is None or channel not in visible_channels(guild, member):
        return web.json_response({"error": "Salon inaccessible."}, status=403)
    payload = await request.json()
    content = str(payload.get("content", "")).strip()[:1900]
    if not content:
        return web.json_response({"error": "Message vide."}, status=400)
    record = await message_owner(mid)
    mine = record is not None and record["user_id"] == member.id
    if not mine and level < 2:
        return web.json_response({"error": "Tu ne peux modifier que tes messages."},
                                 status=403)
    try:
        if record and record["webhook_id"]:
            hook = await channel_webhook(channel)
            if hook is None:
                return web.json_response({"error": "Webhook introuvable."}, status=500)
            await hook.edit_message(mid, content=content)
        else:
            message = await channel.fetch_message(mid)
            if message.author.id != guild.me.id:
                return web.json_response(
                    {"error": "Seuls les messages envoyés depuis le site sont modifiables."},
                    status=403)
            await message.edit(content=content)
    except discord.NotFound:
        return web.json_response({"error": "Message introuvable."}, status=404)
    except discord.Forbidden:
        return web.json_response({"error": "Modification refusée par Discord."}, status=403)
    except Exception as exc:
        return web.json_response({"error": f"Échec ({type(exc).__name__})."}, status=500)
    return web.json_response({"ok": True})


async def api_client_delete(request: web.Request) -> web.Response:
    session, guild, member, level = await guild_context(request)
    channel = guild.get_channel(int(request.match_info["cid"]))
    mid = int(request.match_info["mid"])
    if channel is None or channel not in visible_channels(guild, member):
        return web.json_response({"error": "Salon inaccessible."}, status=403)
    record = await message_owner(mid)
    mine = record is not None and record["user_id"] == member.id
    staff = level >= 2 and channel.permissions_for(member).manage_messages
    if not mine and not staff:
        return web.json_response({"error": "Tu ne peux supprimer que tes messages."},
                                 status=403)
    try:
        if record and record["webhook_id"] and not staff:
            hook = await channel_webhook(channel)
            if hook is None:
                return web.json_response({"error": "Webhook introuvable."}, status=500)
            await hook.delete_message(mid)
        else:
            message = await channel.fetch_message(mid)
            await message.delete()
    except discord.NotFound:
        pass
    except discord.Forbidden:
        return web.json_response(
            {"error": "Le bot n'a pas « Gérer les messages » dans ce salon."}, status=403)
    except Exception as exc:
        return web.json_response({"error": f"Échec ({type(exc).__name__})."}, status=500)
    await STATE.db.execute("DELETE FROM web_messages WHERE message_id = ?", (mid,))
    return web.json_response({"ok": True})


# ---- messages privés dans le même client ---------------------------------- #
async def api_dm_list(request: web.Request) -> web.Response:
    """Conversations privées du bot (staff uniquement)."""
    session, guild, member, level = await guild_context(request)
    if level < 2:
        return web.json_response({"error": "Réservé au staff."}, status=403)
    rows = await STATE.db.fetchall(
        "SELECT DISTINCT user_id FROM web_accounts ORDER BY last_login DESC LIMIT 0")
    seen, out = set(), []
    for channel in list(getattr(STATE.bot, "private_channels", ())):
        user = getattr(channel, "recipient", None)
        if user is None or user.id in seen:
            continue
        seen.add(user.id)
        out.append({"id": str(user.id), "name": user.display_name,
                    "avatar": user.display_avatar.url,
                    "member": guild.get_member(user.id) is not None})
    query = request.query.get("q", "").lower().strip()
    if query:
        for m in guild.members:
            if m.bot or m.id in seen:
                continue
            if query in m.display_name.lower() or query in str(m.id):
                out.append({"id": str(m.id), "name": m.display_name,
                            "avatar": m.display_avatar.url, "member": True})
                if len(out) > 40:
                    break
    return web.json_response({"conversations": out[:40]})


async def api_dm_messages(request: web.Request) -> web.Response:
    session, guild, member, level = await guild_context(request)
    if level < 2:
        return web.json_response({"error": "Réservé au staff."}, status=403)
    user = STATE.bot.get_user(int(request.match_info["uid"]))
    if user is None:
        return web.json_response({"error": "Utilisateur inconnu."}, status=404)
    channel = user.dm_channel or await user.create_dm()
    out = []
    try:
        async for message in channel.history(limit=40):
            item = serialize_message(message)
            item["mine"] = False
            item["web"] = False
            out.append(item)
    except Exception:
        pass
    out.reverse()
    return web.json_response({"messages": out, "name": user.display_name})


async def api_dm_send(request: web.Request) -> web.Response:
    session, guild, member, level = await guild_context(request)
    if level < 2:
        return web.json_response({"error": "Réservé au staff."}, status=403)
    user = STATE.bot.get_user(int(request.match_info["uid"]))
    if user is None:
        return web.json_response({"error": "Utilisateur inconnu."}, status=404)
    payload = await request.json()
    content = str(payload.get("content", "")).strip()[:1900]
    if not content:
        return web.json_response({"error": "Message vide."}, status=400)
    signature = "" if payload.get("as_bot") else f"**{member.display_name}** · {guild.name}\n"
    try:
        await user.send(f"{signature}{content}")
    except discord.Forbidden:
        return web.json_response({"error": "Cette personne refuse les MP."}, status=403)
    except Exception as exc:
        return web.json_response({"error": f"Échec ({type(exc).__name__})."}, status=500)
    return web.json_response({"ok": True})


CLIENT_JS = r"""
let GID = new URLSearchParams(location.search).get('guild') || '';
let MODE = 'chan', CID = null, UID = null, timer = null, sig = '', ME = {};
const $ = (id) => document.getElementById(id);
const esc = (t) => (t||'').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function jget(u){
  const r = await fetch(u, {headers: {'Accept': 'application/json'}});
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok || (data && data.error)) {
    throw new Error((data && data.error) || ('Erreur ' + r.status));
  }
  return data;
}

function fatal(message){
  document.getElementById('chans').innerHTML =
    `<div class="oops"><b>Impossible de charger le serveur</b><p>${message}</p>
     <a class="btn ghost" href="/dashboard">Retour au tableau de bord</a></div>`;
  document.getElementById('mename').textContent = 'Erreur';
  document.getElementById('melevel').textContent = '';
}

async function loadChannels(){
  let d;
  try { d = await jget(`/api/guild/${GID}/channels`); }
  catch (e) { fatal(e.message); return; }
  ME = d.me;
  $('gname').textContent = d.guild.name;
  $('mename').textContent = d.me.name;
  $('melevel').textContent = d.me.level;
  if (d.me.avatar) $('meavatar').src = d.me.avatar;
  $('staffbox').style.display = d.me.staff ? 'flex' : 'none';
  $('tabdm').style.display = d.me.staff ? 'block' : 'none';
  let html = '', cat = null;
  d.channels.forEach(c => {
    if (c.category !== cat){ cat = c.category;
      html += `<div class="cat">${esc(cat) || 'Salons'}</div>`; }
    html += `<div class="chan ch" data-id="${c.id}" data-send="${c.can_send?1:0}">
      <span style="color:#7d86a0">#</span><span><b>${esc(c.name)}</b></span></div>`;
  });
  $('chans').innerHTML = html ||
    '<div class="oops"><b>Aucun salon visible</b><p>Tes rôles ne te donnent accès à '
    + 'aucun salon écrit, ou le bot ne peut pas les voir.</p></div>';
  document.querySelectorAll('#chans .ch').forEach(el =>
    el.addEventListener('click', () => openChan(el)));
  const first = document.querySelector('#chans .ch');
  if (first) openChan(first);
}

async function loadDMs(q){
  let d;
  try { d = await jget(`/api/guild/${GID}/dms?q=${encodeURIComponent(q||'')}`); }
  catch (e) { fatal(e.message); return; }
  $('chans').innerHTML = (d.conversations||[]).map(c =>
    `<div class="chan dm" data-id="${c.id}"><img src="${c.avatar}" class="mini"
       onerror="this.src='/logo.png'">
     <span><b>${esc(c.name)}</b><small>${c.member ? 'membre du serveur' : 'hors serveur'}</small></span></div>`
  ).join('') || '<p class="sub">Aucune conversation. Cherche un membre ci-dessus.</p>';
  document.querySelectorAll('#chans .dm').forEach(el =>
    el.addEventListener('click', () => openDM(el)));
}

function setMode(m){
  MODE = m;
  $('tabchan').classList.toggle('on', m === 'chan');
  $('tabdm').classList.toggle('on', m === 'dm');
  $('search').style.display = m === 'dm' ? 'block' : 'none';
  $('clip').style.display = m === 'chan' ? 'inline-flex' : 'none';
  $('fname').textContent = '';
  if (timer) clearInterval(timer);
  if (m === 'chan') loadChannels(); else { loadDMs(''); $('msgs').innerHTML = ''; CID = null; }
}

function openChan(el){
  document.querySelectorAll('#chans .chan').forEach(e => e.classList.remove('on'));
  el.classList.add('on');
  CID = el.dataset.id; UID = null; MODE = 'chan';
  $('cname').textContent = '#' + el.querySelector('b').textContent;
  const can = el.dataset.send === '1';
  $('input').disabled = !can; $('send').disabled = !can;
  $('input').placeholder = can ? 'Écris ton message…' : 'Lecture seule dans ce salon';
  refresh(true);
  if (timer) clearInterval(timer);
  timer = setInterval(() => refresh(false), 3000);
}

function openDM(el){
  document.querySelectorAll('#chans .chan').forEach(e => e.classList.remove('on'));
  el.classList.add('on');
  UID = el.dataset.id; CID = null; MODE = 'dm';
  $('cname').textContent = '✉️ ' + el.querySelector('b').textContent;
  $('input').disabled = false; $('send').disabled = false;
  $('input').placeholder = 'Message privé…';
  refresh(true);
  if (timer) clearInterval(timer);
  timer = setInterval(() => refresh(false), 4000);
}

function canManage(m){ return MODE === 'chan' && (ME.staff || m.mine); }

function render(list){
  $('msgs').innerHTML = list.map(m => `
    <div class="msg2" data-id="${m.id}"><img src="${m.avatar}" alt=""
         onerror="this.src='/logo.png'">
      <div style="flex:1">
        <div class="head"><b>${esc(m.author)}</b>
          ${m.bot ? '<span class="botb">BOT</span>' : ''}
          <span class="t">${m.time}</span>
          ${canManage(m) ? `<span class="acts">
             <a href="#" data-act="edit" data-id="${m.id}">modifier</a>
             ${m.bot && ME.staff ? `<a href="/guild/${GID}/annonce?edit=${CID}-${m.id}"
                title="Modifier le texte, l'embed et les boutons">éditeur</a>` : ''}
             <a href="#" data-act="del" data-id="${m.id}">supprimer</a></span>` : ''}
        </div>
        ${m.content ? `<div class="body">${esc(m.content)}</div>` : ''}
        ${(m.embeds||[]).map(e => `<div class="emb" style="border-color:${e.color||'#5865f2'}">
            ${e.author ? `<div class="ea">${esc(e.author)}</div>` : ''}
            ${e.title ? `<div class="et">${esc(e.title)}</div>` : ''}
            ${e.description ? `<div class="ed">${esc(e.description)}</div>` : ''}
            ${(e.fields||[]).map(f => `<div class="ef"><b>${esc(f[0])}</b><br>${esc(f[1])}</div>`).join('')}
            ${e.image ? `<img class="ei" src="${e.image}" alt="">` : ''}
            ${e.footer ? `<div class="eo">${esc(e.footer)}</div>` : ''}
          </div>`).join('')}
        ${(m.attachments||[]).map(a => a.image
            ? `<a href="${a.url}" target="_blank"><img class="ai" src="${a.url}" alt=""></a>`
            : `<a href="${a.url}" target="_blank" class="att">📎 ${esc(a.name)}</a>`).join('')}
        ${(!m.content && !(m.embeds||[]).length && !(m.attachments||[]).length)
            ? '<i style="color:#79829a">(message sans texte)</i>' : ''}
      </div></div>`).join('');
  document.querySelectorAll('.acts a').forEach(a =>
    a.addEventListener('click', ev => { ev.preventDefault();
      a.dataset.act === 'del' ? delMsg(a.dataset.id) : editMsg(a.dataset.id); }));
  const box = $('msgs'); box.scrollTop = box.scrollHeight;
}

async function refresh(force){
  if (!CID && !UID) return;
  let d;
  try {
    d = await jget(MODE === 'chan'
      ? `/api/guild/${GID}/channel/${CID}/messages`
      : `/api/guild/${GID}/dm/${UID}/messages`);
  } catch(e){ $('msgs').innerHTML = '<p class="sub">Conversation illisible.</p>'; return; }
  const key = JSON.stringify((d.messages||[]).map(m => m.id + m.content));
  if (key === sig && !force) return;
  sig = key;
  render(d.messages || []);
}

async function delMsg(id){
  if (!confirm('Supprimer ce message ?')) return;
  const r = await fetch(`/api/guild/${GID}/channel/${CID}/message/${id}`, {method:'DELETE'});
  if (!r.ok) alert((await r.json().catch(()=>({}))).error || 'Suppression refusée');
  refresh(true);
}

async function editMsg(id){
  const el = document.querySelector(`.msg2[data-id="${id}"] .body`);
  const now = el ? el.textContent : '';
  const text = prompt('Nouveau contenu du message :', now);
  if (text === null || !text.trim()) return;
  const r = await fetch(`/api/guild/${GID}/channel/${CID}/message/${id}`, {
    method:'PATCH', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({content: text})});
  if (!r.ok) alert((await r.json().catch(()=>({}))).error || 'Modification refusée');
  refresh(true);
}

async function send(){
  const text = $('input').value.trim();
  const file = $('upload').files[0];
  if (!text && !file) return;
  const asBot = $('asbot').checked;
  $('input').value = ''; let r;
  if (MODE === 'dm'){
    r = await fetch(`/api/guild/${GID}/dm/${UID}/send`, {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({content: text, as_bot: asBot})});
  } else if (file){
    const fd = new FormData();
    fd.append('content', text); fd.append('as_bot', asBot ? '1' : '0');
    fd.append('file', file);
    r = await fetch(`/api/guild/${GID}/channel/${CID}/send`, {method:'POST', body: fd});
    $('upload').value = ''; $('fname').textContent = '';
  } else {
    r = await fetch(`/api/guild/${GID}/channel/${CID}/send`, {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({content: text, as_bot: asBot})});
  }
  if (!r.ok) alert((await r.json().catch(()=>({}))).error || 'Envoi refusé');
  refresh(true);
}

$('upload').addEventListener('change', e => {
  const f = e.target.files[0];
  $('fname').textContent = f ? ('📎 ' + f.name) : '';
});
$('send').addEventListener('click', send);
$('input').addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
$('gpick').addEventListener('change', e => { GID = e.target.value; setMode('chan'); });
$('tabchan').addEventListener('click', () => setMode('chan'));
$('tabdm').addEventListener('click', () => setMode('dm'));
$('search').addEventListener('input', e => { if (MODE === 'dm') loadDMs(e.target.value); });
setMode('chan');
"""


async def page_client(request: web.Request) -> web.Response:
    session = require_session(request)
    account = await get_account(session["user_id"])
    if account is None:
        raise web.HTTPFound("/logout")
    if account.get("must_change"):
        raise web.HTTPFound("/setup")
    guilds = await user_guilds(account["user_id"])
    if not guilds:
        return page("Client", '<div class="panel"><h2>Aucun serveur</h2>'
                              '<p class="sub">Le bot ne te voit sur aucun serveur.</p></div>',
                    session)
    wanted = request.query.get("guild") or str(guilds[0]["id"])
    options = "".join(
        f'<option value="{g["id"]}"{" selected" if str(g["id"]) == wanted else ""}>'
        f'{escape(g["name"])}</option>' for g in guilds)
    body = f"""
<div class="client">
  <aside>
    <select id="gpick">{options}</select>
    <div class="tabs"><button id="tabchan" class="tab on">💬 Salons</button>
      <button id="tabdm" class="tab" style="display:none">✉️ MP</button></div>
    <input id="search" placeholder="Chercher un membre…" style="display:none">
    <div class="me"><img id="meavatar" alt="" src="/logo.png"
         onerror="this.src='/logo.png'"><div><b id="mename">Chargement…</b>
      <small id="melevel">connexion au serveur</small></div></div>
    <div id="chans" class="list"><p class="sub" style="color:#79829a;padding:8px">
      Chargement des salons…</p></div>
  </aside>
  <main>
    <div class="chead"><b id="cname">#…</b>
      <span class="sub" style="color:#79829a"> · <span id="gname"></span></span></div>
    <div id="msgs" class="msgs"></div>
    <div class="composer">
      <label id="staffbox" class="asbot" style="display:none">
        <input type="checkbox" id="asbot"> parler en tant que <b>bot</b></label>
      <div class="row">
        <span id="fname" class="fname"></span>
        <input type="file" id="upload" hidden>
        <label for="upload" id="clip" class="clip" title="Joindre un fichier (8 Mo max)">📎</label>
        <input id="input" placeholder="Écris ton message…" maxlength="1900" autocomplete="off">
        <button id="send" class="btn">Envoyer</button>
      </div>
    </div>
  </main>
</div>
<p class="sub" style="color:#79829a;margin-top:14px">
 Tes messages partent avec <b>ton pseudo et ton avatar</b> (webhook) — le bot ne change
 jamais sa propre identité. Le staff peut cocher « parler en tant que bot », consulter les
 <b>MP du bot</b> dans le même client, et modifier ou supprimer <b>tous</b> les messages
 publiés depuis le site ; un membre ne gère que les siens.</p>
<script>{CLIENT_JS}</script>"""
    return page("Client Discord", body, session, wide=True)


CLIENT_CSS = """
.client{display:grid;grid-template-columns:270px minmax(0,1fr);gap:16px;height:72vh}
.client aside{background:var(--panel);border:1px solid var(--line);
 border-radius:18px;padding:14px;display:flex;flex-direction:column;gap:10px;
 overflow:visible;min-height:0}
.client aside > select{position:relative;z-index:5}
.client aside .list{overflow:auto;flex:1}
.client .me{display:flex;align-items:center;gap:10px;padding:8px;border-radius:12px;
 background:rgba(255,255,255,.05)}
.client .me img{width:36px;height:36px;border-radius:50%}
.client .me small{display:block;color:#8d96ad;font-size:.75rem}
.cat{font-size:.72rem;text-transform:uppercase;letter-spacing:.6px;color:#79829a;
 margin:12px 0 4px;padding-left:6px}
.client main{background:var(--panel);border:1px solid var(--line);
 border-radius:18px;display:flex;flex-direction:column;overflow:hidden}
.chead{padding:14px 18px;border-bottom:1px solid var(--line)}
.msgs{flex:1;overflow:auto;padding:16px 18px;display:flex;flex-direction:column;gap:14px}
.msg2{display:flex;gap:12px}
.msg2 img{width:38px;height:38px;border-radius:50%;flex:none}
.msg2 .head{display:flex;align-items:center;gap:8px}
.msg2 .t{color:#79829a;font-size:.75rem}
.msg2 .body{color:#dfe4f2;white-space:pre-wrap;word-break:break-word;font-size:.94rem}
.botb{background:#5865f2;color:#fff;font-size:.62rem;padding:1px 5px;border-radius:4px}
.att{display:inline-block;margin-top:4px;font-size:.82rem}
.composer{display:flex;flex-direction:column;gap:8px;padding:14px;
 border-top:1px solid var(--line)}
.composer .row{display:flex;gap:10px;align-items:center}
.composer .row input#input{flex:1}
.asbot{display:flex;align-items:center;gap:8px;font-size:.82rem;color:#9aa3ba;margin:0}
.asbot input{width:auto}
.tabs{display:flex;gap:6px}
.tab{flex:1;padding:8px;border-radius:10px;border:1px solid var(--line2);
 background:rgba(255,255,255,.05);color:#c7cddf;cursor:pointer;font-size:.86rem}
.tab.on{background:rgba(255,106,61,.16);border-color:rgba(255,106,61,.45);color:#fff}
.oops{padding:14px;border-radius:12px;background:rgba(237,66,69,.12);
 border:1px solid rgba(237,66,69,.35);font-size:.86rem;color:#ffc9cb}
.oops p{margin:8px 0 12px;color:#e8b4b6;line-height:1.5}
.mini{width:26px;height:26px;border-radius:50%}
.acts{margin-left:auto;display:flex;gap:10px;opacity:0;transition:.15s}
.msg2:hover .acts{opacity:1}
.acts a{font-size:.74rem;color:#8d96ad}
.acts a:hover{color:#ff9ba0}
.clip{display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;
 border-radius:11px;background:rgba(255,255,255,.07);border:1px solid var(--line2);
 cursor:pointer;font-size:1.1rem;flex:none}
.clip:hover{background:rgba(255,255,255,.13)}
.fname{font-size:.8rem;color:var(--acc2)}
.emb{border-left:4px solid #5865f2;background:rgba(255,255,255,.04);border-radius:6px;
 padding:10px 12px;margin-top:6px;max-width:520px}
.emb .ea{font-size:.78rem;color:#aab3c9;margin-bottom:2px}
.emb .et{font-weight:650;color:#fff;margin-bottom:4px}
.emb .ed{font-size:.88rem;color:#dfe4f2;white-space:pre-wrap}
.emb .ef{margin-top:8px;font-size:.84rem;color:#c7cddf}
.emb .ei{max-width:100%;border-radius:8px;margin-top:8px;display:block}
.emb .eo{font-size:.74rem;color:#79829a;margin-top:8px}
.rolebox{display:grid;gap:6px;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));
 max-height:190px;overflow:auto;padding:10px;border-radius:12px;background:var(--bg2);
 border:1px solid var(--line2)}
.rolepick{display:flex;align-items:center;gap:8px;margin:0;padding:6px 8px;border-radius:9px;
 cursor:pointer;font-size:.88rem;color:#dfe4f2}
.rolepick:hover{background:rgba(255,255,255,.06)}
.rolepick input{width:auto;margin:0}
.rolepick .dot{width:10px;height:10px;border-radius:50%;flex:none}
.rolepick small{margin-left:auto;color:#79829a}
.annwrap{display:grid;gap:18px;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr)}
@media(max-width:940px){.annwrap{grid-template-columns:1fr}}
.btnrow{border:1px solid var(--line);border-radius:14px;padding:14px;
 margin-bottom:12px;background:rgba(255,255,255,.03)}
.bgrid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.pv{display:flex;background:#2b2d31;border-radius:6px;overflow:hidden}
.pv-bar{width:4px;background:#5865f2;flex:none}
.pv-body{padding:12px 14px;flex:1;min-width:0}
.pv-title{font-weight:700;color:#fff;margin-bottom:6px}
.pv-desc{color:#dbdee1;font-size:.9rem;white-space:pre-wrap;word-break:break-word}
.pv-foot{color:#949ba4;font-size:.75rem;margin-top:10px}
.pv-body img{max-width:100%;border-radius:6px;margin-top:10px}
.pv-btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.pvping{background:rgba(255,106,61,.22);color:var(--acc2);padding:2px 6px;border-radius:4px;
 font-size:.86rem}
.pvb{padding:8px 14px;border-radius:8px;color:#fff;font-size:.84rem;font-weight:600}
.ai{max-width:360px;max-height:260px;border-radius:10px;margin-top:6px;display:block}
@media(max-width:820px){.client{grid-template-columns:1fr;height:auto}
 .client main{height:66vh}}
"""



# --------------------------------------------------------------------------- #
#  Règlement du serveur Discord (/therms) — modifiable depuis le site
# --------------------------------------------------------------------------- #
DEFAULT_RULES = """1. Respect — aucune insulte, moquerie, harcèlement, menace ni discrimination. On peut être en désaccord sans être désagréable.
2. Contenus interdits — pas de contenu à caractère sexuel, violent, choquant, haineux, ni de gore. Les serveurs sont ouverts aux mineurs.
3. Pas de spam — évite les répétitions, les majuscules permanentes, les mentions abusives et les émojis à la chaîne.
4. Publicité — aucune invitation vers un autre serveur, aucun lien commercial ou d'affiliation sans l'accord d'un administrateur.
5. Salons — utilise chaque salon pour son sujet. Les discussions hors-sujet vont dans le salon général.
6. Vocal — pas de bruit intentionnel, pas de larsen, pas d'enregistrement sans l'accord des participants. Ne coupe pas la parole.
7. Pseudos et avatars — lisibles, mentionnables, sans contenu choquant ni usurpation d'identité d'un membre du staff.
8. Vie privée — ne publie jamais d'informations personnelles, les tiennes ou celles des autres (adresse, téléphone, photos privées).
9. Piratage — aucun lien de téléchargement illégal, aucun logiciel malveillant, aucun partage de compte.
10. Staff — les décisions du staff s'appliquent. Un désaccord se règle en message privé ou en ticket, jamais en public.
11. Site et bot — tes identifiants du site sont personnels. Toute tentative de contourner les permissions entraîne un bannissement.
12. Bon sens — ce règlement ne peut pas tout prévoir. Le staff tranche au cas par cas, et les règles de Discord s'appliquent en plus de celles-ci."""


async def get_rules() -> str:
    return await config_get("rules", DEFAULT_RULES)


async def page_rules(request: web.Request) -> web.Response:
    session = session_of(request)
    rules = await get_rules()
    can_edit = False
    if session:
        for guild in await user_guilds(session["user_id"]):
            if guild["level"] >= 3:
                can_edit = True
                break
    items = "".join(f"<li style='margin-bottom:10px'>{escape(line.strip())}</li>"
                    for line in rules.splitlines() if line.strip())
    editor = ""
    if can_edit:
        editor = f"""
<h2 class="section">✏️ Modifier le règlement</h2>
<div class="card"><form method="post" action="/therms">
  <label>Une règle par ligne</label>
  <textarea name="rules" rows="14" style="width:100%;padding:12px;border-radius:11px;
    background:#0e111b;border:1px solid rgba(255,255,255,.14);color:#e8ebf5;
    font-family:inherit;font-size:.93rem">{escape(rules)}</textarea>
  <button class="btn" style="margin-top:14px">Enregistrer</button>
  <p class="sub" style="color:#79829a;margin-top:8px">Visible par les administrateurs
   uniquement. La page publique est mise à jour immédiatement.</p>
</form></div>"""
    notice = request.query.get("ok", "")
    body = f"""
{f'<div class="msg ok">{escape(notice)}</div>' if notice else ''}
<h2 class="section">📜 Règlement du serveur Discord</h2>
<div class="card">
  <p style="color:#aab3c9;margin-bottom:16px">En rejoignant le serveur et en utilisant ce
  site, tu acceptes ce règlement ainsi que les
  <a href="/conditions">conditions d'utilisation</a>, la
  <a href="/confidentialite">politique de confidentialité</a> et les
  <a href="https://discord.com/terms" target="_blank">conditions de Discord</a>.</p>
  <ol style="padding-left:20px;color:#dfe4f2">{items}</ol>
</div>
<h2 class="section">⚖️ Sanctions</h2>
<div class="card"><table>
  <tr><th>Niveau</th><th>Mesure</th></tr>
  <tr><td>1<sup>re</sup> fois</td><td>Rappel à l'ordre ou avertissement</td></tr>
  <tr><td>Récidive</td><td>Exclusion temporaire (de 10 minutes à 7 jours)</td></tr>
  <tr><td>Faute grave</td><td>Expulsion</td></tr>
  <tr><td>Faute très grave ou répétée</td><td>Bannissement définitif</td></tr>
</table>
<p class="sub" style="color:#79829a;margin-top:12px">Une sanction peut être contestée en
ouvrant un ticket ou en écrivant à un administrateur.</p></div>
{editor}"""
    return page("Règlement", body, session)


async def post_rules(request: web.Request) -> web.Response:
    session = require_session(request)
    allowed = any(g["level"] >= 3 for g in await user_guilds(session["user_id"]))
    if not allowed:
        raise web.HTTPFound("/therms")
    data = await request.post()
    text = str(data.get("rules", "")).strip()[:8000]
    if text:
        await config_set("rules", text)
    raise web.HTTPFound("/therms?ok=Règlement mis à jour.")


# --------------------------------------------------------------------------- #
#  Pages légales (exigées par Google pour publier l'application OAuth)
# --------------------------------------------------------------------------- #
PRIVACY = """
<h2 class="section">Politique de confidentialité</h2>
<div class="card">
<p><b>Qui&nbsp;?</b> Ce site est l'interface web du bot Discord <b>Lulyo</b>, hébergé par
l'administrateur du serveur Discord concerné. Il n'y a aucune société, aucune revente de
données, aucune publicité.</p>

<h3 style="margin-top:20px">Données collectées</h3>
<ul class="rules">
  <li>Ton identifiant Discord, ton pseudo et tes rôles sur les serveurs communs avec le bot,
      afin de déterminer tes droits d'accès.</li>
  <li>Un identifiant de connexion et une empreinte de ton mot de passe
      (PBKDF2-SHA256, 240&nbsp;000 itérations, avec sel&nbsp;: le mot de passe en clair
      n'est jamais stocké).</li>
  <li>Si tu lies un compte Google&nbsp;: ton identifiant Google (<code>sub</code>) et ton
      adresse e-mail, uniquement pour te reconnaître à la connexion.</li>
  <li>La date de création du compte et celle de la dernière connexion.</li>
</ul>

<h3 style="margin-top:20px">Ce que nous ne faisons pas</h3>
<ul class="rules">
  <li>Aucune donnée n'est vendue, louée ni transmise à un tiers.</li>
  <li>Aucun traceur publicitaire, aucun cookie tiers. Le seul cookie posé
      (<code>lulyo_session</code>) sert à te garder connecté&nbsp;; il expire au bout de 7&nbsp;jours.</li>
  <li>Aucun accès à tes messages privés Google, tes contacts ou tes fichiers&nbsp;: les
      autorisations demandées se limitent à <code>openid</code>, <code>email</code> et
      <code>profile</code>.</li>
</ul>

<h3 style="margin-top:20px">Conservation et suppression</h3>
<p>Les données sont stockées dans une base SQLite sur le serveur du bot. Tu peux à tout
moment demander la suppression de ton compte&nbsp;: écris à l'administrateur du serveur
Discord ou utilise la commande <code>/compte</code>. La suppression est définitive et
immédiate. Délier un compte Google se fait depuis la page <i>Mon compte</i>.</p>

<h3 style="margin-top:20px">Contact</h3>
<p>Pour toute question ou demande d'effacement, contacte l'administrateur du serveur
Discord sur lequel tu utilises Lulyo.</p>
</div>"""

TERMS = """
<h2 class="section">Conditions d'utilisation</h2>
<div class="card">
<p><b>1. Objet.</b> Ce site permet aux membres d'un serveur Discord d'accéder aux
fonctions du bot Lulyo (télévision, radio, modération, configuration) selon les
permissions que leur donnent leurs rôles Discord.</p>

<p style="margin-top:14px"><b>2. Accès.</b> L'accès est personnel. Tes identifiants sont
envoyés en message privé par le bot&nbsp;: ne les partage jamais. Tu es responsable des
actions effectuées depuis ton compte. Tes droits sont recalculés en direct à partir de tes
rôles&nbsp;: perdre un rôle sur Discord retire immédiatement l'accès correspondant.</p>

<p style="margin-top:14px"><b>3. Usage correct.</b> Il est interdit de tenter de contourner
les contrôles de permissions, d'automatiser des requêtes, de perturber le service ou de
l'utiliser pour harceler autrui ou diffuser des contenus illégaux. Tout abus entraîne la
suppression du compte, sans préavis.</p>

<p style="margin-top:14px"><b>4. Contenus tiers.</b> Les flux de télévision et de radio
proposés sont des flux publics diffusés par leurs éditeurs respectifs. Ce site ne les
héberge pas, ne les modifie pas et n'en revendique aucun droit&nbsp;; il se contente de les
lire. Toute demande de retrait sera traitée sans délai.</p>

<p style="margin-top:14px"><b>5. Service fourni « tel quel ».</b> Le service est gratuit,
sans garantie de disponibilité. Il peut être interrompu, modifié ou arrêté à tout moment.
La responsabilité de l'administrateur ne peut être engagée en cas d'indisponibilité ou de
perte de données.</p>

<p style="margin-top:14px"><b>6. Données personnelles.</b> Voir la
<a href="/confidentialite">politique de confidentialité</a>.</p>
</div>"""


async def page_privacy(request: web.Request) -> web.Response:
    return page("Confidentialité", PRIVACY, session_of(request))


async def page_terms(request: web.Request) -> web.Response:
    return page("Conditions", TERMS, session_of(request))


# =========================================================================== #
#  STUDIO — édition du site par les propriétaires (BOT_OWNER_IDS du .env)
#  Pages personnalisées, boutons du menu, textes de l'accueil.
# =========================================================================== #
SITE_NAV: list[dict] = []          # cache du menu, rechargé à chaque modification
SITE_HOME: dict = {}               # cache des textes de l'accueil

STUDIO_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS site_pages (
           slug       TEXT PRIMARY KEY,
           title      TEXT NOT NULL,
           body       TEXT NOT NULL DEFAULT '',
           in_nav     INTEGER NOT NULL DEFAULT 1,
           position   INTEGER NOT NULL DEFAULT 100,
           access     TEXT NOT NULL DEFAULT 'public',
           published  INTEGER NOT NULL DEFAULT 1,
           updated_at TEXT NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS site_redirects (
           path       TEXT PRIMARY KEY,
           target     TEXT NOT NULL,
           hits       INTEGER NOT NULL DEFAULT 0,
           created_at TEXT NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS site_links (
           id       INTEGER PRIMARY KEY AUTOINCREMENT,
           label    TEXT NOT NULL,
           url      TEXT NOT NULL,
           position INTEGER NOT NULL DEFAULT 100,
           style    TEXT NOT NULL DEFAULT 'lien',
           access   TEXT NOT NULL DEFAULT 'public'
       )""",
]


def owner_ids() -> set[int]:
    raw = env("BOT_OWNER_IDS") or env("PANEL_OWNERS")
    return {int(part) for part in re.split(r"[,\s]+", raw) if part.isdigit()}


def is_owner(session: Optional[dict]) -> bool:
    return bool(session) and int(session.get("user_id", 0)) in owner_ids()


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40]


SAFE_TAGS = {
    "p", "br", "hr", "h2", "h3", "h4", "ul", "ol", "li", "b", "strong", "i", "em",
    "u", "s", "blockquote", "code", "pre", "a", "img", "div", "span", "table",
    "thead", "tbody", "tr", "th", "td", "small", "iframe",
}
SAFE_ATTRS = {"href", "src", "alt", "title", "class", "style", "target", "rel",
              "width", "height", "allow", "allowfullscreen", "frameborder"}


def sanitize_html(raw: str) -> str:
    """Nettoyage : on retire scripts, événements et protocoles douteux."""
    text = raw or ""
    text = re.sub(r"(?is)<\s*(script|style|object|embed|form|input|button)[^>]*>.*?"
                  r"<\s*/\s*\1\s*>", "", text)
    text = re.sub(r"(?is)<\s*/?\s*(script|style|object|embed|form|input|button)[^>]*>",
                  "", text)
    text = re.sub(r"(?i)\son\w+\s*=\s*'[^']*'", "", text)
    text = re.sub(r'(?i)\son\w+\s*=\s*"[^"]*"', "", text)
    text = re.sub(r"(?i)(href|src)\s*=\s*['\"]?\s*javascript:[^'\" >]*",
                  r"\1=#", text)

    def keep(match):
        tag = match.group(2).lower()
        if tag not in SAFE_TAGS:
            return ""
        return match.group(0)

    return re.sub(r"<\s*(/?)\s*([a-zA-Z0-9]+)([^>]*)>", keep, text)


def markdown_lite(raw: str) -> str:
    """Écriture simple : ## titre, **gras**, *italique*, [texte](lien), listes."""
    out = []
    for block in (raw or "").replace("\r", "").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if block.startswith("<"):
            out.append(block)
            continue
        lines = block.split("\n")
        if all(line.lstrip().startswith(("- ", "* ")) for line in lines):
            items = "".join(f"<li>{line.lstrip()[2:]}</li>" for line in lines)
            out.append(f"<ul>{items}</ul>")
            continue
        if block.startswith("### "):
            out.append(f"<h4>{block[4:]}</h4>")
            continue
        if block.startswith("## "):
            out.append(f'<h2 class="section">{block[3:]}</h2>')
            continue
        if block.startswith("# "):
            out.append(f'<h2 class="section">{block[2:]}</h2>')
            continue
        out.append("<p>" + "<br>".join(lines) + "</p>")
    html = "\n".join(out)
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", html)
    html = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", html)
    html = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+|/[^\s)]*)\)",
                  r'<a href="\2">\1</a>', html)
    return html


EDITOR_CSS = """
.edwrap{border:1px solid var(--line2);border-radius:14px;overflow:hidden;background:var(--panel)}
.edbar{display:flex;flex-wrap:wrap;gap:4px;padding:8px;background:var(--panel2);
 border-bottom:1px solid var(--line)}
.edbar button,.edbar select{background:transparent;border:1px solid transparent;color:var(--tx);
 border-radius:8px;padding:6px 9px;font-size:.86rem;cursor:pointer;width:auto;min-width:0}
.edbar button:hover,.edbar select:hover{background:rgba(255,255,255,.08);border-color:var(--line2)}
.edbar .sep{width:1px;background:var(--line2);margin:4px 3px}
.edbar b{font-weight:700}
#editor{min-height:420px;max-height:70vh;overflow:auto;padding:22px 24px;outline:none;
 font-size:.97rem;line-height:1.7}
#editor:empty::before{content:attr(data-placeholder);color:var(--dim)}
#editor h2,.prose h2{font-family:var(--serif);font-size:1.6rem;margin:18px 0 10px}
#editor h3,.prose h3{font-family:var(--serif);font-size:1.25rem;margin:16px 0 8px}
#editor p,.prose p{margin:10px 0}
#editor ul,#editor ol,.prose ul,.prose ol{margin:10px 0 10px 22px}
#editor img,.prose img{max-width:100%;border-radius:12px;margin:10px 0}
#editor a,.prose a{color:var(--acc)}
#editor blockquote,.prose blockquote{border-left:3px solid var(--acc);padding:4px 14px;
 margin:12px 0;color:var(--mute)}
#editor pre,.prose pre{background:#0c0c0e;border:1px solid var(--line);border-radius:10px;
 padding:12px;overflow:auto;font-size:.85rem}
#editor table,.prose table{width:100%;border-collapse:collapse;margin:12px 0}
#editor td,#editor th,.prose td,.prose th{border:1px solid var(--line2);padding:8px}
.prose .btn{display:inline-block;margin:6px 6px 6px 0}
.ed-cols{display:grid;gap:16px;grid-template-columns:1fr 1fr;margin:12px 0}
@media(max-width:700px){.ed-cols{grid-template-columns:1fr}}
.ed-callout{border:1px solid var(--accln);background:var(--accbg);border-radius:12px;padding:14px 16px;margin:12px 0}
#htmlview{display:none;width:100%;min-height:420px;font-family:ui-monospace,Consolas,monospace;
 font-size:.85rem;border:0;border-radius:0;background:#0c0c0e}
.edfoot{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:10px 12px;
 border-top:1px solid var(--line);background:var(--panel2);font-size:.84rem;color:var(--mute)}
.dropzone{border:2px dashed var(--line2);border-radius:14px;padding:18px;text-align:center;
 color:var(--mute);font-size:.88rem;margin-top:12px;transition:.15s;cursor:pointer}
.dropzone.hot{border-color:var(--acc);background:var(--accbg);color:var(--tx)}
.filelist{display:grid;gap:8px;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));margin-top:12px}
.fileitem{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:10px;
 background:var(--panel2);border:1px solid var(--line);font-size:.82rem;cursor:pointer;
 overflow:hidden}
.fileitem:hover{border-color:var(--acc)}
.fileitem span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
.filedel{background:none;border:0;color:var(--dim);cursor:pointer;font-size:.9rem;
 padding:2px 4px;border-radius:6px;width:auto;flex:none}
.filedel:hover{color:var(--err);background:rgba(255,133,133,.12)}
.fileitem img{width:28px;height:28px;object-fit:cover;border-radius:6px;flex:none}
.fileitem b{font-size:1.1rem;flex:none}
.medias{display:grid;gap:8px;grid-template-columns:repeat(auto-fill,minmax(92px,1fr));margin-top:12px}
.medias img{width:100%;height:72px;object-fit:cover;border-radius:8px;cursor:pointer;
 border:1px solid var(--line2)}
.medias img:hover{border-color:var(--acc)}
"""

EDITOR_JS = r"""
const ed = document.getElementById('editor');
const htmlView = document.getElementById('htmlview');
const field = document.getElementById('bodyfield');
let codeMode = false;

function cmd(name, value){ document.execCommand(name, false, value || null); ed.focus(); sync(); }
function sync(){
  const mode = document.querySelector('[name=mode]');
  if (mode && mode.value === 'fichier') { count(); return; }   // la page = un fichier
  field.value = codeMode ? htmlView.value : ed.innerHTML;
  count();
}
function count(){
  const text = (codeMode ? htmlView.value : ed.innerText) || '';
  document.getElementById('wc').textContent =
    text.trim().split(/\s+/).filter(Boolean).length + ' mots';
}
function insert(html){
  if (codeMode){ htmlView.value += '\n' + html; sync(); return; }
  ed.focus();
  document.execCommand('insertHTML', false, html);
  sync();
}

document.querySelectorAll('[data-cmd]').forEach(b => b.addEventListener('click', e => {
  e.preventDefault(); cmd(b.dataset.cmd, b.dataset.val);
}));

document.getElementById('fmtblock').addEventListener('change', e => {
  cmd('formatBlock', e.target.value); e.target.selectedIndex = 0;
});

document.getElementById('lnk').addEventListener('click', e => {
  e.preventDefault();
  const url = prompt('Adresse du lien :', 'https://');
  if (url) cmd('createLink', url);
});

document.getElementById('btnblock').addEventListener('click', e => {
  e.preventDefault();
  const label = prompt('Texte du bouton :', 'Nous rejoindre');
  if (!label) return;
  const url = prompt('Lien du bouton :', 'https://discord.gg/');
  if (!url) return;
  insert(`<p><a class="btn" href="${url}">${label}</a></p>`);
});

document.getElementById('cardblock').addEventListener('click', e => {
  e.preventDefault();
  insert('<div class="card"><h3>Titre de l\'encart</h3><p>Ton texte ici.</p></div><p><br></p>');
});
document.getElementById('colsblock').addEventListener('click', e => {
  e.preventDefault();
  insert('<div class="ed-cols"><div><h3>Colonne 1</h3><p>Texte…</p></div>'
       + '<div><h3>Colonne 2</h3><p>Texte…</p></div></div><p><br></p>');
});
document.getElementById('calloutblock').addEventListener('click', e => {
  e.preventDefault();
  insert('<div class="ed-callout"><b>À noter</b><br>Message important.</div><p><br></p>');
});
document.getElementById('videoblock').addEventListener('click', e => {
  e.preventDefault();
  const url = prompt('Lien YouTube :', 'https://www.youtube.com/watch?v=');
  if (!url) return;
  const id = (url.match(/(?:v=|youtu\.be\/|embed\/)([A-Za-z0-9_-]{6,})/) || [])[1];
  if (!id) return alert('Lien YouTube non reconnu.');
  insert(`<p><iframe width="100%" height="380" src="https://www.youtube.com/embed/${id}"`
       + ` frameborder="0" allowfullscreen></iframe></p>`);
});
document.getElementById('hrblock').addEventListener('click', e => {
  e.preventDefault(); insert('<hr><p><br></p>');
});

// ---- images -------------------------------------------------------------
const picker = document.getElementById('imgfile');
document.getElementById('imgbtn').addEventListener('click', e => { e.preventDefault(); picker.click(); });
picker.addEventListener('change', async () => {
  const file = picker.files[0]; if (!file) return;
  const fd = new FormData(); fd.append('file', file);
  const status = document.getElementById('upstatus');
  status.textContent = 'Envoi…';
  const r = await fetch('/studio/upload', {method:'POST', body: fd});
  const d = await r.json().catch(() => ({}));
  picker.value = '';
  if (!r.ok || !d.url){ status.textContent = d.error || 'Échec de l\'envoi'; return; }
  status.textContent = 'Image ajoutée';
  insert(`<p><img src="${d.url}" alt=""></p>`);
  const grid = document.getElementById('medias');
  if (grid){ const i = document.createElement('img'); i.src = d.url;
    i.addEventListener('click', () => insert(`<p><img src="${d.url}" alt=""></p>`));
    grid.prepend(i); }
});
document.querySelectorAll('#medias img').forEach(img =>
  img.addEventListener('click', () => insert(`<p><img src="${img.src}" alt=""></p>`)));

// ---- glisser-déposer de fichiers ----------------------------------------
const drop = document.getElementById('dropzone');
const ICONS = {image:'🖼️', style:'🎨', script:'⚙️', page:'📄', police:'🔤',
               video:'🎬', audio:'🎵', fichier:'📎'};

function insertAsset(asset){
  const url = asset.url, kind = asset.kind;
  if (kind === 'image'){
    insert(`<p><img src="${url}" alt=""></p>`);
  } else if (kind === 'style'){
    const css = document.getElementById('cssfield');
    css.value = (css.value ? css.value + '\n' : '') + `@import url("${url}");`;
    document.getElementById('advanced').open = true;
    alert('Feuille de style liée à la page (voir « Code avancé »).');
  } else if (kind === 'script'){
    const js = document.getElementById('jsfield');
    js.value = (js.value ? js.value + '\n' : '')
      + `var s=document.createElement('script');s.src='${url}';document.body.appendChild(s);`;
    document.getElementById('advanced').open = true;
    alert('Script lié à la page (voir « Code avancé »).');
  } else if (kind === 'page'){
    useStaticFile(url, asset.name || url);
  } else if (kind === 'video'){
    insert(`<p><video src="${url}" controls style="max-width:100%"></video></p>`);
  } else if (kind === 'audio'){
    insert(`<p><audio src="${url}" controls></audio></p>`);
  } else {
    insert(`<p><a href="${url}">${asset.name || 'fichier'}</a></p>`);
  }
  addToLibrary(asset);
}

function addToLibrary(asset){
  const list = document.getElementById('filelist');
  if (!list || list.querySelector(`[data-url="${asset.url}"]`)) return;
  const el = document.createElement('div');
  el.className = 'fileitem'; el.dataset.url = asset.url;
  el.innerHTML = (asset.kind === 'image'
      ? `<img src="${asset.url}" alt="">` : `<b>${ICONS[asset.kind] || '📎'}</b>`)
    + `<span>${asset.name || asset.url}</span>`;
  el.addEventListener('click', () => insertAsset(asset));
  list.prepend(el);
}

async function sendFile(file){
  const status = document.getElementById('upstatus');
  status.textContent = 'Envoi de ' + file.name + '…';
  const fd = new FormData(); fd.append('file', file);
  const r = await fetch('/studio/upload', {method:'POST', body: fd});
  const d = await r.json().catch(() => ({}));
  if (!r.ok || !d.url){ status.textContent = d.error || 'Échec'; return null; }
  status.textContent = file.name + ' envoyé';
  return d;
}

if (drop){
  drop.addEventListener('click', () => document.getElementById('anyfile').click());
  ['dragenter','dragover'].forEach(evt => drop.addEventListener(evt, e => {
    e.preventDefault(); drop.classList.add('hot');
  }));
  ['dragleave','drop'].forEach(evt => drop.addEventListener(evt, e => {
    e.preventDefault(); drop.classList.remove('hot');
  }));
  drop.addEventListener('drop', async e => {
    for (const file of e.dataTransfer.files){
      const asset = await sendFile(file);
      if (asset) insertAsset(asset);
    }
  });
  document.getElementById('anyfile').addEventListener('change', async e => {
    for (const file of e.target.files){
      const asset = await sendFile(file);
      if (asset) insertAsset(asset);
    }
    e.target.value = '';
  });
}
document.querySelectorAll('#filelist .fileitem').forEach(el =>
  el.addEventListener('click', () => insertAsset(
    {url: el.dataset.url, kind: el.dataset.kind, name: el.dataset.name})));

// l'éditeur accepte aussi le dépôt direct
ed.addEventListener('dragover', e => e.preventDefault());
ed.addEventListener('drop', async e => {
  if (!e.dataTransfer.files.length) return;
  e.preventDefault();
  for (const file of e.dataTransfer.files){
    const asset = await sendFile(file);
    if (asset) insertAsset(asset);
  }
});

function useStaticFile(url, name){
  const choix = confirm(
    `Fichier HTML « ${name} »\n\n`
    + 'OK  →  la page AFFICHE ce fichier tel quel (site statique, CSS et JS compris)\n'
    + 'Annuler  →  importer son contenu dans l\'éditeur');
  const modeField = document.querySelector('[name=mode]');
  if (choix){
    modeField.value = 'fichier';
    field.value = url;
    if (codeMode){ htmlView.value = url; }
    showStatic(url);
  } else {
    fetch(url).then(r => r.text()).then(html => {
      modeField.value = 'html';
      if (codeMode){ htmlView.value = html; } else { ed.innerHTML = html; }
      showStatic(null); sync();
    });
  }
}

function showStatic(url){
  const note = document.getElementById('staticnote');
  if (!note) return;
  if (url){
    note.style.display = 'block';
    document.getElementById('staticfile').textContent = url;
    document.getElementById('staticopen').href = url;
    document.querySelector('.edwrap').style.opacity = '.45';
  } else {
    note.style.display = 'none';
    document.querySelector('.edwrap').style.opacity = '1';
  }
}

// suppression d'un fichier de la bibliothèque
document.addEventListener('click', e => {
  const del = e.target.closest('.filedel');
  if (!del) return;
  e.preventDefault(); e.stopPropagation();
  if (!confirm('Supprimer définitivement ' + del.dataset.file + ' ?')) return;
  document.getElementById('filedelname').value = del.dataset.file;
  document.getElementById('filedelform').submit();
});

// le type de page pilote l'affichage de l'éditeur
const modeSelect = document.querySelector('[name=mode]');
if (modeSelect) modeSelect.addEventListener('change', () => {
  if (modeSelect.value === 'fichier'){
    const current = (field.value || '').trim();
    showStatic(current.startsWith('/u/') ? current : null);
    if (!current.startsWith('/u/'))
      alert('Dépose un fichier .html ci-dessous, ou choisis-en un dans « Fichiers du site ».');
  } else { showStatic(null); }
});

// ---- code / visuel ------------------------------------------------------
document.getElementById('codebtn').addEventListener('click', e => {
  e.preventDefault();
  codeMode = !codeMode;
  if (codeMode){ htmlView.value = ed.innerHTML; htmlView.style.display = 'block';
                 ed.style.display = 'none'; e.target.textContent = '👁️ Visuel'; }
  else { ed.innerHTML = htmlView.value; htmlView.style.display = 'none';
         ed.style.display = 'block'; e.target.textContent = '</> HTML'; }
  sync();
});

// ---- collage sans mise en forme parasite --------------------------------
ed.addEventListener('paste', e => {
  e.preventDefault();
  const text = (e.clipboardData || window.clipboardData).getData('text/plain');
  document.execCommand('insertText', false, text);
});
ed.addEventListener('input', sync);
htmlView.addEventListener('input', sync);
document.getElementById('pageform').addEventListener('submit', sync);

// ---- aperçu -------------------------------------------------------------
document.getElementById('previewbtn').addEventListener('click', e => {
  e.preventDefault();
  const box = document.getElementById('preview');
  box.innerHTML = codeMode ? htmlView.value : ed.innerHTML;
  box.parentElement.style.display = box.parentElement.style.display === 'none' ? 'block' : 'none';
});

// ---- raccourcis ---------------------------------------------------------
document.addEventListener('keydown', e => {
  if (!(e.ctrlKey || e.metaKey)) return;
  if (e.key === 's'){ e.preventDefault(); document.getElementById('savebtn').click(); }
});
if (modeSelect && modeSelect.value === 'fichier'){
  const current = (field.value || '').trim();
  if (current.startsWith('/u/')) showStatic(current);
} else { sync(); }
"""


FILE_ICONS = {"image": "🖼️", "style": "🎨", "script": "⚙️", "page": "📄",
              "police": "🔤", "video": "🎬", "audio": "🎵", "fichier": "📎"}


def studio_files() -> list[dict]:
    """Tous les fichiers envoyés dans le studio, les plus récents d'abord."""
    out = []
    for item in sorted(uploads_dir().iterdir(),
                       key=lambda p: p.stat().st_mtime if p.is_file() else 0,
                       reverse=True):
        if not item.is_file():
            continue
        kind = UPLOAD_TYPES.get(item.suffix.lower(), "fichier")
        out.append({"url": f"/u/{item.name}", "name": item.name, "kind": kind})
    return out[:60]


def editor_block(current: Optional[dict], medias: list[dict]) -> str:
    """Barre d'outils + zone d'édition visuelle, à la façon d'un traitement de texte."""
    raw_value = str((current or {}).get("body") or "")
    if str((current or {}).get("mode") or "") == "fichier":
        content = ("<p class=\"sub\">Cette page affiche le fichier "
                   f"<b>{escape(raw_value)}</b>.</p>")
        gallery_only = True
    else:
        gallery_only = False
    content = str((current or {}).get("body") or "")
    if gallery_only:
        content = ""
    if content and not content.lstrip().startswith("<"):
        content = markdown_lite(content)          # ancien contenu en texte simple
    gallery = "".join(
        f'<div class="fileitem" data-url="{escape(f["url"])}" data-kind="{f["kind"]}" '
        f'data-name="{escape(f["name"])}" title="{escape(f["name"])}">'
        + (f'<img src="{escape(f["url"])}" alt="" loading="lazy">'
           if f["kind"] == "image" else f'<b>{FILE_ICONS.get(f["kind"], "📎")}</b>')
        + f'<span>{escape(f["name"])}</span>'
        + f'<button type="button" class="filedel" data-file="{escape(f["name"])}" '
          f'title="Supprimer ce fichier">✕</button></div>' for f in medias)
    return f"""
<div class="edwrap">
  <div class="edbar">
    <select id="fmtblock" title="Style de texte">
      <option>Style…</option>
      <option value="p">Paragraphe</option>
      <option value="h2">Titre</option>
      <option value="h3">Sous-titre</option>
      <option value="blockquote">Citation</option>
      <option value="pre">Code</option>
    </select>
    <span class="sep"></span>
    <button data-cmd="bold" title="Gras (Ctrl+B)"><b>G</b></button>
    <button data-cmd="italic" title="Italique"><i>I</i></button>
    <button data-cmd="underline" title="Souligné"><u>S</u></button>
    <button data-cmd="strikeThrough" title="Barré">S̶</button>
    <span class="sep"></span>
    <button data-cmd="insertUnorderedList" title="Liste à puces">• Liste</button>
    <button data-cmd="insertOrderedList" title="Liste numérotée">1. Liste</button>
    <button data-cmd="justifyLeft" title="Aligner à gauche">⬅</button>
    <button data-cmd="justifyCenter" title="Centrer">↔</button>
    <span class="sep"></span>
    <button id="lnk" title="Insérer un lien">🔗 Lien</button>
    <button id="imgbtn" title="Insérer une image">🖼️ Image</button>
    <button id="videoblock" title="Vidéo YouTube">▶️ Vidéo</button>
    <span class="sep"></span>
    <button id="btnblock" title="Bouton cliquable">🔘 Bouton</button>
    <button id="cardblock" title="Encart">🗂️ Encart</button>
    <button id="colsblock" title="Deux colonnes">🧱 Colonnes</button>
    <button id="calloutblock" title="Encadré coloré">💡 Mise en avant</button>
    <button id="hrblock" title="Séparateur">— Séparateur</button>
    <span class="sep"></span>
    <button data-cmd="removeFormat" title="Effacer la mise en forme">🧽</button>
    <button data-cmd="undo" title="Annuler">↶</button>
    <button data-cmd="redo" title="Rétablir">↷</button>
    <span class="sep"></span>
    <button id="codebtn" title="Voir le HTML">&lt;/&gt; HTML</button>
    <button id="previewbtn" title="Aperçu">👁️ Aperçu</button>
  </div>
  <div id="editor" contenteditable="true"
       data-placeholder="Écris ici… utilise la barre d'outils pour les titres, images, boutons.">{content}</div>
  <textarea id="htmlview" spellcheck="false"></textarea>
  <div class="edfoot">
    <span id="wc">0 mots</span>
    <span id="upstatus"></span>
    <input type="file" id="imgfile" accept="image/*" hidden>
    <span style="margin-left:auto">Ctrl+S pour enregistrer</span>
  </div>
</div>
<input type="hidden" name="body" id="bodyfield" value="{escape(raw_value)}">
<div class="card" style="display:none;margin-top:14px">
  <h3 style="margin-bottom:10px">Aperçu</h3><div id="preview" class="prose"></div>
</div>
<form method="post" action="/studio/file/delete" id="filedelform" style="display:none">
  <input type="hidden" name="name" id="filedelname"></form>
<div class="dropzone" id="dropzone">
  📥 <b>Glisse tes fichiers ici</b> — html, css, js, png, svg, jpg, gif, webp, mp4, woff2…<br>
  <small>Une image s'insère, un .css ou .js se lie à la page, un .html remplace le contenu.</small>
</div>
<input type="file" id="anyfile" multiple hidden
       accept=".html,.htm,.css,.js,.png,.jpg,.jpeg,.gif,.webp,.svg,.avif,.ico,.json,.txt,.woff,.woff2,.mp4,.webm,.mp3,.ogg">
<details style="margin-top:14px" id="library"><summary style="cursor:pointer;color:var(--acc)">
  📁 Fichiers du site ({len(medias)})</summary>
  <div class="filelist" id="filelist">{gallery or '<p class="sub">Aucun fichier envoyé.</p>'}</div>
</details>

<details style="margin-top:14px" id="advanced"><summary style="cursor:pointer;color:var(--acc)">
  🧩 Code avancé (CSS et JavaScript de cette page)</summary>
  <label style="margin-top:10px">CSS appliqué à cette page</label>
  <textarea name="css" id="cssfield" rows="6" class="code"
    placeholder=".ma-classe {{ color: #ff6a3d; }}">{escape(str((current or {}).get('css') or ''))}</textarea>
  <label style="margin-top:10px">JavaScript de cette page</label>
  <textarea name="js" id="jsfield" rows="6" class="code"
    placeholder="console.log('ma page');">{escape(str((current or {}).get('js') or ''))}</textarea>
  <p class="sub" style="color:var(--mute);margin-top:8px">Exécutés uniquement sur cette
   page. Réservé aux propriétaires : ce code n'est pas filtré.</p>
</details>"""


async def studio_setup() -> None:
    for query in STUDIO_SCHEMA:
        await STATE.db.execute(query)
    # ajout en douceur des colonnes apparues après coup
    for column, definition in (("published", "INTEGER NOT NULL DEFAULT 1"),
                               ("mode", "TEXT NOT NULL DEFAULT 'riche'"),
                               ("css", "TEXT NOT NULL DEFAULT ''"),
                               ("js", "TEXT NOT NULL DEFAULT ''")):
        try:
            await STATE.db.execute(
                f"ALTER TABLE site_pages ADD COLUMN {column} {definition}")
        except Exception:
            pass
    uploads_dir()
    await studio_reload()


def uploads_dir() -> Path:
    path = Path(env("WEB_UPLOADS", "")) if env("WEB_UPLOADS") else BASE_DIR / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


UPLOAD_TYPES = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image", ".svg": "image", ".avif": "image", ".ico": "image",
    ".css": "style", ".js": "script", ".html": "page", ".htm": "page",
    ".json": "fichier", ".txt": "fichier", ".woff2": "police", ".woff": "police",
    ".mp4": "video", ".webm": "video", ".mp3": "audio", ".ogg": "audio",
}
UPLOAD_MIME = {".svg": "image/svg+xml", ".css": "text/css; charset=utf-8",
               ".js": "application/javascript; charset=utf-8",
               ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8"}


async def serve_upload(request: web.Request) -> web.StreamResponse:
    """Sert une image envoyée depuis le studio."""
    name = re.sub(r"[^A-Za-z0-9._-]", "", request.match_info.get("name", ""))
    target = uploads_dir() / name
    if not name or not target.is_file():
        raise web.HTTPNotFound()
    headers = {"Cache-Control": "public, max-age=604800"}
    mime = UPLOAD_MIME.get(target.suffix.lower())
    if mime:
        headers["Content-Type"] = mime
    return web.FileResponse(target, headers=headers)


async def studio_upload(request: web.Request) -> web.Response:
    """Téléversement d'une image depuis l'éditeur (retourne son adresse)."""
    session = studio_guard(request)
    reader = await request.multipart()
    saved = None
    async for part in reader:
        if part.name != "file" or not part.filename:
            continue
        from urllib.parse import unquote
        filename = unquote(part.filename)
        suffix = Path(filename).suffix.lower()
        if suffix not in UPLOAD_TYPES:
            return web.json_response(
                {"error": "Format refusé. Acceptés : "
                          + ", ".join(sorted(UPLOAD_TYPES))}, status=400)
        # on garde le nom d'origine : indispensable pour qu'un index.html
        # retrouve son style.css et ses images par chemin relatif
        stem = slugify(Path(filename).stem) or "fichier"
        name, target, index = f"{stem}{suffix}", uploads_dir() / f"{stem}{suffix}", 2
        while target.exists():
            name = f"{stem}-{index}{suffix}"
            target = uploads_dir() / name
            index += 1
        size = 0
        with target.open("wb") as handle:
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    handle.close()
                    target.unlink(missing_ok=True)
                    return web.json_response({"error": "Image trop lourde (8 Mo max)."},
                                             status=413)
                handle.write(chunk)
        saved = {"url": f"/u/{name}", "kind": UPLOAD_TYPES[suffix], "name": name}
    if saved is None:
        return web.json_response({"error": "Aucun fichier reçu."}, status=400)
    return web.json_response(saved)


async def studio_delete_file(request: web.Request) -> web.Response:
    """Supprime un fichier envoyé dans le studio."""
    session = studio_guard(request)
    data = await request.post()
    name = re.sub(r"[^A-Za-z0-9._-]", "", str(data.get("name", "")))
    target = uploads_dir() / name
    if name and target.is_file():
        try:
            target.unlink()
        except OSError:
            raise web.HTTPFound("/studio?error=Suppression impossible.")
        used = await STATE.db.fetchall(
            "SELECT slug FROM site_pages WHERE body LIKE ?", (f"%/u/{name}%",))
        warn = ""
        if used:
            pages = ", ".join(str((row_dict(r) or {}).get("slug")) for r in used)
            warn = f" Attention : encore utilisé par {pages}."
        raise web.HTTPFound(f"/studio?ok=Fichier {name} supprimé.{warn}")
    raise web.HTTPFound("/studio?error=Fichier introuvable.")


async def studio_duplicate(request: web.Request) -> web.Response:
    session = studio_guard(request)
    data = await request.post()
    row = row_dict(await STATE.db.fetchone(
        "SELECT * FROM site_pages WHERE slug = ?", (slugify(str(data.get("slug", ""))),)))
    if row is None:
        raise web.HTTPFound("/studio?error=Page introuvable.")
    base = f"{row['slug']}-copie"
    slug, index = base, 2
    while await STATE.db.fetchone("SELECT slug FROM site_pages WHERE slug = ?", (slug,)):
        slug, index = f"{base}-{index}", index + 1
    await STATE.db.execute(
        "INSERT INTO site_pages(slug, title, body, in_nav, position, access, published,"
        " updated_at) VALUES(?, ?, ?, 0, ?, ?, 0, ?)",
        (slug, f"{row['title']} (copie)", row["body"], row.get("position") or 100,
         row.get("access") or "public", stamp()))
    await studio_reload()
    raise web.HTTPFound(f"/studio?page={slug}&ok=Copie créée, en brouillon.")


async def studio_reload() -> None:
    """Recharge le cache du menu et des textes de l'accueil."""
    global SITE_NAV, SITE_HOME
    items: list[dict] = []
    try:
        for row in await STATE.db.fetchall(
                "SELECT label, url, position, style, access FROM site_links "
                "ORDER BY position, id") or []:
            items.append(row_dict(row) or {})
        for row in await STATE.db.fetchall(
                "SELECT title, slug, position, access FROM site_pages "
                "WHERE in_nav = 1 AND COALESCE(published, 1) = 1 "
                "ORDER BY position, title") or []:
            page_row = row_dict(row) or {}
            items.append({"label": page_row.get("title"),
                          "url": f"/p/{page_row.get('slug')}",
                          "position": page_row.get("position", 100),
                          "style": "lien", "access": page_row.get("access", "public")})
    except Exception:
        log.debug("Studio : menu non chargé", exc_info=True)
    SITE_NAV = sorted(items, key=lambda item: int(item.get("position") or 100))
    home = {}
    for key in ("titre", "sous_titre", "bouton", "bouton_url", "eyebrow"):
        try:
            home[key] = await config_get(f"home_{key}", "")
        except Exception:
            home[key] = ""
    SITE_HOME = home


def nav_extra(session: Optional[dict]) -> str:
    """Boutons ajoutés par le studio, insérés dans la barre du site."""
    out = []
    for item in SITE_NAV:
        access = str(item.get("access") or "public")
        if access == "connecte" and not session:
            continue
        if access == "proprietaire" and not is_owner(session):
            continue
        label = escape(str(item.get("label") or ""))[:40]
        url = str(item.get("url") or "#")
        if not url.startswith(("/", "http://", "https://")):
            url = "/" + url
        external = ' target="_blank" rel="noopener"' if url.startswith("http") else ""
        cls = ' class="cta"' if item.get("style") == "bouton" else ""
        out.append(f'<a href="{escape(url)}"{cls}{external}>{label}</a>')
    return "".join(out)


async def page_custom(request: web.Request) -> web.Response:
    """Affiche une page créée dans le studio."""
    session = session_of(request)
    slug = slugify(request.match_info.get("slug", ""))
    row = row_dict(await STATE.db.fetchone(
        "SELECT * FROM site_pages WHERE slug = ?", (slug,)))
    if row is None:
        raise web.HTTPNotFound(text="Page introuvable")
    if not int(row.get("published") or 0) and not is_owner(session):
        raise web.HTTPNotFound(text="Page introuvable")
    access = str(row.get("access") or "public")
    if access == "connecte" and session is None:
        raise web.HTTPFound("/login")
    if access == "proprietaire" and not is_owner(session):
        raise web.HTTPFound("/")
    mode = str(row.get("mode") or "riche")
    raw = str(row.get("body") or "")

    if mode == "fichier":
        # la page EST le fichier déposé : on le sert tel quel, comme un site statique
        name = re.sub(r"[^A-Za-z0-9._-]", "", raw.strip().rsplit("/", 1)[-1])
        target = uploads_dir() / name
        if not name or not target.is_file():
            raise web.HTTPNotFound(text="Fichier de la page introuvable")
        document = target.read_text(encoding="utf-8", errors="replace")
        # la page vit en /p/<slug> mais ses fichiers en /u/ : sans cette balise,
        # « style.css » pointerait vers /p/style.css et ne chargerait pas
        if "<base " not in document.lower():
            if re.search(r"(?i)<head[^>]*>", document):
                document = re.sub(r"(?i)(<head[^>]*>)", r'\1<base href="/u/">',
                                  document, count=1)
            else:
                document = '<base href="/u/">' + document
        return web.Response(text=document, content_type="text/html")
    extra_css = str(row.get("css") or "").strip()
    extra_js = str(row.get("js") or "").strip()

    if mode == "complete":
        # page HTML autonome : le propriétaire fournit le document entier
        document = raw if "<html" in raw.lower() else (
            "<!doctype html><html lang=\"fr\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{escape(str(row.get('title')))}</title>"
            f"{'<style>' + extra_css + '</style>' if extra_css else ''}"
            f"</head><body>{raw}"
            f"{'<script>' + extra_js + '</script>' if extra_js else ''}"
            "</body></html>")
        return web.Response(text=document, content_type="text/html")

    body = raw if mode == "html" else sanitize_html(markdown_lite(raw))
    if extra_css:
        body = f"<style>{extra_css}</style>" + body
    if extra_js:
        body += f"<script>{extra_js}</script>"
    draft = ("" if int(row.get("published") or 0) else
             '<div class="msg info">Brouillon — visible uniquement par les propriétaires.</div>')
    edit = (f'<p style="margin-top:28px"><a class="btn ghost" '
            f'href="/studio?page={slug}">✏️ Modifier cette page</a></p>'
            if is_owner(session) else "")
    return page(str(row.get("title") or "Page"),
                f'{draft}<h2 class="section">{escape(str(row.get("title")))}</h2>'
                f'<div class="card prose">{body}</div>{edit}', session)


def studio_guard(request: web.Request) -> dict:
    session = require_session(request)
    if not is_owner(session):
        raise web.HTTPFound("/dashboard?error=Le studio est réservé aux propriétaires.")
    return session


# --------------------------------------------------------------------------- #
#  Serveurs partenaires — widgets Discord, page publique /collab
# --------------------------------------------------------------------------- #
PARTNER_SCHEMA = """CREATE TABLE IF NOT EXISTS site_partners (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    label      TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    server_id  TEXT NOT NULL,
    invite     TEXT NOT NULL DEFAULT '',
    position   INTEGER NOT NULL DEFAULT 100,
    created_at TEXT NOT NULL
)"""


async def partners_setup() -> None:
    await STATE.db.execute(PARTNER_SCHEMA)


def parse_widget(raw: str) -> str:
    """Accepte un <iframe> complet, une adresse de widget, ou juste l'identifiant."""
    text = (raw or "").strip()
    found = (re.search(r"widget\?id=(\d{15,25})", text)
             or re.search(r"\b(\d{15,25})\b", text))
    return found.group(1) if found else ""


async def partners_list() -> list[dict]:
    rows = await STATE.db.fetchall(
        "SELECT * FROM site_partners ORDER BY position, id")
    return [row_dict(r) or {} for r in rows or []]


async def page_collab(request: web.Request) -> web.Response:
    """Page publique : tous les serveurs partenaires."""
    session = session_of(request)
    partners = await partners_list()
    titre = await config_get("collab_titre", "Nos serveurs partenaires")
    intro = await config_get(
        "collab_intro",
        "Des communautés qu'on aime et qu'on vous recommande. "
        "Rejoignez-les directement depuis cette page.")

    cartes = []
    for item in partners:
        invite = str(item.get("invite") or "")
        bouton = (f'<a class="btn" href="{escape(invite)}" target="_blank" '
                  f'rel="noopener">Rejoindre</a>' if invite.startswith("http") else "")
        cartes.append(f"""
<article class="collab-card reveal">
  <div class="collab-head">
    <h3>{escape(str(item.get('label') or 'Serveur Discord'))}</h3>
    {f'<p>{escape(str(item.get("note")))}</p>' if item.get("note") else ''}
  </div>
  <div class="collab-widget">
    <iframe src="https://discord.com/widget?id={escape(str(item['server_id']))}&theme=dark"
            width="100%" height="420" allowtransparency="true" frameborder="0"
            loading="lazy" title="{escape(str(item.get('label') or 'Serveur'))}"
            sandbox="allow-popups allow-popups-to-escape-sandbox allow-same-origin allow-scripts">
    </iframe>
  </div>
  {f'<div class="collab-foot">{bouton}</div>' if bouton else ''}
</article>""")

    admin = (f'<p style="margin-top:28px"><a class="btn ghost" href="/studio#collab">'
             f'Gérer les partenaires</a></p>' if is_owner(session) else "")
    body = f"""
<section class="collab-hero reveal">
  <p class="eyebrow">Communauté</p>
  <h1>{escape(titre)}</h1>
  <p class="lead">{escape(intro)}</p>
</section>
<div class="collab-grid">
  {''.join(cartes) or '<p class="sub">Aucun serveur partenaire pour l\'instant.</p>'}
</div>{admin}"""
    return page(titre, body, session, wide=True)


async def partners_add(request: web.Request) -> web.Response:
    session = studio_guard(request)
    data = await request.post()
    server_id = parse_widget(str(data.get("widget", "")))
    if not server_id:
        raise web.HTTPFound("/studio?error=Colle l'iframe du widget, son adresse, "
                            "ou l'identifiant du serveur.")
    invite = str(data.get("invite", "")).strip()[:200]
    if invite and not invite.startswith("http"):
        invite = "https://discord.gg/" + invite.lstrip("/")
    rows = await partners_list()
    await STATE.db.execute(
        "INSERT INTO site_partners(label, note, server_id, invite, position, created_at)"
        " VALUES(?, ?, ?, ?, ?, ?)",
        (str(data.get("label", "")).strip()[:60] or "Serveur Discord",
         str(data.get("note", "")).strip()[:160], server_id, invite,
         (rows[-1]["position"] + 10) if rows else 10, stamp()))
    raise web.HTTPFound("/studio?ok=Serveur partenaire ajouté.#collab")


async def partners_move(request: web.Request) -> web.Response:
    """Monte ou descend un partenaire dans la liste."""
    session = studio_guard(request)
    data = await request.post()
    try:
        cible = int(str(data.get("id", "0")))
    except ValueError:
        raise web.HTTPFound("/studio#collab")
    sens = -1 if str(data.get("sens")) == "haut" else 1
    rows = await partners_list()
    index = next((i for i, r in enumerate(rows) if int(r["id"]) == cible), None)
    if index is None:
        raise web.HTTPFound("/studio#collab")
    voisin = index + sens
    if 0 <= voisin < len(rows):
        rows[index], rows[voisin] = rows[voisin], rows[index]
    for rang, item in enumerate(rows, start=1):
        await STATE.db.execute("UPDATE site_partners SET position = ? WHERE id = ?",
                               (rang * 10, item["id"]))
    raise web.HTTPFound("/studio?ok=Ordre mis à jour.#collab")


async def partners_delete(request: web.Request) -> web.Response:
    session = studio_guard(request)
    data = await request.post()
    raw = str(data.get("id", "0"))
    await STATE.db.execute("DELETE FROM site_partners WHERE id = ?",
                           (int(raw) if raw.isdigit() else 0,))
    raise web.HTTPFound("/studio?ok=Partenaire retiré.#collab")


async def partners_texts(request: web.Request) -> web.Response:
    session = studio_guard(request)
    data = await request.post()
    await config_set("collab_titre", str(data.get("titre", "")).strip()[:120])
    await config_set("collab_intro", str(data.get("intro", "")).strip()[:400])
    raise web.HTTPFound("/studio?ok=Textes de la page enregistrés.#collab")


async def partners_panel() -> str:
    """Section du studio."""
    rows = await partners_list()
    titre = await config_get("collab_titre", "")
    intro = await config_get("collab_intro", "")
    lignes = "".join(f"""<tr>
  <td><b>{escape(str(p.get('label')))}</b>
    {f'<br><small style="color:var(--mute)">{escape(str(p.get("note")))}</small>' if p.get('note') else ''}</td>
  <td><code>{escape(str(p['server_id']))}</code></td>
  <td>{'<a href="' + escape(str(p['invite'])) + '" target="_blank">lien</a>' if p.get('invite') else '—'}</td>
  <td style="white-space:nowrap">
    <form method="post" action="/studio/partenaire/ordre" style="display:inline;margin:0">
      <input type="hidden" name="id" value="{p['id']}">
      <input type="hidden" name="sens" value="haut">
      <button class="btn ghost" title="Monter">▲</button></form>
    <form method="post" action="/studio/partenaire/ordre" style="display:inline;margin:0">
      <input type="hidden" name="id" value="{p['id']}">
      <input type="hidden" name="sens" value="bas">
      <button class="btn ghost" title="Descendre">▼</button></form>
    <form method="post" action="/studio/partenaire/supprimer" style="display:inline;margin:0"
          onsubmit="return confirm('Retirer ce partenaire ?')">
      <input type="hidden" name="id" value="{p['id']}">
      <button class="btn ghost">✕</button></form></td></tr>""" for p in rows)

    return f"""
<h2 class="section" id="collab">🤝 Serveurs partenaires</h2>
<div class="card">
  <p class="sub" style="color:var(--mute)">Colle l'<b>iframe</b> du widget Discord, son
   adresse, ou simplement l'identifiant du serveur. La page publique est
   <a href="/collab">/collab</a>.</p>
  <form method="post" action="/studio/partenaire"
        style="display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
               margin-top:14px">
    <div style="grid-column:1/-1"><label>Widget Discord</label>
      <input name="widget" required
             placeholder='&lt;iframe src="https://discord.com/widget?id=1549105228358352896&amp;theme=dark" …&gt;'></div>
    <div><label>Nom affiché</label><input name="label" placeholder="NiesMC"></div>
    <div><label>Invitation (facultatif)</label>
      <input name="invite" placeholder="discord.gg/xxxx"></div>
    <div style="grid-column:1/-1"><label>Petite description</label>
      <input name="note" maxlength="160" placeholder="Serveur Minecraft francophone"></div>
    <div><button class="btn">Ajouter</button></div>
  </form>
  <div style="overflow:auto;margin-top:16px"><table>
    <tr><th>Serveur</th><th>Identifiant</th><th>Invitation</th><th>Ordre</th></tr>
    {lignes or "<tr><td colspan='4'>Aucun partenaire. Ajoute le premier ci-dessus.</td></tr>"}
  </table></div>
</div>

<div class="card" style="margin-top:16px">
  <form method="post" action="/studio/partenaire/textes">
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">
      <div><label>Titre de la page</label><input name="titre" value="{escape(titre)}"
           placeholder="Nos serveurs partenaires"></div>
    </div>
    <label>Texte d'introduction</label>
    <textarea name="intro" rows="2">{escape(intro)}</textarea>
    <button class="btn" style="margin-top:12px">Enregistrer les textes</button>
  </form>
</div>"""


async def page_studio(request: web.Request) -> web.Response:
    session = studio_guard(request)
    error, notice = request.query.get("error", ""), request.query.get("ok", "")
    editing = slugify(request.query.get("page", ""))
    current = row_dict(await STATE.db.fetchone(
        "SELECT * FROM site_pages WHERE slug = ?", (editing,))) if editing else None

    medias = studio_files()
    pages = [row_dict(r) or {} for r in await STATE.db.fetchall(
        "SELECT * FROM site_pages ORDER BY position, title") or []]
    links = [row_dict(r) or {} for r in await STATE.db.fetchall(
        "SELECT * FROM site_links ORDER BY position, id") or []]

    rows_pages = "".join(
        f"""<tr><td><a href="/p/{p['slug']}">{escape(str(p['title']))}</a></td>
        <td><code>/p/{p['slug']}</code></td>
        <td>{'publiée' if int(p.get('published') or 0) else 'brouillon'}</td>
        <td>{'oui' if int(p.get('in_nav') or 0) else 'non'}</td>
        <td>{escape(str(p.get('access') or 'public'))}</td>
        <td><a class="btn ghost" href="/studio?page={p['slug']}">Modifier</a>
            <form method="post" action="/studio/page/duplicate" style="display:inline;margin:0">
              <input type="hidden" name="slug" value="{p['slug']}">
              <button class="btn ghost">Dupliquer</button></form></td>
        <td><form method="post" action="/studio/page/delete" style="margin:0"
             onsubmit="return confirm('Supprimer cette page ?')">
             <input type="hidden" name="slug" value="{p['slug']}">
             <button class="btn ghost">Supprimer</button></form></td></tr>"""
        for p in pages) or "<tr><td colspan='7'>Aucune page pour l'instant.</td></tr>"

    redirects = [row_dict(r) or {} for r in await STATE.db.fetchall(
        "SELECT * FROM site_redirects ORDER BY hits DESC, path") or []]
    rows_redirects = "".join(
        f"""<tr><td><a href="/{d['path']}" target="_blank"><code>/{escape(str(d['path']))}</code></a></td>
        <td style="max-width:320px;overflow:hidden;text-overflow:ellipsis">
          <a href="{escape(str(d['target']))}" target="_blank" rel="noopener">
          {escape(str(d['target'])[:70])}</a></td>
        <td>{int(d.get('hits') or 0)}</td>
        <td><form method="post" action="/studio/redirect/delete" style="margin:0">
            <input type="hidden" name="path" value="{escape(str(d['path']))}">
            <button class="btn ghost">Supprimer</button></form></td></tr>"""
        for d in redirects) or "<tr><td colspan='4'>Aucun raccourci.</td></tr>"

    rows_links = "".join(
        f"""<tr><td>{escape(str(l['label']))}</td><td><code>{escape(str(l['url']))}</code></td>
        <td>{escape(str(l.get('style') or 'lien'))}</td>
        <td>{escape(str(l.get('access') or 'public'))}</td><td>{l.get('position')}</td>
        <td><form method="post" action="/studio/link/delete" style="margin:0">
            <input type="hidden" name="id" value="{l['id']}">
            <button class="btn ghost">Retirer</button></form></td></tr>"""
        for l in links) or "<tr><td colspan='6'>Aucun bouton ajouté.</td></tr>"

    def selected(value: str, expected: str) -> str:
        return " selected" if value == expected else ""

    access_now = str((current or {}).get("access") or "public")
    mode_now = str((current or {}).get("mode") or "riche")
    body = f"""
{f'<div class="msg err">{escape(error)}</div>' if error else ''}
{f'<div class="msg ok">{escape(notice)}</div>' if notice else ''}
<h2 class="section">🎨 Studio du site</h2>
<p class="sub" style="color:var(--mute)">Réservé aux propriétaires déclarés dans
 <code>BOT_OWNER_IDS</code>. Tes modifications sont visibles immédiatement.</p>

<h2 class="section">✍️ {'Modifier' if current else 'Nouvelle'} page</h2>
<div class="card"><form method="post" action="/studio/page" id="pageform">
  <input type="hidden" name="original" value="{escape(editing)}">
  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
    <div><label>Titre</label>
      <input name="title" required maxlength="80"
             value="{escape(str((current or {}).get('title') or ''))}"></div>
    <div><label>Adresse (laisser vide = d'après le titre)</label>
      <input name="slug" maxlength="40" placeholder="regles-du-serveur"
             value="{escape(str((current or {}).get('slug') or ''))}"></div>
    <div><label>Visible par</label><select name="access">
      <option value="public"{selected(access_now, 'public')}>Tout le monde</option>
      <option value="connecte"{selected(access_now, 'connecte')}>Membres connectés</option>
      <option value="proprietaire"{selected(access_now, 'proprietaire')}>Propriétaires</option>
    </select></div>
    <div><label>Type de page</label><select name="mode">
      <option value="riche"{selected(mode_now, 'riche')}>Éditeur visuel (sécurisé)</option>
      <option value="html"{selected(mode_now, 'html')}>HTML brut dans le site</option>
      <option value="complete"{selected(mode_now, 'complete')}>Page HTML complète (sans en-tête du site)</option>
      <option value="fichier"{selected(mode_now, 'fichier')}>Fichier HTML déposé (site statique)</option>
    </select></div>
    <div><label>Position dans le menu</label>
      <input name="position" type="number" value="{int((current or {}).get('position') or 100)}"></div>
  </div>
  <label class="asbot" style="margin-top:10px"><input type="checkbox" name="published"
    {'checked' if (current is None or int((current or {}).get('published') or 0)) else ''}>
    page publiée (décoche pour la garder en brouillon)</label>
  <label class="asbot" style="margin-top:6px"><input type="checkbox" name="in_nav"
    {'checked' if (current is None or int((current or {}).get('in_nav') or 0)) else ''}>
    afficher dans le menu du site</label>
  <label style="margin-top:10px">Contenu</label>
  <div id="staticnote" class="msg info" style="display:none">
    📄 Cette page affiche le fichier <b><span id="staticfile"></span></b> tel quel,
    comme un site statique. L'éditeur ci-dessous est ignoré tant que le type reste
    « Fichier HTML déposé ».
    <a id="staticopen" href="#" target="_blank">Ouvrir le fichier</a>
  </div>
  {editor_block(current, medias)}
  <div style="margin-top:16px;display:flex;gap:10px;flex-wrap:wrap">
    <button class="btn" id="savebtn">{'Enregistrer' if current else 'Créer la page'}</button>
    {f'<a class="btn ghost" href="/p/{editing}" target="_blank">Voir la page</a>' if current else ''}
    {'<a class="btn ghost" href="/studio">Nouvelle page</a>' if current else ''}
  </div>
</form></div>
<script>{EDITOR_JS}</script>

<h2 class="section">📄 Pages ({len(pages)})</h2>
<div class="card"><div style="overflow:auto"><table>
  <tr><th>Titre</th><th>Adresse</th><th>État</th><th>Menu</th><th>Accès</th>
      <th></th><th></th></tr>
  {rows_pages}</table></div></div>

<h2 class="section">🔗 Boutons du menu</h2>
<div class="card">
  <form method="post" action="/studio/link"
        style="display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(170px,1fr))">
    <div><label>Texte</label><input name="label" required maxlength="40"
         placeholder="Nous rejoindre"></div>
    <div><label>Lien</label><input name="url" required placeholder="https://discord.gg/…"></div>
    <div><label>Apparence</label><select name="style">
      <option value="lien">Lien simple</option><option value="bouton">Bouton coloré</option>
    </select></div>
    <div><label>Visible par</label><select name="access">
      <option value="public">Tout le monde</option>
      <option value="connecte">Membres connectés</option>
      <option value="proprietaire">Propriétaires</option>
    </select></div>
    <div><label>Position</label><input name="position" type="number" value="100"></div>
    <div style="display:flex;align-items:flex-end"><button class="btn">Ajouter</button></div>
  </form>
  <div style="overflow:auto;margin-top:16px"><table>
    <tr><th>Texte</th><th>Lien</th><th>Apparence</th><th>Accès</th><th>Position</th><th></th></tr>
    {rows_links}</table></div>
</div>

<h2 class="section">🤖 Apparence des bots</h2>
{bots_panel()}

{await partners_panel()}

<h2 class="section">🔗 Raccourcis d'adresse</h2>
<div class="card">
  <p class="sub" style="color:var(--mute)">Une adresse courte sur ton domaine qui renvoie
   où tu veux. Exemple : <code>/discord</code> →
   <code>https://discord.gg/NnF3RSWywT</code>. Autant que tu veux.</p>
  <form method="post" action="/studio/redirect"
        style="display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
               margin-top:14px">
    <div><label>Adresse courte</label>
      <input name="path" required placeholder="discord" maxlength="60"></div>
    <div style="grid-column:span 2"><label>Destination</label>
      <input name="target" required placeholder="https://discord.gg/NnF3RSWywT"></div>
    <div style="display:flex;align-items:flex-end"><button class="btn">Créer</button></div>
  </form>
  <div style="overflow:auto;margin-top:16px"><table>
    <tr><th>Adresse</th><th>Destination</th><th>Clics</th><th></th></tr>
    {rows_redirects}</table></div>
</div>

<h2 class="section">🏠 Textes de l'accueil</h2>
<div class="card"><form method="post" action="/studio/home">
  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">
    <div><label>Sur-titre</label><input name="eyebrow"
         value="{escape(SITE_HOME.get('eyebrow') or '')}" placeholder="Tableau de bord {BRAND}"></div>
    <div><label>Titre principal</label><input name="titre"
         value="{escape(SITE_HOME.get('titre') or '')}" placeholder="Ton serveur Discord, piloté depuis ton navigateur."></div>
  </div>
  <label>Texte d'introduction</label>
  <textarea name="sous_titre" rows="3">{escape(SITE_HOME.get('sous_titre') or '')}</textarea>
  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">
    <div><label>Bouton principal</label><input name="bouton"
         value="{escape(SITE_HOME.get('bouton') or '')}" placeholder="Se connecter"></div>
    <div><label>Lien du bouton</label><input name="bouton_url"
         value="{escape(SITE_HOME.get('bouton_url') or '')}" placeholder="/login"></div>
  </div>
  <p class="sub" style="color:var(--mute);margin-top:8px">Laisse vide pour garder le texte
   d'origine.</p>
  <button class="btn" style="margin-top:14px">Enregistrer l'accueil</button>
</form></div>"""
    return page("Studio", body, session, wide=True)


async def studio_save_page(request: web.Request) -> web.Response:
    session = studio_guard(request)
    data = await request.post()
    title = str(data.get("title", "")).strip()[:80]
    slug = slugify(str(data.get("slug", "")) or title)
    original = slugify(str(data.get("original", "")))
    if not title or not slug:
        raise web.HTTPFound("/studio?error=Titre ou adresse invalide.")
    if slug in ("login", "logout", "dashboard", "account", "client", "tv", "studio",
                "setup", "therms", "conditions", "confidentialite"):
        raise web.HTTPFound("/studio?error=Cette adresse est déjà utilisée par le site.")
    body = str(data.get("body", ""))[:60000]
    try:
        position = int(str(data.get("position", "100")) or 100)
    except ValueError:
        position = 100
    access = str(data.get("access", "public"))
    if access not in ("public", "connecte", "proprietaire"):
        access = "public"
    in_nav = 1 if data.get("in_nav") else 0
    published = 1 if data.get("published") else 0
    mode = str(data.get("mode", "riche"))
    if mode not in ("riche", "html", "complete", "fichier"):
        mode = "riche"
    extra_css = str(data.get("css", ""))[:20000]
    extra_js = str(data.get("js", ""))[:20000]

    if original and original != slug:
        await STATE.db.execute("DELETE FROM site_pages WHERE slug = ?", (original,))
    await STATE.db.execute(
        "INSERT OR REPLACE INTO site_pages(slug, title, body, in_nav, position, access,"
        " published, mode, css, js, updated_at)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (slug, title, body, in_nav, position, access, published, mode,
         extra_css, extra_js, stamp()))
    await studio_reload()
    raise web.HTTPFound(f"/studio?page={slug}&ok=Page enregistrée : /p/{slug}")


async def studio_delete_page(request: web.Request) -> web.Response:
    session = studio_guard(request)
    data = await request.post()
    await STATE.db.execute("DELETE FROM site_pages WHERE slug = ?",
                           (slugify(str(data.get("slug", ""))),))
    await studio_reload()
    raise web.HTTPFound("/studio?ok=Page supprimée.")


async def studio_add_link(request: web.Request) -> web.Response:
    session = studio_guard(request)
    data = await request.post()
    label = str(data.get("label", "")).strip()[:40]
    url = str(data.get("url", "")).strip()[:300]
    if not label or not url:
        raise web.HTTPFound("/studio?error=Texte ou lien manquant.")
    if not url.startswith(("/", "http://", "https://")):
        url = "https://" + url
    style = "bouton" if str(data.get("style")) == "bouton" else "lien"
    access = str(data.get("access", "public"))
    if access not in ("public", "connecte", "proprietaire"):
        access = "public"
    try:
        position = int(str(data.get("position", "100")) or 100)
    except ValueError:
        position = 100
    await STATE.db.execute(
        "INSERT INTO site_links(label, url, position, style, access) VALUES(?, ?, ?, ?, ?)",
        (label, url, position, style, access))
    await studio_reload()
    raise web.HTTPFound("/studio?ok=Bouton ajouté au menu.")


async def studio_delete_link(request: web.Request) -> web.Response:
    session = studio_guard(request)
    data = await request.post()
    raw = str(data.get("id", "0"))
    await STATE.db.execute("DELETE FROM site_links WHERE id = ?",
                           (int(raw) if raw.isdigit() else 0,))
    await studio_reload()
    raise web.HTTPFound("/studio?ok=Bouton retiré.")


# Adresses déjà prises par le site : un raccourci ne peut pas les remplacer.
RESERVED_PATHS = {
    "", "login", "logout", "dashboard", "account", "client", "tv", "studio", "setup",
    "therms", "reglement", "conditions", "confidentialite", "privacy", "terms",
    "guild", "api", "oauth", "p", "u", "logo.png", "favicon.ico", "vendor",
    "voc-invit", "voc-invite", "panel",
}


def clean_path(raw: str) -> str:
    """« /Discord » ou « Discord/ » deviennent « discord »."""
    text = (raw or "").strip().strip("/").lower()
    text = re.sub(r"[^a-z0-9/_-]", "-", text)
    return re.sub(r"-+", "-", text).strip("-/")[:60]


async def redirect_target(path: str) -> Optional[dict]:
    return row_dict(await STATE.db.fetchone(
        "SELECT * FROM site_redirects WHERE path = ?", (path,)))


async def page_redirect(request: web.Request) -> web.StreamResponse:
    """Attrape les adresses courtes : /discord, /yt, /boutique…"""
    path = clean_path(request.match_info.get("path", ""))
    if not path:
        raise web.HTTPNotFound()
    row = await redirect_target(path)
    if row is None:
        raise web.HTTPNotFound()
    try:
        await STATE.db.execute(
            "UPDATE site_redirects SET hits = hits + 1 WHERE path = ?", (path,))
    except Exception:
        pass
    raise web.HTTPFound(str(row["target"]))


async def studio_add_redirect(request: web.Request) -> web.Response:
    session = studio_guard(request)
    data = await request.post()
    path = clean_path(str(data.get("path", "")))
    target = str(data.get("target", "")).strip()[:500]
    if not path or not target:
        raise web.HTTPFound("/studio?error=Adresse ou destination manquante.")
    if path.split("/")[0] in RESERVED_PATHS:
        raise web.HTTPFound(f"/studio?error=/{path} est déjà utilisé par le site.")
    if not target.startswith(("http://", "https://", "/")):
        target = "https://" + target
    await STATE.db.execute(
        "INSERT OR REPLACE INTO site_redirects(path, target, hits, created_at) "
        "VALUES(?, ?, COALESCE((SELECT hits FROM site_redirects WHERE path = ?), 0), ?)",
        (path, target, path, stamp()))
    raise web.HTTPFound(f"/studio?ok=Raccourci enregistré : {public_url()}/{path}")


async def studio_delete_redirect(request: web.Request) -> web.Response:
    session = studio_guard(request)
    data = await request.post()
    await STATE.db.execute("DELETE FROM site_redirects WHERE path = ?",
                           (clean_path(str(data.get("path", ""))),))
    raise web.HTTPFound("/studio?ok=Raccourci supprimé.")


# --------------------------------------------------------------------------- #
#  Apparence des bots : pseudo, photo de profil, bannière
#  Concerne le bot principal et le bot vocal (voice_bot.py, accueil vocal).
# --------------------------------------------------------------------------- #
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def bot_clients() -> list[dict]:
    """Les clients Discord pilotables depuis le studio."""
    out = []
    if STATE.bot is not None:
        out.append({"key": "principal", "label": f"{BRAND} (bot principal)",
                    "client": STATE.bot, "note": "commandes, modération, musique"})
    voice = sys.modules.get("voice_bot")
    helper = getattr(voice, "HELPER", None) if voice else None
    client = getattr(helper, "client", None) if helper else None
    if client is not None:
        out.append({"key": "vocal", "label": "Bot vocal (accueil et vérification)",
                    "client": client,
                    "note": "celui qui parle dans les salons vocaux"})
    return out


def bot_by_key(key: str):
    for item in bot_clients():
        if item["key"] == key:
            return item
    return None


def bot_identity(client: Any) -> dict:
    """Pseudo, avatar et bannière actuels, pour l'affichage."""
    user = getattr(client, "user", None)
    if user is None:
        return {"name": "hors ligne", "avatar": "/logo.png", "banner": "", "id": ""}
    banner = ""
    try:
        banner = user.banner.url if getattr(user, "banner", None) else ""
    except Exception:
        banner = ""
    return {"name": str(user.name), "id": str(user.id),
            "avatar": user.display_avatar.url if user.display_avatar else "/logo.png",
            "banner": banner}


async def studio_save_bot(request: web.Request) -> web.Response:
    """Applique les changements d'apparence sur le bot choisi."""
    session = studio_guard(request)
    fields: dict[str, str] = {}
    images: dict[str, bytes] = {}

    if not request.content_type.startswith("multipart/"):
        # formulaire sans fichier (ou appel programmatique) : champs simples
        data = await request.post()
        fields = {key: str(value) for key, value in data.items()}
        return await apply_bot_identity(fields, images)

    reader = await request.multipart()
    async for part in reader:
        if part.name in ("avatar", "banner") and part.filename:
            suffix = Path(part.filename).suffix.lower()
            if suffix not in IMAGE_SUFFIXES:
                raise web.HTTPFound("/studio?error=Image : png, jpg, gif ou webp "
                                    "uniquement.")
            data = await part.read(decode=False)
            if len(data) > 10 * 1024 * 1024:
                raise web.HTTPFound("/studio?error=Image trop lourde (10 Mo maximum).")
            if data:
                images[part.name] = data
        else:
            fields[part.name] = (await part.text()).strip()

    return await apply_bot_identity(fields, images)


async def apply_bot_identity(fields: dict, images: dict) -> web.Response:
    """Applique pseudo / avatar / bannière une fois les champs lus."""
    entry = bot_by_key(fields.get("bot", ""))
    if entry is None:
        raise web.HTTPFound("/studio?error=Bot introuvable ou hors ligne.")
    user = getattr(entry["client"], "user", None)
    if user is None:
        raise web.HTTPFound("/studio?error=Ce bot n'est pas connecté.")

    changes: dict[str, Any] = {}
    pseudo = fields.get("username", "")[:32]
    if pseudo and pseudo != user.name:
        if len(pseudo) < 2:
            raise web.HTTPFound("/studio?error=Pseudo trop court (2 caractères minimum).")
        changes["username"] = pseudo
    if "avatar" in images:
        changes["avatar"] = images["avatar"]
    elif fields.get("clear_avatar"):
        changes["avatar"] = None
    if "banner" in images:
        changes["banner"] = images["banner"]
    elif fields.get("clear_banner"):
        changes["banner"] = None

    if not changes:
        raise web.HTTPFound("/studio?error=Rien à modifier.")

    try:
        await user.edit(**changes)
    except discord.HTTPException as exc:
        detail = (getattr(exc, "text", "") or str(exc))[:200]
        if "USERNAME_RATE_LIMIT" in detail or getattr(exc, "status", 0) == 429:
            detail = ("Discord limite les changements de pseudo à 2 par heure. "
                      "Réessaie plus tard.")
        elif "banner" in detail.lower():
            detail += " (la bannière peut exiger que l'application ait une image valide)"
        raise web.HTTPFound(f"/studio?error=Discord a refusé : {detail}")
    except Exception as exc:
        raise web.HTTPFound(f"/studio?error=Échec ({type(exc).__name__}).")

    done = ", ".join(
        {"username": "pseudo", "avatar": "photo de profil", "banner": "bannière"}[key]
        for key in changes)
    raise web.HTTPFound(f"/studio?ok={entry['label']} mis à jour : {done}.")


def bots_panel() -> str:
    """Section du studio : apparence de chaque bot."""
    blocks = []
    for entry in bot_clients():
        identity = bot_identity(entry["client"])
        banner = (f'<img src="{identity["banner"]}" alt="" '
                  f'style="width:100%;max-height:120px;object-fit:cover;border-radius:12px;'
                  f'margin-bottom:12px">' if identity["banner"] else "")
        blocks.append(f"""
<div class="card">
  {banner}
  <div style="display:flex;gap:14px;align-items:center;margin-bottom:14px">
    <img src="{identity['avatar']}" alt="" width="64" height="64"
         style="border-radius:50%;border:1px solid var(--line2)">
    <div><b>{escape(identity['name'])}</b>
      <small style="display:block;color:var(--mute)">{escape(entry['note'])}
      · <code>{identity['id']}</code></small></div>
  </div>
  <form method="post" action="/studio/bot" enctype="multipart/form-data">
    <input type="hidden" name="bot" value="{entry['key']}">
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
      <div><label>Pseudo</label>
        <input name="username" maxlength="32" value="{escape(identity['name'])}"></div>
      <div><label>Photo de profil</label><input type="file" name="avatar"
           accept=".png,.jpg,.jpeg,.gif,.webp"></div>
      <div><label>Bannière</label><input type="file" name="banner"
           accept=".png,.jpg,.jpeg,.gif,.webp"></div>
    </div>
    <div class="microw" style="margin-top:10px">
      <label class="asbot"><input type="checkbox" name="clear_avatar">
        remettre l'avatar par défaut</label>
      <label class="asbot"><input type="checkbox" name="clear_banner">
        retirer la bannière</label>
    </div>
    <button class="btn" style="margin-top:14px">Appliquer</button>
  </form>
</div>""")
    if not blocks:
        return ('<div class="card"><p class="sub">Aucun bot connecté pour le moment.</p>'
                '</div>')
    return "".join(blocks) + """
<p class="sub" style="color:var(--dim);margin-top:10px">
 Discord limite les changements de <b>pseudo à 2 par heure</b>. Les images acceptées
 font 10 Mo maximum (png, jpg, gif animé, webp). La bannière demande parfois que
 l'application possède déjà une image valide côté portail développeur.</p>"""


async def studio_save_home(request: web.Request) -> web.Response:
    session = studio_guard(request)
    data = await request.post()
    for key in ("titre", "sous_titre", "bouton", "bouton_url", "eyebrow"):
        await config_set(f"home_{key}", str(data.get(key, "")).strip()[:400])
    await studio_reload()
    raise web.HTTPFound("/studio?ok=Accueil mis à jour.")


# =========================================================================== #
#  MICRO — parler dans un salon vocal Discord depuis le navigateur
#
#  Le navigateur capte le micro (WebM/Opus) et l'envoie par WebSocket. FFmpeg
#  décode le flux en PCM 48 kHz stéréo, qui alimente une source audio branchée
#  sur le salon vocal : le bot parle avec ta voix.
#
#  Réservé aux propriétaires du bot (BOT_OWNER_IDS) et au propriétaire du
#  serveur Discord concerné.
# =========================================================================== #
FRAME_BYTES = 3840          # 20 ms en 48 kHz, 16 bits, stéréo
MIC_SILENCE = b"\x00" * FRAME_BYTES


class MicSource(discord.AudioSource):
    """Source audio alimentée en direct par le micro du navigateur."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False
        self.frames = 0

    def feed(self, data: bytes) -> None:
        self.buffer.extend(data)
        if len(self.buffer) > FRAME_BYTES * 250:        # ~5 s de retard maximum
            del self.buffer[:len(self.buffer) - FRAME_BYTES * 100]

    def read(self) -> bytes:
        if self.closed:
            return b""
        if len(self.buffer) < FRAME_BYTES:
            return MIC_SILENCE                           # silence plutôt que coupure
        chunk = bytes(self.buffer[:FRAME_BYTES])
        del self.buffer[:FRAME_BYTES]
        self.frames += 1
        return chunk

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self.closed = True
        self.buffer.clear()


def voice_owner(member: Any, guild: Any) -> bool:
    """Propriétaire du bot/site, ou propriétaire du serveur Discord."""
    user_id = getattr(member, "id", 0)
    return user_id in owner_ids() or user_id == getattr(guild, "owner_id", None)


async def mic_stop(guild_id: int) -> None:
    session = STATE.mic.pop(guild_id, None)
    if session is None:
        return
    source = session.get("source")
    if source is not None:
        source.closed = True
    proc = session.get("proc")
    if proc is not None and proc.returncode is None:
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
    bridge = session.get("bridge")
    if bridge is not None:
        bridge.stop()
    client = session.get("client")
    if client is not None:
        try:
            if getattr(client, "is_listening", None) and client.is_listening():
                client.stop_listening()
            if client.is_playing():
                client.stop()
            await client.disconnect(force=False)
        except Exception:
            log.debug("Micro : déconnexion impossible", exc_info=True)


async def mic_pump(guild_id: int, proc, source: MicSource) -> None:
    """Lit la sortie PCM de FFmpeg et la pousse vers le salon vocal."""
    try:
        while True:
            chunk = await proc.stdout.read(FRAME_BYTES)
            if not chunk:
                break
            source.feed(chunk)
    except Exception:
        log.debug("Micro : lecture FFmpeg interrompue", exc_info=True)


# --------------------------------------------------------------------------- #
#  Écoute : recevoir l'audio du salon vocal et l'envoyer au navigateur
#  discord.py ne sait pas recevoir d'audio ; l'extension « discord-ext-voice-recv »
#  ajoute cette capacité. Sans elle, le micro fonctionne quand même, mais en
#  émission seulement.
# --------------------------------------------------------------------------- #
try:
    from discord.ext import voice_recv            # type: ignore
except Exception:                                  # extension absente
    voice_recv = None                              # type: ignore

try:
    import numpy as _np                            # mélange rapide des voix
except Exception:
    _np = None


def listening_available() -> bool:
    return voice_recv is not None


def mix_frames(frames: list) -> bytes:
    """Additionne les voix de plusieurs personnes en une seule piste."""
    frames = [f for f in frames if f]
    if not frames:
        return b""
    if len(frames) == 1:
        return frames[0]
    if _np is not None:
        total = _np.frombuffer(frames[0], dtype="<i2").astype(_np.int32)
        for frame in frames[1:]:
            other = _np.frombuffer(frame, dtype="<i2").astype(_np.int32)
            size = min(len(total), len(other))
            total[:size] += other[:size]
        _np.clip(total, -32768, 32767, out=total)
        return total.astype("<i2").tobytes()
    from array import array
    total = array("h", frames[0])
    for frame in frames[1:]:
        other = array("h", frame)
        for index in range(min(len(total), len(other))):
            value = total[index] + other[index]
            total[index] = -32768 if value < -32768 else min(value, 32767)
    return total.tobytes()


class VoiceBridge:
    """Collecte ce que disent les membres et l'envoie au navigateur."""

    def __init__(self, websocket: Any, me_id: int) -> None:
        self.websocket = websocket
        self.me_id = me_id
        self.buffers: dict[int, deque] = {}
        self.names: dict[int, str] = {}
        self.speaking: set[int] = set()
        self.running = True
        self.sent = 0

    def on_voice(self, member: Any, data: Any) -> None:
        """Appelé par discord-ext-voice-recv pour chaque paquet reçu."""
        pcm = getattr(data, "pcm", None)
        if not pcm or member is None or not self.running:
            return
        if member.bot:
            return                       # on ne renvoie pas le bot à lui-même
        queue = self.buffers.setdefault(member.id, deque(maxlen=25))
        queue.append(pcm)
        self.names[member.id] = member.display_name

    async def pump(self) -> None:
        """Toutes les 20 ms : mélange les voix et envoie une trame au navigateur."""
        try:
            while self.running:
                await asyncio.sleep(0.02)
                frames, talking = [], []
                for user_id, queue in list(self.buffers.items()):
                    if queue:
                        frames.append(queue.popleft())
                        talking.append(user_id)
                if not frames:
                    continue
                mixed = mix_frames(frames)
                if not mixed:
                    continue
                try:
                    await self.websocket.send_bytes(mixed)
                    self.sent += 1
                except Exception:
                    break
                current = set(talking)
                if current != self.speaking:
                    self.speaking = current
                    try:
                        await self.websocket.send_json({
                            "speaking": [self.names.get(uid, "?") for uid in current]})
                    except Exception:
                        break
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        self.running = False
        self.buffers.clear()


async def connect_voice(channel: Any, listen: bool) -> Any:
    """Rejoint le salon, en mode « écoute » si l'extension est présente."""
    guild = channel.guild
    client = guild.voice_client
    want_recv = listen and listening_available()
    good_type = (not want_recv) or (voice_recv is not None
                                    and isinstance(client, voice_recv.VoiceRecvClient))
    if client is not None and not good_type:
        await client.disconnect(force=True)       # mauvais type de client : on refait
        client = None
        await asyncio.sleep(0.4)
    if client is None:
        if want_recv:
            return await channel.connect(timeout=20, reconnect=False,
                                         cls=voice_recv.VoiceRecvClient)
        return await channel.connect(timeout=20, reconnect=False, self_deaf=True)
    if client.channel is None or client.channel.id != channel.id:
        await client.move_to(channel)
    return client


async def api_mic_ws(request: web.Request) -> web.WebSocketResponse:
    """Flux audio du navigateur vers le salon vocal."""
    session, guild, member, level = await guild_context(request)
    if not voice_owner(member, guild):
        raise ApiError("Réservé au propriétaire du serveur ou du bot.", 403)

    channel = guild.get_channel(int(request.query.get("channel", "0") or 0))
    if not isinstance(channel, discord.VoiceChannel):
        raise ApiError("Salon vocal introuvable.", 404)
    perms = channel.permissions_for(guild.me)
    if not (perms.connect and perms.speak):
        raise ApiError("Il me manque « Se connecter » ou « Parler » sur ce salon.", 403)

    websocket = web.WebSocketResponse(heartbeat=25, max_msg_size=4 * 1024 * 1024)
    await websocket.prepare(request)

    await mic_stop(guild.id)
    listen = request.query.get("listen", "1") not in ("0", "false", "no")
    try:
        client = await connect_voice(channel, listen)
    except Exception as exc:
        await websocket.send_json({"error": f"Connexion vocale impossible ({exc})."})
        await websocket.close()
        return websocket

    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg_path(), "-hide_banner", "-loglevel", "error",
            "-fflags", "+nobuffer", "-flags", "low_delay",
            "-i", "pipe:0", "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
    except FileNotFoundError:
        await websocket.send_json({"error": "FFmpeg est introuvable sur le serveur."})
        await websocket.close()
        return websocket

    source = MicSource()
    STATE.mic[guild.id] = {"proc": proc, "source": source, "client": client,
                           "user_id": member.id, "channel": channel.id,
                           "started": time.time()}
    pump = asyncio.create_task(mic_pump(guild.id, proc, source))
    try:
        if client.is_playing():
            client.stop()
        client.play(source)
    except Exception as exc:
        await websocket.send_json({"error": f"Lecture impossible ({exc})."})
        await mic_stop(guild.id)
        await websocket.close()
        return websocket

    bridge = None
    bridge_task = None
    if listen and listening_available() and isinstance(client, voice_recv.VoiceRecvClient):
        bridge = VoiceBridge(websocket, member.id)
        try:
            if client.is_listening():
                client.stop_listening()
            client.listen(voice_recv.BasicSink(bridge.on_voice))
            bridge_task = asyncio.create_task(bridge.pump())
            STATE.mic[guild.id]["bridge"] = bridge
        except Exception:
            log.debug("Micro : écoute impossible", exc_info=True)
            bridge = None

    await websocket.send_json({
        "ok": True, "channel": channel.name,
        "listening": bridge is not None,
        "note": ("" if bridge is not None else
                 "Écoute indisponible : installe « discord-ext-voice-recv » "
                 "sur le serveur pour entendre les autres.")})
    log.info("Micro : %s parle dans %s (%s) — écoute : %s", member, channel.name,
             guild.name, "oui" if bridge is not None else "non")

    try:
        async for message in websocket:
            if message.type == web.WSMsgType.BINARY:
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.write(message.data)
                    try:
                        await proc.stdin.drain()
                    except Exception:
                        break
            elif message.type == web.WSMsgType.TEXT and message.data == "stop":
                break
            elif message.type == web.WSMsgType.ERROR:
                break
    except Exception:
        log.debug("Micro : flux interrompu", exc_info=True)
    finally:
        pump.cancel()
        if bridge is not None:
            bridge.stop()
        if bridge_task is not None:
            bridge_task.cancel()
        await mic_stop(guild.id)
        log.info("Micro : session terminée sur %s", guild.name)
    return websocket


async def api_mic_stop(request: web.Request) -> web.Response:
    session, guild, member, level = await guild_context(request)
    if not voice_owner(member, guild):
        raise ApiError("Réservé au propriétaire du serveur ou du bot.", 403)
    await mic_stop(guild.id)
    return web.json_response({"ok": True})


MIC_JS = r"""
let ws = null, recorder = null, stream = null, meter = null, audioCtx = null;
const btn = document.getElementById('micbtn');
const stopBtn = document.getElementById('micstop');
const statusEl = document.getElementById('micstatus');
const bar = document.getElementById('miclevel');
const pick = document.getElementById('micchannel');
const gid = (document.querySelector('.console') || {}).dataset?.guild
            || document.body.dataset.guild;

function setState(on, text){
  statusEl.textContent = text;
  btn.style.display = on ? 'none' : 'inline-flex';
  stopBtn.style.display = on ? 'inline-flex' : 'none';
  document.getElementById('micdot').className = on ? 'dot on' : 'dot';
}

// ---- lecture de ce que disent les autres --------------------------------
let outCtx = null, playAt = 0, gain = null;

function ensureOutput(){
  if (outCtx) return outCtx;
  outCtx = new (window.AudioContext || window.webkitAudioContext)({sampleRate: 48000});
  gain = outCtx.createGain();
  gain.gain.value = parseFloat((document.getElementById('vol') || {}).value || '1');
  gain.connect(outCtx.destination);
  playAt = outCtx.currentTime + 0.12;        // petite avance pour absorber le réseau
  return outCtx;
}

function playChunk(buffer){
  if (document.getElementById('deaf') && document.getElementById('deaf').checked) return;
  const ctx = ensureOutput();
  const pcm = new Int16Array(buffer);
  const frames = pcm.length / 2;             // 48 kHz, stéréo, 16 bits
  if (!frames) return;
  const audio = ctx.createBuffer(2, frames, 48000);
  const left = audio.getChannelData(0), right = audio.getChannelData(1);
  for (let i = 0; i < frames; i++){
    left[i] = pcm[i * 2] / 32768;
    right[i] = pcm[i * 2 + 1] / 32768;
  }
  const node = ctx.createBufferSource();
  node.buffer = audio; node.connect(gain);
  const now = ctx.currentTime;
  if (playAt < now + 0.02) playAt = now + 0.06;      // on a pris du retard
  if (playAt > now + 0.6) playAt = now + 0.2;        // ou trop d'avance
  node.start(playAt);
  playAt += audio.duration;
}

function stopOutput(){
  if (outCtx){ outCtx.close().catch(() => {}); outCtx = null; gain = null; }
  const who = document.getElementById('whospeaks');
  if (who) who.textContent = '';
}

async function start(){
  const channel = pick.value;
  if (!channel) return alert('Choisis un salon vocal.');
  try {
    stream = await navigator.mediaDevices.getUserMedia({audio: {
      echoCancellation: true, noiseSuppression: true, autoGainControl: true,
      channelCount: 2, sampleRate: 48000}});
  } catch (e) {
    return alert("Micro refusé par le navigateur : " + e.message
      + "\nLe site doit être en https pour accéder au micro.");
  }

  // vumètre
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const src = audioCtx.createMediaStreamSource(stream);
  const analyser = audioCtx.createAnalyser();
  analyser.fftSize = 512; src.connect(analyser);
  const data = new Uint8Array(analyser.frequencyBinCount);
  meter = setInterval(() => {
    analyser.getByteTimeDomainData(data);
    let peak = 0;
    for (const v of data) peak = Math.max(peak, Math.abs(v - 128));
    bar.style.width = Math.min(100, peak / 64 * 100) + '%';
  }, 100);

  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const listen = document.getElementById('listen').checked ? '1' : '0';
  ws = new WebSocket(
    `${scheme}://${location.host}/api/guild/${gid}/mic?channel=${channel}&listen=${listen}`);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus' : 'audio/webm';
    recorder = new MediaRecorder(stream, {mimeType: mime, audioBitsPerSecond: 96000});
    recorder.ondataavailable = async (e) => {
      if (e.data.size && ws && ws.readyState === 1) ws.send(await e.data.arrayBuffer());
    };
    recorder.start(200);                       // un paquet toutes les 200 ms
    setState(true, 'En direct dans le salon vocal');
  };
  ws.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer){ playChunk(e.data); return; }
    try {
      const d = JSON.parse(e.data);
      if (d.error){ alert(d.error); stop(); return; }
      if (d.speaking){
        const who = d.speaking.filter(Boolean);
        document.getElementById('whospeaks').textContent =
          who.length ? '🔊 ' + who.join(', ') : '';
        return;
      }
      if (d.channel){
        statusEl.textContent = 'En direct dans ' + d.channel;
        const ear = document.getElementById('earstate');
        if (ear) ear.textContent = d.listening
          ? '🎧 tu entends le salon'
          : '🔇 écoute indisponible';
        if (!d.listening && d.note) console.warn(d.note);
      }
    } catch (err) {}
  };
  ws.onclose = () => stop(true);
  ws.onerror = () => { statusEl.textContent = 'Connexion interrompue'; stop(true); };
}

function stop(silent){
  if (recorder && recorder.state !== 'inactive') recorder.stop();
  recorder = null;
  if (ws && ws.readyState === 1){ ws.send('stop'); ws.close(); }
  ws = null;
  if (stream){ stream.getTracks().forEach(t => t.stop()); stream = null; }
  if (meter){ clearInterval(meter); meter = null; }
  if (audioCtx){ audioCtx.close().catch(() => {}); audioCtx = null; }
  stopOutput();
  bar.style.width = '0%';
  const ear = document.getElementById('earstate');
  if (ear) ear.textContent = '';
  setState(false, silent ? 'Micro arrêté' : 'Prêt');
  if (!silent) fetch(`/api/guild/${gid}/mic/stop`, {method:'POST'});
}

const volume = document.getElementById('vol');
if (volume) volume.addEventListener('input', () => {
  if (gain) gain.gain.value = parseFloat(volume.value);
});
btn.addEventListener('click', start);
stopBtn.addEventListener('click', () => stop());
addEventListener('beforeunload', () => { if (ws) stop(true); });
"""

MIC_CSS = """
.micbox{display:flex;flex-direction:column;gap:14px}
.microw{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.levelbar{height:8px;border-radius:99px;background:rgba(255,255,255,.08);overflow:hidden;flex:1;
 min-width:180px}
.levelbar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--acc),var(--acc2));
 transition:width .1s}
.dot{width:10px;height:10px;border-radius:50%;background:var(--dim);flex:none}
.dot.on{background:#7fd9a8;box-shadow:0 0 10px #7fd9a8;animation:pulse 1.4s infinite}
@keyframes pulse{50%{opacity:.45}}
"""


def mic_panel(guild: Any) -> str:
    """Encart « Micro » affiché dans la console du serveur."""
    options = "".join(f'<option value="{c.id}">🔊 {escape(c.name)}</option>'
                      for c in guild.voice_channels)
    if not options:
        return ('<div class="card"><h3>🎙️ Micro</h3>'
                '<p class="sub">Aucun salon vocal sur ce serveur.</p></div>')
    return f"""
<h2 class="section">🎙️ Parler avec la voix du bot</h2>
<div class="card micbox">
  <p class="sub" style="color:var(--mute)">Ton micro est envoyé au salon vocal choisi :
   le bot parle en direct avec ta voix. Réservé au propriétaire du serveur et aux
   propriétaires du bot.</p>
  <div class="microw">
    <select id="micchannel" style="max-width:280px">{options}</select>
    <button class="btn" id="micbtn">🎙️ Activer le micro</button>
    <button class="btn ghost" id="micstop" style="display:none">⏹️ Couper</button>
  </div>
  <div class="microw">
    <span class="dot" id="micdot"></span>
    <span id="micstatus" class="sub">Prêt</span>
    <span class="levelbar"><i id="miclevel"></i></span>
  </div>
  <div class="microw" style="gap:16px">
    <label class="asbot"><input type="checkbox" id="listen" checked>
      entendre le salon</label>
    <label class="asbot"><input type="checkbox" id="deaf">
      me mettre en sourdine</label>
    <label class="asbot" style="gap:8px">🔉
      <input type="range" id="vol" min="0" max="2" step="0.05" value="1"
             style="width:120px;padding:0"></label>
    <span id="earstate" class="sub"></span>
    <span id="whospeaks" class="sub" style="color:var(--acc)"></span>
  </div>
  <p class="sub" style="color:var(--dim);font-size:.82rem">
    Le navigateur demande l'autorisation du micro : le site doit être en <b>https</b>.
    Latence d'environ une demi-seconde. FFmpeg est requis sur le serveur, et
    <code>discord-ext-voice-recv</code> pour entendre les autres.</p>
</div>
<script>{MIC_JS}</script>"""


# =========================================================================== #
#  PARTAGE DE FICHIERS  —  /share
#  Le staff dépose un fichier, le site génère un lien public du type
#  https://mon-site/share?id=1a2b3c4d5e : image, audio, vidéo et PDF sont lus
#  directement dans la page, tout le reste se télécharge.
# =========================================================================== #
SHARE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS web_shares (
           token      TEXT PRIMARY KEY,
           user_id    INTEGER NOT NULL,
           guild_id   INTEGER,
           title      TEXT NOT NULL DEFAULT '',
           filename   TEXT NOT NULL,
           original   TEXT NOT NULL,
           mime       TEXT NOT NULL DEFAULT 'application/octet-stream',
           size       INTEGER NOT NULL DEFAULT 0,
           views      INTEGER NOT NULL DEFAULT 0,
           created_at TEXT NOT NULL
       )""",
    "CREATE INDEX IF NOT EXISTS idx_shares_user ON web_shares(user_id)",
]

SHARE_MIME = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
    ".ogg": "audio/ogg", ".opus": "audio/opus", ".flac": "audio/flac",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".svg": "image/svg+xml", ".avif": "image/avif", ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8", ".json": "application/json",
    ".zip": "application/zip", ".rar": "application/vnd.rar",
    ".7z": "application/x-7z-compressed",
}
SHARE_MAX = 512 * 1024 * 1024          # 512 Mo par fichier


def shares_dir() -> Path:
    raw = env("SHARE_DIR")
    path = Path(raw) if raw else BASE_DIR / "shares"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def share_setup() -> None:
    for query in SHARE_SCHEMA:
        await STATE.db.execute(query)
    shares_dir()


async def get_share(token: str) -> Optional[dict]:
    return row_dict(await STATE.db.fetchone(
        "SELECT * FROM web_shares WHERE token = ?", (token,)))


async def session_level(user_id: int) -> int:
    """Niveau le plus élevé de la personne, tous serveurs confondus."""
    best = 0
    try:
        for guild in await user_guilds(user_id):
            best = max(best, int(guild.get("level") or 0))
    except Exception:
        log.debug("Partage : niveau indéterminé", exc_info=True)
    return best


def is_sharer(session: Optional[dict]) -> bool:
    """Staff d'au moins un serveur, ou propriétaire du site."""
    if not session:
        return False
    return int(session.get("level") or 0) >= 2 or is_owner(session)


def share_size(size: int) -> str:
    value = float(size)
    for unit in ("o", "Ko", "Mo", "Go"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "o" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} To"


def share_kind(mime: str) -> str:
    mime = (mime or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("text/") or mime == "application/json":
        return "texte"
    if mime == "application/pdf":
        return "pdf"
    return "fichier"


SHARE_ICONS = {"image": "🖼️", "audio": "🎵", "video": "🎬", "texte": "📄",
               "pdf": "📕", "fichier": "📦"}


async def page_share(request: web.Request) -> web.Response:
    """Page publique d'un fichier, ou espace de partage du staff."""
    session = session_of(request)
    token = re.sub(r"[^a-z0-9]", "", (request.query.get("id") or "").lower())[:32]

    if token:
        record = await get_share(token)
        if record is None:
            return page("Partage", """
<div class="panel" style="max-width:620px;margin:40px auto">
  <h2>Lien invalide</h2>
  <p class="sub">Ce fichier n'existe plus ou le lien est erroné.</p>
  <a class="btn ghost" href="/">Accueil</a></div>""", session)

        await STATE.db.execute(
            "UPDATE web_shares SET views = views + 1 WHERE token = ?", (token,))
        kind = share_kind(str(record["mime"]))
        url = f"/share/file/{token}"
        title = escape(str(record.get("title") or record["original"]))
        author = STATE.bot.get_user(int(record["user_id"])) if STATE.bot else None

        if kind == "image":
            viewer = (f'<a href="{url}" target="_blank">'
                      f'<img src="{url}" alt="{title}" '
                      f'style="max-width:100%;border-radius:12px"></a>')
        elif kind == "audio":
            viewer = (f'<audio src="{url}" controls preload="metadata" '
                      f'style="width:100%"></audio>')
        elif kind == "video":
            viewer = (f'<video src="{url}" controls playsinline preload="metadata" '
                      f'style="width:100%;border-radius:12px;background:#000"></video>')
        elif kind == "pdf":
            viewer = (f'<iframe src="{url}" style="width:100%;height:70vh;border:0;'
                      f'border-radius:12px;background:#fff"></iframe>')
        elif kind == "texte":
            try:
                extrait = (shares_dir() / str(record["filename"])).read_text(
                    encoding="utf-8", errors="replace")[:20000]
            except OSError:
                extrait = ""
            viewer = f'<pre class="console" style="height:auto;max-height:60vh">{escape(extrait)}</pre>'
        else:
            viewer = (f'<p class="sub" style="font-size:3rem;text-align:center;'
                      f'margin:20px 0">{SHARE_ICONS.get(kind, "📦")}</p>')

        gestion = ""
        if session and (session["user_id"] == int(record["user_id"]) or is_owner(session)):
            gestion = f"""
<form method="post" action="/share/delete" style="display:inline"
      onsubmit="return confirm('Supprimer définitivement ce fichier ?')">
  <input type="hidden" name="id" value="{token}">
  <button class="btn ghost">Supprimer</button></form>"""

        body = f"""
<h2 class="section">{SHARE_ICONS.get(kind, '📦')} {title}</h2>
<div class="card">
  {viewer}
  <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:16px">
    <a class="btn" href="{url}?dl=1" download>⬇️ Télécharger</a>
    <button class="btn ghost" id="copy">🔗 Copier le lien</button>
    {gestion}
  </div>
  <p class="sub" style="color:var(--mute);margin-top:12px">
    {escape(str(record['original']))} · {share_size(int(record['size'] or 0))} ·
    envoyé par {escape(author.display_name if author else 'le staff')} ·
    {escape(str(record['created_at'])[:16])} ·
    {int(record['views'] or 0)} vue(s)</p>
</div>
<script>
document.getElementById('copy').addEventListener('click', async () => {{
  await navigator.clipboard.writeText(location.origin + '/share?id={token}');
  document.getElementById('copy').textContent = '✅ Lien copié';
}});
</script>"""
        return page(title, body, session, wide=True)

    # ----- espace du staff --------------------------------------------- #
    if not is_sharer(session):
        raise web.HTTPFound("/dashboard?error=Le partage de fichiers est réservé au staff.")

    error, notice = request.query.get("error", ""), request.query.get("ok", "")
    rows = await STATE.db.fetchall(
        "SELECT * FROM web_shares ORDER BY created_at DESC LIMIT 200")
    mine, others = [], []
    for row in rows or []:
        item = row_dict(row) or {}
        (mine if item["user_id"] == session["user_id"] else others).append(item)

    def ligne(item: dict) -> str:
        kind = share_kind(str(item["mime"]))
        who = STATE.bot.get_user(int(item["user_id"])) if STATE.bot else None
        return f"""<tr>
  <td>{SHARE_ICONS.get(kind, '📦')} <a href="/share?id={item['token']}">
      {escape(str(item.get('title') or item['original']))}</a></td>
  <td>{share_size(int(item['size'] or 0))}</td>
  <td>{int(item['views'] or 0)}</td>
  <td>{escape(who.display_name if who else '—')}</td>
  <td>{escape(str(item['created_at'])[:16])}</td>
  <td><form method="post" action="/share/delete" style="margin:0"
        onsubmit="return confirm('Supprimer ?')">
      <input type="hidden" name="id" value="{item['token']}">
      <button class="btn ghost">Supprimer</button></form></td></tr>"""

    body = f"""
{f'<div class="msg err">{escape(error)}</div>' if error else ''}
{f'<div class="msg ok">{escape(notice)}</div>' if notice else ''}
<h2 class="section">📤 Partage de fichiers</h2>
<div class="card">
  <form method="post" action="/share/upload" enctype="multipart/form-data">
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">
      <div><label>Fichier</label><input type="file" name="file" required></div>
      <div><label>Titre affiché (facultatif)</label>
        <input name="title" maxlength="80" placeholder="Bande-son de l'événement"></div>
    </div>
    <button class="btn" style="margin-top:16px">Envoyer et créer le lien</button>
    <p class="hint" style="color:var(--mute);font-size:.85rem;margin-top:10px">
      {share_size(SHARE_MAX)} maximum. Les images, musiques, vidéos et PDF sont lus
      directement dans la page ; tout le reste se télécharge.
      <b>Toute personne ayant le lien peut y accéder</b>, même sans compte.</p>
  </form>
</div>

<h2 class="section">Mes fichiers ({len(mine)})</h2>
<div class="card"><div style="overflow:auto"><table>
  <tr><th>Fichier</th><th>Taille</th><th>Vues</th><th>Par</th><th>Date</th><th></th></tr>
  {''.join(ligne(i) for i in mine) or "<tr><td colspan='6'>Aucun fichier partagé.</td></tr>"}
</table></div></div>

<h2 class="section">Partagés par l'équipe ({len(others)})</h2>
<div class="card"><div style="overflow:auto"><table>
  <tr><th>Fichier</th><th>Taille</th><th>Vues</th><th>Par</th><th>Date</th><th></th></tr>
  {''.join(ligne(i) for i in others) or "<tr><td colspan='6'>Rien pour l'instant.</td></tr>"}
</table></div></div>"""
    return page("Partage", body, session, wide=True)


async def share_upload(request: web.Request) -> web.Response:
    session = require_session(request)
    if not is_sharer(session):
        raise web.HTTPFound("/dashboard?error=Réservé au staff.")
    reader = await request.multipart()
    title, token, saved = "", secrets.token_hex(5), None
    original, mime, size = "", "application/octet-stream", 0

    async for part in reader:
        if part.name == "title":
            title = (await part.text()).strip()[:80]
        elif part.name == "file" and part.filename:
            from urllib.parse import unquote
            original = unquote(part.filename)[:120]
            suffix = Path(original).suffix.lower()[:10]
            mime = SHARE_MIME.get(suffix) or part.headers.get(
                "Content-Type", "application/octet-stream")
            target = shares_dir() / f"{token}{suffix}"
            with target.open("wb") as handle:
                while True:
                    chunk = await part.read_chunk()
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > SHARE_MAX:
                        handle.close()
                        target.unlink(missing_ok=True)
                        raise web.HTTPFound(
                            f"/share?error=Fichier trop lourd ({share_size(SHARE_MAX)} max).")
                    handle.write(chunk)
            saved = target.name

    if not saved:
        raise web.HTTPFound("/share?error=Aucun fichier reçu.")
    await STATE.db.execute(
        "INSERT INTO web_shares(token, user_id, title, filename, original, mime, size,"
        " created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (token, session["user_id"], title, saved, original, mime, size, stamp()))
    raise web.HTTPFound(f"/share?id={token}")


async def share_file(request: web.Request) -> web.StreamResponse:
    """Sert le fichier : lecture en continu, avec avance possible."""
    token = re.sub(r"[^a-z0-9]", "", request.match_info.get("token", "").lower())[:32]
    record = await get_share(token)
    if record is None:
        raise web.HTTPNotFound()
    target = shares_dir() / str(record["filename"])
    if not target.is_file():
        raise web.HTTPNotFound()
    headers = {"Cache-Control": "public, max-age=86400",
               "Content-Type": str(record["mime"]),
               "Accept-Ranges": "bytes"}
    if request.query.get("dl"):
        safe = re.sub(r'[^\w.\- ]', "_", str(record["original"])) or "fichier"
        headers["Content-Disposition"] = f'attachment; filename="{safe}"'
    return web.FileResponse(target, headers=headers)


async def share_delete(request: web.Request) -> web.Response:
    session = require_session(request)
    data = await request.post()
    token = re.sub(r"[^a-z0-9]", "", str(data.get("id", "")).lower())[:32]
    record = await get_share(token)
    if record is None:
        raise web.HTTPFound("/share")
    if session["user_id"] != int(record["user_id"]) and not is_owner(session):
        raise web.HTTPFound("/share?error=Seul l'auteur peut supprimer ce fichier.")
    (shares_dir() / str(record["filename"])).unlink(missing_ok=True)
    await STATE.db.execute("DELETE FROM web_shares WHERE token = ?", (token,))
    raise web.HTTPFound("/share?ok=Fichier supprimé.")


# --------------------------------------------------------------------------- #
#  Serveur HTTP
# --------------------------------------------------------------------------- #
@web.middleware
async def embed_middleware(request: web.Request, handler) -> web.StreamResponse:
    """Autorise l'affichage du site dans l'iframe de la vitrine GitHub Pages."""
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
    origins = embed_origins()
    if origins:
        allowed = " ".join(origins)
        response.headers["Content-Security-Policy"] = (
            f"frame-ancestors 'self' {allowed} https://*.discord.com")
        response.headers.pop("X-Frame-Options", None)
        origin = request.headers.get("Origin", "")
        if origin.rstrip("/") in origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
    if isinstance(response, web.HTTPException):
        raise response
    return response


# Modules annexes (maison.py…) : chaque fonction reçoit l'application aiohttp.
EXTRA_ROUTES: list = []


def register_routes(setup) -> None:
    """Permet à un module externe d'ajouter ses pages au site."""
    if setup not in EXTRA_ROUTES:
        EXTRA_ROUTES.append(setup)


def build_app() -> web.Application:
    app = web.Application(middlewares=[embed_middleware])
    app.router.add_get("/api/guild/{gid}/mic", api_mic_ws)
    app.router.add_post("/api/guild/{gid}/mic/stop", api_mic_stop)
    app.router.add_get("/share", page_share)
    app.router.add_post("/share/upload", share_upload)
    app.router.add_get("/share/file/{token}", share_file)
    app.router.add_post("/share/delete", share_delete)
    app.router.add_get("/collab", page_collab)
    app.router.add_get("/partenaires", page_collab)
    app.router.add_post("/studio/partenaire", partners_add)
    app.router.add_post("/studio/partenaire/ordre", partners_move)
    app.router.add_post("/studio/partenaire/supprimer", partners_delete)
    app.router.add_post("/studio/partenaire/textes", partners_texts)
    app.router.add_get("/studio", page_studio)
    app.router.add_post("/studio/page", studio_save_page)
    app.router.add_post("/studio/page/delete", studio_delete_page)
    app.router.add_post("/studio/page/duplicate", studio_duplicate)
    app.router.add_post("/studio/upload", studio_upload)
    app.router.add_post("/studio/file/delete", studio_delete_file)
    app.router.add_get("/u/{name}", serve_upload)
    app.router.add_post("/studio/link", studio_add_link)
    app.router.add_post("/studio/link/delete", studio_delete_link)
    app.router.add_post("/studio/home", studio_save_home)
    app.router.add_post("/studio/bot", studio_save_bot)
    app.router.add_post("/studio/redirect", studio_add_redirect)
    app.router.add_post("/studio/redirect/delete", studio_delete_redirect)
    app.router.add_get("/p/{slug}", page_custom)
    app.router.add_get("/", page_home)
    app.router.add_get("/login", page_login)
    app.router.add_post("/login", do_login)
    app.router.add_get("/logout", page_logout)
    app.router.add_get("/setup", page_setup)
    app.router.add_post("/setup", do_setup)
    app.router.add_get("/dashboard", page_dashboard)
    app.router.add_get("/account", page_account)
    app.router.add_post("/account/password", do_password)
    app.router.add_get("/oauth/{provider}/start", oauth_start)
    app.router.add_get("/oauth/{provider}/callback", oauth_callback)
    app.router.add_get("/guild/{gid}", page_guild)
    app.router.add_post("/guild/{gid}/say", act_say)
    app.router.add_post("/guild/{gid}/moderate", act_moderate)
    app.router.add_post("/guild/{gid}/radio", act_radio)
    app.router.add_post("/guild/{gid}/radio/stop", act_radio_stop)
    app.router.add_get("/vendor/{name}", serve_vendor)
    app.router.add_get("/logo.png", serve_logo)
    app.router.add_get("/favicon.ico", serve_logo)
    app.router.add_get("/tv/stream", tv_proxy)
    app.router.add_get("/tv", page_tv)
    app.router.add_get("/tv/activity", page_tv_activity)
    app.router.add_post("/guild/{gid}/dm", act_dm)
    app.router.add_post("/guild/{gid}/dm-role", act_dm_role)
    app.router.add_get("/guild/{gid}/annonce", page_announce)
    app.router.add_post("/guild/{gid}/annonce", act_announce)
    app.router.add_post("/guild/{gid}/announce", act_announce)
    app.router.add_get("/client", page_client)
    app.router.add_get("/api/guild/{gid}/channels", api_client_channels)
    app.router.add_get("/api/guild/{gid}/channel/{cid}/messages", api_client_messages)
    app.router.add_post("/api/guild/{gid}/channel/{cid}/send", api_client_send)
    app.router.add_patch("/api/guild/{gid}/channel/{cid}/message/{mid}", api_client_edit)
    app.router.add_delete("/api/guild/{gid}/channel/{cid}/message/{mid}", api_client_delete)
    app.router.add_get("/api/guild/{gid}/dms", api_dm_list)
    app.router.add_get("/api/guild/{gid}/dm/{uid}/messages", api_dm_messages)
    app.router.add_post("/api/guild/{gid}/dm/{uid}/send", api_dm_send)
    app.router.add_get("/therms", page_rules)
    app.router.add_post("/therms", post_rules)
    app.router.add_get("/reglement", page_rules)
    app.router.add_get("/confidentialite", page_privacy)
    app.router.add_get("/privacy", page_privacy)
    app.router.add_get("/conditions", page_terms)
    app.router.add_get("/terms", page_terms)
    app.router.add_get("/api/channels", api_channels)
    app.router.add_get("/api/health", api_health)
    # en tout dernier : les raccourcis d'adresse (/discord, /yt…)
    app.router.add_get("/{path:[^/]+(?:/[^/]+)?}", page_redirect)

    for setup in EXTRA_ROUTES:
        try:
            setup(app)
        except Exception:
            log.exception("Site web : module annexe non chargé")
    return app


async def start_server() -> None:
    if STATE.started or not env_bool("WEB_ENABLED", True):
        return
    await setup_tables()
    await detect_public_ip()
    STATE.runner = web.AppRunner(build_app(), access_log=None)
    await STATE.runner.setup()
    STATE.site = web.TCPSite(STATE.runner, host(), port())
    try:
        await STATE.site.start()
    except OSError as exc:
        log.error("Site web : port %s indisponible (%s)", port(), exc)
        return
    STATE.started = True
    log.info("Site web %s en ligne sur %s", BRAND, public_url())
    print(f"  🌐 Site web      : {public_url()}")


async def stop_server() -> None:
    if STATE.runner is not None:
        await STATE.runner.cleanup()
    STATE.started = False


# --------------------------------------------------------------------------- #
#  Réponses automatiques
# --------------------------------------------------------------------------- #
def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


async def load_autoreplies(guild_id: int) -> list[dict]:
    if guild_id in STATE.autoreplies:
        return STATE.autoreplies[guild_id]
    rows = await STATE.db.fetchall(
        "SELECT * FROM web_autoreplies WHERE guild_id = ? ORDER BY id", (guild_id,))
    items = [row_dict(r) or {} for r in rows]
    STATE.autoreplies[guild_id] = items
    return items


def invalidate(guild_id: int) -> None:
    STATE.autoreplies.pop(guild_id, None)


def rule_matches(content: str, rule: dict) -> bool:
    haystack = normalize(content)
    needle = normalize(rule["trigger"])
    if not needle:
        return False
    if rule.get("partial"):
        return needle in haystack
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


async def on_message_autoreply(message: discord.Message) -> None:
    if message.author.bot or message.guild is None or not message.content:
        return
    rules = await load_autoreplies(message.guild.id)
    if not rules:
        return
    for rule in rules:
        if rule_matches(message.content, rule):
            text = (rule["response"]
                    .replace("{user}", message.author.mention)
                    .replace("{name}", message.author.display_name)
                    .replace("{server}", message.guild.name))
            try:
                await message.reply(text[:1900], mention_author=False)
            except discord.HTTPException:
                pass
            return


# --------------------------------------------------------------------------- #
#  Radio en vocal
# --------------------------------------------------------------------------- #
FFMPEG_BEFORE = ("-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
                 "-nostdin -loglevel error")


def ffmpeg_path() -> str:
    return env("FFMPEG_PATH", "ffmpeg")


def resolve_station(choice: str) -> Optional[dict]:
    radios = load_channels()["radios"]
    choice = (choice or "").strip()
    if choice.isdigit():
        index = int(choice) - 1
        if 0 <= index < len(radios):
            return radios[index]
        return None
    if choice.lower().startswith(("http://", "https://")):
        return {"nom": "Flux personnalisé", "url": choice}
    lowered = normalize(choice)
    for station in radios:
        if normalize(station["nom"]).startswith(lowered) or lowered in normalize(station["nom"]):
            return station
    return None


# --------------------------------------------------------------------------- #
#  Commandes Discord
# --------------------------------------------------------------------------- #
def build_commands(bot: Any) -> list:
    commands_list: list = []

    # ---------------- /compte ------------------------------------------- #
    compte = app_commands.Group(name="compte",
                                description=f"Ton accès au site {BRAND}")

    @compte.command(name="obtenir", description="Recevoir en MP ton identifiant et ton mot de passe")
    async def compte_obtenir(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        account, password = await ensure_account(interaction.user)
        if password is None:
            return await interaction.followup.send(
                f"✅ Tu as déjà un compte : identifiant **{account['username']}**.\n"
                f"Mot de passe oublié ? Utilise `/compte reset`.\n🌐 {public_url()}",
                ephemeral=True)
        sent = await send_credentials(interaction.user, account["username"], password,
                                      "Ne partage jamais ce mot de passe.")
        await interaction.followup.send(
            "📬 Identifiants envoyés en message privé !" if sent else
            "❌ Je ne peux pas t'écrire en privé. Ouvre tes MP puis réessaie.",
            ephemeral=True)

    @compte.command(name="reset", description="Régénérer un mot de passe provisoire")
    async def compte_reset(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        account, password = await ensure_account(interaction.user, reset=True)
        sent = await send_credentials(interaction.user, account["username"], password or "",
                                      "Mot de passe régénéré.")
        await interaction.followup.send(
            "📬 Nouveau mot de passe envoyé en message privé." if sent else
            "❌ Je ne peux pas t'écrire en privé. Ouvre tes MP puis réessaie.", ephemeral=True)

    @compte.command(name="site", description="Afficher l'adresse du site")
    async def compte_site(interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title=f"🌐 Site {BRAND}", color=0x5865F2,
            description=(f"{public_url()}\n\nTape `/compte obtenir` pour recevoir tes "
                         f"identifiants en message privé. Aucun téléchargement."))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @compte.command(name="diffuser", description="Envoyer leurs identifiants à tous les membres")
    @app_commands.default_permissions(manage_guild=True)
    async def compte_diffuser(interaction: discord.Interaction) -> None:
        perms = getattr(interaction.user, "guild_permissions", None)
        if not (perms and (perms.administrator or perms.manage_guild)):
            return await interaction.response.send_message(
                "❌ Permission **Gérer le serveur** requise.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        sent = failed = skipped = 0
        for member in list(interaction.guild.members):
            if member.bot:
                continue
            existing = await get_account(member.id)
            if existing is not None:
                skipped += 1
                continue
            account, password = await ensure_account(member)
            if await send_credentials(member, account["username"], password or "",
                                      f"Envoyé depuis {interaction.guild.name}"):
                sent += 1
            else:
                failed += 1
            await asyncio.sleep(1.1)
        await interaction.followup.send(
            f"📬 Envoyés : **{sent}** · MP fermés : **{failed}** · déjà inscrits : **{skipped}**",
            ephemeral=True)

    commands_list.append(compte)

    # ---------------- /autoreply ----------------------------------------- #
    auto = app_commands.Group(name="autoreply", description="Réponses automatiques à un mot",
                              guild_only=True,
                              default_permissions=discord.Permissions(manage_messages=True))

    @auto.command(name="add", description="Quand un message contient un mot, le bot répond")
    @app_commands.describe(mot="Mot ou expression déclencheur",
                           reponse="Réponse du bot ({user}, {name}, {server} acceptés)",
                           mot_entier="Oui = déclenche seulement sur le mot exact")
    async def auto_add(interaction: discord.Interaction, mot: str, reponse: str,
                       mot_entier: bool = False) -> None:
        mot = mot.strip()
        if len(mot) < 2:
            return await interaction.response.send_message(
                "❌ Le mot déclencheur doit faire 2 caractères minimum.", ephemeral=True)
        count = await STATE.db.fetchval(
            "SELECT COUNT(*) FROM web_autoreplies WHERE guild_id = ?",
            (interaction.guild_id,), 0) or 0
        if count >= 100:
            return await interaction.response.send_message(
                "❌ Limite de 100 réponses automatiques atteinte.", ephemeral=True)
        await STATE.db.execute(
            "INSERT INTO web_autoreplies(guild_id, trigger, response, partial, created_by, "
            "created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (interaction.guild_id, mot, reponse.strip(), 0 if mot_entier else 1,
             interaction.user.id, stamp()))
        invalidate(interaction.guild_id)
        await interaction.response.send_message(
            f"✅ Quand un message contient **{mot}**"
            f"{' (mot entier)' if mot_entier else ''}, je répondrai :\n> {reponse[:300]}",
            ephemeral=True)

    @auto.command(name="list", description="Liste des réponses automatiques")
    async def auto_list(interaction: discord.Interaction) -> None:
        rules = await load_autoreplies(interaction.guild_id)
        if not rules:
            return await interaction.response.send_message(
                "Aucune réponse automatique. Crée-en une avec `/autoreply add`.", ephemeral=True)
        lines = [f"`#{r['id']}` **{r['trigger']}**"
                 f"{' (mot entier)' if not r['partial'] else ''} → {r['response'][:90]}"
                 for r in rules]
        embed = discord.Embed(title="💬 Réponses automatiques", color=0x5865F2,
                              description="\n".join(lines)[:4000])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @auto.command(name="remove", description="Supprimer une réponse automatique")
    @app_commands.describe(identifiant="Numéro affiché par /autoreply list")
    async def auto_remove(interaction: discord.Interaction, identifiant: int) -> None:
        row = await STATE.db.fetchone(
            "SELECT id FROM web_autoreplies WHERE id = ? AND guild_id = ?",
            (identifiant, interaction.guild_id))
        if row is None:
            return await interaction.response.send_message("❌ Introuvable.", ephemeral=True)
        await STATE.db.execute("DELETE FROM web_autoreplies WHERE id = ? AND guild_id = ?",
                               (identifiant, interaction.guild_id))
        invalidate(interaction.guild_id)
        await interaction.response.send_message(f"🗑️ Réponse `#{identifiant}` supprimée.",
                                                ephemeral=True)

    @auto.command(name="clear", description="Supprimer toutes les réponses automatiques")
    async def auto_clear(interaction: discord.Interaction) -> None:
        await STATE.db.execute("DELETE FROM web_autoreplies WHERE guild_id = ?",
                               (interaction.guild_id,))
        invalidate(interaction.guild_id)
        await interaction.response.send_message("🧹 Toutes les réponses ont été supprimées.",
                                                ephemeral=True)

    commands_list.append(auto)

    # ---------------- /radio ---------------------------------------------- #
    async def radio_autocomplete(interaction: discord.Interaction, current: str):
        radios = load_channels()["radios"]
        current = normalize(current)
        choices = []
        for index, station in enumerate(radios, start=1):
            label = f"{index}. {station['nom']}"
            if not current or current in normalize(label):
                choices.append(app_commands.Choice(name=label[:100], value=str(index)))
        return choices[:25]

    radio = app_commands.Group(name="radio", description="Radio en direct dans le salon vocal",
                               guild_only=True)

    @radio.command(name="play", description="Lancer une radio (numéro, nom ou URL du flux)")
    @app_commands.describe(chaine="Numéro de la station, son nom, ou l'URL d'un flux")
    @app_commands.autocomplete(chaine=radio_autocomplete)
    async def radio_play(interaction: discord.Interaction, chaine: str) -> None:
        level = await level_of(interaction.user, interaction.guild)
        if level < 1:
            return await interaction.response.send_message(
                "❌ Il faut être **DJ** (ou plus) pour lancer la radio.", ephemeral=True)
        voice = getattr(getattr(interaction.user, "voice", None), "channel", None)
        if voice is None:
            return await interaction.response.send_message(
                "❌ Rejoins d'abord un salon vocal.", ephemeral=True)
        station = resolve_station(chaine)
        if station is None:
            return await interaction.response.send_message(
                "❌ Station inconnue. Regarde `/radio liste` ou donne une URL de flux.",
                ephemeral=True)
        await interaction.response.defer(thinking=True)
        # Si un bot radio dédié (TOKEN_RADIO) est en ligne, c'est lui qui diffuse :
        # le bot principal garde ainsi sa connexion vocale pour la musique.
        if radio_bot_handles(voice):
            ok, problem = await voice_bot.radio_play(voice, station["url"])
            if not ok:
                return await interaction.followup.send(f"❌ Radio impossible : {problem}")
        else:
            client = interaction.guild.voice_client
            try:
                if client is None:
                    client = await voice.connect(self_deaf=True)
                elif client.channel != voice:
                    await client.move_to(voice)
                if client.is_playing() or client.is_paused():
                    client.stop()
                source = discord.FFmpegOpusAudio(
                    station["url"], executable=ffmpeg_path(),
                    before_options=FFMPEG_BEFORE, options="-vn")
                client.play(source)
            except Exception as exc:
                log.warning("Radio impossible : %s", exc)
                return await interaction.followup.send(
                    f"❌ Impossible de lire ce flux ({type(exc).__name__}). "
                    f"Vérifie l'URL et l'installation de FFmpeg.")
        STATE.radio_now[interaction.guild_id] = station
        embed = discord.Embed(title="📻 Radio en direct", color=0x00D4AA,
                              description=f"**{station['nom']}**\n{voice.mention}")
        embed.set_footer(text=f"Demandé par {interaction.user.display_name} · /radio stop pour arrêter")
        await interaction.followup.send(embed=embed)

    @radio.command(name="liste", description="Toutes les stations disponibles")
    async def radio_liste(interaction: discord.Interaction) -> None:
        radios = load_channels()["radios"]
        lines = [f"`{i:>2}` **{s['nom']}**" for i, s in enumerate(radios, start=1)]
        embed = discord.Embed(
            title="📻 Stations disponibles", color=0x5865F2,
            description="\n".join(lines)[:4000])
        embed.set_footer(text="/radio play chaine:<numéro, nom ou URL>")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @radio.command(name="stop", description="Arrêter la radio et quitter le vocal")
    async def radio_stop(interaction: discord.Interaction) -> None:
        level = await level_of(interaction.user, interaction.guild)
        if level < 1:
            return await interaction.response.send_message(
                "❌ Réservé aux DJ.", ephemeral=True)
        stopped = await radio_bot_stop(interaction.guild)
        client = interaction.guild.voice_client
        if client is None and not stopped:
            return await interaction.response.send_message("Je ne suis pas en vocal.",
                                                           ephemeral=True)
        if client is not None:
            client.stop()
            await client.disconnect(force=False)
        STATE.radio_now.pop(interaction.guild_id, None)
        await interaction.response.send_message("⏹️ Radio arrêtée.")

    commands_list.append(radio)

    # ---------------- /tv -------------------------------------------------- #
    tv = app_commands.Group(name="tv", description="Télévision en direct")

    @tv.command(name="regarder", description="Lien pour regarder la télé (site ou activité)")
    async def tv_regarder(interaction: discord.Interaction) -> None:
        channels = load_channels()["tv"]
        lines = "\n".join(f"`{i:>2}` {c.get('logo','📺')} **{c['nom']}**"
                          for i, c in enumerate(channels, start=1))
        embed = discord.Embed(
            title="📺 Télé Lulyo", color=0xEB459E,
            description=f"[Ouvrir la télé sur le site]({public_url()}/tv)\n\n{lines}"[:4000])
        embed.set_footer(text="Activité Discord : /tv activite")
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Regarder la télé", emoji="📺",
                                        url=f"{public_url()}/tv"))
        await interaction.response.send_message(embed=embed, view=view)

    @tv.command(name="activation",
                description="Ré-enregistrer la commande de point d'entrée de l'activité")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def tv_activation(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await ensure_entry_point(interaction.client)
        name = activity_command_name()
        embed = discord.Embed(
            title="🚀 Point d'entrée de l'activité",
            description=(
                f"Commande `/{name}` (ré)enregistrée sur l'application.\n\n"
                "**Si l'activité refuse toujours de se lancer :**\n"
                "1. Portail développeur → **Activities → Settings** → *Enable Activities*\n"
                "2. **URL Mappings** : préfixe `/` → cible ton domaine (sans `https://`)\n"
                "3. Remplis la fiche (plateformes, orientation, classification d'âge)\n"
                "4. Réinstalle l'application sur le serveur, puis attends 1 à 2 minutes\n"
                "5. Regarde les journaux du bot : la raison exacte y est écrite"),
            color=0x5865F2)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tv.command(name="activite", description="Lancer la télé en activité Discord (salon vocal)")
    async def tv_activite(interaction: discord.Interaction) -> None:
        app_id = env("DISCORD_CLIENT_ID")
        voice = getattr(getattr(interaction.user, "voice", None), "channel", None)
        embed = discord.Embed(title="📺 Télé en activité Discord", color=0xEB459E)
        if not app_id:
            embed.description = ("Ajoute `DISCORD_CLIENT_ID` dans le `.env` pour activer "
                                 "l'activité Discord.")
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        if voice is None:
            embed.description = ("Rejoins un salon vocal puis relance la commande, "
                                 f"ou regarde sur le site : {public_url()}/tv")
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        try:
            invite = await voice.create_invite(
                max_age=3600, target_type=discord.InviteTarget.embedded_application,
                target_application_id=int(app_id))
            embed.description = (f"Clique pour lancer la télé dans **{voice.name}** :\n"
                                 f"{invite.url}")
        except Exception as exc:
            embed.description = (
                "Impossible de créer l'activité "
                f"(`{type(exc).__name__}`).\n\n"
                "Vérifie dans le portail développeur : **Activities → Enabled**, puis "
                "**URL Mappings** → `/` vers "
                f"`{public_url().replace('https://', '').replace('http://', '')}` "
                f"et page d'entrée `/tv/activity`.\n"
                f"Si Discord répond « commande de point d'entrée manquante », "
                f"redémarre le bot : il l'enregistre automatiquement au démarrage.\n\n"
                f"En attendant : {public_url()}/tv")
        await interaction.response.send_message(embed=embed)

    commands_list.append(tv)
    return commands_list


# --------------------------------------------------------------------------- #
#  Compatibilité base de données
# --------------------------------------------------------------------------- #
REQUIRED = ("execute", "fetchone", "fetchall")


def db_ready(db: Any) -> list[str]:
    return [name for name in REQUIRED if not callable(getattr(db, name, None))]


class DBWrap:
    """Ajoute fetchval si la base ne l'a pas."""

    def __init__(self, db: Any) -> None:
        self._db = db

    def __getattr__(self, item: str) -> Any:
        return getattr(self._db, item)

    async def fetchval(self, query: str, params: Any = (), default: Any = None) -> Any:
        inner = getattr(self._db, "fetchval", None)
        if callable(inner):
            return await inner(query, params, default)
        row = await self._db.fetchone(query, params)
        if row is None:
            return default
        try:
            return row[0]
        except Exception:
            return default


# --------------------------------------------------------------------------- #
#  Branchement
# --------------------------------------------------------------------------- #
def resolve_port_conflict() -> None:
    """L'ancienne API Lulyo Manager et le site ne peuvent pas partager un port.

    Le Manager démarre avant nous : s'il occupe déjà le port du site, celui-ci ne
    peut plus écouter et le navigateur affiche une erreur 502. On éteint donc
    l'ancienne API, qui ne sert plus à rien depuis que le site existe.
    """
    manager = sys.modules.get("manager_server")
    if manager is None:
        return
    try:
        manager_port = int(getattr(manager, "PORT", 0) or 0)
    except (TypeError, ValueError):
        return
    if manager_port != port() or not getattr(manager, "ENABLED", False):
        return
    manager.ENABLED = False
    log.warning("Site web : l'ancienne API Lulyo Manager utilisait le même port (%s). "
                "Elle a été désactivée pour laisser la place au site.", manager_port)


def attach(bot: Any, db: Any, permission_level: Optional[Callable] = None) -> None:
    """Branche le site web sur le bot. À appeler une seule fois avant bot.run()."""
    if STATE.bot is not None:
        return
    missing = db_ready(db)
    if missing:
        log.error("Site web désactivé : la base n'expose pas %s", ", ".join(missing))
        return
    if not env_bool("WEB_ENABLED", True):
        log.info("Site web désactivé (WEB_ENABLED=0)")
        return

    STATE.bot, STATE.db = bot, DBWrap(db)
    STATE.permission_level = permission_level

    for command in build_commands(bot):
        try:
            bot.tree.add_command(command)
        except app_commands.CommandAlreadyRegistered:
            log.warning("Site web : /%s existe déjà, commande ignorée", command.name)

    async def _on_ready() -> None:
        try:
            await start_server()
        except Exception:
            log.exception("Site web : démarrage impossible")
        try:
            await partners_setup()
        except Exception:
            log.exception("Partenaires : initialisation impossible")
        try:
            await studio_setup()
        except Exception:
            log.exception("Studio : initialisation impossible")
        try:
            await share_setup()
        except Exception:
            log.exception("Partage : initialisation impossible")
        await ensure_entry_point(bot)

    try:
        patch_global_sync(bot)
    except Exception:
        log.debug("Activité : patch de synchronisation impossible", exc_info=True)

    bot.add_listener(_on_ready, "on_ready")
    bot.add_listener(on_message_autoreply, "on_message")
    bot.add_listener(on_role_button, "on_interaction")

    async def _on_member_join(member: discord.Member) -> None:
        """Chaque nouveau membre reçoit ses identifiants, quelles que soient ses permissions."""
        if member.bot or not env_bool("WEB_DM_ON_JOIN", True):
            return
        try:
            existing = await get_account(member.id)
            if existing is not None:
                return
            account, password = await ensure_account(member)
            await send_credentials(member, account["username"], password or "",
                                   f"Bienvenue sur {member.guild.name} !")
        except Exception:
            log.debug("Envoi des identifiants impossible", exc_info=True)

    bot.add_listener(_on_member_join, "on_member_join")
    resolve_port_conflict()
    log.info("Site web %s branché (port %s)", BRAND, port())
