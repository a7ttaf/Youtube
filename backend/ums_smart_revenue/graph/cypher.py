"""Named read-only Cypher query templates for dashboard graph views."""

import re

READ_ONLY_CYPHER: dict[str, str] = {
    "hierarchy": """
        MATCH path = (:Holding)-[:HAS_SECTOR]->(:Sector)
                     -[:HAS_COMPANY]->(:Company)
                     -[:OWNS_CHANNEL]->(:YouTubeChannel)
        RETURN path
    """,
    "revenue_flow": """
        MATCH path = (:Company)-[:OWNS_CHANNEL]->(:YouTubeChannel)
                     -[:HAS_REVENUE_FOR]->(:RevenueFact)
                     -[:FOR_MONTH]->(:Month {month: $month})
        RETURN path
    """,
    "issues": """
        MATCH path = (issue:Issue)-[:AFFECTS_CHANNEL]->(:YouTubeChannel)
        WHERE $month IS NULL OR EXISTS {
            MATCH (issue)-[:AFFECTS_MONTH]->(:Month {month: $month})
        }
        RETURN path
    """,
}


def assert_query_is_read_only(query: str) -> None:
    blocked_patterns = (
        r"\bCREATE\b",
        r"\bMERGE\b",
        r"\bSET\b",
        r"\bDELETE\b",
        r"\bREMOVE\b",
        r"\bDROP\b",
        r"\bCALL\b",
    )
    for pattern in blocked_patterns:
        if re.search(pattern, query, flags=re.IGNORECASE):
            token = pattern.replace(r"\b", "")
            raise ValueError(f"Graph dashboard query is not read-only: found {token}")
