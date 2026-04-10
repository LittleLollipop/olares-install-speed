## 目标
监控一个应用从“点击安装”到“最终可访问 / `Running`”之间的**每一个阶段**与**每一个容器关键步骤**的耗时，并把数据导出为 `JSON/CSV`，用于后续针对性优化安装速度。

本目录提供一个可直接运行的采集脚本：`collect_install_timeline.py`。
也提供一个实时监控脚本：`watch_install_live.py`（终端 UI）。

## 你将得到什么
- **阶段级耗时（AppManager 状态机）**：`Pending -> Downloading -> Installing -> Initializing -> Running`
- **Pod 级耗时**：
  - 调度：`PodScheduled`
  - 拉镜像：从 event `Pulling` 到 `Pulled`（以及 ImagePullBackOff 等失败原因）
  - 创建/启动：`Created` / `Started`
  - 就绪：Pod `Ready=True` 的时间点
- **容器级耗时**：
  - `containerStatuses[*].state.running.startedAt`（容器实际进入 running 的时间）
  - `containerStatuses[*].ready` 变化（配合 Pod Ready 时间）

> 说明：Kubernetes 事件与状态字段在不同集群/运行时可能略有差异；脚本会尽量兼容，并在缺失时给出空值。

## 安装依赖
建议用 venv：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 运行方式
你需要能访问集群（`~/.kube/config` 或集群内 serviceaccount）。

### 实时监控（终端 UI）
实时刷新 `ApplicationManager` 状态与 Pod/容器关键进度，适合你边安装边看“卡在哪里”。

#### 只提供应用名（可在安装前启动）
`ApplicationManager` 是 **Cluster** 资源（`kubectl get applicationmanagers` 不带 `-n`）。脚本列的是全集群的 `ApplicationManager`，再按 **`spec.appName` / `spec.rawAppName`** 匹配（大小写不敏感兜底）。若你环境里有多个用户装同名应用，可加 **`--namespace <spec.appNamespace>`**，只保留「该用户命名空间」下的那条（对应 CR 里的 `spec.appNamespace`，不是 kubeconfig 的默认 ns）。

脚本会先尝试匹配「**本进程启动之后**」才发生的安装（锚点时间取 `creationTimestamp` 与 `opTime/statusTime/updateTime` 的**最大值**，并与启动时间做 **UTC 归一**比较）。若应用**已经处于 Running**、没有新的安装事件，默认会**自动挂到该应用最新的 `ApplicationManager`** 上并继续展示。

若你只想等**全新一次安装**（例如在已有 CR 的机器上重装），请加上：

```bash
python watch_install_live.py --app <app-name> --wait-new-install --refresh 1.0
```

Web 控制台等**非 TTY** 终端里 Rich 可能不刷新界面，可尝试：

```bash
export FORCE_TERMINAL_UI=1
python watch_install_live.py --app <app-name> --refresh 1.0 --until-running
```

占比默认用**横向条形图**（`--pie bars`，默认），且**合并为一张表**（阶段与 Pod 汇总同在 `Share` 里，比「Phase + Combined」两块更省行数）。需要饼图或双表时：

- `--pie compact`：同一行两个小号饼图  
- `--pie full`：两行大号饼图  
- `--pie off`：只显示百分比表，不占条形/饼图高度  
- `--share-dual`：条形模式下恢复「Phase share」与「Combined share」两张表（更占垂直空间）

**终端高度不够时**，Rich 的 `Live` 会裁掉底部内容；旧默认最后一行是红色的 `…`，容易误以为是图表坏了。本脚本默认 `--live-overflow crop`（不占用那一行省略号，多一行有效内容）。仍装不下时可：`--pie off`、把终端拉高、`--share-max-rows 6` 减小行数，或 `--live-overflow visible`（尽量显示全部；刷新时偶发残行，见 Rich 文档说明）。

#### 直接提供 appmgr（立即开始）
```bash
python watch_install_live.py \
  --appmgr <applicationmanager-name> \
  --refresh 1.0 \
  --until-running
```

> 终端 UI 里会额外显示：
> - `ApplicationManager` 状态切换的阶段时间线（enter 时间与已耗时）
> - 阶段 / 合并占比：默认**单表条形图**；可用 `--pie compact` / `--pie full` / `--pie off` / `--share-dual` 调整；`--live-overflow` 控制超出终端高度时的裁切方式
> - 每个 Pod 的 `Sched/Pull/Start->Ready` 耗时（若常见 workload label 对不上 `--app`，会自动按 Pod 名包含应用名、再不行则列出 `spec.appNamespace` 下全部 Pod，并有一行灰色说明）
> - 每个 Pod 的最新告警事件（例如 `FailedScheduling/ImagePullBackOff/BackOff/CrashLoopBackOff`）及持续时间
> - 每个容器的 `Pull(+)`、`Created/Started` 事件时间、`startedAt`、`waiting reason`、重启次数等

