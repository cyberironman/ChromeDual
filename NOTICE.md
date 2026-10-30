# 第三方组件与分发说明

本仓库（`ChromeDual`）自身代码以 [MIT](LICENSE) 授权。

## 不随仓库分发的部分

| 组件 | 授权 | 说明 |
|---|---|---|
| **Xray-core** | MPL-2.0 | 代理内核。本仓库**不包含**任何内核二进制，程序会引导你从 [XTLS/Xray-core](https://github.com/XTLS/Xray-core/releases) 官方页面获取。 |
| **wintun.dll** | WireGuard 项目授权 | 仅在启用 TUN 模式时需要，同样请从 [WireGuard/wintun](https://github.com/WireGuard/wintun/releases) 官方页面获取。 |
| **WebView2 Runtime** | 微软授权 | 控制面板的渲染引擎，由安装程序引导安装微软官方运行时。 |
| **Google Chrome** | Google 授权 | 本程序不修改、不重新分发 Chrome，只按你的配置以独立 `--user-data-dir` 启动你机器上已有的 Chrome。 |

## 名字与商标

「Chrome」是 Google LLC 的商标。本项目与 Google 无任何隶属关系，
仅指「启动本机已安装的 Chrome」这一行为。

## Release 里的安装包

`release` 页面的 `Setup.exe` / `portable.zip` 是用本仓库源码构建的产物，
其中同样**不含**任何代理内核二进制与你个人的配置文件。
