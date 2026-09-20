# Stage3供应链与目标基础验收操作指南

> 核对日期：2026-09-14。适用于既有LiteLLM迁移路径，对应[主执行手册的Stage3](customer-migration-guide-zh.md#阶段3供应链与目标基础)。
>
> 三项检查：`oidc_scope`、`source_image_sbom_scan`、`target_isolation`。本文指导人工审核实际证据，没有新增workflow，不自动签发passed，不代表新环境业务已经可用。

阅读顺序：准备 → OIDC及授权范围 → 派生运行镜像报告 → 目标资源隔离 → draft/confirm。先完成Check public source image及主手册S3-01/02的platform计划、审核和部署；draft可提前生成，但三项全部实际核验后才能confirm。

## 1. 准备与执行位置

### 1.1 本阶段覆盖什么

| 检查ID | 你要核对的事实 | 不能用什么替代 |
| --- | --- | --- |
| oidc_scope | 本次基础设施作业使用正确的部署服务主体、Environment范围联邦信任及获批Azure权限，实际操作范围正确 | 个人az login成功、源镜像检查成功或仅有某个Client ID |
| source_image_sbom_scan | 固定公开源digest与本次代码一致，SBOM/扫描报告有效、哈希一致并通过当前策略 | 仅有artifact、仅看到Docker tag，或把源报告当成目标镜像签名 |
| target_isolation | Stage3变更限定在批准目标范围，实际ACR安全设置正确，既有目标资源保留且旧业务未受非预期影响 | 资源名称不同、deploy绿勾，或目标ACR暂时不可拉取 |

当前Stage3/platform创建Premium ACR，引用Stage0已有的新Log Analytics工作区；`deployStage4=false`、`deployStage5=false`。它不部署新AKS、Firewall、工作负载身份、ACR Private Endpoint或新PG/Redis，也不发布新LiteLLM应用。Stage0备份VNet/Blob PE已经存在不等于ACR私网已建立。

源镜像检查不登录Azure，也不需要私网Runner；基础设施plan/deploy使用GitHub托管Runner和`AZURE_CLIENT_ID`。本阶段人工核对主要是管理面读取和本地报告审核，不要求提前运行Stage4的check_target=true、镜像晋级、签名或私网拉取。

### 1.2 区分明文与加密附件

| workflow / artifact前缀 | 内容与打开方式 |
| --- | --- |
| Check public source image / source-image-<run ID> | **不需要解密**。公开源镜像的source-summary.json、source-sbom.spdx.json、source-scan.json，解压直接审核，不使用WORKFLOW_ARTIFACT_KEY |
| Customer infrastructure deployment / infrastructure-test-3-platform-<run ID> | 含客户资源变化，只有sealed-artifact.json是正常的；用原WORKFLOW_ARTIFACT_KEY解密后审核计划或部署回执 |
| Customer stage acceptance / acceptance-draft-test-3-<run ID>、acceptance-record-test-3-<run ID> | 加密的pending报告/验收账本；不是源镜像扫描报告，也不是基础设施plan |

以上附件当前保留7天。下载到受控机器的仓库忽略目录temp/，每次运行用独立目录，不混用不同run的文件。公开源报告明文上传是这个workflow的明确行为，不意味着客户配置、部署计划或数据库备份也可以公开。Git忽略不是磁盘加密，仍须按客户要求保管。

### 1.3 获取本次要用的值

本地文件审核不需要Azure登录；Azure查询使用符合客户设备策略、具有相应读取权限的管理身份，不需要进入Runner。以下是本机Bash变量，不是新增GitHub Variables；准备Azure CLI、jq、sha256sum和已有Python依赖，不打印凭据。

```bash
set +x
set -o pipefail
umask 077
SUBSCRIPTION_ID="REPLACE_SUBSCRIPTION_ID"
TARGET_RG="REPLACE_TARGET_RESOURCE_GROUP"
LEGACY_RG="REPLACE_LEGACY_RESOURCE_GROUP"
LEGACY_AKS="REPLACE_LEGACY_AKS_NAME"
LOG_WORKSPACE="REPLACE_TARGET_LOG_WORKSPACE"
ACR_NAME="REPLACE_TARGET_ACR_NAME"
DEPLOY_CLIENT_ID="REPLACE_AZURE_CLIENT_ID"
ENVIRONMENT_NAME="test"
az account show --query '{tenantId:tenantId,subscriptionId:id}' --output json
```

| 值 | 客户侧获取位置 | 核对方法 |
| --- | --- | --- |
| SUBSCRIPTION_ID、tenantId | 客户JSON的azure字段；Portal订阅/Entra Overview | 与当前读取身份及实际资源所属订阅/租户一致 |
| TARGET_RG、LEGACY_RG、LEGACY_AKS | 客户JSON的target/legacy字段；Stage0/1盘点记录 | 目标RG与旧RG不同，不用Runner RG代替目标RG |
| LOG_WORKSPACE、ACR_NAME | parameters.platform.logAnalyticsWorkspaceName/containerRegistryName | 再与T-1部署输出、T-2真实资源核对；新工作区沿用Stage0，不是旧监控工作区 |
| DEPLOY_CLIENT_ID | Settings → Environments → 本次环境 → AZURE_CLIENT_ID | 本次基础设施身份，不是AZURE_RUNTIME_CLIENT_ID、用户Object ID或Runner VM ID |
| DEPLOY_OBJECT_ID | O-1的企业应用Object ID，或UAMI Principal ID | 必须对应DEPLOY_CLIENT_ID；应用注册的Object ID不是服务主体ID |
| SOURCE_RUN_ID | 成功Check public source image运行URL中actions/runs/后的数字 | 只选默认分支，没有environment/stage输入；记录完整Git SHA与本次尝试时间 |
| PLATFORM_PLAN_RUN_ID / PLATFORM_DEPLOY_RUN_ID | 成功S3-01 / S3-02运行URL | stage=3、component=platform、同环境/SHA；deploy使用审核过的plan ID |
| DRAFT_RUN_ID | 本Stage成功acceptance draft运行URL | 不能填源镜像、platform plan/deploy或Stage2 draft的ID |
| SOURCE_DIR | 解压本次source-image附件后的实际目录 | 内含三份源报告；不同于基础设施解密目录 |

先核对Stage0至2的验收账本仍有效。配置/代码变更需复核受影响证据，不重建资源来“刷新账本”。有条件访问或读取权限阻塞时请相应Owner核验，不复制Actions Token、关闭安全策略或授予通用Owner兜底。

## 2. oidc_scope：登录身份、信任及权限范围

### O-1 从GitHub变量核验实际Azure主体

从本次Environment取得DEPLOY_CLIENT_ID，再在正确客户租户只读查询：

```bash
az ad sp show --id "$DEPLOY_CLIENT_ID" \
  --query '{displayName:displayName,clientId:appId,objectId:id}' --output json
```

clientId应等于GitHub变量，displayName与批准部署身份一致。把objectId记录为DEPLOY_OBJECT_ID。目录读取失败时由Entra管理员从Enterprise applications查询；UAMI也可在Managed Identities → 对应身份 → Overview同时取Client ID与Principal ID，具体方法见[主手册0-A1](customer-migration-guide-zh.md#0-a1-备份身份的获取与核验)。不从ownerEmail或当前CLI用户推断部署身份。

在S3-02的Azure登录步骤及目标RG Activity log中核对实际调用。按运行时间找到本次部署/ACR管理操作，事件详情或JSON中的appid与对象标识应对应上述两项ID；字段缺失时请Owner使用获准的服务主体登录记录交叉核验，不猜caller显示值。只保留必要身份/时间/结果引用，不复制Token、完整claims或原始敏感日志到公共验收字段。

**注意：** Check public source image没有Azure登录，不能作为OIDC成功的证据；你本机使用个人管理员查询成功也不能证明Actions使用了相同身份。

### O-2 核对联邦信任和GitHub边界

Portal在部署身份的Federated credentials页面检查：应用注册通常在Certificates & secrets → Federated credentials，UAMI在该托管身份的Federated credentials。核对以下值来自**客户自己的仓库和Environment**，不是上游仓库、旧fork名称或默认分支subject：

| 项 | 本次要求 |
| --- | --- |
| issuer | https://token.actions.githubusercontent.com |
| audience | api://AzureADTokenExchange |
| subject | repo:OWNER/REPOSITORY:environment:test，其中OWNER/REPOSITORY与本次fork一致，test替换为实际Environment |
| GitHub运行来源 | 手动执行受保护默认分支main，记录实际40位Git SHA；Environment部署分支策略与批准方案一致 |
| 批准与身份分工 | governance和实际操作者一致，部署/普通运行职责按Stage2批准方案执行，公共PR不得取得生产私网Runner或部署权限 |

Environment subject本身不限制分支，因此不能只核对subject而忽略分支保护/Environment限制。检查身份是否还有未获批的联邦信任或其他登录凭据；有其他合法用途时记录范围，由Owner处理冲突，不在验收中直接删除共享信任。

### O-3 核对获批Azure授权和实际操作范围

DEPLOY_OBJECT_ID取O-1输出，不取Client ID。以下列出该主体在目标RG可见的直接及继承角色分配；订阅级宽权限也要如实识别：

```bash
DEPLOY_OBJECT_ID="REPLACE_DEPLOY_SERVICE_PRINCIPAL_OBJECT_ID"
TARGET_SCOPE="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${TARGET_RG}"
az role assignment list --subscription "$SUBSCRIPTION_ID" \
  --assignee-object-id "$DEPLOY_OBJECT_ID" --scope "$TARGET_SCOPE" --include-inherited --fill-principal-name false \
  --query '[].{role:roleDefinitionName,roleDefinitionId:roleDefinitionId,scope:scope,principalId:principalId,condition:condition}' --output json
```

与Stage2批准的权限矩阵逐项比对，确认本次部署/What-if、ACR创建和所需读取在批准范围内；同时审核T-1实际资源变更。输出不是完整有效权限计算，组继承、自定义角色、条件、PIM或Deny assignment等须由权限Owner结合IAM核对。不能因显示Contributor就宣称“只有目标RG权限”，或认为它同时拥有Blob/PG/Graph权限。

尚保留订阅级授权时记录真实范围及批准依据，不能写成已完成RG级最小化；违反客户必需边界的授权先解决。不要通过向未授权生产RG尝试写入来做负向验证，也不在本阶段自动缩减仍被其他动作依赖的权限。

**oidc_scope通过标准：** 本次登录主体、联邦信任、GitHub来源/治理与实际部署范围一致，权限符合获批边界，没有未处理的错误主体或越界授权。单次登录成功不能证明全部最小权限已实现。

## 3. source_image_sbom_scan：源镜像报告审核

### S-1 下载正确的明文附件

Actions → Check public source image → 本次成功运行 → Summary → Artifacts，下载source-image-<SOURCE_RUN_ID>并解压到独立temp目录。三份文件应为source-summary.json、source-sbom.spdx.json、source-scan.json。**不需要解密，也不要调用workflow_security open处理这些明文文件。** 若只看到sealed-artifact.json，先确认是否下载了另一类workflow附件。

失败运行也可能上传报告，所以artifact存在不能代替workflow成功。下载/哈希检查前不要让编辑器格式化或改写JSON；文件哈希按原字节计算。

### S-2 核对摘要与本次固定源

把SOURCE_DIR设为刚解压的实际目录，以下只读查看报告摘要：

```bash
SOURCE_DIR="$PWD/temp/REPLACE_SOURCE_REVIEW_DIRECTORY"
jq '{schemaVersion,check,revision,sourceImage,evaluatedImage,evaluatedImageId,buildInputsSha256,observedAt,status,policy,stageAccepted,results,notCovered}' \
  "$SOURCE_DIR/source-summary.json"
```

| 字段 | 通过时应核对 |
| --- | --- |
| schemaVersion / check | 1 / source_image_sbom_scan |
| revision | 本次GitHub运行的完整40位Git SHA；与用于本次Stage3审核的代码一致，不只比较分支名 |
| sourceImage | 与该SHA版本的LiteLLM/runtime/Dockerfile中FROM完整引用一致，包含repo@sha256:digest，不是镜像tag或Docker IMAGE ID |
| evaluatedImage / evaluatedImageId / buildInputsSha256 | 本次本地派生镜像tag、实际扫描的不可变Docker image ID，以及Dockerfile和安全覆盖清单的绑定哈希；不能拿另一次构建结果替换 |
| observedAt | 本次检查时间，明确时区及所用运行尝试，近期证据仍适用 |
| status | passed |
| policy | severity为["CRITICAL"]，ignoreUnfixed为true；若实际政策不同须重新审核 |
| results | 恰好build、sbom和scan三项，各自status=passed、exitCode=0及64位sha256；sbom和scan另有对应artifact |
| stageAccepted | false是正常值：工具未自动验收整个Stage3 |
| notCovered | 保留上游发布者身份、目标ACR签名/私网拉取、客户阶段批准等未覆盖项 |

固定公共基线来自[源检查脚本](../scripts/source_supply_chain.py)读取的[派生镜像Dockerfile](../LiteLLM/runtime/Dockerfile)。检查会先构建该Dockerfile，再扫描实际派生镜像。当前派生层使用带SHA256的`security-requirements.txt`把AnyIO固定为`4.14.2`，修复基线层的`CVE-2026-63374`，同时保持LiteLLM `1.98.0`及其Prisma合同不变。在GitHub运行页面点击commit SHA，再打开该版本文件核对FROM和安全覆盖；不要使用另一个分支或后来改变的本地文件来比较。代码/digest不匹配时重新生成适用报告，不手改摘要以求一致。

### S-3 核对SBOM、扫描内容与策略边界

```bash
jq '{spdxVersion,packageCount:(.packages | length)}' "$SOURCE_DIR/source-sbom.spdx.json"
jq '{ArtifactName,targets:[.Results[] | {Target,Class,Type,vulnerabilityCount:((.Vulnerabilities // []) | length)}]}' \
  "$SOURCE_DIR/source-scan.json"
```

SBOM应有spdxVersion及非空packages，检查LiteLLM仍为`1.98.0`、AnyIO为`4.14.2`且其他组成符合该派生镜像，而非空壳报告；不要求把每个包手工逐行验收。扫描ArtifactName须与摘要evaluatedImageId完全一致，Results有实际扫描目标，查询/文件损坏不等于零漏洞。sourceImage只是不可变上游基线，不是允许直接部署的最终镜像。

查看发现项时关注VulnerabilityID、PkgName、InstalledVersion、FixedVersion和Severity。当前调用Trivy使用`--severity CRITICAL --ignore-unfixed --exit-code 1`，只有在工具正常完成且符合此策略时才通过：没有可修复CRITICAL阻断不等于零漏洞，HIGH/MEDIUM、未修复漏洞可能被过滤。SBOM不是恶意代码检测或上游发布者签名证明。

若报告与passed摘要矛盾、发现阻断项或客户要求更严格策略，先处理差异；不能关闭扫描、提高允许阈值或改JSON。需要变更源digest时按受审查代码变更处理，保留旧报告，不用latest冒充原固定镜像。

### S-4 校验文件哈希并记录结论

在同一SOURCE_DIR中执行，先限定摘要只能引用本workflow的两个报告文件，再校验哈希：

```bash
pushd "$SOURCE_DIR" > /dev/null &&
jq -er '
  if ([.results[].name] | sort) == ["build", "sbom", "scan"]
     and ([.results[] | select(.artifact) | .artifact] | sort) == ["source-sbom.spdx.json", "source-scan.json"]
     and all(.results[]; (.sha256 | test("^[0-9a-f]{64}$")))
  then .results[] | select(.artifact) | "\(.sha256)  \(.artifact)"
  else error("Unexpected source evidence entries") end
' source-summary.json | sha256sum --check -
popd > /dev/null
```

两个文件都应显示OK，无jq/sha256sum错误。任何失败都停在此项，不能因最后popd成功而忽略前面校验失败。文件重新格式化也会导致哈希不同，应重新取回原始附件比较，不把新哈希写入摘要。

哈希只证明报告与摘要一致，不是独立认证或数字签名；信任起点仍是受审查仓库/运行及固定源。保留run URL、SHA、digest、时间、政策、两份文件校验及未覆盖范围。

**source_image_sbom_scan通过标准：** 本次受审查默认分支的固定源匹配，三份报告齐全有效，SBOM/扫描均通过当前获批策略且报告哈希一致。这里不是target_image_signature_sbom验收，后者在Stage4对实际晋级镜像与私网路径验证。

## 4. target_isolation：计划、实际资源与旧环境

### T-1 审核加密计划和部署回执

下载S3-01和S3-02各自的infrastructure-test-3-platform-<run ID>附件到不同目录。沿用[主手册3.3解密方法](customer-migration-guide-zh.md#33-plan结果怎样审核)：在受控终端安全注入原WORKFLOW_ARTIFACT_KEY，再运行下列命令；不把密钥写到参数、shell历史或公开日志。本机审核不需要Azure登录。

```bash
REPOSITORY="REPLACE_OWNER/REPLACE_REPOSITORY"
REVIEW_RUN_ID="REPLACE_PLATFORM_PLAN_OR_DEPLOY_RUN_ID"
ARTIFACT_NAME="infrastructure-${ENVIRONMENT_NAME}-3-platform-${REVIEW_RUN_ID}"
.venv/bin/python -m scripts.workflow_security open \
  --file "temp/REPLACE_DOWNLOADED_ARTIFACT_DIRECTORY/sealed-artifact.json" \
  --repository "$REPOSITORY" --run-id "$REVIEW_RUN_ID" --name "$ARTIFACT_NAME" \
  --output-dir "temp/stage3-platform-review-${REVIEW_RUN_ID}"
unset WORKFLOW_ARTIFACT_KEY
```

替换为实际仓库、URL中run ID、下载路径和artifact名，不填ZIP名/job ID/哈希。依次审核两次运行，不能混用目录。原artifact key不可取回时请获准保管人通过受控方式审核或提供等效可信证据；没有现成密钥导出workflow，不能用新key解旧附件，也不能打印GitHub Secret。

| 文件 | 要核对的内容 |
| --- | --- |
| plan-summary.json | stage=3、component=platform、environment、revision、configSha256、planSha256与本次批准一致 |
| reviewed-plan.json | 逐项资源ID、changeType和属性变化；仅批准的目标ACR及相关嵌套部署范围，无旧RG/共享模型/Stage0网络删除或非预期修改 |
| deployment-receipt.json（deploy） | provisioningState=Succeeded，deploymentId属于目标订阅/RG，计划/配置/修订字段匹配；stageAccepted=false正常 |
| deployment-outputs.json（deploy） | platform.value.environment正确，containerRegistryDeployed=true，stage4Deployed=false，stage5Deployed=false，containerRegistryName与批准名称一致 |

先前已审核的plan不必为验收重新部署；但应确认deploy确实使用该计划和同SHA/配置。源码不执行删除并不等于人工不用审查变化；发现异常停止，不靠修改计划哈希绕过。Stage3的具体新建范围以本次reviewed-plan为准，既有资源复用时可能显示NoChange。

再从Azure读取本次实际部署记录，核对回执与当前管理面；命令只投影非秘密字段：

```bash
az deployment group show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "llmgw-${ENVIRONMENT_NAME}-s3-platform" \
  --query '{id:id,state:properties.provisioningState,timestamp:properties.timestamp,createAcr:properties.parameters.deployContainerRegistry.value,stage4:properties.parameters.deployStage4.value,stage5:properties.parameters.deployStage5.value,acrPublic:properties.parameters.containerRegistryPublicNetworkAccess.value,platform:properties.outputs.platform.value}' \
  --output json
```

预期state=Succeeded、createAcr=true、stage4=false、stage5=false、acrPublic=Disabled。同名ARM部署记录后续运行可能更新，须与本次run时间/回执交叉核对，不能拿另一轮成功记录代替当前运行。

**输出中的未来资源名不是部署成功证明。** privateAksName、keyVaultName、postgresqlServerName等在Stage3也可能有预定名称；判断是否由本次创建必须看阶段开关和实际资源，而不是只看字符串非空。

### T-2 核对ACR实际安全设置及工作区

ACR_NAME取T-1的containerRegistryName并与客户JSON比较。Portal → 目标RG → 对应Container registry → Overview/Networking/Access keys；不要点击显示或生成管理员密码。CLI可只读查询：

```bash
az acr show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" --name "$ACR_NAME" \
  --query '{id:id,name:name,resourceGroup:resourceGroup,location:location,state:provisioningState,sku:sku.name,loginServer:loginServer,publicNetworkAccess:publicNetworkAccess,adminUserEnabled:adminUserEnabled,networkRuleBypassOptions:networkRuleBypassOptions}' --output json
az monitor log-analytics workspace show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --workspace-name "$LOG_WORKSPACE" \
  --query '{id:id,name:name,state:provisioningState,workspaceId:customerId}' --output json
```

通过条件：ACR属于批准的目标订阅/RG/区域，状态Succeeded、sku=Premium、publicNetworkAccess=Disabled、adminUserEnabled=false、networkRuleBypassOptions=None，loginServer是该资源实际输出。字段缺失/null或不符时核对实际API/Portal，不直接视为通过。工作区Succeeded，资源ID及customerId与Stage0新工作区记录一致；Stage3不应该把旧AKS监控重定向到这里。

**Stage3不要求ACR私网拉取成功。** ACR的Private Endpoint/DNS和AKS拉取角色在Stage4才创建；此时不可拉取不等于配置错误，管理面az acr show成功也不证明数据面可达。不要打开ACR公网、启用admin账号、用临时密码拉取或提前晋级镜像来通过此项。

### T-3 交叉核对实际资源和保留范围

```bash
az resource list --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --query '[].{name:name,type:type,location:location,id:id}' --output json
```

与Stage0目标清单及本次计划比较：已有备份Storage、VNet、Blob Private Endpoint/DNS和新工作区保留，新增/复用ACR符合本次变更范围，不误把已存在的Blob PE认成ACR PE。资源清单不覆盖全部子资源、属性或RBAC，须结合T-1计划和O-3权限审核，不能仅凭数量不变认定未修改。

若目标已有后续阶段AKS/PG等资源，先确认部署历史和当前阶段，不因为本次stage4Deployed=false就宣称这些资源不存在；更不能重放早期模板或删除资源来迎合文档。没有新PG和应用的Stage3也不存在“已验证新旧数据库不串写”的实测证据，应用接线检查在Stage5/6完成。

### T-4 确认旧业务未受非预期影响

从计划及同一时间窗口Activity log核对本次作业没有修改旧AKS、旧数据库/PVC、旧监控、旧入口DNS或共享模型访问设置。用以下管理面查询核对旧AKS状态；它本身不能证明推理可用：

```bash
az aks show --subscription "$SUBSCRIPTION_ID" --resource-group "$LEGACY_RG" --name "$LEGACY_AKS" \
  --query '{id:id,state:provisioningState,power:powerState.code}' --output json
```

由实际使用者在部署后用原客户端/原入口/获准测试Key发一条无敏感正常请求，并与请求元数据关联；记录时间、结果和原服务监控是否有新增异常。近期部署后真实使用记录适用时可复核引用，不为验收重启旧Pod、重跑备份或反复更改配置。必要时按[Stage1健康检查](customer-stage1-acceptance-checklist-zh.md#2-legacy_health验证旧业务未被破坏)补充只读核查。

**target_isolation通过标准：** 已审核计划/回执与实际Stage3资源一致，ACR网络/账号设置符合要求，Stage0目标资源保留，本次变更无未处理越界或旧业务回归。它不是未来新AKS/PG、admin私网、源站防绕过或数据库隔离的整体验收。

## 5. 记录并填写draft/confirm

### 5.1 保存实际验收证据

| 检查ID | 证据引用 | 时间/代码/范围 | 实际结果与核验人 |
| --- | --- | --- | --- |
| oidc_scope | O-1主体与本次调用、O-2信任/GitHub保护、O-3权限与批准矩阵 | 待填写 | 待核验 |
| source_image_sbom_scan | 源run URL、SHA/digest、摘要/报告及文件哈希、策略边界 | 待填写 | 待核验 |
| target_isolation | platform plan/deploy ID、回执、ACR设置与资源核对、部署后旧业务观察 | 待填写 | 待核验 |

记录保存在客户受控位置。不要把本指南、旧阶段报告、他人环境结果或仅有资源截图当作全部实测通过。config-check不能验证Azure权限、资源实际状态或源镜像扫描结果。

### 5.2 生成有效的Stage3草稿

Actions → Customer stage acceptance → Run workflow，选择main、environment=test、stage=3、operation=draft，reviewed_run_id、checked_items、evidence_notes、confirm_environment留空。待成功后查看Summary中的三个check IDs并记录DRAFT_RUN_ID。

draft生成pending清单，不会下载并替你审核源扫描附件，也不会自动检查ACR配置。确认前复核Stage0至2账本仍在7天内且环境、阶段配置、策略和操作者匹配；single-operator允许这些前序记录来自先前Git SHA。本Stage draft与confirm仍须同环境、完整Git SHA及适用配置。若代码改动影响前序验收或源扫描结论则先重验；双人模式仍要求前序账本同SHA。

### 5.3 按页面标签填写confirm

以下沿用已批准single-operator路径；双人策略继续使用主手册record流程，不为简化填表改governance。

| 页面字段/说明 | confirm时填写 |
| --- | --- |
| Use workflow from / environment / stage | main / test / 3，与有效draft一致 |
| operation | confirm |
| For confirm, successful draft run ID for this stage and revision | DRAFT_RUN_ID，本Stage的成功acceptance draft，不是source-image或platform的run ID |
| For confirm, every check ID personally verified; comma separated as shown in the draft Summary | checked_items：三项实际通过后填下面字符串，以draft实际输出为准 |
| For confirm, actual observations and evidence references; no secrets or prompt content | evidence_notes：普通文本，12至4000字符，逐项真实结果及允许公开的证据编号，不是JSON |
| For confirm, type the selected environment again | confirm_environment：test |

```text
oidc_scope,source_image_sbom_scan,target_isolation
```

evidence_notes从5.1摘取：登录主体/信任/权限及实际范围核对；源扫描run、固定digest审核、SBOM/扫描/哈希结果和策略限制；目标ACR设置及旧业务未受影响的证据。可以明确“Stage4私网拉取、晋级镜像签名及新应用测试未在本阶段执行”，不要把这些写成已通过。不填写内部URL、凭据、客户完整资源清单或请求正文。

**当前confirm不支持部分通过。** 任一报告缺失/校验失败、OIDC主体错误、未处理越权、ACR公网开启或旧业务回归时，不提交完整confirm，也不以少填ID绕过。旧名称image_signature_sbom不是本Stage检查项。

成功生成`acceptance-record-test-3-<run ID>`加密账本后，后续workflow自动读取，不需要更新MIGRATION_EVIDENCE_JSON。`independentlyVerified=false`表示操作者记录，不是独立事实认证或业务切流批准；之后才按主手册进入Stage4。

## 6. 常见阻塞与下一步

| 情况 | 处理 |
| --- | --- |
| source-image附件不是密文 | 正常，按S项直接审核；部署/验收附件仍需解密 |
| stageAccepted=false | 源扫描和部署回执的正常边界，不手工改成true |
| 扫描通过但仍有其他级别漏洞 | 说明报告策略范围；客户更严格要求另行处理，不宣称零漏洞 |
| 哈希失败 | 重新核对本次原始附件、是否被格式化/混用或文件损坏，不重写摘要哈希 |
| Azure登录成功但范围不对 | 按O项核对实际主体、信任、角色和调用记录；不继续部署或确认 |
| ACR目前拉取失败 | Stage3先核对管理面安全设置，Stage4建私网后实测，不开公网兜底 |
| 已有后续资源或计划出现越界修改 | 停止早期模板重放，审核实际阶段与变更，不删除资源来凑通过 |

进入Stage4前确认网络/AKS参数仍与Stage2批准方案一致，管理网络、Private DNS及证书依赖有实际交付安排；镜像晋级、目标签名/私网拉取按Stage4执行。保管好本次报告和验收引用，不删除旧资源或云端备份作为收尾。