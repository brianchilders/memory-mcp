"""
integrations/openhome/background.py — Memory Context Daemon for OpenHome

WHAT THIS DOES
--------------
A background daemon ability that gives your OpenHome agent persistent memory
across sessions by connecting to a local memory-mcp server.

Two phases run automatically while the session is active:

  1. Session start
     Calls /get_context on memory-mcp for the configured person and injects
     the result into the agent's live personality prompt.  The agent immediately
     "knows" stored preferences, habits, and past observations — before the
     user says a word.

  2. Observation loop  (runs every OBSERVATION_INTERVAL_SEC seconds)
     Reads the growing conversation history, uses the agent's own LLM to
     extract durable facts worth keeping, and pushes each one to /remember.
     Over time, this builds a self-updating memory of who the user is and what
     they care about — entirely hands-free.

PATTERN
-------
Observer archetype — runs silently in the background.
Never calls resume_normal_flow().  All output goes through editor_logging_handler.

SETUP
-----
1. Edit the CONFIGURATION block below — set your memory-mcp URL and token.
2. Set PERSON_NAME to the entity name in memory-mcp (usually your first name).
3. Deploy this file inside an OpenHome ability zip (see README.md).
4. Add httpx to your ability's requirements (pip: httpx>=0.27).
5. Enable the ability on your agent from the OpenHome dashboard.

NETWORK NOTE
------------
OpenHome abilities run in a cloud sandbox.  For the ability to reach your
local memory-mcp server, the server must be reachable from the internet.
See README.md for Cloudflare Tunnel / ngrok setup.
"""

import json
import logging

import httpx

# ── CONFIGURATION ─────────────────────────────────────────────────────────────

MEMORY_API_URL   = "https://memory.yourdomain.com"   # ← CHANGE: your public memory-mcp URL
MEMORY_API_TOKEN = "your-token-here"                  # ← CHANGE: from /admin/settings
PERSON_NAME      = "Brian"                            # ← CHANGE: entity name in memory-mcp

# Context injected at session start — tune to what your agent should know about
CONTEXT_QUERY    = "preferences, habits, routines, and recent observations"
MAX_CONTEXT_FACTS = 8

# How many seconds between observation passes in the background loop
OBSERVATION_INTERVAL_SEC = 60

# Minimum new conversation turns before running fact extraction
# (prevents running the LLM on every tick when the user is quiet)
MIN_NEW_TURNS_TO_EXTRACT = 4

# ──────────────────────────────────────────────────────────────────────────────

_HEADERS = {
    "Authorization": f"Bearer {MEMORY_API_TOKEN}",
    "Content-Type":  "application/json",
}

_EXTRACT_PROMPT = """\
You are a memory extraction assistant.  Given the conversation excerpt below,
identify any concrete, durable facts worth remembering about the user:
preferences, habits, opinions, personal details, or anything the agent should
recall in future sessions.

Rules:
- Return a JSON array of fact strings.  Return [] if nothing is worth storing.
- Each fact must be one sentence.
- Do not store conversational filler, transient state, or things the user
  already knows (e.g. "User said hello").
- Focus on: preferences ("prefers X"), habits ("usually does Y at Z time"),
  opinions ("dislikes X"), personal facts ("has a dog named Max").

Conversation:
{transcript}

JSON array of facts:"""


class MemoryContextDaemon:
    """Background daemon that seeds and updates memory-mcp context each session."""

    def call(self, worker, background_daemon_mode: bool):
        log = logging.getLogger(__name__)
        log.addHandler(worker.editor_logging_handler)

        # ── Phase 1: inject stored context into the agent prompt ──────────────
        context = self._get_context(log)
        if context:
            worker.update_personality_agent_prompt(
                f"\n\n[Persistent memory — what you know about this user]\n{context}\n"
            )
            log.info("memory-mcp: context injected into agent prompt")
        else:
            log.warning(
                "memory-mcp: no context retrieved — "
                "check MEMORY_API_URL and MEMORY_API_TOKEN, or no memories exist yet"
            )

        # ── Phase 2: observation loop — extract and store new facts ───────────
        last_turn_count = 0

        while True:
            worker.session_tasks.sleep(OBSERVATION_INTERVAL_SEC)

            history = worker.get_full_message_history()
            current_count = len(history)
            new_turns = current_count - last_turn_count

            if new_turns >= MIN_NEW_TURNS_TO_EXTRACT:
                excerpt = history[last_turn_count:]
                facts = self._extract_facts(worker, excerpt, log)
                stored = 0
                for fact in facts:
                    if self._remember(fact, log):
                        stored += 1
                if stored:
                    log.info("memory-mcp: stored %d new facts from conversation", stored)
                last_turn_count = current_count

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _get_context(self, log) -> str:
        """Fetch a context snapshot from memory-mcp for PERSON_NAME."""
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(
                    f"{MEMORY_API_URL}/get_context",
                    headers=_HEADERS,
                    json={
                        "entity_name":   PERSON_NAME,
                        "context_query": CONTEXT_QUERY,
                        "max_facts":     MAX_CONTEXT_FACTS,
                    },
                )
                resp.raise_for_status()
                return resp.json().get("result", "")
        except httpx.HTTPStatusError as exc:
            log.error("memory-mcp get_context HTTP %s: %s", exc.response.status_code, exc)
        except Exception as exc:
            log.error("memory-mcp get_context failed: %s", exc)
        return ""

    def _extract_facts(self, worker, turns: list, log) -> list:
        """Use the agent's LLM to extract durable facts from recent turns."""
        lines = []
        for turn in turns:
            role    = turn.get("role", "")
            content = turn.get("content", "").strip()
            if role in ("user", "assistant") and content:
                lines.append(f"{role.title()}: {content}")

        if not lines:
            return []

        prompt = _EXTRACT_PROMPT.format(transcript="\n".join(lines))
        try:
            raw = worker.text_to_text_response(prompt)
            # Parse the JSON array the LLM returns
            start = raw.find("[")
            end   = raw.rfind("]") + 1
            if start >= 0 and end > start:
                facts = json.loads(raw[start:end])
                return [f for f in facts if isinstance(f, str) and f.strip()]
        except Exception as exc:
            log.warning("memory-mcp fact extraction failed: %s", exc)
        return []

    def _remember(self, fact: str, log) -> bool:
        """Push a single extracted fact to memory-mcp. Returns True on success."""
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(
                    f"{MEMORY_API_URL}/remember",
                    headers=_HEADERS,
                    json={
                        "entity_name": PERSON_NAME,
                        "fact":        fact,
                        "category":    "observation",
                        "confidence":  0.85,
                        "source":      "openhome_session",
                        "entity_type": "person",
                    },
                )
                resp.raise_for_status()
            return True
        except httpx.HTTPStatusError as exc:
            log.error("memory-mcp remember HTTP %s: %s", exc.response.status_code, exc)
        except Exception as exc:
            log.error("memory-mcp remember failed: %s", exc)
        return False
