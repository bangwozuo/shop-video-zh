# -*- coding: utf-8 -*-
"""
短视频脚本生成工作流 —— 端到端编排脚本。

流程（与 SKILL.md 的 DAG 一致）：
  S1 短视频脚本生成（原子技能，模型产出）：卖点+素材 → 四段式分镜脚本
  S2 黄金 3 秒钩子库（原子技能，模型产出）：钩子候选 → 选定钩子（≤15 字）
  S3 本步（脚本承担）：段落预算/语速/字数/平台时长上限的确定性核算

本脚本承担确定性部分（60 秒基准，其他时长等比缩放 ±2s）：
  - 四段式时间轴预算：钩子 3s / 痛点 12s / 主体 35s / CTA 10s
  - 语速基准 4-5 字/秒 → 全文台词 = 时长 × 4 ~ × 5 字
  - 钩子红线：≤3 秒、≤15 字
  - 平台时长上限核对（抖音/视频号 60s，小红书 240s，B站 600s）
  - 卖点数量核对：输入素材中的卖点条目 ≥ 3 条
  成稿由模型按 prompt.txt 完成；成稿后的逐段校验可把 segments 交本脚本核算。

用法：
  python run_flow.py --input input.json --outdir out
  python run_flow.py --demo

产物（每步读上一步产物，逐步落盘）：
  out/steps/s1_素材解析.json → out/steps/s2_钩子要求.json
  out/段落预算.csv  out/脚本骨架.md  out/flow_result.json
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

SEG_BUDGET_60 = {"钩子": 3, "痛点": 12, "主体": 35, "CTA": 10}  # 60s 基准（秒）
PLATFORM_MAX = {"抖音": 60, "视频号": 60, "小红书": 240, "B站": 600}
HOOK_MAX_CHARS = 15
MIN_POINTS = 3
SPEED_LO, SPEED_HI = 4.0, 5.0  # 字/秒

# 演示数据：与 runs_v2/shop-video-zh/workflows/shortvideo-script-flow 实跑同源
# （便携挂脖风扇 FS-02 通勤场景，45 秒，抖音）
DEMO = {
    "input": "主题：便携挂脖风扇 FS-02 通勤场景带货短视频。已确认卖点清单："
             "①内置 4000mAh 电池，满电续航 4-8 小时；"
             "②整机 265g，挂脖硅胶包覆，久戴不勒；"
             "③3 档风力+静音电机，办公室可用；"
             "④Type-C 充电口，2.5 小时充满。目标时长 45 秒，投放平台抖音。",
    "duration": "45秒",
    "platform": "抖音",
}


def _parse_duration(v):
    if v is None:
        return 60
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"(\d+)", str(v))
    return int(m.group(1)) if m else 60


def s1_parse(payload):
    """S1 输入解析：主题 / 卖点条目 / 时长 / 平台（确定性）。"""
    text = payload.get("input") or payload.get("topic") or ""
    points = re.findall(r"[①②③④⑤⑥⑦⑧⑨⑩]|(?:^|\s)\d+[.、]\s*\S", text)
    platform = payload.get("platform") or "抖音"
    duration_s = _parse_duration(payload.get("duration_s") or payload.get("duration") or 60)
    return {"topic": text.strip()[:80], "n_points": len(set(points)),
            "points_raw": text, "duration_s": duration_s, "platform": platform}


def s2_hook_brief(s1):
    """S2 钩子要求（确定性规则，候选句由模型按钩子库 prompt 产出）。"""
    total = s1["duration_s"]
    scale = total / 60.0
    hook_s = min(3, max(2, round(SEG_BUDGET_60["钩子"] * scale)))
    return {"hook_budget_s": hook_s, "hook_max_chars": HOOK_MAX_CHARS,
            "note": f"钩子 ≤ {hook_s}s 且 ≤ {HOOK_MAX_CHARS} 字，由黄金 3 秒钩子库产出候选"}


def s3_check(s1, hook_brief):
    """S3 确定性核算：段落预算 / 语速字数区间 / 平台上限 / 卖点覆盖。"""
    total, platform = s1["duration_s"], s1["platform"]
    scale = total / 60.0
    budget, used = {}, 0
    for seg, base in SEG_BUDGET_60.items():
        s = max(1, round(base * scale))
        budget[seg] = s
        used += s
    budget["主体"] += total - used  # 尾差归主体，保证合计 = 目标时长

    chars_lo, chars_hi = int(total * SPEED_LO), int(total * SPEED_HI)
    issues = []
    mx = PLATFORM_MAX.get(platform)
    platform_ok = (mx is None) or (total <= mx)
    if not platform_ok:
        issues.append(f"{platform} 时长上限 {mx}s，目标 {total}s 超出 {total - mx}s")
    if s1["n_points"] < MIN_POINTS:
        issues.append(f"卖点条目 {s1['n_points']} 条 < {MIN_POINTS} 条，不足以支撑主体段")

    rows = []
    cursor = 0
    for seg in ("钩子", "痛点", "主体", "CTA"):
        s = budget[seg]
        rows.append({"段落": seg, "起止": f"{cursor}-{cursor + s}s", "时长s": s,
                     "字数预算": f"{int(s * SPEED_LO)}-{int(s * SPEED_HI)} 字",
                     "备注": {"钩子": f"≤{HOOK_MAX_CHARS} 字",
                             "CTA": "只写 1 个动作"}[seg] if seg in ("钩子", "CTA") else ""})
        cursor += s
    return {"segment_budget": rows, "total_s": total,
            "chars_lo": chars_lo, "chars_hi": chars_hi,
            "platform": platform, "platform_max_s": mx, "platform_ok": platform_ok,
            "n_points": s1["n_points"], "min_points": MIN_POINTS,
            "hook": hook_brief, "issues": issues}


def build(payload, outdir):
    steps_dir = os.path.join(outdir, "steps")
    os.makedirs(steps_dir, exist_ok=True)

    s1 = s1_parse(payload)
    if not s1["points_raw"].strip():
        print("[错误] input 为空：没有主题与素材就不生成脚本，不编造卖点。", file=sys.stderr)
        sys.exit(3)
    with open(os.path.join(steps_dir, "s1_素材解析.json"), "w", encoding="utf-8") as f:
        json.dump(s1, f, ensure_ascii=False, indent=2)

    hook_brief = s2_hook_brief(s1)
    with open(os.path.join(steps_dir, "s2_钩子要求.json"), "w", encoding="utf-8") as f:
        json.dump(hook_brief, f, ensure_ascii=False, indent=2)

    check = s3_check(s1, hook_brief)

    csv_path = os.path.join(outdir, "段落预算.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["段落", "起止", "时长s", "字数预算", "备注"])
        for r in check["segment_budget"]:
            w.writerow([r["段落"], r["起止"], r["时长s"], r["字数预算"], r["备注"]])

    md_path = os.path.join(outdir, "脚本骨架.md")
    verdict = "打回（时长/卖点不达标）" if check["issues"] else "预算核算通过"
    lines = [
        "# 短视频脚本骨架（工作流交付物）",
        "",
        f"- 主题：{s1['topic']}…",
        f"- 目标时长 {check['total_s']}s｜平台 {check['platform']}"
        f"（上限 {check['platform_max_s'] if check['platform_max_s'] else '以官方为准'}s）"
        f"｜卖点条目 {check['n_points']} 条",
        f"- 全文台词预算：{check['chars_lo']}-{check['chars_hi']} 字（{SPEED_LO}-{SPEED_HI} 字/秒）",
        f"- 钩子要求：≤ {hook_brief['hook_budget_s']}s、≤ {hook_brief['hook_max_chars']} 字",
        "",
        "| 段落 | 起止 | 时长s | 字数预算 | 备注 |",
        "|---|---|---|---|---|",
    ]
    for r in check["segment_budget"]:
        lines.append(f"| {r['段落']} | {r['起止']} | {r['时长s']} | {r['字数预算']} | {r['备注']} |")
    if check["issues"]:
        lines += ["", "## 不达标项", ""]
        lines += [f"- {i}" for i in check["issues"]]
    lines += ["",
              "## 成稿要求",
              "",
              "- 每个卖点至少对应 1 个分镜的「画面内容」列；单分镜 3-8 秒，超 8 秒必须拆分",
              "- 分镜表最后一行结束时间 = 目标时长（±2s）；CTA 只写 1 个动作",
              "- 成稿由模型按 prompt.txt（S1 短视频脚本生成 + S2 黄金 3 秒钩子库）完成"]
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    js_path = os.path.join(outdir, "flow_result.json")
    result = {
        "flow": "shortvideo-script-flow", "flow_id": "de_ecom_05_wf02",
        "steps": ["S1 短视频脚本生成（原子技能）", "S2 黄金 3 秒钩子库（原子技能）",
                  "S3 预算与红线核算（脚本）"],
        "summary": f"{check['total_s']}s / {check['platform']} / 卖点 {check['n_points']} 条 —— {verdict}",
        "steps_result": {"s1": {k: s1[k] for k in ("topic", "n_points", "duration_s", "platform")},
                         "s2": hook_brief,
                         "s3": {"segment_budget": check["segment_budget"],
                                "chars": [check["chars_lo"], check["chars_hi"]],
                                "issues": check["issues"]}},
        "deliverable": os.path.abspath(md_path),
        "verdict": verdict,
    }
    with open(js_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"工作流完成 —— {check['total_s']}s / {check['platform']} / 卖点 {check['n_points']} 条：{verdict}")
    files = [csv_path, md_path, js_path]
    for f_ in files:
        print(" 产物:", f_, f"({os.path.getsize(f_) / 1024:.1f} KB)")


def main():
    ap = argparse.ArgumentParser(description="短视频脚本生成工作流（S1 脚本 → S2 钩子 → S3 核算）")
    ap.add_argument("--input", help="输入 JSON（input/duration/platform）")
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
