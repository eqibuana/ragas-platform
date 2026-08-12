"""
PermataBank AI Platform - RAGAS Evaluation Script
==================================================
Evaluates both HR and Contact Center RAG pipelines using golden dataset.

Golden Dataset: ragas_data.xlsx
  - Sheet "HR" → evaluated with domain="hr" (People Care Agent)
  - Sheet "Contact Center" → evaluated with domain="contact_center" (Contact Center Agent)
  - Columns: "Test Scenario" (question), "Expected Result" (ground_truth)

API: http://k8s-aiplatfo-frontend-1732695a13-fe3650871f8fb59b.elb.ap-southeast-3.amazonaws.com

Metrics:
  - Faithfulness
  - Answer Relevancy
  - Context Precision
  - Context Recall
  - Context (retrieved passages, not a score - kept for auditing. Uses the real
    retrieved chunk body when the deployed agentic-chatbot-service honors the
    include_chunk_text=True flag sent with every request; falls back to a
    metadata-only proxy (doc_title/section_path/page_number/score/chunk_type/
    caption) per-chunk for any citation missing chunk_text, e.g. an older
    deployment that hasn't wired that flag through yet — see
    _build_pseudo_context() and the "Data Sources" sheet in the output workbook)
  - Answer Correctness
  - Noise Sensitivity
  - Summarization Score

Each metric is computed in its OWN isolated ragas.evaluate() call. This matters:
previously all metrics were batched into one evaluate() call, so a single
metric that couldn't run on the shared dataset (e.g. Summarization Score,
which needs a "reference_contexts" field that none of the other metrics use)
raised an exception that silently discarded EVERY optional metric, including
ones that would have worked fine (Answer Correctness, Noise Sensitivity).
Isolating each metric means one failure only removes that one column.

Usage (from repo root):
  uv run python services/ragas-service/ragas_evaluation.py
  uv run python services/ragas-service/ragas_evaluation.py --domain contact_center
  uv run python services/ragas-service/ragas_evaluation.py --domain hr
"""

import argparse
import asyncio
import re
import socket
import sys
import time
from datetime import datetime

import httpx
import pandas as pd

# Check RAGAS availability at import time
RAGAS_AVAILABLE = False
RAGAS_IMPORT_ERROR = None
try:
    from ragas import evaluate as ragas_evaluate
    from ragas.metrics import (
        answer_relevancy,
        context_precision,
        context_recall,
        faithfulness,
    )
    RAGAS_AVAILABLE = True
except Exception as e:  # was `except ImportError: pass` - swallowed the real reason
    RAGAS_IMPORT_ERROR = f"{type(e).__name__}: {e}"

# AWS credentials for RAGAS judge LLM (Bedrock)
# These are used ONLY for the evaluation LLM (not for the RAG pipeline API calls).
# Loaded from services/ragas-service/.env (gitignored — see .env.example for the template).
# Never hardcode credentials here; if the file is missing, Bedrock calls will fail
# with a clear boto3 credentials error rather than silently using someone else's key.
import os
from pathlib import Path

SERVICE_DIR = Path(__file__).parent

try:
    from dotenv import load_dotenv
    load_dotenv(SERVICE_DIR / ".env")
except ImportError:
    pass

# ==============================================================================
# CONFIG
# ==============================================================================

API_URL = "http://k8s-aiplatfo-frontend-1732695a13-fe3650871f8fb59b.elb.ap-southeast-3.amazonaws.com"
# Resolved against this file's directory (not CWD) so the script works the same
# whether it's run from repo root, from inside services/ragas-service/, or via Docker.
EXCEL_PATH = str(SERVICE_DIR / "ragas_data.xlsx")
OUTPUT_PATH = str(SERVICE_DIR / "output" / "ragas_results.xlsx")

# Credentials per domain — evaluate with ALL users (manager + users)
# Each user runs the same questions to test RBAC consistency
CREDENTIALS = {
    "hr": [
        {"email": "hr_mgr_1@example.com", "password": "Password123", "role": "hr_manager"},
        {"email": "hr_usr_1@example.com", "password": "Password123", "role": "hr_user"},
        {"email": "hr_usr_2@example.com", "password": "Password123", "role": "hr_user"},
        {"email": "hr_usr_3@example.com", "password": "Password123", "role": "hr_user"},
    ],
    "contact_center": [
        {"email": "cc_mgr_1@example.com", "password": "Password123", "role": "cc_manager"},
        {"email": "cc_usr_1@example.com", "password": "Password123", "role": "cc_user"},
        {"email": "cc_usr_2@example.com", "password": "Password123", "role": "cc_user"},
        {"email": "cc_usr_3@example.com", "password": "Password123", "role": "cc_user"},
    ],
}

# Domain mapping: sheet name → API domain value
SHEET_TO_DOMAIN = {
    "HR": "hr",                      # People Care Agent
    "Contact Center": "contact_center",  # Contact Center Agent
}

# Retry settings
MAX_RETRIES = 3
RETRY_BACKOFF = 3

RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    socket.gaierror,
)


# ==============================================================================
# API CLIENT
# ==============================================================================

def _build_pseudo_context(c: dict) -> str:
    """Render one citation's metadata as a readable proxy for its retrieved chunk.

    See the NOTE in RAGClient.ask() — the chat API's Citation object never
    carries the chunk's body text, so this stitches together every field that
    IS available (source service + document + section + page + score + type
    + caption) into one line so it's at least traceable back to a specific
    KB chunk, even though it isn't the chunk's real content.
    """
    doc_title = c.get("doc_title") or "Unknown"
    section = c.get("section_path") or ""
    page = c.get("page_number")
    doc_id = c.get("doc_id") or ""
    score = c.get("score")
    chunk_type = c.get("chunk_type") or "text"
    caption = c.get("caption") or ""

    parts = [f"[retrieval-service via agentic-chatbot-service citation] Doc: {doc_title}"]
    if section:
        parts.append(f"Section: {section}")
    if page is not None:
        parts.append(f"Page: {page}")
    if caption:
        parts.append(f"Caption: {caption}")
    parts.append(f"Type: {chunk_type}")
    if isinstance(score, (int, float)):
        parts.append(f"Score: {score:.4f}")
    if doc_id:
        parts.append(f"doc_id: {doc_id}")
    return " | ".join(parts)


def _extract_chunk_detail(c: dict) -> dict:
    """Structured per-chunk metadata (for the Trace/Results sheets), tagged
    with the originating service so the score can be traced back to exactly
    which document/chunk/service produced it."""
    return {
        "service": "retrieval-service (via agentic-chatbot-service /api/v1/chat citations)",
        "doc_title": c.get("doc_title"),
        "doc_id": c.get("doc_id"),
        "section_path": c.get("section_path"),
        "page_number": c.get("page_number"),
        "chunk_type": c.get("chunk_type"),
        "caption": c.get("caption"),
        "score": c.get("score"),
        "chunk_text": c.get("chunk_text"),
    }


class RAGClient:
    """Client to call the PermataBank AI Platform API."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.token = None
        self.user_id = None
        self.user_roles = []
        self.client = httpx.AsyncClient(timeout=300.0)  # 5 minutes per request

    async def _retry_request(self, method, url, **kwargs):
        """HTTP request with retry on network errors."""
        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await self.client.request(method, url, **kwargs)
            except RETRYABLE_EXCEPTIONS as e:
                last_exc = e
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                    print(f"\n     [retry {attempt}/{MAX_RETRIES}] {e}, waiting {wait}s...")
                    await asyncio.sleep(wait)
        raise last_exc

    async def login(self, email: str, password: str):
        """Login and get user info."""
        # Get token
        resp = await self._retry_request(
            "POST", f"{self.base_url}/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        resp.raise_for_status()
        self.token = resp.json()["access_token"]

        # Get user info (user_id + roles required for /api/v1/chat)
        me_resp = await self._retry_request(
            "GET", f"{self.base_url}/api/v1/auth/me",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        me_resp.raise_for_status()
        me = me_resp.json()
        self.user_id = me["id"]
        self.user_roles = [r["name"] for r in me.get("roles", [])]
        print(f"  Logged in: {email} (roles: {self.user_roles})")

    async def ask(self, question: str, domain: str) -> dict:
        """Send question to POST /api/v1/chat with specific domain.

        domain="hr" → People Care Agent
        domain="contact_center" → Contact Center Agent
        """
        resp = await self._retry_request(
            "POST", f"{self.base_url}/api/v1/chat",
            headers={"Authorization": f"Bearer {self.token}"},
            json={
                "message": question,
                "stream": False,
                "user_id": self.user_id,
                "user_roles": self.user_roles,
                "domain": domain,
                # Eval/debug flag (see ChatRequest.include_chunk_text) — asks the
                # agentic-chatbot-service to populate Citation.chunk_text with the
                # real retrieved chunk body instead of leaving it null. Requires a
                # deployed agentic-chatbot-service that wires this flag through
                # build_initial_state → generate._extract_citations; older
                # deployments will just ignore the field and return chunk_text=None,
                # in which case we fall back to the metadata proxy below.
                "include_chunk_text": True,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        answer = data.get("answer", "")
        citations = data.get("citations", [])

        # Build retrieved_contexts from the real chunk text when the deployed
        # agentic-chatbot-service returns it (Citation.chunk_text, populated only
        # when include_chunk_text=True was honored). Falls back to
        # _build_pseudo_context — a METADATA PROXY (doc_title/section_path/page/
        # score/chunk_type/caption, NOT the chunk's real content) — for any
        # citation missing chunk_text, e.g. an older deployment that doesn't wire
        # the flag through yet. RAGAS metrics that need actual text (faithfulness,
        # context_precision/recall, summarization_score) are only as good as
        # whichever of these two was actually used per chunk.
        contexts = [
            c["chunk_text"] if c.get("chunk_text") else _build_pseudo_context(c)
            for c in citations
        ]
        chunk_details = [_extract_chunk_detail(c) for c in citations]

        # Empty only when the agent genuinely returned zero citations —
        # a real retrieval gap, not a data-format issue.
        if not contexts:
            contexts = ["[NO CITATIONS RETURNED — agent produced zero citations for this question]"]

        return {
            "answer": answer,
            "contexts": contexts,
            "citations": citations,
            "chunk_details": chunk_details,
            "has_citations": bool(citations),
        }

    async def close(self):
        await self.client.aclose()


# ==============================================================================
# RAGAS EVALUATION
# ==============================================================================

def _setup_judge():
    """Build the Bedrock LLM + SageMaker bge-m3 embeddings for RAGAS scoring.
    Uses the same SageMaker endpoint your platform uses for embeddings.

    Returns RAGAS-wrapped LLM and embeddings (LangchainLLMWrapper / LangchainEmbeddingsWrapper)
    which are required by ragas 0.2.x for metric.init().
    """
    try:
        import json as _json
        import boto3
        from langchain_aws import ChatBedrockConverse
        from langchain_core.embeddings import Embeddings
        from ragas.llms import LangchainLLMWrapper
        from ragas.embeddings import LangchainEmbeddingsWrapper

        # LLM judge (this works - proven before)
        bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name="ap-southeast-3",
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        )
        raw_llm = ChatBedrockConverse(
            model="amazon.nova-pro-v1:0",
            region_name="ap-southeast-3",
            client=bedrock_client,
            temperature=0,
        )
        # Wrap for RAGAS 0.2.x compatibility
        llm = LangchainLLMWrapper(raw_llm)
        print("  LLM judge: Bedrock Nova Pro (temperature=0) ✓ [wrapped for ragas 0.2.x]")

        # Try embeddings (needs sagemaker:InvokeEndpoint permission)
        embeddings = None
        try:
            class SageMakerBgeEmbeddings(Embeddings):
                def __init__(self):
                    self._client = boto3.client(
                        "sagemaker-runtime",
                        region_name="ap-southeast-3",
                        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
                        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
                    )
                    self.endpoint_name = "bge-m3-embedding"
                    self.ic_name = "bge-m3-ic"

                def _invoke(self, texts):
                    kwargs = {
                        "EndpointName": self.endpoint_name,
                        "ContentType": "application/json",
                        "Body": _json.dumps({"inputs": texts, "truncate": True}),
                    }
                    if self.ic_name:
                        kwargs["InferenceComponentName"] = self.ic_name
                    resp = self._client.invoke_endpoint(**kwargs)
                    return _json.loads(resp["Body"].read())

                def embed_documents(self, texts):
                    if not texts:
                        return []
                    results = []
                    for i in range(0, len(texts), 16):
                        results.extend(self._invoke(texts[i:i+16]))
                    return results

                def embed_query(self, text):
                    return self._invoke([text])[0]

            print("  Testing SageMaker bge-m3 embeddings...", end=" ")
            raw_embeddings = SageMakerBgeEmbeddings()
            test_vec = raw_embeddings.embed_query("test")
            print(f"OK (dim={len(test_vec)})")
            # Wrap for RAGAS 0.2.x compatibility
            embeddings = LangchainEmbeddingsWrapper(raw_embeddings)
            print("  Embeddings wrapped for ragas 0.2.x ✓")
        except Exception as emb_err:
            print(f"\n  [!] Embeddings failed: {emb_err}")
            print("  → answer_relevancy & answer_correctness will be skipped")
            embeddings = None

        # Also store the raw (unwrapped) LLM for trace extraction calls
        # (those use langchain .invoke() directly, not ragas internals)
        llm._raw_langchain_llm = raw_llm

        return llm, embeddings
    except Exception as e:
        print(f"  [!] Judge setup failed: {e}")
        import traceback
        traceback.print_exc()
        return None, None


# Global storage for trace logs (populated during scoring, written to Excel later)
_TRACE_LOGS = []

# Global storage for comparison history (detailed text comparisons for each dimension)
# Each entry stores the full text used in comparison so user can trace score derivation
_COMPARISON_HISTORY = []


def _get_raw_llm(llm):
    """Extract the raw langchain LLM from a RAGAS wrapper for direct .invoke() calls.
    If it's already a raw LLM (has .invoke), return it as-is."""
    if hasattr(llm, "_raw_langchain_llm"):
        return llm._raw_langchain_llm
    if hasattr(llm, "langchain_llm"):
        return llm.langchain_llm
    return llm


