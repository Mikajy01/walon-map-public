@echo off
setlocal EnableDelayedExpansion

echo === Walonmap - Construction de l'executable portable ===
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERREUR] Python introuvable dans le PATH. Installez Python 3.11+ et reessayez.
    exit /b 1
)

REM Construit dans un environnement virtuel DEDIE (.venv_build), pas dans
REM l'environnement Python courant : sur une machine de developpement
REM partagee, des paquets sans rapport (data science, etc.) installes pour
REM d'autres projets peuvent sinon etre detectes et embarques par erreur
REM par l'analyse de dependances de PyInstaller, gonflant l'executable de
REM quelques Mo a plusieurs centaines de Mo pour rien.
if not exist .venv_build (
    echo Creation d'un environnement virtuel dedie au build - dossier .venv_build...
    python -m venv .venv_build
    if errorlevel 1 (
        echo [ERREUR] Echec de la creation de l'environnement virtuel.
        exit /b 1
    )
)

echo Installation des dependances (execution + build) dans l'environnement dedie...
.venv_build\Scripts\python.exe -m pip install --upgrade pip >nul
.venv_build\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
if errorlevel 1 (
    echo [ERREUR] Echec de l'installation des dependances.
    exit /b 1
)

echo.
echo Nettoyage des builds precedents...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
del /q Walonmap*.spec >nul 2>nul

echo.
REM Demande la version a construire (au lieu de se contenter, comme avant,
REM de relire config.APP_VERSION sans jamais l'incrementer) - constate en
REM conditions reelles : plusieurs reconstructions avec du code reellement
REM different, toujours etiquetees "1.0.0" faute d'y penser a chaque fois.
for /f %%v in ('.venv_build\Scripts\python.exe -c "import config; print(config.APP_VERSION)"') do set VERSION_ACTUELLE=%%v
if not defined VERSION_ACTUELLE (
    echo [ERREUR] Impossible de lire config.APP_VERSION.
    exit /b 1
)

REM Version passee en argument (ex: "build.bat 1.2.0") : permet un build
REM automatise/non interactif (aucune invite possible, pas de terminal
REM attache) - sans argument, demande interactivement, Entree pour garder
REM la version actuelle.
if not "%~1"=="" (
    set NOUVELLE_VERSION=%~1
    echo Version indiquee en argument : !NOUVELLE_VERSION!
) else (
    set /p "NOUVELLE_VERSION=Version actuelle : %VERSION_ACTUELLE% - nouvelle version a construire (Entree pour garder) : "
    if "!NOUVELLE_VERSION!"=="" set NOUVELLE_VERSION=%VERSION_ACTUELLE%
)

if not "!NOUVELLE_VERSION!"=="%VERSION_ACTUELLE%" (
    echo Mise a jour de config.py : APP_VERSION = "!NOUVELLE_VERSION!"...
    REM chr(34) au lieu d'un guillemet litteral dans le code Python : evite
    REM tout guillemet imbrique dans cet argument -c "..." passe par
    REM cmd.exe, dont l'echappement par antislash (\") n'est PAS valide en
    REM batch (source du bug "'.' etait inattendu" / "assert etait
    REM inattendu" rencontre en le testant - la chaine se retrouvait coupee
    REM en plein milieu, le reste interprete comme des commandes cmd).
    REM ".*?" (non-greedy) plutot que "[^"]*" : "^" est AUSSI le caractere
    REM d'echappement de cmd.exe, avale silencieusement meme entre guillemets
    REM (deuxieme piege rencontre en testant - re.subn ne trouvait plus rien
    REM a remplacer, "[^"]*" etant devenu "["]*" une fois passe par cmd.exe).
    .venv_build\Scripts\python.exe -c "import pathlib, re; p = pathlib.Path('config.py'); t = p.read_text(encoding='utf-8'); q = chr(34); t2, n = re.subn('APP_VERSION = ' + q + '.*?' + q, 'APP_VERSION = ' + q + '!NOUVELLE_VERSION!' + q, t, count=1); assert n == 1; p.write_text(t2, encoding='utf-8')"
    if errorlevel 1 (
        echo [ERREUR] Echec de la mise a jour de config.py.
        exit /b 1
    )
)
set APP_VERSION=!NOUVELLE_VERSION!
echo Version : %APP_VERSION%

