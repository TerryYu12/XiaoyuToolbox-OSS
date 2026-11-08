# -*- coding: utf-8 -*-
"""AI 决策报告生成引擎 (AI Decision Report Generator)

将磁盘清理扫描结果、智能画像溯源与工程归属属性，格式化为结构化、
上下文完备的 Markdown 审计报告。Master 可一键复制该报告发给大模型（Claude/Gemini/DeepSeek/GPT），
由 AI 深度评估每一个目录的保留价值，给出绝对稳妥的保留/清理裁决与一键批处理命令。
"""
from __future__ import annotations
import os
import time
from typing import List, Dict, Any, Optional

from .scanner import human
from .analyzer import Candidate


def build_ai_decision_report(candidates: List[Candidate], roots: List[str],
                             drive_meta: Optional[List[Dict[str, Any]]] = None,
                             is_admin: bool = False) -> str:
    """生成面向 AI 深度裁决的高质量 Markdown 决策报告。"""
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    total_size = sum(c.size for c in candidates)
    total_count = len(candidates)

    proj_cands = [c for c in candidates if getattr(c, "is_project", False)]
    safe_cands = [c for c in candidates if c.risk == "safe" and not getattr(c, "is_project", False)]
    review_cands = [c for c in candidates if c.risk == "review" and not getattr(c, "is_project", False)]
    caution_cands = [c for c in candidates if c.risk == "caution"]

    lines = []
    lines.append("# 🤖 磁盘清理与资产安全 AI 决策审计报告")
    lines.append("")
    lines.append("> **生成时间**：" + now_str)
    lines.append(f"> **扫描目标**：`{', '.join(roots) if roots else '本机磁盘'}`")
    lines.append(f"> **候选总计**：**{total_count}** 项 | 逻辑释放潜力：**{human(total_size)}**")
    lines.append(f"> **管理员权限**：{'已提权运行 (可清理系统底层垃圾)' if is_admin else '普通用户运行'}")
    lines.append("")
    lines.append("## 🎯 给 AI 助手的审计指令 (Prompt to AI)")
    lines.append("```text")
    lines.append("你是一位资深的系统运维与跨平台全栈架构专家。")
    lines.append("请仔细审阅以下从用户电脑真实扫描出的【待清理候选清单】。重点审查以下风险：")
    lines.append("1. 本地代码工程与 Git 仓库：严格排查是否影响正在活跃开发的代码、是否包含未提交文件、重建依赖是否需要特殊网络/私有 Token；")
    lines.append("2. AI 模型与预训练权重：评估体积大、下载昂贵的大模型文件（HuggingFace / Ollama / PyTorch），确认是否仍有使用价值；")
    lines.append("3. 系统与日常软件缓存：识别可零风险清除的纯临时垃圾；")
    lines.append("请在审阅后，按以下三部分给出明确裁决：")
    lines.append("  [1] 【推荐直接清理清单】（注明预计释放体积，并给出确认理由）")
    lines.append("  [2] 【强烈建议保留清单】（详细说明为何不能动、动了会有何后果）")
    lines.append("  [3] 【一键安全清理执行建议】（提供可直接执行的精简路径或 CLI 命令）")
    lines.append("```")
    lines.append("")

    # 各盘占用概况
    if drive_meta:
        lines.append("## 📊 一、各盘扫描概况")
        lines.append("| 盘符 | 磁盘占用 | 扫描耗时 | 文件总数 | 发现可清理项 | 释放潜力 |")
        lines.append("|---|---|---|---|---|---|")
        for m in drive_meta:
            lines.append(f"| {m.get('drive','?')} 盘 | {m.get('total_h','-')} | {m.get('elapsed',0)}s | {m.get('nfiles',0):,} 个 | {m.get('cand_count',0)} 项 | **{m.get('cand_size_h','-')}** |")
        lines.append("")

    # 本地工程关联区 (高风险重点审查区)
    lines.append(f"## 🏢 二、本地代码工程相关项 ({len(proj_cands)} 项，共 {human(sum(c.size for c in proj_cands))})")
    if proj_cands:
        lines.append("*⚠️ 注意：以下清理项位于用户的本地开发工程或代码仓库内。虽然清理不会删除源代码文件（保存在 src/ 等目录下），但会清空本地依赖库或编译产物。*")
        lines.append("")
        for idx, c in enumerate(proj_cands, 1):
            sz_str = getattr(c, "size_h", human(getattr(c, "size", 0)))
            lines.append(f"### {idx}. `[{c.project_name or '本地工程'}]` {c.title}")
            lines.append(f"- **绝对路径**：`{c.path}`")
            lines.append(f"- **占用体积**：**{sz_str}** ({c.size:,} 字节) {'(目录 📁)' if c.is_dir else '(单文件 📄)'}")
            lines.append(f"- **归属工程根**：`{c.project_root or '未明确工程根'}`")
            lines.append(f"- **所属应用/框架**：{c.owner_app or '前端/通用工程'}")
            lines.append(f"- **缓存性质**：{c.cache_type or '项目依赖/构建产物'}")
            lines.append(f"- **详细用途**：{c.purpose or c.description or '工程日常编译产物或第三方依赖库'}")
            lines.append(f"- **清理后果与重建代价**：{c.recreate_cost or '无损工程代码。下次运行需重新安装依赖或编译。'}")
            lines.append(f"- **规则建议**：{c.advice or '活跃项目建议保留；已归档工程可清理。'}")
            if c.insight:
                lines.append(f"- **智能探针洞察**：`{c.insight}`")
            lines.append("")
    else:
        lines.append("*(未检测到本地代码工程相关的待清理项，安全。)*\n")

    # AI 大模型与研发缓存区
    ai_cands = [c for c in candidates if c.category == "ai" or "huggingface" in c.path.lower() or "ollama" in c.path.lower()]
    lines.append(f"## 🧠 三、AI 模型权重与大模型缓存 ({len(ai_cands)} 项，共 {human(sum(c.size for c in ai_cands))})")
    if ai_cands:
        lines.append("*⚠️ 注意：大模型文件通常体积巨大且下载流量高昂，请结合自身实际使用频率裁决。*")
        lines.append("")
        for idx, c in enumerate(ai_cands, 1):
            sz_str = getattr(c, "size_h", human(getattr(c, "size", 0)))
            lines.append(f"### {idx}. {c.title}")
            lines.append(f"- **绝对路径**：`{c.path}`")
            lines.append(f"- **占用体积**：**{sz_str}**")
            lines.append(f"- **所属工具**：{c.owner_app or 'AI 工具链'}")
            lines.append(f"- **详细用途**：{c.purpose or '模型权重或推理缓存'}")
            lines.append(f"- **清理后果**：{c.recreate_cost or '删除后若代码重新推理需耗时重新下载大量流量。'}")
            lines.append(f"- **建议**：{c.advice or '只有确定不再使用的模型才建议删除。'}")
            if c.insight:
                lines.append(f"- **包含模型**：`{c.insight}`")
            lines.append("")
    else:
        lines.append("*(未检测到大体积 AI 模型缓存。)*\n")

    # 确定可安全清理的系统与日常缓存 (表格精炼呈现)
    lines.append(f"## ⚡ 四、系统与应用安全垃圾项 ({len(safe_cands)} 项，共 {human(sum(c.size for c in safe_cands))})")
    lines.append("*此类通常属于已安装更新残留、临时交换文件、网页离线静态副本，删后完全无损，下次按需重建。*")
    lines.append("")
    if safe_cands:
        lines.append("| 序号 | 规则名称 | 体积 | 绝对路径 | 详细用途 | 影响与重建 |")
        lines.append("|---|---|---|---|---|---|")
        for idx, c in enumerate(safe_cands[:40], 1):
            sz_str = getattr(c, "size_h", human(getattr(c, "size", 0)))
            purp = (c.purpose or c.description or '临时交换文件').replace("|", " ")
            cost = (c.recreate_cost or '完全无损，自动重建').replace("|", " ")
            lines.append(f"| {idx} | {c.title} | **{sz_str}** | `{c.path}` | {purp} | {cost} |")
        if len(safe_cands) > 40:
            lines.append(f"| ... | 另有 {len(safe_cands)-40} 个小项省略呈现 | ... | ... | ... | ... |")
        lines.append("")
    else:
        lines.append("*(无安全项)*\n")

    # 建议确认与谨慎项
    other_review = [c for c in review_cands if c not in ai_cands]
    lines.append(f"## ⚠️ 五、建议审阅项与备份档案 ({len(other_review)} 项，共 {human(sum(c.size for c in other_review))})")
    if other_review:
        lines.append("| 序号 | 规则名称 | 体积 | 绝对路径 | 建议说明 |")
        lines.append("|---|---|---|---|---|")
        for idx, c in enumerate(other_review, 1):
            sz_str = getattr(c, "size_h", human(getattr(c, "size", 0)))
            adv = (c.advice or c.purpose or '建议确认后再清理').replace("|", " ")
            lines.append(f"| {idx} | {c.title} | **{sz_str}** | `{c.path}` | {adv} |")
        lines.append("")
    else:
        lines.append("*(无其它建议审阅项)*\n")

    # 报告收尾
    lines.append("---")
    lines.append("## 📝 请 AI 专家开始裁决")
    lines.append("请通读上述各区清单，综合考虑 Master 的磁盘健康度与研发资产安全性，给出精确到项的决策指导方案。")

    return "\n".join(lines)


def save_ai_report_to_disk(markdown_text: str) -> str:
    """将报告保存到本地磁盘并返回绝对文件路径。"""
    base_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                            "diskcleaner", "reports")
    os.makedirs(base_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(base_dir, f"ai_cleanup_decision_{ts}.md")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(markdown_text)
    return filepath
