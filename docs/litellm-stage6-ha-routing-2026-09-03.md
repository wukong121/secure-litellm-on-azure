# LiteLLM 安全增强阶段 6：双副本安全基线、共享路由与容量治理

> 文档状态：目标配置、Kustomize组件和静态验证完成；尚未部署或切流  
> 完成日期：2026-09-03  
> 前置条件：阶段4/5代码与What-if通过，云资源仍未部署  
> 目标：建立可审查的双副本安全运行基线，以低变量路由验证共享状态和长连接行为  
> 明确边界：不部署Azure/Kubernetes资源；不修改当前生产；不启用`usage-based-routing-v2`；不写入真实模型端点、ID、配额或Secret

## 1. 执行摘要

阶段6第一轮代码改造已完成：

- 新增独立`stage6-ha` Kustomize组件和Stage4/5/6组合渲染目标；
- 固定LiteLLM `1.98.0`候选镜像digest，保持非Root、只读根文件系统和三类探针；
- 双副本、`maxUnavailable=0`、`maxSurge=1`、PDB和跨节点强制拓扑分布；
- 增加`minReadySeconds=30`、600秒终止窗口和30秒端点摘除等待；
- 新增CPU/内存HPA，最少2副本、最多6副本，并限制快速缩容；
- Git管理模型组模板、稳定`model_info.id`、Router和日志安全策略；
- 保持`simple-shuffle`，增加四类affinity、Redis Entra/TLS共享状态、失败阈值和冷却；
- 明确不填入未经真实配额和压测证明的RPM、TPM及最大并发；
- 新增Stage6静态验证、完整前序回归、CI和Makefile门禁。

Stage6组件未加入dev/test/prod overlay，只存在于`deploy/validation/stage6`。当前生产和Azure/Kubernetes资源均未改变。

## 2. 双副本和滚动更新基线

目标Deployment：

- `replicas=2`，HPA `minReplicas=2`、`maxReplicas=6`；
- `maxUnavailable=0`、`maxSurge=1`；
- `minReadySeconds=30`，避免刚Ready的Pod立即被当作稳定容量；
- PDB `minAvailable=1`；
- topology spread以`kubernetes.io/hostname`为维度，`DoNotSchedule`；
- startup、readiness和liveness probes保持独立；
- 资源requests/limits继续作为调度和HPA基础；
- Pod和容器安全上下文沿用Stage3基线；
- Service继续使用ClusterIP，不创建公网LoadBalancer或NodePort。

West US目标集群不能宣称跨Availability Zone。当前拓扑约束只保证跨节点，不保证跨可用区。

## 3. 优雅终止与长连接边界

目标配置将`terminationGracePeriodSeconds`从120提高到600，并在`preStop`等待30秒，使EndpointSlice、Service和上游负载均衡器有时间停止向旧Pod分配新连接。

`preStop sleep 30`不是LiteLLM drain endpoint，也不能迁移已经建立的WebSocket/SSE连接。600秒窗口只提供退出预算，以下真实验收仍是上线Gate：

1. 新Pod通过readiness且稳定30秒后才进入容量；
2. rollout期间新HTTP/SSE/WebSocket请求成功；
3. 既有长连接的断开率、错误帧和客户端重连时间满足SLO；
4. 节点drain时PDB至少保留一个Ready副本；
5. 连接超过终止窗口时行为可观测且不会无限阻塞发布；
6. Ingress/Front Door的timeout、buffering和连接排空在实际入口纳入后单独验证。

当前代码不能被描述为“WebSocket/SSE无损滚动已通过”。

## 4. Git配置事实源

Stage6最终渲染配置明确以下边界：

| 数据 | 事实源 |
| --- | --- |
| 模型组、deployment映射、稳定`model_info.id` | Git |
| Router、affinity、重试、冷却和日志安全策略 | Git |
| Team、Virtual Key、预算和授权对象 | PostgreSQL |
| Master Key、永久Salt和数据库连接串 | Key Vault |
| Redis短期路由、冷却和affinity状态 | Azure Managed Redis |

环境变量`STORE_MODEL_IN_DB=false`防止UI/API与Git长期维护同一模型和Router配置。Stage6模板中的所有`REPLACE_*`和Azure OpenAI环境变量必须由受保护部署流程注入，带占位符的manifest不得应用。

每个deployment必须提供永久稳定且唯一的`model_info.id`。资源重建、名称变化或跨订阅迁移不得随意改变ID，否则affinity和历史归因可能失效。

## 5. 第一阶段低变量路由

当前冻结策略：

```text
simple-shuffle + encrypted-content/Responses/session/deployment affinity + Redis
```

配置包括：

- `num_retries=2`；
- `allowed_fails=2`；
- `cooldown_time=30`；
- `deployment_affinity_ttl_seconds=3600`；
- `encrypted_content_affinity`；
- `responses_api_deployment_check`；
- `session_affinity`；
- `deployment_affinity`；
- Redis Workload Identity Entra Token、TLS、CA和主机名校验。

