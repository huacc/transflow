from __future__ import annotations

import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import NODE_CHOICES, NODE_PROMPTS, PROVIDER
from .evidence import build_evidence, compact_evidence
from .io_utils import append_jsonl, read_jsonl, sha256_file, sha256_value, write_json
from .models import NodeJudgement, NodeResolution, ProviderResult
from .qwen import QwenJudge
from .resolver import resolve_node
from .rules import decide_rule


class ClassificationEngine:
    def __init__(self, root: Path, sample_dir: Path | None = None, source_manifest: Path | None = None) -> None:
        PROVIDER.api_key()
        self.root = root
        self.sample_dir = sample_dir or root / "样本1"
        self.source_manifest = source_manifest or root / "manifests" / "source_manifest.jsonl"
        self.run_id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_root = root / "artifacts" / "runs" / self.run_id
        self.images_root = self.run_root / "evidence" / "page_images"
        self.source_rows = read_jsonl(self.source_manifest)
        self.source_by_id = {row["sample_id"]: row for row in self.source_rows}
        self.exemplars = read_jsonl(root / "exemplars" / "manifest.jsonl")
        self.prompt_text = {
            path: (root / path).read_text(encoding="utf-8")
            for node in NODE_PROMPTS.values()
            for path in node.values()
        }
        self.qwen = QwenJudge(root, self.prompt_text)
        self.evidence: dict[str, dict[str, Any]] = {}
        self.routes: list[dict[str, Any]] = []
        self.resolutions: list[dict[str, Any]] = []
        self.call_count = 0
        self.review_count = 0
        self.counter_lock = threading.Lock()
        self._initialize()

    def _initialize(self) -> None:
        self.run_root.mkdir(parents=True)
        for relative in (
            "evidence/page_evidence.jsonl",
            "calls/qwen_calls.jsonl",
            "judgements/node_resolutions.jsonl",
            "routes/final_routes.jsonl",
        ):
            target = self.run_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")
        write_json(
            self.run_root / "run_manifest.json",
            {
                "run_id": self.run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "sample_count": len(self.source_rows),
                "sample_dir": str(self.sample_dir),
                "source_manifest": str(self.source_manifest),
                "provider": {"base_url": PROVIDER.base_url, "model": PROVIDER.model, "api_key_env": PROVIDER.api_key_env},
                "nodes": NODE_CHOICES,
                "prompt_sha256": {path: sha256_file(self.root / path) for path in self.prompt_text},
                "keys_persisted": False,
                "filename_or_gold_in_model_payload": False,
                "image_content_policy": "classification_only_no_translation_no_relayout",
            },
        )

    def build_all_evidence(self) -> None:
        for source in self.source_rows:
            sample_id = source["sample_id"]
            sample_path = self.sample_dir / f"{sample_id}.pdf"
            image_path = self.images_root / f"{sample_id}.png"
            evidence = build_evidence(sample_path, source, image_path)
            self.evidence[sample_id] = evidence
            public = {key: value for key, value in evidence.items() if key not in {"native_text"}}
            append_jsonl(
                self.run_root / "evidence" / "page_evidence.jsonl",
                {**public, "evidence_sha256": sha256_value(compact_evidence(evidence))},
            )

    def _record_call(
        self,
        sample_id: str,
        node_key: str,
        stage: str,
        judgement: NodeJudgement,
        result: ProviderResult,
        payload_hash: str,
        prompt_path: str,
    ) -> None:
        with self.counter_lock:
            self.call_count += 1
            if stage == "REVIEW":
                self.review_count += 1
        append_jsonl(
            self.run_root / "calls" / "qwen_calls.jsonl",
            {
                "run_id": self.run_id,
                "sample_id": sample_id,
                "node_key": node_key,
                "stage": stage,
                "prompt_path": prompt_path,
                "prompt_sha256": sha256_file(self.root / prompt_path),
                "request_business_payload_sha256": payload_hash,
                "provider": {"base_url": PROVIDER.base_url, "configured_model": PROVIDER.model},
                "response": asdict(result),
                "normalized_judgement": judgement.as_dict(),
            },
        )

    def _exemplar_images(self, node_key: str, labels: set[str]) -> list[tuple[str, Path]]:
        selected: list[tuple[str, Path]] = []
        for label in sorted(labels):
            row = next((item for item in self.exemplars if item["node_key"] == node_key and item["label"] == label), None)
            if row:
                path = self.images_root / f"{row['sample_id']}.png"
                if path.exists():
                    selected.append((label, path))
        return selected[:2]

    def _review_exemplar_labels(self, node_key: str, candidate_labels: set[str]) -> set[str]:
        labels = set(candidate_labels)
        if node_key == "body.layout_owner" and labels & {"flow_text", "anchored_blocks"}:
            labels.update({"flow_text", "anchored_blocks"})
        if node_key == "body.composite.kind" and labels:
            labels.update(NODE_CHOICES[node_key])
        return labels

    @staticmethod
    def _uses_direct_table_evidence(rule: NodeJudgement) -> bool:
        return (
            rule.node_key == "body.layout_owner"
            and rule.status == "DECIDED"
            and rule.selected_child in {"table", "composite"}
            and rule.confidence >= 0.9
            and bool(set(rule.evidence_refs) & {"TABLE1", "BTABLE1"})
        )

    def decide_node(self, sample_id: str, node_key: str, parent_path: list[str]) -> NodeResolution:
        evidence = self.evidence[sample_id]
        compact = compact_evidence(evidence)
        serialized = json.dumps(compact, ensure_ascii=False)
        source = self.source_by_id[sample_id]
        forbidden = [source["source_path"], Path(source["source_path"]).name]
        if any(value and value in serialized for value in forbidden):
            raise RuntimeError(f"payload_source_leakage:{sample_id}")
        rule = decide_rule(node_key, evidence)
        if self._uses_direct_table_evidence(rule):
            qwen_primary = NodeJudgement(
                node_key,
                "QWEN_SKIPPED",
                "INCONCLUSIVE",
                None,
                0.0,
                rule.evidence_refs,
                "直接表格证据置信度达到 0.90，未调用千问初判",
            )
        else:
            qwen_primary, provider_result, payload_hash, prompt_path = self.qwen.decide(
                node_key=node_key,
                stage="PRIMARY",
                sample_id=sample_id,
                evidence=evidence,
                compact_evidence={"confirmed_parent_path": parent_path, **compact},
                page_image=self.images_root / f"{sample_id}.png",
            )
            self._record_call(sample_id, node_key, "PRIMARY", qwen_primary, provider_result, payload_hash, prompt_path)

        def review_factory() -> NodeJudgement:
            candidate_labels = {
                item.selected_child
                for item in (rule, qwen_primary)
                if item.status == "DECIDED" and item.selected_child is not None
            }
            exemplar_labels = self._review_exemplar_labels(node_key, {str(value) for value in candidate_labels})
            review_context = {
                "rule": rule.as_dict(),
                "qwen_primary": qwen_primary.as_dict(),
                "candidate_labels": sorted(candidate_labels),
                "contrast_exemplar_labels": sorted(exemplar_labels),
                "instruction": "不要投票；重新依据当前页面、细粒度证据和已确认正例裁决当前节点",
            }
            review, result, review_hash, review_prompt = self.qwen.decide(
                node_key=node_key,
                stage="REVIEW",
                sample_id=sample_id,
                evidence=evidence,
                compact_evidence={"confirmed_parent_path": parent_path, **compact},
                page_image=self.images_root / f"{sample_id}.png",
                review_context=review_context,
                exemplar_images=self._exemplar_images(node_key, exemplar_labels),
            )
            self._record_call(sample_id, node_key, "REVIEW", review, result, review_hash, review_prompt)
            return review

        resolution = resolve_node(node_key, rule, qwen_primary, review_factory)
        record = {"run_id": self.run_id, "sample_id": sample_id, "parent_path": parent_path, **resolution.as_dict()}
        append_jsonl(self.run_root / "judgements" / "node_resolutions.jsonl", record)
        with self.counter_lock:
            self.resolutions.append(record)
        return resolution

    def classify_sample(self, source: dict[str, Any]) -> dict[str, Any]:
        sample_id = source["sample_id"]
        path: list[str] = []
        role = self.decide_node(sample_id, "page.role", [])
        if role.final.status == "DECIDED" and role.final.selected_child:
            path.append(role.final.selected_child)
        else:
            route = {"run_id": self.run_id, "sample_id": sample_id, "final_path": path, "complete_to_leaf": False, "failed_node": "page.role"}
            with self.counter_lock:
                self.routes.append(route)
            return route
        if path == ["body"]:
            layout = self.decide_node(sample_id, "body.layout_owner", path)
            if layout.final.status == "DECIDED" and layout.final.selected_child:
                path.append(layout.final.selected_child)
            else:
                path.append("freeform")
                route = {
                    "run_id": self.run_id,
                    "sample_id": sample_id,
                    "final_path": path,
                    "complete_to_leaf": True,
                    "failed_node": "body.layout_owner",
                    "taxonomy_fallback": True,
                }
                with self.counter_lock:
                    self.routes.append(route)
                return route
        if path == ["body", "flow_text"]:
            topology = self.decide_node(sample_id, "body.flow.topology", path)
            if topology.final.status == "DECIDED" and topology.final.selected_child:
                path.append(topology.final.selected_child)
            else:
                route = {
                    "run_id": self.run_id,
                    "sample_id": sample_id,
                    "final_path": ["body", "freeform"],
                    "complete_to_leaf": True,
                    "failed_node": "body.flow.topology",
                    "taxonomy_fallback": True,
                }
                with self.counter_lock:
                    self.routes.append(route)
                return route
        if path == ["body", "composite"]:
            kind = self.decide_node(sample_id, "body.composite.kind", path)
            if kind.final.status == "DECIDED" and kind.final.selected_child:
                path.append(kind.final.selected_child)
            else:
                route = {
                    "run_id": self.run_id,
                    "sample_id": sample_id,
                    "final_path": ["body", "freeform"],
                    "complete_to_leaf": True,
                    "failed_node": "body.composite.kind",
                    "taxonomy_fallback": True,
                }
                with self.counter_lock:
                    self.routes.append(route)
                return route
        route = {"run_id": self.run_id, "sample_id": sample_id, "final_path": path, "complete_to_leaf": True, "failed_node": None, "taxonomy_fallback": False}
        with self.counter_lock:
            self.routes.append(route)
        return route

    def run(self) -> str:
        self.build_all_evidence()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(self.classify_sample, self.source_rows))
        routes = sorted(self.routes, key=lambda row: row["sample_id"])
        route_file = self.run_root / "routes" / "final_routes.jsonl"
        route_file.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in routes), encoding="utf-8")
        final_counts = Counter("/".join(row["final_path"]) if row["complete_to_leaf"] else f"INCONCLUSIVE/{row['failed_node']}" for row in routes)
        write_json(
            self.run_root / "summary.json",
            {
                "run_id": self.run_id,
                "ENGINE_RUN_COMPLETE": True,
                "sample_count": len(self.source_rows),
                "qwen_call_count": self.call_count,
                "review_call_count": self.review_count,
                "complete_route_count": sum(row["complete_to_leaf"] for row in routes),
                "route_stopped_count": sum(not row["complete_to_leaf"] for row in routes),
                "taxonomy_fallback_count": sum(bool(row.get("taxonomy_fallback")) for row in routes),
                "classification_counts": dict(sorted(final_counts.items())),
            },
        )
        return self.run_id
