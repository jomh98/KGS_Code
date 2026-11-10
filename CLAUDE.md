# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Streamlit Q&A web app (Korean UI) that answers gas-safety questions using RAG over **currently-in-force** Korean law (법률·시행령·시행규칙·행정규칙 via the law.go.kr Open API) and the **current** KGS Code (scraped from cyber.kgs.or.kr). Public, no login; deployed from GitHub to Streamlit Community Cloud. Requirements: `PRD.md` (feature IDs F-01…F-17 and § numbers are referenced in code). Build stages: `PROMPTS.md` (1 skeleton → 2 features → 3 UI → 4 Playwright review). Windows dev environment — use `pathlib`.

**Read `REVIEW.md` before changing behavior.** It is the stage-4 review (issues C1–C3, M1–M10, L1–L14) plus a "수정 결과" section: all C/M items are fixed and verified; **L items are still open**. Code comments cite these IDs. Current data state: KGS 187 codes indexed, **law data 0 docs** (no OC yet) — the chat screen shows a "KGS Code만 검색" notice until law chunks exist.

## Commands

```bash
.venv/Scripts/python -m pytest                                  # all tests (no network, no keys)
.venv/Scripts/python -m pytest tests/test_retriever.py::test_search_finds_relevant   # single test
.venv/Scripts/streamlit run app.py
.venv/Scripts/python -m ingest.build_index --sources kgs        # KGS only; no key needed
.venv/Scripts/python -m ingest.build_index --sources law        # needs LAW_API_OC + registered IP
.venv/Scripts/python -m ingest.build_index --force --limit 3    # reprocess, first 3 docs per source
.venv/Scripts/python -m scripts.eval_gate --verbose             # grounding gate on the REAL index (exit 1 on regression)
```

Manual/Playwright testing notes:
- Streamlit re-runs page scripts (`views/*.py`) on reload, but **does not re-import edited `core/` or `ui/` modules** — restart the server after changing them.
- Any `DEFAULTS` key can be set per run via env var, e.g. `MAX_QUESTIONS_PER_CLIENT_PER_DAY=0 streamlit run app.py --server.port 8600` (quota-exhausted state). `ANTHROPIC_API_KEY=<invalid>` exercises the API-error path without a real key.
- On localhost `st.context.ip_address` is None, so rate limiting falls back to per-session keys; simulate per-IP behavior by sending an `X-Forwarded-For` header (Playwright `page.setExtraHTTPHeaders`). With an invalid key nothing is ever deducted (errors are refunded), so quota exhaustion is tested with `MAX_QUESTIONS_PER_CLIENT_PER_DAY=0`. Admin login locks per IP/session after `ADMIN_MAX_FAILS`; restart the server to reset during testing.
- Playwright may keep the sidebar open on phone width if desktop tests ran earlier (browser storage); clear `localStorage` before mobile checks.
- First load after a server start shows only the header + "검색 색인을 준비하는 중" spinner for ~5–15 s while the index builds; screenshots taken then come out blank. Example buttons have hidden duplicates in the DOM — use `:visible` selectors. Playwright output must go under `.playwright-mcp/` (gitignored).

Secrets: `.streamlit/secrets.toml` (gitignored; template `secrets.toml.example`) or env vars. Every key is optional — the app must keep running with none (no LLM key → shows retrieved sources only; no embedding key → BM25 only; no Supabase → `data/logs/queries.jsonl`). Tunables live in `core/config.py` `DEFAULTS` and can be overridden by a secret of the same name.

## Architecture

Two flows sharing only `data/`:

