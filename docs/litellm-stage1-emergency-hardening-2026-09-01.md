> 发布说明：本文历史账号与应用资源名已去标识化，实际客户需重新确认身份/CA权限和验收，不继承参考环境状态。操作入口见[迁移指南](customer-migration-guide-zh.md)。

# LiteLLM 安全增强阶段 1：现有环境紧急加固

> 文档状态：当前环境可执行的核心技术加固已完成；Entra深度授权与MFA转客户环境实施  
> 启动日期：2026-09-01  
> 前置条件：阶段 0 Gate已通过  
> 目标：在新 Private AKS建设期间降低现有环境风险，不对当前 PoC执行高风险架构重构  
> 明确边界：不引入或修改 APIM；不迁移生产数据库；不切换 Router；不启用 L3原文审计

## 0. 当前进度

2026-09-01 已完成第一批代码级加固：

- LiteLLM startup/readiness/liveness probes；
- PostgreSQL `pg_isready` startup/readiness/liveness probes；
- LiteLLM `250m/1Gi` requests和 `1000m/2Gi` limits；
- Secret更新仅保留既有 `LITELLM_SALT_KEY`，避免整份替换删除永久 Salt，同时不保留未知或过期键；
- Azure CLI路径测试改为显式 mock解析结果，消除 Linux `/bin/az` 与 `az` 的环境差异；
- 相关单元测试从 8项扩展到 11项，11/11通过。

当前没有把上述 Pod模板变更应用到现有集群。原因是 LiteLLM和 PostgreSQL均为单副本，任何 Pod模板修改都会触发滚动重启；应先确认维护窗口，再执行生产变更和完整回归。

Owner随后确认 2026-09-02 当前无人使用，可立即作为维护窗口。已按 PostgreSQL、LiteLLM顺序逐个滚动和验证，没有同时更新两个工作负载。

维护窗口执行结果：

| 项目 | 结果 |
| --- | --- |
| 回退快照 | 已保存到本地忽略目录，权限 `700` |
| PostgreSQL首次 RollingUpdate尝试 | 因 RWO PVC Multi-Attach未完成；已立即回退，数据库恢复正常 |
| PostgreSQL策略修正 | 改为 `Recreate`，避免新旧 Pod并发挂载单一 RWO PVC |
| PostgreSQL最终滚动 | 成功，Ready 1/1，restartCount 0，`pg_isready`通过 |
| LiteLLM首次验证 | 新 Pod实际已启动；验证脚本对启动期单次探针失败/镜像格式判断过严而回退 |
| LiteLLM最终滚动 | 成功，Ready 1/1，restartCount 0，两个健康端点HTTP 200 |
| LiteLLM镜像 | 已固定到阶段 0记录的 ACR digest |
| LiteLLM resources/probes | 已应用 |

首次失败均已安全回退，最终配置已重新应用并验证。历史 `Multi-Attach`和启动期 `connect refused` Warning属于本维护窗口已解决事件，不代表当前故障。

## 1. 已批准决策

- Salt迁移采用路径 A：当前有效数据库加密材料作为未来永久 `LITELLM_SALT_KEY`；
- 当前 Master Key没有已知安全隐患；
- 生产 Salt显式化延期到新 Private AKS、Key Vault和隔离 PostgreSQL就绪并验证后执行；
- 阶段 0 dump上传及 Blob下载恢复验证延期到新 Private AKS建成后执行；
- 当前 `1.95.0` 继续作为回退基线，不在阶段 1升级 LiteLLM版本；
- 当前已运行镜像 digest作为阶段 1固定目标；
- 现有公网入口只做临时收敛，不在阶段 1原地改造成最终 WAF/私有入口。

## 2. 当前状态与任务

