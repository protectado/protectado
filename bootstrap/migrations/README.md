# Migrations système des boîtiers livrés

Une mise à jour ne fait qu'aligner le dépôt git puis redémarrer les services. Tout ce
qui vit **hors du dépôt** (un paquet système, un fichier de `/etc`, une unité systemd)
n'atteindrait donc jamais un boîtier déjà chez un client. Ce dossier est le seul chemin
prévu pour ces changements.

## Fonctionnement

- Le runner (root) applique ces scripts au démarrage, donc après chaque mise à jour,
  dans l'ordre de leur numéro, une seule fois chacun. Ce qui est fait est inscrit dans
  `/var/lib/protectado/migrations.done`, hors du dépôt.
- Il les exécute en tâche de fond, après l'armement de la protection : une migration
  n'en retarde jamais l'application.
- Un échec arrête la série (les suivantes peuvent en dépendre), s'affiche au parent sur
  le tableau de bord, et la série est retentée toutes les heures.
- Un script qui n'appartiendrait pas à root, ou que le groupe ou les autres pourraient
  modifier, est refusé : exécuté en root, il serait une porte vers root. Les migrations
  ne viennent que de ce dossier, jamais de `data/` ni de la file d'actions.
- Une installation neuve marque toutes les migrations comme faites : `bootstrap.sh`
  produit déjà l'état qu'elles décrivent.

## Ajouter une migration

1. Créer `NNNN-description-courte.sh`, avec le numéro suivant (quatre chiffres, minuscules
   et tirets). Ne jamais renuméroter ni modifier une migration publiée : un boîtier l'a
   peut-être déjà appliquée. Pour corriger, ajouter une nouvelle migration.
2. Écrire en tête les trois champs, vérifiés par les tests :
   - `# Effet :` ce que le script change sur le système ;
   - `# Équivalent bootstrap :` où `bootstrap.sh` produit le même état ;
   - `# Retour arrière :` la commande manuelle pour revenir en arrière.
3. Écrire un script **idempotent** (`set -euo pipefail`) : il vérifie ce qui est déjà
   fait et s'arrête sans erreur dans ce cas. Il peut être relancé après un échec.
4. Reporter le même changement dans `bootstrap.sh`, pour qu'un boîtier neuf naisse dans
   l'état final.
5. Rendre le script exécutable (`chmod 755`).

Variables disponibles : `INSTALL_DIR` (racine de l'installation). Le script tourne en
root, avec un `PATH` système minimal, depuis `INSTALL_DIR`.
