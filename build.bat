@echo off
setlocal

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
echo Lecture du numero de version (config.APP_VERSION)...
REM Inclus dans le nom de l'executable pour qu'un collaborateur ne se
REM retrouve jamais a utiliser une ancienne version sans le savoir (une rue
REM deja marquee "redecouverte" par une ancienne version, par exemple, ne
REM profite d'aucun correctif ulterieur tant que l'app n'est pas mise a
REM jour - voir config.py::APP_VERSION et gui.py::APP_TITLE, qui l'affiche
REM aussi dans le titre de la fenetre).
for /f %%v in ('.venv_build\Scripts\python.exe -c "import config; print(config.APP_VERSION)"') do set APP_VERSION=%%v
if not defined APP_VERSION (
    echo [ERREUR] Impossible de lire config.APP_VERSION.
    exit /b 1
)
echo Version : %APP_VERSION%

echo.
echo Construction de l'executable (PyInstaller --onefile --windowed)...
REM --collect-all customtkinter : embarque les fichiers de theme JSON de la
REM bibliotheque, qu'une analyse PyInstaller standard ne detecte pas seule.
.venv_build\Scripts\pyinstaller.exe --onefile --windowed --name Walonmap-%APP_VERSION% --collect-all customtkinter gui.py
if errorlevel 1 (
    echo [ERREUR] Echec de la construction PyInstaller.
    exit /b 1
)

echo.
echo Preparation du dossier portable (dist\)...
if not exist dist\data mkdir dist\data
if exist data\liens_communaux.csv (
    if not exist dist\data\liens_communaux.csv copy data\liens_communaux.csv dist\data\liens_communaux.csv >nul
)

echo.
echo === Termine ===
echo Executable portable : dist\Walonmap-%APP_VERSION%.exe
echo.
echo Pour distribuer l'application, copiez tout le dossier "dist" (l'exe et
echo son dossier data\) sur la machine cible. Les dossiers cache\, logs\ et
echo output\ seront crees automatiquement a cote de l'exe au premier lancement.
echo Aucune installation de Python n'est requise sur la machine cible.

endlocal
