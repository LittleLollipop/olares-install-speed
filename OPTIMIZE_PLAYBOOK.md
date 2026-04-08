## 如何用采集数据做针对性优化

你会拿到两类关键耗时：
- **阶段耗时**：来自 `ApplicationManager` 状态机（`Pending/Downloading/Installing/Initializing/Running`）
- **Pod/容器耗时**：来自 Pod condition、event（Pulling/Pulled/Started）以及 `containerStatuses.startedAt`

下面给出一个“从现象到动作”的排查与优化清单。

### 1) `Pending` 很久
通常意味着“任务还没进入下载/安装执行”。

- **排查点**：
  - 是否 `ApplicationManagerController` 处理队列拥塞（并发被限制为 1）
  - 是否频繁重试/卡在 canceling/failed 的回环
- **优化动作**：
  - 提升 controller 并发（谨慎：可能引发资源争抢）
  - 缩短无效重试，减少 reconcile 全量扫描带来的抖动

### 2) `Downloading` 很久（镜像下载/预拉取）
对应脚本里的 `bottlenecks.phase_slow(Downloading)` 或 Pod 的 `pull_duration_seconds` 普遍偏大。

- **排查点**：
  - 节点到 registry 的 RTT/带宽/丢包
  - 镜像层过大、层变化频繁导致缓存命中差
  - 同一镜像在多个 Pod 重复拉取（缺少预拉取/复用）
- **优化动作**：
  - **镜像瘦身**：减少大层、清理构建缓存、分离 runtime 与 build
  - **近端加速**：节点侧 registry mirror / 局域网 registry
  - **预拉取**：在安装前先把 refs 下发到节点（daemonset 预拉）
  - **稳定 tag/层**：提高 layer cache 命中率（减少“每次都变”的层）

### 3) `Installing` 很久（Helm / hook / Job / CRD）
对应 `bottlenecks.phase_slow(Installing)`，但 Pod 层面未必慢。

- **排查点**：
  - Helm chart hooks 是否有串行 Job（尤其 init DB、migrate、download assets）
  - CRD/资源 apply 频繁失败重试
  - 单个 release 资源量过大，API server 限流
- **优化动作**：
  - 拆分 chart：把非关键组件后置（延迟安装/按需安装）
  - 让耗时初始化异步化：由 app 自己启动后再后台完成（配合 UI/状态提示）
  - 降低一次性 apply 资源数量，减少 webhook/validating 负担

### 4) Pod 调度慢（`schedule_duration_seconds` 高）
常见于资源 requests 过大、亲和/反亲和约束、GPU/特殊资源不足。

- **排查点**：
  - 是否出现 `Unschedulable` event
  - requests/limits 与节点余量、拓扑约束冲突
- **优化动作**：
  - 调整默认 requests（尤其 memory/cpu/GPU）
  - 把“可选能力”做成 profile（默认轻量，用户按需开启）
  - 减少严格亲和性，避免绑死到稀缺节点

### 5) `Started -> Ready` 慢（`start_to_ready_seconds` 高）
说明容器启动了，但 readiness 变绿很慢。

- **排查点**：
  - readinessProbe 是否过严（initialDelaySeconds/timeout/failureThreshold）
  - 是否在启动路径做了重 IO/网络依赖（拉模型、全量索引、迁移）
- **优化动作**：
  - 将“可用”与“完全就绪”拆开：先提供最小可用能力，再后台 warm-up
  - readinessProbe 改为更贴近“可访问”的信号（HTTP 轻量 endpoint）
  - 使用缓存/预热（依赖包、模型、索引）与增量初始化

### 6) 单个容器维度的异常
脚本会导出 `containers[].started_at/restart_count`，你可以按容器聚合。

- **排查点**：
  - 反复重启：CrashLoopBackOff（查看 termination reason / logs）
  - 单容器 pull 特别慢：镜像过大或 registry 慢
- **优化动作**：
  - 拆分镜像（基础镜像 + 业务层），减少变更层体积
  - 对大依赖做外置卷/缓存层

## 推荐的下一步（更精细的“点击安装”对齐）
如果你希望把“点击安装”的前端时间点与后端全链路完全对齐，建议补一条：
- 在 `app-service` 的 install handler 中生成一个 `trace_id`（例如 UUID），写入 `ApplicationManager` annotation（例如 `bytetrade.io/install-trace-id`）
- 前端/调用方把同一个 `trace_id` 放到请求 header 中并写入日志

这样你能把“点击 -> API 受理 -> Pending enter -> Downloading enter -> ... -> Running”的所有时间串成一条严格一致的链路。

