# GNOME 托盘隔离实验

可重复入口在 [scripts/gnome-tray-lab](../../../scripts/gnome-tray-lab/README.md)。
本目录保存运行证据。时序结果按 `strict` 管理：一次通过不能当作修复完成。

## 源码绑定复现（#529）

工作树 SHA `f82c24a03082148b49b646a6e26b57c9a1450039`，命令：

```sh
npm ci --prefix frontend
npm run build --prefix frontend
(cd src-tauri && cargo build --locked --release --features custom-protocol)
./scripts/gnome-tray-lab/verify-fixtures.sh
./scripts/gnome-tray-lab/run.sh --expect-failures --repeats 10 --theme light
```

二进制 SHA-256 `20b08e55774005a7d90dd743dbe6b65bcd600ddbb093eda7afb700c71ef15335`。
隔离环境：Ubuntu 26.04.1、GNOME Shell 50.1、原版 AppIndicators 包 `50.26.04.7ubuntu`、Yaru `26.04.5.1ubuntu`。不加载 Contrast Guard，不连接宿主 D-Bus，Gateway 自动启动关闭。

机器结果在 [source-f82c24a](source-f82c24a/README.md)。

## 2026-09-15 探索实验（未绑定源码 SHA）

当时使用已有 release 二进制 `701b999f6086756d32b08dfc946dedcf18bb98281fd28a5163ec81ca977383fc`，不能代表当前工作树。

| 条件 | 结果 |
| --- | --- |
| 原版 AppIndicators，Yaru 浅色，连续重启 CodexHub 10 次 | 6 次正常，4 次 7 个菜单标签全部为空 |
| 实验修补属性请求 cancellable，其他条件相同，重启 10 次 | 10 次完整显示 |
| 同时运行的最小 GTK/AppIndicator 对照程序 | 上述两组采样均正常；对照进程未逐次重启 |
| 原版出现空标签后切换深色 | 标签仍为空；颜色对比度正常 |
| 实验修补后浅色切换深色 | 标签完整；深色下额外重复开关 3 次通过 |

浅色前景 `#222222` / 背景 `#ffffff`，对比度 15.91:1；深色前景 `#ffffff` / 背景 `#36363a`，对比度 12.03:1。分隔符不计入标签检查。

原版失败时 D-Bus `GetLayout` 仍有完整文字，见 [stock-app-layout.txt](stock-app-layout.txt)；Shell actor 的 text 是空串，见 [stock-light-both.json](stock-light-both.json)。因此复现的是属性/标签加载问题，没有复现“文字存在但前景与背景同色”。

实验仅增加一行 `cancellable = this._cancellable;`，见 [experimental.patch](experimental.patch)。这把属性请求绑定到菜单代理的生命周期，支持“请求取消时序导致标签丢失”的判断；尚未审查其在菜单销毁、更新、进程重连等场景下的完整正确性，不能直接作为发布补丁。

## 静态复核

```sh
./scripts/gnome-tray-lab/verify-fixtures.sh
```

这只检查已保存样本，不是实时回归。
