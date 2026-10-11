# SJTUFitMan · 交大健身房助手

在自己的 Windows 电脑上运行，登录交大体育平台、查询预约余量、创建待付款订单，并用邮件提醒付款。每位同学使用自己的账号、邮箱和本地配置；程序不会自动付款，也不调用 LLM。

## 功能

| 页面 | 用途 |
| --- | --- |
| 登录与会话 | 本地 ddddocr 图片识别或手动登录，保存并验证会话 |
| 单次预约 | 日期和时段下拉选择、余量检查、须知确认、订单核查 |
| 每日开抢 | 北京时间每天 12:00 预约七天后，按候选优先顺序尝试 |
| 退选候补 | 选择今天至七天后的固定日期/时段，立即或定时等待退选名额 |
| 邮件设置 | 配置发信邮箱、发送测试邮件、查看和重试付款提醒 |

支持 **1 小时或连续 2 小时**。两小时要求同一场地的两个小时都有余量，合为一张订单。登录验证码使用本地 ddddocr，预约滑块使用本地 OpenCV；失败时转手动处理。

## 普通用户：下载发布版

没有VS Code或Python也可以使用。下载发布附件 **`SJTUFitMan-v版本号-windows-x64.zip`**，将整个文件夹解压到桌面或“下载”目录，双击 **`交大健身房助手.exe`**。电脑仍需安装Chrome；账号、邮件、日期和时段均通过界面设置。

发布版自带Python运行环境、ddddocr模型、OpenCV和Playwright驱动。首次启动在应用目录创建自己的 `config/config.toml`，运行记录保存在 `.local/`。个人配置写在程序目录外层，不写入exe；请保留完整解压目录。

本地生成的发布包位于 `dist/`。仓库没有发布附件时，由维护者按[打包说明](docs/RELEASE_GUIDE.md)生成；GitHub 的 **Source code** 压缩包是开发者源码，不是普通用户的可执行发布版。

## 开发者：源码安装与启动

当前支持 **Windows、Python 3.13（64 位）和 Google Chrome**。使用项目独立 `.venv`，依赖版本固定在 `requirements.txt`。

