# Packs the PyInstaller output into an installable, Store-ready MSIX.
#
#   .\packaging\build-msix.ps1
#   .\packaging\build-msix.ps1 -IdentityName "12345Publisher.CharacterEditor" `
#       -Publisher "CN=ABCDEF12-3456-..." -PublisherDisplay "Your Name" -Version 1.0.1.0
#
# Requires the Windows 10/11 SDK (for makeappx.exe). Run build.ps1 first.
# The unsigned .msix is what you upload to the Microsoft Store (Partner Center
# signs it). To install it locally for testing it must be signed - see the
# instructions printed at the end.
param(
    [string]$IdentityName = "",
    [string]$Publisher = "",
    [string]$PublisherDisplay = "",
    [string]$Version = ""
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$dist = Join-Path $here "dist\CharacterEditor"
if (-not (Test-Path (Join-Path $dist "CharacterEditor.exe"))) {
    throw "No build found - run packaging\build.ps1 first."
}

# Locate makeappx.exe in the Windows SDK (newest kit wins).
$makeappx = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\makeappx.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $makeappx) {
    $cmd = Get-Command makeappx.exe -ErrorAction SilentlyContinue
    if ($cmd) { $makeappx = $cmd.Source }
}
if (-not $makeappx) {
    throw "makeappx.exe not found - install the Windows 10/11 SDK (via Visual Studio Installer or https://developer.microsoft.com/windows/downloads/windows-sdk/)."
}

# Stage: app folder + tile assets + manifest.
$staging = Join-Path $here "msix\staging"
if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Force "$staging\Assets" | Out-Null
Copy-Item $dist "$staging\CharacterEditor" -Recurse
Copy-Item (Join-Path $here "assets\Square150x150Logo.png") "$staging\Assets"
Copy-Item (Join-Path $here "assets\Square44x44Logo.png") "$staging\Assets"
Copy-Item (Join-Path $here "assets\Wide310x150Logo.png") "$staging\Assets"
Copy-Item (Join-Path $here "assets\StoreLogo.png") "$staging\Assets"

$manifest = Get-Content (Join-Path $here "msix\AppxManifest.xml") -Raw
if ($IdentityName) { $manifest = $manifest -replace 'Name="CharacterEditor.App"', "Name=`"$IdentityName`"" }
if ($Publisher) { $manifest = $manifest -replace 'Publisher="CN=CHANGE-ME"', "Publisher=`"$Publisher`"" }
if ($PublisherDisplay) { $manifest = $manifest -replace '<PublisherDisplayName>CHANGE-ME</PublisherDisplayName>', "<PublisherDisplayName>$PublisherDisplay</PublisherDisplayName>" }
if ($Version) { $manifest = $manifest -replace 'Version="1.0.0.0"', "Version=`"$Version`"" }
Set-Content -Path "$staging\AppxManifest.xml" -Value $manifest -Encoding utf8

$out = Join-Path $here "dist\CharacterEditor.msix"
if (Test-Path $out) { Remove-Item $out -Force }
& $makeappx pack /d $staging /p $out /o
if ($LASTEXITCODE -ne 0) { throw "makeappx failed" }

Write-Host ""
Write-Host "Done: $out"
Write-Host ""
Write-Host "To upload to the Microsoft Store: reserve the app name in Partner Center,"
Write-Host "copy the Identity Name / Publisher values it assigns into this script's"
Write-Host "parameters, rebuild, and upload the .msix - the Store signs it for you."
Write-Host ""
Write-Host "To install locally for testing, sign it with a self-signed cert:"
Write-Host '  $cert = New-SelfSignedCertificate -Type Custom -Subject "CN=CHANGE-ME" -KeyUsage DigitalSignature -FriendlyName "CE dev" -CertStoreLocation Cert:\CurrentUser\My -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3")'
Write-Host '  signtool sign /fd SHA256 /a /sha1 $cert.Thumbprint packaging\dist\CharacterEditor.msix'
Write-Host "then import the cert into Trusted People and double-click the .msix."
