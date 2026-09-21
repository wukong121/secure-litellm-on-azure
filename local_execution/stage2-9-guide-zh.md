# Stage2–9本地手工部署指南

> 核对日期：2026-09-20
>
> 适用：Stage0–1已按本地执行包完成，客户不能使用GitHub Actions，需要继续在受控执行主机上部署Stage2–9。

## 1. 执行边界

本指南只扩展`local_execution`，不修改或替代现有GitHub workflows。底层继续调用同一套Bicep、What-if范围校验、Kubernetes dry-run、数据库迁移和运行时检查。

本地路径与Actions有三个差异：

- 不读取GitHub Environment、run ID或验收账本；基础设施和运行时变更使用本地两阶段plan/execute，并在execute前重新计划检查漂移。
- 本地操作者负责在命令前取得变更批准，命令输出不等于Stage验收通过。
- 镜像使用客户自管Cosign密钥签名；Actions仍使用GitHub OIDC keyless签名，两种信任路径不混用。

Stage9仍没有自动完成最终停写、最终备份、干净目标库选择、最终对账或停旧的按钮。未完成人工最终迁移方案时，只能执行`stage9-origin`和`stage9-edge-prepare`，不得启流量或切DNS。

## 2. Azure登录方式

### 2.1 复用现有az登录

客户能够在执行主机登录时，保留默认配置：

```json
"authentication": {
  "default": {"method": "existing"}
}
```

执行器不会重新登录，只会切换到customer.json中的订阅并核对租户和订阅。可以使用交互用户，也可以是客户已通过证书等合规方式登录的服务主体。不要使用`sudo az login`或长期Client Secret。profile未声明`clientId`时适合一般deploy/runtime/database人工操作；一旦声明`clientId`，执行器还会在内存中解析Azure CLI Token的`appid/azp`并拒绝身份错配，Token不会写入日志。

Entra Security Defaults可能阻止设备码登录；这时不要关闭Security Defaults来迁就脚本，改用浏览器支持的登录方式或下面的UAMI。

### 2.2 推荐的两种登录方式

**客户现场：直接登录为主。** 客户能够在Runner VM执行交互登录时，保持`default=existing`，Stage3、Stage4、Stage5 platform及普通AKS操作不需要为deploy/runtime/certificate分别创建UAMI：

```bash
az login --tenant "REPLACE_TENANT_ID"
az account set --subscription "REPLACE_SUBSCRIPTION_ID"
```

脚本中的deploy/runtime/database等名称是权限类别；没有专项profile时会复用当前Azure CLI会话。客户账号仍需具备对应Stage列出的Azure、AKS、PG或DNS权限。

但有两个例外不能用普通用户账号代替：

- Stage5的`backend-secrets`、`restore-target`和`schema-migrate`必须使用`databaseAccess.migrationPrincipalId`对应的服务身份Token，因为数据库角色固定映射为服务主体`llmgw_migrator`。客户可在这些步骤前切换到已有runtime UAMI，或使用下面的单一operator UAMI。
- Stage7的Entra动作必须使用两个彼此分离、且与迁移身份不同的服务身份，见第2.5节。

**实施方验证：单一临时UAMI。** 无法在Runner交互登录时，只创建一个`llmgw-test-local-operator`，并只配置`default`：

```json
"authentication": {
  "default": {
    "method": "managed-identity",
    "clientId": "REPLACE_LOCAL_OPERATOR_UAMI_CLIENT_ID"
  }
}
```

所有未单独配置的profile会自动回退到这个身份。当前默认`entraMode=deferred`，因此这一个UAMI足以完成Stage2–6、可选Stage8观测基础设施，以及Stage9禁流量的origin/edge准备。

### 2.3 创建单一验证UAMI

执行位置：**客户外部管理终端**。已有获批验证UAMI时只查询，不重复创建：

已有`github-developer`等UAMI且客户批准复用时，可以直接把它作为local-operator，不必再创建新身份；先核对其Client ID、Principal ID和现有角色。Runner必须保持Offline，恢复Actions Runner服务前必须卸载该高权限UAMI。

```bash
set -euo pipefail
SUBSCRIPTION_ID="REPLACE_SUBSCRIPTION_ID"
LOCATION="REPLACE_AZURE_REGION"
IDENTITY_RG="REPLACE_CUSTOMER_IDENTITY_RESOURCE_GROUP"
OPERATOR_NAME="llmgw-test-local-operator"

az account set --subscription "$SUBSCRIPTION_ID"
if ! az identity show --resource-group "$IDENTITY_RG" --name "$OPERATOR_NAME" \
  --output none 2>/dev/null; then
  az identity create --resource-group "$IDENTITY_RG" --name "$OPERATOR_NAME" \
    --location "$LOCATION" --output none
fi
az identity show --resource-group "$IDENTITY_RG" --name "$OPERATOR_NAME" \
  --query '{name:name,clientId:clientId,principalId:principalId,resourceId:id}' \
  --output json
```

保存三个值：Client ID填`authentication.default.clientId`；Principal ID用于RBAC、`databaseAccess.migrationPrincipalId`和PG管理员组成员；Resource ID用于挂载/卸载。

### 2.4 单一验证UAMI的临时权限

这是**隔离测试环境的简化模式**，权限高于生产最小权限。Runner必须Offline，身份只在验证窗口挂载，完成后卸载并回收角色。不要授予订阅Owner。下列角色组合是现场验证的操作建议，代码仍以每项真实Azure/AKS/数据面调用结果为准，并不会把这些角色名称当作安全证明。

```bash
TARGET_RG="REPLACE_TARGET_RESOURCE_GROUP"
RUNNER_VNET_ID="REPLACE_RUNNER_VNET_RESOURCE_ID"
OPERATOR_OBJECT_ID="REPLACE_LOCAL_OPERATOR_UAMI_PRINCIPAL_ID"
TARGET_RG_SCOPE="$(az group show --subscription "$SUBSCRIPTION_ID" \
  --name "$TARGET_RG" --query id --output tsv)"

# 目标RG内集中完成资源部署、锁和角色分配。
az role assignment create --assignee-object-id "$OPERATOR_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal --role Owner \
  --scope "$TARGET_RG_SCOPE" --output none

# 只在Runner VNet授权网络链接管理。
az role assignment create --assignee-object-id "$OPERATOR_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal --role "Network Contributor" \
  --scope "$RUNNER_VNET_ID" --output none
```

Stage4前，每个模型RG也需给该UAMI临时Owner，或使用附录中的最小模型角色加受约束RBAC Administrator；不要扩大到模型订阅。Stage4 AKS创建后再补节点RGContributor、AKS Cluster User/RBAC Cluster Admin和ACR AcrPush。Stage9自动DNS切换前再给目标DNS zone的DNS Zone Contributor。

Stage5使用同一UAMI时，客户Entra管理员创建一个PG管理员安全组，把该UAMI加入组；`stage5Data`填写组Object ID/名称/`Group`，`databaseAccess.migrationPrincipalId`填写UAMI Principal ID。这样PG管理员对象与`llmgw_migrator`服务主体保持不同，符合脚本的分离检查。

### 2.5 Stage7是唯一例外

当前代码强制Entra应用初始化身份、授权身份和数据库迁移身份三者分离。因此一个UAMI**不能**执行Stage7全部动作。当前客户若保持`entraMode=deferred`，先不创建额外身份。

将来启用Stage7时，再新增两个UAMI并只覆盖两个profile：

```json
"authentication": {
  "default": {
    "method": "managed-identity",
    "clientId": "REPLACE_LOCAL_OPERATOR_UAMI_CLIENT_ID"
  },
  "entraBootstrap": {
    "method": "managed-identity",
    "clientId": "REPLACE_ENTRA_BOOTSTRAP_UAMI_CLIENT_ID"
  },
  "entraAccess": {
    "method": "managed-identity",
    "clientId": "REPLACE_ENTRA_ACCESS_UAMI_CLIENT_ID"
  }
}
```

entra-bootstrap需Graph `Application.ReadWrite.OwnedBy`；entra-access需`Application.Read.All`、`AppRoleAssignment.ReadWrite.All`、`DelegatedPermissionGrant.ReadWrite.All`和`User.Read.All`，均需Admin consent。两者在目标RG临时Contributor即可保存回执；不要把Graph权限授给通用operator UAMI。

### 2.6 挂载和卸载单一验证UAMI

外部管理终端挂载一次：

```bash
RUNNER_VM_RESOURCE_ID="REPLACE_RUNNER_VM_RESOURCE_ID"
OPERATOR_RESOURCE_ID="REPLACE_LOCAL_OPERATOR_UAMI_RESOURCE_ID"
az vm identity assign --ids "$RUNNER_VM_RESOURCE_ID" \
  --identities "$OPERATOR_RESOURCE_ID"
```

