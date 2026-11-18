[CmdletBinding(DefaultParameterSetName = 'Official')]
param(
    [string]$OutputDir,
    [Parameter(ParameterSetName = 'Official')]
    [string]$WebView2Package,
    [Parameter(Mandatory, ParameterSetName = 'LocalSdk')]
    [string]$WebView2SdkDir,
    [Parameter(ParameterSetName = 'LocalSdk')]
    [string]$WebView2License
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'The desktop executable must be built on Windows.' }
$ccawRoot = Split-Path -Parent $PSScriptRoot
$ccawDesktop = Join-Path $ccawRoot 'desktop'
$ccawDist = Join-Path $ccawRoot 'dist'
if (-not $OutputDir) { $OutputDir = Join-Path $ccawDist 'desktop' }
$ccawDestination = [IO.Path]::GetFullPath($OutputDir)
$ccawVersion = '1.0.3485.44'
$ccawPackageHash = 'BC09150B179246AC90189649B13BE8E6B11B3AC200E817E18DF106E1F3CF489E'
$ccawPackageName = "microsoft.web.webview2.$ccawVersion.nupkg"
$ccawVswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$ccawReferenceDir = Join-Path ${env:ProgramFiles(x86)} 'Reference Assemblies\Microsoft\Framework\.NETFramework\v4.8'
if (-not (Test-Path -LiteralPath $ccawVswhere -PathType Leaf)) {
    throw 'Install Visual Studio or Build Tools with the C# compiler and .NET Framework 4.8 targeting pack.'
}
$ccawInstall = & $ccawVswhere -latest -products '*' -requires Microsoft.Component.MSBuild -property installationPath
if ($LASTEXITCODE -ne 0 -or -not $ccawInstall) { throw 'No Visual Studio installation with MSBuild was found.' }
$ccawCompiler = Join-Path $ccawInstall 'MSBuild\Current\Bin\Roslyn\csc.exe'
if (-not (Test-Path -LiteralPath $ccawCompiler -PathType Leaf)) { throw 'The Visual Studio C# compiler was not found.' }
$ccawReferences = foreach ($ccawFile in @(
    'mscorlib.dll', 'System.dll', 'System.Core.dll', 'System.Web.Extensions.dll',
    'PresentationCore.dll', 'PresentationFramework.dll', 'WindowsBase.dll', 'System.Xaml.dll'
)) {
    $ccawPath = Join-Path $ccawReferenceDir $ccawFile
    if (-not (Test-Path -LiteralPath $ccawPath -PathType Leaf)) { throw "Missing .NET Framework 4.8 reference: $ccawFile" }
    "/reference:$ccawPath"
}

$ccawScratch = (New-Item -ItemType Directory -Path (Join-Path $ccawDist ('.desktop-build-' + [guid]::NewGuid().ToString('N'))) -Force).FullName
$ccawCoreName = 'Microsoft.Web.WebView2.Core.dll'
$ccawWpfName = 'Microsoft.Web.WebView2.Wpf.dll'
$ccawLoaderName = 'WebView2Loader.dll'
$ccawLicense = $null
if ($PSCmdlet.ParameterSetName -eq 'LocalSdk') {
    $ccawCore = Join-Path $WebView2SdkDir $ccawCoreName
    $ccawWpf = Join-Path $WebView2SdkDir $ccawWpfName
    $ccawLoader = Join-Path $WebView2SdkDir $ccawLoaderName
    if ($WebView2License) { $ccawLicense = (Get-Item -LiteralPath $WebView2License).FullName }
    $ccawSdkSource = 'local SDK'
} else {
    if (-not $WebView2Package) {
        $WebView2Package = Join-Path $ccawScratch $ccawPackageName
        $ccawUri = "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/$ccawVersion/$ccawPackageName"
        Invoke-WebRequest -UseBasicParsing -Uri $ccawUri -OutFile $WebView2Package
    }
    $ccawPackagePath = (Get-Item -LiteralPath $WebView2Package).FullName
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $ccawPackagePath).Hash -ne $ccawPackageHash) {
        throw 'The WebView2 NuGet package does not match the expected SHA-256.'
    }
    $ccawExtracted = Join-Path $ccawScratch 'sdk'
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($ccawPackagePath, $ccawExtracted)
    $ccawCore = Join-Path $ccawExtracted "lib\net462\$ccawCoreName"
    $ccawWpf = Join-Path $ccawExtracted "lib\net462\$ccawWpfName"
    $ccawLoader = Join-Path $ccawExtracted "runtimes\win-x64\native\$ccawLoaderName"
    if (-not (Test-Path -LiteralPath $ccawLoader -PathType Leaf)) {
        $ccawLoader = Join-Path $ccawExtracted "build\native\x64\$ccawLoaderName"
    }
    $ccawLicense = Join-Path $ccawExtracted 'LICENSE.txt'
    $ccawSdkSource = "$ccawPackageName"
}

foreach ($ccawComponent in @($ccawCore, $ccawWpf, $ccawLoader)) {
    if (-not (Test-Path -LiteralPath $ccawComponent -PathType Leaf)) { throw "Missing WebView2 component: $ccawComponent" }
}
if ($ccawLicense -and -not (Test-Path -LiteralPath $ccawLicense -PathType Leaf)) { throw 'The official SDK license was not found.' }
if (-not $ccawLicense -and (Test-Path -LiteralPath (Join-Path $ccawDestination 'WEBVIEW2-LICENSE.txt'))) {
    throw 'Choose a new output directory for a local SDK build without its redistribution license.'
}

$ccawExeName = 'CCAW.Desktop.exe'
$ccawExe = Join-Path $ccawScratch $ccawExeName
$ccawReferences += "/reference:$ccawCore", "/reference:$ccawWpf"
$ccawProgram = Join-Path $ccawDesktop 'Program.cs'
$ccawManifest = Join-Path $ccawDesktop 'app.manifest'
$ccawAssets = @('index.html', 'app.css', 'app.js')
foreach ($ccawAsset in $ccawAssets) {
    if (-not (Test-Path -LiteralPath (Join-Path $ccawDesktop "assets\$ccawAsset") -PathType Leaf)) {
        throw "Missing desktop source asset: $ccawAsset"
    }
}
& $ccawCompiler /nologo /noconfig /nostdlib+ /target:winexe /platform:x64 /optimize+ /deterministic+ /utf8output `
    "/out:$ccawExe" "/win32manifest:$ccawManifest" @ccawReferences $ccawProgram
if ($LASTEXITCODE -ne 0) { throw "Desktop compilation failed with exit code $LASTEXITCODE." }

[void](New-Item -ItemType Directory -Path (Join-Path $ccawDestination 'assets') -Force)
foreach ($ccawAsset in $ccawAssets) {
    Copy-Item -LiteralPath (Join-Path $ccawDesktop "assets\$ccawAsset") -Destination (Join-Path $ccawDestination 'assets')
}
Copy-Item -LiteralPath $ccawCore, $ccawWpf, $ccawLoader -Destination $ccawDestination
if ($ccawLicense) {
    Copy-Item -LiteralPath $ccawLicense -Destination (Join-Path $ccawDestination 'WEBVIEW2-LICENSE.txt')
}
Copy-Item -LiteralPath $ccawExe -Destination $ccawDestination
Write-Output ("Built {0} with {1}" -f (Join-Path $ccawDestination $ccawExeName), $ccawSdkSource)
