# CodexHub 0.2.0 界面设计规范

本规范记录已锁定的桌面软件风格，是生产界面的统一依据。适用于概览、统计、Provider、客户端、设置，以及它们的弹窗、下拉菜单、提示与加载状态。原型仅作为历史参考。重构必须保留真实行为和数据语义。

## 布局与信息层级

- 紧凑桌面窗口，顶部导航和 Gateway 状态栏固定；不使用网页式大标题、宽幅留白或营销区块。
- 概览优先显示 Codex 连接、今日统计、资源额度；上方固定，资源列表独立滚动。
- Provider 编辑器只有一页：账户状态与操作同一行，连接字段两列，模型列表在下方独立滚动，保存栏固定。上方字段不得裁切或出现嵌套滚动。
- Provider 管理侧重模型与连接配置，概览侧重资源剩余量，避免重复整张表。
- 外部客户端区域独立滚动；通用客户端仅展示连接参数，不承诺自动适配未知配置格式。
- 主要内容边距 24 px；窄窗口可使用 16–18 px。面板内边距 12–16 px；紧凑控件 6–10 px。间距使用偶数尺度 2、4、6、8、10、12、14、16、18、20、22、24 px，优先 4 的倍数。

## 几何规范

| 对象 | 尺寸与圆角 |
| --- | --- |
| 普通按钮、图标按钮、输入框、选择框 | 标准高度 32 px；图标按钮宽 32 px；圆角 8 px |
| 紧凑工具栏按钮、分段选项 | 高度 28 px；圆角 6 px；外容器圆角 8 px |
| 标签、能力标记 | 高度 20–24 px；圆角 6 px；文字 11–12 px |
| 面板、模型行、弹窗 | 圆角 12 px；嵌套区域圆角 8 px |
| 开关轨道 | 36 × 20 px；**胶囊圆角 999 px** |
| 开关滑块 | 16 × 16 px；**圆形圆角 999 px**；距轨道边缘 2 px |
| 进度条、状态圆点 | 圆角随对象保持胶囊／圆形，不套用标签圆角 |
| 热力图单元 | 4 px 圆角；尺寸及颜色表达活动量，不受普通控件规则影响 |

禁止通过 `span.rounded-full.border` 等外观类组合修改所有子元素的圆角。必须使用组件语义类，如 `.ws-model-tag`、`.ws-model-switch`、`.ws-switch-control`。胶囊开关、圆点、图表不得被按钮或标签样式覆盖。

## 颜色

颜色由 `workspace.css` 中 `--ws-*` 变量提供。浅色为暖白表面，深色为紫灰表面；紫色是唯一主要操作强调色。

| 用途 | 变量 |
| --- | --- |
| 页面 / 面板 / 内嵌区域 | `--ws-bg` / `--ws-surface` / `--ws-inset` |
| 主文字 / 次要文字 | `--ws-ink` / `--ws-muted` |
| 边框 / 表面高光 | `--ws-line` / `--ws-rim` |
| 选中 / 悬停区域 | `--ws-accent` / `--ws-soft` |
| 成功 / 警告 / 危险 | `--ws-green` / `--ws-warning` / `--ws-danger` |
| 强调按钮文字 | `--ws-on-accent` |

状态标签底色用语义色与面板色混合，不硬编码浅色背景。开关滑块保持白色；品牌 Logo、热力图和数据系列颜色允许保留独立色彩。未知值使用“未知／不可用”，不得显示为 0 或伪造满额度。

模态遮罩使用 `--ws-scrim`；品牌承载底色／文字使用 `--ws-brand-surface`／`--ws-brand-ink`，保持品牌对比度；白色开关滑块使用 `--ws-switch-thumb`。主按钮、选中导航和额度条的渐变均由主题变量混合生成。

## 字体、边框与阴影

- 正文与控件默认 13 px；辅助说明 12 px；日期等紧凑元数据最低 11 px。标题按层级 14–20 px，关键概览数字 24–26 px。整体默认缩放 110%；Linux 使用原生 WebView zoom，其余环境沿用 FitStage 缩放，布局按缩放后的可用宽高重排。
- 普通文字 400–500，控件与标签 500–600，指标 600–700。避免同层级文字同时出现过多字号。
- 边框统一 1 px `--ws-line`。焦点使用 2 px `--ws-accent`，偏移 2 px；键盘操作不能仅依赖悬停。
- 控件、面板、浮层和模态框分别使用 `--ws-shadow-control`、`--ws-shadow-surface`、`--ws-shadow-floating`、`--ws-shadow-overlay`。不为每个按钮发明阴影。
- 开关滑块允许单独的轻阴影以表达运动层。禁用态降低整体透明度，仍保持状态可辨。

## 资源展示约定

- 单独周额度保留右侧槽位，左侧留空。
- Command Code：概览显示 5 小时与周额度，不显示余额。
- OpenCode Go：概览显示周与月额度，不显示 5 小时额度。
- 后端继续保留真实完整数据；显示精简不改变查询、认证或路由逻辑。

