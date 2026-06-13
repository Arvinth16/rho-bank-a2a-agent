"""Rho-Bank customer service agent: policy + env tools + KB search (RAG)."""

import os
from pathlib import Path

from google.adk.agents import LlmAgent

from env_toolset import EnvApiToolset
from rag_tools import kb_search_bm25, kb_search_hybrid, kb_search_vector

MODEL = os.environ.get("MODEL", "gemini-3.5-flash")
POLICY_PATH = Path(os.environ.get("KB_POLICY_PATH", "/app/kb/policy.md"))

RAG_GUIDANCE = """

## Knowledge Base Access

You do NOT have the knowledge base inlined. You MUST search the knowledge base
before answering any policy question, making any recommendation, or performing
any scenario-specific procedure.

### Which search tool to use
- **kb_search_hybrid(query, category)** — USE THIS BY DEFAULT. Combines keyword
  and semantic search for highest recall. Always pass `category` when you know
  the product line.
- **kb_search_bm25(query, category)** — use when you need exact keyword matches
  (e.g. exact tool names, account codes, specific fee amounts).
- **kb_search_vector(query, category)** — use when the query is a natural-language
  question and keyword terms may not appear verbatim in the documents.

### Category values for filtering (always use when possible)
"checking_accounts", "savings_accounts", "credit_cards",
"business_checking_accounts", "business_savings_accounts",
"business_credit_cards", "buy_now_pay_later", "bank_accounts"

### How to handle comparison/recommendation tasks
When the customer asks which account, card, or product is best for them:
1. Search broadly first: kb_search_hybrid("<product type> overview", category="<relevant category>")
2. Then search for EACH individual candidate separately to get its fee/eligibility details.
   Do NOT skip this step — marketing names are misleading. Always verify the numbers.
3. Check eligibility restrictions (age, deposit minimums, existing account limits) for
   every candidate BEFORE recommending it. An ineligible option must be excluded.
4. Calculate the exact costs using the numbers from the KB, not intuition.
5. Recommend only after comparing ALL eligible options numerically.

### Retry strategy
If a search returns no useful results: rephrase with different keywords and retry.
Never tell the customer you cannot find information without trying at least two
different queries.
"""

root_agent = LlmAgent(
    name="cs_agent",
    model=MODEL,
    instruction=POLICY_PATH.read_text() + RAG_GUIDANCE,
    tools=[EnvApiToolset(), kb_search_hybrid, kb_search_bm25, kb_search_vector],
)
