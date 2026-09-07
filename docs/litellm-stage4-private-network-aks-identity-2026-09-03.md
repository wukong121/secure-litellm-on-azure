# LiteLLM 安全增强阶段 4：私有网络、Private AKS 与 Workload Identity

> 文档状态：设计、IaC和静态/What-if验证完成；云资源尚未部署  
> 完成日期：2026-09-03  
> 前置条件：阶段3代码Gate通过  
> 目标：在不修改当前生产环境的前提下，建立新Private AKS网络、身份、ACR和模型私网连接的可审查IaC  
> 明确边界：不创建或修改APIM；不移除当前VMSS业务UAMI；不禁用现有模型公网；不部署阶段5数据服务

## 1. 执行摘要

阶段4代码改造已完成：

- 复用阶段0已部署的`litellm-security-vnet`，不重新声明父VNet；
- 新增Azure Firewall、AKS System/User、Private ingress子网规划；
- 新增Azure Firewall Premium、Policy、显式FQDN出口和UDR；
- 新增Private AKS模块；
- 启用Entra Azure RBAC、禁用Local Accounts、OIDC、Workload Identity和Cilium；
- System/User Node Pool分离，多节点且不分配节点公网IP；
- 新增LiteLLM专用UAMI与Federated Identity Credential；
- 新增ACR Premium Private Endpoint、Private DNS和AcrPull；
- 新增Azure OpenAI Private Endpoint和跨订阅最小数据平面RBAC模块；
- 新增Default Deny、DNS/Private Endpoint/HTTPS出口和IMDS阻断NetworkPolicy组件；
- 新增AKS控制面和Firewall诊断设置；
- 新增阶段4静态安全和仓库门禁。

所有dev/test/prod参数的`deployStage4=false`，当前默认不会创建资源。

启用场景What-if已成功：Create 24、Modify 0、Delete 0、Ignore 54、Unsupported 1。现有VNet父资源、Private Endpoint子网、Blob Private Endpoint、APIM和当前AKS均保持不变。

## 2. 当前环境基线

当前已部署：

- `litellm-security-vnet`：`10.30.0.0/16`；
- `snet-private-endpoints`：`10.30.8.0/24`；
- Blob Private Endpoint：1个；
- Blob Private DNS Zone及VNet Link；
- 当前公网AKS和节点级LiteLLM UAMI继续运行。

当前目标Premium ACR不存在。当前AKS仍是公网控制面、Local Accounts未禁用、Azure RBAC未启用、NetworkPolicy未启用、`outboundType=LoadBalancer`。OIDC和Workload Identity集群能力存在，但生产LiteLLM Pod尚未使用。

## 3. 网络规划

| 功能 | 名称 | CIDR |
| --- | --- | --- |
| VNet | `litellm-security-vnet` | `10.30.0.0/16` |
| Azure Firewall | `AzureFirewallSubnet` | `10.30.0.0/26` |
| AKS System Pool | `snet-aks-system` | `10.30.1.0/24` |
| AKS User Pool | `snet-aks-user` | `10.30.2.0/23` |
| Private ingress/PLS | `snet-private-ingress` | `10.30.4.0/24` |
| Private Endpoints | `snet-private-endpoints` | `10.30.8.0/24` |
| Pod overlay | - | `10.244.0.0/16` |
| Kubernetes Service | - | `10.31.0.0/16` |
| DNS Service IP | - | `10.31.0.10` |

静态检查确认上述子网互不重叠，Pod CIDR、Service CIDR和VNet互不重叠。但只能证明当前仓库规划内部一致，不能证明与客户本地网络、Hub、其他订阅和未来网络不重叠。部署前必须由网络Owner批准。

阶段4只以`existing`引用父VNet，通过子资源新增子网，不重新提交VNet的`subnets`数组。因此What-if不会修改或删除现有Private Endpoint子网。

## 4. Azure Firewall与出口治理

目标配置：