| 任务 | 当前状态 | 阶段 1动作 |
| --- | --- | --- |
| 固定 Salt | 决策完成、未显式配置 | 保持现状；增加部署保护，防止覆盖未来 Salt；隔离验证后再实施 |
| 固定 Master Key | 当前有效 | 禁止自动生成或无意覆盖；不执行轮换 |
| PostgreSQL容量 | PVC `100Gi`，健康 | 保持禁止缩容；持续监控 |
| Spend Logs | `7d`/`1d`，正文关闭 | 保持并验证 |
| 数据库备份 | 本地逻辑备份恢复通过；私有 Blob已部署 | 新 Private AKS建成后上传并再次恢复验证 |
| LiteLLM镜像 | `1.95.0` Tag，运行摘要已记录 | Deployment固定到已验证 digest |
| LiteLLM probes | 无 | 增加 startup/readiness/liveness |
| LiteLLM resources | 无；当前约 `6m CPU / 1145Mi` | 增加保守 requests/limits并观察 |
| PostgreSQL probes | 无 | 增加 `pg_isready` startup/readiness/liveness |
| PostgreSQL resources | 已有 `100m/128Mi` requests、`500m/256Mi` limits | 暂不调整，先加 probes |
| Admin UI访问 | 与公网数据面共用入口 | 形成临时来源限制方案；避免在未知客户端清单下直接阻断 |
| SSO/App Role/MFA | SSO已验证，角色分配仍需确认 | 核验角色 claim和普通用户边界 |
| 告警 | 缺少正式容量/可用性告警 | 设计并部署最小告警集 |

截至 2026-09-02，运行状态已与阶段 1代码对齐：PostgreSQL使用 `Recreate`和三类探针；LiteLLM使用固定 digest、资源限制和三类探针。

## 3. 本阶段实施顺序

1. 将探针、资源限制、镜像固定和 Secret键保护纳入部署代码与测试；
2. 使用现有工作区测试验证 Kubernetes对象生成；
3. 保存生产 Deployment快照和当前 Codex 10轮结果；
4. 在维护窗口对现有单副本工作负载执行一次滚动变更；
5. 验证健康、Chat、模型列表、Codex affinity和 PostgreSQL；
6. 配置数据库容量、recovery、Pod可用性和入口异常的最小告警；
7. 核验 SSO App Role、MFA和普通用户访问边界；
8. 形成阶段 1验收记录。

## 4. 变更安全边界

- 当前 LiteLLM是单副本，任何 Pod模板变更都会引发短暂重启；部署前必须安排维护窗口；
- 不直接给当前生产 Secret设置 `LITELLM_SALT_KEY`；
- 不使用新 Salt，不轮换当前 Master Key；
- 不移除节点 VMSS业务 UAMI；该动作属于新环境 Workload Identity迁移；
- 不改变公网 DNS、Ingress、证书或 Router；
- 不改变 PostgreSQL PVC、schema或数据库密码；
- 不把目标状态误报为当前已经完成；
- 所有变更必须能够恢复到阶段 0记录的 Deployment和镜像摘要。

## 5. 初始资源建议

根据阶段 0采样：LiteLLM约使用 `6m CPU / 1145Mi`，PostgreSQL约使用 `1m CPU / 75Mi`。阶段 1建议：

| 组件 | CPU request | CPU limit | Memory request | Memory limit |
| --- | ---: | ---: | ---: | ---: |
| LiteLLM | `250m` | `1000m` | `1Gi` | `2Gi` |
| PostgreSQL | `100m` | `500m` | `128Mi` | `256Mi` |

LiteLLM内存已接近并偶尔可能超过 `1Gi`，因此 `1Gi`只作为调度 request，`2Gi`作为初始 limit。上线后必须观察 working set、OOM、GC、延迟和节点压力；若接近 limit，不应等待 OOM后再调整。

## 6. 建议探针

LiteLLM：

- startup：`GET /health/liveliness`，允许最长约 5分钟启动；
- readiness：`GET /health/readiness`，失败后从 Service移除；
- liveness：`GET /health/liveliness`，仅检测进程不可恢复卡死；
- liveness阈值应避免数据库短暂故障触发重启风暴。

PostgreSQL：

- startup/readiness/liveness均使用 `pg_isready`；
- readiness负责阻止未完成 recovery的实例接收依赖流量；
- Pod `Running`不能替代数据库可用性验证。
- 当前单副本 PostgreSQL使用 `ReadWriteOnce` PVC，Deployment必须使用 `Recreate`策略；默认 `RollingUpdate`会让新旧 Pod同时挂载同一磁盘并触发 `Multi-Attach`。

## 7. 阶段 1验收门槛

