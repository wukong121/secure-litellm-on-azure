# 安全增强版LiteLLM Azure基础设施

本目录提供客户安全网关的Bicep模板，不提供旧生产环境的一键原地升级。客户从[项目总览](../README_ZH.md)和[迁移指南](../docs/customer-migration-guide-zh.md)进入，使用Customer staged migration workflow按阶段注入配置、审查What-if，再单独批准部署。

## 目录边界

- `modules/`：可复用Azure资源模块；
- `environments/`：`dev/test/prod`环境入口与参数；
- [backup-storage](backup-storage/README_ZH.md)：阶段0可选的私有备份存储及初始专用VNet；禁止接管共享VNet或在阶段4后重放bootstrap模板；
- [monitoring](monitoring/README_ZH.md)：阶段1旧网关最小告警，需匹配客户实际日志与命名；
- [audit-storage](audit-storage/main.bicep)：阶段8独立CMK私有审计存储及分离权限；
- [audit-detection](audit-detection/main.bicep)：默认禁用的检测规则及响应契约，非已运行的自动响应；
- [edge](edge/README_ZH.md)及[edge-origin](edge-origin/main.bicep)：阶段9 Front Door/WAF和API专用Private Link Service。

这些模板和本地验证结果不等于客户资源已部署。提交的环境参数默认关闭资源创建；客户迁移工具只为选定阶段生成显式参数和只读预览，不执行部署。尚未完成的身份、入口、数据认证和观测接线见[收尾台账](../docs/litellm-code-completion-backlog-2026-09-07.md)。

## ACR模块

`modules/container-registry`声明Premium ACR，并默认：

- 禁用Admin User；匿名拉取保持服务默认关闭，并在部署后验收；
- 禁用公网访问；
- 禁止网络绕过；
- 禁止镜像导出；
- 开启未标记manifest保留。ACR Soft Delete需在目标API和区域能力确认后单独启用。

环境入口中的 `deployContainerRegistry` 默认均为 `false`。原因是公网禁用的ACR必须先完成Private Endpoint、Private DNS和受控构建/导入执行路径。阶段4网络设计批准前不得为了方便把生产ACR长期开放公网。

## 参数与Secret

客户workflow使用`CUSTOMER_CONFIG_JSON` Environment variable生成明确的资源名、区域、Owner和组件参数；配置必须与OIDC的订阅/租户一致。阶段验收记录放在`MIGRATION_EVIDENCE_JSON` Environment secret。完整字段和前置条件见[客户配置模板](../config/customer.example.json)及迁移指南。

直接编译环境参数时使用`LITELLM_ACR_SUFFIX`、`AZURE_LOCATION`、`OWNER_EMAIL`和`LOG_ANALYTICS_WORKSPACE_NAME`；`stage3check`、`owner@example.com`等回退只用于离线检查，不是部署默认值。客户入口拒绝占位符，不读取个人本地环境。

任何Secret、Token、Client Secret、数据库密码和连接串都不得写入Bicep或`.bicepparam`。目标值通过Key Vault和Workload Identity提供。

## 验证

在仓库根目录执行`make validate-stage9`覆盖前序阶段的编译、渲染和安全检查；执行`./.venv/bin/python -m unittest tests.test_customer_templates`验证客户生成参数与Bicep入口的契约。两者均不部署云资源。

云端What-if可使用手动workflow的OIDC身份，或迁移指南中的客户受控终端流程。实际资源部署须另行审批，不属于自动验证。

## 阶段4网络和Private AKS模块

阶段4模块已经加入，但所有环境参数的`deployStage4`仍为`false`，当前不会创建云资源。

以下为参考网络规划，客户必须独立批准CIDR与资源名称：

| 用途 | 子网/CIDR |
| --- | --- |
| Azure Firewall | `AzureFirewallSubnet` / `10.30.0.0/26` |
| AKS System Pool | `snet-aks-system` / `10.30.1.0/24` |
| AKS User Pool | `snet-aks-user` / `10.30.2.0/23` |
| Private ingress/PLS | `snet-private-ingress` / `10.30.4.0/24` |
| Private Endpoints | 需预建 `snet-private-endpoints` / `10.30.8.0/24` |
| Pod CIDR | `10.244.0.0/16` |
| Service CIDR | `10.31.0.0/16` |

