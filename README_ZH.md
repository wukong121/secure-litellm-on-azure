# Azure OpenAI 负载均衡与配额突破方案

本项目提供了一个完整的解决方案，用于突破单一 Azure 订阅中 Azure OpenAI 服务的配额限制。通过实现具备负载均衡功能的反向代理，将请求分发到多个 Azure OpenAI 资源上，并能够稳健地处理 **429 (Too Many Requests)**、**500** 和 **503** 等常见错误。

## 已有LiteLLM的安全增强迁移

面向客户的分阶段入口见[客户迁移指南](docs/customer-migration-guide-zh.md)：使用GitHub Environment variables/secrets配置客户环境，通过 **Customer staged migration** 引导阶段0至9的准备、预检、What-if和人工受控替换。安全增强路线不使用APIM或LiteLLM Enterprise；不是旧部署脚本的一键原地升级。尚未完成的集成会阻断上线，旧环境保留用于回退。

## 🌟 核心特性

- **突破配额**: 聚合多个 Azure OpenAI 资源/订阅的吞吐量，突破单实例瓶颈。
- **自动容错**: 针对 429、500和503 错误提供零延迟的自动重试、切换路由逻辑。
- **模型路由兼容**: 深度适配普通 Chat 请求、DALL-E 图像端点，并对 Sora 等特定模型的非标准路径规则进行精准处理。
- **协议兼容**: 同时原生支持 Azure OpenAI 格式 API、OpenAI 兼容格式 API 以及最新的低延迟 Response API 路由。
- **多部署选项**:
  1. **Azure API Management (APIM)**: 全托管、云原生的 Azure 解决方案。
  2. **LiteLLM on AKS**: 高性价比的开源代理方案。

## 📂 项目结构

- **`requirements.txt`**: 根目录统一项目的依赖文件。
- **`tests/`**: 统一的端到端（E2E）测试框架文件夹。

### 1. Azure API Management (APIM)
位于 [`APIM/`](./APIM/) 目录。该方案使用 Azure 原生 API Management 服务智能管理流量。

### 2. LiteLLM on AKS
位于 [`LiteLLM/`](./LiteLLM/) 目录。该方案将开源 OpenAI 代理库部署在 AKS 上。

## 💰 成本分析 (估算)

以下是两种方案的月预估成本分析。价格基于 **East US 2** 区域（2026年参考价），实际费用可能随区域和使用量波动。

### 方案 1: Azure API Management (Standard v2)

部署脚本中默认使用了 APIM 的 **Standard v2** SKU，该 SKU 适用于流量较大的生产级负载（约 $700/月）。

| 资源项 | SKU | 单价 (估算) | 用量 | 月费用 |
| :--- | :--- | :--- | :--- | :--- |
| **API Management** | Standard v2 | ~$0.96 / 小时 | 730 小时 | **~$700.00** |
| **流量数据** | - | 极低 | - | (包含 50M API 请求/月) |
| **合计** | | | | **~$700.00 / 月** |

*注意：**Standard v2** 提供了 5000 万次/月的 API 请求配额。如果是小规模生产环境，您可以考虑修改部署脚本，将 SKU 改为 **Basic v2** (约 $150/月，包含 1000 万次请求/月)，以大幅降低成本。*

### 方案 2: LiteLLM on AKS

LiteLLM 方案部署了一个轻量级 AKS 集群。参数均在 `LiteLLM/deploy_mi_aks_litellm.py` 定义。
*   **VM 规格**: `Standard_B2s` (2 vCPU, 4GB RAM)
*   **节点数**: 1

| 资源项 | 规格 / SKU | 单价 (估算) | 月费用 |
| :--- | :--- | :--- | :--- |
| **AKS 集群管理** | 标准层 (SLA) | ~$0.10 / 小时 | ~$73.00 |
| **虚拟机** | Standard_B2s | ~$0.023 / 小时 | ~$17.00 |
| **托管磁盘** | Standard SSD (128GB) | ~$0.06 / GB | ~$8.00 |
| **负载均衡器** | Standard Load Balancer | ~$0.025 / 小时 | ~$18.00 |
| **公网 IP** | Standard Public IP | ~$0.005 / 小时 | ~$3.65 |
| **合计** | | | **~$119.65 / 月** |

### 总结

- **最具性价比**：**LiteLLM on AKS** (~$120/月) 适合中小型规模或对成本敏感的场景。
- **最省心维护**：**APIM** (~$700/月) 适合企业级生产环境，尤其是需要零服务器运维的场景。

---

*免责声明：以上价格仅供参考，不作为最终计费依据。最新定价请访问 [Azure 定价计算器](https://azure.microsoft.com/zh-cn/pricing/calculator/)。*

## ⚙️ 快速上手

**1. 安装全局依赖:**
```bash
pip install -r requirements.txt
```

**2. 查阅模块部署指南:**
- 分别进入 `APIM/` 或 `LiteLLM/` 目录执行自动化搭建。

**3. 执行全局验证测试:**
- 当部署完成后，使用 `tests/test_all_deployments.py` 执行验证脚本。
## LiteLLM 部署补充

LiteLLM 部署前请先执行 `az login`，并通过环境变量 `AZURE_SUBSCRIPTION_ID` 指定创建 AKS、Managed Identity 和资源组所使用的订阅。LiteLLM 的用户预算、Team、Virtual Key 和模型权限配置见 [`LiteLLM/USER_BUDGET_AND_MODEL_ACCESS_ZH.md`](./LiteLLM/USER_BUDGET_AND_MODEL_ACCESS_ZH.md)。