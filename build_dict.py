import csv
import json
import os
import glob
import re
import logging
import argparse
import unicodedata
from collections import Counter
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ============ 配置 ============
# 默认全量读取 BASE_DIR 下所有 .csv；如果只想读白名单目录，把 USE_WHITELIST 改为 True
USE_WHITELIST = False
WHITELIST_DIRS = ["nhi_excels", "医保目录", "医保"]

COL_ALIASES = {
    "drug_name": ["药品名称（完整通用名）", "药品名称(完整通用名)", "药品名称", "通用名"],
    "dosage_form": ["剂型"],
    "year_direct": ["年份"],                      # 目录年份，仅当执行时间缺失时 fallback
    "exec_time": ["执行时间", "执行日期"],          # 实际执行时间，时间轴优先使用
    "restriction": ["医保限制（备注）", "医保限制(备注)", "医保限制", "备注"],
    "catalog_type": ["目录类型"],
    "drug_class": ["药品分类"],
    "insurance_category": ["医保类别"],
    "price": ["医保支付标准", "支付标准"],
}

EXCLUDED_DIR_PARTS = {"data", "docs", "tests", "venv", ".venv", "__pycache__", ".git"}
NAME_MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "nhi_normalization", "nhi_name_map.json")
SPLITS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "nhi_normalization", "nhi_name_splits.csv")


def _clean_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = (text.replace('&nbsp;', ' ').replace('&amp;', '&')
                .replace('&lt;', '<').replace('&gt;', '>'))
    return re.sub(r'\s+', ' ', text).strip()


def _is_excluded(file: str) -> str:
    """返回排除原因；空字符串表示不排除"""
    name = os.path.basename(file)
    lower = file.lower()
    path_parts = set(Path(file).parts)
    if path_parts & EXCLUDED_DIR_PARTS:
        return "报告/测试/环境目录"
    if 'venv' in lower or '__pycache__' in lower:
        return "虚拟环境/缓存目录"
    if name.startswith('.'):
        return "隐藏文件"
    if name.startswith('nhi_name_'):
        return "名称归一化报告"
    if not name.lower().endswith('.csv'):
        return "非 CSV 文件"
    if USE_WHITELIST:
        hit = any(w.lower() in lower for w in WHITELIST_DIRS)
        if not hit and '医保' not in name:
            return "不在白名单目录且文件名不含'医保'"
    return ""


def read_csv_robustly(file: str):
    """返回 (encoding, header, data_rows)；失败返回 (None, [], [])"""
    last_err = None
    for enc in ('utf-8-sig', 'gb18030', 'gbk', 'utf-8'):
        try:
            with open(file, mode='r', encoding=enc, newline='') as f:
                reader = csv.reader(f)
                rows = list(reader)
            if not rows:
                return enc, [], []
            header = [h.strip().lstrip('\ufeff') for h in rows[0]]
            return enc, header, rows[1:]
        except UnicodeDecodeError as e:
            last_err = e
            continue
        except Exception as e:
            last_err = e
            logger.warning(f"  读取失败({enc}): {e}")
            continue
    logger.error(f"  跳过无法解码/读取的文件: {file} ({last_err})")
    return None, [], []


def build_col_index(header):
    idx = {}
    for key, aliases in COL_ALIASES.items():
        idx[key] = -1
        for alias in aliases:
            if alias in header:
                idx[key] = header.index(alias)
                break
    return idx


def normalize_row(row, header_len):
    """把行补齐/合并到表头长度，返回 (row, warning)"""
    if len(row) == header_len:
        return row, None
    if len(row) > header_len:
        # 多余字段拼到最后一列，尽量不丢信息
        merged = row[:header_len - 1] + [','.join(row[header_len - 1:])]
        return merged, f"字段数 {len(row)} > 表头 {header_len}，多余字段已合并到最后一列"
    return row + [''] * (header_len - len(row)), f"字段数 {len(row)} < 表头 {header_len}，已补空"