Runner VM核验：

```bash
az login --identity --client-id "REPLACE_LOCAL_OPERATOR_UAMI_CLIENT_ID" --output none
az account set --subscription "REPLACE_SUBSCRIPTION_ID"
az account show --query '{tenantId:tenantId,subscriptionId:id,identity:user.name}' --output json
```

本地执行器每一步都会自动重新选择同一个default UAMI，无需手工切换deploy/runtime/database profile。完成验证后先在Runner执行`az logout && az account clear`，再从外部管理终端卸载：

```bash
az vm identity remove --ids "$RUNNER_VM_RESOURCE_ID" \
  --identities "$OPERATOR_RESOURCE_ID"
```

最后回收临时Owner、节点RG、AKS、ACR、模型RG和DNS角色；仅卸载UAMI不会自动删除其Azure角色分配。

## 3. Entra ID Free决策

当前Stage7使用应用注册、服务主体、OAuth scope/app role、定向用户授权和托管身份；代码不调用Conditional Access、PIM、Identity Protection或Access Reviews。Microsoft Entra ID Free本身不阻止这些基础对象，也不对Azure Managed Identity收费，因此**Free不是自动跳过Stage7的理由**。

需要单独评估的是登录保护：

- Free可启用Security Defaults，对整个租户提供统一MFA和旧认证阻断。
- 按应用、用户组、设备、位置等条件精细控制的Conditional Access需要Entra ID P1。
- 风险登录策略和PIM等能力需要更高许可。
- Security Defaults可能阻止设备码登录；UAMI不依赖人工设备码。

客户应在Stage2决定以下二选一：

1. `entraMode=enabled`：确认Free + Security Defaults满足本次范围，或先取得所需P1/P2能力，然后按Stage7执行。
2. `entraMode=deferred`：先完成Stage2–6。执行器会阻止Stage7身份/代理、Stage8应用发布、Stage9 edge绑定、启流量和DNS变更；仍可创建Stage8观测基础设施，以及Stage9禁流量的origin/edge资源。

配置示例默认使用保守值：

```json
"features": {
  "entraMode": "deferred",
  "allowTrafficRelease": false
}
```

Stage5的PG/Redis Entra-only和应用Workload Identity不属于可跳过的Stage7用户认证。不要把`entraMode=deferred`解释为允许密码数据库、Redis Key或匿名公网API。也不要删除Stage7后直接启用edge；当前Stage9流量路径依赖Stage7代理和企业认证。

## 4. 执行主机和配置

除Stage0–1工具外，还需要：

```bash
command -v docker jq psql pg_restore openssl skopeo syft trivy cosign
az bicep install
```