- 部署代码和测试包含 LiteLLM/PostgreSQL探针；
- LiteLLM具有保守资源 requests/limits；
- 生产镜像按已验证 digest运行；
- Secret更新不会无意删除未来的 `LITELLM_SALT_KEY`；
- 滚动后健康、模型列表、Chat和 PostgreSQL检查通过；
- Codex多轮任务不出现 backend切换或 `invalid_encrypted_content`；
- 没有 OOM、重启风暴或持续 readiness失败；
- 最小告警集已创建并测试；
- SSO/App Role/MFA和普通用户访问边界已有证据；
- 回退步骤已验证或至少完成 dry run。

## 8. Container Insights与最小告警集

已为当前 AKS启用 Container Insights，并关联现有 Log Analytics workspace。未启用 Managed Prometheus或 Grafana。

采集组件：

- `ama-logs` DaemonSet：2/2 Ready；
- `ama-logs-rs` Deployment：1/1 Ready；
- `ContainerLogV2`、`KubePodInventory`、`KubeNodeInventory`、`Perf`和 `InsightsMetrics`已有数据。

已通过 `infra/monitoring/main.bicep` 部署：

- 1个 Owner邮件 Action Group；
- 5条 Scheduled Query Alert；
- 2条 Activity Log Alert。

覆盖范围：

1. PostgreSQL磁盘耗尽、PANIC、recovery和写文件失败；
2. `pg-data`使用率 >70%和 >85%；
3. LiteLLM/PostgreSQL缺少近期 Running inventory；
4. LiteLLM数据库/认证相关错误突增；
5. AKS管理操作失败和删除操作。

部署时 `pg-data`使用率约 `0.07%`，健康查询不匹配任何容量告警。详细说明见 `infra/monitoring/README_ZH.md`。

告警规则和通知目标已经创建并启用。Azure已接受一次 `logalertv2` Action Group测试邮件请求，Owner已确认实际收到，邮件通知通道验收通过。尚未通过合成日志触发具体规则；删除 AKS、填满磁盘和制造 PostgreSQL PANIC不得用于测试。

## 9. 生产回归结果

| 验证 | 结果 |
| --- | --- |
| PostgreSQL Ready | 1/1 |
| PostgreSQL `pg_isready` | 通过 |
| PostgreSQL restartCount | 0 |
| PostgreSQL PVC | `100Gi` |
| LiteLLM Ready | 1/1 |
| LiteLLM restartCount | 0 |
| LiteLLM当前资源使用 | 约 `6m CPU / 1121Mi` |
| LiteLLM镜像 digest | 与阶段 0一致 |
| LiteLLM Pod内 liveliness/readiness | HTTP 200/200 |
| 公网 liveliness | HTTP 200 |
| 公网模型列表 | HTTP 200 |
| 最小 Chat | HTTP 200，有 choices/usage，无 error |
| Codex任务 | `gpt-5.6-terra`，10/10完成，通过 |
| 配置后端数 | 3 |
| 最终轮缓存率 | `89.32%` |
| 最后三轮稳态缓存率 | `88.12%` |
| 聚合缓存率 | `82.84%` |
| Prefix continuity | `100%` |
| 唯一 backend | 1 |
| backend切换 | 0 |

`gpt-5.6-terra`具有3个可选后端，因此该结果可以证明多后端场景下 affinity将同一会话固定到单一 backend。此前 `gpt-5.6-sol`只有1个后端，其10轮结果仅证明缓存连续性和功能回归，不再作为 affinity有效性的证据。

第一次 Chat冒烟使用过低的输出上限，模型在达到输出限制后返回 BadRequest且客户端等待超时；使用正确的 `max_completion_tokens`重跑后HTTP 200。该问题不是探针或资源限制回归。

重启日志中仍存在少量历史数据库模型记录无法识别 provider并被丢弃的 Warning。当前 `/model/info`返回10个 deployment记录、4个模型别名，公网模型列表和实际推理正常。该 Warning与既有数据库加密/配置治理风险一起转入 SEC-07/SEC-14，不在阶段 1直接修改数据库。

## 10. 后续事项

1. 执行非破坏性合成日志告警规则演练；Action Group测试邮件已由 Owner确认收到；
2. 在客户 Entra租户完成 App Role assignment、`roles` claim、MFA/Conditional Access和普通用户负向权限测试；当前微软内部环境不再执行该项；
3. 持续观察 LiteLLM内存 working set、OOM、readiness和节点压力；
4. 新 Private AKS建成后完成 Blob备份上传/下载恢复和 Salt路径 A隔离验证；
5. 将 Container Insights、DCR和告警完整纳入目标平台 IaC；
6. 在阶段 2处理数据库模型 Warning和配置事实源治理。

