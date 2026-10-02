# Validation du mode secours sur boîtier

Procédure manuelle : elle n'est pas lancée par `pytest`. Elle vérifie sur un **boîtier
réel en passerelle** ce que les tests unitaires ne font qu'affirmer : la perte de la box
est détectée, le mode secours s'ouvre au bon délai, les téléphones voient le portail,
personne ne sort, et le retour de la box ramène tout comme avant.

## Préparation

- Boîtier en passerelle, configuré, à jour. Accès SSH par un câble Ethernet ou par
  l'écran et le clavier du Pi : pendant les essais, il n'est plus joignable par la box.
- Un profil enfant avec sa clé Wi-Fi (cas A), un iPhone et un téléphone Android rattachés
  à ce profil.
- Accès à l'administration de la box (changer sa clé, son nom, couper son Wi-Fi).

### Délais réduits

Les délais réels sont de 15 min (clé refusée) et 24 h (box introuvable). Pour les essais,
on les réduit par deux variables lues par le runner, **réservées aux essais** :

```bash
sudo mkdir -p /etc/systemd/system/protectado-runner.service.d
printf '[Service]\nEnvironment=PROTECTADO_SECOURS_AUTH_SEC=120\nEnvironment=PROTECTADO_SECOURS_ABSENT_SEC=600\n' \
  | sudo tee /etc/systemd/system/protectado-runner.service.d/essais-secours.conf
sudo systemctl daemon-reload && sudo systemctl restart protectado-runner
```

Soit 2 min pour une clé refusée, 10 min pour une box introuvable. **À retirer à la fin**
(voir « Remise en état »).

### Relevés utiles pendant les essais

```bash
cat /opt/protectado/data/uplink_state.json; echo
cat /opt/protectado/data/secours.json; echo
wpa_cli -i wlan_up status | grep -E 'wpa_state|^ssid'
sudo iptables -t nat -S PREROUTING | head -5
sudo iptables -S PROTECTADO_FWD
sudo grep -E 'uplink|secours|SECOURS|réinitialisation' /var/log/protectado-runner.log | tail -20
```

## 1. Clé de la box changée (cas A)

1. Changer la clé Wi-Fi de la box. Si le boîtier reste connecté (certaines box ne
   déconnectent pas les appareils), redémarrer le Pi.
2. `uplink_state.json` : `connected` faux, `cause` `auth_refused`, `refused_sec` qui
   monte. Avant le délai, `http://protectado.admin` sur un téléphone enfant affiche le
   bandeau « le boîtier ne joint plus la box depuis… ».
3. Après 2 min : `secours.json` `active: true`, `case: "kids"` ; le premier saut de
   `PREROUTING` est `-i wlan_ap -j PROTECTADO_SECOURS` ; `PROTECTADO_FWD` ne contient
   **aucune** ligne `ACCEPT`.
4. iPhone puis Android, rejoindre le Wi-Fi enfants : la fenêtre du portail s'ouvre
   d'elle-même sur la page de secours. Noter si elle ne s'ouvre pas, et sur quel
   téléphone.
5. Page de secours : « Chercher les réseaux », choisir la box, saisir la **nouvelle**
   clé et un **mauvais** mot de passe parent : refus « Mot de passe incorrect », et
   rien ne change côté boîtier. Recommencer avec le bon mot de passe : « Test de la
   connexion », puis « Boîtier reconnecté ».
