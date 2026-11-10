# Build the Vue3 console (permission-console) for astrbot_plugin_moe_permission.
#
# Steps:
#   1) inject the plugin version from metadata.yaml into webui-src/src/version.ts
#      (metadata.yaml is the single source of truth for the version)
#   2) npm install on first build
#   3) vite build -> ../pages/permission-console/
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File .\build_webui.ps1
#
# NOTE: keep this file ASCII-only. PS 5.1 on a non-UTF8 code page can mis-parse
#       UTF-8 no-BOM files that contain CJK comments.

$ErrorActionPreference = "Stop"

if ($PSScriptRoot) {
    $root = $PSScriptRoot
} elseif ($MyInvocation.MyCommand.Path) {
    $root = Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
    $root = (Get-Location).Path
}

$src = Join-Path $root "webui-src"
$out = Join-Path $root "pages/permission-console"

Write-Host "==> Building console (permission-console)..." -ForegroundColor Cyan

if (-not (Test-Path (Join-Path $src "package.json"))) {
    Write-Host "ERROR: $src\package.json not found" -ForegroundColor Red
    exit 1
}

Push-Location $src
try {
    # --- inject version from metadata.yaml (read as UTF8, file has CJK comments) ---
    $metaPath = Join-Path $root "metadata.yaml"
    $pluginVersion = "dev"
    if (Test-Path $metaPath) {
        $metaRaw = Get-Content -Raw -Encoding UTF8 $metaPath
        if ($metaRaw -match '(?m)^\s*version:\s*"?([^"#\r\n]+?)"?\s*(?:#.*)?$') {
            $pluginVersion = $Matches[1].Trim()
        }
    }
    $versionTs = "export const PLUGIN_VERSION = `"$pluginVersion`";`n"
    Set-Content -Path (Join-Path $src "src/version.ts") -Value $versionTs -Encoding UTF8
    Write-Host "==> injected plugin version: $pluginVersion" -ForegroundColor Green

    # --- dependencies on first build ---
    if (-not (Test-Path (Join-Path $src "node_modules"))) {
        Write-Host "==> node_modules missing, running npm install..." -ForegroundColor Yellow
        npm install
        if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
    }

    # --- build ---
    Write-Host "==> vite build..." -ForegroundColor Yellow
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "vite build failed" }
} finally {
    Pop-Location
}

if (-not (Test-Path $out)) {
    Write-Host "ERROR: build finished but output directory is missing: $out" -ForegroundColor Red
    exit 1
}

Write-Host "==> done. output: $out" -ForegroundColor Green
