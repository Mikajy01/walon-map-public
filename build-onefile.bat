@echo off
REM Toute la logique vit dans build_onefile.py/build_common.py (voir ce
REM dernier pour le contexte) - ce .bat ne sert qu'a lancer Python d'un
REM double-clic, sans rien faire d'autre.
python "%~dp0build_onefile.py" %*
