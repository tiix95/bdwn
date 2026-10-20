#!/usr/bin/env python3
"""bdwn-portable: standalone backup of a computer to an SFTP server with restic.

Run it without arguments: a setup wizard runs the first time, then a menu.
Backups are deduplicated and encrypted with a passphrase by restic, the SFTP
server is reached with an SSH key (no password). Works on Linux and Windows,
needs only Python 3.11+, restic and the OpenSSH client.
"""
# pylint: disable=too-many-lines

import sys
from argparse import ArgumentParser, Namespace
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from getpass import getpass, getuser
from json import dumps, loads
from locale import getlocale
from logging import INFO, Formatter, StreamHandler, getLogger
from logging.handlers import RotatingFileHandler
from os import environ
from pathlib import Path
from secrets import token_urlsafe
from shutil import which
from socket import gethostname
from subprocess import DEVNULL, PIPE, STDOUT, CompletedProcess, Popen, run
from time import sleep as time_sleep
from typing import Any
from urllib.parse import urlsplit

if sys.platform != "win32":
    from os import geteuid  # pylint: disable=ungrouped-imports

VERSION = "0.1.0"

APP = "bdwn-portable"
HOST_ALIAS = "bdwn-portable-target"
TAG = "bdwn-portable"
RETRY_ATTEMPTS = 10
RETRY_DELAY = 1800.0
RESTIC_PARTIAL = 3
RESTIC_NO_REPO = 10
RESTIC_WRONG_PASSWORD = 12
OLD_RESTIC_NO_REPO = "Is there a repository at the following location?"
# subprocess.CREATE_NO_WINDOW only exists on Windows: avoid console popups there
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
YES = {"y", "yes", "o", "oui"}
NO = {"n", "no", "non"}

DEFAULT_EXCLUDES: list[str] = [
    "*.tmp",
    ".cache",
    "Cache",
    "Caches",
    "Temp",
    "node_modules",
    "Thumbs.db",
    "desktop.ini",
    "$RECYCLE.BIN",
    ".Trash-*",
]

