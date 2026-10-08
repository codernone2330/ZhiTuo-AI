#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""智拓·政企企业评分模型 —— 独立运行入口（一键重建）。

输入：企查查「高级搜索」导出的 .xlsx（33 列）。脚本会自动识别表头行
      （扫描前若干行找到含「企业名称」的行），因此无论表头在第 1 行还是第 2 行都能读。

输出（默认写入 ZhiTuo-AI/output/enterprise_scoring/）：
    ├── 政企企业评分_三版本.csv        全量评分名单（含联系方式，可直接分派）
    ├── scoring_anchors_lock.json      锚点锁（保证同区域多次运行分数一致）
    ├── scoring_metrics.json           区分度/分级/方案线统计（报告的唯一数据源）
    └── scoring_report.html            自包含 HTML 报告（中文·浅色商务风）

用法：
    python scripts/score_enterprise.py                       # 用内置样例
    python scripts/score_enterprise.py path/to/企业清单.xlsx
    python scripts/score_enterprise.py 企业清单.xlsx --out ./out --top 100
    python scripts/score_enterprise.py 企业清单.xlsx --as-of 2026-10-06
    python scripts/score_enterprise.py 企业清单.xlsx --no-lock   # 忽略锚点锁，强制重标

环境：Python 3.10+，pandas / openpyxl（仅用于读写表格；评分引擎本身零第三方依赖）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

# 让脚本能直接 import 后端内的纯引擎（不触发 FastAPI/数据库依赖）
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

from app.modules.scoring import engine  # noqa: E402

DEFAULT_SRC = Path(
    r"C:/Users/Mark Wu/Desktop/政企客户拜访评分系统/samples/输入样例_企业清单.xlsx"
)
DEFAULT_OUT = REPO / "output" / "enterprise_scoring"


# --------------------------------------------------------------------------- #
# 读取输入
# --------------------------------------------------------------------------- #
def read_companies(src: Path) -> tuple[pd.DataFrame, int]:
    """读取企查查 xlsx，自动定位表头行。返回 (DataFrame, 表头行号)。"""
    raw = pd.read_excel(src, header=None, dtype=object)
    header_row = 0
    for index in range(min(8, len(raw))):
        values = [str(v) for v in raw.iloc[index].tolist()]
        if any("企业名称" == v.strip() for v in values):
            header_row = index
            break
    frame = pd.read_excel(src, header=header_row, dtype=object)
    frame = frame.dropna(how="all")
    frame = frame[frame.iloc[:, 0].notna()]
    return frame, header_row


def frame_to_companies(frame: pd.DataFrame) -> list[dict]:
    return [engine.normalize_company(row) for row in frame.to_dict("records")]


