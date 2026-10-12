"""Offline tests for scripts/bedrock_authority_probe.py (botocore Stubber, no AWS calls)."""
import importlib.util
import io
import json
import os

import boto3
import pytest
from botocore.stub import Stubber

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_spec = importlib.util.spec_from_file_location(
    "bedrock_authority_probe", os.path.join(PROJECT_ROOT, "scripts", "bedrock_authority_probe.py")
)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

REGION = "us-west-2"
MODEL_ID = probe.DEFAULT_MODEL_ID
ACCESS_KEY = "ASIATESTPROBEKEY0001"
SECRET_KEY = "probe-test-secret-do-not-print"
SESSION_TOKEN = "probe-test-session-token-do-not-print"
ROLE_ARN = "arn:aws:sts::123456789012:assumed-role/RiskAgentBedrockWorkerRole/e2-test"
EXPLICIT_DENY = (
    f"User: {ROLE_ARN} is not authorized to perform: bedrock:InvokeModel on resource: "
    "arn:aws:bedrock:us-west-2:123456789012:inference-profile/" + MODEL_ID +
    " with an explicit deny in an identity-based policy"
)
EXPECTED_CONVERSE = {
    "modelId": MODEL_ID,
    "messages": [{"role": "user", "content": [{"text": "Reply with OK."}]}],
    "inferenceConfig": {"maxTokens": 5, "temperature": 0.0},
}


@pytest.fixture
def harness():
    session = boto3.Session(
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        aws_session_token=SESSION_TOKEN,
        region_name=REGION,
    )
    bedrock = session.client("bedrock-runtime", config=probe.CLIENT_CONFIG)
    sts = session.client("sts", config=probe.CLIENT_CONFIG)
    with Stubber(bedrock) as bedrock_stub, Stubber(sts) as sts_stub:
        yield session, bedrock, sts, bedrock_stub, sts_stub
        bedrock_stub.assert_no_pending_responses()
        sts_stub.assert_no_pending_responses()


def _identity(stub):
    stub.add_response("get_caller_identity", {
        "Arn": ROLE_ARN, "Account": "123456789012", "UserId": "AROATESTROLEID:e2-test",
    })