当前环境的阶段 1结论：**核心技术加固完成**。Entra深度授权与MFA因微软内部租户目录权限受限被批准转移到客户环境，不作为当前环境继续推进阶段 2的阻塞项，但必须作为客户生产上线门禁保留。

## 11. SSO、App Role、MFA和普通用户权限核验

### 11.1 SSO应用

通过实际 SSO授权重定向定位到应用 `example-legacy-auth-app`：

- Enterprise Application处于 enabled；
- 回调地址包含正确的 `/sso/callback`；
- 2026-09-02 已将 enabled App Roles完善为 `proxy_admin`、`proxy_admin_viewer`和 `internal_user`；
- 三个角色均只允许 `User`成员类型；
- `appRoleAssignmentRequired=false`。

### 11.2 App Role assignment和 `roles` claim

Enterprise Application当前仍有2个用户 assignment，但两项均是默认 assignment：

- 当前 示例环境Owner账号已分配到企业应用；
- 当前账号没有显式 `internal_user` App Role assignment；
- `internal_user`显式 assignment数量为0；
- 默认 assignment不会产生已定义角色对应的 `roles` claim；
- 现有登录成功只能证明 SSO认证链路有效，不能证明 App Role授权已经完成。

已经尝试将当前账号显式分配为 `proxy_admin`、另一名现有用户分配为 `internal_user`，但 Microsoft Graph返回 `Authorization_RequestDenied`。当前账号可以维护自己拥有的 App Registration角色定义，但没有企业应用用户角色分配权限。失败发生在第一项 assignment创建时，核验确认没有产生部分修改：仍为2个默认 assignment，当前账号无显式角色，`appRoleAssignmentRequired`仍为 `false`。

完成 assignment需要租户管理员临时授予适当 Entra目录角色，或由现有管理员代为执行。推荐最小权限由客户 Entra管理员确认，通常使用 Cloud Application Administrator/Application Administrator管理企业应用assignment；不应为此长期授予 Global Administrator。

因此阶段 1的角色验收尚未通过。角色定义已完成，剩余顺序为：

1. 当前 示例环境Owner账号显式分配 `proxy_admin`；
2. 现有普通测试用户显式分配 `internal_user`；
3. 后续需要只读管理员时分配 `proxy_admin_viewer`；
4. 确认显式角色生效后，删除无角色的默认 assignment；
5. 启用 `appRoleAssignmentRequired=true`，避免未分配用户登录；
6. 重新登录并只验证解码后的 ID token含预期 `roles`值，不记录完整 token；
7. 验证 LiteLLM将 Entra角色映射为预期数据库角色；
8. 验证普通用户不能访问管理功能。

### 11.3 MFA

Owner确认当前只配置了 SSO，尚未配置 MFA/Conditional Access。因此不能将“使用 Microsoft登录”解释为“已经强制 MFA”。

MFA推荐通过 Microsoft Entra Conditional Access强制，而不是由 LiteLLM自行实现。最低建议：

- 目标资源选择 `example-legacy-auth-app`；
- 当前只有一个管理员，按 Owner决定可直接纳入 示例环境Owner账号，不强制先建立管理员组；
- Grant要求 MFA；
- 当前按 Owner决定暂不设置 Break Glass排除；该选择存在唯一管理员被锁定的风险，必须先使用 Report-only并保留租户管理员恢复路径；
- 先 Report-only观察，再切换 On；
- 在启用前至少验证管理员和普通用户登录；
- 生产管理员进一步结合合规设备、登录风险和 PIM。

创建 Conditional Access策略需要客户 Entra许可证和相应管理员权限。当前账号甚至无法读取 Conditional Access策略，Microsoft Graph明确要求 Security Reader、Global Reader、Security Administrator、Conditional Access Administrator或 Global Administrator等角色之一。因此本次不能安全创建或验证策略。推荐由租户管理员临时授予 `Conditional Access Administrator`，先创建仅针对 示例环境Owner和该应用的 Report-only策略，验证登录日志后再切换 On。

### 11.4 当前微软内部租户的实施边界

