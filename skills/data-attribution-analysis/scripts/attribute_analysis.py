# -*- coding: utf-8 -*-
"""
数据归因分析 —— 确定性量化归因脚本。

职责边界（重要）：
  本脚本只做**确定性计算与产物生成**：播放均值/爆款倍数判定、完播率与核销的周环比、
  每万播放核销数（转化强度）、贡献度排序、CSV/JSON/MD 落盘。
  因果解释、混杂因素剥离的语境判断、置信度措辞，由模型按 prompt.txt 完成。

量化规则（与 prompt.txt 一致）：
  - 爆款判定：单条播放 ≥ 其余条目播放均值 × 2.0
  - 显著波动：周环比 |Δ| ≥ 20% 才计入驱动因素
  - 转化强度：每万播放核销数 = 核销单数 / (播放/10000)，用于跨条目对比
  - 样本保护：条目数 < 4 时不输出倍数结论，只输出排名

用法：
  python attribute_analysis.py --input input.json --outdir out
  python attribute_analysis.py --demo --outdir out

产物：
  out/归因明细.csv      逐条目量化指标与排名
  out/attribution.json  机器可读结果（供工作流下游读取）
  out/归因摘要.md       量化摘要（不含因果结论，结论由模型补写）
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR_DEFAULT = os.path.normpath(os.path.join(HERE, "..", "out"))

HOT_MULTIPLE = 2.0        # 爆款倍数：播放 ≥ 其余均值 × 2.0
SIGNIFICANT_PCT = 20.0    # 周环比显著波动阈值（%）
MIN_SAMPLE = 4            # 倍数结论的最小样本条数

# 演示数据：与 runs_v2/shop-video-zh/skills/data-attribution-analysis 实跑同源
# （麦禾烘焙日记 6 条视频，2026-09-22 ~ 2026-10-05）
DEMO = {
    "videos": [
        {"title": "碱水结试吃",        "week": 1, "plays": 12000, "completion_pct": 17, "redemptions": 8},
        {"title": "店面静态空镜合集A", "week": 1, "plays": 9000,  "completion_pct": 18, "redemptions": 7},
        {"title": "产品特写混剪",      "week": 1, "plays": 15000, "completion_pct": 19, "redemptions": 12},
        {"title": "店内静态空镜合集B", "week": 2, "plays": 11000, "completion_pct": 18, "redemptions": 9},
        {"title": "可颂出炉全过程",    "week": 2, "plays": 46000, "completion_pct": 26, "redemptions": 25},
        {"title": "店主采访",          "week": 2, "plays": 10000, "completion_pct": 24, "redemptions": 7},
    ],
    "context": "10/1-10/3 为国庆假期，门店客流本身偏高；10/4 起店内上线「过夜半价」活动。账号未投流。",
}


def compute(videos):
    """确定性指标：均值/倍数/环比/转化强度/排名。全部数值在此算出，模型不得自行计算。"""
    n = len(videos)
    plays = [v["plays"] for v in videos]
    mean_plays = sum(plays) / n if n else 0.0
    rows = []
    for v in videos:
        others = [p for p in plays if p != v["plays"]] or [0]
        others_mean = sum(others) / len(others)
        multiple = v["plays"] / others_mean if others_mean else 0.0
        rows.append({
            "title": v["title"],
            "week": v.get("week"),
            "plays": v["plays"],
            "completion_pct": v.get("completion_pct"),
            "redemptions": v.get("redemptions", 0),
            "plays_vs_mean_pct": round((v["plays"] / mean_plays - 1) * 100, 1) if mean_plays else 0.0,
            "hot_multiple": round(multiple, 2),
            "is_hot": v["plays"] >= HOT_MULTIPLE * others_mean and n >= MIN_SAMPLE,
            "redemptions_per_wan": round(v.get("redemptions", 0) / (v["plays"] / 10000), 2)
                                    if v["plays"] else 0.0,
        })
    rows.sort(key=lambda r: r["plays"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    # 周维度汇总（week 1 vs week 2）
    weeks = {}
    for r in rows:
        w = weeks.setdefault(r["week"], {"plays": 0, "redemptions": 0, "n": 0, "completions": []})
        w["plays"] += r["plays"]
        w["redemptions"] += r["redemptions"]
        w["n"] += 1
        if r["completion_pct"] is not None:
            w["completions"].append(r["completion_pct"])
    week_summary = []
    wkeys = sorted(weeks)
    for w in wkeys:
        d = weeks[w]
        comp = sum(d["completions"]) / len(d["completions"]) if d["completions"] else None
        week_summary.append({
            "week": w, "videos": d["n"], "plays": d["plays"], "redemptions": d["redemptions"],
            "avg_completion_pct": round(comp, 1) if comp is not None else None,
            "redemptions_per_wan": round(d["redemptions"] / (d["plays"] / 10000), 2) if d["plays"] else 0.0,
        })
    # 周环比
    for i in range(1, len(week_summary)):
        prev, cur = week_summary[i - 1], week_summary[i]
        for key in ("plays", "redemptions"):
            if prev[key]:
                cur[f"{key}_wow_pct"] = round((cur[key] / prev[key] - 1) * 100, 1)
    return {
        "n_videos": n,
        "mean_plays": round(mean_plays, 0),
        "total_plays": sum(plays),
        "total_redemptions": sum(r["redemptions"] for r in rows),
        "hot_multiple_threshold": HOT_MULTIPLE,
        "significant_pct": SIGNIFICANT_PCT,
        "rows": rows,
        "week_summary": week_summary,
        "hot_titles": [r["title"] for r in rows if r["is_hot"]],
        "top3_titles": [r["title"] for r in rows[:3]],
    }


def write_outputs(result, context, outdir):
    os.makedirs(outdir, exist_ok=True)
    files = []

    # CSV（utf-8-sig，Excel 直接打开不乱码）
    csv_path = os.path.join(outdir, "归因明细.csv")
    cols = ["rank", "title", "week", "plays", "completion_pct", "redemptions",
            "plays_vs_mean_pct", "hot_multiple", "is_hot", "redemptions_per_wan"]
    cn = {"rank": "排名", "title": "标题", "week": "周次", "plays": "播放",
          "completion_pct": "完播率%", "redemptions": "核销单", "plays_vs_mean_pct": "对均值偏离%",
          "hot_multiple": "爆款倍数", "is_hot": "判定爆款", "redemptions_per_wan": "每万播放核销"}
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([cn[c] for c in cols])
        for r in result["rows"]:
            w.writerow([("是" if r[c] else "否") if c == "is_hot" else r[c] for c in cols])
    files.append(csv_path)

    # JSON
    json_path = os.path.join(outdir, "attribution.json")
    payload = {**result, "context": context,
               "note": "数值均由本脚本计算；因果解释、置信度措辞由模型按 prompt.txt 完成"}
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    files.append(json_path)

    # MD 摘要（只含量化事实，结论留白由模型补写）
    md_path = os.path.join(outdir, "归因摘要.md")
    lines = [
        "# 数据归因分析 —— 量化摘要（脚本产物）",
        "",
        f"- 样本：{result['n_videos']} 条视频，总播放 {result['total_plays']}，总核销 {result['total_redemptions']} 单",
        f"- 播放均值：{result['mean_plays']:.0f}；爆款判定线：其余均值 × {HOT_MULTIPLE}",
        f"- 爆款条目：{'、'.join(result['hot_titles']) if result['hot_titles'] else '无（未达倍数线）'}",
        "",
        "| 排名 | 标题 | 播放 | 完播率% | 核销单 | 每万播放核销 | 爆款倍数 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in result["rows"]:
        lines.append(f"| {r['rank']} | {r['title']} | {r['plays']} | {r['completion_pct']} "
                     f"| {r['redemptions']} | {r['redemptions_per_wan']} | {r['hot_multiple']} |")
    lines += ["", "## 周维度汇总", "",
              "| 周次 | 条数 | 播放 | 平均完播率% | 核销单 | 每万播放核销 | 播放环比% |",
              "|---|---|---|---|---|---|---|"]
    for w in result["week_summary"]:
        lines.append(f"| 第{w['week']}周 | {w['videos']} | {w['plays']} | {w['avg_completion_pct']} "
                     f"| {w['redemptions']} | {w['redemptions_per_wan']} | {w.get('plays_wow_pct', '—')} |")
    lines += ["", "> 因果结论、混杂因素剥离与置信度：由模型按 prompt.txt 在本摘要基础上完成。"]
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    files.append(md_path)
    return files


def main():
    ap = argparse.ArgumentParser(description="数据归因分析（确定性量化归因）")
    ap.add_argument("--input", help="输入 JSON（videos 数组：title/week/plays/completion_pct/redemptions）")
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

    videos = payload.get("videos")
    if not videos or len(videos) < 2:
        print("[错误] videos 至少需要 2 条结构化记录（title/plays），缺失不估算。", file=sys.stderr)
        sys.exit(3)

    result = compute(videos)
    files = write_outputs(result, payload.get("context", ""), a.outdir)
    print(f"归因计算完成 —— {result['n_videos']} 条，爆款 {len(result['hot_titles'])} 条，"
          f"总播放 {result['total_plays']}，总核销 {result['total_redemptions']} 单")
    for f in files:
        print(" 产物:", f, f"({os.path.getsize(f) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
