# Desktop packaging

Turns Character Editor into a double-clickable Windows app. Three ways to
ship it, all built from the same output:

| Form | What you get | Build with |
|---|---|---|
| Portable exe | `dist\CharacterEditor\` folder — run `CharacterEditor.exe`, zip and share | `build.ps1` |
| Installed app | `dist\CharacterEditor.msix` — double-click installs with a Start-menu entry (needs signing, see below) | `build.ps1` then `build-msix.ps1` |
| Microsoft Store | the same `.msix`, uploaded to Partner Center | `build-msix.ps1` with your Store identity |

## How the app runs

`CharacterEditor.exe` starts the bundled FastAPI server on a free localhost
port and opens the UI in its own chromeless window (Edge/Chrome `--app` mode
with a dedicated profile). Closing the window shuts the server down. If
neither Edge nor Chrome is present it falls back to the default browser.

Everything the UI needs (Three.js, fonts, model-viewer) is bundled — the app
works fully offline. **Blender is still required** for the engine-backed tools
(topology, wrap, rig, face, cleanup, converter); the app detects it on PATH /
standard install folders / `BLENDER_PATH` and each tool shows an install hint
when it's missing. The browser-side tools (Paint, Cloth, Style) work without
it.

User data (job files, projects, logs) is written to
`%LOCALAPPDATA%\CharacterEditor`, never into the install folder.

## Building

```powershell
# 1. Portable app (installs PyInstaller into backend\.venv on first run)
.\packaging\build.ps1

# 2. Optional: MSIX for install / Store (needs the Windows 10/11 SDK)
.\packaging\build-msix.ps1
```

`gen_assets.py` draws the icon and Store tiles from code (no image tools
needed); the build scripts run it automatically.

## Installing the MSIX locally (sideload)

Windows only installs signed packages. For your own machine:

```powershell
$cert = New-SelfSignedCertificate -Type Custom -Subject "CN=CHANGE-ME" `
  -KeyUsage DigitalSignature -FriendlyName "CE dev" `
  -CertStoreLocation Cert:\CurrentUser\My `
  -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3")
signtool sign /fd SHA256 /a /sha1 $cert.Thumbprint packaging\dist\CharacterEditor.msix
```

The certificate subject must match the manifest's `Publisher`
(`build-msix.ps1 -Publisher "CN=CHANGE-ME"`). Import the cert into
*Trusted People* (certmgr), then double-click the `.msix`.

## Publishing to the Microsoft Store

1. Register at [Partner Center](https://partner.microsoft.com/dashboard) and
   reserve the app name.
2. Under *Product management → Product identity*, copy the
   `Package/Identity/Name` and `Package/Identity/Publisher` values.
3. Rebuild with them:
   ```powershell
   .\packaging\build-msix.ps1 -IdentityName "<Identity Name>" `
     -Publisher "<Publisher>" -PublisherDisplay "<Your display name>" -Version 1.0.0.0
   ```
4. Upload `dist\CharacterEditor.msix` in a new submission — do **not** sign it
   yourself; the Store signs it during certification.

Store notes: the package uses the `runFullTrust` capability (it's a classic
desktop app that runs a local server and launches Blender), which is fine for
the Store but is reviewed manually the first time. Mention in the submission
notes that Blender is an optional external dependency the user installs
separately.
