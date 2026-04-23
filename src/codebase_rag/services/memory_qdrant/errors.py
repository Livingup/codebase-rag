"""Memory Qdrant service error types."""


class MemoryQdrantError(Exception):
    """Base class for all Memory Qdrant errors."""


class MemoryInstanceError(MemoryQdrantError):
    """In-memory instance operation failed."""


class InstanceNotFoundError(MemoryQdrantError):
    """Instance handle is invalid or has been evicted."""


class NoActiveInstanceError(MemoryQdrantError):
    """Project has no active in-memory instance."""


class CacheFullError(MemoryQdrantError):
    """Cache pool is full and no instance can be evicted (all active)."""