继续使用受控的`local_execution/customer.json`。Stage2–9客户业务字段按[主手册第2.3节](../docs/customer-migration-guide-zh.md#23-客户json分批准备)分批加入；不要一次复制未决的占位块。`localExecution`只保存本地执行方式和非秘密引用，不保存Token、Master Key、Salt、PEM正文或Cosign口令。

### 分阶段配置模板

[基础模板](customer.example.json)保持Stage0–1最小可运行范围；[Stage2–9片段目录](customer.stage2-9.fragments.example.json)补充后续全部字段。片段目录本身不是合法的客户配置，不能整份复制为`customer.json`，也不能把`catalogVersion`或`stages`写入客户配置。

使用合并器，不手工递归编辑：

```bash
# 1. 预览当前Stage；不会修改customer.json
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json \
  --stage REPLACE_STAGE --operation plan
```

最后一行JSON会给出：

- `preview`：递归合并后的完整配置预览。
- `requiredValues`：本Stage尚未替换的`REPLACE_*`列表。
- `changedPaths`：本次会修改的JSON路径。

若`missingPlaceholders`非空，把生成的`values.required.json`复制到Git忽略的本地文件并填写客户值：

```bash
cp "REPLACE_REQUIRED_VALUES_PATH" \
  "local_execution/stage-REPLACE_STAGE-values.local.json"
chmod 600 "local_execution/stage-REPLACE_STAGE-values.local.json"
# 使用受控编辑器填写右侧空字符串，不添加密码、Token、Master Key或Salt。
```

然后再次预览，确认`missingPlaceholders=[]`：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json \
  --stage REPLACE_STAGE --operation plan \
  --values "local_execution/stage-REPLACE_STAGE-values.local.json"
```

审核`preview`和`changedPaths`后才真正写入：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json \
  --stage REPLACE_STAGE --operation apply \
  --values "local_execution/stage-REPLACE_STAGE-values.local.json"
```

`apply`会先调用现有配置校验器，成功后以0600权限原子替换`customer.json`，并把变更前完整配置保存到输出目录的`customer.before.json`。校验失败或仍有占位符时不会修改原文件。

可选块必须显式加`--option`：

| Stage | 选项 | 作用 |
| --- | --- | --- |
| 2 | `single-validation-identity` | 配置单一local-operator UAMI；客户直接登录不选 |
| 4 | `automatic-api-certificate` | 启用自动API证书；手工导入不选 |
| 5 | `database-admin-identity` | 使用独立database UAMI作为PG管理员；无管理员组时选择 |
| 8 | `observability` | 加入可选collector |
| 8 | `enhanced-l3` | 删除原生`contentAudit`并切换增强L3；不能与observability在同一次合并 |
| 9 | `azure-dns` | 使用仓库自动发布Azure DNS |
| 9 | `approved-release` | 打开最终流量开关并加入release报告路径；默认只合并禁流量prepare |

例如验证环境Stage2：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 2 --operation plan \
  --option single-validation-identity
```

合并器会自动复用customer.json中已有的订阅、区域、baseDomain、目标Workspace/VNet、Runner VNet、ACR和证书Vault名称；其他实际客户值从values文件提供。

片段覆盖关系如下：

| Stage | 合并内容 |
| --- | --- |
| 2 | `governance`、原生`contentAudit`决策；可选UAMI认证profile |
| 3 | `parameters.platform`的ACR、Workspace、目标网络和AKS |
| 4 | 模型账号、稳定角色命名、证书Vault、Runner目标DNS、私有入口及本地Cosign配置；自动证书为可选块 |
| 5 | `stage5Data`、数据库迁移身份、Private DNS归属及Stage0备份引用/SHA256 |
| 6 | `application`后端镜像digest和模型部署映射 |
| 7 | `entra`、`proxy`和`entraMode=enabled` |
| 8 | 可选原生观测collector；增强L3是单独替代方案 |
| 9 | origin、edge；Azure DNS和最终release开关均为可选/后置块 |

合并后先运行当前Stage的`--operation plan`。plan会校验该组件配置并生成What-if或运行时预览；配置缺字段或仍有占位符时不得进入execute。

### 4.1 Stage5恢复输入

从**当前Runner本地执行Stage0**时生成的同一个成功备份报告取得Blob名和SHA256；不要使用Actions artifact、开发机`temp/reviewed-backup-*`或另一台机器保存的旧报告。先在Runner仓库根目录定位并核验报告：

```bash
set -euo pipefail
mapfile -t BACKUP_REPORTS < <(
  find temp -mindepth 3 -maxdepth 3 -type f \
    -path 'temp/local-stage*/*-backup-restore-*/acceptance-report.json' -printf '%T@ %p\n' \
    | sort -nr | cut -d' ' -f2-
)
BACKUP_REPORT=
for candidate in "${BACKUP_REPORTS[@]}"; do
  run_dir="${candidate%/acceptance-report.json}"
  if jq -e '.step == "backup-restore" and .result.status == "completed"' \
      "$run_dir/local-execution.json" >/dev/null \
    && jq -e '.observations.fullRestoreSucceeded == true
      and (.observations.backupBlob | test("^pre-change/[0-9a-f]{32}\\.dump$"))
      and (.observations.backupSha256 | test("^[0-9a-f]{64}$"))
      and .observations.backupBytes > 0
      and .observations.publicTableCount > 0' "$candidate" >/dev/null; then
    BACKUP_REPORT="$candidate"
    break
  fi
done
[[ -n "$BACKUP_REPORT" ]] || {
  echo 'No successful local Stage0 backup report found on this Runner' >&2
  false
}
printf 'Using local Stage0 report: %s\n' "$BACKUP_REPORT"
jq '.observations | {backupBlob,backupSha256,backupBytes,publicTableCount,fullRestoreSucceeded}' \
  "$BACKUP_REPORT"
```

只有上述命令找到成功报告时才继续。将输出的完整`backupBlob`和`backupSha256`原样用于Stage5；values文件中的`REPLACE_32_HEX`只填写`pre-change/`与`.dump`之间的32位标识：

```json
"runtimeInputs": {
  "backupBlob": "pre-change/REPLACE_32_HEX.dump",
  "backupSha256": "REPLACE_64_HEX_SHA256",
  "postgresMigrationUser": "llmgw_migrator"
}
```

配置了`databaseAccess`时，`postgresMigrationUser`可省略，执行器固定使用`llmgw_migrator`。两项备份值不是Blob URL、SAS、镜像digest或任意旧备份。

### 4.2 本地镜像签名

在客户受控密钥目录生成独立Cosign密钥对。私钥必须加密、权限为0600并进入客户密码库/备份策略；不要提交Git或放入Actions工作目录。

```bash
install -d -m 0700 /srv/runner/manual/keys
cd /srv/runner/manual/keys
cosign generate-key-pair
chmod 600 cosign.key
chmod 644 cosign.pub
cd /srv/runner/manual/secure-litellm-on-azure
```

`cosign generate-key-pair`提示输入和确认私钥口令时，必须设置客户批准的**非空口令**，不能直接回车。执行器拒绝空口令密钥。若误生成空口令密钥且尚未签过任何镜像，先从工作路径移走该密钥对，再重新生成；若已用于签名，按客户密钥轮换流程保留旧公钥和历史验证证据，不直接覆盖。

在customer.json加入路径和目标tag。tag不是最终digest；每次步骤成功后从`target-image-summary.json`取完整`ACR/repository@sha256:...`回填业务配置。

```json
"imageSigning": {
  "privateKeyPath": "/srv/runner/manual/keys/cosign.key",
  "publicKeyPath": "/srv/runner/manual/keys/cosign.pub",
  "backendTargetTag": "litellm-azure:rehearsal-1",
  "proxyTargetTag": "auth-proxy:rehearsal-1",
  "collectorTargetTag": "otel-collector:0.148.0"
}
```

每次镜像步骤前在当前shell安全输入口令，命令结束后清除：

```bash
COSIGN_PASSWORD=
while [[ -z "$COSIGN_PASSWORD" ]]; do
  read -r -s -p 'Cosign key password: ' COSIGN_PASSWORD
  printf '\n'
  [[ -n "$COSIGN_PASSWORD" ]] || echo 'Cosign key password must not be empty' >&2
done
export COSIGN_PASSWORD
# 执行一个镜像步骤
unset COSIGN_PASSWORD
```

本地镜像步骤会构建或导入、推送、解析不可变digest、生成SPDX SBOM、用Trivy阻断可修复CRITICAL、签名并以同一公钥回验。ACR Token只写入本次0700临时目录，并在结束时删除。

### 4.3 每个变更步骤都要执行plan和execute

后文会为每个变更明确列出两条命令。第一条生成plan，最后一行会显示`result.planSha256`和`outputDirectory`；先审核`<outputDirectory>/plan/`中的`plan-summary.json`、`reviewed-plan.json`或`runtime-review.json`。确认无误后，把该64位哈希原样填入紧接着的execute命令：

```bash
# 1. 生成并审核计划
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step REPLACE_STEP --operation plan

# 2. 审核通过后真正部署/执行
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step REPLACE_STEP --operation execute \
  --approved-plan-sha256 "REPLACE_PLAN_SHA256_FROM_PREVIOUS_COMMAND"
```

execute会重新生成实时plan；代码、配置、云状态或发布报告变化导致哈希不一致时停止，必须重新plan和审核。**只运行plan不算完成该步骤。** `--operation apply`仅保留给Stage0–1，本地执行器会拒绝用它绕过Stage2–9批准。

下面三类步骤没有plan/execute命令对：

- `stage2-decisions`、`stage3-source-check`和`stage4-target-check`是直接检查。
- 三个`promote-*-image`会直接构建/导入、扫描、签名和推送镜像，须显式使用`--operation execute`。
- 最终停写、最终数据同步及旧环境下线仍是人工批准操作，不伪装成已有自动化步骤。

## 5. Stage2–6顺序

所有本地执行命令都在**Offline Runner VM的仓库根目录**运行；身份创建、角色分配和UAMI挂载命令在**客户外部管理终端**运行。

### Stage2：冻结决策，不部署资源

1. 合并Stage2的`governance`和`contentAudit`。客户直接登录不加option；验证环境的单一UAMI加`--option single-validation-identity`：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 2 --operation plan \
  --values local_execution/stage-2-values.local.json \
  --option single-validation-identity
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 2 --operation apply \
  --values local_execution/stage-2-values.local.json \
  --option single-validation-identity
```

客户直接登录时删除两条命令中的`--option single-validation-identity`。确认网络CIDR、区域/SKU配额、PG HA、域名证书、实际客户端协议、正文留存和Entra Free决策均已有客户负责人。
2. Runner VM执行：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage2-decisions
```

3. 打开命令输出中的`outputDirectory/stage2-decision-checklist.json`，逐项记录负责人和决定。该步骤不创建Azure资源；存在`pending`或未决项就停在Stage2。

### Stage3：供应链检查和目标基础资源

**执行身份：客户当前登录账号，或验证模式的operator UAMI。** 确认该主体在目标RG有部署权限；模型RG权限可在Stage4前补齐。

1. 合并Stage3 `parameters.platform`；先确认CIDR不与Runner、旧环境或企业网络重叠：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 3 --operation plan \
  --values local_execution/stage-3-values.local.json
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 3 --operation apply \
  --values local_execution/stage-3-values.local.json
```
2. Runner VM先检查固定上游源镜像：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage3-source-check
```

该步骤先从固定LiteLLM `1.98.0`上游digest构建当前仓库的派生运行镜像，再对派生镜像生成SBOM并执行Trivy门禁；不会推送ACR。派生构建通过SHA256固定的`security-requirements.txt`把AnyIO升级到`4.14.2`，用于修复上游层中的`CVE-2026-63374`，但不改变LiteLLM或Prisma版本。不要把上游层单独作为最终应用镜像部署。

失败时查看本次三项结果；`build`、`sbom`和`scan`必须全部为`passed`：

```bash
RUN_DIR="$(ls -1dt temp/local-stage09/*-stage3-source-check-* | head -n 1)"
jq '{sourceImage,evaluatedImage,evaluatedImageId,buildInputsSha256,status,results}' \
  "$RUN_DIR/source-summary.json"
```

发现新的可修复CRITICAL时停在Stage3，更新受审查的安全覆盖或选择通过完整兼容验证的新稳定LiteLLM版本；不得改报告、降低严重级别或改用RC/dev镜像绕过。

3. 计划并真正部署Stage3 platform：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage3-platform --operation plan

.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage3-platform --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE3_PLATFORM_PLAN_SHA256"
```

第二条命令中的哈希来自第一条命令最终输出；执行成功后应创建/确认目标ACR和Stage3基础资源，不创建PG或切换流量。

4. Runner VM只读核验：

```bash
SUBSCRIPTION_ID="REPLACE_SUBSCRIPTION_ID"
TARGET_RG="REPLACE_TARGET_RESOURCE_GROUP"
ACR_NAME="REPLACE_GLOBALLY_UNIQUE_ACR_NAME"

az acr show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$ACR_NAME" \
  --query '{state:provisioningState,publicNetworkAccess:publicNetworkAccess,sku:sku.name}' \
  --output json
```

通过条件：`state=Succeeded`、`publicNetworkAccess=Disabled`，SKU与客户批准一致；Stage3源扫描报告为passed。旧AKS、旧PG和旧入口仍正常，未发生流量变化。

### Stage4：私网AKS、证书、入口和目标连通性

**客户模式使用当前登录账号；验证模式使用同一个local-operator UAMI。** 文中的deploy/runtime是权限类别，不再代表两个身份。Stage4开始前，IAM管理员完成：

- 客户账号按客户最小权限策略授权；验证operator在目标RG临时Owner。
- 客户账号在每个模型RG使用附录最小角色；验证operator可在每个批准模型RG临时Owner，但不得扩大到模型订阅。
- 当前执行主体在Runner VNet有Network Contributor。
- Stage4模板中的模型账号、区域、AKS版本/SKU和CIDR已获批。

1. 合并Stage4片段。手工导入证书使用下列命令；选择自动API证书时，两条命令都追加`--option automatic-api-certificate`：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 4 --operation plan \
  --values local_execution/stage-4-values.local.json
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 4 --operation apply \
  --values local_execution/stage-4-values.local.json
```

确认模型账号、证书Vault、Runner VNet、入口证书地址和CIDR后，部署私网AKS和模型连接：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-platform --operation plan

.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-platform --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE4_PLATFORM_PLAN_SHA256"
```

2. 平台创建成功后，客户外部管理终端取得新AKS、节点RG和ACR资源ID，并补runtime和DNS连接权限：

```bash
TARGET_AKS="REPLACE_NEW_PRIVATE_AKS_NAME"
ACR_NAME="REPLACE_GLOBALLY_UNIQUE_ACR_NAME"
AKS_ID="$(az aks show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" --query id --output tsv)"
NODE_RG="$(az aks show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" --query nodeResourceGroup --output tsv)"
NODE_RG_SCOPE="$(az group show --subscription "$SUBSCRIPTION_ID" --name "$NODE_RG" \
  --query id --output tsv)"
ACR_ID="$(az acr show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$ACR_NAME" --query id --output tsv)"

az role assignment create --assignee-object-id "$OPERATOR_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Azure Kubernetes Service Cluster User Role" --scope "$AKS_ID" --output none
az role assignment create --assignee-object-id "$OPERATOR_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Azure Kubernetes Service RBAC Cluster Admin" --scope "$AKS_ID" --output none

# runner-target-connectivity需要在AKS系统DNS所在节点RG写入嵌套部署和DNS链接。
# 这里使用Stage4窗口内的节点RG Contributor；客户有等效最小自定义角色时优先替代。
az role assignment create --assignee-object-id "$OPERATOR_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal --role Contributor \
  --scope "$NODE_RG_SCOPE" --output none

# 同一operator负责后端、入口和后续代理镜像晋级。
az role assignment create --assignee-object-id "$OPERATOR_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal --role AcrPush \
  --scope "$ACR_ID" --output none
```

若客户ACR启用了ABAC repository permissions，不使用旧`AcrPush`，改由ACR管理员授予等效的目标仓库Writer/Reader角色并实测推送。

3. 创建独立证书Vault：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-certificate-vault --operation plan

.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-certificate-vault --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE4_CERTIFICATE_VAULT_PLAN_SHA256"
```

4. 建立Runner到AKS API和ACR的Private DNS链接：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-target-connectivity --operation plan

.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-target-connectivity --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE4_TARGET_CONNECTIVITY_PLAN_SHA256"
```

5. 验证模式只需挂载operator UAMI；deploy/runtime两个逻辑profile都会回退到它。运行只读私网检查：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step stage4-target-check
```

通过条件：AKS API和ACR域名只解析到已发现的Private Endpoint IP、TLS命中相同私网IP、runtime身份能读取新AKS Deployment。失败时修DNS/路由/NSG或RBAC，不开放AKS/ACR公网。

AKS私有API证书由集群CA签发，检查器会从本次生成的kubeconfig提取当前context的`certificate-authority-data`，写入0600临时文件并仅给AKS的curl使用；ACR仍使用系统CA。若看到curl 60，先确认Runner已拉取包含该逻辑的版本，不使用`-k`、不关闭证书校验，也不把集群CA永久加入系统信任库。执行器退出时会删除kubeconfig和临时`aks-ca.crt`。

6. 创建三个namespace并接入Container Insights：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-cluster-bootstrap --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-cluster-bootstrap --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE4_CLUSTER_BOOTSTRAP_PLAN_SHA256"

.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-monitoring-onboard --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-monitoring-onboard --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE4_MONITORING_PLAN_SHA256"
```

7. 按第4.2节准备Cosign口令，构建、扫描、签名并推送后端镜像：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-promote-backend-image --operation execute
unset COSIGN_PASSWORD
```

保存输出`result.image`的完整digest，Stage6填入`application.backendImage`。镜像推送成功但扫描或签名失败时不得使用。

8. 准备入口证书。手工导入按[Stage4证书导入步骤](../docs/customer-migration-guide-zh.md#4-c-部署后手动导入两个secret)上传`api-tls`和`admin-tls`。选择自动API证书时，先给当前客户账号或operator UAMI授予公共DNS zone的DNS Zone Contributor和证书Vault的Key Vault Secrets Officer，再执行：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-certificate-renew --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-certificate-renew --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE4_CERTIFICATE_PLAN_SHA256"
```

9. 发布API/admin两套私有入口：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-private-ingress --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-private-ingress --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE4_PRIVATE_INGRESS_PLAN_SHA256"
```

只有Service事件明确显示AKS控制面缺目标VNet权限时，才额外执行下面的plan和execute；正常路径不要重复授权：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-aks-ingress-role --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage4-aks-ingress-role --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE4_AKS_INGRESS_ROLE_PLAN_SHA256"
```

10. Runner VM验证资源和工作负载：

```bash
set -euo pipefail
SUBSCRIPTION_ID="$(jq -er '.azure.subscriptionId' local_execution/customer.json)"
TARGET_RG="$(jq -er '.target.resourceGroup' local_execution/customer.json)"
TARGET_AKS="$(jq -er '.parameters.platform.stage4Aks.name' local_execution/customer.json)"
ENVIRONMENT_NAME="$(jq -er '.environment' local_execution/customer.json)"
VERIFY_KUBECONFIG="$(mktemp)"
trap 'rm -f "$VERIFY_KUBECONFIG"' EXIT
az aks get-credentials --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" --file "$VERIFY_KUBECONFIG" --overwrite-existing
kubelogin convert-kubeconfig --kubeconfig "$VERIFY_KUBECONFIG" -l azurecli
kubectl --kubeconfig "$VERIFY_KUBECONFIG" get namespace \
  litellm llm-api-ingress llm-admin-ingress
kubectl --kubeconfig "$VERIFY_KUBECONFIG" get pods,service \
  -n llm-api-ingress
kubectl --kubeconfig "$VERIFY_KUBECONFIG" get pods,service \
  -n llm-admin-ingress
```

通过条件：三个namespace存在；两套入口Pod Ready、内部LoadBalancer有不同私网IP；证书域名/指纹正确；错误Host和未批准来源被拒绝；AKS、ACR和证书Vault公网均关闭。此时仍不切业务流量。

#### 私有AKS自动停机后的启动复核

客户Policy夜间执行AKS stop/start时，通常不需要重跑Stage3–5部署。Private Link模式的AKS会在启动时重建由AKS管理的API Server Private Endpoint，其私网IP可能变化；用户另行创建且目标指向该AKS的Private Endpoint则不由AKS恢复，需要网络Owner删除后重建。ACR、证书Vault、PostgreSQL和Redis各自的Private Endpoint不属于“目标指向AKS”的PE，不因该提示删除。

每次启动后先等待管理面完全恢复；`power=Running`但`state=Starting`仍不能继续：

```bash
set -euo pipefail
SUBSCRIPTION_ID="$(jq -er '.azure.subscriptionId' local_execution/customer.json)"
TARGET_RG="$(jq -er '.target.resourceGroup' local_execution/customer.json)"
TARGET_AKS="$(jq -er '.parameters.platform.stage4Aks.name' local_execution/customer.json)"
ENVIRONMENT_NAME="$(jq -er '.environment' local_execution/customer.json)"

az aks wait --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" --updated --interval 30 --timeout 1800
az aks show --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" \
  --query '{state:provisioningState,power:powerState.code,privateFqdn:privateFqdn}' \
  --output json
```

只有`state=Succeeded`且`power=Running`时继续。先运行只读检查，它会重新发现本次API PE地址，验证Runner DNS、TLS、ACR和实际Kubernetes读取，不依赖停机前IP：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step stage4-target-check
```

然后重新取得临时kubeconfig，确认节点、系统Pod和两套入口恢复，并把当前入口IP与Stage4回执比较：

```bash
VERIFY_KUBECONFIG="$(mktemp)"
trap 'rm -f "$VERIFY_KUBECONFIG"' EXIT
az aks get-credentials --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" --file "$VERIFY_KUBECONFIG" --overwrite-existing
kubelogin convert-kubeconfig --kubeconfig "$VERIFY_KUBECONFIG" -l azurecli
kubectl --kubeconfig "$VERIFY_KUBECONFIG" wait --for=condition=Ready nodes --all --timeout=15m
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n kube-system get pods
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n llm-api-ingress rollout status \
  deployment/llm-api-ingress --timeout=15m
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n llm-admin-ingress rollout status \
  deployment/llm-admin-ingress --timeout=15m

EXPECTED_API_IP="$(az deployment group show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --name "llmgw-${ENVIRONMENT_NAME}-s4-private-ingress" \
  --query properties.outputs.privateIngress.value.api.privateIpAddress --output tsv)"
EXPECTED_ADMIN_IP="$(az deployment group show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --name "llmgw-${ENVIRONMENT_NAME}-s4-private-ingress" \
  --query properties.outputs.privateIngress.value.admin.privateIpAddress --output tsv)"
ACTUAL_API_IP="$(kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n llm-api-ingress \
  get service llm-api-ingress -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
ACTUAL_ADMIN_IP="$(kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n llm-admin-ingress \
  get service llm-admin-ingress -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
printf 'api expected=%s actual=%s\nadmin expected=%s actual=%s\n' \
  "$EXPECTED_API_IP" "$ACTUAL_API_IP" "$EXPECTED_ADMIN_IP" "$ACTUAL_ADMIN_IP"
[[ -n "$ACTUAL_API_IP" && -n "$ACTUAL_ADMIN_IP" && \
   "$ACTUAL_API_IP" == "$EXPECTED_API_IP" && \
   "$ACTUAL_ADMIN_IP" == "$EXPECTED_ADMIN_IP" ]]
```

处理边界：

- `stage4-target-check`通过且入口IP一致、rollout Ready：不重跑platform、证书Vault、target-connectivity、cluster-bootstrap、monitoring、private-ingress或Stage5。
- `stage4-target-check`显示Runner VNet的AKS Private DNS链接缺失时：重新执行`stage4-target-connectivity`的plan，审核后仅在计划显示修复该链接时用新哈希execute；不重跑Stage4 platform。
- 实际API PE NIC与AKS私有区域A记录长期不一致时：等待AKS托管资源协调，仍不一致则交Azure/网络Owner处理。`stage4-target-connectivity`不拥有或改写AKS托管PE/A记录，不能靠反复execute修复。
- 入口Pod/Service未恢复或入口IP与回执不同：先查看Service事件；控制面授权错误才执行`stage4-aks-ingress-role`。随后为当前状态重新plan/execute `stage4-private-ingress`，让TLS在线验证和ARM回执绑定新地址，不复用停机前哈希。
- 查询到用户自建且目标为该AKS的Private Endpoint时，停止自动恢复，由网络Owner按客户变更流程删除并重建该PE；不要删除AKS管理的API PE，也不要误删ACR/Vault/PG/Redis PE。
- 已进入Stage9时，还要复核Private Link Service、Front Door origin和实际客户端；入口前端地址或资源映射变化时重新plan受影响的Stage9步骤，不直接启流量。

### Stage5：PostgreSQL、Redis、后台秘密和恢复演练

**客户模式按步骤使用IT部署账号、PG管理员和migration/runtime身份；验证模式除可选的独立database UAMI外继续使用operator。** Stage5开始前：

- PG管理员使用安全组时，把获批执行主体加入组，`stage5Data`填写组Object ID、组名和`Group`；没有组管理权限时可选择独立database UAMI路径。
- `databaseAccess.migrationPrincipalId`填operator UAMI Principal ID，不能填PG管理员组ID。
- operator具有Stage0备份容器Blob Data Contributor，以及旧AKS `litellm-env` Secret读取权；目标RGOwner已覆盖管理面和回执写入。
- `runtimeInputs.backupBlob`和`backupSha256`来自同一个成功Stage0报告。
- deploy仍有目标RG Contributor、Lock Writer和受约束角色分配权限。

验证模式中，`REPLACE_LOCAL_OPERATOR_UAMI_PRINCIPAL_ID`填写operator UAMI的Principal ID；两个`REPLACE_PG_ADMIN_GROUP_*`字段必须填写另一个真实Entra安全组的显示名和Object ID，不能重复填写UAMI名称/Principal ID。这个组不是另一个UAMI：它没有Client ID、凭据或VM挂载，只把现有operator列为成员。不能用Azure RBAC角色分配代替Entra组成员关系，也不能把同一个operator Principal ID直接同时配置成PG管理员和`migrationPrincipalId`；执行器会阻止该权限合并。若客户尚无批准的PG管理员组，由Entra组管理员在客户外部管理终端创建专用安全组并加入operator服务主体：

```bash
PG_ADMIN_GROUP_NAME="llmgw-test-pg-admins"
OPERATOR_OBJECT_ID="REPLACE_LOCAL_OPERATOR_UAMI_PRINCIPAL_ID"

PG_ADMIN_GROUP_OBJECT_ID="$(az ad group create \
  --display-name "$PG_ADMIN_GROUP_NAME" \
  --mail-nickname "$PG_ADMIN_GROUP_NAME" \
  --query id --output tsv)"
az ad group member add --group "$PG_ADMIN_GROUP_OBJECT_ID" \
  --member-id "$OPERATOR_OBJECT_ID"

az ad group show --group "$PG_ADMIN_GROUP_OBJECT_ID" \
  --query '{name:displayName,objectId:id,securityEnabled:securityEnabled}' --output json
az ad group member check --group "$PG_ADMIN_GROUP_OBJECT_ID" \
  --member-id "$OPERATOR_OBJECT_ID" --output json
```

已有批准组时只查询并复用，不重复运行`group create`；先核对`securityEnabled=true`和成员检查`value=true`。组Object ID填`REPLACE_PG_ADMIN_GROUP_OBJECT_ID`，显示名填`REPLACE_PG_ADMIN_GROUP_NAME`。新组成员关系及托管身份Token可能延迟生效，以实际PG Entra登录为准；失败时等待传播并重新登录，不把PG管理员改成migration UAMI绕过分离检查。

没有组管理权限但已有独立database UAMI时，外部管理员把该UAMI挂载到Runner，并确认它在目标RG具有Reader或等效读取权限。该选项固定使用`ServicePrincipal`作为PG管理员，并只让`stage5-database-roles`使用`authentication.database`；platform和后续secrets/restore/schema仍使用operator。database UAMI与`databaseAccess.migrationPrincipalId`必须是两个不同Principal ID。

选择此路径后，先带option重新生成required文件；不能沿用之前未带option生成、仍包含两个`REPLACE_PG_ADMIN_GROUP_*`字段的文件：

```bash
STAGE5_DISCOVERY="$(
  .venv/bin/python -m local_execution.merge_config \
    --config local_execution/customer.json --stage 5 --operation plan \
    --option database-admin-identity
)"
printf '%s\n' "$STAGE5_DISCOVERY" | jq .
STAGE5_REQUIRED_VALUES="$(jq -er '.requiredValues' <<<"$STAGE5_DISCOVERY")"
cp "$STAGE5_REQUIRED_VALUES" local_execution/stage-5-values.local.json
chmod 600 local_execution/stage-5-values.local.json
```

新文件应包含`REPLACE_DATABASE_ADMIN_UAMI_NAME`、`REPLACE_DATABASE_ADMIN_UAMI_CLIENT_ID`和`REPLACE_DATABASE_ADMIN_UAMI_PRINCIPAL_ID`，且不再包含两个组占位符。填写备份引用、operator Principal ID、database UAMI三项标识和PG SKU后，执行完整的预览及应用命令：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 5 --operation plan \
  --values local_execution/stage-5-values.local.json \
  --option database-admin-identity
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 5 --operation apply \
  --values local_execution/stage-5-values.local.json \
  --option database-admin-identity
```

