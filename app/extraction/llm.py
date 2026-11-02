"""LLM-backed extraction with mandatory citations, plus the citation verifier.

The model is constrained to a JSON schema, and every value it returns must carry
a verbatim quote from the source. A value whose quote cannot be found in the text
is DISCARDED — not stored with low confidence. That rule is what makes the
extraction pipeline safe to put in front of a procurement team.

Works with no API key: `LlmClient.available` is False, extraction falls back to
the deterministic rules, and the pipeline still produces usable records.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import httpx

from ..config import settings

log = logging.getLogger(__name__)

EXTRACTION_FIELDS = {
    "title": "the tender title or item being procured",
    "quantity": "total quantity as a number",
    "estimated_value": "estimated tender value in INR as a number, only if stated",
    "emd_amount": "earnest money deposit in INR as a number",
    "tender_fee": "tender document fee in INR as a number",
    "gst_treatment": "'inclusive', 'exclusive' or 'unknown'",
    "delivery_location": "city/state of delivery",
    "warranty_years": "warranty period in years as a number",
    "eligibility": "a short summary of eligibility/qualification criteria",
    "specifications": "a short summary of key technical specifications",
}

SYSTEM_PROMPT = """You extract structured fields from Indian government tender documents.

Rules you must follow exactly:
1. Return ONLY a JSON object. No prose, no markdown fences.
2. Every field is an object: {"value": <value or null>, "quote": "<verbatim text from the document>"}.
3. "quote" must be copied character-for-character from the document. If you cannot
   quote it, set value to null. Never paraphrase a quote.
4. If a field is not stated in the document, use null for both value and quote.
   Do not infer, estimate, or fill from general knowledge.
5. Numbers must be plain numbers in INR, without currency symbols or commas.
"""


@dataclass
class CitationCheck:
    verified: dict = field(default_factory=dict)
    dropped: list[dict] = field(default_factory=list)

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)


_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", (text or "")).strip().lower()


def verify_citations(fields: dict, source_text: str, pages: list[str] | None = None) -> CitationCheck:
    """Keep only fields whose quote actually appears in the source text.

    This is the anti-hallucination contract in code (blueprint Section 9.1). It is
    deliberately strict: a slightly wrong quote means the model reformatted or
    invented it, and either way the value is not safe to store.
    """
    haystack = _norm(source_text)
    check = CitationCheck()
    for key, payload in (fields or {}).items():
        if not isinstance(payload, dict):
            check.dropped.append({"field": key, "reason": "not an object with value+quote"})
            continue
        value = payload.get("value")
        quote = payload.get("quote")
        if value in (None, "", []) and quote in (None, ""):
            continue  # honestly absent; nothing to verify
        if not quote:
            check.dropped.append({"field": key, "value": value, "reason": "no quote supplied"})
            continue
        needle = _norm(quote)
        if len(needle) < 4 or needle not in haystack:
            check.dropped.append(
                {"field": key, "value": value, "reason": "quote not found verbatim in source"}
            )
            continue
        verified = {"value": value, "quote": quote, "method": payload.get("method", "llm")}
        if pages:
            from .pdf import page_of_offset

            idx = haystack.find(needle)
            raw_offset = _raw_offset(source_text, quote)
            verified["page"] = page_of_offset(pages, raw_offset if raw_offset >= 0 else 0)
        check.verified[key] = verified
    return check


def _raw_offset(text: str, quote: str) -> int:
    """Find the quote's offset in the original text, tolerant of whitespace runs."""
    idx = text.find(quote)
    if idx >= 0:
        return idx
    pattern = r"\s+".join(re.escape(part) for part in quote.split())
    m = re.search(pattern, text)
    return m.start() if m else -1


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class LlmClient:
    """Minimal OpenRouter client. The API key comes from the environment only."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None):
        self.api_key = api_key if api_key is not None else settings.openrouter_api_key
        self.base_url = (base_url or settings.openrouter_base_url).rstrip("/")
        self.model = model or settings.extraction_model

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def chat_json(self, messages: list[dict], *, model: str | None = None, temperature: float = 0.0,
                  max_tokens: int = 2000) -> dict:
        if not self.available:
            raise RuntimeError("no LLM API key configured (set OPENROUTER_API_KEY)")
        payload = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
            resp = client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/infisource/procintel",
                    "X-Title": "Procurement Intelligence Platform",
                },
                json=payload,
            )
            resp.raise_for_status()
            body = resp.json()
        content = body["choices"][0]["message"]["content"]
        return _loads(content)

    def extract_tender_fields(self, text: str, *, max_chars: int = 24000) -> dict:
        """Ask the model for cited fields. Returns {} when unavailable."""
        if not self.available:
            return {}
        truncated = text[:max_chars]
        field_spec = json.dumps(EXTRACTION_FIELDS, indent=2)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Extract these fields:\n"
                    f"{field_spec}\n\n"
                    "Document text follows between the markers.\n<<<DOCUMENT\n"
                    f"{truncated}\nDOCUMENT>>>"
                ),
            },
        ]
        try:
            return self.chat_json(messages)
        except Exception as exc:  # noqa: BLE001 - extraction must never kill a run
            log.warning("LLM extraction failed: %s", exc)
            return {}


def _loads(content: str) -> dict:
    """Parse JSON from a model response, tolerating stray fences."""
    if isinstance(content, dict):
        return content
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
    log.warning("could not parse JSON from model response")
    return {}
