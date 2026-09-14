# Stage1旧环境加固验收操作指南

> 核对日期：2026-09-14。适用于既有LiteLLM迁移路径，对应[主执行手册的Stage1](customer-migration-guide-zh.md#阶段1旧环境最小加固)，不是greenfield或新网关切流验收。
>
> 默认三项：`legacy_health`、`alerts_received`、`rollback_snapshot`。只有客户JSON配置顶层`legacyAccess`时才增加`legacy_source_access`。本文没有新增workflow，不自动生成passed，不代表客户云上已经完成这些检查。

阅读顺序：准备 → 旧业务健康 → 监控与通知 → 回退材料 → 可选来源验证 → draft/confirm。**尚未执行S1-08时，先做R-1保存变更前快照；已经执行时，先看R-3，不能把当前配置当作变更前状态。** 已有同范围、同版本、仍有效的真实证据可复核引用，不必为填表重做部署。

## 1. 准备与执行位置

### 1.1 哪些动作已自动完成，哪些要你做

| 检查ID | workflow实际覆盖 | 你还需要完成 |
| --- | --- | --- |
| legacy_health | S1-08只补缺失的startup/readiness/liveness探针、将PG Deployment设为Recreate，并等待rollout | 核对Pod/Deployment/PVC现状、旧客户端实际调用、必要管理路径和批准观察窗口内的业务连续性 |
| alerts_received | S1-04接入Container Insights；S1-06部署5条日志告警、2条AKS管理操作告警和邮件Action Group | 验证真实数据、规则范围/阈值/通知绑定，以及收件人实际收到测试通知；明确是否还要求真实规则触发演练 |
| rollback_snapshot | S1-07/08运行中读取原Deployment；报告只保留策略/探针摘要 | 独立保管可核验的变更前Deployment配置、原镜像/依赖引用及获批恢复步骤，不把报告摘要当完整快照 |
| legacy_source_access（可选） | S1-10按批准CIDR修改既定入口的来源限制 | 从批准与未批准的实际来源分别验证，并确认无其他入口绕过 |

本次若仅演练、明确接受旧入口公网可达且未配置`legacyAccess`，可以跳过S1-09/10及本文第5节，并在受控记录写清适用范围和风险接受；这不是客户生产环境的默认豁免。不得为了减少检查而删除已经启用的配置块，或把来源验证失败改为“不适用”。旧入口原有认证和TLS仍需保持。

S1-04之后先做A-1/A-2，再执行依赖日志表的S1-05/06；S1-08之后做H项。S1-11 draft可以提前生成，但S1-13 confirm必须等适用检查全部完成。没有新GitHub Variable/Secret要求，继续使用同一Environment和受保护默认分支。

| 操作 | 执行位置/权限 | 影响 |
| --- | --- | --- |
| Azure资源与日志核对 | 获准管理设备上的Portal/CLI；旧AKS/工作区/规则读取及日志查询权限 | 只读，不需要连接Kubernetes API；日志查询仍受客户权限和数据政策约束 |
| Kubernetes状态与配置核对 | 有旧AKS API网络路径和授权的管理终端，使用独立非admin kubeconfig | 状态查询只读；R-1会把敏感配置保存到本机受控文件 |
| 客户端/管理页面验证 | 客户实际使用的设备、原入口、获准测试账号/Key和模型 | 推理会产生少量费用和请求记录；写入操作仅使用批准的测试对象 |
| 邮件通道测试 | 获准操作者在Action Group中执行Test，收件人核对邮箱 | 会发送真实测试邮件，但不故意制造业务故障 |

SSH进入Runner不等于已Azure登录，个人管理员的成功也不能替代Actions运行身份授权。遇到条件访问/受管设备要求，按[Stage0准备说明](customer-stage0-acceptance-checklist-zh.md#11-先区分三种操作)处理，不复制Token、关闭条件访问、使用`--admin`或开放公网绕过。

### 1.2 获取本次要用的值

在获准管理机的仓库根目录使用Bash；以下是本机变量，不会自动从GitHub同步。准备Azure CLI、kubectl、kubelogin、jq和sha256sum，先确认工具已安装。不要把秘密填进命令、聊天或公共Git。

```bash
set +x
set -o pipefail
umask 077
SUBSCRIPTION_ID="REPLACE_SUBSCRIPTION_ID"
LEGACY_RG="REPLACE_LEGACY_RESOURCE_GROUP"
LEGACY_AKS="REPLACE_LEGACY_AKS_NAME"
LEGACY_NAMESPACE="REPLACE_LEGACY_NAMESPACE"
POSTGRES_PVC="REPLACE_LEGACY_POSTGRES_PVC"
LOG_WORKSPACE="REPLACE_LEGACY_LOG_ANALYTICS_WORKSPACE_NAME"
ENVIRONMENT_NAME="test"
CASE_ID="stage1-review-20260914-01"
mkdir -p "temp/$CASE_ID"
```

| 值 | 客户侧获取方法 | 怎样核对 |
| --- | --- | --- |
| SUBSCRIPTION_ID | 客户JSON的azure.subscriptionId；Portal订阅Overview | 与当前登录订阅及旧资源所属订阅一致；tenantId另与azure.tenantId核对 |
| LEGACY_RG、LEGACY_AKS | 客户JSON的legacy.resourceGroup、legacy.aksClusterName；旧AKS Overview | 必须是旧业务集群，不是目标集群或Runner资源组 |
| LEGACY_NAMESPACE、POSTGRES_PVC | legacy.namespace、legacy.postgresPvc；旧AKS命名空间及Storage/PVC列表 | 与实际工作负载/挂载对应；当前告警模板要求litellm、pg-data |
| LOG_WORKSPACE | parameters.monitoring.logAnalyticsWorkspaceName；旧AKS监控目的地 | 是旧RG工作区，不是parameters.backup/platform的新工作区 |
| AKS_RESOURCE_ID | H-1命令的id，或旧AKS Overview → JSON View的id | 完整ARM路径，末尾为managedClusters/旧集群名；A-2查询使用它 |
| 工作区ARM ID / Workspace ID | A-1查询的id / workspaceId，或工作区Overview/JSON View | 前者是完整资源路径，后者是customerId GUID，不能混用；同名重建后GUID可能不同 |
| 通知邮箱 | 客户JSON的ownerEmail；收件人确认的实际邮箱 | 再与Action Group email receiver比较，不由邮箱推断RBAC身份 |
| HARDENING_PLAN_RUN_ID / HARDENING_RUN_ID | 成功S1-07 / S1-08运行URL中actions/runs/后的数字 | action=legacy-hardening、stage=1、环境/完整Git SHA匹配，不是job ID |
| DRAFT_RUN_ID | 成功Stage1 acceptance draft的运行URL | 不是Stage0 draft、monitoring deploy或hardening plan的ID |
| CASE_ID、快照位置、证据编号 | 本次获准记录目录/客户变更工单，由操作者创建并记录 | 使用新编号，不覆盖历史证据；Git忽略不等于加密或访问控制 |

## 2. legacy_health：验证旧业务未被破坏

### H-1 锁定旧集群和查询上下文

```bash
az account show --query '{tenantId:tenantId,subscriptionId:id}' --output json
az aks show --subscription "$SUBSCRIPTION_ID" --resource-group "$LEGACY_RG" --name "$LEGACY_AKS" \
  --query '{id:id,name:name,state:provisioningState,power:powerState.code}' --output json
```

确认租户/订阅、旧集群及运行状态后，按[Stage0的I-2](customer-stage0-acceptance-checklist-zh.md#i-2-取得独立的非admin-kubeconfig)取得本次独立kubeconfig。新终端需要重新设置变量；已有文件只能在确认其确实对应同一旧集群且凭据有效后复用。Snap kubelogin的AppArmor问题也见I-2，不放宽文件权限。

```bash
PRIVATE_KUBECONFIG="$PWD/temp/$CASE_ID/legacy-kubeconfig"
az aks get-credentials --subscription "$SUBSCRIPTION_ID" --resource-group "$LEGACY_RG" \
  --name "$LEGACY_AKS" --file "$PRIVATE_KUBECONFIG" &&
chmod 600 "$PRIVATE_KUBECONFIG" &&
kubelogin convert-kubeconfig --kubeconfig "$PRIVATE_KUBECONFIG" -l azurecli
```

任一步失败就停止，不继续使用默认context。下面每条kubectl命令都显式选择该文件及命名空间，不打印或上传凭据。

### H-2 核对Deployment、Pod、探针和PVC

```bash
kubectl --kubeconfig "$PRIVATE_KUBECONFIG" -n "$LEGACY_NAMESPACE" get deployment postgres litellm-mi-proxy \
  -o 'custom-columns=NAME:.metadata.name,DESIRED:.spec.replicas,UPDATED:.status.updatedReplicas,READY:.status.readyReplicas,AVAILABLE:.status.availableReplicas'
kubectl --kubeconfig "$PRIVATE_KUBECONFIG" -n "$LEGACY_NAMESPACE" rollout status deployment/postgres --timeout=90s
kubectl --kubeconfig "$PRIVATE_KUBECONFIG" -n "$LEGACY_NAMESPACE" rollout status deployment/litellm-mi-proxy --timeout=90s
kubectl --kubeconfig "$PRIVATE_KUBECONFIG" -n "$LEGACY_NAMESPACE" get pods -l 'app in (postgres,litellm-mi-proxy)' \
  -o 'custom-columns=NAME:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,RESTARTS:.status.containerStatuses[*].restartCount'
kubectl --kubeconfig "$PRIVATE_KUBECONFIG" -n "$LEGACY_NAMESPACE" get pvc "$POSTGRES_PVC" \
  -o 'custom-columns=NAME:.metadata.name,PHASE:.status.phase,VOLUME:.spec.volumeName,CAPACITY:.status.capacity.storage'
```

两个Deployment应达到各自批准的副本数，rollout完成、服务Pod Ready，PG PVC为Bound。Stage1不强制把旧后台改成双副本；不要套用新目标环境的副本要求。Running不等于Ready，Bound不等于数据库已可读写；后者还要由实际业务调用验证。

下面只投影非秘密字段，不输出env、Secret或完整Pod配置：

```bash
kubectl --kubeconfig "$PRIVATE_KUBECONFIG" -n "$LEGACY_NAMESPACE" get deployment postgres litellm-mi-proxy -o json |
  jq '.items[] | {name: .metadata.name, strategy: .spec.strategy.type,
    pvc: [.spec.template.spec.volumes[]? | .persistentVolumeClaim.claimName // empty],
    containers: [.spec.template.spec.containers[] | select(.name == "postgres" or .name == "litellm") |
      {name, image, startup: has("startupProbe"), readiness: has("readinessProbe"), liveness: has("livenessProbe")} ]}'
```

PG应为Recreate，目标容器三类探针应存在，镜像与挂载仍与已审核的旧基线一致。与S1-07的runtime-review摘要比较探针设置；既有探针不会被自动改写，存在也不等于配置正确。任何空输出、jq报错或资源缺失均不能通过；A-2另查真实磁盘使用量。不能把S1-08成功解释为旧镜像已自动锁定digest、密钥已轮换或管理面已隔离。

### H-3 用实际客户端和管理路径回归

沿用Stage0记录的旧入口、原认证方式、获准测试Key/模型与实际客户端版本，不切到新域名，也不在此时强制加上Stage7的Entra双凭据合同。按[Stage0的P-1至P-3](customer-stage0-acceptance-checklist-zh.md#5-protocol_baseline旧入口真实客户端基线)的方法确认请求确实经过旧网关。

| 项目 | 你要做的操作 | 通过标准 |
| --- | --- | --- |
| 普通推理 | 用真实Codex/SDK发一条无敏感短请求 | 正常返回；用request ID或时间窗口关联旧网关请求元数据，不只看health/models成功 |
| 连续对话 | 在同一会话中连续两轮使用无敏感标记 | 两轮均成功，上下文符合预期 |
| 流式、取消与再调用 | 所选客户端启用流式，看到增量后取消，再发短请求 | 无解析错误，取消后仍可正常使用 |
| 客户必需能力 | 复测实际依赖的工具调用、结构化输出、重连等 | 按Stage0同一矩阵通过；不要求所有客户端/协议，但不能把失败改成不适用 |
| 必要管理路径 | 用获准管理员登录旧UI，核对必要的用户/Team/Key/模型/预算读取；必要写入仅操作批准测试对象 | 管理动作符合原权限，业务配置无非预期改变；不在记录里显示完整Key或正文 |
| 原认证边界 | 按批准测试验证缺失/无效凭据拒绝；核对已批准废弃凭据的处置记录 | 没有因加固关闭认证或TLS；不为测试撤销真实业务Key，不把Stage1当作自动轮换工具 |

### H-4 记录观察窗口

由业务Owner明确观察起止时间，覆盖加固后的实际调用和后台运行，而不是只看一次Pod截图。窗口开始/结束各核对一次H-2，检查重启数是否持续增加、是否存在CrashLoop/Pending、错误和延迟是否异常；单次滚动重启或旧Pod残留要结合时间解释。必要时由授权人员限定时间查看错误日志，不公开原始日志正文。

**legacy_health通过标准：** H-2状态/配置符合预期，H-3的实际适用业务与管理路径通过，H-4没有未处理的加固回归。Stage0的变更前测试不能单独证明S1-08之后仍健康；缺少变更后实测时继续标为待核验。

## 3. alerts_received：验证采集、规则和通知

### A-1 确认采集目的地

```bash
az aks show --subscription "$SUBSCRIPTION_ID" --resource-group "$LEGACY_RG" --name "$LEGACY_AKS" \
  --query '{enabled:addonProfiles.omsagent.enabled,workspace:addonProfiles.omsagent.config.logAnalyticsWorkspaceResourceID}' --output json
az monitor log-analytics workspace show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$LEGACY_RG" --workspace-name "$LOG_WORKSPACE" \
  --query '{id:id,workspaceId:customerId,state:provisioningState}' --output json
```

通过条件：enabled=true，AKS绑定的workspace完整ARM ID与工作区id一致，工作区Succeeded。Portal也可从旧AKS的Insights/监控设置查看目的地，再打开对应工作区Overview核对Workspace ID。资源存在、部署Succeeded都不能代替A-2的真实数据。

### A-2 在旧工作区查询三类真实数据

Portal打开A-1核对的**旧工作区 → Logs → KQL模式**，关闭示例查询窗口（若出现），时间范围覆盖最近30分钟。不是在Overview的Observability Agent输入查询，也不要误选新工作区。

每段独立执行，并替换所有REPLACE值：LegacyAksId取H-1的完整id，命名空间/PVC取1.2核对值，不把shell变量名直接粘贴进KQL。查询均只返回元数据和汇总，不展示LogMessage正文。

**容器日志：**

```kusto
let LegacyAksId = "REPLACE_LEGACY_AKS_RESOURCE_ID";
let LegacyNamespace = "REPLACE_LEGACY_NAMESPACE";
ContainerLogV2
| where TimeGenerated > ago(30m) and _ResourceId =~ LegacyAksId
| where PodNamespace == LegacyNamespace
| where PodName startswith "postgres-" or PodName startswith "litellm-mi-proxy-"
| extend Workload = iff(PodName startswith "postgres-", "postgres", "litellm-mi-proxy")
| summarize Records=count(), LastEvent=max(TimeGenerated), LastIngested=max(ingestion_time()) by Workload
```

两个工作负载应有与实际输出对应的近期日志。刚接入或容器没有新输出时可能暂时为空，可结合正常业务调用和下两类数据判断；不要制造PG故障来产生日志。空结果尚不足以签发日志采集通过，表不存在或查询错误也不能当作零错误。

**Pod清单：**

```kusto
let LegacyAksId = "REPLACE_LEGACY_AKS_RESOURCE_ID";
let LegacyNamespace = "REPLACE_LEGACY_NAMESPACE";
KubePodInventory
| where TimeGenerated > ago(30m) and _ResourceId =~ LegacyAksId
| where Namespace == LegacyNamespace
| where Name startswith "postgres-" or Name startswith "litellm-mi-proxy-"
| extend Workload = iff(Name startswith "postgres-", "postgres", "litellm-mi-proxy")
| summarize Records=count(), LastEvent=max(TimeGenerated) by Workload, PodStatus
```

应有两个工作负载的近期Running记录，事件时间进入实际告警最近10分钟窗口；历史Pending记录须结合最新状态解释。此表证明采集到了运行清单，不替代H-2的Ready检查或H-3的推理测试。

**PG卷容量：**

```kusto
let LegacyAksId = "REPLACE_LEGACY_AKS_RESOURCE_ID";
let LegacyNamespace = "REPLACE_LEGACY_NAMESPACE";
let PostgresPvc = "REPLACE_LEGACY_POSTGRES_PVC";
InsightsMetrics
| where TimeGenerated > ago(15m) and _ResourceId =~ LegacyAksId
| where Namespace == "container.azm.ms/pv" and Name == "pvUsedBytes"
| extend VolumeTags = parse_json(Tags)
| where tostring(VolumeTags.pvcNamespace) == LegacyNamespace and tostring(VolumeTags.pvcName) == PostgresPvc
| extend CapacityBytes = todouble(VolumeTags.pvCapacityBytes)
| extend UsedPercent = iff(CapacityBytes > 0, 100.0 * todouble(Val) / CapacityBytes, real(null))
| summarize Records=count(), ValidSamples=countif(CapacityBytes > 0), LastEvent=max(TimeGenerated), PeakUsedPercent=max(UsedPercent)
```

Records和ValidSamples应大于0，LastEvent近期（同时满足85%规则的10分钟窗口），PeakUsedPercent不是null，超过阈值时按真实风险处理。**没有磁盘样本不等于使用率0%**，不能因为没有触发磁盘告警就认定容量正常。

**Overview显示No data was ingested怎么办：** 先核对是否同一个Workspace ID、时间范围及上述表。摄取用量`Usage`按小时汇总，可能晚于实际日志；可在同工作区执行下面的只读对照。日志持续入库而Usage为空，符合统计尚未更新的情况，但不能仅凭这一点断定所有Portal显示问题都是缓存。不要因此重建工作区或反复开关监控。

```kusto
Usage
| where TimeGenerated > ago(24h)
| summarize Records=count(), LastUsageWindow=max(TimeGenerated), RecordedMB=sum(Quantity)
```

时间以查询显示的时区为准，记录时明确UTC或本地偏移。[Usage官方说明](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/usage)可供核对。若三类表持续无新数据，检查范围/权限、采集代理、DCR及网络，由Owner定位，不用“等概览刷新”解释真正的采集中断。

### A-3 核对实际规则，不只看部署回执

部署输出用于取得预期清单，不证明资源此刻仍启用：

```bash
az deployment group show --subscription "$SUBSCRIPTION_ID" --resource-group "$LEGACY_RG" \
  --name "llmgw-${ENVIRONMENT_NAME}-s1-monitoring" \
  --query '{state:properties.provisioningState,alerts:properties.outputs.minimumAlerting.value}' --output json
```

Portal → Azure Monitor → Alerts → Alert rules，按旧订阅/RG筛选；使用全局列表核对7条，单看工作区资源页可能看不到AKS范围的Activity Log规则。当前[监控模板](../infra/monitoring/main.bicep)预期如下，5条日志规则每5分钟评估一次：

| 规则资源名 | 严重级别 | 条件/窗口 |
| --- | --- | --- |
| alert-litellm-postgres-critical-log | Sev0 | 10分钟内PG日志出现模板列出的磁盘满/PANIC/恢复或关闭等关键字 |
| alert-litellm-error-burst | Sev1 | 10分钟内匹配指定数据库/认证/未处理异常模式的日志超过4条，不是统计全部HTTP错误 |
| alert-litellm-workload-unavailable | Sev0 | 最近10分钟缺少预期工作负载的Running清单；不等于真实Readiness或API可用性探测 |
| alert-litellm-postgres-volume-70-percent | Sev2 | 最近15分钟PG卷最大使用率大于70% |
| alert-litellm-postgres-volume-85-percent | Sev0 | 最近10分钟PG卷最大使用率大于85% |
| alert-litellm-aks-failed-administrative-operation | Activity Log | 旧AKS Administrative事件状态Failed，不依赖上述日志表 |
| alert-litellm-aks-delete | Activity Log | 旧AKS的managedClusters/delete管理事件；绝不通过删除集群测试 |

逐条打开确认Enabled、查询/阈值/窗口、Scope及Actions。日志规则Scope应为旧工作区，Activity Log规则Scope为旧AKS；全部绑定`ag-litellm-stage1-owner`。检查有无告警处理规则（Alert processing rules）在维护时间抑制通知，及查询/评估错误；处理实际问题，不为了验收直接取消客户既有抑制策略。

当前模板固定namespace=litellm、PVC=pg-data及两种Pod前缀，且日志规则没有额外按AKS Resource ID过滤。同工作区有其他同名工作负载的集群时会有混淆风险，需另行审核、调整并测试实际告警查询；A-2中的集群过滤不会自动修改告警。客户资源名不匹配时也先调整模板/规则，不重命名生产资源掩盖差异。

### A-4 发送获准测试邮件并核对收件

1. Portal → Azure Monitor → Alerts → Action groups → 打开`ag-litellm-stage1-owner`。确认属于旧RG、Enabled，Email接收器地址与ownerEmail及收件人确认值一致；当前模板启用Common alert schema。
2. 告知收件人并取得本次通知测试许可；点击Test，选择门户提供的日志告警样本类型（例如Log Alert V2）及现有Email接收器，检查摘要后发送。不新增真实收件人，不修改业务规则阈值。
3. 记录测试开始时间、Portal显示结果/测试标识；请收件人检查收件箱、垃圾邮件或企业隔离区，核对确实是本次Action Group测试。
4. 记录收件时间、核验人及受控证据编号。Portal发送成功不等于邮件实际送达，规则Enabled也不等于收件；未收到时检查地址、接收器状态、邮件安全策略和测试错误，不能先填passed。

**alerts_received通过标准：** A-1/A-2证明采集和必要数据可用，A-3证明正确规则及通知绑定，A-4有实际收件确认。若客户本次批准的是最小通知验收，记录为“采集/配置核验 + Action Group测试收件”，明确未覆盖真实规则触发；**Action Group测试不等于真实告警条件已经触发**，也不要求Total fired alerts因此增加。

客户验收若要求“条件满足 → 规则评估 → 告警实例 → 通知”端到端演练，A-4不够；须另行批准隔离环境或独立测试规则的无业务影响方案、费用及清理范围，关联真实告警实例和邮件后才确认。当前没有自动完成这套演练的workflow按钮；不停止PG、不填满磁盘、不制造认证风暴、不删除AKS来触发生产规则。

## 4. rollback_snapshot：确认能恢复本次配置变更

### R-1 在变更前独立保存Deployment配置

**这一步应在S1-08之前完成。** 已执行S1-08且未保存时跳到R-3，不运行此命令后声称取得了旧状态。这里的snapshot是Deployment配置快照，不是Azure磁盘快照，也不是数据库备份；PG数据恢复仍依赖Stage0的备份和原密钥材料。

在H-1已核对上下文的受控管理终端执行。完整Deployment可能含env明文或敏感annotation，所以只重定向到私有文件，不打印、不上传公共Git或Actions附件。目录首次创建失败、导出失败或JSON损坏都不算完成；已有目录不覆盖，先核对其来源。

```bash
SNAPSHOT_DIR="$PWD/temp/$CASE_ID/before-hardening"
mkdir -m 700 "$SNAPSHOT_DIR" &&
kubectl --kubeconfig "$PRIVATE_KUBECONFIG" -n "$LEGACY_NAMESPACE" get deployment postgres litellm-mi-proxy -o json \
  > "$SNAPSHOT_DIR/deployments-before.json" &&
jq -e '.kind == "List" and (.items | length == 2)' "$SNAPSHOT_DIR/deployments-before.json" > /dev/null &&
pushd "$SNAPSHOT_DIR" > /dev/null &&
sha256sum deployments-before.json > deployments-before.sha256 &&
popd > /dev/null
```

在客户受控系统记录快照实际时间、旧集群/namespace、两个Deployment UID/resourceVersion、S1-07计划ID、Git SHA、操作者及保管位置。须确认S1-07至S1-08之间没有未经复核的配置变化；有变化应重新plan并保存对应变更前状态。

### R-2 验证文件完整、范围正确且保管人能取回

从获准保管位置取回副本，SNAPSHOT_DIR改为该副本所在目录，而不是盲目沿用新导出目录。以下只打印校验结果与非秘密元数据：

```bash
pushd "$SNAPSHOT_DIR" > /dev/null &&
sha256sum --check deployments-before.sha256 &&
popd > /dev/null
jq '{kind, deployments: [.items[] | {name: .metadata.name, namespace: .metadata.namespace,
  uid: .metadata.uid, resourceVersion: .metadata.resourceVersion, strategy: .spec.strategy.type,
  images: [.spec.template.spec.containers[] | {name, image}],
  pvc: [.spec.template.spec.volumes[]? | .persistentVolumeClaim.claimName // empty]}]}' \
  "$SNAPSHOT_DIR/deployments-before.json"
```

核对哈希匹配、恰好是两个旧Deployment、命名空间/UID与记录对应，且原spec完整。哈希只证明文件未变，**不能证明它产生于加固前**，时间和来源必须另有可核对记录。image是tag时还需关联Stage0实际digest和可拉取的原镜像，不能凭tag假设内容永远不变。

Deployment中的Secret/ConfigMap引用不是这些对象内容的备份。核对原引用对象及获准版本、Master/Salt保管材料、PG PVC和Stage0备份仍可用；只记录引用，不把Secret导出进普通证据文件。将快照及校验清单移交获准加密保管位置，并让负责回退的人确认有权取回；只放在7天后过期的workflow artifact或单一临时目录不够。

### R-3 已执行但没有变更前快照，怎么办

[runtime实现](../scripts/migration_runtime.py)会临时保存`before-postgres.stdout.txt`和`before-litellm-mi-proxy.stdout.txt`，但[workflow](../.github/workflows/customer-runtime.yml)加密附件清单**不包含这两个完整快照**，结束时还会清理运行目录。`runtime-review.json`只有变更后策略/探针摘要，`runtime-summary.json`的applied也不是回退备份，不能从它们承诺恢复完整原spec。

按以下顺序处理，不为找快照重跑S1-08：

1. 查找S1-08之前独立保存的Deployment导出、获准配置备份或与当时实际部署一致的IaC/Git记录；Stage0若已保存同一原配置且期间无变化，可交叉核验后复用。
2. Kubernetes历史ReplicaSet可辅助还原旧Pod template和镜像，但不能单独恢复Deployment的strategy等完整spec。不要把`kubectl rollout undo`当作完整快照恢复；由平台Owner结合其他变更前证据审核重建的恢复配置。
3. 能从可信历史完整还原时，明确记录“依据哪些历史材料重建”、审核人、校验和及验证范围，不冒充当时已导出原件。只有当前配置或无法确定原状态时，`rollback_snapshot`保持待核验，不能用备注把缺项变成通过。

缺少原WORKFLOW_ARTIFACT_KEY时，不能用新密钥解旧报告；按[主手册附件审核说明](customer-migration-guide-zh.md#33-plan结果怎样审核)与获准保管人处理。不要输出GitHub Secret、复制Actions Token或轮换业务Master/Salt来取证。

### R-4 明确恢复步骤和批准边界

负责回退的Owner应能依据保存材料说明：

| 恢复事项 | 验收时要核对 |
| --- | --- |
| 触发与批准 | 什么健康/业务异常触发回退、谁批准、谁执行、维护窗口与停止条件 |
| 恢复对象 | 仅恢复经审核的旧Deployment配置；核对当前UID/状态和后续合法变更，不能直接覆盖整集群 |
| 恢复方法 | 由Owner把原spec整理成受审查的恢复清单/补丁，处理服务端元数据和并发变化；原始kubectl导出不能不经审核直接apply |
| 依赖与权限 | 旧镜像可拉取，原Secret/ConfigMap/PVC仍在，操作者具备限定更新权限；不清库、不删PVC、不轮换Salt |
| 恢复后验证 | 重做H-2/H-3并确认监控仍工作；配置回退不自动解决数据库数据损坏 |
| 演练范围 | 记录配置/材料审核或已批准隔离演练的真实结果；客户要求实际恢复演练时需另行获批，不能把文件存在写成“回退已实测” |

当前没有`legacy-hardening-restore` workflow动作。来源限制有独立的legacy-access-restore，但它不恢复Deployment探针/策略。**rollback_snapshot通过标准：** 可核验的变更前配置（或有充分历史依据且经审核的重建配置）完整、独立可取回，恢复依赖与步骤已核对，并满足本次批准的演练范围。

## 5. legacy_source_access：仅选择来源限制时验证

### S-1 确认是否适用和来源值

未配置legacyAccess时按第1节记录未选用，第6节只填三项。已配置时核对mode、allowedCidrs、accessImpactAccepted和S1-09/10记录；CIDR来自客户网络Owner批准的实际出口/NAT/代理路径，不是随便查到的本机私有IP，也不能只把Runner地址当所有用户来源。限制覆盖共享入口时，API和管理路径都受影响，白名单不是独立管理隔离。

### S-2 从两个实际来源分别测试

| 来源 | 操作与通过标准 |
| --- | --- |
| 批准来源 | 在该来源设备上用原域名/TLS和有效测试凭据调用真实旧API，必要管理路径也成功；记录实际出口、时间及结果 |
| 未批准来源 | 从客户批准的另一个测试网络请求同一域名和路径；预期在来源控制层拒绝，不是因为vkey无效才返回401 |

NGINX拒绝常见403，LoadBalancer来源过滤可能表现为连接超时，应结合实际入口规则和有界超时判断；任意超时不能单独证明白名单生效。不要泄露凭据来做拒绝测试，不关TLS、不借助未知公共代理；没有获准的第二来源时标记待核验。核查是否还有其他Ingress/公网Service/直连端口绕过限制，不能只测试一个URL就认定所有入口已封闭。

### S-3 核对恢复记录

需要恢复来源规则时，使用Customer private runtime operations，stage=1、action=legacy-access-restore，重新plan、审核后execute并引用该恢复plan ID；不得引用restrict的plan，也不要为验收无故切换来源规则。确认原规则恢复依据/检查点仍存在，结果记录在受控位置。

**legacy_source_access通过标准：** 适用入口的批准来源成功、未批准来源因来源控制拒绝，无未处理绕过，恢复步骤明确；限制后旧业务仍符合H项。

## 6. 记录结果并填写draft/confirm

### 6.1 私有验收记录

先在客户受控位置填写，默认都是待核验。不能把本文示例、他人环境的截图或之前助手的配置查询结果当作你全部检查已完成。

| 检查ID | 方法/证据引用 | 时间/版本/范围 | 实际结果 | 核验人/缺项 |
| --- | --- | --- | --- | --- |
| legacy_health | H-2状态、H-3客户端/管理回归、H-4观察记录 | 待填写 | 待核验 | 待填写 |
| alerts_received | A-2查询、A-3规则核验、A-4测试标识与实际收件记录；需端到端时补实例 | 待填写 | 待核验 | 待填写 |
| rollback_snapshot | R-1/R-3来源、R-2完整性/保管、R-4步骤和演练范围 | 待填写 | 待核验 | 待填写 |
| legacy_source_access（仅配置时） | S-2两种来源实测及S-3恢复依据 | 待填写 | 待核验或明确未选用 | 待填写 |

### 6.2 生成有效Stage1 draft

确认Stage0账本有效、同完整Git SHA及相容配置，governance已批准并同步Environment Secret；沿用原artifact key，不为读报告生成新key。代码、相关配置或审批策略改变时先复核受影响证据，不只重跑draft假装前序仍有效。

打开Actions → Customer stage acceptance → Run workflow，选择main、environment=test、stage=1、operation=draft；reviewed_run_id、checked_items、evidence_notes、confirm_environment全部留空。待成功后检查Summary中的实际check IDs，记录URL里的DRAFT_RUN_ID。draft仅生成pending清单，不部署、不进行H/A/R/S实测。

### 6.3 按页面标签填写confirm

以下沿用已批准single-operator路径；双人策略继续按主手册record流程，不因本指南更改治理。draft须7天内、同环境/阶段/完整Git SHA和适用配置，证据须真实有效；以实际draft的检查集为准。

| 页面字段/说明 | confirm时填写 |
| --- | --- |
| Use workflow from / environment / stage | main / test / 1，与draft一致 |
| operation | confirm |
| For confirm, successful draft run ID for this stage and revision | DRAFT_RUN_ID，即本Stage成功draft运行ID，不是Stage0、plan或deploy/execute ID |
| For confirm, every check ID personally verified; comma separated as shown in the draft Summary | checked_items：全部适用项实际通过后，填下面对应字符串 |
| For confirm, actual observations and evidence references; no secrets or prompt content | evidence_notes：普通文本，12至4000字符，逐项真实结果与允许公开的证据编号，不是JSON |
| For confirm, type the selected environment again | confirm_environment：test |

**未配置legacyAccess，三项全通过时：**

```text
legacy_health,alerts_received,rollback_snapshot
```

**已配置legacyAccess，四项全通过时：**

```text
legacy_health,alerts_received,rollback_snapshot,legacy_source_access
```

evidence_notes从6.1摘取：加固后客户端/管理回归和观察记录；三类数据/规则核对及邮件实际接收证据，注明Action Group测试或端到端范围；变更前配置来源/完整性/恢复步骤记录；适用时加两种来源实测。只引用允许公开的编号，不填客户内部URL、完整资源清单、秘密或日志正文。未选来源限制的演练可记录获批范围，但不填写legacy_source_access通过。

**当前confirm不支持部分通过。** 未收到邮件、没有可靠回退配置、旧业务回归失败或适用来源检查未完成时，都不能提交完整confirm，也不能仅填已完成的ID绕过。本文不提供可直接粘贴的“全部通过”说明。

成功后产生`acceptance-record-test-1-<run ID>`加密账本，后续workflow自动读取，不需要更新MIGRATION_EVIDENCE_JSON Secret。它是操作者验收记录，`independentlyVerified=false`，不是自动事实认证或切流批准；之后才按主手册进入Stage2冻结决策。

## 7. 常见阻塞与收尾

| 情况 | 下一步 |
| --- | --- |
| Overview无摄取统计但Logs有近期数据 | 按A-2对比实际表与Usage、范围/时区，不重建日志资源 |
| 规则存在但邮件没收到 | 完成A-4真实测试并排查邮件链路，不能只凭Enabled确认alerts_received |
| PG卷查询无样本或比例null | 核对采集、PVC标签/容量字段与规则窗口，不能当0%通过 |
| S1-08成功但没有原快照 | 按R-3找可信变更前材料；当前导出/摘要/单独ReplicaSet都不自动满足完整回退要求 |
| 只做了Stage0客户端测试 | 补S1-08之后的实际回归，不能用变更前健康证明变更后健康 |
| 未配legacyAccess却执行restrict失败 | 未选此控制时跳过S1-09/10；已要求来源限制时先准备获批配置，不能猜CIDR |
| draft/confirm报配置或revision不匹配 | 核对同main完整SHA、环境、governance及阶段相关配置；重新审核受影响证据，不手改哈希 |

按客户保管策略归档快照和必要记录后，再处理本次临时kubeconfig/解密文件；先确认不是唯一副本，不提供自动删除命令。不要删云端备份、日志库、PG/PVC或原密钥来“收尾”。