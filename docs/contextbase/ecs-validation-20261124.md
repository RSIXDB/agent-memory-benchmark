# ECS 复用核验与配置记录

日期：2026年11月24日。代码仓：`RSIXDB/agent-memory-benchmark`；开发分支：`codex/contextbase-lme-beam-repro`。本轮没有重新导入数据，没有修改PG配置、既有bank或ContextBase核心源码。

## 结论

| 项目 | LongMemEval-S | BEAM 10M |
|---|---:|---:|
| 可复用 bank | 500 | 10 |
| 原文文档 | 23867 | 5265 |
| chunks | 127245 | 166167 |
| memory_units | 230126 | 392292 |
| 完整材料回放 | 500/500上下文和Reader提示一致 | 200/200上下文和展示文档ID一致 |
| 历史分数复算 | 476/500，95.2% | 66.0462分 |
| 最终版本真实调用测试 | 1题端到端，无技术失败，2次显式模型调用 | 1题端到端，无技术失败，3次显式模型调用 |

与历史验收收据相比，未发现实验后的原文、事实或关系增删改写，现有数据可以复用。LongMemEval校验原文/context/日期、准备语料SHA、数量、事实校验和与向量维度，不包含向量逐字节比较；BEAM比较七张核心表的行JSON哈希，含向量序列化表示。390份冻结服务源码均未变化。

**上述95.2%和66.0462是历史分数复算，不是本轮全量重新答题的准确率。** 本轮完整回放不调用模型；真实调用只验证流程可运行。开发中两轮各对每集1题做过真实测试，共10次客户端显式模型调用，记录全部保留。调用数量不包含服务端内部潜在的模型调用。自动化行为测试13项通过，并通过Ruff与编译检查。

LongMemEval四轮相同逻辑输入的历史成绩为95.2%、94.0%、95.0%、94.0%，均值94.55%；95.2%是单轮最高记录，见[#115](https://github.com/RSIXDB/hindsight/issues/115)。

LongMemEval复用的是已披露的历史输入变体：1段源会话占位替换，答题视图另有涉及27题的固定安全筛选。BEAM复用的是已披露的GLM为主、DeepSeek回退的混合抽取数据。数据与历史收据一致，不等于原始实验条件没有这些处理，也不证明全部抽取事实语义正确。

## PostgreSQL 实际配置

两套库使用同一台PG实例：PostgreSQL 18.7。LongMemEval库为 `contextbase_lmeslow_20261122`；BEAM库为 `contextbase_beam10m_night_20261124`。旧的未完成BEAM导入库不参与本轮。

| 项目 | 实际值 |
|---|---|
| BM25扩展 | **pg_search 0.27.0**，不是pg_textsearch |
| 其他扩展 | vector 0.8.7、pg_trgm 1.6、btree_gin 1.3、plpgsql 1.0 |
| shared_preload_libraries | pg_search,pg_stat_statements；预加载不代表每个数据库都创建了pg_stat_statements扩展 |
| shared_buffers | 4 GiB |
| work_mem | 32 MiB |
| maintenance_work_mem | 1 GiB |
| effective_cache_size | 10 GiB |
| max_connections | 120 |
| max_locks_per_transaction | 64 |
| random_page_cost | 1.1 |
| effective_io_concurrency | 16 |
| max_parallel_workers_per_gather | 2 |
| jit | on |
| TimeZone | Asia/Shanghai |
| default_text_search_config | pg_catalog.english；与pg_search实际tokenizer配置分别记录 |

事实表存在真实BM25索引，包含 `id,text,context,text_signals`，以id为key；mental_models也有BM25索引。原生检索中的BM25与客户端v7的Python BM25选窗是两层机制。配置取证中，ContextBase的pg_search tokenizer显式覆盖为空，不能仅凭PG的english设置推断插件tokenizer完全相同。

只读核验首次采用跨500个workspace的长事务时触及共享锁上限，后改成逐bank快照；没有提高PG共享锁参数。比对哈希时沿用历史Asia/Shanghai会话时区。核验器自身的180秒statement_timeout属于会话设置，不是修改服务端的全局查询超时。

## ContextBase 与模型配置

服务源码提交记录为 `0951c3664b3083a99268040c8c84113073a057b3`，实际390文件冻结指纹匹配；不能只用提交号代替部署指纹。配置文件、运行进程配置、bank覆盖、单次recall参数分别保存。

| 配置 | LongMemEval-S | BEAM 10M |
|---|---|---|
| 抽取 | GLM5.3，adaptive/low；拒答后同输入DeepSeek回退；另1个普通chunk两模型拒答后用原生chunks保留 | GLM5.3，adaptive/low；历史refusal/非法facts时同输入DeepSeek回退 |
| 抽取模式／分块／输出上限 | concise／3000字符／32000 tokens | 相同 |
| Bank覆盖 | 开启全文、图、时间检索与原文存储 | 同左，另有BEAM专用retain_mission |
| Observations／自动consolidation | false／false | false／false |
| Embedding | TEI，BAAI/bge-m3，1024维 | 相同 |
| Reranker | TEI，bge-reranker-v2-m3；候选上限300，batch32，并发2 | 相同 |
| BM25词项控制 | selective_terms=true，max_query_terms=16 | 相同 |
| recall并发上限 | 4 | 4 |
| Worker槽位 | 16 | 8 |
| retain模型并发／retain并发 | 32／4 | 32／4 |
| 本次复测Reader | GLM5.3 adaptive/high，16384输出 | DeepSeek V4.1 Flash，16384输出 |
| Judge | DeepSeek V4.1 Flash，8192输出，作者yes/no | DeepSeek V4.1 Flash，8192输出，AMB逐rubric |
| 额外查询 | 无新增模型改写，使用v7源材料组织 | DeepSeek生成两条查询，8192输出 |

推理设置部分通过 `extra_body` 的thinking/output_config表达，不能只看单独的reasoning_effort字段是否为空。服务配置中的默认recall token值不代替实验请求显式传入的32768/16384或12288/8192；完整预算见[运行说明](README.md)。`CONTEXTDB_RECALL_USAGE_ENABLED=false`，避免实验召回使用计数改写记忆统计。

模型端点在11月24日已经迁移，当前Embedding/Reranker地址与最初实验不同。本轮记录了当前模型服务身份和元数据SHA；原文回放一致不等于证明新召回排名、模型输出或时延逐值不变。

## ECS 位置与恢复状态

- AMB fork：`/srv/contextbase/agent-memory-benchmark`。
- 本轮私有产物：`/srv/contextbase/benchmarks/amb-repro-20261124`，包含只读核验、配置、700题回放和真实调用记录。
- LongMemEval历史目录：`/srv/contextbase/benchmarks/longmemeval-s-night-20261121`；使用其中 `candidates/concise-low` 的500个已验收bank。
- BEAM历史目录：`/srv/contextbase/benchmarks/beam10m-night-20261124`；只使用该目录登记的10个完整bank。
- BEAM隔离API仅为真实调用临时启动，结束后恢复inactive；LongMemEval原有API保持active。没有启动旧导入任务或历史自动调优控制器。

公开的[聚合核验收据](validation-20261124.json)只包含统计、哈希和验证范围。完整来源、请求、响应、上下文、数据库备份及凭据留在ECS，不随代码推送。其他机器复现历史结果需恢复匹配的bank及证据；重新导入或改模型应建立新基线，不能直接沿用历史分数。
