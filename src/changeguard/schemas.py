"""A small, dependency-free JSON-schema-style validator plus the intake model.

Only the subset we need is supported: object/array/string/integer/number/boolean/null,
required, enum, pattern, min/max, additionalProperties=false. Unknown fields are rejected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .errors import ValidationError

ID_PATTERN = r"^(CMP|ASM|PRD)-[A-Z0-9]{2,12}$"
SITE_PATTERN = r"^SITE-[A-Z0-9]{2,10}$"
MARKETS = ["EU", "US", "UK"]
CHANGE_TYPES = ["discontinued", "supplier_change", "revision", "compliance_change", "shortage"]
URGENCIES = ["low", "normal", "high", "critical"]


def _type_ok(expected: str, value: Any) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def validate(schema: dict, value: Any, path: str = "$") -> list[str]:
    """Return a list of error strings (empty list means valid)."""
    errors: list[str] = []
    types = schema.get("type")
    if types is not None:
        tlist = types if isinstance(types, list) else [types]
        if not any(_type_ok(t, value) for t in tlist):
            return [f"{path}: expected {'|'.join(tlist)}, got {type(value).__name__}"]
    if value is None:
        return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
    if isinstance(value, str):
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if "pattern" in schema and not re.match(schema["pattern"], value):
            errors.append(f"{path}: does not match required format")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: must be <= {schema['maximum']}")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                errors.extend(validate(schema["items"], item, f"{path}[{i}]"))
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}.{req}: required")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append(f"{path}.{key}: unknown field")
        for key, sub in props.items():
            if key in value:
                errors.extend(validate(sub, value[key], f"{path}.{key}"))
    return errors


def require_valid(schema: dict, value: Any) -> None:
    errs = validate(schema, value)
    if errs:
        raise ValidationError(errs)


# ----------------------------------------------------------------------------- intake (FR-01)

CHANGE_REQUEST_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["component_id", "change_type", "description"],
    "properties": {
        "component_id": {"type": "string", "pattern": ID_PATTERN, "maxLength": 20},
        "change_type": {"type": "string", "enum": CHANGE_TYPES},
        "description": {"type": "string", "minLength": 5, "maxLength": 1500},
        "urgency": {"type": "string", "enum": URGENCIES},
        "target_market": {"type": ["string", "null"], "enum": MARKETS + [None]},
        "constraints": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "max_lead_time_days": {"type": ["integer", "null"], "minimum": 1, "maximum": 365},
                "site_id": {"type": ["string", "null"], "pattern": SITE_PATTERN},
            },
        },
    },
}


@dataclass(frozen=True)
class ChangeRequest:
    component_id: str
    change_type: str
    description: str
    urgency: str = "normal"
    target_market: str | None = None
    constraints: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "component_id": self.component_id,
            "change_type": self.change_type,
            "description": self.description,
            "urgency": self.urgency,
            "target_market": self.target_market,
            "constraints": dict(self.constraints),
        }


def parse_change_request(data: Any) -> ChangeRequest:
    """Validate and normalise an incoming change request. Raises ValidationError."""
    if not isinstance(data, dict):
        raise ValidationError("request must be a JSON object")
    require_valid(CHANGE_REQUEST_SCHEMA, data)
    constraints = {k: v for k, v in (data.get("constraints") or {}).items() if v is not None}
    return ChangeRequest(
        component_id=data["component_id"],
        change_type=data["change_type"],
        description=data["description"].strip(),
        urgency=data.get("urgency", "normal"),
        target_market=data.get("target_market"),
        constraints=constraints,
    )
