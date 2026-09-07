# 给 AKS 上的 LiteLLM 配置阿里云域名（操作教程）

本教程教你把「已经部署在 AKS 上的 LiteLLM 服务」绑定到一个你从阿里云购买的域名上，并启用 HTTPS。

> 当前部署现状（本仓库默认）：
> - 命名空间：`litellm`
> - 服务：`litellm-mi-proxy`，类型 `LoadBalancer`，端口 `4000`，**纯 HTTP**
> - 客户端目前通过 `http://<公网IP>:4000` 访问
>
> 目标：改成通过 `https://litellm.你的域名.com`（标准 443 端口，带证书）访问。

---

## 目录

- [第 0 步：准备工作](#第-0-步准备工作)
- [第 1 步：在阿里云买域名](#第-1-步在阿里云买域名)
- [方案选择](#方案选择)
- [方案 A（最快）：直接把域名指向 LoadBalancer（仅 HTTP）](#方案-a最快直接把域名指向-loadbalancer仅-http)
- [方案 B（推荐）：Ingress + HTTPS 证书](#方案-b推荐ingress--https-证书)
  - [B-零. 用部署脚本一键启用（推荐）](#b-零-用部署脚本一键启用推荐)
  - [B1. 安装 ingress-nginx](#b1-安装-ingress-nginx)
  - [B2. 拿到 Ingress 公网 IP 并在阿里云配置 DNS](#b2-拿到-ingress-公网-ip-并在阿里云配置-dns)
  - [B3-甲. 用 Let's Encrypt 自动签发证书（推荐、免费、自动续期）](#b3-甲用-lets-encrypt-自动签发证书推荐免费自动续期)
  - [B3-乙. 用阿里云免费证书（手动下载导入）](#b3-乙用阿里云免费证书手动下载导入)
  - [B4. 创建指向 LiteLLM 的 Ingress](#b4-创建指向-litellm-的-ingress)
  - [B5. 把 LiteLLM Service 收敛为 ClusterIP（可选，更安全）](#b5-把-litellm-service-收敛为-clusterip可选更安全)
- [第 3 步：验证](#第-3-步验证)
- [常见问题排查](#常见问题排查)
- [回滚](#回滚)

---

## 第 0 步：准备工作

确认本机已安装并登录：

```powershell
# Azure CLI 已登录，且选中了部署 AKS 的订阅
az login
az account show --query "{name:name, id:id}" -o table

# 安装 kubectl（若未安装）
az aks install-cli

# 拉取 AKS 的访问凭据（把名字换成你的资源组和集群名）
az aks get-credentials --resource-group <你的资源组> --name litellm-mi-aks

# 验证能连上集群，并看到 LiteLLM
kubectl get svc -n litellm
```

你应当能看到类似：

```text
NAME                TYPE           CLUSTER-IP     EXTERNAL-IP      PORT(S)          AGE
litellm-mi-proxy    LoadBalancer   10.0.x.x       20.x.x.x         4000:xxxxx/TCP   1d
```

记下这里的 `EXTERNAL-IP`，它是目前的公网入口。

---

## 第 1 步：在阿里云买域名

1. 登录[阿里云域名控制台](https://dc.console.aliyun.com/)，搜索并购买一个域名，例如 `example.com`。
2. 我们通常给 LiteLLM 用一个**子域名**，例如 `litellm.example.com`，而不是根域名。
3. 购买完成后进入 **域名解析 / DNS 解析** 页面，后面会在这里加解析记录。

> 关于 ICP 备案：只有当服务器在**中国大陆境内**时才需要备案。本仓库的 AKS 默认部署在海外区域（如 `eastus2`），把海外 IP 解析到阿里云域名**不需要备案**。如果你之后把服务迁到大陆区域，则需要按阿里云要求备案后才能用 80/443 端口对外提供网页服务。

---

## 方案选择

| | 方案 A（最快） | 方案 B（推荐） |
| --- | --- | --- |
| 访问地址 | `http://litellm.example.com:4000` | `https://litellm.example.com` |
| 是否加密 | 否（明文，API Key 会暴露在网络中） | 是（TLS 加密） |
| 端口 | 非标准 4000 | 标准 443 |
| 复杂度 | 低 | 中 |
| 适合场景 | 临时测试 | 生产 / 长期使用 |

**强烈建议使用方案 B**：LiteLLM 的请求里带有 Virtual Key（相当于密码），走明文 HTTP 会被中间网络截获。方案 A 仅用于临时验证域名解析是否生效。

---

## 方案 A（最快）：直接把域名指向 LoadBalancer（仅 HTTP）

1. 在阿里云 DNS 解析页面添加一条记录：

   | 记录类型 | 主机记录 | 记录值 | TTL |
   | --- | --- | --- | --- |
   | `A` | `litellm` | 上面记下的 `EXTERNAL-IP` | 600 |

2. 等待 1~10 分钟解析生效后验证：

   ```powershell
   nslookup litellm.example.com
   curl.exe "http://litellm.example.com:4000/health/liveliness"
   ```

3. 访问地址就是 `http://litellm.example.com:4000`。

> 缺点：明文、非标准端口。请尽快切到方案 B。

---

## 方案 B（推荐）：Ingress + HTTPS 证书

思路：在集群里装一个 **ingress-nginx** 入口控制器，它会申请一个新的公网 IP 并监听 443；用证书做 TLS 终止；再用一条 Ingress 规则把 `litellm.example.com` 的流量转发到内部的 `litellm-mi-proxy:4000`。

```text
客户端 --HTTPS:443--> 阿里云DNS(A记录) --> ingress-nginx 公网IP
        --TLS终止/证书--> Ingress 规则 --> litellm-mi-proxy:4000 --> LiteLLM Pod
```

方案 B 有两条实现路径，任选其一：

- **B-零（推荐，一键）**：直接用部署脚本自动完成，见下方「用部署脚本一键启用」。
- **B1~B5（手动）**：自己一步步执行 Helm 和 kubectl 命令，适合想理解每步细节、或不想让脚本装 Helm 组件的场景。

---

### B-零. 用部署脚本一键启用（推荐）

部署脚本 `deploy_mi_aks_litellm.py` 已内置 Ingress 模式。当你设置了 `LITELLM_HOSTNAME` 环境变量时，脚本会自动：

1. 把 LiteLLM 的 Service 从 `LoadBalancer` 收敛为 `ClusterIP`；
2. 用 Helm 安装 / 升级 **ingress-nginx** 和 **cert-manager**；
3. 创建 Let's Encrypt 的 `ClusterIssuer` 和指向 `litellm-mi-proxy:4000` 的 TLS Ingress；
4. 为 Ingress 自动设置大请求和流式响应相关注解（`proxy-buffering=off`、`proxy-read-timeout=600`、`proxy-send-timeout=600`、`proxy-body-size=100m`）；
5. 读取 ingress-nginx 的公网 IP，并在结尾打印你需要在阿里云配置的 A 记录。

**前置条件**：本机已安装 [Helm](https://helm.sh/docs/intro/install/)（`winget install Helm.Helm`）和 `kubectl`，且已 `az login`。

```powershell
cd .\LiteLLM

# 设置域名（触发 Ingress 模式）和 Let's Encrypt 邮箱
$env:LITELLM_HOSTNAME = "litellm.example.com"
$env:LETSENCRYPT_EMAIL = "you@example.com"

# 可选：显式指定 master key（推荐生产环境由密钥管理系统注入）
# 不设置时脚本会自动生成强随机 key，并在结尾打印一次
# $env:LITELLM_MASTER_KEY = "<LITELLM_MASTER_KEY_FROM_SECRET_STORE>"

# 可选：按需调整启动等待时长（秒），默认 180
# $env:LITELLM_STARTUP_WAIT_SECONDS = "300"

python .\deploy_mi_aks_litellm.py
```

脚本跑完会输出类似：

```text
  Next step: point Alibaba Cloud DNS at the ingress
  Hostname          : litellm.example.com
  Ingress public IP : 20.x.x.x

  Create an A record in Alibaba Cloud DNS:
    A  litellm.example.com  ->  20.x.x.x
```

拿到这个 IP 后，去阿里云 DNS 添加 A 记录（见 [B2](#b2-拿到-ingress-公网-ip-并在阿里云配置-dns) 的表格），DNS 生效后 cert-manager 会**自动**签发证书。用下面命令查看签发进度：

```powershell
kubectl get certificate -n litellm
# READY 变成 True 即签发成功
```

> **注意事项**
> - **不设 `LITELLM_HOSTNAME`** 时脚本保持原有行为：`LoadBalancer:4000` + 明文 HTTP，不安装任何 Ingress 组件。
> - 设了 `LITELLM_HOSTNAME` 就**必须**同时设 `LETSENCRYPT_EMAIL`，否则脚本会报错退出。
> - `LITELLM_MASTER_KEY`：
>   - 不设置时，脚本会自动生成强随机 key 并用于本次部署；
>   - 建议生产环境显式注入（来自 Key Vault / 密钥管理系统），便于审计和轮换。
> - 首次部署时 DNS 还没配好，证书无法立即签发，因此脚本会**跳过公网 smoke test**，这是正常的。配好 DNS、证书 `READY=True` 后，再用[第 3 步](#第-3-步验证)手动验证。
> - 脚本默认会设置以下 Ingress 注解（可通过环境变量覆盖）：
>   - `nginx.ingress.kubernetes.io/proxy-buffering=off`
>   - `nginx.ingress.kubernetes.io/proxy-read-timeout=600`
>   - `nginx.ingress.kubernetes.io/proxy-send-timeout=600`
>   - `nginx.ingress.kubernetes.io/proxy-body-size=100m`
> - 脚本可反复执行（幂等）：Helm 用 `upgrade --install`，Ingress / ClusterIssuer 存在时会更新而非报错。

之后就可以跳到[第 3 步：验证](#第-3-步验证)。如果你想手动完成或排查脚本行为，继续看下面的 B1~B5。

---

### B1. 安装 ingress-nginx

用 Helm 安装（若没有 Helm：`winget install Helm.Helm`）：

```powershell
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update

helm install ingress-nginx ingress-nginx/ingress-nginx `
  --namespace ingress-nginx --create-namespace `
  --set controller.service.type=LoadBalancer
```

> 也可以用 AKS 托管的 Application Routing 附加组件（`az aks approuting enable`）。本教程用社区版 ingress-nginx，通用性更好、迁移方便。

### B2. 拿到 Ingress 公网 IP 并在阿里云配置 DNS

等待入口控制器拿到公网 IP（第一次可能要 1~3 分钟）：

```powershell
kubectl get svc -n ingress-nginx ingress-nginx-controller -w
```

看到 `EXTERNAL-IP` 从 `<pending>` 变成真实 IP 后，记下它，然后到**阿里云 DNS 解析**添加：

| 记录类型 | 主机记录 | 记录值 | TTL |
| --- | --- | --- | --- |
| `A` | `litellm` | ingress-nginx 的 `EXTERNAL-IP` | 600 |

验证解析生效（Let's Encrypt 签发前必须先生效）：

```powershell
nslookup litellm.example.com
```

`nslookup` 返回的地址必须等于 ingress-nginx 的 IP，再继续下一步。

接下来选择 **B3-甲**（自动，推荐）或 **B3-乙**（手动导入阿里云证书）其中一个即可。

### B3-甲. 用 Let's Encrypt 自动签发证书（推荐、免费、自动续期）

1. 安装 cert-manager：

   ```powershell
   helm repo add jetstack https://charts.jetstack.io
   helm repo update
   helm install cert-manager jetstack/cert-manager `
     --namespace cert-manager --create-namespace `
     --set crds.enabled=true
   ```

2. 创建一个 ClusterIssuer（把邮箱换成你自己的）：

   ```powershell
   @"
   apiVersion: cert-manager.io/v1
   kind: ClusterIssuer
   metadata:
     name: letsencrypt-prod
   spec:
     acme:
       server: https://acme-v02.api.letsencrypt.org/directory
       email: you@example.com
       privateKeySecretRef:
         name: letsencrypt-prod
       solvers:
         - http01:
             ingress:
               class: nginx
   "@ | kubectl apply -f -
   ```

   证书会在你创建 Ingress（B4）时**自动**通过 HTTP-01 挑战签发。前提是 B2 的 DNS 已指向 ingress-nginx。跳到 [B4](#b4-创建指向-litellm-的-ingress)。

### B3-乙. 用阿里云免费证书（手动下载导入）

如果你更愿意用阿里云的证书：

1. 在阿里云 **数字证书管理服务（SSL 证书）** 里申请一张**免费个人测试证书**，绑定 `litellm.example.com`，通过 DNS 验证签发。
2. 签发后，**下载 Nginx 格式**的证书，得到两个文件：`xxx.pem`（证书链）和 `xxx.key`（私钥）。
3. 在集群里创建 TLS Secret（放在 `litellm` 命名空间）：

   ```powershell
   kubectl create secret tls litellm-tls `
     --namespace litellm `
     --cert=.\xxx.pem `
     --key=.\xxx.key
   ```

> 阿里云免费证书有效期通常为 3 个月或 1 年，到期需要重新下载并 `kubectl create secret tls ... --dry-run=client -o yaml | kubectl apply -f -` 更新。若嫌麻烦，请用 B3-甲 的自动续期方案。

### B4. 创建指向 LiteLLM 的 Ingress

根据你在 B3 选择的方案，二选一执行。把 `litellm.example.com` 换成你的真实域名。

**如果用了 B3-甲（Let's Encrypt / cert-manager）：**

```powershell
@"
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: litellm-ingress
  namespace: litellm
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
    nginx.ingress.kubernetes.io/proxy-buffering: "off"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "600"
    nginx.ingress.kubernetes.io/proxy-body-size: "100m"
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - litellm.example.com
      secretName: litellm-tls
  rules:
    - host: litellm.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: litellm-mi-proxy
                port:
                  number: 4000
"@ | kubectl apply -f -
```

**如果用了 B3-乙（阿里云证书，已创建 `litellm-tls` Secret）：**

用上面同样的 YAML，但**删掉** `cert-manager.io/cluster-issuer: letsencrypt-prod` 这一行注解即可（因为证书是你手动导入的，不需要 cert-manager 签发）。

检查证书是否就绪（B3-甲 首次签发需 1~3 分钟）：

```powershell
kubectl get certificate -n litellm
# READY 列变成 True 即签发成功

kubectl describe ingress litellm-ingress -n litellm
```

### B5. 把 LiteLLM Service 收敛为 ClusterIP（可选，更安全）

现在流量都从 Ingress 进来了，原来的 `LoadBalancer:4000` 公网入口就没必要再暴露。把它改成 `ClusterIP`，只在集群内可达：

```powershell
kubectl patch svc litellm-mi-proxy -n litellm -p '{\"spec\":{\"type\":\"ClusterIP\"}}'
```

> 这会释放旧的公网 IP。如果你还没验证好 Ingress，请先跳过这步，等方案 B 验证通过后再执行。

---

## 第 3 步：验证

```powershell
# 1. 健康检查（应返回 200 / 正常 JSON）
curl.exe "https://litellm.example.com/health/liveliness"

# 2. 用 Virtual Key 跑一次真实推理（用本仓库的统一测试脚本）
python ..\tests\test_all_deployments.py `
  --config .\azure-openai.json `
  --base-url "https://litellm.example.com" `
  --api-key "<你的 LiteLLM Virtual Key>" `
  --prompt ok

# 3. 打开管理 UI
# 浏览器访问 https://litellm.example.com/ui
```

浏览器地址栏出现锁图标、证书有效，即大功告成。之后把客户端 / Codex 里的 `base_url` 全部改成 `https://litellm.example.com`。

---

## 常见问题排查

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| `nslookup` 解析不到或还是旧 IP | DNS 未生效 / 记录值填错 | 等几分钟；核对阿里云 A 记录的记录值等于 ingress-nginx 的 IP |
| 证书一直 `READY=False` | DNS 未指向 ingress-nginx，HTTP-01 挑战失败 | 先确保 B2 解析生效，再 `kubectl describe certificate -n litellm` 看报错 |
| 浏览器提示证书不受信任 | 证书没签发成功，走了 ingress 默认自签名证书 | 检查 `kubectl get certificate -n litellm`，或确认 `litellm-tls` Secret 存在 |
| `502 Bad Gateway` | Ingress 后端指向错了 / LiteLLM 未就绪 | 确认 backend 是 `litellm-mi-proxy:4000`；`kubectl get pods -n litellm` 看 Pod 是否 Running |
| 长请求/流式响应异常（超时、断流、413） | Nginx 默认缓冲与请求大小限制 | 建议注解：`proxy-buffering=off`、`proxy-read-timeout=600`、`proxy-send-timeout=600`、`proxy-body-size=100m`；仍不足再继续调大 |
| 拿不到 ingress-nginx 的 EXTERNAL-IP | LoadBalancer 还在分配 | `kubectl get svc -n ingress-nginx -w` 等待；若长期 pending 检查订阅配额 |

查看入口控制器日志：

```powershell
kubectl logs -n ingress-nginx deploy/ingress-nginx-controller --tail=100
```

---

## 回滚

如果想恢复到「直接用 LoadBalancer:4000」的状态：

```powershell
# 1. 删除 Ingress
kubectl delete ingress litellm-ingress -n litellm

# 2. 把 LiteLLM Service 改回 LoadBalancer（若之前收敛成了 ClusterIP）
kubectl patch svc litellm-mi-proxy -n litellm -p '{\"spec\":{\"type\":\"LoadBalancer\"}}'

# 3. （可选）卸载入口控制器与 cert-manager
helm uninstall ingress-nginx -n ingress-nginx
helm uninstall cert-manager -n cert-manager

# 4. 阿里云 DNS 里把 A 记录改回旧的 LoadBalancer IP，或删除该记录
```

---

## 小结

- 临时测试：**方案 A**，阿里云 A 记录直接指向 `litellm-mi-proxy` 的 LoadBalancer IP。
- 生产使用：**方案 B**，装 ingress-nginx + 证书（Let's Encrypt 自动续期最省心），A 记录指向 ingress-nginx IP，最后把 LiteLLM Service 收敛为 ClusterIP。
- 无论哪种方案，域名的 A 记录都是在**阿里云 DNS 解析**里配置，指向 AKS 上对应的公网 IP。
