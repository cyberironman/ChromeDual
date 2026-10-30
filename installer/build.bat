@echo off
rem ============================================================
rem  一键构建 ChromeDual 的 exe 与安装包
rem  需要：Python 3.10+，且已 pip install pyinstaller 和 requirements.txt
rem  产物：release\ChromeDual-Setup-x.y.z.exe
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0.."

echo.
echo ==== 检查 Python ====
python -V
if errorlevel 1 (
    echo.
    echo [X] 没找到 python。请先安装 Python 3.10 以上，并勾选 "Add to PATH"。
    pause
    exit /b 1
)

echo.
echo ==== 开始构建（含免安装 zip，大约 5 分钟）====
python installer\build.py --portable
if errorlevel 1 (
    echo.
    echo [X] 构建失败，请把上面的报错发到 Issues。
    pause
    exit /b 1
)

echo.
echo ==== 完成，产物在 release\ ====
pause