logger = getLogger("bdwn_portable")

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "error": "Error: {error}",
        "bye": "Bye.",
        "invalid_choice": "Invalid choice, try again.",
        "choice": "Your choice",
        "yes_no_yes": "[Y/n]",
        "yes_no_no": "[y/N]",
        "press_enter": "Press Enter to continue...",
        "no_config": "No configuration found in {path}. Run the setup first: setup",
        "need_admin": "System mode needs administrator rights (Windows: run the terminal "
                      "as administrator, Linux: use sudo).",
        "tools_missing_linux": "restic and/or ssh not found. Install them with:\n"
                               "    sudo apt install restic openssh-client",
        "tools_missing_windows": "restic and/or ssh not found. Install restic "
                                 "(winget install restic.restic) and the OpenSSH client "
                                 "(Settings > Apps > Optional features).",
        "winget_offer": "restic is not installed. Install it now with winget?",
        "setup_overwrite": "A configuration already exists in {path}. Replace it?",
        "setup_lang": "Language / Langue:  1) Francais  2) English",
        "setup_welcome": "Welcome to bdwn-portable. Your files will be backed up, encrypted, "
                         "to an SFTP server.",
        "setup_server": "Server address (e.g. sftp://user@server.example.com/backups "
                        "or user@server:/backups)",
        "setup_bad_url": "Invalid address: {error}",
        "setup_auth": "SSH authentication (key only, no password):\n"
                      "  1) Use an existing private key (without passphrase)\n"
                      "  2) Generate a new key (recommended)",
        "setup_key_path": "Path of the private key",
        "key_missing": "File not found: {path}",
        "key_has_passphrase": "This key is protected by a passphrase (or is unreadable). "
                              "Scheduled backups run unattended and cannot type it: use a key "
                              "without passphrase, or generate a new one.",
        "key_generated": "A new SSH key was created: {path}",
        "key_send": "Send the public key below to the administrator of the server, so that "
                    "it is added to the authorized keys of the account '{user}':",
        "setup_pass_choice": "Encryption passphrase of the backups:\n"
                             "  1) Generate a strong one (recommended)\n"
                             "  2) Type my own",
        "setup_pass_type": "Passphrase: ",
        "setup_pass_again": "Passphrase again: ",
        "setup_pass_mismatch": "The passphrases are empty or do not match, try again.",
        "setup_pass_warning": "WRITE DOWN THIS PASSPHRASE AND KEEP IT SAFE "
                              "(paper, password manager).\n"
                              "Without it, NOBODY can restore your backups.",
        "setup_add_home": "Back up your personal folder {path}?",
        "setup_more_paths": "Other folder to back up (empty to finish)",
        "setup_time": "Daily backup time (HH:MM)",
        "bad_time": "Invalid time, expected HH:MM (e.g. 12:30).",
        "setup_vss": "Use Windows shadow copies (VSS) to back up files in use?",
        "setup_saved": "Configuration saved in {path}",
        "setup_test_now": "Test the connection to the server now?",
        "setup_schedule_now": "Install the daily scheduled backup now?",
        "setup_done": "Setup complete. Run this script again to open the menu.",
        "item_added": "Added: {value}",
        "item_not_added": "Empty or already in the list: {value}",
        "path_not_found": "Warning: {value} does not exist (yet).",
        "item_removed": "Removed: {value}",
        "item_replaced": "Replaced by: {value}",
        "items_empty": "  (empty list)",
        "paths_title": "Folders backed up:",
        "excludes_title": "Excluded patterns:",
        "ask_new_path": "Folder to add",
        "ask_new_exclude": "Pattern to exclude (e.g. *.iso, Downloads)",
        "ask_item_number": "Number",
        "ask_new_value": "New value",
        "bad_number": "Invalid number: {value}",
        "items_menu": "1) Add  2) Remove  3) Edit  0) Back",
        "no_paths": "No folder to back up: add one first.",
        "repo_init": "Repository not found, creating it...",
        "repo_unavailable": "Repository unavailable, backup aborted.",
        "repo_error": "Cannot open the repository (restic exit code {code}):\n{output}",
        "backup_start": "Backup of {count} folder(s) started",
        "backup_partial": "Backup done, but some files could not be read (see above).",
        "backup_failed": "Backup failed (restic exit code {code}).",
        "backup_ok": "Backup completed successfully.",
        "forget_failed": "Cleanup of old backups failed (restic exit code {code}), "
                         "it will be done next time.",
        "test_running": "Testing the connection to {host}...",
        "test_ok": "OK: server reachable, key accepted, repository ready.",
        "test_repo_missing": "OK: server reachable and key accepted. The repository does not "
                             "exist yet: it will be created at the first backup.",
        "test_auth": "The server refused the SSH key. Check that the public key was added "
                     "to the account '{user}' on the server.",
        "test_wrong_password": "The repository exists but the passphrase is wrong.",
        "test_hostkey": "The identity of the server (host key) changed. If this is expected, "
                        "delete {path} and try again.",
        "test_unreachable": "Cannot reach the server or open the repository. Details:\n{output}",
        "status_last_attempt": "Last attempt : {value}",
        "status_last_success": "Last success : {value}",
        "status_last_error": "Last error   : {value} {message}",
        "never": "never",
        "status_snapshots": "Backups      : {count}, latest {latest}",
        "status_snapshots_error": "Cannot list the backups (run the connection test).",
        "status_schedule": "Schedule:",
        "restore_none": "No backup available.",
        "restore_pick": "Backup to restore",
        "restore_target": "Restore into folder",
        "restore_include": "Restore only this path of the backup (empty = everything)",
        "restore_ok": "Files restored into {path}",
        "restore_failed": "Restore failed (restic exit code {code}).",
        "schedule_menu": "1) Install / update  2) Remove  3) Status  0) Back",
        "schedule_installed": "Daily backup scheduled at {time}.",
        "schedule_failed": "Scheduling failed:\n{output}",
        "schedule_removed": "Scheduled backup removed.",
        "schedule_none": "No scheduled backup installed.",
        "linger_advice": "The timer only runs while you are logged in, unless lingering is "
                         "enabled (loginctl enable-linger {user}).",
        "linger_offer": "Enable lingering now?",
        "menu_title": "bdwn-portable {version} - backup to {host}",
        "menu_backup": "Back up now",
        "menu_status": "Status",
        "menu_paths": "Folders to back up",
        "menu_excludes": "Exclusions",
        "menu_test": "Test the connection",
        "menu_schedule": "Scheduled backup",
        "menu_restore": "Restore files",
        "menu_retention": "Retention (how many backups to keep)",
        "menu_pubkey": "Show the public SSH key",
        "menu_lang": "Change language",
        "menu_quit": "Quit",
        "ret_daily": "Daily backups to keep",
        "ret_weekly": "Weekly backups to keep",
        "ret_monthly": "Monthly backups to keep",
        "saved": "Saved.",
        "pubkey_error": "Cannot read the public key: {error}",
    },
    "fr": {
        "error": "Erreur : {error}",
        "bye": "Au revoir.",
        "invalid_choice": "Choix invalide, recommencez.",
        "choice": "Votre choix",
        "yes_no_yes": "[O/n]",
        "yes_no_no": "[o/N]",
        "press_enter": "Appuyez sur Entree pour continuer...",
        "no_config": "Aucune configuration dans {path}. Lancez d'abord : setup",
        "need_admin": "Le mode systeme demande les droits administrateur (Windows : lancez le "
                      "terminal en administrateur, Linux : utilisez sudo).",
        "tools_missing_linux": "restic et/ou ssh introuvable. Installez-les avec :\n"
                               "    sudo apt install restic openssh-client",
        "tools_missing_windows": "restic et/ou ssh introuvable. Installez restic "
                                 "(winget install restic.restic) et le client OpenSSH "
                                 "(Parametres > Applications > Fonctionnalites facultatives).",
        "winget_offer": "restic n'est pas installe. L'installer maintenant avec winget ?",
        "setup_overwrite": "Une configuration existe deja dans {path}. La remplacer ?",
        "setup_lang": "Language / Langue :  1) Francais  2) English",
        "setup_welcome": "Bienvenue dans bdwn-portable. Vos fichiers seront sauvegardes, "
                         "chiffres, sur un serveur SFTP.",
        "setup_server": "Adresse du serveur (ex : sftp://utilisateur@serveur.exemple.fr/sauvegarde "
                        "ou utilisateur@serveur:/sauvegardes)",
        "setup_bad_url": "Adresse invalide : {error}",
        "setup_auth": "Authentification SSH (par cle uniquement, sans mot de passe) :\n"
                      "  1) Utiliser une cle privee existante (sans phrase de passe)\n"
                      "  2) Generer une nouvelle cle (recommande)",
        "setup_key_path": "Chemin de la cle privee",
        "key_missing": "Fichier introuvable : {path}",
        "key_has_passphrase": "Cette cle est protegee par une phrase de passe (ou illisible). "
                              "Les sauvegardes planifiees tournent sans personne pour la "
                              "saisir : utilisez une cle sans phrase de passe, ou generez-en "
                              "une nouvelle.",
        "key_generated": "Une nouvelle cle SSH a ete creee : {path}",
        "key_send": "Envoyez la cle publique ci-dessous a l'administrateur du serveur, pour "
                    "qu'il l'ajoute aux cles autorisees du compte '{user}' :",
        "setup_pass_choice": "Phrase de passe de chiffrement des sauvegardes :\n"
                             "  1) En generer une solide (recommande)\n"
                             "  2) Saisir la mienne",
        "setup_pass_type": "Phrase de passe : ",
        "setup_pass_again": "Phrase de passe (encore) : ",
        "setup_pass_mismatch": "Les phrases de passe sont vides ou differentes, recommencez.",
        "setup_pass_warning": "NOTEZ CETTE PHRASE DE PASSE ET GARDEZ-LA EN LIEU SUR "
                              "(papier, gestionnaire de mots de passe).\n"
                              "Sans elle, PERSONNE ne pourra restaurer vos sauvegardes.",
        "setup_add_home": "Sauvegarder votre dossier personnel {path} ?",
        "setup_more_paths": "Autre dossier a sauvegarder (vide pour terminer)",
        "setup_time": "Heure de la sauvegarde quotidienne (HH:MM)",
        "bad_time": "Heure invalide, format attendu HH:MM (ex : 12:30).",
        "setup_vss": "Utiliser les cliches instantanes Windows (VSS) pour les fichiers ouverts ?",
        "setup_saved": "Configuration enregistree dans {path}",
        "setup_test_now": "Tester la connexion au serveur maintenant ?",
        "setup_schedule_now": "Installer la sauvegarde quotidienne planifiee maintenant ?",
        "setup_done": "Configuration terminee. Relancez ce script pour ouvrir le menu.",
        "item_added": "Ajoute : {value}",
        "item_not_added": "Vide ou deja dans la liste : {value}",
        "path_not_found": "Attention : {value} n'existe pas (encore).",
        "item_removed": "Supprime : {value}",
        "item_replaced": "Remplace par : {value}",
        "items_empty": "  (liste vide)",
        "paths_title": "Dossiers sauvegardes :",
        "excludes_title": "Motifs exclus :",
        "ask_new_path": "Dossier a ajouter",
        "ask_new_exclude": "Motif a exclure (ex : *.iso, Telechargements)",
        "ask_item_number": "Numero",
        "ask_new_value": "Nouvelle valeur",
        "bad_number": "Numero invalide : {value}",
        "items_menu": "1) Ajouter  2) Supprimer  3) Modifier  0) Retour",
        "no_paths": "Aucun dossier a sauvegarder : ajoutez-en un d'abord.",
        "repo_init": "Depot introuvable, creation...",
        "repo_unavailable": "Depot indisponible, sauvegarde abandonnee.",
        "repo_error": "Impossible d'ouvrir le depot (code de sortie restic {code}) :\n{output}",
        "backup_start": "Sauvegarde de {count} dossier(s) lancee",
        "backup_partial": "Sauvegarde faite, mais certains fichiers n'ont pas pu etre lus "
                          "(voir ci-dessus).",
        "backup_failed": "Echec de la sauvegarde (code de sortie restic {code}).",
        "backup_ok": "Sauvegarde terminee avec succes.",
        "forget_failed": "Le nettoyage des anciennes sauvegardes a echoue (code de sortie "
                         "restic {code}), il sera refait la prochaine fois.",
        "test_running": "Test de la connexion a {host}...",
        "test_ok": "OK : serveur joignable, cle acceptee, depot pret.",
        "test_repo_missing": "OK : serveur joignable et cle acceptee. Le depot n'existe pas "
                             "encore : il sera cree a la premiere sauvegarde.",
        "test_auth": "Le serveur a refuse la cle SSH. Verifiez que la cle publique a ete "
                     "ajoutee au compte '{user}' sur le serveur.",
        "test_wrong_password": "Le depot existe mais la phrase de passe est fausse.",
        "test_hostkey": "L'identite du serveur (cle d'hote) a change. Si c'est normal, "
                        "supprimez {path} et recommencez.",
        "test_unreachable": "Impossible de joindre le serveur ou d'ouvrir le depot. "
                            "Details :\n{output}",
        "status_last_attempt": "Derniere tentative : {value}",
        "status_last_success": "Dernier succes     : {value}",
        "status_last_error": "Derniere erreur    : {value} {message}",
        "never": "jamais",
        "status_snapshots": "Sauvegardes        : {count}, la plus recente {latest}",
        "status_snapshots_error": "Impossible de lister les sauvegardes (lancez le test "
                                  "de connexion).",
        "status_schedule": "Planification :",
        "restore_none": "Aucune sauvegarde disponible.",
        "restore_pick": "Sauvegarde a restaurer",
        "restore_target": "Restaurer dans le dossier",
        "restore_include": "Restaurer seulement ce chemin de la sauvegarde (vide = tout)",
        "restore_ok": "Fichiers restaures dans {path}",
        "restore_failed": "Echec de la restauration (code de sortie restic {code}).",
        "schedule_menu": "1) Installer / mettre a jour  2) Supprimer  3) Etat  0) Retour",
        "schedule_installed": "Sauvegarde quotidienne planifiee a {time}.",
        "schedule_failed": "Echec de la planification :\n{output}",
        "schedule_removed": "Sauvegarde planifiee supprimee.",
        "schedule_none": "Aucune sauvegarde planifiee installee.",
        "linger_advice": "Le minuteur ne tourne que lorsque vous etes connecte, sauf si le "
                         "lingering est active (loginctl enable-linger {user}).",
        "linger_offer": "Activer le lingering maintenant ?",
        "menu_title": "bdwn-portable {version} - sauvegarde vers {host}",
        "menu_backup": "Sauvegarder maintenant",
        "menu_status": "Etat",
        "menu_paths": "Dossiers a sauvegarder",
        "menu_excludes": "Exclusions",
        "menu_test": "Tester la connexion",
        "menu_schedule": "Sauvegarde planifiee",
        "menu_restore": "Restaurer des fichiers",
        "menu_retention": "Retention (combien de sauvegardes garder)",
        "menu_pubkey": "Afficher la cle SSH publique",
        "menu_lang": "Changer de langue",
        "menu_quit": "Quitter",
        "ret_daily": "Sauvegardes quotidiennes a garder",
        "ret_weekly": "Sauvegardes hebdomadaires a garder",
        "ret_monthly": "Sauvegardes mensuelles a garder",
        "saved": "Enregistre.",
        "pubkey_error": "Impossible de lire la cle publique : {error}",
    },
}


