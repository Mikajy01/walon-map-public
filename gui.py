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
from typing import Optional

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
from main import ResultatTraitement, traiter_commune
from services.cache_service import CacheService, ProgressStore
from services.cadastre_service import CadastreService
from services.excel_service import ExcelService
from services.geoportail_service import ArcGISRestClient
from services.layers_service import LayersService
from services.sync_service import RapportSynchronisation, reecrire_excel_depuis_base, synchroniser_depuis_excel
from utils.licence import charger_mot_de_passe_local, verifier_et_enregistrer
from utils.logger import setup_logging
from utils.rate_limiter import RateLimiter

APP_TITLE = "Walonmap — Remplissage automatique"


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
        libelle = "Découverte des adresses" if phase == "découverte" else "Remplissage des colonnes"
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
            cadastre_service = CadastreService(client)
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

    # -- Import d'un Excel corrigé (reprise des cellules ERREUR) -------------

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

        self._en_cours = True
        self.bouton_lancer.configure(state="disabled")
        self.bouton_importer_corrige.configure(state="disabled", text="Importation en cours…")
        self.bouton_ouvrir_dossier.configure(state="disabled")
        self.barre_progression.set(0)
        self.label_statut.configure(text="Importation et synchronisation en cours…")
        self.zone_log.configure(state="normal")
        self.zone_log.delete("1.0", "end")
        self.zone_log.configure(state="disabled")

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
        texte de l'Excel."""
        try:
            cache_service = CacheService(config.CACHE_DIR)
            progress_store = ProgressStore(config.CACHE_DIR)
            client = ArcGISRestClient(
                cache=cache_service, rate_limiter=RateLimiter(config.MAX_REQUESTS_PER_SECOND),
                timeout=config.HTTP_TIMEOUT_SECONDS,
            )
            cadastre_service = CadastreService(client)
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

    def _reactiver_boutons(self) -> None:
        self._en_cours = False
        self.bouton_lancer.configure(state="normal", text="Démarrer le traitement")
        self.bouton_importer_corrige.configure(state="normal", text="Importer un Excel corrigé…")

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
            self.label_statut.configure(text=statut)
            self.bouton_ouvrir_dossier.configure(state="normal")
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
