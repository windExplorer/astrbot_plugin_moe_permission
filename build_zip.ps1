# Package astrbot_plugin_moe_permission into an AstrBot-installable zip.
#
# Design notes (learned from sibling plugins, keep them):
#   1) EXPLICIT include list. New top-level .py modules MUST be added here too,
#      otherwise the repo has the file but the released zip silently misses it
#      (the feature then just does not work for users).
#   2) Refuse to overwrite an existing zip of the same version: bump the version
#      in metadata.yaml (and add a CHANGELOG entry) before repacking. Historical
#      zips in dist/ are kept forever so users can roll back.
#   3) Archive layout: wrapped in a top-level folder named after the plugin, with
#      explicit directory entries and forward slashes. Do NOT use PowerShell
#      Compress-Archive (it omits directory entries).
#   4) dist/ is git-ignored: the zip is a local install artifact, not source.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File .\build_zip.ps1
#
# NOTE: keep this file ASCII-only. PS 5.1 on a non-UTF8 code page can mis-parse
#       UTF-8 no-BOM files that contain CJK comments.

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

if ($PSScriptRoot) {
    $root = $PSScriptRoot
} elseif ($MyInvocation.MyCommand.Path) {
    $root = Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
    $root = (Get-Location).Path
}

$pluginName = "astrbot_plugin_moe_permission"
$distDir = Join-Path $root "dist"

# --- version from metadata.yaml (UTF8) ---
$metaPath = Join-Path $root "metadata.yaml"
if (-not (Test-Path $metaPath)) {
    Write-Host "ERROR: metadata.yaml not found" -ForegroundColor Red
    exit 1
}
$metaRaw = Get-Content -Raw -Encoding UTF8 $metaPath
$version = ""
if ($metaRaw -match '(?m)^\s*version:\s*v?([0-9][^\s#]*)') {
    $version = $Matches[1].Trim()
}
if (-not $version) {
    Write-Host "ERROR: cannot read version from metadata.yaml" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $distDir)) {
    New-Item -ItemType Directory -Path $distDir | Out-Null
}
$zipName = "${pluginName}_v${version}.zip"
$zipPath = Join-Path $distDir $zipName

if (Test-Path $zipPath) {
    Write-Host "ERROR: $zipName already exists. Bump 'version' in metadata.yaml and add a CHANGELOG entry before repacking." -ForegroundColor Red
    exit 1
}

# --- files / directories that go into the release ---
# Keep this in sync with main.py's import list and the modules under this folder.
$includeList = @(
    "main.py",
    "store.py",
    "webui_api.py",
    "gate.py",
    "quota.py",
    "sync.py",
    "avatar.py",
    "model_switch.py",   # /switch-model command flow (v1.3.3)
    "model_card.py",     # Pillow card renderer (v1.3.3)
    "recall.py",         # auto-recall temporary messages (v1.3.9)
    "detect.py",         # /switch-model-detect: proxies model_panel detection (v1.3.18)
    "rename_migration.py",  # one-shot user_gateway -> moe_permission data/config migration (v1.5.0)
    "provider_refs.py",     # stale model reference report / prune (v1.5.1)
    "_conf_schema.json",
    "metadata.yaml",
    "requirements.txt",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "logo.png",   # plugin logo shown in the AstrBot plugin list (docs: root logo.png)
    "assets",     # bundled CJK font for the switch-model card (OFL licensed)
    "pages"
)

# --- self-check: every top-level .py in the repo MUST be listed above ---
# This guards a trap that was hit for real in v0.2.0: three new modules (gate/quota/sync)
# were added to the repo but not to this list, so the released zip shipped without them
# and users saw "No module named 'quota'" instead of an obvious packaging error.
# The failure is silent at build time and confusing at install time - so fail loudly here.
$topPy = @(Get-ChildItem $root -Filter *.py -File | Select-Object -ExpandProperty Name)
$notListed = @($topPy | Where-Object { $includeList -notcontains $_ })
if ($notListed.Count -gt 0) {
    Write-Host ("ERROR: top-level module(s) missing from includeList: " + ($notListed -join ", ")) -ForegroundColor Red
    Write-Host "       Add them to the list at the top of this script before packaging." -ForegroundColor Red
    exit 1
}
Write-Host ("includeList check OK: all " + $topPy.Count + " top-level .py files are listed")

# Built console must exist before packaging (run build_webui.ps1 first).
$consoleIndex = Join-Path $root "pages/permission-console/index.html"
if (-not (Test-Path $consoleIndex)) {
    Write-Host "ERROR: pages/permission-console/index.html missing. Run .\build_webui.ps1 first." -ForegroundColor Red
    exit 1
}

function Add-ItemToZip($zip, $fsPath, $entryPath) {
    if (Test-Path -PathType Container $fsPath) {
        # directory entry must end with '/'
        $null = $zip.CreateEntry($entryPath + "/")
        Get-ChildItem $fsPath |
            Where-Object { $_.Name -ne "__pycache__" -and $_.Extension -notin @(".pyc", ".pyo") } |
            ForEach-Object {
                Add-ItemToZip $zip $_.FullName ($entryPath + "/" + $_.Name)
            }
    } else {
        # entries are named with forward slashes so Linux-side zipfile sees a real tree
        $entry = $zip.CreateEntry($entryPath, [System.IO.Compression.CompressionLevel]::Optimal)
        $stream = $entry.Open()
        try {
            $bytes = [System.IO.File]::ReadAllBytes($fsPath)
            $stream.Write($bytes, 0, $bytes.Length)
        } finally {
            $stream.Dispose()
        }
    }
}

$fs = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::Create)
$zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Create, $false)
try {
    foreach ($name in $includeList) {
        $fsPath = Join-Path $root $name
        if (-not (Test-Path $fsPath)) {
            Write-Host "ERROR: missing required item: $name" -ForegroundColor Red
            $zip.Dispose(); $fs.Dispose()
            Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
            exit 1
        }
        Add-ItemToZip $zip $fsPath ($pluginName + "/" + $name)
    }
} finally {
    $zip.Dispose()
    $fs.Dispose()
}

# NOTE: $zip.Entries.Count is 0 while the archive is still open for Create,
# so count with tar instead (also serves as a readability check of the layout).
$entries = @(tar -tf $zipPath)
$sizeKb = [math]::Round((Get-Item $zipPath).Length / 1024, 1)
Write-Host "Packaged: $zipPath" -ForegroundColor Green
Write-Host ("Entries : {0}   Size: {1} KB" -f $entries.Count, $sizeKb)

# --- verify the archive layout (top level must be exactly the plugin folder) ---
# @() is required: with a single top-level name, Sort-Object returns a string and
# $top[0] would then address the first *character*.
$top = @($entries | ForEach-Object { ($_ -split '/')[0] } | Sort-Object -Unique)
Write-Host ("Top-level: " + ($top -join ","))
if ($top.Count -eq 1 -and $top[0] -eq $pluginName) {
    Write-Host "OK: wrapped layout as expected." -ForegroundColor Green
} else {
    Write-Host "WARN: unexpected archive layout, check packaging!" -ForegroundColor Yellow
}