def t(key: str, lang: str, **kwargs: object) -> str:
    """Return the message `key` in `lang` (English if unknown), formatted with kwargs."""
    messages = MESSAGES.get(lang, MESSAGES["en"])
    return messages[key].format(**kwargs)


def _lang_of(value: str | None) -> str | None:
    """Map a locale name (fr_FR.UTF-8, French_France...) to "fr"/"en", None if not set."""
    if not value or value.upper().startswith(("C.", "POSIX")) or value.upper() == "C":
        return None
    return "fr" if value.lower().startswith("fr") else "en"


def detect_lang() -> str:
    """Guess the user interface language: "fr" or "en" (the default)."""
    for name in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        lang = _lang_of(environ.get(name))
        if lang:
            return lang
    if sys.platform == "win32":
        try:
            from ctypes import windll  # pylint: disable=import-outside-toplevel
            # The low 10 bits of a Windows language id are the language, 0x0C is French
            return "fr" if windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x0C else "en"
        except (ImportError, AttributeError, OSError):
            pass
    try:
        return _lang_of(getlocale()[0]) or "en"
    except ValueError:
        return "en"


@dataclass
class Config:
    """Everything needed to back up this computer, saved as JSON."""
    user: str
    host: str
    path: str
    key_path: str
    restic_password: str
    port: int = 22
    paths: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    keep_daily: int = 7
    keep_weekly: int = 4
    keep_monthly: int = 6
    schedule_time: str = "12:30"
    use_vss: bool = False
    lang: str = ""
    system: bool = False


