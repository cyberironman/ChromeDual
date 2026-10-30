<div align="center">

# ChromeDual · Chrome 代理管家

**一份 Chrome 资料 = 一个独立身份 = 一条独立代理线路。**
开哪个窗口，才起哪个内核；其余线路一律不碰，系统代理全程不动。

`Windows` · `Python 3.10+` · `MIT`

</div>

---

## 它解决什么问题

大多数人用代理软件的姿势是「一个 Chrome、一套账号、一条线」。于是：

| 场景 | 普通代理软件 | ChromeDual |
|---|---|---|
| 多个账号 | 只能开多个 Chrome，窗口长得一模一样，认不出哪个是谁 | 每条线一个独立资料目录 + 独立图标，一眼分得清 |
| 换线路 | 全局切换，正在用的窗口全变 | 只影响你点的那条线，其他窗口纹丝不动 |
| 端口冲突 | 几个代理软件抢同一段端口，谁先起谁活 | 每个程序一段互不重叠的随机端口段，重启自动复核 |
| 系统代理 | 常常要接管全局代理，影响所有软件 | **从不写系统代理**，只给自家 Chrome 传 `--proxy-server` |
| 掉登录 | 资料目录被搬动 / Chrome 升级 → 静默清库，找不回来 | 路径冻结 + 开窗前体检 + 一键找回 |

一句话：**把「全局代理」拆成「按窗口代理」，把「一个身份」拆成「多个互不干扰的身份」。**

---

## 三个核心概念

**线路（line）**
一条线路 = 一个内核配置 + 一个本地端口 + **一份完全独立的 Chrome 资料目录**。
线路之间不共享 Cookie、书签、历史、缓存。

**资料隔离**
每条线路的 `--user-data-dir` 都指向自己的 `data/chrome/<线路>/`，
绝不共用系统默认的 `%LOCALAPPDATA%\Google\Chrome\User Data`。
共用就等于「所有窗口共享一套登录」，一个窗口出事，全部受牵连。

**按需启动**
管家启动时**不预热任何内核**。点哪条线路，才拉哪条线路的内核；
窗口全关超过 2 分钟（可配），内核自动退场，不占端口、不占内存。

---

## 功能一览

- **多身份 Chrome**：每条线路一份独立资料，登录、书签、扩展全隔离
- **按需启停**：开窗才起内核，关窗自动停；托盘随时显示「运行中 / 待机 / 遗留 / 被占用」
- **端口随机化**：端口按程序分段随机分配，写进 `var/run/ports.json`，重启沿用，被占用自动重挑
- **进程级代理**：只给自家 Chrome 传 `--proxy-server` / `--proxy-pac-url`，**不碰系统代理**
- **分流 PAC**：国内站直连、视频站走视频线路、其余境外站走默认线路，三档优先级自动兜底
- **重点网址自愈**：指定几个必开站，自动挑一条能打开的线路；打不开的那条顺手重建一次
- **登录态守门员**：路径冻结 + 开窗前只读体检，掉登录立刻能定位是哪一步造成的
- **破坏性操作门禁**：任何会动 Chrome 数据的操作，先快照、再确认、后动手，全程审计
- **系统默认浏览器接管**：点微信/QQ 里的链接，直接落到你指定那条线已登录的窗口
- **本地 AI 模型清理**：一键删掉 Chrome 的本地 AI 模型并用只读文件占位，省下几个 GB

---

## 快速开始

### 方式 A：装 exe（推荐，不需要装 Python）

