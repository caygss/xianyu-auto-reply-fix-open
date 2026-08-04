---
name: 闲鱼虚拟商品交付工具
description: 面向非技术卖家的本地商品管理与自动交付操作台
colors:
  primary: "#4f46e5"
  primary-hover: "#4338ca"
  primary-light: "#6366f1"
  success: "#10b981"
  warning: "#f59e0b"
  danger: "#ef4444"
  canvas: "#f8fafc"
  surface: "#ffffff"
  text: "#1f2937"
  text-muted: "#6b7280"
  border: "#e5e7eb"
  input-border: "#d1d5db"
  dark-canvas: "#0f172a"
  dark-surface: "#1e293b"
  dark-input: "#334155"
  dark-text: "#f1f5f9"
  dark-text-muted: "#94a3b8"
typography:
  title:
    fontFamily: "Inter, Segoe UI, Tahoma, Geneva, Verdana, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 700
    lineHeight: 1.3
  body:
    fontFamily: "Inter, Segoe UI, Tahoma, Geneva, Verdana, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "Inter, Segoe UI, Tahoma, Geneva, Verdana, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 600
    lineHeight: 1.4
  mono:
    fontFamily: "JetBrains Mono, Fira Code, Courier New, monospace"
    fontSize: "0.85rem"
    fontWeight: 400
    lineHeight: 1.4
rounded:
  control: "8px"
  button: "10px"
  panel: "12px"
  legacy-card: "16px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
  xxl: "32px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
    padding: "8px 14px"
    height: "44px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
    textColor: "{colors.surface}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "8px 14px"
    height: "44px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "10px 12px"
    height: "44px"
  panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.panel}"
    padding: "16px"
---

# Design System: 闲鱼虚拟商品交付工具

## Overview

**Creative North Star: "手把手操作台"**

界面像一张始终摆在用户面前的操作台：当前账号、当前商品、正在执行的动作和下一步都必须清楚可见。它使用熟悉的管理工具结构，不要求用户学习新的交互方式，也不依赖装饰来制造专业感。

视觉密度适中，默认浅色画布承载白色工作面板，靛蓝只强调主操作和当前状态。暗色模式沿用同一信息层级。动画只说明加载、展开和状态变化，并兼容减少动态效果设置。

系统明确拒绝终端工具感、大段英文、内部错误码、无法行动的模糊状态、炫技动画和复杂嵌套层级。状态不确定时，界面必须安全停止并用中文说明下一步。

**Key Characteristics:**

- 当前对象始终可识别，不让账号和商品上下文串写。
- 主要动作使用稳定靛蓝，成功、库存预警和阻断使用固定语义色。
- 控件尺寸适合鼠标和触屏，核心操作最小高度为 44px。
- 面板使用浅边框和低阴影，层级服务于操作，不服务于装饰。
- 中文状态文本与颜色同时表达结果，错误必须给出可执行下一步。

## Colors

色彩采用克制的状态型体系：靛蓝负责主操作，绿、琥珀和红只表达业务结果，冷静的中性色承担阅读和分层。

### Primary

- **稳健靛蓝**：用于主要按钮、当前选择、焦点边框和关键进度。
- **深靛蓝**：用于主要按钮悬停和按下状态。
- **提示紫蓝**：用于轻量提示、图标和低强度强调。

### Secondary

- **完成绿**：仅用于成功、已连接、库存充足和完成状态。
- **库存琥珀**：仅用于库存不足、需要注意和可恢复暂停。
- **阻断红**：仅用于失败、危险操作和必须停止的状态。

### Neutral

- **冷雾画布**：页面底层背景，承托工作区域。
- **清晰白面板**：表单、配置面板和表格的主要工作表面。
- **石墨正文**：正文、标签和主要数据。
- **中性说明灰**：辅助说明、时间和次要信息；不得用于关键操作文字。
- **轻分隔线**：面板、输入框和表格的结构边界。
- **深夜画布与深蓝灰面板**：暗色模式的对应层级，不改变语义色含义。

**The One Accent Rule.** 靛蓝只用于主操作、当前选择和状态指示。非活动内容禁止使用高饱和靛蓝装饰。

**The Semantic Color Rule.** 绿、琥珀和红只能表达成功、预警和阻断；任何状态都必须同时提供中文文字，禁止只靠颜色传达。

## Typography

**Display Font:** Inter（回退到 Segoe UI、Tahoma、Geneva、Verdana 和系统 sans-serif）

**Body Font:** Inter（回退到 Segoe UI、Tahoma、Geneva、Verdana 和系统 sans-serif）

**Label/Mono Font:** JetBrains Mono（回退到 Fira Code、Courier New 和 monospace，仅用于必须保持等宽的数据）

**Character:** 全界面使用同一套熟悉的无衬线字体，通过字重和固定字号建立层级。技术数据可以使用等宽字体，但按钮、标签和帮助文字禁止使用等宽字体制造技术感。

### Hierarchy

- **Headline**（700，1.25rem，1.3）：页面标题、主要工作区标题和侧栏品牌。
- **Title**（600，1.1rem，1.4）：面板标题、表格分组和关键步骤标题。
- **Body**（400，1rem，1.5）：表单说明、状态说明和普通内容；连续说明文字控制在 65 到 75 个字符宽度内。
- **Label**（600，0.875rem，1.4）：输入标签、按钮和短状态；使用正常中文大小写，不做全大写正文。
- **Mono**（400，0.85rem，1.4）：Cookie、卡密脱敏预览和需要逐字符核对的值。