def _extract_claims_from_answer(answer: str, llm) -> list:
    """Use the judge LLM to extract atomic claims/statements from an answer.
    This replicates what RAGAS does internally for faithfulness scoring."""
    raw_llm = _get_raw_llm(llm)
    prompt = (
        "Extract all atomic factual claims from the following answer. "
        "Each claim should be a single, self-contained statement that can be "
        "verified independently. Return them as a numbered list.\n\n"
        f"Answer: {answer}\n\n"
        "Claims:"
    )
    try:
        resp = raw_llm.invoke(prompt)
        text = resp.content if hasattr(resp, "content") else str(resp)
        # Parse numbered list
        claims = []
        for line in text.strip().split("\n"):
            line = line.strip()
            if line and (line[0].isdigit() or line.startswith("-")):
                # Remove numbering/bullet
                claim = line.lstrip("0123456789.-) ").strip()
                if claim:
                    claims.append(claim)
        return claims if claims else [text.strip()]
    except Exception as e:
        return [f"[Claim extraction failed: {e}]"]


def _verify_claims_against_context(claims: list, contexts: list, llm) -> list:
    """For each claim, check if it's supported by the contexts.
    Returns list of dicts: {claim, verdict, reasoning}"""
    raw_llm = _get_raw_llm(llm)
    ctx_text = "\n---\n".join(contexts[:5])  # Limit to 5 contexts for token budget
    verdicts = []
    for claim in claims:
        prompt = (
            "Given the following contexts, determine if the claim is supported.\n\n"
            f"Contexts:\n{ctx_text}\n\n"
            f"Claim: {claim}\n\n"
            "Respond with EXACTLY one of: SUPPORTED, NOT SUPPORTED, or PARTIAL\n"
            "Then on the next line, explain briefly why.\n"
            "Format:\nVerdict: SUPPORTED/NOT SUPPORTED/PARTIAL\nReason: <explanation>"
        )
        try:
            resp = raw_llm.invoke(prompt)
            text = resp.content if hasattr(resp, "content") else str(resp)
            # Parse verdict
            verdict_line = "UNKNOWN"
            reason_line = ""
            for line in text.strip().split("\n"):
                if "verdict" in line.lower() or "supported" in line.upper() or "not supported" in line.upper():
                    if "NOT SUPPORTED" in line.upper():
                        verdict_line = "NOT SUPPORTED"
                    elif "PARTIAL" in line.upper():
                        verdict_line = "PARTIAL"
                    elif "SUPPORTED" in line.upper():
                        verdict_line = "SUPPORTED"
                if "reason" in line.lower():
                    reason_line = line.split(":", 1)[-1].strip()
            if not reason_line:
                reason_line = text.strip()[:200]
            verdicts.append({"claim": claim, "verdict": verdict_line, "reasoning": reason_line})
        except Exception as e:
            verdicts.append({"claim": claim, "verdict": "ERROR", "reasoning": str(e)})
    return verdicts


# Matches "Statement:", "1. Statement:", "**Statement:**", etc. — tolerant of numbering
# and markdown bold that LLMs commonly add despite being told not to.
_STMT_RE = re.compile(r"^[\d.\-\s*]*\**\s*statement\s*\**\s*:\s*", re.IGNORECASE)
_ATTR_RE = re.compile(r"^[\d.\-\s*]*\**\s*attributed\s*\**\s*:\s*", re.IGNORECASE)
_REASON_RE = re.compile(r"^[\d.\-\s*]*\**\s*reason\s*\**\s*:\s*", re.IGNORECASE)


def _check_gt_statements_in_context(ground_truth: str, contexts: list, llm) -> list:
    """For context_recall: check which ground truth statements are attributable to contexts.
    Returns list of dicts: {statement, attributed, reasoning}.

    NOTE: this is a SEPARATE, hand-parsed LLM call used only to produce a human-readable
    trace of the real ragas context_recall score — it does not feed into the score itself.
    If parsing fails, attributed is "PARSE_FAILED" (not "NO") so the trace summary doesn't
    silently contradict the real RAGAS score with a fabricated 0-count.
    """
    raw_llm = _get_raw_llm(llm)
    ctx_text = "\n---\n".join(contexts[:5])
    prompt = (
        "Break the following ground truth into individual statements, then for each "
        "determine if it can be attributed to the given contexts.\n\n"
        f"Ground Truth: {ground_truth}\n\n"
        f"Contexts:\n{ctx_text}\n\n"
        "Respond with ONLY the statements below, in EXACTLY this format, with no "
        "introductory sentence, preamble, or closing remarks:\n"
        "Statement: <statement>\n"
        "Attributed: YES/NO\n"
        "Reason: <brief explanation>\n"
        "---"
    )
    try:
        resp = raw_llm.invoke(prompt)
        text = resp.content if hasattr(resp, "content") else str(resp)
        results = []
        current = {}
        for raw_line in text.strip().split("\n"):
            line = raw_line.strip()
            if _STMT_RE.match(line):
                if current:
                    results.append(current)
                current = {"statement": _STMT_RE.sub("", line).strip(), "attributed": "PARSE_FAILED", "reasoning": ""}
            elif _ATTR_RE.match(line) and current:
                val = _ATTR_RE.sub("", line).strip().upper()
                current["attributed"] = "YES" if "YES" in val else "NO"
            elif _REASON_RE.match(line) and current:
                current["reasoning"] = _REASON_RE.sub("", line).strip()
        if current:
            results.append(current)
        if results:
            return results
        return [{
            "statement": ground_truth[:200],
            "attributed": "PARSE_FAILED",
            "reasoning": f"[Trace parsing failed — judge response didn't follow the expected format. "
                         f"The RAGAS score itself is unaffected. Raw response: {text[:300]}]",
        }]
    except Exception as e:
        return [{"statement": ground_truth[:200], "attributed": "ERROR", "reasoning": str(e)}]


def _check_answer_vs_ground_truth(answer: str, ground_truth: str, llm) -> dict:
    """For answer_correctness: compare answer factual claims vs ground truth.
    Returns dict with TP, FP, FN claims."""
    raw_llm = _get_raw_llm(llm)
    prompt = (
        "Compare the Answer to the Ground Truth. Identify:\n"
        "- TP (True Positive): claims in the answer that are also in ground truth\n"
        "- FP (False Positive): claims in the answer that are NOT in ground truth\n"
        "- FN (False Negative): claims in ground truth that are MISSING from answer\n\n"
        f"Answer: {answer}\n\n"
        f"Ground Truth: {ground_truth}\n\n"
        "Format your response as:\n"
        "TP claims:\n- <claim1>\n- <claim2>\n\n"
        "FP claims:\n- <claim1>\n\n"
        "FN claims:\n- <claim1>\n\n"
        "If a section has no claims, write exactly '(none)' under that heading — "
        "do not write 'None' as if it were a claim.\n"
    )
    # Placeholder values the judge LLM sometimes emits for an empty section
    # ("- None", "- N/A", "- (none)") — these are not real claims and must not be counted.
    _EMPTY_CLAIM = {"none", "(none)", "n/a", "na", "-"}
    try:
        resp = raw_llm.invoke(prompt)
        text = resp.content if hasattr(resp, "content") else str(resp)
        tp, fp, fn = [], [], []
        current_section = None
        for line in text.strip().split("\n"):
            line = line.strip()
            if "tp" in line.lower() and "claim" in line.lower():
                current_section = "tp"
            elif "fp" in line.lower() and "claim" in line.lower():
                current_section = "fp"
            elif "fn" in line.lower() and "claim" in line.lower():
                current_section = "fn"
            elif line.startswith("-") or line.startswith("•"):
                claim = line.lstrip("-•* ").strip()
                if not claim or claim.lower().strip(".() ") in _EMPTY_CLAIM:
                    continue
                if current_section == "tp":
                    tp.append(claim)
                elif current_section == "fp":
                    fp.append(claim)
                elif current_section == "fn":
                    fn.append(claim)
        return {"tp": tp, "fp": fp, "fn": fn, "raw_response": text[:500]}
    except Exception as e:
        return {"tp": [], "fp": [], "fn": [], "raw_response": f"ERROR: {e}"}


