import os, sqlite3, json, hashlib, re
from typing import Tuple, Optional

def normalize_text(t: str) -> str:
    t = t.strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t

def sha256_hex(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()

SQLITE_DEFAULT = "./data/caia_memory.db"

DDL = """
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    collection TEXT NOT NULL,
    type TEXT NOT NULL,
    when_ts TEXT,
    context TEXT,
    what TEXT,
    insight TEXT,
    decision TEXT,
    why TEXT,
    tags TEXT,
    recall_hooks TEXT,
    digest_hash TEXT UNIQUE,
    created_at TEXT,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_records_collection ON records(collection);
CREATE INDEX IF NOT EXISTS idx_records_digest ON records(digest_hash);
"""

class Storage:
    def __init__(self, backend: str, **kwargs):
        self.backend = backend
        if backend == "sqlite":
            path = kwargs.get("path") or os.environ.get("SQLITE_PATH", SQLITE_DEFAULT)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            cur = self.conn.cursor()
            for stmt in DDL.strip().split(";"):
                s = stmt.strip()
                if s:
                    cur.execute(s)
            self.conn.commit()
        elif backend == "qdrant":
            # lazy import
            from qdrant_client import QdrantClient
            url = kwargs.get("url") or os.environ.get("QDRANT_URL")
            api_key = kwargs.get("api_key") or os.environ.get("QDRANT_API_KEY")
            self.qdrant = QdrantClient(url=url, api_key=api_key)
            self.vector_name = os.environ.get("QDRANT_VECTOR_NAME","text_vector")
            self.vector_size = int(os.environ.get("QDRANT_VECTOR_SIZE","1024"))
            # Collections are created on demand (upsert)
        else:
            raise ValueError(f"unknown backend: {backend}")

    @classmethod
    def from_env(cls) -> "Storage":
        be = os.environ.get("STORAGE_BACKEND","sqlite").lower()
        if be == "qdrant":
            return cls("qdrant")
        return cls("sqlite")

    # ---- SQLite implementations ----
    def _sqlite_upsert(self, rec: dict) -> Tuple[str, str]:
        cur = self.conn.cursor()
        try:
            cur.execute("""INSERT INTO records
                (id, collection, type, when_ts, context, what, insight, decision, why, tags, recall_hooks, digest_hash, created_at, expires_at)
                VALUES (:id, :collection, :type, :when, :context, :what, :insight, :decision, :why, :tags, :recall_hooks, :digest_hash, :created_at, :expires_at)""",
                {
                    "id": rec["id"],
                    "collection": rec["collection"],
                    "type": rec["type"],
                    "when": rec.get("when"),
                    "context": rec.get("context"),
                    "what": rec.get("what"),
                    "insight": rec.get("insight"),
                    "decision": rec.get("decision"),
                    "why": rec.get("why"),
                    "tags": json.dumps(rec.get("tags") or []),
                    "recall_hooks": json.dumps(rec.get("recall_hooks") or []),
                    "digest_hash": rec.get("digest_hash"),
                    "created_at": rec.get("created_at"),
                    "expires_at": rec.get("expires_at"),
                }
            )
            self.conn.commit()
            return "inserted", rec["id"]
        except sqlite3.IntegrityError as e:
            # duplicate digest -> fetch existing id
            cur.execute("SELECT id FROM records WHERE digest_hash=?", (rec["digest_hash"],))
            row = cur.fetchone()
            rid = row["id"] if row else rec["id"]
            # Merge: update tags/recall_hooks minimally
            cur.execute("SELECT tags, recall_hooks FROM records WHERE id=?", (rid,))
            row = cur.fetchone()
            tags_old = set(json.loads(row["tags"])) if row and row["tags"] else set()
            hooks_old = set(json.loads(row["recall_hooks"])) if row and row["recall_hooks"] else set()
            tags_new = tags_old.union(set(rec.get("tags") or []))
            hooks_new = hooks_old.union(set(rec.get("recall_hooks") or []))
            cur.execute("UPDATE records SET tags=?, recall_hooks=? WHERE id=?",
                        (json.dumps(sorted(tags_new)), json.dumps(sorted(hooks_new)), rid))
            self.conn.commit()
            return "merged", rid

    def _sqlite_get(self, rid: str) -> Optional[dict]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM records WHERE id=?", (rid,))
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["tags"] = json.loads(d["tags"]) if d.get("tags") else []
        d["recall_hooks"] = json.loads(d["recall_hooks"]) if d.get("recall_hooks") else []
        return d

    # ---- Qdrant implementations (minimal) ----
    def _qdrant_upsert(self, rec: dict) -> Tuple[str, str]:
        from qdrant_client.models import Distance, VectorParams, PointStruct
        collection = rec["collection"]
        # Ensure collection
        try:
            self.qdrant.get_collection(collection)
        except Exception:
            self.qdrant.recreate_collection(
                collection_name=collection,
                vectors_config={self.vector_name: VectorParams(size=self.vector_size, distance=Distance.COSINE)}
            )
        # Use digest_hash as point id for dedup
        pid = int(int(hashlib.sha256(rec["digest_hash"].encode()).hexdigest(), 16) % (10**18))
        payload = rec.copy()
        vector = [0.0] * self.vector_size  # placeholder (embed externally if needed)
        self.qdrant.upsert(
            collection_name=collection,
            points=[PointStruct(id=pid, vector={self.vector_name: vector}, payload=payload)]
        )
        return "upserted", rec["id"]

    def _qdrant_get(self, rid: str) -> Optional[dict]:
        # Not efficient without an index on id; this is a placeholder.
        # In practice, you'd also store a mapping id->point id.\n
        return None

    # ---- Public ----
    def upsert(self, rec: dict) -> Tuple[str, str]:
        if self.backend == "sqlite":
            return self._sqlite_upsert(rec)
        return self._qdrant_upsert(rec)

    def get_by_id(self, rid: str) -> Optional[dict]:
        if self.backend == "sqlite":
            return self._sqlite_get(rid)
        return self._qdrant_get(rid)
