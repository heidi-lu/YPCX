# NHI 药品名称归一化交接说明

> 这是给**新对话/新 agent**的任务说明。只做数据整理，不改代码，不覆盖 `nhi_dictionary.json`。

## 目标

把 2017–2025 年医保目录中同一药品的历史名称合并到同一个规范名，产出可审计的映射表和差异报告。后续由主代码对话负责接入 `build_dict.py` 和 `_match_nhi`。

## 工作区

项目根目录：

```text
/Users/xiaoxu/Desktop/YPCX
```

数据目录（注意名字前面有一个空格）：

```text
" nhi_excels"/
```

输入文件：

```text
" nhi_excels"/2017年国家医保药品目录_2022通用名版.csv
" nhi_excels"/2019年国家医保药品目录_2022通用名版.csv
" nhi_excels"/2020年国家医保药品目录_2022通用名版.csv
" nhi_excels"/2021年国家医保药品目录.csv
" nhi_excels"/2022年国家医保药品目录.csv
" nhi_excels"/2023年国家医保药品目录.csv
" nhi_excels"/2024年国家医保药品目录.csv
" nhi_excels"/2025年国家医保+商保创新药目录.csv
```

只允许读取上述数据、`nhi_dictionary.json`、`medical_aliases.json`。
**不要修改**任何 `.py`、`nhi_dictionary.json`、`medical_aliases.json`。

## 关键事实

- 2017、2019、2020 三个文件是“2022 通用名版”，表头包含：
  - `药品名称（完整通用名）`：已经更新为 2022 规范名
  - `原名（2022通用名更新前）`：更新前的旧名
  - `更新说明`：通常为“已更新为2022通用名”
- 2021–2025 文件没有 `原名` 列，`药品名称（完整通用名）` 视为候选规范名。
- 当前 `nhi_dictionary.json` 用 `药品名|剂型` 作为 key，共 6259 条；历史名没有合并。
- 同一旧名可能对应多个剂型或多个规范名。**必须以 `旧名|剂型` 为映射粒度**，不能只按药名合并。

## 处理顺序

1. **排版规范化**：Unicode NFKC、全角/半角、空格、括号、连字符、`Ⅰ/Ⅱ`、`α2b/α-2b` 等。
2. **显式旧名映射优先**：从 2017/2019/2020 的 `原名（2022通用名更新前）` 建立
   `旧名|剂型 -> 规范名|剂型`。同一旧 key 对应多个规范 key 时，不要猜，放歧义报告。
3. **规范化后精确匹配**：2021–2025 中仅排版不同的名称，归一到相同规范 key。
4. **模糊候选**：上述步骤后仍未匹配的，只做候选，不自动合并；输出到 `nhi_name_unmatched.csv` 供人工复核。
5. **时间轴校验**：同一规范 key 的 `restriction_timeline` 取并集；同一年有多个限制值时，记录冲突，不要静默覆盖。
6. **不要跨剂型合并**；不同盐型、不同规格、复方、不同给药途径默认不合并，除非有明确旧名证据。

## 输出

在项目下新建：

```text
data/nhi_normalization/
  nhi_name_map.json
  nhi_name_report.csv
  nhi_name_unmatched.csv
  README.md
```

### `nhi_name_map.json`

只包含**高置信度、可自动执行**的映射：

```json
{
  "多拉司琼|注射剂": {
    "canonical_name": "甲磺酸多拉司琼注射液",
    "canonical_key": "甲磺酸多拉司琼注射液|注射剂",
    "aliases": ["多拉司琼"],
    "evidence": "原名列",
    "years": ["2017", "2019", "2020"],
    "confidence": 1.0,
    "source_files": ["2017年国家医保药品目录_2022通用名版.csv"]
  }
}
```

字段要求：

- `canonical_name`：规范药品名
- `canonical_key`：`规范名|剂型`
- `aliases`：该规范名下所有已知旧名/排版变体
- `evidence`：`原名列` / `规范化` / `人工确认`
- `years`：证据出现的年份
- `confidence`：0–1
- `source_files`：证据文件

### `nhi_name_report.csv`

列：

```text
old_key,new_key,old_name,new_name,dosage_form,evidence,confidence,years,sources,note
```

### `nhi_name_unmatched.csv`

列：

```text
name,dosage_form,years,reason,candidates
```

`candidates` 用 `|` 分隔，最多 5 个。

### `README.md`

至少写：

- 输入行数、唯一 key 数
- 映射条数、规范 key 数
- 歧义/未匹配条数
- 合并前后估算（6259 -> ?）
- 3–5 个代表性合并例子
- 仍然存疑、需要人工确认的列表

## 已知例子（用于自测）

以下映射必须出现在结果中：

```text
多拉司琼|注射剂 -> 甲磺酸多拉司琼注射液|注射剂
阿替普酶|注射剂 -> 注射用阿替普酶|注射剂
埃克替尼|口服常释剂型 -> 盐酸埃克替尼片|口服常释剂型
艾曲泊帕乙醇胺|口服常释剂型 -> 艾曲泊帕乙醇胺片|口服常释剂型
```

反例（不能合并）：

```text
不同剂型不能合并，例如 片剂 vs 注射剂
不同盐型没有明确旧名证据时不能合并
复方制剂不能合并到单方
```

## 验收标准

- `nhi_name_map.json` 是合法 JSON，key 唯一。
- 每条映射的 `canonical_key` 在至少一个年份的原始数据中出现。
- 不存在同一 `old_key` 映射到多个 `canonical_key`；有歧义的必须在 unmatched 报告中。
- 不修改任何代码、`nhi_dictionary.json` 或 `medical_aliases.json`。
- 报告中的数字可由输入文件复算。
- 运行环境只用 Python 标准库（`csv/json/glob/re/unicodedata/difflib`），不要依赖项目 `venv`。

## 完成后

把 `data/nhi_normalization/` 下的文件提交到当前分支，并在回复中给出：

1. 映射条数 / 未匹配条数
2. 5 个代表性映射
3. 仍然存疑的 top 10
4. 建议主代码如何接入（只写建议，不改代码）
