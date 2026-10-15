import json

import httpx
import pytest

from resume_builder import providers


@pytest.fixture
def base():
    return {
        "name": "Test evidence", "is_example": False, "verified": True,
        "identity": {"name": "Fixed Person", "email": "person@example.test"},
        "skills": ["Python", "Testing"],
        "roles": [{
            "id": "role-a", "employer": "Fixed Employer", "title": "Fixed Title", "dates": "2020–2024",
            "achievements": [
                {"id": "ev-a", "text": "Reduced processing time by 25%.", "critical": True},
                {"id": "ev-b", "text": "Built Python tests.", "supporting_details": "Maintained 12 checks."},
            ],
        }],
        "education": [{"qualification": "Fixed Degree", "institution": "Fixed School"}],
        "certifications": ["Fixed Certification"], "references": "Available on request",
        "additional_sections": [{"heading": "Volunteering", "entries": ["Fixed volunteering"]}],
    }


def mock_result(base, kind="resume"):
    return providers.generate(kind, {"description_text": "Unique job description"}, base, "", {"provider": "mock"}, None)


def remote_config(**kwargs):
    return {"provider": "openai", "model": "test-model", **kwargs}


def install_transport(monkeypatch, handler):
    original = httpx.Client
    observed = []

    def factory(**kwargs):
        observed.append(kwargs.copy())
        kwargs["transport"].close()
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    monkeypatch.setattr(providers.httpx, "Client", factory)
    return observed


def envelope(content, **kwargs):
    return {"choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop", **kwargs}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "unsafe": "secret"}}


def test_mock_is_offline_and_valid(base, monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("Mock performed network I/O"))
    for kind in ("resume", "letter"):
        result = mock_result(base, kind)
        assert result["model"] == "mock"
        assert "NOT FOR SUBMISSION" in json.dumps(result["content"])
        assert providers.validate_content(kind, result["content"], base) == result["content"]


@pytest.mark.parametrize("mutation", [
    lambda c: c["summary"].update(evidence_ids=["unknown"]),
    lambda c: c["summary"].update(evidence_ids=["ev-a", "ev-a"]),
    lambda c: c["summary"].update(text="Reduced time by 99%."),
    lambda c: c["expertise"].append("Invented Skill"),
    lambda c: c["expertise"].append("Python"),
    lambda c: c["achievements"][0].update(role_id="another-role"),
    lambda c: c["achievements"].append(c["achievements"][0].copy()),
    lambda c: c["achievements"].pop(0),
    lambda c: c.update(employer="invented"),
])
def test_grounding_rejects_invalid_output(base, mutation):
    content = mock_result(base)["content"]
    mutation(content)
    with pytest.raises(providers.ProviderError):
        providers.validate_content("resume", content, base)


def test_letter_numeric_support_is_per_paragraph(base):
    content = {"paragraphs": [{"text": "Maintained 12 checks.", "evidence_ids": ["ev-a"]}]}
    with pytest.raises(providers.ProviderError, match="numeric"):
        providers.validate_content("letter", content, base)
    content["paragraphs"][0]["evidence_ids"] = ["ev-a", "ev-b"]
    assert providers.validate_content("letter", content, base)


def test_missing_evidence_actionable(base):
    base["roles"][0]["achievements"] = []
    with pytest.raises(providers.ProviderError, match="Add at least one achievement"):
        mock_result(base)


@pytest.mark.parametrize("url", [
    "http://api.openai.com/v1", "******example.test/v1",
    "https://example.test/v1?token=secret", "https://example.test/v1#secret",
    "https://example.test/v1?", "https://example.test/v1#", "https:///v1",
    "https://example.test:invalid", "https://example.test\\evil/v1",
])
def test_reject_unsafe_urls(url):
    with pytest.raises(providers.ProviderError):
        providers.validate_base_url(url, {"custom_endpoint_acknowledged": True})


def test_custom_endpoint_requires_warning_acknowledgement():
    with pytest.raises(providers.ProviderError, match="Explicitly acknowledge"):
        providers.validate_base_url("https://example.test/v1", {})
    assert providers.validate_base_url("https://example.test/v1/", {"custom_endpoint_acknowledged": True}) == "https://example.test/v1"


