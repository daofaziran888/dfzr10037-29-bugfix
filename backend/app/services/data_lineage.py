"""数据血缘领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。

跨模块写入约定（链路 L12/L13），修复「同一血缘来源在演化列表和比对工作台
重复生成待办」的问题：

- 提交验证：只在图谱演化留一份「验证取证」记录（pending=False，仅留存可查，
  不可领取、不可流转）；未确认链路不产生可执行的图谱改动，也不触发图谱比对。
- 确认链路：在图谱演化留一份「确认改动」记录（pending=True，可领取待办），
  并把确认结果同步到图谱比对清单；两处都按同一份血缘来源（来源链路编号）取数。
- 同一链路的同一动作重复提交只保留一笔有效结果；血缘数据有更新时在原记录上
  刷新，不新增重复待办，比对清单不残留旧待办。
- 历史血缘编号兼容：旧格式记录（无溯源字段）按链路编号/实体对识别并接管，
  多余的历史重复记录合并下线，不再出现在可领取待办中。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 下游记录溯源字段：血缘来源 + 写入动作，让验证与确认的演化数据可区分
SOURCE_LINK_FIELD = "来源链路编号"
SOURCE_ACTION_FIELD = "来源动作"
DATA_ROLE_FIELD = "数据角色"
DEDUP_NOTE_FIELD = "去重说明"

ACTION_VERIFY = "提交验证"
ACTION_CONFIRM = "确认链路"

ROLE_EVIDENCE = "验证取证"
ROLE_CONFIRMED = "确认改动"


class DataLineageService(BaseModuleService):
    MODULE = "data_lineage"
    ENTITY = "血缘链路"
    OPERATION_TERM = "转换"
    LIST_FIELDS = ["链路编号", "上游实体", "下游实体", "转换描述", "关联字段", "影响层级", "链路类型", "最后更新"]
    REQUIRED_FIELDS = ["链路编号", "上游实体", "下游实体"]
    STATUS_ORDER = ["自动推导", "已确认", "待验证", "已废弃"]
    ACTION_RULES = {"确认链路": "已确认", "提交验证": "待验证", "废弃链路": "已废弃"}
    NEGATIVE_ACTIONS = []

    # ===== 跨模块数据流实现 =====
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == ACTION_VERIFY:
            # 链路 L12（验证侧）：只留取证记录，未确认链路不生成可执行改动、不触发比对
            message += "；" + self._record_verification_evidence(entry)
        elif action == ACTION_CONFIRM:
            # 链路 L12（确认侧）：确认改动写入图谱演化，生成可领取待办
            message += "；" + self._push_to_graph_evolve(entry)
            # 链路 L13：确认结果同步到图谱比对清单，与演化记录共用同一血缘来源
            message += "；" + self._push_to_graph_compare(entry)
        return entry, message

    # ----- 溯源匹配：新格式按溯源字段，历史格式按链路编号/实体对兼容识别 -----
    @staticmethod
    def _lineage_no(entry: dict[str, Any]) -> str:
        return str(entry.get("链路编号", ""))

    def _is_confirm_evolve_row(self, row: dict[str, Any], lineage_no: str) -> bool:
        if DEDUP_NOTE_FIELD in row:
            return False  # 已合并下线的重复记录不再参与认领
        source = str(row.get(SOURCE_LINK_FIELD, "") or "")
        if source:
            # 新格式：同一条血缘来源的确认记录（验证取证记录不算）
            return source == lineage_no and row.get(SOURCE_ACTION_FIELD) != ACTION_VERIFY
        # 历史格式：旧代码把链路编号写在变更描述里，按历史血缘编号兼容识别
        return str(row.get("变更描述", "")) == f"血缘链路{lineage_no}已变更"

    def _is_lineage_compare_row(self, row: dict[str, Any], entry: dict[str, Any], lineage_no: str) -> bool:
        if DEDUP_NOTE_FIELD in row:
            return False
        source = str(row.get(SOURCE_LINK_FIELD, "") or "")
        if source:
            return source == lineage_no
        # 历史格式：比对维度 + 上下游实体对，对应同一条血缘链路
        return (
            str(row.get("比对维度", "")) == "lineage-change-diff"
            and str(row.get("基准图", "")) == str(entry.get("上游实体", ""))
            and str(row.get("对比图", "")) == str(entry.get("下游实体", ""))
        )

    @staticmethod
    def _merge_duplicates(rows: list[dict[str, Any]], keep_no: str) -> int:
        """同源重复记录合并下线：保留数据可审计，但不再出现在可领取待办中。"""
        for row in rows:
            row["pending"] = False
            row["abnormal"] = True
            row[DEDUP_NOTE_FIELD] = f"与{keep_no}同源重复，已合并去重"
        return len(rows)

    # ----- 验证侧：取证留档，可查询、不可执行 -----
    def _record_verification_evidence(self, entry: dict[str, Any]) -> str:
        lineage_no = self._lineage_no(entry)
        rows = store.rows("graph_evolve")
        existing = next(
            (row for row in rows
             if str(row.get(SOURCE_LINK_FIELD, "") or "") == lineage_no
             and row.get(SOURCE_ACTION_FIELD) == ACTION_VERIFY),
            None,
        )
        if existing is not None:
            if str(existing.get("演化时间", "")) == str(entry.get("最后更新", "")):
                return f"验证取证{existing['记录编号']}已存在，重复提交未重复留档"
            # 重复提交验证：在原取证记录上刷新留档内容，不新增记录
            existing["影响实体"] = str(entry.get("下游实体", ""))
            existing["影响关系"] = str(entry.get("关联字段", ""))
            existing["变更描述"] = f"血缘链路{lineage_no}提交验证，取证留档（未确认，不构成图谱改动）"
            existing["演化时间"] = str(entry.get("最后更新", "2026-11-18"))
            existing["pending"] = False
            return f"验证取证{existing['记录编号']}已存在，留档内容已刷新（未确认链路不产生可执行图谱改动）"
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        ev_entry: dict[str, Any] = {
            "id": next_id,
            "status": "已记录",
            "pending": False,  # 取证留档仅可查询，不进入可领取待办
            "abnormal": False,
            "记录编号": f"GRAP-EVOL-{next_id:04d}",
            "变更类型": f"lineage-verify-{entry.get('链路类型', '')}",
            "影响实体": str(entry.get("下游实体", "")),
            "影响关系": str(entry.get("关联字段", "")),
            "版本标签": "未确认取证",
            "变更描述": f"血缘链路{lineage_no}提交验证，取证留档（未确认，不构成图谱改动）",
            "回滚版本": "无",
            "演化时间": str(entry.get("最后更新", "2026-11-18")),
            SOURCE_LINK_FIELD: lineage_no,
            SOURCE_ACTION_FIELD: ACTION_VERIFY,
            DATA_ROLE_FIELD: ROLE_EVIDENCE,
        }
        rows.append(ev_entry)
        return f"验证取证已留档 {ev_entry['记录编号']}（未确认链路不产生可执行图谱改动，不触发图谱比对）"

    # ----- 确认侧：一份确认只写一笔有效演化记录 -----
    def _push_to_graph_evolve(self, entry: dict[str, Any]) -> str:
        lineage_no = self._lineage_no(entry)
        rows = store.rows("graph_evolve")
        candidates = [row for row in rows if self._is_confirm_evolve_row(row, lineage_no)]
        if not candidates:
            next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            ev_entry: dict[str, Any] = {
                "id": next_id,
                "status": "已记录",
                "pending": True,  # 确认改动：可领取待办
                "abnormal": False,
                "记录编号": f"GRAP-EVOL-{next_id:04d}",
                "变更类型": f"lineage-confirm-{entry.get('链路类型', '')}",
                "影响实体": str(entry.get("下游实体", "")),
                "影响关系": str(entry.get("关联字段", "")),
                "版本标签": f"v{next_id}.0",
                "变更描述": f"血缘链路{lineage_no}已确认，同步可执行图谱改动",
                "回滚版本": f"v{next_id - 1}.0",
                "演化时间": str(entry.get("最后更新", "2026-11-18")),
                SOURCE_LINK_FIELD: lineage_no,
                SOURCE_ACTION_FIELD: ACTION_CONFIRM,
                DATA_ROLE_FIELD: ROLE_CONFIRMED,
            }
            rows.append(ev_entry)
            return f"已记录图谱演化 {ev_entry['记录编号']}（确认改动，可领取待办）"
        chosen = candidates[0]
        merged = self._merge_duplicates(candidates[1:], chosen["记录编号"])
        adopted = SOURCE_ACTION_FIELD not in chosen  # 历史记录被接管，补齐溯源字段
        chosen[SOURCE_LINK_FIELD] = lineage_no
        chosen[SOURCE_ACTION_FIELD] = ACTION_CONFIRM
        chosen[DATA_ROLE_FIELD] = ROLE_CONFIRMED
        chosen["变更类型"] = f"lineage-confirm-{entry.get('链路类型', '')}"
        note = "，已兼容历史血缘编号接管旧记录" if adopted else ""
        if merged:
            note += f"，合并同源重复记录{merged}笔"
        if str(chosen.get("演化时间", "")) != str(entry.get("最后更新", "")):
            # 血缘已有更新：在原记录上刷新并重新置为可领取待办，不新增重复记录
            chosen["影响实体"] = str(entry.get("下游实体", ""))
            chosen["影响关系"] = str(entry.get("关联字段", ""))
            chosen["变更描述"] = f"血缘链路{lineage_no}已确认，同步可执行图谱改动"
            chosen["演化时间"] = str(entry.get("最后更新", "2026-11-18"))
            chosen["status"] = "已记录"
            chosen["pending"] = True
            chosen["abnormal"] = False
            return f"图谱演化 {chosen['记录编号']} 已同步血缘最新状态{note}，未新增重复待办"
        return f"图谱演化 {chosen['记录编号']} 为同一次确认的有效结果，重复确认未重复写入{note}"

    # ----- 确认侧：确认结果同步到比对清单，同一来源只留一份待办 -----
    def _push_to_graph_compare(self, entry: dict[str, Any]) -> str:
        lineage_no = self._lineage_no(entry)
        rows = store.rows("graph_compare")
        candidates = [row for row in rows if self._is_lineage_compare_row(row, entry, lineage_no)]
        if not candidates:
            next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            cmp_entry: dict[str, Any] = {
                "id": next_id,
                "status": "待执行",
                "pending": True,
                "abnormal": False,
                "任务编号": f"GRAP-CMP-{next_id:04d}",
                "基准图": str(entry.get("上游实体", "")),
                "对比图": str(entry.get("下游实体", "")),
                "比对维度": "lineage-change-diff",
                "新增实体": "auto-calc",
                "删除实体": "auto-calc",
                "变更关系": "auto-calc",
                "比对时间": str(entry.get("最后更新", "2026-11-18")),
                SOURCE_LINK_FIELD: lineage_no,
                SOURCE_ACTION_FIELD: ACTION_CONFIRM,
            }
            rows.append(cmp_entry)
            return f"已触发图谱比对 {cmp_entry['任务编号']}（与演化记录同源：{lineage_no}）"
        chosen = candidates[0]
        merged = self._merge_duplicates(candidates[1:], chosen["任务编号"])
        adopted = SOURCE_LINK_FIELD not in chosen
        chosen[SOURCE_LINK_FIELD] = lineage_no
        chosen[SOURCE_ACTION_FIELD] = ACTION_CONFIRM
        note = "，已兼容历史血缘编号接管旧任务" if adopted else ""
        if merged:
            note += f"，合并同源重复任务{merged}笔"
        if str(chosen.get("比对时间", "")) != str(entry.get("最后更新", "")):
            # 血缘已有更新：刷新原待办到最新状态，不在清单里残留旧待办
            chosen["基准图"] = str(entry.get("上游实体", ""))
            chosen["对比图"] = str(entry.get("下游实体", ""))
            chosen["比对时间"] = str(entry.get("最后更新", "2026-11-18"))
            chosen["status"] = "待执行"
            chosen["pending"] = True
            chosen["abnormal"] = False
            return f"图谱比对 {chosen['任务编号']} 已同步血缘最新状态{note}，原待办已刷新未重复生成"
        return f"图谱比对 {chosen['任务编号']} 与本次确认同源且已是最新，重复确认未重复生成{note}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["graph_evolve", "graph_compare"]
        return base
