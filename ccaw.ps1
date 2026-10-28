$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run Install.ps1 first.' }
$previous = $env:PYTHONPATH
$env:PYTHONPATH = $PSScriptRoot + [IO.Path]::PathSeparator + $previous
try {
    if ($args.Count -gt 0 -and $args[0] -eq 'demo') {
        $commandArguments = @((Join-Path $PSScriptRoot 'examples\offline_demo.py'))
    } else {
        $commandArguments = @('-m', 'ccaw', '--home', (Join-Path $PSScriptRoot '.ccaw')) + @($args)
    }
    # Windows PowerShell's native argument forwarding drops embedded quotes and empty arguments.
    $quotedArguments = foreach ($argument in $commandArguments) {
        $escaped = [regex]::Replace([string]$argument, '(\\*)"', '$1$1\"')
        '"' + [regex]::Replace($escaped, '(\\+)$', '$1$1') + '"'
    }
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $python
    $startInfo.Arguments = $quotedArguments -join ' '
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = [Text.Encoding]::UTF8
    $startInfo.StandardErrorEncoding = [Text.Encoding]::UTF8
    $startInfo.EnvironmentVariables['PYTHONUTF8'] = '1'
    $process = [System.Diagnostics.Process]::Start($startInfo)
    try {
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $outputText = $stdout.GetAwaiter().GetResult()
        $errorText = $stderr.GetAwaiter().GetResult()
        if ($outputText.Length -gt 0) { Write-Output $outputText }
        if ($errorText.Length -gt 0) { [Console]::Error.Write($errorText) }
        $result = $process.ExitCode
    } finally {
        $process.Dispose()
    }
} finally {
    $env:PYTHONPATH = $previous
}
exit $result
