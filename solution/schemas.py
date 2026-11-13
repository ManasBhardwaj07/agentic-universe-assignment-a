"""
Dynamic schema and disposition catalogue loader.
Reads JSON schemas and dispositions.csv without hardcoding validation logic.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_SCHEMAS_DIR = Path("assignment-a-data/schemas")


@dataclass
class FieldDefinition:
    name: str
    type: str  # 'string', 'phone', 'date', 'decimal', 'enum'
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: List[str] = field(default_factory=list)
    values: List[str] = field(default_factory=list)
    default: Optional[str] = None


@dataclass
class WorkflowSchema:
    workflow_id: str
    description: str
    fields: List[FieldDefinition]

    @property
    def reference_field(self) -> Optional[FieldDefinition]:
        for f in self.fields:
            if f.reference:
                return f
        return None

    def get_field(self, name: str) -> Optional[FieldDefinition]:
        for f in self.fields:
            if f.name == name:
                return f
        return None


@dataclass
class DispositionInfo:
    workflow_id: str
    code: str
    label: str
    is_terminal: bool
    is_success: bool


class SchemaRegistry:
    """Manages loaded workflow schemas and disposition catalogue."""

    def __init__(self, schemas_dir: Path | str = DEFAULT_SCHEMAS_DIR):
        self.schemas_dir = Path(schemas_dir)
        self.workflows: Dict[str, WorkflowSchema] = {}
        self.dispositions: Dict[str, Dict[str, DispositionInfo]] = {}
        self._load()

    def _load(self) -> None:
        # Load workflow JSON schemas
        for json_path in self.schemas_dir.glob("*.json"):
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            wf_id = data["workflow_id"]
            fields = []
            for item in data.get("fields", []):
                fields.append(FieldDefinition(
                    name=item["name"],
                    type=item["type"],
                    required=item.get("required", False),
                    pii=item.get("pii", False),
                    reference=item.get("reference", False),
                    aliases=item.get("aliases", []),
                    values=item.get("values", []),
                    default=item.get("default"),
                ))
            self.workflows[wf_id] = WorkflowSchema(
                workflow_id=wf_id,
                description=data.get("description", ""),
                fields=fields,
            )

        # Load dispositions.csv
        disp_path = self.schemas_dir / "dispositions.csv"
        if disp_path.exists():
            with open(disp_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    wf_id = row["workflow_id"].strip()
                    code = row["code"].strip()
                    label = row["label"].strip()
                    is_term = row["is_terminal"].strip().lower() == "true"
                    is_succ = row["is_success"].strip().lower() == "true"
                    if wf_id not in self.dispositions:
                        self.dispositions[wf_id] = {}
                    self.dispositions[wf_id][code] = DispositionInfo(
                        workflow_id=wf_id,
                        code=code,
                        label=label,
                        is_terminal=is_term,
                        is_success=is_succ,
                    )

    def get_workflow(self, workflow_id: str) -> Optional[WorkflowSchema]:
        return self.workflows.get(workflow_id)

    def is_valid_disposition(self, workflow_id: str, code: str) -> bool:
        wf_disps = self.dispositions.get(workflow_id, {})
        return code in wf_disps

    def get_disposition(self, workflow_id: str, code: str) -> Optional[DispositionInfo]:
        return self.dispositions.get(workflow_id, {}).get(code)
