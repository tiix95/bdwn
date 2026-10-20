# bdwn-portable

Sauvegarde automatique d'un ordinateur Linux ou Windows vers un serveur SFTP,
chiffrée, en un seul fichier Python.

## Ce que c'est

- **Un seul fichier** : `bdwn_portable.py`. Rien à compiler, pas de paquet à
  installer : il suffit de Python, restic et du client OpenSSH.
- **Linux et Windows** (Windows 10 / 11).
- Les sauvegardes sont faites par [restic](https://restic.net) et envoyées par
  **SFTP** sur un serveur. Elles sont **dédupliquées** (un fichier inchangé
  n'est pas renvoyé) et **chiffrées** : le serveur ne peut pas lire vos
  fichiers.
- Connexion au serveur **par clé SSH uniquement**, jamais par mot de passe. La
  clé doit être **sans phrase de passe**, car les sauvegardes planifiées
  tournent sans personne devant l'écran.
- Interface en **français ou en anglais**, au choix.
- Un **assistant** pose les questions au premier lancement, puis un **menu**
  donne accès à tout.

Ce script est indépendant des paquets Debian `bdwn` : il ne les utilise pas et
n'en a pas besoin.

## Installation

### Linux (Debian, Ubuntu)

```sh
sudo apt install python3 restic openssh-client
```

Copiez ensuite `bdwn_portable.py` où vous voulez, par exemple dans votre
dossier personnel. Python 3.11 ou plus récent est nécessaire.

### Windows 10 / 11

1. Ouvrez un terminal (menu Démarrer, tapez `terminal` ou `powershell`).
2. Installez Python :

   ```powershell
   winget install Python.Python.3.12
   ```

   Fermez puis rouvrez le terminal après l'installation.
3. Le client OpenSSH est déjà inclus dans Windows. S'il manque, ajoutez-le dans
   **Paramètres > Applications > Fonctionnalités facultatives > Client
   OpenSSH**.
4. restic n'est pas à installer à la main : si le script ne le trouve pas, il
   propose de l'installer avec winget (`winget install restic.restic`).

Copiez `bdwn_portable.py` dans un dossier qui ne bougera plus, par exemple
`C:\Users\marie\bdwn`. La sauvegarde planifiée retient l'emplacement exact du
script : si vous le déplacez, réinstallez la planification (voir
[Sauvegarde automatique](#sauvegarde-automatique)).

## Premier lancement : l'assistant

Lancez le script sans argument :

```sh
python3 bdwn_portable.py          # Linux
```

```powershell
python bdwn_portable.py           # Windows
```

S'il n'y a pas encore de configuration, l'assistant démarre. Répondez aux
questions dans l'ordre. Entre crochets, la valeur proposée : appuyez sur
Entrée pour l'accepter.

### 1. Langue

`1` pour le français, `2` pour l'anglais. La langue proposée par défaut est
celle du système.

### 2. Adresse du serveur

L'administrateur du serveur vous donne l'adresse. Trois formes sont acceptées :

| Forme | Exemple |
| --- | --- |
| `sftp://utilisateur@serveur/chemin` | `sftp://marie@sauvegarde.exemple.fr/sauvegardes` |
| `sftp://utilisateur@serveur:port/chemin` | `sftp://marie@sauvegarde.exemple.fr:2222/sauvegardes` |
| `utilisateur@serveur:/chemin` | `marie@sauvegarde.exemple.fr:/sauvegardes` |

L'utilisateur, le serveur et le chemin sont obligatoires. Le port par défaut
est `22` ; pour un autre port, utilisez la forme `sftp://`.

Si le serveur est installé avec le paquet `bdwn-server`, le chemin est le nom
du compte : `pc-marie@sauvegarde.exemple.fr:/pc-marie`.

### 3. Clé SSH

Deux choix :

1. **Utiliser une clé privée existante.** Donnez son chemin (proposé :
   `~/.ssh/id_ed25519`). Le script vérifie qu'elle n'a pas de phrase de passe,
   sinon il la refuse.
2. **Générer une nouvelle clé (recommandé).** Le script crée une clé
   `id_ed25519` dans son dossier de configuration (voir
   [Où sont les fichiers](#où-sont-les-fichiers)).

Dans les deux cas, le script affiche la **clé publique**, une ligne qui
commence par `ssh-ed25519`. **Envoyez cette ligne à l'administrateur du
serveur** : il doit l'ajouter au compte indiqué. Tant que ce n'est pas fait, la
connexion est refusée. Cette ligne n'est pas secrète, vous pouvez l'envoyer par
e-mail. Vous pouvez la réafficher plus tard depuis le menu.

### 4. Phrase de passe de chiffrement

C'est la phrase qui chiffre vos sauvegardes. Deux choix :

1. **En générer une solide (recommandé).** Elle s'affiche à l'écran.
2. **Saisir la vôtre** (deux fois, elle ne s'affiche pas).

> **NOTEZ CETTE PHRASE DE PASSE ET GARDEZ-LA EN LIEU SÛR** (sur papier, dans
> un gestionnaire de mots de passe).
>
> Si cet ordinateur est perdu ou cassé, c'est la **seule** façon de récupérer
> vos fichiers. Sans elle, **personne** ne peut restaurer vos sauvegardes : ni
> vous, ni l'administrateur du serveur.

Elle est aussi enregistrée dans le fichier de configuration de cet ordinateur,
pour que les sauvegardes automatiques fonctionnent.

### 5. Dossiers à sauvegarder

Le script propose d'abord votre dossier personnel (`/home/marie` ou
`C:\Users\marie`). Ajoutez ensuite d'autres dossiers, un par ligne, et laissez
la ligne vide pour terminer. Un dossier qui n'existe pas est accepté, avec un
avertissement.

### 6. Heure de la sauvegarde

Heure de la sauvegarde quotidienne, au format `HH:MM` (proposé : `12:30`).
Choisissez une heure où l'ordinateur est en général allumé.

### 7. Clichés instantanés (Windows, mode système seulement)

En mode système sous Windows (voir [Mode système](#mode-système)), le script
demande s'il faut utiliser les clichés instantanés Windows (VSS). Ils
permettent de sauvegarder les fichiers ouverts (messagerie, bases de données).
Répondez oui.

### 8. Fin de l'assistant

La configuration est enregistrée, puis le script propose :

- de **tester la connexion** au serveur ;
- d'**installer la sauvegarde quotidienne planifiée**.

Répondez oui aux deux. Si la clé publique n'a pas encore été ajoutée sur le
serveur, le test échoue : relancez-le plus tard depuis le menu.

## Utilisation au quotidien

Relancez le script sans argument : le menu s'ouvre.

```text
   1) Sauvegarder maintenant
   2) Etat
   3) Dossiers a sauvegarder
   4) Exclusions
   5) Tester la connexion
   6) Sauvegarde planifiee
   7) Restaurer des fichiers
   8) Retention (combien de sauvegardes garder)
   9) Afficher la cle SSH publique
  10) Changer de langue
   0) Quitter
```

Tout se fait aussi en ligne de commande (sous Windows, remplacez `python3` par
`python`) :

| Commande | Effet |
| --- | --- |
| `python3 bdwn_portable.py` | Ouvre le menu (ou l'assistant la première fois) |
| `python3 bdwn_portable.py setup` | Relance l'assistant (remplace la configuration après confirmation) |
| `python3 bdwn_portable.py backup` | Sauvegarde maintenant |
| `python3 bdwn_portable.py status` | Dernière sauvegarde, nombre de sauvegardes, planification |
| `python3 bdwn_portable.py test` | Teste le serveur, la clé et le dépôt |
| `python3 bdwn_portable.py restore` | Restaure des fichiers (questions interactives) |
| `python3 bdwn_portable.py paths list` | Liste les dossiers sauvegardés |
| `python3 bdwn_portable.py paths add ~/Documents` | Ajoute un dossier |
| `python3 bdwn_portable.py paths remove 2` | Retire le dossier numéro 2 |
| `python3 bdwn_portable.py paths edit 2 ~/Photos` | Remplace le dossier numéro 2 |
| `python3 bdwn_portable.py excludes list` | Liste les exclusions |
| `python3 bdwn_portable.py excludes add "*.iso"` | Ajoute une exclusion |
| `python3 bdwn_portable.py excludes remove 3` | Retire l'exclusion numéro 3 |
| `python3 bdwn_portable.py schedule install` | Installe ou met à jour la sauvegarde planifiée |
| `python3 bdwn_portable.py schedule remove` | Supprime la sauvegarde planifiée |
| `python3 bdwn_portable.py schedule status` | Affiche l'état de la planification |

Options valables avec toutes les commandes, à placer **avant** la commande :

| Option | Effet |
| --- | --- |
| `--config FICHIER` | Utilise ce fichier de configuration au lieu de celui par défaut. Les autres fichiers (clé, journal...) sont rangés dans le même dossier. |
| `--system` | Mode système, voir ci-dessous. |
| `--version` | Affiche la version du script. |

Exemple :

```sh
python3 bdwn_portable.py --config /srv/bdwn/config.json backup
```

La numérotation de `paths remove` et `excludes remove` est celle affichée par
`paths list` et `excludes list`. Dans le menu, une exclusion peut aussi être
modifiée ; en ligne de commande, retirez-la puis ajoutez la nouvelle.

### Mode système

Par défaut, le script sauvegarde **vos** fichiers, avec **vos** droits. Le mode
système sauvegarde tout l'ordinateur, avec les droits administrateur :

- **Linux** : lancez le script avec `sudo`. Le mode système est alors
  automatique (sauf si vous passez `--config`, auquel cas ajoutez `--system`).

  ```sh
  sudo python3 bdwn_portable.py
  ```

- **Windows** : ouvrez le terminal **en tant qu'administrateur** et ajoutez
  `--system`. Sans les droits administrateur, le script refuse.

  ```powershell
  python bdwn_portable.py --system
  ```

  Passez `--system` à chaque lancement, sinon le script cherche la
  configuration de l'utilisateur.

Le mode système a sa propre configuration, séparée de celle de l'utilisateur.

## Sauvegarde automatique

La sauvegarde planifiée tourne **une fois par jour** à l'heure choisie.
Installez-la depuis le menu (`6) Sauvegarde planifiee`) ou avec
`schedule install`. Pour changer l'heure, passez par le menu : il redemande
l'heure puis réinstalle la planification.

| | Mode utilisateur | Mode système |
| --- | --- | --- |
| **Linux** | Minuteur systemd de l'utilisateur (`~/.config/systemd/user/bdwn-portable.timer`) | Minuteur systemd du système (`/etc/systemd/system/bdwn-portable.timer`), lancé par root |
| **Windows** | Tâche planifiée `bdwn-portable`, lancée sous votre compte, que vous soyez connecté ou non (S4U, sans stocker de mot de passe) | Tâche planifiée `bdwn-portable`, lancée par le compte SYSTEM, avec les clichés instantanés (VSS) si choisis |

Comportement commun :

- **Rattrapage** : si l'ordinateur était éteint à l'heure prévue, la sauvegarde
  part dès qu'il est rallumé.
- **Nouvelles tentatives** : si une sauvegarde planifiée échoue (serveur
  injoignable, pas de réseau), le script réessaie lui-même, jusqu'à **10
  tentatives espacées de 30 minutes**.
- La sauvegarde tourne en priorité basse, pour ne pas ralentir l'ordinateur.
- Durée maximale d'une sauvegarde : 12 heures.

Particularités :

- **Linux, mode utilisateur** : le minuteur ne tourne que lorsque vous êtes
  connecté, sauf si le *lingering* est activé. Le script propose de l'activer
  à l'installation. Pour le faire à la main :

  ```sh
  loginctl enable-linger "$USER"
  ```

- **Linux** : un délai aléatoire de 5 minutes au plus s'ajoute à l'heure
  choisie.
- **Windows** : la tâche se lance aussi **à chaque ouverture de session**, en
  plus de l'heure quotidienne. Elle tourne aussi sur batterie.

Suivre les sauvegardes planifiées :

```sh
python3 bdwn_portable.py status                    # état et dernière sauvegarde
systemctl --user list-timers bdwn-portable.timer   # Linux, mode utilisateur
journalctl --user -u bdwn-portable.service         # Linux, mode utilisateur
```

Sous Windows, la tâche `bdwn-portable` est visible dans le **Planificateur de
tâches**.

## Où sont les fichiers

Tous les fichiers sont dans un seul dossier :

| Système et mode | Dossier |
| --- | --- |
| Linux, utilisateur | `~/.config/bdwn-portable` (ou `$XDG_CONFIG_HOME/bdwn-portable`) |
| Linux, système | `/etc/bdwn-portable` |
| Windows, utilisateur | `%APPDATA%\bdwn-portable` (par exemple `C:\Users\marie\AppData\Roaming\bdwn-portable`) |
| Windows, système | `%ProgramData%\bdwn-portable` (par exemple `C:\ProgramData\bdwn-portable`) |

Avec `--config`, c'est le dossier du fichier donné.

| Fichier | Contenu |
| --- | --- |
| `config.json` | Configuration : serveur, clé, **phrase de passe**, dossiers, exclusions, rétention, heure, langue |
| `id_ed25519`, `id_ed25519.pub` | Clé SSH privée et publique, si elle a été générée par le script |
| `ssh_config` | Réglages SSH vers le serveur, réécrits par le script à partir de `config.json` |
| `known_hosts` | Identité du serveur, enregistrée à la première connexion |
| `state.json` | Date de la dernière tentative, du dernier succès et de la dernière erreur |
| `backup.log` | Journal des sauvegardes (jusqu'à 4 fichiers de 1 Mo : `backup.log`, `backup.log.1`...) |

Droits d'accès, posés par le script :

- **Linux** : dossier en `0700`, `config.json`, `ssh_config` et la clé en
  `0600` : vous seul pouvez les lire (root en mode système).
- **Windows** : l'héritage des droits est coupé et seul votre compte a accès.
  En mode système, SYSTEM et le groupe Administrateurs ont aussi accès, et le
  groupe Administrateurs devient propriétaire.

Ne copiez jamais `config.json` ni la clé privée ailleurs : ils donnent accès à
vos sauvegardes.

## Rétention

Après chaque sauvegarde, les plus anciennes sont supprimées du serveur. Par
défaut, le script garde :

| Sauvegardes | Nombre gardé |
| --- | --- |
| Quotidiennes | 7 |
| Hebdomadaires | 4 |
| Mensuelles | 6 |

Changez ces nombres dans le menu, `8) Retention`. Seules les sauvegardes faites
par ce script sont concernées. Si le nettoyage échoue, la sauvegarde reste
valide et le nettoyage est refait la fois suivante.

## Exclusions

Les fichiers et dossiers qui correspondent à un motif d'exclusion ne sont pas
sauvegardés. Motifs par défaut :

```text
*.tmp  .cache  Cache  Caches  Temp  node_modules  Thumbs.db  desktop.ini  $RECYCLE.BIN  .Trash-*
```

Un motif sans `/` s'applique partout, à n'importe quelle profondeur. Exemples :

```sh
python3 bdwn_portable.py excludes add "*.iso"           # toutes les images ISO
python3 bdwn_portable.py excludes add Telechargements   # tous les dossiers Telechargements
```

Mettez le motif entre guillemets s'il contient `*`.

## Restaurer des fichiers

Depuis le menu, `7) Restaurer des fichiers`, ou :

```sh
python3 bdwn_portable.py restore
```

1. Le script liste les sauvegardes, de la plus ancienne à la plus récente, avec
   leur date. Tapez le numéro voulu (Entrée = la plus récente).
2. Choisissez le dossier où restaurer. Proposé : un nouveau dossier dans votre
   dossier personnel, par exemple `~/bdwn-restore-20261021-1230`. Vos fichiers
   actuels ne sont pas touchés.
3. Pour ne restaurer qu'une partie, donnez son chemin tel qu'il était dans la
   sauvegarde, par exemple `/home/marie/Documents/factures`. Laissez vide
   pour tout restaurer.

Les fichiers sont recréés dans le dossier choisi avec leur chemin complet :
`~/bdwn-restore-20261021-1230/home/marie/Documents/factures`. Copiez ensuite
ceux dont vous avez besoin.

### Restaurer sur un autre ordinateur

Si l'ordinateur d'origine est perdu, installez le script sur un nouvel
ordinateur et lancez l'assistant avec **la même adresse de serveur**, une clé
acceptée par le serveur et **la phrase de passe notée** (choix `2) Saisir la
mienne`). Lancez ensuite `restore`.

## Dépannage

### Messages du test de connexion

| Message | Signification | Que faire |
| --- | --- | --- |
| `OK : serveur joignable, cle acceptee, depot pret.` | Tout fonctionne. | Rien. |
| `OK : ... Le depot n'existe pas encore` | Connexion correcte, aucune sauvegarde encore faite. | Rien : le dépôt est créé à la première sauvegarde. |
| `Le serveur a refuse la cle SSH.` | La clé publique n'est pas installée sur le serveur. | Réaffichez-la (menu `9`) et renvoyez-la à l'administrateur. |
| `Le depot existe mais la phrase de passe est fausse.` | La phrase de passe ne correspond pas à ce dépôt. | Relancez `setup` avec la bonne phrase. |
| `L'identite du serveur (cle d'hote) a change.` | Le serveur a été réinstallé, ou quelqu'un se fait passer pour lui. | Vérifiez avec l'administrateur. Si c'est normal, supprimez `known_hosts` du dossier de configuration et relancez le test. |
| `Impossible de joindre le serveur ou d'ouvrir le depot.` | Réseau, nom du serveur, port, chemin... | Lisez les détails affichés en dessous. Vérifiez l'adresse et votre connexion. |

### Autres cas

- **`restic et/ou ssh introuvable`** : installez ce qui manque (voir
  [Installation](#installation)).
- **`Sauvegarde faite, mais certains fichiers n'ont pas pu etre lus`** : la
  sauvegarde est valide, mais certains fichiers manquent (fichiers ouverts ou
  sans droit de lecture). La liste est dans `backup.log`. Sous Windows, le mode
  système avec VSS règle la plupart des cas.
- **`Le mode systeme demande les droits administrateur`** : relancez avec
  `sudo` (Linux) ou dans un terminal ouvert en administrateur (Windows).
- **Une sauvegarde planifiée n'a pas eu lieu** : regardez `status`, puis
  `backup.log` dans le dossier de configuration. Chaque sauvegarde, manuelle
  ou planifiée, y écrit tout ce qu'elle fait.

### Phrase de passe perdue

Si la phrase de passe est perdue **et** que cet ordinateur ne fonctionne plus,
les sauvegardes sont **définitivement perdues**. Personne ne peut les
déchiffrer. Tant que l'ordinateur fonctionne, la phrase est lisible dans
`config.json` (champ `restic_password`) : notez-la tout de suite.

## Désinstaller

```sh
python3 bdwn_portable.py schedule remove
```

Supprimez ensuite le dossier de configuration (voir
[Où sont les fichiers](#où-sont-les-fichiers)) et le fichier
`bdwn_portable.py`. Les sauvegardes restent sur le serveur.

## Limites connues

- **Une partie du fonctionnement Windows n'a pas été testée sur un vrai
  Windows** : les droits posés avec `icacls`, l'enregistrement de la tâche
  planifiée et la recherche automatique de restic et d'OpenSSH. Vérifiez une
  première sauvegarde planifiée avant de vous y fier.
- **Windows, mode utilisateur** : l'enregistrement d'une tâche S4U (lancée même
  sans session ouverte) peut demander les droits administrateur selon la
  configuration de Windows. Si `schedule install` échoue, relancez-le depuis un
  terminal ouvert en administrateur.
- **Clés SSH avec phrase de passe** : refusées. Utilisez une clé dédiée sans
  phrase de passe, ou laissez le script en générer une.
- **Première connexion** : l'identité du serveur est acceptée automatiquement
  la première fois, puis vérifiée à chaque connexion.
- **Emplacement du script** : la sauvegarde planifiée pointe vers le chemin du
  script et de Python au moment de `schedule install`. Après un déplacement du
  script ou une mise à jour de Python, relancez `schedule install`.
- **Linux, mode utilisateur** : sans *lingering*, aucune sauvegarde
  automatique quand vous n'êtes pas connecté.
