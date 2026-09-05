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
CloseApplications=no
RestartApplications=no

[Files]
; Install the main executable last so the post-copy runtime check follows it
; immediately. The old complete runtime remains quarantined until that check
; has actually run the new executable successfully.
Source: "{#DistDir}\*"; DestDir: "{app}"; Excludes: "{#AppExeName}"; Flags: recursesubdirs createallsubdirs ignoreversion
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
Source: "{#DistDir}\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[UninstallDelete]
; Normal setup removes these exact upgrade backups immediately. Uninstall is
; the final cleanup path if security software temporarily held either one.
Type: filesandordirs; Name: "{app}\.installing-previous"
Type: files; Name: "{app}\.installing-previous.state"
Type: files; Name: "{app}\{#AppExeName}.installing-previous"

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
Filename: "{app}\{#AppExeName}"; Parameters: "--settings"; Description: "打开 {#AppName} {#AppVersion}"; Flags: postinstall nowait skipifsilent; Check: ShouldLaunchInstalledApplication

[Code]
const
  StopNeedsElevationExitCode = 10;
  StopUnsafeToContinueExitCode = 20;
  StopProbeFailedExitCode = 21;
  StopUserActionRequiredExitCode = 22;
  StopOtherLocationRunningExitCode = 23;
  StopOtherSessionRunningExitCode = 24;
  HidHelperAccountUnsupportedExitCode = 25;
  ErrorAlreadyExists = 183;
  InstallerMaintenanceMutexName = 'Global\RemoteMicRC003_InstallerMaintenance';
  LegacySettingsMutexName = 'Local\RemoteMicRC003_SettingsInstance';
  LegacyBridgeMutexName = 'Local\RemoteMicRC003_BridgeInstance';
  LegacyHandoffMutexName = 'Local\RemoteMicRC003_ApplicationHandoff';
  FileCleanupAttempts = 5;
  FileCleanupDelayMilliseconds = 200;
  UpgradeStatePreparing = 'preparing';
  UpgradeStateQuarantined = 'quarantined';
  UpgradeStateRestorePending = 'restore-pending';
  UpgradeStateRestoring = 'restoring';
  UpgradeStateCommitted = 'committed';
  InstallValidationFailureExitCode = 2;

var
  HidHelperInstallSucceeded: Boolean;
  InstallFilesCompleted: Boolean;
  InstallValidationFailed: Boolean;
  InstallRecoverySucceeded: Boolean;
  InstallHadPreviousRuntime: Boolean;
  InstallValidationMessage: String;
  InstallerMaintenanceMutexHandle: LongWord;
  LegacySettingsMutexHandle: LongWord;
  LegacyBridgeMutexHandle: LongWord;
  LegacyHandoffMutexHandle: LongWord;
  UpgradeApplicationPath: String;
  UpgradeInternalPath: String;
  UpgradeLegacyHelperPath: String;
  UpgradeRuntimeHoldPath: String;
  UpgradeStatePath: String;
  UpgradeHeldApplicationPath: String;
  UpgradeHeldInternalPath: String;
  UpgradeHeldLegacyHelperPath: String;
  UpgradeRuntimeQuarantined: Boolean;
  UpgradeRuntimeMutationStarted: Boolean;

function CreateMutex(SecurityAttributes: LongWord; InitialOwner: Boolean;
  Name: String): LongWord;
  external 'CreateMutexW@kernel32.dll stdcall';
function CloseKernelHandle(Handle: LongWord): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';
function GetWindowsLastError(): LongWord;
  external 'GetLastError@kernel32.dll stdcall';
function ShellIsUserAnAdmin(): Boolean;
  external 'IsUserAnAdmin@shell32.dll stdcall';

procedure ReleaseInstallerMaintenanceMutex;
begin
  if InstallerMaintenanceMutexHandle <> 0 then
  begin
    CloseKernelHandle(InstallerMaintenanceMutexHandle);
    InstallerMaintenanceMutexHandle := 0;
  end;
end;

procedure ReleaseLegacyRuntimeBarriers;
begin
  if LegacyHandoffMutexHandle <> 0 then
  begin
    CloseKernelHandle(LegacyHandoffMutexHandle);
    LegacyHandoffMutexHandle := 0;
  end;
  if LegacyBridgeMutexHandle <> 0 then
  begin
    CloseKernelHandle(LegacyBridgeMutexHandle);
    LegacyBridgeMutexHandle := 0;
  end;
  if LegacySettingsMutexHandle <> 0 then
  begin
    CloseKernelHandle(LegacySettingsMutexHandle);
    LegacySettingsMutexHandle := 0;
  end;
end;

function DeleteFileWithRetries(const FileName: String): Boolean;
var
  Attempt: Integer;
begin
  for Attempt := 1 to FileCleanupAttempts do
  begin
    if not FileExists(FileName) then
    begin
      Result := True;
      exit;
    end;
    if DeleteFile(FileName) then
    begin
      Result := True;
      exit;
    end;
    if Attempt < FileCleanupAttempts then
      Sleep(FileCleanupDelayMilliseconds);
  end;
  Result := not FileExists(FileName);
end;

function DeleteDirectoryWithRetries(const DirectoryName: String): Boolean;
var
  Attempt: Integer;
