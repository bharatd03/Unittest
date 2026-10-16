@echo off
rem ===========================================================================
rem  BaseAgent on Windows: runs the tests and/or starts BaseAgent.
rem  Brains: the Claude models your ANTHROPIC_API_KEY can access (Opus 5.5, Fable 5.1,
rem  Sonnet 5.5, Haiku 5.5, in that order), else Qwen3 8B through Ollama.
rem  No WSL. Needs Windows Python 3.11+; Ollama for Windows for the Qwen fallback.
rem
rem    baseagent.bat          menu
rem    baseagent.bat test     run all tests, results in test_results.txt
rem    baseagent.bat test QwenLive   only the tests whose name contains QwenLive
rem    baseagent.bat start    start BaseAgent at http://127.0.0.1:8765/mcp
rem    baseagent.bat all      tests, then start
rem    baseagent.bat check    show which brains are available and ask each one question
rem
rem  Optional settings, set before running:
rem    set BASEAGENT_QWEN_MODEL=qwen3:8b   Ollama tag of your model, see: ollama list
rem    set BASEAGENT_BRAIN=auto            auto = the first Claude model the key can access,
rem                                        Qwen when none is; or one brain: opus, fable,
rem                                        sonnet, haiku or qwen
rem    set BASEAGENT_PYTHON=C:\path\python.exe   use this Python instead of .venv
rem    set BASEAGENT_QWEN_THINK=1          live Qwen tests with thinking mode - slower
rem    ANTHROPIC_API_KEY stays an environment variable; it is never written to a file.
rem ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
if not defined BASEAGENT_QWEN_MODEL set "BASEAGENT_QWEN_MODEL=qwen3:8b"
if not defined BASEAGENT_BRAIN set "BASEAGENT_BRAIN=auto"
if not defined BASEAGENT_OLLAMA_URL set "BASEAGENT_OLLAMA_URL=http://127.0.0.1:11434"
set "MODEL=%BASEAGENT_QWEN_MODEL%"
set "RESULTS=%~dp0test_results.txt"
set "PACKAGES=pydantic langgraph fastmcp uvicorn anthropic"
set "IMPORTS=import pydantic, langgraph, fastmcp, uvicorn, anthropic"

set "ACTION=%~1"
set "FILTER="
if not "%~2"=="" set "FILTER=-k %~2"
set "INTERACTIVE="
if not defined ACTION set "INTERACTIVE=1"
if not defined ACTION goto menu
goto dispatch

:menu
echo.
echo  BaseAgent - model: %MODEL% - brain: %BASEAGENT_BRAIN%
echo.
echo    1  Run all tests - results go to test_results.txt
echo    2  Start BaseAgent - keep the window open while agents use it
echo    3  Run all tests, then start BaseAgent
echo    4  Quick check - which brains are available, one question to each
echo    Q  Quit
echo.
choice /c 1234Q /n /m "Choose 1, 2, 3, 4 or Q: "
if errorlevel 5 goto end
if errorlevel 4 goto pick_check
if errorlevel 3 goto pick_all
if errorlevel 2 goto pick_start
set "ACTION=test"
goto dispatch
:pick_check
set "ACTION=check"
goto dispatch
:pick_all
set "ACTION=all"
goto dispatch
:pick_start
set "ACTION=start"

:dispatch
if /i "%ACTION%"=="test"  goto prepare
if /i "%ACTION%"=="start" goto prepare
if /i "%ACTION%"=="all"   goto prepare
if /i "%ACTION%"=="check" goto prepare
echo Unknown option "%ACTION%". Use: test, start, all or check.
set "BA_EXIT=2"
goto end

rem ---------------------------------------------------------------- Python
:prepare
set "PY="
if defined BASEAGENT_PYTHON set "PY=%BASEAGENT_PYTHON%"
if defined PY goto have_python
rem Reuse the static_analysis environment only if it already has every package.
set "SHARED=%~dp0..\static_analysis\.venv\Scripts\python.exe"
if not exist "%SHARED%" goto own_venv
"%SHARED%" -c "%IMPORTS%" >nul 2>&1
if errorlevel 1 goto own_venv
set "PY=%SHARED%"
goto have_python

:own_venv
set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%PY%" goto have_python
echo Creating the Python environment .venv - first run only ...
where py >nul 2>&1
if errorlevel 1 goto venv_with_python
py -3 -m venv "%~dp0.venv"
goto venv_made
:venv_with_python
where python >nul 2>&1
if errorlevel 1 goto no_python
python -m venv "%~dp0.venv"
:venv_made
if exist "%PY%" goto have_python
:no_python
echo Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/windows/
echo and tick "Add python.exe to PATH", then run this file again.
set "BA_EXIT=1"
goto end