def test_strict_request_and_context_once(base, monkeypatch):
    expected = mock_result(base)["content"]
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        assert request.url == "https://api.openai.com/v1/chat/completions"
        return httpx.Response(200, json=envelope(expected))

    options = install_transport(monkeypatch, handle)
    prompt = "Tailor {job_description} {job_description} {evidence} {evidence}"
    result = providers.generate("resume", {"description_text": "Unique job description"}, base, prompt,
                                remote_config(), "test-key")
    payload = requests[0]
    fmt = payload["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False
    text = payload["messages"][1]["content"]
    assert text.count("Unique job description") == 1
    assert text.count('"role_id":"role-a"') == 1
    assert "Fixed Person" not in text and "Fixed Employer" not in text
    assert "12 checks" in text and "critical" in text
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False
    assert options[0]["verify"] is True
    assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    assert result["model"] == "test-model"


def test_omitted_placeholders_and_explicit_json_object(base, monkeypatch):
    captured = []

    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=envelope(mock_result(base)["content"]))

    install_transport(monkeypatch, handle)
    providers.generate("resume", {"description_text": "Unique job description"}, base,
                       "Please tailor this.", remote_config(provider="compatible", mode="json_object"), "test-key")
    assert captured[0]["response_format"] == {"type": "json_object"}
    assert "Required JSON schema" in captured[0]["messages"][0]["content"]
    assert captured[0]["messages"][1]["content"].count("Unique job description") == 1


@pytest.mark.parametrize("status, expected", [
    (401, "authentication"), (403, "authentication"), (429, "rate limit"),
    (500, "service failed"), (400, "no fallback"), (422, "no fallback"), (302, "redirects"),
])
def test_http_errors_sanitized_no_retry(base, monkeypatch, status, expected):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, text="private-response test-key")

    install_transport(monkeypatch, handle)
    with pytest.raises(providers.ProviderError, match=expected) as error:
        providers.generate("resume", {"description_text": "Job"}, base, "", remote_config(), "test-key")
    assert "private-response" not in str(error.value) and "test-key" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize("data, expected", [
    ({"choices": [{"message": {"refusal": "private"}, "finish_reason": "stop"}]}, "refused"),
    ({"choices": [{"message": {}, "finish_reason": "length"}]}, "incomplete"),
    ({"choices": []}, "malformed"),
    ({"choices": [{"message": {"content": "not JSON private"}, "finish_reason": "stop"}]}, "malformed"),
    (envelope({"invented": "private"}), "invalid schema"),
])
def test_bad_responses(base, monkeypatch, data, expected):
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=data))
    with pytest.raises(providers.ProviderError, match=expected) as error:
        providers.generate("resume", {"description_text": "Job"}, base, "", remote_config(), "test-key")
    assert "private" not in str(error.value)


def test_timeout_sanitized(base, monkeypatch):
    def handle(request):
        raise httpx.ReadTimeout("test-key private URL")
    install_transport(monkeypatch, handle)
    with pytest.raises(providers.ProviderError, match="timed out") as error:
        providers.generate("resume", {"description_text": "Job"}, base, "", remote_config(), "test-key")
    assert "test-key" not in str(error.value)
    assert "may have reached the server and been billed" in str(error.value)
    assert "Check provider usage/billing" in str(error.value)


def test_budget_requires_explicit_ack_and_never_truncates(base):
    parsed = providers._base(base)
    with pytest.raises(providers.ProviderError, match="budget"):
        providers._messages({"description_text": "Job"}, parsed, "", {"input_budget_chars": 1})
    messages = providers._messages({"description_text": "Job"}, parsed, "", {
        "input_budget_chars": 1, "budget_acknowledged": True,
    })
    assert "Maintained 12 checks" in messages[1]["content"]
    with pytest.raises(providers.ProviderError, match="nothing was truncated"):
        providers._messages({"description_text": "a" * providers.MAX_INPUT_CHARS}, parsed, "", {})


def test_unknown_template_placeholder(base):
    with pytest.raises(providers.ProviderError, match="Only"):
        providers._messages({"description_text": "Job"}, providers._base(base), "{identity}", {})


def test_job_placeholders_are_not_reinterpreted(base):
    job = "A job containing {evidence} and {job_description} as untrusted text"
    messages = providers._messages({"description_text": job}, providers._base(base),
                                   "{job_description}\n{evidence}", {})
    text = messages[1]["content"]
    assert text.count(job) == 1
    assert text.count('"role_id":"role-a"') == 1