不启用`usage-based-routing-v2`。Stage6门禁会拒绝它及`routing_strategy_args`进入组件，除非后续正式修改门禁并附带A/B证据。

真实RPM、TPM和`max_parallel_requests`没有写入模板。它们必须来自Foundry配额、模型差异、并发压测和429行为，不能使用方案文档中的示例数字。

## 6. Redis和数据库故障语义

Redis只承载可重建的短期状态，不是Team、Key、预算或授权的事实源。

| 功能 | Redis故障目标行为 | 上线要求 |
| --- | --- | --- |
| session/deployment affinity | 可降级重新选择健康deployment并告警 | 不得导致跨隔离组重试或`invalid_encrypted_content`循环 |
| 冷却和失败计数 | 可短时退化，但必须限制upstream attempts | 429/5xx风暴测试通过 |
| 软性缓存/性能优化 | 可fail-open | 禁止影响身份或租户边界 |
| Virtual Key认证 | PostgreSQL为权威；Redis不得成为唯一认证状态 | 无Redis时认证仍正确或明确fail-closed |
| 硬预算/租户隔离 | 不允许因Redis不可用而绕过 | 必须fail-closed或由PostgreSQL权威校验 |

现有静态配置只能证明连接选项和路由字段存在，不能证明LiteLLM在每种Redis故障下自动满足该矩阵。需要真实故障注入和调用链证据。

PostgreSQL短暂不可用时，创建/更新管理对象以及需要权威DB校验的路径应fail-closed。是否允许已验证短期凭据继续推理由Stage7身份授权测试决定，Stage6不提前放宽。

## 7. HPA和容量边界

首版HPA只使用Kubernetes Resource Metrics：

- CPU平均利用率65%；
- 内存平均利用率75%；
- 2至6副本；
- 扩容可每分钟增加100%或2个Pod；
- 缩容稳定窗口600秒，每分钟最多缩减25%。

仓库尚无Prometheus Adapter、KEDA或已验证的LiteLLM请求/并发指标，因此没有伪造自定义指标。生产阈值必须通过负载测试校准，并观察CPU、内存、事件循环阻塞、TTFT、429、连接数和数据库连接池。

## 8. 路由A/B升级门禁

只有满足以下条件才考虑`usage-based-routing-v2`：

1. 每个deployment的RPM/TPM和稳定ID准确；
2. `max_parallel_requests`来自目标模型和SKU压测；
3. Redis Entra Token刷新、连接池重认证和failover通过；
4. 双Pod affinity和Responses多轮连续性通过；
5. Prompt Cache读取率不下降；
6. 429、吞吐或TTFT/尾延迟至少一项有显著改善，且成功率和成本不恶化；
7. 长会话和批处理模型已分组隔离；
8. 回退到`simple-shuffle`经过演练。

## 9. 验证结果

统一入口：`make validate-stage6`或`bash scripts/validate-stage6.sh`。

已验证：

- Stage3/4/5完整回归；
- 13项Python单元测试；
- 全部Bicep及参数编译；
- Stage4/5/6组合Kustomize渲染；
- 双副本、RollingUpdate、PDB、拓扑分布和安全上下文；
- HPA上下限、Resource Metrics和缩容稳定窗口；
- Git配置、稳定ID占位符、四类affinity和Redis Entra/TLS字段；
- `usage-based-routing-v2`禁止门禁；
- 无明文数据库/Redis连接串或Key。

未验证：真实Pod、HPA指标流、模型调用、Redis/PG故障、WebSocket/SSE rollout、节点drain和生产流量。

## 10. 部署前Gate

以下全部完成前Stage6组件不得进入生产overlay：

1. Stage4/5新环境完成部署和私网验证；
2. 所有`REPLACE_*`由受保护部署流程替换，真实模型ID唯一且稳定；
3. RPM、TPM、并发、重试和冷却通过模型Owner及容量评审；
4. 双Pod跨节点、删除Pod和节点drain测试通过；
5. Redis Entra、Token刷新、连接池、故障转移和不可用语义通过；
6. PostgreSQL迁移、重连、PITR和Salt解密通过；
7. WebSocket、SSE、Responses HTTP和Codex多轮rollout测试通过；
8. HPA扩缩容不会中断长连接或耗尽PG/Redis连接；
9. Internal ingress/Front Door Private Link路径完成，不创建公网Service；
10. `simple-shuffle`回退路径和旧环境回退窗口保留。

## 11. 阶段状态

阶段6的目标Kubernetes代码、低变量Router配置、HPA和静态门禁已完成，但运行时验收依赖尚未部署的Stage4/5环境。准确状态是：

> 双副本安全基线和`simple-shuffle + affinity + Redis`目标代码已建立并通过本地组合渲染；未启用容量路由，未证明真实长连接、故障或扩缩容行为，等待新Private AKS环境进行集成验证。