**The Plain Language Rule.** 字体层级只能提高可读性，不能把内部术语包装成视觉重点。用户看不懂的词必须改写，而不是加粗。

## Elevation

新界面以色调分层和浅边框为主，阴影只用于区分浮在画布上的主要面板或响应悬停。交付配置面板的低阴影是标准；旧页面中大面积模糊、玻璃效果和大幅上浮卡片属于兼容样式，不得复制到新组件。

### Shadow Vocabulary

- **轻面板阴影**（`0 1px 2px 0 rgba(0, 0, 0, 0.05)`）：交付配置、设置面板和静态工作容器。
- **中层操作阴影**（`0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06)`）：下拉层、需要从背景中分离的短暂操作区域。
- **侧栏结构阴影**（`2px 0 10px rgba(0, 0, 0, 0.1)`）：固定侧栏与主内容的结构分隔。

**The Flat Work Surface Rule.** 表单和状态区域默认保持平面。阴影不得与厚边框同时用于装饰，也不得通过大幅位移让工作面板在悬停时跳动。

## Components

组件遵循熟悉、可预测、状态完整的原则。所有可操作组件必须具备默认、悬停、焦点、按下、禁用、加载和错误状态。

### Buttons

- **Shape:** 清晰圆角（8px 至 10px），核心操作最小高度为 44px。
- **Primary:** 稳健靛蓝底色、白色文字、水平内边距 14px；标签使用“动词 + 对象”，例如“保存当前商品配置”。
- **Hover / Focus:** 悬停转为深靛蓝；键盘焦点使用可见的靛蓝焦点环；按下只做轻微色阶变化，不做弹跳。
- **Secondary:** 白色或暗色面板底色、输入边框色描边、石墨正文；危险操作必须使用清楚的红色语义和具体动作文字。
- **Disabled / Loading:** 禁用时降低强调但保持文字可读；加载时按钮不可重复点击，并在附近显示正在处理的中文状态。

### Chips

- **Style:** 小型状态标签使用中等字重、5px × 10px 内边距和对应语义色的低强度底色。
- **State:** 标签只能描述状态，不伪装成按钮；可点击筛选必须具备焦点和选中状态。

### Cards / Containers

- **Corner Style:** 新工作面板使用 12px 圆角；16px 仅保留给现有旧卡片，不继续扩散。
- **Background:** 浅色模式为清晰白面板，暗色模式为深蓝灰面板。
- **Shadow Strategy:** 默认使用轻面板阴影或单一边框，不叠加宽模糊阴影。
- **Border:** 1px 轻分隔线用于表达结构，不使用彩色侧边条作为强调。
- **Internal Padding:** 常规区域 16px，宽松页面区域 24px，紧凑表格单元可使用 8px 至 12px。

### Inputs / Fields

- **Style:** 面板底色、1px 输入边框、8px 圆角、10px × 12px 内边距，单行控件最小高度为 44px。
- **Focus:** 边框切换为稳健靛蓝，并显示足够对比度的焦点环。
- **Error / Disabled:** 错误同时显示阻断红边界和中文解释；禁用字段保留标签和当前值，说明为什么不可操作。

### Navigation

- 固定侧栏宽度为 250px，使用靛蓝到深靛蓝的结构背景；导航文字使用白色的不同透明度表达层级。
- 导航项使用 0.75rem × 1.5rem 内边距，图标固定宽度 20px；活动项必须同时具备高对比文字和背景状态。
- 768px 以下侧栏收起，由清楚标注的菜单按钮打开；键盘焦点不得被侧栏裁剪。

### Delivery Configuration Panel

- 这是新工作流的标准组件：顶部明确当前商品，主体按“默认内容、当前商品配置、库存与预览”分区。
- 900px 以上允许双列布局，520px 以下标题与操作纵向排列；任何尺寸都不能隐藏主要保存动作。
- 加载期间整个面板使用 `aria-busy`，所有写入按钮禁用；完成、失败和取消通过 `aria-live` 中文状态反馈。

## Do's and Don'ts

### Do

- **Do** 在每个配置面板中持续显示当前账号、商品标题和商品 ID，切换对象时先清空旧数据再加载新数据。
- **Do** 使用 44px 最小控件高度、8px 控件圆角、12px 面板圆角和 16px 基础内边距。
- **Do** 让加载、成功、失败和取消都有完整中文状态，并告诉用户下一步。
- **Do** 保留键盘焦点、`aria-live`、非颜色状态文字和减少动态效果支持。
- **Do** 在状态不确定时禁用自动重试和重复写入，明确提示需要核实。

### Don't

- **Don't** 做成需要终端命令、配置文件或开发知识才能使用的工具。
- **Don't** 使用大段英文、内部错误码、模糊的“处理中”或无法行动的报错。
- **Don't** 让用户猜当前配置属于哪个账号或商品。
- **Don't** 以炫技动画、装饰性视觉效果或复杂层级分散用户注意力。
- **Don't** 在状态不确定时自动重试可能产生重复发货的操作。
- **Don't** 为新组件复制玻璃拟态、大面积渐变卡片、厚彩色侧边条或悬停大幅上浮效果。