def _check_context_relevance_to_question(question: str, contexts: list, llm) -> list:
    """For context_precision trace: check each context chunk's relevance to the question.
    Returns list of dicts: {context_rank, context_snippet, relevant, reasoning}"""
    raw_llm = _get_raw_llm(llm)
    results = []
    for rank, ctx in enumerate(contexts[:5], 1):
        ctx_str = str(ctx)[:500]
        prompt = (
            "Given the following question and a retrieved context chunk, determine if "
            "the context is relevant to answering the question.\n\n"
            f"Question: {question}\n\n"
            f"Context Chunk (Rank #{rank}):\n{ctx_str}\n\n"
            "Respond EXACTLY in this format:\n"
            "Relevant: YES or NO\n"
            "Reason: <brief explanation of why this context is or is not relevant>"
        )
        try:
            resp = raw_llm.invoke(prompt)
            text = resp.content if hasattr(resp, "content") else str(resp)
            relevant = "YES" if "YES" in text.upper().split("\n")[0] else "NO"
            reason = ""
            for line in text.strip().split("\n"):
                if "reason" in line.lower():
                    reason = line.split(":", 1)[-1].strip()
                    break
            if not reason:
                reason = text.strip()[:200]
            results.append({
                "context_rank": rank,
                "context_snippet": ctx_str[:200],
                "relevant": relevant,
                "reasoning": reason,
            })
        except Exception as e:
            results.append({
                "context_rank": rank,
                "context_snippet": ctx_str[:200],
                "relevant": "ERROR",
                "reasoning": str(e),
            })
    return results


def _build_score_assessment(metric_name: str, score) -> str:
    """Generate a score assessment label and diagnostic recommendation."""
    if pd.isna(score):
        return "⚠ N/A — Score could not be computed"

    # Noise sensitivity is inverse (lower is better)
    if metric_name == "noise_sensitivity":
        if score <= 0.2:
            return f"✓ EXCELLENT ({score:.4f}) — Model is robust against irrelevant context"
        elif score <= 0.3:
            return f"✓ PASS ({score:.4f}) — Acceptable noise handling"
        elif score <= 0.5:
            return f"~ PARTIAL ({score:.4f}) — Model somewhat influenced by noise. Consider strengthening 'answer ONLY from context' rules in system prompt"
        else:
            return f"✗ FAIL ({score:.4f}) — Model is heavily influenced by irrelevant context. Strengthen generate prompt security section"

    # All other metrics: higher is better
    thresholds = {
        "faithfulness": {
            "pass": "Answer is well-grounded in retrieved documents",
            "partial": "Some statements lack context support. Review hallucination prevention rules in system prompt",
            "fail": "Significant hallucination detected. Tighten generate prompt and add 'cite only from provided context' rules",
        },
        "answer_relevancy": {
            "pass": "Answer directly addresses the question asked",
            "partial": "Answer partially addresses the question. Improve decompose/generate prompts",
            "fail": "Answer is off-topic. Review decompose_query prompt and intent classification",
        },
        "context_precision": {
            "pass": "Retrieved documents are relevant and well-ranked",
            "partial": "Mix of relevant/irrelevant docs. Tune expand_queries prompt, add domain terms",
            "fail": "Poor retrieval quality. Review embedding model, chunking strategy, and query expansion",
        },
        "context_recall": {
            "pass": "All needed information was retrieved from knowledge base",
            "partial": "Some information missing from retrieval. Upload more documents or improve chunking",
            "fail": "Critical information gaps. Check if relevant documents are in the knowledge base",
        },
        "answer_correctness": {
            "pass": "Answer matches the expected answer well",
            "partial": "Answer is partially correct. Review generate prompt structure and formatting rules",
            "fail": "Answer significantly differs from expected. Check pipeline end-to-end",
        },
        "summarization_score": {
            "pass": "Answer captures key information from contexts well",
            "partial": "Some key info from contexts is missed. Improve generation prompt",
            "fail": "Answer poorly summarizes retrieved information. Review generate step",
        },
    }

    diag = thresholds.get(metric_name, {"pass": "Good", "partial": "Needs improvement", "fail": "Poor"})

    if score >= 0.8:
        return f"✓ PASS ({score:.4f}) — {diag['pass']}"
    elif score >= 0.5:
        return f"~ PARTIAL ({score:.4f}) — {diag['partial']}"
    else:
        return f"✗ FAIL ({score:.4f}) — {diag['fail']}"


def _score_one_metric(name, metric, dataset, llm, embeddings):
    """Run a single RAGAS metric in its own evaluate() call so a failure here can
    never take down any other metric. Returns a list of per-row scores, or None.

    After scoring, runs detailed trace extraction using the judge LLM to log
    exactly HOW each score was derived (claims, verdicts, comparisons).
    All trace data is stored in _TRACE_LOGS for Excel export.
    """
    global _TRACE_LOGS
    print(f"  [{name}] running...", end=" ", flush=True)
    kwargs = {}
    if llm is not None:
        kwargs["llm"] = llm
        # Set LLM directly on metric instance (ragas 0.2.x requirement)
        if hasattr(metric, "llm"):
            metric.llm = llm
    if embeddings is not None:
        kwargs["embeddings"] = embeddings
        # Set embeddings directly on metric instance (ragas 0.2.x requirement)
        if hasattr(metric, "embeddings"):
            metric.embeddings = embeddings
    try:
        result = ragas_evaluate(dataset, metrics=[metric], **kwargs)
        scores_df = result.to_pandas()
        # The output column is usually named after the metric; fall back to whatever
        # new column evaluate() appended if the exact name doesn't match.
        input_cols = set(dataset.column_names)
        col = name if name in scores_df.columns else next(
            (c for c in scores_df.columns if c not in input_cols), None
        )
        if col is None:
            print("FAILED (no score column returned)")
            return None
        values = scores_df[col].values
        avg_score = pd.Series(values).mean()
        print(f"OK (avg={avg_score:.4f})")

        # --- Detailed per-row trace extraction & logging ---
        _extract_and_log_traces(name, dataset, values, llm)

        return values
    except Exception as e:
        import traceback
        print(f"FAILED ({e})")
        traceback.print_exc()
        return None


