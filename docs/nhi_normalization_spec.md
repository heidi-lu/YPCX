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

文件状态：截图里部分 CSV 带云端感叹号。当前机器上 8 个文件都能读到，大小约 350–492 KB；
如果新会话读不到或读到占位内容，先让用户在 Finder 里右键“始终保留在此设备上”/下载，
或把 8 个文件复制到一个不做云同步的本地目录再跑。

## 关键事实

- 2017、2019、2020 三个文件是“2022 通用名版”，但**只更新了一部分**：
  - 表头有 `药品名称（完整通用名）`、`原名（2022通用名更新前）`、`更新说明`
  - 实测 `原名` 非空行数：2017 年 22 行、2019 年 77 行、2020 年 135 行；
    大部分行仍为空，**不能把空 `原名` 当成“已经是规范名”**。
- 2021–2025 文件没有 `原名` 列，`药品名称（完整通用名）` 只能作为候选规范名。
- 当前 `nhi_dictionary.json` 用 `药品名|剂型` 作为 key，共 6259 条；历史名没有合并。
- 实测 2017–2020 中有 **1184 个药名**在 2022–2025 中完全没出现；其中只有 1 个能在别处的 `原名` 列找到显式映射。其余大量是：
  - 真正的改名
  - 全角/半角、括号、连字符、`α2b/α-2b` 等排版差异
  - 旧目录的“合并剂型名”，例如 `一清片（胶囊、颗粒）`、`三七片（胶囊）`
  - 一个旧名对应多个新剂型/新规格（一对多）
- 因此不能只靠 `原名` 列，也不能对 1184 个名字一律自动合并。**一个旧 key 只能自动映射到一个新 key；一对多、复合剂型、低置信度全部进候选/歧义报告。**
- 同一旧名可能对应多个剂型或多个规范名。**必须以 `旧名|剂型` 为映射粒度**，不能只按药名合并。

## 处理顺序

1. **排版规范化**：Unicode NFKC、全角/半角、空格、括号、连字符、`Ⅰ/Ⅱ`、`α2b/α-2b` 等。
2. **显式旧名映射优先**：从 2017/2019/2020 的 `原名（2022通用名更新前）` 建立
   `旧名|剂型 -> 规范名|剂型`。这是最高置信度证据。
3. **规范化后精确匹配**：全角/半角、空格、括号、连字符、`Ⅰ/Ⅱ`、`α2b/α-2b` 等只做排版统一的，归一到相同 key。
4. **跨年份聚类**：对 2017–2020 中 `原名` 为空的名称，先看它是否在 2021–2025 中以同名/规范化同名出现；
   出现则视为候选规范名。不要在未匹配时直接拿 2022 的名字硬套。
5. **复合剂型拆分**：名称里带 `（胶囊、颗粒）`、`片（胶囊）` 这类“一个名字覆盖多个剂型”的，
   单独输出到 `nhi_name_splits.csv`，不要自动合并成单条。
6. **模糊候选**：上述步骤后仍未匹配的，用同剂型 `difflib` 相似度做候选，**不自动合并**；
   输出到 `nhi_name_unmatched.csv` / `nhi_name_candidates.csv` 供人工复核。
7. **时间轴校验**：同一规范 key 的 `restriction_timeline` 取并集；同一年有多个限制值时，记录冲突，不要静默覆盖。
8. **不要跨剂型合并**；不同盐型、不同规格、复方、不同给药途径默认不合并，除非有明确旧名证据。

## 输出

在项目下新建：

```text
data/nhi_normalization/
  nhi_name_map.json
  nhi_name_report.csv
  nhi_name_unmatched.csv
  nhi_name_candidates.csv
  nhi_name_splits.csv
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

### `nhi_name_candidates.csv`

列：

```text
old_key,old_name,dosage_form,years,best_candidate_key,similarity,other_candidates,note
```

只放中等置信度候选，方便人工批量确认；不要直接写进 `nhi_name_map.json`。

### `nhi_name_splits.csv`

列：

```text
old_key,old_name,dosage_form,years,split_names,reason
```

处理 `一清片（胶囊、颗粒）` 这类一个旧名覆盖多个剂型/规格的情况。

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
- 不存在同一 `old_key` 映射到多个 `canonical_key`；有歧义/一对多的必须进 candidates 或 splits 报告。
- `nhi_name_map.json` 只放一对一的自动映射；`原名` 为空的 1184 个名字不得被静默批量合并。
- 不修改任何代码、`nhi_dictionary.json` 或 `medical_aliases.json`。
- 报告中的数字可由输入文件复算。
- 运行环境只用 Python 标准库（`csv/json/glob/re/unicodedata/difflib`），不要依赖项目 `venv`。
- 文件在 Desktop，可能被 iCloud/OneDrive 挂起；先确认 8 个 CSV 都能实际读取（`os.path.getsize` + 读第一行），
  不要用 0 字节/占位文件继续跑。

## 完成后

把 `data/nhi_normalization/` 下的文件提交到当前分支，并在回复中给出：

1. 映射条数 / 未匹配条数
2. 5 个代表性映射
3. 仍然存疑的 top 10
4. 建议主代码如何接入（只写建议，不改代码）
