param([string]$Destination = 'C:\CCAW')
$ErrorActionPreference = 'Stop'
$source = $PSScriptRoot
$target = [IO.Path]::GetFullPath($Destination)
$python = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -c "import sys; assert sys.version_info >= (3,11)"
    if ($LASTEXITCODE -eq 0) { $python = @('py', '-3') }
}
if (-not $python -and (Get-Command python -ErrorAction SilentlyContinue)) {
    & python -c "import sys; assert sys.version_info >= (3,11)"
    if ($LASTEXITCODE -eq 0) { $python = @('python') }
}
if (-not $python) { throw 'Install Python 3.11 or newer, then rerun this script.' }
if ($source.TrimEnd('\') -ne $target.TrimEnd('\')) {
    $productPaths = @('.gitignore', 'AGENTS.md', 'README.md', 'pyproject.toml', 'Install.ps1', 'ccaw.ps1', 'ccaw', 'tests', 'docs', 'scripts', 'examples', '.github')
    foreach ($name in $productPaths) {
        $item = Join-Path $source $name
        if ($target.Equals($item, [StringComparison]::OrdinalIgnoreCase) -or $target.StartsWith($item + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Choose an installation directory outside the product source directories.'
        }
    }
    if (Test-Path -LiteralPath $target) {
        if (Get-ChildItem -LiteralPath $target -Force | Select-Object -First 1) {
            throw 'Target folder is not empty. Extract into a new subfolder or run Install.ps1 from the project folder. No files were overwritten.'
        }
    } else {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
    }
    foreach ($name in $productPaths) {
        $item = Join-Path $source $name
        if (Test-Path -LiteralPath $item) { Copy-Item -LiteralPath $item -Destination $target -Recurse }
    }
}
Push-Location -LiteralPath $target
try {
    if (-not (Test-Path '.venv\Scripts\python.exe')) {
        if ($python.Length -eq 2) { & $python[0] $python[1] -m venv .venv } else { & $python[0] -m venv .venv }
        if ($LASTEXITCODE -ne 0) { throw 'Failed to create the Python environment.' }
    }
    & '.\.venv\Scripts\python.exe' -m ccaw --home (Join-Path $target '.ccaw') init
    if ($LASTEXITCODE -ne 0) { throw 'Initialization failed.' }
    & '.\.venv\Scripts\python.exe' -m ccaw --home (Join-Path $target '.ccaw') doctor
    if ($LASTEXITCODE -ne 0) { throw 'Environment check failed. Review the error above before using CCAW.' }
    Write-Host "Installed at: $target"
    Write-Host 'Try: .\ccaw.ps1 task list'
    Write-Host 'Offline demonstration: .\ccaw.ps1 demo'
} finally {
    Pop-Location
}
