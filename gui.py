"""Interface graphique (CustomTkinter) pour le remplissage automatique du
fichier Walonmap.

Réutilise directement `main.traiter_commune` et les services existants —
aucune logique métier n'est dupliquée ici, seulement la présentation et le
pilotage (thread d'arrière-plan, barre de progression, journal visuel).

Lancement : `python gui.py`
Empaquetage portable : voir `build.bat` (PyInstaller --onefile).
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog
from typing import Dict, Optional

# Doit s'exécuter AVANT tout import de code utilisant potentiellement
# stdout/stderr (ex: tqdm dans utils/progress.py, via main.py). Un
# exécutable PyInstaller construit avec --windowed n'a pas de console :
# sys.stdout/sys.stderr valent alors None, et tqdm (qui y écrit par
# défaut) plante avec "AttributeError: 'NoneType' object has no attribute
# 'write'". Problème connu des applications --windowed ; correction
# standard : rediriger vers un flux nul si absent.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import customtkinter as ctk

import config
from main import (
    RapportEchecs,
    ResultatTraitement,
    recalculer_cote_position,
    retraiter_echecs,
    supprimer_parcelles_hors_codes_postaux,
    traiter_commune,
)
from services.cache_service import CacheService, ProgressStore
from services.cadastre_service import CadastreService
from services.excel_service import ExcelService
from services.geoportail_service import ArcGISRestClient
from services.layers_service import LayersService
from services.sync_service import (
    PlanSynchronisation,
    RapportSynchronisation,
    appliquer_plan_synchronisation,
    calculer_synchronisation,
    reecrire_excel_depuis_base,
    synchroniser_depuis_excel,
)
from utils.licence import charger_mot_de_passe_local, verifier_et_enregistrer
from utils.logger import setup_logging
from utils.rate_limiter import RateLimiter

APP_TITLE = f"Walonmap — Remplissage automatique (v{config.APP_VERSION})"


class _QueueLogHandler(logging.Handler):
    """Pousse chaque log formaté dans une queue thread-safe, lue par l'UI.

    Le traitement tourne dans un thread d'arrière-plan (voir `App._lancer`) ;
    les widgets Tkinter ne doivent être touchés que depuis le thread
    principal, d'où ce pont par queue + `after()` (voir `App._poll_queue`).
    """

    def __init__(self, message_queue: "queue.Queue") -> None:
        super().__init__()
        self._queue = message_queue
        self.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self._queue.put(("log", self.format(record)))


def _parse_codes_postaux(texte: str) -> Optional[list]:
    """'4367, 4400' -> ['4367', '4400'] ; chaîne vide -> None (pas de filtre)."""
    import re
    codes = [c.strip() for c in re.split(r"[,;\s]+", texte.strip()) if c.strip()]
    return codes or None


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("760x680")
        self.minsize(660, 560)

        self._message_queue: "queue.Queue" = queue.Queue()
        self._en_cours = False
        self._output_path: Optional[Path] = None

        self._construire_interface()
        self._brancher_logging()
        self.after(100, self._poll_queue)

    # -- Construction de l'interface ---------------------------------------

    def _construire_interface(self) -> None:
        pad = {"padx": 16, "pady": (8, 0)}

        ctk.CTkLabel(
            self, text=APP_TITLE, font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(16, 4))
        ctk.CTkLabel(
            self,
            text="Remplit automatiquement le fichier Excel à partir du Géoportail de Wallonie.",
            text_color=("gray30", "gray70"),
        ).pack(anchor="w", padx=16, pady=(0, 12))

        form = ctk.CTkFrame(self)
        form.pack(fill="x", padx=16, pady=(0, 8))
        form.grid_columnconfigure(1, weight=1)

        row = 0
        ctk.CTkLabel(form, text="Pays").grid(row=row, column=0, sticky="w", padx=12, pady=8)
        self.entry_pays = ctk.CTkEntry(form, placeholder_text="Belgique")
        self.entry_pays.insert(0, "Belgique")
        self.entry_pays.grid(row=row, column=1, sticky="ew", padx=12, pady=8)

        row += 1
        ctk.CTkLabel(form, text="Commune *").grid(row=row, column=0, sticky="w", padx=12, pady=8)
        self.entry_commune = ctk.CTkEntry(form, placeholder_text="ex : Crisnée")
        self.entry_commune.grid(row=row, column=1, sticky="ew", padx=12, pady=8)

        row += 1
        ctk.CTkLabel(form, text="Code(s) postal(aux)").grid(row=row, column=0, sticky="w", padx=12, pady=8)
        self.entry_codes_postaux = ctk.CTkEntry(
            form, placeholder_text="ex : 4367, 4400 — laisser vide = tous les codes de la commune"
        )
        self.entry_codes_postaux.grid(row=row, column=1, sticky="ew", padx=12, pady=8)

        row += 1
        ctk.CTkLabel(form, text="Limite (parcelles)").grid(row=row, column=0, sticky="w", padx=12, pady=8)
        self.entry_limit = ctk.CTkEntry(
            form, placeholder_text="laisser vide = traiter tout ce qui reste"
        )
        self.entry_limit.grid(row=row, column=1, sticky="ew", padx=12, pady=8)

        row += 1
        ctk.CTkLabel(form, text="Vitesse (requêtes/s)").grid(row=row, column=0, sticky="w", padx=12, pady=8)
        frame_vitesse = ctk.CTkFrame(form, fg_color="transparent")
        frame_vitesse.grid(row=row, column=1, sticky="ew", padx=12, pady=8)
        frame_vitesse.grid_columnconfigure(0, weight=1)
        self.slider_vitesse = ctk.CTkSlider(
            frame_vitesse, from_=1, to=20, number_of_steps=19, command=self._maj_label_vitesse,
        )
        self.slider_vitesse.set(config.MAX_REQUESTS_PER_SECOND)
        self.slider_vitesse.grid(row=0, column=0, sticky="ew")
        self.label_vitesse = ctk.CTkLabel(frame_vitesse, text=f"{int(config.MAX_REQUESTS_PER_SECOND)} req/s", width=70)
        self.label_vitesse.grid(row=0, column=1, padx=(8, 0))

        row += 1
        ctk.CTkLabel(
            form, text="Plus rapide = plus de risques d'erreurs transitoires du serveur (déjà gérées par réessai).",
            text_color=("gray30", "gray70"), font=ctk.CTkFont(size=11),
        ).grid(row=row, column=1, sticky="w", padx=12, pady=(0, 4))

        row += 1
        ctk.CTkLabel(form, text="Fichier Excel gabarit *").grid(row=row, column=0, sticky="w", padx=12, pady=8)
        frame_in = ctk.CTkFrame(form, fg_color="transparent")
        frame_in.grid(row=row, column=1, sticky="ew", padx=12, pady=8)
        frame_in.grid_columnconfigure(0, weight=1)
        self.entry_fichier_entree = ctk.CTkEntry(frame_in, placeholder_text="Fichier Excel vide à compléter (.xlsx)")
        self.entry_fichier_entree.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(
            frame_in, text="Parcourir…", width=100, command=self._choisir_fichier_entree,
        ).grid(row=0, column=1, padx=(8, 0))

        row += 1
        ctk.CTkLabel(form, text="Fichier de sortie *").grid(row=row, column=0, sticky="w", padx=12, pady=8)
        frame_out = ctk.CTkFrame(form, fg_color="transparent")
        frame_out.grid(row=row, column=1, sticky="ew", padx=12, pady=8)
        frame_out.grid_columnconfigure(0, weight=1)
        self.entry_fichier_sortie = ctk.CTkEntry(frame_out, placeholder_text="Où enregistrer le résultat (.xlsx)")
        self.entry_fichier_sortie.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(
            frame_out, text="Enregistrer sous…", width=130, command=self._choisir_fichier_sortie,
        ).grid(row=0, column=1, padx=(8, 0))

        row += 1
        self.check_debug = ctk.CTkCheckBox(form, text="Mode DEBUG (journalisation détaillée)")
        self.check_debug.grid(row=row, column=1, sticky="w", padx=12, pady=(4, 12))

        # -- Action + progression -------------------------------------------
        action = ctk.CTkFrame(self, fg_color="transparent")
        action.pack(fill="x", **pad)
        self.bouton_lancer = ctk.CTkButton(
            action, text="Démarrer le traitement", height=40,
            font=ctk.CTkFont(size=14, weight="bold"), command=self._lancer,
        )
        self.bouton_lancer.pack(side="left")
        self.bouton_ouvrir_dossier = ctk.CTkButton(
            action, text="Ouvrir le dossier de sortie", height=40, state="disabled",
            command=self._ouvrir_dossier_sortie,
        )
        self.bouton_ouvrir_dossier.pack(side="left", padx=(8, 0))
        self.bouton_importer_corrige = ctk.CTkButton(
            action, text="Importer un Excel corrigé…", height=40,
            command=self._importer_excel_corrige,
        )
        self.bouton_importer_corrige.pack(side="left", padx=(8, 0))
        self.bouton_recalculer_cote_position = ctk.CTkButton(
            action, text="Recalculer côté/position et doublons…", height=40,
            command=self._recalculer_cote_position,
        )
        self.bouton_recalculer_cote_position.pack(side="left", padx=(8, 0))
        self.bouton_retraiter_echecs = ctk.CTkButton(
            action, text="Retraiter les parcelles échouées…", height=40,
            command=self._retraiter_echecs,
        )
        self.bouton_retraiter_echecs.pack(side="left", padx=(8, 0))

        # -- Action destructive, isolée sur sa propre ligne + couleur
        # d'avertissement pour ne jamais être cliquée par réflexe à côté
        # des boutons normaux ci-dessus (voir _supprimer_hors_code_postal :
        # double confirmation avant toute suppression réelle).
        danger = ctk.CTkFrame(self, fg_color="transparent")
        danger.pack(fill="x", padx=16, pady=(6, 0))
        self.bouton_supprimer_hors_code_postal = ctk.CTkButton(
            danger, text="Supprimer les lignes hors code(s) postal(aux)…", height=36,
            fg_color="#8B0000", hover_color="#B22222",
            command=self._supprimer_hors_code_postal,
        )
        self.bouton_supprimer_hors_code_postal.pack(side="left")
        ctk.CTkLabel(
            danger,
            text="Suppression définitive — nécessite le champ Code(s) postal(aux) rempli (les codes à CONSERVER).",
            text_color=("gray30", "gray70"), font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=(8, 0))

        self.label_statut = ctk.CTkLabel(self, text="Prêt.", anchor="w")
        self.label_statut.pack(fill="x", padx=16, pady=(10, 2))
        self.barre_progression = ctk.CTkProgressBar(self)
        self.barre_progression.set(0)
        self.barre_progression.pack(fill="x", padx=16, pady=(0, 10))

        ctk.CTkLabel(self, text="Journal", text_color=("gray30", "gray70")).pack(anchor="w", padx=16)
        self.zone_log = ctk.CTkTextbox(self, wrap="word", font=ctk.CTkFont(family="Consolas", size=11))
        self.zone_log.pack(fill="both", expand=True, padx=16, pady=(2, 16))
        self.zone_log.configure(state="disabled")

    def _maj_label_vitesse(self, valeur: float) -> None:
        self.label_vitesse.configure(text=f"{int(round(valeur))} req/s")

    # -- Sélection de fichiers -----------------------------------------------

    def _choisir_fichier_entree(self) -> None:
        chemin = filedialog.askopenfilename(
            title="Choisir le fichier Excel gabarit (vide)",
            filetypes=[("Fichiers Excel", "*.xlsx"), ("Tous les fichiers", "*.*")],
        )
        if chemin:
            self.entry_fichier_entree.delete(0, "end")
            self.entry_fichier_entree.insert(0, chemin)

    def _choisir_fichier_sortie(self) -> None:
        commune = self.entry_commune.get().strip() or "sortie"
        chemin = filedialog.asksaveasfilename(
            title="Choisir où enregistrer le fichier de sortie",
            defaultextension=".xlsx",
            initialfile=f"{commune}.xlsx",
            filetypes=[("Fichiers Excel", "*.xlsx"), ("Tous les fichiers", "*.*")],
        )
        if chemin:
            self.entry_fichier_sortie.delete(0, "end")
            self.entry_fichier_sortie.insert(0, chemin)

    def _ouvrir_dossier_sortie(self) -> None:
        if self._output_path and self._output_path.exists():
            os.startfile(self._output_path.parent)  # Windows uniquement

    # -- Journalisation --------------------------------------------------

    def _brancher_logging(self) -> None:
        setup_logging(config.LOGS_DIR, debug=False)
        handler = _QueueLogHandler(self._message_queue)
        logging.getLogger().addHandler(handler)

    def _poll_queue(self) -> None:
        try:
            while True:
                message = self._message_queue.get_nowait()
                self._traiter_message(message)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _traiter_message(self, message: tuple) -> None:
        kind = message[0]
        if kind == "log":
            self._ajouter_log(message[1])
        elif kind == "progress":
            _, phase, actuel, total = message
            self._maj_progression(phase, actuel, total)
        elif kind == "done":
            self._fin_traitement(succes=True, resultat=message[1])
        elif kind == "sync_done":
            self._fin_synchronisation(rapport=message[1], chemin=message[2])
        elif kind == "sync_done_verrouille":
            self._fin_synchronisation(rapport=message[1], chemin=message[2], fichier_verrouille=True)
        elif kind == "sync_preview":
            self._gerer_apercu_synchronisation_miroir(*message[1:])
        elif kind == "recalcul_done":
            self._fin_recalcul(n=message[1], chemin=message[2])
        elif kind == "retraitement_echecs_done":
            self._fin_retraitement_echecs(rapport=message[1], chemin=message[2])
        elif kind == "error":
            self._fin_traitement(succes=False, erreur=message[1])

    def _ajouter_log(self, ligne: str) -> None:
        self.zone_log.configure(state="normal")
        self.zone_log.insert("end", ligne + "\n")
        self.zone_log.see("end")
        self.zone_log.configure(state="disabled")

    def _maj_progression(self, phase: str, actuel: int, total: int) -> None:
        ratio = (actuel / total) if total else 0
        self.barre_progression.set(ratio)
        libelles = {
            "découverte": "Découverte des adresses",
            "redecouverte_rayon_elargi": "Redécouverte (rayon élargi)",
            "recollement_troncons": "Recollement des tronçons",
            "recalcul_côté_position": "Recalcul côté/position",
            "retraitement_echecs": "Retraitement des parcelles échouées",
            "nettoyage_adresse_ailleurs": "Nettoyage des doublons (adresse ailleurs)",
            "nettoyage_rue_plus_proche": "Nettoyage des doublons (rue la plus proche)",
        }
        libelle = libelles.get(phase, "Remplissage des colonnes")
        self.label_statut.configure(text=f"{libelle}… ({actuel}/{total})")

    # -- Validation et lancement ---------------------------------------------

    def _valider_formulaire(self) -> Optional[dict]:
        commune = self.entry_commune.get().strip()
        fichier_entree = self.entry_fichier_entree.get().strip()
        fichier_sortie = self.entry_fichier_sortie.get().strip()

        if not commune:
            messagebox.showerror(APP_TITLE, "Le champ Commune est obligatoire.")
            return None
        if not fichier_entree or not Path(fichier_entree).is_file():
            messagebox.showerror(APP_TITLE, "Choisissez un fichier Excel gabarit valide.")
            return None
        if not fichier_sortie:
            messagebox.showerror(APP_TITLE, "Choisissez où enregistrer le fichier de sortie.")
            return None

        limite_texte = self.entry_limit.get().strip()
        limite: Optional[int] = None
        if limite_texte:
            if not limite_texte.isdigit() or int(limite_texte) <= 0:
                messagebox.showerror(APP_TITLE, "La limite doit être un nombre entier positif (ou vide).")
                return None
            limite = int(limite_texte)

        return {
            "pays": self.entry_pays.get().strip() or "Belgique",
            "commune": commune,
            "codes_postaux": _parse_codes_postaux(self.entry_codes_postaux.get()),
            "limit": limite,
            "rate_limit": self.slider_vitesse.get(),
            "fichier_entree": Path(fichier_entree),
            "fichier_sortie": Path(fichier_sortie),
            "debug": bool(self.check_debug.get()),
        }

    def _verifier_licence(self, commune: str) -> bool:
        """Vérifie l'accès en ligne pour `commune` avant tout traitement
        (voir utils/licence.py). Réutilise silencieusement le mot de passe
        déjà sauvegardé localement s'il est encore valide ; sinon (première
        utilisation, ou accès révoqué/mot de passe changé) le redemande via
        une boîte de dialogue, jusqu'à annulation ou succès."""
        mot_de_passe = charger_mot_de_passe_local()
        if mot_de_passe:
            ok, message = verifier_et_enregistrer(commune, mot_de_passe)
            if ok:
                return True
        while True:
            mot_de_passe = simpledialog.askstring(
                APP_TITLE, f"Mot de passe d'accès pour la commune « {commune} » :",
                show="*", parent=self,
            )
            if mot_de_passe is None:
                return False
            ok, message = verifier_et_enregistrer(commune, mot_de_passe)
            if ok:
                return True
            messagebox.showerror(APP_TITLE, message or "Mot de passe incorrect.")

    def _lancer(self) -> None:
        if self._en_cours:
            return
        parametres = self._valider_formulaire()
        if parametres is None:
            return
        if not self._verifier_licence(parametres["commune"]):
            return

        self._en_cours = True
        self.bouton_lancer.configure(state="disabled", text="Traitement en cours…")
        self.bouton_importer_corrige.configure(state="disabled")
        self.bouton_recalculer_cote_position.configure(state="disabled")
        self.bouton_retraiter_echecs.configure(state="disabled")
        self.bouton_supprimer_hors_code_postal.configure(state="disabled")
        self.bouton_ouvrir_dossier.configure(state="disabled")
        self.barre_progression.set(0)
        self.label_statut.configure(text="Démarrage…")
        self.zone_log.configure(state="normal")
        self.zone_log.delete("1.0", "end")
        self.zone_log.configure(state="disabled")

        logging.getLogger().setLevel(logging.DEBUG if parametres["debug"] else logging.INFO)

        thread = threading.Thread(target=self._executer, args=(parametres,), daemon=True)
        thread.start()

    def _executer(self, parametres: dict) -> None:
        """Tourne dans un thread d'arrière-plan : aucun accès direct aux
        widgets ici, uniquement via `self._message_queue` (voir `_poll_queue`,
        exécuté lui sur le thread principal)."""
        try:
            cache_service = CacheService(config.CACHE_DIR)
            progress_store = ProgressStore(config.CACHE_DIR)
            rate_limiter = RateLimiter(parametres["rate_limit"])
            client = ArcGISRestClient(
                cache=cache_service, rate_limiter=rate_limiter, timeout=config.HTTP_TIMEOUT_SECONDS,
            )
            cadastre_service = CadastreService(client, progress_store)
            layers_service = LayersService(client, config.LIENS_COMMUNAUX_PATH)
            excel_service = ExcelService(parametres["fichier_entree"])

            def on_progress(phase: str, actuel: int, total: int) -> None:
                self._message_queue.put(("progress", phase, actuel, total))

            resultat = traiter_commune(
                parametres["commune"], parametres["pays"],
                cadastre_service, layers_service, progress_store, excel_service,
                limit=parametres["limit"], codes_postaux=parametres["codes_postaux"],
                output_path=parametres["fichier_sortie"], on_progress=on_progress,
            )
            self._message_queue.put(("done", resultat))
        except Exception as exc:  # noqa: BLE001 — remonté proprement à l'UI
            logging.getLogger("gui").exception("Échec du traitement")
            self._message_queue.put(("error", str(exc)))

    # -- Import d'un Excel corrigé (reprise des cellules ERREUR/vides) -------

    def _demander_mode_synchronisation(self) -> Optional[bool]:
        """Popup de confirmation avec case à cocher, affiché avant chaque
        import — même principe que `_demander_forcer_redecouverte` : demande
        si la synchronisation doit être "complète" (miroir) plutôt que le
        mode par défaut.

        Mode par défaut (décoché) : seules les cellules ERREUR/vides sont
        reprises depuis l'Excel, aucune ligne n'est jamais supprimée — voir
        `services/sync_service.py`.

        Mode miroir (coché) : reprend TOUTE cellule différente (même si la
        base avait déjà une vraie valeur), et supprime toute ligne de la
        commune absente de l'Excel importé — demandé par des collaborateurs
        qui corrigent une cellule déjà remplie ou suppriment une ligne
        directement dans l'Excel. Un aperçu chiffré et une confirmation par
        saisie du nom de la commune suivent avant toute écriture réelle
        (voir `_gerer_apercu_synchronisation_miroir`) — rien n'est modifié
        rien qu'en cochant cette case.

        Renvoie `True`/`False` selon la case cochée si l'utilisateur
        confirme, `None` s'il annule (l'import ne doit alors pas
        démarrer)."""
        resultat: Dict[str, Optional[bool]] = {"valeur": None}

        fenetre = ctk.CTkToplevel(self)
        fenetre.title(APP_TITLE)
        fenetre.geometry("540x300")
        fenetre.resizable(False, False)
        fenetre.transient(self)
        fenetre.grab_set()

        ctk.CTkLabel(
            fenetre, text="Options de la synchronisation", font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(anchor="w", padx=20, pady=(20, 8))

        ctk.CTkLabel(
            fenetre, wraplength=490, justify="left",
            text=(
                "Par défaut, seules les cellules en erreur ou vides sont reprises depuis "
                "l'Excel importé — une cellule qui avait déjà une vraie valeur n'est jamais "
                "écrasée, et aucune ligne n'est jamais supprimée.\n\n"
                "Cocher l'option ci-dessous active une synchronisation COMPLÈTE (miroir) : "
                "toute cellule différente est reprise depuis l'Excel (même si elle avait déjà "
                "une valeur), et toute ligne de la commune absente de l'Excel importé est "
                "supprimée définitivement de la base."
            ),
        ).pack(anchor="w", padx=20, pady=(0, 8))

        ctk.CTkLabel(
            fenetre, wraplength=490, justify="left", text_color=("darkorange3", "orange"),
            text=(
                "⚠ Un aperçu chiffré (lignes ajoutées/modifiées/supprimées) et une "
                "confirmation par saisie du nom de la commune seront demandés avant toute "
                "modification réelle — rien n'est appliqué en cochant simplement cette case."
            ),
        ).pack(anchor="w", padx=20, pady=(0, 12))

        case_miroir = ctk.CTkCheckBox(fenetre, text="Synchronisation complète (miroir)")
        case_miroir.pack(anchor="w", padx=20, pady=(0, 16))

        def _valider() -> None:
            resultat["valeur"] = bool(case_miroir.get())
            fenetre.destroy()

        def _annuler() -> None:
            resultat["valeur"] = None
            fenetre.destroy()

        boutons = ctk.CTkFrame(fenetre, fg_color="transparent")
        boutons.pack(fill="x", padx=20, pady=(0, 20))
        ctk.CTkButton(boutons, text="Annuler", fg_color="gray40", command=_annuler).pack(side="right")
        ctk.CTkButton(boutons, text="Continuer", command=_valider).pack(side="right", padx=(0, 8))

        fenetre.protocol("WM_DELETE_WINDOW", _annuler)
        self.wait_window(fenetre)
        return resultat["valeur"]

    def _importer_excel_corrige(self) -> None:
        if self._en_cours:
            return
        commune = self.entry_commune.get().strip()
        pays = self.entry_pays.get().strip() or "Belgique"
        if not commune:
            messagebox.showerror(
                APP_TITLE, "Le champ Commune est obligatoire pour importer un Excel corrigé "
                           "(il indique dans quelle progression chercher les corrections).",
            )
            return
        if not self._verifier_licence(commune):
            return
        chemin = filedialog.askopenfilename(
            title="Choisir l'Excel corrigé à importer",
            filetypes=[("Fichiers Excel", "*.xlsx"), ("Tous les fichiers", "*.*")],
        )
        if not chemin:
            return

        mode_miroir = self._demander_mode_synchronisation()
        if mode_miroir is None:
            return

        self._en_cours = True
        self.bouton_lancer.configure(state="disabled")
        self.bouton_importer_corrige.configure(state="disabled", text="Importation en cours…")
        self.bouton_recalculer_cote_position.configure(state="disabled")
        self.bouton_retraiter_echecs.configure(state="disabled")
        self.bouton_supprimer_hors_code_postal.configure(state="disabled")
        self.bouton_ouvrir_dossier.configure(state="disabled")
        self.barre_progression.set(0)
        self.label_statut.configure(
            text="Calcul de la synchronisation miroir en cours…" if mode_miroir
            else "Importation et synchronisation en cours…",
        )
        self.zone_log.configure(state="normal")
        self.zone_log.delete("1.0", "end")
        self.zone_log.configure(state="disabled")

        if mode_miroir:
            thread = threading.Thread(
                target=self._executer_calcul_synchronisation, args=(commune, pays, Path(chemin)), daemon=True,
            )
        else:
            thread = threading.Thread(
                target=self._executer_synchronisation, args=(commune, pays, Path(chemin)), daemon=True,
            )
        thread.start()

    def _executer_synchronisation(self, commune: str, pays: str, chemin: Path) -> None:
        """Tourne dans un thread d'arrière-plan, comme `_executer` : voir
        `services/sync_service.py` pour la logique de correspondance et de
        correction elle-même (aucune duplication ici). `cadastre_service`
        sert à vérifier au registre ICAR l'existence de toute ligne de
        l'Excel absente de la base avant de l'y ajouter (voir
        sync_service.py) — jamais de donnée inventée à partir du seul
        texte de l'Excel. Mode par défaut uniquement (case "synchronisation
        complète" décochée) — voir `_executer_calcul_synchronisation` pour
        le mode miroir, qui nécessite un aperçu avant application."""
        try:
            cache_service = CacheService(config.CACHE_DIR)
            progress_store = ProgressStore(config.CACHE_DIR)
            client = ArcGISRestClient(
                cache=cache_service, rate_limiter=RateLimiter(config.MAX_REQUESTS_PER_SECOND),
                timeout=config.HTTP_TIMEOUT_SECONDS,
            )
            cadastre_service = CadastreService(client, progress_store)
            excel_service = ExcelService(chemin)
            wb = excel_service.load_output_workbook()
            ws = excel_service.get_active_sheet(wb)

            # Les corrections/ajouts sont déjà appliqués en base à ce stade
            # — si la réécriture du fichier échoue ensuite (fichier encore
            # ouvert dans Excel), rien n'est perdu, seul l'aperçu immédiat
            # manque.
            rapport = synchroniser_depuis_excel(commune, pays, ws, excel_service, progress_store, cadastre_service)

            try:
                reecrire_excel_depuis_base(commune, excel_service, progress_store, chemin)
            except PermissionError:
                self._message_queue.put(("sync_done_verrouille", rapport, chemin))
                return

            self._message_queue.put(("sync_done", rapport, chemin))
        except Exception as exc:  # noqa: BLE001 — remonté proprement à l'UI
            logging.getLogger("gui").exception("Échec de l'importation/synchronisation")
            self._message_queue.put(("error", str(exc)))

    def _executer_calcul_synchronisation(self, commune: str, pays: str, chemin: Path) -> None:
        """Tourne dans un thread d'arrière-plan : calcule une
        synchronisation en mode miroir SANS rien écrire en base (voir
        `sync_service.calculer_synchronisation`) — nécessaire pour afficher
        un aperçu chiffré avant confirmation (mode miroir uniquement,
        potentiellement destructeur). Le plan calculé revient via la queue
        pour être présenté sur le fil principal (voir
        `_gerer_apercu_synchronisation_miroir`) ; l'application réelle
        n'a lieu qu'après confirmation explicite."""
        try:
            cache_service = CacheService(config.CACHE_DIR)
            progress_store = ProgressStore(config.CACHE_DIR)
            client = ArcGISRestClient(
                cache=cache_service, rate_limiter=RateLimiter(config.MAX_REQUESTS_PER_SECOND),
                timeout=config.HTTP_TIMEOUT_SECONDS,
            )
            cadastre_service = CadastreService(client, progress_store)
            excel_service = ExcelService(chemin)
            wb = excel_service.load_output_workbook()
            ws = excel_service.get_active_sheet(wb)

            plan = calculer_synchronisation(
                commune, pays, ws, excel_service, progress_store, cadastre_service, mode_miroir=True,
            )
            self._message_queue.put(("sync_preview", plan, progress_store, excel_service, chemin))
        except Exception as exc:  # noqa: BLE001 — remonté proprement à l'UI
            logging.getLogger("gui").exception("Échec du calcul de la synchronisation miroir")
            self._message_queue.put(("error", str(exc)))

    def _gerer_apercu_synchronisation_miroir(
        self, plan: PlanSynchronisation, progress_store: ProgressStore,
        excel_service: ExcelService, chemin: Path,
    ) -> None:
        """Tourne sur le fil principal (appelé depuis `_traiter_message`) :
        affiche l'aperçu chiffré d'un `PlanSynchronisation` calculé en mode
        miroir, puis une confirmation forte (taper le nom de la commune —
        même principe que `_supprimer_hors_code_postal`, vu le caractère
        potentiellement destructeur) avant de lancer l'application réelle
        dans un nouveau thread d'arrière-plan. Réactive les boutons sans
        rien modifier si l'utilisateur annule à n'importe quelle étape."""
        rapport = plan.rapport
        confirme = messagebox.askyesno(
            APP_TITLE,
            f"Aperçu de la synchronisation miroir pour « {plan.commune} » — RIEN n'est encore "
            f"appliqué :\n\n"
            f"{rapport.lignes_ajoutees} ligne(s) seront ajoutée(s)\n"
            f"{rapport.lignes_modifiees} ligne(s) seront modifiée(s) ({rapport.cellules_corrigees} "
            f"cellule(s) au total)\n"
            f"{rapport.lignes_supprimees} ligne(s) seront SUPPRIMÉES DÉFINITIVEMENT\n\n"
            f"Continuer ?",
            icon="warning",
        )
        if not confirme:
            self._reactiver_boutons()
            self.label_statut.configure(text="Synchronisation miroir annulée — rien n'a été modifié.")
            return

        saisie = simpledialog.askstring(
            APP_TITLE,
            f"Pour confirmer, tape exactement le nom de la commune :\n{plan.commune}",
            parent=self,
        )
        if saisie != plan.commune:
            messagebox.showinfo(APP_TITLE, "Confirmation incorrecte ou annulée — rien n'a été modifié.")
            self._reactiver_boutons()
            self.label_statut.configure(text="Synchronisation miroir annulée — rien n'a été modifié.")
            return

        self.label_statut.configure(text="Application de la synchronisation miroir en cours…")
        thread = threading.Thread(
            target=self._executer_application_synchronisation,
            args=(plan, progress_store, excel_service, chemin),
            daemon=True,
        )
        thread.start()

    def _executer_application_synchronisation(
        self, plan: PlanSynchronisation, progress_store: ProgressStore,
        excel_service: ExcelService, chemin: Path,
    ) -> None:
        """Tourne dans un thread d'arrière-plan : écrit réellement en base
        le `PlanSynchronisation` déjà calculé et confirmé (voir
        `sync_service.appliquer_plan_synchronisation`) — aucun appel réseau
        ici, tout a déjà été décidé lors du calcul."""
        try:
            rapport = appliquer_plan_synchronisation(plan, progress_store)
            try:
                reecrire_excel_depuis_base(plan.commune, excel_service, progress_store, chemin)
            except PermissionError:
                self._message_queue.put(("sync_done_verrouille", rapport, chemin))
                return
            self._message_queue.put(("sync_done", rapport, chemin))
        except Exception as exc:  # noqa: BLE001 — remonté proprement à l'UI
            logging.getLogger("gui").exception("Échec de l'application de la synchronisation miroir")
            self._message_queue.put(("error", str(exc)))

    # -- Recalcul côté/position (rattrapage pour les parcelles déjà résolues) -

    def _demander_forcer_redecouverte(self) -> Optional[bool]:
        """Popup de confirmation avec case à cocher, affiché avant chaque
        recalcul : demande si la redécouverte au rayon élargi doit être
        refaite pour TOUTES les rues déjà vérifiées, y compris celles déjà
        marquées comme redécouvertes lors d'un appel précédent (voir
        main.py::recalculer_cote_position, paramètre `forcer_redecouverte`).

        Nécessaire depuis qu'une rue une fois marquée n'est plus jamais
        retentée — cas réel (Rue Baudouin 1er, Courcelles) : une rue
        redécouverte avant l'élargissement du rayon à 200m ne retrouvait
        plus jamais de nouvelles parcelles, même après mise à jour de
        l'application, sans que rien ne le signale à l'utilisateur.

        Renvoie `True`/`False` selon la case cochée si l'utilisateur
        confirme, `None` s'il annule (le recalcul ne doit alors pas
        démarrer)."""
        resultat: Dict[str, Optional[bool]] = {"valeur": None}

        fenetre = ctk.CTkToplevel(self)
        fenetre.title(APP_TITLE)
        fenetre.geometry("520x300")
        fenetre.resizable(False, False)
        fenetre.transient(self)
        fenetre.grab_set()

        ctk.CTkLabel(
            fenetre, text="Options du recalcul", font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(anchor="w", padx=20, pady=(20, 8))

        ctk.CTkLabel(
            fenetre, wraplength=470, justify="left",
            text=(
                "Par défaut, une rue déjà redécouverte au rayon élargi lors d'un "
                "recalcul précédent n'est plus jamais retentée — même après une "
                "mise à jour de l'application qui changerait la recherche (rayon, "
                "correctif de tracé...).\n\n"
                "Cocher l'option ci-dessous force une nouvelle redécouverte pour "
                "TOUTES les rues déjà vérifiées, pas seulement les nouvelles. "
                "Aucune ligne déjà trouvée n'est supprimée — seulement complétée "
                "si de nouvelles parcelles apparaissent."
            ),
        ).pack(anchor="w", padx=20, pady=(0, 8))

        ctk.CTkLabel(
            fenetre, wraplength=470, justify="left", text_color=("darkorange3", "orange"),
            text="⚠ Nettement plus long qu'un recalcul normal — à cocher seulement en cas de doute concret.",
        ).pack(anchor="w", padx=20, pady=(0, 12))

        case_forcer = ctk.CTkCheckBox(fenetre, text="Redécouvrir toutes les rues déjà vérifiées")
        case_forcer.pack(anchor="w", padx=20, pady=(0, 16))

        def _valider() -> None:
            resultat["valeur"] = bool(case_forcer.get())
            fenetre.destroy()

        def _annuler() -> None:
            resultat["valeur"] = None
            fenetre.destroy()

        boutons = ctk.CTkFrame(fenetre, fg_color="transparent")
        boutons.pack(fill="x", padx=20, pady=(0, 20))
        ctk.CTkButton(boutons, text="Annuler", fg_color="gray40", command=_annuler).pack(side="right")
        ctk.CTkButton(boutons, text="Lancer le recalcul", command=_valider).pack(side="right", padx=(0, 8))

        fenetre.protocol("WM_DELETE_WINDOW", _annuler)
        self.wait_window(fenetre)
        return resultat["valeur"]

    def _recalculer_cote_position(self) -> None:
        """Rattrapage géométrique à lancer une fois par commune — voir
        main.py::recalculer_cote_position pour le détail des corrections
        appliquées en une passe (côté/position manquants, doublons "sans
        adresse" ayant une adresse ailleurs, doublons "sans adresse"
        candidats sur plusieurs rues à la fois, redécouverte au rayon
        élargi). Les parcelles traitées via « Démarrer le traitement »
        depuis l'ajout de ces vérifications n'ont déjà plus ces problèmes,
        inutile de relancer ceci après.

        Réutilise le champ Code(s) postal(aux) du formulaire principal :
        vide = toute la commune, sinon restreint le rattrapage à ces codes
        postaux — à renseigner pour une commune fusionnée dont on ne gère
        qu'un sous-ensemble des codes postaux, sans quoi le rattrapage peut
        toucher des rues d'un code postal dont on n'est pas responsable."""
        if self._en_cours:
            return
        commune = self.entry_commune.get().strip()
        fichier_entree = self.entry_fichier_entree.get().strip()
        fichier_sortie = self.entry_fichier_sortie.get().strip()
        if not commune:
            messagebox.showerror(APP_TITLE, "Le champ Commune est obligatoire pour le recalcul côté/position.")
            return
        if not fichier_entree or not Path(fichier_entree).is_file():
            messagebox.showerror(
                APP_TITLE, "Choisissez un fichier Excel gabarit valide (sert à réécrire le fichier de sortie).",
            )
            return
        if not fichier_sortie:
            messagebox.showerror(APP_TITLE, "Choisissez où enregistrer le fichier de sortie.")
            return
        if not self._verifier_licence(commune):
            return

        forcer_redecouverte = self._demander_forcer_redecouverte()
        if forcer_redecouverte is None:
            return

        self._en_cours = True
        self.bouton_lancer.configure(state="disabled")
        self.bouton_importer_corrige.configure(state="disabled")
        self.bouton_recalculer_cote_position.configure(state="disabled", text="Recalcul en cours…")
        self.bouton_retraiter_echecs.configure(state="disabled")
        self.bouton_supprimer_hors_code_postal.configure(state="disabled")
        self.bouton_ouvrir_dossier.configure(state="disabled")
        self.barre_progression.set(0)
        self.label_statut.configure(text="Recalcul côté/position en cours…")
        self.zone_log.configure(state="normal")
        self.zone_log.delete("1.0", "end")
        self.zone_log.configure(state="disabled")

        thread = threading.Thread(
            target=self._executer_recalcul_cote_position,
            args=(
                commune, Path(fichier_entree), Path(fichier_sortie), self.slider_vitesse.get(),
                _parse_codes_postaux(self.entry_codes_postaux.get()), forcer_redecouverte,
            ),
            daemon=True,
        )
        thread.start()

    def _executer_recalcul_cote_position(
        self, commune: str, fichier_entree: Path, fichier_sortie: Path, rate_limit: float,
        codes_postaux: Optional[list] = None, forcer_redecouverte: bool = False,
    ) -> None:
        """Tourne dans un thread d'arrière-plan, comme `_executer` : voir
        main.py::recalculer_cote_position pour la logique elle-même
        (aucune duplication ici). `codes_postaux` (même champ que le
        traitement normal) restreint le rattrapage à ces codes postaux —
        indispensable pour une commune fusionnée dont on ne gère qu'un
        sous-ensemble des codes postaux (ex: Comines-Warneton), sans quoi
        le rattrapage touche toute la commune (incident réel constaté).
        `forcer_redecouverte` vient de la case à cocher du popup affiché par
        `_recalculer_cote_position` (voir `_demander_forcer_redecouverte`)."""
        try:
            cache_service = CacheService(config.CACHE_DIR)
            progress_store = ProgressStore(config.CACHE_DIR)
            client = ArcGISRestClient(
                cache=cache_service, rate_limiter=RateLimiter(rate_limit), timeout=config.HTTP_TIMEOUT_SECONDS,
            )
            cadastre_service = CadastreService(client, progress_store)
            excel_service = ExcelService(fichier_entree)

            def on_progress(phase: str, actuel: int, total: int) -> None:
                self._message_queue.put(("progress", phase, actuel, total))

            n = recalculer_cote_position(
                commune, cadastre_service, progress_store, excel_service,
                output_path=fichier_sortie, codes_postaux=codes_postaux, on_progress=on_progress,
                forcer_redecouverte=forcer_redecouverte,
            )
            self._message_queue.put(("recalcul_done", n, fichier_sortie))
        except Exception as exc:  # noqa: BLE001 — remonté proprement à l'UI
            logging.getLogger("gui").exception("Échec du recalcul côté/position")
            self._message_queue.put(("error", str(exc)))

    # -- Retraitement ciblé des parcelles échouées ---------------------------

    def _retraiter_echecs(self) -> None:
        """Retente UNIQUEMENT les parcelles enregistrées en échec (voir
        main.py::retraiter_echecs) — à la différence de « Démarrer le
        traitement », qui les rattrape aussi mais mélangées avec le reste
        de la commune pas encore traitée. Peut être cliqué plusieurs fois
        de suite jusqu'à ce que le rapport affiche 0 échec restant."""
        if self._en_cours:
            return
        commune = self.entry_commune.get().strip()
        fichier_entree = self.entry_fichier_entree.get().strip()
        fichier_sortie = self.entry_fichier_sortie.get().strip()
        if not commune:
            messagebox.showerror(APP_TITLE, "Le champ Commune est obligatoire pour retraiter les échecs.")
            return
        if not fichier_entree or not Path(fichier_entree).is_file():
            messagebox.showerror(
                APP_TITLE, "Choisissez un fichier Excel gabarit valide (sert à réécrire le fichier de sortie).",
            )
            return
        if not fichier_sortie:
            messagebox.showerror(APP_TITLE, "Choisissez où enregistrer le fichier de sortie.")
            return
        if not self._verifier_licence(commune):
            return

        self._en_cours = True
        self.bouton_lancer.configure(state="disabled")
        self.bouton_importer_corrige.configure(state="disabled")
        self.bouton_recalculer_cote_position.configure(state="disabled")
        self.bouton_retraiter_echecs.configure(state="disabled", text="Retraitement en cours…")
        self.bouton_supprimer_hors_code_postal.configure(state="disabled")
        self.bouton_ouvrir_dossier.configure(state="disabled")
        self.barre_progression.set(0)
        self.label_statut.configure(text="Retraitement des parcelles échouées en cours…")
        self.zone_log.configure(state="normal")
        self.zone_log.delete("1.0", "end")
        self.zone_log.configure(state="disabled")

        thread = threading.Thread(
            target=self._executer_retraiter_echecs,
            args=(commune, Path(fichier_entree), Path(fichier_sortie), self.slider_vitesse.get()),
            daemon=True,
        )
        thread.start()

    def _executer_retraiter_echecs(
        self, commune: str, fichier_entree: Path, fichier_sortie: Path, rate_limit: float,
    ) -> None:
        """Tourne dans un thread d'arrière-plan, comme `_executer` : voir
        main.py::retraiter_echecs pour la logique elle-même (aucune
        duplication ici)."""
        try:
            cache_service = CacheService(config.CACHE_DIR)
            progress_store = ProgressStore(config.CACHE_DIR)
            client = ArcGISRestClient(
                cache=cache_service, rate_limiter=RateLimiter(rate_limit), timeout=config.HTTP_TIMEOUT_SECONDS,
            )
            cadastre_service = CadastreService(client, progress_store)
            layers_service = LayersService(client, config.LIENS_COMMUNAUX_PATH)
            excel_service = ExcelService(fichier_entree)

            def on_progress(phase: str, actuel: int, total: int) -> None:
                self._message_queue.put(("progress", phase, actuel, total))

            rapport = retraiter_echecs(
                commune, cadastre_service, layers_service, progress_store, excel_service,
                output_path=fichier_sortie, on_progress=on_progress,
            )
            self._message_queue.put(("retraitement_echecs_done", rapport, fichier_sortie))
        except Exception as exc:  # noqa: BLE001 — remonté proprement à l'UI
            logging.getLogger("gui").exception("Échec du retraitement des parcelles échouées")
            self._message_queue.put(("error", str(exc)))

    # -- Suppression définitive des lignes hors code(s) postal(aux) ---------

    def _supprimer_hors_code_postal(self) -> None:
        """Nettoyage définitif pour une commune touchée par un rattrapage
        lancé sans filtre de code postal avant que `recalculer_cote_position`
        le respecte (voir main.py::supprimer_parcelles_hors_codes_postaux —
        incident réel : Courcelles, collaboratrice en charge du seul 6180,
        lignes jusqu'à 6183 remontées après coup).

        Purement local (aucun appel réseau : uniquement la base de
        progression déjà en cache) donc exécuté directement sur le fil
        principal, sans thread d'arrière-plan — mais protégé par une double
        confirmation vu le caractère irréversible : un aperçu du nombre de
        lignes concernées, puis la saisie exacte du nom de la commune avant
        toute suppression réelle. Le champ Code(s) postal(aux) est ici
        obligatoire (contrairement aux autres boutons) et se lit comme
        « codes à CONSERVER » — le laisser vide supprimerait toute la
        commune, jamais autorisé."""
        if self._en_cours:
            return
        commune = self.entry_commune.get().strip()
        codes_postaux = _parse_codes_postaux(self.entry_codes_postaux.get())
        fichier_entree = self.entry_fichier_entree.get().strip()
        fichier_sortie = self.entry_fichier_sortie.get().strip()
        if not commune:
            messagebox.showerror(APP_TITLE, "Le champ Commune est obligatoire.")
            return
        if not codes_postaux:
            messagebox.showerror(
                APP_TITLE,
                "Le champ Code(s) postal(aux) est OBLIGATOIRE pour cette action — indique "
                "le ou les codes postaux à CONSERVER, tout le reste sera supprimé "
                "définitivement. Le laisser vide supprimerait toute la commune.",
            )
            return
        if not fichier_entree or not Path(fichier_entree).is_file():
            messagebox.showerror(
                APP_TITLE, "Choisissez un fichier Excel gabarit valide (sert à réécrire le fichier de sortie).",
            )
            return
        if not fichier_sortie:
            messagebox.showerror(APP_TITLE, "Choisissez où enregistrer le fichier de sortie.")
            return
        if not self._verifier_licence(commune):
            return

        progress_store = ProgressStore(config.CACHE_DIR)
        identifiants = progress_store.identifiants_hors_codes_postaux(commune, codes_postaux)
        if not identifiants:
            messagebox.showinfo(
                APP_TITLE,
                f"Aucune ligne à supprimer — toutes les parcelles déjà résolues de "
                f"« {commune} » ont déjà un code postal dans {codes_postaux}.",
            )
            return

        if not messagebox.askyesno(
            APP_TITLE,
            f"ATTENTION — action irréversible.\n\n"
            f"{len(identifiants)} ligne(s) de « {commune} » ont un code postal HORS de "
            f"{codes_postaux} et seront SUPPRIMÉES DÉFINITIVEMENT de la base de progression.\n\n"
            f"Continuer ?",
            icon="warning",
        ):
            return

        confirmation = simpledialog.askstring(
            APP_TITLE,
            f"Pour confirmer la suppression définitive de {len(identifiants)} ligne(s), "
            f"tape exactement le nom de la commune :\n{commune}",
            parent=self,
        )
        if confirmation != commune:
            messagebox.showinfo(APP_TITLE, "Confirmation incorrecte ou annulée — rien n'a été supprimé.")
            return

        self._en_cours = True
        self.bouton_lancer.configure(state="disabled")
        self.bouton_importer_corrige.configure(state="disabled")
        self.bouton_recalculer_cote_position.configure(state="disabled")
        self.bouton_retraiter_echecs.configure(state="disabled")
        self.bouton_supprimer_hors_code_postal.configure(state="disabled", text="Suppression en cours…")
        self.bouton_ouvrir_dossier.configure(state="disabled")
        self.label_statut.configure(text="Suppression définitive en cours…")

        try:
            excel_service = ExcelService(Path(fichier_entree))
            n = supprimer_parcelles_hors_codes_postaux(
                commune, codes_postaux, progress_store, excel_service, output_path=Path(fichier_sortie),
            )
        except Exception as exc:  # noqa: BLE001 — remonté proprement à l'UI
            logging.getLogger("gui").exception("Échec de la suppression hors code(s) postal(aux)")
            self._reactiver_boutons()
            messagebox.showerror(APP_TITLE, f"La suppression a échoué :\n{exc}")
            return

        self._reactiver_boutons()
        self._output_path = Path(fichier_sortie)
        self.bouton_ouvrir_dossier.configure(state="normal")
        texte = f"{n} ligne(s) supprimée(s) définitivement. Fichier régénéré : {fichier_sortie}"
        self._ajouter_log(texte)
        self.label_statut.configure(text=texte)
        messagebox.showinfo(APP_TITLE, texte)

    def _reactiver_boutons(self) -> None:
        self._en_cours = False
        self.bouton_lancer.configure(state="normal", text="Démarrer le traitement")
        self.bouton_importer_corrige.configure(state="normal", text="Importer un Excel corrigé…")
        self.bouton_recalculer_cote_position.configure(state="normal", text="Recalculer côté/position et doublons…")
        self.bouton_retraiter_echecs.configure(state="normal", text="Retraiter les parcelles échouées…")
        self.bouton_supprimer_hors_code_postal.configure(
            state="normal", text="Supprimer les lignes hors code(s) postal(aux)…",
        )

    def _fin_traitement(
        self, succes: bool, resultat: Optional[ResultatTraitement] = None, erreur: Optional[str] = None,
    ) -> None:
        self._reactiver_boutons()
        if succes:
            self._output_path = resultat.output_path
            self.barre_progression.set(1)
            if resultat.termine:
                statut = f"Terminé — commune entièrement traitée. Fichier généré : {resultat.output_path}"
            else:
                statut = (
                    f"Terminé pour cette exécution — {resultat.restantes} adresse(s) restante(s) sur "
                    f"{resultat.total_adresses} (relancez pour continuer). Fichier généré : {resultat.output_path}"
                )
            # Mentionné explicitement dans le résumé (pas seulement dans le
            # journal qui défile, où c'était auparavant facile à manquer —
            # voir main.py::ResultatTraitement.echecs) : rassure sans avoir
            # à faire défiler le journal pour retrouver l'information.
            if resultat.echecs > 0:
                statut += (
                    f" — {resultat.echecs} échec(s) pendant cette exécution, pas perdu(s), "
                    f"retenté(s) automatiquement au prochain lancement."
                )
            self.label_statut.configure(text=statut)
            self.bouton_ouvrir_dossier.configure(state="normal")
            if resultat.echecs > 0:
                messagebox.showwarning(
                    APP_TITLE,
                    f"Traitement terminé, mais {resultat.echecs} parcelle(s) ont échoué pendant "
                    f"cette exécution (incident réseau transitoire, généralement).\n\n"
                    f"Rien n'est perdu : une parcelle en échec n'est jamais enregistrée comme "
                    f"traitée, elle sera automatiquement redétectée et retentée au prochain "
                    f"lancement — aucune action manuelle n'est nécessaire.",
                )
        else:
            self.label_statut.configure(text="Échec du traitement — voir le journal ci-dessous.")
            messagebox.showerror(APP_TITLE, f"Le traitement a échoué :\n{erreur}")

    def _fin_synchronisation(
        self, rapport: RapportSynchronisation, chemin: Path, fichier_verrouille: bool = False,
    ) -> None:
        self._reactiver_boutons()
        self.barre_progression.set(1)
        texte = rapport.resume()
        if fichier_verrouille:
            texte += (
                " ATTENTION : les corrections ont bien été enregistrées, mais le fichier n'a "
                "PAS pu être réécrit — il est probablement encore ouvert dans Excel ou un autre "
                "programme. Fermez-le puis réimportez-le (ou cliquez sur Démarrer) pour voir le "
                "fichier mis à jour ; rien n'est perdu."
            )
            self._ajouter_log(texte)
            self.label_statut.configure(text="Corrections enregistrées, mais fichier non réécrit (fermé ?).")
            messagebox.showwarning(APP_TITLE, texte)
        else:
            self._output_path = chemin
            self.bouton_ouvrir_dossier.configure(state="normal")
            self._ajouter_log(texte)
            self.label_statut.configure(text=texte)
            messagebox.showinfo(APP_TITLE, texte)

    def _fin_recalcul(self, n: int, chemin: Path) -> None:
        self._reactiver_boutons()
        self.barre_progression.set(1)
        if n:
            self._output_path = chemin
            self.bouton_ouvrir_dossier.configure(state="normal")
            texte = f"Recalcul terminé — {n} correction(s) appliquée(s) (côté/position et doublons). Fichier généré : {chemin}"
        else:
            texte = "Recalcul terminé — rien à corriger (côté/position déjà calculés, aucun doublon détecté)."
        self._ajouter_log(texte)
        self.label_statut.configure(text=texte)
        messagebox.showinfo(APP_TITLE, texte)

    def _fin_retraitement_echecs(self, rapport: RapportEchecs, chemin: Path) -> None:
        self._reactiver_boutons()
        self.barre_progression.set(1)
        if rapport.tentees == 0:
            texte = "Retraitement terminé — aucune parcelle en échec à retraiter."
        elif rapport.termine:
            self._output_path = chemin
            self.bouton_ouvrir_dossier.configure(state="normal")
            texte = (
                f"Retraitement terminé — {rapport.reussies}/{rapport.tentees} parcelle(s) "
                f"en échec corrigée(s), plus aucun échec restant. Fichier généré : {chemin}"
            )
        else:
            self._output_path = chemin
            self.bouton_ouvrir_dossier.configure(state="normal")
            texte = (
                f"Retraitement terminé — {rapport.reussies}/{rapport.tentees} parcelle(s) "
                f"en échec corrigée(s), {rapport.restantes} encore en échec. Relancez ce "
                f"bouton pour continuer. Fichier généré : {chemin}"
            )
        self._ajouter_log(texte)
        self.label_statut.configure(text=texte)
        messagebox.showinfo(APP_TITLE, texte)


def _erreur_demarrage_fatale(message: str) -> None:
    """Dernier recours si l'application plante avant même d'afficher une
    fenêtre (ex: dépendance manquante dans l'exécutable). Construit avec
    --windowed (pas de console), un tel plantage serait sinon totalement
    silencieux pour l'utilisateur. `MessageBoxW` est un appel Windows natif
    indépendant de l'état de Tk/CustomTkinter, donc fiable même si c'est
    justement l'initialisation de Tk qui a échoué."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, APP_TITLE, 0x10)  # MB_ICONERROR
    except Exception:  # noqa: BLE001 - vraiment le dernier recours
        print(message, file=sys.stderr)


def main() -> int:
    try:
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")
        app = App()
        app.mainloop()
        return 0
    except Exception as exc:  # noqa: BLE001 - ne jamais planter en silence
        import traceback
        _erreur_demarrage_fatale(
            f"L'application n'a pas pu démarrer :\n\n{exc}\n\n{traceback.format_exc()}"
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
