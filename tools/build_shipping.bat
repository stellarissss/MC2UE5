@echo off
REM ============================================================
REM  build_shipping.bat -- compile the Win64 Shipping target.
REM
REM  This is the *compile* step only. It is separate from
REM  package_shipping.bat (cook + stage + pak), because the two fail for
REM  unrelated reasons and mixing them makes the failure ambiguous:
REM  a cook failure and a link failure look the same from UAT.
REM
REM  Env: the toolchain lives on Q:. UBT discovers Visual Studio via the
REM  registry and C: loses the Windows SDK entry on reboot, so the SDK root
REM  is re-registered first by tools\reg_sdk.py.
REM
REM  -NoUBA is required on this host: the Unreal Build Accelerator session
REM  dir Q:\UBA is ACL locked, and the executor fails where the same steps
REM  pass standalone. See docs/p0_freeze_notes.md.
REM
REM  Usage:  build_shipping.bat [config]      (default Shipping)
REM ============================================================
setlocal
set "UBA_ROOT=Q:\UBA"
set "DOTNET_ROOT=Q:\dotnet"
set "PATH=Q:\dotnet;%PATH%"
set "UE_SDKS_ROOT=Q:\AutoSDK"
set "WindowsSDKDir=Q:\WindowsKits"
set "WindowsSDKVersion=10.0.26100.0"

set "PY=Q:\MC2UE5\venv\Scripts\python.exe"
set "UAT=Q:\UE\UE_5.8\Engine\Build\BatchFiles\Build.bat"
set "PROJ=Q:\MC2UE5\repo\project\MCReplica.uproject"
set "CFG=%~1"
if "%CFG%"=="" set "CFG=Shipping"
set "LOGDIR=Q:\MC2UE5\logs\s6a"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

if exist "%~dp0reg_sdk.py" "%PY%" "%~dp0reg_sdk.py" >nul 2>&1

echo ===BUILD MCReplica Win64 %CFG%===
call "%UAT%" MCReplica Win64 %CFG% "%PROJ%" -WaitMutex -NoUBA
echo ===BUILD_EXIT=%ERRORLEVEL%===

endlocal
