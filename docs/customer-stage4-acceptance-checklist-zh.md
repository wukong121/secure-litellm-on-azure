# Stage4私网、AKS身份与入口验收操作指南

> 核对日期：2026-09-16。适用于既有LiteLLM迁移路径，对应[主执行手册的Stage4](customer-migration-guide-zh.md#阶段4私网private-aks与身份)。
>
> 五项检查：`private_dns_egress`、`private_runner`、`target_image_signature_sbom`、`workload_identity`、`private_ingress`。本文指导人工审核实际证据，没有新增验收豁免，不自动签发passed，也不代表数据库、应用、Entra登录或业务切流已经完成。

阅读顺序：准备与证据包 → 私网DNS/出口 → Runner → 目标镜像 → Workload Identity → 私有入口 → draft/confirm。先完成主手册中适用的Stage4部署、证书、Runner连接、命名空间初始化和入口操作；draft可提前生成，但五项全部实际核验后才能confirm。

## 1. 准备、执行范围与证据包

### 1.1 五项检查分别证明什么

| 检查ID | 必须核对的事实 | 不能用什么替代 |
| --- | --- | --- |
| private_dns_egress | Private AKS、ACR Private Endpoint及Private DNS与批准网络一致；实际Runner通过OIDC和私网路径读取目标AKS；AKS受控出口配置与批准方案一致 | ARM资源存在、DNS链接Completed、个人电脑能访问，或仅有Stage3 ACR管理面检查 |
| private_runner | 本次作业确实落在批准的self-hosted Runner；工具、Python/Node、Docker、Azure范围和目标AKS只读访问实际通过 | Runner显示Idle、SSH登录成功、个人`az login`，或仅有机器规格截图 |
| target_image_signature_sbom | Azure派生runtime在目标私有ACR中以digest固定；目标digest已私网拉取、生成SBOM、通过当前Trivy策略并带正确GitHub OIDC签名注解 | Stage3公共源扫描、SBOM artifact右侧的GitHub artifact digest、镜像tag，或只有`cosign sign`日志而没有适用的验证依据 |
| workload_identity | AKS OIDC/WI、目标UAMI、唯一正确Federated Credential和模型角色范围一致；绑定ServiceAccount的实际正向Token/模型访问及未绑定主体拒绝得到验证 | UAMI/Federated Credential存在、静态YAML通过、kubelet身份可调用，或节点VMSS Managed Identity |
| private_ingress | 两个证书材料、两套Traefik、两个不同内部LB前端及批准入口子网一致；TLS指纹/域名与跨平面Host拒绝通过，当前无未处理LB同步错误 | S4-11 plan、LB资源存在、HTTP 502/404本身，或Stage7业务认证测试 |

Stage4/platform创建Private AKS、系统/用户/入口子网、Firewall/UDR、ACR Private Endpoint、工作负载UAMI/Federated Credential和模型数据面角色。它不部署LiteLLM应用Pod、新PostgreSQL/Redis、API/admin认证代理或Front Door。`cluster-bootstrap`只准备命名空间；`private-ingress`只建立隔离TLS入口，不证明后端业务可用。

**当前必须正视的门禁缺口：** 标准Stage4流程尚未创建使用`litellm` ServiceAccount的应用Pod，也没有独立的Workload Identity正反向探针动作。镜像晋级已在同一OIDC/ACR认证作业内完成签名后验证并生成专用摘要；旧版promotion run没有这两份验证文件，不能追溯当作新证据。因此，客户没有实际WI正反向探针时，`workload_identity`必须保持待核验，不能仅为进入Stage5提交完整confirm。第4、5节说明证据和后续实现边界。

### 1.2 本阶段运行与artifact索引

| 来源 | artifact名称 | 主要内容与用途 |
| --- | --- | --- |
| Stage4 platform plan/deploy | `infrastructure-test-4-platform-<run ID>` | `plan-summary.json`、`reviewed-plan.json`、`deployment-receipt.json`、`deployment-outputs.json`；审核网络、AKS、身份、ACR PE和角色范围 |
| certificate-vault plan/deploy | `infrastructure-test-4-certificate-vault-<run ID>` | 证书Vault、PE、DNS、角色和输出；Secret值不在附件内 |
| runner-target-connectivity plan/deploy | `infrastructure-test-4-runner-target-connectivity-<run ID>` | `connectivity-review.json`及部署输出；绑定真实AKS/ACR PE、DNS记录和Runner VNet链接 |
| 独立AKS网络角色修复（仅需要时） | `infrastructure-test-4-aks-ingress-role-<run ID>` | 只允许控制面身份在专用目标VNet下的Network Contributor角色变化 |
| Customer private runner checks | `runner-checks-test-<run ID>` | `runner-readiness.json`；实际Runner工具、OIDC范围和所选集群只读访问 |
| Promote LiteLLM image | `litellm-sbom-<run ID>` | 加密的`litellm-sbom.spdx.json`、`target-image-verification.json`和`target-image-summary.json`；artifact名称沿用SBOM前缀 |
| S4-11/S4-12 private-ingress | `runtime-test-4-<run ID>` | `runtime-review.json`、`runtime-summary.json`、`operation-receipt.json`；入口计划、现状、证书元数据和执行结果 |
| Stage4 draft/confirm | `acceptance-draft-test-4-<run ID>`、`acceptance-record-test-4-<run ID>` | pending清单和最终人工验收账本 |

除公开页面上的固定状态外，上述客户附件均使用`WORKFLOW_ARTIFACT_KEY`加密。下载到受控机器的`temp/`独立目录，不混用不同run的`sealed-artifact.json`。附件通常保留7天；镜像SBOM当前保留30天。Git忽略不等于磁盘加密。

### 1.3 解密本阶段附件

在同一受控终端安全导入本Environment原有的artifact key；不要把密钥写进命令参数、聊天、Git或截图。下面以Runner检查为例：

```bash
set +x
umask 077
REPOSITORY="REPLACE_OWNER/REPLACE_REPOSITORY"
RUN_ID="REPLACE_RUNNER_CHECK_RUN_ID"
ARTIFACT_NAME="runner-checks-test-${RUN_ID}"
.venv/bin/python -m scripts.workflow_security open \
  --file "temp/runner-checks-${RUN_ID}/sealed-artifact.json" \
  --repository "$REPOSITORY" --run-id "$RUN_ID" --name "$ARTIFACT_NAME" \
  --output-dir "temp/stage4-runner-review-${RUN_ID}"
unset WORKFLOW_ARTIFACT_KEY
```

其他附件只替换run ID、实际下载目录和上表artifact名。使用`.venv/bin/python`可避免未激活虚拟环境时`python: command not found`。出现`Cannot authenticate workflow evidence`时，先核对原Environment key、仓库、run ID和artifact名称；GitHub不能回显Secret，不能新生成密钥解旧附件。

### 1.4 取得客户自己的值

以下是本机Bash变量，不是新增GitHub Variables。值来自客户JSON、实际部署输出和运行URL，只保存在受控记录中：

```bash
set +x
set -o pipefail
SUBSCRIPTION_ID="REPLACE_SUBSCRIPTION_ID"
TARGET_RG="REPLACE_TARGET_RESOURCE_GROUP"
TARGET_AKS="REPLACE_TARGET_AKS_NAME"
TARGET_VNET="REPLACE_TARGET_VNET_NAME"
ACR_NAME="REPLACE_TARGET_ACR_NAME"
RUNNER_VNET_ID="REPLACE_RUNNER_VNET_RESOURCE_ID"
ENVIRONMENT_NAME="test"
az account show --query '{tenantId:tenantId,subscriptionId:id}' --output json
```

| 值 | 来源与核对方法 |
| --- | --- |
| 订阅、租户、TARGET_RG | 客户JSON的azure/target字段；与当前管理身份和实际资源比较 |
| TARGET_AKS、TARGET_VNET、子网名 | parameters.platform.stage4Aks/stage4Network；再与platform输出和实际AKS比较 |
| ACR_NAME | parameters.platform.containerRegistryName；与Stage3/4输出和实际ACR比较 |
| RUNNER_VNET_ID | parameters.runner-target-connectivity；必须是实际执行作业的Runner VNet，不是开发机VNet |
| RUNTIME_CLIENT_ID/Object ID | Environment的AZURE_RUNTIME_CLIENT_ID及其企业应用/UAMI Principal ID；用于Runner检查和Kubernetes授权 |
| WORKLOAD_CLIENT_ID/Principal ID | Stage4 platform输出；是LiteLLM Pod身份，不是runtime、kubelet或控制面身份 |
| 各run ID和revision | 对应成功运行URL及40位Git SHA；promotion签名注解和后续应用验证会绑定revision/environment |

先确认Stage0至3账本仍满足环境、阶段配置、治理和7天时效。single-operator允许前序记录来自先前SHA，但本Stage证据受代码变化影响时仍须重验；双人策略继续按主手册要求处理。

## 2. private_dns_egress：私网解析、Runner路径与受控出口

### D-1 审核platform及Runner连接计划/回执

分别解密S4-01/02 platform和S4-04A/04B runner-target-connectivity附件。至少核对：

| 文件 | 通过时关注 |
| --- | --- |
| platform的plan-summary/reviewed-plan | stage=4、component=platform、环境/revision/config/plan一致；无Delete/Unsupported；变更限定在批准目标、模型账号授权和相关节点RG范围 |
| platform的deployment-outputs | `stage4Deployed=true`；Private AKS、目标VNet、ACR、工作负载身份名称/Client ID/Principal ID与批准配置一致 |
| connectivity-review.json | `aksResourceId`、`apiHostname`、AKS PE IP/zone及A记录一致；`acrResourceId`、`acrLoginServer`、ACR PE IP/zone及登录/data记录一致；Runner VNet正确 |
| runnerTargetConnectivity输出 | 管理的AKS/ACR链接均Completed；`managed`、`reused`或`external`与批准DNS归属一致 |

`managed`只表示本组件管理固定链接；`reused`/`external`要求网络Owner提供现有链接或企业DNS转发证据。任何一种都必须以实际Runner解析/访问为准。DNS链接不会创建Peering、路由、NSG或Firewall规则。

### D-2 只读核对AKS、ACR和目标网络

在符合客户条件访问策略的管理终端执行，不要求Runner保存个人`az login`：

```bash
az aks show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" --name "$TARGET_AKS" \
  --query '{id:id,state:provisioningState,power:powerState.code,private:apiServerAccessProfile.enablePrivateCluster,publicFqdn:apiServerAccessProfile.enablePrivateClusterPublicFQDN,privateFqdn:privateFqdn,nodeResourceGroup:nodeResourceGroup,oidc:oidcIssuerProfile.enabled,workloadIdentity:securityProfile.workloadIdentity.enabled,pools:agentPoolProfiles[].{name:name,subnet:vnetSubnetId}}' \
  --output json --only-show-errors
az acr show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" --name "$ACR_NAME" \
  --query '{id:id,state:provisioningState,sku:sku.name,loginServer:loginServer,publicNetworkAccess:publicNetworkAccess,adminUserEnabled:adminUserEnabled}' \
  --output json --only-show-errors
```

AKS应Running/Succeeded、private=true、无获准公网FQDN，OIDC和Workload Identity启用，节点池位于批准子网。ACR应Premium/Succeeded、publicNetworkAccess=Disabled、adminUserEnabled=false。再按[主手册4-B1](customer-migration-guide-zh.md#4-b1-runner到新aks的dns连接)核对真实PE/NIC、A记录和Runner VNet链接；不要靠hosts、裸IP或打开公网修复解析。

核对UDR和Firewall管理面，名称从platform计划/实际资源取得，不照抄示例：

```bash
ROUTE_TABLE="REPLACE_STAGE4_ROUTE_TABLE_NAME"
FIREWALL_NAME="REPLACE_STAGE4_FIREWALL_NAME"
az network route-table route list --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --route-table-name "$ROUTE_TABLE" \
  --query '[].{name:name,prefix:addressPrefix,nextHopType:nextHopType,nextHopIp:nextHopIpAddress,state:provisioningState}' --output json
az network firewall show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" --name "$FIREWALL_NAME" \
  --query '{id:id,state:provisioningState,sku:sku,policy:firewallPolicy.id,ips:ipConfigurations[].privateIPAddress}' --output json
```

输出须与批准出口方案和plan一致。资源Succeeded只证明管理面配置，不证明所有FQDN已允许或Pod能调用模型。

### D-3 从实际Runner验证目标AKS路径

运行`Customer private runner checks`：受保护main、environment=test、`check_target=true`、`check_backup=false`。确认作业分配到批准Runner且成功，解密`runner-checks-test-<run ID>`后检查：

```bash
REPORT="temp/stage4-runner-review-REPLACE_RUN_ID/runner-readiness.json"
jq '{environment,revision,observedAt,status,checks:[.checks[]|{name,status}],notCovered,stageAccepted}' "$REPORT"
```

`runner-tools`、`runtime-versions`、`docker-daemon`、`azure-scope`、迁移路径下的`legacy-cluster-read`及`target-cluster-read`应passed。`target-cluster-read`由同一次OIDC登录取得非admin kubeconfig并真实列举目标Deployment；个人管理员kubectl成功不能替代。

### D-4 明确本阶段出口验证边界

当前Stage4没有LiteLLM应用Pod，因此platform/Runner检查不能证明Pod使用工作负载身份成功调用每个模型，也不能证明所有Firewall FQDN或失败模式。若客户把`private_dns_egress`定义为必须包含应用Pod数据面访问，应在confirm前运行获批、无业务正文的有界探针并记录目标、身份、私网IP/TLS、允许与拒绝结果；当前仓库没有该独立按钮，未做就保持待核验。正式后台模型/Redis/PG路径仍在Stage5/6实测。

**private_dns_egress通过标准：** platform及连接计划/回执与实际资源一致；AKS/ACR保持私网；真实Runner通过正确OIDC和私网路径访问目标AKS；路由/Firewall符合批准方案；所有客户定义为Stage4必需的数据面探针已有真实结果。仅有DNS链接或ARM绿勾不能通过。

## 3. private_runner：执行机、工具与OIDC实际调用

### R-1 核对作业确实使用批准Runner

在成功Runner checks和Stage4 runtime作业页面查看Job的Runner名称/标签，与仓库Settings → Actions → Runners及Repository Variable `MIGRATION_PRIVATE_RUNNER_LABELS`比较。Runner应为客户专用Linux x64机器，不接收公共PR或其他不可信仓库作业；标签只是路由条件，不是授权边界。

按[Runner准备说明](customer-private-runner-preparation-zh.md#7-准备完成后怎样验收)核对VM、工作盘、Docker data-root、systemd服务和实际工具版本。SSH管理员能运行命令不代表`actions-runner`服务账户具有相同PATH、组或磁盘权限。

### R-2 审核runner-readiness实际结果

使用D-3报告核对Python 3.13、Node 24、Docker daemon、工具清单、OIDC租户/订阅和目标集群读取。报告的`status=passed`且没有failed项；`stageAccepted=false`正常，表示该workflow不签发Stage4验收。

检查`notCovered`，不能删除或忽略其含义。当前检查不覆盖写权限、Pod exec/cp、Vault/ACR/PG/Graph数据访问、模型调用、完整恢复或全部迁移就绪。其他Stage4动作成功可补充对应证据，但不能把Runner检查本身描述成“全部权限通过”。

### R-3 核对清理和运行稳定性

确认作业期间VM未被停止/重启，工作盘空间满足镜像构建和证据处理；运行后没有把kubeconfig、ACR临时认证或明文附件保存到共享目录。workflow会清理自身临时路径，但不保证整个持久VM、Docker层或异常中断残留全部清理；按客户主机策略做受控检查，不执行无范围的全局prune。

**private_runner通过标准：** 实际作业命中批准Runner，服务账户工具/版本/Docker/OIDC范围和目标AKS读取均通过，网络与磁盘满足本阶段动作，未覆盖项及残留处理有真实记录。Idle、SSH或个人登录单独不能通过。

## 4. target_image_signature_sbom：目标digest、拉取、扫描与签名

### I-1 核对promotion输入和运行绑定

本阶段Azure runtime运行`Promote LiteLLM image`时应使用：目标environment、客户配置中的Premium ACR、仓库固定源digest、独立`target_tag`（例如`litellm-azure:rehearsal-1`）、`build_azure_runtime=true`、`build_auth_proxy=false`。两个build选项不能同时选择。

记录成功promotion run URL、run ID、40位Git SHA、environment和表单输入。派生Dockerfile固定基础镜像；build模式下`source_image`字段仍须是digest格式，但不改变Dockerfile的FROM。目标tag是可变标签，验收只使用最终`@sha256:`引用。

### I-2 记录真正的目标镜像引用

在成功Job最后的`Require manifest update through pull request`步骤复制：

```text
Promoted immutable image: REPLACE_ACR.azurecr.io/litellm-azure@sha256:REPLACE_DIGEST
```

该完整值才是后续`application.backendImage`候选。GitHub Artifacts页面右侧`sha256:...`是加密SBOM artifact的摘要，**不是镜像digest**；artifact名称`litellm-sbom-<run ID>`也不是镜像引用。管理终端无法访问私有ACR数据面时，不要打开公网查询，使用成功作业输出或实际私网Runner核验。

### I-3 解密并审核目标镜像证据

下载`litellm-sbom-<run ID>`，使用其精确artifact上下文解密：

```bash
set +x
umask 077
REPOSITORY="REPLACE_OWNER/REPLACE_REPOSITORY"
RUN_ID="REPLACE_PROMOTION_RUN_ID"
ARTIFACT_NAME="litellm-sbom-${RUN_ID}"
.venv/bin/python -m scripts.workflow_security open \
  --file "temp/litellm-sbom-${RUN_ID}/sealed-artifact.json" \
  --repository "$REPOSITORY" --run-id "$RUN_ID" --name "$ARTIFACT_NAME" \
  --output-dir "temp/stage4-image-review-${RUN_ID}"
unset WORKFLOW_ARTIFACT_KEY
jq '{spdxVersion,packageCount:(.packages|length),name}' \
  "temp/stage4-image-review-${RUN_ID}/litellm-sbom.spdx.json"
jq '{schemaVersion,check,runId,repository,ref,revision,environment,image,runtime,signatureVerified,vulnerabilityPolicy,evidenceSha256,observedAt,stageAccepted,notCovered}' \
  "temp/stage4-image-review-${RUN_ID}/target-image-summary.json"
jq '{jsonType:type,signatureCount:(if type == "array" then length else 1 end)}' \
  "temp/stage4-image-review-${RUN_ID}/target-image-verification.json"
```

SBOM应为有效SPDX且packages非空。摘要的check应为`target_image_signature_sbom`，run/repository/ref/revision/environment/image与本次成功运行一致，runtime=azure、signatureVerified=true、两个证据文件哈希均为64位SHA256，stageAccepted=false。verification须为有效、非空Cosign JSON。promotion还应确认`Pull promoted image`、`Generate SBOM`、`Block critical vulnerabilities`、`Sign promoted digest`和`Verify promoted digest signature`步骤成功；当前Trivy策略为阻断可修复CRITICAL（`--severity CRITICAL --ignore-unfixed`），不是零漏洞保证。

### I-4 验证签名及当前实现缺口

Azure runtime签名必须绑定：

- certificate identity：`https://github.com/OWNER/REPOSITORY/.github/workflows/promote-litellm-image.yml@refs/heads/main`
- OIDC issuer：`https://token.actions.githubusercontent.com`
- `llmgw.runtime=azure`
- `llmgw.revision=<promotion的40位Git SHA>`
- `llmgw.environment=test`（替换为实际环境）

更新后的promotion workflow在同一个GitHub OIDC登录和临时`az acr login`上下文中，先签名，再以以上合同运行`cosign verify`；验证成功后才写入`target-image-verification.json`和`target-image-summary.json`。Runner VM按设计没有业务Managed Identity，交互账户也不保留Azure登录，因此不要在普通SSH shell直接运行Cosign并配置永久ACR密码；出现`UNAUTHORIZED: authentication required`只说明该shell没有ACR pull凭据，不是签名不匹配。

旧run若artifact只有`litellm-sbom.spdx.json`，说明运行版本早于内置验证。合并更新后的workflow后重新执行promotion，使用新run的revision、目标digest和三份加密证据；不要给旧run手工补一份脱离运行上下文的结果。Stage6 application plan仍会再次使用相同合同验证当前配置的镜像。

**target_image_signature_sbom通过标准：** 成功run、代码/environment、完整目标digest、私网拉取、非空SBOM、当前漏洞策略、非空验证JSON和signatureVerified摘要形成同一认证证据链；artifact digest与镜像digest没有混用。缺任一验证文件时不确认。

## 5. workload_identity：身份对象、联邦合同与正反向访问

### W-1 核对AKS及platform输出

从Stage4 platform部署输出读取`workloadIdentityName`、`workloadIdentityClientId`和`workloadIdentityPrincipalId`，同时确认`stage4Deployed=true`。三者属于LiteLLM工作负载UAMI，不是AKS控制面身份、kubelet身份、GitHub runtime身份或旧VMSS业务身份。

只读核对AKS开关和UAMI：

```bash
WORKLOAD_IDENTITY_NAME="REPLACE_WORKLOAD_IDENTITY_NAME"
az aks show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" --name "$TARGET_AKS" \
  --query '{oidc:oidcIssuerProfile,workloadIdentity:securityProfile.workloadIdentity,aad:aadProfile}' --output json
az identity show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" --name "$WORKLOAD_IDENTITY_NAME" \
  --query '{id:id,name:name,clientId:clientId,principalId:principalId,tenantId:tenantId}' --output json
az identity federated-credential list --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --identity-name "$WORKLOAD_IDENTITY_NAME" \
  --query '[].{name:name,issuer:issuer,subject:subject,audiences:audiences}' --output json
```

应只有获批联邦项，issuer等于实际AKS OIDC issuer，subject精确为`system:serviceaccount:litellm:litellm`，audiences恰好为`["api://AzureADTokenExchange"]`。错误namespace/account、额外issuer/subject或空ID均不通过。

### W-2 核对模型角色范围

对`azureOpenAIConnections`中的每个模型账号，用WORKLOAD_OBJECT_ID等于platform输出Principal ID执行只读查询：

```bash
WORKLOAD_OBJECT_ID="REPLACE_WORKLOAD_IDENTITY_PRINCIPAL_ID"
MODEL_ACCOUNT_SCOPE="REPLACE_MODEL_ACCOUNT_RESOURCE_ID"
MODEL_SUBSCRIPTION_ID="REPLACE_MODEL_SUBSCRIPTION_ID"
az role assignment list --subscription "$MODEL_SUBSCRIPTION_ID" \
  --assignee-object-id "$WORKLOAD_OBJECT_ID" --scope "$MODEL_ACCOUNT_SCOPE" \
  --include-inherited --fill-principal-name false \
  --query '[].{role:roleDefinitionName,roleDefinitionId:roleDefinitionId,scope:scope,principalId:principalId,condition:condition}' \
  --output json
```

核对`Cognitive Services OpenAI User`及批准账号scope，不授予部署管理员、账号Key读取或其他模型账号权限。角色显示存在不证明Token交换、Private Endpoint或模型调用成功。

### W-3 实际正向和负向验证

通过标准至少需要以下无敏感测试记录：

1. **正向：** 使用精确`litellm/litellm` ServiceAccount和对应client ID的受控Pod取得认知服务Token，并通过批准私网FQDN完成一条有预算上限的模型请求；不打印Token、Key或Prompt正文。
2. **负向：** 同namespace未绑定ServiceAccount或错误client ID不能取得该身份Token/调用同一模型；不能用故意破坏生产Deployment来测试。
3. **身份归属：** 上游审计/调用元数据对应WORKLOAD_OBJECT_ID，而不是kubelet、节点或GitHub runtime身份。
4. **清理：** 临时Pod、Role及诊断文件按批准范围清理，保留元数据证据；不得上传ServiceAccount Token。

当前Stage4流程没有创建该应用Pod或提供有界probe workflow；静态`workload-identity-patch.yaml`只证明模板合同。由平台Owner提供经过审核的临时探针或先补自动化；未执行W-3就不能确认此项。Stage6真实应用还需继续验证Token刷新、重连、Redis/PG和多模型路由，不能把一次探针扩展为全部生产能力。

**workload_identity通过标准：** AKS开关、UAMI/Federated Credential、模型角色和实际正负向访问全部匹配，且没有回退到节点/开发者身份。仅有ARM对象或静态YAML不能通过。

## 6. private_ingress：证书、双入口、LB与隔离

### P-1 审核S4-11计划和当前状态

解密成功S4-11的`runtime-test-4-<run ID>`，审核：

| 文件/字段 | 通过时关注 |
| --- | --- |
| runtime-review.json | stage/action/revision/config/plan一致；cluster为目标Private AKS；subnet是批准入口子网且PLS策略Disabled |
| certificates.api/admin | 两个Secret来自批准证书Vault、解析到具体版本，sha256/expiresAt与本地批准材料一致；附件不含私钥 |
| documents | API/admin分属`llm-api-ingress`/`llm-admin-ingress`，固定Traefik digest、独立TLS Secret、明确source ranges和默认拒绝网络策略 |
| currentIngressObservationNotPlanBound | 已有Service、EndpointSlice、LB status和最近事件；动态观察不参与plan hash，但必须用于发现部分部署/新错误 |

plan只做证书读取、信任检查、Kubernetes server-side dry-run和现状读取，不发布入口。旧计划、失败execute或仅有`loadBalancerIngressCount`不能批准新的状态。

### P-2 审核S4-12执行结果与ARM回执

S4-12必须引用审核过的新S4-11 plan ID。解密执行artifact，`runtime-summary.json`应有`applied=true`、`verified=true`；`endpoints.api/admin`各自包含不同privateIpAddress，均位于批准入口子网，并映射到恰好一个Standard内部LB frontend。

核对保存的ARM输出：

```bash
az deployment group show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "llmgw-${ENVIRONMENT_NAME}-s4-private-ingress" \
  --query '{state:properties.provisioningState,timestamp:properties.timestamp,ingress:properties.outputs.privateIngress.value}' \
  --output json
```

输出revision/configSha256/证书指纹/verifiedAt与本次执行一致，API/admin LB资源、frontend和IP不相同。不要把该回执当成持续健康证明；当前Service/事件仍需复核。

### P-3 核对TLS、Host隔离和当前LB状态

执行流程已从实际Runner连接两个私网IP：校验证书叶指纹、域名/SNI和系统信任链，并向每个入口发送另一个平面的Host，要求404或421。确认这些步骤成功且admin私有CA已安装在实际Runner信任库。

再次运行新的S4-11 plan可读取现状，不执行apply。审核`ingressObservation`和详细events：两个Service应存在、各有一个LB IP、EndpointSlice有ready endpoint，修复之后不再增长`SyncLoadBalancerFailed`/`AuthorizationFailed`/`LinkedAuthorizationFailed`。历史事件可以保留，但必须按first/last timestamp区分旧错误与当前错误。

`loadBalancerSourceRanges`和NetworkPolicy必须覆盖批准Runner/未来PLS来源且不含未批准大网段。开发机直连成功不是必要条件；admin入口不得公开。不要为调试开放公网LB、共享API/admin前端或关闭TLS验证。

### P-4 明确入口验证边界

Stage4入口后端API/admin代理尚未部署，正常Host可能返回502；这不等于TLS/LB失败，也不能作为业务成功。Stage4不验证Entra登录、双凭据API、模型响应、原生UI、Prompt日志或Front Door源站，这些在Stage6至9继续完成。错误Host拒绝只证明当前路由隔离，不证明所有绕过路径不存在。

**private_ingress通过标准：** S4-11/12计划与执行绑定；两份批准证书、两个不同私网LB前端、入口子网、TLS指纹/域名/信任和跨平面Host拒绝全部通过；当前Endpoint/LB状态正常且无未处理同步错误。业务认证和后端调用另行验收。

## 7. 保存记录并完成draft/confirm

### 7.1 五项证据记录表

| 检查ID | 应关联的证据 | 时间/代码/范围 | 实际结果与核验人 |
| --- | --- | --- | --- |
| private_dns_egress | platform及connectivity plan/deploy、AKS/ACR/PE/DNS/UDR/Firewall、Runner实际目标读取及客户要求的出口探针 | 待填写 | 待核验 |
| private_runner | runner-checks run/report、实际Runner/标签、工具/版本/Docker/OIDC/目标AKS、残留检查 | 待填写 | 待核验 |
| target_image_signature_sbom | promotion run/SHA/输入、完整目标image@digest、目标拉取、SBOM/Trivy、签名验证 | 待填写 | 待核验 |
| workload_identity | platform输出、AKS OIDC/WI、UAMI/Federated Credential、模型角色、正负向实际调用 | 待填写 | 待核验 |
| private_ingress | S4-11/12、证书版本/指纹、双LB/IP、TLS/Host拒绝、当前Endpoint/events及ARM回执 | 待填写 | 待核验 |

记录保存在客户受控位置。公开`evidence_notes`只引用允许公开的编号和结论，不放内部IP、完整资源清单、Token、Key、证书内容、Prompt或原始错误响应。

### 7.2 生成Stage4 draft

Actions → `Customer stage acceptance` → Run workflow：main、environment=test、stage=4、operation=draft；`reviewed_run_id`、`checked_items`、`evidence_notes`、`confirm_environment`全部留空。成功后记录DRAFT_RUN_ID并查看Summary的五个检查ID。

draft只生成pending清单，不自动读取promotion、Runner、平台或入口结果。生成后若Stage4配置、证书版本、镜像digest、代码或实际资源状态变化，复核证据并按主手册判断是否需要新draft/plan。

### 7.3 confirm页面填写

以下沿用已批准single-operator路径；双人策略继续使用主手册record流程。

| 页面字段/说明 | confirm时填写 |
| --- | --- |
| Use workflow from / environment / stage | main / test / 4，与有效draft一致 |
| operation | confirm |
| For confirm, successful draft run ID for this stage and revision | DRAFT_RUN_ID；不是platform、Runner、promotion或S4-11/12 run ID |
| For confirm, every check ID personally verified; comma separated as shown in the draft Summary | 五项全部实际通过后填下面字符串，以draft实际输出为准 |
| For confirm, actual observations and evidence references; no secrets or prompt content | 普通文本，12至4000字符；逐项真实结果和允许公开的证据编号，不是JSON |
| For confirm, type the selected environment again | test |

```text
private_dns_egress,private_runner,target_image_signature_sbom,workload_identity,private_ingress
```

`evidence_notes`应概括：私网AKS/ACR/DNS/出口及Runner实际检查；目标runtime digest、SBOM/扫描/私网拉取和签名验证；WI对象/角色与正负向调用；证书、双入口LB、TLS/Host隔离和当前事件。尚待Stage5/6/7验证的数据库、完整模型路由、企业登录和业务协议应明确保持后续范围，不写成已经通过。

**当前confirm不支持部分通过。** 任一私网路径失败、Runner检查失败、镜像digest或签名未验证、WI正反向探针缺失、入口仍有同步错误时，不提交完整confirm，也不少填ID绕过。当前仍缺少标准WI probe，必须先补真实证据或改进自动化。

成功后生成`acceptance-record-test-4-<run ID>`加密账本，后续workflow自动读取，不更新`MIGRATION_EVIDENCE_JSON`。`independentlyVerified=false`表示这是操作者记录，不是独立审计、Stage5数据平台完成或业务切流批准。

## 8. 常见阻塞、重跑边界与进入Stage5条件

| 情况 | 正确处理 |
| --- | --- |
| Runner解析AKS/ACR到公网或无法解析 | 按4-B1修复Runner DNS链接/企业转发；不改hosts、不开放AKS/ACR公网 |
| ACR管理面成功但本机manifest查询403 | 管理机不在私网是预期边界；从实际Runner/workflow验证，不临时开放ACR |
| promotion只有SBOM artifact digest | 从成功Job记录`Promoted immutable image`；两种digest不能互换 |
| promotion签名成功但artifact无verify结果 | 这是旧版run；合并更新后重新运行promotion，使用新run三份证据，不在Runner交互shell保存永久ACR凭据 |
| UAMI和联邦项存在但没有Pod实测 | 不能确认workload_identity；先提供获批正负向探针，不用kubelet/节点身份代替 |
| `lb-ready-api`超时 | 新S4-11读取Service events；按实际Authorization/LinkedAuthorization/网络错误修复，不能复用旧plan |
| API入口成功、admin尚未创建 | execute可能部分完成；重新plan审核当前状态，不假定回滚、不手工复制API IP |
| 正常Host返回502 | Stage4后端尚未发布时可能正常；仍须确认TLS/LB/Host隔离，业务正向在后续Stage验证 |
| 代码或配置改变 | 未执行的旧plan失效，重新plan；前序账本按治理、指纹、时效和实际影响复核，不改旧哈希 |

进入Stage5前必须满足：五项真实完成并confirm；目标镜像完整digest已保管供Stage6配置；Stage0备份引用和原Master/Salt仍可恢复；PG/Redis/数据库身份参数已按Stage2批准方案准备；没有重放Stage0网络模板覆盖Stage4 VNet。Stage4通过仍不允许启用业务流量。