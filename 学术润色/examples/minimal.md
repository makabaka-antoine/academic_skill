# 最小调用示例

以下材料完全为演示合成。语句和输出均为**示例**。

## 输入与调用

提供 [原稿](source.md) 与 [术语表](../assets/glossary.example.tsv)，调用 `$academic-language-polish`。

> 润色 Results。保留观察性设计、试点范围、所有数字及图表和引用指向。统一已批准的术语 `follow-up response rate`。交付英文改稿及逐句修改说明。

## 示意输出

```markdown
# Results

In the pilot cohort (n = 32), the follow-up response rate was 4 percentage points higher under the revised schedule than under the standard reminder schedule (Table 2) [3].
The observational analysis does not establish a causal effect.
```

| ID | 原稿位置 | 原句 | 改句 | 类型 | 理由 |
|---|---|---|---|---|---|
| E01 | `source.md` 第 3 行 | In the pilot cohort (n = 32), the followup response rate were 4 percentage points higher under the revised schedule than under the standard reminder schedule (Table 2) [3]. | In the pilot cohort (n = 32), the follow-up response rate was 4 percentage points higher under the revised schedule than under the standard reminder schedule (Table 2) [3]. | 术语、语法 | 应用已批准的连字符写法，单数主语 `rate` 与 `was` 一致。 |
| E02 | `source.md` 第 4 行 | The observational analysis do not establish a causal effect. | The observational analysis does not establish a causal effect. | 语法 | 单数主语 `analysis` 与 `does` 一致，否定和因果限定保留。 |

**事实定位**　样本量、比较组、4 个百分点及 `Table 2` 和 `[3]` 均来自原稿第 3 行，因果限定来自第 4 行。表格与引文来源在这个演示中未提供，原指向保留为原稿所载。改稿覆盖了要求的两句。

## 自查

把示意输出的正文保存成 UTF-8 `example-revised.md`。在 Skill 根目录运行下面的检查，查看保真锚点和术语。

```sh
python3 scripts/edit_audit.py check examples/source.md example-revised.md --glossary assets/glossary.example.tsv --language en
```

对照原句和改句查 `revised schedule`、`standard reminder schedule` 的方向及 `does not establish` 的限定。把 `4 percentage points` 改成 `4%` 再运行，锚点比较应产生差异。
