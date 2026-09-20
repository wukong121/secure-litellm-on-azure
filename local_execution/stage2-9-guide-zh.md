# Stage2–9本地手工部署指南

> 核对日期：2026-09-17
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

### 2.2 由脚本选择UAMI

客户不能在VM交互登录时，为每类动作配置UAMI Client ID：

```json
"authentication": {
  "default": {"method": "existing"},
  "deploy": {
    "method": "managed-identity",
    "clientId": "REPLACE_DEPLOY_UAMI_CLIENT_ID"
  },
  "runtime": {
    "method": "managed-identity",
    "clientId": "REPLACE_RUNTIME_UAMI_CLIENT_ID"
  },
  "database": {
    "method": "managed-identity",
    "clientId": "REPLACE_DATABASE_UAMI_CLIENT_ID"
  },
  "certificate": {
    "method": "managed-identity",
    "clientId": "REPLACE_CERTIFICATE_UAMI_CLIENT_ID"
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

每个步骤先执行`az login --identity --client-id <对应Client ID>`，再核对customer.json中的租户和订阅。未单独配置的profile回退到`default`；这只是兼容能力，不代表客户应把所有权限合并到一个身份。

| profile | 使用范围 |
| --- | --- |
| `deploy` | 所有Bicep/What-if、镜像推送及Stage9边缘资源 |
| `runtime` | AKS、Key Vault、备份、迁移、应用发布、DNS及edge绑定 |
| `database` | Stage5数据库角色初始化，必须是获批PG Entra管理员或管理员组成员 |
| `certificate` | 可选ACME DNS挑战和API证书Secret |
| `entraBootstrap` | 应用创建及admin凭据生命周期 |
| `entraAccess` | 用户/服务主体角色分配和OAuth授权 |

`entraBootstrap`和`entraAccess`必须配置Client ID，实际Azure CLI Token也必须由对应服务主体/UAMI签发；不能一边填写专项UAMI，一边沿用个人管理员Token。普通个人`az login`仍可用于客户明确批准的通用deploy/runtime/database操作，但权限分离和审计责任由客户记录。

Runner服务必须保持Offline。高权限UAMI不应长期挂载，更不能在Runner恢复在线后保留。由客户外部管理终端在对应步骤前挂载所需UAMI，步骤完成后卸载；UAMI资源ID用于挂载，Client ID用于本地登录，Principal ID用于RBAC和客户配置，不能互换。

```bash
az vm identity assign --ids "REPLACE_RUNNER_VM_RESOURCE_ID" \
  --identities "REPLACE_UAMI_RESOURCE_ID"

# 对应步骤完成后，从外部管理终端执行
az vm identity remove --ids "REPLACE_RUNNER_VM_RESOURCE_ID" \
  --identities "REPLACE_UAMI_RESOURCE_ID"
```

各身份的最小Azure、Kubernetes、PG和Graph权限仍按[客户迁移执行手册](../docs/customer-migration-guide-zh.md)对应Stage准备。本地登录成功不等于拥有数据平面权限。

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
command -v psql pg_restore openssl skopeo syft trivy cosign
az bicep install
```

