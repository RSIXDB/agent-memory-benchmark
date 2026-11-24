# ContextBase 夜间复测记录

执行窗口：2026年11月24日23:57:59至11月25日08:57:59（北京时间）。数据、原始请求、模型响应和密钥保存在私有运行目录，仓库只发布工具、配置、汇总及工件摘要。

## 已完成的复测

| 测试 | 本次结果 | 解释 |
|---|---|---|
| LoCoMo 10 统一配置 | 1450题正确，1538/1540完成；全分母失败计零94.16%，缺失两题可能区间94.16%–94.29% | GLM-5.3 reader32768、Qwen4-Max judge4096；1540题全新答题和判分，复用冻结召回；没有重抽完整答案 |
| LoCoMo #112历史工件核算 | 1452/1540，94.29% | 历史结果重算，不能当成本次新复测 |
| LongMemEval-S v7历史工件核算 | 476/500，95.2% | 复用原先输入变体；1段占位替换、27题历史阅读视图筛选，不能称为未修改的官方原始输入成绩 |
| BEAM 10M #119历史工件核算 | 66.0462% | 历史结果重算；沿用AMB逐rubric评分协议 |
| PersonaMem Qwen4-Max cleanrole | 未答题，无本次500/589成绩 | 已保留195份源文档、6435条记忆；严格复测仍需要最新cleanrole原文与哈希、原B16配置或冻结上下文，不以重新编写的提示冒充原实验 |

PersonaMem目标配置来自issue114最新记录：Qwen4-Max默认档、recall8192/chunk20480、cleanrole提示。cleanrole是清洗后的角色归属提示词版本，不是服务端参数；本轮未取得完整原文与哈希，因此不发起答题费用。

LoCoMo两题失败分别涉及Reader输出格式/完整性与Judge非JSON传输响应；保留首次记录，不另抽答案补分。早先为兼容网关而产生多种输出上限的1540题诊断运行另存，不作为此统一配置成绩。

## 通用服务端方案

实验路由默认关闭。启用后统一执行原生事实召回、授权原文词法候选检索、BM25消息选窗、相邻问答补全、去重及完整证据预算控制，不接收测试集名称或gold，不增加生成模型调用。

| 固定探索/验证 | 原生对照 | 原文增强 | 配对观察 |
|---|---:|---:|---|
| V1 LoCoMo60题 | 59/60 | 58/60 | 0改善、1回退；统一LoCoMo对照的预声明补充比较 |
| V1 LongMemEval-S60题 | 59/60 | 57/60 | 0改善、2回退；相对历史v7为2改善、2回退 |
| V1 BEAM60题 | 67.0417% | 68.6458% | +1.6042个百分点；与客户端多查询66.8056%比较需注明增加调用的差别 |
| V2 BEAM剩余140题 | 60.3787% | 61.8537% | 有效配对138/140，25改善、17回退；会话聚类95%区间[-2.1648,+5.5248]个百分点，未证实稳定增益 |
| V3 NativeFirst LoCoMo50题 | 48/50 | 50/50 | +2题，原生事实平均保留98.74% |
| V3 NativeFirst LongMemEval-S50题 | 47/50 | 48/50 | +1题，原生事实平均保留100% |

V1把材料预算过多分给原文，追踪中原生事实平均仅保留18%–48%。V3采用统一native_first64K：优先保留事实及日期、来源、实体和其他语义字段，将重复context只保存一份，再补原文窗口。50题扩展样本按固定种子选取，排除先前60题试验；它们仍是公开、曾经评测的数据，不能称为全新未见测试集。

本轮新的LME双臂直接使用同一份已审计PG原文与实时召回，不套用v7涉及27题的历史阅读视图筛选；原文仍沿用那1段占位替换，因此结果属于已披露的固定输入变体。

V2另有缺失源日期被导入日期替代的问题，140题中1题选中这种窗口。V3保留未知日期，REST拒绝尚未完整支持的历史截止过滤。V2质量统计不能归属于V3。

