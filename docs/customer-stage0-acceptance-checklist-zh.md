# Stage0四项验收操作指南

> 核对日期：2026-09-14。适用于本仓库的既有LiteLLM迁移路径，不适用于greenfield新建路径。
>
> 本文指导人工完成`inventory`、`backup_restore`、`key_salt_recovery`、`protocol_baseline`，对应[主执行手册](customer-migration-guide-zh.md#0-c-按顺序运行)的S0-10。没有新增workflow，也不自动生成passed。

阅读顺序：准备与执行位置 → 盘点 → 审核备份 → 核验原Master/Salt → 旧客户端测试 → 填写confirm。四项都有实际证据才能确认；已经做过且范围、版本、时效仍适用的检查，可以复核已有记录，不必重建资源或重复备份。

## 1. 准备与执行位置

### 1.1 先区分三种操作

| 操作 | 执行位置/身份 | 前提与副作用 |
| --- | --- | --- |
| 审核GitHub加密附件 | 有本仓库和Python依赖的获准管理机 | 不需要Azure登录或访问私网；需要原artifact key；解密会在本机产生受控明文文件 |
| 查询Azure、Kubernetes、Blob | 满足客户设备/登录策略且有对应网络的管理环境 | Azure读取、Kubernetes读取或指定Pod exec、Blob数据读取分别授权；SSH登录Runner不等于Azure登录 |
| 测试旧入口 | 用户实际使用的Codex/SDK等客户端所在设备 | 使用获准旧入口、原认证方式、测试Key/模型与无敏感测试内容；会产生少量推理费用和请求日志 |

先运行主手册S0-01至S0-08。S0-08成功只是备份操作成功，不能替代本次人工验收。若只想先看清单，可先运行`Customer stage acceptance / test / stage=0 / draft`，这不部署、不表示通过。

**遇到访问阻塞时：** Runner的`runneradmin`与Actions运行身份不是同一个身份。个人设备代码登录被Entra要求受管设备时，改由客户批准的管理设备/人员完成核验；不复制Actions Token、不创建长期密钥、不取消条件访问或开启Storage公网。没有合规访问路径时标记该项“待核验”，由客户IT提供路径或单独批准诊断自动化，不靠填写表单越过检查。

### 1.2 值从哪里取得

以下均为本机shell变量或私下记录，不是新增GitHub Variable。使用Bash，先在仓库根目录关闭命令追踪；只替换公开标识占位符，秘密不要写进命令或聊天。

```bash
set +x
umask 077
SUBSCRIPTION_ID="REPLACE_SUBSCRIPTION_ID"
TENANT_ID="REPLACE_TENANT_ID"
LEGACY_RG="REPLACE_LEGACY_RESOURCE_GROUP"
LEGACY_AKS="REPLACE_LEGACY_AKS_NAME"
LEGACY_NAMESPACE="REPLACE_LEGACY_NAMESPACE"
CASE_ID="stage0-review-20260914-01"
mkdir -p "temp/$CASE_ID"
```

| 值 | 来源 |
| --- | --- |
| SUBSCRIPTION_ID、TENANT_ID | 客户配置`azure.subscriptionId`、`azure.tenantId`，并与Azure当前登录范围核对 |
| LEGACY_RG、LEGACY_AKS、LEGACY_NAMESPACE | 客户配置`legacy.resourceGroup`、`legacy.aksClusterName`、`legacy.namespace`；同时与旧环境实际值核对 |
| CASE_ID | 本次验收的本地记录目录名，示例日期/编号需替换；不要与旧验收目录混用 |
| BACKUP_RUN_ID | **成功的S0-08**运行URL中`/actions/runs/`后的数字，不是job ID、draft ID或ZIP文件哈希 |
| DRAFT_RUN_ID | 本次Stage0 acceptance draft的运行ID，与备份ID分别记录 |
| 旧入口、模型别名、客户端版本 | 用户现有客户端的生效配置与旧LiteLLM管理页面；不是尚未部署的新API域名，不猜模型部署名 |
| 原Master/Salt保管位置 | 客户密码库/获准密钥库的记录与版本，由保管人取回；不是GitHub的WORKFLOW_ARTIFACT_KEY |

客户值和检查结果只存受控位置，不能提交公共Git。`temp/`被仓库忽略，但Git忽略不是磁盘加密或访问控制；还需符合客户管理机和证据保管要求。

## 2. inventory：盘点与核对

### I-1 核对租户、订阅和旧集群

在已通过客户登录策略的管理终端执行：

```bash
az account show --query '{tenantId:tenantId,subscriptionId:id,identity:user.name}' --output json
az aks show --subscription "$SUBSCRIPTION_ID" --resource-group "$LEGACY_RG" --name "$LEGACY_AKS" \
  --query '{name:name,resourceGroup:resourceGroup,kubernetesVersion:kubernetesVersion,state:provisioningState,power:powerState.code}' \
  --output json
```

逐项比较：租户/订阅等于客户配置，集群是旧业务集群而非新AKS或Runner，状态符合既有运行预期。记录时间、身份与结果；不一致就停止，不能用改客户JSON的方式冒充已检查另一个环境。

### I-2 取得独立的非admin kubeconfig

有权获取用户凭据且能到达旧AKS API的管理员执行。使用本次私有文件，不覆盖默认context；文件存在时先检查是不是本次上下文，命令可能提示合并，不盲目覆盖：

```bash
PRIVATE_KUBECONFIG="$PWD/temp/$CASE_ID/legacy-kubeconfig"
az aks get-credentials --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$LEGACY_RG" --name "$LEGACY_AKS" --file "$PRIVATE_KUBECONFIG"
chmod 600 "$PRIVATE_KUBECONFIG"
kubelogin convert-kubeconfig --kubeconfig "$PRIVATE_KUBECONFIG" -l azurecli
export PRIVATE_KUBECONFIG LEGACY_NAMESPACE
```

后续命令均显式指定它。不能用`--admin`绕过授权，不能打印`kubectl config view --raw`或上传kubeconfig。非Entra旧集群可能使用证书形式的用户凭据；审批范围由客户集群管理员确认，不能把能获取凭据等同于最小权限已经满足。

**转换时出现本地permission denied：** 先区分文件权限与程序沙箱。这类错误发生在加载kubeconfig时，不能据此判断Azure RBAC不足。以下命令只检查本机元数据，不读取凭据内容：

```bash
id
namei -l "$PRIVATE_KUBECONFIG"
stat -c '%A %a %U:%G %n' "$PRIVATE_KUBECONFIG"
type -a kubelogin
```

文件应属于获准使用它的当前用户，保持`600`；本次私有目录保持`700`，父目录须可进入。若文件由另一个用户创建，应由有权管理员核对来源与属主，不递归更改整个仓库权限。若这些权限正常，但`kubelogin`来自`/snap/bin/kubelogin`，继续查看本机内核审计日志：

```bash
journalctl -k --since '20 minutes ago' --no-pager -o cat \
  | grep -E 'apparmor="DENIED".*profile="snap\.kubelogin\.kubelogin"'
```

若日志中拒绝读取的`name`正是该kubeconfig，且`requested_mask="r"`，说明Snap的AppArmor沙箱拦截了文件访问；本项目演练遇到过此情况。没有日志或无权读日志不代表不存在拦截，可请主机管理员核验。不要使用`chmod 777`、`sudo kubelogin`或关闭AppArmor来解决，也不要把文件搬到公开可读目录。

**确认是Snap限制后，安装官方工具到用户目录。** 以下在运行kubelogin的同一台管理机执行，不是给AKS Pod安装工具，不需要sudo。`az aks install-cli`会同时下载Azure官方kubelogin和kubectl；安装前检查目标路径，已有工具时先确认是否允许替换。`KUBECTL_VERSION`取I-1中已核对的集群版本对应的获批kubectl发行版本（例如`v1.35.0`仅为格式示例）；遵守kubectl与API Server小版本相差不超过1的兼容范围，不盲目安装latest。

```bash
KUBECTL_VERSION="REPLACE_APPROVED_KUBECTL_VERSION"
mkdir -p "$HOME/.local/bin"
az aks install-cli \
  --client-version "$KUBECTL_VERSION" \
  --kubelogin-version v0.2.19 \
  --kubelogin-install-location "$HOME/.local/bin/kubelogin" \
  --install-location "$HOME/.local/bin/kubectl"
```

安装成功后在**同一个终端**调整PATH并清除旧命令路径缓存：

```bash
export PATH="$HOME/.local/bin:$PATH"
hash -r
command -v kubelogin
kubelogin --version
command -v kubectl
kubectl version --client
```

两个路径应位于`$HOME/.local/bin`，不再是`/snap/bin`；若仍显示alias/function，用`type -a`核对本机命令解析。只用完整路径运行一次转换不够，后续kubectl的认证插件也可能按PATH寻找kubelogin。此PATH设置仅对当前终端及子进程生效，新终端需重新设置，或按客户主机管理规范配置用户PATH；不要修改共享Runner服务的环境来修复个人管理终端。

保持原来的PRIVATE_KUBECONFIG值，重试并继续I-3：

```bash
kubelogin convert-kubeconfig --kubeconfig "$PRIVATE_KUBECONFIG" -l azurecli
export PRIVATE_KUBECONFIG LEGACY_NAMESPACE
```

仅修复本地程序路径，不需要重新获取凭据或更改文件的`600`权限。若后续出现Azure登录、条件访问或Kubernetes Forbidden，属于另一层授权问题，仍按客户策略处理，不能以此次修复绕过。

### I-3 核对Deployment、Pod、镜像和PVC

下面只显示元数据，不显示Secret或完整Deployment YAML：

```bash
kubectl --kubeconfig "$PRIVATE_KUBECONFIG" -n "$LEGACY_NAMESPACE" get deployment postgres litellm-mi-proxy \
  -o 'custom-columns=NAME:.metadata.name,DESIRED:.spec.replicas,READY:.status.readyReplicas,IMAGES:.spec.template.spec.containers[*].image'
kubectl --kubeconfig "$PRIVATE_KUBECONFIG" -n "$LEGACY_NAMESPACE" get pods \
  -o 'custom-columns=NAME:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,CONTAINERS:.spec.containers[*].name,IMAGES:.status.containerStatuses[*].imageID,DELETING:.metadata.deletionTimestamp'
kubectl --kubeconfig "$PRIVATE_KUBECONFIG" -n "$LEGACY_NAMESPACE" get deployment postgres \
  -o 'jsonpath={range .spec.template.spec.volumes[*]}{.persistentVolumeClaim.claimName}{"\n"}{end}'
kubectl --kubeconfig "$PRIVATE_KUBECONFIG" -n "$LEGACY_NAMESPACE" get pvc \
  -o 'custom-columns=NAME:.metadata.name,STATUS:.status.phase,CAPACITY:.status.capacity.storage,CLASS:.spec.storageClassName'
```

记录旧LiteLLM/PG的版本及**运行时镜像digest**，不要只记录可变tag。上述版本从已批准镜像信息/旧发布记录交叉核验；不能凭仓库示例认定客户版本。核对PG挂载的PVC与`legacy.postgresPvc`一致且Bound、所需副本就绪、Postgres恰好一个正常Pod，无意外终止/重建。如果名称或容器不同，先适配并审批检查命令和迁移脚本，不重命名旧资源以满足示例。

### I-4 核对数据库基本信息

由有权读取旧PG的DBA在批准窗口执行。下例通过postgres Pod内部既有数据库身份，仅查询元数据；Pod exec本身需要授权，不获取数据库密码。先从I-3选取实际正常Postgres Pod名：

```bash
POSTGRES_POD="REPLACE_RUNNING_POSTGRES_POD_NAME"
kubectl --kubeconfig "$PRIVATE_KUBECONFIG" -n "$LEGACY_NAMESPACE" exec "$POSTGRES_POD" -c postgres -- \
  sh -c 'PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=10000" psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SHOW server_version;" -c "SELECT pg_database_size(current_database()) AS database_bytes;" -c "SELECT extname, extversion FROM pg_extension ORDER BY extname;" -c "SELECT count(*) AS public_table_count FROM information_schema.tables WHERE table_schema = '\''public'\'' AND table_type = '\''BASE TABLE'\'';"'
```

保存版本、数据库字节数、扩展和表数量。数据库大小不等于压缩dump大小，也不能只根据大小推出停机时间。大库不默认全表COUNT；关键业务表的计数/配置抽样由DBA按容量和获批范围安排。这个命令不证明数据库所有数据完整，不修改PG/PVC或停业务。

### I-5 核对业务配置与恢复材料位置

从旧LiteLLM的获准管理界面/已有发布记录，记录模型别名与后端映射、必要用户/Team/Key数量或匿名标识、模型ACL和预算策略，以及旧入口与TLS配置。记录Key指纹/记录编号，不导出完整vkey或正文。管理界面没有相应统计时由DBA提供不含秘密的只读结果，不能照搬其他LiteLLM版本的未知表结构。

记录原Master/Salt保管人的责任、密码库记录编号/版本和恢复流程，不记录值。盘点表应能回答“原来运行什么、数据在哪里、谁能恢复、哪些客户端必须保留”，与S0-08的`observations.legacyWorkloads`交叉核对。

**inventory通过标准：** 配置、实际旧环境、版本/存储/业务范围相符且有带时间的盘点记录。只有I-3的Deployment列表或填写完JSON不能独立算全部通过。差异记录待解决，不修改资源掩盖差异。

## 3. backup_restore：备份恢复证据

### B-1 确认所审核的是成功的S0-08

进入客户仓库Actions → Customer private runtime operations → 对应运行，确认`stage=0`、`action=backup-restore`、`operation=execute`、environment正确，整体Success。记录运行ID、完整Git SHA、完成时间和artifact名。Runner checks、backup基础设施deploy和acceptance draft的绿勾都不是备份成功证据。

### B-2 下载、解压和解密报告

在获准管理机的仓库根目录执行，不要求从Runner解密。通过Actions下载`runtime-test-0-<BACKUP_RUN_ID>`的ZIP，确保文件已经位于当前机器。下面采用Bash；每次使用独立目录，不把失败运行的附件与成功运行混在一起：

```bash
BACKUP_RUN_ID="REPLACE_SUCCESSFUL_BACKUP_RUN_ID"
REPOSITORY="REPLACE_OWNER/REPLACE_REPOSITORY"
BACKUP_ZIP="REPLACE_ABSOLUTE_PATH_TO_DOWNLOADED_ZIP"
mkdir -p "temp/$CASE_ID/downloaded-$BACKUP_RUN_ID"
unzip -n "$BACKUP_ZIP" -d "temp/$CASE_ID/downloaded-$BACKUP_RUN_ID"
ls -l "temp/$CASE_ID/downloaded-$BACKUP_RUN_ID/sealed-artifact.json"
```

在同一终端安全输入与生成该附件时相同的Environment Secret，不粘进命令参数。`read -s`只是隐藏输入，仍可粘贴后回车：

```bash
set +x
read -r -s -p "WORKFLOW_ARTIFACT_KEY: " WORKFLOW_ARTIFACT_KEY
printf '\n'
export WORKFLOW_ARTIFACT_KEY
.venv/bin/python -m scripts.workflow_security open \
  --file "temp/$CASE_ID/downloaded-$BACKUP_RUN_ID/sealed-artifact.json" \
  --repository "$REPOSITORY" --run-id "$BACKUP_RUN_ID" \
  --name "runtime-test-0-$BACKUP_RUN_ID" \
  --output-dir "temp/$CASE_ID/backup-$BACKUP_RUN_ID"
unset WORKFLOW_ARTIFACT_KEY
```

解密成功后在VS Code打开该输出目录的`acceptance-report.json`，不在公开Actions中打印全文。不需要为解密重新Azure登录，也不从GitHub Secret界面读取旧key；GitHub不会回显已保存Secret。

**原artifact key丢失怎么办：** 先找获准密码库中的原值，不能重新运行OpenSSL生成新key解旧附件。若只有GitHub Secret还保存原值，现有workflow可以使用它，但**当前没有专门的报告核验/密钥导出workflow**。应停止本地解密，让实施负责人安排独立审批的受控报告核验，或由获准DBA在私网按B-4重新取证；这不是要求立刻重跑有副作用的恢复动作。不要输出Secret、关闭设备策略、删旧附件或直接替换key；缺少可审核证据时该项保持待核验。

### B-3 核对报告字段

| 字段 | 核验方法与期望 |
| --- | --- |
| stage、environment、revision | 是本次Stage0、客户环境、成功运行Git SHA；记录变更过的配置并重新评估证据适用性，不能只改报告哈希 |
| observedAt | 是已批准备份窗口内的观察时间，不是任意旧报告 |
| observations.legacyWorkloads | 原postgres/litellm Deployment与镜像，与I-3相符 |
| observations.fullRestoreSucceeded | 必须为布尔值true |
| observations.backupAccount / backupContainer | 与成功backup部署输出一致，容器为当前模板的litellm-postgresql |
| observations.backupBlob | 格式为pre-change/加32位标识再加.dump；这是本次备份引用，不是任意旧dump |
| observations.backupBytes | 大于0；不是数据库原始容量，也不是GitHub ZIP大小 |
| observations.backupSha256 | 64位十六进制；对应dump，不是镜像digest/plan hash |
| observations.publicTableCount | 大于0，并与相同schema基线合理一致；数量相同也不保证每行完整 |
| observations.restoreSeconds | 恢复容器中pg_restore耗时，仅为迁移估时的一部分 |
| checks | 仍可能全部pending，这是待人工验收报告的正常状态，不能手改为passed |

控制代码在**隔离恢复、上传、下载及哈希比对全部成功后**才写出上述成功观察值，见[backup_restore](../scripts/migration_runtime.py)。它先恢复本地dump，再验证从Blob下载的内容与原dump完全一致，不是再次启动目标生产数据库。

仅有`operation-status.json`或缺少成功观察字段，不能认定备份完成。加密附件有几百字节不代表dump很小：数据库dump从不上传GitHub，附件只放证据。

### B-4 需要实时核对或额外数据抽验时

成功报告证明当次上传并回读，不保证今天Blob仍然存在。客户要求实时核对时，在**已通备份私网、登录获准Blob读取身份**的环境执行；值取B-3报告和backup部署输出，不填SAS或Account Key：

```bash
STORAGE_ACCOUNT="REPLACE_ACCOUNT_FROM_REPORT"
BACKUP_BLOB="REPLACE_BACKUP_BLOB_FROM_REPORT"
az storage blob show --subscription "$SUBSCRIPTION_ID" \
  --account-name "$STORAGE_ACCOUNT" --container-name litellm-postgresql \
  --name "$BACKUP_BLOB" --auth-mode login \
  --query '{name:name,bytes:properties.contentLength,lastModified:properties.lastModified}' --output json
```

名字和大小应与报告一致。403/超时是检查失败，不是空容器；DNS须指向实际PE，不能从未接通的开发机访问公网端点。人工账号被设备策略拒绝时返回1.1解决，不使用Runner服务账号的凭据缓存。

如获批需要额外下载复核，指定尚不存在的本地文件，禁止覆盖已有材料：

```bash
az storage blob download --subscription "$SUBSCRIPTION_ID" \
  --account-name "$STORAGE_ACCOUNT" --container-name litellm-postgresql \
  --name "$BACKUP_BLOB" --auth-mode login --overwrite false \
  --file "temp/$CASE_ID/reviewed-backup.dump" --output none
chmod 600 "temp/$CASE_ID/reviewed-backup.dump"
sha256sum "temp/$CASE_ID/reviewed-backup.dump"
```

将结果与报告哈希比较，哈希只作完整性证据，不是加密。若报告不可读且没有可信的原始哈希，不能把此刻计算出的哈希声称为“与原备份一致”。

S0-08的临时恢复容器已清理；有额外抽验要求时，由DBA单独批准空白隔离PG环境，用同大版本/必要扩展恢复下载副本，记录关键表的计数/配置抽样与备份窗口基线。运行`pg_restore --exit-on-error --no-owner --no-acl`时只指向该隔离环境，不指向旧生产库或Stage5目标库，不使用`--clean`。大表计数需评估负载；持续写入导致的时间差必须解释，不能要求任意两个时刻所有行数完全一致。不得用`pg_restore -l`代替完整恢复，也不得仅比较表数量就声称密文/角色/所有数据验证成功。隔离环境的建立及销毁须有明确范围，不自动删除原dump或生产PVC。

**backup_restore通过标准：** 审核成功的恢复与回读证据、备份范围/时间/引用符合批准要求，并完成客户要求的数据抽验。它不等于旧角色/ACL已复制，也不包含下节的密钥验证。将backupBlob/SHA256保存在受控记录，Stage5使用同一对值。

## 4. key_salt_recovery：原Master与Salt可恢复

### K-1 明确要找的不是artifact key

本仓库旧部署通过`litellm-env`提供`LITELLM_MASTER_KEY`；原部署脚本不会自动创建`LITELLM_SALT_KEY`，只在更新Secret时保留已经存在的Salt，见[旧部署脚本](../LiteLLM/deploy_mi_aks_litellm.py)。因此旧环境可能没有显式Salt，不能假定每个客户都有两个独立的值。后续Stage5代码要求读取这两个字段，见[read_legacy_keys](../scripts/runtime_secrets.py)。这两个值与`WORKFLOW_ARTIFACT_KEY`、普通用户vkey、数据库密码不同。

请旧环境Owner确认真实的配置来源、当前运行版本以及是否有覆盖/回退规则。没有显式Salt时，**不能假设等于Master、随便生成Salt或把空值算通过**，须按旧版本实际行为制定并验证兼容方案。Stage5默认`legacySaltSource=secret`会拒绝缺失值；只有已批准并验证“当前Master Key作为永久Salt”兼容路径的环境，才能显式选择`legacy-master-key-salt`选项。

`ABSENT`仅表示所查Pod进程环境没有该变量，不证明应用未使用其他加密材料，也不证明数据库无法解密。遇到这种情况，停止反复输入所谓Salt：由实施人员核对实际运行镜像版本、加密函数及配置回退规则，再用获准隔离副本验证历史密文与拟定迁移材料。不能仅为满足检查而给旧Secret增加新Salt或重启Pod；本指南不自动实施兼容迁移。

### K-2 从独立保管材料取回

由获准保管人从客户密码库/Secret备份记录取回两个原值，核对记录的环境、用途和版本。取回的是**独立保管副本**，不是刚从正在运行的Pod复制出来再声称“备份恢复成功”。只记录保管系统记录编号、版本、可访问人员、取回时间和恢复路径，不保存明文或普通摘要到验收报告。

若目前没有独立副本，先安排获准的加密保管和恢复演练；不把当前Pod或只存在GitHub Secret中的某个无关key当作全部灾备材料。

### K-3 在受控终端比较副本与运行值

以下为**客户批准秘密读取与Pod exec后**的人工命令，不在公共Actions、录屏、调试器或开启shell trace的终端执行，本文不会替客户运行。只在I-2的已核对kubeconfig范围内执行。客户政策不允许这个方法时，由获准保管人在内部工具完成等价的精确比较并出具记录。

从I-3选择所有当前Ready、未终止的旧LiteLLM Pod，核对容器名为`litellm`。代码在Pod中只读取两个环境变量，父进程只在内存中比较，不打印值/哈希、不写Secret文件、不改变Pod；但秘密会在获准机器内存中存在，不能把它称为无需秘密权限。

```bash
BACKEND_PODS="REPLACE_SPACE_SEPARATED_RUNNING_LITELLM_POD_NAMES"
export BACKEND_PODS
.venv/bin/python - <<'PY'
import getpass
import hmac
import json
import os
import subprocess
import warnings

names = ("LITELLM_MASTER_KEY", "LITELLM_SALT_KEY")
warnings.simplefilter("error", getpass.GetPassWarning)

def verify():
    kubeconfig = os.environ["PRIVATE_KUBECONFIG"]
    namespace = os.environ["LEGACY_NAMESPACE"]
    pods = os.environ["BACKEND_PODS"].split()
    if not pods or any("REPLACE" in pod for pod in pods):
        raise ValueError("Select actual backend pods first")
    saved = {name: getpass.getpass("Recovered " + name + ": ") for name in names}
    if not all(saved.values()):
        raise ValueError("Recovered material is empty")
    read_environment = "import json, os; print(json.dumps({name: os.environ.get(name) for name in ('LITELLM_MASTER_KEY', 'LITELLM_SALT_KEY')}))"
    all_matched = True
    for pod_index, pod in enumerate(pods, 1):
        response = subprocess.run(
            ["kubectl", "--kubeconfig", kubeconfig, "-n", namespace, "exec", pod,
             "-c", "litellm", "--", "python", "-c", read_environment],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if response.returncode:
            raise ValueError("Approved pod read failed")
        actual = json.loads(response.stdout)
        if not isinstance(actual, dict):
            raise ValueError("Invalid runtime response")
        for name in names:
            value = actual.get(name)
            if value is None:
                field_status = "ABSENT"
            elif not isinstance(value, str):
                field_status = "INVALID"
            elif not value:
                field_status = "EMPTY"
            elif hmac.compare_digest(saved[name].encode(), value.encode()):
                field_status = "MATCH"
            else:
                field_status = "MISMATCH"
            print("Pod", pod_index, name + ":", field_status)
            all_matched = all_matched and field_status == "MATCH"
    if not all_matched:
        print("NOT VERIFIED: review the field statuses; existing runtime material was not changed.")
        return 1
    print("MATCH: recovered Master/Salt match the selected runtime pods.")
    return 0

try:
    result = verify()
except (Exception, KeyboardInterrupt):
    print("NOT VERIFIED: check approved access, pod selection and recovered material; no secret output.")
    result = 1
raise SystemExit(result)
PY
```

输入提示通过控制终端隐藏读取，输入原值后回车；不把值写入`export`命令或发给助手。运行环境没有安全TTY时立即停止，不退回可见输入。此命令需要该LiteLLM容器提供`python`；没有时由实施负责人适配已审核的等价方式，不在生产容器临时安装工具。

逐字段结果只显示Pod序号、固定变量名与状态，不显示值、长度或哈希：`MATCH`为精确一致，`MISMATCH`为所输入保管副本与Pod值不一致，`ABSENT`为变量未设置，`EMPTY`为空值，`INVALID`为响应格式异常。旧版示例将这些情况合并成一条“differs or runtime value is absent”，不能据此认定两个输入都错了。仅有`PRESENT`存在性结果不能证明Master匹配；Salt缺失时按K-1处理，不能把Master或artifact key试填进去。

**MATCH的边界：** 只证明所选Pod的环境变量与取回值一致。还需确认选择覆盖全部当前后端副本、应用确实使用这两个环境变量，且配置文件/启动代码没有其他覆盖或兼容回退。若实际来自挂载文件或不同环境变量，停止使用此示例，由Owner按实际来源核验。Secret对象存在但Pod尚未滚动更新，也可能与运行值不同，不能只读Secret对象下结论。

### K-4 记录恢复能力与未覆盖部分

保管人写下：哪个受控副本、何时取回、与哪些旧运行版本核对、结果、谁有权在故障时恢复、材料缺失时联系谁。Master/Salt应能与该批数据库备份配套使用；不只确认“密码库有同名记录”。如客户要求实际密文解密/业务恢复证明，须在批准的隔离恢复副本上使用原版本及原材料验证，由DBA/应用Owner记录结果，不连接外部模型生产资源、不改旧库；不要提前执行Stage5绕过当前阶段。

**key_salt_recovery通过标准：** 独立材料可以受控取回，与旧应用实际使用材料一致，恢复步骤/责任明确；客户要求的隔离恢复核验已完成。K-3不是自动验收，也不证明后续新版所有历史密文都能解密。ABSENT、MISMATCH、访问失败或兼容策略未验证时，保持待核验，不生成替代密钥。

## 5. protocol_baseline：旧入口真实客户端基线

这一项不要求新网关已经上线，也不是让你运行`Customer gateway isolation checks`检查尚不存在的新入口。保留旧入口、旧vkey/认证方式，使用本次迁移后仍需支持的实际客户端；不能提前强加Stage7双凭据协议到旧网关。

### P-1 固定测试对象与预算

1. 记录客户端名称与版本（例如实际Codex CLI版本、VS Code扩展版本、SDK版本），从客户端About/扩展详情或本机版本命令取得。
2. 记录现有base URL、模型别名、认证方式和对应测试账号/Key指纹。只在受控记录保存敏感范围，不记录完整vkey。
3. 与业务Owner确认必须保留的功能、成功标准和测试费用上限。选择无生产秘密的临时工作目录，不给测试客户端高危工具权限。
4. 固定同一套无敏感测试内容；先截图/记录客户端当前配置中的非秘密部分，避免中途自动切回官方服务而误判旧网关通过。

### P-2 按所需功能执行

在**真实客户端**中依次完成下表，记录时间、模型、是否通过及证据编号。使用已有近期记录时也按表复核版本、入口和功能范围。只有确实不在客户首发范围的功能才标“不适用”并说明原因；失败不能改成不适用。

| 项目 | 具体操作 | 通过时应看到 |
| --- | --- | --- |
| 普通请求 | 用现有测试Key与模型发“只回复BASELINE_OK”或等价无敏感请求 | 能得到正常回复，旧网关有对应成功请求记录；不能仅凭health或models列表成功 |
| 连续对话 | 第一轮给一个无敏感标记，第二轮要求回忆该标记 | 同一客户端会话连续调用成功，历史/上下文未丢失，非重新手动拼一条独立请求 |
| 流式与取消 | 开启该客户端流式显示，发较长的无敏感回答请求；观察增量后取消，再发一条短请求 | 持续收到增量，无解析报错；取消后客户端可再次正常请求 |
| 工具调用（若需要） | 在空测试目录创建一份无敏感小文件，让Codex读取并总结；按客户端流程批准只读工具 | 观察到实际工具调用及工具结果后续回复，不是模型口头说已读取；无参数/协议解析错误 |
| 结构化输出（若需要） | 用客户实际SDK/客户端的既有结构化输出选项请求小JSON | 能被该客户端的实际解析器接受；手工要求模型“写JSON”不等于验证response_format |
| 断线恢复/续期（若需要） | 按现有客户端支持方式在批准测试中重连或重新登录，不操作业务网络 | 会话恢复/重新请求行为符合客户预期；不能只重启进程当作自动续期通过 |

不要给第三方上传代码或生产Prompt来做测试。不要通过切换模型、去掉工具或更换协议隐藏失败；差异记录为迁移风险。若客户实际客户端使用WebSocket、对象引用或加密上下文，记录为实际要求；即使旧入口通过，也不代表新代理已支持，Stage7仍需对新入口重新验证。

### P-3 交叉核对请求确实经过旧网关

用客户端显示的request ID/时间窗口与旧LiteLLM的获准请求元数据、Spend Logs或已有访问日志对照。只记请求状态/模型/时间等必要信息，不为此次验收开启正文日志或改变旧系统日志策略。没有日志可读时，由Owner采用获准的替代关联方法；不能仅凭“客户端能回答”就认定走了正确端点。

### P-4 保存基线

将各项结果、失败详情的私有引用、客户端/服务端版本及首发范围整理为记录。已正常使用的客户端不必为了验收重新安装；之前真实测试记录仍适用时可复用。失败先解决旧基线或由Owner明确缩小获批首发范围并更新记录，不能让confirm替技术验证。

**protocol_baseline通过标准：** 客户实际首发范围内的旧入口调用已验证、路径可核对、有版本和结果记录。不要求遍历所有SDK/移动端插件，但必须覆盖客户真正依赖的行为；新网关兼容性另在Stage7检验。

## 6. 记录结果并填写confirm

### 6.1 私有验收记录

在客户受控记录系统维护下面的表格，默认均为“待核验”。秘密、数据库dump、kubeconfig不进入这张表；无需把表格上传公共GitHub。

| 检查ID | 方法/证据引用 | 时间/版本/范围 | 实际结果 | 核验人 | 未覆盖或待处理项 |
| --- | --- | --- | --- | --- | --- |
| inventory | I-1至I-5的盘点记录 | 待填写 | 待核验 | 待填写 | 待填写 |
| backup_restore | 成功备份run ID及B-3报告审核/必要B-4抽验 | 待填写 | 待核验 | 待填写 | 待填写 |
| key_salt_recovery | 独立保管记录版本、K-3比较及恢复方法 | 待填写 | 待核验 | 待填写 | 待填写 |
| protocol_baseline | P-2测试矩阵与P-3关联记录 | 待填写 | 待核验 | 待填写 | 待填写 |

### 6.2 检查draft是否仍适用

确认客户JSON的governance已获批、已同步所选Environment，当前代码与draft一致，draft在7天内且使用同一artifact key。变更相关配置/代码/key后先解决证据适用性并重新draft，不手工改报告哈希；生成新key不会恢复旧key加密的附件。

如没有有效draft，运行`Customer stage acceptance`：`main / test / stage=0 / operation=draft`，其余确认字段留空。它只生成pending清单，不会自动完成I/B/K/P步骤。

### 6.3 按页面标签填写

以下为已配置获批single-operator的路径；双人策略使用已有record流程，不由此文改变审批模式。

| 页面字段/说明 | confirm时填写 |
| --- | --- |
| Use workflow from / environment / stage | main / test / 0，与draft一致 |
| operation | confirm |
| For confirm, successful draft run ID for this stage and revision | 有效Stage0 **draft**运行ID，不是backup-restore、plan或deploy的ID |
| For confirm, every check ID personally verified; comma separated as shown in the draft Summary | 只有四项实际通过后才填下面完整字符串 |
| For confirm, actual observations and evidence references; no secrets or prompt content | 普通文本，12至4000字符，逐项实际结果与允许公开的证据引用；不是JSON |
| For confirm, type the selected environment again | test |

`checked_items`在四项全通过时为：

```text
inventory,backup_restore,key_salt_recovery,protocol_baseline
```

`evidence_notes`按“盘点记录引用及结果；成功备份run ID与报告核验结果；原Master/Salt受控恢复核验记录引用；旧客户端版本/测试矩阵引用及结果”组织。本文不提供伪造已通过的可直接粘贴说明；应从6.1实际记录摘取允许公开的摘要。GitHub公开仓库的workflow输入可能公开，不填内部密码库URL、用户名清单、正文或密钥值。

当前confirm不支持部分通过。任一项目未完成、证据不可获取或结果失败时，不提交confirm，也不能只填部分ID。完成后运行confirm，其成功只代表按已配置操作者记录了验收，不是独立审计或新业务切流批准。

confirm成功产生`acceptance-record-test-0-<run ID>`账本后，才按主手册进入Stage1。补governance等相关配置会改变计划绑定，应重新生成并审核legacy-logging的plan，**不是重建前八步资源**。不要为验收重复执行Stage5恢复/密钥导入，不因旧报告无法解密去清库或轮换业务密钥。

## 7. 收尾与常见阻塞

| 情况 | 如何处理 |
| --- | --- |
| 没有合规管理设备或无法登录Runner Azure | 联系客户IT提供获准设备/身份与私网，不绕过条件访问或改成公网 |
| kubelogin读取本地kubeconfig报permission denied | 按I-2核对属主/父目录及Snap AppArmor日志；确认沙箱拦截后使用官方用户目录工具和正确PATH，不放宽文件权限 |
| 没有原artifact key | 按B-2安排受控报告核验或独立取证，不用新key解旧附件 |
| Secret没有Salt或与保管材料不一致 | 停在K项，核对旧版本/配置来源/兼容策略，不生成替代Salt |
| 只有备份workflow成功 | 审核报告并补I/K/P项，不自动填写四项通过 |
| 客户端测试失败或不能关联旧网关 | 停在P项，记录真实结果及范围，不用健康检查替代 |
| prior-stage-not-confirmed | 确认有同代码/环境/有效配置的Stage0 confirm账本；draft或备份成功不足以解锁 |

结束后按客户保管策略归档必要的脱敏记录，移除本次临时kubeconfig、下载的dump和解密明文；只处理本次目录，先确认没有仍需保留的唯一副本。本文不提供自动删除命令，不删除云端备份、生产Secret、PG/PVC或旧运行记录。生产密钥只留在获准秘密管理系统，不留在终端变量或公开验收输入中。