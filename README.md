## Existing LiteLLM Customer Migration

For an existing gateway, start with the [customer migration guide (Chinese)](docs/customer-migration-guide-zh.md). The **Customer staged migration** workflow uses customer-owned GitHub Environment variables/secrets, staged readiness checks and read-only Azure previews. Deployment, database migration and traffic cutover require separate customer approval; this is not an in-place one-click upgrade. The hardening path does not depend on APIM or LiteLLM Enterprise.

# Azure OpenAI Load Balancer & Quota Breaker

This project provides a comprehensive solution to bypass the quota limits of single Azure subscriptions for Azure OpenAI Service. By implementing a reverse proxy with load balancing capabilities, it distributes requests across multiple Azure OpenAI resources. It also robustly handles common errors like **429 (Too Many Requests)**, **500**, and **503**.

## 🌟 Key Features

- **Quota Bypass**: Aggregates throughput from multiple Azure OpenAI resources/subscriptions.
- **Error Handling**: Automatic retry logic for 429, 500, and 503 errors.
- **Model Compatibility**: Support for routing standard interactions, Image endpoints (DALL-E), and specially handling structure endpoints for models like Sora.
- **Protocol Flexibility**: Supports Azure OpenAI native format APIs, OpenAI compatible format APIs, and the ultra-low latency Response API mode.
- **Multiple Deployment Options**:
  1. **Azure API Management (APIM)**: A fully managed, cloud-native Azure solution.
  2. **LiteLLM on AKS**: A cost-effective, open-source solution running on Azure Kubernetes Service.

## 📂 Project Structure

- **`requirements.txt`**: Consolidated dependencies for deploying both APIM and LiteLLM projects.
- **`tests/`**: Unified E2E testing framework to validate models and APIs against any deployed solution.

### 1. Azure API Management (APIM)
Located in the [APIM/](./APIM/) directory.

This solution uses Azure's native API Management service to create a gateway that manages traffic to your backend Azure OpenAI endpoints.
- **Key File**: `deploy_mi_apim.py` - Automated deployment script.
- **Resources Created**: Azure API Management Service, Managed Identities, Backend policies.

### 2. LiteLLM on AKS
Located in the [LiteLLM/](./LiteLLM/) directory.

This solution deploys [LiteLLM](https://github.com/BerriAI/litellm), an open-source OpenAI proxy, onto an Azure Kubernetes Service (AKS) cluster.

The deployment uses AZURE_SUBSCRIPTION_ID for the AKS subscription. After deployment, configure per-user budgets, teams, and model allowlists using [LiteLLM/USER_BUDGET_AND_MODEL_ACCESS_ZH.md](./LiteLLM/USER_BUDGET_AND_MODEL_ACCESS_ZH.md).
- **Key File**: `deploy_mi_aks_litellm.py` - Automated deployment script.
- **Resources Created**: AKS Cluster, PostgreSQL, Load Balancer.

## 💰 Cost Analysis (Estimated)

Below is an estimated monthly cost analysis for both solutions. Prices are based on the **East US 2** region (as of 2026) and may vary by region and actual usage.

### Option 1: Azure API Management (Standard v2)

The APIM deployment script uses the **Standard v2** SKU.

| Resource | SKU | Unit Price (Est.) | Quantity | Monthly Cost |
| :--- | :--- | :--- | :--- | :--- |
| **API Management** | Standard v2 | ~$0.96 / hour | 730 hours | **~$700.00** |
| **Traffic Data** | - | Varied | - | (Minimal for text) |
| **Total** | | | | **~$700.00 / month** |

*Note: The **Standard v2** tier is designed for production workloads with higher throughput limits (approx. 50M requests/month included). If this exceeds your budget, you may consider modifying the script to use **Basic v2** (~$150/month), provided its limits (10M requests/month) meet your requirements.*

### Option 2: LiteLLM on AKS

The LiteLLM solution deploys a lightweight AKS cluster. The parameters are defined in `LiteLLM/deploy_mi_aks_litellm.py`.
*   **VM Size**: `Standard_B2s` (2 vCPU, 4GB RAM)
*   **Node Count**: 1

| Resource | Spec / SKU | Unit Price (Est.) | Monthly Cost |
| :--- | :--- | :--- | :--- |
| **AKS Cluster Management** | Standard Tier (SLA) | ~$0.10 / hour | ~$73.00 |
| **Virtual Machine** | Standard_B2s | ~$0.023 / hour | ~$17.00 |
| **Managed Disk** | Standard SSD (128GB) | ~$0.06 / GB | ~$8.00 |
| **Load Balancer** | Standard Load Balancer | ~$0.025 / hour | ~$18.00 |
| **Public IP** | Standard Public IP | ~$0.005 / hour | ~$3.65 |
| **Total** | | | **~$119.65 / month** |

### Summary

- **Lowest Cost**: **LiteLLM on AKS** (~$120/mo) is generally more cost-effective for smaller scale or standard throughput needs.
- **Lowest Maintenance**: **APIM** (~$700/mo) requires zero OS/Cluster management.

---

*Disclaimer: Prices are estimates only and subject to change by Microsoft Azure. Please check the [Azure Pricing Calculator](https://azure.microsoft.com/en-us/pricing/calculator/) for the most accurate current pricing.*

## ⚙️ Quick Start

**1. Install Global Dependencies:**
```bash
pip install -r requirements.txt
```

**2. Follow specific deployments:**
- Navigate to `APIM/` or `LiteLLM/` and follow their respective `README.md` to run the deployment scripts.

**3. Run Unified Tests:**
- Once endpoints are provisioned, use `tests/test_all_deployments.py` to validate API configurations (Dual format logic).