在查看V3质量结果前冻结了扩展规则：两集各50有效配对、平均事实保留≥95%、均不回退且至少一集改善、剩余时间≥3.5小时，才以相同参数扩展全量；扩展保存首次结果，单独报告先导50题和其余确认集。完整扩展已结束；结果如下。


## NativeFirst V3 完整扩展结果

**尚未证实同一方案可稳定提高多个测试集的准确率，继续默认关闭。** LoCoMo本轮小幅上升，LME本轮回退；不能把V2 BEAM和V3 LoCoMo的不同版本结果拼成统一跨集收益。全部首次结果保留，不追加调参或重抽答案。

| 测试 | 原生对照 | NativeFirst64K | 全量分母变化 |
|---|---:|---:|---:|
| LoCoMo1540题 | 1450正确，1538有效，94.1558% | 1454正确，1538有效，94.4156% | +0.2597个百分点 |
| LongMemEval-S500题 | 463正确，499有效，92.6% | 461正确，500有效，92.2% | −0.4个百分点 |

本表分母固定1540/500，技术失败计零，不把它们混称为全部有效答题。LoCoMo原生缺2题、增强缺2题，其中1题重叠；共同有效1537题为1449→1454，24改善、19回退，会话聚类bootstrap95%区间为[−0.4264,+1.1017]个百分点。扣除先导50题后，剩余1487个有效配对仅净+3题，区间仍跨零。

LME共同有效499题为463→461，11改善、13回退，问题级bootstrap95%区间为[−2.4048,+1.4028]个百分点。扣除先导50题后，剩余449个有效配对为416→413。原生缺失1题是空模型回答；增强500题均完成。LoCoMo增强两题分别是无法唯一解析结构化回答、非JSON传输响应；均保留原记录，不补抽答案。

**确定的工程收益是输入压缩与低额外检索开销。**

| 指标 | LoCoMo | LongMemEval-S |
|---|---:|---:|
| 成功Reader请求平均输入tokens，原生→增强 | 113771→64970，减少42.89% | 122683→66482，减少45.81% |
| 原生fact条目平均保留率 | 99.07% | 100% |
| 原文查找+组装平均额外耗时 | 39.70ms | 66.88ms |
| 额外生成模型调用 | 0 | 0 |

输入均值按成功Reader回包usage统计，LoCoMo各1539条、LME499/500条；不是总费用或总端到端时延的承诺。额外检索耗时在并发实验环境测量。全部2040份增强材料都满足64K完整证据预算；Reader提示和问题在此之外。LoCoMo仅3题最终选入新的源文档，LME为101题；更多原文候选和fact保留率不能替代准确率验收。

优先产品化的候选点是“完整材料预算与重复上下文压缩”；“独立原文补检与消息选窗”保留为独立实验模块。下一轮只围绕已有证据保真、原始关联chunk和补检机制做同预算消融，固定bank、Reader、Judge和提示；不直接用该组合替换已有专项v7或多查询配置。