def test_explicit_connection_test_is_tiny_and_has_no_career_data(monkeypatch):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=envelope({"ok": True}))

    install_transport(monkeypatch, handle)
    result = providers.test_connection(remote_config(), "test-key")
    assert result["ok"] is True and result["model"] == "test-model"
    assert requests[0]["max_completion_tokens"] == 16
    assert requests[0]["response_format"]["json_schema"]["strict"] is True
    assert len(json.dumps(requests[0])) < 1_000
    assert "evidence" not in json.dumps(requests[0])


def test_mock_connection_test_is_offline(monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("Mock performed network I/O"))
    assert providers.test_connection({"provider": "mock"}, None) == {"ok": True, "model": "mock", "usage": {}}


def test_connection_test_never_silently_downgrades(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(400, text="private-response")

    install_transport(monkeypatch, handle)
    with pytest.raises(providers.ProviderError, match="no fallback"):
        providers.test_connection(remote_config(), "test-key")
    assert len(calls) == 1


def test_fixed_openai_profile_refuses_custom_url_even_with_acknowledgement(monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("Unexpected network I/O"))
    with pytest.raises(providers.ProviderError, match="advanced compatible"):
        providers.test_connection(remote_config(base_url="https://example.test/v1",
                                                custom_endpoint_acknowledged=True), "test-key")


def test_compatible_profile_supports_acknowledged_custom_url(monkeypatch):
    urls = []

    def handle(request):
        urls.append(str(request.url))
        return httpx.Response(200, json=envelope({"ok": True}))

    install_transport(monkeypatch, handle)
    result = providers.test_connection({
        "provider": "compatible", "model": "test-model", "base_url": "https://example.test/v1",
        "custom_endpoint_acknowledged": True,
    }, "test-key")
    assert result["ok"] is True
    assert urls == ["https://example.test/v1/chat/completions"]


def test_snapshot_budget_acknowledgement_alias(base):
    messages = providers._messages({"description_text": "Job"}, providers._base(base), "", {
        "input_budget_chars": 1, "acknowledge_budget": True,
    })
    assert "Maintained 12 checks" in messages[1]["content"]


def test_escaped_braces_agree_with_shared_prompt_policy(base):
    messages = providers._messages({"description_text": "Job"}, providers._base(base),
                                   '{{"literal":"value"}} {{identity}} {job_description}', {})
    assert '{"literal":"value"} {identity} Job' in messages[1]["content"]


def test_complete_profile_and_role_background_are_preserved(base):
    base["profile"] = "Experienced in careful testing."
    base["roles"][0]["background"] = "Responsible for internal automation and documentation."
    messages = providers._messages({"description_text": "Job"}, providers._base(base), "", {})
    assert base["profile"] in messages[1]["content"]
    assert base["roles"][0]["background"] in messages[1]["content"]
    assert "Fixed Employer" not in messages[1]["content"]
    assert "Fixed Title" not in messages[1]["content"]


def test_cached_usage_details_are_preserved_but_not_raw_metadata(base, monkeypatch):
    response = envelope(mock_result(base)["content"])
    response["usage"]["prompt_tokens_details"] = {"cached_tokens": 8, "private": "test-key"}
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=response))
    result = providers.generate("resume", {"description_text": "Job"}, base, "", remote_config(), "test-key")
    assert result["usage"]["prompt_tokens_details"] == {"cached_tokens": 8}
    assert "test-key" not in repr(result["usage"])


def test_named_openai_profile_requires_strict_schema(monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("Unexpected network I/O"))
    with pytest.raises(providers.ProviderError, match="requires strict json_schema"):
        providers.test_connection(remote_config(mode="json_object"), "test-key")


@pytest.mark.parametrize("is_example,verified", [(True, True), (False, False), (True, False)])
def test_live_adapter_rejects_example_or_unverified_bases(base, monkeypatch, is_example, verified):
    base.update(is_example=is_example, verified=verified)
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("Unexpected network I/O"))
    with pytest.raises(providers.ProviderError, match="verified, non-example"):
        providers.generate("resume", {"description_text": "Job"}, base, "", remote_config(), "test-key")
    assert mock_result(base)["model"] == "mock"


