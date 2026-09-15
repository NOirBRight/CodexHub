# GNOME 托盘菜单对比度：应用责任与修复边界

日期：2026-09-15。范围：方案研究、隔离 GNOME 实测，以及随后在 `fix/gnome-tray-empty-labels`（`488bc45`）落地的 Linux 托盘标签重发。研究当时未改应用源码；实现见 `src-tauri/src/main.rs`。时序证据按 `strict` 管理。

## 结论

CodexHub 应承担可用性和兼容性验收，但当前 GNOME 托盘菜单由桌面进程绘制。应用不能通过前端 CSS 或 GTK CSS 给 GNOME 菜单指定可靠的文字颜色。现有强制深色文字扩展不适合作为产品依赖。

**隔离实测更新：原版 AppIndicators 存在可复现的空标签问题。** 菜单的原生颜色在 Yaru 浅/深主题下对比度正常，但 CodexHub 的 7 个文字项可能全部为空；对照程序正常。只在隔离扩展副本中改变属性请求的 cancellation 生命周期，即可恢复标签，无需颜色扩展。具体结果和局限见本文末尾及[实验记录](../evidence/gnome-tray-2026-09-15/README.md)。这不能证明用户当前桌面历史上的颜色异常与空标签是同一问题。

推荐保留标准原生托盘，优先针对已经复现的 AppIndicators 属性请求时序缺陷准备上游报告与生命周期修复；应用侧若需要兼容旧扩展，再单独验证菜单发布顺序或属性更新方案，不能承诺简单延时或重发就可靠。主窗口应有完整操作入口、可从应用启动器重新唤起，避免必须依赖托盘。当前没有证据支持为此重写自绘菜单或给所有用户安装颜色扩展。

## 已确认的证据

