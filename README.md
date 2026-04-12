## 目标
监控一个应用从“点击安装”到“最终可访问 / `Running`”之间的**每一个阶段**与**每一个容器关键步骤**的耗时，并把数据导出为 `JSON/CSV`，用于后续针对性优化安装速度。

本目录脚本：
- **`collect_install_timeline.py`**：安装结束后采集，导出 JSON / CSV / 可选饼图 PNG。
- **`watch_install_live.py`**：安装过程中实时监控（Rich 终端 UI）。

下文与当前仓库内脚本行为一致；更细的参数以 **`--help`** 为准。

---

## 安装依赖
建议用 venv（在包含 `requirements.txt` 的目录执行）：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 使用指南

### 前置条件
- 能访问 Kubernetes API：`~/.kube/config` 已配置，或集群内通过 ServiceAccount 访问。
- 在仓库中：若脚本在 **`speed/`** 子目录，下述命令使用前缀 `speed/`；若仓库根目录直接放脚本（无 `speed/`），则去掉该前缀。

### 一、实时监控 `watch_install_live.py`（推荐边装边看）

#### 1）只用应用名（可在点击安装**之前**启动）
`ApplicationManager` 是 **Cluster** 资源（`kubectl get applicationmanagers` 一般**不带** `-n`）。脚本按 **`spec.appName` / `spec.rawAppName`** 匹配（大小写不敏感）。多用户同名应用时，加 **`--namespace <spec.appNamespace>`**（对应 CR 里的 `appNamespace`，不是 kubeconfig 默认命名空间）。

- 默认会尝试挂到「本进程启动之后」的安装；若已是 `Running` 且无新安装，会挂到**最新一条** `ApplicationManager` 继续展示。
- **只等全新一次安装**（例如重装）时加 **`--wait-new-install`**。

```bash
python speed/watch_install_live.py --app <app-name> --wait-new-install --refresh 1.0
```

安装完成后可自动退出：

```bash
python speed/watch_install_live.py --app <app-name> --refresh 1.0 --until-running
```

#### 2）已知 `ApplicationManager` 名字（立刻跟一条 CR）

```bash
python speed/watch_install_live.py \
  --appmgr <applicationmanager-name> \
  --refresh 1.0 \
  --until-running
```

#### 3）Web / 非 TTY / Olares 嵌套终端
- 界面不刷新或空白：先设 **`export FORCE_TERMINAL_UI=1`** 再运行。
- 画面**上下被裁掉**：Rich `Live` 在嵌套 PTY 里常按错误高度裁剪，请改用**整页重绘**（不用 `Live`）：

```bash
export INSTALL_SPEED_NO_LIVE=1
python speed/watch_install_live.py --app <app-name> --wait-new-install --refresh 1.0
```

等价于命令行加 **`--no-live`**。若仍裁切，多半是宿主终端区域高度不够，可拉高窗口或减小输出（如 `--pie off`、`--share-max-rows 6`）。仍异常时可设 **`INSTALL_SPEED_TERM_ROWS`** / **`INSTALL_SPEED_TERM_COLS`**（整数）覆盖 Rich 检测到的行列数。

- 不想用备用屏幕、要保留主屏滚动历史：**`--no-alt-screen`**。
- 刷新闪烁时可试 **`--live-overflow crop`**（默认可为 `visible`，见 `--help`）。

#### 4）占比图与界面选项（默认即可，按需改）
- **`--pie bars`**（默认）：**三张条形图**，三套 **各自合计 100%**，不要横向相加——**阶段时间线**、**按 Pod**（每 Pod：`sched + pull + Started→Ready`）、**按容器**（`pod/container` 的 Pulling→Pulled；仅 Pod 级事件时可能出现 `pod (pod pull)`）。
- **`--pie compact` / `full` / `off`**：两饼 / 三饼 / 仅数字表；细节见 `--help`。
- **`--share-bar-width`**、**`--share-max-rows`**：控制条形宽度与行数。

#### 5）界面上你会看到什么
- `ApplicationManager` 状态、阶段时间线（进入时间与已耗时）。
- **Pods** 表：调度 / 拉镜像 / 启动→就绪、告警事件等；label 对不上 `--app` 时会按 **Pod 名包含应用名** 或 **命名空间下全部 Pod** 回退（有一行灰色说明）。
- **Containers** 表：每容器 `Pull(+)`、事件时间、`startedAt`、waiting、镜像等。

