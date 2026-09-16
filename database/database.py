import logging
import aiomysql
from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

log = logging.getLogger(__name__)

pool: aiomysql.Pool | None = None

async def init_pool():
    global pool
    pool = await aiomysql.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        db=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        autocommit=True,
        minsize=5,
        maxsize=50,
        charset="utf8mb4",
    )
    log.info("Datenbankverbindung hergestellt.")

async def close_pool():
    if pool:
        pool.close()
        await pool.wait_closed()
        log.info("Datenbankverbindung geschlossen.")
