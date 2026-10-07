@echo off
REM ============================================================
REM  package_shipping.bat -- S6A verification package.
REM
REM  Why this is two steps and not one `BuildCookRun -cook`:
REM  on this host a single-step cook fails with a Zen oplog
REM  `HTTP NotFound` (see docs/p0_freeze_notes.md #6 and
REM  RELEASE_NOTES.md). The working recipe is cook-to-loose with
REM  `-SkipZenStore`, then stage/pak/archive with `-skipcook`.
REM
REM  Why `-NoUBA` on the stage step:
REM  the Unreal Build Accelerator session dir on Q:\UBA is ACL
REM  locked here; link.exe and the WriteMetadata step both exit 0
REM  when run standalone with the identical .rsp and fail only
REM  under the executor. Deleting per-target intermediates does
REM  NOT fix it (QA proved that). So the executor is bypassed.
REM
REM  Why no `-build`/`-cook` in step 2:
REM  MCReplica-Win64-Shipping.exe is already newer than every
REM  file under project/Source, so there is nothing to compile.
REM  `-nocompile -nocompileeditor` makes that explicit instead
REM  of implicit.
REM
REM  Usage:  package_shipping.bat [archive-dir]
REM ============================================================
setlocal
set "UBA_ROOT=Q:\UBA"
set "DOTNET_ROOT=Q:\dotnet"
set "PATH=Q:\dotnet;%PATH%"
set "UE_SDKS_ROOT=Q:\AutoSDK"
set "WindowsSDKDir=Q:\WindowsKits"
set "WindowsSDKVersion=10.0.26100.0"

set "PY=Q:\MC2UE5\venv\Scripts\python.exe"
set "UE=Q:\UE\UE_5.8\Engine"
set "PROJ=Q:\MC2UE5\repo\project\MCReplica.uproject"
set "ARCHIVE=%~1"
if "%ARCHIVE%"=="" set "ARCHIVE=Q:\MC2UE5\dist_verify"
set "LOGDIR=Q:\MC2UE5\logs\s6a"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

REM C: loses the Windows SDK registry entry on reboot; re-register it.
if exist "%~dp0reg_sdk.py" "%PY%" "%~dp0reg_sdk.py" >nul 2>&1

REM NOTE: no '>' in any echo below. cmd parses a bare '>' as a redirect even
REM inside an echo, so a literal arrow here silently creates a *file* named
REM after the archive directory and then RunUAT cannot mkdir it.
echo ===STEP1 loose cook -SkipZenStore [log %LOGDIR%\cook1.log]===
"%UE%\Binaries\Win64\UnrealEditor-Cmd.exe" "%PROJ%" -run=Cook ^
  -TargetPlatform=Windows -unversioned -SkipZenStore ^
  -unattended -nopause -nosplash -nullrhi -stdout ^
  > "%LOGDIR%\cook1.log" 2>&1
echo ===COOK1_EXIT=%ERRORLEVEL%===

echo ===STEP2 stage+pak+archive [archive %ARCHIVE%]===
call "%UE%\Build\BatchFiles\RunUAT.bat" BuildCookRun ^
  -project="%PROJ%" -noP4 -platform=Win64 -clientconfig=Shipping ^
  -nocompile -nocompileeditor -skipcook ^
  -stage -pak -archive -archivedirectory="%ARCHIVE%" ^
  -utf8output -NoUBA -NoCodeSign -unattended -nopause -nosplash ^
  > "%LOGDIR%\cook2.log" 2>&1
echo ===COOK2_EXIT=%ERRORLEVEL%===

endlocal
