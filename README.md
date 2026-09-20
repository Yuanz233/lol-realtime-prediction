# LoL 职业比赛单局胜率观察

本项目提供手机网页、历史曲线、免费多赛区实时采集、CPU模型训练和SQLite持久化。**无需 API key 的历史帧**已从 LoL Esports 网站公开接口实测，项目已导入2026 LPL、VCS和LEC共56局、3,776条存储帧；其中46局已有可信胜方，以2,920个均匀30秒样本重训CPU逻辑回归模型，另外10局显示“赛果待核验”。2026-09-19的JDG–IG直播十分钟实测中，`window`响应约每10秒变化，最新源帧到接收的中位滞后约77秒。Cito 适配器只作为默认关闭的旧备选。

完整设计见[架构与部署设计](./架构与部署设计.md)，服务器操作见[部署与迁移](./部署与迁移.md)。数据依据和测量记录见[训练数据采集记录](./训练数据采集记录.md)、[实时帧实测](./实时帧实测_2026-09-19.md)、[实时调试计划](./实时调试计划.md)、[帧字段与训练样本](./帧字段与训练样本.md)、[免费数据源验证](./免费数据源验证.md)、[调研与实施方案](./调研与实施方案.md)和[付费成本分析](./付费成本分析.md)。当前模型已经训练但尚未做独立概率校准。

Windows Server 原生部署和开机自启见[Windows服务器部署](./Windows服务器部署.md)。

## 从 GitHub 部署到 Windows Server

服务器只需要 Git 和 Python 3.9+，不需要 Docker、GPU或发布包。仓库会携带当前生产模型 `models/live.json`、历史数据库 `data/matches.sqlite3` 和训练清单；`.env`、日志、SQLite WAL、调试抓包和备份不会提交。

首次部署，在 PowerShell 中执行：

```powershell
Set-Location C:\Apps
git clone https://github.com/你的账号/你的仓库.git lol-realtime-prediction
Set-Location .\lol-realtime-prediction
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\start-windows.ps1 -Port 8000
```

打开 `http://服务器IP:8000`，按 `Ctrl+C` 停止。启动脚本默认启用免费的 LoL Esports 采集器，自动发现 LPL、LCK、LEC、VCS 的当前或下一场比赛，并优先使用项目 `.venv`。完整的首次部署、更新、备份和可选开机自启步骤见[Windows服务器部署](./Windows服务器部署.md)和[部署与迁移](./部署与迁移.md)。

## 本地启动

Python 3.9+，首版无需 GPU。`requirements.txt` 只安装 `certifi`，用于在 Windows Server 上提供可验证的 HTTPS 根证书。建议建立虚拟环境并执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

打开 `http://127.0.0.1:8000`。直接运行默认启用免费的 LoL Esports 采集器，并自动发现 LPL、LCK、LEC、VCS；它使用网站展示接口，仍属于没有稳定性保证的实验路径。默认每30秒保存一次，与训练样本间隔一致。如需纯离线查看历史，可用 `LIVE_PROVIDER=none python app.py`。

网页、JavaScript、CSS和JSON接口都返回 `Cache-Control: no-store`，更新代码并重启服务后，手机重新载入页面即可取得最新版本。

需要在本机显示待核验赛果按钮时运行：

```bash
LIVE_PROVIDER=lolesports \
LOLESPORTS_LEAGUES=lpl,lck,lec,vcs \
ALLOW_MANUAL_RESULT=true \
python app.py
```

`ALLOW_MANUAL_RESULT=true` 会在“赛果待核验”的历史对局下显示两个胜方确认按钮，需要同时填写回放或逐局赛果的 HTTPS 链接。它适合本机自用，公网部署默认关闭。

前端本地渲染预览会复制现有历史数据库到临时目录，并加入一场虚构的进行中比赛；关闭进程后临时数据自动清理，不污染正式数据库：

```bash
python3 preview.py
# 打开 http://127.0.0.1:8001
python3 preview.py --no-live  # 检查“暂无实时比赛”状态
```

Cito 代码仍保留用于必要时比较，但必须同时显式设置 `LIVE_PROVIDER=cito` 与 `CITO_API_KEY` 才会运行；当前定时调试任务禁止使用它。

## 训练与历史曲线

训练采用 CPU 正则化逻辑回归，输入是**实际可取得并经过校验**的局内时间，以及蓝红双方的金币、击杀、防御塔、小龙、Baron、兵营差值。推理只需少量乘加和 sigmoid；模型计算开销通常远小于数据抓取间隔。训练脚本也可在普通 CPU 机器运行，不依赖 GPU。若比赛样本增多，可再以 CPU LightGBM 做对照，并对概率校准；不要直接用论文或其他 API 的现成权重。

