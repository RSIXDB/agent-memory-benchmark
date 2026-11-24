# LoCoMo 10 持久化复测

复测入口沿用 AMB 的 LoCoMo 数据加载、答题提示、裁判提示及类别 1–4 的 1540 题。独立模块负责保留 bank、核验原文、记录模型请求和恢复检查点；不会删除 bank，也不会按分数重新生成答案。

```bash
export LOCOMO_DATA_PATH=/absolute/path/to/locomo10.json
export CONTEXTBASE_READ_KEY='your-service-key'
export CONTEXTBASE_MODEL_URL='https://your-compatible-gateway'
export CONTEXTBASE_MODEL_KEY='your-model-key'

PYTHONPATH=src uv run python -m memory_bench.contextbase.locomo_repro prepare \
  --recipe configs/contextbase/locomo-issue112-repro.json
```

先复制配置，修改 `api_url`、`output` 和唯一的 `bank_prefix`。数据 SHA256 固定为 `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`，预期为 272 份 session、10 个 conversation bank、1540 题。输出目录须为 0700；运行记录包含原文、gold、请求和答题，应保持私有。

ingest 阶段的服务 key 必须具备创建 bank、配置和 retain 权限；后续审计与答题可改用对应读取权限的 key。新建数据需要单独允许写入；已存在 bank 不会被覆盖。创建后的 bank 使用 verbose、配置中的固定抽取 mission，关闭 observations 和自动 consolidation。服务端抽取模型、reasoning、最大输出和 embedding 配置需要由部署方预先设置并记录，客户端配置不会替代这些服务设置。可参考 [本次服务配置快照](../../configs/contextbase/locomo-server-profile.json)：抽取输出上限实际为 64000，使用 BGE-M3 及 BGE-reranker-v2-m3 的自托管 TEI 兼容服务。

```bash
PYTHONPATH=src uv run python -m memory_bench.contextbase.locomo_repro ingest \
  --recipe configs/contextbase/locomo-issue112-repro.json --allow-retain
PYTHONPATH=src uv run python -m memory_bench.contextbase.locomo_repro audit \
  --recipe configs/contextbase/locomo-issue112-repro.json
PYTHONPATH=src uv run python -m memory_bench.contextbase.locomo_repro evaluate \
  --recipe configs/contextbase/locomo-issue112-repro.json --max-new-model-calls 3200
```

`ingest` 使用稳定 operation ID 并保存接收回执。操作状态不明时停止；只有审计后才能通过服务端既有 operation retry 恢复，不能直接重发 retain。`audit` 检查 bank 配置、完整文档集合、原文、日期、会话说明、metadata 和 tags；事实、向量及原有大库是否被修改，仍需 PostgreSQL 只读审计。

`evaluate` 默认允许的新模型调用数为 0；上述 3200 限制本工具向配置模型端点新增的 HTTP 请求数，不是 token 预算。网关若轮换 key 或重试，上游实际尝试数可能更多，须另设上游请求及 token/费用上限。已有首次有效答案和判分会直接复用；尚未确定结果的请求不能自动重试。格式错误最多重试一次，完整答案（包括错误答案）不会重抽。只在 1540 题均完成后发布完整 accuracy，技术失败保留在单独清单中。

## 与 issue 112 历史运行的关系

配置参考 [issue 112](https://github.com/RSIXDB/hindsight/issues/112)：GLM-5.3 答题、Qwen4-Max 判题，记忆正文 32768 / chunk 正文 16384 tokens。本模块使用非流式兼容接口和显式输出上限 reader 32768 / judge 4096；历史网关和服务代码版本不同，不能称为逐字节相同的历史执行。

本次适配保留 `document_id`，默认不写额外 `metadata.doc_id`，与本次 ECS 运行一致；上游适配器会写该 metadata。需要原生上游格式时显式设置 `ingest_doc_id_metadata=true`，使用新 bank 和新输出目录，并单独报告。

这两个正文预算不包含返回 JSON 的来源 ID、日期、分数等开销。实际答题输入应读取模型 usage；不能把 32768+16384 当作总提示长度。

完整安装使用仓库 `uv sync`。精简环境可通过 `uv run --no-project --with aiohttp --with pydantic --with tiktoken` 加 `PYTHONPATH=src` 运行此模块，无需安装无关的 memory provider SDK。


## 在同一份数据上复测服务端增强

[增强配置示例](../../configs/contextbase/locomo-enhanced.example.json) 复用相同 bank_prefix，使用独立 output；将 api_url 指向已安装 V3、开启实验路由并准备好索引的服务。只需设置 enhanced_memory=true 即选择 native_first64K，额外参数可在 evidence_options 中冻结。不要再次 ingest，也不要把两种模式写入同一输出目录。

依次执行 prepare、audit、evaluate，命令与上文一致，只替换 recipe 路径。新增入口保留同一 LoCoMo Loader、Reader/Judge 提示与评分；改变的是召回后交给 Reader 的证据材料。测分须报告实际版本和预算，不能将 V2 的历史试验分数归属给 V3。
