@echo off
chcp 936 >nul
cd /d "%~dp0"

echo ============================================
echo   体检之后 —— 本地服务器
echo ============================================
echo.

rem ── 找 Python ──────────────────────────────────────────────
rem 顺序：环境变量 PY（可选，指向你想用的 python.exe）→ PATH 里的 python
rem 以前这里写死了开发者本机的绝对路径，别人拿到仓库双击必然失败。
if defined PY goto :have_py
set "PY=python"

:have_py
"%PY%" -c "import sys; print(sys.version.split()[0])" >nul 2>&1
if errorlevel 1 (
    echo [错误] 没有找到可用的 Python。
    echo.
    echo   试过：%PY%
    echo.
    echo 请先安装 Python 3.11 或更高版本，并勾选 "Add Python to PATH"；
    echo 或者用记事本打开本文件，在最上面加一行：
    echo     set PY=你的python.exe完整路径
    echo （conda 环境通常在 ...\envs\环境名\python.exe）
    echo.
    pause
    exit /b 1
)

rem ── 检查依赖装没装 ────────────────────────────────────────
rem 只查 uvicorn 和 rapidocr 两个：前者没有服务起不来，
rem 后者没有则上传报告时会报「未安装 rapidocr-onnxruntime」。
"%PY%" -c "import uvicorn, rapidocr_onnxruntime" >nul 2>&1
if errorlevel 1 (
    echo [提示] 这个 Python 里还缺依赖，现在开始安装...
    echo         命令：%PY% -m pip install -r requirements.txt
    echo.
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败。请检查网络后，手动执行：
        echo     %PY% -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
    echo.
    echo 依赖安装完成。
    echo.
)

echo 正在启动，请稍候...
echo 浏览器会在服务就绪后自动打开 http://127.0.0.1:8000/（通常 3-6 秒）
echo.
echo 提示：如果浏览器打不开，多半是系统代理拦截了本机地址，
echo       请在浏览器的代理设置里给 127.0.0.1 或 localhost 加一条例外。
echo.
echo 要停止服务，直接关闭这个黑窗口即可。
echo.

rem 等端口真的通了再开浏览器：实测启动耗时在 3.3～6 秒之间浮动
rem （受磁盘和首次导入影响），写死几秒必然有一半概率开早了。
rem curl 会一直重试到连上为止（最多 60 秒），连不上也会开，不会让你
rem 因为机器慢就什么都看不到。/b 表示在当前窗口后台跑，不额外闪黑框。
start "" /b cmd /c "%SystemRoot%\System32\curl.exe -s -o nul --retry 60 --retry-delay 1 --retry-connrefused http://127.0.0.1:8000/health & start http://127.0.0.1:8000/"

"%PY%" -m uvicorn app.main:app --port 8000

echo.
echo 服务已停止。
pause
