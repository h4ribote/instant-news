"""The OpenAPI document is the API's contract: docs/openapi.json must match what the app generates, and every route must declare what it returns."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.routing import APIRoute
from openapi_spec_validator import validate

from server.main import create_app
from server.openapi import render

_CONTRACT = Path(__file__).resolve().parent.parent / 'docs' / 'openapi.json'

_REGENERATE = 'docker compose -f docker-compose.test.yml build tests && docker compose -f docker-compose.test.yml run --rm --no-deps -T tests python -m server.openapi > docs/openapi.json'


def test_generated_document_matches_the_committed_contract() -> None:
    generated = json.loads(render())
    committed = json.loads(_CONTRACT.read_text(encoding='utf-8'))
    assert generated == committed, f'docs/openapi.json is out of date with the routes and models. Review the change, then regenerate it: {_REGENERATE}'


def test_contract_is_a_valid_openapi_document() -> None:
    validate(json.loads(_CONTRACT.read_text(encoding='utf-8')))


def test_every_route_declares_its_success_response() -> None:
    undeclared = []
    for route in create_app().routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        status = route.status_code or 200
        declares_body = route.response_model is not None
        declares_no_body = status in (204, 302) or status in route.responses
        if not (declares_body or declares_no_body):
            undeclared.append(f'{sorted(route.methods)} {route.path}')
    assert undeclared == []


def test_every_route_documents_the_error_body() -> None:
    document = json.loads(render())
    missing = [
        f'{method.upper()} {path}'
        for path, operations in document['paths'].items()
        for method, operation in operations.items()
        if operation['responses'].get('default', {}).get('content', {}).get('application/json', {}).get('schema') != {'$ref': '#/components/schemas/ErrorOut'}
    ]
    assert missing == []
    assert 'HTTPValidationError' not in document['components']['schemas']
