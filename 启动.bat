@echo off
chcp 936 >nul
cd /d "%~dp0"

echo ============================================
echo   体检之后 —— 开发服务器
echo ============================================
echo.

set PY=D:\aconda3\envs\hwh_ml\python.exe

if not exist "%PY%" (
    echo [错误] 找不到 Python：
    echo   %PY%
    echo.
    echo 请用记事本打开本文件，把 PY= 那一行改成你自己的 python.exe 路径。
    echo （conda 环境通常在 ...\envs\环境名\python.exe）
    echo.
    pause
    exit /b 1
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
