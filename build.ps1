param(
    [ValidatePattern('^[A-Za-z0-9_-]+$')]
    [string]$AppName = 'EXOCADE'
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' }
}
& $pythonPath -m pip install --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
New-Item -ItemType Directory -Path (Join-Path $PSScriptRoot 'build') -Force | Out-Null
@'
from pathlib import Path
from PySide6 import QtGui
from PIL import Image
application = QtGui.QGuiApplication([])
image = QtGui.QIcon('assets/icon.svg').pixmap(256, 256).toImage()
if not image.save('build/icon.png'):
    raise RuntimeError('Icon rendering failed.')
Image.open('build/icon.png').save('assets/icon.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
'@ | & $pythonPath -
if ($LASTEXITCODE -ne 0) { throw 'Icon creation failed.' }
$arguments = @(
    '--noconfirm', '--windowed', '--onefile',
    '--name', $AppName, '--icon', (Join-Path $PSScriptRoot 'assets\icon.ico'),
    '--add-data', ((Join-Path $PSScriptRoot 'assets') + ';assets'),
    '--distpath', $PSScriptRoot,
    '--workpath', (Join-Path $PSScriptRoot 'build\pyinstaller'),
    '--specpath', (Join-Path $PSScriptRoot 'build'),
    '--exclude-module', 'scipy',
    '--exclude-module', 'matplotlib',
    '--exclude-module', 'tkinter',
    '--exclude-module', 'IPython',
    '--exclude-module', 'PySide6.QtWebEngineCore',
    '--exclude-module', 'PySide6.QtWebEngineWidgets',
    '--exclude-module', 'PySide6.QtWebEngineQuick',
    '--exclude-module', 'PySide6.QtPdf',
    '--exclude-module', 'PySide6.QtPdfWidgets',
    (Join-Path $PSScriptRoot 'app.py')
)
& $pythonPath -m PyInstaller @arguments
if ($LASTEXITCODE -ne 0) { throw 'EXOCADE packaging failed.' }
