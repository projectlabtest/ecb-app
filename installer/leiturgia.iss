; Leiturgia Windows installer (Inno Setup 6/7).
;
; Build:  powershell -File build_installer.ps1
;   1. Assembles installer\stage\ from the release bundle + updater.exe +
;      fresh templates/static.
;   2. Compiles this script into installer_out\Leiturgia-Setup-<version>.exe.
;
; Design notes:
;   - Single Setup EXE: the user never sees the individual files.
;   - Default install dir is C:\Leiturgia so existing installs and the
;     auto-update channel keep working (update_url points at GitHub releases).
;   - license.dat is NEVER bundled: every machine activates with its own key
;     on the activation page after install.
;   - Upgrades keep the machine's license.dat, config.json and data/ DBs
;     (onlyifdoesntexist + uninsneveruninstall); program files and
;     templates/static are refreshed.

#ifndef MyAppVersion
  #define MyAppVersion "1.4.4"
#endif

#define MyAppName "Leiturgia"
#define MyAppPublisher "LIFE HOPE CENTER"
#define MyAppExeName "Leiturgia.exe"

[Setup]
AppId={{d65006ee-e002-4e43-bc7c-3ac65fbf11d5}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName=C:\Leiturgia
DisableProgramGroupPage=yes
PrivilegesRequired=admin
CloseApplications=yes
RestartApplications=no
WizardStyle=modern
SetupIconFile=leiturgia.ico
Compression=lzma2/max
SolidCompression=yes
OutputDir=installer_out
OutputBaseFilename=Leiturgia-Setup-{#MyAppVersion}
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoProductName={#MyAppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Program files: always refreshed.
Source: "stage\Leiturgia.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "stage\LeiturgiaServer.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "stage\updater.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "stage\app.version"; DestDir: "{app}"; Flags: ignoreversion
Source: "stage\templates\*"; DestDir: "{app}\templates"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "stage\static\*"; DestDir: "{app}\static"; Flags: ignoreversion recursesubdirs createallsubdirs
; Per-machine / user files: installed only on fresh setups, kept on upgrade
; and on uninstall.
Source: "stage\config.json"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "stage\data\bible_en.db"; DestDir: "{app}\data"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "stage\data\bible_tl.db"; DestDir: "{app}\data"; Flags: onlyifdoesntexist uninsneveruninstall
; NOTE: license.dat is intentionally NOT shipped. A fresh install boots to the
; activation page where the owner-issued key for that PC is entered.

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