:have_python
"%PY%" -c "%IMPORTS%" >nul 2>&1
if not errorlevel 1 goto python_ok
echo Installing %PACKAGES% - first run only ...
"%PY%" -m pip install --disable-pip-version-check %PACKAGES%
"%PY%" -c "%IMPORTS%"
if not errorlevel 1 goto python_ok
echo.
echo A required package cannot be imported - see the message above.
echo If it says a DLL or file was blocked, Windows App Control stopped that package;
echo send the message to Claude rather than turning App Control off.
set "BA_EXIT=1"
goto end
:python_ok
echo Python : %PY%

rem ---------------------------------------------------------------- Ollama and Qwen
set "QWEN_READY="
where ollama >nul 2>&1
if errorlevel 1 goto no_ollama
ollama list >nul 2>&1
if not errorlevel 1 goto ollama_running
echo Starting Ollama ...
start "Ollama" /min ollama serve
set /a TRIES=0
:wait_ollama
ping -n 3 127.0.0.1 >nul
ollama list >nul 2>&1
if not errorlevel 1 goto ollama_running
set /a TRIES+=1
if %TRIES% LSS 15 goto wait_ollama
echo Ollama did not start. Open the Ollama app from the Start menu and run this file again.
goto qwen_checked
:ollama_running
ollama show %MODEL% >nul 2>&1
if not errorlevel 1 goto model_ok
echo The model %MODEL% is not in Ollama yet. See your models with: ollama list
echo If your Qwen3 8B has another name, set BASEAGENT_QWEN_MODEL to it.
choice /c YN /m "Download %MODEL% now - about 5 GB"
if errorlevel 2 goto qwen_checked
ollama pull %MODEL%
ollama show %MODEL% >nul 2>&1
if errorlevel 1 goto qwen_checked
:model_ok
set "QWEN_READY=1"
echo Qwen   : %MODEL% is ready in Ollama
goto qwen_checked
:no_ollama
echo Ollama is not installed. Get it from https://ollama.com/download/windows
:qwen_checked
if not defined QWEN_READY echo Qwen   : NOT available - the live Qwen tests will be skipped; BaseAgent uses the Claude models the API key can access.

if /i "%ACTION%"=="start" goto start_server
if /i "%ACTION%"=="check" goto check

rem ---------------------------------------------------------------- tests
:run_tests
echo.
echo Running the tests. The live Qwen tests take several minutes on a CPU ...
> "%RESULTS%" echo ==== BaseAgent tests %date% %time% ====
"%PY%" -c "import sys, importlib.metadata as m; print('Python', sys.version); [print(p, next((d.version for d in m.distributions(name=p)), 'MISSING')) for p in ('pydantic', 'langgraph', 'fastmcp', 'mcp', 'uvicorn', 'anthropic')]" >> "%RESULTS%" 2>&1
ollama --version >> "%RESULTS%" 2>&1
>> "%RESULTS%" echo Model: %MODEL%   Ollama: %BASEAGENT_OLLAMA_URL%   Thinking: %BASEAGENT_QWEN_THINK%   Filter: %FILTER%
>> "%RESULTS%" echo.
rem Repository layout: the tests are in the Test folder at the root, two levels up.
pushd "%~dp0..\..\Test"
"%PY%" -u -m unittest -v %FILTER% test_baseagent >> "%RESULTS%" 2>&1
set "BA_EXIT=%errorlevel%"
popd
echo.
echo ---- summary ----
findstr /b /c:"FAIL:" /c:"ERROR:" "%RESULTS%"
findstr /r /c:"^Ran [0-9]* test" /c:"^OK" /c:"^FAILED" "%RESULTS%"
echo Full results: %RESULTS%
if not "%BA_EXIT%"=="0" echo Some tests failed. Send test_results.txt to Claude.
if /i not "%ACTION%"=="all" goto end

rem ---------------------------------------------------------------- server
:start_server
echo.
if exist "demo\config.json" goto have_demo
"%PY%" baseagent.py init-demo demo
if errorlevel 1 goto start_failed
:have_demo
"%PY%" baseagent.py renew-token --config demo\config.json
if errorlevel 1 goto start_failed
echo.
echo BaseAgent: http://127.0.0.1:8765/mcp - token in demo\client.token - brain %BASEAGENT_BRAIN%
echo Keep this window open while agents use BaseAgent. Stop with Ctrl+C.
echo.
"%PY%" baseagent.py serve --config demo\config.json --brain %BASEAGENT_BRAIN% --qwen-model %MODEL%
set "BA_EXIT=%errorlevel%"
echo BaseAgent stopped.
goto end
:start_failed
set "BA_EXIT=1"
echo BaseAgent could not be prepared - see the message above.
goto end

rem ---------------------------------------------------------------- quick check
:check
if exist "demo\config.json" goto check_demo
"%PY%" baseagent.py init-demo demo
if errorlevel 1 goto start_failed
:check_demo
echo Checking which brains are available and asking each one question - the first Qwen answer can take a few minutes ...
"%PY%" baseagent.py probe --config demo\config.json --ask --qwen-model %MODEL%
set "BA_EXIT=%errorlevel%"
goto end

:end
if not defined BA_EXIT set "BA_EXIT=0"
if defined INTERACTIVE pause
endlocal & exit /b %BA_EXIT%