def _extract_and_log_traces(name, dataset, values, llm):
    """Extract detailed trace information for each metric showing exactly
    how the score was calculated, what was compared, and the intermediate
    verdicts. Logs to console AND stores in _TRACE_LOGS for Excel output.

    Also populates _COMPARISON_HISTORY with the full text-to-text comparison
    so users can see exactly what was compared (Answer vs Context, Answer vs
    Ground Truth, Context vs Question) and the evaluation reasoning.

    Builds a 'history_log' field that contains a formatted text block
    (mirrors the console output) so it can be read directly in the Excel Trace sheet.
    """
    global _TRACE_LOGS, _COMPARISON_HISTORY

    ds_dict = {col: dataset[col] for col in dataset.column_names}
    questions = ds_dict.get("user_input", ds_dict.get("question", [None] * len(values)))
    answers = ds_dict.get("response", ds_dict.get("answer", [None] * len(values)))
    ground_truths = ds_dict.get("reference", ds_dict.get("ground_truth", [None] * len(values)))
    contexts_list = ds_dict.get("retrieved_contexts", ds_dict.get("contexts", [None] * len(values)))

    print(f"\n  ┌── [{name}] Detailed Trace & Verdicts ──")

    for i, score in enumerate(values):
        q = questions[i] if questions[i] else "N/A"
        a = answers[i] if answers[i] else "N/A"
        gt = ground_truths[i] if ground_truths[i] else "N/A"
        ctx = contexts_list[i] if contexts_list[i] else []

        # Determine verdict label
        if pd.isna(score):
            verdict = "⚠ N/A"
        elif score >= 0.8:
            verdict = "✓ PASS"
        elif score >= 0.5:
            verdict = "~ PARTIAL"
        else:
            verdict = "✗ FAIL"

        score_str = f"{score:.4f}" if not pd.isna(score) else "N/A"
        assessment = _build_score_assessment(name, score)

        print(f"  │")
        print(f"  │ Row {i+1}: [{verdict}] Score={score_str}")
        print(f"  │   Assessment: {assessment}")
        print(f"  │   Question: {str(q)[:100]}")

        # history_log accumulates the formatted text that goes into Excel
        history_log = []
        history_log.append(f"{'='*60}")
        history_log.append(f"│ Row {i+1}: [{verdict}] Score={score_str}")
        history_log.append(f"│   Metric: {name}")
        history_log.append(f"│   Assessment: {assessment}")
        history_log.append(f"│   Question: {str(q)}")
        history_log.append(f"│   Answer: {str(a)}")
        history_log.append(f"│   Ground Truth: {str(gt)}")
        history_log.append(f"│   Contexts: {' | '.join(str(c)[:200] for c in ctx[:3]) if ctx else 'N/A'}")
        history_log.append(f"│")

        trace_entry = {
            "row": i + 1,
            "metric": name,
            "score": float(score) if not pd.isna(score) else None,
            "verdict": verdict,
            "score_assessment": assessment,
            "question": str(q),
            "answer": str(a),
            "ground_truth": str(gt),
            "contexts": " | ".join(str(c) for c in ctx) if ctx else "N/A",
            "trace_details": "",
            "claims_or_statements": "",
            "supported_count": "",
            "unsupported_count": "",
            "calculation": "",
            "history_log": "",
        }

        # === FAITHFULNESS: Extract claims from answer, verify against contexts ===
        if name == "faithfulness" and llm is not None:
            print(f"  │   [Extracting claims from answer...]")
            claims = _extract_claims_from_answer(str(a), llm)
            print(f"  │   Claims extracted ({len(claims)}):")
            history_log.append(f"│   [Extracting claims from answer...]")
            history_log.append(f"│   Claims extracted ({len(claims)}):")
            for ci, claim in enumerate(claims):
                print(f"  │     {ci+1}. {claim[:120]}")
                history_log.append(f"│     {ci+1}. {claim}")

            print(f"  │   [Verifying claims against contexts...]")
            history_log.append(f"│")
            history_log.append(f"│   [Verifying claims against contexts...]")
            verdicts_list = _verify_claims_against_context(claims, [str(c) for c in ctx], llm)
            supported = sum(1 for v in verdicts_list if v["verdict"] == "SUPPORTED")
            not_supported = sum(1 for v in verdicts_list if v["verdict"] != "SUPPORTED")

            for v in verdicts_list:
                icon = "✓" if v["verdict"] == "SUPPORTED" else "✗"
                print(f"  │     {icon} [{v['verdict']}] {v['claim'][:80]}")
                print(f"  │       Reason: {v['reasoning'][:120]}")
                history_log.append(f"│     {icon} [{v['verdict']}] {v['claim']}")
                history_log.append(f"│       Reason: {v['reasoning']}")

            calc = f"Faithfulness = {supported}/{len(claims)} supported claims = {supported/len(claims):.4f}" if claims else "No claims"
            print(f"  │   Calculation: {calc}")
            print(f"  │   Assessment: {assessment}")
            print(f"  │   RAGAS Score: {score_str}")
            history_log.append(f"│")
            history_log.append(f"│   Calculation: {calc}")
            history_log.append(f"│   Assessment: {assessment}")
            history_log.append(f"│   RAGAS Score: {score_str}")

            trace_entry["claims_or_statements"] = "\n".join(f"[{v['verdict']}] {v['claim']}" for v in verdicts_list)
            trace_entry["supported_count"] = supported
            trace_entry["unsupported_count"] = not_supported
            trace_entry["calculation"] = calc
            trace_entry["trace_details"] = "\n".join(f"{v['claim']} → {v['verdict']}: {v['reasoning']}" for v in verdicts_list)

        # === ANSWER RELEVANCY: Show what's being compared ===
        elif name == "answer_relevancy":
            print(f"  │   Answer: {str(a)[:150]}")
            print(f"  │   Comparison: Answer embedding vs Question embedding (cosine similarity)")
            print(f"  │   Method: LLM generates N questions from the answer, embeddings compared to original")
            print(f"  │   Assessment: {assessment}")
            print(f"  │   RAGAS Score: {score_str}")
            history_log.append(f"│   [Answer Relevancy Evaluation]")
            history_log.append(f"│   Method: LLM generates N questions from the answer,")
            history_log.append(f"│           then computes cosine_similarity of generated question")
            history_log.append(f"│           embeddings vs original question embedding.")
            history_log.append(f"│   Original Question: {str(q)}")
            history_log.append(f"│   Answer: {str(a)}")
            history_log.append(f"│   Comparison: embedding(generated_questions) ↔ embedding(original_question)")
            history_log.append(f"│   Formula: answer_relevancy = mean(cosine_sim(gen_q_i, original_q))")
            history_log.append(f"│   Assessment: {assessment}")
            history_log.append(f"│   RAGAS Score: {score_str}")
            trace_entry["trace_details"] = (
                f"Method: LLM generates questions from answer, computes cosine_similarity(generated_q_embeddings, original_question_embedding)\n"
                f"Answer used: {str(a)}\n"
                f"Original question: {str(q)}"
            )
            trace_entry["calculation"] = f"answer_relevancy = mean(cosine_sim(gen_questions, original_question)) = {score_str}"

        # === CONTEXT PRECISION: Contexts ranking vs ground truth ===
        elif name == "context_precision":
            print(f"  │   Ground Truth: {str(gt)[:150]}")
            print(f"  │   Contexts (ranked):")
            history_log.append(f"│   [Context Precision Evaluation]")
            history_log.append(f"│   Ground Truth: {str(gt)}")
            history_log.append(f"│   Contexts (ranked):")
            for ci, c in enumerate(ctx[:5]):
                print(f"  │     [{ci+1}] {str(c)[:100]}")
                history_log.append(f"│     [{ci+1}] {str(c)[:200]}")

            # Detailed: check each context relevance to question
            if llm is not None:
                print(f"  │   [Checking each context's relevance to question...]")
                history_log.append(f"│")
                history_log.append(f"│   [Context vs Question — Relevance Check:]")
                ctx_relevance = _check_context_relevance_to_question(str(q), [str(c) for c in ctx], llm)
                relevant_count = sum(1 for cr in ctx_relevance if cr["relevant"] == "YES")
                for cr in ctx_relevance:
                    icon = "✓" if cr["relevant"] == "YES" else "✗"
                    print(f"  │     {icon} [Rank {cr['context_rank']}] {cr['relevant']}: {cr['reasoning'][:100]}")
                    history_log.append(f"│     {icon} [Rank {cr['context_rank']}] {cr['relevant']}")
                    history_log.append(f"│       Context: {cr['context_snippet'][:150]}")
                    history_log.append(f"│       Reason: {cr['reasoning']}")
                calc = f"precision@k weighted avg, {relevant_count}/{len(ctx_relevance)} contexts relevant"
                trace_entry["claims_or_statements"] = "\n".join(
                    f"[Rank {cr['context_rank']}] {cr['relevant']}: {cr['context_snippet'][:100]}" for cr in ctx_relevance
                )
                trace_entry["supported_count"] = relevant_count
                trace_entry["unsupported_count"] = len(ctx_relevance) - relevant_count
            else:
                calc = "precision@k weighted avg (no detailed trace — LLM unavailable)"

            print(f"  │   Method: LLM judges if each context is relevant to ground truth, precision@k computed")
            print(f"  │   RAGAS Score: {score_str}")
            history_log.append(f"│")
            history_log.append(f"│   Method: For each context at rank k, LLM judges relevance to ground truth.")
            history_log.append(f"│   Formula: precision@k = (relevant items in top-k) / k")
            history_log.append(f"│   Final: context_precision = weighted_avg(precision@k)")
            history_log.append(f"│   RAGAS Score: {score_str}")
            history_log.append(f"│   Assessment: {assessment}")
            trace_entry["trace_details"] = (
                f"Method: For each context chunk at rank k, LLM judges relevance to ground truth.\n"
                f"precision@k = (relevant items in top-k) / k\n"
                f"Final = weighted average of precision@k values\n"
                f"Ground Truth: {str(gt)}\n"
                f"Contexts: {' | '.join(str(c)[:200] for c in ctx[:5])}"
            )
            trace_entry["calculation"] = f"context_precision = weighted_avg(precision@k) = {score_str}"

        # === CONTEXT RECALL: Ground truth statements vs contexts ===
        elif name == "context_recall" and llm is not None:
            print(f"  │   [Checking GT statements attribution to contexts...]")
            history_log.append(f"│   [Context Recall Evaluation]")
            history_log.append(f"│   [Checking ground truth statements attribution to contexts...]")
            gt_check = _check_gt_statements_in_context(str(gt), [str(c) for c in ctx], llm)
            attributed = sum(1 for s in gt_check if s["attributed"] == "YES")
            unparsed = sum(1 for s in gt_check if s["attributed"] in ("PARSE_FAILED", "ERROR"))
            total_stmts = len(gt_check)

            history_log.append(f"│   Ground Truth Statements ({total_stmts}):")
            for si, s in enumerate(gt_check):
                icon = "✓" if s["attributed"] == "YES" else ("?" if s["attributed"] in ("PARSE_FAILED", "ERROR") else "✗")
                print(f"  │     {icon} [{s['attributed']}] {s['statement'][:100]}")
                print(f"  │       Reason: {s['reasoning'][:120]}")
                history_log.append(f"│     {si+1}. {icon} [{s['attributed']}] {s['statement']}")
                history_log.append(f"│        Reason: {s['reasoning']}")

            if total_stmts == 0:
                calc = "No statements"
            elif unparsed:
                calc = (
                    f"Context Recall breakdown INCOMPLETE ({unparsed}/{total_stmts} statements could not be parsed "
                    f"from the trace judge's response — see reasoning above). This breakdown does NOT determine the "
                    f"score; the RAGAS score above ({score_str}) comes from ragas's own internal scoring, not this trace."
                )
            else:
                calc = f"Context Recall = {attributed}/{total_stmts} GT statements attributed = {attributed/total_stmts:.4f}"
            print(f"  │   Calculation: {calc}")
            print(f"  │   Assessment: {assessment}")
            print(f"  │   RAGAS Score: {score_str}")
            history_log.append(f"│")
            history_log.append(f"│   Calculation: {calc}")
            history_log.append(f"│   Assessment: {assessment}")
            history_log.append(f"│   RAGAS Score: {score_str}")

            trace_entry["claims_or_statements"] = "\n".join(f"[{s['attributed']}] {s['statement']}" for s in gt_check)
            trace_entry["supported_count"] = attributed
            trace_entry["unsupported_count"] = total_stmts - attributed
            trace_entry["calculation"] = calc
            trace_entry["trace_details"] = "\n".join(f"{s['statement']} → {s['attributed']}: {s['reasoning']}" for s in gt_check)

        # === ANSWER CORRECTNESS: Answer vs Ground Truth (TP/FP/FN) ===
        elif name == "answer_correctness" and llm is not None:
            print(f"  │   [Comparing answer claims vs ground truth claims...]")
            history_log.append(f"│   [Answer Correctness Evaluation]")
            history_log.append(f"│   [Comparing answer claims vs ground truth claims...]")
            comparison = _check_answer_vs_ground_truth(str(a), str(gt), llm)
            tp_count = len(comparison["tp"])
            fp_count = len(comparison["fp"])
            fn_count = len(comparison["fn"])

            print(f"  │   TP (correct claims in answer): {tp_count}")
            history_log.append(f"│   TP (correct claims in answer): {tp_count}")
            for claim in comparison["tp"][:10]:
                print(f"  │     ✓ {claim[:100]}")
                history_log.append(f"│     ✓ {claim}")
            print(f"  │   FP (wrong claims in answer): {fp_count}")
            history_log.append(f"│   FP (wrong/extra claims in answer): {fp_count}")
            for claim in comparison["fp"][:10]:
                print(f"  │     ✗ {claim[:100]}")
                history_log.append(f"│     ✗ {claim}")
            print(f"  │   FN (missing from answer): {fn_count}")
            history_log.append(f"│   FN (missing from answer): {fn_count}")
            for claim in comparison["fn"][:10]:
                print(f"  │     ○ {claim[:100]}")
                history_log.append(f"│     ○ {claim}")

            # F1-like calculation
            if tp_count + 0.5 * (fp_count + fn_count) > 0:
                factual_sim = tp_count / (tp_count + 0.5 * (fp_count + fn_count))
            else:
                factual_sim = 0
            calc = (
                f"factual_similarity = TP/(TP + 0.5*(FP+FN)) = {tp_count}/({tp_count} + 0.5*({fp_count}+{fn_count})) = {factual_sim:.4f}\n"
                f"answer_correctness = 0.75*factual + 0.25*semantic ≈ {score_str}"
            )
            print(f"  │   Calculation: {calc}")
            print(f"  │   Assessment: {assessment}")
            print(f"  │   RAGAS Score: {score_str}")
            history_log.append(f"│")
            history_log.append(f"│   Calculation:")
            history_log.append(f"│     factual_similarity = TP/(TP + 0.5*(FP+FN)) = {tp_count}/({tp_count} + 0.5*({fp_count}+{fn_count})) = {factual_sim:.4f}")
            history_log.append(f"│     answer_correctness = 0.75*factual + 0.25*semantic ≈ {score_str}")
            history_log.append(f"│   Assessment: {assessment}")
            history_log.append(f"│   RAGAS Score: {score_str}")

            trace_entry["claims_or_statements"] = (
                "TP: " + "; ".join(comparison["tp"][:10]) + "\n"
                "FP: " + "; ".join(comparison["fp"][:10]) + "\n"
                "FN: " + "; ".join(comparison["fn"][:10])
            )
            trace_entry["supported_count"] = tp_count
            trace_entry["unsupported_count"] = fp_count + fn_count
            trace_entry["calculation"] = calc
            trace_entry["trace_details"] = comparison["raw_response"]

        # === SUMMARIZATION / NOISE SENSITIVITY / OTHER: Logging ===
        else:
            print(f"  │   Answer: {str(a)[:150]}")
            if gt and str(gt) != "N/A":
                print(f"  │   Ground Truth: {str(gt)[:150]}")
            print(f"  │   RAGAS Score: {score_str}")

            if name == "noise_sensitivity":
                history_log.append(f"│   [Noise Sensitivity Evaluation]")
                history_log.append(f"│   Method: Measures robustness against irrelevant context chunks")
                history_log.append(f"│   Formula: noise_sensitivity = (relevant claims from noisy context) / (total claims)")
                history_log.append(f"│   Answer: {str(a)}")
                history_log.append(f"│   Contexts: {' | '.join(str(c)[:200] for c in ctx[:3]) if ctx else 'N/A'}")
                history_log.append(f"│   RAGAS Score: {score_str}")
            elif name == "summarization_score":
                history_log.append(f"│   [Summarization Score Evaluation]")
                history_log.append(f"│   Method: Measures how well the answer captures key info from contexts")
                history_log.append(f"│   Formula: summarization = (key info points captured) / (total key info in contexts)")
                history_log.append(f"│   Answer: {str(a)}")
                history_log.append(f"│   Contexts: {' | '.join(str(c)[:200] for c in ctx[:3]) if ctx else 'N/A'}")
                history_log.append(f"│   RAGAS Score: {score_str}")
            else:
                history_log.append(f"│   [{name} Evaluation]")
                history_log.append(f"│   Answer: {str(a)}")
                if gt and str(gt) != "N/A":
                    history_log.append(f"│   Ground Truth: {str(gt)}")
                history_log.append(f"│   RAGAS Score: {score_str}")

            trace_entry["trace_details"] = f"Score computed by RAGAS evaluate(). Answer: {str(a)}"
            trace_entry["calculation"] = f"{name} = {score_str}"

        # Finalize the history_log and store
        history_log.append(f"{'='*60}")
        trace_entry["history_log"] = "\n".join(history_log)
        _TRACE_LOGS.append(trace_entry)

        # --- Build comparison history entry for the "Comparison History" sheet ---
        comparison_entry = {
            "row": i + 1,
            "metric": name,
            "score": float(score) if not pd.isna(score) else None,
            "score_assessment": assessment,
            "question": str(q),
            # Full text comparisons:
            "answer_text": str(a),
            "context_text": "\n---\n".join(str(c) for c in ctx[:5]) if ctx else "N/A",
            "ground_truth_text": str(gt),
            # Dimension-specific comparison narratives:
            "answer_vs_context": "",
            "answer_vs_ground_truth": "",
            "context_vs_question": "",
            "evaluation_reasoning": trace_entry.get("trace_details", ""),
        }

        # Populate the dimension comparisons based on metric type
        if name == "faithfulness":
            comparison_entry["answer_vs_context"] = (
                f"[FAITHFULNESS — Answer vs Context]\n"
                f"The answer was decomposed into atomic claims.\n"
                f"Each claim was checked against the retrieved contexts.\n"
                + trace_entry.get("claims_or_statements", "")
            )
        elif name == "answer_relevancy":
            comparison_entry["answer_vs_context"] = (
                f"[ANSWER RELEVANCY — Answer vs Question]\n"
                f"Generated questions from answer are compared via embedding similarity to original question.\n"
                f"Original Question: {str(q)}\n"
                f"Answer: {str(a)}"
            )
        elif name == "context_precision":
            comparison_entry["context_vs_question"] = (
                f"[CONTEXT PRECISION — Context vs Question/Ground Truth]\n"
                f"Each retrieved context was evaluated for relevance.\n"
                + trace_entry.get("claims_or_statements", "")
            )
        elif name == "context_recall":
            comparison_entry["answer_vs_ground_truth"] = (
                f"[CONTEXT RECALL — Context vs Ground Truth]\n"
                f"Ground truth statements were checked for attribution to contexts.\n"
                + trace_entry.get("claims_or_statements", "")
            )
        elif name == "answer_correctness":
            comparison_entry["answer_vs_ground_truth"] = (
                f"[ANSWER CORRECTNESS — Answer vs Ground Truth]\n"
                + trace_entry.get("claims_or_statements", "")
            )

        _COMPARISON_HISTORY.append(comparison_entry)

    # Summary stats
    valid_scores = [s for s in values if not pd.isna(s)]
    if valid_scores:
        pass_count = sum(1 for s in valid_scores if s >= 0.8)
        partial_count = sum(1 for s in valid_scores if 0.5 <= s < 0.8)
        fail_count = sum(1 for s in valid_scores if s < 0.5)
        print(f"  │")
        print(f"  │ Summary: {pass_count} PASS, {partial_count} PARTIAL, {fail_count} FAIL "
              f"(avg={sum(valid_scores)/len(valid_scores):.4f})")
    print(f"  └── End [{name}] ──\n")