begin
  for Attempt := 1 to FileCleanupAttempts do
  begin
    if not DirExists(DirectoryName) then
    begin
      Result := True;
      exit;
    end;
    if DelTree(DirectoryName, True, True, True) then
    begin
      Result := True;
      exit;
    end;
    if Attempt < FileCleanupAttempts then
      Sleep(FileCleanupDelayMilliseconds);
  end;
  Result := not DirExists(DirectoryName);
end;

function AcquireLegacyRuntimeBarriers(var ErrorMessage: String): Boolean;
begin
  ErrorMessage := '';
  if (LegacySettingsMutexHandle <> 0) and
     (LegacyBridgeMutexHandle <> 0) and
     (LegacyHandoffMutexHandle <> 0) then
  begin
    Result := True;
    exit;
  end;

  ReleaseLegacyRuntimeBarriers;
  LegacySettingsMutexHandle := CreateMutex(0, False, LegacySettingsMutexName);
  if LegacySettingsMutexHandle <> 0 then
    LegacyBridgeMutexHandle := CreateMutex(0, False, LegacyBridgeMutexName);
  if LegacyBridgeMutexHandle <> 0 then
    LegacyHandoffMutexHandle := CreateMutex(0, False, LegacyHandoffMutexName);
  if LegacyHandoffMutexHandle = 0 then
  begin
    ReleaseLegacyRuntimeBarriers;
    ErrorMessage :=
      '无法锁定旧版启动入口。请完全退出无线麦后重试；本次没有覆盖程序文件。';
    Result := False;
    exit;
  end;
  Result := True;
end;

procedure InitializeUpgradeRuntimePaths;
begin
  UpgradeApplicationPath := ExpandConstant('{app}\{#AppExeName}');
  UpgradeInternalPath := ExpandConstant('{app}\_internal');
  UpgradeLegacyHelperPath := ExpandConstant('{app}\{#HidHelperExeName}');
  UpgradeRuntimeHoldPath := ExpandConstant('{app}\.installing-previous');
  UpgradeStatePath := ExpandConstant('{app}\.installing-previous.state');
  UpgradeHeldApplicationPath :=
    AddBackslash(UpgradeRuntimeHoldPath) + '{#AppExeName}';
  UpgradeHeldInternalPath := AddBackslash(UpgradeRuntimeHoldPath) + '_internal';
  UpgradeHeldLegacyHelperPath :=
    AddBackslash(UpgradeRuntimeHoldPath) + '{#HidHelperExeName}';
end;

function CurrentRuntimeExists(): Boolean;
begin
  InitializeUpgradeRuntimePaths;
  Result := FileExists(UpgradeApplicationPath) or
    DirExists(UpgradeInternalPath) or
    FileExists(UpgradeLegacyHelperPath);
end;

function HeldRuntimeExists(): Boolean;
begin
  InitializeUpgradeRuntimePaths;
  Result := FileExists(UpgradeHeldApplicationPath) or
    DirExists(UpgradeHeldInternalPath) or
    FileExists(UpgradeHeldLegacyHelperPath);
end;

function HeldRuntimeIsComplete(): Boolean;
begin
  InitializeUpgradeRuntimePaths;
  Result := FileExists(UpgradeHeldApplicationPath) and
    DirExists(UpgradeHeldInternalPath);
end;

function CurrentRuntimeIsComplete(): Boolean;
begin
  InitializeUpgradeRuntimePaths;
  Result := FileExists(UpgradeApplicationPath) and
    DirExists(UpgradeInternalPath);
end;

function RestoringRuntimeIsComplete(): Boolean;
begin
  InitializeUpgradeRuntimePaths;
  Result :=
    (FileExists(UpgradeHeldApplicationPath) or
     FileExists(UpgradeApplicationPath)) and
    (DirExists(UpgradeHeldInternalPath) or
     DirExists(UpgradeInternalPath));
end;

function ReadUpgradeRuntimeState(): String;
var
  StateText: AnsiString;
begin
  Result := '';
  InitializeUpgradeRuntimePaths;
  if LoadStringFromFile(UpgradeStatePath, StateText) then
    Result := Trim(String(StateText));
end;

function WriteUpgradeRuntimeState(const StateText: String): Boolean;
begin
  InitializeUpgradeRuntimePaths;
  Result := ForceDirectories(UpgradeRuntimeHoldPath) and
    SaveStringToFile(UpgradeStatePath, AnsiString(StateText), False);
end;

function DeleteUpgradeRuntimeStateAfterCleanup(): Boolean;
begin
  InitializeUpgradeRuntimePaths;
  if DirExists(UpgradeRuntimeHoldPath) then
  begin
    Result := False;
    exit;
  end;
  Result := DeleteFileWithRetries(UpgradeStatePath);
end;