完整参数：

```bash
python speed/watch_install_live.py --help
```

**环境变量小结**

| 变量 | 作用 |
|------|------|
| `FORCE_TERMINAL_UI=1` | 非 TTY 时仍按终端渲染 Rich |
| `INSTALL_SPEED_NO_LIVE=1` | 同 `--no-live`，整页重绘，避免嵌套终端纵向裁剪 |
| `INSTALL_SPEED_TERM_ROWS` / `INSTALL_SPEED_TERM_COLS` | 整数，覆盖检测到的终端行列 |

---

### 二、事后采集 `collect_install_timeline.py`

#### 方式 A：按 `ApplicationManager` 名称（推荐）

```bash
python speed/collect_install_timeline.py \
  --appmgr <applicationmanager-name> \
  --namespace <app-namespace> \
  --out speed/out/<name>.json
```

#### 方式 B：按 `app` + `owner` 推断 appmgr（命名因环境而异，以脚本提示为准）

```bash
python speed/collect_install_timeline.py \
  --app <app-name> \
  --owner <username> \
  --namespace <app-namespace> \
  --out speed/out/<app>.json
```

#### 导出 CSV（便于瀑布图）

```bash
python speed/collect_install_timeline.py ... --csv speed/out/<name>.csv
```

#### 阶段耗时饼图 PNG

```bash
python speed/collect_install_timeline.py \
  --appmgr <applicationmanager-name> \
  --namespace <app-namespace> \
  --out speed/out/<name>.json \
  --pie speed/out/<name>_phases.png
```

```bash
python speed/collect_install_timeline.py --help
```

---

## 你将得到什么（数据含义）
- **阶段级（AppManager 状态机）**：`Pending -> Downloading -> Installing -> Initializing -> Running`
- **Pod 级**：`PodScheduled`、事件 `Pulling`→`Pulled`、`Created` / `Started`、`Ready=True`
- **容器级**：`containerStatuses[*].state.running.startedAt`、`ready` 等

> Kubernetes 事件与字段因版本/运行时而异，脚本尽力兼容；缺失处为空或 `-`。

## 输出字段说明（简版）
- **`appmgr.phases[]`**：各状态 `enter_time`、`duration_seconds`
- **`pods[]`**：各 Pod 调度/拉镜像/启动/就绪时间与耗时
- **`containers[]`**：容器 `startedAt` 与所属 Pod
- **`bottlenecks[]`**：脚本按简单规则标注的可能瓶颈

## 如何用这些数据做优化（建议清单）
- **Downloading 很慢**：镜像拉取/解包 — registry 镜像、预拉取、瘦身、多架构策略、层复用
- **PodScheduled 慢**：调度/资源 — requests/limits、亲和性、节点资源
- **Pulling→Pulled 慢**：镜像体积与网络 — 分层、缓存、带宽
- **Started→Ready 慢**：应用启动与探针 — readiness、启动参数、init 阻塞
- **Installing 很长**：Helm/Job/CRD — hooks、串行步骤、依赖中间件

---


## 远端宿主机上使用（克隆后）

假设克隆到 `~/olares-install-speed`，且已能访问集群 API：

```bash
git clone https://github.com/<你的用户名>/<仓库名>.git ~/olares-install-speed
cd ~/olares-install-speed

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 若仓库根目录即本目录（无 speed/ 前缀）：
python watch_install_live.py --app <app-name> --refresh 1.0 --until-running

python collect_install_timeline.py \
  --appmgr <applicationmanager-name> \
  --namespace <app-namespace> \
  --out ./out/timeline.json \
  --pie ./out/phases.png \
  --csv ./out/timeline.csv
```

### Debian / Ubuntu：`ensurepip is not available` / 无法创建 venv

```bash
sudo apt update
sudo apt install -y python3-venv
# 若提示需指定版本：sudo apt install -y python3.12-venv
```

```bash
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

不用 venv 时：

```bash
sudo apt install -y python3-pip
pip3 install --user -r requirements.txt
# python3 watch_install_live.py ...
```
