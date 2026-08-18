[🇬🇧 English](INSTALL.md) | [🇫🇷 Français](INSTALL.fr.md) | [🇪🇸 Español](INSTALL.es.md) | [🇵🇹 Português](INSTALL.pt.md)

# Protectado — Guide d'installation

Ce guide couvre l'installation complète de Protectado chez une nouvelle famille,
depuis la carte SD vierge jusqu'au dashboard opérationnel.

---

## Installation sur Linux existant (NAS, vieux PC...)

Si tu as déjà une machine Linux sur le réseau de la famille — un NAS, un mini-PC, un vieux PC sous Ubuntu — le bootstrap fonctionne directement dessus.

**Prérequis :**
- Debian / Ubuntu (le script utilise `apt`)
- La machine doit être sur le **même réseau local** que les appareils des enfants
- Pi-hole v6 déjà installé, **ou** pas encore installé (le bootstrap l'installe)
- Python 3.10 minimum (`python3 --version`)
- systemd actif

> **VPS / serveur distant : non compatible.** Pi-hole doit voir le trafic DNS local. Un serveur cloud ne peut pas jouer ce rôle sans VPN.

```bash
curl -sSL https://raw.githubusercontent.com/protectado/protectado/main/bootstrap/bootstrap.sh | sudo bash
```

Si Pi-hole est déjà installé et configuré, le bootstrap le détecte et le laisse intact — il installe uniquement Protectado par-dessus. Si Pi-hole est absent, il l'installe.

Reprendre ensuite à l'**Étape 4** ci-dessous (configuration via le wizard).

---

## Installation sur Raspberry Pi (voie nominale)

---

## Ce qu'il faut préparer AVANT d'aller chez la famille

### Matériel

| Article | Notes |
|---------|-------|
| Raspberry Pi | Pi 3B+, Pi 4 ou Pi 5 recommandé (Ethernet intégré). Pi 2W fonctionne en WiFi. |
| Carte SD | 16 Go minimum, classe 10 |
| Alimentation | USB-C (Pi 4/5) ou micro-USB (Pi 2W/3) |
| Câble Ethernet | Optionnel mais recommandé — branche le Pi directement sur la box |

### Comptes / clés à créer à l'avance

**Clé API OpenRouter** (indispensable — l'IA ne fonctionnera pas sans elle)
1. Créer un compte sur [openrouter.ai](https://openrouter.ai)
2. Ajouter du crédit (quelques euros suffisent pour plusieurs mois)
3. Générer une clé API → copier la clé (commence par `sk-or-`)

---

## Étape 1 — Préparer la carte SD (sur ton PC)

1. Télécharger **Raspberry Pi Imager** : [raspberrypi.com/software](https://www.raspberrypi.com/software/)
2. Insérer la carte SD dans ton PC
3. Dans Raspberry Pi Imager :
   - **Appareil** → choisir ton modèle de Pi
   - **Système d'exploitation** → `Ubuntu Server (64-bit)` (requis par la sandbox de l'agent IA)
   - **Stockage** → ta carte SD
4. Cliquer sur **⚙️ Modifier les réglages** (avant de flasher !)

Dans les réglages avancés, configurer :

```
✅ Nom d'hôte        → protectado
✅ Activer SSH        → Utiliser un mot de passe
   Nom d'utilisateur  → pi
   Mot de passe       → [choisir un mot de passe SSH]
✅ Configurer le WiFi → [SSID et mot de passe du foyer]
   Pays WiFi          → FR
```

> **Si tu utilises un câble Ethernet** : tu peux laisser le WiFi non configuré.
> Le Pi obtiendra son IP automatiquement via le câble.

5. Flasher la carte → insérer dans le Pi

---

## Étape 2 — Premier démarrage

1. Brancher le câble Ethernet **ou** laisser le WiFi se connecter automatiquement
2. Brancher l'alimentation
3. Attendre ~60 secondes (le Pi démarre et rejoint le réseau)

**Trouver l'adresse IP du Pi :**

```bash
# Option A — depuis ton PC sur le même réseau
ping protectado.local

# Option B — interface admin de la box (souvent 192.168.1.1)
# Chercher "protectado" ou "raspberrypi" dans la liste des appareils connectés
```

---

## Étape 3 — Connexion SSH et installation

```bash
ssh pi@protectado.local
# (ou ssh pi@192.168.x.x avec l'IP trouvée)
```

Une fois connecté, lancer l'installation en une seule commande :

```bash
curl -sSL https://raw.githubusercontent.com/protectado/protectado/main/bootstrap/bootstrap.sh | sudo bash
```

L'installation prend **5 à 10 minutes**. Elle installe automatiquement :
- Pi-hole (filtrage DNS)
- Protectado (agent IA + dashboard)
- Les mises à jour automatiques

À la fin, le script affiche :

```
╔══════════════════════════════════════════════════╗
║          Protectado installé avec succès !      ║
╚══════════════════════════════════════════════════╝

  Dashboard  →  http://192.168.x.x

  ┌─ Informations de configuration ─────────────────
  │  PIHOLE_PASSWORD :  xxxxxxxxxxxxxxxx
  │  PAIRING_CODE    :  XXXXXXXX
  └──────────────────────────────────────────────────
```

**Conserver le mot de passe Pi-hole** — pour l'interface d'admin Pi-hole, sur `http://<ip>:81`.

**Conserver le code d'appairage** — en mode DNS, l'assistant le demande avant d'accepter le mot de
passe parent. Tant que le boîtier n'est pas configuré, il répond à tout le réseau de la maison :
sans ce code, n'importe quel appareil — y compris celui d'un enfant — pourrait définir le mot de
passe parent avant vous. En mode passerelle, l'assistant n'est joignable que depuis le réseau
isolé `Protectado-Setup` et le code n'est pas demandé.

---

## Étape 4 — Assistant de premier démarrage

Au premier démarrage, Protectado choisit son **mode automatiquement** et ouvre un assistant.
Vous ne choisissez pas le mode — il dépend du matériel
(voir [modes de fonctionnement](../README.fr.md#deux-modes-de-fonctionnement)).

### Mode DNS (par défaut)

Depuis n'importe quel appareil du réseau, ouvrez `http://protectado.local` (ou l'IP de
l'étape 3). L'assistant demande le **mot de passe parent** du tableau de bord, puis affiche une
**étape indispensable**.

> ⚠️ **En mode DNS, rien n'est filtré tant que votre box n'envoie pas les appareils vers
> le boîtier.** Le Pi n'est pas routeur : il ne voit que les appareils qui l'interrogent
> pour résoudre les noms de sites.

Dans l'interface de votre box (souvent `http://192.168.1.1`), cherchez **DNS** dans les
réglages réseau ou DHCP, et remplacez le serveur DNS par l'adresse du boîtier — l'assistant
l'affiche. Redémarrez ensuite le Wi-Fi des appareils des enfants pour qu'ils la prennent en
compte.

Si votre box ne permet pas de changer le DNS, réglez-le appareil par appareil dans leurs
paramètres Wi-Fi. Les profils et plannings s'ajoutent ensuite depuis le tableau de bord.

### Mode passerelle (matériel compatible détecté)

Le boîtier diffuse un Wi-Fi ouvert temporaire nommé **`Protectado-Setup`**. Connectez-y un
téléphone — un portail captif s'ouvre tout seul — et suivez les étapes :

| Étape | Ce qu'il faut renseigner |
|-------|--------------------------|
| 1 | Votre box internet — choisir votre réseau Wi-Fi et saisir **sa** clé |
| 2 | Wi-Fi des enfants — un nom et une clé facile de 3 mots (WPA2) |
| 3 | Mot de passe parent du tableau de bord |
| 4 | Reconnecter le téléphone à votre box, ouvrir l'adresse indiquée, cliquer **Finir** |

Le boîtier redémarre alors en mode passerelle : le Wi-Fi des enfants s'active et se filtre,
le réseau de configuration disparaît. Le tableau de bord reste joignable sur `http://<ip-du-boîtier>`.

> La **clé API OpenRouter** se saisit plus tard, depuis le panneau de discussion du tableau
> de bord — pas dans cet assistant.

---

## Étape 5 — Assigner les appareils aux profils

Dans le dashboard → onglet **Appareils** :

1. Cliquer **Scanner le réseau**
2. Pour chaque appareil détecté : sélectionner le profil dans le menu déroulant
3. Cliquer **Assigner**

> **Astuce** : allumer les téléphones/tablettes des enfants pour qu'ils apparaissent dans le scan.

---

## Étape 6 — Configurer les plages horaires

Dans le dashboard → onglet **Profils** :

1. Cliquer **Modifier** sur un profil
2. Ajouter des plages horaires pour chaque jour de la semaine (l'ancien format
   Semaine/Weekend reste accepté en lecture)
3. Modes disponibles : `blocked` (tout coupé), `work` (éducatif seulement), `permissive` (accès libre)
4. Cliquer **Enregistrer**
5. Cliquer **⚙️ Reconfigurer Pi-hole** pour appliquer les groupes

---

## Sauvegarde & Restauration

Dans le dashboard → onglet **Gestion** → carte **Sauvegarde & Restauration** :

| Action | Description |
|--------|-------------|
| ⬇️ Télécharger | Génère un fichier ZIP contenant `config.json` (profils, planning, clés API) et la base de données SQLite |
| ⬆️ Restaurer | Importe un ZIP précédemment téléchargé — la configuration est rechargée immédiatement sans redémarrage |

> ⚠️ **Le ZIP contient des secrets EN CLAIR** : le mot de passe parent, la clé de l'API IA et, en mode passerelle, la clé Wi-Fi de votre box et celle du réseau enfants. Le téléchargement et la restauration exigent donc une nouvelle saisie du mot de passe parent. Conservez le fichier comme vous conserveriez ces mots de passe : jamais sur un espace partagé, jamais envoyé par courriel.

> **Conseil** : faire une sauvegarde avant chaque mise à jour manuelle et après toute modification importante des profils.

---

## Dépannage

**Le Pi n'apparaît pas sur le réseau**
- Attendre 2 minutes supplémentaires
- Vérifier que le SSID/mot de passe WiFi est correct (refaire l'étape 1)
- Essayer avec un câble Ethernet

**Le dashboard ne s'ouvre pas**
```bash
sudo systemctl status protectado-agent
sudo journalctl -u protectado-agent -n 30
```

**Pi-hole non accessible**
```bash
pihole status
sudo systemctl restart pihole-FTL
```

**Mettre à jour manuellement**
```bash
sudo bash /opt/protectado/update.sh
```

---

## Réinitialiser pour reconfigurer

Pour relancer l'assistant (ex. remettre le boîtier à une autre famille) :

```bash
# Reset simple — garde les valeurs, réaffiche juste l'assistant au prochain boot
sudo bash /opt/protectado/bootstrap/protectado-boot.sh reset && sudo reboot

# Reset total — sortie d'usine : efface config, Wi-Fi enregistré et état détecté
sudo bash /opt/protectado/bootstrap/protectado-boot.sh reset --full && sudo reboot
```

Après un reset total, le boîtier repart comme neuf et re-choisit son mode (DNS ou
passerelle) automatiquement au démarrage.

---

## Mises à jour automatiques

Protectado se met à jour seul chaque nuit à 3h00 depuis la branche **`stable`**.

`main` est la branche de développement ; `stable` est promue **manuellement**. Une
régression poussée le soir ne peut donc pas casser tous les boîtiers au réveil. La
branche retenue à l'installation est mémorisée dans `data/branch` et réutilisée par
l'updater : un boîtier ne change jamais de branche tout seul.

```bash
# Promouvoir la version courante de main vers stable (depuis le dépôt de dev)
git checkout stable && git merge --ff-only main && git push origin stable

# Installer une machine de test sur main plutôt que stable
curl -sSL .../bootstrap.sh | sudo PROTECTADO_BRANCH=main bash
```

La version installée (commit court, branche, date) est affichée dans le tableau de bord
→ onglet **Gestion** → carte **Mise à jour**, ainsi que sur la page de connexion.
Pi-hole se met à jour chaque dimanche à 4h00.
Les patches de sécurité OS s'installent automatiquement via `unattended-upgrades`.

---

## Mettre à jour une installation existante

Le script bootstrap détecte automatiquement une installation existante et passe en mode mise à jour au lieu de réinstaller.

```bash
curl -sSL https://raw.githubusercontent.com/protectado/protectado/main/bootstrap/bootstrap.sh | sudo bash
```

Ce que la mise à jour effectue :
1. Sauvegarde `config.json` et `protectado.db` dans un répertoire horodaté dans `/opt/`
2. Tire le dernier code depuis la branche suivie par le boîtier
3. Restaure `config.json` (vos profils et configuration sont conservés)
4. Lance les migrations de base de données (`database.init_db()`)
5. Redémarre les services

Si l'agent ne démarre pas après la mise à jour, le script revient automatiquement à la sauvegarde.