1. 到 [Releases](https://github.com/cyberironman/ChromeDual/releases) 下载 `ChromeDual-Setup-x.y.z.exe`
2. 双击 → 回车确认安装位置 → 装完自动启动
3. 右下角出现托盘图标，右键 → 打开控制面板

安装器会顺手处理掉这些麻烦事：

- 缺 **WebView2 运行时** → 问一句，然后从微软官网静默装（控制面板靠它渲染）
- 缺 **Xray 内核** → 首次启动时提示并自动下载
- 桌面 / 开始菜单快捷方式、卸载项（「设置 → 应用」里能正常卸载）

> 重装或升级**不会**动你的 `config\`、`data\`、`var\` —— 登录态、Chrome 资料、备份都在。

### 方式 B：免安装 zip

下载 `ChromeDual-x.y.z-portable.zip`，解压到**固定文件夹**（别放「下载」或临时目录），
双击 `ChromeDual.exe`。

### 方式 C：从源码跑（改代码 / 自己编）

需要 Python 3.10 以上。

```bat
pip install -r requirements.txt
python ChromeDual.py
```

> 少装了库也不用自己记包名：启动时会弹一个窗口把缺的列出来，点「是」自动装好再继续。

首次运行自动生成 `config\profiles.json`（从 `config\profiles.example.json` 复制）。
托盘图标出现 → 右键打开主面板 → 点线路图标开窗。

想自己打 exe 发版：看 [`docs/打包与发布.md`](docs/打包与发布.md)，一条命令搞定。

### 内核从哪来

仓库里不放 `bin\*.exe`（36 MB 的二进制会让 `git clone` 变得很痛苦），三种办法任选：

1. **让它自己下**：启动时缺内核会问一句，点是就从 [XTLS/Xray-core](https://github.com/XTLS/Xray-core/releases) 拉最新的 `Xray-windows-64.zip` 并解出 `xray.exe`
2. **手动放**：下载后解压出 `xray.exe`，源码版放 `bin\xray.exe`，exe 版放安装目录的 `bin\`
3. **要 TUN 模式**再额外放一份 `wintun.dll`（[WireGuard/wintun](https://github.com/WireGuard/wintun/releases)）

> 没有内核也能先跑起来：程序会生成一份**占位直连配置**，窗口能开、软件能点，
> 但不翻墙。日志会明确提示缺什么、怎么补。

### 依赖体检

任何时候想确认环境齐没齐：

```bat
ChromeDual.exe --check
```

exe 版没有控制台，所以它会把报告同时写到 `var\logs\deps-check.txt`。
「双击没反应」的时候第一个该看的就是这个文件。

### 4. 接入你自己的线路（可选）

**方式一：现成的 xray 配置**

把你的 `xray-runtime.json` 放到 `var\runtime\<线路 id>\` 下即可。

**方式二：有订阅地址**

```bat
python tools\build_balancer_config.py ^
    --root data\source\profile-b ^
    --sub-url "https://你的订阅地址/sub?token=xxx"
```

这条脚本会：拉订阅 → 并发探活剔除死节点 → 现场测速 → 挑最快的几个 → 生成 xray 配置。
它还支持「指定站点走指定国家」（见 `region-rules.json`），用于对付有地域风控的站点。

> ⚠ 订阅地址带 token，**只在你自己的机器上用**。`config/profiles.json`、
> `data/`、`var/` 都已在 `.gitignore` 里，不会跟着代码传上去。

---

## 配置说明

只有一个文件要管：`config/profiles.json`（首次运行自动生成）。

```jsonc
{
  "defaults": {
    "chrome": "",                    // 留空 = 自动探测本机 Chrome
    "xray": "${ROOT}/bin/xray.exe",  // ${ROOT} = 程序所在目录
    "idle_stop_seconds": 120        // 窗口全关多久后自动停内核
  },
  "pac_server": {
    "directory": "${ROOT}/assets/pac",
    "port": 0,                      // 0 = 随机分配
    "video_line": "profile-a",      // PAC 里「视频名单」优先走哪条
    "default_line": "profile-b"     // PAC 里「其余境外站」优先走哪条
  },
  "lines": [
    {
      "id": "profile-a",            // 线路 id（命令行 --open 用它）
      "name": "资料 A",             // 显示名
      "enabled": true,
      "http_port": 0,               // 0 = 随机分配
      "chrome": {
        "user_data_dir": "${ROOT}/data/chrome/profile-a",
        "profile_directory": "Default",
        "startup_urls": []          // 打开时自动访问的网址（可留空）
      }
    }
  ]
}
```

**加一条线路**：在 `lines` 里追加一段，`id` 换个新名字即可。
资料目录、订阅目录、备份目录都会按 `id` 自动分开，互不干扰。

**换系统默认浏览器走哪条线**：改 `config/sysdefault.json` 里的 `line`，不用重新注册。

**端口**：全部写 `0` = 随机分配。实际值看 `var/run/ports.json`。
程序分段是固定的，互不重叠：

| 程序 | 端口区间 |
|---|---|
| 分应用代理类 | 10000–19998 |
| **ChromeDual** | **20000–39998** |
| 其它代理管家 | 40000–59998 |

---

## 目录结构

```
ChromeDual/
├─ ChromeDual.py           入口
├─ app/                    程序（只有代码，没有任何数据）
│  ├─ paths.py             路径（唯一来源，不写死任何绝对路径）
│  ├─ ports.py             端口随机分配与持久化
│  ├─ guard.py             总控：线路集合、PAC、保活
│  ├─ line.py              单条线路：起内核、开窗、状态
│  ├─ pac.py               PAC 静态服务与端口渲染
│  ├─ login_guard.py       登录态守门员（纯只读）
│  ├─ safety.py            破坏性操作门禁（快照 + 确认 + 审计）
│  ├─ net.py / procs.py    端口探测 / 进程识别
│  └─ panel.html / tray.py 控制面板 / 托盘
├─ config/                 配置（profiles.json 是你的，example 是模板）
├─ assets/                 图标 / PAC 模板 / 起始页
├─ tools/                  构建、注册、清理、找回等辅助脚本
├─ Tray/                   兼容外壳 + 系统默认浏览器转接器
├─ data/                   你的资料与订阅（不进仓库）
└─ var/                    日志、运行期配置、备份（不进仓库）
```

---

## 关键设计取舍

**1. 宁可不代理，也不碰系统代理**
接管系统代理看起来省事，代价是所有软件都被卷进来。本程序只给自家 Chrome 传进程级代理参数，
别人家的软件一点不受影响。

**2. 端口必须随机，分段必须不重叠**
几个代理软件挤在同一段固定端口上，是「内核莫名其妙掉线」的头号原因。
随机 + 持久化 + 启动复核 + 分段，是唯一稳的做法。

**3. 资料目录只能拆一次**
Chrome 127 之后 Cookie 用应用绑定加密（ABE），密文里写进了 `--user-data-dir` 的绝对路径。
目录一复制一改名，这批密文就解不开，Chrome 会**静默删掉**它们（日志里一个字都没有）。
所以本程序**永不复制、永不搬移**资料目录，并且把这个路径冻结下来。

**4. 破坏性操作一律先问一句**
任何会动 Chrome 数据的操作：查占用 → 完整快照 → 明确确认 → 写审计日志。
宁可多一步，也不能让你「莫名其妙丢了登录」还不知道是谁干的。

**5. 端口、路径、线路名都不写死**
路径由 `app/paths.py` 按文件位置推导，配置里可以用 `${ROOT}` 占位符；
线路名只是 id，不参与任何业务判断。整个文件夹搬到 U 盘都能跑。

---

## 隐私与安全

- **数据全在本地**：没有云端账号、没有遥测、没有上报。
- **仓库里零个人信息**：订阅地址、网址列表、登录态、Chrome 资料全部在 `.gitignore` 里。
- **不读 Chrome 密码**：只统计 Cookie 条数与加密版本，用来判断「登录是不是少了」。
- **系统代理可还原**：本程序根本不碰它。
- **不做 kill-switch**：宁可漏一会儿，也不让内核异常导致整机断网。
- **不替用户选服务商**：线路是你自己的，软件只负责「按窗口分流」这件事。

---

## 常见问题

**Q：面板里某个线路是灰色的「待机」？**
正常。管家不预热任何内核，点它才会起。

**Q：窗口开了但境外站打不开？**
多半是这条线路还没有真正的代理配置，只用了占位直连。日志里会写明，去 `var/logs/` 看一眼。

**Q：以前登录的账号不见了？**
先看面板那一行下面的橙色警示条，它会告诉你「是在这次开窗之前就已经少了」。
再用 `tools\救回登录.py` 从历史快照里把登录 Cookie 找回来（需先关掉该线路的 Chrome）。

**Q：能改成别的浏览器吗？**
内核是 xray，浏览器参数走的是 Chrome 家族的标准参数，Edge / Chromium 系基本都能用，
改 `config/profiles.json` 里的 `chrome` 路径即可。

**Q：端口冲突了怎么办？**
不用管。端口每次启动都会复核，被别的程序占了会自动换一个新的。

---

## 致谢与许可

- 内核：[XTLS/Xray-core](https://github.com/XTLS/Xray-core)（MPL-2.0）
- 托盘：[pystray](https://github.com/morningspace/pystray) · 面板：[pywebview](https://github.com/r0x0r/pywebview)

本项目以 MIT 协议开源，详见 [LICENSE](LICENSE)。

> 如果这套东西对你有用，欢迎提 issue、给 star、或者直接拿去改。
> 你自己的订阅、网址、账号永远留在你自己机器上。
