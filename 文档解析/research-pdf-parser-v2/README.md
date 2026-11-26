# 科研 PDF 解析与原文核验

版本 2.0.0。面向科研 PDF 的多栏读序与表格证据定位，可为翻译、抽取、核验和问答提供原文依据。Agent 用 [SKILL.md](SKILL.md) 执行题目，脚本可离线独立运行。题号 `product-arena-08-3027d350`。正式输入由评测平台挂载。

## 安装与调用

使用 Python 3.10 或更新版本。先检查平台预装环境和实际的 `environment/resources.yaml`。本地复现可按下方命令安装已验证的 Python 依赖。PyMuPDF 的来源、版本与 AGPL/商业双许可说明见项目页面。jsonschema 的来源和许可见项目页面。仅在当地环境需要时安装可选系统程序。

```sh
python3 -m pip install -r requirements.txt
python3 scripts/pdf.py doctor
python3 scripts/case.py inspect --help
```

- 主路径用 [PyMuPDF](https://pypi.org/project/PyMuPDF/) 产生可见页坐标、文字跨度和框线表格候选。在 Python 3.12.14 和 PyMuPDF 1.26.6 下核对调用。
- 结果使用观察到的 JSON Schema 时，使用 [jsonschema](https://pypi.org/project/jsonschema/) 4.26.0。字段清单或文字契约可在阅读并确认后用 `--reviewed-contract` 完成发布。
- 缺少 PyMuPDF 时可用 [Poppler](https://poppler.freedesktop.org/) 的 `pdfinfo` 与 `pdftotext` 索引，`pdftoppm` 渲染。用 `doctor` 确认路径。该路径以词坐标重建空格，无字体与上标跨度，表格转为区域读图与手工结构复核。
- OCR 需要 [Tesseract](https://tesseract-ocr.github.io/tessdoc/Installation.html) 及目标语言训练数据。先用 `tesseract --list-langs` 确认实际语言，按需要选 `eng`、`chi_sim`、`fra` 等已安装项。系统环境和字体由安装方或平台提供。

Harbor 作者交付目录的根为 `/app/submission`，执行时用实际安装目录 `/skills/weekly-skill`。工作目录为 `/app/work`，正式结果为 `/app/output/result.json`。独立运行时，`inspect` 可传 `--case`、`--task`。`check` 和 `publish` 可传 `--case`、`--draft`、`--output-dir`。只使用本次任务允许的真实材料。

| 操作 | 用法 |
| --- | --- |
| 检查输入 | `case.py inspect --case INPUT/case.json --task INPUT/task.md` |
| 建页索引 | `pdf.py index INPUT.pdf --pages 2-4,7 --out WORK/index`。用新目录。允许全篇时用 `--pages all` |
| 浏览与定位 | `pdf.py read WORK/index --pages 2-4 --regex 'Table\|Results' --context 2`。正则的备选写法用普通竖线。可加 `--bbox`，显示上限默认 110 行 |
| 渲染原图 | `pdf.py render INPUT.pdf --page 2 --dpi 300 --out WORK/p2.png`。可选 `--bbox 36,100,330,610` |
| 候选表格 | `pdf.py tables INPUT.pdf --page 2 --out WORK/t2.json`。有框线默认 `lines_strict`。可用 `lines` 或经原图确认区域后用 `--bbox ... --strategy text` |
| 区域 OCR | `pdf.py ocr INPUT.pdf --page 2 --bbox 36,100,330,610 --lang eng --out WORK/o2.json`。默认用 `--psm 6`，可按布局改为 `3` 或 `11`。默认限时 25 秒 |
| 检查和发布 | `case.py check`、`case.py publish`。可重复传 `--artifact 文件相对路径` 核查要求交付的文件实际存在且非空 |

表中脚本均用 `python3 scripts/脚本名` 调用。页面坐标按显示页的 CropBox，左上原点，四值顺序为 `x0,y0,x1,y1`，单位 pt。索引中的 `printed_label` 是 PDF 元数据提供的页标签。纸面页号须在原图核对。密码通过 `--password-env` 指定环境变量名，由 PyMuPDF 打开密码受保护的 PDF。

## 方法及复核

1. **按列读，又保留原行。** 逐页记录原生行与跨度、框和样式，在纵向有并行文字支持的位置找栏间空白。通栏内容切开局部读序，区段内依次读各列。原行 ID 在重排后仍可追溯。字体标志、原始跨度和组号供复核。公式密集区、短栏、边注、右到左或竖排文字、跨栏插图均需对照原图。原文中的断词、段落与标题层级由核验后决定。相邻重复边缘文字标为页眉页脚候选，保留原行。
2. **单元格按物理框归属。** 从绘制线和相交边界生成候选，提取单元格内容，按边界推得行列及跨行跨列。JSON 保留 `cells` 与 `slot_owners`。一个 ID 占多格表示合并候选，空占位列表表示暂无归属，多个 ID 表示冲突。原图确定空白、破折号及空值语义。对无框线与截图表格，区域内文字对齐或 OCR 供定位和交叉核对。跨页关系需核对题号、列头/单位、行序和页面，页片段全部到位时才计为一张完整逻辑表。
3. **证据跟着结论走。** `manifest.json` 记录源路径和 SHA-256、实际读取物理页、警告、耗时及失败页。`pNNNN.json` 保存有序行、可见页尺寸、跨度和图像候选区域。随主结果写真实页、行 ID 或原页框、图号或行列。扫描件以原页或区域图校验 OCR，任何补录均保留前值、后值和理由。跨材料核验时逐份引用。表格与 OCR 标记为候选，`indexed` 仅说明建索引成功。
4. **证据校验与契约校验各有入口。** 回读原文和原图核对支持关系，复核未报告项的查阅范围。`case.py` 检查原始 `case_id`、显式 JSON Schema 或字段清单、以及指定成品文件是否存在。对显式封闭的顶层结构，若仅漏写赛事要求的 `case_id` 定义，脚本检查声明字段并在最终结果保留该 ID。遇机器无法解析的契约时阅读原约定，逐条核查结构与内容。`check` 只执行检查，`publish` 原子写入。结构校验失败时保留上次成果。

实用检查顺序是匹配文件和任务范围，检查未索引页，定位关键行、表格和图像，逐项核对证据后检查 JSON 和产物。查不到表格候选时查看实际页图。索引 `partial` 时先保存已核实部分，再按失败页重试。OCR 或图像复核不可用时清楚注明受影响位置。生成 editable 工件后用评测环境可用的真实渲染器导出预览，并打开检查。源文件变化后重新建立索引。

`case.py check` 和 `publish` 可传 `--source-checks WORK/source-checks.json`。该文件使用 JSON 数组，每项核对一组已经写入答案的源位置。`source` 相对 `case.json` 的目录，`index` 与 `evidence_data` 相对核对文件的目录，均可使用绝对路径。

- 原生行填写 `source`、`physical_page`、`index`、`line_ids`。按原图注明区块时可用 `bbox` 代替行 ID。
- 表格及 OCR 填写 `source`、`physical_page`、`evidence_data`（当次生成的表格或 OCR JSON）与 `cell_ids` 或 `word_ids`。对原页裁图可把 `render` 的 JSON 回显重定向到 `evidence_data`，再给 `bbox`。
- 其他附件填写 `source`、该原文件实际的 `sha256` 和可复核的 `locator`。未找到或仅推测的证据留在结果的不确定性记录中。
- 结果中已有同名结构化证据时，可加 `result_pointer`，例如 `/evidence/0`。校验器同时核对实际结果中该条证据的来源、物理页及对应的坐标、定位或 ID。单个 ID 可用 `line_id`、`cell_id` 或 `word_id`。

源校验检查文件存在、当前哈希和被引用的页内记录。引文核对和论证支持关系需要对照原文。

## 最小调用示例

以下为接口示例。运行时生成一个自制单页 PDF、一个示例契约和已核实的示例结果。正式案例的字段和证据由当次输入决定。命令在包含 `SKILL.md` 的目录下执行。

```sh
D=$(mktemp -d)
mkdir -p "$D/input" "$D/work"
python3 - "$D/input" <<'PY'
from pathlib import Path
import sys
import pymupdf

root = Path(sys.argv[1])
with pymupdf.open() as pdf:
    pdf.new_page().insert_text((72, 80), 'Run label: DEMO-A')
    pdf.save(root / 'note.pdf')
(root / 'task.md').write_text('Report the exact run label on physical page 1.', encoding='utf-8')
(root / 'case.json').write_text('''{"case_id":"demo-only","material":"note.pdf",
  "output_contract":{"required_fields":["status","run_label","evidence","uncertainties","changes"]}}''',
  encoding='utf-8')
PY
python3 scripts/case.py inspect --case "$D/input/case.json" --task "$D/input/task.md"
python3 scripts/pdf.py index "$D/input/note.pdf" --pages 1 --out "$D/work/index"
python3 scripts/pdf.py read "$D/work/index" --pages 1
```

预期读到 `p0001-l0001` 的 `Run label: DEMO-A`。核对读到的结果后填写以下示例草稿并校验发布。

```sh
cat > "$D/work/draft.json" <<'JSON'
{"status":"success","run_label":"DEMO-A","evidence":[{"source":"note.pdf","physical_page":1,"line_id":"p0001-l0001","quote":"Run label: DEMO-A"}],"uncertainties":[],"changes":[]}
JSON
python3 scripts/case.py publish --case "$D/input/case.json" --draft "$D/work/draft.json" \
  --output-dir "$D/output" --reviewed-contract
```

示例 `result.json` 包含草稿的全部字段及从案例读取的 `"case_id": "demo-only"`。`publish` 显示 `written: true` 和实际路径。正式提交目录的用途是交付本 Skill 及所需资源。
