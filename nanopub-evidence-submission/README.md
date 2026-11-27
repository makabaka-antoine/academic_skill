# Nanopub Evidence

`nanopub-evidence` 1.0.0 将论文中的关键陈述拆成可独立核查的断言。每条断言带有原文证据、限定条件、极性、语气、归属和来源版本。机器输出使用 JSON、JSONL、CSV 和四命名图 TriG。

公开作品地址（上传后生效）：https://github.com/makabaka-antoine/academic_skill/tree/main/Nanopublication

## 安装与运行

把整个技能目录放到支持 Agent Skills 的宿主里。Harbor 会将提交内容安装到 `/skills/weekly-skill`。在其他环境中使用当前目录里的 `scripts/np_evidence.py`。运行需要 Python 3.9 或以上。Markdown、纯文本、LaTeX 文本、DOCX 正文、JSON、HTML、CSV 和 TSV 的处理使用标准库。

| 能力 | 可用依赖 |
|---|---|
| PDF 正文和版面坐标 | PyMuPDF 或 Poppler `pdftotext`。支持表格检测的 PyMuPDF 版本也会索引部分有网格线的表格 |
| PDF 正文和物理页码 | `pypdf` 作为后备 |
| 扫描页或局部图像复核 | Tesseract 加 PyMuPDF 或 Poppler `pdftoppm` |
| 独立解析输出的 TriG | `rdflib` 可选 |

`doctor` 检查这些依赖。如果赛事镜像中的工具未在 PATH 中，按平台提供的 `environment/resources.yaml` 定位预装工具。文本流程和固定格式导出可离线运行。

~~~bash
python3 -B scripts/np_evidence.py doctor
python3 -B scripts/np_evidence.py prepare --input paper.pdf --work work --out out
python3 -B scripts/np_evidence.py outline --work work
python3 -B scripts/np_evidence.py read --work work --page 1
python3 -B scripts/np_evidence.py candidates --work work
python3 -B scripts/np_evidence.py build --work work --out out --draft work/claims.json --final
python3 -B scripts/np_evidence.py verify --out out
~~~

从 `read` 输出挑选实际证据后，按 [字段字典](references/field-dictionary.md) 构造 `work/claims.json`。案例模式把准备命令改为 `prepare --case <case.json> --work work --out out`。工作区快照保存输入指纹。源文件变化后，重新建立索引并重新绑定断言。

扫描 PDF 默认尝试有限的 OCR。中文扫描件可以在有语言包时使用 `--ocr-language chi_sim+eng`。`--ocr off` 关闭 OCR。用 `page --work work --source S1 --number 2 --layout` 对照相应物理页的文本版面，或加 `--render work/page-2.png` 生成单页图像。若某个图表的关键数字只能从图像辨读，且无法形成可核回的支持片段，把该结论标为 `unclear` 并写明页码和图表编号。

## 输出

| 文件 | 内容 |
|---|---|
| `result.json` | 案例标识、状态、输入、主结果、来源、边界、变更记录和原案例契约字段 |
| `claims.jsonl` 和 `claims.csv` | 已通过导出检查的断言，ID 和证据定位一致 |
| `nanopubs.trig` | 每条已接受断言一个四图草稿，保留条件、极性和语气 |
| `evidence_index.json` | 被引用的文本单元、页或行位置、句界与指纹 |
| `claim_audit.json` | 已接受、被拒绝和无法确定的提案，含具名诊断 |
| `review.md` | 人工复核时可按断言键、证据、诊断快速定位 |

先提交可复核的部分结果，再补来源和覆盖缺口。`build` 的退出码 0 表示结果状态为成功，2 表示部分完成或无法判断且已写结果，1 表示执行失败。`verify` 的退出码表示文件一致性验证，业务状态仍以 `result.json` 为准。

## 运行示例和验收

~~~bash
python3 -B scripts/selftest.py --demo --out demo-output
python3 -B scripts/selftest.py
~~~

[示例材料](examples/fictional-study.md) 和 [20 条示例断言](examples/example-draft.json) 都是合成材料。示例命令在临时工作区实际运行准备、审查、导出和验证，目标输出是 20 条接受断言和 1 条明确未报告项。可用 `claim_audit.json` 查看无报告项的检索范围。自测覆盖错误证据、单位错配、误抄统计运算符、否定与假说、相邻句、来源混淆、失效快照、输出破损及格式投影。测试文件仅用于可复现验证。

## 方法与边界

脚本对指定锚点匹配原文，验证偏移回取，再用语言线索对数字、极性、语气、方向和归属做定向检查。模型判断主张是否忠实于证据、证据是否足够、实验条件是否需要分开。中英文可以处理，复杂句、跨页表格、脚注和图像证据需要仔细复核。元数据有可追溯的出处，缺失字段保持未知。导出为待复核的本地草稿。

关键方法、规则覆盖和复核路径见 [方法说明](references/review-method.md)。格式依据为 [Nanopublication Guidelines](https://nanopub.net/docs/guidelines/)、[W3C TriG](https://www.w3.org/TR/trig/) 与 [Web Annotation Data Model](https://www.w3.org/TR/annotation-model/)。版本维护时，在改动锚点、词表或契约前先运行 `selftest.py`，再检查合成示例输出。平台成绩依据平台实际案例和对照结果。

## 提交目录与公开链接

本目录就是提交根目录，入口为根路径的 `SKILL.md`。运行脚本、字段说明和明确标注的合成示例随包提供，具体提交范围见 [提交说明](提交说明.md)。

根目录 `GITHUB_URL.txt` 已按题面填写：第一行是本作品的公开目录，第二行是相对本提交根目录的 `SKILL.md`。上传后必须使用未登录 GitHub 的浏览器窗口打开该链接，确认无需登录即可找到本作品及必要资源。
