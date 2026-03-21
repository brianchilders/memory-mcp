"""
integrations/openhome/main.py — Memory Recall Skill for OpenHome

WHAT THIS DOES
--------------
An interactive skill that lets you ask your OpenHome agent to recall specific
facts from memory-mcp by voice.

Example interaction:
  User:  "What do you remember about my coffee preferences?"
  Agent: "You like a flat white with oat milk, no sugar, at about 65 degrees."

Example interaction:
  User:  "What do you know about my sleep habits?"
  Agent: "You usually get around seven hours, and you've mentioned preferring
          the bedroom at 66 degrees when you sleep."

PATTERN
-------
Responder archetype — user triggers, agent answers, then exits.
resume_normal_flow() is called on every exit path (required by OpenHome SDK).

SETUP
-----
1. Edit the CONFIGURATION block below — set your memory-mcp URL and token.
2. Set PERSON_NAME to the entity name in memory-mcp (usually your first name).
3. Deploy this file inside an OpenHome ability zip (see README.md).
4. In the OpenHome dashboard, set trigger phrases such as:
     "what do you remember"
     "recall memory"
     "what do you know about"
5. Add httpx to your ability's requirements (pip: httpx>=0.27).

NETWORK NOTE
------------
OpenHome abilities run in a cloud sandbox.  For the ability to reach your
local memory-mcp server, the server must be reachable from the internet.
See README.md for Cloudflare Tunnel / ngrok setup.
"""

import logging

import httpx

# ── CONFIGURATION ─────────────────────────────────────────────────────────────

MEMORY_API_URL   = "https://memory.yourdomain.com"   # ← CHANGE: your public memory-mcp URL
MEMORY_API_TOKEN = "your-token-here"                  # ← CHANGE: from /admin/settings
PERSON_NAME      = "Brian"                            # ← CHANGE: entity name in memory-mcp

# Number of memories to retrieve per query
TOP_K = 4

# ──────────────────────────────────────────────────────────────────────────────

_HEADERS = {
    "Authorization": f"Bearer {MEMORY_API_TOKEN}",
    "Content-Type":  "application/json",
}

# Prompt that converts raw memory results into a spoken sentence
_VOICE_PROMPT = """\
The user asked: "{query}"

Here are the relevant memories retrieved:
{memories}

Summarise these in 1-2 natural spoken sentences.  Rules:
- No markdown, bullet points, lists, URLs, or emojis.
- Use contractions.  Sound like a person talking, not a database.
- If the memories feel stale or uncertain, say so briefly.
- Maximum 2 sentences."""


class MemoryRecallSkill:
    """Interactive skill that answers voice queries against memory-mcp."""

    def call(self, worker):
        log = logging.getLogger(__name__)
        log.addHandler(worker.editor_logging_handler)

        worker.speak("What would you like me to recall?")
        query = worker.user_response()

        if not query or not query.strip():
            worker.speak("I didn't catch that.")
            worker.resume_normal_flow()
            return

        query = query.strip()
        result_text = self._recall(query, log)

        if not result_text:
            worker.speak("I couldn't reach memory storage right now. Try again in a moment.")
            worker.resume_normal_flow()
            return

        # Use the agent's LLM to convert the raw memory text into a natural voice response
        prompt  = _VOICE_PROMPT.format(query=query, memories=result_text)
        summary = worker.text_to_text_response(prompt)

        if summary and summary.strip():
            worker.speak(summary.strip())
        else:
            worker.speak("I don't have anything stored about that yet.")

        worker.resume_normal_flow()

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _recall(self, query: str, log) -> str:
        """Search memory-mcp and return the formatted result text, or empty string."""
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(
                    f"{MEMORY_API_URL}/recall",
                    headers=_HEADERS,
                    json={
                        "entity_name": PERSON_NAME,
                        "query":       query,
                        "top_k":       TOP_K,
                    },
                )
                resp.raise_for_status()
                return resp.json().get("result", "")
        except httpx.HTTPStatusError as exc:
            log.error("memory-mcp recall HTTP %s: %s", exc.response.status_code, exc)
        except Exception as exc:
            log.error("memory-mcp recall failed: %s", exc)
        return ""
