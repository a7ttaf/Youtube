"""Parser protocol shared by every Google source-system parser.

Parsers receive a pre-recorded payload + a tenant_id and emit
ParsedSourceRow instances. They are the only place where
source_row_key is derived (via source_row_keys.build_source_row_key).
Repositories never re-derive the key.
"""

from collections.abc import Iterable
from typing import Protocol
from uuid import UUID

from ums_smart_revenue.connectors.google_source_rows import ParsedSourceRow


class ParserError(ValueError):
    """Raised when a payload is malformed or violates the parser's contract."""


class SourceRowParser(Protocol):
    source_system: str

    def parse(
        self,
        payload: dict[str, object],
        *,
        tenant_id: UUID,
    ) -> Iterable[ParsedSourceRow]:
        """Translate a single pre-recorded report payload into ParsedSourceRow rows."""
        ...
