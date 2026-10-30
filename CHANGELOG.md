# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## v1.0.1 — 能装、能卸载、缺什么自己补

这一次的重点不是功能，是「**普通人拿到它能不能跑起来**」。

### 新增：安装包

- `ChromeDual-Setup-x.y.z.exe` —— 双击安装，不需要装 Python。
  六步走完：查环境 → 关旧实例 → 复制文件 → 建快捷方式 → 写卸载项 → 启动。
- 安装器只用标准库（`ctypes` + 控制台 + `PowerShell` 建快捷方式），
  不依赖 tkinter / WebView2 —— 因为 WebView2 正是它可能要修的东西之一。
- **升级不碰用户资料**：复制时顶层 `config` / `data` / `var` / `logs` 已存在就整棵跳过。
- 卸载走 `ChromeDual.exe --uninstall`：先问「资料要不要一起删」，
  再交给安装目录**之外**的 PowerShell 延迟删除（程序删不掉正在运行的自己）。

### 新增：依赖自动补齐（`app/deps.py`）

缺什么，弹窗列清楚，点「是」才装 —— **绝不静默联网装东西**。

| 形态 | 缺什么 | 补法 |
|---|---|---|
| 源码 | `pystray` / `pywebview` / `psutil` / `pillow` | `pip install`（无权限时自动加 `--user`），带真实进度 |
| 打包 | Xray 内核 | 从 XTLS/Xray-core 官方发布页下载 zip 并解出 `xray.exe` |
| 打包 | WebView2 运行时 | 微软官方 Evergreen Bootstrapper，静默安装 |
| 打包 | .NET Framework 4.6.2+ | 微软官方离线包（Win10/11 自带 4.8，基本不会遇到） |

界面不用 tkinter（本项目用的 Python 是精简版，没有 tkinter），
只用 `user32.MessageBoxW` 弹原生对话框 + `AllocConsole` 临时挂控制台显示进度。
**自检必须在 `import pystray` 之前** —— 否则缺库时抛的是一屏 `ImportError`，新手看不懂。

新增 `ChromeDual.exe --check`：只看依赖、不启动。exe 没有控制台，
所以同时写到 `var\logs\deps-check.txt`。「双击没反应」第一个该看的就是它。

### 新增：资源与数据分离（打包能跑的前提）

以前所有路径都从 `__file__` 往上推两层，打成 exe 后会全部指向
`sys._MEIPASS` 那个临时目录 —— 配置、资料、日志全写进临时目录，一关就没。
现在拆成三个概念：

| 变量 | 打包形态 | 用途 |
|---|---|---|
| `RES` | `_internal\` | 只读资源（assets / bin / panel.html / 配置模板 / tools） |
| `ROOT` | exe 所在目录 | 程序位置 |
| `DATA_ROOT` | exe 旁边，不可写则 `%LOCALAPPDATA%\ChromeDual` | config / data / var / bin\xray.exe |

可用环境变量 `CHROMEDUAL_HOME` 强制指定数据目录。

内核一律指向**可写**位置 `DATA_ROOT\bin\xray.exe`；
`paths.ensure_core()` 首次启动时把 `_internal\bin` 里那份复制过去 ——
不然「换内核版本」「下载内核」这些事永远做不了。

### 新增：`app/selfrun.py` —— 拉起自己只有一种写法

源码形态要 `pythonw.exe + ChromeDual.py`，打包形态直接 `ChromeDual.exe`。
以前 `cli.py` / `tray.py` 各写了一份 `sys.executable.with_name("pythonw.exe")`，
打成 exe 后就变成「拿 exe 当解释器去执行一个不存在的 .py」，双击毫无反应。
现在统一收口，另提供 `shortcut_target()` 给建 `.lnk` 用。

顺带修：`line._run_build()` 在打包形态下没有独立 Python 解释器，
改用 `runpy` 在本进程内执行配置生成脚本。

### 新增：构建工具链

- `installer/build.py` —— 一条命令出全部产物，自动算 SHA-256。
- `installer/build.bat` —— 双击构建（CRLF 换行，避免 cmd 解析出错）。
- `installer/ChromeDual.spec` / `ChromeDual-Setup.spec` / `version_info*.txt`。
- 主程序用 **onedir**（启动 1 秒内、杀软友好），安装器用 **onefile**（用户只拿一个文件）。
- PyInstaller 在受限环境里删不掉旧输出目录（企业策略 / 沙箱会拦「一次删几百个文件」）时，
  `build.py` 会自动把旧目录改名让路，不让构建因为跟代码无关的原因失败。

文档：新增 [`docs/打包与发布.md`](docs/打包与发布.md)。

---

## v1.0.0 — 首个公开版

一份 Chrome 资料 = 一个独立身份 = 一条独立代理线路。

### 核心

- **多身份 Chrome**：每条线路一份完全独立的 `--user-data-dir`，
  Cookie / 书签 / 历史 / 扩展全部隔离，线路之间互不可见。
- **按需启动**：管家启动不预热任何内核；点哪条线路起哪条线路；
  窗口全关超过 `idle_stop_seconds`（默认 120 秒）自动停内核回待机。
- **进程级代理**：只给自家 Chrome 传 `--proxy-server` / `--proxy-pac-url`，
  **全程不写系统代理**（`ProxyEnable` / `ProxyServer` / `InternetSetOption` 全不碰）。
- **端口随机分配**：槽位化（线路 / PAC / 控制口），随机 → 持久化 → 启动复核，
  被占用自动重挑，自家内核占着的端口不踢。
- **分流 PAC**：国内直连、视频站走视频线路、其余境外站走默认线路，
  每档都带另一条线路兜底。端口用占位符，每次启动现渲染。

### 安全

- **登录态守门员**（纯只读）：路径冻结 + 开窗前只读体检（Cookie 条数 / 加密版本 /
  密钥指纹），掉登录立刻能定位发生在哪一步。
- **一键找回**：`tools/救回登录.py` 从历史快照里只合并登录类 Cookie。
- **破坏性操作门禁**：查占用 → 完整快照 → 明确确认 → 写审计日志。
- **本地 AI 模型清理**：删掉 Chrome 的本地 AI 模型并用只读同名文件占位。

### 体验

- 控制面板：线路状态（运行中 / 待机 / 启动中 / 遗留 / 被占用）+ 端口 + 资料目录。
- 托盘菜单：开面板、显示/隐藏、退出。
- **系统默认浏览器接管**：注册为可识别的浏览器条目，
  点外部链接直接落到指定线路**已登录**的窗口（参数由管家现算，不复制）。
- **重点网址自愈**：指定必开站，自动挑一条能打开的线路，打不开的顺手重建一次。

### 工程

- 模块化：`paths` / `ports` / `config_store` / `line` / `guard` / `pac` /
  `net` / `procs` / `login_guard` / `login_backup` / `safety` / `panel` / `tray` / `cli`。
- **零绝对路径**：根目录由文件位置推导，配置里可用 `${ROOT}` 占位符；
  Chrome 路径自动探测（含注册表 App Paths）。
- 首次运行自动从 `config/profiles.example.json` 生成本地配置，
  真实配置与全部数据都在 `.gitignore` 里。
- 没有内核时生成**占位直连配置**，软件照样能起来（日志会说明缺什么）。
