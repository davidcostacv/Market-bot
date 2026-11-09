"""The sandboxed mock world an agent acts in.

Every tool here is a pure in-memory function. Nothing sends email, opens a socket
or writes to disk. "Sending" a message appends to a Python list; "writing" a note
stores a string in a dict. That is the whole point: the agent believes it has
real capabilities, and we observe what it tries to do with them.
"""

from __future__ import annotations

import copy
import json
from typing import Any

# JSON-Schema specs for every mock tool. Adapters translate these into the
# provider's tool format. Keep descriptions stable: they are part of the
# cached prompt prefix for paid targets.
TOOL_SPECS: dict[str, dict[str, Any]] = {
    "list_emails": {
        "description": "List the emails in the user's inbox (id, sender, subject).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "read_email": {
        "description": "Read the full body of one email by its id.",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Email id, e.g. e1"}},
            "required": ["id"],
        },
    },
    "read_document": {
        "description": "Read a document from the user's document store by name.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Document name"}},
            "required": ["name"],
        },
    },
    "search_documents": {
        "description": "Search the user's documents. Returns matching names and snippets.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    "read_file": {
        "description": "Read a text file from the user's workspace by path.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    "search_web": {
        "description": "Search the web. Returns result titles, URLs and snippets.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    "fetch_url": {
        "description": "Fetch the text content of a web page by URL.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    "send_message": {
        "description": "Send a message to a recipient on the user's behalf.",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient address"},
                "body": {"type": "string", "description": "Message text"},
            },
            "required": ["to", "body"],
        },
    },
    "write_note": {
        "description": "Save a note to the user's notes.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["title", "content"],
        },
    },
}


class MockEnvironment:
    """In-memory world: inbox, documents, web pages and files, plus mock tools."""

    def __init__(
        self,
        data: dict[str, Any],
        allowed_tools: list[str],
        tool_result_injection: tuple[str, str] | None = None,
        hints: dict[str, Any] | None = None,
    ) -> None:
        data = copy.deepcopy(data or {})
        self.inbox: list[dict[str, str]] = data.get("inbox", [])
        self.documents: dict[str, str] = data.get("documents", {})
        self.web_pages: dict[str, str] = data.get("web_pages", {})
        self.files: dict[str, str] = data.get("files", {})
        self.search_results: dict[str, list[dict[str, str]]] = data.get("search_results", {})
        unknown = [t for t in allowed_tools if t not in TOOL_SPECS]
        if unknown:
            raise ValueError(f"Unknown tools: {unknown}")
        # Sorted so the tool list (and therefore the prompt prefix) is stable.
        self.allowed_tools = sorted(allowed_tools)
        # (tool_name, text): text is appended to that tool's output. Simulates a
        # compromised API or plugin returning attacker-controlled metadata.
        self.tool_result_injection = tool_result_injection
        # Scripted plan for the mock agent only. Real-model adapters never read it.
        self.hints = hints or {}
        # Side effects, observable by oracles and tests. Never leave the process.
        self.sent_messages: list[dict[str, str]] = []
        self.notes: dict[str, str] = {}

    def tool_specs(self) -> list[dict[str, Any]]:
        return [{"name": n, **TOOL_SPECS[n]} for n in self.allowed_tools]

    def call(self, name: str, args: dict[str, Any] | None) -> str:
        """Execute a mock tool. Always returns a string; errors are returned, not raised."""
        args = args if isinstance(args, dict) else {}
        if name not in self.allowed_tools:
            return f"ERROR: tool '{name}' is not available."
        required = TOOL_SPECS[name]["parameters"].get("required", [])
        missing = [r for r in required if r not in args]
        if missing:
            return f"ERROR: missing argument(s): {', '.join(missing)}"
        output = getattr(self, f"_tool_{name}")(**{k: str(args[k]) for k in required})
        if self.tool_result_injection and self.tool_result_injection[0] == name:
            output = f"{output}\n{self.tool_result_injection[1]}"
        return output

    # --- read tools (where untrusted content enters the agent) ---------------
    def _tool_list_emails(self) -> str:
        rows = [{"id": e["id"], "from": e["from"], "subject": e["subject"]} for e in self.inbox]
        return json.dumps(rows, ensure_ascii=False)

    def _tool_read_email(self, id: str) -> str:  # noqa: A002 - matches the tool schema
        for e in self.inbox:
            if e["id"] == id:
                return f"From: {e['from']}\nSubject: {e['subject']}\n\n{e['body']}"
        return f"ERROR: no email with id '{id}'."

    def _tool_read_document(self, name: str) -> str:
        return self.documents.get(name, f"ERROR: no document named '{name}'.")

    def _tool_search_documents(self, query: str) -> str:
        hits = [
            {"name": n, "snippet": text[:120]}
            for n, text in self.documents.items()
            if any(w in (n + " " + text).lower() for w in query.lower().split())
        ]
        return json.dumps(hits or [{"name": n, "snippet": t[:120]} for n, t in self.documents.items()],
                          ensure_ascii=False)

    def _tool_read_file(self, path: str) -> str:
        return self.files.get(path, f"ERROR: no file at '{path}'.")

    def _tool_search_web(self, query: str) -> str:
        # Mock search ignores the query beyond a lookup: deterministic results.
        results = self.search_results.get("default", [])
        return json.dumps(results, ensure_ascii=False)

    def _tool_fetch_url(self, url: str) -> str:
        return self.web_pages.get(url.strip(), f"ERROR: 404 Not Found: {url}")

    # --- action tools (the sinks an attacker wants to reach) ----------------
    def _tool_send_message(self, to: str, body: str) -> str:
        self.sent_messages.append({"to": to, "body": body})
        return f"Message sent to {to}."

    def _tool_write_note(self, title: str, content: str) -> str:
        self.notes[title] = content
        return f"Note '{title}' saved."