使用PG管理员组的默认路径才运行以下不带option的命令：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 5 --operation plan \
  --values local_execution/stage-5-values.local.json
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 5 --operation apply \
  --values local_execution/stage-5-values.local.json
```

1. 客户模式使用当前Azure部署账号，验证模式使用operator，创建Stage5私有数据资源：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-platform --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-platform --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE5_PLATFORM_PLAN_SHA256"
```

该步骤创建/配置私有Entra-only PostgreSQL、Managed Redis、后台Key Vault、Private Endpoint/DNS、诊断设置和应用Workload Identity；不会恢复数据库。

若旧版本在`send-managed-redis-to-log-analytics`报`CategoryGroup: 'allLogs' is not supported`，本次Stage5父部署为Failed，不能继续`database-roles`。Azure Managed Redis集群资源只提供`AllMetrics`，`default`数据库子资源提供`ConnectionEvents`日志；旧模板把集群误配为`allLogs`。失败时Redis集群/数据库及其他Stage5资源可能已经成功创建，但集群诊断设置整项未创建，因此缺少送往Log Analytics的Redis集群指标。不要删除这些部分成功资源，也不要手工把父部署改成成功。更新到包含“集群`AllMetrics`、数据库`ConnectionEvents`”修复的版本后，重新运行`stage5-platform --operation plan`，审核当前实际状态下的全部增量，再用新plan哈希execute；不得复用失败前的plan哈希。成功后确认父部署为Succeeded，并分别回读两级诊断设置。

