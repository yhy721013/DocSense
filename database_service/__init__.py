# __init__.py
from .mongodb_service import MongoDBService
from .mysql_service import MySQLService
from .database_config import DatabaseConfig

# 全局实例
document_db = MongoDBService()
mysql_db = MySQLService()
