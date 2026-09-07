# LiteLLM 阶段 1 最小告警集

> 客户入口：[迁移指南](../../docs/customer-migration-guide-zh.md)阶段1，component=monitoring。
> aksClusterName、logAnalyticsWorkspaceName、ownerEmail为客户必填参数；模板不提供个人默认值。
> 下述历史实测仅为参考，客户必须重新验证Container Insights、查询命名约定及通知收件。

## 1. “最小告警集”的含义

阶段 1 不一次建设完整 SOC，而是优先覆盖最可能造成 LiteLLM整体中断或数据风险的信号：

- PostgreSQL磁盘耗尽、PANIC和 recovery异常；
- PostgreSQL PVC使用率达到 70%或 85%；
- LiteLLM或 PostgreSQL工作负载没有近期 Running记录；
- LiteLLM数据库/认证相关 401、503或异常突增；
- AKS管理操作失败；
- AKS删除操作启动。

这些告警发送到 `ag-litellm-stage1-owner`，当前邮件接收人是项目 Owner，并启用 Azure Common Alert Schema。

## 2. 数据采集

已为现有 AKS启用 Container Insights，使用 Managed Identity认证并关联现有 Log Analytics workspace。

已验证：

- `ama-logs` DaemonSet：2/2 Ready；
- `ama-logs-rs` Deployment：1/1 Ready；
- `ContainerLogV2`、`KubePodInventory`、`KubeNodeInventory`、`Perf`和 `InsightsMetrics`开始产生数据；
- PostgreSQL PVC的 `pvUsedBytes`和 `pvCapacityBytes`可以计算使用率。
- Azure已接受一次 `logalertv2` Action Group测试通知请求，Owner已确认实际收到邮件。

Container Insights会产生 Log Analytics摄入成本。本阶段未启用 Managed Prometheus或 Grafana。

## 3. 已部署告警

| 告警 | 数据源 | 严重级别 | 条件 |
| --- | --- | ---: | --- |
| `alert-litellm-postgres-critical-log` | `ContainerLogV2` | Sev 0 | 10分钟内出现磁盘写满、PANIC、shutdown、recovery或写文件失败证据 |
| `alert-litellm-postgres-volume-70-percent` | `InsightsMetrics` | Sev 2 | `pg-data`使用率 >70% |
| `alert-litellm-postgres-volume-85-percent` | `InsightsMetrics` | Sev 0 | `pg-data`使用率 >85% |
| `alert-litellm-workload-unavailable` | `KubePodInventory` | Sev 0 | 10分钟内缺少 LiteLLM或 PostgreSQL Running记录 |
| `alert-litellm-error-burst` | `ContainerLogV2` | Sev 1 | 10分钟内数据库/认证 401、503或异常证据超过4条 |
| `alert-litellm-aks-failed-administrative-operation` | Activity Log | - | AKS Administrative操作失败 |
| `alert-litellm-aks-delete` | Activity Log | - | AKS删除操作启动 |

说明：Sev 0表示关键，Sev 1表示错误，Sev 2表示警告。

## 4. 当前基线

部署时实际查询结果：

- 最近10分钟已有 LiteLLM namespace Pod inventory；
- PostgreSQL关键日志匹配数为0；
- `pg-data`使用率约 `0.07%`；
- 70%容量规则当前不匹配；
- 工作负载缺失规则当前不匹配。

## 5. 验证与测试

IaC位于 `infra/monitoring/main.bicep`。重新部署前应先运行 Bicep build和 resource group What-if。

推荐使用阶段workflow注入客户参数，不能原样重放示例部署。此模块引用旧RG中的AKS和workspace；跨RG日志归集需先扩展引用。KQL仍按`litellm` namespace、`pg-data` PVC及`postgres-` Pod命名过滤，名称不符时先调整并回归，避免创建看似成功但无法触发的告警。

非破坏性验证：

1. 确认 Action Group enabled且接收人正确；
2. 确认5条 Scheduled Query Rule和2条 Activity Log Alert均 enabled；
3. 在 Log Analytics中执行每条 KQL，健康状态下应返回0条匹配；
4. 使用脱敏合成日志测试 PostgreSQL关键日志规则时，应先评估生产日志污染和告警通知影响；
5. 不通过真实填满磁盘、删除 AKS或制造数据库 PANIC测试告警。

2026-09-02 已使用与 Action Group完全匹配的现有邮件接收器提交测试通知，Azure CLI返回提交成功，Owner随后确认实际收到邮件。Action Group邮件通道验收通过。

## 6. 已知边界

- 日志采集和 Scheduled Query Alert不是实时系统，通常存在数分钟延迟；
- “工作负载不可用”基于近期 inventory，不替代外部 HTTP可用性探测；
- LiteLLM错误规则依赖应用日志格式，升级版本后必须回归；
- 当前没有 Front Door/WAF，因此尚未覆盖边缘 4xx/5xx和源站健康；
- 当前没有 Managed Prometheus，因此尚未覆盖更细粒度的 Kubernetes指标；
- 当前没有自动修复动作，所有通知由 Owner人工判断；
- 完整 SOC、Sentinel和 SOAR属于后续阶段。

## 7. 运维响应

收到 PostgreSQL容量或 PANIC告警时：

1. 检查 `pg_isready`、PostgreSQL日志、PVC容量和 inode；
2. 不删除 PVC、PV、数据库文件或 WAL；
3. 不缩容 PVC；
4. 如确认容量不足，按 Runbook扩容原 PVC；
5. 恢复后立即执行逻辑备份并验证。

收到工作负载不可用告警时：

1. 检查 Deployment Ready/Available、Pod events和 probes；
2. 检查节点调度、镜像拉取、PVC attach和资源压力；
3. 若与刚执行的阶段 1滚动相关，使用预先保存的 Deployment快照或 `kubectl rollout undo`；
4. 恢复后执行健康、Chat、模型列表、PostgreSQL和 Codex affinity回归。