2. 创建`llmgw_migrator`、`llmgw_app`及DDL/DML边界。身份取决于前面选择的管理员路径：

- PG管理员组路径：当前Azure CLI账号必须是该组成员；客户IT管理员可保持交互登录，验证operator必须已加入组。
- `database-admin-identity`路径：无需手工切换账号；本地执行器在本步骤前自动执行`az login --identity --client-id <authentication.database.clientId>`，使用独立database UAMI取得PG Token。

客户IT账号在目标RG拥有Azure `Owner`或`Contributor`只代表管理面部署权限，不会自动成为PostgreSQL Entra管理员、数据库owner或schema owner。当前自动化把已迁移业务对象归`llmgw_migrator`所有；需要IT人员直接进入PG时，须另行批准并把该用户配置为额外Entra管理员，或使用包含该用户的PG管理员组，不能用Azure RBAC代替数据库数据面授权。

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-database-roles --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-database-roles --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE5_DATABASE_ROLES_PLAN_SHA256"
```

3. 在此处使用`databaseAccess.migrationPrincipalId`对应的runtime服务身份；验证模式继续使用operator。客户若在`localExecution.authentication.runtime`配置了managed identity，执行器会自动登录；未配置专项runtime profile、而`default=existing`时，必须在运行本步骤前从database UAMI或IT用户会话切换到已挂载的migration/runtime UAMI：

```bash
az logout
az account clear
az login --identity --client-id "REPLACE_MIGRATION_RUNTIME_UAMI_CLIENT_ID" --output none
az account set --subscription "$SUBSCRIPTION_ID"
```

该身份从旧`litellm-env`读取原Master Key/Salt并写入新后台Vault。执行前先确认同一身份能读取旧Secret，但不要打印内容：

```bash
kubectl --kubeconfig "REPLACE_LEGACY_RUNTIME_KUBECONFIG" \
  -n "REPLACE_LEGACY_NAMESPACE" auth can-i get secret/litellm-env

