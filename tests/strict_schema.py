"""Dependency-free executor for the JSON Schema vocabulary used by this project."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def load_schema(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("schema root is not an object:" + str(path))
    return value


def _json_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
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
    raise ValueError("unsupported schema type:" + expected)


def _resolve_ref(root: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError("only local schema references are supported:" + ref)
    value: Any = root
    for part in ref[2:].split("/"):
        value = value[part.replace("~1", "/").replace("~0", "~")]
    if not isinstance(value, dict):
        raise ValueError("schema reference is not an object:" + ref)
    return value


def _instance_path(instance: Any, dotted_path: str) -> Any:
    value = instance
    for part in dotted_path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def validate(instance: Any, schema: dict[str, Any], *,
             root: dict[str, Any] | None = None, path: str = "$") -> list[str]:
    root = schema if root is None else root
    errors: list[str] = []
    if "$ref" in schema:
        return validate(instance, _resolve_ref(root, str(schema["$ref"])), root=root, path=path)
    for index, branch in enumerate(schema.get("allOf", [])):
        errors.extend(validate(instance, branch, root=root, path=f"{path}.allOf[{index}]"))
    if "anyOf" in schema:
        branch_errors = [validate(instance, branch, root=root, path=path)
                         for branch in schema["anyOf"]]
        if all(branch_errors):
            errors.append(path + ":anyOf")
    if "oneOf" in schema:
        passes = sum(not validate(instance, branch, root=root, path=path)
                     for branch in schema["oneOf"])
        if passes != 1:
            errors.append(f"{path}:oneOf:{passes}")
    if "not" in schema and not validate(instance, schema["not"], root=root, path=path):
        errors.append(path + ":not")
    if "if" in schema:
        selected = schema.get("then") if not validate(
            instance, schema["if"], root=root, path=path) else schema.get("else")
        if selected is not None:
            errors.extend(validate(instance, selected, root=root, path=path))
    if "const" in schema and instance != schema["const"]:
        errors.append(path + ":const")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(path + ":enum")
    expected_types = schema.get("type")
    if expected_types is not None:
        values = [expected_types] if isinstance(expected_types, str) else expected_types
        if not any(_json_type(instance, item) for item in values):
            errors.append(f"{path}:type:{'|'.join(values)}")
            return errors
    if isinstance(instance, dict):
        for group in schema.get("x-equalCardinality", []):
            values = [_instance_path(instance, str(item)) for item in group]
            if all(isinstance(value, list) for value in values):
                lengths = {len(value) for value in values}
                if len(lengths) != 1:
                    errors.append(path + ":x-equalCardinality:" + "|".join(group))
        discriminator = schema.get("x-derivationCardinality")
        if isinstance(discriminator, dict):
            counted = _instance_path(instance, str(discriminator["countPath"]))
            actual = _instance_path(instance, str(discriminator["valuePath"]))
            if isinstance(counted, list) and counted:
                expected = (discriminator["many"] if len(counted) > 1
                            else discriminator["one"])
                if actual != expected:
                    errors.append(path + ":x-derivationCardinality")
        if isinstance(schema.get("propertyNames"), dict):
            for key in instance:
                errors.extend(validate(key, schema["propertyNames"], root=root,
                                       path=f"{path}.propertyNames[{key}]"))
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}:required:{key}")
        properties = schema.get("properties", {})
        for key, value in instance.items():
            child = f"{path}.{key}"
            if key in properties:
                errors.extend(validate(value, properties[key], root=root, path=child))
            elif schema.get("additionalProperties", True) is False:
                errors.append(f"{path}:additionalProperties:{key}")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors.extend(validate(value, schema["additionalProperties"],
                                       root=root, path=child))
        if len(instance) < int(schema.get("minProperties", 0)):
            errors.append(path + ":minProperties")
        if "maxProperties" in schema and len(instance) > int(schema["maxProperties"]):
            errors.append(path + ":maxProperties")
    if isinstance(instance, list):
        if len(instance) < int(schema.get("minItems", 0)):
            errors.append(path + ":minItems")
        if "maxItems" in schema and len(instance) > int(schema["maxItems"]):
            errors.append(path + ":maxItems")
        if schema.get("uniqueItems"):
            rendered = [json.dumps(item, ensure_ascii=True, sort_keys=True,
                                   separators=(",", ":")) for item in instance]
            if len(rendered) != len(set(rendered)):
                errors.append(path + ":uniqueItems")
        unique_by = schema.get("x-uniqueBy")
        if isinstance(unique_by, str):
            values = [item.get(unique_by) for item in instance if isinstance(item, dict)]
            if len(values) != len(set(values)):
                errors.append(path + ":x-uniqueBy:" + unique_by)
        if isinstance(schema.get("items"), dict):
            for index, value in enumerate(instance):
                errors.extend(validate(value, schema["items"], root=root,
                                       path=f"{path}[{index}]"))
    if isinstance(instance, str):
        if len(instance) < int(schema.get("minLength", 0)):
            errors.append(path + ":minLength")
        if "maxLength" in schema and len(instance) > int(schema["maxLength"]):
            errors.append(path + ":maxLength")
        if "pattern" in schema and re.search(str(schema["pattern"]), instance) is None:
            errors.append(path + ":pattern")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(path + ":minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(path + ":maximum")
    return errors
