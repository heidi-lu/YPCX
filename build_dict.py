import csv
import json
import os
import glob
import re
import logging
import argparse
from collections import Counter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ============ 配置 ============
# 默认全量读取 BASE_DIR 下所有 .csv；如果只想读白名单目录，把 USE_WHITELIST 改为 True
USE_WHITELIST = False
WHITELIST_DIRS = ["nhi_excels", "医保目录", "医保"]

COL_ALIASES = {
    "drug_name": ["药品名称（完整通用名）", "药品名称(完整通用名)", "药品名称", "通用名"],
    "dosage_form": ["剂型"],
    "year_direct": ["年份"],                      # 目录年份，优先使用
    "exec_time": ["执行时间", "执行日期"],          # 执行时间，作为年份 fallback
    "restriction": ["医保限制（备注）", "医保限制(备注)", "医保限制", "备注"],
    "catalog_type": ["目录类型"],
    "drug_class": ["药品分类"],
    "insurance_category": ["医保类别"],
    "price": ["医保支付标准", "支付标准"],
}


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
    if 'venv' in lower or '__pycache__' in lower:
        return "虚拟环境/缓存目录"
    if name.startswith('.'):
        return "隐藏文件"
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


def parse_year(row, col_idx):
    # 1) 优先目录年份列
    y = get_val(row, col_idx.get('year_direct', -1))
    if re.fullmatch(r'\d{4}', y) and 1900 <= int(y) <= 2099:
        return y
    # 2) 从执行时间提取
    exec_time = get_val(row, col_idx.get('exec_time', -1))
    m = re.search(r'(19\d{2}|20\d{2})', exec_time)
    if m:
        return m.group(1)
    return ""


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

    dict_db = {}
    stats = Counter()
    per_file_stats = {}

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

            year_str = parse_year(row, col_idx)
            if not year_str:
                file_stat["年份解析失败"] += 1
                continue

            restriction = _clean_html(get_val(row, col_idx.get('restriction', -1))) or "无限制"
            catalog_type = get_val(row, col_idx.get('catalog_type', -1)) or "基本医保"
            drug_class = get_val(row, col_idx.get('drug_class', -1))
            insurance_category = get_val(row, col_idx.get('insurance_category', -1))
            price = get_val(row, col_idx.get('price', -1))

            compound_key = f"{drug_name}|{dosage_form}"
            if compound_key not in dict_db:
                dict_db[compound_key] = {
                    "generic_name": drug_name,
                    "dosage_form": dosage_form,
                    "drug_class": drug_class,
                    "insurance_category": insurance_category,
                    "price_medical_insurance": price,
                    "catalog_type": catalog_type,
                    "restriction_timeline": {},
                }
            entry = dict_db[compound_key]
            entry["restriction_timeline"][year_str] = restriction
            if insurance_category:
                entry["insurance_category"] = insurance_category
            if price and not entry.get("price_medical_insurance"):
                entry["price_medical_insurance"] = price
            if catalog_type == '基本医保':
                entry["catalog_type"] = '基本医保'

        file_stat["有效行数"] = file_stat["总行数"] - file_stat["空行"] - file_stat["药品名为空"] - file_stat["年份解析失败"]
        per_file_stats[os.path.basename(file)] = file_stat
        logger.info(f"  {os.path.basename(file)} [{enc}]: 总 {file_stat['总行数']}，有效 {file_stat['有效行数']}，"
                    f"空行 {file_stat['空行']}，药名为空 {file_stat['药品名为空']}，年份失败 {file_stat['年份解析失败']}，"
                    f"列数异常 {file_stat['列数异常行']}")

    # 推导 latest / first
    for k, info in dict_db.items():
        timeline = info.get("restriction_timeline", {})
        valid_years = sorted([y for y in timeline if y and y.isdigit() and len(y) == 4], key=int)
        if valid_years:
            info["latest_restriction"] = timeline[valid_years[-1]]
            info["first_year"] = valid_years[0]
            info["restriction_timeline"] = {y: timeline[y] for y in valid_years}
        else:
            info["latest_restriction"] = ""
            info["first_year"] = ""

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