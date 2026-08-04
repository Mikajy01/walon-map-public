# Walonmap — Remplissage automatique du tableau Géoportail de Wallonie

Automatise le remplissage du fichier Excel « Walonmap » à partir des
services publics (ArcGIS REST) du [Géoportail de Wallonie](https://geoportail.wallonie.be/carte),
en reproduisant le travail de collecte manuelle décrit dans le document de
règles métier, sans utiliser de navigateur.

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

## Interface graphique

Pour une utilisation sans ligne de commande, `gui.py` fournit une interface
(CustomTkinter) reprenant les mêmes options : Pays, Commune, Code(s)
postal(aux) (optionnel), Limite (optionnel), fichier Excel gabarit à
importer (bouton « Parcourir… ») et fichier de sortie à choisir (bouton
« Enregistrer sous… », nom et emplacement libres). Une barre de progression
et un journal en direct affichent l'avancement pendant le traitement, qui
tourne en arrière-plan sans geler la fenêtre.

```bash
python gui.py
```

**Version portable (.exe)** : `build.bat` construit un exécutable Windows
autonome avec PyInstaller (aucune installation de Python requise sur la
machine cible) :

```bash
build.bat
```

Produit `dist\Walonmap.exe` (+ `dist\data\`). Pour distribuer l'application,
copier tout le dossier `dist` ; les dossiers `cache\`, `logs\`, `output\`
sont créés automatiquement à côté de l'exécutable au premier lancement (le
programme détecte s'il tourne en exécutable empaqueté et adapte ses chemins
en conséquence — voir `config.py`), donc l'ensemble reste déplaçable tel
quel (clé USB, autre PC) tout en conservant sa progression.

### Accès par commune (copies vendues à des tiers)

Le GUI demande un mot de passe (une seule fois par installation, sauvegardé
localement dans `.licence` puis revalidé automatiquement à chaque
lancement) avant de démarrer un traitement ou un import — vérifié en ligne
auprès du fichier public configuré dans `utils/licence.py::URL_AUTORISATIONS`
(jamais le mot de passe en clair, seulement un hash salé par commune).
Objectif : qu'une copie vendue pour une commune ne puisse pas être réutilisée
pour une autre, et que révoquer un accès (édition du fichier public, sans
toucher au client) coupe la copie déjà distribuée dès son prochain
lancement — nécessite donc une connexion internet à chaque lancement.

Gestion des accès (réservée au propriétaire, jamais empaqueté dans l'exe —
voir `build.bat`) :

```bash
python outils/gerer_licences.py generer --commune Chimay   # affiche le mot de passe à transmettre
python outils/gerer_licences.py revoquer --commune Chimay
python outils/gerer_licences.py lister
```

Met à jour `outils/autorisations.json` (jamais versionné, voir
`.gitignore`) — la publication vers l'emplacement public reste manuelle
(copier son contenu dans le Gist/fichier configuré), pour garder un
contrôle explicite sur ce qui est rendu public.

## Exécution automatique via GitHub Actions (sans utiliser son propre PC)

Ce dépôt (le code) est **public** : les minutes GitHub Actions y sont
gratuites et illimitées, contrairement à un dépôt privé (2000 min/mois sur
le plan gratuit, facturé au-delà). Les vraies données (progression, fichiers
Excel livrés aux clients) vivent volontairement dans un **second dépôt,
privé** (`Mikajy01/walon-map-data`) — jamais dans ce dépôt-ci, pour ne
jamais les exposer publiquement. Le workflow clone ce dépôt de données au
début de chaque exécution (via un jeton stocké en secret GitHub Actions,
`DATA_REPO_TOKEN`) et y pousse les résultats à la fin.

Utilisation : onglet **Actions** de ce dépôt → *Traiter une commune* →
**Run workflow**, en renseignant la commune, la limite, le débit, etc. À la
fin (ou même si le job s'arrête avant la fin — rien n'est perdu), la base de
progression (`cache/<commune>/http_cache.sqlite3`, cache HTTP brut vidé
avant de committer — seule la vraie progression est conservée), le fichier
Excel (`output/<commune>.xlsx`) et les logs sont automatiquement commités et
poussés dans **walon-map-data** : `git pull` de ce dépôt (privé) pour les
récupérer.

Déclenchement manuel uniquement (pas de planification automatique) — chaque
run reprend là où le précédent s'est arrêté, exactement comme des
exécutions locales successives. À surveiller : les runners hébergés gratuits
de GitHub sont limités à 6h par exécution, et le débit réel observé est
souvent inférieur au débit configuré (latence réseau par requête, pas la
limitation de débit elle-même) — une limite très élevée peut ne pas
tenir dans une seule exécution ; relancer simplement le workflow pour
continuer.

## Utilisation en ligne de commande

```bash
python main.py --commune Crisnée --limit 50
python main.py --commune Crisnée                      # sans limite : traite tout ce qui reste
python main.py --commune Crisnée --commune Awans --debug
python main.py --commune Crisnée --code-postal 4367 --limit 50   # restreint à un code postal
```

- `--commune` : nom de commune à traiter (répéter l'option pour plusieurs communes). Le programme découvre automatiquement toutes les rues de la commune via le registre ICAR, puis toutes les adresses de chaque rue.
- `--code-postal` : optionnel, restreint le traitement à ce(s) code(s) postal/postaux au sein de la commune (répéter l'option pour en spécifier plusieurs). Omis = tous les codes postaux de la commune sont traités.
- `--limit` : nombre maximum de **nouvelles** parcelles à résoudre à cette exécution (voir « Traitement incrémental » ci-dessous). Omis = traite tout ce qui reste.
- `--rate-limit` : requêtes par seconde vers le Géoportail (par défaut `5.0`). Augmenter accélère le traitement, au prix d'un risque un peu plus élevé d'erreurs transitoires côté serveur (déjà gérées par réessai automatique). Dans le GUI, c'est le curseur « Vitesse (requêtes/s) », réglable de 1 à 20.
- `--pays` : valeur de la colonne Pays (par défaut `Belgique`).
- `--debug` : journalisation détaillée (chaque service interrogé, paramètres, réponses, raison de chaque valeur — voir `logs/walonmap.log`).
- `--no-cache` : désactive le cache HTTP local (déconseillé, fait perdre la reprise après interruption).
- `--template` : chemin du fichier Excel gabarit, si différent de `config.TEMPLATE_PATH` (par défaut, le dossier parent de `main.py`). Utilisé par le workflow GitHub Actions, où le gabarit est versionné dans le dépôt plutôt qu'à côté.

Le code postal n'est jamais demandé en entrée obligatoire : il est lu
automatiquement pour chaque adresse dans le registre ICAR (champ
`CODE_POSTAL`), seule source fiable pour les communes fusionnées pouvant
couvrir plusieurs codes postaux (anciennes communes/sections rattachées).
`--code-postal` sert uniquement à *filtrer* parmi ces adresses déjà
identifiées, pas à les découvrir.

Le fichier de sortie est produit dans `output/<commune>.xlsx` (un seul
fichier par commune, toujours à jour — c'est la référence, le total réel),
à partir d'une copie du gabarit `Entête walonmap (avec colonnes en plus).xlsx`
(mise en forme conservée). Sauf si `--output` (interne, utilisé par le GUI)
est fourni, une copie datée est en plus archivée à côté à chaque exécution
(`output/<commune>-JJ-MM-AAAA.xlsx`, numérotée en cas de collision le même
jour : `... 2.xlsx`, `... 3.xlsx`, etc.) — pratique comme instantané/historique,
mais **jamais** la référence à utiliser pour connaître le total réel (voir
« Traitement incrémental » ci-dessous) : c'est toujours `<commune>.xlsx` sans
date qui contient l'intégralité de la progression.

## Traitement incrémental (`--limit`)

Le travail équivalent était auparavant réparti sur environ un mois de
saisie manuelle : `--limit` permet de reproduire ce rythme en ne traitant
qu'un lot de parcelles à la fois, sur plusieurs exécutions successives,
sans jamais recalculer ni dupliquer une ligne déjà produite.

Concrètement, à chaque exécution :

1. **Découverte** : toutes les rues et adresses de la commune sont
   toujours listées en entier (registre ICAR, bon marché — une requête par
   rue, quelques secondes même pour ~1300 adresses).
2. **Tri** : pour chaque adresse, le programme vérifie si elle a déjà été
   entièrement résolue lors d'une exécution précédente (sans aucun appel
   réseau supplémentaire).
3. **Résolution** : seules les adresses pas encore résolues sont
   effectivement traitées (rattachement cadastral + interrogation des ~90
   couches métier), jusqu'à concurrence de `--limit`. C'est l'étape
   coûteuse, donc la seule bornée par `--limit`.
4. **Écriture** : `output/<commune>.xlsx` est reconstruit à partir de
   TOUTES les adresses déjà résolues (précédentes + nouvelles) — chaque
   ligne apparaît une seule fois ; les adresses pas encore résolues
   n'apparaissent simplement pas encore.

Chaque exécution affiche le compte total, ex. :
`Commune 'Crisnée' : 1274 parcelle(s) au total, 5 déjà traitée(s), 50 à
traiter à cette exécution (limite=50).` Relancer la même commande plus
tard reprend automatiquement la suite.

## Reprise après interruption

Chaque parcelle entièrement traitée est mémorisée dans
`cache/http_cache.sqlite3` (table `parcelle_resultats`, distincte du cache
HTTP mais dans le même fichier). Relancer le programme sur la même
commune — avec ou sans interruption entretemps — reprend automatiquement
là où il s'était arrêté, sans re-solliciter les services déjà interrogés
(le cache HTTP, dans la même base, évite aussi toute requête réseau
redondante).

## Cellules `"ERREUR"` et import d'un Excel corrigé

Si une colonne échoue à se résoudre (erreur réseau persistante, service
externe en panne, etc.), la cellule reçoit la valeur `"ERREUR"` et le
traitement continue normalement pour le reste — mais cette valeur reste
figée telle quelle pour cette parcelle, elle n'est jamais retentée
automatiquement au relancement suivant (les colonnes déjà résolues ne sont
pas recalculées, par conception, pour ne jamais refaire d'appels réseau
inutiles).

Ces cellules ne sont cependant pas figées pour toujours : à **chaque
nouvelle exécution**, toute cellule encore à `"ERREUR"` est automatiquement
retentée (uniquement les colonnes concernées, pas toute la parcelle — les
colonnes déjà résolues avec succès ne sont jamais re-sollicitées). C'est
utile en pratique : un échec est souvent un incident transitoire côté
serveur du Géoportail (observé en conditions réelles — plusieurs colonnes en
échec pendant quelques minutes, puis de nouveau disponibles normalement) et
se résout donc tout seul à la prochaine exécution, sans aucune action
manuelle. Le journal indique combien de cellules ont été corrigées ainsi
(`X cellule(s) corrigée(s) au nouvel essai`). Si l'échec persiste (ex :
donnée réellement absente du service, ou vraie erreur de requête), la
cellule reste `"ERREUR"` et continue d'être retentée aux exécutions
suivantes, sans jamais bloquer le reste du traitement ni masquer le
problème dans les logs.

Pour une correction immédiate sans attendre une nouvelle exécution : ouvrez
le fichier Excel généré, corrigez à la main les cellules marquées
`"ERREUR"` (en revérifiant la valeur sur le Géoportail), enregistrez, puis
dans le GUI cliquez **« Importer un Excel corrigé… »** et sélectionnez ce
fichier. L'outil :

1. relit le fichier et retrouve chaque ligne dans la progression enregistrée
   (par Rue + Numéro + Numéro cadastral) ;
2. pour chaque ligne reconnue, reprend uniquement les colonnes où la base
   valait `"ERREUR"` et où l'Excel a maintenant une vraie valeur — aucune
   autre cellule n'est jamais modifiée, même si elle diffère de l'Excel ;
3. pour chaque ligne **non reconnue** (absente de la base — ex: une parcelle
   ajoutée à la main dans l'Excel), vérifie d'abord son existence réelle au
   registre ICAR (la rue, puis le numéro) avant de l'accepter : jamais de
   donnée inventée à partir du seul texte de l'Excel. Si la rue ou le
   numéro n'existe pas au registre, la ligne est ignorée et comptée
   « invalide ». Si elle existe mais correspond en réalité à une parcelle
   déjà en base sous un identifiant différent (ex: numéro cadastral
   orthographié différemment) — contrôle de redondance —, elle est traitée
   comme une correction (étape 2) plutôt que dupliquée. Sinon, c'est une
   parcelle réellement nouvelle : ses colonnes d'identification (A-F) sont
   recalculées depuis les registres officiels (ICAR/CADMAP, jamais copiées
   du texte Excel), ses colonnes de données sont reprises telles quelles
   depuis l'Excel, et elle est ajoutée à la base ;
4. réécrit le fichier sur place avec l'état à jour ;
5. affiche un rapport complet : lignes reconnues et cellules `"ERREUR"`
   corrigées, nouvelles parcelles ajoutées, doublons détectés, lignes
   invalides (rue/numéro introuvable), avec le décompte final de parcelles
   en base pour la commune.

Aucun changement de schéma de la base : les anciens `http_cache.sqlite3` et
anciens fichiers Excel restent utilisables tels quels avec cette fonction.

## Table de correspondance communale (liens manuels)

Certaines colonnes (`AP` — lien du schéma de développement communal, `CL` —
lien du contrat de rénovation urbaine) ne peuvent pas être trouvées de
manière fiable par une recherche automatique (voir le rapport de
validation). Elles sont renseignées via `data/liens_communaux.csv`, à
compléter manuellement une fois par commune :

```csv
commune,lien_schema_developpement_communal,lien_contrat_renovation_urbaine
Crisnée,https://...,https://...
```

Si une commune est absente de ce fichier, la cellule correspondante reçoit
la valeur `À COMPLÉTER MANUELLEMENT` (avec un avertissement dans les logs)
plutôt qu'une valeur inventée.

## Architecture

```
main.py                    Point d'entrée CLI (orchestration, reprise) — traiter_commune() est réutilisé par gui.py
gui.py                     Interface graphique (CustomTkinter), même logique que le CLI
build.bat                  Construit l'exécutable portable (.exe) via PyInstaller
config.py                  URLs des services ArcGIS REST + règle de chaque colonne (chemins compatibles PyInstaller)
services/
    cadastre_service.py    Découverte des rues/adresses (ICAR) + rattachement cadastral (CADMAP)
    geoportail_service.py  Client ArcGIS REST générique (query, identify, cache, réessai, débit)
    layers_service.py      Moteur générique : interprète les règles de config.py
    excel_service.py       Lecture du gabarit / écriture du fichier de sortie / relecture d'un Excel existant
    sync_service.py        Synchronisation d'un Excel corrigé (reprise des cellules "ERREUR") avec la progression
    cache_service.py       Cache HTTP local + mémorisation des résultats (reprise)
models/
    parcelle.py             Une adresse/parcelle à traiter
    rules.py                 Définition déclarative des règles de colonne (RuleType, ColumnRule, LayerRef)
utils/
    logger.py, rate_limiter.py, retry.py, progress.py
data/liens_communaux.csv    Table manuelle (voir ci-dessus)
cache/, logs/, output/
```

Ajouter ou corriger une colonne ne nécessite jamais de nouveau code : une
seule stratégie d'exécution existe par type de règle
(`models.rules.RuleType`), toute la logique métier par colonne vit dans
`config.COLUMN_RULES`.

## Choix technique : pas de geopandas/shapely/pyproj/fiona

Toutes les opérations spatiales (intersection parcelle/couche) sont
déléguées au serveur ArcGIS REST via le paramètre `geometry` des requêtes
`query`/`identify` : la géométrie de la parcelle (renvoyée par CADMAP) est
transmise telle quelle, sans jamais être manipulée localement. Ces
bibliothèques géospatiales (dépendances lourdes, GDAL requis) n'apportent
donc pas de bénéfice ici et ont été volontairement omises ; `requirements.txt`
ne liste que ce qui est réellement utilisé.

## Points connus, non automatisables

Voir le rapport de validation transmis avant développement : colonnes `AC`
(zone de recul) et `AD` (zone d'accès et de stationnement) sans couche
source identifiée dans le plan de secteur (valeur `N` par défaut,
journalisée) ; colonnes `AP`/`CL` nécessitant la table manuelle
ci-dessus.
