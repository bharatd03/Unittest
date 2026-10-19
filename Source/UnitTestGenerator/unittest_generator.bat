@echo off
rem ===========================================================================
rem  Unit-test generator on Windows. No WSL.
rem
rem    unittest_generator.bat                   menu
rem    unittest_generator.bat setup             check every tool again and install what is missing
rem    unittest_generator.bat check             show the tools, Python packages and brains in use
rem    unittest_generator.bat test              the component tests (a few minutes), results in test_results.txt
rem    unittest_generator.bat test-full         every test incl. whole pipeline runs, in parallel (release validation)
rem    unittest_generator.bat pilot-c           full pipeline on the C pilot
rem    unittest_generator.bat pilot-cpp         full pipeline on the C++ pilot (three SUT files)
rem    unittest_generator.bat pilot-deps        full pipeline on the CMake pilot with dependencies (three SUT files)
rem    unittest_generator.bat pilot-embedded    full pipeline on the embedded C pilot (registers, superloop, asm; four files)
rem    unittest_generator.bat pilot-embedded-cpp  full pipeline on the embedded C++ pilot (fakes, exceptions, template; four files)
rem    unittest_generator.bat view RUN_FOLDER   show an earlier run again in the viewer (and viewer.html)
rem    unittest_generator.bat baseagent-test    the BaseAgent tests, results in test_results_baseagent.txt [TP-24]
rem    unittest_generator.bat benchmark [on|off|both]
rem                                             seeded-defect benchmark on the pilots, with the knowledge
rem                                             packs (on, default), without (off) or both to compare them
rem    unittest_generator.bat run REPO SUT [options]
rem                                             REPO = git repository link (https://... or git@...) or a folder
rem                                             SUT  = file relative to REPO, e.g. src/foo.c
rem                                             options: --branch NAME  --sut ANOTHER_FILE
rem                                                      --build-command "..."  --compile-commands FILE
rem
rem  A repository link: the default branch is tested unless --branch names another one. Tests only and
rem  verified: one commit to that branch. Production source changed, or not verified: a review branch and a
rem  pull request - merging it is your approval. Git uses the login already set up on this PC.
rem
rem  Every command first runs ut_setup.py [TP-12]: it checks the tools and installs what is missing
rem  into the tools folder next to this file, from the official release pages, with SHA-256 checks.
rem  The pipeline then uses only the tools listed in tools\toolchain.json; it has no fallbacks.
rem
rem  Brains: the Claude models ANTHROPIC_API_KEY can access (Opus 5.5, Fable 5.1, Sonnet 5.5, Haiku 5.5,
rem  in the order of ut_config.json), else Qwen3 8B through Ollama. "check" shows which are available.
rem
rem  Live viewer: every run opens it in the browser or prints its address; add --no-view to switch it
rem  off. Settings: "viewer" in ut_config.json.
rem
rem  Python: the BaseAgent environment ..\BaseAgent\.venv (created with Python 3.12 if missing),
rem  or set BASEAGENT_PYTHON. ANTHROPIC_API_KEY stays an environment variable; it is never written
rem  to a file.
rem ===========================================================================
setlocal EnableExtensions
rem  A relative repository folder is meant from where the command was started [TP-23].
set "CALLER=%CD%"
cd /d "%~dp0"
rem  The exit code is kept in UT_EXIT, never in RC: CMake reads a variable named RC as the Windows
rem  resource compiler, and every program started from here inherits it [A3-BD15].
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
if not defined BASEAGENT_HOME set "BASEAGENT_HOME=%~dp0..\BaseAgent"
if not defined BASEAGENT_QWEN_MODEL set "BASEAGENT_QWEN_MODEL=qwen3:8b"
set "RESULTS=%~dp0test_results.txt"
rem Repository layout: Source\UnitTestGenerator (this file), Source\BaseAgent, Test (tests and pilots).
set "TESTS=%~dp0..\..\Test"
set "PILOTS=%~dp0..\..\Test\pilots"
set "ACTION=%~1"
set "INTERACTIVE="
set "EXTRA="
if not defined ACTION set "INTERACTIVE=1"
if not defined ACTION goto menu
goto prepare

:menu
echo.
echo  Unit-test generator
echo.
echo    0  Setup - check every tool again and install what is missing
echo    1  Check - show the tools, Python packages and brains in use
echo    2  Run the component tests - a few minutes, results go to test_results.txt
echo    3  Run the pipeline on the C pilot
echo    4  Run the pipeline on the C++ pilot
echo    5  Run the pipeline on the CMake pilot with dependencies
echo    6  Run the seeded-defect benchmark
echo    7  Run the pipeline on your own repository (link or folder)
echo    8  Run every test, with the whole pipeline runs (test-full, slower)
echo    Q  Quit
echo.
choice /c 012345678Q /n /m "Choose 0-8 or Q: "
if errorlevel 10 goto end
if errorlevel 9 (set "ACTION=test-full" & goto prepare)
if errorlevel 8 goto pick_run
if errorlevel 7 (set "ACTION=benchmark" & goto prepare)
if errorlevel 6 (set "ACTION=pilot-deps" & goto prepare)
if errorlevel 5 (set "ACTION=pilot-cpp" & goto prepare)
if errorlevel 4 (set "ACTION=pilot-c" & goto prepare)
if errorlevel 3 (set "ACTION=test" & goto prepare)
if errorlevel 2 (set "ACTION=check" & goto prepare)
set "ACTION=setup"
goto prepare
:pick_run
set "ACTION=run"
set /p "REPO=Repository link (https://... or git@...) or folder: "
set /p "SUT=SUT file, relative to the repository: "
set "BRANCH="
set /p "BRANCH=Branch (Enter = the repository's default branch): "
goto prepare

rem ---------------------------------------------------------------- Python 3.12
:prepare
if /i "%ACTION%"=="run" if not "%~2"=="" set "REPO=%~2"
if /i "%ACTION%"=="run" if not "%~3"=="" set "SUT=%~3"
if /i "%ACTION%"=="run" if not "%~4"=="" set EXTRA=%4 %5 %6 %7 %8 %9
set "KNOWLEDGE=on"
if /i "%ACTION%"=="benchmark" if not "%~2"=="" set "KNOWLEDGE=%~2"
if /i "%ACTION%"=="view" set "VIEWDIR=%~2"
if /i "%ACTION:~0,6%"=="pilot-" if not "%~2"=="" set EXTRA=%2 %3
rem  Without quotes and without a trailing backslash: "C:\repo\" would escape the closing quote.
rem  Only when REPO is set: cmd turns a substitution on an unset variable into broken text.
if not defined REPO goto repo_clean
set "REPO=%REPO:"=%"
if "%REPO:~-1%"=="\" set "REPO=%REPO:~0,-1%"
:repo_clean
rem  TP-23: a relative folder (not a link, not a drive or network path) is resolved from the caller's folder.
if not defined REPO goto repo_done
if /i "%REPO:~0,4%"=="http" goto repo_done
if /i "%REPO:~0,4%"=="git@" goto repo_done
if /i "%REPO:~0,6%"=="ssh://" goto repo_done
if "%REPO:~1,1%"==":" goto repo_done
if "%REPO:~0,2%"=="\\" goto repo_done
if exist "%CALLER%\%REPO%" for %%I in ("%CALLER%\%REPO%") do set "REPO=%%~fI"
:repo_done
set "PY="
if defined BASEAGENT_PYTHON set "PY=%BASEAGENT_PYTHON%"
if defined PY goto have_python
set "PY=%BASEAGENT_HOME%\.venv\Scripts\python.exe"
if exist "%PY%" goto have_python
echo The BaseAgent Python environment was not found; creating it with Python 3.12 ...
where py >nul 2>&1
if errorlevel 1 goto no_python
py -3.12 -m venv "%BASEAGENT_HOME%\.venv"
if exist "%PY%" goto have_python
:no_python
echo Python 3.12 was not found. Install it from python.org (with the "py" launcher) and run this file again.
set "UT_EXIT=1"
goto end
:have_python
if exist "%BASEAGENT_HOME%\baseagent.py" goto have_baseagent
echo baseagent.py was not found in %BASEAGENT_HOME%. Set BASEAGENT_HOME to the BaseAgent folder.
set "UT_EXIT=1"
goto end
:have_baseagent

if /i "%ACTION%"=="view" goto view_run
if /i "%ACTION%"=="baseagent-test" goto baseagent_tests

rem ---------------------------------------------------------------- tools [TP-12]
set "SETUP_ARGS=ensure"
if /i "%ACTION%"=="setup" set "SETUP_ARGS=ensure --force"
if /i "%ACTION%"=="run" if defined REPO set "SETUP_ARGS=ensure --repo "%REPO%""
if /i "%ACTION%"=="run" if defined REPO if defined BRANCH set "SETUP_ARGS=ensure --repo "%REPO%" --branch "%BRANCH%""
if /i "%ACTION%"=="pilot-deps" set "SETUP_ARGS=ensure --repo "%PILOTS%\c_deps_pilot""
"%PY%" ut_setup.py %SETUP_ARGS%
set "UT_EXIT=%errorlevel%"
if "%UT_EXIT%"=="0" goto tools_ready
echo.
if "%UT_EXIT%"=="2" echo Windows blocked a tool. Nothing was worked around - see the options above.
if "%UT_EXIT%"=="3" echo An action by you is needed - see above.
if not "%UT_EXIT%"=="2" if not "%UT_EXIT%"=="3" echo Setup did not complete. The pipeline does not start without its tools.
goto end
:tools_ready
if /i "%ACTION%"=="setup" goto end

rem ---------------------------------------------------------------- Ollama (Qwen3 8B, the fallback brain)
if /i "%ACTION%"=="test" goto dispatch
if /i "%ACTION%"=="test-full" goto dispatch
if /i "%ACTION%"=="check" goto dispatch
where ollama >nul 2>&1
if errorlevel 1 goto dispatch
ollama list >nul 2>&1
if not errorlevel 1 goto dispatch
echo Starting Ollama ...
start "Ollama" /min ollama serve
set /a TRIES=0
:wait_ollama
ping -n 3 127.0.0.1 >nul
ollama list >nul 2>&1
if not errorlevel 1 goto dispatch
set /a TRIES+=1
if %TRIES% LSS 15 goto wait_ollama
echo Ollama did not start: no Qwen fallback. LLM steps use the Claude models the API key can access, or are skipped.

rem ---------------------------------------------------------------- actions
:dispatch
if /i "%ACTION%"=="check" goto check
if /i "%ACTION%"=="test" goto run_tests
if /i "%ACTION%"=="test-full" goto run_tests
if /i "%ACTION%"=="pilot-c" goto pilot_c
if /i "%ACTION%"=="pilot-cpp" goto pilot_cpp
if /i "%ACTION%"=="pilot-deps" goto pilot_deps
if /i "%ACTION%"=="pilot-embedded" goto pilot_embedded
if /i "%ACTION%"=="pilot-embedded-cpp" goto pilot_embedded_cpp
if /i "%ACTION%"=="benchmark" goto benchmark
if /i "%ACTION%"=="run" goto run_repo
echo Unknown option "%ACTION%". Use: setup, check, test, test-full, baseagent-test, pilot-c, pilot-cpp, pilot-deps, pilot-embedded, pilot-embedded-cpp, benchmark, view RUN_FOLDER or run REPO SUT.
set "UT_EXIT=2"
goto end

:check
echo.
"%PY%" ut_pipeline.py check --config ut_config.json
set "UT_EXIT=%errorlevel%"
goto end

:run_tests
rem  TP-20: "test" runs the component tests; "test-full" every test, spread over parallel processes.
rem  TP-21: no test reaches a real brain (API key, Ollama), whatever this PC has.
set "UT_TESTS=quick"
if /i "%ACTION%"=="test-full" set "UT_TESTS=full"
echo.
if /i "%ACTION%"=="test-full" echo Running every test in parallel, including whole pipeline runs ...
if /i not "%ACTION%"=="test-full" echo Running the component tests ...
> "%RESULTS%" echo ==== Unit-test generator tests (%ACTION%) %date% %time% ====
"%PY%" ut_pipeline.py check --config ut_config.json >> "%RESULTS%" 2>&1
>> "%RESULTS%" echo.
pushd "%TESTS%"
if /i "%ACTION%"=="test-full" goto tests_full
"%PY%" -u -m unittest -v test_unittest_generator >> "%RESULTS%" 2>&1
set "UT_EXIT=%errorlevel%"
goto tests_done
:tests_full
"%PY%" -u test_unittest_generator.py --parallel >> "%RESULTS%" 2>&1
set "UT_EXIT=%errorlevel%"
:tests_done
popd
echo.
echo ---- summary ----
findstr /b /c:"FAIL:" /c:"ERROR:" "%RESULTS%"
if /i "%ACTION%"=="test-full" findstr /b /c:"Summary:" "%RESULTS%"
if /i not "%ACTION%"=="test-full" findstr /r /c:"^Ran [0-9]* test" /c:"^OK" /c:"^FAILED" "%RESULTS%"
echo Full results: %RESULTS%
if not "%UT_EXIT%"=="0" echo Some tests failed. Send test_results.txt to Claude.
goto end

:baseagent_tests
rem  TP-24: the BaseAgent tests with this application's Python environment (baseagent.bat test is unchanged).
set "BA_RESULTS=%~dp0test_results_baseagent.txt"
echo.
echo Running the BaseAgent tests ...
pushd "%TESTS%"
"%PY%" -u -m unittest -v test_baseagent > "%BA_RESULTS%" 2>&1
set "UT_EXIT=%errorlevel%"
popd
findstr /b /c:"FAIL:" /c:"ERROR:" "%BA_RESULTS%"
findstr /r /c:"^Ran [0-9]* test" /c:"^OK" /c:"^FAILED" "%BA_RESULTS%"
echo Full results: %BA_RESULTS%
goto end

:benchmark
echo.
echo Running the seeded-defect benchmark - one full pipeline run per case ...
"%PY%" ut_pipeline.py benchmark --config ut_config.json --knowledge %KNOWLEDGE%
set "UT_EXIT=%errorlevel%"
goto end

:pilot_c
set "PILOT=c_pilot"
set "SUTS=--sut src\thermostat.c"
goto pilot
:pilot_cpp
set "PILOT=cpp_pilot"
set "SUTS=--sut src\rate_limiter.cpp --sut src\steering_monitor.cpp --sut src\torque_map.cpp"
goto pilot
:pilot_deps
set "PILOT=c_deps_pilot"
set "SUTS=--sut src\ctrl.c --sut src\brake_ctrl.c --sut src\steer_assist.c"
goto pilot
:pilot_embedded
set "PILOT=c_embedded_pilot"
set "SUTS=--sut src\gpio_driver.c --sut src\scheduler.c --sut src\critical.c --sut src\main_loop.c"
goto pilot
:pilot_embedded_cpp
set "PILOT=cpp_embedded_pilot"
set "SUTS=--sut src\motor_ctrl.cpp --sut src\counter.cpp --sut src\app_main.cpp --sut include\ring_buffer.hpp"
:pilot
rem  The pipeline writes the generated tests into the repository, so it runs on a fresh copy.
set "REPO=%~dp0ut_output\pilots\%PILOT%"
if exist "%REPO%" rmdir /s /q "%REPO%"
xcopy "%PILOTS%\%PILOT%" "%REPO%\" /E /I /Q /Y >nul
echo.
echo Running the pipeline on %REPO% - this can take several minutes ...
"%PY%" ut_pipeline.py run --repo "%REPO%" %SUTS% --config ut_config.json %EXTRA%
set "UT_EXIT=%errorlevel%"
goto end

:view_run
if not defined VIEWDIR (echo Usage: unittest_generator.bat view RUN_FOLDER & set "UT_EXIT=2" & goto end)
"%PY%" ut_pipeline.py view "%VIEWDIR%"
set "UT_EXIT=%errorlevel%"
goto end

:run_repo
if not defined REPO goto run_usage
if not defined SUT goto run_usage
set "SUT=%SUT:"=%"
echo.
echo Running the pipeline on %REPO% for %SUT% ...
set "BRANCH_ARG="
if defined BRANCH set BRANCH_ARG=--branch "%BRANCH%"
"%PY%" ut_pipeline.py run --repo "%REPO%" --sut "%SUT%" --config ut_config.json %BRANCH_ARG% %EXTRA%
set "UT_EXIT=%errorlevel%"
goto end
:run_usage
echo Usage: unittest_generator.bat run REPO SUT [--branch NAME] [--sut FILE] [--build-command "..."] [--compile-commands FILE]
echo        REPO is a git repository link or a folder.
set "UT_EXIT=2"
goto end

:end
if not defined UT_EXIT set "UT_EXIT=0"
if defined INTERACTIVE pause
endlocal & exit /b %UT_EXIT%