1. 仓库 `src-tauri/src/main.rs::setup_tray` 使用 `MenuBuilder` 创建纯文字菜单，包括 `Show CodexHub`、Gateway 操作和退出；没有设置菜单颜色。锁定版本为 Tauri 2.11.3、tray-icon 0.24.1、muda 0.19.3，见 [Cargo.lock](../../src-tauri/Cargo.lock) 和 [main.rs](../../src-tauri/src/main.rs)。
2. tray-icon 的 Linux 后端通过 `AppIndicator::set_menu` 传入 GTK 菜单。[锁定版本源码](https://github.com/tauri-apps/tray-icon/blob/tray-icon-v0.24.1/src/platform_impl/gtk/mod.rs)。GNOME AppIndicators 将 D-Bus 菜单项重新创建为 `PopupMenu.PopupMenuItem`，标签使用 `set_text`；其属性处理分支没有文字前景色设置。[上游 dbusMenu.js](https://github.com/ubuntu/gnome-shell-extension-appindicator/blob/master/dbusMenu.js)（浮动分支，查阅于本日）；本机同名源码也已检查。
3. 本机只读 D-Bus `GetLayout` 实际返回的是标签、分隔符和 enabled 等属性，没有文字或背景颜色。调用成功的命令如下；`:1.176` 是本次会话的临时名称，后续须重新发现：

   ```sh
   gdbus call --session --dest :1.176 \
     --object-path /org/ayatana/NotificationItem/tray_icon_tray_app_codexhub/Menu \
     --method com.canonical.dbusmenu.GetLayout -- 0 -1 '[]'
   ```

   返回 revision 2，包含 `Show CodexHub`、`Connect Codex to Official`、`Connect Codex to CodexHub`、三个 Gateway 操作和 `Exit`。这证明菜单数据路径可用，不证明屏幕文字可读。
4. [linux_window.rs](../../src-tauri/src/linux_window.rs) 的 GTK CSS 为 `#codexhub-main { background-color: transparent; }`，provider 加在主窗口的 style context；当前代码没有通过该处对所有 GTK 菜单设置文字颜色。应用 WebView 与 GNOME Shell 的样式不属于同一绘制上下文。
5. 本机环境：GNOME Shell 50.1、GTK theme `Yaru`、color-scheme `default`；启用了 `ubuntu-appindicators@ubuntu.com` 和 `codexhub-tray-contrast@codexhub.app`。这是启用列表证据，并非完整运行健康检查。
6. 本机用户扩展 `~/.local/share/gnome-shell/extensions/codexhub-tray-contrast@codexhub.app/extension.js` 每秒扫描顶栏菜单，打开时匹配文字 `Show CodexHub`，对顶层标签设置 `color: #1f1f1f;`。它不检查背景、悬停、禁用状态，也没有在 disable 时恢复已写入的 style。声明仅支持 Shell 50。切换暗背景可能失去对比度；按文字匹配还会受本地化/改名影响。因此禁用后不能直接把仍存活的菜单当作无补丁基线，应重建菜单或使用全新测试会话。
7. `dpkg -V gnome-shell-ubuntu-extensions` 返回 `??5?????? .../dbusMenu.js`，证明系统文件与包记录校验值不同。文件内存在 `// CodexHub` 注释和针对 `/tray_icon_tray_app_codexhub/` 的 `_requestProperties` cancellable 修补。当前包版本为 `50.26.04.7ubuntu`。这属于另外一项菜单标签请求干预，不能据此认定它导致颜色问题；也不能将本机直接当成标准 Ubuntu 安装。

## 方案比较

| 方案 | 效果与代价 | 建议 |
| --- | --- | --- |
| 修改前端 CSS、主窗口 theme 或 GTK 菜单文字 | 不会给 GNOME 重建的 Shell 菜单设置颜色；其他桌面的 GTK fallback 是另一个验收路径 | 不作为本问题修复 |
| 随应用安装现有 GNOME 扩展 | 固定颜色、轮询、私有 Shell API、版本维护，还干预桌面环境 | 不采用为产品依赖 |
| 原生托盘 + 干净环境复现 + 上游修复 | 保留桌面原生行为与可访问性，维护成本最低；桌面缺陷修复可能受发行版更新节奏限制 | 首选 |
| 主窗口保证功能和重新唤起入口完整 | 可作为托盘异常时的可用性保障，但不修复托盘颜色；当前已有单实例重新唤起回调，仍须实际验证桌面启动器链路 | 配套验收 |
| Linux 自绘托盘控制窗口 | 应用可成对控制前景/背景，但需要处理激活、焦点、定位、Wayland、键盘和屏幕阅读器 | 只有明确产品需求时采用 |

自绘不能直接套用 Windows 的点击处理：[Tauri TrayIconEvent](https://docs.rs/tauri/2.11.3/tauri/tray/enum.TrayIconEvent.html) 明确 Linux 不发出这些事件；[show_menu_on_left_click](https://docs.rs/tauri/2.11.3/tauri/tray/struct.TrayIconBuilder.html#method.show_menu_on_left_click) 在 Linux 不支持。仓库现有左键回调因此不能被当作 Linux 自绘菜单入口。若追求直接点击打开自绘界面，需要另行验证/实现 StatusNotifier 激活链路；若从原生菜单项打开，自绘窗口仍不能消除原生入口自身的颜色问题。

## 初步计划与后续完整验收

先在隔离的桌面会话或 VM 使用发行版原包、无 CodexHub 专用扩展启动同一应用构建；不覆盖当前用户的系统文件。准备只有 Show/Exit 的最小 AppIndicator 样例，与 CodexHub 并排比较，以区分应用特有数据问题和共同宿主问题。首次复现后再提出并逐一验证具体根因假设。

| 环境/操作 | 观察与通过标准 |
| --- | --- |
| 原版 GNOME 50 + Ubuntu AppIndicators，Yaru 浅/深色 | 首次打开及重复打开，普通文字与实际背景对比度建议至少 4.5:1；标签完整 |
| 同会话切换浅/深主题 | 菜单前景与背景同步适配，无遗留强制色 |
| 悬停、键盘选中、禁用项、分隔符 | 状态可辨认，文字可读，操作与键盘导航正确 |
| CodexHub 与最小 AppIndicator 样例 | 对照差异，判断能否归因到应用菜单数据 |
| KDE Plasma 与实际支持的其他 Linux 桌面 | 原生菜单仍可用，不引入 GNOME 专用依赖 |
| 托盘不可用，窗口已隐藏 | 应用启动器可重新唤起；必要操作在主窗口中可达 |

第一阶段在活跃桌面的只读 `org.gnome.Shell.Eval` 查询返回 `(false, '')`。第二阶段已在独立沙箱会话中通过测试 observer 打开调试查询，完成无颜色扩展的截图、颜色测量、主题切换和空标签实验；活跃桌面未开启 unsafe mode。跨桌面、物理指针、辅助技术和发布验收仍未完成。

本次新增研究文档及运行证据，执行文档内容/链接检查与 `git diff --check`；没有运行应用全套测试，也未发布实验补丁。

## 隔离实验摘要

- 环境：GNOME Shell 50.1 无头 Wayland、1280×900 虚拟显示器、软件渲染、Ubuntu/Yaru；从发行版 `gnome-shell-ubuntu-extensions=50.26.04.7ubuntu` 的原始 deb 解包 AppIndicators。
- 隔离：bubblewrap 独立 PID/网络/IPC、独立 D-Bus、临时用户目录和 XDG 数据；仓库只读。系统 D-Bus 也指向测试总线，不连接宿主系统服务。真实 CodexHub Gateway 自动启动已在临时配置中关闭。
- 用例：现成 release 二进制与最小 GTK/AppIndicator C 对照程序。固定记录二进制 SHA-256，未构建或声称覆盖当前未提交源码。
- 原版：CodexHub 的 D-Bus GetLayout 有完整文字，但 Shell 中可出现 7 个空字符串；浅色文字/背景 `#222222 / #ffffff`、对比度 15.91:1；深色 `#ffffff / #36363a`、对比度 12.03:1。空白与颜色对比度无关。
- 实验：只在测试扩展的 `_requestProperties` 开头增加 `cancellable = this._cancellable;`，不设置颜色，浅/深主题标签均完整；重复开关菜单 3 次通过。此行为变化用于定位请求生命周期，不是经过完整生命周期审查的可发布补丁。
- 撤回后并非每次失败，进一步执行连续启动对照；计数、原始输出、截图和重跑脚本见[证据目录](../evidence/gnome-tray-2026-09-15/README.md)。


## 源码绑定复现

#529 已把复现入口固化为 `scripts/gnome-tray-lab/`，并用 SHA `f82c24a03082148b49b646a6e26b57c9a1450039` 构建的 release 二进制（SHA-256 `20b08e55774005a7d90dd743dbe6b65bcd600ddbb093eda7afb700c71ef15335`）在隔离原版 AppIndicators 上再次捕获空标签。失败时 Shell actor 文本为空，D-Bus `GetLayout` 仍有七个完整标签；对照程序正常；浅/深主题对比度分别为 15.91:1 与 12.03:1。未修复基线证据见 [source-f82c24a](../evidence/gnome-tray-2026-09-15/source-f82c24a/README.md)。修复候选 SHA `488bc453796387b4e9cff76b48d4e17ac6c027c7`、二进制 `1a5094b32b79a4f3fab96e6407abe68ebd8d5f486495ced597a6f9a572cbf461` 见 [candidate-531](../evidence/gnome-tray-2026-09-15/source-f82c24a/candidate-531/README.md)。