def test_schema_valid_credential_echo_is_never_returned_for_checkpoint(base, monkeypatch):
    content = mock_result(base)["content"]
    content["summary"]["text"] += " Authorization: test-key"
    # This response would otherwise pass the document schema and grounding checks.
    assert providers.validate_content("resume", content, base)
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=envelope(content)))
    with pytest.raises(providers.ProviderError, match="credential material") as error:
        providers.generate("resume", {"description_text": "Job"}, base, "", remote_config(), "test-key")
    assert "test-key" not in str(error.value)


def test_public_input_estimate_matches_prompt_budget_and_is_offline(base, monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("Estimate performed network I/O"))
    job = {"description_text": "Unique job description"}
    prompt = "Tailor {job_description} using {evidence}"
    expected = sum(len(message["content"]) for message in providers._messages(
        job, providers._base(base), prompt, {},
    ))
    assert providers.estimate_input_chars(job, base, prompt) == expected


def test_public_input_estimate_rejects_hard_limit_without_truncating(base):
    with pytest.raises(providers.ProviderError, match="nothing was truncated"):
        providers.estimate_input_chars({"description_text": "a" * providers.MAX_INPUT_CHARS}, base, "")


def test_json_object_estimate_and_soft_budget_include_appended_schema(base, monkeypatch):
    job = {"description_text": "Job"}
    prompt_only = providers.estimate_input_chars(job, base, "")
    actual = providers.estimate_input_chars(job, base, "", kind="resume", mode="json_object")
    assert actual > prompt_only
    config = remote_config(provider="compatible", mode="json_object", input_budget_chars=prompt_only)
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("Unacknowledged request performed network I/O"))
    with pytest.raises(providers.ProviderError, match="budget"):
        providers.generate("resume", job, base, "", config, "test-key")


def test_json_object_schema_budget_can_be_explicitly_acknowledged(base, monkeypatch):
    job = {"description_text": "Job"}
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=envelope(mock_result(base)["content"])))
    result = providers.generate("resume", job, base, "", remote_config(
        provider="compatible", mode="json_object",
        input_budget_chars=providers.estimate_input_chars(job, base, ""),
        budget_acknowledged=True,
    ), "test-key")
    assert result["content"] == mock_result(base)["content"]


@pytest.mark.parametrize("provider", ["openai", "compatible"])
@pytest.mark.parametrize("kind", ["resume", "letter"])
def test_strict_request_schema_omits_nested_length_constraints(base, monkeypatch, provider, kind):
    captured = []

    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=envelope(mock_result(base, kind)["content"]))

    install_transport(monkeypatch, handle)
    providers.generate(kind, {"description_text": "Job"}, base, "",
                       remote_config(provider=provider), "test-key")
    schema = captured[0]["response_format"]["json_schema"]["schema"]
    pending = [schema]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            assert not {"minLength", "maxLength", "minItems", "maxItems"}.intersection(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    assert schema["additionalProperties"] is False
    assert schema["required"]
    assert schema["$defs"]["GroundedText"]["additionalProperties"] is False
    assert "$ref" in json.dumps(schema)


@pytest.mark.parametrize("paragraphs", [
    [],
    [{"text": "Supported wording.", "evidence_ids": ["ev-a"]}] * 7,
    [{"text": "", "evidence_ids": ["ev-a"]}],
    [{"text": "Supported wording.", "evidence_ids": []}],
])
def test_local_length_constraints_remain_enforced(base, paragraphs):
    with pytest.raises(providers.ProviderError, match="invalid schema"):
        providers.validate_content("letter", {"paragraphs": paragraphs}, base)


def test_short_advert_never_deletes_matching_characters_from_instructions(base):
    prompt = "Create a careful application and retain all factual achievements."
    messages = providers._messages({"description_text": "a"}, providers._base(base), prompt, {})
    user = messages[1]["content"]
    assert user.startswith(prompt + "\n\nJOB DESCRIPTION:\na\n\nEVIDENCE:\n")
    assert user.count("\n\nJOB DESCRIPTION:\na\n\n") == 1


def test_literal_pasted_advert_is_preserved_and_placeholders_are_preferred(base):
    prompt = "Instructions quoting a literal advert: Unique job description"
    messages = providers._messages({"description_text": "Unique job description"},
                                   providers._base(base), prompt, {})
    assert messages[1]["content"].startswith(prompt + "\n\nJOB DESCRIPTION:\n")
