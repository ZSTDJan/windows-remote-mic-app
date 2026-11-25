# 无线麦自定义构建与更新通道

本分支是 `ZSTDJan/windows-remote-mic-app` 的个人维护分支，加入了两类改动：

1. **豆包输入法动态兼容**：不再只接受固定版本的 `ImeService.exe`。已知构建直接命中；未知构建会解析 PE，自动定位 `SetWindowsHookExW` 的键盘回调和 `CallNextHookEx` 转发点。找不到结构时仍然 fail-closed。
2. **自定义更新通道**：官方通道和自定义通道可切换，自定义版本使用自己的版本号序列。

## 版本号

自定义构建使用：

```text
1.0.112-custom.1
1.0.112-custom.2
1.0.113-custom.1
```

- `x.y.z` 继续表示上游基线。
- `-custom.N` 表示本分支的第 N 个自定义构建。
- `-candidate.N` 仍是上游预发布语义；`-custom.N` 是稳定自定义版本。
- GitHub tag 建议同名加 `v`：`v1.0.112-custom.1`。

## 选择更新通道

优先级从高到低：

1. 启动参数：

```powershell
"无线麦 win版 1.0.112-custom.1.exe" --update-channel custom
"无线麦 win版 1.0.112-custom.1.exe" --update-channel official
```

2. 环境变量：

```powershell
$env:REMOTEMIC_UPDATE_CHANNEL = "custom"
```

3. 用户文件：

```text
%LOCALAPPDATA%\RemoteMic\RC003\update-channel.txt
```

内容为 `official` 或 `custom`。

4. 当前构建的默认通道：`-custom.N` 构建默认 `custom`，普通版本默认 `official`。

官方通道仓库：`ZSTDJan/windows-remote-mic-app`

自定义通道仓库：`zyj1195078324/windows-remote-mic-app`

## 发布自定义版本

1. 同步 upstream：

```bash
git fetch upstream
git checkout main
git rebase upstream/main
git push origin main
```

2. 在 `custom/doubao-compat` 上合并/重放改动：

```bash
git checkout custom/doubao-compat
git rebase main
```

3. 更新 `apps/windows/rc003/src/ovb_rc003/VERSION` 为新的 `x.y.z-custom.N`。

4. 构建并打包：

```powershell
.\build\build-candidate.ps1
```

5. 打 tag 并发布：

```bash
git tag v1.0.112-custom.1
git push origin v1.0.112-custom.1
```

Release 资产至少包含：

- `RemoteMicRC003-1.0.112-custom.1-portable-unsigned.zip`
- `SHA256SUMS.txt`

后续增量更新可以再加 `update-manifest.json` 和 `delta-*.zip`。

## 豆包兼容逻辑

源码位置：`apps/windows/rc003/src/ovb_rc003/doubao_rpc.py`

- 已知构建表 `_VERIFIED_IME_SERVICE_BUILDS` 直接命中；
- 未命中时调用 `_find_dynamic_hook_layout()`，从 `ImeService.exe` 的导入表、`SetWindowsHookExW` 调用点和 `CallNextHookEx` 调用点推导布局；
- 推导出的入口/转发字节仍由 Frida 脚本在目标进程内校验，不匹配就失败关闭；
- 建议每次豆包大版本更新后，先在本机跑一次 `--dry-run` 和一次真实语音，再把新构建哈希加入已知表（可选，动态查找已能兜底）。

## 许可

本分支继续遵守 GPL-3.0-only。发布二进制时保留 `LICENSE.md`、`COPYRIGHT.md`、`THIRD_PARTY_NOTICES.md`、`THIRD_PARTY_LICENSES/` 等文件，并公开本分支源码。