## 检查与例外

新增或修改共享控件时，检查浅／深色、默认／悬停／选中／禁用／焦点，以及 840 × 600 和常规窗口。开关必须同时检查开和关的形状；不实际修改账户或运行服务来做视觉验收。

运行 `npm run check:design` 检查生产 workspace 样式的圆角、阴影、间距和危险选择器。该检查只覆盖静态规则，不能代替浏览器检查。兼容旧功能组件的 Tailwind 映射集中在 workspace 样式层，后续新增组件应直接使用语义类和变量。品牌资产、图表数据色、原型代码属于明确例外。

本次审计修正：过宽圆角选择器覆盖开关轨道；31/33/36 px 按钮高度混杂；3–13 px 多种临时圆角；零散阴影；奇数内容间距。保留图表、热力图和开关的特殊几何，不把所有元素改成同一种形状。

### 本轮浏览器核对记录

- 概览、统计、Provider、客户端、设置均检查过布局；当前窗口无页面横向溢出。
- 模型、客户端、设置三处共享开关的轨道／滑块计算圆角均为 999 px。
- 统计时间筛选容器为 8 px，内部按钮为 6 px，未覆盖日历单元和图例圆点。
- 深色 840 × 600 下，模型开／关状态分别使用强调色／边框色，滑块为白色；OpenAI 顶部实际高度与滚动高度同为 276 px。
- 此记录覆盖代码静态规则和上述浏览器状态；未声称穷举所有错误弹窗、平台或系统缩放组合。

## 0.2.0 发布后反馈

- Gateway 独立为顶层页面：连接参数与已保存的三个接口地址固定在上方，真实 Gateway 模型目录在下方独立滚动，按 Provider 分组并可折叠，可按来源和模型名称／ID 筛选并复制。
- 概览移除底部重复导航；统计图例不加顶部分割线。
- Provider 与 Clients 页移除底部说明／跳转栏。Gateway 三个接口地址并排展示，标题在上、URL 在下，保留完整地址 tooltip 和复制按钮，移除可见复制说明。设置与 Gateway 的 Save／Discard 放在顶层 Tab 右侧，不占用底部高度；只读 About 无草稿时隐藏操作。
- 全局 Toast 与页面使用同一组文档级主题变量，包含正文、状态图标、操作和关闭按钮；不能假设浮层在 `.workspace-root` 内。
- Linux 窗口采用 RGBA 透明宿主：正常窗口预留 12 px 透明阴影区，内容面板圆角 14 px，以两层低透明度黑色阴影形成柔和渐变；圆角外不得出现白底或不透明矩形。最大化／全屏取消阴影区并铺满窗口。GTK 样式只作用于主窗口，输入区域保留完整矩形以支持拖动与缩放；验证必须同时覆盖真实 Wayland 合成与 X11 点击。
- Usage 图表只保留一层背景容器，不再叠加带内边距的内层背景框。110% 缩放、1076×820 窗口下，Workspace 与 Provider 列表必须完整显示至少四项；缩减行内留白，保留字体大小和操作控件。
- 品牌图标采用用户确认的 `reference-aligned-b.svg` 描摹版。界面源文件为 `frontend/src/assets/brand/codexhub-icon.svg`，所有打包 PNG／ICO 由该 SVG 通过 `cargo tauri icon` 生成；保留原图比例、灰紫配色与透明外边距，禁止单独重画平台版本。后续更改造型仍需用户确认。

### Scroll regions

- Reserve a stable scrollbar gutter for scrollable lists, settings, dialogs,
  menus and previews. Keep an additional 8 px content clearance on unpadded
  list regions so overlay scrollbars cannot cover switches or copy buttons.
- Scroll rails stay at the scroll container edge, outside the cards. Provider
  is the sole stable exception: its dedicated list may reserve and compensate
  for its known right rail width in the page gutter, so cards retain symmetric
  margins and no control sits beneath the rail. Do not use conditional negative
  margins for other model or provider content.
- Verify both fitting and overflowing content at 110% on native Linux;
  scrollbar appearance must not change card widths or hide the final controls.

### Refresh and compact client cards

- Quota panels paint their last successful cached values immediately, then
  replace them after a successful refresh. A transient refresh failure keeps
  those values; confirmed sign-out clears account quota caches.
- Provider card model previews omit the containing provider's redundant name
  prefix. Official models use the same preview treatment.
- At 110% scale, client cards must fit two complete rows at 820 px viewport
  height, including configuration path, connection status and switch.
- Search fields with an icon share one background across the wrapper; the
  nested input remains transparent.

### Default-window density

- Validate overview density at the default 1024×768 native window, accounting
  for 110% zoom and transparent shadow insets: all four resource rows fit.
- Provider cards retain symmetric page margins. Position the scroll rail in
  the right page gutter, compensating for the platform's actual rail width.
- Model previews use available width and a single-line ellipsis, never a fixed
  two-model limit. Keep enable counts and reorder controls visible.
- The Usage page places its content directly at normal page margins without
  an extra outer card, background, border or shadow.
