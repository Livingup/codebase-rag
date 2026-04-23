"""Qdrant storage error types."""


class QdrantError(Exception):
    """Base class for all Qdrant storage errors."""


class QdrantConnectionError(QdrantError):
    """Cannot connect to Qdrant Server."""


class QdrantSchemaError(QdrantError):
    """Collection schema mismatch (dimension, vector config)."""


class QdrantOperationError(QdrantError):
    """Qdrant data operation failed (upsert/delete/query)."""


class QdrantTimeoutError(QdrantError):
    """Qdrant operation timed out."""
