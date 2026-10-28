param([string]$Name = 'ccaw-workflow')
$ErrorActionPreference = 'Stop'
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'Install GitHub CLI (gh) first.' }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Install Git first.' }
& gh auth status
if ($LASTEXITCODE -ne 0) { throw 'Run gh auth login, then retry. Do not paste tokens into chat.' }
$account = (& gh api user --jq '.login').Trim()
if ($LASTEXITCODE -ne 0 -or -not $account) { throw 'Could not verify the signed-in GitHub account.' }
$root = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $root
try {
    if (-not (Test-Path '.git')) {
        & git init -b main
        if ($LASTEXITCODE -ne 0) { throw 'git init failed.' }
    }
    $gitName = & git config user.name
    $gitEmail = & git config user.email
    if (-not $gitName -or -not $gitEmail) { throw 'Configure your Git user.name and user.email before creating the initial commit.' }
    $remotes = @(& git remote)
    if ($remotes -contains 'origin') {
        $origin = & git remote get-url origin
        throw "This project already has origin: $origin. Verify it before pushing; this script will not replace it."
    }
    # Stage only the product's explicit source paths, never an entire user's folder.
    & git add -- .gitignore AGENTS.md README.md pyproject.toml Install.ps1 ccaw.ps1 ccaw tests docs scripts examples .github
    if ($LASTEXITCODE -ne 0) { throw 'git add failed.' }
    & git diff --cached --quiet
    if ($LASTEXITCODE -eq 1) {
        & git commit -m 'Build durable Codex workflow with verified corrective lessons'
        if ($LASTEXITCODE -ne 0) { throw 'Initial commit failed.' }
    }
    $repo = "$account/$Name"
    & gh repo create $repo --private --source . --remote origin --push --description 'Durable, cost-aware Codex workflows with verified engineering lessons'
    if ($LASTEXITCODE -ne 0) { throw 'Repository creation or push failed. Check gh output; do not blindly retry if the remote may already exist.' }
    Write-Host "Created and pushed: https://github.com/$repo"
} finally {
    Pop-Location
}
