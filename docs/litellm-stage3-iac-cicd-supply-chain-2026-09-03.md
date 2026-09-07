# LiteLLM 安全增强阶段 3：IaC、CI/CD 与供应链基线

> 文档状态：代码与本地门禁完成；GitHub OIDC、Environment审批和云端流水线执行待仓库侧配置  
> 完成日期：2026-09-03  
> 前置条件：阶段2第一轮Gate通过  
> 目标：建立可审查、可重复、可追溯的IaC、Kubernetes应用层和镜像供应链基线  
> 明确边界：本阶段未部署Private AKS、Key Vault、Flexible Server、Managed Redis、WAF或新ACR；未创建或修改APIM

## 1. 执行摘要

阶段3已完成以下代码交付：

- `infra/modules/`可复用Bicep模块骨架；
- `infra/environments/`的dev/test/prod环境入口和参数文件；
- Premium ACR安全模块；
- `deploy/base`和dev/test/prod Kustomize overlays；
- 非Root、只读根文件系统、双副本、probes、resources、PDB和topology spread应用基线；
- Python manifest策略验证；
- Bicep、Kustomize、单元测试和仓库策略统一验证脚本；
- Pull Request CI；
- 手动Azure What-if工作流；
- 手动LiteLLM镜像导入、SBOM、Critical漏洞阻断和Cosign签名工作流；
- 第三方GitHub Actions完整commit SHA固定；
- Dependabot GitHub Actions更新配置。

本地阶段3验证、Bicep编译、参数编译、Kustomize渲染、manifest安全策略和actionlint全部通过。prod环境默认`deployContainerRegistry=false`，Azure What-if没有可执行变更。

## 2. 最终目录

```text
infra/
  README_ZH.md
  modules/
    container-registry/
      main.bicep
  environments/
    main.bicep
    dev/main.bicepparam
    test/main.bicepparam
    prod/main.bicepparam
  backup-storage/       # 阶段0已部署，暂不重构
  monitoring/           # 阶段1已部署，暂不重构

deploy/
  README_ZH.md
  base/
    namespace.yaml
    serviceaccount.yaml
    config.yaml
    deployment.yaml
    service.yaml
    pdb.yaml
    kustomization.yaml
  overlays/
    dev/kustomization.yaml
    test/kustomization.yaml
    prod/kustomization.yaml

scripts/
  validate-stage3.sh
  validate_manifests.py

.github/
  README_ZH.md
  dependabot.yml
  workflows/
    ci.yml
    iac-what-if.yml
    promote-litellm-image.yml

Makefile
```

## 3. Bicep环境和模块

### 3.1 环境边界

`infra/environments/main.bicep`是统一资源组级入口，dev/test/prod通过独立`.bicepparam`提供参数。

环境参数当前统一：

- 区域：`westus`；
- 数据分类：`confidential`；
- ACR名称后缀由`LITELLM_ACR_SUFFIX`环境变量注入；
- `deployContainerRegistry=false`；
- ACR公网访问默认`Disabled`。

没有Secret、Tenant ID、Subscription ID、Client Secret、数据库密码或连接串进入参数文件。

### 3.2 Premium ACR模块

模块声明：

- Premium SKU；
- Admin User禁用；
- 公网访问默认禁用；
- Azure Services绕过禁用；
- 镜像导出禁用；
- 未标记manifest保留7天；
- 资源标签和环境归属。

当前模块没有部署。原因是私有ACR必须先完成阶段4的Private Endpoint、Private DNS和受控runner网络路径。不能先创建一个公网禁用但完全无法导入镜像的生产ACR，也不能为了bootstrap长期开放公网。

ACR Soft Delete没有写入当前稳定API模块；需在目标区域/API验证后作为独立增强，不能用Bicep类型警告掩盖不确定能力。

### 3.3 已运行IaC边界

阶段0备份存储和阶段1监控告警已经运行，暂保持原独立模板，避免为了目录整洁重构已部署资源并制造漂移。后续可在导入、deployment stack或完整状态治理方案明确后再模块化。

## 4. Kustomize应用层

### 4.1 已实现安全控制

- prod/test两副本，dev一副本；
- ClusterIP Service；
- 不创建Ingress、LoadBalancer或NodePort；
- 专用ServiceAccount；
- `automountServiceAccountToken=false`；
- UID/GID `10001`；
- `runAsNonRoot=true`；
- RuntimeDefault seccomp；
- `allowPrivilegeEscalation=false`；
- drop ALL capabilities；
- `readOnlyRootFilesystem=true`；
- 受限`/tmp` emptyDir；
- startup/readiness/liveness probes；
- CPU/内存requests和limits；
- PDB `minAvailable=1`；
- topology spread；
- termination grace和preStop drain；
- Secret逐项引用，不使用`envFrom`；
- Prompt/Response正文默认关闭；
- Spend Logs默认保留7天。

### 4.2 镜像

当前渲染使用阶段2验证的官方LiteLLM `1.98.0` digest：

`sha256:20b5044b619055374061a6d5b7b08754cad75aeabbf82ddf4f69cc0cf80ddaf4`

这只是候选渲染基线，不表示允许从官方registry直接部署生产。正式部署前必须由镜像晋级工作流导入目标ACR、扫描、生成SBOM和签名，再通过Pull Request把overlay更新为ACR digest。

### 4.3 明确未包含

为了避免把阶段4/5目标误报为已实现，本阶段没有创建假占位资源：

- Workload Identity注解和Federation；
- Key Vault CSI与SecretProviderClass；
- 实际runtime Secret；
- NetworkPolicy；
- 私有入口、PLS和WAF；
- Flexible Server和Managed Redis；
- HPA/KEDA；
- 生产模型、目标Router、客户自有Entra认证代理、OSS/Azure Guardrail和审计callback。

