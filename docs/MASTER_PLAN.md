---
name: project-rawos
description: "rawos — AI-native OS platform at downgrade.app. Full architecture, phase plan, primitives, tech stack, migration from tg-claude. Master planning document."
metadata: 
  node_type: memory
  type: project
  originSessionId: 665b776f-5702-4520-8114-c4c9a6a3b6f6
---

# rawos — Master Plan

**Vision**: First AI-native OS. Not a chatbot. Not a tool collection. An OS where AI IS the abstraction layer — for everyone, from grandmothers to senior engineers.

**Portal**: downgrade.app (domain owned)
**Core**: rawos (the OS name, the project identity)
**Server**: root@178.104.255.197, `/root/rawos/`
**Philosophy**: Approach 1 spirit (one AI does everything, zero learning curve) + Approach 3 form (chat + visual workspace). Target: everyone — benchmark is Windows/iOS.

**Why it has never existed**: ChatGPT talks but cannot do real work in the real world. Copilot is code-only. Notion AI is documents-only. rawos does everything, for everyone, from one interface. The AI IS the OS — hiding all complexity, showing only results.

---

## Architecture

```
Layer 5: downgrade.app (Frontend)
  Next.js 14, TypeScript, Tailwind, shadcn/ui
  Chat input + Workspace panel (split view)
  Rich output: website preview, charts, files, code
  Real-time via WebSocket (SSE streaming)

Layer 4: Adapters
  Web Adapter → serves downgrade.app
  Telegram Adapter → tg-claude refactored (thin wrapper calling rawos API)
  [Future] API Adapter, CLI Adapter

Layer 3: rawos HTTP API
  FastAPI, async, WebSocket
  POST /intent → stream response (SSE)
  CRUD /projects, /agents, /artifacts
  POST /auth/signup, /auth/login (JWT)

Layer 2: rawos Services (Daemons)
  Auth Service: user identity, JWT, permissions
  Billing Service: usage tracking, quota per tier
  Memory Daemon: background semantic indexing, cleanup
  Scheduler Daemon: queued intents, background agents
  Monitor Daemon: health, alerts, metrics

Layer 1: rawos Kernel (THE NOVEL CORE)
  Agent Engine: identity, lifecycle, spawn, suspend, archive
  Intent Router: NL → structured intent → dispatch → tool execution
  Memory Subsystem: working(Redis) / episodic(SQLite) / semantic(vector) / procedural(prompts)
  Tool Registry: typed capabilities, sandboxed execution, permissions
  Resource Manager: token budgets, rate limits, API quota management

Layer 0: Infrastructure
  Linux (Ubuntu) on Hetzner
  Python 3.12
  SQLite → PostgreSQL (at scale)
  Redis (working memory, pub/sub)
  ChromaDB or Qdrant (vector semantic memory)
  Docker (sandbox isolation per user)
```

---

## Core Primitives (8 fundamental units — equivalent to processes/files/syscalls)

```python
User         # identity, subscription tier, API quota, preference profile
Project      # named workspace (replaces "workdir"), persistent memory + files + context
Agent        # running instance: goal, memory access, tool permissions, resource budget
Intent       # structured validated request: goal + context + constraints + urgency + user_id
Memory       # tiered: working(Redis) / episodic(SQLite) / semantic(vector) / procedural(prompts)
Artifact     # output: file, website, chart, document — stored, versioned, downloadable
Tool         # typed capability: bash, read, write, web_search, deploy, etc.
Event        # system event: agent_started, task_complete, error, quota_exceeded
```

Every primitive has: `id`, `user_id` (mandatory, multi-tenancy by construction), `created_at`, `updated_at`.

---

## Tech Stack (exact, no ambiguity)

| Concern | Choice | Reason |
|---|---|---|
| Backend runtime | Python 3.12 | Proven with tg-claude, fast iteration |
| Web framework | FastAPI | Async, WebSocket, auto OpenAPI, SSE streaming |
| Primary DB | SQLite → PostgreSQL | SQLite for V1 simplicity, migrate at scale |
| Cache/Working memory | Redis | Pub/sub for real-time, fast session state |
| Vector memory | ChromaDB (local) | Simple, no external service needed for V1 |
| AI provider (sole, user-invisible) | DeepSeek | One provider, users never configure or see this |
| AI primary model | deepseek-v4-pro | Complex tasks, primary reasoning |
| AI fast model | deepseek-v4-flash | Quick responses, simple tasks, cost saving |
| Internal compression (system-only) | Groq | Context summarization to reduce DeepSeek token cost — same role as in tg-claude. Never user-facing, never exposed. |
| Embeddings | sentence-transformers local (all-MiniLM-L6-v2) | Free, no external API, no second provider dependency, sufficient for semantic memory |
| Frontend | Next.js 14 + TypeScript | App Router, server components, industry standard |
| UI components | Tailwind + shadcn/ui | Professional, accessible, fast to build |
| Sandbox isolation | Docker per user | Hard isolation, cannot cross user boundaries |
| Domain | downgrade.app → Cloudflare DNS | Already owned |
| SSL | Cloudflare / Let's Encrypt | Zero cost |
| Frontend deploy | Vercel or self-hosted Nginx | Vercel for V1 simplicity |

