# LiteLLM安全增强代码收尾台账

> **历史记录：** 本文保留2026-09-07当时的缺口判断。Stage9的“Admin不公开”目标已于2026-09-22后演进为独立Admin Front Door、WAF来源IP白名单、内层登录、双PLS和双平面FDID绑定；当前状态以迁移指南和自动化测试为准。

> 2026-09-10范围决策：第一阶段改为原生 Spend Logs，自建 L3、独立 Blob/HSM 与治理为可选增强，不再一律阻断基础版。2026-10-09原生配置发布/受控查询已有代码和隔离测试；字段/权限的云端证据、容量/清理/备份/故障验收仍须完成。历史 L3 检查不能直接填 passed，详见[部署指南](customer-deployment-workflows-zh.md)。

> 日期：2026-09-07
> 总体状态：阶段0至9均已有阶段性交付，不代表全部编码、IaC集成或生产验收完成
> 本轮：OSS回调实测、未接入部署的薄适配器、独立CI门禁及收尾清单
> 操作边界：不部署Azure/Kubernetes、不改DNS或生产流量、不退役旧资源、不提交Git

## 当前校准（2026-10-09）

只读 Azure 管理核查确认 West US3 新建 dev/test 的 private AKS Running/Succeeded、Azure CNI overlay、Azure RBAC、禁用本地账户、WI/OIDC；System/User 池各 2 台 `Standard_D4s_v4`。API/Admin Front Door Premium、独立 PLS/内部 LB/隔离私有入口与 WAF Prevention 已形成，Admin 否定 SocketAddr IPMatch 白名单包含三个 IPv4 `/32` 与一个 IPv6 `/128` 精确出口。模型 WI、PG/Managed Redis 私网 Entra-only、私有 ACR、分离的后端/证书 Vault 及 Firewall 路由出站为当前基线。

默认原生 LiteLLM `1.104.0` 网关；用户验证了 Admin 密码 fallback 登录及 vkey Codex Responses 推理，不是 Entra 用户 SSO 或生产就绪。Entra 企业准入延期，无 APIM、App Service 或单一共享认证代理。I01/I06 的“没有 controller/未部署边缘”、C07 的“原生 UI 未接通”已不适用于当前路径；数据库升级/原生字段权限的隔离证据见[升级记录](litellm-1.104.0-upgrade-validation-2026-10-07.md)。长期令牌刷新、CSI 换密、HA/PITR、负载、协议负向、审计故障与企业身份仍需运行证据。

以下 C/I 表格及“下一批次”保留 **2026-09-07 的历史缺口和增强分支依赖**，不是当前待办快照或所有基础版 P0。生产门槛以[实施路线](litellm-security-hardening-implementation-roadmap-zh.md)、[迁移指南](customer-migration-guide-zh.md)和实际证据为准；Stage0–9 CLI/workflow 编号不变，不宣称全阶段通过或模型同步已云端验证。

## 1. 当时形成的证据（2026-09-07）

LiteLLM `1.98.0` OSS CustomLogger已经通过实际SDK和13场景Proxy测试，可以提供正常Chat、Responses（含SSE）、Embeddings及非流式工具调用的模型侧内容。新增5项信封与4项适配器测试通过。详见[OSS回调实测](litellm-oss-callback-validation-2026-09-07.md)。

同时证明：上游未正常结束的Chat流仍可能触发success；回调交付异常不会阻止模型200响应。因此后续方向为**OSS回调提供模型事件，认证代理提供可信入口事实，独立交付层负责可靠审计**。现有Node内容采集尚未替换，不能宣称已完成生产集成。

不使用APIM，不采购LiteLLM Enterprise，不依赖其原生JWT/RBAC或付费存储连接器。API/admin保持`llm-api`与`llm-admin`两个子域；公开模板使用客户可替换域名，不包含个人域名或真实Azure标识。

## 2. 历史增强分支离线开发缺口（2026-09-07）

以下编号为收尾项，不新增或重编号SEC工作包。P0项阻断试点；P1若属于客户首发范围，也必须进入试点门禁。

| 编号 | 优先级/状态 | 下一交付与验收条件 |
| --- | --- | --- |
| C01 L3受信接收器 | P0，未实现 | 将回调关联到入口认证意图；不相信客户metadata/Trace；显式校验事件结构、tenant/subject/Team、重放与内容上限；跨租户及伪造事件测试拒绝 |
| C02 L3可靠交付 | P0，未实现 | 确定持久化边界、幂等键、attempt与request关系；实现重试、背压、缺口对账与恢复；用kill、磁盘/队列满、写入结果不确定测试证明所声明语义 |
| C03 协议与完整性 | P0，部分验证 | 补齐重试/fallback、上下游断连、Responses断流、流式工具及结果续轮；分别记录模型终态/传输/客户端/持久化状态；不能以success回调或200判完整 |
| C04 Codex兼容与对象授权 | P0，未完成 | 用真实客户端需要的协议矩阵确定范围；补充对象/会话所有权及跨租户拒绝后才开放引用、多轮和WS；当前Files/MCP、加密引用等继续拒绝 |
| C05 L3治理收尾 | P0，部分实现 | 接入正文脱敏与保真预览、解决hold/delete竞态、明确access日志期限；验证审批撤销、过期、保全和删除失败；客户优先的L3不是远期待办 |
| C06 采集部署集成 | P0，未实现 | 固定镜像打包/注册回调、受信通道、身份与配置；隔离环境新旧一致性/负载比对；决定采集所有权后再替换Node内容捕获，保留入口准入和断连事实 |
| C07 身份及运维流程 | P0核心/P1完整UI，部分实现 | 每平面Vault/UAMI与Key初始化/轮换编排、最小管理权限及真实Entra负向测试；当前有限管理路由不等于LiteLLM完整原生UI已接通 |
| C08 观测与响应 | P0日志接入/P1扩展，部分实现 | 实现DCR/DCRA、需要的Prometheus接入及身份输出，关联模型调用；将检测响应契约变成批准的通知/工单Playbook；Collector本地配置通过不等于云端入库 |
| C09 Guardrail扩展 | P1，未完成 | 按批准范围实现PII、Prompt Shields、输出/工具动作控制及中英文误报召回验收；当前仅输入Content Safety观察/阻断，不外推全部协议 |

