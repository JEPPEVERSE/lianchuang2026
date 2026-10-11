"""Small explicit JSON Schema contracts, validated locally before execution."""

import math


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def array(item, minimum=0, maximum=100):
    return {"type": "array", "items": item, "minItems": minimum, "maxItems": maximum}


TEXT = {"type": "string", "minLength": 1, "maxLength": 20000}
ID = {"type": "string", "minLength": 1, "maxLength": 200}
ANALYSIS_SCHEMA = obj({
    "points": array(obj({
        "id": ID, "name": ID, "aliases": array(ID, maximum=20),
        "chunk_ids": array(ID, minimum=1),
        "existing_id": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    }), minimum=1),
    "edges": array(obj({"from": ID, "to": ID, "chunk_ids": array(ID, minimum=1)})),
    "problems": array(obj({
        "id": ID, "name": ID, "content": TEXT, "question_type": ID,
        "point_ids": array(ID, minimum=1), "chunk_id": ID, "source_quote": TEXT,
    })),
})
ANSWER_SCHEMA = obj({
    "status": {"type": "string", "enum": ["answered", "partial", "insufficient"]},
    "answer": TEXT,
    "citations": array(obj({"chunk_id": ID, "quote": TEXT})),
    "missing_information": {"type": "string", "maxLength": 20000},
})


def validate(value, schema, path="output"):
    """Validate only the JSON Schema vocabulary used in the contracts above."""
    types = schema.get("type", [])
    types = types if isinstance(types, list) else [types]
    checks = {"object": isinstance(value, dict), "array": isinstance(value, list),
              "string": isinstance(value, str), "null": value is None,
              "number": type(value) in (int, float) and math.isfinite(value)}
    if types and not any(checks.get(kind, False) for kind in types):
        raise ValueError(f"{path}: expected {types}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: unsupported value")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            raise ValueError(f"{path}: missing required fields")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError(f"{path}: unknown fields {sorted(set(value) - set(properties))}")
        for key, child in properties.items():
            if key in value:
                validate(value[key], child, f"{path}.{key}")
    elif isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100000):
            raise ValueError(f"{path}: invalid array length")
        for i, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{i}]")
    elif isinstance(value, str):
        if (schema.get("minLength", 0) and not value.strip()) or not (
            schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 1000000)
        ):
            raise ValueError(f"{path}: invalid string length")
    elif type(value) in (int, float):
        if not schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf):
            raise ValueError(f"{path}: number out of bounds")