def _ok(stub, request_id="req-ok"):
    stub.add_response("converse", {
        "output": {"message": {"role": "assistant", "content": [{"text": "OK"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 12, "outputTokens": 2, "totalTokens": 14},
        "metrics": {"latencyMs": 321},
        "ResponseMetadata": {"RequestId": request_id, "HTTPStatusCode": 200},
    }, EXPECTED_CONVERSE)


def _denied(stub, request_id="req-denied"):
    stub.add_client_error(
        "converse", service_error_code="AccessDeniedException", service_message=EXPLICIT_DENY,
        http_status_code=403, expected_params=EXPECTED_CONVERSE,
        response_meta={"RequestId": request_id},
    )


def _run(harness, commands):
    session, bedrock, sts, _, _ = harness
    out = io.StringIO()
    code = probe.run(
        commands, bedrock=bedrock, sts=sts,
        fingerprint=lambda: probe.credential_fingerprint(session),
        model_id=MODEL_ID, region=REGION, out=out,
    )
    text = out.getvalue()
    return code, [json.loads(line) for line in text.splitlines()], text


def _assert_no_secrets(text):
    for secret in (ACCESS_KEY, SECRET_KEY, SESSION_TOKEN):
        assert secret not in text


def test_success_records_identity_and_one_converse(harness):
    _, _, _, bedrock_stub, sts_stub = harness
    _identity(sts_stub)
    _ok(bedrock_stub)
    _identity(sts_stub)

    code, records, text = _run(harness, [json.dumps({"probe": "baseline-1"}) + "\n"])

    assert code == probe.EXIT_OK
    start, result, end = records
    assert start["event"] == "start"
    assert start["identity"]["arn"] == ROLE_ARN
    assert start["process"]["pid"] == os.getpid()
    assert start["process"]["started_at"]
    assert start["credential_fingerprint"].startswith("sha256:")
    assert start["request"] == {"prompt": "Reply with OK.", "max_tokens": 5, "temperature": 0.0}
    assert start["retries"] == {"total_max_attempts": 1, "mode": "standard"}

    assert result["event"] == "probe" and result["seq"] == 1 and result["label"] == "baseline-1"
    assert result["outcome"] == "ok"
    assert result["request_id"] == "req-ok"
    assert (result["input_tokens"], result["output_tokens"]) == (12, 2)
    assert result["output_text"] == "OK"
    assert result["bedrock_latency_ms"] == 321
    assert result["latency_ms"] >= 0
    assert result["credential_fingerprint"] == start["credential_fingerprint"]

    assert end["event"] == "end" and end["probes"] == 1
    assert end["identity"]["arn"] == ROLE_ARN
    assert end["credential_fingerprint"] == start["credential_fingerprint"]
    _assert_no_secrets(text)


def test_explicit_access_denied_is_recorded(harness):
    _, _, _, bedrock_stub, sts_stub = harness
    _identity(sts_stub)
    _denied(bedrock_stub)
    _identity(sts_stub)

    code, records, text = _run(harness, [json.dumps({"probe": "revoked-1"})])

    assert code == probe.EXIT_OK
    result = records[1]
    assert result["outcome"] == "denied"
    assert result["error_code"] == "AccessDeniedException"
    assert result["explicit_deny"] is True
    assert ROLE_ARN in result["error_message"]
    assert result["request_id"] == "req-denied"
    assert result["http_status"] == 403
    _assert_no_secrets(text)


def test_unexpected_aws_error_is_not_a_denial(harness):
    _, _, _, bedrock_stub, sts_stub = harness
    _identity(sts_stub)
    bedrock_stub.add_client_error(
        "converse", service_error_code="ThrottlingException", service_message="Too many requests",
        http_status_code=429, expected_params=EXPECTED_CONVERSE, response_meta={"RequestId": "req-throttled"},
    )
    _identity(sts_stub)

    _, records, _ = _run(harness, [json.dumps({"probe": "x"})])

    result = records[1]
    assert result["outcome"] == "error"
    assert result["error_code"] == "ThrottlingException"
    assert result["explicit_deny"] is False
    assert result["request_id"] == "req-throttled"


@pytest.mark.parametrize("line", [
    "not json",
    json.dumps(["probe"]),
    json.dumps({"invoke": "x"}),
    json.dumps({"probe": "x", "extra": 1}),
    json.dumps({"probe": 5}),
])
def test_malformed_command_sends_no_request(harness, line):
    _, _, _, _, sts_stub = harness
    _identity(sts_stub)
    _identity(sts_stub)

    code, records, _ = _run(harness, ["", line, "   "])

    assert code == probe.EXIT_OK
    assert [r["event"] for r in records] == ["start", "invalid_command", "end"]
    assert records[2]["probes"] == 0


def test_same_process_continues_after_denial(harness):
    """ok -> denied -> ok on one client: one request per command, fingerprint unchanged."""
    _, _, _, bedrock_stub, sts_stub = harness
    _identity(sts_stub)
    _ok(bedrock_stub, "req-1")
    _denied(bedrock_stub, "req-2")
    _ok(bedrock_stub, "req-3")
    _identity(sts_stub)

    code, records, text = _run(harness, [json.dumps({"probe": label}) for label in ("allowed", "revoked", "restored")])

    assert code == probe.EXIT_OK
    probes = [r for r in records if r["event"] == "probe"]
    assert [(p["seq"], p["label"], p["outcome"], p["request_id"]) for p in probes] == [
        (1, "allowed", "ok", "req-1"), (2, "revoked", "denied", "req-2"), (3, "restored", "ok", "req-3"),
    ]
    assert len({p["credential_fingerprint"] for p in probes} | {records[0]["credential_fingerprint"]}) == 1
    assert {p["pid"] for p in probes} == {os.getpid()}
    assert records[-1]["probes"] == 3
    _assert_no_secrets(text)


def test_transport_error_is_recorded_and_session_continues(harness, monkeypatch):
    from botocore.exceptions import EndpointConnectionError
    _, bedrock, _, bedrock_stub, sts_stub = harness
    _identity(sts_stub)
    _identity(sts_stub)
    calls = []

    def failing_converse(**kwargs):
        calls.append(kwargs)
        raise EndpointConnectionError(endpoint_url="https://bedrock-runtime.us-west-2.amazonaws.com")

    monkeypatch.setattr(bedrock, "converse", failing_converse)
    _, records, _ = _run(harness, [json.dumps({"probe": "a"}), json.dumps({"probe": "b"})])

    assert len(calls) == 2
    assert [r["outcome"] for r in records if r["event"] == "probe"] == ["error", "error"]
    assert records[1]["error_code"] == "EndpointConnectionError"


def test_start_identity_failure_exits_without_probes(harness):
    _, _, _, _, sts_stub = harness
    sts_stub.add_client_error("get_caller_identity", service_error_code="ExpiredToken",
                              service_message="The security token included in the request is expired")

    code, records, text = _run(harness, [json.dumps({"probe": "never"})])

    assert code == probe.EXIT_NO_IDENTITY
    assert [r["event"] for r in records] == ["start_failed"]
    assert records[0]["error_code"] == "ExpiredToken"
    _assert_no_secrets(text)


def test_clients_make_exactly_one_attempt(harness):
    """total_max_attempts=1: no retries on any client built with CLIENT_CONFIG."""
    _, bedrock, sts, _, _ = harness
    for client in (bedrock, sts):
        assert client.meta.config.retries["total_max_attempts"] == 1
        assert client.meta.config.retries["mode"] == "standard"


def test_fingerprint_is_stable_and_not_the_key():
    session = boto3.Session(aws_access_key_id=ACCESS_KEY, aws_secret_access_key=SECRET_KEY,
                            aws_session_token=SESSION_TOKEN, region_name=REGION)
    other = boto3.Session(aws_access_key_id="ASIATESTPROBEKEY0002", aws_secret_access_key=SECRET_KEY,
                          aws_session_token=SESSION_TOKEN, region_name=REGION)
    first = probe.credential_fingerprint(session)
    assert first == probe.credential_fingerprint(session)
    assert first != probe.credential_fingerprint(other)
    assert ACCESS_KEY not in first