6. Attendu : `secours.json` `active: false`, `exit.action` `new_box` ; le filtrage
   reprend (un site bloqué l'est de nouveau, un site permis passe) ; dans le journal
   complet du tableau de bord : entrée en mode secours, puis fin avec « Un parent a
   reconnecté le boîtier ».

## 2. Wi-Fi de la box coupé, puis rallumé

Avec `PROTECTADO_SECOURS_ABSENT_SEC=1800` (30 min) le temps de cet essai.

1. Couper le Wi-Fi de la box 20 min.
2. `uplink_state.json` : `cause` `not_found`, `offline_sec` autour de 1200.
3. Rallumer : reconnexion en quelques minutes, `uplink_state.json` remis à zéro, aucun
   mode secours, aucun événement d'entrée au journal.

## 3. Box renommée (délai long)

1. Changer le nom du Wi-Fi de la box (la clé ne change pas). **Redémarrer le Pi** : un
   renommage ne déconnecte pas toujours les appareils déjà connectés (mesuré : le
   boîtier restait connecté sous l'ancien nom).
2. `uplink_state.json` : `cause` `not_found` (la box n'est plus listée sous son ancien
   nom), pas `auth_refused`.
3. Après 10 min : mode secours. Page de secours, choisir le **nouveau** nom, saisir la
   clé : reconnexion, `network.box.ssid` mis à jour dans `config.json`.

## 4. Réinitialisation depuis la page de secours

1. Provoquer le mode secours (essai 1, sans se reconnecter).
2. Page de secours, cocher « J'ai compris », « Réinitialiser » : compte à rebours de
   60 s. « Annuler la réinitialisation » avant la fin : rien n'est effacé (vérifier que
   `config.json` et `protectado.db` sont toujours là).
3. Recommencer et laisser aller au bout : le boîtier redémarre sur l'assistant neuf
   (Protectado-Setup).
4. Vérifier qu'il ne reste aucun historique :
   ```bash
   ls -la /opt/protectado/data/
   ls /etc/protectado/hostapd-psk /var/lib/misc/protectado-ap.leases 2>&1
   sudo ls -la /var/log/pihole/
   sudo python3 -c "import sqlite3;c=sqlite3.connect('/etc/pihole/pihole-FTL.db');print([(t,c.execute(f'select count(*) from {t}').fetchone()[0]) for t in ('query_storage','network','network_addresses')])"
   sudo python3 -c "import sqlite3;c=sqlite3.connect('/etc/pihole/gravity.db');print(c.execute('select name, description from \"group\"').fetchall());print(c.execute('select ip, comment from client').fetchall());print(c.execute(\"select domain, comment from domainlist where comment like 'protectado%'\").fetchall())"
   sudo journalctl --no-pager | head -3
   ```
   Attendu : ni `config.json` ni `protectado.db` ; pas de `hostapd-psk` ni de baux ;
   plus d'archives `*.log.*` dans `/var/log/pihole/` ; tables de requêtes et d'appareils
   de FTL vides ; plus aucun groupe, client ni domaine Protectado dans `gravity.db` ;
   journal systemd vidé. Conservés : `version.json`, `branch`, `pairing_code`.
   Points jamais vérifiés sur boîtier, à regarder en particulier : le schéma réel de
   `gravity.db` et l'existence de `pihole arpflush` en v6 (sortie de
   `factory_reset` dans `/var/log/protectado-runner.log`).

## 5. Sans réseau enfants (cas B)

1. Boîtier en passerelle **sans aucun profil enfant** (donc sans réseau enfants).
2. Changer la clé de la box. Après 2 min, le boîtier redémarre et diffuse
   **Protectado-Setup**.
3. S'y connecter : la page de secours s'ouvre à la place de l'assistant, avec les deux
   choix. Vérifier que l'assistant normal n'est pas accessible (`/onboarding` ramène à la
   page de secours).
4. « Reconnecter » avec la nouvelle clé et le mot de passe parent existant : le boîtier
   redémarre en passerelle. Le mot de passe parent est inchangé (connexion au tableau de
   bord avec l'ancien), `configured` vaut `true`.

## 6. Personne ne sort pendant le mode secours

Depuis un appareil enfant, en mode secours (cas A) :

```bash
curl -m 5 -sI http://exemple.com | head -1     # attendu : redirection vers /secours (portail)
curl -m 5 -sI https://exemple.com | head -1    # attendu : échec (aucune réponse)
curl -m 5 -sI https://1.1.1.1 | head -1        # attendu : échec, adresse IP directe
ping -c 2 -W 2 1.1.1.1                         # attendu : 100 % de pertes
dig +short exemple.com                         # attendu : 192.168.50.1 (le boîtier)
```

Un HTTP vers n'importe quelle adresse, IP comprise, arrive sur la page de secours : c'est
le portail, pas un accès à Internet.

## 7. Débranchement pendant l'attente

1. Box introuvable (Wi-Fi coupé), relever `offline_sec` dans `uplink_state.json`.
2. Débrancher le Pi 5 min, le rebrancher.
3. `offline_sec` reprend à la valeur d'avant le débranchement (les 5 min hors tension ne
   comptent pas), puis continue de monter.

## Remise en état

```bash
sudo rm /etc/systemd/system/protectado-runner.service.d/essais-secours.conf
sudo systemctl daemon-reload && sudo systemctl restart protectado-runner
```

Remettre la box dans son état d'origine (nom, clé, Wi-Fi).
