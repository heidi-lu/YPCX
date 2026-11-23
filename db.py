import os
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models import Base

# 确保路径正确
engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

# SQLite 开启 WAL，缓解并发写 database is locked
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
        finally:
            cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

def init_db():
    print("========================================")
    print("🚀 [DB] 开始检查并初始化数据库表...")
    try:
        # 核心建表逻辑
        Base.metadata.create_all(engine)
        
        # 验证表是否真的存在
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        print(f"✅ [DB] 初始化完成，当前数据库包含表: {tables}")
        
        if "drugs" not in tables:
            print("❌ [DB] 严重警告：'drugs' 表未能成功创建！请检查 models.py 的 Base 导入是否正确。")
        else:
            cols = [c['name'] for c in inspector.get_columns('drugs')]
            for _col, _ddl in (
                ("insurance_status_text", "ALTER TABLE drugs ADD COLUMN insurance_status_text VARCHAR(200) DEFAULT ''"),
                ("price_insurance", "ALTER TABLE drugs ADD COLUMN price_insurance VARCHAR(200) DEFAULT ''"),
                ("origin_region", "ALTER TABLE drugs ADD COLUMN origin_region VARCHAR(10) DEFAULT ''"),
                ("indication_approval_source", "ALTER TABLE drugs ADD COLUMN indication_approval_source VARCHAR(500) DEFAULT ''"),
                ("drug_insurance_date", "ALTER TABLE drugs ADD COLUMN drug_insurance_date VARCHAR(100) DEFAULT ''"),
            ):
                if _col not in cols:
                    with engine.begin() as conn:
                        conn.execute(text(_ddl))
                    print(f"✅ [DB] 已补充字段 {_col}")
            print(f"✅ [DB] 'drugs' 表字段数: {len(cols)}")
            if "manual_locked_fields" not in cols:
                print("⚠️ [DB] 警告：缺少新字段 manual_locked_fields，可能使用了旧代码！")
    except Exception as e:
        print(f"❌ [DB] 初始化表失败: {e}")
    print("========================================")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()