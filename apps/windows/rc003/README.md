# Remote Mic — Windows client (RC003)

> **状态：已获得部分真实硬件验证、仍待完整复测的源码/构建候选。** 本目录包含
> 跨平台协议测试，以及针对 WinRT BLE、Raw Input、SendInput 和 PortAudio 的
> Windows CI/构建流程。CI 可以证明代码能够编译并通过 Windows API 调用契约
> 测试；真实小米蓝牙遥控器 2 Pro / RC003 已验证多候选连接、13 个实体按键在
> 映射页识别、记事本普通映射、有效语音 PCM 和可见识别文字。当前未发布源码已
> 将正式语音能力收敛为“实体话筒键按住说话”；开关型语音和其他按键上的旧语音
> 配置会整键停用，等待用户重新选择并保存。最新时序、重连、休眠和长期运行仍
> 需要当前候选真机复测。
> 当前产物未签名，也不会自动安装虚拟音频驱动。完整冻结包会带入固定版本、固定
> 哈希的 Frida Gadget，但 HID tap 仍只在桥接进程已有管理员权限时可用；VB-CABLE
> 仍是需要用户明确安装的可选第三方组件（见下文）。

这是本仓库独立维护的 Windows RC003 客户端，面向小米蓝牙遥控器 2 Pro / RC003，
提供按键映射和 ATVV
（Android TV Voice-over-BLE）语音桥接。项目整体说明请阅读仓库根目录的
`README.md`。

维护者需要先阅读 `WINDOWS-ARCHITECTURE-LEDGER.md`。它集中说明项目背景、
当前范围、Windows 版的进程、线程、BLE/ATVV、Raw Input、Frida、音频、配置、
安装和验证边界；具体故障和批次证据再分别查阅 `bugs/`、`MAINTENANCE.md`
与 `reviews/`。

需要决定是否进入完整检查、本地测试包、候选、正式发布、复用已有结果，或长任务
确需分阶段和安排复核批次时，读取
[`VALIDATION-AND-DELIVERY.md`](VALIDATION-AND-DELIVERY.md)。本文只提供实际命令和
使用入口；普通定向检查不需要先读专项，命令存在也不表示每次修改都必须执行。

设置窗口当前只提供 **小米 RC003**，使用桥接、虚拟输出和 13 键映射界面。
DJI Mic 2 不再出现在当前设备选择和自动诊断中；既有设备档案与底层识别代码仅作
旧配置兼容，不属于当前用户功能。

## 中文安装与使用说明

> 本节面向想要试用这个候选版本的用户；后面的技术说明用于开发者和维护者。
> 本候选已完成部分真实 RC003 路径验证，仍有明确的真机复测队列；未签名，
> 首次运行可能触发 SmartScreen 提示。

### 界面截图

![连接设置页](../../../docs/screenshots/settings-connection.png)

![按键映射页](../../../docs/screenshots/settings-buttons.png)

> 截图在 Windows 11 + RC003 实测环境拍摄；如与你的系统主题/分辨率不同属正常差异。

### 系统要求

- Windows 10 1809（内部版本 17763）或以上，64 位；
- 未签名安装包/可执行文件：首次运行时 Windows SmartScreen 可能提示
  "Windows 已保护你的电脑"——这是预期行为，不是错误，本项目目前没有代码
  签名证书。

  在点击"仍要运行"之前，建议先核对文件的 SHA-256 校验值是否与同一次构建
  产出的 `SHA256SUMS.txt` 一致。以 PowerShell 为例：

  ```powershell
  Get-FileHash -Algorithm SHA256 .\RemoteMicRC003Setup-<版本号>-unsigned.exe
  ```

  把输出的 `Hash` 值（不区分大小写）与 `SHA256SUMS.txt` 中同一个文件名那
  一行的哈希值逐字比较；只要有一个字符不一致就不要运行，重新下载或联系
  发布者核实。核对一致后，再点击 SmartScreen 提示中的"更多信息"，然后点击
  "仍要运行"。

### 获取构建产物

首选来源是本仓库的 Releases 列表页——这是列表页本身，不是指向某个具体
tag 的链接，因此始终是获取最新预发行版的稳定入口，请直接使用这个地址：

  https://github.com/miaomiaozii/windows-remote-mic-app/releases

在列表中找到本 RC003 Windows 候选对应的预发行版（预发行版会明确标记为
prerelease，发布说明会写清楚它基于哪一次真实 Windows CI 运行）。

预发行版的仓库级 tag（例如 `v0.3.0-windows-rc003-candidate.1`）只是发布
编号，和资产文件名里的内部构建版本号是两回事：当前内部构建版本号固定为
`0.1.0-candidate`（来自安装器脚本
`installer/RemoteMicRC003Setup.iss` 的 `AppVersion`）。不要因为
文件名里的版本号和 tag 不一致就怀疑下载错了文件，具体对应关系以该
预发行版自己的发布说明为准。

每个 RC003 Windows 候选预发行版恰好包含以下三个文件，文件名精确匹配这个模式（下面的
`<版本号>` 就是上面说的内部构建版本号，不是 tag）：

- `RemoteMicRC003Setup-<版本号>-unsigned.exe`——安装器；
- `RemoteMicRC003-<版本号>-portable-unsigned.zip`——便携版（解压后
  得到一个已带版本号的顶层文件夹，里面除程序本体外还包含
  LICENSE.txt/COPYRIGHT.txt/THIRD_PARTY_NOTICES.md/ATTRIBUTION.md/
  README.txt，和安装器携带的说明与授权文件相同）；