### 方式 A：按 ApplicationManager 名称采集（推荐）
`app-service` 会创建 `ApplicationManager` CR，脚本会以它为主线串起整条安装链路。

```bash
python speed/collect_install_timeline.py \
  --appmgr <applicationmanager-name> \
  --namespace <app-namespace> \
  --out speed/out/<name>.json
```

### 方式 B：按 app + owner 推断 appmgr 名称
如果你更像是“从点击安装”开始对齐，可以用 `app`/`owner` 来推断名字（实际命名规则可能因版本不同而变化，脚本会尝试常见规则并回退提示）。

```bash
python speed/collect_install_timeline.py \
  --app <app-name> \
  --owner <username> \
  --namespace <app-namespace> \
  --out speed/out/<app>.json
```

### 导出 CSV（便于画瀑布图）

```bash
python speed/collect_install_timeline.py ... --csv speed/out/<name>.csv
```

### 生成饼图（每个阶段耗时占比）
从采集到的 `ApplicationManager` 阶段耗时生成饼图 PNG。

```bash
python speed/collect_install_timeline.py \
  --appmgr <applicationmanager-name> \
  --namespace <app-namespace> \
  --out speed/out/<name>.json \
  --pie speed/out/<name>_phases.png
```

## 输出字段说明（简版）
- **`appmgr.phases[]`**：每个状态的 `enter_time` 与 `duration_seconds`
- **`pods[]`**：每个 Pod 的调度/拉镜像/启动/就绪时间点与耗时
- **`containers[]`**：容器 `startedAt` 与所属 Pod
- **`bottlenecks[]`**：脚本基于简单规则给出的“可能瓶颈点”

## 如何用这些数据做优化（建议清单）
- **Downloading 很慢**：镜像拉取/解包慢
  - 近端 registry / mirror、预拉取、镜像瘦身、多架构镜像策略
  - 并行拉取与复用层（镜像 tag/层稳定）
- **PodScheduled 慢**：调度/资源不足
  - 资源 requests/limits 过大、节点选择/亲和性、GPU/特殊资源约束
  - 优化 Chart 默认资源、延迟启动非关键组件、拆分大 Pod
- **Pulling->Pulled 慢**：镜像大或网络慢
  - 镜像分层优化、减少大层、使用更快的存储/网络、启用节点镜像缓存
- **Started->Ready 慢**：应用启动慢 / 探针过严 / 初始化逻辑耗时
  - readinessProbe 优化、启动参数与缓存、避免阻塞 init、减少冷启动依赖
- **Installing 阶段很长**：Helm/CRD/Job 执行慢
  - 检查 chart hooks、CRD 安装、依赖中间件初始化、减少串行步骤

## 独立仓库：推送到 GitHub（公开）

在**本机** `speed/` 目录已初始化 Git 后，在 GitHub 新建 **空** 的 public 仓库（不要勾选添加 README），然后：

```bash
cd speed
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git branch -M main
git push -u origin main
```

## 远端宿主机上使用（克隆后）

假设仓库克隆到 `~/olares-install-speed`，且该机器已能访问 Kubernetes API（`~/.kube/config` 已配置好）：

```bash
git clone https://github.com/<你的用户名>/<仓库名>.git ~/olares-install-speed
cd ~/olares-install-speed

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 实时监控（安装前可启动，仅应用名）
python watch_install_live.py --app <app-name> --refresh 1.0 --until-running

# 事后采集 JSON + 饼图 PNG
python collect_install_timeline.py \
  --appmgr <applicationmanager-name> \
  --namespace <app-namespace> \
  --out ./out/timeline.json \
  --pie ./out/phases.png \
  --csv ./out/timeline.csv
```

### Debian / Ubuntu：`ensurepip is not available` / 无法创建 venv

系统自带的 Python 常**不带** `venv` 模块，需要先装（版本号与 `python3 --version` 一致，例如 3.12 则用 `python3.12-venv`）：

```bash
sudo apt update
sudo apt install -y python3-venv
# 若仍报错，按提示安装对应版本，例如：
# sudo apt install -y python3.12-venv
```

然后删掉失败留下的目录并重建：

```bash
cd ~/olares-install-speed
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**不打算用 venv 时**（仅当前用户 / root 全局依赖），可：

```bash
sudo apt update
sudo apt install -y python3-pip
pip3 install --user -r requirements.txt
# 运行：python3 watch_install_live.py ...
```

