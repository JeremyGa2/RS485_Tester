@echo off
REM ============================================================
REM  Construction de RS485_COM_tester_V6.exe (Windows)
REM  Prérequis : Python 3.9 à 3.12 installé (case "Add to PATH")
REM  Double-cliquez ce fichier ou lancez-le depuis une invite.
REM ============================================================
setlocal
cd /d "%~dp0"

echo [1/4] Environnement virtuel...
if not exist .venv_build (
    py -3 -m venv .venv_build || python -m venv .venv_build || goto :err
)
call .venv_build\Scripts\activate.bat || goto :err

echo [2/4] Dependances...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt pyinstaller pillow || goto :err
python tools\make_icon.py || goto :err

echo [3/4] Auto-tests (liaison simulee)...
python RS485_COM_tester_V6.py --selftest || goto :err

echo [4/4] Construction de l'exe...
pyinstaller --noconfirm --clean RS485_COM_tester_V6.spec || goto :err

echo.
echo OK : dossier dist\RS485_Tester\ (lancer RS485_Tester.exe)
echo Copiez tout le dossier dist\RS485_Tester sur le PC cible.
pause
exit /b 0

:err
echo.
echo ECHEC de la construction (voir messages ci-dessus).
pause
exit /b 1