Owner是微软内部员工，当前账号在该 Entra租户中没有企业应用角色分配和 Conditional Access管理所需的目录角色。该限制属于租户权限边界，不是 LiteLLM或方案缺陷。

本环境中的处理结论：

- 保留已经完成的三个 App Role定义；
- 不继续尝试提升当前员工账号的目录权限；
- 不创建或启用 Conditional Access/MFA策略；
- 不删除现有默认 assignment，也不启用 `appRoleAssignmentRequired`，避免在无法完成显式assignment时阻断现有SSO；
- App Role assignment、`roles` claim、MFA和普通用户负向测试转移到客户 Entra租户执行；
- 当前环境只保留“SSO认证链路已验证”的结论，不能宣称角色授权或MFA已完成。

客户环境需要具备：

- 可管理 App Registration和 Enterprise Application的目录管理员；
- 可创建用户/组 App Role assignment的权限；
- Conditional Access Administrator或等价批准权限；
- 支持 Conditional Access的 Entra许可证；
- 至少一个管理员测试账号和一个普通用户测试账号。

客户实施顺序：

1. 导入或创建 `proxy_admin`、`proxy_admin_viewer`、`internal_user`三个角色；
2. 客户管理员显式分配 `proxy_admin`，普通测试用户分配 `internal_user`；
3. 验证重新登录后的 ID token包含预期 `roles`，但不保存完整token；
4. 验证 LiteLLM角色映射正确；
5. 删除无角色的默认assignment并启用 `appRoleAssignmentRequired=true`；
6. 创建针对管理员和 LiteLLM应用的 Conditional Access MFA策略；
7. 先使用 Report-only评估，再切换为 On；
8. 执行管理员正向测试和普通用户负向测试；
9. 保存脱敏验收证据和回退步骤。

### 11.5 LiteLLM数据库用户角色

当前数据库角色分布：

| LiteLLM角色 | 用户数 |
| --- | ---: |
| `proxy_admin` | 1 |
| `internal_user` | 2 |
| `internal_user_viewer` | 1 |

关联 Virtual Key数量分别为：`proxy_admin` 21、`internal_user` 4、`internal_user_viewer` 1。该统计不包含Key或用户标识。

角色数据存在不等于权限边界已经通过测试。当前尚未使用普通用户会话执行以下负向验证：

- 普通用户访问 Admin UI/API应被拒绝；
- `internal_user_viewer`不能执行写管理操作；
- 普通用户不能跨 Team、模型 ACL或预算边界；
- 禁用用户或移除 assignment后访问应失效。

这些测试应在 App Role显式分配和 MFA策略完成后，以批准的测试账号执行。

### 11.6 普通用户负向测试矩阵

“负向测试”是主动尝试不应被允许的操作，并证明系统返回拒绝，而不是只验证普通用户能够登录和调用模型。

| 测试身份 | 尝试操作 | 预期结果 |
| --- | --- | --- |
| 未分配用户 | 登录 Enterprise Application | Entra拒绝登录；`appRoleAssignmentRequired=true`生效 |
| `internal_user` | 打开 Admin UI或管理API | 401/403，不能看到管理功能 |
| `internal_user` | 创建、删除或查看完整 Virtual Key | 拒绝；不能取得其他用户Key |
| `internal_user` | 创建/修改模型、Router、Guardrail、SSO | 拒绝 |
| `internal_user` | 修改用户、Team、预算、RPM/TPM | 拒绝 |
| `internal_user` | 调用未授权模型 | 403或等价授权错误，不跨模型fallback |
| `internal_user` | 越过自己的 Team/预算 | 拒绝或达到限制后停止 |
| `proxy_admin_viewer` | 查看批准的管理信息 | 允许只读 |
| `proxy_admin_viewer` | 执行任何写管理操作 | 401/403 |
| 已移除assignment用户 | 使用旧浏览器会话重新访问 | Token过期/重新鉴权后失效；验证回收SLA |
| 已禁用用户 | 新登录和刷新Token | 均失败 |

测试只记录身份类型、路径类别、HTTP状态和授权结果，不记录完整 Token、Cookie、Virtual Key、用户邮箱或响应敏感正文。所有测试使用专门测试账号和测试数据，不对真实生产模型、用户或预算执行破坏性写入。