.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-backend-secrets --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-backend-secrets --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE5_BACKEND_SECRETS_PLAN_SHA256"
```

若`auth can-i`不是`yes`，由旧AKS管理员按[主手册Stage5旧Secret读取步骤](../docs/customer-migration-guide-zh.md#5-migration模式核对旧aks-secret读取)授予精确读取权；不要把Secret正文复制到customer.json或终端日志。

4. 保持上述迁移服务身份登录，下载Stage0备份、核对SHA256，并只恢复到绑定的空目标库：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-restore-target --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-restore-target --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE5_RESTORE_TARGET_PLAN_SHA256"
```

目标库非空时脚本会拒绝覆盖。不得手工加`--clean`、DROP数据库或改连任意DATABASE_URL来强行重跑。

5. 保持迁移服务身份登录，执行固定Schema迁移并保存回执：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-schema-migrate --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage5-schema-migrate --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE5_SCHEMA_MIGRATE_PLAN_SHA256"
```

6. Runner VM核验Stage5部署输出及公网关闭状态：

```bash
ENVIRONMENT_NAME="test"
az deployment group show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --name "llmgw-${ENVIRONMENT_NAME}-s5-platform" \
  --query '{state:properties.provisioningState,platform:properties.outputs.platform.value}' \
  --output json

PG_NAME="$(az deployment group show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --name "llmgw-${ENVIRONMENT_NAME}-s5-platform" \
  --query properties.outputs.platform.value.postgresqlServerName --output tsv)"
az postgres flexible-server show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TARGET_RG" --name "$PG_NAME" \
  --query '{state:state,host:fullyQualifiedDomainName,auth:authConfig,public:network.publicNetworkAccess}' \
  --output json
```

通过条件：部署Succeeded；PG为Ready、`activeDirectoryAuth=Enabled`、`passwordAuth=Disabled`、公网Disabled；恢复和Schema摘要均`applied=true`；新Vault中两份秘密存在且版本固定，但日志中没有秘密值；Redis和应用身份授权与plan一致。完成受控解密、Redis Token和CSI轮换实测后再进入Stage6。

### Stage6：发布新LiteLLM后端

**客户模式使用当前登录账号；验证模式使用operator UAMI。** 它已获得新AKS Cluster User/Cluster Admin；Stage5数据库、秘密和Schema回执必须属于当前代码和配置。

1. 将Stage4镜像步骤输出的完整digest和实际模型deployment填入Stage6 values文件；`connectionAlias`必须匹配Stage4模型账号，然后合并：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 6 --operation plan \
  --values local_execution/stage-6-values.local.json
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 6 --operation apply \
  --values local_execution/stage-6-values.local.json
```
2. 计划并发布后端：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage6-application --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage6-application --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE6_APPLICATION_PLAN_SHA256"
```

该步骤再次用本地Cosign公钥验证镜像签名，生成无密码PG/Redis/CSI配置，发布双副本、PDB、HPA和NetworkPolicy；不发布API/admin代理，也不切流量。

3. Runner VM验证：

```bash
VERIFY_KUBECONFIG="$(mktemp)"
az aks get-credentials --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" --file "$VERIFY_KUBECONFIG" --overwrite-existing
kubelogin convert-kubeconfig --kubeconfig "$VERIFY_KUBECONFIG" -l azurecli
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n litellm rollout status \
  deployment/litellm --timeout=15m
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n litellm get deployment litellm \
  -o 'custom-columns=READY:.status.readyReplicas,DESIRED:.spec.replicas,IMAGE:.spec.template.spec.containers[0].image'
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n litellm get pdb,hpa,networkpolicy
rm -f "$VERIFY_KUBECONFIG"
```

通过条件：Ready副本等于Desired且至少2个；镜像是批准digest；Pod重建后会话/Redis行为正常；应用身份只能DML不能DDL；新版没有连接旧PG。完成副本故障、容量和模型调用实测后进入Stage7。

## 6. Stage7启用或延期

若保持`entraMode=deferred`，在Stage6停止。可以提前执行`stage7-promote-proxy-image`准备镜像，但不能部署代理或发布后续流量。

客户批准Stage7后，将`entraMode`改为`enabled`。Free租户还要确认Security Defaults、管理员MFA和实际客户端现代OAuth兼容性；客户明确要求按应用/组/位置细分策略时，先取得Entra ID P1，不以关闭MFA换取测试成功。

**客户登录账号或operator负责deploy/runtime；Stage7额外使用entra-bootstrap和entra-access。** 开始前确认：

- 两类Entra UAMI已按第2.5节获得Graph应用权限和Admin consent。
- 验证operator仍有目标RG临时Owner；两个Entra身份在目标RG有Contributor用于保存回执。
- 客户最小权限模式下，runtime、entra-bootstrap和entra-access使用`LLMGW Runtime Receipt Writer`，deploy保留Lock Writer和受约束角色分配权限。
- Stage6后端Ready，API/admin证书和私有入口仍正常。

1. deploy身份构建、扫描、签名并推送代理镜像：

```bash
read -r -s -p 'Cosign key password: ' COSIGN_PASSWORD
printf '\n'
export COSIGN_PASSWORD
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-promote-proxy-image --operation execute
unset COSIGN_PASSWORD
```

保存输出完整digest，在Stage7 values文件填写代理digest、真实调用客户端Application Client ID、API用户/服务主体Object ID、管理员用户Object ID及两个Entra身份，然后合并：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 7 --operation plan \
  --values local_execution/stage-7-values.local.json
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 7 --operation apply \
  --values local_execution/stage-7-values.local.json
```

2. deploy身份创建API/admin代理Workload Identity、独立Vault、Private Endpoint/DNS和最小Vault角色：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-proxy-foundation --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-proxy-foundation --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_PROXY_FOUNDATION_PLAN_SHA256"
```

3. entra-bootstrap身份创建本方案自有的API/admin应用及service principal，不授予用户访问：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-entra-apps --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-entra-apps --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_ENTRA_APPS_PLAN_SHA256"
```

4. entra-access身份只创建配置中声明的app role assignment和逐用户`llm.invoke`委托授权：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-entra-access --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-entra-access --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_ENTRA_ACCESS_PLAN_SHA256"
```

5. entra-bootstrap身份创建admin OIDC凭据并立即写入私有admin Vault；秘密正文不进入输出：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-admin-credentials --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-admin-credentials --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_ADMIN_CREDENTIALS_PLAN_SHA256"
```

6. runtime身份通过本机loopback到AKS后台初始化配置中声明的管理用户和Key，不创建普通API客户端vkey：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-proxy-credentials --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-proxy-credentials --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_PROXY_CREDENTIALS_PLAN_SHA256"
```

7. runtime身份再次验证代理镜像签名，发布API/admin代理和更新后的后端网络策略：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-application --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage7-application --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_APPLICATION_PLAN_SHA256"
```

8. Runner VM验证：

```bash
VERIFY_KUBECONFIG="$(mktemp)"
az aks get-credentials --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" --file "$VERIFY_KUBECONFIG" --overwrite-existing
kubelogin convert-kubeconfig --kubeconfig "$VERIFY_KUBECONFIG" -l azurecli
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n litellm rollout status \
  deployment/llm-api-proxy --timeout=15m
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n litellm rollout status \
  deployment/llm-admin-proxy --timeout=15m
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n litellm get deployment \
  litellm llm-api-proxy llm-admin-proxy
rm -f "$VERIFY_KUBECONFIG"
```

