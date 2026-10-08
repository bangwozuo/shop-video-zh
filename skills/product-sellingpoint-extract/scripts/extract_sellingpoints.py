# -*- coding: utf-8 -*-
"""
商品卖点提炼 —— 确定性分组统计脚本。

职责边界（重要）：
  本脚本只做**确定性计算与产物生成**：条目拆分编号、按信息来源（参数/用户评价/
  卖点页文案/客服记录/其他）规则化分组、频次统计、关键词命中统计、CSV/JSON/MD 落盘。
  跨来源的语义归并、卖点主题命名、洞察撰写，由模型按 prompt.txt 完成。

量化规则（与 prompt.txt 一致）：
  - 来源分组：按「参数：/用户评价：/卖点页文案：/客服记录：」前缀归类，未匹配进「其他」
  - 高频主题：某来源条目数 ≥ 3 才允许作为独立卖点主题
  - 关键词命中：输入可附 keywords 列表，脚本统计每个关键词命中的条目编号
  - 证据链：每条统计结果保留原始条目编号，禁止无编号结论

用法：
  python extract_sellingpoints.py --input input.json --outdir out
  python extract_sellingpoints.py --demo --outdir out

产物：
  out/卖点分组统计.csv   分组/条目编号/频次
  out/sellingpoints.json 机器可读结果（供工作流下游读取）
  out/卖点提炼摘要.md    量化摘要（主题命名与洞察由模型补写）
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR_DEFAULT = os.path.normpath(os.path.join(HERE, "..", "out"))

SOURCE_PATS = [
    ("参数", re.compile(r"^\s*(参数|规格)")),
    ("用户评价", re.compile(r"^\s*(用户评价|评价|买家评价)")),
    ("卖点页文案", re.compile(r"^\s*(卖点页文案|文案|卖点)")),
    ("客服记录", re.compile(r"^\s*(客服记录|客服|咨询)")),
    ("商品标题", re.compile(r"^\s*(商品标题|标题)")),
]
MIN_GROUP_N = 3  # 高频主题门槛：来源条目数 ≥ 3

# 演示数据：与 runs_v2/shop-video-zh/skills/product-sellingpoint-extract 实跑同源
DEMO = {
    "product": "便携挂脖风扇 FS-02 白色",
    "items": "商品：便携挂脖风扇 FS-02 白色。原始条目：\n"
             "1. 商品标题：便携挂脖风扇 FS-02 白色\n"
             "2. 参数：内置 4000mAh 锂电池，满电续航 4-8 小时\n"
             "3. 参数：3 档风力调节，涡轮无叶出风\n"
             "4. 参数：整机重量 265g，挂脖处硅胶包覆\n"
             "5. 参数：Type-C 充电口，2.5 小时充满\n"
             "6. 用户评价：「上下班地铁里用很方便，不用手持」\n"
             "7. 用户评价：「风扇声音不大，办公室开着同事不投诉」\n"
             "8. 用户评价：「脖子那里是软的，戴久了不勒」\n"
             "9. 用户评价：「续航没说的那么久，3 档风大概只能用 4 小时」\n"
             "10. 卖点页文案：解放双手，通勤路上随戴随走\n"
             "11. 卖点页文案：无叶设计，长发不怕卷入\n"
             "12. 卖点页文案：静音电机，图书馆也能用\n"
             "13. 客服记录：被问最多的 3 个问题是续航多久、用什么充电线、会不会夹头发",
    "keywords": ["续航", "静音", "无叶", "重量", "充电"],
}


def split_items(raw: str):
    """把自由文本按「N. 内容」编号行拆成条目，保留编号。"""
    items = []
    for line in (raw or "").splitlines():
        m = re.match(r"^\s*(\d+)\.\s*(.+)$", line.strip())
        if m:
            items.append({"no": int(m.group(1)), "text": m.group(2).strip()})
    if not items:  # 兜底：整段作为单条
        items = [{"no": 1, "text": (raw or "").strip()}]
    return items


def classify(text: str) -> str:
    for name, pat in SOURCE_PATS:
        if pat.match(text):
            return name
    return "其他"


def compute(payload):
    raw = payload.get("items") or ""
    items = split_items(raw)
    keywords = payload.get("keywords") or []

    groups = {}
    for it in items:
        src = classify(it["text"])
        groups.setdefault(src, []).append(it["no"])

    kw_hits = {}
    for kw in keywords:
        kw_hits[kw] = [it["no"] for it in items if kw in it["text"]]

    group_rows = []
    for src, nos in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        group_rows.append({
            "source": src,
            "count": len(nos),
            "item_nos": nos,
            "is_major_theme": len(nos) >= MIN_GROUP_N,
        })

    return {
        "product": payload.get("product", ""),
        "n_items": len(items),
        "n_sources": len(groups),
        "min_group_n": MIN_GROUP_N,
        "group_rows": group_rows,
        "keyword_hits": [{"keyword": k, "hits": v, "hit_count": len(v)} for k, v in kw_hits.items()],
        "items": items,
    }


def write_outputs(result, outdir):
    os.makedirs(outdir, exist_ok=True)
    files = []

    csv_path = os.path.join(outdir, "卖点分组统计.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["信息来源", "条目数", "条目编号", "是否高频主题(≥3条)"])
        for r in result["group_rows"]:
            w.writerow([r["source"], r["count"], "、".join(map(str, r["item_nos"])),
                        "是" if r["is_major_theme"] else "否"])
    files.append(csv_path)

    json_path = os.path.join(outdir, "sellingpoints.json")
    payload = {**result,
               "note": "分组与频次均由本脚本规则化统计；跨来源语义归并、主题命名与洞察由模型按 prompt.txt 完成"}
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    files.append(json_path)

    md_path = os.path.join(outdir, "卖点提炼摘要.md")
    lines = [
        f"# 卖点提炼量化摘要（脚本产物）" + (f" —— {result['product']}" if result["product"] else ""),
        "",
        f"- 共解析 {result['n_items']} 条原始条目，覆盖 {result['n_sources']} 类信息来源",
        f"- 高频主题门槛：单来源条目数 ≥ {MIN_GROUP_N} 条",
        "",
        "## 来源分组",
        "",
        "| 信息来源 | 条目数 | 条目编号 | 高频主题 |",
        "|---|---|---|---|",
    ]
    for r in result["group_rows"]:
        lines.append(f"| {r['source']} | {r['count']} | {'、'.join(map(str, r['item_nos']))} "
                     f"| {'是' if r['is_major_theme'] else '否'} |")
    if result["keyword_hits"]:
        lines += ["", "## 关键词命中", "",
                  "| 关键词 | 命中条目数 | 条目编号 |", "|---|---|---|"]
        for k in result["keyword_hits"]:
            lines.append(f"| {k['keyword']} | {k['hit_count']} "
                         f"| {'、'.join(map(str, k['hits'])) or '—'} |")
    lines += ["", "> 跨来源语义归并、卖点主题命名与共性洞察：由模型按 prompt.txt 在本摘要基础上完成。"]
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    files.append(md_path)
    return files


def main():
    ap = argparse.ArgumentParser(description="商品卖点提炼（确定性分组统计）")
    ap.add_argument("--input", help="输入 JSON（items 文本；可选 product / keywords）")
    ap.add_argument("--outdir", default=OUTDIR_DEFAULT, help="输出目录")
    ap.add_argument("--demo", action="store_true", help="用内置演示数据运行")
    a = ap.parse_args()

    if a.demo:
        payload = DEMO
    elif a.input:
        with open(a.input, encoding="utf-8") as f:
            payload = json.load(f)
    else:
        ap.error("需提供 --input 或 --demo")

    if not (payload.get("items") or "").strip():
        print("[错误] items 为空：没有条目就不做分组，不编造卖点。", file=sys.stderr)
        sys.exit(3)

    result = compute(payload)
    files = write_outputs(result, a.outdir)
    majors = [r["source"] for r in result["group_rows"] if r["is_major_theme"]]
    print(f"卖点分组完成 —— {result['n_items']} 条条目 / {result['n_sources']} 类来源，"
          f"高频主题 {len(majors)} 个（{'、'.join(majors) or '无'}）")
    for f in files:
        print(" 产物:", f, f"({os.path.getsize(f) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