# --------------------------------------------------------------------------- #
# 指标（动态派生 —— 报告里所有数字都从这里来）
# --------------------------------------------------------------------------- #
def build_metrics(scored: list[dict], meta: dict, config: dict, check: dict) -> dict:
    versions = list(engine.active_versions(config))
    frame = pd.DataFrame(
        {
            "name": [s["name"] for s in scored],
            "solutionLine": [s["solutionLine"] for s in scored],
            "coefficient": [s["solutionCoefficient"] for s in scored],
            **{v: [s["scores"][v] for s in scored] for v in versions},
            **{f"feat_{k}": [s["features"][k] for s in scored] for k in engine.FEATURES},
        }
    )
    if versions:
        frame["persistScore"] = [s["scores"][meta["persistVersion"]] for s in scored]
        frame["persistTier"] = [s["tiers"][meta["persistVersion"]] for s in scored]

    version_stats = {}
    for version in versions:
        col = frame[version]
        # 首尾区分度：取头部/尾部十分位（最多各 100 家），避免小样本退化为 0
        k = min(100, max(5, len(col) // 10))
        head = col.nlargest(k).mean()
        tail = col.nsmallest(k).mean()
        tier_series = pd.Series([s["tiers"][version] for s in scored])
        version_stats[version] = {
            "mean": round(float(col.mean()), 1),
            "std": round(float(col.std(ddof=0)), 2),
            "p10": round(float(col.quantile(0.10)), 1),
            "median": round(float(col.median()), 1),
            "p90": round(float(col.quantile(0.90)), 1),
            "min": round(float(col.min()), 1),
            "max": round(float(col.max()), 1),
            "head100": round(float(head), 1),
            "tail100": round(float(tail), 1),
            "spread": round(float(head - tail), 1),
            "tier": {
                tier: int(tier_series.value_counts().get(tier, 0))
                for tier in ("S", "A", "B", "C", "D")
            },
        }

    # 版本间 Spearman 相关（互补性检验）
    # 注意：不用 pandas 的 method="spearman"——它会隐式依赖 scipy。Spearman 本质是
    # 「秩上的 Pearson」，用 rank() 后取默认 pearson 相关即可，无需 scipy。
    corr = {}
    for i, a in enumerate(versions):
        for b in versions[i + 1:]:
            value = frame[a].rank().corr(frame[b].rank())
            corr[f"{a} vs {b}"] = round(float(value), 3) if value == value else 0.0

    # 特征区分度诊断（方案契合 std 是 target leakage 的关键指标）
    feature_stats = {}
    for feature in engine.FEATURES:
        col = frame[f"feat_{feature}"]
        feature_stats[feature] = {
            "mean": round(float(col.mean()), 3),
            "std": round(float(col.std(ddof=0)), 3),
            "ceilingRatio": round(float((col >= 0.999).mean()), 3),
        }

    # 方案线分层
    lines: list[dict] = []
    for line, sub in frame.groupby("solutionLine"):
        lines.append(
            {
                "line": line,
                "coefficient": float(sub["coefficient"].iloc[0]),
                "count": int(len(sub)),
                "ratio": round(len(sub) / len(frame) * 100, 1),
                "avgScore": round(float(sub["persistScore"].mean()), 1),
                "topTier": int(sub["persistTier"].isin(["S", "A"]).sum()),
            }
        )
    lines.sort(key=lambda item: (item["coefficient"], item["count"]), reverse=True)

    tier_counts = {
        tier: int(sum(1 for s in scored if s["tiers"][meta["persistVersion"]] == tier))
        for tier in ("S", "A", "B", "C", "D")
    }

    return {
        "meta": meta,
        "versions": version_stats,
        "correlation": corr,
        "features": feature_stats,
        "lines": lines,
        "tierCounts": tier_counts,
        "selfCheck": check,
        "total": len(scored),
    }


# --------------------------------------------------------------------------- #
# HTML 报告
# --------------------------------------------------------------------------- #
TIER_COLORS = {"S": "#c0392b", "A": "#e67e22", "B": "#2f80ed", "C": "#27ae60", "D": "#95a5a6"}
SOLUTION_COLORS = [
    "#c0392b", "#d35400", "#e67e22", "#f39c12", "#2f80ed", "#2980b9",
    "#16a085", "#27ae60", "#8e44ad", "#7f8c8d", "#34495e", "#2c3e50",
    "#1abc9c", "#3498db", "#9b59b6", "#e84393",
]


def _esc(text: object) -> str:
    return (
        str(text)
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _bar(value: float, maximum: float, color: str, width: int = 160) -> str:
    pct = 0 if maximum <= 0 else max(0.0, min(1.0, value / maximum))
    return (
        f'<span class="bar"><i style="width:{pct * width:.0f}px;background:{color}"></i></span>'
        f'<span class="barnum">{value:.0f}</span>'
    )


def render_report(metrics: dict, scored: list[dict], config: dict, top_n: int) -> str:
    meta = metrics["meta"]
    versions = list(metrics["versions"])
    tier_counts = metrics["tierCounts"]
    total = metrics["total"]
    check = metrics["selfCheck"]

    # ---- KPI ----
    kpis = [
        ("企业总数", f"{total:,}", "本次评分样本"),
        ("S 级 · 优先拜访", f"{tier_counts['S']}", "客户经理一对一上门"),
        ("A 级 · 重点拜访", f"{tier_counts['A']}", "客户经理上门"),
        ("方案线覆盖", f"{len(metrics['lines'])}", "共 16 条方案线"),
        ("行业映射通过", "是" if check["passed"] else "否", f"兜底桶占比 {check['fallbackRatio'] * 100:.1f}%"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="kpi-v">{_esc(v)}</div>'
        f'<div class="kpi-l">{_esc(l)}</div><div class="kpi-h">{_esc(h)}</div></div>'
        for l, v, h in kpis
    )

    # ---- 分级分布 ----
    max_tier = max(tier_counts.values()) or 1
    tier_rows = "".join(
        f'<tr><td><span class="tag" style="background:{TIER_COLORS[t]}">{t}</span></td>'
        f'<td class="num">{tier_counts[t]}</td>'
        f'<td>{_bar(tier_counts[t], max_tier, TIER_COLORS[t])}</td>'
        f'<td class="num">{tier_counts[t] / total * 100:.1f}%</td></tr>'
        for t in ("S", "A", "B", "C", "D")
    )

    # ---- 三版本对比 ----
    version_head = "".join(f"<th>{_esc(v)}</th>" for v in versions)
    stat_labels = [
        ("均值", "mean"), ("标准差", "std"), ("P10", "p10"), ("中位数", "median"),
        ("P90", "p90"), ("最低", "min"), ("最高", "max"),
        ("头部100均值", "head100"), ("尾部100均值", "tail100"), ("首尾差", "spread"),
    ]
    version_rows = "".join(
        "<tr>" + f'<td class="lbl">{label}</td>'
        + "".join(f'<td class="num">{metrics["versions"][v][key]}</td>' for v in versions)
        + "</tr>"
        for label, key in stat_labels
    )
    corr_rows = "".join(
        f'<tr><td class="lbl">{_esc(k)}</td><td class="num">{v}</td></tr>'
        for k, v in metrics["correlation"].items()
    )

    # ---- 特征诊断 ----
    max_std = max((f["std"] for f in metrics["features"].values()), default=1) or 1
    feature_rows = "".join(
        f'<tr><td class="lbl">{_esc(name)}</td>'
        f'<td class="num">{info["mean"]:.3f}</td>'
        f'<td class="num">{info["std"]:.3f}</td>'
        f'<td>{_bar(info["std"], max_std, "#2f80ed")}</td>'
        f'<td class="num">{info["ceilingRatio"] * 100:.1f}%</td></tr>'
        for name, info in sorted(
            metrics["features"].items(), key=lambda kv: kv[1]["std"], reverse=True
        )
    )

    # ---- 方案线分层 ----
    line_rows = "".join(
        f'<tr><td>{_esc(item["line"])}</td>'
        f'<td class="num">{item["coefficient"]:.1f}</td>'
        f'<td class="num">{item["count"]}</td>'
        f'<td class="num">{item["ratio"]:.1f}%</td>'
        f'<td class="num">{item["avgScore"]:.1f}</td>'
        f'<td class="num">{item["topTier"]}</td></tr>'
        for item in metrics["lines"]
    )

    # ---- Top 名单 ----
    top_rows = ""
    for item in scored[:top_n]:
        version_cells = "".join(
            f'<td class="num"><b>{item["scores"][v]:.1f}</b>'
            f'<span class="mini tag" style="background:{TIER_COLORS[item["tiers"][v]]}">'
            f'{item["tiers"][v]}</span></td>'
            for v in versions
        )
        contact = " / ".join(x for x in (item["mobile"], item["email"]) if x) or "—"
        top_rows += (
            f'<tr><td class="num">{item["rank"]}</td>'
            f'<td class="name">{_esc(item["name"])}</td>'
            f'<td>{_esc(item["solutionLine"])}</td>'
            f'<td class="num">{item["insuredCount"]:g}</td>'
            f'<td class="num">{_esc(item["registeredCapital"])}</td>'
            f'{version_cells}'
            f'<td class="contact">{_esc(contact)}</td></tr>'
        )

    anchors = meta["anchors"]
    generated = meta["generatedAt"]

    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>智拓 · 政企企业评分模型报告（v1）</title>
<style>
  :root{{--ink:#1c2733;--muted:#65758b;--line:#e4ebf2;--bg:#f4f7fa;--card:#fff;
    --accent:#0b6bcb;--accent-d:#0957a6;}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);
    font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
  .wrap{{max-width:1120px;margin:0 auto;padding:28px 22px 60px}}
  header{{display:flex;align-items:center;gap:14px;margin-bottom:6px}}
  .logo{{width:44px;height:44px;border-radius:13px;color:#fff;font-weight:800;font-size:19px;
    display:grid;place-items:center;background:linear-gradient(145deg,#28a3e6,var(--accent-d))}}
  h1{{font-size:21px;margin:0}}
  .sub{{color:var(--muted);font-size:13px;margin-top:2px}}
  .kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:20px 0}}
  .kpi{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;
    box-shadow:0 6px 18px rgba(16,24,40,.04)}}
  .kpi-v{{font-size:24px;font-weight:800;color:var(--accent-d)}}
  .kpi-l{{font-size:13px;font-weight:600;margin-top:2px}}
  .kpi-h{{font-size:12px;color:var(--muted);margin-top:2px}}
  .card{{background:var(--card);border:1px solid var(--line);border-radius:16px;
    padding:18px 20px;margin-bottom:18px;box-shadow:0 8px 24px rgba(16,24,40,.04)}}
  h2{{font-size:16px;margin:0 0 4px}}
  .note{{color:var(--muted);font-size:12.5px;margin:0 0 12px}}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  th,td{{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}}
  th{{color:var(--muted);font-weight:600;background:#fafcfe;position:sticky;top:0}}
  td.num,th.num{{text-align:right;font-variant-numeric:tabular-nums}}
  td.lbl{{font-weight:600}}
  td.name{{font-weight:600}}
  td.contact{{color:var(--muted);font-size:12px}}
  .tag{{display:inline-block;min-width:20px;text-align:center;color:#fff;font-weight:700;
    font-size:11px;border-radius:999px;padding:1px 7px}}
  .mini{{font-size:10px;margin-left:5px;padding:0 5px}}
  .bar{{display:inline-block;width:160px;height:8px;background:#eef3f8;border-radius:999px;
    overflow:hidden;vertical-align:middle;margin-right:8px}}
  .bar i{{display:block;height:100%;border-radius:999px}}
  .barnum{{font-size:12px;color:var(--muted)}}
  .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
  .scroll{{max-height:560px;overflow:auto;border:1px solid var(--line);border-radius:12px}}
  .pill{{display:inline-block;background:#eaf4fb;color:var(--accent-d);border-radius:8px;
    padding:2px 9px;font-size:12px;font-weight:600;margin-right:6px}}
  .ok{{color:#1e874b;font-weight:700}} .bad{{color:#c0392b;font-weight:700}}
  footer{{color:var(--muted);font-size:12px;margin-top:22px;line-height:1.9}}
  @media(max-width:820px){{.grid2{{grid-template-columns:1fr}}}}
</style></head><body>
<div class="wrap">
  <header>
    <div class="logo">智</div>
    <div><h1>政企企业评分模型报告（v1）</h1>
      <div class="sub">区域 {_esc(meta['region'])} · 锚点模式 {_esc(meta['anchorMode'])} · 生成于 {_esc(generated)}</div>
    </div>
  </header>
  <div style="margin:8px 0 4px">
    <span class="pill">参保锚点 {anchors['size']:g} 人</span>
    <span class="pill">资本锚点 {anchors['capital']:g} 万元</span>
    <span class="pill">缺失策略 参保记0 / 资本中位数</span>
    <span class="pill">基准于 {_esc(meta['asOf'])}</span>
  </div>

  <div class="kpis">{kpi_html}</div>

  <div class="grid2">
    <div class="card"><h2>分级分布</h2>
      <p class="note">按 <b>{_esc(meta['persistVersion'])}</b> 分级：S≥80 / A70–79 / B60–69 / C50–59 / D&lt;50</p>
      <table><thead><tr><th>等级</th><th class="num">家数</th><th>分布</th><th class="num">占比</th></tr></thead>
      <tbody>{tier_rows}</tbody></table></div>

    <div class="card"><h2>方案线覆盖（16 条）</h2>
      <p class="note">映射自检：{'<span class="ok">通过</span>' if check['passed'] else '<span class="bad">未通过</span>'}
        · 兜底桶占比 {check['fallbackRatio'] * 100:.1f}%（阈值 ≤{check['maxFallbackRatio'] * 100:.0f}%）
        · 未映射 {check['unmappedCount']} 家</p>
      <div class="scroll"><table><thead><tr><th>方案线</th><th class="num">系数</th>
        <th class="num">家数</th><th class="num">占比</th><th class="num">均分</th><th class="num">S/A</th></tr></thead>
      <tbody>{line_rows}</tbody></table></div></div>
  </div>

  <div class="card"><h2>三版本区分度对比</h2>
    <p class="note">三版特征完全相同，仅权重不同 —— 同一批公司跑出三份可对比的名单。</p>
    <table><thead><tr><th>指标</th>{version_head}</tr></thead><tbody>{version_rows}</tbody></table>
    <p class="note" style="margin-top:12px"><b>版本间 Spearman 相关</b>（越低越互补）</p>
    <table><tbody>{corr_rows}</tbody></table>
  </div>

  <div class="card"><h2>特征区分度诊断</h2>
    <p class="note">「方案契合」的标准差是 <b>target leakage 的关键指标</b>：若标准差偏低、封顶占比偏高，
      说明 blob 可能混入了标签侧描述文本（本项目曾因此 83% 封顶）。</p>
    <table><thead><tr><th>特征</th><th class="num">均值</th><th class="num">标准差</th>
      <th>区分度</th><th class="num">封顶占比</th></tr></thead>
    <tbody>{feature_rows}</tbody></table>
  </div>

  <div class="card"><h2>拜访优先级名单 · Top {top_n}</h2>
    <p class="note">联系方式可直接分派。列 = 三版分数（含分级标签）。</p>
    <div class="scroll"><table><thead><tr><th class="num">#</th><th>企业名称</th><th>方案线</th>
      <th class="num">参保</th><th class="num">注册资本</th>
      {''.join(f'<th class="num">{_esc(v)}</th>' for v in versions)}
      <th>联系方式</th></tr></thead><tbody>{top_rows}</tbody></table></div>
  </div>

  <footer>
    <b>模型定位</b>：本模型基于工商登记数据输出「拜访优先级线索排名」，<b>不是成交概率预测</b>（线索筛选级）。<br/>
    <b>锚点自适应</b>：归一化分母是数据分布的函数；本次参保/资本锚点由当前数据集自动标定
      （来源：{_esc(meta['anchorSources']['size'])} / {_esc(meta['anchorSources']['capital'])}）。
      <code>auto</code> 模式下分数为区域内相对分，<b>不可跨区域直接比较</b>。<br/>
    <b>待校准</b>：三版 beta 与时效曲线均为业务先验，未经真实拜访转化数据验证；须用 300 家分层试点跑
      AUC / KS / Lift 回测后确定最优版本。<br/>
    <b>本报告全部数字由 scoring_metrics.json 动态派生</b>，无手写统计值。
  </footer>
</div></body></html>"""
    return html


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="智拓·政企企业评分模型")
    parser.add_argument("src", nargs="?", default=str(DEFAULT_SRC), help="企查查导出的 xlsx 路径")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    parser.add_argument("--top", type=int, default=60, help="HTML 报告展示的 Top N")
    parser.add_argument("--as-of", default=None, help="计算成立月数的基准日 YYYY-MM-DD")
    parser.add_argument("--region", default=None, help="覆盖配置中的区域")
    parser.add_argument("--no-lock", action="store_true", help="忽略锚点锁，强制重新标定")
    args = parser.parse_args()

    src = Path(args.src)
    if not src.is_file():
        print(f"[错误] 找不到输入文件：{src}")
        return 2
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    config = engine.load_config()
    if args.region:
        config["region"] = args.region
    print("=" * 70)
    print(f"智拓·政企企业评分模型 v1  |  区域={config['region']}  模式={config['anchor_mode']}")
    print("=" * 70)

    frame, header_row = read_companies(src)
    print(f"读取 {src.name}：表头在第 {header_row + 1} 行，共 {len(frame)} 家企业，{len(frame.columns)} 列")
    companies = frame_to_companies(frame)

    # 锚点锁：同区域复用，保证多次运行分数一致
    lock_path = out_dir / "scoring_anchors_lock.json"
    lock = {}
    if lock_path.is_file() and not args.no_lock:
        try:
            existing = json.loads(lock_path.read_text(encoding="utf-8"))
            if existing.get("region") == config["region"]:
                lock = existing
        except (OSError, ValueError):
            lock = {}

    as_of = engine.parse_date(args.as_of) if args.as_of else None
    scored, meta = engine.score_batch(companies, config, lock, as_of)
    check = engine.self_check(companies, config)

    # 写锚点锁
    if not lock or meta["anchorSources"]["size"] == "auto(本次标定)":
        lock_payload = {
            "region": config["region"],
            "anchor_mode": meta["anchorMode"],
            "size_anchor": meta["anchors"]["size"],
            "cap_anchor": meta["anchors"]["capital"],
            "size_pct": config["size_anchor"]["pct"],
            "cap_pct": config["cap_anchor"]["pct"],
        }
        lock_path.write_text(
            json.dumps(lock_payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"锚点标定完成 → {lock_path.name}")
    else:
        print(f"复用锚点锁（区域 {config['region']}）")

    print(f"  参保人数锚点 = {meta['anchors']['size']:g} 人  [{meta['anchorSources']['size']}]")
    print(f"  注册资本锚点 = {meta['anchors']['capital']:g} 万元  [{meta['anchorSources']['capital']}]")
    print(f"  映射自检：{'通过' if check['passed'] else '未通过'}"
          f"（兜底桶 {check['fallbackRatio'] * 100:.1f}%，未映射 {check['unmappedCount']} 家）")
    min_rows = config.get("self_check", {}).get("min_rows_for_stable_anchor", 200)
    if len(scored) < min_rows:
        print(f"  [警告] 样本仅 {len(scored)} 家，少于建议的 {min_rows} 家；"
              f"分位锚点不稳定，建议复用该区域已锁定的锚点。")

    metrics = build_metrics(scored, meta, config, check)

    # CSV 名单
    versions = list(engine.active_versions(config))
    rows = []
    for item in scored:
        row = {
            "拜访优先级": item["rank"],
            "企业名称": item["name"],
            "国标行业大类": item["industryMajor"],
            "企查查行业中类": item["industryMid"],
            "方案线": item["solutionLine"],
            "方案价值系数": item["solutionCoefficient"],
            "典型方案": item["typicalSolution"],
            "参保人数": item["insuredCount"],
            "注册资本": item["registeredCapital"],
            "成立日期": item["establishedAt"],
            "成立月数": item["foundedMonths"],
            "企业(机构)类型": item["entityType"],
            "有效手机号": item["mobile"],
            "邮箱": item["email"],
            "官网网址": item["website"],
            "注册地址": item["address"],
        }
        for version in versions:
            row[version] = item["scores"][version]
            row[version + "_Tier"] = item["tiers"][version]
        for feature in engine.FEATURES:
            row["特征_" + feature] = item["features"][feature]
        rows.append(row)
    csv_path = out_dir / "政企企业评分_三版本.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False, encoding="utf-8-sig")

    metrics_path = out_dir / "scoring_metrics.json"
    metrics_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    html_path = out_dir / "scoring_report.html"
    html_path.write_text(render_report(metrics, scored, config, args.top), encoding="utf-8")

    print("\n=== 三版本区分度对比 ===")
    stat_frame = pd.DataFrame(metrics["versions"]).T[
        ["mean", "std", "p10", "median", "p90", "head100", "tail100", "spread"]
    ]
    print(stat_frame.to_string())
    print("\n等级分布（基准版 {}）：{}".format(meta["persistVersion"], metrics["tierCounts"]))
    print("版本间相关：", metrics["correlation"])
    print("方案契合 std = {}（封顶占比 {:.1%}）".format(
        metrics["features"]["方案契合"]["std"], metrics["features"]["方案契合"]["ceilingRatio"]
    ))
    print("\n已生成产物：")
    for path in (csv_path, lock_path, metrics_path, html_path):
        print(f"  ✓ {path.relative_to(REPO)}  ({path.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
