"""公开 P4 起唯一使用并在 P6 收敛的 SharedPdfKernel 机械能力。"""

from transflow.pdf_kernel.constraints import ConstraintChecker
from transflow.pdf_kernel.facts import (
    FACTS_SCHEMA_VERSION,
    ExtractedPageFacts,
    KernelDrawingFact,
    KernelImageFact,
    KernelTableFact,
    KernelTextFact,
    PageFactsExtractor,
    PageObjectFact,
    stable_page_identity,
)
from transflow.pdf_kernel.fingerprint import KernelFingerprint, build_kernel_fingerprint
from transflow.pdf_kernel.fonts import ControlledFontRegistry
from transflow.pdf_kernel.models import FontProbe, KernelFinding, PatchManifest, RepairDecision
from transflow.pdf_kernel.patch import (
    INTERPRETER_ID,
    PagePatchInterpreter,
    PatchApplicationResult,
    ReplayPage,
    build_patch_manifest,
    patch_operation_hash,
)
from transflow.pdf_kernel.preservation import (
    DEFAULT_SUPPORT_MATRIX,
    DocumentStructure,
    FeatureDisposition,
    PreflightDecision,
    PreservationPreflightResult,
    PreservationResult,
    PreservationSupportMatrix,
    capture_document_structure,
    load_support_matrix,
    preflight_document,
    validate_minimal_preservation,
    validate_preservation,
)
from transflow.pdf_kernel.renderer import CandidateRender, PyMuPdfPageRenderer
from transflow.pdf_kernel.repair import BoundedRepairController, RepairLimits, shrink_font_patch
from transflow.pdf_kernel.workspace import RunWorkspace, WorkspaceAllocator

__all__ = [
    "DEFAULT_SUPPORT_MATRIX",
    "FACTS_SCHEMA_VERSION",
    "INTERPRETER_ID",
    "BoundedRepairController",
    "CandidateRender",
    "ConstraintChecker",
    "ControlledFontRegistry",
    "DocumentStructure",
    "ExtractedPageFacts",
    "FeatureDisposition",
    "FontProbe",
    "KernelDrawingFact",
    "KernelFinding",
    "KernelFingerprint",
    "KernelImageFact",
    "KernelTableFact",
    "KernelTextFact",
    "PageFactsExtractor",
    "PageObjectFact",
    "PagePatchInterpreter",
    "PatchApplicationResult",
    "PatchManifest",
    "PreflightDecision",
    "PreservationPreflightResult",
    "PreservationResult",
    "PreservationSupportMatrix",
    "PyMuPdfPageRenderer",
    "RepairDecision",
    "RepairLimits",
    "ReplayPage",
    "RunWorkspace",
    "WorkspaceAllocator",
    "build_kernel_fingerprint",
    "build_patch_manifest",
    "capture_document_structure",
    "load_support_matrix",
    "patch_operation_hash",
    "preflight_document",
    "shrink_font_patch",
    "stable_page_identity",
    "validate_minimal_preservation",
    "validate_preservation",
]
