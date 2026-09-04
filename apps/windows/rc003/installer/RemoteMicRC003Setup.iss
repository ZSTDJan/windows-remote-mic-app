; Inno Setup source for Remote Mic · RC003 (Windows source/build
; candidate). Unsigned and partially real-device verified - see this
; subtree's README.md and TESTING.md for the exact verified paths and
; remaining checks before treating this as a supported release artifact.
;
; Hard boundaries enforced by this script:
;   - PrivilegesRequired=lowest: the installer and desktop application stay
;     per-user/non-elevated. After files are installed, one narrow helper is
;     launched with the Windows runas verb so the user can approve creation
;     of the fixed on-demand HID task. Normal launch and login startup never
;     request elevation.
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
#define AppVersion "0.1.0-candidate"
#define AppExeName "RemoteMicRC003.exe"
#define HidHelperExeName "RemoteMicRC003HidHelper.exe"
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
; This marker is the upgrade-time proof that the installed desktop build
; consumes application-exit-request.json and must never be force-stopped.
Source: "application-exit-contract-v1.json"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; PyInstaller's runtime directory contains no user settings or logs. Remove
; it only after PrepareToInstall has proved the old application is stopped,
; so files removed from a newer build cannot survive an in-place upgrade.
Type: filesandordirs; Name: "{app}\_internal"
; Builds before 2026-09-04 exposed the internal helper beside the main EXE.
; Remove that exact obsolete file during upgrade so users continue to see
; only the supported application entry point at the install root.
Type: files; Name: "{app}\{#HidHelperExeName}"
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
Name: "{group}\停止 {#AppName}"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\stop-app.ps1"" -AppPath ""{app}"" -ConfigRoot ""{localappdata}\RemoteMic\{#AppFolder}"""; WorkingDir: "{app}"; Flags: runminimized
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
; Deliberately no {userstartup} icon anywhere in this file.

[Run]
; Post-install may open Settings, but must never silently start the
; bridge (that would touch BLE/HID/audio before the user has configured
; anything) - unchecked by default either way.
Filename: "{app}\{#AppExeName}"; Parameters: "--settings"; Description: "打开 {#AppName} 设置"; Flags: postinstall nowait skipifsilent unchecked

[Code]
const
  StopNeedsElevationExitCode = 10;
  StopUnsafeToContinueExitCode = 20;
  StopProbeFailedExitCode = 21;

var
  HidHelperInstallSucceeded: Boolean;

function RunStopApplication(const StopScript: String; Elevated: Boolean;
  var ResultCode: Integer): Boolean;
var
  Parameters: String;
  PowerShellPath: String;
begin
  PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  Parameters :=
    '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    StopScript + '" -AppPath "' + ExpandConstant('{app}') +
    '" -ConfigRoot "' +
    ExpandConstant('{localappdata}\RemoteMic\{#AppFolder}') + '"';
  if Elevated then
  begin
    Parameters := Parameters + ' -ElevatedRetry';
    Result := ShellExec(
      'runas',
      PowerShellPath,
      Parameters,
      ExtractFileDir(StopScript),
      SW_HIDE,
      ewWaitUntilTerminated,
      ResultCode
    );
  end
  else
    Result := Exec(
      PowerShellPath,
      Parameters,
      ExtractFileDir(StopScript),
      SW_HIDE,
      ewWaitUntilTerminated,
      ResultCode
    );
end;

function RunHidHelper(const Parameters: String; var ResultCode: Integer): Boolean;
var
  HelperPath: String;
begin
  HelperPath := ExpandConstant('{app}\_internal\{#HidHelperExeName}');
  if not FileExists(HelperPath) then
  begin
    ResultCode := -1;
    Result := False;
    exit;
  end;
  Result := ShellExec(
    'runas',
    HelperPath,
    Parameters,
    ExpandConstant('{sys}'),
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
  Result := Result and (ResultCode = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  Started: Boolean;
  StopScript: String;
begin
  Result := '';
  ExtractTemporaryFile('stop-app.ps1');
  StopScript := ExpandConstant('{tmp}\stop-app.ps1');
  Started := RunStopApplication(StopScript, False, ResultCode);
  if not Started then
  begin
    Result := '无法运行旧进程清理程序；安装已停止，以免覆盖仍在使用的文件。';
    exit;
  end;
  if ResultCode = StopNeedsElevationExitCode then
  begin
    Started := RunStopApplication(StopScript, True, ResultCode);
    if not Started then
    begin
      Result := '需要管理员权限关闭正在以管理员身份运行的旧版。未完成 UAC 确认，安装没有覆盖任何程序文件。';
      exit;
    end;
  end;
  if ResultCode <> 0 then
  begin
    if ResultCode = StopUnsafeToContinueExitCode then
      Result := '无线麦没有完成安全退出。安装没有覆盖任何程序文件；请先处理窗口中的保存提示并完全退出。旧版若没有可用的退出入口，请关闭软件或重启 Windows 后再安装。'
    else if ResultCode = StopProbeFailedExitCode then
      Result := '无法安全核对正在运行的旧程序。安装没有覆盖任何程序文件；请关闭所有无线麦进程或重启 Windows 后再安装。'
    else
      Result := '无线麦没有完成安全退出。安装没有覆盖任何程序文件；请完全退出后重试。';
    exit;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep <> ssPostInstall then
    exit;

  HidHelperInstallSucceeded := RunHidHelper('--install-task', ResultCode);
  if not HidHelperInstallSucceeded then
    MsgBox(
      '主程序已经安装，但管理员按键组件没有安装成功。方向键仍按 Windows 原始方向执行一次，自定义方向映射已停用。可打开无线麦，在“按键接收”旁点击“修复权限”重试。',
      mbError,
      MB_OK
    );
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and (not HidHelperInstallSucceeded) then
    WizardForm.FinishedLabel.Caption :=
      '无线麦主程序已安装，但管理员按键组件未完成。方向键不会连击；修复权限后才能使用自定义方向映射。';
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

  Started := RunStopApplication(StopScript, False, ResultCode);
  if Started then
  begin
    if ResultCode = StopNeedsElevationExitCode then
      Started := RunStopApplication(StopScript, True, ResultCode);
  end;
  if (not Started) or (ResultCode <> 0) then
  begin
    MsgBox(
      '无线麦没有完成安全退出，或管理员确认被取消。卸载尚未开始；请处理窗口中的保存提示并完全退出。旧版若没有可用的退出入口，请关闭软件或重启 Windows 后再卸载。',
      mbError,
      MB_OK
    );
    Result := False;
    exit;
  end;

  if not RunHidHelper('--uninstall-task', ResultCode) then
  begin
    MsgBox(
      '管理员按键组件未能卸载。卸载尚未开始，请确认 UAC 后重试，以免系统中留下失效的计划任务。',
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