- `SHA256SUMS.txt`——覆盖以上两个文件的哈希清单，来自同一次构建，用于
  上一节"系统要求"所说的哈希核对。

只需要下载安装器**或**便携版其中一个，不需要两个都下载；两者内容等价，
都来自同一次真实 Windows CI 运行——安装器会安装到当前用户目录，便携版
解压即用、不需要安装。

也可以使用以下备选来源：

- `.github/workflows/windows-rc003-ci.yml` 在真实 Windows GitHub Actions
  runner 上产出的、结构相同的未签名便携版 ZIP、安装器 `.exe` 与
  `SHA256SUMS.txt`（作为该次 CI 运行的构建产物而不是正式发布，需要登录
  GitHub 账号后在对应 Actions 运行页面下载）——下载后同样请自行核对哈希
  再使用；
- 或在一台 Windows 机器上自行运行 `.\build\build-candidate.ps1` 从源码
  构建（见下方"Building an unsigned candidate"一节）。

### 安装

安装器和便携版是两种不同的使用方式，步骤不完全一样，分别说明如下；
后面"首次使用、停止/重启、卸载"一节也会按这两种方式分别给出步骤。

**方式一：安装器（提供 Start Menu 入口）**

运行安装器：只安装到当前用户目录，不请求管理员权限，不设置开机自动启动，
不安装任何驱动。安装完成后可以选择打开"设置"，但不会自动以无参数方式启动
桥接——桥接模式需要在 Start Menu 中显式点击"启动"，或在设置窗口“设备”页点击
“启动”（见下方“首次使用”一节）。安装器的 Start Menu
分组固定提供"设置""启动""停止""卸载"四个独立入口；主快捷方式与桌面快捷方式
默认都打开"设置"，不会直接进入桥接模式。

**方式二：便携版 ZIP（解压即用，没有 Start Menu 入口）**

把便携版 ZIP 解压到你自己选择的目录：不请求管理员权限，不安装任何驱动，
不写入 Start Menu 或桌面快捷方式，不设置开机自动启动。便携版**没有**
安装器提供的"设置""启动""停止""卸载"四个 Start Menu 入口，也没有打包
停止脚本或卸载程序；桥接启动后可从 Windows 右下角通知区域打开设置或
正常退出，具体步骤见下一节"便携版 ZIP 用户"。

### 配对 RC003

1. 同时长按遥控器的【主页键】+【菜单键】，直到遥控器进入配对广播状态；
2. 打开 Windows"设置 → 蓝牙和其他设备"，等待遥控器出现后完成配对；
3. 程序按蓝牙名称自动查找已配对设备，不需要手动输入地址；找到 0 个或
   超过 1 个匹配设备时会拒绝猜测并报错退出，而不是随意连接一个。

### （可选）安装 VB-CABLE 作为虚拟麦克风