C01/C02先形成一个可故障注入的最小端到端闭环，再推进C03/C04与C06。协议测试可离线增加，但开放原来被阻断的协议必须同时完成授权与所有权控制。

## 3. 历史 IaC 与部署集成缺口（2026-09-07）

| 编号 | 当前缺口 | 编码或配置收尾 | 需要的隔离环境验收 |
| --- | --- | --- | --- |
| I01 私有入口 | 尚未编排controller和内部Standard LB，PLS只引用已有frontend | 明确受支持controller、独立API/admin命名空间、内部LB、TLS/证书续期及NetworkPolicy；输出供PLS使用 | 私网可达、admin不公开、证书有效、探针与长流超时实测 |
| I02 身份/Vault/CMK | 应用、审计、Collector等身份与Vault/Key仍有外部依赖和占位值 | 补全UAMI/federation、最小RBAC、CSI/CMK参数与跨模块输出；不将Secret写入模板 | WI令牌、私网DNS、CSI轮换、CMK取钥/轮换与负向权限验证 |
| I03 PostgreSQL | 当前Entra-only且默认无HA，应用令牌续期未端到端验证 | 实测驱动/Prisma连接与令牌更新；若批准密码补偿，必须同时实现`passwordAuth`、秘密轮换和应用配置；仅生成密码无效 | 1.95到1.98隔离迁移、密文可读性、备份/PITR恢复、连接池及批准的HA/恢复目标 |
| I04 Redis/容量 | Entra/TLS模板存在，实际令牌与路由容量未验收 | 完善应用配置、RPM/TPM/并发及亲和测试输入 | token轮换、故障恢复、双副本共享状态、扩缩容和429/缓存/成本对比 |
| I05 Monitor/审计存储 | 审计账户/Collector/检测模板未部署 | 接线监控身份与诊断目的地，审计账户/容器RBAC与CMK依赖 | Trace/日志实际入库、告警闭环、原文不入普通日志、留存删除与恢复演练 |
| I06 Front Door/切流 | 默认关闭What-if通过，不含启用场景 | 补齐I01后生成启用场景参数，审查路由/WAF/PLS批准和发布证据 | 获准后做enabled What-if与试点，验证长流、旁路拒绝及回退；无admin公网路由 |

以上模块不得通过“去掉占位符”简单视为接线完成。新增身份、网络、数据库认证或默认开关改变都需相应测试；不为获得What-if绿色结果放宽最小权限或忽略Unsupported项。

## 4. 必须由客户或 Owner 确认（按所选基础/增强模式）

- L3允许的最大缺口、故障时拒绝/继续策略、响应延迟及成本预算；在此之前不承诺零丢失，不撤销已有pending准入控制。
- 正文采集对象和告知、默认留存、access日志期限、审批发布者和保全法律要求；法定不可变与到期物理删除可能冲突，需明确规则。
- 首发Codex/Agent协议与管理功能范围，分别列出必须支持和继续拒绝的能力，不以当前最小API范围替代业务验收。
- PG认证路径、HA与RTO/RPO、区域/配额、入口controller和证书管理方式，以及真实Entra应用注册/角色/CA/MFA责任人。
- 批准新增资源费用及隔离试验范围后，才进行云端部署。生产试点、DNS、切流、旧环境退役分别审批，不隐含包含在编码授权内。

## 5. 当时的下一验收批次（2026-09-07，非当前默认计划）

1. **本轮已完成**：固定版本回调证据、显式信封/错误隔离、可复现门禁和本台账；CI定义已添加，远端执行未验证。
2. **下一代码批次**：C01/C02受信接收器与可靠交付最小闭环，先验证伪造/重放、并发/重复、写入失败及重启恢复，再补协议矩阵。持久化方案不得绕过上述客户约束。
3. **部署准备批次**：I01至I05接线、无占位符预检、enabled What-if审查和隔离部署Runbook；未授权不执行部署。
4. **试点验收批次**：L3恢复与治理、真实Codex/Entra/负载、Azure权限、数据库迁移与回退全部形成证据后，再进入Stage9获准客户端试点。

历史迁移方案要求旧LiteLLM、数据库、VMSS业务UAMI及Master/Salt路径A保留；当前 greenfield dev/test 不据此假定存在旧生产迁移源。任何新验证失败都不回滚用户已有代码，也不以生产原地变更代替隔离验证。