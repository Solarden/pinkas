; pinkas for Windows: a per-user install of PyInstaller's folder, built by packaging/build.py.
; No administrator: Postgres refuses to run elevated, so the app and its install never are.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{1C4C3893-8BAE-4F2F-9BCF-566B5153B8AA}
AppName=pinkas
AppVersion={#AppVersion}
; SignPath signs only an installer whose metadata names this product and version.
VersionInfoProductName=pinkas
VersionInfoVersion={#AppVersion}
AppPublisher=pinkas
AppPublisherURL=https://github.com/Solarden/pinkas
DefaultDirName={localappdata}\Programs\pinkas
DefaultGroupName=pinkas
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\build
OutputBaseFilename=pinkas-setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\pinkas.exe
; A running pinkas holds its files and its database: setup and uninstall ask to close it first, by the mutex
; pinkas.exe holds while it runs (pinkas/desktop.py, APP_MUTEX).
AppMutex=pinkas
CloseApplications=yes
RestartApplications=no

[InstallDelete]
; An update replaces the whole folder: a file the new build no longer has must not linger.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\build\pyinstaller\dist\pinkas\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\pinkas"; Filename: "{app}\pinkas.exe"

[Run]
Filename: "{app}\pinkas.exe"; Description: "Start pinkas"; Flags: nowait postinstall skipifsilent

[Code]
// The data stays in %LOCALAPPDATA%\pinkas unless the friend says otherwise: an uninstall before a reinstall
// must not cost them their characters.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
    if MsgBox('Also delete your pinkas data (characters, notes, routes, settings)?', mbConfirmation,
              MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{localappdata}\pinkas'), True, True, True);
end;
