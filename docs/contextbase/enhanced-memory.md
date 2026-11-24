# 实验性服务端原文证据

原型把两个机制放入 ContextBase：授权范围内的独立原文候选检索，以及带出处、去重和完整 token 预算的证据组装。算法不接收测试集名称、gold 或 rubric，不增加生成模型调用。是否提高分数需要按固定配置配对验证；当前保持实验状态。

服务端须使用配套 source-evidence-v2 分支，配置 `CONTEXTBASE_SOURCE_EVIDENCE_ENABLED=true`，并由管理员显式准备各 bank 的派生索引。默认部署没有这个路由，普通 recall 行为和 OpenAPI 保持兼容。Oracle 等非 PostgreSQL store 明确返回不支持。

管理员在服务端虚拟环境执行，DSN 只通过环境变量传入；必须指定正确的 schema/bank，不要连接无关生产数据。

```bash
python -m hindsight_api.engine.memories.pg.source_evidence_admin check \
  --schema ctxdb_ws_example --bank example
python -m hindsight_api.engine.memories.pg.source_evidence_admin build \
  --schema ctxdb_ws_example --bank example --allow-derived-index-write
```

环境变量 `SOURCE_EVIDENCE_ADMIN_DSN` 指向目标 PostgreSQL。构建只写 `source_evidence_documents_v2` 与 `source_evidence_state_v2`，使用英文 PostgreSQL FTS/GIN；它与原生事实检索的 `pg_search` BM25 是两件事。消息窗口再使用 Python BM25 排序。派生索引保留 tsvector、长度、版本和摘要，不复制完整原文；原文来自已鉴权的 documents。

AMB 的 HTTP Hindsight provider 通过显式开关启用：

```bash
export AMB_ENHANCED_MEMORY=true
export AMB_ENHANCED_MEMORY_OPTIONS='{"version":"source-evidence-v2","max_tokens":48000,"independent_search":true,"neighbor_messages":1,"user_fraction":0.6}'
```

沿用 AMB 原有运行命令。客户端默认最终证据预算为 48000，BEAM 为 60000，可用上面配置显式覆盖。服务端不识别 benchmark。LoCoMo 专用持久化复测模块直接使用普通 recall，不读取此开关；增强对照应使用 provider 或独立配对实验，使用不同的输出目录。

路由为 `POST /v1/default/banks/{bank_id}/memories/recall-evidence`，请求顶层沿用 query、budget、max_tokens、query_timestamp、tags、tags_match、tag_groups，增强参数置于 `evidence`。普通召回预算与最终 `evidence.max_tokens` 分开；后者计算整个 evidence.context 的来源标签、日期及正文，不包含 Reader 提示和问题。

返回上下文、实际 tokens/tokenizer、原文位置与 SHA256、独立检索来源、各阶段耗时和 `extra_generative_calls=0`。客户端复算完整上下文 tokens 并校验版本；服务未启用、索引缺失/过期或参数不支持时明确失败，不静默回退。

原型限制：索引需要管理员显式重建，尚未集成自动更新/迁移/运维；时间过滤 `evidence.before` 必须带时区。当前 AMB 增强适配器拒绝 query gate/fallback 等未支持过滤，避免扩大授权范围。候选上限、字符上限与最终预算可能省略相关原文，因此更大窗口或覆盖用户消息不能预设为准确率提升。

## 是否必须增加一次 LLM 交互

多查询是已有检索方法：[LangChain MultiQueryRetriever](https://reference.langchain.com/python/langchain-classic/retrievers/multi_query/MultiQueryRetriever) 使用 LLM 生成多条查询再取检索并集。增加调用可能改善覆盖，也会增加延迟和成本，应作为可选能力单独计量。

[Mem0 的搜索说明](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/search.mdx) 提供可选 reranking，不能据此推断其每次 search 都需要额外生成模型。ContextBase 本原型选择原问题的独立词法检索与确定性组装，保留零额外生成调用这一可检验边界。

现有只读 LME/BEAM 复测入口也支持 `method="source-evidence-v2"`，并在 recipe 内冻结 `evidence_options`，不用环境变量隐式改参数。分别参考 [LME 示例](../../configs/contextbase/longmemeval-s-source-evidence.example.json) 与 [BEAM 示例](../../configs/contextbase/beam10m-source-evidence.example.json)。先按主 README 完成源库审计与问题准备，再设置环境配置报告、服务地址和私有 key 文件：

```bash
PYTHONPATH=src uv run python -m memory_bench.contextbase run \
  --config configs/contextbase/beam10m-source-evidence.example.json \
  --execute-models --limit 60 --max-model-calls 1200
```

该路径只请求一次 recall-evidence，缓存完整证据及参数摘要，不先重复请求普通 recall，不使用 rewriter。Reader、judge 和评分沿用对应数据集的既有实现。固定抽样应先写入独立 questions.jsonl，记录种子和问题集合摘要；不要按旧判分挑选测试题。普通 AMB HTTP provider 则仍使用前述环境变量开关。
