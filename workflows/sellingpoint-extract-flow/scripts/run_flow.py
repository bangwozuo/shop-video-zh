# -*- coding: utf-8 -*-
"""
商品卖点提取工作流 —— 端到端编排脚本。

流程（与 SKILL.md 的 DAG 一致）：
  S1 商品卖点提炼（原子技能，模型产出）：原始条目 → 语义归并卖点清单
  S2 本步（脚本承担）：来源分组 / 频次统计 / 关键词命中 / 证据链确定性核算
  S3 本步（脚本承担）：交付物组装（卖点清单骨架 + 校验表 + 机器可读结果）

本脚本承担确定性部分：
  - 条目拆分编号（「N. 来源：内容」规则解析）
  - 来源分组（参数/用户评价/卖点页文案/客服记录/商品标题/其他）
  - 高频主题判定（单来源条目数 ≥ 3）
  - 关键词命中统计与 CSV/MD/JSON 落盘
  语义归并、主题命名、洞察撰写由模型按 prompt.txt 的 S1 完成。

用法：
  python run_flow.py --input input.json --outdir out
  python run_flow.py --demo

产物（每步读上一步产物，逐步落盘）：
  out/steps/s1_条目清单.json → out/steps/s2_分组统计.json
  out/卖点清单.md  out/卖点分组统计.csv  out/flow_result.json
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WF_DIR = os.path.dirname(HERE)
OUTDIR_DEFAULT = os.path.normpath(os.path.join(WF_DIR, "out"))

SOURCE_PATS = [
    ("参数", re.compile(r"^\s*(参数|规格)")),
    ("用户评价", re.compile(r"^\s*(用户评价|评价|买家评价)")),
    ("卖点页文案", re.compile(r"^\s*(卖点页文案|文案|卖点)")),
    ("客服记录", re.compile(r"^\s*(客服记录|客服|咨询)")),
    ("商品标题", re.compile(r"^\s*(商品标题|标题)")),
]
MIN_GROUP_N = 3  # 高频主题门槛

# 演示数据：与 runs_v2/shop-video-zh/workflows/sellingpoint-extract-flow 实跑同源
# （小熊保温吸管杯 儿童款 320ml）
DEMO = {
    "product": "小熊保温吸管杯（儿童款，320ml）",
    "input": "商品：小熊保温吸管杯（儿童款，320ml）。商品资料条目：\n"
             "1. 商品标题：小熊保温吸管杯 儿童款 薄荷绿 320ml\n"
             "2. 参数：316 不锈钢内胆，6 小时保温约 50℃（装 95℃ 热水测试）\n"
             "3. 参数：杯身重量 230g，配可调节背带\n"
             "4. 参数：吸管为食品级硅胶，可整体拆洗\n"
             "5. 参数：杯盖带安全锁扣，倒置不漏\n"
             "6. 用户评价：「装热水进去，下午接娃的时候还是温的」\n"
             "7. 用户评价：「吸管能拆下来洗，没有藏污纳垢的死角」\n"
             "8. 用户评价：「娃扔进书包里滚了一天，没漏」\n"
             "9. 用户评价：「背带扣有点松，娃跑起来会滑」\n"
             "10. 客服记录：被问最多的是保温时长、能不能装碳酸饮料、吸管口径粗细",
    "keywords": ["保温", "吸管", "漏", "背带", "拆洗"],
}


def s1_parse(payload):
    """S1：条目拆分编号（确定性前置；语义归并由模型在上游原子技能完成）。"""
    raw = payload.get("input") or payload.get("items") or ""
    items = []
    for line in raw.splitlines():
        m = re.match(r"^\s*(\d+)\.\s*(.+)$", line.strip())
        if m:
            items.append({"no": int(m.group(1)), "text": m.group(2).strip()})
    if not items:
        items = [{"no": 1, "text": raw.strip()}]
    return {"product": payload.get("product", ""), "items": items}


def classify(text):
    for name, pat in SOURCE_PATS:
        if pat.match(text):
            return name
    return "其他"


def s2_group(s1, keywords):
    """S2：来源分组 + 频次 + 关键词命中（全部确定性计算）。"""
    groups = {}
    for it in s1["items"]:
        src = classify(it["text"])
        groups.setdefault(src, []).append(it["no"])
    kw_hits = [{"keyword": kw,
                "hits": [it["no"] for it in s1["items"] if kw in it["text"]]}
               for kw in (keywords or [])]
    for k in kw_hits:
        k["hit_count"] = len(k["hits"])
    group_rows = []
    for src, nos in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        group_rows.append({"source": src, "count": len(nos), "item_nos": nos,
                           "is_major_theme": len(nos) >= MIN_GROUP_N})
    return {"n_items": len(s1["items"]), "n_sources": len(groups),
            "min_group_n": MIN_GROUP_N, "group_rows": group_rows, "keyword_hits": kw_hits}


def s3_deliverable(s1, s2, outdir):
    """S3：交付物组装 —— 卖点清单骨架（量化事实）+ 校验表 + 机器可读结果。"""
    steps_dir = os.path.join(outdir, "steps")
    os.makedirs(steps_dir, exist_ok=True)
    with open(os.path.join(steps_dir, "s1_条目清单.json"), "w", encoding="utf-8") as f:
        json.dump(s1, f, ensure_ascii=False, indent=2)
    with open(os.path.join(steps_dir, "s2_分组统计.json"), "w", encoding="utf-8") as f:
        json.dump(s2, f, ensure_ascii=False, indent=2)

    csv_path = os.path.join(outdir, "卖点分组统计.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["信息来源", "条目数", "条目编号", "高频主题(≥3条)"])
        for r in s2["group_rows"]:
            w.writerow([r["source"], r["count"], "、".join(map(str, r["item_nos"])),
                        "是" if r["is_major_theme"] else "否"])

    md_path = os.path.join(outdir, "卖点清单.md")
    lines = [f"# 卖点清单（工作流交付物）" + (f" —— {s1['product']}" if s1["product"] else ""),
             "",
             f"- 解析条目 {s2['n_items']} 条，信息来源 {s2['n_sources']} 类，高频主题门槛 ≥ {MIN_GROUP_N} 条",
             "",
             "## 来源分组与频次",
             "",
             "| # | 来源 | 条目数 | 条目编号 | 高频主题 |",
             "|---|---|---|---|---|"]
    for i, r in enumerate(s2["group_rows"], 1):
        lines.append(f"| {i} | {r['source']} | {r['count']} "
                     f"| {'、'.join(map(str, r['item_nos']))} | {'是' if r['is_major_theme'] else '否'} |")
    if s2["keyword_hits"]:
        lines += ["", "## 关键词命中", "",
                  "| 关键词 | 命中数 | 条目编号 |", "|---|---|---|"]
        for k in s2["keyword_hits"]:
            lines.append(f"| {k['keyword']} | {k['hit_count']} "
                         f"| {'、'.join(map(str, k['hits'])) or '—'} |")
    lines += ["",
              "## 分层卖点清单（骨架）",
              "",
              "> 主线卖点（高频主题）、辅助卖点、需规避表述三类，由模型在上游 S1 节点",
              "> （商品卖点提炼原子技能）的语义归并结果中补全命名与洞察；",
              "> 本骨架保留全部量化事实与证据条目编号，确保每条卖点可回溯。"]
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    js_path = os.path.join(outdir, "flow_result.json")
    result = {
        "flow": "sellingpoint-extract-flow",
        "flow_id": "de_ecom_05_wf01",
        "steps": ["S1 商品卖点提炼（原子技能）", "S2 分组统计核算（脚本）", "S3 交付物组装（脚本）"],
        "summary": f"解析 {s2['n_items']} 条条目 / {s2['n_sources']} 类来源，"
                   f"高频主题 {len([r for r in s2['group_rows'] if r['is_major_theme']])} 个",
        "steps_result": {"s1": {"n_items": s2["n_items"]},
                         "s2": {"groups": s2["group_rows"], "keywords": s2["keyword_hits"]}},
        "deliverable": os.path.abspath(md_path),
        "generated_at_note": "确定性计算由 run_flow.py 完成；语义洞察由模型按 prompt.txt 完成",
    }
    with open(js_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return [csv_path, md_path, js_path]


def main():
    ap = argparse.ArgumentParser(description="商品卖点提取工作流（S1 解析 → S2 统计 → S3 组装）")
    ap.add_argument("--input", help="输入 JSON（input 条目文本；可选 product/keywords）")
    ap.add_argument("--outdir", default=OUTDIR_DEFAULT, help="输出目录")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()

    if a.demo:
        payload = DEMO
    elif a.input:
        with open(a.input, encoding="utf-8") as f:
            payload = json.load(f)
    else:
        ap.error("需要 --input 或 --demo")

    s1 = s1_parse(payload)
    if not s1["items"] or not s1["items"][0]["text"]:
        print("[错误] input 为空：没有条目就不做分组，不编造卖点。", file=sys.stderr)
        sys.exit(3)
    s2 = s2_group(s1, payload.get("keywords"))
    files = s3_deliverable(s1, s2, a.outdir)
    print(f"工作流完成 —— {s2['n_items']} 条条目 / {s2['n_sources']} 类来源")
    for f in files:
        print(" 产物:", f, f"({os.path.getsize(f) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
