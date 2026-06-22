; NexusGrid Windows installer (Inno Setup 6).
;
; Wraps the PyInstaller output (dist\NexusGrid.exe) into a setup.exe that:
;   * shows the standard install-location wizard (default per-user, no admin;
;     the user can switch to all-users / a custom path),
;   * creates Start-Menu (and optional Desktop) shortcuts,
;   * registers an uninstaller in Windows "Apps & features",
;   * on uninstall, offers to also delete the per-user data folder.
;
; The app stores its runtime data in %LOCALAPPDATA%\NexusGrid (see
; nexus/core/paths.py), NOT next to the .exe — so the program files and the
; user's node data are cleanly separated.
;
; Build the installer (needs Inno Setup's compiler, ISCC.exe, on PATH):
;   1) build\build.bat            (produces dist\NexusGrid.exe)
;   2) iscc build\NexusGrid.iss   (produces dist\NexusGrid-Setup-<ver>.exe)
; or run build\build_installer.bat which does both.

#define MyAppName "NexusGrid"
; Version is passed by build_installer.bat (/DMyAppVersion=<ver>, read from
; nexus/__init__.py). The fallback here only applies if ISCC is run by hand.
#ifndef MyAppVersion
  #define MyAppVersion "1.1.0"
#endif
#define MyAppPublisher "NexusGrid"
#define MyAppExeName "NexusGrid.exe"

[Setup]
; A stable, unique AppId so upgrades/uninstall track the same product.
AppId={{A3F1C2E4-7B9D-4E2A-9C1F-2D6B8E4A1C57}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-user install by default (no admin prompt); the wizard lets the user pick
; all-users + Program Files instead.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=NexusGrid-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent

[Code]
{ On uninstall, offer to remove the per-user data folder too. We never delete it
  silently — a user reinstalling later usually wants to keep their node identity
  and data. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\NexusGrid');
    if DirExists(DataDir) then
    begin
      if MsgBox('Also delete your NexusGrid data (node identity, database, and'
        + ' files you stored) at:' + #13#10 + DataDir + #13#10#13#10
        + 'Choose No to keep it for a future reinstall.',
        mbConfirmation, MB_YESNO) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