function RemoveCurrentRuntimePayload(): Boolean;
begin
  InitializeUpgradeRuntimePaths;
  Result := True;
  if FileExists(UpgradeApplicationPath) and
     (not DeleteFileWithRetries(UpgradeApplicationPath)) then
  begin
    Log('Could not remove the replacement application executable.');
    Result := False;
  end;
  if DirExists(UpgradeInternalPath) and
     (not DeleteDirectoryWithRetries(UpgradeInternalPath)) then
  begin
    Log('Could not remove the replacement runtime directory.');
    Result := False;
  end;
  if FileExists(UpgradeLegacyHelperPath) and
     (not DeleteFileWithRetries(UpgradeLegacyHelperPath)) then
  begin
    Log('Could not remove the replacement legacy helper.');
    Result := False;
  end;
end;

function ValidateInstalledApplication(var ErrorMessage: String): Boolean;
  forward;

function RestoreHeldRuntime(RemoveAllCurrent: Boolean): Boolean;
var
  RuntimeState: String;
begin
  InitializeUpgradeRuntimePaths;
  RuntimeState := ReadUpgradeRuntimeState();
  if not DirExists(UpgradeRuntimeHoldPath) then
  begin
    if RemoveAllCurrent then
      Result := (RuntimeState = UpgradeStateRestoring) and
        CurrentRuntimeIsComplete()
    else
      Result := (RuntimeState = UpgradeStatePreparing) and
        CurrentRuntimeIsComplete();
    if Result then
    begin
      Result := DeleteFileWithRetries(UpgradeStatePath);
      if Result then
        UpgradeRuntimeQuarantined := False;
    end
    else
      Log(
        'Previous runtime backup is missing; recovery was not reported as successful.'
      );
    exit;
  end;

  if RemoveAllCurrent and
     (RuntimeState <> UpgradeStateRestorePending) and
     (RuntimeState <> UpgradeStateRestoring) then
  begin
    if not HeldRuntimeIsComplete() then
    begin
      Log(
        'Previous runtime backup is incomplete; current runtime was preserved.'
      );
      Result := False;
      exit;
    end;
    if not WriteUpgradeRuntimeState(UpgradeStateRestorePending) then
    begin
      Log(
        'Could not record previous-runtime recovery intent; the current runtime and complete backup were preserved.'
      );
      Result := False;
      exit;
    end;
    RuntimeState := UpgradeStateRestorePending;
  end;

  if RemoveAllCurrent and (RuntimeState = UpgradeStateRestorePending) then
  begin
    if not HeldRuntimeIsComplete() then
    begin
      Log(
        'Previous runtime backup is incomplete; recovery cannot start safely.'
      );
      Result := False;
      exit;
    end;
    if not RemoveCurrentRuntimePayload() then
    begin
      Result := False;
      exit;
    end;
    if not WriteUpgradeRuntimeState(UpgradeStateRestoring) then
    begin
      Log(
        'Could not advance previous-runtime recovery state; the complete backup was preserved.'
      );
      Result := False;
      exit;
    end;
    RuntimeState := UpgradeStateRestoring;
  end
  else if RemoveAllCurrent and (not RestoringRuntimeIsComplete()) then
  begin
    Log(
      'Previous runtime recovery is incomplete and cannot be resumed safely.'
    );
    Result := False;
    exit;
  end;

  if (not RemoveAllCurrent) and
     (RuntimeState <> UpgradeStatePreparing) then
  begin
    Log('Partial previous-runtime recovery has an unexpected state.');
    Result := False;
    exit;
  end;

  if RemoveAllCurrent and (RuntimeState <> UpgradeStateRestoring) then
  begin
    Log('Full previous-runtime recovery did not enter the restoring state.');
    Result := False;
    exit;
  end;

  if DirExists(UpgradeHeldInternalPath) then
  begin
    if DirExists(UpgradeInternalPath) and
       (not DeleteDirectoryWithRetries(UpgradeInternalPath)) then
    begin
      Result := False;
      exit;
    end;
    if not RenameFile(UpgradeHeldInternalPath, UpgradeInternalPath) then
    begin
      Log('Could not restore the previous runtime directory.');
      Result := False;
      exit;
    end;
  end;

  if FileExists(UpgradeHeldLegacyHelperPath) then
  begin
    if FileExists(UpgradeLegacyHelperPath) and
       (not DeleteFileWithRetries(UpgradeLegacyHelperPath)) then
    begin
      Result := False;
      exit;
    end;
    if not RenameFile(
      UpgradeHeldLegacyHelperPath,
      UpgradeLegacyHelperPath
    ) then
    begin
      Log('Could not restore the previous legacy helper.');
      Result := False;
      exit;
    end;
  end;

  if FileExists(UpgradeHeldApplicationPath) then
  begin
    if FileExists(UpgradeApplicationPath) and
       (not DeleteFileWithRetries(UpgradeApplicationPath)) then
    begin
      Result := False;
      exit;
    end;
    if not RenameFile(UpgradeHeldApplicationPath, UpgradeApplicationPath) then
    begin
      Log('Could not restore the previous application executable.');
      Result := False;
      exit;
    end;
  end;

  if RemoveAllCurrent and (not CurrentRuntimeIsComplete()) then
  begin
    Log('Previous runtime recovery did not produce a complete current runtime.');
    Result := False;
    exit;
  end;

  UpgradeRuntimeQuarantined := HeldRuntimeExists();
  if not UpgradeRuntimeQuarantined then
  begin
    if not DeleteDirectoryWithRetries(UpgradeRuntimeHoldPath) then
      Log('Could not remove the empty previous-runtime quarantine directory.');
    DeleteFileWithRetries(UpgradeStatePath);
  end;
  Result := True;
