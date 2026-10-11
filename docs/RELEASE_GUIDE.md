# Windows发布版

普通用户无需安装VS Code、Python或PowerShell。将 `SJTUFitMan-v版本号-windows-x64.zip` 整体解压，双击 `交大健身房助手.exe` 即可；电脑需有Chrome。

## 维护者在本机生成

准备好README中的开发环境后，在项目目录执行：

```powershell
pwsh -NoProfile -File .\scripts\build_release.ps1
```

脚本安装固定版本的打包工具，执行本地测试，生成独立Windows程序，再运行发布版的离线自检。输出位于 `dist/`：

- `SJTUFitMan-v0.1.0-windows-x64.zip`：分发给用户的完整压缩包。
- 同名 `.zip.sha256`：用于核对下载是否完整。
- 同名文件夹：可直接测试；分享时使用原始ZIP。

发布包自带Python、OCR模型、OpenCV、浏览器驱动和界面；Chrome由用户电脑提供。主程序和后台模块共用内置依赖。打包定义见 `packaging/SJTUFitMan.spec`；实现依据[PyInstaller打包文档](https://pyinstaller.org/en/stable/spec-files.html)及[Playwright打包说明](https://playwright.dev/python/docs/library#pyinstaller)。

程序只带空白配置样例，首次启动才生成自己的 `config/config.toml`。不会收集本机账号、授权码、会话、订单或邮件记录。发布前会分别检查源码/暂存区及生成的程序目录；发现个人信息就停止打包。

自检验证实际exe中的OCR推理、OpenCV、Playwright驱动、所有界面和后台命令。验证不访问体育平台、不创建订单、不发邮件；不替代真实学校服务器的预约验证。

当前输出为64位Windows免安装文件夹，保留完整目录即可使用。以后需要安装向导、快捷方式或自动升级，可在同一程序目录基础上制作安装包。

## 后续版本

修改 `scripts/version.py` 中的版本号，例如 `0.1.1`，再运行打包脚本。已有版本不会被覆盖，避免碰到已经开始使用的应用目录。构建日志保存在 `build/`，打包结果和日志都不会进入Git。

用户升级时先关闭旧程序，把新版本解压到新文件夹，再将自己的 `config/config.toml` 和 `.local/` 复制到相同位置。应用的配置不写入exe，升级时可继续使用。

## 在GitHub发布

1. 将源码推送到自己的GitHub仓库。
2. 在仓库 **Releases → Draft a new release** 中选择或创建与版本一致的标签，如 `v0.1.0`。
3. 上传本地生成的ZIP和SHA256文件，写明版本变化，然后点击 **Publish release**。
4. 发给同学的是Release页面或ZIP附件链接；GitHub自动生成的 **Source code** 仍是源码。

也可在 **Actions → Build Windows release → Run workflow** 中生成发布包，下载构建产物后上传到Release。推送 `v*` 标签时也会构建；标签必须与程序版本一致。工作流只生成并保存附件，不需要个人账号或邮箱授权码；正式发布由维护者操作。

GitHub构建尚未运行前，本机生成的ZIP已经可以直接分发。是否已在GitHub公开发布，以仓库的Release页面为准。