---

## Phase Plan

### Phase 0 — rawos Core (Weeks 1-2)
**Goal**: All 8 primitives implemented, tested, API skeleton running

Deliverables:
- SQLite schema: users, projects, agents, intents, artifacts, events, memories
- Python models for all 8 primitives (Pydantic v2)
- FastAPI skeleton: /health, /auth/signup, /auth/login, /intent (stub)
- JWT auth: signup → token → protected routes
- Agent Engine: create_agent, lifecycle FSM (dormant→active→suspended→archived)
- Intent Router: parse text → IntentSchema (goal, context, project_id, user_id)
- Tool Registry: import tools.py with clean typed interface
- Memory Subsystem: episodic layer (SQLite), working layer (in-memory for now)
- Unit tests: 100% coverage on primitives and auth
- Migration script: tg-claude sessions.db → rawos projects/memories

Verify: `pytest tests/ --cov=100`, `curl POST /auth/signup`, `curl POST /intent` returns stub response

**INVARIANT**: tg-claude stays running. rawos is a NEW process on a different port. Zero disruption.

---

### Phase 1 — downgrade.app V1 (Weeks 3-4)
**Goal**: Non-technical user can sign up and use in <2 minutes

Deliverables:
- Next.js app at downgrade.app (or staging subdomain first)
- Pages: landing (/, /signup, /login, /dashboard, /project/[id])
- Chat interface: input → POST /intent → SSE streaming response → rendered output
- Project sidebar: create, switch, rename projects
- Markdown + code block rendering in chat
- WebSocket connection for real-time agent status
- Mobile responsive (everyone = phone users too)
- Auth flow: signup → email verify → dashboard

Verify: Non-technical person (test with actual non-dev) uses rawos start-to-finish in <2 minutes

---

### Phase 2 — Real Power (Weeks 5-7)
**Goal**: AI can build and deploy a real website from one sentence

Deliverables:
- Full tool execution pipeline in rawos kernel (bash, file CRUD, web search)
- Artifact storage: all AI outputs stored, versioned, downloadable
- Website builder flow: intent → agent creates files → preview renders in iframe
- File browser panel in workspace (right side of split view)
- Background agents: long tasks async, user sees live progress bar
- Docker sandbox: each user's code execution isolated
- Deploy tool: push static site to public URL
- Image upload + vision input (multi-modal)

Verify: "Build me a landing page for my coffee shop" → rendered preview in <60s

---

### Phase 3 — Memory & Context Intelligence (Weeks 8-9)
**Goal**: AI remembers project context across sessions without prompting

Deliverables:
- ChromaDB integration: project files semantically indexed on write
- Semantic search in intent routing: "what did we do?" → relevant context retrieved
- Cross-session memory: episodic store with intelligent summarization
- User preference learning: procedural memory (preferred stack, language, style)
- Memory daemon: background indexing, TTL cleanup, re-indexing on file change
- "Project memory" UI: user can see/edit what AI remembers about their project

Verify: Open project after 2 weeks, ask "what's the status?" → accurate answer without re-explaining

---

### Phase 4 — Multi-Agent Orchestration (Weeks 10-12)
**Goal**: Complex tasks completed by parallel agents, user just watches

Deliverables:
- Agent spawning: one intent → orchestrator agent → spawn N sub-agents
- Parallel execution: sub-agents run concurrently (asyncio + proper resource isolation)
- Inter-agent messaging: structured typed messages, shared artifact access
- Dependency graph: agent B waits for artifact from agent A
- Agent status panel: visual tree showing agents and their status
- Specialized agents: CodeAgent, DesignAgent, ResearchAgent, DataAgent
- Resource manager: token budgets per user tier, enforced hard limits
- Graceful degradation: if sub-agent fails, orchestrator handles

Verify: "Build and deploy a full restaurant ordering app" → parallel agents, done in <15min

---

### Phase 5 — Production (Weeks 13-16)
**Goal**: 100 concurrent users, SLA 99.9%, billing live

Deliverables:
- Stripe billing: usage-based (per token consumed) + subscription tiers
- Admin dashboard: usage, costs, user management, error rates
- PostgreSQL migration: SQLite → Postgres with zero downtime migration
- Nginx load balancer + rate limiting
- tg-claude migration: bot.py refactored to thin Telegram Adapter calling rawos API
- Security audit: penetration test, multi-tenant isolation verification
- Performance: intent routing <200ms, streaming starts <500ms
- Error handling: every failure path explicit, user-facing messages clear and friendly
- Monitoring: Prometheus + Grafana (or Datadog), alerts for errors/latency/quota
- Onboarding flow: new user guided to first successful task in <5 min

