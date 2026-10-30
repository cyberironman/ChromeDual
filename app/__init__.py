# -*- coding: utf-8 -*-
"""ChromeDual · Chrome 代理管家 —— 应用包。

目录约定（改路径只改 paths.py，别在业务代码里写死）：

    <根>\\
        ChromeDual.py           入口
        app\\                    程序（只有代码，没有任何数据）
        config\\                 配置（profiles.json / targets.json）
        bin\\                    内核（xray.exe）
        assets\\                 资源（图标 / PAC / 起始页）
        data\\chrome\\<线路>\\     每条线路**各自独立**的 Chrome 资料目录
        data\\source\\<线路>\\     订阅、节点池、探测缓存
        var\\logs\\               日志
        var\\backups\\<线路>\\     登录态备份（按线路隔离）
        var\\runtime\\<线路>\\     运行期生成的内核配置
        var\\run\\                命令文件、端口表等运行期文件

★ 根目录由 app\\paths.py 按本文件位置推导，**不写死任何绝对路径**，
  整个文件夹搬到别的盘、别的机器都能直接跑。
"""