def run_ragas_metrics(results_df: pd.DataFrame) -> pd.DataFrame:
    """Run RAGAS evaluation metrics on collected results.

    Each metric is scored in its own isolated evaluate() call (see _score_one_metric)
    so that one metric's incompatibility with the dataset never wipes out the others.

    Also verifies that questions are included in the ground truth evaluation dataset.

    Produces:
    - Per-row scores for each metric
    - Detailed trace logs (_TRACE_LOGS) with history of how each score was derived
    - Comparison history (_COMPARISON_HISTORY) with full text comparisons:
      * Answer vs Context (faithfulness)
      * Answer vs Ground Truth (answer_correctness, context_recall)
      * Context vs Question (context_precision, answer_relevancy)
    - Score assessments with diagnostic recommendations
    """
    global _TRACE_LOGS, _COMPARISON_HISTORY
    _TRACE_LOGS = []
    _COMPARISON_HISTORY = []

    if not RAGAS_AVAILABLE:
        print("\n  ╔══════════════════════════════════════════════════════════════╗")
        print("  ║  RAGAS NOT USABLE - No metrics will be computed!            ║")
        print("  ╚══════════════════════════════════════════════════════════════╝")
        print(f"  Reason: {RAGAS_IMPORT_ERROR}")
        print("  If this is a missing-module error, install with:")
        print("    uv pip install ragas datasets langchain-aws")
        print("  If it's an ImportError/AttributeError naming something inside ragas.metrics,")
        print("  your installed ragas version renamed or removed that symbol - run:")
        print("    uv run python -c \"import ragas; print(ragas.__version__)\"")
        print("  and check docs.ragas.io for that version's metric names.")
        return results_df

    from datasets import Dataset

    llm, embeddings = _setup_judge()

    # --- Verify questions are included and matched with ground truth ---
    print("\n  ── Question ↔ Ground Truth Verification ──")
    for i, row in results_df.iterrows():
        q = row["question"]
        gt = row["ground_truth"]
        q_short = q[:60] + "..." if len(str(q)) > 60 else q
        gt_short = gt[:60] + "..." if len(str(gt)) > 60 else gt
        has_gt = bool(gt and str(gt).strip() and str(gt).strip().lower() != "nan")
        status = "✓" if has_gt else "✗ MISSING"
        print(f"    [{i+1}] {status} Q: {q_short}")
        if has_gt:
            print(f"         GT: {gt_short}")
        else:
            print(f"         GT: ⚠ No ground truth provided - metrics requiring reference will be inaccurate!")
    print(f"  ── Total: {len(results_df)} questions, "
          f"{sum(1 for _, r in results_df.iterrows() if str(r['ground_truth']).strip() and str(r['ground_truth']).strip().lower() != 'nan')} with ground truth ──\n")

    # --- Standard RAG dataset for RAGAS 0.2.x ---------
    # ragas 0.2.x expects: user_input, response, retrieved_contexts, reference
    eval_data = {
        "user_input": results_df["question"].tolist(),
        "response": results_df["answer"].tolist(),
        "retrieved_contexts": results_df["contexts"].tolist(),
        "reference": results_df["ground_truth"].tolist(),
    }
    dataset = Dataset.from_dict(eval_data)

    metric_specs = [
        ("faithfulness", faithfulness),
        ("answer_relevancy", answer_relevancy),
        ("context_precision", context_precision),
        ("context_recall", context_recall),
    ]

    try:
        from ragas.metrics import answer_correctness
        metric_specs.append(("answer_correctness", answer_correctness))
    except (ImportError, AttributeError):
        print("  [!] answer_correctness not available in this ragas version")

    try:
        from ragas.metrics import NoiseSensitivity
        # Previously disabled for a "string dtype" bug — that turned out to be a symptom of
        # ragas_env having an unpinned/mismatched dependency set (ragas 0.4.3 alongside
        # langchain/pandas/numpy versions never tested with it), not an upstream ragas bug.
        # Verified working (incl. single-context and no-context-retrieved edge cases) after
        # reinstalling the pinned requirements-ragas.txt set (ragas==0.2.15).
        metric_specs.append(("noise_sensitivity", NoiseSensitivity()))
    except (ImportError, AttributeError):
        print("  [!] noise_sensitivity not available in this ragas version")

    print(f"\n  Metrics to compute: {[n for n, _ in metric_specs]} + summarization_score")
    print(f"  Questions to evaluate: {len(results_df)}")
    print("  Running... (each metric is scored independently, this takes several minutes)\n")

    computed = {}
    for name, metric in metric_specs:
        values = _score_one_metric(name, metric, dataset, llm, embeddings)
        if values is not None:
            computed[name] = values

    # --- Summarization Score: DIFFERENT schema (response + reference_contexts) -----
    # This is the metric that used to silently kill every other optional metric:
    # it needs "reference_contexts", a field the standard RAG dataset never had, so
    # batching it together with the rest raised an exception for the whole group.
    # It measures whether the generated answer ("response") captures the key
    # information present in the retrieved chunks ("reference_contexts").
    SummarizationMetric = None
    try:
        from ragas.metrics import SummarizationScore as SummarizationMetric
    except ImportError:
        try:
            from ragas.metrics import summarization_score as SummarizationMetric
        except (ImportError, AttributeError):
            print("  [!] summarization_score not available in this ragas version")

    if SummarizationMetric is not None:
        summ_dataset = Dataset.from_dict({
            "response": results_df["answer"].tolist(),
            "reference_contexts": results_df["contexts"].tolist(),
        })
        metric_instance = SummarizationMetric() if isinstance(SummarizationMetric, type) else SummarizationMetric
        values = _score_one_metric("summarization_score", metric_instance, summ_dataset, llm, embeddings)
        if values is not None:
            computed["summarization_score"] = values

    # Attach computed metric columns to the dataframe
    for col, values in computed.items():
        results_df[col] = values

    # Print summary table
    if computed:
        print("\n  ┌─────────────────────────────┬────────┐")
        print("  │ Metric                      │ Score  │")
        print("  ├─────────────────────────────┼────────┤")
        for col in computed:
            avg = results_df[col].mean()
            if pd.isna(avg):
                print(f"  │ {col:27s} │  N/A   │")
            else:
                bar = "█" * int(avg * 10) + "░" * (10 - int(avg * 10))
                print(f"  │ {col:27s} │ {avg:.4f} │ {bar}")
        print("  └─────────────────────────────┴────────┘")
    else:
        print("\n  ✗ No metrics were successfully computed.")
        print("  To fix, ensure one of:")
        print("    - AWS credentials configured for Bedrock (ap-southeast-3)")
        print("    - Or set OPENAI_API_KEY environment variable")

    return results_df


