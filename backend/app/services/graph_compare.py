"""图谱比对领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class GraphCompareService(BaseModuleService):
    MODULE = "graph_compare"
    ENTITY = "比对任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["任务编号", "基准图", "对比图", "比对维度", "新增实体", "删除实体", "变更关系", "比对时间"]
    REQUIRED_FIELDS = ["任务编号", "基准图", "对比图"]
    STATUS_ORDER = ["待执行", "比对中", "已出报告", "比对失败"]
    ACTION_RULES = {"启动比对": "比对中", "确认报告": "已出报告", "标记失败": "比对失败"}
    NEGATIVE_ACTIONS = []

    # ===== 跨模块数据流实现 =====
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        # 链路 L14: graph_compare → report_gen（比对报告触发报表生成）
        if entry.get("status") == "已出报告":
            report_msg = self._push_to_report_gen(entry)
            message += "；" + report_msg
        # 链路 L15: graph_compare → quality_audit（比对结果触发质量审计）
        if entry.get("status") == "已出报告":
            audit_msg = self._push_to_quality_audit(entry)
            message += "；" + audit_msg
        return entry, message

    def _push_to_report_gen(self, entry: dict[str, Any]) -> str:
        rows = store.rows("report_gen")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rp_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "报告编号": f"REPO-CMP-{next_id:04d}",
            "报告类型": "graph-compare-report",
            "数据范围": f"{entry.get('基准图','')}-vs-{entry.get('对比图','')}",
            "图表配置": "diff-table,chord-diagram",
            "输出格式": "html",
            "生成周期": "on-compare-done",
            "分发列表": "",
            "最近生成": str(entry.get("比对时间", "2026-11-18")),
        }
        rows.append(rp_entry)
        return f"已生成比对报表 {rp_entry['报告编号']}"

    def _push_to_quality_audit(self, entry: dict[str, Any]) -> str:
        rows = store.rows("quality_audit")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        qa_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待审计",
            "pending": True,
            "abnormal": False,
            "报告编号": f"QUAL-CMP-{next_id:04d}",
            "审计范围": f"graph-compare-{entry.get('任务编号','')}",
            "实体准确率": "from-compare-delta",
            "关系完整率": "from-compare-delta",
            "冗余度": "estimated-low",
            "更新时效": "实时",
            "合规结论": "待审计",
            "审计时间": str(entry.get("比对时间", "2026-11-18")),
        }
        rows.append(qa_entry)
        return f"已触发质量审计 {qa_entry['报告编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["report_gen", "quality_audit"]
        # 比对清单与图谱演化按同一份血缘来源（来源链路编号）取数
        rows = store.rows(self.MODULE)
        base["lineage_sourced"] = sum(1 for row in rows if row.get("来源链路编号"))
        base["claimable_todos"] = sum(1 for row in rows if row.get("pending"))
        return base