阶段4模块包括：

- Azure Firewall Premium、Policy、显式HTTPS allowlist和UDR；
- Private AKS、禁用Local Accounts、Entra Azure RBAC、Cilium、OIDC和Workload Identity；
- System/User Node Pool分离和多节点；当前模板不声明`availabilityZones`，客户跨区要求需按实际区域能力另行设计和验证；
- ACR及Azure OpenAI Private Endpoint和Private DNS；
- LiteLLM专用UAMI及Federated Identity Credential；
- 跨订阅Azure OpenAI最小数据平面RBAC。

Private AKS创建模板保留控制面Diagnostic Settings和Defender，但不在`addonProfiles`中直接启用Container Insights。参考环境曾出现workspace解析预检失败；后续独立DCR/DCRA接入仍是待完成项，必须在客户集群中验证实际数据流。

真实Azure OpenAI资源ID、订阅和资源组不会进入提交的参数文件，必须在受保护的What-if/部署变量中注入。

启用`deployStage4=true`前必须确认：

1. 全部CIDR与客户Hub、本地网络和其他订阅不重叠；
2. 客户区域内所选AKS版本、AzureLinux和VM SKU可用；要求跨可用区时，须选择支持Availability Zones的区域并评估数据驻留和成本；
3. Azure Firewall FQDN清单通过实际依赖观测补全；
4. GitHub self-hosted runner或其他私网执行路径已设计；
5. 跨订阅Private Endpoint审批和RBAC权限已获得；
6. What-if不会修改或删除现有Blob Private Endpoint子网；
7. 当前生产环境继续保留，不移除VMSS业务UAMI。

本地运行`make validate-stage4`验证Stage3回归、CIDR、Bicep、参数和仓库安全策略。

## 阶段5 Key Vault与托管数据服务

阶段5模块已经加入，但所有环境参数的`deployStage5`仍为`false`，且只有`deployStage4=true`时才会生效。当前不会创建云资源。

目标资源：

- Key Vault Premium：RBAC、90天Soft Delete、Purge Protection、删除锁、公网禁用、Private Endpoint、审计日志；
- PostgreSQL Flexible Server 16：Entra-only、128 GiB自动扩容、14天PITR、公网禁用、Private Endpoint和诊断；
- Azure Managed Redis：Balanced B0、HA、TLS 1.2、NoCluster、AOF、禁用Access Key、Workload Identity Entra访问策略、Private Endpoint和诊断；
- Private DNS：`privatelink.vaultcore.azure.net`、`privatelink.postgres.database.azure.com`和`privatelink.redis.azure.net`。

基础参数沿用参考环境的DNS复用设置，不代表客户已具备对应Zone。客户配置模板显式列出`createStage5*PrivateDnsZone`开关，必须按实际Zone归属填写；由中心团队预建时关闭创建，避免重复管理共享资源。

IaC不会创建任何Key Vault Secret值，也不会接收数据库密码或连接串。PostgreSQL Entra管理员对象信息必须从受保护部署变量注入；默认空值只用于安全编译，不代表可用数据库。数据库应用角色、`DATABASE_URL`、固定Salt和Master Key必须通过受控引导流程写入Key Vault，不能出现在Git、Bicep参数、部署输出或命令日志中。

Stage5 Kustomize组件使用Secrets Store CSI和Workload Identity。由于LiteLLM当前通过环境变量读取Master Key、Salt和数据库URL，组件暂时启用逐项Kubernetes Secret同步作为显式补偿控制；禁止`envFrom`，并要求最小RBAC、etcd加密、轮换和未授权读取测试。Redis使用Entra Token刷新，不保存Access Key。

本地运行`make validate-stage5`执行Stage4回归、Bicep编译、Kustomize渲染和Secret仓库检查。该命令不部署Azure或Kubernetes资源。
