@echo off
setlocal enabledelayedexpansion
REM ===========================================================================
REM Build tdjson from source, on Windows, with unambiguous provenance.
REM
REM This script is the build procedure. DEVELOPMENT_WORKFLOW.md section 26
REM describes it in prose; this is what was actually run to produce the binary
REM recorded in tdjson_manifest.json, which is why it is committed rather than
REM documented and hoped for.
REM
REM Everything it links is built from source by vcpkg and linked STATICALLY.
REM That is a security property, not a preference: the manifest checksums one
REM file, so anything that file loads at runtime would be unverified code
REM inside the trust boundary (ADR-047). A dynamically linked tdjson works
REM perfectly while its OpenSSL sits unchecked beside it -- the failure mode is
REM silence, which is why the linkage is forced rather than left to default.
REM
REM Usage:   scripts\build-tdjson.bat [build-root] [tdlib-commit]
REM Default: E:\tdlib-build  and the commit pinned below.
REM
REM Requires: git, and Visual Studio 2022 or newer with the C++ workload.
REM           CMake and Ninja ship with Visual Studio; vcpkg is cloned here.
REM Takes:    roughly one hour on a first run, most of it OpenSSL.
REM ===========================================================================

set ROOT=%~1
if "%ROOT%"=="" set ROOT=E:\tdlib-build

set TD_COMMIT=%~2
if "%TD_COMMIT%"=="" set TD_COMMIT=022d60202e446ad1287b9fb68e687c8a0760788b

REM --- Locate Visual Studio -------------------------------------------------
REM Discovered rather than hardcoded: the edition and version differ per
REM machine, and a hardcoded path is a script that works only on one.
set VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe
if not exist "%VSWHERE%" (
    echo ERROR: vswhere.exe not found. Install Visual Studio with the C++ workload.
    exit /b 1
)
for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set VSROOT=%%i
if "%VSROOT%"=="" (
    echo ERROR: no Visual Studio installation with the C++ toolset was found.
    exit /b 1
)

set VCVARS=%VSROOT%\VC\Auxiliary\Build\vcvars64.bat
set CMAKE=%VSROOT%\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe
set NINJA=%VSROOT%\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe
for %%F in ("%VCVARS%" "%CMAKE%" "%NINJA%") do (
    if not exist %%F echo ERROR: missing %%F & exit /b 1
)

echo Visual Studio : %VSROOT%
echo Build root    : %ROOT%
echo TDLib commit  : %TD_COMMIT%
echo.

if not exist "%ROOT%" mkdir "%ROOT%"
cd /d "%ROOT%" || exit /b 1

REM --- 1. vcpkg -------------------------------------------------------------
echo [1/6] vcpkg
if not exist "%ROOT%\vcpkg\.git" (
    git clone --depth 1 https://github.com/microsoft/vcpkg.git "%ROOT%\vcpkg" || exit /b 1
)
if not exist "%ROOT%\vcpkg\vcpkg.exe" (
    call "%ROOT%\vcpkg\bootstrap-vcpkg.bat" -disableMetrics || exit /b 1
)

REM --- 2. Dependencies ------------------------------------------------------
REM gperf is a build-time code generator and stays on the dynamic triplet: it
REM is run, not linked, so it never enters the artefact. OpenSSL and zlib are
REM static for the reason at the top of this file.
echo [2/6] dependencies - openssl is the long one, around 25 minutes
"%ROOT%\vcpkg\vcpkg.exe" install gperf:x64-windows zlib:x64-windows-static openssl:x64-windows-static --clean-after-build || exit /b 1

REM --- 3. Source at the pinned commit ---------------------------------------
REM Fetched by SHA rather than cloned and checked out. TDLib tags rarely (its
REM newest tag is years older than its releases), so a commit is the only
REM precise pin -- and fetching that SHA directly cannot be confused with
REM whatever master happens to be today.
echo [3/6] TDLib source at the pinned commit
if not exist "%ROOT%\td\.git" (
    mkdir "%ROOT%\td" 2>nul
    cd /d "%ROOT%\td" || exit /b 1
    git init -q . || exit /b 1
    git remote add origin https://github.com/tdlib/td.git || exit /b 1
)
cd /d "%ROOT%\td" || exit /b 1
git fetch --depth 1 origin %TD_COMMIT% || exit /b 1
git checkout -q FETCH_HEAD || exit /b 1
git rev-parse HEAD > "%ROOT%\pinned-commit.txt"

REM --- 4. Compiler environment ----------------------------------------------
echo [4/6] MSVC environment
call "%VCVARS%" || exit /b 1

REM --- 5. Configure ---------------------------------------------------------
REM Ninja rather than a Visual Studio generator: the CMake bundled with VS 18
REM offers no VS18 generator, so the IDE generator is not always available even
REM when the IDE is.
REM
REM GPERF_EXECUTABLE has to be named. vcpkg installs host tools under
REM installed\<triplet>\tools\<port>\, which is not on PATH and not a location
REM TDLib's find_program searches.
echo [5/6] configure
if not exist "%ROOT%\td\build" mkdir "%ROOT%\td\build"
cd /d "%ROOT%\td\build" || exit /b 1
"%CMAKE%" -G Ninja ^
  -DCMAKE_MAKE_PROGRAM="%NINJA%" ^
  -DCMAKE_BUILD_TYPE=Release ^
  -DCMAKE_TOOLCHAIN_FILE="%ROOT%\vcpkg\scripts\buildsystems\vcpkg.cmake" ^
  -DVCPKG_TARGET_TRIPLET=x64-windows-static ^
  -DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded ^
  -DGPERF_EXECUTABLE="%ROOT%\vcpkg\installed\x64-windows\tools\gperf\gperf.exe" ^
  .. || exit /b 1

REM --- 6. Build -------------------------------------------------------------
echo [6/6] build tdjson
"%CMAKE%" --build . --target tdjson --parallel || exit /b 1

echo.
echo BUILD COMPLETE
for /r "%ROOT%\td\build" %%F in (tdjson.dll) do echo   %%F
echo.
echo Next: copy it to ^<data_dir^>\tdlib\tdjson.dll, then run
echo   tgassist tdlib verify     to see the manifest entry to add
echo   tgassist tdlib doctor     to check it end to end
endlocal
