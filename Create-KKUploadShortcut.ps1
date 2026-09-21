param(
    [string]$ShortcutPath = (Join-Path $PSScriptRoot "KKUpload.lnk")
)

$ProjectRoot = $PSScriptRoot
$AppPath = Join-Path $ProjectRoot "app.py"
$IconPath = Join-Path $ProjectRoot "kkupload.ico"
$LocalPythonw = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$LocalPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $AppPath)) {
    throw "Cannot find app.py next to this script."
}

if (Test-Path -LiteralPath $LocalPythonw) {
    $PythonPath = $LocalPythonw
} elseif (Test-Path -LiteralPath $LocalPython) {
    $PythonPath = $LocalPython
} else {
    $PythonCommand = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($null -eq $PythonCommand) {
        $PythonCommand = Get-Command python.exe -ErrorAction Stop
    }
    $PythonPath = $PythonCommand.Source
}

$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $PythonPath
$Shortcut.Arguments = "`"$AppPath`""
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.Description = "Launch KKUpload"

if (Test-Path -LiteralPath $IconPath) {
    $Shortcut.IconLocation = $IconPath
}

$Shortcut.Save()
Write-Output "Created shortcut: $ShortcutPath"
