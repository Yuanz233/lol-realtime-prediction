# Windows Server 部署

项目可以直接从 GitHub 拉取后运行。服务器需要 Git、64 位 Python 3.9+ 和可访问 `lolesports.com`、`feed.lolesports.com` 的网络；不需要 Docker、GPU、Node.js 或发布压缩包。

## 1. 首次部署

把下面的 GitHub 地址替换为你之后创建的仓库地址，在 PowerShell 中执行：

```powershell
New-Item -ItemType Directory -Force C:\Apps | Out-Null
Set-Location C:\Apps
git clone https://github.com/你的账号/你的仓库.git lol-realtime-prediction
Set-Location .\lol-realtime-prediction

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

如果 PowerShell 阻止激活脚本，只对当前终端临时放行后重试：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
```

当前 `requirements.txt` 只安装 `certifi`，用于给 Windows Server 上的 Python 提供可信 HTTPS 根证书。HTTP 服务、SQLite、训练和预测本身仍使用 Python 标准库。

如果服务器只有 `py` 命令，则把上面的 `python -m venv .venv` 改为 `py -3 -m venv .venv`。

仓库应包含以下首次运行所需文件：

- `data\matches.sqlite3`：已有历史比赛和胜率曲线；
- `models\live.json`：当前训练模型；
- `training_manifest.json`：已核验训练清单。

`.env`、日志、备份、SQLite 的 `-wal`/`-shm` 文件和调试抓包不会提交到 GitHub。

## 2. 在终端启动

在项目根目录执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\start-windows.ps1 -Port 8000
```

启动脚本优先使用 `.venv\Scripts\python.exe`，绑定 `0.0.0.0:8000`，并启用免费的 LoL Esports 数据源。默认监控 `lpl,lck,lec,vcs,worlds`，每30秒保存一个训练/展示点，同时在后台补齐最近结束的比赛。

检查服务：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

在浏览器打开 `http://服务器IP:8000`。终端窗口需要保持打开；按 `Ctrl+C` 停止服务。

如果只监控部分赛区：

```powershell
.\scripts\start-windows.ps1 -Port 8000 -Leagues "lpl,lck"
```

如果只在本机根据回放确认待核验胜方，可临时启用编辑按钮：

```powershell
.\scripts\start-windows.ps1 -Port 8000 -AllowManualResult
```

公网运行时不要开启 `AllowManualResult`。

## 3. 更新代码

先在正在运行服务的终端按 `Ctrl+C`，然后执行：

```powershell
Set-Location C:\Apps\lol-realtime-prediction
git pull --ff-only
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
.\scripts\start-windows.ps1 -Port 8000
```

`git pull` 不会删除服务器运行期间写入的数据库。若你后来在本机也提交了新版 `data\matches.sqlite3`，而服务器上的同一文件已有运行数据，Git 会拒绝覆盖或产生冲突；这时应保留服务器数据库，只更新代码和模型，不要直接丢弃数据库。

## 4. 防火墙

管理员 PowerShell 执行：

```powershell
New-NetFirewallRule -DisplayName "LoL胜率服务 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

云服务器还需在云厂商安全组中开放 TCP 8000。个人使用建议只允许自己的公网 IP。使用域名时，建议由 IIS、Caddy 或其他反向代理提供 HTTPS，并只让反向代理访问本服务端口。

## 5. 备份和重新训练

停止服务后执行备份：

```powershell
.\.venv\Scripts\python.exe .\backup.py
```

积累足够多的已核验比赛后再训练并重算历史曲线：

```powershell
.\.venv\Scripts\python.exe .\backup.py
.\.venv\Scripts\python.exe .\train_model.py --db .\data\matches.sqlite3 --output .\models\live.json
.\.venv\Scripts\python.exe .\apply_model.py --db .\data\matches.sqlite3 --model .\models\live.json
.\scripts\start-windows.ps1 -Port 8000
```

只有带可信逐局胜方的比赛会进入训练；“赛果待核验”的曲线会保留，但不会进入训练样本。

## 6. 可选：开机自动运行

先按第2节完成前台验证。如果以后不想保持终端窗口，可以管理员身份执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install-windows-task.ps1 -Port 8000
Get-ScheduledTask -TaskName LoLRealtimePrediction
Get-Content .\logs\server.log -Tail 50 -Wait
```

停止或移除任务：

```powershell
Stop-ScheduledTask -TaskName LoLRealtimePrediction
Unregister-ScheduledTask -TaskName LoLRealtimePrediction -Confirm:$false
```