在线采集的帧留在 SQLite。服务启动后还会每6小时扫描最近14天的已完赛对局，把遗漏的30秒曲线自动补入历史页。LoL Esports 的公开赛程只给系列赛总比分，`window` 完赛帧也没有明确的单局胜方字段，所以新曲线会先显示“赛果待核验”，并排除在训练之外；不能依据末帧经济差或系列比分猜胜者。核对独立单局赛果后再用 `finalize_game.py` 补标签。项目数据库目前覆盖 2026-09-03 至 09-19 的 11 场 BO 系列赛、43 小局。当前历史曲线统一使用 `models/live.json` 中的模型重算，并在页面/API中标为未校准训练模型。免费路径的实测、B 站页核查和剩余风险见[免费数据源验证](./免费数据源验证.md)。

旧 Cito 适配器的在线历史曲线只包含上线后实际收集的对局，当前免费路线不会调用它。通用归档原则保持不变：如果数据没有可映射的明确胜者，保留为超时对局，不根据经济领先伪造结局；若只有赛后胜者而没有终局帧，末点保留最后一次**预测值**，不会把它伪装成终局时间的 0% 或 100%。旧适配器可手动对账：

```bash
python3 reconcile.py --game-id YOUR_GAME_ID
```

离线导入的 JSONL 为一行一个本项目规范帧，含 `game_id`、`match_id`、`current_timestamp`、蓝红双方 `id/name/gold/kills/towers/drakes/nashors/inhibitors`；完赛帧需含 `finished: true` 与 `winner_id`。例如：

```json
{"game_id":101,"match_id":9,"current_timestamp":600,"finished":false,"winner_id":null,"blue":{"id":1,"name":"蓝队","gold":18000,"kills":3,"towers":1,"drakes":1,"nashors":0,"inhibitors":0},"red":{"id":2,"name":"红队","gold":17000,"kills":2,"towers":0,"drakes":0,"nashors":0,"inhibitors":0}}
```

```bash
python3 import_frames.py path/to/frames.jsonl
python3 train_model.py
```

免费历史帧导入示例（该局蓝方 BLG、红方 AL，独立单局结果见 Games of Legends）：

```bash
python3 import_lolesports_history.py 117155436343202203 \
  --blue-name BLG --red-name AL --winner-side red \
  --label-source https://gol.gg/game/stats/82967/page-game/
```

批量收集时，先核对[training_manifest.json](./training_manifest.json)里每小局的独立结果与时长，再运行：

```bash
python3 crawl_training_history.py --index-only
python3 crawl_training_history.py
python3 audit_training_history.py
python3 train_model.py --output models/candidate_30s.json
python3 apply_model.py --model models/candidate_30s.json
```

[crawl_training_history.py](./crawl_training_history.py)只读官网公开赛程网页以发现比赛/队伍 ID，按人工核对的胜者清单获取免费历史帧；它在写库前比较官网 ID、队伍、逐局胜者汇总和独立页面的终局时长。已导入的单局会跳过，结果及失败局记在 `data/history_crawl_report.json`。[audit_training_history.py](./audit_training_history.py)可离线核对数据库与报告中的标签、逐帧时间轴和队伍 ID。Games of Legends 页面这次对脚本直连返回 403，因此清单的胜者由独立网页阅读核对，**没有自动从经济领先推断**。

训练脚本至少要求20局带胜者的完整对局与5场不同系列赛，使用真实30秒采样，按**整个BO系列赛**分组，最近20%系列赛作为按时间顺序的留出集；输出log loss与Brier score，再用全部已标注对局拟合最终生产参数。目前时间留出集为最近3场系列赛的11局、731帧，log loss 0.5633、Brier score 0.1994；恒定预测50%的Brier为0.2500。生产参数随后用全部46局、12场系列赛、2,920个训练帧重拟合。46局仍只是原型规模，尚不足以声称模型可靠或完成概率校准；应继续积累数百局、多个赛区和多个版本，并按局及按赛区评估。

## 接口与部署

- `GET /api/matches`：进行中、历史和待归档对局；超过两分钟未更新的未完赛记录进入历史区，不再算作直播。
- `GET /api/games/{game_id}`：当前统计和蓝方概率曲线。
- `GET /api/status`：总状态、逐赛区轮询状态、历史同步状态、最近帧、错误、模型类型、训练规模和留出指标。
- `GET /api/health`：服务健康、运行时间和模型类型。

公网部署由 Nginx/Caddy 提供 HTTPS 并代理到本服务，以服务管理器常驻运行、备份 SQLite。已对43局LPL、10局VCS、3局LEC历史帧导入、一局LCK历史帧结构和LPL/LEC直播读取进行验证。直播实测表明接口发布延迟会波动；采集器会在查询时间过新而返回HTTP 400时自动向前回退。这个网站接口没有稳定性承诺，后续赛事仍需监控字段、延迟和断流。
