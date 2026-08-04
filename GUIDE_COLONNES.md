# Guide de correspondance colonne par colonne

| Colonne | Procédé manuel (ce qu'il aurait fallu faire à la main) | Logique dans le code |
|---|---|---|
| A — Pays | Valeur fixe, ne se cherche pas. | Valeur constante `"Belgique"` écrite directement, aucun appel réseau. |
| B — Code Postal | Rechercher l'adresse dans l'annuaire ICAR et lire le code postal affiché. | Interroger le service ICAR (`DONNEES_BASE/ICAR_ADR_PT`) pour la rue, lire le champ `CODE_POSTAL` de chaque adresse retournée (pas une valeur unique par commune). |
| C — Communes | Nom de la commune traitée. | Fourni en paramètre au programme (`--commune`). |
| D — Rue | Ouvrir la couche « ICAR - Listing des noms de rue », lister toutes les rues de la commune. | Interroger ICAR (`ICAR_ADR_PT`), extraire les valeurs distinctes du champ `RUE_NM` pour la commune donnée. |
| E — Numéro | Ouvrir la couche « ICAR - Points d'adresses », relever chaque numéro ; mettre `/` si absent. | Pour chaque adresse retournée par ICAR, lire le champ `ADR_NUMERO` ; si vide, écrire `/`. |
| F — Numéro cadastral | Cliquer sur la parcelle sous l'adresse dans « Plan parcellaire cadastral - situation courante », lire le numéro affiché. | Prendre la position (x,y) du point d'adresse ICAR, interroger `PLAN_REGLEMENT/CADMAP_PARCELLES` en intersection à ce point, récupérer les champs `RADICAL`/`BIS`/`EXPOSANT`/`PUISSANCE`, puis reconstituer le numéro lisible (zéros de tête supprimés, `/` inséré avant le bis s'il est non nul). |
| G — Zone d'habitat | Afficher « Plan de secteur en vigueur (version coordonnée vectorielle) » > Zones d'affectation, cliquer sur la parcelle, regarder si la légende affiche « Habitat ». | Interroger `AMENAGEMENT_TERRITOIRE/PDS` (couche 22) en intersection avec la géométrie de la parcelle, lire le champ `AFFECT`, comparer au code `H01` → O si trouvé, sinon N. |
| H — Habitat à caractère rural | Idem G, regarder si la légende affiche « Habitat à caractère rural ». | Idem G, comparer `AFFECT` au code `H02`. |
| I — Habitat vert | Idem G, « Habitat vert ». | Idem G, code `H03`. |
| J — Services publics et équipements communautaires | Idem G. | Idem G, code `P01`. |
| K — Zone de loisirs | Idem G, « Loisirs ». | Idem G, codes `L01`/`L11`/`L12`/`L13`. |
| L — Activité économique mixte | Idem G. | Idem G, code `A01`. |
| M — Activité économique industrielle | Idem G. | Idem G, code `A02`. |
| N — Activité économique spécifique agro-économique OU grande distribution | Idem G. | Idem G, codes `A11` ou `A12`. |
| O — Activité économique spécifique Risque majeur | Idem G. | Idem G, code `A13`. |
| P — Zones de dépendances Extraction à destination agricole | Idem G. | Idem G, code `XR1`. |
| Q — Zones de dépendances Extraction à destination forestière | Idem G. | Idem G, code `XR2`. |
| R — Zones de dépendances Extraction à destination espaces verts | Idem G. | Idem G, code `XR3`. |
| S — Zones de dépendances Extraction à destination zone naturelle | Idem G. | Idem G, code `XR4`. |
| T — Zone d'aménagement communautaire concerté | Idem G. | Idem G, code `D01`. |
| U — Zone d'enjeu communal | Idem G. | Idem G, code `ZEC`. |
| V — Zone d'enjeu régional | Idem G. | Idem G, code `ZER`. |
| W — Zone d'aménagement communautaire concerté à caractère économique | Idem G. | Idem G, code `D02`. |
| X — Zone agricole | Idem G. | Idem G, code `R01`. |
| Y — Zone forestière | Idem G. | Idem G, code `R02`. |
| Z — Zone d'espaces verts | Idem G. | Idem G, code `R03`. |
| AA — Zone naturelle | Idem G. | Idem G, code `R04`. |
| AB — Zone de parc | Idem G. | Idem G, code `R05`. |
| AC — Zone de recul | Chercher cette notion dans la légende « Zones d'affectation » — **elle n'y figure pas**, ni dans aucune autre couche identifiée du Géoportail actuel. | Aucun service trouvé après vérification exhaustive des 34 codes existants du champ `AFFECT`. Valeur `N` appliquée par défaut, avertissement journalisé à chaque utilisation. |
| AD — Zone d'accès et de stationnement | Idem AC — introuvable dans la légende. | Idem AC : aucune source, `N` par défaut. |
| AE — Zone d'extraction | Idem G, « Dépendances d'extraction » (libellé officiel du service, le plus proche de « zone d'extraction »). | Idem G, code `X01`. |
| AF — Zone non affectée | Idem G, « Non affectée / zone blanche ». | Idem G, codes `P12`/`V01`/`V02`. |
| AG — Points de vue remarquable | Afficher Plan de secteur > Périmètres de protection, cocher « Points de vue remarquable » si elle recouvre la parcelle. | Interroger `AMENAGEMENT_TERRITOIRE/PDS` couche 15 en intersection avec la parcelle → O si un résultat, sinon N. |
| AH — Périmètres de points de vue remarquable | Idem AG. | Idem AG, PDS couche 16. |
| AI — Intérêt paysager | Idem AG. | Idem AG, PDS couche 17. |
| AJ — Intérêt culturel, historique ou esthétique | Idem AG. | Idem AG, PDS couche 18. |
| AK — Liaisons écologiques | Idem AG. | Idem AG, PDS couche 19. |
| AL — Réservation d'infrastructure principale | Idem AG. | Idem AG, PDS couche 20. |
| AM — Extension de zone d'extraction | Idem AG. | Idem AG, PDS couche 21. |
| AN — Aire d'habitat | Non documenté nulle part ; toujours laissé à `/` (confirmé avec l'employeur). | Valeur constante `/`, aucun appel réseau. |
| AO — Schéma de développement communal | Rechercher « schéma » dans le Géoportail, activer « Schéma de Développement Communal (SDC) », regarder si une couche existe pour la commune. | Interroger `AMENAGEMENT_TERRITOIRE/SDC` (couche 0) en intersection avec la parcelle → O/N selon présence. |
| AP — Lien Schéma de développement communal | Si AO = O : chercher sur Google « schéma de développement communal + nom de la commune », ouvrir le document trouvé, copier son lien (même lien pour toutes les rues de la commune). | Si AO = O : lire le lien dans `data/liens_communaux.csv` (colonne `lien_schema_developpement_communal`) pour la commune ; sinon `/`. Si la commune est absente du fichier, écrire « À COMPLÉTER MANUELLEMENT » plutôt que d'inventer un lien. |
| AQ — Schéma d'orientation locale | Activer « Schéma d'Orientation Local (SOL) », regarder si présent. | Interroger `AMENAGEMENT_TERRITOIRE/SOL` (couche 0) en intersection → O/N. |
| AR — GRU, Zones Protégées en matière d'Urbanisme | Rechercher « GRU », activer la sous-couche « Zones Protégées en matière d'Urbanisme ». | Interroger `AMENAGEMENT_TERRITOIRE/GRU` couche 5 en intersection → O/N. |
| AS — GRU, Règlement Général sur les Bâtisses en Site Rural | Idem AR, sous-couche « Site Rural ». | Idem AR, GRU couche 4. |
| AT — GRU, Qualité acoustique des constructions | Idem AR, sous-couche « Qualité acoustique ». | Idem AR, GRU couche 3. |
| AU — Guide communal d'urbanisme | Rechercher « GCU », regarder si présent. | Interroger `AMENAGEMENT_TERRITOIRE/GCU` couche 0 en intersection → O/N. |
| AV — Aléa très faible | Dézoomer la carte pour afficher la version « petite échelle » de la cartographie de l'aléa d'inondation, lire la couleur/classe sous la parcelle. | Interroger `EAU/ALEA_INOND` couche 1 (raster) via l'opération `identify` (le `/query` classique ne fonctionne pas sur un raster classifié) : la géométrie entière ET chacun de ses sommets sont interrogés séparément pour ne pas manquer une classe qui ne touche qu'un coin de la parcelle ; lire le champ `Raster.VALEUR`, comparer à « Aléa très faible ». |
| AW — Aléa faible | Idem AV. | Idem AV, comparer à « Aléa faible ». |
| AX — Aléa moyen | Idem AV. | Idem AV, comparer à « Aléa moyen ». |
| AY — Aléa élevé | Idem AV. | Idem AV, comparer à « Aléa élevé ». |
| AZ — Risque de ruissellement diffus (ERRUISOL) | Activer la couche ERRUISSOL - Risque de ruissellement diffus, regarder si classée sous la parcelle. | Interroger `SOL_SOUS_SOL/ERRUISSOL__RUISSELLEMENT_DIFFUS` (raster) via `identify` (polygone + sommets) ; O si une classe autre que « NoData » est trouvée. |
| BA — Axe de concentration de ruissellement (LIDAXES v2) | Activer LIDAXES (version 2), regarder si un axe touche la parcelle. | Interroger `EAU/LIDAXES` couche 1 en intersection → O/N. |
| BB — Zone de surveillance | Activer « Protection des captages », ouvrir les légendes, cocher « Zone de surveillance ». | Interroger `EAU/PROTECT_CAPT` couche 0 en intersection → O/N. |
| BC — Zone de prévention rapprochée IIa | Idem BB, « Prévention rapprochée IIa ». | Idem BB, PROTECT_CAPT couche 2. |
| BD — Zone de prévention éloignée IIb | Idem BB, « Prévention éloignée IIb ». | Idem BB, PROTECT_CAPT couche 3. |
| BE — Zones de prévention forfaitaires rapprochée IIa | Idem BB, « Forfaitaire rapprochée IIa ». | Interroger PROTECT_CAPT couche 4 en intersection, lire le champ `TYPE_CODE`, comparer à `"IIa"`. |
| BF — Zones de prévention forfaitaires éloignée IIb | Idem BB, « Forfaitaire éloignée IIb ». | Idem BE, comparer `TYPE_CODE` à `"IIb"`. |
| BG — Collectif | Activer PASH, n'afficher que « Régime d'assainissement », lire la couleur (collectif = rose). | Interroger le service externe PASH (SPGE, hors Géoportail), lire les champs `pashcode` et `pdsaffectcode` de la parcelle : O si `pashcode='I'` ET `pdsaffectcode` différent de `'HZU'`. |
| BH — Collectif hors zone urbanisable | Idem BG, distinguer visuellement la sous-zone « hors zone urbanisable ». | Idem BG, mais O si `pashcode='I'` ET `pdsaffectcode='HZU'` (les deux champs du même service sont croisés). |
| BI — Autonome | Idem BG, régime « autonome ». | Interroger PASH, O si `pashcode='II'`. |
| BJ — Transitoire | Idem BG, régime « transitoire ». | Interroger PASH, O si `pashcode='III'`. |
| BK — Zone archéologique (carte archéologique) | Activer « Carte archéologique de Wallonie (CAW) ». | Interroger `AMENAGEMENT_TERRITOIRE/CAW` couche 0 en intersection → O/N. |
| BL — Patrimoine immobilier culturel (inventaire régional) | Activer « Inventaire Régional du Patrimoine (IPIC) ». | Interroger `AMENAGEMENT_TERRITOIRE/IPIC` couche 0 en intersection → O/N. |
| BM — Biens mondiaux (Unesco) | Activer la couche des biens Unesco. | Interroger `AMENAGEMENT_TERRITOIRE/PAT_MND_UNESCO` couche 0 en intersection → O/N. |
| BN — Bien classé et zone de protection | Activer « Biens classés », cocher toutes les sous-catégories (monuments, sites, ensembles, sites archéo classés, zones de protection) — le document de référence précise explicitement « et zone de protection » pour cette colonne. | Interroger successivement les 5 sous-couches de `AMENAGEMENT_TERRITOIRE/BC_PAT` (0 à 4) en intersection ; O dès que l'une d'elles retourne un résultat. |
| BO — Bien en liste de sauvegarde | Activer « Biens en liste de sauvegarde », cocher les sous-catégories sauf « zone de protection » (non mentionnée pour cette colonne). | Interroger les 4 sous-couches 0 à 3 de `AMENAGEMENT_TERRITOIRE/PAT_LSTSAV` ; O dès qu'une intersecte. |
| BP — Biens exceptionnels | Idem BO, sur la couche des biens exceptionnels. | Interroger les 4 sous-couches 0 à 3 de `AMENAGEMENT_TERRITOIRE/PAT_EXC` ; O dès qu'une intersecte. |
| BQ — Permis d'urbanisation | Taper « permis d'urbanisation et lotissement » dans la recherche du Géoportail, activer la couche trouvée. | Interroger `AMENAGEMENT_TERRITOIRE/LOT` couche 0 en intersection → O/N. |
| BR — Plan Habitat permanent | Taper « plan habitat permanent », activer la couche. | Interroger `AMENAGEMENT_TERRITOIRE/PLANHP` couche 0 en intersection → O/N. |
| BS — Périmètre de remembrement urbain | Taper « PRU », activer la couche. | Interroger `AMENAGEMENT_TERRITOIRE/RMBMT_URB` couche 0 en intersection → O/N. |
| BT — Site à réaménager de droit | Taper « SAR », activer la couche. | Interroger `AMENAGEMENT_TERRITOIRE/SAR` couche 0 en intersection → O/N (service distinct de celui utilisé pour CB-CF). |
| BU — Sites SEVESO | Taper « SEVESO », activer la couche. | Interroger `INDUSTRIES_SERVICES/SEVESO` couche 1 (contours des entreprises, sans distinguer petit/grand seuil, une seule colonne dans le gabarit) → O/N. |
| BV — Rénovation urbaine | Activer la couche dédiée « Rénovation urbaine ». | Interroger `AMENAGEMENT_TERRITOIRE/RENOV_URB` couche 0 en intersection → O/N. |
| BW — Revitalisation urbaine | Activer la couche dédiée « Revitalisation urbaine ». | Interroger `AMENAGEMENT_TERRITOIRE/REVIT_URB` couche 0 en intersection → O/N. |
| BX — Périmètre de reconnaissance économique | Taper « PRE », activer la couche. | Interroger `AMENAGEMENT_TERRITOIRE/PRE` couche 0 en intersection, lire le champ `NATURE`, comparer à `"PRE"` (le service mélange PRE/DRPRE/PEX dans le même champ, il faut filtrer). |
| BY — Terrains non urbanisés en zones destinées au plan de secteur | Ouvrir la capture de référence, activer la couche correspondante. | Interroger `HABITAT/TNU_INZH` couche 0 en intersection → O/N. |
| BZ — Terrils, Non Majeur | Activer « Terrils du point de vue aménagement et urbanisme ». | Interroger `AMENAGEMENT_TERRITOIRE/TERRILS` couche 0 en intersection, lire le champ `MAJEUR`, comparer à `"Non"`. |
| CA — Terrils, Majeur | Idem BZ. | Idem BZ, comparer `MAJEUR` à `"Oui"`. |
| CB — Site à réaménager (Périmètres des sites d'activité) | Activer « Sites à réaménager (inventaire) ». | Interroger `AMENAGEMENT_TERRITOIRE/ISA` couche 0 en intersection → O/N. |
| CC — Site à réaménager (Activités antérieures et actuelles) | Idem CB. | Idem CB, ISA couche 1. |
| CD — Site à réaménager (Inventaire) - Potentiel de reconversion | Idem CB. | Idem CB, ISA couche 2. |
| CE — Site à réaménager (Inventaire) - Bâtiments | Idem CB. | Idem CB, ISA couche 3. |
| CF — Site à réaménager (Inventaire) - Site à Réaménager (sar) | Idem CB. | Idem CB, ISA couche 4. |
| CG — Zones d'initiative privilégiée | Taper « ZIP », activer la couche. | Interroger `AMENAGEMENT_TERRITOIRE/ZIP` couche 0 en intersection → O/N. |
| CH — Risques d'érosion hydrique diffuse | Activer la couche, dézoomer pour l'afficher. | Interroger les 3 couches raster de `SOL_SOUS_SOL/ERRUISSOL__EROSION_DIFFUSE` (prairie, culture non sarclée, culture sarclée) via `identify` ; O dès qu'une classe est trouvée sur l'une des 3. |
| CI — Arbres et haies remarquables | Activer « Arbres et haies remarquables (AHREM) ». | Interroger les 4 sous-couches de `FAUNE_FLORE/AHREM` (0 à 3) en intersection ; O dès qu'une intersecte. |
| CJ — Site Natura 2000 | Activer « Réseau Natura 2000 », couche des périmètres de sites. | Interroger les couches 9 et 10 de `FAUNE_FLORE/NATURA2000` (périmètres des sites, pas les unités de gestion internes) ; O dès qu'une intersecte. |
| CK — Forêt (masque forestier) | Activer « Masque forestier ». | Interroger `FORET/MASQUES_FORESTIERS` couche 0 (raster) via `identify` ; O si une classe autre que « NoData » est trouvée. |
| CL — Contrats de rénovation urbaine | Si BV = O : chercher sur Google « contrat de rénovation urbaine + nom de la commune », copier le lien trouvé. | Si BV = O : lire le lien dans `data/liens_communaux.csv` pour la commune ; sinon `/`. Si absent du fichier : « À COMPLÉTER MANUELLEMENT ». |
| CM — Zone de préemption | Rechercher « périmètre d'application d'un droit de préemption en aménagement du territoire », activer la couche. | Interroger `AMENAGEMENT_TERRITOIRE/PPR` couche 0 en intersection → O/N. |
| CN — Parcelle nécessitant des démarches (BDES) | Activer BDES, sous-catégorie « démarches de gestion des sols réalisées ou à prévoir ». | Interroger `SOL_SOUS_SOL/BDES_INVENTAIRE` couche 0 en intersection → O/N. |
| CO — Parcelle de nature indicative (BDES) | Idem CN, sous-catégorie « informations strictement indicatives ». | Idem CN, BDES_INVENTAIRE couche 1. |
| CP — Quartier à loyers majorés | Règle du document de référence : toujours `/`, ne pas chercher. | Valeur constante `/`. |
| CQ — Territoire du Canal | Idem CP. | Valeur constante `/`. |
| CR — Pôle de développement prioritaire | Idem CP. | Valeur constante `/`. |
| CS — Zone de revitalisation urbaine | Idem CP. | Valeur constante `/`. |
| CT — EDRLR | Idem CP. | Valeur constante `/`. |
| CU — Lien Guide régional d'urbanisme (ZPU) | Si AR = O : copier le lien de la page catalogue du Géoportail pour le GRU. | Si AR = O : écrire l'URL fixe du catalogue (toujours la même) ; sinon `/`. |
| CV — Lien Règlement général sur les bâtisses en site rural | Si AS = O : idem CU. | Si AS = O : même URL fixe de catalogue GRU ; sinon `/`. |
| CW — Lien Règlement d'urbanisme qualité acoustique aéroports | Si AT = O : idem CU. | Si AT = O : même URL fixe de catalogue GRU ; sinon `/`. |
| CX — Lien Guide communal d'urbanisme | Si AU = O : copier le lien de la page catalogue du GCU. | Si AU = O : écrire l'URL fixe du catalogue GCU ; sinon `/`. |
