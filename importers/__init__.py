"""
importers — Registry of available importer functions.

Each importer is an async function that accepts source-specific parameters
and returns an ImportResult(added, skipped, errors).

    from importers.jsonl             import import_jsonl
    from importers.mem0              import import_mem0
    from importers.mcp_memory_service import import_mcp_memory_service
"""

from importers.base import ImportResult
from importers.jsonl import import_jsonl
from importers.mem0 import import_mem0
from importers.mcp_memory_service import import_mcp_memory_service

__all__ = [
    "ImportResult",
    "import_jsonl",
    "import_mem0",
    "import_mcp_memory_service",
]