echo.
echo Construction ONEFILE (PyInstaller --onefile --windowed)...
REM --collect-all customtkinter : embarque les fichiers de theme JSON de la
REM bibliotheque, qu'une analyse PyInstaller standard ne detecte pas seule.
REM --distpath/--workpath separes du build onedir ci-dessous : les deux
REM utilisent le meme --name, un cache d'analyse partage entre les deux
REM modes (onefile/onedir different a l'etape EXE/COLLECT) serait risque.
REM --log-level WARN : reduit le volume de sortie (des milliers de lignes
REM INFO sinon) - constate en conditions reelles : enchainer deux builds
REM PyInstaller verbeux dans le meme script, avec la sortie redirigee/
REM capturee (execution non interactive), corrompt parfois la lecture par
REM cmd.exe de la suite du script juste apres le premier build ("'.' etait
REM inattendu", reproduit plusieurs fois) - bug d'environnement cmd.exe
REM connu (position de lecture du fichier .bat desynchronisee par un gros
REM volume de sortie d'un sous-processus), pas une erreur de syntaxe.
.venv_build\Scripts\pyinstaller.exe --onefile --windowed --log-level WARN --name Walonmap-%APP_VERSION% --collect-all customtkinter --distpath dist\onefile --workpath build\onefile gui.py
if errorlevel 1 (
    echo [ERREUR] Echec de la construction PyInstaller (onefile).
    exit /b 1
)

echo.
echo Construction ONEDIR (PyInstaller --onedir --windowed)...
REM Un seul .exe qui s'auto-extrait au lancement (mode onefile) est un
REM classique faux positif antivirus (comportement heuristique proche d'un
REM dropper de malware) - cas reel constate, un .exe envoye a un
REM collaborateur supprime automatiquement par son Windows. Le mode onedir
REM (un dossier avec l'exe + ses dependances en fichiers separes, sans
REM extraction cachee au lancement) est nettement moins souvent flague ;
REM les deux sont construits pour laisser le choix selon le contexte de
REM distribution.
.venv_build\Scripts\pyinstaller.exe --onedir --windowed --log-level WARN --name Walonmap-%APP_VERSION% --collect-all customtkinter --distpath dist\onedir --workpath build\onedir gui.py
if errorlevel 1 (
    echo [ERREUR] Echec de la construction PyInstaller (onedir).
    exit /b 1
)

echo.
echo Preparation des dossiers portables (dist\onefile\, dist\onedir\)...
if not exist dist\onefile\data mkdir dist\onefile\data
if exist data\liens_communaux.csv (
    if not exist dist\onefile\data\liens_communaux.csv copy data\liens_communaux.csv dist\onefile\data\liens_communaux.csv >nul
)
if not exist "dist\onedir\Walonmap-%APP_VERSION%\data" mkdir "dist\onedir\Walonmap-%APP_VERSION%\data"
if exist data\liens_communaux.csv (
    if not exist "dist\onedir\Walonmap-%APP_VERSION%\data\liens_communaux.csv" copy data\liens_communaux.csv "dist\onedir\Walonmap-%APP_VERSION%\data\liens_communaux.csv" >nul
)

echo.
echo === Termine ===
echo Executable onefile (un seul .exe) : dist\onefile\Walonmap-%APP_VERSION%.exe
echo Dossier onedir (moins susceptible d'etre bloque par un antivirus) : dist\onedir\Walonmap-%APP_VERSION%\
echo.
echo Pour distribuer l'application, copiez le dossier complet correspondant
echo au mode choisi (dist\onefile\ ou dist\onedir\Walonmap-%APP_VERSION%\, avec
echo son dossier data\) sur la machine cible. Les dossiers cache\, logs\ et
echo output\ seront crees automatiquement a cote de l'exe au premier lancement.
echo Aucune installation de Python n'est requise sur la machine cible.

endlocal
