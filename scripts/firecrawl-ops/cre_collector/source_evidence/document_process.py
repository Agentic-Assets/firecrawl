"""Shared signal that a document subprocess group was not proven settled."""


class DocumentProcessCleanupUnknown(RuntimeError):
    """The foreground owner must preserve its lock and require recovery."""


def cleanup_unknown_in_chain(error):
    """Find cleanup uncertainty through explicit causes and implicit contexts."""
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, DocumentProcessCleanupUnknown):
            return True
        pending.extend((current.__cause__, current.__context__))
    return False
