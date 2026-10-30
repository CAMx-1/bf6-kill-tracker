; BF6 Kill Tracker installer (Inno Setup 6). Built in CI:
;   iscc /DAppVersion=0.4.0 packaging\installer.iss
; Installs per user (no admin prompt), so the in-app updater can run it silently.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6B1F7D2E-3C4A-4F5B-9E8D-BF6C0FFEE001}
AppName=BF6 Kill Tracker
AppVersion={#AppVersion}
AppPublisher=CAMx-1
AppPublisherURL=https://github.com/CAMx-1/bf6-kill-tracker
AppUpdatesURL=https://github.com/CAMx-1/bf6-kill-tracker/releases
DefaultDirName={localappdata}\Programs\BF6 Kill Tracker
DefaultGroupName=BF6 Kill Tracker
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=BF6KillTracker-setup
UninstallDisplayIcon={app}\BF6KillTracker.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\BF6KillTracker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; old program files from a previous version (your scans, settings and sounds live elsewhere and are kept)
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\BF6 Kill Tracker"; Filename: "{app}\BF6KillTracker.exe"
Name: "{group}\Uninstall BF6 Kill Tracker"; Filename: "{uninstallexe}"
Name: "{userdesktop}\BF6 Kill Tracker"; Filename: "{app}\BF6KillTracker.exe"; Tasks: desktopicon

[Run]
; also runs after a silent (in-app) update, so the app comes back by itself
Filename: "{app}\BF6KillTracker.exe"; Description: "Start BF6 Kill Tracker"; Flags: nowait postinstall