end;

function ValidateInstalledApplication(var ErrorMessage: String): Boolean;
var
  ResultCode: Integer;
  Started: Boolean;
  HelperPath: String;
begin
  InitializeUpgradeRuntimePaths;
  ErrorMessage := '';
  HelperPath := AddBackslash(UpgradeInternalPath) + '{#HidHelperExeName}';
  if not FileExists(UpgradeApplicationPath) then
  begin
    ErrorMessage := '无线麦主程序未能写入。';
    Result := False;
    exit;
  end;
  if not DirExists(UpgradeInternalPath) then
  begin
    ErrorMessage := '无线麦运行文件未能完整写入。';
    Result := False;
    exit;
  end;
  if not FileExists(HelperPath) then
  begin
    ErrorMessage := '管理员按键组件未能完整写入。';
    Result := False;
    exit;
  end;
  Started := Exec(
    UpgradeApplicationPath,
    '--dry-run',
    ExpandConstant('{app}'),
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
  if (not Started) or (ResultCode <> 0) then
  begin
    ErrorMessage := '无线麦新版本未通过启动检查。';
    Result := False;
    exit;
  end;
  Result := True;
end;

function NormalizeLegacyExecutableHold(var ErrorMessage: String): Boolean;
var
  LegacyHoldPath: String;
begin
  InitializeUpgradeRuntimePaths;
  ErrorMessage := '';
  LegacyHoldPath := UpgradeApplicationPath + '.installing-previous';
  if not FileExists(LegacyHoldPath) then
  begin
    Result := True;
    exit;
  end;
  if FileExists(UpgradeApplicationPath) then
    Result := DeleteFileWithRetries(LegacyHoldPath)
  else
    Result := RenameFile(LegacyHoldPath, UpgradeApplicationPath);
  if not Result then
    ErrorMessage :=
      '无法处理上次安装留下的旧程序备份。请关闭安全软件拦截后重试。';
end;

function NormalizePreviousUpgradeRuntime(var ErrorMessage: String): Boolean;
var
  RuntimeState: String;
  ValidationError: String;
begin
  ErrorMessage := '';
  InitializeUpgradeRuntimePaths;
  if not NormalizeLegacyExecutableHold(ErrorMessage) then
  begin
    Result := False;
    exit;
  end;
  RuntimeState := ReadUpgradeRuntimeState();
  if not DirExists(UpgradeRuntimeHoldPath) then
  begin
    UpgradeRuntimeQuarantined := False;
    if (RuntimeState = '') or (RuntimeState = UpgradeStateCommitted) then
      Result := DeleteUpgradeRuntimeStateAfterCleanup()
    else if ((RuntimeState = UpgradeStatePreparing) or
             (RuntimeState = UpgradeStateRestoring)) and
            CurrentRuntimeIsComplete() then
      Result := DeleteUpgradeRuntimeStateAfterCleanup()
    else
      Result := False;
    if not Result then
      ErrorMessage :=
        '上次安装或恢复没有完成，旧版备份也已丢失。为避免继续覆盖，本次没有改动文件；请保留安装目录并联系维护人员处理。';
    exit;
  end;

  UpgradeRuntimeQuarantined := HeldRuntimeExists();
  if RuntimeState = UpgradeStateCommitted then
  begin
    if not DeleteDirectoryWithRetries(UpgradeRuntimeHoldPath) then
    begin
      ErrorMessage :=
        '无法清理上次安装留下的旧版备份。请关闭安全软件拦截后重试。';
      Result := False;
      exit;
    end;
    UpgradeRuntimeQuarantined := False;
    Result := DeleteUpgradeRuntimeStateAfterCleanup();
    if not Result then
      ErrorMessage :=
        '无法清理上次安装留下的状态文件。请关闭安全软件拦截后重试。';
    exit;
  end;

  if RuntimeState = UpgradeStateRestoring then
  begin
    Result := RestoreHeldRuntime(True);
    if not Result then
      ErrorMessage :=
        '无法继续上次未完成的旧版恢复。请不要启动无线麦，保留安装目录并联系维护人员处理。';
    exit;
  end;

  if RuntimeState = UpgradeStateRestorePending then
  begin
    Result := RestoreHeldRuntime(True);
    if not Result then
      ErrorMessage :=
        '无法开始上次未完成的旧版恢复。请不要启动无线麦，保留安装目录并联系维护人员处理。';
    exit;
  end;

  if UpgradeRuntimeQuarantined and
     (RuntimeState = UpgradeStatePreparing) then
  begin
    Result := RestoreHeldRuntime(False);
    if not Result then
      ErrorMessage :=
        '无法恢复上次安装保留的旧版。请不要启动无线麦，保留安装目录并联系维护人员处理。';
    exit;
  end;
  if UpgradeRuntimeQuarantined and
     (RuntimeState = UpgradeStateQuarantined) then
  begin
    Result := RestoreHeldRuntime(True);
    if not Result then
      ErrorMessage :=
        '无法恢复上次安装保留的旧版。请不要启动无线麦，保留安装目录并联系维护人员处理。';
    exit;
  end;
  if UpgradeRuntimeQuarantined then
  begin
    if CurrentRuntimeExists() and
       ValidateInstalledApplication(ValidationError) then
    begin
      if not WriteUpgradeRuntimeState(UpgradeStateCommitted) then
      begin
        ErrorMessage :=
          '无法确认当前程序是否完整。为避免误删，本次未改动文件；请保留安装目录并联系维护人员处理。';
        Result := False;
        exit;
      end;
      if not DeleteDirectoryWithRetries(UpgradeRuntimeHoldPath) then
      begin
        ErrorMessage :=
          '当前程序可用，但无法清理上次安装留下的备份。请关闭安全软件拦截后重试。';
        Result := False;
        exit;
      end;
      UpgradeRuntimeQuarantined := False;
      Result := DeleteUpgradeRuntimeStateAfterCleanup();
      if not Result then
        ErrorMessage :=
          '当前程序可用，但无法清理上次安装留下的状态文件。请关闭安全软件拦截后重试。';
      exit;
    end;
    if HeldRuntimeIsComplete() then
    begin
      Result := RestoreHeldRuntime(True);
      if not Result then
        ErrorMessage :=
          '无法恢复上次安装保留的旧版。请不要启动无线麦，保留安装目录并联系维护人员处理。';
      exit;
    end;
    ErrorMessage :=
      '上次安装留下的程序和备份都不完整。为避免继续误删，本次未改动文件；请不要启动无线麦，保留安装目录并联系维护人员处理。';
    Result := False;
    exit;
  end;

  Result := DeleteDirectoryWithRetries(UpgradeRuntimeHoldPath) and
    DeleteUpgradeRuntimeStateAfterCleanup();
  if not Result then
    ErrorMessage :=
      '无法清理上次安装留下的空备份目录。请关闭安全软件拦截后重试。';
end;

function QuarantineExistingApplication(var ErrorMessage: String): Boolean;
begin
  ErrorMessage := '';
  if not NormalizePreviousUpgradeRuntime(ErrorMessage) then
  begin
    Result := False;
    exit;
  end;
  InitializeUpgradeRuntimePaths;
  if not CurrentRuntimeExists() then
  begin
    Result := True;
    exit;
  end;

  InstallHadPreviousRuntime := True;
  if not WriteUpgradeRuntimeState(UpgradeStatePreparing) then
  begin
    ErrorMessage :=
      '无法创建旧版安全备份。请检查磁盘和安全软件后重试；本次没有覆盖程序文件。';
    Result := False;
    exit;
  end;

  if FileExists(UpgradeApplicationPath) and
     (not RenameFile(UpgradeApplicationPath, UpgradeHeldApplicationPath)) then
  begin
    ErrorMessage := '无法隔离旧版主程序；本次没有覆盖程序文件。';
    RestoreHeldRuntime(False);
    Result := False;
    exit;
  end;
  if DirExists(UpgradeInternalPath) and
     (not RenameFile(UpgradeInternalPath, UpgradeHeldInternalPath)) then
  begin
    ErrorMessage := '无法隔离旧版运行文件；本次没有覆盖程序文件。';
    RestoreHeldRuntime(False);
    Result := False;
    exit;
  end;
  if FileExists(UpgradeLegacyHelperPath) and
     (not RenameFile(
       UpgradeLegacyHelperPath,
       UpgradeHeldLegacyHelperPath
     )) then
  begin
    ErrorMessage := '无法隔离旧版管理员按键组件；本次没有覆盖程序文件。';
    RestoreHeldRuntime(False);
    Result := False;
    exit;
  end;

  UpgradeRuntimeQuarantined := HeldRuntimeExists();
  if not WriteUpgradeRuntimeState(UpgradeStateQuarantined) then
  begin
    ErrorMessage :=
      '无法确认旧版安全备份。安装已停止，正在恢复旧版。';
    if not RestoreHeldRuntime(False) then
      ErrorMessage := ErrorMessage +
        ' 自动恢复失败，请不要启动无线麦，保留安装目录并联系维护人员处理。';
    Result := False;
    exit;
  end;
  Result := True;
end;

function CommitUpgradeRuntimeQuarantine(var ErrorMessage: String): Boolean;
begin
  ErrorMessage := '';
  if not UpgradeRuntimeQuarantined then
  begin
    Result := True;
    exit;
  end;
  if not WriteUpgradeRuntimeState(UpgradeStateCommitted) then
  begin
    ErrorMessage := '无法确认新版安装状态。';
    Result := False;
    exit;
  end;
  if DeleteDirectoryWithRetries(UpgradeRuntimeHoldPath) then
  begin
    UpgradeRuntimeQuarantined := False;
    if not DeleteUpgradeRuntimeStateAfterCleanup() then
      Log('Could not remove the committed previous-runtime state file.');
  end
  else
    Log('Could not remove the committed previous runtime; cleanup will retry.');
  Result := True;
end;

procedure FinishUpgradeRuntimeQuarantine;
begin
  InitializeUpgradeRuntimePaths;
  if ReadUpgradeRuntimeState() = UpgradeStateCommitted then
  begin
    if DirExists(UpgradeRuntimeHoldPath) and
       (not DeleteDirectoryWithRetries(UpgradeRuntimeHoldPath)) then
      Log('Could not remove the committed previous runtime.');
    if not DirExists(UpgradeRuntimeHoldPath) then
    begin
      UpgradeRuntimeQuarantined := False;
      if not DeleteUpgradeRuntimeStateAfterCleanup() then
        Log('Could not remove the committed previous-runtime state file.');
    end;
  end;
end;

function RecoverUpgradeRuntimeQuarantine(): Boolean;
var
  RuntimeState: String;
begin
  InitializeUpgradeRuntimePaths;
  RuntimeState := ReadUpgradeRuntimeState();
  if RuntimeState = UpgradeStateCommitted then
  begin
    FinishUpgradeRuntimeQuarantine;
    Result := True;
    exit;
  end;
  if not DirExists(UpgradeRuntimeHoldPath) then
  begin
    if UpgradeRuntimeQuarantined then
      Result := False
    else if RuntimeState = UpgradeStateRestoring then
    begin
      Result := CurrentRuntimeIsComplete();
      if Result then
        Result := DeleteUpgradeRuntimeStateAfterCleanup();
    end
    else if RuntimeState = UpgradeStatePreparing then
    begin
      Result := CurrentRuntimeIsComplete();
      if Result then
        Result := DeleteUpgradeRuntimeStateAfterCleanup();
    end
    else if RuntimeState = '' then
      Result := True
    else
      Result := False;
    exit;
  end;
  UpgradeRuntimeQuarantined := HeldRuntimeExists();
  if not UpgradeRuntimeQuarantined then
  begin
    Result := (RuntimeState = '') or
      (((RuntimeState = UpgradeStatePreparing) or
        (RuntimeState = UpgradeStateRestoring)) and
       CurrentRuntimeIsComplete());
    if Result then
      Result := DeleteDirectoryWithRetries(UpgradeRuntimeHoldPath) and
        DeleteUpgradeRuntimeStateAfterCleanup();
    exit;
  end;
  if (RuntimeState = UpgradeStateRestorePending) or
     (RuntimeState = UpgradeStateRestoring) then
    Result := RestoreHeldRuntime(True)
  else if RuntimeState = UpgradeStatePreparing then
    Result := RestoreHeldRuntime(False)
  else if RuntimeState = UpgradeStateQuarantined then
    Result := RestoreHeldRuntime(True)
  else
    Result := False;
end;

procedure DeleteObsoleteShortcuts;
begin
  DeleteFile(ExpandConstant('{userdesktop}\Remote Mic · 小米遥控器2 Pro.lnk'));
  DeleteFile(ExpandConstant('{userdesktop}\Remote Mic · RC003.lnk'));
  DeleteFile(ExpandConstant('{userprograms}\Remote Mic\Remote Mic · 小米遥控器2 Pro.lnk'));
  DeleteFile(ExpandConstant('{userprograms}\Remote Mic\Remote Mic · 小米遥控器2 Pro 设置.lnk'));
  DeleteFile(ExpandConstant('{userprograms}\Remote Mic\停止 Remote Mic · 小米遥控器2 Pro.lnk'));
  DeleteFile(ExpandConstant('{userprograms}\Remote Mic\卸载 Remote Mic · 小米遥控器2 Pro.lnk'));
  DeleteFile(ExpandConstant('{userprograms}\Remote Mic\Remote Mic · RC003.lnk'));
  DeleteFile(ExpandConstant('{userprograms}\Remote Mic\Remote Mic · RC003 设置.lnk'));
  DeleteFile(ExpandConstant('{userprograms}\Remote Mic\停止 Remote Mic · RC003.lnk'));
  DeleteFile(ExpandConstant('{userprograms}\Remote Mic\卸载 Remote Mic · RC003.lnk'));
  RemoveDir(ExpandConstant('{userprograms}\Remote Mic'));
end;

function AcquireInstallerMaintenanceMutex(): Boolean;
var
  LastError: LongWord;
begin
  if InstallerMaintenanceMutexHandle <> 0 then
  begin
    Result := True;
    exit;
  end;
  InstallerMaintenanceMutexHandle := CreateMutex(
    0,
    False,
    InstallerMaintenanceMutexName
  );
  if InstallerMaintenanceMutexHandle = 0 then
  begin
    MsgBox(
      '无法锁定无线麦程序文件。请关闭其它安装或卸载窗口后重试。',
      mbError,
      MB_OK
    );
    Result := False;
    exit;
  end;
  LastError := GetWindowsLastError();
  if LastError = ErrorAlreadyExists then
  begin
    ReleaseInstallerMaintenanceMutex;
    MsgBox(
      '无线麦正在安装或卸载。请完成前一个操作后重试。',
      mbError,
      MB_OK
    );
    Result := False;
    exit;
  end;
  Result := True;
end;

function InitializeSetup(): Boolean;
begin
  if ShellIsUserAnAdmin() then
  begin
    MsgBox(
      '请关闭安装程序，然后普通双击安装包。无线麦不需要以管理员身份安装或长期运行。',
      mbError,
      MB_OK
    );
    Result := False;
    exit;
  end;
  Result := True;
end;

procedure DeinitializeSetup();
begin
  if UpgradeRuntimeMutationStarted then
  begin
    if InstallFilesCompleted then
      FinishUpgradeRuntimeQuarantine
    else if not RecoverUpgradeRuntimeQuarantine() then
    begin
      MsgBox(
        '安装没有完成，且旧版运行文件未能自动恢复。为避免继续误删，请不要启动无线麦，保留安装目录并联系维护人员处理。',
        mbError,
        MB_OK
      );
    end;
  end;
  ReleaseLegacyRuntimeBarriers;
  ReleaseInstallerMaintenanceMutex;
end;

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

function StopApplicationForInstall(const StopScript: String;
  var ErrorMessage: String): Boolean;
var
  ResultCode: Integer;
  Started: Boolean;
begin
  ErrorMessage := '';
  Started := RunStopApplication(StopScript, False, True, ResultCode);
  if not Started then
  begin
    ErrorMessage :=
      '无法运行旧进程清理程序；安装已停止，以免覆盖仍在使用的文件。';
    Result := False;
    exit;
  end;
  if ResultCode = StopNeedsElevationExitCode then
  begin
    Started := RunStopApplication(StopScript, True, True, ResultCode);
    if not Started then
    begin
      ErrorMessage :=
        '需要管理员权限关闭正在以管理员身份运行的旧版。未完成 UAC 确认，安装没有覆盖任何程序文件。';
      Result := False;
      exit;
    end;
  end;
  if ResultCode = 0 then
  begin
    Result := True;
    exit;
  end;
  if ResultCode = StopOtherLocationRunningExitCode then
    ErrorMessage :=
      '检测到旧便携版仍在运行。请完全退出旧版后重试；安装尚未修改文件。'
  else if ResultCode = StopOtherSessionRunningExitCode then
    ErrorMessage :=
      '另一个 Windows 会话正在运行无线麦。请到那个会话完全退出后重试；安装尚未修改文件。'
  else if ResultCode = StopUserActionRequiredExitCode then
    ErrorMessage :=
      '旧版仍需您确认退出。请保存或放弃修改，完全退出旧版后重试；安装尚未修改文件。'
  else if ResultCode = StopUnsafeToContinueExitCode then
    ErrorMessage :=
      '无线麦没有完全退出。请处理旧版窗口并完全退出后重试；安装尚未修改文件。'
  else if ResultCode = StopProbeFailedExitCode then
    ErrorMessage :=
      '无法确认旧版状态。请完全退出无线麦或重启 Windows 后重试；安装尚未修改文件。'
  else
    ErrorMessage :=
      '无线麦没有完全退出。请完全退出后重试；安装尚未修改文件。';
  Result := False;
end;

function StopApplicationForUninstall(const StopScript: String): Boolean;
var
  ResultCode: Integer;
  Started: Boolean;
begin
  Started := RunStopApplication(StopScript, False, False, ResultCode);
  if Started and (ResultCode = 0) then
  begin
    Result := True;
    exit;
  end;

  if ResultCode = StopNeedsElevationExitCode then
    MsgBox(
      '无线麦正以管理员身份运行。请先在旧版通知区域选择“完全退出”，再重新卸载。',
      mbError,
      MB_OK
    )
  else if ResultCode = StopOtherSessionRunningExitCode then
    MsgBox(
      '另一个 Windows 会话正在运行无线麦。请到那个会话完全退出后重新卸载。',
      mbError,
      MB_OK
    )
  else if ResultCode = StopUserActionRequiredExitCode then
    MsgBox(
      '旧版仍需您确认退出。请保存或放弃修改，完全退出后重新卸载。',
      mbError,
      MB_OK
    )
  else
    MsgBox(
      '无线麦没有完全退出。请完全退出后重新卸载。',
      mbError,
      MB_OK
    );
  Result := False;
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
  StopScript: String;
  BarrierError: String;
  QuarantineError: String;
  StopError: String;
begin
  Result := '';
  if not AcquireInstallerMaintenanceMutex() then
  begin
    Result := '另一个安装或卸载正在进行；本次安装没有修改文件。';
    exit;
  end;
  ExtractTemporaryFile('stop-app.ps1');
  StopScript := ExpandConstant('{tmp}\stop-app.ps1');
  if not StopApplicationForInstall(StopScript, StopError) then
  begin
    Result := StopError;
    exit;
  end;
  if not AcquireLegacyRuntimeBarriers(BarrierError) then
  begin
    Result := BarrierError;
    exit;
  end;
  if not StopApplicationForInstall(StopScript, StopError) then
  begin
    Result := StopError;
    exit;
  end;
  UpgradeRuntimeMutationStarted := True;
  if not QuarantineExistingApplication(QuarantineError) then
  begin
    Result := QuarantineError;
    exit;
  end;
end;

procedure RecordInstallValidationFailure(const ErrorMessage: String);
var
  RemovedReplacement: Boolean;
  RestoredPrevious: Boolean;
begin
  InstallValidationFailed := True;
  InstallValidationMessage := ErrorMessage;
  RemovedReplacement := False;
  RestoredPrevious := False;
  if InstallHadPreviousRuntime then
  begin
    if UpgradeRuntimeQuarantined then
      RestoredPrevious := RestoreHeldRuntime(True);
    InstallRecoverySucceeded := RestoredPrevious;
  end
  else
  begin
    RemovedReplacement := RemoveCurrentRuntimePayload();
    InstallRecoverySucceeded := RemovedReplacement;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  CommitError: String;
  ResultCode: Integer;
  ValidationError: String;
begin
  if CurStep <> ssPostInstall then
    exit;
  try
    if not ValidateInstalledApplication(ValidationError) then
    begin
      RecordInstallValidationFailure(ValidationError);
      exit;
    end;
    if not CommitUpgradeRuntimeQuarantine(CommitError) then
    begin
      RecordInstallValidationFailure(CommitError);
      exit;
    end;
    InstallFilesCompleted := True;
    DeleteObsoleteShortcuts;
    HidHelperInstallSucceeded := RunApplicationMaintenance(
      '--install-hid-helper',
      ResultCode
    );
    if not HidHelperInstallSucceeded then
    begin
      if ResultCode = HidHelperAccountUnsupportedExitCode then
        MsgBox(
          '当前 Windows 账号不是管理员，不能启用管理员按键组件。请登录管理员账号后重试；在 UAC 中临时输入另一个管理员账号无效。',
          mbError,
          MB_OK
        )
      else
        MsgBox(
          '管理员按键组件未启用。打开无线麦，在“按键接收”中点击“启用改键”即可重试。',
          mbError,
          MB_OK
        );
    end;
  finally
    if (not InstallValidationFailed) or InstallRecoverySucceeded then
    begin
      ReleaseLegacyRuntimeBarriers;
      ReleaseInstallerMaintenanceMutex;
    end
    else
      Log(
        'Install recovery is incomplete; runtime barriers remain held until setup exits.'
      );
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID <> wpFinished then
    exit;
  if InstallValidationFailed then
  begin
    WizardForm.FinishedHeadingLabel.Caption := '安装失败';
    if InstallHadPreviousRuntime and InstallRecoverySucceeded then
      WizardForm.FinishedLabel.Caption :=
        InstallValidationMessage + ' 已恢复旧版，请检查安全软件后重试。'
    else if InstallRecoverySucceeded then
      WizardForm.FinishedLabel.Caption :=
        InstallValidationMessage + ' 请检查安全软件后重试。'
    else
      WizardForm.FinishedLabel.Caption :=
        InstallValidationMessage + ' 安装目录已保留，防止继续误删；请关闭本窗口后联系维护人员处理。';
    exit;
  end;
  if not HidHelperInstallSucceeded then
    WizardForm.FinishedLabel.Caption :=
      '无线麦已安装；自定义按键映射可稍后在程序内启用。';
end;

function ShouldLaunchInstalledApplication(): Boolean;
begin
  Result := (not InstallValidationFailed) and InstallFilesCompleted;
end;

function GetCustomSetupExitCode(): Integer;
begin
  if InstallValidationFailed then
    Result := InstallValidationFailureExitCode
  else
    Result := 0;
end;

function InitializeUninstall(): Boolean;
var
  BarrierError: String;
  ResultCode: Integer;
  StopScript: String;
begin
  Result := True;
  if ShellIsUserAnAdmin() then
  begin
    MsgBox(
      '请关闭卸载程序，然后普通方式重新打开。无线麦按当前 Windows 账号卸载，不能以管理员身份运行卸载程序。',
      mbError,
      MB_OK
    );
    Result := False;
    exit;
  end;
  if not AcquireInstallerMaintenanceMutex() then
  begin
    Result := False;
    exit;
  end;
  StopScript := ExpandConstant('{app}\stop-app.ps1');
  if not FileExists(StopScript) then
  begin
    MsgBox(
      '无法找到无线麦进程清理程序。卸载尚未开始，请修复或重新安装当前版本后重试。',
      mbError,
      MB_OK
    );
    ReleaseInstallerMaintenanceMutex;
    Result := False;
    exit;
  end;

  if not StopApplicationForUninstall(StopScript) then
  begin
    ReleaseInstallerMaintenanceMutex;
    Result := False;
    exit;
  end;

  if not AcquireLegacyRuntimeBarriers(BarrierError) then
  begin
    MsgBox(BarrierError, mbError, MB_OK);
    ReleaseInstallerMaintenanceMutex;
    Result := False;
    exit;
  end;

  if not StopApplicationForUninstall(StopScript) then
  begin
    ReleaseLegacyRuntimeBarriers;
    ReleaseInstallerMaintenanceMutex;
    Result := False;
    exit;
  end;

  if not RunApplicationMaintenance('--uninstall-hid-helper', ResultCode) then
  begin
    MsgBox(
      '管理员按键组件未能移除，卸载尚未开始。请确认管理员权限后重试。',
      mbError,
      MB_OK
    );
    ReleaseLegacyRuntimeBarriers;
    ReleaseInstallerMaintenanceMutex;
    Result := False;
  end;
end;

procedure DeinitializeUninstall();
begin
  ReleaseLegacyRuntimeBarriers;
  ReleaseInstallerMaintenanceMutex;
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
