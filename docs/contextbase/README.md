# ContextBase LongMemEval-S 和 BEAM 10M 复测

这套入口将 issues [#115](https://github.com/RSIXDB/hindsight/issues/115) 和 [#119](https://github.com/RSIXDB/hindsight/issues/119) 的客户端优化固化到 AMB fork。保留上游 `amb` 的默认行为，使用独立的 `memory_bench.contextbase` 模块。只复用已核验的 bank，不包含导入、删除 bank、重建索引、重试写入任务或修改服务配置的操作。

上游基准：`01bd83f3fa5245d6be3986cfab81fdd828f26acd`。历史提示及来源分别固定在 `prompts.py` 的注释中；本实现没有把升级上游提示后的分数冒充为历史复现。

## 成绩与复测状态（2026-11-24）

| 测试集与方法 | 历史完整运行成绩，本次重新核算 | 本次全题集回放 | 本次新模型调用测试 |
|---|---|---|---|
| LongMemEval-S / v7 | **95.2%**（476/500，作者 yes/no 判题） | 500/500 题上下文和 Reader 提示与历史一致 | 1题完成、答对；2次显式模型调用，0次技术失败 |
| BEAM 10M / I 多查询 | **66.05%**（200题，AMB rubric 题均分；原始值 0.6604620310245312） | 200/200 题上下文和展示文档ID与历史一致 | 1题完成、得分0；3次显式模型调用，0次技术失败 |

**95.2% 和 66.05% 是历史完整运行成绩的复算，不是本次固化代码重新完成全量答题、判题后的成绩。** 全题集 `replay` 不调用模型；本次新跑每集1题，仅验证调用流程，不能据此估计整体准确率。尚未完成基于本工具包的新一轮全量模型复测，也未据此计算相对基线的提升幅度。

LongMemEval-S 相同逻辑输入的四轮历史结果为95.2%、94.0%、95.0%、94.0%，均值 **94.55%**；95.2% 是其中最高单轮，详见 [#115](https://github.com/RSIXDB/hindsight/issues/115)。这些仍是历史结果，不是本工具包新跑成绩。

成绩适用条件：LongMemEval-S 历史输入有1段源会话占位替换，答题视图另有涉及27题的固定筛选，因此不能标为未修改官方语料成绩；BEAM 使用 AMB rubric 评分，不能与作者原版 Kendall 指标直接比较。两个方案均已用测试集调参。

核验依据：[机器可读验证结果](validation-20261124.json)记录全题集回放、历史分数复算及单题调用次数；[ECS 验证记录](ecs-validation-20261124.md)记录数据复用、环境配置和验证范围。复测步骤见下文第3、4节。

## 方法与验证范围

| 测试集 | 增强方法 | 固定预算和模型 |
|---|---|---|
| LongMemEval-S | v7：按问题 bank 补读已存原文；用户原话按时间保留；短答补必要前文；BM25选择助手问答；无抽取摘要正文 | 原生召回事实32768、原文16384；最终48000 o200k tokens，用户最多30000、助手最多16000；GLM5.3 adaptive/high，输出16384；DeepSeek裁判8192 |
| BEAM 10M | I：保留原问题召回材料；只根据问题生成两个互补查询；两次补充召回；按事实ID去重、交替追加完整文档 | 初次事实12288、原文8192；每次补充6144/4096；最终60000 cl100k tokens；DeepSeek Reader16384、改写器8192、裁判8192 |

BEAM使用固定AMB逐rubric判分与题均分，包含事件排序；不是作者原版Kendall指标。LongMemEval使用作者的分类与abstention yes/no判题，按作者的 `yes` 解析计分，并另记严格yes/no格式状态。开跑前仅检查评估清单结构和评分依据是否完整；金标、rubric、题型和abstention不进入检索、改写或Reader，实际判题在答案保存之后执行。

LongMemEval历史抽取为GLM为主、拒答时同输入DeepSeek回退；另有1个普通会话的1个chunk经两模型拒答后用原生chunks保留。历史库存在1段源会话占位替换，答题视图另有涉及27题的固定安全筛选。v7保留该筛选与原有提示，以便重建历史材料；不是未修改原语料的严格复现。BEAM历史库为已披露的GLM/DeepSeek混合抽取。两个方案均已用测试集调参，分数不能作为独立盲测或跨产品同条件排名。

- `replay`：重建历史上下文、核对提示或文档ID、复算已有分数；不调用模型，不重新判题。
- `run`：新召回、新改写/答题/判题；有物理模型调用上限，失败保留在分母中。小样本只验调用链，不报告为全量准确率。

第一批保护修复的边界与验证见[保护修复说明](phase1-guards-20261124.md)。

## 安装与凭据

```bash
git clone https://github.com/RSIXDB/agent-memory-benchmark.git
cd agent-memory-benchmark
git switch codex/contextbase-lme-beam-repro
```

轻量入口 `bash scripts/contextbase/run.sh ...` 通过uv安装固定的四个依赖，不需要安装AMB全部记忆供应商。已有依赖的ECS优先设置 `CONTEXTBASE_PYTHON`，入口会直接复用该解释器，避免下载另一套Python和依赖：

```bash
export CONTEXTBASE_PYTHON=/srv/contextbase/source/.venv/bin/python
bash scripts/contextbase/run.sh --help
```

也可以直接运行模块：

```bash
export PYTHONPATH="$PWD/src"
uv run --no-project /srv/contextbase/source/.venv/bin/python -m memory_bench.contextbase --help
```

模型凭据通过 `CONTEXTBASE_MODEL_URL`、`CONTEXTBASE_MODEL_KEY` 环境变量提供，使用Anthropic兼容 `/v1/messages` 接口。URL应是该接口的前缀，例如 `https://provider.example/compatible`，不要重复追加 `/v1/messages`。每个模型也可在配置中指定不同的环境变量名。

普通API key通过 `CONTEXTBASE_READ_KEY` 提供；BEAM逐bank读取密钥可通过 `--bank-keys-file` 指定。原文SQL读取使用 `CONTEXTBASE_SOURCE_DSN` 或 `--source-dsn-file`。优先使用只读凭据；代码的来源查询在只读事务中执行，读取前核对bank到workspace的映射。该SQL通道用于本次历史复测，整bank读取不作为10M通用产品方案。

**原文、gold、密钥、模型请求返回和运行产物只留在ECS私有目录。不要提交到public仓库。** 默认写入文件为0600，新建运行目录为0700。提供 `configs/contextbase/*.example.json`；机器相关配置放在 `.contextbase/` 或仓库外。

## 1 只读核验已有数据

先定义本机已有历史目录；ECS当前位置见验证记录。该脚本只读取历史凭据和收据，不执行历史控制器：

```bash
uv run --no-project --python 3.11 --with asyncpg==0.30.0 python scripts/contextbase/audit_existing.py \
  --lme-root "$LME_ROOT" --beam-root "$BEAM_ROOT" --output .contextbase/audit
```

核对500个LongMemEval bank的文档正文/context/日期、表数量、事实校验和、向量维度、完成任务；核对10个BEAM bank七张核心表的行JSON哈希（含向量表示）与任务状态。保留插件、索引和PG设置记录。LongMemEval向量不做逐字节哈希；这不是对全部事实语义正确性的证明。

逐bank采用只读快照，避免500个workspace在同一事务中耗尽共享锁。哈希必须沿用历史会话时区：本轮为Asia/Shanghai。时区差异会改变带时间字段的行JSON，不应直接判为数据污染。失败时停止复用，不自动重导。审计目录由进程锁保护；每次生成独立run ID，只有本次完整成功清单及匹配哈希的报告可用于新跑。重新审计失败或中断后，旧成功报告不可继续用于新跑。

## 2 生成机器相关复测配置

```bash
bash scripts/contextbase/run.sh prepare-history --dataset longmemeval-s \
  --historical-root "$LME_ROOT" --audit-directory "$PWD/.contextbase/audit" \
  --output "$PWD/.contextbase/lme"
bash scripts/contextbase/run.sh prepare-history --dataset beam10m \
  --historical-root "$BEAM_ROOT" --audit-directory "$PWD/.contextbase/audit" \
  --output "$PWD/.contextbase/beam"
```

生成独立的检索问题清单、评估金标清单及 `recipe.private.json`；BEAM同时生成仅供本机使用的逐bank密钥映射。准备过程不复制整份历史原文、不调用模型、不导入数据。私有配置中核对API地址、模型、预算、输出目录；基线对照另用新输出目录，将 `method` 改为 `baseline`。基线含义以固定协议和实际prompt为准，不保证等同所有历史公开运行。

## 3 全题集无模型回放

```bash
bash scripts/contextbase/run.sh replay --config .contextbase/lme/recipe.private.json \
  --source-dsn-file "$LME_ROOT/candidates/concise-low/instance/input/database-url" \
  --historical-root "$LME_ROOT" --report .contextbase/lme-replay.json
bash scripts/contextbase/run.sh replay --config .contextbase/beam/recipe.private.json \
  --historical-root "$BEAM_ROOT" --report .contextbase/beam-replay.json
```

LongMemEval从当前PG原文和冻结召回重建500题最终材料及Reader提示；BEAM从冻结原始召回、改写查询及两次补充回包重建200题材料。发生差异返回非零退出码。历史上下文只用于最后比对，不参与选证据。

## 4 新召回与真实答题复测

新跑前先用当前代码生成成功审计，并按下文“配置取证”生成本次服务快照 `effective-config.json`。两套测试均需提供对应数据库的只读连接，以核对数据库身份和bank配置。

确认API已按其认证部署流程启动、写入队列为空。不要为了看API而启动旧的未完成导入实例。脚本不负责启动服务。

```bash
bash scripts/contextbase/run.sh run --config .contextbase/lme/recipe.private.json \
  --source-dsn-file "$LME_ROOT/candidates/concise-low/instance/input/database-url" \
  --read-key-file "$LME_ROOT/candidates/concise-low/instance/runtime/admin-key.json" \
  --environment-report .contextbase/lme/effective-config.json \
  --execute-models --limit 1 --max-model-calls 8
bash scripts/contextbase/run.sh run --config .contextbase/beam/recipe.private.json \
  --bank-keys-file .contextbase/beam/bank-keys.private.json \
  --source-dsn-file "$BEAM_ROOT/instance/input/database-url" \
  --environment-report .contextbase/beam/effective-config.json \
  --execute-models --limit 1 --max-model-calls 8
```

上例每集只测1题。完整复测删除 `--limit`，事前按LME的Reader+Judge、BEAM的改写+Reader+逐rubric Judge计算物理调用上限，并使用新输出目录。初次完整成功调用至少约LME1000次、BEAM1021次；错误尝试也占用上限，自动重试关闭。上限只约束本工具包显式的改写、Reader和Judge调用；服务端内部可能的模型调用及费用另由服务端记录。

运行开始到退出持有输出目录独占锁，同目录第二进程直接拒绝。协议同时固定实际模型端点指纹、数据库连接指纹及服务配置快照；不写入明文密钥。源读取另比较审计中规范化正文指纹，完整性校验失败直接终止、不发布新汇总成绩，不将此类错误改判为0分。常规答题／判题技术失败仍按原协议保留在分母中。

配置快照需要在本次运行前重新采集；工具比对的是所提供文件，不能自动证明代理后端未更换模型，也不替代运行前后完整审计。新协议与旧输出目录不兼容，请新建目录；旧成绩和原始产物保持原样。

运行固定输入、评估清单、审计收据和代码哈希；同目录配置变化会被拒绝。每题独立模型调用，检查点命中不重复调用；未决或失败的物理请求需要先核对原始记录，不自动重试。恢复不能重置整次运行的调用上限。失败题仍计入分母，进程非零退出，不能从另一轮挑高分答案补入。

## 配置取证与测试

每个实例分别设置其 `API_ENV`、`API_PID` 和 `EFFECTIVE_REPORT`；后者对应上例的 `.contextbase/lme/effective-config.json` 或 `.contextbase/beam/effective-config.json`。新跑使用POSIX进程锁，适用于本次Linux ECS与macOS。

```bash
uv run --no-project "$CONTEXTBASE_PYTHON" scripts/contextbase/capture_configuration.py \
  --source-root "$CONTEXTBASE_SOURCE" --env-file "$API_ENV" \
  --runtime-pid "$API_PID" --output "$EFFECTIVE_REPORT"
PYTHONPATH=src uv run --no-project "$CONTEXTBASE_PYTHON" \
  -m unittest discover -s tests -p test_contextbase_recipes.py -v
```

取证脚本仅解析配置和哈希源码，不启动服务或调用模型；停止实例可省略 `--runtime-pid`，结果会标记为配置文件快照。共享PG参数与服务运行配置、bank覆盖、单次recall预算分别记录。模型服务地址迁移应重新记录模型身份与配置，不能仅凭同一个服务commit声称环境完全一致。

代码来源及算法边界：`lme_v7.py`保留历史v7的渲染和选择行为；`prompts.py`提取固定版本提示；`beam.py`实现#119的合并顺序。原始数据、模型和评分的上游归属保留，不将本工具包称为上游或数据集作者认证。

## LoCoMo 与通用能力实验

- [LoCoMo 持久化复测方法](locomo-reproduction.md)：固定输入、原文审计、保留 bank、首次有效答案检查点与模型调用上限。
- [实验性服务端原文证据](enhanced-memory.md)：默认关闭的独立原文检索、BM25 选窗、去重和完整上下文预算。需要配套服务分支及派生索引；并非已验证四集都能提升的默认能力。
- PersonaMem 最新 Qwen4-Max cleanrole 500/589 的严格复测需要原始提示词、配置及其哈希。只有历史成绩或策略描述不能代替冻结工件；在拿到工件前不自动发起 PersonaMem 答题调用。
