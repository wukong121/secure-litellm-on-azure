# 安全增强版 LiteLLM on Azure

[English](README.md) | [客户迁移指南](docs/customer-migration-guide-zh.md)

面向客户交付的 Azure 上 [LiteLLM](https://github.com/BerriAI/litellm) 网关部署与迁移项目。方案将 Azure 基础设施即代码、Kubernetes 部署组件、客户自有 Microsoft Entra 认证、审计治理与分阶段交付流程整合在同一仓库。

面向客户运维，交付目标同时覆盖已有 LiteLLM 网关的分阶段增强迁移和空白环境的从零部署。客户提供必要身份、资源ID、域名及决策，部署和验证应通过workflow完成，不要求客户自行编写应用清单或测试代码。模型路由仍受已授权Azure OpenAI资源的配额约束，不绕过服务限额。

> **交付状态**：已提供迁移/新建模式、阶段指导、配置预检、实际基础设施部署和受限私网运行入口；应用自动生成、运行集成和全自动客户验收尚未全部完成。这不是一键原地升级工具，未完成项仍是上线阻断条件。

## 目标架构

**第一阶段审计决策（2026-09-10）**：采用原生 Spend Logs 将获批的Prompt/Response保存在私有PostgreSQL。自建L3采集、正文Blob/HSM及恢复治理服务是可选增强项，不再作为所有客户的首发前提。当前配置仍关闭正文，发布、受控查询和阶段证据门禁尚待适配；详见[基础版方案](docs/litellm-content-audit-phase1-customer-brief-zh.md)及[部署指南](docs/customer-deployment-workflows-zh.md)，不得通过跳过现有门禁启用。

```text
API客户端 -> llm-api.<客户域名> -> Front Door / WAF -> 私有API入口
                                                     -> Entra API认证代理
管理员 -> 私网 llm-admin.<客户域名> -> Entra管理代理
                                                  |
                                       Private AKS上的LiteLLM
                                                  |
                                      Azure OpenAI / Foundry

配套服务：Key Vault、PostgreSQL Flexible Server、Managed Redis、
私有ACR和仅接收必要元数据的监控。批准的正文进入PostgreSQL原生Spend Logs；
独立L3审计存储仅在选择增强方案时部署。
```

| 领域 | 设计与实现范围 |
| --- | --- |
| 网络隔离 | Private AKS、Private Endpoint/DNS、受控出口和默认拒绝网络策略 |
| 身份授权 | Entra认证、API/admin分离、Workload Identity及客户自有授权逻辑 |
| 数据与秘密 | Key Vault/CSI、托管PostgreSQL和Redis模板、备份与恢复控制 |
| 运行基线 | 固定镜像digest、非Root/只读容器、高可用及路由组件 |
| 审计与观测 | 第一阶段原生Spend Logs留痕（接线待适配）、元数据监控；L3、Trace及Guardrail组件按需选用 |
| 客户交付 | Environment配置、OIDC、阶段证据门禁、IaC预览及离线验证 |

以上描述目标能力，不表示全部组件已部署或生产就绪。方案仅使用Azure服务、OSS和客户自有代码，不依赖LiteLLM Enterprise；原APIM部署实现已移除。

## 客户从这里开始

1. 阅读[客户部署与验收指南](docs/customer-deployment-workflows-zh.md)，选择已有网关迁移或从零部署；迁移细节另见[客户迁移指南](docs/customer-migration-guide-zh.md)。
2. 在客户仓库创建受保护的GitHub Environments：`dev`、`test`、`prod`，并建立Environment范围的Azure OIDC身份。
3. 使用[迁移配置模板](config/customer.example.json)或[新建配置模板](config/customer.greenfield.example.json)填写`CUSTOMER_CONFIG_JSON` Environment secret；新建使用`deploymentMode=greenfield`，不填写`legacy`。按部署指南配置部署/运行OIDC身份；阶段证据初始为`[]`。
4. 打开 **Customer staged migration** workflow，先选择阶段`0`、模式`guide`、组件`none`，再逐步执行`preflight`和获准的`what-if`。
5. 迁移在旧网关旁建设隔离新环境；新建Stage0使用`bootstrap → network`，跳过不适用的Stage1。两条路径的实际部署和发布分别经过客户审批，不以基础设施部署成功代替应用验收。

原迁移检查workflow不部署资源；实际执行使用新增的[客户部署与验收工作流](docs/customer-deployment-workflows-zh.md)，提供基础设施plan/deploy、私网备份/恢复和应用发布、单人或双人验收。DNS和旧环境退役不自动执行，运行时集成阻断项仍需完成。Master Key、Salt、数据库凭据和OIDC秘密保存在客户Key Vault，不写入配置JSON。

## 迁移阶段

| 阶段 | 客户里程碑 |
| --- | --- |
| 0-2 | 现状盘点、可恢复备份、旧环境最小加固和架构决策 |
| 3-5 | 供应链、隔离目标基础设施、私网与数据迁移演练 |
| 6-8 | HA/路由、Entra与双域名、协议授权、原生正文留痕与监控；增强审计按需选用 |
| 9 | 批准试点、切流及验证回退窗口；资源退役另立变更 |

回退条件满足前保留旧数据库、网关、身份和Master/Salt路径。禁止将候选版本直接连接旧生产数据库执行自动schema migration。

## 仓库导航

| 路径 | 用途 |
| --- | --- |
| [config](config/customer.example.json) | 通用客户配置与证据示例，不含真实客户值 |
| [.github](.github/README_ZH.md) | 验证、分阶段迁移和镜像晋级workflow |
| [infra](infra/README_ZH.md) | Bicep平台、备份、监控、审计与边缘入口模板 |
| [deploy](deploy/README_ZH.md) | Kustomize基础清单、阶段组件与验证overlay |
| [auth-proxy](auth-proxy/README_ZH.md) | 客户自有Entra API/admin认证代理与审计治理代码 |
| [LiteLLM](LiteLLM/README_ZH.md) | 旧网关参考实现、运维手册及OSS回调适配器 |
| [tests](tests/README_ZH.md) | 离线检查、隔离运行时验证及显式执行的真实环境测试 |
| [scripts](scripts/customer_migration.py) | 客户预检、参数生成和验证工具 |

## 本地验证

需要Python 3.10+、Node.js 24、Azure CLI及Bicep、kubectl和make。OSS回调测试还需要Docker。在仓库根目录运行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm ci --prefix auth-proxy --ignore-scripts
make validate-stage9
make validate-oss-callbacks
.venv/bin/python -m unittest tests.test_customer_migration tests.test_customer_templates tests.test_public_config
```

这些检查不部署资源或调用客户网关；依赖安装和未缓存镜像需要下载访问。真实协议、私网、Entra及Azure数据面验收需使用单独批准的环境，详见[测试说明](tests/README_ZH.md)。

## 上线边界与成本

- 新安全入口有明确的路由白名单。旧网关的图像、视频、WebSocket和Codex验证结果，不能替代新授权层的兼容性验收。
- 第一阶段采用原生Spend Logs，不再默认要求自建L3。原生模式发布、受控查询、容量/留存和按模式区分的阶段证据仍是上线阻断项，当前L3门禁未被放宽。原生日志不保证零丢失或不可变取证；选择增强L3时另行批准并完成其专项验收。
- 私有入口/controller、身份/Vault接线、PostgreSQL认证/HA和监控集成等仍需完成与验收。不能将带占位符的validation overlay直接用于生产。

当前边界见[代码收尾台账](docs/litellm-code-completion-backlog-2026-09-07.md)、[安全架构](docs/litellm-azure-security-hardening-zh.md)和[实施路线](docs/litellm-security-hardening-implementation-roadmap-zh.md)。历史阶段记录是参考证据，不是客户验收报告。

成本请结合[安全增强版BOM](docs/litellm-bom-cost-comparison-zh.md)与[Azure定价计算器](https://azure.microsoft.com/zh-cn/pricing/calculator/)评估。区域、HA、Firewall、Private Endpoint、边缘流量、数据库容量及审计留存均影响费用，旧单节点网关报价不适用于本架构。