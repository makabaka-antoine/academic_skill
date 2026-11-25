### 文献文档解析：科研阅读：回答问题，也给出依据

#### 1. 题目介绍

你需要围绕“文献文档解析”制作一个可被 Agent 调用的 Skill，在组织方提供的 3 个固定科研案例上完成实际任务，并交付能够被复核的结果。  
读取固定科研材料并完成本题指定的解析、翻译、问答、提取、核验或知识结构化，关键结果回到原文证据。

组织方为本题准备 3 个固定案例。每个案例包含任务说明、科研材料、必要的输入约束和评分所需的证据范围；案例会以相同内容分别交给基础 Agent 和加载参赛 Skill 的 Agent。你不需要自带正式测试案例，也不需要提交预先写好的答案。

1. 先识别输入材料和问题边界。
2. 按题目要求完成阅读或解析动作。
3. 为关键结论保留页码、段落、字段或图表定位。
4. 对未报告、无法判断和材料冲突明确说明。

围绕真实、高频、输入输出明确的科研 PDF 解析任务，开发能被 Agent 调用的 Skill、插件或轻量工作流，重点改善文字提取、多栏阅读顺序、复杂表格结构、章节层级识别、公式与特殊字符保真，以及扫描版 PDF 的文字识别。  
参赛者可选择多栏阅读顺序修正、复杂表格解析、章节结构识别、公式与特殊字符提取、扫描版 PDF 文字识别等具体方向，聚焦一种版式或一个问题即可，不要求开发完整的 PDF 解析系统。图表与公式在本课题中按结构元素识别，不要求理解其科学含义。  
本课题重点关注四个问题：  
提得全： 正文、图表题注、脚注与参考文献能否逐字提取，不漏行、不丢段、不把页眉页脚混进正文。排得对： 双栏、多栏、跨栏与跨页内容能否按原版面顺序输出，不串栏、不错行、不把相邻段落拼成一句。拆得清： 章节层级、表格行列与单元格归属、公式上下标与希腊字母能否还原成可继续处理的结构。核得回： 每段文字、每张表格能否指回原文页码与区块，读不出的部分是否如实标注，而不是靠猜补齐。

可选择的突破方向：  
优先考虑以下五个方向，也允许提出其他具有明确科研价值的 PDF 解析问题。  
多栏阅读顺序与文字提取： 修正双栏、多栏论文的串栏、错行与漏段，处理页眉页脚、边注和跨页续接。顺序对了，后续翻译、问答和知识提取才不会读到拼接错的句子。复杂表格解析： 处理跨行跨列、合并单元格、多层表头与跨页续表，输出 Markdown、HTML 或结构化数据。表格结构还原后，实验参数与结果才能直接比对和复用。章节与版式结构识别： 识别标题、摘要、章节、段落与参考文献，保留多级层级与编号。层级清楚，长文档才能按节切分、定位和引用。公式与特殊字符： 改善数学公式、上下标、希腊字母与特殊符号的提取与保留，按公式编号给出原文对照。符号不失真，方法描述与推导过程才可读。扫描版 PDF 与 OCR： 针对扫描论文、低分辨率页面和 OCR 错字，做图像预处理、识别与后处理校验。扫描件在真实文献库里占比不低，能处理才算覆盖真实输入。

平台固定运行 3 个案例，比较不加载 Skill 与加载参赛 Skill 时的实际结果。双方使用相同案例输入、任务要求和可比条件；需要相对比较的评分项使用组织方预先固定的白板。白板是对照结果，不是参赛者答案。

#### 2. 评分维度概要

原题满分 **100 分**。GitHub 公开作品链接为额外附加分，最高加 **5 分**。综合三个固定案例的实际表现和提交材料评分，各评分项只计一次；本节只展示评分维度、分值和考察内容，不展示内部详细分档。

| 评分维度 | 分值 | 评分点及分值 | 考察内容 |
|-|-|-|-|
| 实际效果 | 40 分 | 任务完成度 15 分；结果正确性 15 分；结果完整性 5 分；结果可核验性 5 分 | 是否真的把一份 PDF 解析成可用的结构化结果（正文、章节、表格、公式、图表位置），而不是给一段"看起来像全文"的文本。；文字内容与原文是否逐字一致、阅读顺序是否正确、表格行列与单元格归属是否准确、公式符号是否失真。；是否覆盖论文的关键结构：标题、作者与单位、摘要、章节层级、图表与题注、公式编号、参考文献、页眉页脚与脚注。；每个解析结果能否指回原文的页码、区块位置与解析依据。 |
| 使用增益 | 25 分 | 质量增益 15 分；效率增益 5 分；增益可验证性 5 分 | 相同条件下加载 Skill、插件或工作流后，相比不加载的基础 Agent 是否提升解析准确性、版式还原度与可用性。；是否减少解析时间、人工校对、重跑与格式转换的成本。；是否通过公平对照证明解析质量或效率改善。 |
| 创新增益 | 15 分 | 新增能力 5 分；方法改进 5 分；创新证据 5 分 | 是否形成基础 Agent 或现有解析方案不具备、或难以稳定实现的能力（如跨栏顺序还原、跨页表格合并、公式结构还原、扫描件增强）。；版面分析、阅读顺序判定、表格结构重建、公式识别、后处理校验等方法是否带来实质改进。；是否证明创新点相对已有解析方案的边界与价值。 |
| 可靠体验 | 10 分 | 调用与易用性 3 分；运行稳定性 4 分；异常处理 3 分 | Agent 能否正确触发和调用，用户能否按说明完成一次完整解析。；在不同版式、不同页数、不同语言文档和重复运行下能否持续产出可用结果。；对加密 PDF、无文字层扫描件、图片型表格、乱码字体、页码错乱等情况的识别与处置。 |
| 生态贡献 | 10 分 | 文档完整性 3 分；资源可复用性 4 分；分享推广价值 3 分 | 使用说明、依赖、配置、示例、适用范围、限制与维护信息是否齐全。；解析器本体、脚本、配置、样例文档与题集能否被他人复用。；是否沉淀可分享的案例、版式样本、Benchmark 与实践经验。 |