1. 安装 [Python 3.13](https://www.python.org/downloads/windows/)、[Chrome](https://www.google.com/chrome/) 和 [PowerShell 7](https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-windows)。Python 安装时勾选加入 PATH。
2. 克隆仓库，或在仓库页面选择 **Code → Download ZIP** 并解压。
3. 在项目文件夹打开 PowerShell 7，执行：

   ```powershell
   pwsh -NoProfile -File .\scripts\setup_env.ps1
   ```

   如果有多个 Python，可指定 Python 3.13 的路径：

   ```powershell
   pwsh -NoProfile -File .\scripts\setup_env.ps1 -BasePython 'C:\路径\Python313\python.exe'
   ```

4. 双击根目录唯一入口 **`交大健身房助手.pyw`**。也可在终端启动：

   ```powershell
   .\.venv\Scripts\pythonw.exe .\交大健身房助手.pyw
   ```

普通启动只显示首页，不自动预约或发送邮件。浏览器使用本机 Chrome，无需另外下载 Playwright Chromium。

## 统一个人配置

所有用户设置集中在 **`config/config.toml`**。首次安装或启动时，程序会从 [config/config.example.toml](config/config.example.toml) 创建个人副本；也可以手动复制样例。

| 配置区块 | 内容 |
| --- | --- |
| `[auth]` | jAccount 账号密码、会话文件路径 |
| `[site]` | 体育平台地址与时区 |
| `[polling]` / `[booking]` | 每日任务启用状态、候选时段、场地和轮询限制 |
| `[waitlist]` | 固定候补日期/时段、立即或定时开始、查询间隔 |
| `[mail]` | 发信邮箱、收件邮箱、SMTP 授权码和邮件重试设置 |

账号可以在 **登录与会话 → 保存账号到本地配置** 中保存；预约、候补和邮件设置也通过界面保存到同一文件。密码和授权码在本机配置中为明文，请保管自己的电脑和文件。样例不包含任何个人账号、邮箱或授权码。

之后只使用这一份个人配置，安装和再次启动都会保留已填写的设置。程序不创建配置备份。

## 第一次使用

1. 在 **登录与会话** 中填写自己的账号密码，点击打开登录页，再选择 OCR 自动登录或手动输入图片验证码。成功后自动保存会话。
2. 在 **邮件设置** 中填写自己的发信邮箱、授权码及收件邮箱，点击发送测试邮件并检查收件箱。QQ 邮箱可沿用样例中的服务器、465 端口和 SSL；授权码来自邮箱的 SMTP 设置，不能用邮箱登录密码代替。
3. 选择一种预约方式并明确点击启动。切换页面不会中断任务，底部显示当前任务状态。

### 每日开抢

添加目标时段并调整优先顺序，设置查询间隔、当日时间窗口和最多查询次数。勾选启用每日预约，再点击启动。12:00 前只等待本机时间；成功后停止当天查询、发付款提醒，次日继续执行。

每日查询间隔允许 20～600 秒，时间窗口 1～60 分钟，最多 1～60 次。达到任一上限会结束当天任务。

### 退选候补

选定具体日期和时段，可同步网站当前开放选项；选择立即开始或从下拉选项设置北京时间开始时间，再填写候补查询间隔并启动。候补独立使用 20～600 秒的间隔，不受每日次数或一小时时间窗口限制。

持续到 **预约成功、手动停止或目标时段开始**。例如 `18:00-20:00` 在当天 18:00 到期。成功后结束此次候补，发付款提醒，不启动次日候补。

两种模式的查询间隔从上一次查询结束后计算。查询间隔并不等于网站所有请求的间隔，登录、页面初始化、验证码及订单核查还会产生请求。登录失效、限频、已有待付款订单或下单结果不明时停止并提示处理；普通查询超时会退避，连续三次后停止。

### 付款与关闭

付款提醒包含日期、时段、金额、订单号和体育平台入口。请尽快在体育平台 **个人订单** 中核对并自行付款，期限以平台显示为准。

电脑需保持开机、联网且不睡眠。关闭窗口会请求停止轮询，已发出的下单先完成核实后退出。成功记录先保存再发邮件；重启、邮件失败或修改频次不会导致重复预约。邮件服务器接受不保证收件箱已送达，请检查垃圾邮件。

## 项目结构

```text
交大健身房助手.pyw          # 唯一桌面入口
config/
  config.example.toml       # 可提交的空白样例
  config.toml               # 个人设置，Git 忽略
scripts/
  app_ui.py                 # 统一窗口与任务协调
  configuration.py          # 统一配置读写与初始化
  login.py / login_flow.py   # 登录与会话
  booking_ui.py / booking_flow.py
  poller_ui.py / poll_runner.py / poll_settings.py
  captcha.py / slider.py     # 本地验证码
  mail_notice.py             # 付款提醒和邮件去重
  setup_env.ps1              # 安装独立环境
  check_release.py           # 发布文件与个人信息检查
  build_release.py           # 打包、发布版自检、生成ZIP和校验文件
packaging/                  # Windows打包定义
docs/                       # 操作说明与历史验证报告
tests/                      # 本地模拟测试
.local/                     # 会话、验证码图片、订单、邮件等私有运行记录
.github/workflows/          # GitHub 自动测试
```

各页面复用执行模块，每次只运行一个登录、预约或邮件操作；进程间还通过文件锁限制重复任务。

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
.\.venv\Scripts\python.exe scripts\check_ui.py
.\.venv\Scripts\python.exe scripts\manage_config.py check
.\.venv\Scripts\python.exe scripts\check_release.py
```

Python 测试使用模拟浏览器、模拟邮件和临时配置，不访问体育平台，不创建真实订单。安装 Node.js 后还可运行：

```powershell
node --test tests\appointment_snapshot.test.cjs tests\checkout_snapshot.test.cjs tests\export_session.test.cjs tests\two_hour_frontend.test.cjs
```

最后一项历史前端测试依赖不随仓库分发的 `.inspection/` 缓存，干净检出时自动跳过。GitHub 工作流只运行本地测试，不需要个人账号或邮箱授权码。

真实网站的登录、只读查询和前端模拟已有历史验证；真实预约下单尚未完成学校服务器的端到端验证。当前版本不能保证抢到名额。操作详情见 [登录说明](docs/LOGIN_GUIDE.txt)、[预约说明](docs/BOOKING_GUIDE.txt)、[轮询说明](docs/POLLING_GUIDE.txt) 和 [验证码历史实测](docs/CAPTCHA_REPORT.txt)。

## 提交到 Git

提交代码、样例、依赖文件和文档。`.gitignore` 排除个人配置、`.local/`、`.venv/`、网站缓存和临时文件；`check_release.py` 会检查工作文件和 Git 暂存内容，并在本地比对是否含有个人配置中的账号或授权码，只报告文件名，不输出秘密内容。

不要强制添加被忽略的个人文件，也不要把整个工作目录压缩后分享。使用 GitHub 的源代码下载或 `git archive` 可只导出已提交文件。每位同学首次运行后会生成自己的配置和会话，互不共用账号。
