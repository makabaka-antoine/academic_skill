# 可核验文献翻译 Skill

**版本 1.0.0** · 调用名 `traceable-literature-translation`

公开作品地址（上传后）：https://github.com/makabaka-antoine/academic_skill/tree/main/文献翻译

把英文科研材料译成有来源定位的中文对照稿。用源段和源句标识检查覆盖，用逐次注册表核验术语、数值、公式及引用，用语义复审检查实体、关系、限定和证据强度。问答、提取与已有译文核验可使用同样的定位约定，按题目范围交付。

## 安装与调用

把本目录交给兼容 Agent Skills 的平台，或放在宿主配置的技能目录中，从 `SKILL.md` 加载。脚本只需要 Python **3.10 或以上**即可处理 UTF-8 Markdown/TXT。原生 PDF 的可选依赖是 PyPI 发行包 `pypdf==6.21.4`。安装到实际运行脚本的 Python 环境。

```text
python -m pip install -r requirements-pdf.txt
```

脚本从技能目录的 `scripts/` 运行，相邻模块一并提交。纯文本路线和演示可以直接离线运行。翻译由加载 Skill 的 Agent 执行。脚本没有模型 API、上传和自动安装步骤。包内示例为合成材料，pypdf 使用其发行包所附的 BSD-3-Clause 许可。

### Harbor 赛题运行约定

评测安装后的入口是 `/skills/weekly-skill/SKILL.md`。Agent 必须先读取 `/input/case.json`、`/input/task.md` 和实际材料，在 `/app/work` 处理中间状态，并把最终结果写入 `/app/output`。最终 `result.json` 必须含本题 `case_id`、原 `output_contract` 的全部字段、真实状态、来源定位、不确定性及变更记录。可编辑 Office、幻灯片或图示交付物如由题目要求，还须生成真实的 PDF/PNG 预览。正式案例由平台提供，本包不包含预制正式答案或虚构测评分数。

给 Agent 的调用示例

> 使用 $traceable-literature-translation，把上传论文第 2–5 页译为简体中文。交付逐段双语稿，保留公式、图注和引文。另核对论文是否报告了外部验证，给出原文页码。

## 从原文到结果

以下命令从本目录运行。将 `paper.pdf`、范围和 `case-run` 替换为本次任务值。

```text
python scripts/translate.py prepare --source paper.pdf --pages 2-5 --case-id case-id --run-dir case-run
python scripts/translate.py plan --run-dir case-run --glossary glossary-input.json
python scripts/translate.py check --run-dir case-run
```

1. `prepare` 生成 `source_map.json`。指定 `--pages` 时使用 PDF 物理页或已声明页锚。`--section` 可选取唯一的已识别标题及下属章节。粘贴片段可直接保存为 UTF-8 文本，定位采用真实行号。
2. `plan` 生成 `glossary.json` 和 `requests/chunk-*.json`。无词表文件时可以略去 `--glossary`。在首次翻译前完成必要术语选择。Agent 读请求并把真实翻译响应写到 `responses/`。完整格式见[数据契约](references/contracts.md)。
3. `check` 驱动返修与导出。脚本第一次检查后，Agent 依据原文和回填后的译文进行语义复审，填入 `reviews.json` 后再次运行第三条命令。`result.json` 显示待返修的单元、待复审项和源材料缺口。
4. 仅更改问题单元的响应，保留其他结果。词表改动后重新规划再检查，按受影响单元重译。材料或范围变化时另开运行目录，避免沿用旧页码。所有路径按技能目录或运行目录解析。

证据问答、提取和已有译文核验的输入范围由题目确定。Agent 可使用 `prepare` 取得有关位置，也可直接使用实际读到的原页与段落。结构化交付参考[局部证据任务](references/contracts.md#scoped-evidence-tasks)。

## 输出

| 路径 | 内容 |
|---|---|
| `bilingual.md` | 原文、译文、段号及证据定位，按原文顺序展示，标明待核段落和页面缺口 |
| `alignment.jsonl` | 每个单元的原句与译句映射、批次指纹、术语与保护项、机器诊断和复审 |
| `glossary.json` | 词表、准用形式、术语适用范围和版本 |
| `result.json` | 输入标识、交付路径、范围统计、来源、状态与具体问题 |
| `source_map.json` | 提取依据、冻结的源段、页状态、所供转写修订证据 |

`success` 表示该次译稿满足所记录的交付范围和检查。`partial` 保留已完成的结果与待接管位置。材料缺失或核心材料不可读可返回 `blocked`，依赖、执行和数据契约出错可返回 `failed`。命令退出码 `0` 表示该步骤成功完成，`1` 表示部分完成或已明确阻断，`2` 表示执行或契约错误。`prepare` 和 `plan` 的成功仅对应各自阶段。已有交付时，当次命令异常另写 `last_error.json`，以命令返回的状态判断本次是否完成。

## 可复现的最小示例

```text
python -B examples/minimal/run_example.py --run-dir demo-run
```

`examples/minimal/source.md` 是两页带页锚的合成材料，`glossary.json` 指定三次出现的术语。脚本用预置**演示响应**跑真实的准备、检查与组装代码，并把示例复审标为 `fixture_review`。当前示例输出含 `success`、2 个已译单元、3 次术语恢复、2 次组别归属匹配。真实任务在同样的接口上由 Agent 读取并翻译该次输入。

自查时从新目录运行上面的示例，核对 result.json、双语稿及对齐记录中的页码和段号。再从本目录运行回归测试：

```text
python -B -m unittest discover -s tests -v
```

测试只使用合成材料和临时目录；PDF 用例在已安装可选依赖时运行，否则明确跳过。[首版规划与验收口径](references/design.md)说明如何复核段落、术语和数值归属。

## 已知边界与接管

- 有文字层的 PDF 采用解析器提供的文本顺序。旋转或可疑栏序会标注。复杂跨页、跨栏、图内数据、扫描数学和部分专用字体需要原页复核。读取不到的页还在页覆盖分母里。
- 文本中的常见 LaTeX、引用格式和数值可作为保护项。原生 PDF 中失去结构的公式、原本含糊的单位、超出识别规则的引用和源文冲突需要按原页定位复核。
- 自动数值归属只覆盖显式组名的简单一值陈述、清楚的对应列表及源句或表格单元的注册位置。规则未覆盖的绑定仍需对照原文判断。词表版本记录选择来源，术语词形通过也只证明所登记的用法得到执行。
- 无可靠 OCR 时脚本报告影响页，已判读内容可继续处理。未报告、未读到和材料缺失分别记录。发现重要图片对象提取缺失时，在复审记录中列出原页或图表位置。