## 5. CI门禁

Pull Request和main push运行：

1. Python依赖安装；
2. 11项LiteLLM单元测试；
3. 所有Bicep模板编译；
4. dev/test/prod Bicep参数编译；
5. 三个Kustomize overlay渲染；
6. manifest安全策略；
7. APIM资源声明和明显凭据扫描；
8. Pull Request Dependency Review。

CI仅做静态验证，不登录Azure，不部署资源。

manifest策略强制验证：

- 镜像固定到批准SHA-256 digest；
- 非Root、seccomp、禁止提权、只读根文件系统；
- drop ALL capabilities；
- probes和resources；
- 禁止`envFrom`；
- 仅ClusterIP；
- 不允许Ingress和Secret对象；
- Prompt正文日志关闭。

## 6. Azure OIDC What-if

`iac-what-if.yml`只能手动触发，使用GitHub Environment和OIDC登录Azure，只执行resource group What-if。

每个Environment需配置：

- `AZURE_CLIENT_ID`；
- `AZURE_TENANT_ID`；
- `AZURE_SUBSCRIPTION_ID`；
- `LITELLM_ACR_SUFFIX`。

工作流没有`deployment group create`步骤。Federated Credential和Azure RBAC尚未创建，需要客户或仓库管理员在启用云工作流前按最小权限设置。

## 7. 镜像晋级供应链

`promote-litellm-image.yml`只能手动触发并绑定prod Environment：

1. 拒绝未固定digest的源镜像；
2. 通过Azure OIDC登录；
3. 使用`az acr import`导入；
4. 解析目标不可变digest；
5. 生成SPDX JSON SBOM；
6. Trivy阻断未修复Critical漏洞；
7. Cosign keyless签名；
8. 上传保留30天的SBOM证据；
9. 输出目标digest，但不部署Kubernetes；
10. overlay更新必须走Pull Request。

如果目标ACR只有Private Endpoint，必须使用受控self-hosted runner。不得为GitHub-hosted runner长期开放生产ACR公网。

## 8. Action供应链

所有第三方Action固定到实际40位commit SHA，并保留版本标签注释。当前包括：

- checkout；
- setup-python；
- dependency-review；
- Azure login；
- Syft安装；
- Trivy安装；
- Cosign安装；
- artifact上传。

Dependabot每月提出GitHub Actions升级。升级仍必须通过Dependency Review和Pull Request审批。

## 9. 验证结果

| 验证 | 结果 |
| --- | --- |
| Python单元测试 | 11/11通过 |
| Bicep模板编译 | 全部通过，无诊断 |
| dev/test/prod参数编译 | 全部通过 |
| Kustomize overlay渲染 | 全部通过 |
| Manifest安全策略 | 三个环境全部通过 |
| APIM/凭据仓库策略 | 通过 |
| GitHub Actions actionlint | 通过 |
| prod环境Azure What-if | Succeeded，无Create/Modify/Delete |
| 云资源部署 | 未执行 |
| Kubernetes应用部署 | 未执行 |

统一入口：`bash scripts/validate-stage3.sh`或`make validate`。

### 9.1 Azure配置模板安全修复

阶段3审查发现公开仓库中的 `LiteLLM/azure-openai.json` 曾包含个人Azure订阅ID、资源名和Endpoint。已完成：

- 将真实配置复制到本地忽略文件 `LiteLLM/azure-openai.loc.json`；
- 本地文件权限设为 `600`；
- 将可提交的 `LiteLLM/azure-openai.json`恢复为纯占位模板；
- 使用 `git-filter-repo`从main全部历史中移除旧配置文件，再以安全模板重新加入；
- 清除Foundry同步文档历史中的个人资源名和Endpoint示例；
- 使用`force-with-lease`更新公开远端main，防止覆盖并发提交；
- 验证远端main当前文件和全部可达main历史中相关敏感模式命中数为0；
- 公开仓库查询时fork数量为0；
- 删除清理期间生成、可能含旧历史的本地bundle和patch；
- 新增 `tests/test_config_templates.py`，阻止真实GUID格式订阅ID和未标记Azure OpenAI Endpoint再次进入模板；
- 将模板安全测试加入 `validate-stage3.sh`和CI。

订阅ID和Endpoint不是访问凭据，当前也没有API Key或Token进入该配置；因此本次不涉及模型Key轮换。但历史重写不能保证第三方已经下载的副本被删除，后续仍应遵循最小公开元数据原则。

## 10. 阶段3 Gate

### 10.1 代码Gate已满足

- dev/test/prod边界建立；
- Bicep模块和环境入口可编译；
- Kubernetes应用层可以确定性渲染；
- 安全策略自动验证；
- CI、What-if和镜像晋级工作流已定义；
- Action固定SHA；
- 不包含Secret；
- 没有APIM资源声明；
- 默认不会创建云资源。

### 10.2 仓库/云端启用仍待完成

- 当前文件尚未提交或推送；
- GitHub Actions尚未在远端实际运行；
- GitHub Environments和审批规则尚未配置；
- GitHub OIDC Federated Credential和Azure最小RBAC尚未配置；
- Premium ACR尚未部署；
- 镜像导入、扫描、SBOM和签名工作流尚未真实执行；
- 分支保护和required check尚未配置；
- drift detection目前只有手动What-if，尚未定时运行。

### 10.3 是否允许进入阶段4

允许开始阶段4网络和Private AKS详细设计/IaC，但在创建生产资源前应优先：

1. 审查并提交阶段0-3改动；
2. 在GitHub配置Environment审批和OIDC；
3. 首次运行CI和What-if；
4. 将CI设置为分支保护required check；
5. 明确ACR Private Endpoint和self-hosted runner路径。

在上述仓库治理项完成前，不应自动部署新生产环境。