def default_config_dir(system: bool) -> Path:
    """Directory holding the config, key, ssh_config, state and log."""
    if sys.platform == "win32":
        if system:
            return Path(environ.get("ProgramData", r"C:\ProgramData")) / APP
        return Path(environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / APP
    if system:
        return Path("/etc") / APP
    return Path(environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP


def is_admin() -> bool:
    """True when running as root (Linux) or as an administrator (Windows)."""
    if sys.platform == "win32":
        from ctypes import windll  # pylint: disable=import-outside-toplevel
        admin = bool(windll.shell32.IsUserAnAdmin())
    else:
        admin = geteuid() == 0  # pylint: disable=possibly-used-before-assignment
    return admin


def run_capture(cmd: list[str], env: dict[str, str] | None = None,
                stdin: str = "") -> CompletedProcess[str]:
    """Run a command without a console window and return its captured output."""
    return run(cmd, env=env, input=stdin, capture_output=True, text=True, encoding="utf-8",
               errors="replace", check=False, creationflags=NO_WINDOW)


def run_logged(cmd: list[str], env: dict[str, str] | None = None) -> int:
    """Run a command, send each output line to the log, return the exit code."""
    with Popen(cmd, env=env, stdin=DEVNULL, stdout=PIPE, stderr=STDOUT, text=True,
               encoding="utf-8", errors="replace", creationflags=NO_WINDOW) as proc:
        assert proc.stdout is not None
        for line in proc.stdout:
            logger.info(line.rstrip())
        return proc.wait()


def restrict_permissions(path: Path, system: bool) -> None:
    """Make a file or directory readable by its owner only (plus SYSTEM/admins in system mode).

    OpenSSH refuses private keys readable by others, on Windows too.
    """
    if sys.platform != "win32":
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    inherit = "(OI)(CI)" if path.is_dir() else ""
    grants = [f"{environ.get('USERNAME') or getuser()}:{inherit}F"]
    if system:
        # Well-known SIDs of SYSTEM and Administrators: their names are localized
        grants += [f"*S-1-5-18:{inherit}F", f"*S-1-5-32-544:{inherit}F"]
    run_capture(["icacls", str(path), "/inheritance:r", "/grant:r", *grants])
    if system:
        # OpenSSH run as SYSTEM refuses a key owned by another user
        run_capture(["icacls", str(path), "/setowner", "*S-1-5-32-544"])


def write_private_file(path: Path, text: str, system: bool) -> None:
    """Write a file that only its owner can read."""
    path.touch(mode=0o600, exist_ok=True)
    restrict_permissions(path, system)
    path.write_text(text, encoding="utf-8")


def load_config(path: Path) -> Config:
    """Read the JSON config, ignoring unknown keys. FileNotFoundError if missing."""
    data = loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid configuration file: {path}")
    known = {item.name for item in fields(Config)}
    try:
        return Config(**{key: value for key, value in data.items() if key in known})
    except TypeError as exc:
        raise ValueError(f"invalid configuration file {path}: {exc}") from exc


def save_config(cfg: Config, path: Path) -> None:
    """Write the config as JSON, readable by its owner only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    restrict_permissions(path.parent, cfg.system)
    write_private_file(path, dumps(asdict(cfg), indent=2) + "\n", cfg.system)


def parse_server_url(url: str) -> tuple[str, str, int, str]:
    """Split an SFTP address into (user, host, port, path).

    Accepts sftp://user@host[:port]/path, user@host:/path and user@host:path.
    """
    url = url.strip()
    if url.startswith("sftp://"):
        parts = urlsplit(url)
        user, host, path = parts.username or "", parts.hostname or "", parts.path
        port = parts.port if parts.port is not None else 22  # raises ValueError if bad
    else:
        if "://" in url:
            raise ValueError(f"only sftp:// addresses are supported: {url}")
        user, at_sign, rest = url.removeprefix("sftp:").partition("@")
        host, colon, path = rest.partition(":")
        if not at_sign or not colon:
            raise ValueError(f"expected user@host:/path, got: {url}")
        port = 22
    if not user or not host or path in ("", "/"):
        raise ValueError(f"user, host and path are required: {url}")
    if any(char.isspace() for char in user + host):
        raise ValueError(f"spaces are not allowed in user or host: {url}")
    if not 0 < port < 65536:
        raise ValueError(f"invalid port: {port}")
    return user, host, port, path


def parse_time(value: str) -> str:
    """Validate a HH:MM time and return it normalized (e.g. 9:05 -> 09:05)."""
    hours, colon, minutes = value.strip().partition(":")
    if not colon or not hours.isdigit() or not minutes.isdigit():
        raise ValueError(f"invalid time: {value}")
    if int(hours) > 23 or int(minutes) > 59:
        raise ValueError(f"invalid time: {value}")
    return f"{int(hours):02d}:{int(minutes):02d}"


def repository_url(cfg: Config) -> str:
    """restic repository URL, through the host alias of our ssh_config."""
    return f"sftp:{HOST_ALIAS}:{cfg.path}"


def _quote(value: str) -> str:
    """Quote a path for ssh_config / restic sftp.command (forward slashes on Windows)."""
    if sys.platform == "win32":
        value = value.replace("\\", "/")
    return f'"{value}"' if " " in value else value


def ssh_config_text(cfg: Config, known_hosts: Path) -> str:
    """ssh_config defining the host alias used by restic."""
    return "\n".join([
        f"Host {HOST_ALIAS}",
        f"    HostName {cfg.host}",
        f"    User {cfg.user}",
        f"    Port {cfg.port}",
        f"    IdentityFile {_quote(cfg.key_path)}",
        "    IdentitiesOnly yes",
        "    PreferredAuthentications publickey",
        f"    UserKnownHostsFile {_quote(str(known_hosts))}",
        "    StrictHostKeyChecking accept-new",
        "    BatchMode yes",
        "    ServerAliveInterval 60",
        "    ServerAliveCountMax 240",
    ]) + "\n"


def restic_env(cfg: Config) -> dict[str, str]:
    """Environment for restic: the passphrase never goes on the command line."""
    return {**environ, "RESTIC_REPOSITORY": repository_url(cfg),
            "RESTIC_PASSWORD": cfg.restic_password}


def backup_args(cfg: Config, on_windows: bool) -> list[str]:
    """restic arguments of a backup run."""
    args = ["backup", "--tag", TAG]
    if on_windows and cfg.use_vss:
        args.append("--use-fs-snapshot")
    for pattern in cfg.excludes:
        args += ["--exclude", pattern]
    return args + cfg.paths


def forget_args(cfg: Config) -> list[str]:
    """restic arguments removing the backups beyond the retention policy."""
    return ["forget", "--prune", "--tag", TAG,
            "--keep-daily", str(cfg.keep_daily),
            "--keep-weekly", str(cfg.keep_weekly),
            "--keep-monthly", str(cfg.keep_monthly)]


def add_item(items: list[str], value: str) -> bool:
    """Append a stripped value unless empty or already present."""
    value = value.strip()
    if not value or value in items:
        return False
    items.append(value)
    return True


def remove_item(items: list[str], index: int) -> str:
    """Remove and return items[index] (0-based, no negative index)."""
    if not 0 <= index < len(items):
        raise IndexError(f"no item number {index + 1}")
    return items.pop(index)


def replace_item(items: list[str], index: int, value: str) -> None:
    """Replace items[index] (0-based) by a non-empty stripped value."""
    if not 0 <= index < len(items):
        raise IndexError(f"no item number {index + 1}")
    value = value.strip()
    if not value:
        raise ValueError("empty value")
    items[index] = value


def generate_passphrase() -> str:
    """Random restic passphrase."""
    return token_urlsafe(32)


def run_with_retries(func: Callable[[], bool], attempts: int, delay: float,
                     sleep: Callable[[float], None] = time_sleep) -> bool:
    """Call func until it succeeds, at most `attempts` times, waiting `delay` in between."""
    for attempt in range(1, attempts + 1):
        if func():
            return True
        if attempt < attempts:
            logger.warning("attempt %d/%d failed, retrying in %.0f s", attempt, attempts, delay)
            sleep(delay)
    return False


def _systemd_quote(value: str) -> str:
    return f'"{value}"' if " " in value else value


def systemd_units(cfg: Config, python: str, script: Path, config_path: Path) -> tuple[str, str]:
    """Return (service, timer) unit texts of the daily backup."""
    command = " ".join(_systemd_quote(part) for part in (python, str(script), "--config",
                                                          str(config_path)))
    network = "Wants=network-online.target\nAfter=network-online.target\n" if cfg.system else ""
    service = (
        "[Unit]\n"
        "Description=bdwn-portable backup\n"
        f"{network}"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={command} backup --scheduled\n"
        "Nice=10\n"
        "IOSchedulingClass=idle\n"
        "TimeoutStartSec=12h\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Daily bdwn-portable backup\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar=*-*-* {cfg.schedule_time}:00\n"
        "Persistent=true\n"
        "RandomizedDelaySec=5min\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def _ps_quote(value: str) -> str:
    """PowerShell single-quoted string."""
    return "'" + value.replace("'", "''") + "'"


def windows_task_script(cfg: Config, python: str, script: Path, config_path: Path) -> str:
    """PowerShell script registering the daily scheduled task.

    One statement per line: 'powershell -Command -' reads stdin line by line.
    """
    pythonw = Path(python).with_name("pythonw.exe")
    if Path(python).name.lower() == "python.exe" and pythonw.is_file():
        python = str(pythonw)  # no console window popping up
    arguments = f'"{script}" --config "{config_path}" backup --scheduled'
    if cfg.system:
        logon = "New-ScheduledTaskTrigger -AtLogOn"
        principal = ("New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount "
                     "-RunLevel Highest")
    else:
        logon = "New-ScheduledTaskTrigger -AtLogOn -User $user"
        # S4U: runs whether the user is logged on or not, without storing a password
        principal = "New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited"
    lines = [
        "$ErrorActionPreference = 'Stop'",
        '$user = "$env:USERDOMAIN\\$env:USERNAME"',
        f"$action = New-ScheduledTaskAction -Execute {_ps_quote(python)} "
        f"-Argument {_ps_quote(arguments)} -WorkingDirectory {_ps_quote(str(script.parent))}",
        f"$triggers = @((New-ScheduledTaskTrigger -Daily -At {_ps_quote(cfg.schedule_time)}), "
        f"({logon}))",
        "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 12)",
        f"$principal = {principal}",
        f"Register-ScheduledTask -TaskName '{APP}' -Action $action -Trigger $triggers "
        "-Settings $settings -Principal $principal -Force | Out-Null",
    ]
    return "\n".join(lines) + "\n"


@dataclass
class Tools:
    """External programs, and the ssh_config restic hands to ssh."""
    restic: str
    ssh: str
    ssh_keygen: str
    ssh_config: Path


def _windows_dirs() -> list[Path]:
    """Places where winget and Windows put restic / OpenSSH, outside of PATH."""
    program_files = Path(environ.get("ProgramFiles", r"C:\Program Files"))
    system_root = Path(environ.get("SystemRoot", r"C:\Windows"))
    winget_roots = [program_files / "WinGet"]
    if environ.get("LOCALAPPDATA"):
        winget_roots.insert(0, Path(environ["LOCALAPPDATA"]) / "Microsoft" / "WinGet")
    dirs = [root / "Links" for root in winget_roots]
    dirs += [system_root / "System32" / "OpenSSH", program_files / "OpenSSH"]
    for root in winget_roots:
        dirs += sorted((root / "Packages").glob("restic.restic*"))
    return dirs


def find_program(name: str) -> str | None:
    """Full path of a program, from PATH or the usual Windows install places."""
    found = which(name)
    if found or sys.platform != "win32":
        return found
    for directory in _windows_dirs():
        # winget names the restic binary like restic_0.17.3_windows_amd64.exe
        for candidate in [directory / f"{name}.exe", *sorted(directory.glob(f"{name}_*.exe"))]:
            if candidate.is_file():
                return str(candidate)
    return None


def find_tools(config_dir: Path, lang: str, system: bool, interactive: bool) -> Tools | None:
    """Locate restic and ssh, offering to install restic with winget on Windows."""
    restic = find_program("restic")
    if (restic is None and sys.platform == "win32" and interactive
            and ask_yes(t("winget_offer", lang), lang)):
        scope = ["--scope", "machine"] if system else []
        run(["winget", "install", "--id", "restic.restic", "-e", "--accept-source-agreements",
             "--accept-package-agreements", *scope], check=False)
        restic = find_program("restic")
    ssh = find_program("ssh")
    if restic is None or ssh is None:
        key = "tools_missing_windows" if sys.platform == "win32" else "tools_missing_linux"
        logger.error(t(key, lang))
        return None
    keygen = find_program("ssh-keygen") or str(Path(ssh).with_name("ssh-keygen"))
    return Tools(restic, ssh, keygen, config_dir / "ssh_config")


def restic_cmd(cfg: Config, tools: Tools, args: list[str]) -> list[str]:
    """Full restic command line, going through our ssh_config."""
    sftp = f"{_quote(tools.ssh)} -F {_quote(str(tools.ssh_config))} {HOST_ALIAS} -s sftp"
    return [tools.restic, "--repo", repository_url(cfg), "-o", f"sftp.command={sftp}", *args]


@dataclass
class Session:
    """A loaded configuration, where it lives, and the language to talk in."""
    cfg: Config
    config_path: Path
    lang: str

    @property
    def config_dir(self) -> Path:
        return self.config_path.parent

    def msg(self, key: str, **kwargs: object) -> str:
        return t(key, self.lang, **kwargs)

    def save(self) -> None:
        """Save the config and the ssh_config derived from it."""
        save_config(self.cfg, self.config_path)
        self.write_ssh_config()

    def write_ssh_config(self) -> None:
        path = self.config_dir / "ssh_config"
        text = ssh_config_text(self.cfg, self.config_dir / "known_hosts")
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            write_private_file(path, text, self.cfg.system)

    def tools(self, interactive: bool = True) -> Tools | None:
        tools = find_tools(self.config_dir, self.lang, self.cfg.system, interactive)
        if tools:
            self.write_ssh_config()
        return tools


# ---------------------------------------------------------------------------
# Repository operations
# ---------------------------------------------------------------------------

def probe_repo(cfg: Config, tools: Tools) -> tuple[int, str]:
    """Run 'restic cat config': (exit code, output)."""
    result = run_capture(restic_cmd(cfg, tools, ["cat", "config"]), restic_env(cfg))
    return result.returncode, result.stdout + result.stderr


def _repo_missing(code: int, output: str) -> bool:
    # restic exits 10 for a missing repository since 0.17, older ones print this question
    return code == RESTIC_NO_REPO or OLD_RESTIC_NO_REPO in output


def ensure_repo(session: Session, tools: Tools) -> bool:
    """Create the repository if, and only if, restic reports it absent."""
    code, output = probe_repo(session.cfg, tools)
    if code == 0:
        return True
    if _repo_missing(code, output):
        logger.info(session.msg("repo_init"))
        return run_logged(restic_cmd(session.cfg, tools, ["init"]), restic_env(session.cfg)) == 0
    logger.error(session.msg("repo_error", code=code, output=output.strip()))
    return False


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_state(config_dir: Path) -> dict[str, str]:
    """Last backup attempt / success / error, from state.json."""
    try:
        data = loads((config_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(key): str(value) for key, value in data.items()} if isinstance(data, dict) else {}


def write_state(config_dir: Path, state: dict[str, str]) -> None:
    (config_dir / "state.json").write_text(dumps(state, indent=2) + "\n", encoding="utf-8")


def _backup_error(session: Session, tools: Tools) -> str:
    """Run one backup + cleanup. Return the error message, "" on success."""
    cfg = session.cfg
    if not cfg.paths:
        return session.msg("no_paths")
    if not ensure_repo(session, tools):
        return session.msg("repo_unavailable")
    logger.info(session.msg("backup_start", count=len(cfg.paths)))
    env = restic_env(cfg)
    code = run_logged(restic_cmd(cfg, tools, backup_args(cfg, sys.platform == "win32")), env)
    if code == RESTIC_PARTIAL:
        logger.warning(session.msg("backup_partial"))
    elif code != 0:
        return session.msg("backup_failed", code=code)
    code = run_logged(restic_cmd(cfg, tools, forget_args(cfg)), env)
    if code != 0:
        logger.warning(session.msg("forget_failed", code=code))
    return ""


def run_backup(session: Session, tools: Tools) -> bool:
    """One backup attempt, recorded in state.json."""
    state = read_state(session.config_dir)
    state["last_attempt"] = now_iso()
    write_state(session.config_dir, state)
    error = _backup_error(session, tools)
    if error:
        logger.error(error)
        state["last_error"] = now_iso()
        state["last_error_message"] = error
    else:
        logger.info(session.msg("backup_ok"))
        state["last_success"] = now_iso()
        state["last_error_message"] = ""
    write_state(session.config_dir, state)
    return not error


def enable_file_log(config_dir: Path) -> None:
    """Also log to backup.log (1 MB x 3 files) in the config directory."""
    if any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers):
        return
    handler = RotatingFileHandler(config_dir / "backup.log", maxBytes=1_000_000,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)


def cmd_backup(session: Session, scheduled: bool) -> bool:
    """Back up now. Scheduled runs retry for hours: the schedulers never do it."""
    enable_file_log(session.config_dir)
    tools = session.tools(interactive=not scheduled)
    if tools is None:
        return False
    if scheduled:
        return run_with_retries(lambda: run_backup(session, tools), RETRY_ATTEMPTS, RETRY_DELAY)
    return run_backup(session, tools)


def cmd_test(session: Session) -> bool:
    """Check the server, the key and the repository, and explain the result."""
    tools = session.tools()
    if tools is None:
        return False
    print(session.msg("test_running", host=session.cfg.host))
    code, output = probe_repo(session.cfg, tools)
    if code == 0:
        print(session.msg("test_ok"))
    elif _repo_missing(code, output):
        print(session.msg("test_repo_missing"))
    elif code == RESTIC_WRONG_PASSWORD or "wrong password" in output:
        print(session.msg("test_wrong_password"))
    elif "Permission denied" in output:
        print(session.msg("test_auth", user=session.cfg.user))
    elif "Host key verification failed" in output or "IDENTIFICATION HAS CHANGED" in output:
        print(session.msg("test_hostkey", path=session.config_dir / "known_hosts"))
    else:
        print(session.msg("test_unreachable", output=output.strip()))
    return code == 0 or _repo_missing(code, output)


def list_snapshots(session: Session, tools: Tools) -> list[dict[str, Any]] | None:
    """Our snapshots, oldest first. None when restic fails."""
    result = run_capture(restic_cmd(session.cfg, tools, ["snapshots", "--json", "--tag", TAG]),
                         restic_env(session.cfg))
    if result.returncode != 0:
        logger.error(result.stderr.strip())
        return None
    snapshots = loads(result.stdout or "[]") or []
    return sorted(snapshots, key=lambda snap: str(snap.get("time", "")))


def _short_time(value: str) -> str:
    """2026-10-21T12:30:01.123+02:00 -> 2026-10-21 12:30"""
    return value[:16].replace("T", " ")


def cmd_status(session: Session) -> bool:
    state = read_state(session.config_dir)
    never = session.msg("never")
    print(session.msg("status_last_attempt", value=state.get("last_attempt") or never))
    print(session.msg("status_last_success", value=state.get("last_success") or never))
    if state.get("last_error_message"):
        print(session.msg("status_last_error", value=state.get("last_error", ""),
                          message=state["last_error_message"]))
    tools = session.tools()
    snapshots = list_snapshots(session, tools) if tools else None
    if snapshots is None:
        print(session.msg("status_snapshots_error"))
    else:
        latest = _short_time(str(snapshots[-1].get("time", ""))) if snapshots else never
        print(session.msg("status_snapshots", count=len(snapshots), latest=latest))
    print(session.msg("status_schedule"))
    schedule_status(session)
    return snapshots is not None


def cmd_restore(session: Session) -> bool:
    """Pick a snapshot and restore it (or a part of it) into a folder."""
    tools = session.tools()
    snapshots = list_snapshots(session, tools) if tools else None
    if tools is None or snapshots is None:
        return False
    if not snapshots:
        print(session.msg("restore_none"))
        return False
    for number, snap in enumerate(snapshots, 1):
        paths = ", ".join(snap.get("paths", []))
        print(f"{number:3}) {_short_time(str(snap.get('time', '')))}  {paths}")
    snap = snapshots[ask_number(session.msg("restore_pick"), session.lang, 1, len(snapshots),
                                len(snapshots)) - 1]
    stamp = _short_time(str(snap.get("time", ""))).replace("-", "").replace(":", "")
    default_target = Path.home() / f"bdwn-restore-{stamp.replace(' ', '-')}"
    target = ask(session.msg("restore_target"), str(default_target))
    include = ask(session.msg("restore_include"))
    args = ["restore", str(snap["id"]), "--target", target]
    if include:
        args += ["--include", include]
    code = run_logged(restic_cmd(session.cfg, tools, args), restic_env(session.cfg))
    if code != 0:
        print(session.msg("restore_failed", code=code))
        return False
    print(session.msg("restore_ok", path=target))
    return True


# ---------------------------------------------------------------------------
# Scheduling: systemd timer on Linux, Task Scheduler on Windows
# ---------------------------------------------------------------------------

def systemd_unit_dir(system: bool) -> Path:
    if system:
        return Path("/etc/systemd/system")
    return Path(environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def systemctl(system: bool, *args: str) -> CompletedProcess[str]:
    return run_capture(["systemctl", *([] if system else ["--user"]), *args])


def run_powershell(script: str) -> CompletedProcess[str]:
    return run_capture(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-Command", "-"], stdin=script)


def _report(session: Session, result: CompletedProcess[str], ok_message: str) -> bool:
    if result.returncode != 0:
        print(session.msg("schedule_failed", output=(result.stdout + result.stderr).strip()))
        return False
    print(ok_message)
    return True


def schedule_install(session: Session) -> bool:
    """Install (or update) the daily backup in the OS scheduler."""
    cfg = session.cfg
    if cfg.system and not is_admin():
        print(session.msg("need_admin"))
        return False
    script, config_path = Path(__file__).resolve(), session.config_path.resolve()
    done = session.msg("schedule_installed", time=cfg.schedule_time)
    if sys.platform == "win32":
        text = windows_task_script(cfg, sys.executable, script, config_path)
        return _report(session, run_powershell(text), done)
    unit_dir = systemd_unit_dir(cfg.system)
    unit_dir.mkdir(parents=True, exist_ok=True)
    service, timer = systemd_units(cfg, sys.executable, script, config_path)
    (unit_dir / f"{APP}.service").write_text(service, encoding="utf-8")
    (unit_dir / f"{APP}.timer").write_text(timer, encoding="utf-8")
    systemctl(cfg.system, "daemon-reload")
    if not _report(session, systemctl(cfg.system, "enable", "--now", f"{APP}.timer"), done):
        return False
    if not cfg.system:
        print(session.msg("linger_advice", user=getuser()))
        if ask_yes(session.msg("linger_offer"), session.lang):
            run(["loginctl", "enable-linger", getuser()], check=False)
    return True


def schedule_remove(session: Session) -> bool:
    cfg = session.cfg
    if cfg.system and not is_admin():
        print(session.msg("need_admin"))
        return False
    done = session.msg("schedule_removed")
    if sys.platform == "win32":
        script = f"Unregister-ScheduledTask -TaskName '{APP}' -Confirm:$false\n"
        return _report(session, run_powershell(script), done)
    unit_dir = systemd_unit_dir(cfg.system)
    systemctl(cfg.system, "disable", "--now", f"{APP}.timer")
    for suffix in ("service", "timer"):
        (unit_dir / f"{APP}.{suffix}").unlink(missing_ok=True)
    systemctl(cfg.system, "daemon-reload")
    print(done)
    return True


def schedule_status(session: Session) -> bool:
    if sys.platform == "win32":
        script = (f"Get-ScheduledTask -TaskName '{APP}' | Get-ScheduledTaskInfo | "
                  "Format-List LastRunTime, LastTaskResult, NextRunTime | Out-String\n")
        result = run_powershell(script)
    elif (systemd_unit_dir(session.cfg.system) / f"{APP}.timer").is_file():
        result = systemctl(session.cfg.system, "list-timers", f"{APP}.timer", "--all",
                           "--no-pager")
    else:
        result = CompletedProcess([], 1, "", "")
    if result.returncode != 0 or not result.stdout.strip():
        print(session.msg("schedule_none"))
        return False
    print(result.stdout.strip())
    return True


# ---------------------------------------------------------------------------
# Interactive helpers
# ---------------------------------------------------------------------------

def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    return input(f"{prompt}{suffix}: ").strip() or default


def ask_yes(prompt: str, lang: str, default: bool = True) -> bool:
    hint = t("yes_no_yes" if default else "yes_no_no", lang)
    while True:
        answer = input(f"{prompt} {hint} ").strip().lower()
        if not answer:
            return default
        if answer in YES or answer in NO:
            return answer in YES
        print(t("invalid_choice", lang))


def ask_number(prompt: str, lang: str, low: int, high: int, default: int | None = None) -> int:
    """Ask for an integer between low and high (Enter = default, when given)."""
    while True:
        answer = ask(prompt, "" if default is None else str(default))
        if answer.isdigit() and low <= int(answer) <= high:
            return int(answer)
        print(t("invalid_choice", lang))


def ask_time(lang: str, default: str) -> str:
    while True:
        try:
            return parse_time(ask(t("setup_time", lang), default))
        except ValueError:
            print(t("bad_time", lang))


def choose_language(default: str) -> str:
    print(t("setup_lang", default))
    return "fr" if ask_number(t("choice", default), default, 1, 2,
                              1 if default == "fr" else 2) == 1 else "en"


def public_key(tools: Tools, key_path: str) -> str:
    """Public key matching a private key: the .pub file, or derived by ssh-keygen."""
    pub = Path(key_path + ".pub")
    if pub.is_file():
        return pub.read_text(encoding="utf-8").strip()
    result = run_capture([tools.ssh_keygen, "-y", "-P", "", "-f", key_path])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def add_path(session: Session, value: str) -> bool:
    """Add a folder to back up, as an absolute path."""
    value = value.strip()
    path = Path(value).expanduser().absolute() if value else None
    if path is not None and not path.exists():
        print(session.msg("path_not_found", value=path))
    added = path is not None and add_item(session.cfg.paths, str(path))
    print(session.msg("item_added" if added else "item_not_added", value=path or value))
    return added


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

def _ask_server(lang: str) -> tuple[str, str, int, str]:
    while True:
        try:
            return parse_server_url(ask(t("setup_server", lang)))
        except ValueError as exc:
            print(t("setup_bad_url", lang, error=exc))


def _ask_key(tools: Tools, config_dir: Path, system: bool, lang: str, user: str) -> str:
    """Existing passphrase-less key, or a new ed25519 key in the config directory."""
    print(t("setup_auth", lang))
    if ask_number(t("choice", lang), lang, 1, 2, 2) == 1:
        while True:
            key = Path(ask(t("setup_key_path", lang), str(Path.home() / ".ssh" / "id_ed25519")))
            key = key.expanduser().absolute()
            if not key.is_file():
                print(t("key_missing", lang, path=key))
                continue
            restrict_permissions(key, system)
            check = run_capture([tools.ssh_keygen, "-y", "-P", "", "-f", str(key)])
            if check.returncode == 0:
                return str(key)
            print(t("key_has_passphrase", lang))
    key = config_dir / "id_ed25519"
    if not key.exists():
        result = run_capture([tools.ssh_keygen, "-t", "ed25519", "-N", "", "-C",
                              f"{APP}@{gethostname()}", "-f", str(key)])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        print(t("key_generated", lang, path=key))
    restrict_permissions(key, system)
    print()
    print(t("key_send", lang, user=user))
    print()
    print(public_key(tools, str(key)))
    print()
    input(t("press_enter", lang))
    return str(key)


def _ask_passphrase(lang: str) -> str:
    print(t("setup_pass_choice", lang))
    if ask_number(t("choice", lang), lang, 1, 2, 1) == 2:
        while True:
            first = getpass(t("setup_pass_type", lang))
            if first and first == getpass(t("setup_pass_again", lang)):
                passphrase = first
                break
            print(t("setup_pass_mismatch", lang))
        shown = ""
    else:
        passphrase = generate_passphrase()
        shown = f"    {passphrase}\n\n"
    banner = "!" * 72
    print(f"\n{banner}\n{t('setup_pass_warning', lang)}\n\n{shown}{banner}")
    input(t("press_enter", lang))
    return passphrase


def setup_wizard(config_path: Path, system: bool, lang: str) -> Session | None:
    """Interactive first configuration. None when cancelled."""
    if system and not is_admin():
        print(t("need_admin", lang))
        return None
    if config_path.exists() and not ask_yes(t("setup_overwrite", lang, path=config_path),
                                            lang, default=False):
        return None
    lang = choose_language(lang)
    print(t("setup_welcome", lang))
    config_dir = config_path.parent
    config_dir.mkdir(parents=True, exist_ok=True)
    restrict_permissions(config_dir, system)
    tools = find_tools(config_dir, lang, system, interactive=True)
    if tools is None:
        return None
    user, host, port, path = _ask_server(lang)
    key_path = _ask_key(tools, config_dir, system, lang, user)
    cfg = Config(user=user, host=host, path=path, key_path=key_path,
                 restic_password=_ask_passphrase(lang), port=port, lang=lang, system=system)
    session = Session(cfg, config_path, lang)
    if ask_yes(t("setup_add_home", lang, path=Path.home()), lang):
        add_path(session, str(Path.home()))
    while value := ask(t("setup_more_paths", lang)):
        add_path(session, value)
    cfg.schedule_time = ask_time(lang, cfg.schedule_time)
    if sys.platform == "win32" and system:
        cfg.use_vss = ask_yes(t("setup_vss", lang), lang)
    session.save()
    print(t("setup_saved", lang, path=config_path))
    if ask_yes(t("setup_test_now", lang), lang):
        cmd_test(session)
    if ask_yes(t("setup_schedule_now", lang), lang):
        schedule_install(session)
    return session


# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------

def print_items(title: str, items: list[str], lang: str) -> None:
    print(title)
    for number, item in enumerate(items, 1):
        print(f"  {number:2}) {item}")
    if not items:
        print(t("items_empty", lang))


def _edit_items(session: Session, is_paths: bool, action: str, params: list[str]) -> bool:
    """Add / remove / edit one entry of the paths or excludes list, prompting if needed."""
    items = session.cfg.paths if is_paths else session.cfg.excludes
    if action == "add":
        value = params[0] if params else ask(session.msg(
            "ask_new_path" if is_paths else "ask_new_exclude"))
        if is_paths:
            return add_path(session, value)
        added = add_item(items, value)
        print(session.msg("item_added" if added else "item_not_added", value=value))
        return added
    if not items:
        print(session.msg("items_empty"))
        return False
    number = params[0] if params else ask(session.msg("ask_item_number"))
    if not number.isdigit() or not 1 <= int(number) <= len(items):
        print(session.msg("bad_number", value=number))
        return False
    if action == "remove":
        print(session.msg("item_removed", value=remove_item(items, int(number) - 1)))
        return True
    value = params[1] if len(params) > 1 else ask(session.msg("ask_new_value"),
                                                    items[int(number) - 1])
    if is_paths and value.strip():
        value = str(Path(value.strip()).expanduser().absolute())
    replace_item(items, int(number) - 1, value)
    print(session.msg("item_replaced", value=value.strip()))
    return True


def cmd_items(session: Session, is_paths: bool, action: str, params: list[str]) -> bool:
    """CLI 'paths' / 'excludes' subcommands."""
    if action == "list":
        print_items(session.msg("paths_title" if is_paths else "excludes_title"),
                    session.cfg.paths if is_paths else session.cfg.excludes, session.lang)
        return True
    changed = _edit_items(session, is_paths, action, params)
    if changed:
        session.save()
    return changed


def menu_items(session: Session, is_paths: bool) -> None:
    actions = {1: "add", 2: "remove", 3: "edit"}
    while True:
        cmd_items(session, is_paths, "list", [])
        print(session.msg("items_menu"))
        choice = ask_number(session.msg("choice"), session.lang, 0, 3)
        if choice == 0:
            return
        cmd_items(session, is_paths, actions[choice], [])


def menu_schedule(session: Session) -> None:
    while True:
        print(session.msg("schedule_menu"))
        choice = ask_number(session.msg("choice"), session.lang, 0, 3)
        if choice == 0:
            return
        if choice == 1:
            session.cfg.schedule_time = ask_time(session.lang, session.cfg.schedule_time)
            session.save()
            schedule_install(session)
        elif choice == 2:
            schedule_remove(session)
        else:
            schedule_status(session)


def menu_retention(session: Session) -> None:
    cfg = session.cfg
    cfg.keep_daily = ask_number(session.msg("ret_daily"), session.lang, 0, 9999, cfg.keep_daily)
    cfg.keep_weekly = ask_number(session.msg("ret_weekly"), session.lang, 0, 9999,
                                 cfg.keep_weekly)
    cfg.keep_monthly = ask_number(session.msg("ret_monthly"), session.lang, 0, 9999,
                                  cfg.keep_monthly)
    session.save()
    print(session.msg("saved"))


def menu_pubkey(session: Session) -> None:
    tools = session.tools()
    if tools is None:
        return
    try:
        print(public_key(tools, session.cfg.key_path))
    except (OSError, RuntimeError) as exc:
        print(session.msg("pubkey_error", error=exc))


def menu_lang(session: Session) -> None:
    session.lang = session.cfg.lang = choose_language(session.lang)
    session.save()
    print(session.msg("saved"))


def menu(session: Session) -> int:
    """Main interactive menu."""
    actions: list[tuple[str, Callable[[Session], object]]] = [
        ("menu_backup", lambda s: cmd_backup(s, scheduled=False)),
        ("menu_status", cmd_status),
        ("menu_paths", lambda s: menu_items(s, is_paths=True)),
        ("menu_excludes", lambda s: menu_items(s, is_paths=False)),
        ("menu_test", cmd_test),
        ("menu_schedule", menu_schedule),
        ("menu_restore", cmd_restore),
        ("menu_retention", menu_retention),
        ("menu_pubkey", menu_pubkey),
        ("menu_lang", menu_lang),
    ]
    while True:
        print()
        print(session.msg("menu_title", version=VERSION, host=session.cfg.host))
        for number, (key, _) in enumerate(actions, 1):
            print(f"  {number:2}) {session.msg(key)}")
        print(f"   0) {session.msg('menu_quit')}")
        choice = ask_number(session.msg("choice"), session.lang, 0, len(actions))
        if choice == 0:
            return 0
        actions[choice - 1][1](session)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="bdwn_portable.py",
                            description="Encrypted backups to an SFTP server with restic.")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--config", type=Path,
                        help="config file (default: <config dir>/config.json)")
    parser.add_argument("--system", action="store_true",
                        help="system mode: root on Linux, SYSTEM scheduled task on Windows")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("setup", help="run the setup wizard")
    backup = sub.add_parser("backup", help="back up now")
    backup.add_argument("--scheduled", action="store_true",
                        help="unattended run: log to file, retry on failure")
    sub.add_parser("test", help="test the connection to the server")
    sub.add_parser("status", help="show the last backups and the schedule")
    sub.add_parser("restore", help="restore files from a backup")
    paths = sub.add_parser("paths", help="manage the folders to back up")
    paths.add_argument("action", choices=["list", "add", "remove", "edit"])
    paths.add_argument("params", nargs="*", help="add PATH | remove N | edit N NEWPATH")
    excludes = sub.add_parser("excludes", help="manage the excluded patterns")
    excludes.add_argument("action", choices=["list", "add", "remove"])
    excludes.add_argument("params", nargs="*", help="add PATTERN | remove N")
    schedule = sub.add_parser("schedule", help="manage the daily scheduled backup")
    schedule.add_argument("action", choices=["install", "remove", "status"])
    sub.add_parser("menu", help="interactive menu (default)")
    return parser


def run_command(session: Session, args: Namespace) -> int:
    """Run a subcommand on a loaded configuration."""
    match args.command:
        case "backup":
            ok = cmd_backup(session, args.scheduled)
        case "test":
            ok = cmd_test(session)
        case "status":
            ok = cmd_status(session)
        case "restore":
            ok = cmd_restore(session)
        case "paths" | "excludes":
            ok = cmd_items(session, args.command == "paths", args.action, args.params)
        case "schedule":
            actions = {"install": schedule_install, "remove": schedule_remove,
                       "status": schedule_status}
            ok = actions[args.action](session)
        case _:
            return menu(session)
    return 0 if ok else 1


def dispatch(args: Namespace, config_path: Path, system: bool, lang: str) -> int:
    if args.command == "setup":
        return 0 if setup_wizard(config_path, system, lang) else 1
    if not config_path.exists():
        if args.command not in (None, "menu"):
            logger.error(t("no_config", lang, path=config_path))
            return 1
        session = setup_wizard(config_path, system, lang)
        return menu(session) if session else 1
    cfg = load_config(config_path)
    return run_command(Session(cfg, config_path, cfg.lang or lang), args)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logger.setLevel(INFO)
    logger.handlers.clear()
    if sys.stderr is not None:  # None under pythonw.exe
        console = StreamHandler()
        console.setFormatter(Formatter("%(message)s"))
        logger.addHandler(console)
    system = args.system or (args.config is None and sys.platform != "win32" and is_admin())
    config_path = args.config or default_config_dir(system) / "config.json"
    lang = detect_lang()
    try:
        return dispatch(args, config_path, system, lang)
    except (KeyboardInterrupt, EOFError):
        print()
        print(t("bye", lang))
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        logger.error(t("error", lang, error=exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