本程序不会自动下载、安装、启用或卸载任何虚拟音频驱动，也不会修改
Windows 默认输入/输出设备。如果要让语音识别/听写软件把 RC003 的语音当作
一个"麦克风"使用，需要自行从 VB-Audio 官方网站下载并安装官方
[VB-CABLE](https://vb-audio.com/Cable/)，然后按下面的方向手动配置——
方向不能弄反：

- Remote Mic“语音”页的“输出端点”（语音输出设备） → 优先选择
  `CABLE Input — Windows WASAPI`，没有 WASAPI 时可选
  `CABLE Input — Windows DirectSound`（VB-CABLE 虚拟"扬声器"一侧）；
- 语音识别/听写软件的麦克风输入设置 → 选择 `CABLE Output`
  （VB-CABLE 虚拟"麦克风"一侧）。

不要选择 `Windows WDM-KS` 视图：PortAudio 能枚举它，但当前阻塞播放 API
不能打开它。新版本会隐藏并拒绝该视图，保存前还会实际打开一次端点。两边
选成同一个名字，或方向选反，都会让语音功能失败，但普通按键映射仍然正常。

### 独立测试 Windows 系统听写（Win+H）

这一步只用于排查 Windows 自己的听写链路，不是豆包输入法的快捷键验收。
在记事本中打开一个可编辑文本框，先手动按一次 `Win+H`，确认听写栏出现并且
说话后能输入文字。Windows 11 的联机语音识别入口是
`设置 → 隐私和安全性 → 语音`；Windows 10 的入口是
`设置 → 隐私 → 语音`。如果使用 VB-CABLE，系统或听写软件的麦克风输入必须
选择 `CABLE Output`。Win+H 手动测试通过后，再继续测试 RC003；这只能说明
Windows 系统听写可用，不能替代上方的豆包输入法快捷键测试。

**一键随包安装（XRBM-031，可选）**：打开设置窗口“语音”页，“虚拟音频”一行会
显示 CABLE Input/CABLE Output 两个端点当前是否存在。点击“安装虚拟音频”会先
弹出一个说明对话框
（VB-CABLE by VB-Audio，独立 Donationware，非 GPL 项目代码，可自愿捐赠/
购买授权，仅随包提供基础版、不含付费的 A+B/C+D，安装会改变系统状态并需要
重启），确认后才会解压随安装包携带的官方 `VBCABLE_Driver_Pack45.zip`（构建
时已用固定的 SHA-256 校验过，未被本项目修改），并以 Windows 用户账户控制
(UAC) 提示启动官方原始的 `VBCABLE_Setup_x64.exe`——本程序自身全程不以管理员
身份运行，UAC 提示可以随时取消，取消不会安装任何内容。安装完成后需要重启
电脑，重启后回到“语音”页点击第一行的“应用”。程序会重新检测 VB-CABLE，
优先采用检测到的 CABLE Input，并按 WASAPI、DirectSound 的顺序选择、预检和
保存语音输出端点；“输出端点”下拉仍允许手动选择，选择后立即预检并自动保存
（仍需要按方向手动把
听写/识别软件的麦克风输入设为 `CABLE Output`）。这个入口只是把上面的手动
下载步骤换成随包、离线、显式确认的流程，效果完全一致；仍然可以选择直接从
<https://vb-audio.com/Cable/> 手动下载安装。

“麦克风隐私”只打开 Windows 麦克风权限页，供用户确认桌面语音软件可以访问
麦克风；程序不会把 `CABLE Output` 改成 Windows 默认麦克风，也不显示容易与
目标语音软件自身输入设备设置混淆的“声音输入”入口。

安装并选择端点后，可以在“语音”页“测试验证”的“声音通道”一行点击
“测试通道”。测试只在用户点击后运行；遥控器服务正在运行时会先要求确认，确认后
临时正常退出服务，测试结束再自动恢复。它会发送约一秒
合成扫频，并确认信号是否从 `CABLE Input` 到达同一音频接口下的
`CABLE Output`。测试不保存声音、不修改 Windows 默认设备，但正在监听
`CABLE Output` 的其它程序也可能收到这段测试信号。完整音频测试在一次性子进程
中运行；音频驱动调用卡住时会结束该测试进程，不让设置窗口退出无限等待。通过只
代表本地虚拟音频通道正常，不代表 RC003 已传声、输入法已开启或文字已经上屏。

### 首次使用、停止/重启、卸载

打开设置后，“设备”页的当前设备只提供“小米蓝牙语音遥控器 2 Pro（RC003）”，
“语音”页维护 CABLE 输出、语音程序和语音按键，“按键”页维护 13 键映射。旧配置
若曾保存 DJI Mic 2，本次运行会
按 RC003 显示，但仅打开设置不会自动改写原配置。

“打开设置并选择语音输出端点”“确认按键映射”“手动确认已配置的语音组合键”这几步在
两种安装方式下目标一样，但具体怎么打开设置、怎么启动/停止/卸载不同
——安装器用户走 Start Menu，便携版用户在解压出的文件夹里用命令和任务
管理器，分别在下面两个小节说明。

**安装器用户**

1. 打开“设置”（Start Menu 中的“Remote Mic · RC003”或
   “Remote Mic · RC003 设置”），先在“设备”页检查 RC003 和按键接收，再到“语音”
   页点击“应用”检测并选择 CABLE Input，再选择语音程序和语音按键；端点下拉、
   语音程序和语音按键会各自自动保存，不需要到“按键”页另行保存；
2. 启动遥控器服务有两种等价方式：回到“设备”页点击“启动”，或者关闭设置窗口后
   从 Start Menu 选择“启动 Remote Mic · RC003”（它会以 `--bridge` 参数启动）。
   “设备”页的“启动”使用上次已经保存的设置，不会顺带保存其它页面尚未应用的修改；
   遥控器服务一行会
   显示未运行、正在启动、等待 RC003、RC003 已连接或启动失败等真实阶段。只有
   桥接运行状态确认设备连接后，界面才显示“RC003 已连接”；实际按键和语音是否
   可用仍需按下面步骤真机验证。桥接运行后，Windows 右下角通知区域会出现 Remote Mic
   图标；双击图标打开设置，右键可选择"打开设置"或"退出桥接"。关闭
   设置窗口只关闭设置，不会停止桥接；
3. 按一下已映射为普通动作的按键（例如方向键、确定键）确认按键映射生效；
4. 在“语音”页填写唯一的“语音按键”，并让它与输入法自身设置
   一致；只有实体话筒键可以选择“按住说话”，其他按键只能选择普通动作或组合键。
   新安装、新建配置和“恢复按键与语音默认”继续使用右侧 Alt；升级不会覆盖旧配置
   中已经保存的右侧 Alt 或自定义快捷键。`Alt + Space` 经 UU 远程会打开窗口左上角
   系统菜单，不再作为建议组合；曾评估的 `Ctrl + Alt + F8` 经 UU 后按住时长不稳定，
   也没有作为默认值发包。可按宿主实际设置修改，但不要把 Windows `Win+H` 当作
   豆包快捷键。设置页只校验组合键能否解析，无法
   判断目标语音软件是否已经正确注册或被其他软件抢占，因此仍需先手动确认对应
   按住语义本身能正常工作：
   打开记事本（或任意可编辑文本框），把光标点进文本区域，用实体键盘按下组合键，
   确认豆包语音功能出现并能输入文字。豆包的麦克风输入设备也必须选择
   `CABLE Output`（如果按上一节配置了 VB-CABLE）。手动测试通过后，光标保持在
   同一个可编辑文本框中，再按住遥控器话筒键说话、松开结束；如果手动快捷键都无法
   启动豆包，请先解决豆包快捷键或输入设备配置问题，本程序不能让本来就不工作的
   豆包输入法变得可用。本程序只执行标准按住开始、松手结束，不会在松手后再
   点按一次快捷键；若宿主窗口在实体键盘正常抬键后也不会关闭，需要调整宿主
   自身的快捷键或语音模式；
   “语音程序”是同页的可选设置：默认“不管理”，不会改变现有快捷键链路。
   选择搜狗后会自动查找已安装的搜狗语音组件；选择微信输入法后会从运行进程、
   安装记录、开始菜单和标准安装目录自动识别，新电脑正常安装后无需寻找路径，
   且由 Windows 管理，不随遥控器服务强行启动。Windows 语音输入也是下拉中的一种
   语音程序，选择后可直接采用 `Win+H`；选择“自定义程序”时可指定 `.exe` 或
   `.lnk`。搜狗和自定义程序可随服务尝试启动，查找或启动失败不会阻止服务。只有
   勾选“管理员启动”时才会出现 UAC；
5. 需要时右键通知区域图标选择"退出桥接"，或从 Start Menu 选择
   "停止 Remote Mic · RC003"结束桥接，
   或从"设置 → 应用"/Start Menu 的"卸载"条目卸载（卸载会先自动停止
   正在运行的进程，再删除安装时写入的程序文件）。遇到按键/语音/启动
   问题时，可以在“设备”页点击“日志目录”直接定位到
   `%LOCALAPPDATA%\RemoteMic\RC003\logs`；如果这台电脑上程序还
   从未运行过，日志目录本身也不存在，该按钮会如实提示，而不是伪造一份
   日志。**卸载不会自动删除设置和日志**：`config.json`、`key_bindings.json` 和 `logs\app.log`
   会一直保留在 `%LOCALAPPDATA%\RemoteMic\RC003` 下，因为
   安装脚本没有为这些运行期生成的文件配置卸载删除规则。如果这台
   电脑上不会再安装任何 RC003 版本（安装器或便携版）、也不需要
   保留这些设置和日志，可以在卸载完成后手动删除整个
   `%LOCALAPPDATA%\RemoteMic\RC003` 文件夹；如果还会用到
   同一台电脑上的另一个 RC003 安装，请不要删除这个共享目录。

**便携版 ZIP 用户**

便携版没有停止脚本，也没有打包 Start Menu 入口或卸载程序；
下面每一步都在解压出的文件夹里手动执行：

1. 直接双击 `RemoteMicRC003.exe`（不带参数）即可打开设置窗口——这是默认
   行为；也可以在 PowerShell 里运行 `.\RemoteMicRC003.exe --settings`。在“语音”
   页点击“应用”检测并选择 CABLE Input，再选择语音程序和语音按键；端点下拉、
   语音程序和语音按键会各自自动保存；
2. 启动遥控器服务有两种等价方式：在“设备”页点击“启动”（使用上次已经保存的
   设置，不保存其它未应用修改）；或者关闭设置窗口，在同一个
   文件夹里运行 `.\RemoteMicRC003.exe --bridge` 启动桥接进程
   （不带参数会再次打开设置窗口，所以服务必须显式用 `--bridge`）。“遥控器服务”
   一行会显示未运行、正在启动、等待 RC003、RC003 已连接或启动失败等真实阶段；
   只有桥接运行状态确认设备连接后，界面才显示“RC003 已连接”。
   桥接运行后，Windows 右下角通知区域会出现 Remote Mic 图标；双击打开
   设置，右键可打开设置或退出桥接。关闭设置窗口不会停止后台桥接；
3. 按一下已映射为普通动作的按键（例如方向键、确定键）确认按键映射生效；
4. 手动确认豆包输入法语音快捷键的步骤和上面"安装器用户"一节完全相同（打开
   记事本、光标点进可编辑文本框、按下已配置的组合键、确认豆包能输入文字、
   确认豆包麦克风输入选择的是 `CABLE Output`），这里不重复；
5. **停止**：右键 Windows 右下角通知区域的 Remote Mic 图标，选择
   "退出桥接"。该入口会先正常清理 BLE、HID、语音热键与音频资源再结束
   进程；只有托盘不可用且程序无法正常退出时，才使用任务管理器结束
   `RemoteMicRC003.exe`；
   **卸载/移除**：便携版没有安装程序，不写注册表；删除整个解压出来
   的文件夹即可移除程序本体。但便携版运行时同样会把 `config.json`、
   `key_bindings.json` 和 `logs\app.log` 写到
   `%LOCALAPPDATA%\RemoteMic\RC003`（和安装器用的是同一个
   目录）——删除解压文件夹**不会**清除这些设置和日志文件。如果这台
   电脑上不会再用到任何 RC003 安装（便携版或安装器）、也不需要保留
   这些设置和日志，可以额外手动删除整个
   `%LOCALAPPDATA%\RemoteMic\RC003` 文件夹；如果还会用到
   同一台电脑上的另一个 RC003 安装，请不要删除这个共享目录。

### 默认按键映射与特殊行为

RC003 共 **13 个可映射物理按键**。麦克风图标键默认承担“按住说话”，也可以
改为普通动作；其他按键不能传送遥控器声音，只能选择普通动作或组合键。双击和
长按不允许语音动作。旧开关型、非话筒键语音或次级手势语音配置升级后会整键
停用，必须重新选择并保存，程序不会把它们悄悄转换成按住说话。遥控器**没有独立
的物理静音键**（“系统静音”只是可选的手动绑定，不是任何按键的默认映射）；
“返回”默认映射为退格动作。设置页的“检测真实按键”同时使用 Raw Input 与可选
HID tap；后者补齐 Windows 普通输入链路丢失的返回和音量键。

方向、退格和音量动作可以在持续按住时连续执行；自定义组合键始终按“一次实体
按下对应一次完整点按”处理，不再定时重复组合键，以降低修饰键未释放和意外连发
风险。为普通话筒键配置的双击/长按仍按普通手势处理。

普通动作输入框也可以直接填写 Quicker 的
`quicker:runaction:动作ID或名称?参数`。程序只接受这一种 Quicker 动作协议，不会
把任意 URI 或命令交给 shell；触发后由 Windows 交给已安装的 Quicker 处理。Quicker
需要正常注册该协议，且按其[外部启动说明](https://getquicker.net/kc/manual/doc/quicker-starter)
不应以管理员身份运行。普通权限桥接和管理员桥接下的实际触发仍要分别真机验证。

“按键”页顶部可在“单键与手势”和“遥控器组合”之间切换，恢复默认和保存操作固定
在页面底部。遥控器组合只提供一层类似 Fn 的主键：主键只能选 TV、菜单或主页，
第二键只允许方向、确定、返回和音量加减；话筒、电源及主页+菜单配对组合不参与。
组合主键不能再设置双击或长按。组合命中后吞掉主键与第二键的单键动作；没有命中时，
主键松开后仍执行原单击。一次按住主键可以依次触发多个已配置的第二键。

13 个实体按键已在真实设备的映射页识别，普通映射也已在记事本中执行。Frida
HID tap 在独立线程上报事件，低层钩子吞掉对应原生键并只注入映射动作；全部
按键的长按重复和长期稳定性仍按 `TESTING.md` 队列复测。麦克风键改为普通动作、
按住说话的松手收尾选项，以及旧语音配置停用提示仍要在当前候选上完成真机复测，
不能由自动测试或旧候选结果代替。

### 隐私与来源、真机验证事项

不持久化保存真实蓝牙地址、HID 路径或设备令牌；本候选源自同一 GPL-3.0
参考项目的 Windows 实现，并在本仓库中完成了品牌、构建和说明适配。历史候选
曾记录“已通过真实硬件验收”，包括配对、逐键和豆包语音；但 2026-08-20
重新复核后陆续发现 WDM-KS 无声、设置页漏键、切换竞态和运行中语音设置未应用。
后续实测已经证明多候选连接、逐键识别、有效语音 PCM、可见识别文字和 F5 防漏，
遥控器点按持续传声的最后一条协议探针随后也未收到启动信号，因此当前产品只保留
实体话筒键按住说话。宿主侧按程序适配：微信输入法按言灵已验证的顺序优先点击
`wetype.statusbar.window` 状态栏语音按钮，失败时以 80 ms 的无标记虚拟键
`SendInput` 短点兜底，并在音频停止后按本轮实际成功路径结束；提交后后台最多等待
5 秒，只有微信面板仍残留时才请求关闭。搜狗等按住型程序仍发送按下和抬起。微信
专用协议与其它按住型程序仍待当前源码真机复测。历史记录
不能替代当前构建的验收；详见
`MAINTENANCE.md` 与 `TESTING.md`。只有用户明确确认后，程序才会通过 Windows 的
`runas`/UAC 启动 VB-Audio 官方安装器；Remote Mic 自身不会提权。
用户还可以在“语音”页明确选择让 Windows 以管理员权限启动搜狗或自定义第三方
语音程序；微信输入法和 Windows 语音输入由系统管理，不提供该选项。该操作不会提升
Remote Mic 自身，也不会把第三方程序打包进本项目。

## 功能实现

Windows 客户端围绕 RC003 使用场景实现，主要功能如下：

- 使用 **WinRT BLE** 查找已配对的 RC003，并按设备名称精确匹配；找到 0 个或多个候选时都会拒绝猜测。服务与特征读取使用 `BluetoothCacheMode.UNCACHED`，避免 Windows 返回过期枚举。
- 使用 Windows **Raw Input** 接收遥控器普通按键，并校验选中的 HID 路径，避免误接收另一只相同型号设备的事件。
- 对 Windows Raw Input 丢失的 HID usages，可选复用上游的 Frida Gadget WUDFHost tap。该 tap 上报遥控器的**全部键盘 usage**（返回 `0xF1`、音量 `0x80/0x81`、方向/OK/Home/Menu/TV/Power 等），作为所有普通按键的输入旁路：它在独立 socket 线程上 arm，低层键盘钩子零等待匹配并吞掉原生键，只注入一次映射动作，解决一次按键两次触发的问题。只有显式下载并校验 Gadget 后才会启用；Remote Mic 不会自动提权，需要 tap 时用户必须从已明确提升权限的终端启动桥接。
- 连接 ATVV GATT 服务，协商能力，接收并解码 16 kHz IMA/DVI ADPCM 语音帧。
- 使用 **PortAudio** 把解码后的语音写入用户明确选择的输出端点（WASAPI 优先、DirectSound 回退，拒绝阻塞 API 不支持的 WDM-KS；按端点能力输出立体声并复制声道；16 kHz → 48 kHz 有状态连续插值；解码后经 20 Hz 高通 DC 阻挡和 +10 dB 增益）；语音页选择或检测端点时会真实预检并独立保存，不会自动使用 Windows 默认设备。
- 语音快捷键按目标程序选择注入方式：微信输入法先尝试状态栏语音按钮，失败时按言灵的已验证方式分别发送无私有标记、全虚拟键的 `SendInput` 按下与抬起批次，中间保持 80 ms；其它现有语音程序继续使用带私有标记的虚拟键 `keybd_event`。新配置默认发送右侧 Alt；已有配置继续保留原值。`DoubaoPhysicalizer` 只为右侧 Alt 配置处理豆包 `ImeService.exe` 的 injected 标记。通用三键仍可作为自定义快捷键被解析和发送，但 `Ctrl + Alt + F8` 经 UU 的按住时长不稳定，没有作为默认值发包。界面仍只保存一套“按住说话”快捷键，不增加点按说话；选择 Windows 语音输入时可采用 `Win+H`，但它不替代豆包输入法自己的快捷键验收。UU 远程的按住边沿仍按 `TESTING.md` 做真机验收。
- “语音程序”是独立的可选管理层，默认关闭。当前内置搜狗语音组件、微信输入法和 Windows 语音输入，并支持用户指定其它 `.exe`/`.lnk`。微信输入法和 Windows 语音输入由系统管理；搜狗和自定义程序可随遥控器服务尝试启动。普通启动沿用 Remote Mic 当前权限，管理员启动必须由用户勾选并通过 Windows UAC；程序缺失、UAC 取消或启动失败都不会阻止服务。
- 提供 RC003 的 13 键统一映射界面，以及一层受限的遥控器组合映射。普通动作和组合动作均可使用内置动作、电脑快捷键或 `quicker:runaction:` URI。只有实体话筒键可选择“按住说话”，它也可以改为普通动作；其他实体键只提供普通动作。电源、返回、音量等特殊键仍在扫描码层抑制原生边沿后执行所选动作。
- 提供“设备”“按键”“语音”三个设置页面。设备与权限检查结果放回对应业务行，仍区分自动检查和“需要手动验证”，不会把进程存活伪装成硬件验收通过。
- 设置窗口使用 PySide6 Essentials + Qt Quick/QML；便携版和安装器都通过 PyInstaller 打包，不要求终端用户另装 Python 或 Qt。**单个 `RemoteMicRC003.exe`**：双击（无参数）或 `--settings` 打开设置窗口，`--bridge` 启动桥接。同一 Windows 登录会话只保留一个设置窗口；再次双击程序、任务栏或通知区域入口时，只恢复并置前已有窗口，不再新开一份。
- 后台桥接在 Windows 右下角通知区域提供控制图标；双击打开设置，右键可打开设置或正常退出桥接。设置窗口与桥接进程仍保持分离，关闭设置窗口不会意外中断遥控器。
- 配置写入采用临时文件 + fsync + `os.replace` 原子替换，失败不会留下半写的 JSON；桥接进程在下一次按键或语音事件前按 mtime 一起热加载按键映射和按住说话设置，活跃会话会在收尾后再切换，磁盘数据损坏时保留最后一份有效设置。连接设备和音频输出端点仍需重启桥接。
- VB-CABLE 只是可选的语音路由方案。只有用户明确点击并确认 UAC 后，才会通过 `runas` 启动 VB-Audio 官方安装器；程序自身不会提权，也不会修改系统默认输入/输出设备。

## 运行架构

源码入口位于 `apps/windows/rc003/src/ovb_rc003`，内部 Python 包名仍保留
`ovb_rc003`，这是为了便于持续吸收上游 Windows RC003 的修复；用户可见的产品名、
可执行文件名、安装器名称和文档均使用 Remote Mic。

运行流程大致如下：

1. “语音”页的输出端点、语音程序和按住说话快捷键各自自动保存；第一行“应用”只负责重新检测 VB-CABLE 并选择可用的 CABLE Input。“按键”页单独保存单键、手势和遥控器组合映射。保存时会校验电脑组合键、Quicker URI、组合主键冲突、只有实体话筒键可传声的边界和次级动作边界；音频端点选择会执行 open/start/stop/close 预检，再以原子写 + 回读校验落盘。桥接进程在下一次输入事件前热加载映射和语音设置；输出端点变更仍在重启后应用。
2. 桥接进程启动单实例保护和通知区域控制图标，并由连接监督器负责 BLE
   会话、Raw Input 监听和重连。托盘退出会取消主运行任务，进入同一套
   BLE、HID、热键与音频清理流程；双击托盘图标以参数数组启动设置入口，
   不经过 shell。服务/特征读取使用 `BluetoothCacheMode.UNCACHED`，避免
   陈旧缓存。
3. 可选的 RC003 HID tap 在配对的 WUDFHost 内读取全部键盘 usage，把方向/OK/Home/Menu/TV/Power/返回/音量边沿送入同一映射层，并在独立 socket 线程上 arm，低层钩子零等待吞掉原生键后只注入一次动作；实体话筒键按住说话时同时驱动宿主快捷键和 ATVV 音频，其他按键只进入普通动作分发。
4. BLE 断开、Raw Input 路径失效、热键发送失败或音频写入失败时，相关资源会先关闭，再按策略重连；不会继续向失效音频端点写入数据。

设备发现、音频端点和语音热键均采用“明确选择、失败即停止”的策略。程序不保存真实
蓝牙地址、HID 路径或设备令牌，也不会为了让界面显示“已连接”而猜测设备状态。

## 本地构建与测试

在 Windows PowerShell 中进入本目录后，可以使用以下命令安装开发依赖并运行完整测试。
是否需要完整测试按 `VALIDATION-AND-DELIVERY.md` 判断；普通局部任务优先运行对应的
定向测试：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py' -v
```

需要日常查看当前源码界面时，可以安装一次固定的桌面开发入口：

```powershell
.\build\install-dev-shortcut.ps1
```

桌面的“Remote Mic 开发版”会直接运行当前工作区的 Python 与 QML 源码，不使用
`dist` 中的冻结快照；代码修改后关闭并重新打开该入口即可看到最新内容。它依赖本目录
现有的 `.venv`。构建脚本开始时会结束由该入口启动并继承了开发标记的源码设置与
桥接进程，不会结束已安装版、便携版或其它 Python 程序。

构建未签名候选目录：

```powershell
.\build\build-candidate.ps1
```

如果需要恢复 Windows Raw Input 丢失的返回/音量 usages，可在构建前显式获取
上游 Frida Gadget（不会由构建脚本自动下载）：

```powershell
.\build\fetch-frida-gadget.ps1
```

该脚本会把固定版本、固定 SHA-256 的压缩资产放到被 `.gitignore` 忽略的
`src\ovb_rc003\frida_assets`。源码运行可以不获取该资产并明确降级；
`build-candidate.ps1`、Windows CI 和 PyInstaller 冻结入口都会要求并再次校验它，
缺失或哈希不符时直接停止构建，避免分发一个返回键、音量键识别不完整的程序包。
运行桥接时 tap 还会再次验证资产，定位 RC003 的 WUDFHost，并只在当前进程已有管理员
权限时尝试注入；普通启动不会弹出提权提示，权限不足只会让 tap 不可用，不会阻止 BLE、
Raw Input 可见按键或语音链路启动。需要 tap 时可在管理员 PowerShell 中启动：

```powershell
$root = (Get-Location).Path
Start-Process -Verb RunAs -FilePath (Join-Path $root '.venv\Scripts\python.exe') `
  -WorkingDirectory $root -ArgumentList '-m', 'ovb_rc003'
```

本地 `build\build-candidate.ps1` 会先执行公开边界检查和完整测试，再构建
`dist\RemoteMicRC003` PyInstaller 目录并检查冻结入口；它不会自行生成 ZIP、
Inno Setup 安装器或 `SHA256SUMS.txt`。完整发布封装由 Windows CI 工作流完成，
安装器编译需要可用的 Inno Setup。构建脚本会通过固定哈希的独立步骤获取
Frida Gadget 和 VB-CABLE 官方压缩包；程序运行时不会静默下载二进制或驱动。

该脚本成功后，公开边界、完整测试和同一构建 EXE 的 `--dry-run` 已经完成；同一源码、
依赖、构建输入和产物状态下，不要在脚本外机械重跑。下面的独立命令用于尚未运行
构建脚本、原结果已经失效，或单独排查冻结入口时使用。

也可以只验证冻结后的程序是否能导入全部模块：

```powershell
.\dist\RemoteMicRC003\RemoteMicRC003.exe --dry-run
```

### 按键采集、回放与动作适配

不要直接修改 `raw_input_windows.py` 里的扫描码来猜测返回键或音量键。先用被动采集
工具确认 Windows 实际交付的是键盘事件还是 HID report，再把稳定的物理签名绑定到
RC003 的逻辑键；采集过程不会执行任何已配置动作。

在本目录的 PowerShell 中运行：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
.\.venv\Scripts\python.exe src\rc003_key_test.py guided --duration 20
```

更推荐使用按键向导：它会依次提示返回、音量+、音量-，每个键完整按下并释放后
自动进入下一个键，不显示鼠标或其他设备的 Raw Input，也不会执行音量或删除动作。
如果需要逐个手动运行，也可以使用：

```powershell
.\.venv\Scripts\python.exe src\rc003_key_test.py capture --assign back
.\.venv\Scripts\python.exe src\rc003_key_test.py capture --assign volume_up
.\.venv\Scripts\python.exe src\rc003_key_test.py capture --assign volume_down
```

每次命令只测试一个键：按一下目标键并完整释放。工具会在
`%LOCALAPPDATA%\RemoteMic\RC003\captures` 保存 JSONL 原始样本；只有同时采集到按下、释放
且没有解码错误时，才会把物理签名写入 `key_bindings.json` 的 `physical_bindings`。失败或
超时不会污染已有绑定，也不会写入 HID 路径或蓝牙地址。绑定完成后重启桥接，再用下面的
命令离线确认样本仍然同时包含按下和释放：

```powershell
.\.venv\Scripts\python.exe src\rc003_key_test.py replay `
  --input "$env:LOCALAPPDATA\RemoteMic\RC003\captures\<capture>.jsonl" `
  --button back
```

如果尚未安装可选 Frida Gadget，或 tap 日志显示没有找到 RC003 WUDFHost，普通采集工具
在按键时仍然完全没有事件，再运行广域 Raw Input 探针。它只记录
完整的 `WM_INPUT`，不会执行映射或注入按键；`--seconds` 到期后会自动退出：

```powershell
.\.venv\Scripts\python.exe src\rc003_broad_raw_probe.py `
  --seconds 30 `
  --output "$env:LOCALAPPDATA\RemoteMic\RC003\logs\broad-raw-probe.jsonl"
```

看到 `RC003 HID report tap state: ready` 后，依次只按一个目标键并完整释放。若日志有 `raw_input`，先保留
其中的 `raw_type`、`body`、键盘字段和 `path` 交给解码适配；不要把 `path` 或蓝牙
地址复制进 `key_bindings.json`。若连广域探针也没有 `raw_input`，但 tap 已显示 ready
后仍没有 `RC003 direct HID usage down`，问题在配对设备的 HidOverGatt 注入/报告链路，
而不是按键动作映射。

若回放通过但动作仍不对，问题在语义动作配置而不是物理识别；在设置页检查该逻辑键
的 Windows 动作。语音键不应通过普通 `physical_bindings` 伪装成普通动作，必须单独
验收：先手动确认配置的**豆包输入法语音快捷键**在文本框中可启动，再按遥控器语音键，
同时检查 ATVV 音频、输出端点和日志中的 voice lifecycle。Windows `Win+H` 只能作为
另一个独立的系统听写适配目标，不能替代豆包输入法验收。

公开边界检查会阻止源码、日志、测试输出或未审查的本地路径进入公开发布范围：

```powershell
.\build\check-public-boundary.ps1
```

如果同一状态的 `build-candidate.ps1` 已成功，该项结果直接复用。

Windows GitHub Actions 工作流位于 `.github/workflows/windows-rc003-ci.yml`。运行结果
可在 <https://github.com/miaomiaozii/windows-remote-mic-app/actions> 查看。CI 没有真实 RC003 硬件，
因此构建和测试通过也不能替代真机配对、按键和语音链路验收。

## 已知限制

- 当前版本未签名，首次运行可能触发 SmartScreen 提示，属预期行为。
- 完整冻结包包含经过固定哈希校验的 Frida Gadget；源码运行未执行获取脚本时，
  缺失 usages 不会被猜测或伪造。即使资产存在，也必须从管理员终端启动，并在日志中
  看到 tap ready 和真实按键边沿后，才能确认返回键、音量键的 HID 旁路可用。
- VB-CABLE 是可选的语音路由方案；未安装时语音默认没有虚拟麦克风路由，需要
  用户自行配置输出端点。当前播放实现不支持 `Windows WDM-KS`，应使用
  `Windows WASAPI`，必要时回退 `Windows DirectSound`。
- 遥控器没有独立的物理静音键；语音键的 F5 兼容事件只用于识别，不再作为普通 F5 注入到主机。
- Windows 没有一个可供本程序可靠读取的统一权限状态 API；相关入口和手动检查放在
  对应设备或语音行中，不会显示虚假的“已授权”。
- DJI Mic 2 不再提供设备选择或自动诊断入口；底层设备档案和端点识别仅保留作旧配置
  兼容，不属于本候选承诺的用户功能。
- ATVV 语音延迟与音量、长期重连稳定性仍建议在更多真实场景中继续观察。

## 隐私、许可证与来源

本 Windows 客户端不把真实蓝牙地址、HID 路径或设备令牌写入配置。日志和配置只写入
当前用户的 `%LOCALAPPDATA%\RemoteMic\RC003`，详见上面的安装/卸载说明。

代码按 GPL-3.0-only 发布。Windows 实现基于上游
<https://github.com/nijez/open-voice-bridge> 的 GPL Windows RC003 实现，具体变更
和归属记录见 `ATTRIBUTION.md`、仓库根目录 `COPYRIGHT.md` 与
`THIRD_PARTY_NOTICES.md`。VB-CABLE 是 VB-Audio 的独立 Donationware；本项目不把它
当作 GPL 代码，也不会把付费的 A+B/C+D 版本伪装成随包内容。
RC003 缺失 HID 报告的恢复路径参考 `xxb26553663-star/remote-bridge-hub` 的
Frida Gadget 实现；Frida 的版本、哈希和许可证见仓库根目录
`THIRD_PARTY_NOTICES.md`。

## 发布说明

正式候选和发布的触发条件、执行顺序与去重规则见
[`VALIDATION-AND-DELIVERY.md`](VALIDATION-AND-DELIVERY.md)。

Windows 候选版以预发行版发布。首个发布：

- 发布列表页：<https://github.com/miaomiaozii/windows-remote-mic-app/releases>
- 具体发布：<https://github.com/miaomiaozii/windows-remote-mic-app/releases/tag/v0.1.0-windows-rc003-candidate.1>

每个版本的安装器、便携版 ZIP 和 `SHA256SUMS.txt` 必须来自同一次 Windows CI
构建；发布时必须在说明中准确列出已经完成和仍未完成的真实 RC003 配对、按键、
语音与稳定性验收，不能用自动测试替代。下载哪一个、如何安装，见上文
"中文安装与使用说明"的
"下载与安装"一节。

完整的版本历史见本目录的 [`CHANGELOG.md`](CHANGELOG.md)。