# ==============================================================================
# MAIN
# ==============================================================================

async def evaluate_sheet(client: RAGClient, df: pd.DataFrame, domain: str, sheet_name: str):
    """Evaluate one sheet (one domain) through the RAG API."""
    agent_name = "People Care Agent" if domain == "hr" else "Contact Center Agent"
    print(f"\n{'='*60}")
    print(f"  Sheet: '{sheet_name}' → Domain: {domain} ({agent_name})")
    print(f"  Questions: {len(df)}")
    print(f"{'='*60}")

    answers = []
    all_contexts = []
    all_chunk_details = []
    latencies = []

    for i, row in df.iterrows():
        question = row["question"]
        print(f"  [{i+1}/{len(df)}] {question[:55]}...", end=" ")
        start = time.time()

        try:
            result = await client.ask(question, domain=domain)
            elapsed = time.time() - start
            answers.append(result["answer"])
            all_contexts.append(result["contexts"])
            all_chunk_details.append(result["chunk_details"])
            latencies.append(elapsed)
            ctx_status = f"✓ {len(result['chunk_details'])} chunks (metadata)" if result.get("has_citations") else "⚠ no citations"
            print(f"OK ({elapsed:.1f}s) [{ctx_status}]")
        except Exception as e:
            elapsed = time.time() - start
            answers.append(f"ERROR: {e}")
            all_contexts.append(["No context - API error"])
            all_chunk_details.append([])
            latencies.append(elapsed)
            print(f"FAILED ({e})")

    # Build results
    results_df = pd.DataFrame({
        "question": df["question"].tolist(),
        "ground_truth": df["ground_truth"].tolist(),
        "answer": answers,
        "contexts": all_contexts,
        "chunk_details": all_chunk_details,
        "domain": domain,
        "agent": agent_name,
        "latency_seconds": latencies,
    })

    return results_df


