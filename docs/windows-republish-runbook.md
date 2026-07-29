# Windows 自动发货与重新发布运行手册

## Single-user first use and isolation

For the compiled workflow, each buyer gets one package for one computer and one installation directory. Unzip it, double-click `XianyuAutoDelivery.exe`, and use the local default administrator account (`admin` / `admin123`); change the password at first login. Then configure the Xianyu account, default fixed cloud-drive link, and SKU override links.

No SMTP is required and no ordinary user account registration is needed. Registration is permanently disabled. Automatic delivery, publish, and republish remain local workflows. Keep `data/`, `browser_data/`, `logs/`, SQLite, Cookies, and local configuration in that installation directory. Do not copy an already-running directory to another buyer or run two instances from one directory.

本文针对 Windows、PowerShell、无 Docker、单个闲鱼账号。自动补发功能默认关闭，第一次使用请按“低价验收”执行。

## 1. 安装与启动

在 PowerShell 中进入项目目录：

```powershell
Set-Location "D:\MY_AI_PROJECTS\local-ai-fish\xianyu-auto-reply-fix"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
python Start.py
```

本项目不使用 Docker。若 PowerShell 阻止激活脚本，应由操作者按本机安全策略处理；不要为了绕过策略复制不明脚本。

启动后按页面提示打开浏览器，用闲鱼账号扫码登录。只使用一个闲鱼账号运行本功能；不要在文档、截图或日志中粘贴 Cookie、Token、密码、验证码。

## 2. 先同步商品，再配置链接

1. 等待扫码登录完成，并在页面执行一次商品同步。
2. 打开“商品管理”，找到已经同步的商品。
3. 在自动发货/重新发布配置中填写默认固定网盘链接或文本。
4. 对少数特殊商品，在 SKU 覆盖中填写对应 SKU 的不同网盘链接；未配置覆盖的 SKU 使用默认内容。
5. 按商品需要打开“自动发货”和“自动重新发布”，或先保持暂停进行检查。

链接配置会保存在本机数据库。不要把完整链接放进工单、截图、日志或公开仓库；如果链接本身含有提取码或其他敏感参数，也按凭据处理。

## 3. 安全默认值与开启顺序

`global_config.yml` 中的安全默认值是：

```yaml
REPUBLISH:
  enabled: false
  dry_run: true
  check_interval_seconds: 30
  delay_seconds: 300
  max_retries: 3
  retry_backoff_seconds: [300, 900, 1800]
  account_id: ''
```

默认配置下先保持 `enabled: false`、`dry_run: true`。注意：`enabled: false` 不会启动补发运行时，因此不会产生补发任务。开始演练前先备份数据库，临时改为 `enabled: true`、`dry_run: true`，然后重启 Start.py；演练阶段只记录计划并生成预览，不真实发货/发布。使用一个金额低、内容无误、可接受人工介入的测试订单，确认订单进入已发货流程后查看任务摘要，同时检查生成内容是默认链接或正确的 SKU 特殊链接。

演练任务只入队，不会执行真实发布；演练前先备份，修改后必须重启 Start.py。低价验收通过后，再将 `dry_run: false`（真实配置也可写作 `REPUBLISH.enabled=true`）并重启。若不启用真实功能，恢复 `enabled: false`；此时 `enabled: false` 不会启动补发运行时。

回滚时恢复 enabled: false，并保持 dry_run: true。
enabled: false 不会启动补发运行时。

低价验收通过后，停止程序，编辑 `global_config.yml`，再启动程序。真实模式的关键改动是保留 `enabled: true`（对应 YAML 为 `enabled: true`），并把 `dry_run=false`；如果暂时不启用真实功能，就恢复 `enabled: false`、`dry_run: true`。不要在未完成低价验收前直接切换真实模式。

自动重新上架的实际含义是：通过 `ItemPublisher` 按当前模板新发布一个商品，并记录平台返回的新 ID。它不承诺调用闲鱼官方“原商品重新上架”接口，也不会保证原商品 ID 保持不变。

## 4. 查看任务与人工处理

在“商品管理”中可查看模板的最近结果摘要和 dry-run 状态：

- “暂停”会暂停该模板；处理完问题后点击“恢复”。暂停是安全的止损手段。
- “立即检查”只创建检查任务，不绕过 dry-run、商品状态、开关、账号或权限检查。
- 任务进入人工处理/`manual_required` 时，不要反复点击立即检查。先暂停模板，核对原商品是否仍在售、图片与链接是否完整、账号是否仍登录，再人工完成发货或重新发布，并记录新商品 ID。
- 连续重试失败、商品状态未知、发布返回 ID 为空或与旧 ID 相同、链接缺失、权限异常，都应按人工处理条件停下来核对。

若出现买家退款、取消或平台风控提示，立即暂停相关模板并人工处理；不要为了追求自动化而继续重试。

## 5. 备份与恢复

SQLite 正在写入时不要复制数据库。先在页面暂停自动任务，然后正常关闭 `Start.py`，确认进程已经退出，再在 PowerShell 中备份：

```powershell
Copy-Item ".\data\xianyu_data.db" ".\data\xianyu_data.db.backup-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

备份文件也可能包含订单、账号关联和链接配置，只保存在受控的本机目录，不要上传或分享。恢复前先关闭程序并保留当前数据库副本，再由操作者将已验证的备份文件恢复为 `data/xianyu_data.db`；恢复后先保持 `enabled: false`、`dry_run: true`，重新登录、同步商品并检查配置。

## 6. 故障回滚清单

1. 在“商品管理”立即暂停相关模板；必要时停止程序。
2. 将 `REPUBLISH.enabled` 改回 `false`，并将 `dry_run` 改回 `true`，然后重新启动确认不再入队。
3. 核对任务摘要和人工处理条件；已新发布的商品用记录的新 ID 手动下架或修改，避免重复销售。
4. 若配置或数据库损坏，按上一节在程序关闭状态下恢复备份；恢复后只做低价验收，不要直接开启真实模式。

日志只记录必要的任务状态和摘要。不要在文档、日志或聊天中粘贴 Cookie、Token、密码、验证码或完整敏感链接。自动补发相关的模板、任务和账号关联数据默认只保存在本机项目的 SQLite 数据库中；浏览器登录会话仍应按账号凭据的安全要求保护。
