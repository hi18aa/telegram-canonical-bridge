[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$manifestPath = Join-Path $repoRoot "plugin.yaml"
$versionLine = Select-String -LiteralPath $manifestPath -Pattern '^version:\s*(\S+)\s*$'
if (-not $versionLine) {
    throw "plugin.yaml 缺少 version。"
}
$version = $versionLine.Matches[0].Groups[1].Value
$dist = Join-Path $repoRoot "dist"
$artifact = Join-Path $dist "telegram-canonical-bridge-$version-source.zip"
New-Item -ItemType Directory -Path $dist -Force | Out-Null

$entries = @(
    "README.md",
    "LICENSE",
    "plugin.yaml",
    "pyproject.toml",
    "__init__.py",
    "tools.py",
    "telegram_canonical_bridge",
    "tests",
    "docs",
    "examples",
    "scripts"
)

Push-Location $repoRoot
try {
    & tar.exe -a -cf $artifact --exclude='*/__pycache__' --exclude='*.pyc' @entries
    if ($LASTEXITCODE -ne 0) {
        throw "tar 建立 source artifact 失敗，exit code $LASTEXITCODE。"
    }
}
finally {
    Pop-Location
}

$legacyFiles = & tar.exe -tf $artifact | Select-String -Pattern '(^|/)(hermes_rpc|protocol|service|telegram_api)\.py$|__pycache__|\.pyc$'
if ($legacyFiles) {
    throw "artifact 含 legacy／cache 檔案：$($legacyFiles -join ', ')"
}

$artifact
