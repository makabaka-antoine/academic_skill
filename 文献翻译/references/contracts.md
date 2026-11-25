# 输入、对齐与输出契约

## Sources and page evidence

`prepare` 输入本地 PDF、UTF-8 Markdown 或 TXT。每个源单元有不可从译文重造的 `id`、`block_id`、`kind`、`location` 和原始提取文本。PDF 的 `page_origin` 为 `pdf_physical`，从 1 开始，页内行号是**提取文本的行号**。无分页的文本引用源文件真实行号。简单 Markdown 表格按行分块，单元格保留 `column`。长段使用父块和字符切片定位。独立公式、代码、参考文献条目按原文保留。

若宿主已经对原页完成核对，把提取稿写成下列形式后显式传入 `--page-markers`。页锚的来源为 `declared_page`。使用 `--pages` 声明要求覆盖的范围，包括本次无法提供文字的页。

```markdown
<!-- page: 2 -->

Verified text for physical page 2.

<!-- page: 3 -->
```

可选 `--transcriptions corrections.json` 接收已由宿主或人工对原页核过的改动。提供修订后的文本和一个 JSON 数组。脚本确认 `after` 出现在指定页，保留原始读数和证据来源。

```json
[
  {"page": 2, "before": "2.l%", "after": "2.1%", "evidence": "供稿 PDF 物理第 2 页，图 1 下方数值"}
]
```

如文字层遗漏了题目要的图片、表头、公式或序号，在复审文件里记录 `source_issues`。局部任务可直接注明页内小节、图表、字段与最短必要的证据。未经视觉确认的转换应依据原页可读性报告。

## Glossary and occurrence registration

词表由 `terms` 和可选 `exceptions` 组成。每项有唯一的 `T0003` 形 ID、`source_forms`、规范 `target` 和可核查的 `basis`。`selection_status` 支持 `auto_proposed`、`author_confirmed`、`disputed`。确认状态应对应实际作者决定。歧义词留源词并附理由。

```json
{
  "terms": [
    {
      "id": "T0003",
      "source_forms": ["error rate", "error rates"],
      "target": "误差率",
      "allowed_targets": [],
      "selection_status": "auto_proposed",
      "basis": "物理第 2 页 Results 段将其用作结果指标"
    }
  ],
  "exceptions": []
}
```

`unit_ids` 可指定一个概念的适用单元，处理同形异义，`case_sensitive` 可保护大小写有实质区别的词。`allowed_targets` 只列有实际依据的准用形式。复用词表时，按新材料重新核对词义与区域，尤其是依赖段号的例外。

每个匹配位置都有独立令牌和注册行，例如 `⟦TERM_T0003_U00001_O002⟧`，另有 `NUM`、`MATH`、`CITE`、`LABEL` 和 `LIT` 命名空间。注册行记录原文、目标形式、单元、源句、原文字符范围、匹配次数、恢复状态。不存在于本次源单元的令牌将被报告。保护字符的控制语法 `⟦…⟧` 用在请求和响应临时文件里，输入使用未遮蔽的原文。

单次例外使用确切的 `term_id`、`sentence_id`、`occurrence` 和 `basis`。`occurrence` 为该词在该源句中的 1 基次序。原因只能是 `ambiguous_keep`、`authorized_omission`、`approved_expansion` 或 `not_a_term`。后两项同时给出依据明确的 `target`，受控省略的令牌仍必须返回以便计数，回填时字面为空。整个单元保留由明确的用户要求或源对象类型决定。未译、丢令牌、缺文本层与合法保留分别处理。

## Agent responses

阅读每个请求中的 `sentences[].text`、`sentences[].masked`、`registry`、`context`。`context` 是原文邻段，提供指代和术语语境。只写当前待译单元的中文。每个响应文件是 `{"units": [...]}`。使用请求实际给出的 ID、指纹和令牌。以下为格式示意。

```json
{
  "units": [
    {
      "unit_id": "d01:p0002-b0001",
      "input_sha256": "复制请求中的完整 SHA-256",
      "sentences": [
        {
          "source_sentence_ids": ["d01:p0002-b0001.s001"],
          "text": "A 组的⟦TERM_T0003_U00001_O001⟧为 ⟦NUM_U00001_O001⟧。"
        }
      ]
    }
  ]
}
```

一个源句对应多个译句时，每个译句都带它的源句 ID。多句合译时，列出该译句覆盖的全部原句。全部源句均须可回查。针对实际准用词形，可在单元内增加 `term_forms` 对象，键为确切术语令牌，值为该令牌允许的译名。数字、LaTeX、引用与编号用注册令牌返回。带组别标签的句子保留原标识及数值的对应，合法调整表述顺序。交稿检查会解开遮蔽。返修在同一个响应文件中更新该 ID，重复交稿会触发重复单元检查。

## Semantic review

第一次 `check` 后，逐段核对原文与已回填译文，额外检查主语、对象、否定、范围、条件、时点、比较基线、指标与组别绑定。使用 `alignment.jsonl` 的真实 `review_sha256` 写 `reviews.json`。该指纹包含已检查译文和该段输入。需要复查时更新实际检查记录。只有确实读过且无发现的段才能填空数组。

```json
{
  "method": "model_review",
  "reviews": [
    {
      "unit_id": "d01:p0002-b0001",
      "review_sha256": "复制对齐记录中的完整 SHA-256",
      "findings": []
    }
  ],
  "source_issues": []
}
```

有问题的 `findings` 行填写 `type`、`severity`（`major` 或 `minor`）、`source_sentence_ids`、`source_excerpt`、`target_excerpt`、`detail`。引用原有片段。漏译时没有目标片段可以给空字符串。`source_issues` 行用 `code`、`location`、`detail`，类型为 `visual_unreadable`、`required_material_missing`、`layout_uncertain` 或 `source_conflict`。`human_review` 仅记录实际人工复审，`fixture_review` 专用于合成示例。脚本校验复审记录与译稿的关联指纹，复审主体由写入者据实注明。

## Scoped evidence tasks

当题目只问某项事实、提取字段、结构化材料或核验既有译段时，直接给对应结果和证据，不必运行 `plan`。可使用下列结构，或遵循题目要求的输出格式。

```json
{
  "input": {"case_id": "题目给定的标识", "materials": ["材料标识"], "scope": "实际读到的页、段或字段"},
  "status": "success",
  "main_result": [
    {"item": "所问字段或结论", "value": "可核验内容", "evidence_status": "supported", "evidence": ["材料及页、段、图表或字段，必要时附原文短语"]}
  ],
  "uncertainties": []
}
```

对实际查阅范围内的未报告项用 `not_reported`。有原文冲突时保留双方出处，用 `conflict`。缺失材料用 `material_missing`，不可读或无法判断用 `unresolved` 并给出原因与范围。若只有部分内容可交付，则用 `partial`。核心证据不可得则用 `blocked`，工具异常用 `failed`。受题目限制确需拒答时在结果中写明原因和可用的其他证据。