再用客户批准的真实客户端做正反向测试：正确tenant+Token+vkey成功；缺Token、错误tenant、未授权主体、缺vkey、越权模型和预算超限均拒绝；admin域名只能从批准私网访问。Free租户无法提供的细粒度Conditional Access不能写成已通过。

下列维护动作不属于首次部署。只有对应变更获批时才执行；每个动作的execute哈希都来自它自己的plan，不能互换：

```bash
# 撤销配置中已disabled主体的授权
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-entra-revoke --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-entra-revoke --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_ENTRA_REVOKE_PLAN_SHA256"

# 轮换admin OIDC应用密码
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-admin-credentials-rotate --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-admin-credentials-rotate --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_ADMIN_ROTATE_PLAN_SHA256"

# 清理Graph成功但Vault未记录的孤立密码
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-admin-credentials-recover --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-admin-credentials-recover --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_ADMIN_RECOVER_PLAN_SHA256"

# 轮换admin会话密钥；会使现有会话失效，需另行安排代理rollout
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-admin-session-rotate --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-admin-session-rotate --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_ADMIN_SESSION_ROTATE_PLAN_SHA256"

# 退役已过期且非当前的Graph密码
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-admin-credentials-retire --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage7-admin-credentials-retire --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE7_ADMIN_RETIRE_PLAN_SHA256"
```

Graph成功但Vault失败时先运行recover检查孤立凭据，不盲目重跑初始化。轮换后必须重新plan/execute `stage7-application`，让固定Secret版本和Pod实际使用新值。

## 7. Stage8

一期默认使用原生Spend Logs。**执行身份：deploy负责可选观测基础设施和镜像，runtime负责应用发布。** Stage7必须完成，`entraMode=deferred`时执行器会阻止Stage8应用发布。

### 7.1 原生Spend Logs路径

1. Stage2已经配置`contentAudit`。不启用独立collector时直接跳到第3步。
2. 启用collector时，deploy身份导入固定digest、扫描并签名，再部署私有Application Insights/AMPLS相关资源：

```bash
read -r -s -p 'Cosign key password: ' COSIGN_PASSWORD
printf '\n'
export COSIGN_PASSWORD
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-promote-collector-image --operation execute
unset COSIGN_PASSWORD
```

将输出完整digest填入Stage8 values文件，用`observability`选项合并，再部署：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 8 --operation plan \
  --values local_execution/stage-8-values.local.json --option observability
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 8 --operation apply \
  --values local_execution/stage-8-values.local.json --option observability
```

然后执行：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-observability --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-observability --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE8_OBSERVABILITY_PLAN_SHA256"
```

3. runtime身份发布Stage8应用配置；该步骤会再次验证后端、代理及可选collector签名：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-application --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-application --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE8_APPLICATION_PLAN_SHA256"
```

4. 用批准测试内容发起一次真实请求，在原生UI Logs按时间、用户、Key和request ID核对请求/响应；再确认留存配置。启用collector时额外执行：

```bash
VERIFY_KUBECONFIG="$(mktemp)"
az aks get-credentials --subscription "$SUBSCRIPTION_ID" --resource-group "$TARGET_RG" \
  --name "$TARGET_AKS" --file "$VERIFY_KUBECONFIG" --overwrite-existing
kubelogin convert-kubeconfig --kubeconfig "$VERIFY_KUBECONFIG" -l azurecli
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n litellm rollout status \
  deployment/otel-collector --timeout=15m
kubectl --kubeconfig "$VERIFY_KUBECONFIG" -n litellm get deployment otel-collector
rm -f "$VERIFY_KUBECONFIG"
```

通过条件：正常请求写入Spend Logs且正文范围符合政策；授权管理员可查、未授权主体不可查；过期清理和恢复边界已记录；可选collector双副本Ready且Azure Monitor收到脱敏遥测。

### 7.2 增强L3替代路径

只有客户明确批准增强L3时使用。合并器会删除`contentAudit`并加入独立审计Team映射和审计读取者：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 8 --operation plan \
  --values local_execution/stage-8-values.local.json --option enhanced-l3
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 8 --operation apply \
  --values local_execution/stage-8-values.local.json --option enhanced-l3
```

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-audit-foundation --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-audit-foundation --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE8_AUDIT_FOUNDATION_PLAN_SHA256"

.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-audit-storage --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-audit-storage --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE8_AUDIT_STORAGE_PLAN_SHA256"

.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-application --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage8-application --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE8_L3_APPLICATION_PLAN_SHA256"
```

`audit-pause → audit-recover → audit-resume`只用于受控恢复演练或事故处理：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage8-audit-pause --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage8-audit-pause --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE8_AUDIT_PAUSE_PLAN_SHA256"

.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage8-audit-recover --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage8-audit-recover --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE8_AUDIT_RECOVER_PLAN_SHA256"

.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage8-audit-resume --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json \
  --step stage8-audit-resume --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE8_AUDIT_RESUME_PLAN_SHA256"
```

恢复分页时将上一页非秘密`nextCursor`填入`runtimeInputs.auditRecoveryCursor`后重新plan；未成功resume前禁止再次发布应用或切流。

## 8. Stage9

Stage9分为“禁流量准备”和“正式切流”两个窗口。**执行身份：deploy创建origin/edge，runtime绑定代理和修改Azure DNS。** `stage9-edge-bind`需要Stage7代理；Entra延期时只能准备origin和禁流量edge。

### 8.1 禁流量准备

1. deploy仍需读取AKS节点RG中的内部LB；保留Stage4窗口的节点RG读取/部署权限。默认合并禁流量prepare；使用Azure DNS时追加`--option azure-dns`：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 9 --operation plan \
  --values local_execution/stage-9-values.local.json
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 9 --operation apply \
  --values local_execution/stage-9-values.local.json
```

`privateLinkServiceId`和LB名称可保留模板中的`auto`。
2. 创建Private Link Service源站：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-origin --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-origin --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE9_ORIGIN_PLAN_SHA256"
```

3. 创建默认禁流量、WAF Detection的Front Door/edge：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-edge-prepare --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-edge-prepare --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE9_EDGE_PREPARE_PLAN_SHA256"
```

4. runtime身份把实际Front Door ID绑定到API代理，但仍不启流量：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-edge-bind --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-edge-bind --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE9_EDGE_BIND_PLAN_SHA256"
```

5. 检查origin部署输出、Private Link审批、源站TLS、错误Host拒绝、admin不进入Front Door，并运行实际客户端回归。此时生产DNS仍指向旧环境，edge route不能承载业务流量。

### 8.2 最终数据和发布批准

当前没有自动化“最终停写并覆盖演练库”的动作。客户必须在批准维护窗口手工完成：旧入口停止新写入、排空请求、取得最终备份、选择干净最终目标库、恢复/迁移、用户/Team/Key/预算/模型配置对账以及新环境协议测试。未完成这些动作时不得继续。

完成主手册第5节的最终停写、最终恢复、对账和回退批准后，在customer.json设置：

```json
"features": {
  "entraMode": "enabled",
  "allowTrafficRelease": true
},
"releaseReportPath": "temp/customer-private/stage9-release.json"
```

报告必须符合当前Stage9 release校验器，绑定当前revision、Stage9配置哈希、Front Door ID、私有源站、phase、实际检查和批准人。文件只放受控且Git忽略的位置。

推荐使用合并器打开最终发布开关；若前面使用Azure DNS，此处同时保留`--option azure-dns`：

```bash
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 9 --operation plan \
  --values local_execution/stage-9-values.local.json --option approved-release
.venv/bin/python -m local_execution.merge_config \
  --config local_execution/customer.json --stage 9 --operation apply \
  --values local_execution/stage-9-values.local.json --option approved-release
```

deploy身份正式启用获批phase：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-edge-release --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-edge-release --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE9_EDGE_RELEASE_PLAN_SHA256"
```

使用Azure DNS自动切换时，先由客户DNS管理员给当前客户账号或operator UAMI在实际zone授予DNS Zone Contributor，再执行：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-dns-publish --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-dns-publish --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE9_DNS_PUBLISH_PLAN_SHA256"
```

其他DNS提供商由客户DNS管理员执行等效变更，不修改脚本猜测API。切流后立即验证真实客户端、企业Token+vkey、错误率/延迟、PG连接、Redis、预算和Spend Logs。

### 8.3 回退和旧环境逐步下线

观察期内若新环境不达标，先暂停新写入。仅DNS需要回退且新库没有独有写入时，执行独立的DNS rollback：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-dns-rollback --operation plan
.venv/bin/python -m local_execution \
  --config local_execution/customer.json \
  --step stage9-dns-rollback --operation execute \
  --approved-plan-sha256 "REPLACE_STAGE9_DNS_ROLLBACK_PLAN_SHA256"
