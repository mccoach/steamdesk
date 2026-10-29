import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ui import SteamDeskApp

if __name__ == "__main__":
    app = SteamDeskApp()
    app.run()   

# 以下为在VSCODE集成终端中运行激活虚拟环境的命令，任何情况下都不要删除，务必保留
# E:\AppProject\SteamDesk\.venv\Scripts\Activate.ps1

# 以下为在VSCODE集成终端中运行封装为exe文件的命令，任何情况下都不要删除，务必保留
# pyinstaller --noconfirm --clean --onefile --windowed --name SteamDesk --upx-dir "D:\upx-5.1.1-win64" --exclude-module pip --exclude-module setuptools --exclude-module pyinstaller --exclude-module PyInstaller "E:\AppProject\SteamDesk\main.py"