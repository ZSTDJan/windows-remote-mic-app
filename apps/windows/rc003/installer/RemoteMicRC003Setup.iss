; Inno Setup source for Remote Mic · RC003 (Windows source/build
; candidate). Unsigned; see this subtree's README.md and TESTING.md for
; verification and release details.
;
; Hard boundaries enforced by this script:
;   - PrivilegesRequired=lowest (no admin elevation requested, ever).
;   - No [Tasks]/[Icons] entry adds login startup. The installed app exposes
;     an explicit per-user option and uninstall removes only its owned value.
;   - This INSTALLER SCRIPT never installs, configures, silently modifies,
;     or removes VB-CABLE or any other driver, and never elevates itself to
;     do so, during install OR uninstall (XRBM-031 RETRY 1 item 5 - this
;     comment previously and incorrectly claimed VB-CABLE was never
;     referenced anywhere in this project at all). The application frozen
;     under {#DistDir} (packaged wholesale by the [Files] entry below) DOES
;     carry the official, unmodified VB-CABLE Basic package as opaque
;     application data, and its OWN "检查与修复" settings page can
;     optionally launch the vendor's original setup UI, gated behind its
;     own in-app confirmation and a SEPARATE, real Windows UAC prompt -
;     never this installer, never silently, and only after the app is
;     already running and the user has explicitly clicked to do so. Voice
;     output itself is still chosen by the user inside the app.
;   - Frida Gadget is never downloaded by this installer or by the normal
;     candidate build. If a maintainer explicitly fetched the pinned asset
;     before PyInstaller ran, the frozen DistDir may contain it as optional
;     application data; runtime verifies it again before use.

#define AppName "无线麦"
#define AppPublisher "无线麦项目"
#define AppVersion "0.2.0-candidate.1"
#define AppExeName "RemoteMicRC003.exe"
#define AppFolder "RC003"
#define DistDir "..\dist\RemoteMicRC003"

[Setup]
AppId={{B6E8B6F0-7B9B-4B7C-9E7E-3B7B2C6B0F5C}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\RemoteMic\{#AppFolder}
DefaultGroupName=无线麦
UsePreviousGroup=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
SetupIconFile=..\src\ovb_rc003\assets\icons\remote-mic.ico
OutputBaseFilename=RemoteMicRC003Setup-{#AppVersion}-unsigned
OutputDir=..\dist\installer
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
CloseApplications=yes
RestartApplications=no

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "readme-rc003.txt"; DestDir: "{app}"; Flags: isreadme ignoreversion
Source: "..\..\..\..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; DestName: "THIRD_PARTY_NOTICES.md"; Flags: ignoreversion
Source: "..\..\..\..\THIRD_PARTY_SOURCE.md"; DestDir: "{app}"; DestName: "THIRD_PARTY_SOURCE.md"; Flags: ignoreversion
Source: "..\..\..\..\ASSET_LICENSES.md"; DestDir: "{app}"; DestName: "ASSET_LICENSES.md"; Flags: ignoreversion
Source: "..\..\..\..\THIRD_PARTY_LICENSES\*"; DestDir: "{app}\THIRD_PARTY_LICENSES"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\..\..\..\LICENSE.md"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\..\..\..\COPYRIGHT.md"; DestDir: "{app}"; DestName: "COPYRIGHT.txt"; Flags: ignoreversion
; stop-app.ps1 is shipped TWICE on purpose, for two different lifecycles:
;   - the "dontcopy" entry below makes it available to ExtractTemporaryFile
;     in PrepareToInstall, so an in-place upgrade can stop a running instance
;     BEFORE this run's [Files] have been (re)written to {app};
;   - this normally-installed entry puts a real, permanent copy at
;     {app}\stop-app.ps1, which InitializeUninstall() and the "Stop" shortcut
;     below both depend on existing on disk AFTER install completes.
Source: "stop-app.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "stop-app.ps1"; DestDir: "{tmp}"; Flags: dontcopy

[InstallDelete]
; Remove only shortcut names created by earlier releases with the same AppId.
Type: files; Name: "{userdesktop}\Remote Mic · 小米遥控器2 Pro.lnk"
Type: files; Name: "{userdesktop}\Remote Mic · RC003.lnk"
Type: files; Name: "{userprograms}\Remote Mic\Remote Mic · 小米遥控器2 Pro.lnk"
Type: files; Name: "{userprograms}\Remote Mic\Remote Mic · 小米遥控器2 Pro 设置.lnk"
Type: files; Name: "{userprograms}\Remote Mic\停止 Remote Mic · 小米遥控器2 Pro.lnk"
Type: files; Name: "{userprograms}\Remote Mic\卸载 Remote Mic · 小米遥控器2 Pro.lnk"
Type: files; Name: "{userprograms}\Remote Mic\Remote Mic · RC003.lnk"
Type: files; Name: "{userprograms}\Remote Mic\Remote Mic · RC003 设置.lnk"
Type: files; Name: "{userprograms}\Remote Mic\停止 Remote Mic · RC003.lnk"
Type: files; Name: "{userprograms}\Remote Mic\卸载 Remote Mic · RC003.lnk"
Type: dirifempty; Name: "{userprograms}\Remote Mic"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"; Flags: unchecked
; Deliberately no "start on login" task here.

[Icons]
; Normal shortcuts open the one desktop shell. It owns both the taskbar
; window and notification-area icon; bridge startup remains controlled by
; the saved in-app option.
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{#AppName} 设置"; Filename: "{app}\{#AppExeName}"; Parameters: "--settings"
Name: "{group}\停止 {#AppName}"; Filename: "powershell.exe"; Parameters: "-ExecutionPolicy Bypass -File ""{app}\stop-app.ps1"" -AppPath ""{app}"""; WorkingDir: "{app}"; Flags: runminimized
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
; Deliberately no {userstartup} icon anywhere in this file.

[Run]
; Post-install may open Settings, but must never silently start the
; bridge (that would touch BLE/HID/audio before the user has configured
; anything) - unchecked by default either way.
Filename: "{app}\{#AppExeName}"; Parameters: "--settings"; Description: "打开 {#AppName} 设置"; Flags: postinstall nowait skipifsilent unchecked

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  Started: Boolean;
begin
  Result := '';
  ExtractTemporaryFile('stop-app.ps1');
  Started := Exec('powershell.exe',
    '-ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\stop-app.ps1') + '" -AppPath "' + ExpandConstant('{app}') + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if not Started then
  begin
    Result := '无法运行旧进程清理程序；安装已停止，以免覆盖仍在使用的文件。';
    exit;
  end;
  if ResultCode <> 0 then
  begin
    Result := '无线麦仍在运行或未能确认退出；请先退出程序后再重试安装。';
    exit;
  end;
end;

function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
  Started: Boolean;
  StopScript: String;
begin
  Result := True;
  StopScript := ExpandConstant('{app}\stop-app.ps1');
  if not FileExists(StopScript) then
  begin
    MsgBox(
      '无法找到无线麦进程清理程序。卸载尚未开始，请修复或重新安装当前版本后重试。',
      mbError,
      MB_OK
    );
    Result := False;
    exit;
  end;

  Started := Exec('powershell.exe',
    '-ExecutionPolicy Bypass -File "' + StopScript + '" -AppPath "' + ExpandConstant('{app}') + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if (not Started) or (ResultCode <> 0) then
  begin
    MsgBox(
      '无线麦仍在运行或未能确认退出。卸载尚未开始，请先退出程序后重试。',
      mbError,
      MB_OK
    );
    Result := False;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(
      HKCU,
      'Software\Microsoft\Windows\CurrentVersion\Run',
      'RemoteMicRC003'
    );
end;
