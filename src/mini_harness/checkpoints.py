"""沿用 LangGraph SQLite 接口，只保留每个会话命名空间的最新状态。"""

from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

_KEEP_LATEST = """
    DELETE FROM checkpoints
    WHERE (thread_id, checkpoint_ns, checkpoint_id) NOT IN (
        SELECT thread_id, checkpoint_ns, MAX(checkpoint_id)
        FROM checkpoints GROUP BY thread_id, checkpoint_ns
    );
    DELETE FROM writes WHERE NOT EXISTS (
        SELECT 1 FROM checkpoints
        WHERE checkpoints.thread_id = writes.thread_id
          AND checkpoints.checkpoint_ns = writes.checkpoint_ns
          AND checkpoints.checkpoint_id = writes.checkpoint_id
    );
    UPDATE checkpoints SET parent_checkpoint_id = NULL
    WHERE parent_checkpoint_id IS NOT NULL;
"""


@asynccontextmanager
async def open_checkpointer(database: Path):
    """先整理旧历史，再让每次新快照与历史清理在同一事务中提交。"""
    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        await saver.setup()
        async with saver.lock:
            # 只删除旧快照关联的 writes，最新快照的待写结果用于中断恢复。
            await saver.conn.executescript(f"""
                BEGIN;
                CREATE TRIGGER IF NOT EXISTS keep_latest_checkpoint
                AFTER INSERT ON checkpoints BEGIN
                    {_KEEP_LATEST}
                END;
                {_KEEP_LATEST}
                COMMIT;
            """)
        yield saver