继续使用受控的`local_execution/customer.json`。Stage2–9客户业务字段按[主手册第2.3节](../docs/customer-migration-guide-zh.md#23-客户json分批准备)分批加入；不要一次复制未决的占位块。`localExecution`只保存本地执行方式和非秘密引用，不保存Token、Master Key、Salt、PEM正文或Cosign口令。

### 4.1 Stage5恢复输入

从同一个成功Stage0备份报告取得Blob名和SHA256：

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
read -r -s -p 'Cosign key password: ' COSIGN_PASSWORD
printf '\n'
export COSIGN_PASSWORD
# 执行一个镜像步骤
unset COSIGN_PASSWORD
```

本地镜像步骤会构建或导入、推送、解析不可变digest、生成SPDX SBOM、用Trivy阻断可修复CRITICAL、签名并以同一公钥回验。ACR Token只写入本次0700临时目录，并在结束时删除。

### 4.3 Stage2–9两阶段执行

Stage2–9的基础设施和运行时步骤默认只生成plan。每一步都按以下方式操作，不把plan命令的成功当作已部署：

```bash
STEP="REPLACE_STAGE2_TO_9_STEP"
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step "$STEP" --operation plan
```

从该次输出目录的`local-execution.json`取得`result.planSha256`，审核同目录`plan/`中的变更、目标、配置哈希和费用。批准后把实际64位哈希填入新命令：

```bash
.venv/bin/python -m local_execution \
  --config local_execution/customer.json --step "$STEP" \
  --operation execute \
  --approved-plan-sha256 "REPLACE_REVIEWED_64_HEX_PLAN_SHA256"
```

execute会重新生成实时plan；代码、配置、云状态或发布报告变化导致哈希不一致时停止，必须审核新plan。`--operation apply`仅保留给已验证的Stage0–1，本地执行器会拒绝用它绕过Stage2–9批准。

`stage2-decisions`和`stage3-source-check`是直接检查；三个`promote-*-image`是直接且有副作用的供应链动作，须显式使用`--operation execute`，它们没有ARM plan。

## 5. Stage2–6顺序

所有命令从仓库根目录运行。`stage2-decisions`只生成待人工核对清单，不部署资源：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage2-decisions
```

完成网络/容量、身份Owner、PG认证/HA、正文审计和协议范围决策后进入Stage3：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage3-source-check
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage3-platform --operation plan
```

Stage3人工确认源SBOM/扫描、部署身份范围、目标ACR公网关闭及旧环境未受影响。

Stage4按顺序执行：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage4-platform --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage4-certificate-vault --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage4-target-connectivity --operation plan
```

以上每个plan审核后逐项execute。目标DNS部署完成后直接运行只读检查：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage4-target-check
```

该检查先用`deploy`身份回读AKS节点RG/目标RG中的DNS、PE和预期私有IP，再切换`runtime`身份，要求AKS API和ACR登录域名只解析到这些IP、HTTPS远端IP一致，并读取新AKS Deployment。这样不要求runtime身份读取节点RG网络元数据；它仍不证明Kubernetes写权限、ACR镜像拉取授权或Vault/PG/Redis权限。

检查通过后，继续逐项plan、审核和execute：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage4-cluster-bootstrap --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage4-monitoring-onboard --operation plan
```

然后直接执行已批准的后端镜像供应链动作：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage4-promote-backend-image --operation execute
```

自动签发API证书时运行下面的可选步骤；手工导入证书时不运行：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage4-certificate-renew --operation plan
```

两份证书Secret已按主手册导入、Runner可私网读取后发布入口：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage4-private-ingress --operation plan
```

`stage4-aks-ingress-role`只用于入口Service因AKS控制面缺少目标VNet权限而Pending的已确认故障，不是正常顺序步骤：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage4-aks-ingress-role --operation plan
```

将后端镜像步骤输出的不可变引用填入`application.backendImage`。Stage4人工核对私网DNS/出口、真实Runner、镜像签名/SBOM、Workload Identity正反向和API/admin两个私有入口。

Stage5前补齐`stage5Data`、`databaseAccess`和第4.1节恢复输入，再按顺序执行：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage5-platform --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage5-database-roles --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage5-backend-secrets --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage5-restore-target --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage5-schema-migrate --operation plan
```

`stage5-database-roles`使用`database`身份；其余运行时步骤使用`runtime`身份。确认目标库最初为空、备份SHA一致、原Master/Salt可解密、Redis Entra访问及CSI轮换后进入Stage6：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage6-application --operation plan
```

Stage6会用`imageSigning.publicKeyPath`验证后端镜像的本地签名，并要求签名中的revision/environment与当前执行一致。人工完成双副本故障、负载亲和、容量限制和无旧库写入验证。

## 6. Stage7启用或延期

若保持`entraMode=deferred`，在Stage6停止。可以提前执行`stage7-promote-proxy-image`准备镜像，但不能部署代理或发布后续流量。

客户批准Stage7后，将`entraMode`改为`enabled`，补齐`entra`、`proxy`和两类Entra身份。Free租户还要确认Security Defaults状态、管理员MFA、实际客户端是否支持现代OAuth，以及是否存在必须使用P1 Conditional Access的控制。

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage7-promote-proxy-image --operation execute
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage7-proxy-foundation --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage7-entra-apps --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage7-entra-access --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage7-admin-credentials --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage7-proxy-credentials --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage7-application --operation plan
```

代理镜像步骤输出的digest须先填入`proxy.image`。Entra bootstrap/access UAMI必须由租户管理员预先授予现有设计要求的Microsoft Graph应用权限及管理员同意；Azure订阅Contributor不能替代Graph权限。

下列是维护/回退动作，不属于首次正常部署顺序：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage7-entra-revoke --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage7-admin-credentials-rotate --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage7-admin-credentials-recover --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage7-admin-session-rotate --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage7-admin-credentials-retire --operation plan
```

Stage7人工验证租户负向测试、对象所有权、admin私网隔离和实际首发客户端协议。Free租户无法提供的细粒度Conditional Access不得写成已通过。

## 7. Stage8

一期原生Spend Logs路径只需发布Stage8应用。启用可选collector时，先导入固定digest并部署观测资源：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage8-promote-collector-image --operation execute
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage8-observability --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage8-application --operation plan
```

将collector步骤输出的不可变引用填入`observability.collectorImage`。`stage8-application`的plan会再次以`imageSigning.publicKeyPath`验证Collector签名及revision/environment注解；不是只信任晋级时报告。未配置observability时跳过前两项，只运行`stage8-application`。`entraMode=deferred`时该应用步骤会被阻止，因为现有Stage8应用包含Stage7代理。

只有客户明确选择增强L3时才运行以下基础设施和维护动作；原生Spend Logs客户全部跳过：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage8-audit-foundation --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage8-audit-storage --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage8-audit-pause --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage8-audit-recover --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage8-audit-resume --operation plan
```

恢复分页时把上页报告的非秘密`nextCursor`填入`runtimeInputs.auditRecoveryCursor`后再次运行recover。审计窗口未恢复前不能继续应用发布。

## 8. Stage9

先创建不启业务流量的源站和边缘资源：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage9-origin --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage9-edge-prepare --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage9-edge-bind --operation plan
```

`stage9-edge-bind`需要Stage7代理，因此Entra延期时会被阻止；但前两个禁流量基础设施步骤可以提前完成。

只有完成主手册第5节的最终停写、最终备份/恢复、对账、回退方案和批准报告后，才在customer.json中设置：

```json
"features": {
  "entraMode": "enabled",
  "allowTrafficRelease": true
},
"releaseReportPath": "temp/customer-private/stage9-release.json"
```

报告必须符合当前Stage9 release校验器，绑定当前revision、Stage9配置哈希、Front Door ID、私有源站、phase、实际检查和批准人。文件只放受控且Git忽略的位置。

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage9-edge-release --operation plan
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage9-dns-publish --operation plan
```

DNS回退必须使用单独命令，不能把它描述为数据库回退：

```bash
.venv/bin/python -m local_execution --config local_execution/customer.json --step stage9-dns-rollback --operation plan
```

回退仍须审核plan并用其哈希execute，但不要求把`allowTrafficRelease`重新设为true；底层只允许恢复已有DNS发布checkpoint，不把回退开关变成新的发布授权。

当前没有本地`stop-legacy`步骤。观察期结束后停旧仍需独立批准，并保留旧PG/PVC、备份及Master/Salt恢复材料。

## 9. 输出和收尾

默认输出位于`temp/local-stage09/<UTC时间>-<step>-<随机后缀>/`。每次至少审核：

- `local-execution.json`：本地步骤、实际operation、revision、配置哈希和结果。
- `azure-auth.json`：本次选择的profile、认证方式和Azure scope，不含Token。
- `plan/`与`execute/`：实时计划、部署回执或运行时摘要。
- 镜像步骤的`target-image-summary.json`、SBOM、扫描和签名验证结果。

失败可能留下部分Azure、Graph、PG、Vault或Kubernetes状态。先检查本次输出和实际资源，再重新运行同一步；不要删除数据库、Secret或Entra对象来追求幂等。

全部手工操作结束后执行`az logout`和`az account clear`，卸载临时UAMI、删除ACR临时认证和无须保留的明文输出，再按Stage0–1指南恢复Runner服务。恢复在线前确认VM不再挂载高权限UAMI。

参考：

- [Microsoft Entra licensing](https://learn.microsoft.com/entra/fundamentals/licensing)
- [Microsoft Entra Security Defaults](https://learn.microsoft.com/entra/fundamentals/security-defaults)