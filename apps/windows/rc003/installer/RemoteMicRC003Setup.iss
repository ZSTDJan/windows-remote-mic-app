; Inno Setup source for Remote Mic · RC003 (Windows source/build
; candidate). Unsigned and partially real-device verified - see this
; subtree's README.md and TESTING.md for the exact verified paths and
; remaining checks before treating this as a supported release artifact.
;
; Hard boundaries enforced by this script:
;   - PrivilegesRequired=lowest: the installer and desktop application stay
;     per-user/non-elevated. After files are installed, one narrow helper is
;     requested through the ordinary desktop executable. That executable
;     verifies the current account before its narrow bundled helper uses the
;     Windows runas verb. Normal launch and login startup never request
;     elevation after setup succeeds.
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
#define VersionFileHandle FileOpen(AddBackslash(SourcePath) + "..\src\ovb_rc003\VERSION")
#if !VersionFileHandle
  #error "Cannot read src\ovb_rc003\VERSION"
#endif
#define AppVersion Trim(FileRead(VersionFileHandle))
#expr FileClose(VersionFileHandle)
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
; anything). The finish-page action is checked by default so a successful
; upgrade visibly opens the newly installed version.
Filename: "{app}\{#AppExeName}"; Parameters: "--settings"; Description: "打开 {#AppName} {#AppVersion}"; Flags: postinstall nowait skipifsilent

[Code]
const
  StopNeedsElevationExitCode = 10;
  StopUnsafeToContinueExitCode = 20;
  StopProbeFailedExitCode = 21;
  StopUserActionRequiredExitCode = 22;
  StopOtherLocationRunningExitCode = 23;
  HidHelperAccountUnsupportedExitCode = 25;

var
  HidHelperInstallSucceeded: Boolean;

function RunStopApplication(const StopScript: String; Elevated: Boolean;
  BlockOtherLocations: Boolean; var ResultCode: Integer): Boolean;
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
  if BlockOtherLocations then
    Parameters := Parameters + ' -BlockOtherLocations';
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

function RunApplicationMaintenance(const Parameters: String;
  var ResultCode: Integer): Boolean;
var
  AppPath: String;
begin
  AppPath := ExpandConstant('{app}\{#AppExeName}');
  if not FileExists(AppPath) then
  begin
    ResultCode := -1;
    Result := False;
    exit;
  end;
  Result := Exec(
    AppPath,
    Parameters,
    ExpandConstant('{app}'),
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
  Started := RunStopApplication(StopScript, False, True, ResultCode);
  if not Started then
  begin
    Result := '无法运行旧进程清理程序；安装已停止，以免覆盖仍在使用的文件。';
    exit;
  end;
  if ResultCode = StopNeedsElevationExitCode then
  begin
    Started := RunStopApplication(StopScript, True, True, ResultCode);
    if not Started then
    begin
      Result := '需要管理员权限关闭正在以管理员身份运行的旧版。未完成 UAC 确认，安装没有覆盖任何程序文件。';
      exit;
    end;
  end;
  if ResultCode <> 0 then
  begin
    if ResultCode = StopOtherLocationRunningExitCode then
      Result := '检测到另一个文件夹中的无线麦仍在运行，常见于旧便携版。已尝试把旧窗口唤到前台；请保存或放弃未保存的修改，再从通知区域选择“完全退出”，然后回到安装器重试。安装没有覆盖任何程序文件。'
    else if ResultCode = StopUserActionRequiredExitCode then
      Result := '旧版遥控器服务已经停止，但旧版设置窗口仍在运行。请在旧版窗口保存或放弃未保存的修改，再从通知区域选择“完全退出”；完成后回到安装器重试。安装没有覆盖任何程序文件。'
    else if ResultCode = StopUnsafeToContinueExitCode then
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

  HidHelperInstallSucceeded := RunApplicationMaintenance(
    '--install-hid-helper',
    ResultCode
  );
  if not HidHelperInstallSucceeded then
  begin
    if ResultCode = HidHelperAccountUnsupportedExitCode then
      MsgBox(
        '当前 Windows 账号不是管理员，不能启用方向改键。请登录管理员账号后重试；在 UAC 中临时输入另一个管理员账号无效。',
        mbError,
        MB_OK
      )
    else
      MsgBox(
        '方向改键未启用。打开无线麦，在“按键接收”中点击“启用改键”即可重试。',
        mbError,
        MB_OK
      );
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and (not HidHelperInstallSucceeded) then
    WizardForm.FinishedLabel.Caption :=
      '无线麦已安装；方向改键可稍后在程序内启用。';
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

  Started := RunStopApplication(StopScript, False, False, ResultCode);
  if Started then
  begin
    if ResultCode = StopNeedsElevationExitCode then
      Started := RunStopApplication(StopScript, True, False, ResultCode);
  end;
  if (not Started) or (ResultCode <> 0) then
  begin
    if ResultCode = StopUserActionRequiredExitCode then
      MsgBox(
        '旧版遥控器服务已经停止，但设置窗口仍在运行。请保存或放弃未保存的修改，再从通知区域选择“完全退出”，然后重新卸载。',
        mbError,
        MB_OK
      )
    else
      MsgBox(
        '无线麦没有完成安全退出，或管理员确认被取消。卸载尚未开始；请处理窗口中的保存提示并完全退出。旧版若没有可用的退出入口，请关闭软件或重启 Windows 后再卸载。',
        mbError,
        MB_OK
      );
    Result := False;
    exit;
  end;

  if not RunApplicationMaintenance('--uninstall-hid-helper', ResultCode) then
  begin
    MsgBox(
      '方向改键权限未能移除，卸载尚未开始。请确认管理员权限后重试。',
      mbError,
      MB_OK
    );
    Result := False;
  end;
end;

procedure RemoveOwnedLoginStartupValue;
var
  AppExecutable: String;
  CurrentCommand: String;
  ExpectedCommand: String;
  ExpectedQuotedCommand: String;
begin
  if not RegQueryStringValue(
    HKCU,
    'Software\Microsoft\Windows\CurrentVersion\Run',
    'RemoteMicRC003',
    CurrentCommand
  ) then
    exit;

  AppExecutable := ExpandConstant('{app}\{#AppExeName}');
  ExpectedCommand := AppExecutable + ' --background';
  ExpectedQuotedCommand := '"' + AppExecutable + '" --background';
  CurrentCommand := Trim(CurrentCommand);
  if (CompareText(CurrentCommand, ExpectedCommand) = 0) or
     (CompareText(CurrentCommand, ExpectedQuotedCommand) = 0) then
    RegDeleteValue(
      HKCU,
      'Software\Microsoft\Windows\CurrentVersion\Run',
      'RemoteMicRC003'
    );
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RemoveOwnedLoginStartupValue;
end;
