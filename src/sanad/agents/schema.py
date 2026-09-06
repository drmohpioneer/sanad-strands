"""Describe caller-owned Pydantic fields without giving the model a filled example."""

import json
from typing import Any

from pydantic import BaseModel


def describe_schema(schema: type[BaseModel]) -> str:
    document = schema.model_json_schema()
    definitions = document.get("$defs", {})
    lines = ["Return one JSON object and nothing else. Fields:"]

    def describe(node: dict[str, Any], path: str, required: bool, refs: tuple[str, ...]) -> None:
        reference = node.get("$ref")
        if reference:
            if reference in refs:
                lines.append(f"- {path}: same fields as {reference.rsplit('/', 1)[-1]}.")
                return
            node = definitions[reference.rsplit("/", 1)[-1]] | {
                key: value for key, value in node.items() if key != "$ref"
            }
            refs = (*refs, reference)
        alternatives = node.get("anyOf", node.get("oneOf", []))
        if alternatives:
            lines.append(f"- {path}: {'required' if required else 'optional'}; one of:")
            for alternative in alternatives:
                shared = {
                    key: value for key, value in node.items() if key not in {"anyOf", "oneOf"}
                }
                describe(shared | alternative, path, required, refs)
            return
        kind = node.get("type", "JSON value")
        details = [str(kind), "required" if required else "optional"]
        if "enum" in node or "const" in node:
            allowed = node.get("enum", [node.get("const")])
            details.append("allowed values: " + ", ".join(json.dumps(v) for v in allowed))
        for key in (
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
            "minLength",
            "maxLength",
            "pattern",
            "format",
            "minItems",
            "maxItems",
            "uniqueItems",
        ):
            if key in node:
                details.append(f"{key}: {json.dumps(node[key], ensure_ascii=False)}")
        if node.get("additionalProperties") is False:
            details.append("no extra fields")
        if node.get("description"):
            details.append(str(node["description"]))
        lines.append(f"- {path}: {'; '.join(details)}.")
        for name, child in node.get("properties", {}).items():
            describe(
                child, f"{path}.{name}" if path else name, name in node.get("required", []), refs
            )
        if isinstance(node.get("additionalProperties"), dict):
            describe(node["additionalProperties"], f"{path}.<key>", False, refs)
        if "items" in node:
            describe(node["items"], path + "[]", True, refs)
        for index, child in enumerate(node.get("prefixItems", [])):
            describe(child, f"{path}[{index}]", True, refs)

    for name, child in document.get("properties", {}).items():
        describe(child, name, name in document.get("required", []), ())
    if document.get("additionalProperties") is False:
        lines.append("No other top-level fields are allowed.")
    return "\n".join(lines)