- Azure Firewall Premium；
- Firewall Policy；
- DNS Proxy；
- Threat Intelligence Alert；
- IDPS Alert；
- AKS子网默认路由到Firewall；
- HTTPS仅允许审核FQDN；
- Azure DNS TCP/UDP 53；
- NTP UDP 123；
- Firewall日志和指标进入现有Log Analytics。

基础FQDN覆盖AKS控制面、MCR、Microsoft包源和Azure环境管理/登录主机。该清单仍需通过新集群实际启动、镜像拉取、OIDC、证书验证和模型调用观测补全。

当前规则处于“代码基线”，未部署。TLS Inspection未启用。

## 5. Private AKS

Private AKS目标：

- Private Cluster；
- 不创建Public FQDN；
- 禁用Run Command；
- 禁用Local Accounts；
- Entra integration + Azure RBAC；
- Azure CNI Overlay + Cilium dataplane/NetworkPolicy；
- `outboundType=userDefinedRouting`；
- OIDC issuer和Workload Identity；
- Azure Policy和Defender；
- Image Cleaner；
- System/User Node Pool分离；
- 每个池初始2节点且启用Autoscaler；
- 节点无公网IP；
- AKS控制面诊断和指标进入Log Analytics。

当前冻结区域West US不支持所需AKS Availability Zones，因此模板不声明`availabilityZones`。高可用依靠多节点、PDB和topology spread，但不能宣称跨可用区。如果客户要求Zone HA，必须选择支持Availability Zones的区域并重新确认数据驻留、AOAI可用性和成本。

节点SKU已根据当前订阅/West US实际支持从`Standard_D2s_v5`调整为`Standard_D2s_v3`。

AKS创建模板没有直接启用Container Insights addon。当前订阅预检无法在集群创建时解析现有workspace；集群创建后应由独立DCR/DCRA监控模块启用Container Insights，并验证`ama-logs`和数据流。控制面Diagnostic Settings和Defender仍保留。

## 6. Workload Identity

模块创建：

- LiteLLM专用UAMI；
- AKS OIDC issuer对应的Federated Identity Credential；
- Subject固定为`system:serviceaccount:litellm:litellm`；
- Audience固定为`api://AzureADTokenExchange`。

Kubernetes组件包含：

- ServiceAccount client-id注解占位符；
- Pod `azure.workload.identity/use: "true"`标签；
- `automountServiceAccountToken=false`安全默认。

真实Client ID只能由受保护Bicep输出或部署流水线注入，不能提交仓库。

迁移顺序不可改变：

1. 新Pod使用Workload Identity成功；
2. 未授权Pod获取身份失败；
3. 模型私网调用成功；
4. 完成故障和Token刷新验证；
5. 最后才移除旧VMSS业务UAMI。

阶段4代码不会修改当前VMSS身份。

## 7. ACR私网与供应链

目标：

- Premium ACR；
- 禁用Admin User；
- 公网访问默认关闭；
- Private Endpoint group `registry`；
- Private DNS `privatelink.azurecr.io`；
- 仅新AKS kubelet identity获得`AcrPull`；
- 镜像导入、SBOM、Trivy和Cosign沿用阶段3流程。

What-if中的1项Unsupported来自运行时才能确定的ACR role assignment分析，不是计划修改现有资源。正式部署后必须验证角色存在、未授权身份拉取失败，以及registry/data endpoint均可私网解析。

如果GitHub runner无法访问Private ACR，必须使用受控self-hosted runner；不长期开放公网。

## 8. Azure OpenAI/Foundry私网化

模块支持：

- Private Endpoint group `account`；
- Private DNS `privatelink.openai.azure.com`；
- 跨订阅目标资源ID；
- 每个资源独立PE；
- LiteLLM UAMI仅授予`Cognitive Services OpenAI User`。

真实订阅ID、资源ID、名称和Endpoint不进入提交的`.bicepparam`，通过受保护部署变量注入。默认`azureOpenAIConnections=[]`，因此当前What-if不创建模型Private Endpoint。

正式顺序：