def get_val(row, idx):
    if 0 <= idx < len(row):
        v = row[idx]
        return v.strip() if v is not None else ""
    return ""


def normalize_execution_date(raw: str) -> str:
    """把 2023年3月1日 / 2023-03-01 / 2017年 规范成可展示的中文日期。"""
    text = re.sub(r'\s+', '', str(raw or ''))
    m = re.search(r'(20\d{2})年(\d{1,2})月(\d{1,2})?日?', text)
    if m:
        year, month, day = m.group(1), int(m.group(2)), m.group(3)
        return f"{year}年{month}月" + (f"{int(day)}日" if day else "")
    m = re.search(r'(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})', text)
    if m:
        return f"{m.group(1)}年{int(m.group(2))}月{int(m.group(3))}日"
    m = re.search(r'(20\d{2})', text)
    return f"{m.group(1)}年" if m else ""


def parse_execution(row, col_idx):
    """返回 (执行年份, 执行日期, 来源)。执行时间优先，不以文件名/年份列为准。"""
    exec_time = get_val(row, col_idx.get('exec_time', -1))
    exec_date = normalize_execution_date(exec_time)
    if exec_date:
        year = exec_date[:4]
        return year, exec_date, "执行时间"
    # 没有执行时间才退回年份列；这是目录年份，不是文件名，但必须标记来源
    year = get_val(row, col_idx.get('year_direct', -1))
    if re.fullmatch(r'\d{4}', year) and 1900 <= int(year) <= 2099:
        return year, f"{year}年", "年份列"
    return "", "", ""


def discover_files(base_dir):
    files = set()
    files.update(glob.glob(os.path.join(base_dir, "*.csv")))
    files.update(glob.glob(os.path.join(base_dir, "**", "*.csv"), recursive=True))
    valid, skipped = [], []
    for f in sorted(files):
        reason = _is_excluded(f)
        if reason:
            skipped.append((f, reason))
        else:
            valid.append(f)
    return valid, skipped


def normalize_text_key(text: str) -> str:
    """NFKC + 去空白 + 统一括号；只用于 key，不改展示名。"""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = value.replace("（", "(").replace("）", ")")
    return re.sub(r"\s+", "", value)


def normalize_compound_key(name: str, form: str) -> str:
    return f"{normalize_text_key(name)}|{normalize_text_key(form)}"


def normalize_key_string(key: str) -> str:
    name, _sep, form = str(key or "").partition("|")
    return normalize_compound_key(name, form)


def load_name_map(path: str) -> dict:
    if not os.path.exists(path):
        logger.warning(f"未找到名称映射表，将按原始名称建库: {path}")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("根节点不是对象")
        valid = {}
        for old_key, item in data.items():
            if isinstance(item, dict) and item.get("canonical_key"):
                valid[normalize_key_string(old_key)] = item
        logger.info(f"名称映射表加载成功: {len(valid)} 条")
        return valid
    except Exception as e:
        logger.error(f"名称映射表加载失败，按原始名称建库: {e}")
        return {}