async def main(args):
    print("=" * 60)
    print("PermataBank AI Platform - RAGAS Evaluation")
    print(f"API: {args.api_url}")
    print(f"Dataset: {args.excel}")
    print("=" * 60)

    # 1. Load Excel
    print(f"\n1. Loading golden dataset: {args.excel}")
    xl = pd.ExcelFile(args.excel)
    print(f"   Sheets found: {xl.sheet_names}")

    # Column mapping
    col_mapping = {
        "Test Scenario": "question",
        "test scenario": "question",
        "Query": "question",
        "query": "question",
        "Expected Result": "ground_truth",
        "expected result": "ground_truth",
        "Expected Answer": "ground_truth",
        "Ground Truth": "ground_truth",
    }

    # Determine which sheets to evaluate
    sheets_to_eval = {}
    if args.domain:
        # Single domain mode
        for sheet_name, domain_val in SHEET_TO_DOMAIN.items():
            if domain_val == args.domain and sheet_name in xl.sheet_names:
                sheets_to_eval[sheet_name] = domain_val
                break
        if not sheets_to_eval:
            # Fuzzy match
            for sheet_name in xl.sheet_names:
                if args.domain.replace("_", " ").lower() in sheet_name.lower():
                    sheets_to_eval[sheet_name] = args.domain
                    break
    else:
        # All sheets
        for sheet_name in xl.sheet_names:
            if sheet_name in SHEET_TO_DOMAIN:
                sheets_to_eval[sheet_name] = SHEET_TO_DOMAIN[sheet_name]

    if not sheets_to_eval:
        print(f"   ERROR: No matching sheets found for domain='{args.domain}'")
        print(f"   Available: {xl.sheet_names}")
        return

    print(f"   Will evaluate: {sheets_to_eval}")

    # 2. Login & Evaluate each sheet with ALL users per domain
    all_results = []

    for sheet_name, domain in sheets_to_eval.items():
        df = pd.read_excel(args.excel, sheet_name=sheet_name)
        df.rename(columns={k: v for k, v in col_mapping.items() if k in df.columns}, inplace=True)

        if "question" not in df.columns or "ground_truth" not in df.columns:
            print(f"   SKIP '{sheet_name}': missing 'question'/'ground_truth' columns")
            print(f"   Found: {list(df.columns)}")
            continue

        # Cleanup
        df = df.dropna(subset=["question", "ground_truth"]).reset_index(drop=True)
        df["question"] = df["question"].astype(str)
        df["ground_truth"] = df["ground_truth"].astype(str)

        # Get user list for this domain
        domain_users = CREDENTIALS.get(domain, [])
        if args.email and args.password:
            # Override: single user specified via CLI
            domain_users = [{"email": args.email, "password": args.password, "role": "cli_override"}]

        # Evaluate with EACH user
        for user_creds in domain_users:
            print(f"\n  Logging in as {user_creds['email']} ({user_creds['role']}) for {domain}...")
            client = RAGClient(args.api_url)
            try:
                await client.login(user_creds["email"], user_creds["password"])
            except Exception as e:
                print(f"   ERROR: Login failed for {user_creds['email']}: {e}")
                continue

            results = await evaluate_sheet(client, df, domain, sheet_name)
            # Add user info to results
            results["user_email"] = user_creds["email"]
            results["user_role"] = user_creds["role"]
            all_results.append(results)
            await client.close()

    if not all_results:
        print("   No results collected. Check your Excel format.")
        return

    # 4. Combine results
    combined_df = pd.concat(all_results, ignore_index=True)
    print(f"\n3. Total questions evaluated: {len(combined_df)}")

    # 5. Run RAGAS metrics
    print("\n4. Running RAGAS evaluation...")
    error_rows = combined_df["answer"].str.startswith("ERROR:").sum()
    if error_rows > 0:
        print(f"   WARNING: {error_rows} questions failed with API errors.")
        print(f"   RAGAS will only evaluate {len(combined_df) - error_rows} successful answers.")
        # Filter out error rows for RAGAS (keep them in final output)
        eval_mask = ~combined_df["answer"].str.startswith("ERROR:")
        eval_df = combined_df[eval_mask].reset_index(drop=True)
    else:
        eval_df = combined_df.copy()
        eval_mask = pd.Series([True] * len(combined_df))

    if len(eval_df) > 0:
        scored_df = run_ragas_metrics(eval_df)
        # Merge scores back into combined_df
        metric_cols = [c for c in scored_df.columns if c not in combined_df.columns]
        for col in metric_cols:
            combined_df.loc[eval_mask.values, col] = scored_df[col].values

        # --- Merge history logs from _TRACE_LOGS into combined_df per metric ---
        # Build per-row history log columns from _TRACE_LOGS
        metric_log_columns = {
            "faithfulness": "faithfulness_log",
            "answer_relevancy": "answer_relevancy_log",
            "context_precision": "context_precision_log",
            "context_recall": "context_recall_log",
            "answer_correctness": "answer_correctness_log",
            "noise_sensitivity": "noise_sensitivity_log",
            "summarization_score": "summarization_score_log",
        }
        # Initialize log columns
        for log_col in metric_log_columns.values():
            combined_df[log_col] = ""

        # Populate from _TRACE_LOGS
        for trace in _TRACE_LOGS:
            metric_name = trace.get("metric", "")
            row_idx = trace.get("row", 0) - 1  # convert to 0-indexed
            log_col = metric_log_columns.get(metric_name)
            if log_col and 0 <= row_idx < len(combined_df):
                # Use eval_mask to map back to combined_df index
                eval_indices = combined_df.index[eval_mask.values].tolist()
                if row_idx < len(eval_indices):
                    target_idx = eval_indices[row_idx]
                    combined_df.at[target_idx, log_col] = trace.get("history_log", "")

        # --- Generate score assessment columns for each metric ---
        assessment_metrics = [
            "faithfulness", "answer_relevancy", "context_precision",
            "context_recall", "answer_correctness", "noise_sensitivity",
            "summarization_score",
        ]
        for m in assessment_metrics:
            assessment_col = f"{m}_assessment"
            if m in combined_df.columns:
                combined_df[assessment_col] = combined_df[m].apply(
                    lambda score, metric=m: _build_score_assessment(metric, score)
                )
            else:
                combined_df[assessment_col] = ""
    else:
        print("   No successful answers to evaluate. Check API connectivity.")

    # 6. Save results
    output = args.output
    Path(output).resolve().parent.mkdir(parents=True, exist_ok=True)
    export_df = combined_df.copy()

    # Add RAGAS 0.2.x field names as explicit columns
    # IMPORTANT: These are DIFFERENT data sources:
    #   - response = the GENERATED answer from the AI agent (LLM output)
    #   - retrieved_contexts = the real retrieved chunk body when the deployed
    #     agentic-chatbot-service honors include_chunk_text=True, else a metadata
    #     proxy (doc/section/page/score/type/caption per citation) for any chunk
    #     missing chunk_text (see _build_pseudo_context docstring) — always
    #     traceable to a specific chunk, but only real content when chunk_text
    #     was actually returned.
    # They should NEVER look like plain duplicates of each other.
    export_df["user_input"] = export_df["question"]
    export_df["response"] = export_df["answer"]  # Generated by agent (LLM)
    export_df["retrieved_contexts"] = export_df["contexts"].apply(
        lambda x: " | ".join(x) if isinstance(x, list) else str(x)
    )
    export_df["reference"] = export_df["ground_truth"]

    # Per-chunk structured detail (doc_title/doc_id/section_path/page_number/
    # chunk_type/caption/score + originating service) so a score can be traced
    # back to exactly which KB chunk(s) produced it.
    export_df["retrieved_chunks_detail"] = export_df["chunk_details"].apply(
        lambda chunks: "\n".join(
            f"[{i+1}] service={d.get('service')} | doc_title={d.get('doc_title')} | "
            f"doc_id={d.get('doc_id')} | section_path={d.get('section_path')} | "
            f"page_number={d.get('page_number')} | chunk_type={d.get('chunk_type')} | "
            f"caption={d.get('caption')} | score={d.get('score')} | "
            f"chunk_text={d.get('chunk_text') or '(none — metadata proxy only)'}"
            for i, d in enumerate(chunks)
        ) if isinstance(chunks, list) and chunks else ""
    )

    # Flag rows where the agent returned zero citations (a real retrieval gap,
    # not a data-format issue — every non-empty citation now produces a
    # metadata-proxy context, so this only fires on genuinely empty retrieval).
    export_df["retrieval_warning"] = export_df.apply(
        lambda row: "⚠ NO CITATIONS — agent returned zero citations for this question"
        if isinstance(row["contexts"], list) and any("[NO CITATIONS RETURNED" in str(c) for c in row["contexts"])
        else "",
        axis=1,
    )

    export_df["contexts"] = export_df["contexts"].apply(
        lambda x: " | ".join(x) if isinstance(x, list) else str(x)
    )
    # Drop the raw list-of-dicts column now that retrieved_chunks_detail holds
    # its formatted text — leaving it in would export as an unreadable str(list).
    export_df = export_df.drop(columns=["chunk_details"], errors="ignore")

    # Friendly headers + fixed column order for the assessment sheet. Any metric
    # that failed to compute simply won't be in export_df, so it's skipped here
    # instead of raising - the rest of the report still gets saved.
    column_order = [
        "question", "ground_truth", "answer",
        "user_input", "response", "retrieved_contexts", "retrieved_chunks_detail", "reference",
        "retrieval_warning",
        "domain", "agent", "user_email", "user_role", "latency_seconds",
        "faithfulness", "faithfulness_assessment", "faithfulness_log",
        "answer_relevancy", "answer_relevancy_assessment", "answer_relevancy_log",
        "context_precision", "context_precision_assessment", "context_precision_log",
        "context_recall", "context_recall_assessment", "context_recall_log",
        "contexts",
        "answer_correctness", "answer_correctness_assessment", "answer_correctness_log",
        "noise_sensitivity", "noise_sensitivity_assessment", "noise_sensitivity_log",
        "summarization_score", "summarization_score_assessment", "summarization_score_log",
    ]
    column_labels = {
        "question": "Question",
        "ground_truth": "Ground Truth",
        "answer": "Answer (Generated by Agent)",
        "user_input": "RAGAS user_input",
        "response": "RAGAS response (= Agent Answer)",
        "retrieved_contexts": "RAGAS retrieved_contexts (= KB Chunks, real text or metadata proxy)",
        "retrieved_chunks_detail": "Retrieved Chunks Detail (per-chunk: service, doc, section, page, type, score, chunk_text)",
        "reference": "RAGAS reference (= Ground Truth)",
        "retrieval_warning": "Retrieval Warning",
        "domain": "Domain",
        "agent": "Agent",
        "user_email": "User Email",
        "user_role": "User Role",
        "latency_seconds": "Latency (s)",
        "faithfulness": "Faithfulness",
        "faithfulness_assessment": "Faithfulness Assessment",
        "faithfulness_log": "Faithfulness Log (Claims & Verdicts)",
        "answer_relevancy": "Answer Relevancy",
        "answer_relevancy_assessment": "Answer Relevancy Assessment",
        "answer_relevancy_log": "Answer Relevancy Log (Comparison)",
        "context_precision": "Context Precision",
        "context_precision_assessment": "Context Precision Assessment",
        "context_precision_log": "Context Precision Log (Ranking)",
        "context_recall": "Context Recall",
        "context_recall_assessment": "Context Recall Assessment",
        "context_recall_log": "Context Recall Log (GT Attribution)",
        "contexts": "Context (Raw Chunks)",
        "answer_correctness": "Answer Correctness",
        "answer_correctness_assessment": "Answer Correctness Assessment",
        "answer_correctness_log": "Answer Correctness Log (TP/FP/FN)",
        "noise_sensitivity": "Noise Sensitivity",
        "noise_sensitivity_assessment": "Noise Sensitivity Assessment",
        "noise_sensitivity_log": "Noise Sensitivity Log",
        "summarization_score": "Summarization Score",
        "summarization_score_assessment": "Summarization Score Assessment",
        "summarization_score_log": "Summarization Score Log",
    }
    ordered_cols = [c for c in column_order if c in export_df.columns]
    ordered_cols += [c for c in export_df.columns if c not in ordered_cols]  # keep anything unexpected
    export_df = export_df[ordered_cols].rename(columns=column_labels)

    # Build a per-domain (+ overall) Summary tab so the conclusion doesn't require
    # scrolling every row - only includes metric columns that actually got computed.
    metric_keys = ["faithfulness", "answer_relevancy", "context_precision", "context_recall",
                    "answer_correctness", "noise_sensitivity", "summarization_score"]
    present_metrics = [m for m in metric_keys if m in combined_df.columns]

    summary_rows = []
    for domain in combined_df["domain"].unique():
        subset = combined_df[combined_df["domain"] == domain]
        row = {
            "Domain": domain,
            "Agent": subset["agent"].iloc[0],
            "Questions": len(subset),
            "Avg Latency (s)": round(subset["latency_seconds"].mean(), 2),
        }
        for m in present_metrics:
            row[column_labels[m]] = round(subset[m].mean(), 4)
        summary_rows.append(row)

    overall_row = {
        "Domain": "OVERALL",
        "Agent": "-",
        "Questions": len(combined_df),
        "Avg Latency (s)": round(combined_df["latency_seconds"].mean(), 2),
    }
    for m in present_metrics:
        overall_row[column_labels[m]] = round(combined_df[m].mean(), 4)
    summary_rows.append(overall_row)

    summary_df = pd.DataFrame(summary_rows)
    missing_for_note = [column_labels[m] for m in metric_keys if m not in present_metrics]
    if missing_for_note:
        note_df = pd.DataFrame({"Note": [
            f"Not computed this run (see console log for the reason): {', '.join(missing_for_note)}"
        ]})
    else:
        note_df = None

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Summary", index=False)
        if note_df is not None:
            note_df.to_excel(writer, sheet_name="Summary", index=False,
                              startrow=len(summary_df) + 2)
        export_df.to_excel(writer, sheet_name="Results", index=False)

        # --- Add Formulas sheet explaining each metric calculation ---
        formulas_data = {
            "Metric": [
                "Faithfulness",
                "Answer Relevancy",
                "Context Precision",
                "Context Recall",
                "Answer Correctness",
                "Noise Sensitivity",
                "Summarization Score",
                "Latency (s)",
            ],
            "Formula / Calculation": [
                "Faithfulness = (Number of claims in answer supported by context) / (Total number of claims in answer). "
                "The LLM extracts atomic claims from the answer, then checks each claim against retrieved contexts.",
                "Answer Relevancy = mean(cosine_similarity(generated_questions, original_question)). "
                "The LLM generates N questions from the answer; their embedding similarity to the original question is averaged.",
                "Context Precision = (Weighted sum of precision@k for relevant contexts) / (Total relevant contexts). "
                "Measures if relevant context chunks are ranked higher than irrelevant ones (uses ground truth).",
                "Context Recall = (Number of ground truth statements attributable to context) / (Total ground truth statements). "
                "The LLM checks each sentence in ground_truth to see if it can be attributed to the retrieved contexts.",
                "Answer Correctness = weighted_avg(factual_similarity, semantic_similarity). "
                "factual_similarity = (TP) / (TP + 0.5*(FP + FN)) where TP/FP/FN are factual statement overlaps between answer and ground_truth. "
                "semantic_similarity = cosine_similarity(answer_embedding, ground_truth_embedding). Default weight: 0.75 factual + 0.25 semantic.",
                "Noise Sensitivity = (Number of relevant claims from noisy context) / (Total claims). "
                "Measures robustness against irrelevant context chunks.",
                "Summarization Score = (Number of key information points from contexts captured in answer) / (Total key info points in contexts). "
                "Measures how well the answer summarizes the retrieved contexts.",
                "Latency (s) = time_end - time_start for the HTTP POST /api/v1/chat request. "
                "Measured using Python time.time() around the actual API call to the platform at: "
                f"{API_URL}",
            ],
            "Comparison Basis": [
                "Answer (generated by agent) vs Retrieved Contexts (KB chunks)",
                "Answer (generated by agent) vs Original Question (embedding similarity)",
                "Retrieved Contexts (KB chunks) ranking vs Ground Truth",
                "Retrieved Contexts (KB chunks) vs Ground Truth (statement attribution)",
                "Answer (generated by agent) vs Ground Truth (factual + semantic)",
                "Answer with clean context vs Answer with noisy context",
                "Answer (generated by agent) vs Retrieved Contexts (information coverage)",
                "Wall-clock time for API response",
            ],
            "RAGAS Input Fields": [
                "response + retrieved_contexts",
                "user_input + response",
                "retrieved_contexts + reference",
                "retrieved_contexts + reference",
                "response + reference",
                "response + retrieved_contexts (with noise injection)",
                "response + retrieved_contexts",
                "N/A (measured externally)",
            ],
            "Data Source": [
                "response = POST /api/v1/chat → answer field; retrieved_contexts = chunk_text from citations[] when the deployed service honors include_chunk_text=True, else a metadata proxy (doc_title/section_path/page_number/score/chunk_type/caption — see Data Sources sheet)",
                "user_input = Excel 'Test Scenario'; response = POST /api/v1/chat → answer field",
                "retrieved_contexts = chunk_text from citations[] when available, else a metadata proxy; reference = Excel 'Expected Result'",
                "retrieved_contexts = chunk_text from citations[] when available, else a metadata proxy; reference = Excel 'Expected Result'",
                "response = POST /api/v1/chat → answer field; reference = Excel 'Expected Result'",
                "response = POST /api/v1/chat → answer field; contexts with injected noise",
                "response = POST /api/v1/chat → answer field; retrieved_contexts = chunk_text from citations[] when available, else a metadata proxy",
                "time.time() around HTTP POST /api/v1/chat",
            ],
            "Score Range": [
                "0.0 - 1.0 (higher = more faithful)",
                "0.0 - 1.0 (higher = more relevant)",
                "0.0 - 1.0 (higher = better ranking)",
                "0.0 - 1.0 (higher = better recall)",
                "0.0 - 1.0 (higher = more correct)",
                "0.0 - 1.0 (lower = more robust)",
                "0.0 - 1.0 (higher = better summary)",
                "Seconds (lower = faster)",
            ],
            "Pass Threshold": [
                "≥ 0.8",
                "≥ 0.8",
                "≥ 0.8",
                "≥ 0.8",
                "≥ 0.8",
                "≤ 0.2 (inverse)",
                "≥ 0.8",
                "< 10s recommended",
            ],
        }
        formulas_df = pd.DataFrame(formulas_data)
        formulas_df.to_excel(writer, sheet_name="Formulas", index=False)

        # --- Add Trace sheet with full scoring traceability ---
        if _TRACE_LOGS:
            trace_df = pd.DataFrame(_TRACE_LOGS)
            trace_col_order = [
                "row", "metric", "score", "verdict", "score_assessment", "question", "answer",
                "ground_truth", "contexts", "claims_or_statements",
                "supported_count", "unsupported_count", "calculation",
                "trace_details", "history_log",
            ]
            trace_col_labels = {
                "row": "Row #",
                "metric": "Metric",
                "score": "Score",
                "verdict": "Verdict",
                "score_assessment": "Score Assessment & Diagnosis",
                "question": "Question",
                "answer": "Answer",
                "ground_truth": "Ground Truth",
                "contexts": "Retrieved Contexts",
                "claims_or_statements": "Claims / Statements Extracted",
                "supported_count": "Supported Count",
                "unsupported_count": "Unsupported Count",
                "calculation": "Calculation Formula",
                "trace_details": "Full Trace (LLM Reasoning)",
                "history_log": "History Log (Full Console Output)",
            }
            trace_cols_present = [c for c in trace_col_order if c in trace_df.columns]
            trace_df = trace_df[trace_cols_present].rename(columns=trace_col_labels)
            trace_df.to_excel(writer, sheet_name="Trace", index=False)
            print(f"   Trace sheet: {len(_TRACE_LOGS)} trace entries written")

        # --- Add Comparison History sheet (full text comparison for each dimension) ---
        if _COMPARISON_HISTORY:
            comp_df = pd.DataFrame(_COMPARISON_HISTORY)
            comp_col_order = [
                "row", "metric", "score", "score_assessment",
                "question", "answer_text", "context_text", "ground_truth_text",
                "answer_vs_context", "answer_vs_ground_truth", "context_vs_question",
                "evaluation_reasoning",
            ]
            comp_col_labels = {
                "row": "Row #",
                "metric": "Metric",
                "score": "Score",
                "score_assessment": "Score Assessment & Diagnosis",
                "question": "Question",
                "answer_text": "Answer (Full Text)",
                "context_text": "Retrieved Contexts (Full Text)",
                "ground_truth_text": "Ground Truth (Full Text)",
                "answer_vs_context": "Answer vs Context (Comparison History)",
                "answer_vs_ground_truth": "Answer vs Ground Truth (Comparison History)",
                "context_vs_question": "Context vs Question (Comparison History)",
                "evaluation_reasoning": "Evaluation Reasoning (LLM Judge)",
            }
            comp_cols_present = [c for c in comp_col_order if c in comp_df.columns]
            comp_df = comp_df[comp_cols_present].rename(columns=comp_col_labels)
            comp_df.to_excel(writer, sheet_name="Comparison History", index=False)
            print(f"   Comparison History sheet: {len(_COMPARISON_HISTORY)} entries written")

        # --- Add Data Sources explanation sheet ---
        data_sources_data = {
            "RAGAS Field": [
                "user_input",
                "response",
                "retrieved_contexts",
                "retrieved_chunks_detail",
                "reference",
            ],
            "What It Contains": [
                "The user's question / test scenario from the golden dataset Excel",
                "The GENERATED answer from the AI agent (LLM output after full RAG pipeline)",
                "The REAL retrieved chunk body when the deployed agentic-chatbot-service honors include_chunk_text=True, else a METADATA PROXY (doc/section/page/score/type/caption per citation) for any chunk missing chunk_text",
                "The same per-chunk metadata as retrieved_contexts, but as one line per chunk (unabbreviated) with the originating service tagged, PLUS the raw chunk_text field itself, for score tracing",
                "The expected answer (ground truth) from the golden dataset Excel",
            ],
            "Source": [
                "Excel → 'Test Scenario' column",
                "POST /api/v1/chat → response body → 'answer' field",
                "POST /api/v1/chat → response body → 'citations[]' metadata fields (doc_title/section_path/page_number/score/chunk_type/caption/doc_id)",
                "Same as retrieved_contexts, formatted per-chunk via _extract_chunk_detail() (now includes chunk_text)",
                "Excel → 'Expected Result' column",
            ],
            "Used By Metrics": [
                "answer_relevancy (compared to response embeddings)",
                "faithfulness (claims checked vs contexts), answer_relevancy, answer_correctness, noise_sensitivity",
                "faithfulness (evidence for claims), context_precision (ranked relevance), context_recall (GT attribution), summarization_score",
                "Not fed into scoring — reference-only column for tracing which document/chunk/service produced a given score, including the exact chunk_text used (or lack thereof) per chunk",
                "context_precision (relevance judge), context_recall (attribution target), answer_correctness (F1 comparison)",
            ],
            "IMPORTANT": [
                "This is the INPUT question — same across all metrics",
                "This is the AGENT's answer — DIFFERENT from retrieved_contexts!",
                "Every request sends include_chunk_text=True (see RAGClient.ask()). The agentic-chatbot-service "
                "Citation model (app/api/v1/chat/schemas.py) then populates chunk_text with the real retrieved "
                "chunk body — but only if the deployed service has this flag wired through build_initial_state "
                "and generate._extract_citations. Any citation still missing chunk_text (older/un-redeployed "
                "service) falls back to a metadata-only proxy for that chunk, which RAGAS can only score as an "
                "approximation, not true content grounding — check the Faithfulness/Context Recall/Precision "
                "scores against this column per-row if they look surprisingly low despite a high retrieval score.",
                "Use this column to trace a low/high score back to the exact document, section, page, and service that produced it",
                "This is the HUMAN-WRITTEN expected answer — the gold standard for correctness evaluation",
            ],
        }
        data_sources_df = pd.DataFrame(data_sources_data)
        data_sources_df.to_excel(writer, sheet_name="Data Sources", index=False)

        # Auto-adjust column widths for readability
        for sheet_name_ws in writer.sheets:
            ws = writer.sheets[sheet_name_ws]
            for col_cells in ws.columns:
                max_length = 0
                col_letter = col_cells[0].column_letter
                for cell in col_cells:
                    if cell.value:
                        max_length = max(max_length, min(len(str(cell.value)), 60))
                ws.column_dimensions[col_letter].width = max_length + 2

    print(f"\n5. Results saved to: {output} (sheets: Summary, Results, Formulas, Trace, Comparison History, Data Sources)")
    print(f"   Total: {len(combined_df)} questions")
    print(f"   Avg latency: {combined_df['latency_seconds'].mean():.1f}s")

    metric_display = {
        "faithfulness": "Faithfulness",
        "answer_relevancy": "Answer Relevancy",
        "context_precision": "Context Precision",
        "context_recall": "Context Recall",
        "answer_correctness": "Answer Correctness",
        "noise_sensitivity": "Noise Sensitivity",
        "summarization_score": "Summarization Score",
    }
    missing_metrics = [label for key, label in metric_display.items() if key not in combined_df.columns]
    if missing_metrics:
        print(f"   [!] Not computed this run (see logs above for why): {', '.join(missing_metrics)}")

    # Per-domain summary
    for domain in combined_df["domain"].unique():
        subset = combined_df[combined_df["domain"] == domain]
        agent = subset["agent"].iloc[0]
        print(f"\n   {agent} ({domain}): {len(subset)} total evaluations, "
              f"avg latency {subset['latency_seconds'].mean():.1f}s")
        for key, label in metric_display.items():
            if key in subset.columns:
                print(f"     {label}: {subset[key].mean():.4f}")

        # Per-user breakdown within this domain
        if "user_email" in subset.columns:
            print(f"\n     Per-user breakdown:")
            for user_email in subset["user_email"].unique():
                user_subset = subset[subset["user_email"] == user_email]
                user_role = user_subset["user_role"].iloc[0] if "user_role" in user_subset.columns else ""
                print(f"       {user_email} ({user_role}): {len(user_subset)} questions, "
                      f"latency {user_subset['latency_seconds'].mean():.1f}s")
                for key, label in metric_display.items():
                    if key in user_subset.columns and user_subset[key].notna().any():
                        print(f"         {label}: {user_subset[key].mean():.4f}")


