# LiteLLM Azure IaC 结构

## 目录边界

- `modules/`：可复用Azure资源模块；
- `environments/`：`dev/test/prod`环境入口与参数；
- `backup-storage/`：阶段0已部署的备份存储，暂保持独立以避免重构已运行资源；
- `monitoring/`：阶段1已部署的最小告警，暂保持独立。

阶段3只建立模块、环境参数和验证门禁，不部署阶段4/5的Private AKS、Key Vault、PostgreSQL、Redis、Private Endpoint或WAF。

## ACR模块

`modules/container-registry`声明Premium ACR，并默认：

- 禁用Admin User；匿名拉取保持服务默认关闭，并在部署后验收；
- 禁用公网访问；
- 禁止网络绕过；
- 禁止镜像导出；
- 开启未标记manifest保留。ACR Soft Delete需在目标API和区域能力确认后单独启用。

环境入口中的 `deployContainerRegistry` 默认均为 `false`。原因是公网禁用的ACR必须先完成Private Endpoint、Private DNS和受控构建/导入执行路径。阶段4网络设计批准前不得为了方便把生产ACR长期开放公网。

## 参数与Secret

环境参数使用 `readEnvironmentVariable('LITELLM_ACR_SUFFIX', 'stage3check')`生成名称。`stage3check`只用于编辑器和静态编译，实际创建ACR前流水线必须显式注入全局唯一后缀。该后缀不是Secret，但由部署环境管理，避免仓库绑定单一订阅。

任何Secret、Token、Client Secret、数据库密码和连接串都不得写入Bicep或`.bicepparam`。目标值通过Key Vault和Workload Identity提供。

## 验证

在仓库根目录执行 `scripts/validate-stage3.sh`。脚本只执行静态编译、参数编译、Kustomize渲染、YAML策略检查和单元测试，不部署云资源。

云端What-if只能通过手动触发的工作流执行，并使用GitHub OIDC。生产部署不包含在阶段3自动流水线中。

## 阶段4网络和Private AKS模块

阶段4模块已经加入，但所有环境参数的`deployStage4`仍为`false`，当前不会创建云资源。

网络规划：

| 用途 | 子网/CIDR |
| --- | --- |
| Azure Firewall | `AzureFirewallSubnet` / `10.30.0.0/26` |
| AKS System Pool | `snet-aks-system` / `10.30.1.0/24` |
| AKS User Pool | `snet-aks-user` / `10.30.2.0/23` |
| Private ingress/PLS | `snet-private-ingress` / `10.30.4.0/24` |
| Private Endpoints | 已存在 `snet-private-endpoints` / `10.30.8.0/24` |
| Pod CIDR | `10.244.0.0/16` |
| Service CIDR | `10.31.0.0/16` |

阶段4模块包括：

- Azure Firewall Premium、Policy、显式HTTPS allowlist和UDR；
- Private AKS、禁用Local Accounts、Entra Azure RBAC、Cilium、OIDC和Workload Identity；
- System/User Node Pool分离和多节点；当前冻结区域West US不支持AKS可用区，因此不声明`availabilityZones`；
- ACR及Azure OpenAI Private Endpoint和Private DNS；
- LiteLLM专用UAMI及Federated Identity Credential；
- 跨订阅Azure OpenAI最小数据平面RBAC。

Private AKS创建模板保留控制面Diagnostic Settings和Defender，但不在`addonProfiles`中直接启用Container Insights。当前订阅的AKS创建预检无法通过现有workspace解析；Container Insights应在集群创建成功后通过独立DCR/DCRA监控模块启用并验证数据流，避免把集群创建和日志代理故障耦合。

真实Azure OpenAI资源ID、订阅和资源组不会进入提交的参数文件，必须在受保护的What-if/部署变量中注入。

启用`deployStage4=true`前必须确认：

1. 全部CIDR与客户Hub、本地网络和其他订阅不重叠；
2. West US所选AKS版本、AzureLinux和VM SKU可用；客户若要求跨可用区，必须改选支持Availability Zones的区域并重新评估数据驻留和成本；
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

当前目标资源组已存在Key Vault和PostgreSQL Private DNS Zone，因此模块默认复用且不修改其Tags；Redis Zone默认由阶段5创建。全新资源组必须显式启用环境入口中的对应`createStage5*PrivateDnsZone`参数，或由中心网络团队预先创建共享Zone。

IaC不会创建任何Key Vault Secret值，也不会接收数据库密码或连接串。PostgreSQL Entra管理员对象信息必须从受保护部署变量注入；默认空值只用于安全编译，不代表可用数据库。数据库应用角色、`DATABASE_URL`、固定Salt和Master Key必须通过受控引导流程写入Key Vault，不能出现在Git、Bicep参数、部署输出或命令日志中。

Stage5 Kustomize组件使用Secrets Store CSI和Workload Identity。由于LiteLLM当前通过环境变量读取Master Key、Salt和数据库URL，组件暂时启用逐项Kubernetes Secret同步作为显式补偿控制；禁止`envFrom`，并要求最小RBAC、etcd加密、轮换和未授权读取测试。Redis使用Entra Token刷新，不保存Access Key。

本地运行`make validate-stage5`执行Stage4回归、Bicep编译、Kustomize渲染和Secret仓库检查。该命令不部署Azure或Kubernetes资源。
