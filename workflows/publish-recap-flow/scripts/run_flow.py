# -*- coding: utf-8 -*-
"""
发布排期+数据复盘工作流 —— 端到端编排脚本。

流程（与 SKILL.md 的 DAG 一致）：
  S1 数据归因分析（原子技能，模型产出）：播放/完播/转化 → 归因结论
  S2 机会简报生成（跨仓原子技能，模型产出）：数据 → 结论式周报
  S3 本步（脚本承担）：复盘统计 / 爆款判定 / 转化强度 / 下周排期表（确定性）

本脚本承担确定性部分：
  - 播放均值与爆款判定（播放 ≥ 其余均值 × 2.0）
  - 完播率均值差、每万播放核销数（转化强度横向对比）
  - 下周排期表：复刻爆款结构 2 条 + 新角度测试 1 条（周一/三/五发布）
  复盘的因果解释与周报措辞由模型按 prompt.txt 的 S1/S2 完成。

用法：
  python run_flow.py --input input.json --outdir out
  python run_flow.py --demo

产物（每步读上一步产物，逐步落盘）：
  out/steps/s1_数据解析.json → out/steps/s2_爆款判定.json
  out/复盘统计.csv  out/发布排期.md  out/flow_result.json
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WF_DIR = os.path.dirname(HERE)
OUTDIR_DEFAULT = os.path.normpath(os.path.join(WF_DIR, "out"))

HOT_MULTIPLE = 2.0      # 爆款倍数
WEEKLY_TARGET = 3       # 每周发布条数
PUBLISH_DAYS = [0, 2, 4]  # 周一/周三/周五

# 演示数据：与 runs_v2/shop-video-zh/workflows/publish-recap-flow 实跑同源
# （麦禾烘焙日记 2026-10-06 ~ 10-12 周数据）
DEMO = {
    "account": "麦禾烘焙日记",
    "period": {"start": "2026-10-06", "end": "2026-10-12"},
    "videos": [
        {"title": "V1 碱水结试吃",             "plays": 8000,  "completion_pct": 15, "redemptions": 3},
        {"title": "V2 可颂出炉全过程",         "plays": 52000, "completion_pct": 26, "redemptions": 12},
        {"title": "V3 店主采访：为什么过夜半价", "plays": 14000, "completion_pct": 19, "redemptions": 5},
        {"title": "V4 店内陈列合集",           "plays": 6000,  "completion_pct": 12, "redemptions": 2},
    ],
    "background": "10/6 起评论区置顶「过夜半价」活动；账号未投流。",
}


def s1_parse(payload):
    """S1 数据解析（确定性）。支持结构化 videos 或 runs_v2 自由文本（V1「标题」播放 X 万…）。"""
    videos = list(payload.get("videos") or [])
    text = payload.get("input") or ""
    if not videos and text:
        for m in re.finditer(
                r"(V\d+)[「【]([^」】]+)[」」]播放\s*([\d.]+)\s*万、完播率\s*(\d+)%、到店核销\s*(\d+)\s*单",
                text):
            videos.append({"title": f"{m.group(1)} {m.group(2)}",
                           "plays": int(float(m.group(3)) * 10000),
                           "completion_pct": int(m.group(4)), "redemptions": int(m.group(5))})
    norm = []
    for v in videos:
        plays = v.get("plays", v.get("plays_wan", 0))
        if isinstance(plays, float) and plays < 100 and "万" not in str(v.get("plays", "")):
            pass  # 已是绝对值
        norm.append({"title": v.get("title", ""), "plays": int(plays),
                     "completion_pct": v.get("completion_pct"),
                     "redemptions": int(v.get("redemptions", 0))})
    return {"account": payload.get("account", ""), "videos": norm,
            "period": payload.get("period", {}), "background": payload.get("background", "")}


def s2_stats(s1):
    """S2 复盘统计与爆款判定（确定性）。"""
    videos = s1["videos"]
    n = len(videos)
    plays = [v["plays"] for v in videos]
    mean_plays = sum(plays) / n if n else 0.0
    total_redemptions = sum(v["redemptions"] for v in videos)
    rows = []
    for v in videos:
        others = [p for p in plays if p != v["plays"]] or [0]
        others_mean = sum(others) / len(others)
        rows.append({**v,
                     "plays_vs_mean_pct": round((v["plays"] / mean_plays - 1) * 100, 1) if mean_plays else 0.0,
                     "hot_multiple": round(v["plays"] / others_mean, 2) if others_mean else 0.0,
                     "is_hot": v["plays"] >= HOT_MULTIPLE * others_mean and n >= 4,
                     "redemptions_per_wan": round(v["redemptions"] / (v["plays"] / 10000), 2) if v["plays"] else 0.0})
    rows.sort(key=lambda r: -r["plays"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    completions = [v["completion_pct"] for v in videos if v.get("completion_pct") is not None]
    hot = [r for r in rows if r["is_hot"]]
    return {"n_videos": n, "mean_plays": round(mean_plays), "total_plays": sum(plays),
            "total_redemptions": total_redemptions,
            "avg_completion_pct": round(sum(completions) / len(completions), 1) if completions else None,
            "overall_redemptions_per_wan": round(total_redemptions / (sum(plays) / 10000), 2) if sum(plays) else 0.0,
            "hot_titles": [r["title"] for r in hot],
            "top_title": rows[0]["title"] if rows else None,
            "rows": rows, "hot_multiple": HOT_MULTIPLE}


def s3_schedule(s1, s2):
    """S3 下周排期表（确定性：复刻爆款结构 ×2 + 新角度测试 ×1，周一/三/五）。"""
    end = s1.get("period", {}).get("end")
    base = dt.date.fromisoformat(end) + dt.timedelta(days=1) if end else dt.date.today()
    # 对齐到下周周一
    days_ahead = (7 - base.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    monday = base + dt.timedelta(days=days_ahead)
    hot = s2["hot_titles"]
    plan = []
    for i, wd in enumerate(PUBLISH_DAYS):
        day = monday + dt.timedelta(days=wd)
        if i < min(2, len(hot)):
            action = f"复刻「{hot[i]}」结构（同开头形式+同节奏）"
            kind = "爆款复刻"
        else:
            action = f"新角度测试：围绕「{s2['top_title']}」的衍生选题做 A/B 对照"
            kind = "新角度测试"
        plan.append({"date": day.isoformat(), "kind": kind, "action": action,
                     "kpi": f"播放 ≥ {int(s2['mean_plays'] * 1.2)}，完播率 ≥ {round((s2['avg_completion_pct'] or 15) + 2)}%"})
    return {"next_week": monday.isoformat(), "weekly_target": WEEKLY_TARGET, "plan": plan}


def build(payload, outdir):
    steps_dir = os.path.join(outdir, "steps")
    os.makedirs(steps_dir, exist_ok=True)

    s1 = s1_parse(payload)
    if len(s1["videos"]) < 2:
        print("[错误] videos 至少需要 2 条（title/plays），数据不足不做归因，不编造结论。",
              file=sys.stderr)
        sys.exit(3)
    with open(os.path.join(steps_dir, "s1_数据解析.json"), "w", encoding="utf-8") as f:
        json.dump(s1, f, ensure_ascii=False, indent=2)

    s2 = s2_stats(s1)
    with open(os.path.join(steps_dir, "s2_爆款判定.json"), "w", encoding="utf-8") as f:
        json.dump(s2, f, ensure_ascii=False, indent=2)

    s3 = s3_schedule(s1, s2)

    csv_path = os.path.join(outdir, "复盘统计.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["排名", "标题", "播放", "完播率%", "核销单", "对均值偏离%", "爆款倍数",
                    "判定爆款", "每万播放核销"])
        for r in s2["rows"]:
            w.writerow([r["rank"], r["title"], r["plays"], r["completion_pct"], r["redemptions"],
                        r["plays_vs_mean_pct"], r["hot_multiple"],
                        "是" if r["is_hot"] else "否", r["redemptions_per_wan"]])

    md_path = os.path.join(outdir, "发布排期.md")
    lines = [
        f"# 发布排期+数据复盘（工作流交付物）" + (f" —— {s1['account']}" if s1["account"] else ""),
        "",
        f"- 本期：{s2['n_videos']} 条｜总播放 {s2['total_plays']}｜均值 {s2['mean_plays']}"
        f"｜总核销 {s2['total_redemptions']} 单",
        f"- 平均完播率 {s2['avg_completion_pct']}%｜整体每万播放核销 {s2['overall_redemptions_per_wan']} 单",
        f"- 爆款判定线：其余均值 × {HOT_MULTIPLE} → 爆款：{'、'.join(s2['hot_titles']) or '无'}",
        "",
        "## 下周排期（确定性骨架）",
        "",
        "| 发布日 | 类型 | 动作 | KPI 下限 |",
        "|---|---|---|---|",
    ]
    for p in s3["plan"]:
        lines.append(f"| {p['date']} | {p['kind']} | {p['action']} | {p['kpi']} |")
    lines += ["",
              "## 归因与周报",
              "",
              "> 因果归因结论由 S1（数据归因分析原子技能）撰写；",
              "> 结论式周报由 S2（机会简报生成，跨仓技能）完成；",
              "> 本骨架只保留量化事实与排期，措辞与最终选题需人工确认。"]
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    js_path = os.path.join(outdir, "flow_result.json")
    result = {
        "flow": "publish-recap-flow", "flow_id": "de_ecom_05_wf05",
        "steps": ["S1 数据归因分析（原子技能）", "S2 机会简报生成（跨仓原子技能）",
                  "S3 复盘统计与排期（脚本）"],
        "summary": f"{s2['n_videos']} 条 / 爆款 {len(s2['hot_titles'])} 条 / "
                   f"下周 {s3['weekly_target']} 条排期",
        "steps_result": {"s1": {"n_videos": s2["n_videos"]},
                         "s2": {k: s2[k] for k in ("mean_plays", "total_plays",
                                                   "total_redemptions", "hot_titles",
                                                   "avg_completion_pct")},
                         "s3": s3},
        "deliverable": os.path.abspath(md_path),
    }
    with open(js_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"工作流完成 —— {s2['n_videos']} 条复盘 / 爆款 {len(s2['hot_titles'])} 条 / "
          f"下周排期 {s3['weekly_target']} 条")
    files = [csv_path, md_path, js_path]
    for f_ in files:
        print(" 产物:", f_, f"({os.path.getsize(f_) / 1024:.1f} KB)")


def main():
    ap = argparse.ArgumentParser(description="发布排期+数据复盘工作流（S1/S2 归因简报 → S3 排期）")
    ap.add_argument("--input", help="输入 JSON（account/period/videos/background）")
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
    build(payload, a.outdir)


if __name__ == "__main__":
    main()