1. 创建PE并由目标订阅Owner批准；
2. AKS内部DNS解析到私网地址；
3. Workload Identity调用成功；
4. 跨资源最小权限验证；
5. Chat、Responses HTTP/WebSocket、Image和Codex多轮回归；
6. 最后禁用目标资源Public Network Access和适用的Local Auth。

不得先禁公网再验证私网路径。

## 9. NetworkPolicy

阶段4组件包含：

- namespace Default Deny；
- ingress-nginx到LiteLLM 4000；
- kube-dns TCP/UDP 53；
- Private Endpoint subnet的443、5432、6380和10000；
- 公网HTTPS出口，但排除RFC1918和IMDS；
- IMDS `169.254.169.254/32`明确不可达。

公网HTTPS仍由VNet UDR和Azure Firewall FQDN allowlist进一步限制。

该NetworkPolicy没有加入当前prod overlay。原因是Key Vault、Flexible Server、Redis和最终Private Endpoint依赖尚未创建；过早应用Default Deny会造成服务中断。必须先观察依赖，再在新环境执行允许路径和拒绝路径测试。

## 10. IaC与验证结果

新增模块：

- `network-foundation`；
- `firewall-egress`；
- `aks-network`；
- `private-dns`；
- `private-aks`；
- `workload-identity`；
- `acr-private-endpoint`；
- `acr-pull-role`；
- `aoai-private-endpoint`；
- `model-access-role`。

验证：

| 验证 | 结果 |
| --- | --- |
| 阶段3回归 | 通过，13项测试 |
| CIDR静态检查 | 通过 |
| NetworkPolicy安全检查 | 通过 |
| Workload Identity patch检查 | 通过 |
| 所有Stage4 Bicep模块 | 编译通过，无诊断 |
| 环境入口及参数 | 编译通过 |
| 默认参数What-if | 不创建Stage4资源 |
| Stage4启用What-if | Succeeded |
| 启用What-if变更 | Create 24、Modify 0、Delete 0、Ignore 54、Unsupported 1 |
| 现有VNet父资源 | Ignore |
| 现有PE subnet | 无Modify/Delete |
| Blob PE | Ignore |
| APIM | Ignore |
| 当前AKS | Ignore |
| Azure/Kubernetes部署 | 未执行 |

统一验证入口：`make validate-stage4`或`bash scripts/validate-stage4.sh`。

## 11. 部署前Gate

以下全部完成前，保持`deployStage4=false`：

1. 客户网络Owner确认所有CIDR；
2. 客户确认West US无Availability Zones仍满足首期SLO，或选择支持AZ的区域；
3. GitHub OIDC、Environment审批、分支保护和required checks启用；
4. ACR Private Endpoint和self-hosted runner路径批准；
5. Firewall FQDN、端口和Service Tag清单完成评审；
6. 跨订阅AOAI Private Endpoint审批Owner和权限到位；
7. 真实Azure OpenAI连接参数通过受保护变量注入；
8. 阶段5 Key Vault、PG和Redis设计已与NetworkPolicy端口/DNS对齐；
9. 当前生产回退环境和VMSS UAMI保持不变；
10. 完整What-if再次证明无Modify/Delete现有资源；
11. 维护窗口、成本、配额和资源命名批准。

## 12. 阶段状态

阶段4的**设计、IaC和安全验证已完成**，但云资源尚未部署。可以进入部署前审批，或者并行开始阶段5 Key Vault、PostgreSQL和Redis的设计/IaC，使网络、DNS和身份参数最终对齐。

Owner决定暂不部署Stage4高成本资源。下一步先完成阶段5 Key Vault、PostgreSQL和Redis设计/IaC，使Private Endpoint、DNS、身份、端口和NetworkPolicy一次性对齐，再统一执行最终What-if和成本审批。

本阶段不应被描述为“Private AKS已经运行”。准确状态是：

> Private AKS和零信任网络代码已通过编译与What-if，现有生产资源无修改；等待网络、区域、仓库治理和跨订阅权限Gate后部署。
