# LiteLLM OSS回调实测与L3采集接口

> 日期：2026-09-07
> 状态：固定版本合成实测通过；薄适配器未接入部署，未替换现有Node采集链路
> 边界：无Azure/Kubernetes部署、DNS或流量变更，无生产原文，无LiteLLM Enterprise依赖

## 1. 实测范围

使用LiteLLM `1.98.0`镜像：

```text
docker.litellm.ai/berriai/litellm@sha256:20b5044b619055374061a6d5b7b08754cad75aeabbf82ddf4f69cc0cf80ddaf4
```

实际加载OSS `CustomLogger`，分别运行SDK调用和真实LiteLLM Proxy子进程。Proxy与合成上游位于同一容器，仅使用loopback；无数据库、许可证或Azure端点。容器使用`--network none`、UID10001、只读根文件系统、只读代码挂载、`/tmp` tmpfs、drop ALL及资源上限。SDK使用`mock_response`，不能将SDK测试单独当作协议证明。

本地9项单元测试（5项信封、4项适配器）、SDK验证和13个Proxy场景通过。GitHub Actions独立任务已经配置，尚未在远端运行。

| 场景 | 实际观察 |
| --- | --- |
| Chat JSON / SSE | 200；终态回调包含合成输入、输出、usage及关联字段 |
| Responses JSON / SSE | 200；终态回调包含合成输入、输出、usage及关联字段 |
| Embeddings | 200；回调包含输入及合成向量，不要求文本输出标记 |
| Chat / Responses上游500 | 客户端500；failure回调包含输入、关联字段及异常存在标记 |
| Chat / Responses工具调用 | 200；回调保留合成函数调用，未测试工具执行或结果续轮 |
| 请求`no-log=true` | 在测试服务端设置`global_disable_no_log_param=true`后仍有回调 |
| `x-litellm-disable-callbacks`请求头 | 此固定版本、此自定义回调场景未被该头抑制，不外推所有回调类型 |
| Chat上游提前EOF | 上游未发送正常结束帧，客户端仍200，LiteLLM仍触发success回调 |
| 接收器异常 | 适配器发出交付失败信号，LiteLLM仍返回200及模型输出 |

关联字段检查覆盖LiteLLM Call ID、配置中的model deployment ID及测试Trace上下文。每个Proxy场景目前只观察到一次上游调用，不证明重试、fallback或跨进程去重语义。测试直接访问合成LiteLLM Proxy，不经过Entra认证代理，也不证明Azure provider或租户授权兼容性。

输出仅记录合成场景、状态及字段存在性，不打印完整kwargs、Header、正文或凭据。测试内部必须处理合成正文才能验证内容保真；临时子进程日志随测试清理。

## 2. 本轮代码

- [audit_envelope.py](../LiteLLM/observability/audit_envelope.py)：显式提取请求字段、模型输入、响应字段、usage、cost和关联信息；缺失可选字段可处理，未知Trace格式不采信。
- [oss_audit_callback.py](../LiteLLM/observability/oss_audit_callback.py)：需要显式注入选择器、异步接收器及失败信号函数；没有自动注册实例、网络客户端或存储配置。
- [test_litellm_audit_envelope.py](../tests/test_litellm_audit_envelope.py)：信封字段、缺省值、内容预算、对象隔离和凭据排除测试。
- [tests/spikes](../tests/spikes)：固定版本运行时验证及适配器测试。

信封将`event`与`transportCompleteness`分开，后者始终为`not-verified`。success只代表LiteLLM回调分类，不代表上游完整结束、客户端完整接收或原文已可靠持久化。failure只输出异常类型；交付失败只向LiteLLM抛出固定错误消息，抑制原始异常链的默认格式化输出。

不复制整个kwargs、Header、内部Key、路由metadata或异常文本。**这是字段选择，不是完整DLP**：业务正文、工具参数及嵌套模型内容仍可能含Secret/PII，尚未接入现有脱敏策略。不要在生产中直接写入未经治理的信封。

内容默认上限2MiB；超过预算时`content=null`且`contentTruncated=true`，不把丢弃内容冒充完整审计。预算在序列化之后检查，不是进程内存上限。`usage`与关联字段也需要接收器进一步校验。

`traceId`是关联提示，不是授权凭据。`identityBinding=resolve-at-trusted-receiver`明确要求接收器关联入口已认证、已选择采集的持久化意图；不能从客户端metadata或Header决定tenant、subject、Team和是否强制审计。当前没有实现该受信接收器。

## 3. 冻结的后续实现方向

```text
认证代理：身份/授权、采集选择、可信意图、入口拒绝与客户端断连事实
    -> LiteLLM OSS CustomLogger：模型输入/输出、调用与失败事实
    -> 客户自有薄适配器：字段映射、内容预算、交付失败信号
    -> 待实现的受信交付层：身份绑定、持久化确认、重试/去重、缺口对账
    -> Azure审计存储：独立查询、审批查看、留存与保全治理
```

这是职责方向冻结，不是“生产采集重构完成”。不启用Enterprise `azure_storage`或`generic_api`连接器；不因认证JWT为付费功能而否定OSS内容回调能力。继续保持`store_prompts_in_spend_logs=false`，避免原文散落到普通Spend Logs和PostgreSQL。

现有Node采集、入口pending准入和查询/留存功能保持不变；新适配器未打包进LiteLLM镜像、未配置到Kustomize，也没有激活原文采集。替换前必须完成：

1. 受信身份绑定、去重键和多attempt事件关系；拒绝伪造、跨租户和重放事件。
2. 模型完成、上游传输结束、客户端接收、持久化完成的独立状态；EOF和断连不得误记完整。
3. 持久化队列或逐段写入方案及背压，证明Pod崩溃、进程退出、存储失败后的恢复或可检测缺口；不承诺exactly-once或零丢失。
4. 正文脱敏/保真预览、审批/保全竞态、访问日志期限及Azure权限实测。
5. 新旧采集一致性和性能对比后，才允许切换采集所有权，避免重复持久化及不同语义记录混用。

完整待办见[代码收尾台账](litellm-code-completion-backlog-2026-09-07.md)。

## 4. 可复现门禁

在仓库根目录运行：

```bash
make validate-oss-callbacks
```

Make目标使用现有`.venv/bin/python`运行纯标准库信封测试；真实LiteLLM只在Docker内运行，不安装进宿主虚拟环境。没有该虚拟环境时，可分别运行：

```bash
python3 -m unittest tests.test_litellm_audit_envelope
bash scripts/validate-oss-callbacks.sh
```

需要可用Docker daemon及镜像；镜像未缓存时Docker可能先访问镜像仓库拉取，容器运行阶段仍无外部网络。不挂载Azure配置、凭据目录、数据库或生产配置。不需要公开端口。

门禁是独立CI任务，不隐式改变现有Stage3至9离线验证的依赖。升级LiteLLM时必须重新跑矩阵；观察到EOF/回调失败行为变化时，应复核契约而非直接放宽断言。

尚未覆盖：重试/fallback多attempt、下游真实断连、Responses断流、流式工具调用、工具结果续轮、WebSocket、Files/MCP、对象归属及加密多轮引用、真实Azure调用、Entra授权、云端存储恢复和性能验收。阶段7当前拒绝的协议继续拒绝。