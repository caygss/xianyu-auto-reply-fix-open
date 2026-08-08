# Windows 分发说明

## Windows single-user first use and isolation

One buyer uses one package on one computer and one installation directory. Unzip the package, double-click `XianyuAutoDelivery.exe`, and sign in with the local default administrator account (`admin` / `admin123`). Change the password at first login before configuring the Xianyu account, default fixed cloud-drive link, and SKU override links.

No SMTP is required, and no ordinary user registration is needed: registration is permanently disabled. The package supports local automatic delivery, publish, and republish workflows without forced online registration or embedded credentials.

Keep `data/`, `browser_data/`, `logs/`, and local configuration inside the installation directory. Separate computers and separate installation directories have separate SQLite databases and Cookies. Do not copy an already-running directory to another buyer or run two instances from one installation directory.

## 编译版安装包

本项目的 Windows 分发形式是单目录编译包。安装包包含可执行文件、网页资源、Node.js、Playwright Chromium、许可证和 `SOURCE-CODE.md`，不包含 Python 源文件，也不要求使用 Docker。

对应源码仓库、标签和提交号会写入安装包内的 `SOURCE-CODE.md`。安装包可以作为编译、打包、安装指导和技术支持服务的一部分收费，但不授予独占权，也不禁止买家修改和再次分发。

## 买家使用步骤

1. 将 zip 解压到有写入权限的文件夹，例如“文档”或“桌面”。
2. 直接双击 `XianyuAutoDelivery.exe`，程序会自动创建运行目录并打开浏览器面板。
3. 在面板中使用买家自己的闲鱼账号扫码登录，再同步自己的商品。
4. 在“商品管理”中设置默认网盘链接和少量 SKU 专属链接。

双击 EXE 后不显示终端窗口，运行日志保存在安装目录的 `logs/`。扫码登录成功后默认进入约 3 分钟稳定期，页面会自动倒计时，期间不要重复扫码。

安装包不会带入发布者的 Cookie、Token、数据库、日志、邮箱授权码、API 密钥或卖家专属网盘链接。买家必须使用自己的账号和本地数据。

## 创建桌面快捷方式

如果买家希望桌面双击启动，可在安装包目录中右键打开 PowerShell，执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\create_desktop_shortcut.ps1
```

快捷方式脚本只写入当前 Windows 用户的桌面，不写死开发者个人路径，并且直接指向 `XianyuAutoDelivery.exe`。不需要运行首次安装脚本或启动批处理文件。

## 构建流程

先在源码仓库中构建可执行文件：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\build_windows_executable.ps1 -Clean
```

然后生成带源码定位信息的 zip：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\build_windows_distribution.ps1 `
  -SourceTag "working-tree-uncommitted" `
  -SourceCommit (git rev-parse HEAD)
```

输出位于源码仓库的 `dist` 目录。构建脚本只复制已经编译的 payload、快捷方式工具、许可证和必要文档，并在压缩前检查 Python 源文件、数据库、日志、Cookie、Token 和密钥模式。

## GitHub 发布

GitHub 仓库发布源码和文档；编译 zip 可以作为 Release 附件或单独分发，但不要把买家运行数据、`venv`、`data`、`browser_data`、`logs`、数据库或登录会话提交到 Git。

发布前检查 Git 状态，确认没有 Cookie、Token、密码、验证码、浏览器会话、SQLite 数据库、日志或完整敏感网盘链接。源码仓库不需要读取、复制或上传当前账号的登录内容。

## 默认安全配置与验收

`global_config.yml` 默认保持：

```yaml
REPUBLISH:
  enabled: false
  dry_run: true
```

先按 [Windows 自动发货与重新发布运行手册](windows-republish-runbook.md) 做低价验收。只有在确认订单、默认链接、SKU 链接和新商品 ID 都正确后，才由操作者自行决定是否进入真实模式。

## 隐私和安全边界

不要把 Cookie、Token、密码、验证码、完整敏感网盘链接、日志或聊天记录发给分发者，也不要上传到 GitHub。遇到退款、取消、风控、链接失效、账号失效或发布失败，应暂停模板并人工处理。

## 许可证

请同时阅读根目录 `LICENSE` 和安装包内的 `SOURCE-CODE.md`。分发编译包时必须保留许可证和版权声明；修改后再次分发时，应说明修改内容和日期，并继续遵守 AGPL-3.0 的对应源码要求。
