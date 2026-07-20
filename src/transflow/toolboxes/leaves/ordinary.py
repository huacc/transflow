"""实现 P9 六个证据不足普通叶的结构所有权与有界回退骨架。"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path

from transflow.domain.common import content_sha256
from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.domain.pages import PageExecutionContext
from transflow.domain.text_inventory import InventoryDisposition
from transflow.domain.toolbox import (
    Decision,
    DecisionDisposition,
    Finding,
    PagePatch,
    PatchOperation,
    ToolboxDescriptor,
)
from transflow.domain.translation import TranslationBatch, TranslationUnit
from transflow.pdf_kernel.facts import ExtractedPageFacts, RectTuple
from transflow.pdf_kernel.patch import patch_operation_hash, probe_operation_fit
from transflow.pdf_kernel.text_inventory import (
    CanonicalTextRecord,
    canonical_text_records,
    freeze_page_text_inventory,
)
from transflow.toolboxes.contracts import (
    TOOLBOX_CONTRACT_VERSION,
    PageTemplate,
    ToolboxCandidate,
    ToolboxJudgement,
    ToolboxLayoutPlan,
    TranslationDispatch,
)
from transflow.toolboxes.leaves.ordinary_policy import P9OrdinaryLeafPolicy

LOGGER = logging.getLogger("transflow.toolboxes.leaves.ordinary")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
PAGE_NUMBER = re.compile(r"^\s*(?:[ivxlcdm]+|\d+)(?:\s*/\s*\d+)?\s*$", re.IGNORECASE)
LEADER = re.compile(r"^[\s.·•…_-]+$")


@dataclass(frozen=True, slots=True)
class P9OwnedAtom:
    """记录一个可编辑文本原子及其唯一回退组。"""

    object_id: str
    group_id: str
    source_text: str
    bbox: RectTuple


@dataclass(frozen=True, slots=True)
class P9LeafSnapshot:
    """保存单页普通叶的 owner 清单、显式 KEEP_SOURCE 与受保护对象哈希。"""

    route: str
    facts: ExtractedPageFacts
    atoms: tuple[P9OwnedAtom, ...]
    keep_source_ids: tuple[str, ...]
    protected_hash: str

    @property
    def owner_coverage_complete(self) -> bool:
        """验证每个可编辑文本恰好属于一个 owner 或显式 KEEP_SOURCE。"""

        editable = {item.object_id for item in canonical_text_records(self.facts)}
        owned = tuple(item.object_id for item in self.atoms)
        return (
            len(owned) == len(set(owned))
            and set(owned).isdisjoint(self.keep_source_ids)
            and set(owned) | set(self.keep_source_ids) == editable
        )


def _center(rect: RectTuple) -> tuple[float, float]:
    """返回矩形中心点，供全部结构叶使用同一机械几何口径。"""

    return ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)


def _inside(point: tuple[float, float], rect: RectTuple) -> bool:
    """判断一个中心点是否位于指定闭合矩形内。"""

    return rect[0] <= point[0] <= rect[2] and rect[1] <= point[1] <= rect[3]


def _intersects(first: RectTuple, second: RectTuple) -> bool:
    """判断两个页面矩形是否存在正面积交集。"""

    return not (
        first[2] <= second[0]
        or second[2] <= first[0]
        or first[3] <= second[1]
        or second[3] <= first[1]
    )


def _text_objects(facts: ExtractedPageFacts) -> tuple[CanonicalTextRecord, ...]:
    """返回 Kernel 机械选定的原生文字层级，不读取 Route、样本身份或 OCR。"""

    return canonical_text_records(facts)


def _axis_clusters(values: tuple[float, ...], tolerance: float) -> tuple[int, ...]:
    """按绝对容差对排序后的坐标形成稳定簇编号。"""

    if not values:
        return ()
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    assignments = [0] * len(values)
    centers: list[float] = []
    counts: list[int] = []
    for original_index, value in ordered:
        if not centers or abs(value - centers[-1]) > tolerance:
            centers.append(value)
            counts.append(1)
        else:
            count = counts[-1] + 1
            centers[-1] = (centers[-1] * counts[-1] + value) / count
            counts[-1] = count
        assignments[original_index] = len(centers) - 1
    return tuple(assignments)


class StructuredOrdinaryLeafToolbox:
    """为六个普通叶提供相同六阶段外形，叶语义只在分析方法中实现。"""

    def __init__(
        self,
        route: str,
        policy: P9OrdinaryLeafPolicy,
        font_path: Path,
        atomic_scope: str,
    ) -> None:
        """绑定 Route、集中配置、受控字体和失败时的最小原子范围。"""

        self._route = route
        self._policy = policy
        self._font_path = font_path.resolve()
        self._atomic_scope = atomic_scope
        self._descriptor = ToolboxDescriptor(route, route, TOOLBOX_CONTRACT_VERSION, route)
        self._snapshots: dict[str, P9LeafSnapshot] = {}
        self._snapshots_by_plan: dict[str, P9LeafSnapshot] = {}

    @property
    def descriptor(self) -> ToolboxDescriptor:
        """返回当前叶的稳定 Toolbox、Route、合同和 owner 身份。"""

        return self._descriptor

    def prepare(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
    ) -> PageTemplate:
        """从直接页面事实生成唯一 owner/KEEP_SOURCE 清单。"""

        LOGGER.info(
            "调用 P9 普通叶 prepare，意图=建立结构所有权 route=%s page_no=%s",
            self._route,
            context.page_no,
        )
        if (
            facts.page.source_hash != context.source_hash
            or facts.page.page_no != context.page_no
            or facts.page.geometry_hash != context.geometry_hash
        ):
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "P9 普通叶页面事实漂移")
        atoms, keep_source_ids = self._analyze(facts)
        template_id = f"{self._route.replace('.', '-')}-{facts.page_identity[:24]}"
        snapshot = P9LeafSnapshot(
            route=self._route,
            facts=facts,
            atoms=atoms,
            keep_source_ids=keep_source_ids,
            protected_hash=facts.locked_objects_hash,
        )
        if not snapshot.owner_coverage_complete:
            raise DomainContractError(
                ErrorCode.PATCH_OWNER_VIOLATION,
                "P9 普通叶 owner 未闭合",
            )
        self._snapshots[template_id] = snapshot
        return PageTemplate(
            template_id=template_id,
            context=context,
            facts_hash=facts.kernel_facts_hash,
            owner=self._route,
            object_ids=tuple(item.object_id for item in atoms),
        )

    def _analyze(
        self,
        facts: ExtractedPageFacts,
    ) -> tuple[tuple[P9OwnedAtom, ...], tuple[str, ...]]:
        """由具体叶实现结构所有权分析。"""

        raise NotImplementedError

    def audit_snapshot(self, template: PageTemplate) -> P9LeafSnapshot:
        """返回测试和 Gate 可审计的不可变 owner 清单。"""

        return self._snapshots[template.template_id]

    def build_translation_request(self, template: PageTemplate) -> TranslationBatch | None:
        """按 owner 阅读顺序构造单页 Batch；无安全 owner 时显式零翻译。"""

        LOGGER.info(
            "调用 P9 普通叶翻译请求构造，意图=生成稳定 unit route=%s page_no=%s",
            self._route,
            template.context.page_no,
        )
        snapshot = self._snapshots[template.template_id]
        if not snapshot.atoms:
            return None
        units = tuple(
            TranslationUnit(
                unit_id=hashlib.sha256(
                    f"{snapshot.facts.page_identity}\0{atom.object_id}\0{atom.group_id}".encode(
                        "ascii"
                    )
                ).hexdigest(),
                page_no=template.context.page_no,
                ordinal=ordinal,
                source_text=atom.source_text,
                region_id=atom.group_id,
            )
            for ordinal, atom in enumerate(snapshot.atoms)
        )
        return TranslationBatch(
            batch_id=f"batch-{template.context.run_id}-p{template.context.page_no:04d}-{self._route}",
            source_language=self._policy.source_language,
            target_language=self._policy.target_language,
            units=units,
        )

    def consume_translation_bundle(
        self,
        template: PageTemplate,
        dispatch: TranslationDispatch,
    ) -> ToolboxLayoutPlan:
        """把严格对齐译文变为声明 Patch，能力失败立即请求安全回退。"""

        LOGGER.info(
            "调用 P9 普通叶译文消费，意图=构造声明 Patch route=%s page_no=%s",
            self._route,
            template.context.page_no,
        )
        snapshot = self._snapshots[template.template_id]
        plan_id = f"plan-{template.template_id}"
        self._snapshots_by_plan[plan_id] = snapshot
        if dispatch.failure is not None:
            finding = Finding(
                f"{plan_id}-translation-failure",
                dispatch.failure.code,
                "HARD",
                (template.template_id,),
            )
            return ToolboxLayoutPlan(plan_id, self._route, None, (finding,), True)
        if dispatch.skip_reason is not None:
            return ToolboxLayoutPlan(plan_id, self._route, None, (), passthrough_requested=True)
        if dispatch.batch is None or dispatch.bundle is None:
            raise DomainContractError(ErrorCode.INVALID_TRANSLATION_BUNDLE, "P9 普通叶缺少译文")
        translated = {item.unit_id: item.translated_text for item in dispatch.bundle.units}
        operations: list[PatchOperation] = []
        for unit, atom in zip(dispatch.batch.units, snapshot.atoms, strict=True):
            text = translated[unit.unit_id]
            font_size = max(
                self._policy.minimum_font_size,
                min(
                    self._policy.maximum_font_size,
                    round((atom.bbox[3] - atom.bbox[1]) * self._policy.font_scale, 2),
                ),
            )
            operations.append(
                PatchOperation(
                    operation_id=f"op-{unit.unit_id[:20]}",
                    region_id=atom.group_id,
                    kind="replace_text",
                    payload_hash=patch_operation_hash(
                        owner=self._route,
                        target_object_ids=(atom.object_id,),
                        rect=atom.bbox,
                        replacement_text=text,
                        font_id=self._policy.font_id,
                        font_size=font_size,
                    ),
                    owner=self._route,
                    target_object_ids=(atom.object_id,),
                    rect=atom.bbox,
                    replacement_text=text,
                    font_id=self._policy.font_id,
                    font_size=font_size,
                )
            )
        patch = PagePatch(
            patch_id=f"patch-{snapshot.facts.page_identity[:24]}-{self._route}",
            source_hash=template.context.source_hash,
            page_no=template.context.page_no,
            geometry_hash=template.context.geometry_hash,
            owner=self._route,
            operations=tuple(operations),
        )
        return ToolboxLayoutPlan(plan_id, self._route, patch, ())

    def validate_patch(self, template: PageTemplate, patch: PagePatch) -> tuple[str, ...]:
        """公开执行 owner/cell/column/clip/source guard，供负向 Gate 构造真实非法 Patch。"""

        return self._validate_snapshot_patch(self._snapshots[template.template_id], patch)

    def _validate_snapshot_patch(
        self,
        snapshot: P9LeafSnapshot,
        patch: PagePatch,
    ) -> tuple[str, ...]:
        """验证 Patch 不跨源、不跨 owner、不跨回退组且不修改 KEEP_SOURCE。"""

        violations: list[str] = []
        facts = snapshot.facts
        if (
            patch.source_hash != facts.page.source_hash
            or patch.page_no != facts.page.page_no
            or patch.geometry_hash != facts.page.geometry_hash
        ):
            violations.append("P9_SOURCE_BINDING_REJECTED")
        if patch.owner != self._route:
            violations.append("P9_CROSS_OWNER_REJECTED")
        atom_by_id = {item.object_id: item for item in snapshot.atoms}
        seen: set[str] = set()
        for operation in patch.operations:
            if operation.owner != self._route:
                violations.append("P9_CROSS_OWNER_REJECTED")
            if len(operation.target_object_ids) != 1:
                violations.append("P9_TARGET_CARDINALITY_REJECTED")
                continue
            target = operation.target_object_ids[0]
            atom = atom_by_id.get(target)
            if atom is None or target in snapshot.keep_source_ids:
                violations.append("P9_UNOWNED_TARGET_REJECTED")
                continue
            if target in seen:
                violations.append("P9_DUPLICATE_TARGET_REJECTED")
            seen.add(target)
            if operation.region_id != atom.group_id:
                violations.append("P9_CROSS_GROUP_REJECTED")
            if operation.rect != atom.bbox:
                violations.append("P9_CROSS_CLIP_REJECTED")
            if operation.rect is not None and any(
                _intersects(operation.rect, protected) for protected in facts.protected_regions
            ):
                violations.append("P9_PROTECTED_REGION_REJECTED")
        return tuple(dict.fromkeys(violations))

    def render(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
        plan: ToolboxLayoutPlan,
    ) -> ToolboxCandidate:
        """先执行 owner guard，再用受控字体做真实容量探测。"""

        LOGGER.info(
            "调用 P9 普通叶 render，意图=验证 owner 与字体容量 route=%s page_no=%s",
            self._route,
            context.page_no,
        )
        return self._build_candidate(plan, facts, 0, apply_atomic_fallback=False)

    def _build_candidate(
        self,
        plan: ToolboxLayoutPlan,
        facts: ExtractedPageFacts,
        repair_round: int,
        *,
        apply_atomic_fallback: bool,
    ) -> ToolboxCandidate:
        """构造初始/修复候选，并在预算耗尽时按叶原子范围撤销。"""

        snapshot = self._snapshots_by_plan[plan.plan_id]
        candidate_plan = plan
        overflow_groups: set[str] = set()
        remainders: list[float] = []
        if plan.patch is not None:
            violations = self._validate_snapshot_patch(snapshot, plan.patch)
            if violations:
                findings = tuple(
                    Finding(
                        f"{plan.plan_id}-{code.lower()}-{index}",
                        code,
                        "HARD",
                        (plan.patch.patch_id,),
                    )
                    for index, code in enumerate(violations)
                )
                candidate_plan = ToolboxLayoutPlan(
                    plan.plan_id,
                    plan.route,
                    None,
                    (*plan.findings, *findings),
                    True,
                )
            else:
                for operation in plan.patch.operations:
                    try:
                        remainder = probe_operation_fit(facts, operation, self._font_path)
                    except DomainContractError, ValueError, RuntimeError:
                        remainder = -1.0
                    remainders.append(remainder)
                    if remainder < 0:
                        overflow_groups.add(operation.region_id)
                if overflow_groups and apply_atomic_fallback:
                    candidate_plan = self._apply_atomic_fallback(plan, overflow_groups)
                elif overflow_groups:
                    finding = Finding(
                        f"{plan.plan_id}-overflow-r{repair_round}",
                        "P9_TEXT_LAYOUT_OVERFLOW",
                        "HARD",
                        tuple(sorted(overflow_groups)),
                    )
                    candidate_plan = replace(plan, findings=(*plan.findings, finding))
        fingerprint = content_sha256(
            {
                "findings": tuple(item.code for item in candidate_plan.findings),
                "kernel_facts_hash": facts.kernel_facts_hash,
                "plan_id": plan.plan_id,
                "remainders": tuple(remainders),
                "repair_round": repair_round,
            }
        )
        return ToolboxCandidate(
            candidate_id=f"candidate-{plan.plan_id}-{repair_round}",
            plan=candidate_plan,
            render_fingerprint=fingerprint,
            repair_round=repair_round,
        )

    def _apply_atomic_fallback(
        self,
        plan: ToolboxLayoutPlan,
        failed_groups: set[str],
    ) -> ToolboxLayoutPlan:
        """按 page/table/entry/column/owner 范围撤销不安全 Patch。"""

        finding = Finding(
            f"{plan.plan_id}-atomic-{self._atomic_scope}",
            f"P9_ATOMIC_{self._atomic_scope.upper()}_FALLBACK",
            "HARD",
            tuple(sorted(failed_groups)),
        )
        if self._atomic_scope in {"page", "table"} or plan.patch is None:
            return ToolboxLayoutPlan(
                plan.plan_id,
                plan.route,
                None,
                (*plan.findings, finding),
                True,
            )
        retained = tuple(
            operation
            for operation in plan.patch.operations
            if operation.region_id not in failed_groups
        )
        if not retained:
            return ToolboxLayoutPlan(
                plan.plan_id,
                plan.route,
                None,
                (*plan.findings, finding),
                passthrough_requested=True,
            )
        return ToolboxLayoutPlan(
            plan.plan_id,
            plan.route,
            replace(plan.patch, operations=retained),
            (*plan.findings, finding),
            region_fallback_applied=True,
        )

    def judge(self, candidate: ToolboxCandidate) -> ToolboxJudgement:
        """根据 guard、容量和修复预算返回确定裁决。"""

        LOGGER.info(
            "调用 P9 普通叶 judge，意图=裁决结构与布局 route=%s round=%s",
            self._route,
            candidate.repair_round,
        )
        findings = candidate.plan.findings
        if candidate.plan.fallback_requested:
            disposition = DecisionDisposition.FALLBACK
            reason = "P9_PLAN_FALLBACK"
        elif any(item.code == "P9_TEXT_LAYOUT_OVERFLOW" for item in findings):
            disposition = (
                DecisionDisposition.REPAIR
                if candidate.repair_round < self._policy.repair_limit
                else DecisionDisposition.FALLBACK
            )
            reason = (
                "P9_REPAIR_REQUIRED"
                if disposition is DecisionDisposition.REPAIR
                else "P9_REPAIR_EXHAUSTED"
            )
        else:
            disposition = DecisionDisposition.ACCEPT
            reason = (
                "P9_PATCH_ACCEPTED"
                if candidate.plan.patch is not None
                else "P9_PASSTHROUGH_ACCEPTED"
            )
        return ToolboxJudgement(
            findings=findings,
            decision=Decision(
                f"decision-{candidate.candidate_id}",
                disposition,
                tuple(item.finding_id for item in findings),
                reason,
            ),
        )

    def repair(
        self,
        candidate: ToolboxCandidate,
        judgement: ToolboxJudgement,
    ) -> ToolboxCandidate:
        """最多降低一次字号；仍不安全时执行叶规定的原子回退。"""

        LOGGER.info(
            "调用 P9 普通叶 repair，意图=执行有界修复或原子回退 route=%s round=%s",
            self._route,
            candidate.repair_round,
        )
        if judgement.decision.disposition is not DecisionDisposition.REPAIR:
            return candidate
        patch = candidate.plan.patch
        if patch is None:
            return candidate
        operations = tuple(
            replace(
                operation,
                font_size=self._policy.minimum_font_size,
                payload_hash=patch_operation_hash(
                    owner=self._route,
                    target_object_ids=operation.target_object_ids,
                    rect=operation.rect or (0.0, 0.0, 1.0, 1.0),
                    replacement_text=operation.replacement_text or " ",
                    font_id=operation.font_id or self._policy.font_id,
                    font_size=self._policy.minimum_font_size,
                ),
            )
            for operation in patch.operations
        )
        repaired = replace(
            candidate.plan,
            patch=replace(patch, operations=operations),
            findings=tuple(
                item for item in candidate.plan.findings if item.code != "P9_TEXT_LAYOUT_OVERFLOW"
            ),
        )
        snapshot = self._snapshots_by_plan[candidate.plan.plan_id]
        return self._build_candidate(
            repaired,
            snapshot.facts,
            candidate.repair_round + 1,
            apply_atomic_fallback=True,
        )


class CoverToolbox(StructuredOrdinaryLeafToolbox):
    """拥有封面少量原生标题文字，并把所有视觉对象作为只读 anchor。"""

    def __init__(self, policy: P9OrdinaryLeafPolicy, font_path: Path) -> None:
        """注入统一配置和受控字体，不接受正文流作为 fallback。"""

        super().__init__("cover", policy, font_path, "page")

    def _analyze(
        self, facts: ExtractedPageFacts
    ) -> tuple[tuple[P9OwnedAtom, ...], tuple[str, ...]]:
        """按页面阅读顺序领取非页码原生文本。"""

        text = sorted(_text_objects(facts), key=lambda item: (item.bbox[1], item.bbox[0]))
        owned = tuple(item for item in text if not PAGE_NUMBER.fullmatch(item.text))
        keep = tuple(item.object_id for item in text if item not in owned)
        atoms = tuple(
            P9OwnedAtom(item.object_id, f"cover-owner-{index:03d}", item.text, item.bbox)
            for index, item in enumerate(owned)
        )
        return atoms, keep


class ContentsToolbox(StructuredOrdinaryLeafToolbox):
    """只拥有目录条目文字，页码、点线和链接定位保持源文。"""

    def __init__(self, policy: P9OrdinaryLeafPolicy, font_path: Path) -> None:
        """注入统一配置和受控字体，最小回退单位固定为目录条目。"""

        super().__init__("contents", policy, font_path, "entry")

    def _analyze(
        self, facts: ExtractedPageFacts
    ) -> tuple[tuple[P9OwnedAtom, ...], tuple[str, ...]]:
        """按纵向对齐建立 entry/unit mapping，并保护页码与点线。"""

        text = tuple(sorted(_text_objects(facts), key=lambda item: (item.bbox[1], item.bbox[0])))
        tolerance = facts.page.height_points * self._policy.line_alignment_tolerance_ratio
        rows = _axis_clusters(tuple(_center(item.bbox)[1] for item in text), tolerance)
        atoms: list[P9OwnedAtom] = []
        keep: list[str] = []
        for item, row in zip(text, rows, strict=True):
            if PAGE_NUMBER.fullmatch(item.text) or LEADER.fullmatch(item.text):
                keep.append(item.object_id)
            else:
                atoms.append(
                    P9OwnedAtom(item.object_id, f"contents-entry-{row:03d}", item.text, item.bbox)
                )
        return tuple(atoms), tuple(keep)


class EndToolbox(StructuredOrdinaryLeafToolbox):
    """处理结束页明确原生文本，空白和纯视觉页显式透传。"""

    def __init__(self, policy: P9OrdinaryLeafPolicy, font_path: Path) -> None:
        """注入统一配置和受控字体，不依赖页面是否位于文档末尾。"""

        super().__init__("end", policy, font_path, "page")

    def _analyze(
        self, facts: ExtractedPageFacts
    ) -> tuple[tuple[P9OwnedAtom, ...], tuple[str, ...]]:
        """只按当前页直接文字事实建立 owner。"""

        text = tuple(sorted(_text_objects(facts), key=lambda item: (item.bbox[1], item.bbox[0])))
        atoms = tuple(
            P9OwnedAtom(item.object_id, f"end-owner-{index:03d}", item.text, item.bbox)
            for index, item in enumerate(text)
        )
        return atoms, ()


class MultiFlowTextToolbox(StructuredOrdinaryLeafToolbox):
    """按动态列带和 gutter 建立多栏 owner，并按栏原子回退。"""

    def __init__(self, policy: P9OrdinaryLeafPolicy, font_path: Path) -> None:
        """注入统一配置和受控字体，不读取样本身份或固定栏坐标。"""

        super().__init__("body.flow_text.multi", policy, font_path, "column")

    def _analyze(
        self, facts: ExtractedPageFacts
    ) -> tuple[tuple[P9OwnedAtom, ...], tuple[str, ...]]:
        """由页面宽度、文本宽度与中心点间距动态形成列带。"""

        all_text = _text_objects(facts)
        candidates = tuple(
            item
            for item in all_text
            if (item.bbox[2] - item.bbox[0]) / facts.page.width_points
            < self._policy.multi_spanning_width_ratio
        )
        keep = [item.object_id for item in all_text if item not in candidates]
        if not candidates:
            return (), tuple(keep)
        ordered_x = tuple(_center(item.bbox)[0] for item in candidates)
        columns = _axis_clusters(
            ordered_x,
            facts.page.width_points * self._policy.multi_minimum_gutter_ratio,
        )
        if len(set(columns)) < 2:
            return (), tuple(item.object_id for item in all_text)
        sortable = sorted(
            zip(candidates, columns, strict=True),
            key=lambda pair: (pair[1], pair[0].bbox[1], pair[0].bbox[0]),
        )
        atoms = tuple(
            P9OwnedAtom(item.object_id, f"multi-column-{column:03d}", item.text, item.bbox)
            for item, column in sortable
        )
        return atoms, tuple(keep)


class TableToolbox(StructuredOrdinaryLeafToolbox):
    """处理有直接结构证据的原生表格及其页内上下文，并以整表为回退原子。"""

    def __init__(self, policy: P9OrdinaryLeafPolicy, font_path: Path) -> None:
        """注入统一配置和受控字体；图片表格不 OCR。"""

        super().__init__("body.table", policy, font_path, "table")

    def _analyze(
        self, facts: ExtractedPageFacts
    ) -> tuple[tuple[P9OwnedAtom, ...], tuple[str, ...]]:
        """由 Kernel table bbox 和文本中心构造稳定 row/column/cell owner。"""

        text = _text_objects(facts)
        # 页面中的 Logo、页眉图片等由 Kernel 单独保护，不能因此放弃已有直接表格事实。
        if not facts.table_objects:
            return (), tuple(item.object_id for item in text)
        atoms: list[P9OwnedAtom] = []
        claimed: set[str] = set()
        for table_index, table in enumerate(facts.table_objects):
            inside = tuple(item for item in text if _inside(_center(item.bbox), table.bbox))
            row_ids = _axis_clusters(
                tuple(_center(item.bbox)[1] for item in inside),
                facts.page.height_points * self._policy.table_axis_tolerance_ratio,
            )
            column_ids = _axis_clusters(
                tuple(_center(item.bbox)[0] for item in inside),
                facts.page.width_points * self._policy.table_axis_tolerance_ratio,
            )
            for item, row, column in zip(inside, row_ids, column_ids, strict=True):
                if item.object_id in claimed:
                    continue
                claimed.add(item.object_id)
                atoms.append(
                    P9OwnedAtom(
                        item.object_id,
                        f"table-{table_index:02d}-cell-r{row:03d}-c{column:03d}",
                        item.text,
                        item.bbox,
                    )
                )
        # table Route 仍负责整页翻译闭合：表外标题和说明不能被无理由降为源文透传。
        # 只有 Kernel 在翻译前机械批准的页码、代码和共享边距等对象才进入 KEEP_SOURCE。
        inventory = {item.object_id: item for item in freeze_page_text_inventory(facts).items}
        keep: list[str] = []
        for context_index, item in enumerate(
            (record for record in text if record.object_id not in claimed),
            start=1,
        ):
            inventory_item = inventory[item.object_id]
            if inventory_item.disposition is InventoryDisposition.KEEP_SOURCE:
                keep.append(item.object_id)
                continue
            atoms.append(
                P9OwnedAtom(
                    item.object_id,
                    f"table-page-context-{context_index:03d}",
                    item.text,
                    item.bbox,
                )
            )
        atoms.sort(key=lambda item: (item.group_id, item.bbox[1], item.bbox[0]))
        return tuple(atoms), tuple(keep)


class AnchoredBlocksToolbox(StructuredOrdinaryLeafToolbox):
    """把独立文本块绑定最近只读 anchor，并按 owner block 原子回退。"""

    def __init__(self, policy: P9OrdinaryLeafPolicy, font_path: Path) -> None:
        """注入统一配置和受控字体，不把独立块交给 single/multi 流。"""

        super().__init__("body.anchored_blocks", policy, font_path, "owner")

    def _analyze(
        self, facts: ExtractedPageFacts
    ) -> tuple[tuple[P9OwnedAtom, ...], tuple[str, ...]]:
        """以归一化中心距离绑定 anchor；距离并列或过远时 KEEP_SOURCE。"""

        text = _text_objects(facts)
        anchors: tuple[tuple[str, RectTuple], ...] = (
            *((item.object_id, item.bbox) for item in facts.image_objects),
            *((item.object_id, item.bbox) for item in facts.drawing_objects),
        )
        if not anchors:
            return (), tuple(item.object_id for item in text)
        atoms: list[P9OwnedAtom] = []
        keep: list[str] = []
        for item in text:
            x, y = _center(item.bbox)
            distances = sorted(
                (
                    abs(x - _center(rect)[0]) / facts.page.width_points
                    + abs(y - _center(rect)[1]) / facts.page.height_points,
                    anchor_id,
                )
                for anchor_id, rect in anchors
            )
            nearest_distance, anchor_id = distances[0]
            tied = (
                len(distances) > 1
                and abs(distances[1][0] - nearest_distance)
                <= self._policy.anchor_tie_tolerance_ratio
            )
            if nearest_distance > self._policy.anchor_maximum_distance_ratio or tied:
                keep.append(item.object_id)
                continue
            atoms.append(
                P9OwnedAtom(
                    item.object_id,
                    f"anchored-owner-{anchor_id[:16]}",
                    item.text,
                    item.bbox,
                )
            )
        atoms.sort(key=lambda atom: (atom.bbox[1], atom.bbox[0], atom.object_id))
        return tuple(atoms), tuple(keep)


def main() -> int:
    """记录 P9 普通叶只消费直接结构事实并始终有有界终态。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("P9 普通叶示例，意图=展示六叶独立 owner 与原子回退骨架")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