def load_split_map(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    result = {}
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                old_key = normalize_key_string(row.get("old_key", ""))
                names = [part.strip() for part in re.split(r"[;；]", row.get("split_names", "")) if part.strip()]
                if old_key and names:
                    result[old_key] = names
    except Exception as e:
        logger.warning(f"拆分报告加载失败: {e}")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=None, help="CSV 所在目录，默认脚本所在目录")
    args = parser.parse_args()
    BASE_DIR = os.path.abspath(args.dir) if args.dir else os.path.dirname(os.path.abspath(__file__))

    all_csv_files, skipped_files = discover_files(BASE_DIR)
    logger.info(f"发现 CSV 文件 {len(all_csv_files) + len(skipped_files)} 个，有效 {len(all_csv_files)} 个，排除 {len(skipped_files)} 个")
    for f, reason in skipped_files:
        logger.info(f"  排除: {os.path.basename(f)} -> {reason}")

    if not all_csv_files:
        logger.error("没有找到任何有效 CSV 文件。")
        return

    name_map = load_name_map(NAME_MAP_PATH)
    split_map = load_split_map(SPLITS_PATH)
    map_aliases = {}
    map_alias_keys = {}
    for old_key, item in name_map.items():
        canonical_key = normalize_key_string(str(item.get("canonical_key") or ""))
        if not canonical_key:
            continue
        canonical_name = canonical_key.split("|", 1)[0]
        aliases = set(str(a).strip() for a in (item.get("aliases") or []) if str(a).strip())
        old_name = old_key.split("|", 1)[0]
        if old_name:
            aliases.add(old_name)
        aliases.discard(canonical_name)
        map_aliases.setdefault(canonical_key, set()).update(aliases)
        map_alias_keys.setdefault(canonical_key, set()).add(normalize_key_string(old_key))

    dict_db = {}
    stats = Counter()
    per_file_stats = {}

    def resolve_key(raw_name: str, raw_form: str):
        raw_key = normalize_compound_key(raw_name, raw_form)
        item = name_map.get(raw_key)
        if not item:
            return raw_key, raw_name, raw_form, [], "未映射"
        canonical_key = normalize_key_string(str(item.get("canonical_key") or raw_key))
        canonical_name, _, canonical_form = canonical_key.partition("|")
        aliases = [str(a) for a in (item.get("aliases") or []) if a]
        if raw_name != canonical_name:
            aliases.append(raw_name)
        return canonical_key, canonical_name, canonical_form, aliases, str(item.get("evidence") or "映射")

    for file in all_csv_files:
        enc, header, data_rows = read_csv_robustly(file)
        if header is None:
            stats["文件读取失败"] += 1
            continue

        col_idx = build_col_index(header)
        if col_idx.get('drug_name', -1) < 0:
            logger.warning(f"  文件缺少药品名称列，跳过: {file}，表头={header}")
            stats["缺少药品名称列的文件"] += 1
            continue

        file_stat = Counter()
        file_stat["总行数"] = len(data_rows)

        for row in data_rows:
            if not any(cell.strip() for cell in row):
                file_stat["空行"] += 1
                continue

            row, warn = normalize_row(row, len(header))
            if warn:
                file_stat["列数异常行"] += 1
                if file_stat["列数异常行"] <= 3:
                    logger.warning(f"  [{os.path.basename(file)}] {warn}")

            drug_name = get_val(row, col_idx['drug_name'])
            if not drug_name:
                file_stat["药品名为空"] += 1
                continue
            drug_name = drug_name.replace('|', '/')

            dosage_form = get_val(row, col_idx.get('dosage_form', -1)) or "未分类"
            dosage_form = dosage_form.replace('|', '/')

            year_str, execution_date, date_source = parse_execution(row, col_idx)
            if not year_str:
                file_stat["执行时间解析失败"] += 1
                continue
            stats[f"时间来源:{date_source}"] += 1

            restriction = _clean_html(get_val(row, col_idx.get('restriction', -1))) or "无限制"
            catalog_type = get_val(row, col_idx.get('catalog_type', -1)) or "基本医保"
            drug_class = get_val(row, col_idx.get('drug_class', -1))
            insurance_category = get_val(row, col_idx.get('insurance_category', -1))
            price = get_val(row, col_idx.get('price', -1))
            catalog_year = get_val(row, col_idx.get('year_direct', -1))

            compound_key, canonical_name, canonical_form, aliases, evidence = resolve_key(drug_name, dosage_form)
            if evidence != "未映射":
                stats["映射行数"] += 1
            if compound_key not in dict_db:
                dict_db[compound_key] = {
                    "generic_name": canonical_name,
                    "dosage_form": canonical_form,
                    "drug_class": drug_class,
                    "insurance_category": insurance_category,
                    "price_medical_insurance": price,
                    "catalog_type": catalog_type,
                    "restriction_timeline": {},
                    "execution_dates": {},
                    "catalog_years": {},
                    "aliases": set(),
                    "alias_keys": set(),
                }
            entry = dict_db[compound_key]
            entry["aliases"].update(aliases)
            entry["aliases"].update(map_aliases.get(compound_key, set()))
            entry["alias_keys"].add(normalize_compound_key(drug_name, dosage_form))
            entry["alias_keys"].update(map_alias_keys.get(compound_key, set()))
            entry["execution_dates"][year_str] = execution_date
            if catalog_year:
                entry["catalog_years"][year_str] = catalog_year

            existing = entry["restriction_timeline"].get(year_str)
            if existing is not None and existing != restriction:
                stats["同执行年限制冲突"] += 1
                entry.setdefault("restriction_conflicts", {}).setdefault(year_str, [existing])
                entry["restriction_conflicts"][year_str].append(restriction)
            entry["restriction_timeline"][year_str] = restriction
            if insurance_category:
                entry["insurance_category"] = insurance_category
            if price and not entry.get("price_medical_insurance"):
                entry["price_medical_insurance"] = price
            if catalog_type == '基本医保':
                entry["catalog_type"] = '基本医保'

        file_stat["有效行数"] = file_stat["总行数"] - file_stat["空行"] - file_stat["药品名为空"] - file_stat["执行时间解析失败"]
        per_file_stats[os.path.basename(file)] = file_stat
        logger.info(f"  {os.path.basename(file)} [{enc}]: 总 {file_stat['总行数']}，有效 {file_stat['有效行数']}，"
                    f"空行 {file_stat['空行']}，药名为空 {file_stat['药品名为空']}，执行时间失败 {file_stat['执行时间解析失败']}，"
                    f"列数异常 {file_stat['列数异常行']}")

    # 复合剂型拆分只做元数据，不自动合并
    for old_key, names in split_map.items():
        if old_key in dict_db:
            dict_db[old_key]["split_to"] = names

    # 推导 latest / first
    for k, info in dict_db.items():
        timeline = info.get("restriction_timeline", {})
        valid_years = sorted([y for y in timeline if y and y.isdigit() and len(y) == 4], key=int)
        execution_dates = info.get("execution_dates", {})
        if valid_years:
            first_year = valid_years[0]
            last_year = valid_years[-1]
            info["latest_restriction"] = timeline[last_year]
            info["first_year"] = first_year
            info["first_execution_date"] = execution_dates.get(first_year, f"{first_year}年")
            info["latest_execution_date"] = execution_dates.get(last_year, f"{last_year}年")
            info["restriction_timeline"] = {y: timeline[y] for y in valid_years}
            info["execution_dates"] = {y: execution_dates.get(y, f"{y}年") for y in valid_years}
            info["catalog_years"] = {
                y: v for y, v in info.get("catalog_years", {}).items() if y in valid_years
            }
        else:
            info["latest_restriction"] = ""
            info["first_year"] = ""
            info["first_execution_date"] = ""
            info["latest_execution_date"] = ""
            info["execution_dates"] = {}
        aliases = info.get("aliases") or set()
        info["aliases"] = sorted(a for a in aliases if a and a != info.get("generic_name"))
        alias_keys = info.get("alias_keys") or set()
        canonical_key = f"{info.get('generic_name', '')}|{info.get('dosage_form', '')}"
        info["alias_keys"] = sorted(k for k in alias_keys if k and k != canonical_key)
        info.pop("restriction_conflicts", None) if not info.get("restriction_conflicts") else None

    OUTPUT_JSON = os.path.join(BASE_DIR, "nhi_dictionary.json")
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(dict_db, f, ensure_ascii=False, indent=2)

    logger.info("=" * 60)
    logger.info(f"建库完成！收录药品(按剂型区分)总数: {len(dict_db)}")
    logger.info(f"文件已生成: {OUTPUT_JSON}")
    if stats:
        logger.info(f"全局异常统计: {dict(stats)}")


if __name__ == "__main__":
    main()