完整聚合值与区间算法见[机器可读结果](night-results-20261125.json)。代码与文档分别进入[AMB PR2](https://github.com/RSIXDB/agent-memory-benchmark/pull/2)及[服务端 PR120](https://github.com/RSIXDB/hindsight/pull/120)，均保持草稿，不合入默认功能。

## 配置与可复用数据

PostgreSQL18.7，原生全文BM25使用pg_search0.27.0；不是pg_textsearch。vector0.8.7、pg_trgm1.6、btree_gin1.3。shared_buffers4GiB、work_mem32MiB、maintenance_work_mem1GiB、effective_cache_size10GiB、max_connections120、random_page_cost1.1、jit=on、max_parallel_workers8、每gather并行2。

新增原文候选使用PostgreSQL英文FTS/GIN及ts_rank_cd；消息选窗使用Python BM25，与原生pg_search事实BM25分开计量。派生索引不复制完整原文，需要管理员显式构建/重建。

Embedding为BAAI/bge-m3 float16，reranker为BAAI/bge-reranker-v2-m3@953dc6f float16，通过自建tei-compat/1.0接口运行；该实现不是官方HuggingFace TEI。tokenizer为o200k_base。

LoCoMo新bank使用verbose、固定fidelity mission、DeepSeekV4.1Flash high、retain chunk3000、实际抽取输出上限64000；PersonaMem新bank使用concise、DeepSeek默认effort、chunk3000、上限16384。两者关闭observations及自动consolidation。PersonaMem新bank是准备环境，不是原B16的同一银行或等价性证明。已有LME/BEAM抽取模型不能仅凭当前进程配置追溯认定。

原有LME500个bank与BEAM10个bank通过前后只读审计，核心数据在审计范围内一致。LME核验原文、bank配置、事实非向量字段及向量维度；BEAM核心表摘要包括向量文本表示。不能宣称旧库向量原始二进制经过前后对比。新LoCoMo/PersonaMem另存原文、事实及向量二进制指纹，用于后续复核。

## 复测入口和边界

- LoCoMo：使用[持久化复测入口](locomo-reproduction.md)，新建数据仅运行一次ingest；原生与增强各用独立输出目录，共用同一bank。
- LME/BEAM：按[主README](README.md)执行审计、prepare与环境取证，再用冻结recipe运行。历史baseline/v7/multiquery保持原协议；本轮LME完整SDK JSON对照使用显式native-sdk-json配置。
- 服务端原型：[增强模式说明](enhanced-memory.md)。管理员准备索引并打开服务路由后，AMB使用AMB_ENHANCED_MEMORY=true，或在LoCoMo recipe中设置enhanced_memory=true。
- PersonaMem：最新cleanrole准确工件补齐前，停止答题费用；GPT调用为零，也未执行GPT额度重置。

本轮采用AMB的Reader/Judge与评分协议变体，不代表数据集作者的排行榜认证。gold只进入Judge或离线分析，未进入检索、选窗或Reader；没有按错题选择全量复测对象或拼接最优答案。

默认关闭路径的OpenAPI与基准逐对象相同，80个path。客户端生成脚本因缺cargo未完成；OpenAPI导出成功，文档构建因Node依赖未完成。存储边界原库检查受到基准system_config.py的UTF-8 BOM影响，不能标记为全库通过。原型尚未完成自动索引生命周期、生产资源治理或正式SDK发布，保留实验状态。

本轮服务/SDK基准为ECS部署的`0951c3664b3083a99268040c8c84113073a057b3`，AMB基准为`fbea5e11f563ab193a5fa1c32cb3a1387a941caf`。V2原始测分提交为`8268cafeb`，其API代码等价的main评审提交为`d79f07722`；V3以`0d584d210`为评审实现；冻结运行目录另保存逐文件SHA和原始代码快照。最终服务分支额外修正了授权拒绝的HTTP状态，成功执行路径与测分版本一致。

改动同时涉及证据表示、原文选择及最终预算，当前对照检验的是组合方案，不能单独把分数变化归因于BM25。后续优先做同预算的事实保真与独立原文检索消融，不扩展按题号或gold切换策略。

方法来源：[LoCoMo #112](https://github.com/RSIXDB/hindsight/issues/112)、[PersonaMem #114](https://github.com/RSIXDB/hindsight/issues/114)、[LongMemEval #115](https://github.com/RSIXDB/hindsight/issues/115)、[检索与编排 #118](https://github.com/RSIXDB/hindsight/issues/118)、[BEAM #119](https://github.com/RSIXDB/hindsight/issues/119)。

验证补充：38项客户端检查、17项服务端逻辑/权限检查、9项真实PG检查通过；700份历史材料与提示回放一致。本轮4080份Reader提示（LoCoMo双臂各1540、LME双臂各500）与公开实现重建结果逐一一致，额外模型调用为零。