```

DNS rollback不要求重新打开`allowTrafficRelease`，但只恢复已有checkpoint；它不回迁新库独有数据。新库已有Key、预算或日志写入时，由DBA先对账再决定恢复旧写入或修复新系统。

客户批准的稳定观察期结束后再软下线旧环境：

1. 确认生产DNS只指向新edge，旧入口连续无请求/写入，新系统不依赖旧AKS、旧PG或旧Key Vault。
2. 保留旧PG/PVC、Stage0及最终备份、原Master/Salt和旧部署快照。
3. 旧AKS为本项目专用且无其他工作负载时，由客户管理员执行可逆停止；共享集群不得运行此命令：

```bash
LEGACY_RG="REPLACE_LEGACY_RESOURCE_GROUP"
LEGACY_AKS="REPLACE_LEGACY_AKS_NAME"
az aks stop --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$LEGACY_RG" --name "$LEGACY_AKS"
az aks show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$LEGACY_RG" --name "$LEGACY_AKS" \
  --query '{state:provisioningState,power:powerState.code}' --output json
```

4. 回退窗口内需要恢复旧环境时：

```bash
az aks start --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$LEGACY_RG" --name "$LEGACY_AKS"
```

5. 旧资源删除、日志/备份清理、身份撤权和DNS删除必须另立变更单；本地包没有`stop-legacy`或`delete-legacy`步骤，不把“停止”写成“已删除”。

## 9. 输出和现场收尾

默认输出位于`temp/local-stage09/<UTC时间>-<step>-<随机后缀>/`。每次至少审核：

- `local-execution.json`：本地步骤、实际operation、revision、配置哈希和结果。
- `azure-auth.json`：本次选择的profile、认证方式和Azure scope，不含Token。
- `plan/`与`execute/`：实时计划、部署回执或运行时摘要。
- 镜像步骤的`target-image-summary.json`、SBOM、扫描和签名验证结果。

失败可能留下部分Azure、Graph、PG、Vault或Kubernetes状态。先检查本次输出和实际资源，再重新运行同一步；不要删除数据库、Secret或Entra对象来追求幂等。

全部手工操作结束后执行`az logout`和`az account clear`，卸载临时UAMI、删除ACR临时认证和无须保留的明文输出，再按Stage0–1指南恢复Runner服务。恢复在线前确认VM不再挂载高权限UAMI。

## 10. 附录：客户生产最小权限替代方案

本附录用于客户不接受临时高权限operator、希望拆分生产权限时。使用第2.4节单一验证UAMI时不需要创建这些角色。创建者必须在Assignable Scope拥有`Microsoft.Authorization/roleDefinitions/write`。在对应RG → **Access control (IAM) → Add custom role → JSON → Edit**粘贴定义，替换Scope后创建；已有同名角色时先核对，不覆盖其他团队定义。

### 10.1 模型部署角色

每个获批模型RG创建或复用：

```json
{
  "properties": {
    "roleName": "LLMGW Model Deployment Operator",
    "description": "Read model metadata, manage reviewed deployments and approve private endpoint connections in approved model resource groups.",
    "assignableScopes": [
      "/subscriptions/REPLACE_MODEL_SUBSCRIPTION_ID/resourceGroups/REPLACE_MODEL_RESOURCE_GROUP"
    ],
    "permissions": [{
      "actions": [
        "*/read",
        "Microsoft.Resources/deployments/*",
        "Microsoft.CognitiveServices/accounts/privateEndpointConnectionsApproval/action"
      ],
      "notActions": [],
      "dataActions": [],
      "notDataActions": []
    }]
  }
}
```

实际分配在模型RG给获批deploy主体。模型账号上的受约束RBAC Administrator只允许分配/删除`Cognitive Services OpenAI User`（`5e0bd9bd-7b93-4f28-af87-19fc36ad61bd`），推荐再限制接收者类型为ServicePrincipal。

### 10.2 资源锁角色

目标RG创建并分配给获批deploy主体：

```json
{
  "properties": {
    "roleName": "LLMGW Resource Lock Writer",
    "description": "Read, create and update resource locks in the approved gateway resource group; no lock deletion.",
    "assignableScopes": [
      "/subscriptions/REPLACE_SUBSCRIPTION_ID/resourceGroups/REPLACE_TARGET_RESOURCE_GROUP"
    ],
    "permissions": [{
      "actions": [
        "Microsoft.Authorization/locks/read",
        "Microsoft.Authorization/locks/write"
      ],
      "notActions": [],
      "dataActions": [],
      "notDataActions": []
    }]
  }
}
```

### 10.3 运行回执角色

目标RG创建，分配给runtime、entra-bootstrap和entra-access UAMI：

```json
{
  "properties": {
    "roleName": "LLMGW Runtime Receipt Writer",
    "description": "Read and write reviewed ARM deployment receipts in the approved gateway resource group; no resource or deployment deletion.",
    "assignableScopes": [
      "/subscriptions/REPLACE_SUBSCRIPTION_ID/resourceGroups/REPLACE_TARGET_RESOURCE_GROUP"
    ],
    "permissions": [{
      "actions": [
        "Microsoft.Resources/deployments/read",
        "Microsoft.Resources/deployments/write",
        "Microsoft.Resources/deployments/validate/action",
        "Microsoft.Resources/deployments/operations/read"
      ],
      "notActions": [],
      "dataActions": [],
      "notDataActions": []
    }]
  }
}
```

### 10.4 AKS Namespace初始化角色

角色定义的Assignable Scope为目标RG，但实际只在新AKS资源Scope分配给获批runtime主体：

```json
{
  "properties": {
    "roleName": "LLMGW AKS Namespace Bootstrapper",
    "description": "Read and write Kubernetes namespace objects in an approved AKS cluster; no namespace deletion or RBAC management.",
    "assignableScopes": [
      "/subscriptions/REPLACE_SUBSCRIPTION_ID/resourceGroups/REPLACE_TARGET_RESOURCE_GROUP"
    ],
    "permissions": [{
      "actions": [],
      "notActions": [],
      "dataActions": [
        "Microsoft.ContainerService/managedClusters/namespaces/read",
        "Microsoft.ContainerService/managedClusters/namespaces/write"
      ],
      "notDataActions": []
    }]
  }
}
```

### 10.5 deploy受约束角色分配清单

目标RG和外部模型账号的条件授权只应包含实际模板需要的角色：

| 角色 | Role Definition ID | 用途 |
| --- | --- | --- |
| Network Contributor | `4d97b98b-1d4f-4787-a291-c67834d212e7` | AKS控制面使用目标VNet |
| AcrPull | `7f951dda-4ed3-4680-a7ca-43fe172d538d` | AKS kubelet拉取目标ACR镜像 |
| Cognitive Services OpenAI User | `5e0bd9bd-7b93-4f28-af87-19fc36ad61bd` | 应用Workload Identity调用模型 |
| Key Vault Secrets User | `4633458b-17de-408a-b874-0445c86b69e6` | 应用/代理读取各自Vault Secret |
| Key Vault Secrets Officer | `b86a8fe4-44ce-4948-aee5-eccb2c155cd7` | 获批初始化身份写入对应Vault |

条件应同时覆盖角色分配的新增和删除动作。无法安全配置条件时，不授予无条件广域管理员角色，由IAM管理员在部署窗口代执行。

### 10.6 Stage与登录方式速查

| Stage/步骤 | 客户现场 | 实施方验证 |
| --- | --- | --- |
| Stage2决策、Stage3源检查 | 当前账号 | 无Azure身份或operator |
| Stage3–4 | 当前账号 | 单一operator UAMI |
| Stage5 platform | 当前部署账号 | 单一operator UAMI |
| Stage5 database-roles | PG管理员用户/组成员，或独立database UAMI | PG管理员组内operator，或独立database UAMI |
| Stage5 secrets/restore/schema | migrationPrincipalId对应服务身份 | 单一operator UAMI |
| Stage6 application | 当前账号或客户runtime身份 | 单一operator UAMI |
| Stage7代理镜像/foundation/runtime | 当前账号 | 单一operator UAMI |
| Stage7 entra-apps/admin-credentials | 客户专项服务身份 | entra-bootstrap UAMI |
| Stage7 entra-access/revoke | 客户专项服务身份 | entra-access UAMI |
| Stage8–9非Entra步骤 | 当前账号 | 单一operator UAMI |

参考：

- [Microsoft Entra licensing](https://learn.microsoft.com/entra/fundamentals/licensing)
- [Microsoft Entra Security Defaults](https://learn.microsoft.com/entra/fundamentals/security-defaults)