Verify: Load test 100 concurrent users, zero cross-user data leakage, all metrics green

---

## Migration Strategy (tg-claude → rawos)

tg-claude NEVER goes down. Migration is additive:

| tg-claude Component | rawos Fate |
|---|---|
| db.py | Logic → rawos Memory Subsystem. Schema extended, backward compatible |
| tools.py | Import into Tool Registry with typed interface wrapper |
| ai.py | Import as rawos AI Engine, strip Telegram-specific code |
| bot.py | Thin Telegram Adapter: receive message → call rawos API → format response |
| sessions.db | Migration script → rawos projects + episodic memories |
| groq_client.py | Import into AI Engine as compression provider |

After Phase 5: tg-claude = 50-line file that translates Telegram messages to rawos HTTP calls.

---

## Security & Multi-Tenancy (non-negotiable invariants)

1. **Row-level isolation**: Every DB query includes `WHERE user_id = ?`. No query touches another user's data by construction — not by policy.
2. **Sandbox isolation**: Bash/code execution per user runs in Docker container. No shared filesystem.
3. **JWT short expiry**: access token 15min, refresh token 7 days. No long-lived sessions in DB.
4. **Rate limiting**: Token bucket per user_id at API gateway. Hard quota enforcement.
5. **Audit log**: Every intent + every tool execution logged: user_id, timestamp, input hash, result hash.
6. **Input validation**: IntentSchema validation before ANY execution. No raw prompt injection to tools.
7. **Secret management**: All API keys in environment variables. Never in DB, never in logs.

---

## Success Metrics per Phase

| Phase | Single Definition of Done |
|---|---|
| 0 | All 8 primitives pass 100% unit tests. /intent API responds. |
| 1 | Non-technical person (non-developer) signs up and gets a useful AI response in <2 min. |
| 2 | "Build me a website for my bakery" → live preview rendered in browser in <60s. |
| 3 | AI accurately answers "what's the status of my project?" after 2 weeks without re-prompting. |
| 4 | "Build a full ordering app" completed by parallel agents in <15min. |
| 5 | 100 concurrent users, 99.9% uptime, billing charges correctly, zero cross-user data. |

---

## Key Design Decisions & Rationale

**Q: Why greenfield, not evolve tg-claude?**
A: tg-claude is architected as a Telegram bot. Every OS feature built on that base fights the abstraction. Historical precedent: every serious OS (Linux, XNU, NT) was built correctly from scratch. tg-claude becomes an adapter — no work wasted.

**Q: Why Python, not Rust/Go for the kernel?**
A: rawos kernel is not CPU/latency-bound — it's LLM-API-bound. Python's async is sufficient. Iteration speed matters more at this stage. If a bottleneck is identified, that specific component gets rewritten.

**Q: Why not Telegram as primary interface?**
A: Telegram is a messaging app. Normal users (the target: everyone, like iOS users) expect a web product. Telegram UX cannot render website previews, file browsers, agent trees. It stays as one adapter among many.

**Q: Why SQLite before PostgreSQL?**
A: Operational simplicity for V1. One less service to manage. Migration to Postgres at Phase 5 is straightforward with SQLAlchemy. Do not over-engineer before product-market fit.

**Q: Why Docker sandbox, not firejail?**
A: Docker is the industry standard, well-understood security model, easy to configure resource limits (CPU, memory, network). firejail is Linux-only and more complex to configure correctly for multi-user isolation.

---

## Next Immediate Action

Phase 0, Day 1:
1. `cd /root/rawos`
2. `git init && python3 -m venv venv`
3. Define `models/primitives.py` — all 8 primitives as Pydantic v2 models
4. Define `schema/migrations/001_initial.sql` — full DB schema
5. Write tests for all models BEFORE implementation (TDD from day 1)

**Why:** How to do Step 1 is: start with data model. Every architectural decision flows from what the primitives ARE. Get them right first, code is secondary.

---

## Related Projects (will become rawos Apps in Phase 4+)

- SOVEREIGN (`/root/sovereign/`) → rawos Research Agent app
- KAIZEN (`/root/kaizen-42m`) → rawos Learning Agent app
- PROMETHEUS (`/root/prometheus/`) → rawos Model Factory app
- repo.you → rawos Archive/Social Agent app
- tg-claude (`/root/tg-claude/`) → rawos Telegram Adapter

**Why:** Not full description here — each has own memory file. These become "apps running ON rawos" in Phase 4+, not integrated into the kernel.
