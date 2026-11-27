<#
.SYNOPSIS
    Stage, commit (if needed), and push the local SpecPatchGuard repository to GitHub.
    Tries SSH first, and automatically falls back to HTTPS if SSH push fails.

.PARAMETER SshUrl
    Primary GitHub SSH URL. Defaults to git@github.com:Zyc654/SpecPatch.git.

.PARAMETER HttpsUrl
    Fallback GitHub HTTPS URL. Defaults to https://github.com/Zyc654/SpecPatch.git.

.PARAMETER Branch
    Target branch name to push (defaults to the current local branch, e.g. master).
    If specified and different from the current branch, the local branch is renamed to match.

.PARAMETER CommitMessage
    Commit message used when there are uncommitted local changes.
    Defaults to "Update SpecPatchGuard".

.PARAMETER RemoteName
    Git remote name (defaults to "origin").

.EXAMPLE
    .\scripts\push_to_github.ps1

.EXAMPLE
    .\scripts\push_to_github.ps1 -Branch main -CommitMessage "Initial upload"
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$SshUrl = "git@github.com:Zyc654/SpecPatch.git",

    [Parameter(Mandatory = $false)]
    [string]$HttpsUrl = "https://github.com/Zyc654/SpecPatch.git",

    [Parameter(Mandatory = $false)]
    [string]$Branch = "",

    [Parameter(Mandatory = $false)]
    [string]$CommitMessage = "Update SpecPatchGuard",

    [Parameter(Mandatory = $false)]
    [string]$RemoteName = "origin"
)

$ErrorActionPreference = "Stop"

function Invoke-GitStrict {
    param([Parameter(Mandatory = $true)][string[]]$Args)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & git @Args
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0) {
        throw "git $($Args -join ' ') failed with exit code $code"
    }
}

function Try-GitPush {
    param(
        [Parameter(Mandatory = $true)][string]$Remote,
        [Parameter(Mandatory = $true)][string]$TargetBranch
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & git push -u $Remote $TargetBranch
        return ($LASTEXITCODE -eq 0)
    }
    finally {
        $ErrorActionPreference = $prev
    }
}

function Set-GitRemoteUrl {
    param(
        [Parameter(Mandatory = $true)][string]$Remote,
        [Parameter(Mandatory = $true)][string]$Url
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $existing = (& git remote get-url $Remote 2>$null)
        $hasRemote = ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($existing))
    }
    finally {
        $ErrorActionPreference = $prev
    }

    if (-not $hasRemote) {
        Write-Host "Adding remote '$Remote' -> $Url" -ForegroundColor Cyan
        Invoke-GitStrict @("remote", "add", $Remote, $Url)
    } elseif ($existing.Trim() -ne $Url) {
        Write-Host "Setting remote '$Remote' -> $Url" -ForegroundColor Cyan
        Invoke-GitStrict @("remote", "set-url", $Remote, $Url)
    } else {
        Write-Host "Remote '$Remote' is already set to $Url" -ForegroundColor DarkGray
    }
}

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Push-Location $RepoRoot
try {
    # 1. Verify git repository
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $inside = (& git rev-parse --is-inside-work-tree 2>$null)
    $insideCode = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($insideCode -ne 0 -or ($inside -as [string]).Trim() -ne "true") {
        throw "Current directory is not a Git repository: $RepoRoot"
    }

    # 2. Safety check: ensure .env is never tracked
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $trackedEnv = (& git ls-files --cached ".env" 2>$null)
    $ErrorActionPreference = $prev
    if ($trackedEnv) {
        throw "Refusing to push: '.env' is tracked in Git index. Run 'git rm --cached .env' first."
    }

    # 3. Resolve target branch
    $currentBranch = (& git symbolic-ref --short HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($currentBranch)) {
        throw "HEAD is detached. Checkout a branch before pushing."
    }
    $currentBranch = $currentBranch.Trim()
    if (-not $Branch) {
        $Branch = $currentBranch
    } elseif ($Branch -ne $currentBranch) {
        Write-Host "Renaming local branch '$currentBranch' -> '$Branch'..." -ForegroundColor Cyan
        Invoke-GitStrict @("branch", "-M", $Branch)
    }

    # 4. Stage and commit any pending changes
    $status = (& git status --porcelain)
    if ($status) {
        Write-Host "Staging and committing local changes..." -ForegroundColor Cyan
        Invoke-GitStrict @("add", "-A")
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $trackedEnvAfterAdd = (& git ls-files --cached ".env" 2>$null)
        $ErrorActionPreference = $prev
        if ($trackedEnvAfterAdd) {
            Invoke-GitStrict @("reset", "HEAD", "--", ".env")
            throw "'.env' was staged and has been unstaged. Check .gitignore before retrying."
        }
        Invoke-GitStrict @("commit", "-m", $CommitMessage)
    } else {
        Write-Host "Working tree clean; pushing existing commits on '$Branch'." -ForegroundColor DarkGray
    }

    # 5. Try SSH first, then fallback to HTTPS
    Write-Host "Attempting push via SSH: $SshUrl" -ForegroundColor Cyan
    Set-GitRemoteUrl -Remote $RemoteName -Url $SshUrl
    if (Try-GitPush -Remote $RemoteName -TargetBranch $Branch) {
        Write-Host "Done! Successfully pushed '$Branch' via SSH." -ForegroundColor Green
        return
    }

    Write-Warning "SSH push failed. Falling back to HTTPS: $HttpsUrl"
    Set-GitRemoteUrl -Remote $RemoteName -Url $HttpsUrl
    if (Try-GitPush -Remote $RemoteName -TargetBranch $Branch) {
        Write-Host "Done! Successfully pushed '$Branch' via HTTPS." -ForegroundColor Green
        return
    }

    throw "Push failed via both SSH ($SshUrl) and HTTPS ($HttpsUrl)."
}
finally {
    Pop-Location
}
