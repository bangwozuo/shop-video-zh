# -*- coding: utf-8 -*-
"""
素材混剪指令包工作流 —— 端到端编排脚本。

流程（与 SKILL.md 的 DAG 一致）：
  S1 短视频脚本生成（原子技能，模型产出）：素材清单 → 混剪分镜脚本
  S2 黄金 3 秒钩子库（原子技能，模型产出）：开场钩子段选定
  S3 本步（脚本承担）：素材解析 / 清晰度优先选片 / 时长凑齐 / 指令包组装（确定性）

本脚本承担确定性部分：
  - 素材条目解析（编号 + 描述 + 分辨率 + 时长）
  - 选片规则：4K 优先；单条不超过 8 秒（超长素材拆多段）；按序填充至目标时长
  - 储备校验：素材总时长 ≥ 目标时长 × 1.2（不足打回补素材）
  - 时间轴排列表与剪映/CapCut 步骤指令包生成
  分镜创意与钩子文案由模型按 prompt.txt 的 S1/S2 完成。

用法：
  python run_flow.py --input input.json --outdir out
  python run_flow.py --demo

产物（每步读上一步产物，逐步落盘）：
  out/steps/s1_素材解析.json → out/steps/s2_时间轴.json
  out/时间轴排列.csv  out/剪映指令包.md  out/flow_result.json
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

MAX_CLIP_S = 8          # 单段时间上限（秒），超过必须拆分
RESERVE_RATIO = 1.2     # 素材总时长储备系数
RES_SCORE = {"4K": 3, "8K": 4, "1080P": 2, "720P": 1}

# 演示数据：与 runs_v2/shop-video-zh/workflows/mashcut-instruction-pack-flow 实跑同源
DEMO = {
    "input": "素材清单：A001 可颂出炉特写（4K，15 秒）、A002 门口排队人群（1080P，15 秒）、"
             "A003 柜台面包陈列（1080P，20 秒）、A004 店主揉面采访片段（1080P，60 秒）、"
             "A005 可颂拉丝试吃（4K，25 秒）。成片目标：30 秒竖版 9:16，用于抖音探店号；"
             "配乐方向：轻快爵士；字幕：烧录样式。请生成剪映/CapCut 操作步骤指令包。",
    "duration": "30秒",
    "platform": "抖音",
}


def _parse_duration(v):
    if v is None:
        return 30
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"(\d+)\s*秒", str(v))
    return int(m.group(1)) if m else 30


def s1_parse(payload):
    """S1 素材条目解析（确定性）。"""
    text = payload.get("input") or ""
    text = re.sub(r"[。；;]*\s*请[^。；]*$", "", text)
    materials = []
    for m in re.finditer(
            r"([A-Za-z]\d{2,4})\s*([^，。;；()（）]+)（\s*(4K|8K|1080P|720P)\s*[，,]\s*(\d+)\s*秒）",
            text):
        materials.append({"id": m.group(1), "desc": m.group(2).strip(),
                          "resolution": m.group(3), "duration_s": int(m.group(4)),
                          "res_score": RES_SCORE.get(m.group(3), 0)})
    if not materials:  # 兜底：宽松解析
        for m in re.finditer(r"([A-Za-z]\d{2,4})", text):
            materials.append({"id": m.group(1), "desc": "素材", "resolution": "1080P",
                              "duration_s": 5, "res_score": 2})
    total = sum(m["duration_s"] for m in materials)
    platform = payload.get("platform") or ("抖音" if "抖音" in text else "抖音")
    return {"materials": materials, "total_material_s": total,
            "target_s": _parse_duration(payload.get("duration_s") or payload.get("duration")),
            "platform": platform,
            "aspect": "9:16" if "竖版" in text or "9:16" in text else "横版 16:9",
            "music": (re.search(r"配乐方向[:：]?\s*([^；。;]+)", text) or [None, ""])[1].strip(),
            "subtitle_style": "烧录" if "烧录" in text else "软件字幕"}


def s2_timeline(s1):
    """S2 选片与时间轴排列（确定性：4K 优先、单段 ≤8s、按序凑齐目标时长）。"""
    target = s1["target_s"]
    pool = sorted(s1["materials"], key=lambda m: (-m["res_score"], m["id"]))
    segments, used = [], 0
    for m in pool:
        if used >= target:
            break
        remain = target - used
        take = min(remain, m["duration_s"], MAX_CLIP_S)
        if take <= 0:
            continue
        segs = []
        left = take
        while left > 0:
            cut = min(left, MAX_CLIP_S)
            segs.append(cut)
            left -= cut
        for i, cut in enumerate(segs, 1):
            segments.append({"start_s": used, "clip": m["id"],
                             "desc": m["desc"], "resolution": m["resolution"],
                             "duration_s": cut,
                             "usage": "整段" if cut == m["duration_s"] else
                                      (f"第{i}段/共{len(segs)}段" if len(segs) > 1 else "截取")})
            used += cut
    reserve_ok = s1["total_material_s"] >= target * RESERVE_RATIO
    issues = []
    if not reserve_ok:
        issues.append(f"素材总时长 {s1['total_material_s']}s < 目标 {target}s × {RESERVE_RATIO}，"
                      f"需补素材 ≥ {int(target * RESERVE_RATIO - s1['total_material_s'])}s")
    if used < target:
        issues.append(f"选片仅凑齐 {used}s / 目标 {target}s，素材不足")
    hook_seg = next((s for s in segments if s["resolution"] == "4K"), None)
    if hook_seg is None:
        issues.append("无 4K 素材可用作开场特写，建议补拍")
    return {"segments": segments, "used_s": used, "reserve_ratio": RESERVE_RATIO,
            "reserve_ok": reserve_ok, "hook_clip": hook_seg["clip"] if hook_seg else None,
            "issues": issues}


def build(payload, outdir):
    steps_dir = os.path.join(outdir, "steps")
    os.makedirs(steps_dir, exist_ok=True)

    s1 = s1_parse(payload)
    if not s1["materials"]:
        print("[错误] 未解析到素材条目（格式：A001 描述（4K，15 秒）），不编造素材。", file=sys.stderr)
        sys.exit(3)
    with open(os.path.join(steps_dir, "s1_素材解析.json"), "w", encoding="utf-8") as f:
        json.dump(s1, f, ensure_ascii=False, indent=2)

    s2 = s2_timeline(s1)
    with open(os.path.join(steps_dir, "s2_时间轴.json"), "w", encoding="utf-8") as f:
        json.dump(s2, f, ensure_ascii=False, indent=2)

    csv_path = os.path.join(outdir, "时间轴排列.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["时间轴(s)", "素材", "描述", "分辨率", "时长s", "用量"])
        for s in s2["segments"]:
            w.writerow([f"{s['start_s']}-{s['start_s'] + s['duration_s']}", s["clip"], s["desc"],
                        s["resolution"], s["duration_s"], s["usage"]])

    md_path = os.path.join(outdir, "剪映指令包.md")
    verdict = "打回（素材/储备不足）" if s2["issues"] else "指令包生成通过"
    lines = [
        "# 剪映/CapCut 混剪指令包（工作流交付物）",
        "",
        f"- 成片目标：{s1['target_s']}s｜{s1['aspect']}｜{s1['platform']}探店号",
        f"- 素材储备：{s1['total_material_s']}s（要求 ≥ 目标 × {RESERVE_RATIO} = "
        f"{int(s1['target_s'] * RESERVE_RATIO)}s）",
        f"- 开场钩子段：{s2['hook_clip'] or '无 4K 素材'}（4K 优先作 0-3s 特写）",
        f"- 配乐：{s1['music'] or '自选'}｜字幕：{s1['subtitle_style']}",
        f"- 判定：{verdict}",
        "",
        "## 操作步骤",
        "",
        "1. 新建工程：比例 9:16，导入全部素材并按编号命名轨道",
        "2. 按 `out/时间轴排列.csv` 的顺序拖入主轨，素材-时间一一对应",
        f"3. 每段不超过 {MAX_CLIP_S} 秒；标注「截取」的段用分割工具裁剪",
        "4. 0-3s 开场段加定帧或慢放强化钩子（分镜创意见 S1 原子技能产出）",
        f"5. 配乐铺底：{s1['music'] or '轻快类'}，音量压至人声的 30% 以下",
        f"6. 字幕：{s1['subtitle_style']}样式，单行 ≤16 字",
        "7. 导出：1080P / 30fps，成片时长核对 = 目标时长 ±2s",
    ]
    if s2["issues"]:
        lines += ["", "## 不达标项", ""] + [f"- {i}" for i in s2["issues"]]
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    js_path = os.path.join(outdir, "flow_result.json")
    result = {
        "flow": "mashcut-instruction-pack-flow", "flow_id": "de_ecom_05_wf04",
        "steps": ["S1 短视频脚本生成（原子技能）", "S2 黄金 3 秒钩子库（原子技能）",
                  "S3 选片与指令包组装（脚本）"],
        "summary": f"{s1['target_s']}s 成片 / {len(s2['segments'])} 段时间轴 / "
                   f"素材储备 {'达标' if s2['reserve_ok'] else '不足'} —— {verdict}",
        "steps_result": {"s1": {"n_materials": len(s1["materials"]),
                                "total_material_s": s1["total_material_s"]},
                         "s2": {"segments": s2["segments"], "used_s": s2["used_s"],
                                "issues": s2["issues"]}},
        "deliverable": os.path.abspath(md_path),
        "verdict": verdict,
    }
    with open(js_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"工作流完成 —— {s1['target_s']}s 成片 / {len(s2['segments'])} 段：{verdict}")
    files = [csv_path, md_path, js_path]
    for f_ in files:
        print(" 产物:", f_, f"({os.path.getsize(f_) / 1024:.1f} KB)")


def main():
    ap = argparse.ArgumentParser(description="素材混剪指令包工作流（S1/S2 分镜钩子 → S3 指令包）")
    ap.add_argument("--input", help="输入 JSON（input 素材清单/duration/platform）")
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
