# 开发与维护

普通用户请先阅读[使用说明](../README.md)，下载免安装版即可使用。本文面向修改源码、测试或打包的维护者。


## 开发者：源码安装与启动

当前支持 **Windows、Python 3.13（64 位）、Google Chrome和Microsoft Edge**。使用项目独立 `.venv`，依赖版本固定在 `requirements.txt`。

登录后台在点击登录按钮后才启动浏览器驱动。关闭软件时会等待当前操作和登录浏览器完成清理；看到“正在关闭”时请稍候，窗口会自行退出。下拉选项列表使用自己的滚轮处理，页面滚动不会干扰选项选择。

自动登录会等待jAccount页面跳转完成，账号、密码输入框可见且可编辑后才填入；页面等待最长30秒。加载超时会提示重新打开登录页或手动登录。登录诊断记录包含实际使用的浏览器，不包含账号密码。

1. 安装 [Python 3.13](https://www.python.org/downloads/windows/)、[Chrome](https://www.google.com/chrome/) 或 [Microsoft Edge](https://www.microsoft.com/edge)其中一种，以及[PowerShell 7](https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-windows)。Python安装时勾选加入PATH。
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

普通启动显示自动预约表单，点击“保存并启动”后才运行轮询。已有成功订单的漏发邮件会由后台补发；没有成功记录时不会发送邮件。浏览器使用本机Chrome或Edge，无需另外下载Playwright Chromium。

在 **设置 → 账号与登录 → 使用浏览器** 选择“自动选择”“Google Chrome”或“Microsoft Edge”。选择后自动保存到统一配置，登录、恢复会话、查询时段、候补和每日预约共用。自动模式优先Chrome，未安装或仅在启动阶段失败时尝试Edge；明确选择时遵从该选择，缺少浏览器会提示安装或重新选择。两个浏览器均启动独立窗口，可复用助手保存的体育平台会话。已打开的窗口在下一次启动时切换，预约进行期间不能修改浏览器设置。



## 项目结构

```text
交大健身房助手.pyw          # 唯一桌面入口
config/
  config.example.toml       # 可提交的空白样例
  config.toml               # 个人设置，Git 忽略
scripts/
  app_ui.py                 # 统一窗口与任务协调
  automatic_ui.py           # 两种计划共用的自动预约表单
  records_ui.py              # 订单与邮件记录、异常核查
  settings_ui.py             # 账号登录、邮箱设置
  desktop_worker.py          # 隐藏后台进程、页面生命周期与滚动容器
  configuration.py          # 统一配置读写与初始化
  login.py / login_flow.py   # 登录与会话
  booking_flow.py            # 订单、验证码和下单校验
  poll_runner.py / poll_settings.py
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



## 订单生命周期

`confirmed` 表示曾核实创建真实订单，供成功通知恢复使用。`current_order` 保存最近一次对该订单号的只读核查结果；只有完整匹配原场地、日期、全部小时和金额，并返回明确取消/过期状态，才解除相应订单的重复下单限制。状态不明、申诉和退款中仍保留限制。任务重新启动时先核查相关旧订单，确认失效后才继续轮询；成功通知按账号和订单号去重。

[打包与发行](RELEASE_GUIDE.md)单独维护。