class _Tee:
    """Mirrors everything written to stdout into a log file as well, so the full
    run narrative (retries, per-metric traces, calculation logs) survives after
    the terminal is closed — supplements the Excel Trace/Comparison History sheets
    with the raw console record."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            s.write(data)

    def flush(self):
        for s in self._streams:
            s.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAGAS evaluation for PermataBank AI Platform")
    parser.add_argument("--api-url", default=API_URL, help="API base URL")
    parser.add_argument("--excel", default=EXCEL_PATH, help="Path to golden dataset xlsx")
    parser.add_argument("--domain", default=None, help="Evaluate single domain: hr or contact_center")
    parser.add_argument("--email", default=None, help="Override login email (overrides per-domain credentials)")
    parser.add_argument("--password", default=None, help="Override login password")
    parser.add_argument("--output", default=OUTPUT_PATH, help="Output results xlsx")
    parser.add_argument("--log-dir", default=None,
                         help="Directory for the run log file (default: 'logs/' next to --output)")
    args = parser.parse_args()

    log_dir = Path(args.log_dir) if args.log_dir else Path(args.output).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"ragas_run_{datetime.now():%Y%m%d_%H%M%S}.log"

    with open(log_path, "w", encoding="utf-8") as log_file:
        original_stdout = sys.stdout
        sys.stdout = _Tee(original_stdout, log_file)
        try:
            print(f"[log] Full run log being written to: {log_path}")
            asyncio.run(main(args))
        finally:
            sys.stdout = original_stdout
            print(f"[log] Full run log saved to: {log_path}")
