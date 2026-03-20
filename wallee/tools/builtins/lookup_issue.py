"""Look up print issue diagnosis and intervention guidance from REFERENCE.md.

Use when you detect a specific defect and want detailed guidance beyond
the general principles in LEARNED.md."""

import logging
from pathlib import Path

from wallee.tools.decorator import tool

logger = logging.getLogger(__name__)

_REFERENCE_PATH = Path(__file__).parent.parent.parent / "knowledge" / "REFERENCE.md"


@tool(kind="actuator", requires_approval=False, gate_bypass=True)
def lookup_issue(query: str = "", whiteboard=None, **kwargs) -> dict:
    """Search the print issue reference for diagnosis and intervention guidance.

    Use when you detect a defect and want a detailed decision ladder.
    Examples: lookup_issue("stringing"), lookup_issue("spaghetti"),
    lookup_issue("overextrusion"), lookup_issue("warping")
    """
    if not query:
        return {"error": "query parameter required"}

    if not _REFERENCE_PATH.exists():
        logger.warning(f"REFERENCE.md not found at {_REFERENCE_PATH}")
        return {"status": "not_found", "results": "Reference file not available. Try web_search instead."}

    content = _REFERENCE_PATH.read_text()

    # Split into sections and search
    query_lower = query.lower()
    sections = content.split("\n### ")
    matches = []

    for section in sections:
        if query_lower in section.lower():
            text = ("### " + section.strip()) if not section.startswith("#") else section.strip()
            matches.append(text)

    if matches:
        result = "\n\n---\n\n".join(matches[:2])
        return {"status": "ok", "results": result[:2000]}

    return {"status": "no_match", "results": f"No reference entry found for '{query}'. Try web_search for external knowledge."}