质量与增益以平台固定案例、基础 Agent/Skill Agent 对照结果和可核验记录为依据；文字自述不能替代实际运行证据。

**GitHub 公开作品链接（5 分）**　提交本题作品的 GitHub 公开链接。评审无需登录即可打开链接并找到本次提交的作品，得 5 分。缺交或经核实链接无效，本项得 0 分，其他评分项按原规则计分。

#### 3. 交付要求

**提交材料**

参赛者只提交 Skill 及其必要资源；平台统一使用本题固定的 3 个案例运行 Skill。

| 材料 | 必需内容 | 交付标准 |
|-|-|-|
| Skill 本体 | `SKILL.md`，以及实际使用的提示、规则或处理流程；写清用途、适用条件、调用方式、输入输出和异常处理。 | 平台能据此加载并执行给定案例，不能用预制答案代替实际运行。 |
| 运行必需的脚本与资源 | 实际依赖的脚本、配置、模板、词表或其他资源；写清版本、安装方式、路径、来源和使用条件。 | 文件齐备、路径有效，不依赖作者未说明的个人文件或手工操作。 |
| 使用与方法说明 | 完整调用步骤、输入输出解释、关键方法、适用场景、限制、失败处理和自查方法。 | 其他人或 Agent 可按说明独立使用，说明与提交版本一致。 |
| 最小调用示例 | 至少一组输入、调用方式和示意输出；示意输出须标明为示例。 | 足以说明接口和完整使用流程，不冒充正式案例结果。 |

**Skill 运行后的输出要求**

每个案例返回一份结构化结果，保留输入标识、主结果、证据/来源定位、运行状态和不确定性。

| 内容 | 字段与要求 |
|-|-|
| 阅读/解析结果 | 按题目要求的答案、结构化字段、翻译或知识结构；完成标准：覆盖任务要求且能回到材料定位 |
| 证据与边界 | 页码、段落、图表、字段或公式位置；未报告和不确定项；完成标准：关键结论可复核，无法判断不被写成事实 |
| 运行状态 | 成功、部分完成、无法判断或拒答及原因；完成标准：异常原因和可人工接管位置清楚 |

正式测试案例、白板和测评结果由平台提供或生成，参赛者无须另交三个测试案例、白板或预先生成的正式答案。材料缺失、无法判断和运行失败要分别说明。

**本题作品的 GitHub 公开链接**　请在提交材料中填写作品所在的 GitHub 仓库、目录或文件链接。如一个仓库包含多个作品，请直接链接到本题作品所在位置，或在链接旁注明目录或文件名。提交前请用未登录 GitHub 的浏览器窗口打开一次，确认能看到作品。

请在提交目录根路径放置 `GITHUB_URL.txt`，第一行写 GitHub 链接。如需指定作品文件，可在第二行写本次提交目录中的相对文件路径。

## Harbor技术提交约定

# Runtime contract
This is a Skill authoring task. Create /app/submission/SKILL.md and any required text resources. Use valid name/description YAML frontmatter. Do not submit prepared answers or formal test outputs. The evaluator installs the actual Skill under /skills/weekly-skill, then runs official DeepSeek Harness on each fixed original case.

At execution time original input is /input/case.json and task.md. Material and artifacts are provided according to the topic; PDF parsing receives the actual PDF without a pre-extracted answer layer. Read actual files instead of assuming source content from labels. Work under /app/work; final actual results and editable artifacts belong under /app/output, including result.json with all original output_contract fields and case_id. Include honest status, sources, uncertainty and change records. Editable Office/slide/diagram outputs should also have real rendered PDF/PNG previews for inspection. A partial result must identify its limits. Do not fabricate missing software state or scientific data.

Model, source/case versions, runtime, budgets and actual native traces are recorded by the platform; do not invent benchmark scores. Execution budget: 64 model requests, 900 seconds, 16384 tokens per response including reasoning. Save honest partial deliverables early and improve incrementally. Three cases, three repetitions per condition; actual service failures are paused separately. The platform provides/generates any required comparison; participants do not provide hidden tests or whiteboards. Skill resources: at most 50 UTF-8 text files, 500KB each and 2MB total, no symlinks. Runtime artifacts may be binary and are retained independently of final JSON validity.

实际输入文件和原论文是事实依据；不依据错误的领域标签补造数据。


Resource entry point: environment/resources.yaml. Bundled files are relative to that directory; declared preinstalled runtime paths describe the pinned image. The actual fixed scientific cases are supplied by the evaluation platform.


---

题面已公开，正式提交开放时间以赛事通知为准。