1. **Offline ingest** (`ingest/build_index.py`, also run weekly by `.github/workflows/update_data.yml`): `kgs_crawler` / `law_api` → `chunker` → one JSON per document in `data/chunks/{kgs,law}/` (`{"doc": {..., "version"}, "chunks": [...]}`) + `data/meta.json`. Per-document `version` (KGS `pblcCd`, law 법령일련번호) drives incremental updates; documents missing from the current list are **deleted** so superseded text never stays indexed. Exit-code contract (CI commits only on 0): fatal problems → 1 (list fetch failure, a KGS category returning 0 rows = `KgsListError`, deletion guard tripping at >10% / min 3 docs via `safe_delete`, any law error). Per-document failures → 0, old edition kept and listed in `meta.kgs_stale` (UI shows "최신 개정판 미반영"). A new edition with no text layer deletes the old edition. KGS pagination detects end-of-list per category (codes overlap across categories). `data/raw/` (PDFs, ~800MB) is gitignored.
2. **Online app**: `app.py` is only the `st.navigation` router (sets page config, applies `ui/styles.py`); screens live in `views/chat.py` and `views/admin.py` (`/admin`), shared rendering in `ui/components.py`, process-wide singletons (`get_retriever`, `get_limiter`, `get_answer_cache`, `get_login_throttle`, `client_ip`) in `ui/resources.py`. Never call `st.cache_resource.clear()` — it resets rate-limit counters; clear `get_retriever` only. `pages/` is intentionally absent — `st.navigation` replaces it. `Retriever` is built in-process from the chunk files (`st.cache_resource(max_entries=1)`, keyed by `meta.index_built_at` **and today's KST date** so 시행예정 status is recomputed daily; 5–15 s and ~300 MB for ~19k chunks) → `AnswerStream` (Claude via `client.beta.messages.stream` with server-side fallbacks) → `validate_citations` → render + `save_log` (background thread).

Cross-module invariants:
- **Grounding gate:** `RetrievedChunk.coverage` = idf-weighted share of the question's **domain terms** present in the chunk; below `MIN_RETRIEVAL_SCORE` (0.2) → no LLM call, "확인된 근거 없음". Domain terms = within-word bigrams that appear in the auto-built lexicon (`build_lexicon` over KGS code titles + law names, dropping tokens in >30% of titles) after synonym expansion (LPG→액화석유가스 …). A question with no domain terms scores 0. Fine-grained relevance is left to the LLM (prompt rules 3, 8, 9). Measured on `tests/gate_questions.yaml`: relevant ≥0.57, unrelated 0.00 — re-run `scripts.eval_gate` after touching the tokenizer, lexicon, synonyms or data.
- **Citation numbering:** `Answer.sources` is the full retrieved list and `[근거 n]` indexes into it (1-based); `Answer.cited` lists which were used. Don't reorder or filter `sources` after generation.
- **Tokenizer** (`core/retriever.py`): Hangul char bigrams + cross-space bigrams + lowercase alnum words. Changing it shifts coverage scores (see above).
- **KGS sectioning** (`chunker.find_kgs_sections`): heading candidates are filtered by the *longest consistent numbering chain*, because tables contain lines like "3. 크롬함유량…" that look like headings. Depth ≤4 headings are boundaries; parent titles are prefixed as context.
- **Rate limit & answer cache** (`views/chat.py` `handle_question`, `core/rate_limit.py`): per hashed client IP, in process memory, day boundary = KST midnight. Client IP = `X-Forwarded-For` entry `TRUSTED_PROXY_HOPS` from the **right** (leftmost is client-forgeable); verify on deploy via admin "접속 정보". Flow: `check()` (length/empty/quota) → search → if `will_call_llm(sources)` then atomic `reserve()` → stream → `refund()` if `answer.error`. Cache stores only `answer.cacheable` (normal `end_turn`); errors, refusals and truncated answers are never cached. Use the `Answer.error/llm_called/cacheable` flags, not `answer.model`, for these decisions.
- **Gas category:** KGS lists hydrogen-charging codes (FP216/217) under 고압가스, so filters, the gas boost and the "다른 분야" badge use `chunk_gas_tags()` = site category ∪ gas named in the code title.
- **UI:** colors come from `.streamlit/config.toml` (`[theme.light]`/`[theme.dark]`; don't set `base`) and Streamlit badges — no colors in CSS. `ui/styles.py` `apply_styles()` is the only `unsafe_allow_html` call; never pass user or source text into it. Source titles go through `escape_md` before `st.markdown`. Orange is reserved for warnings (시행예정, 근거 없음).
- **PII:** `logger.mask_pii` runs before storage; IPs are never logged. Regexes use digit lookarounds, not `\b` (Hangul counts as `\w`).

## Source quirks (verified 2026-11)

- law.go.kr DRF API rejects calls unless the caller's IP/domain is registered with the OC → law collection can't run on GitHub-hosted runners. Field names come from the official guide; wrapper nesting is unverified, so `law_api` finds elements by field-name tags. Article links `https://www.law.go.kr/법령/{name}/제N조[의M]` resolve directly.
- KGS list: `kgscode.codeSearch.listV2.ex.do?pblcRlmCd={01,02,03,05,04}&pubEnd01=F` (current only). PDFs download via POST without login. Page headers vary (`KGS AC116 2026`, `KGS S AA012 2026`, `KGS GC1012026`). AC417 is a scanned PDF with no text layer — reported in `meta.kgs_no_text`, not treated as failure.
