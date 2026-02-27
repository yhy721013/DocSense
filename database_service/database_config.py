# database_config.py
import os
from typing import Optional



class DatabaseConfig:
    def __init__(self):
        # MongoDB 配置
        self.mongodb_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
        self.mongodb_database_name = os.getenv("MONGODB_DATABASE_NAME", "document_classification")
        self.mongodb_collection_name = os.getenv("MONGODB_COLLECTION_NAME", "document_results")

        # MySQL 配置
        self.mysql_host = os.getenv("MYSQL_HOST", "localhost")
        self.mysql_port = int(os.getenv("MYSQL_PORT", 3306))
        self.mysql_user = os.getenv("MYSQL_USER", "root")
        self.mysql_password = os.getenv("MYSQL_PASSWORD", "282851") #你的数据库密码
        self.mysql_database = os.getenv("MYSQL_DATABASE", "military_db") # 要创建的军事数据库名


    def get_mongodb_config(self):
        return {
            "uri": self.mongodb_uri,
            "database_name": self.mongodb_database_name,
            "collection_name": self.mongodb_collection_name
        }

    def get_mysql_config(self):
        return {
            "host": self.mysql_host,
            "port": self.mysql_port,
            "user": self.mysql_user,
            "password": self.mysql_password,
            "database": self.mysql_database
        }


def main():
    """测试MongoDB连接"""
    try:
        config = DatabaseConfig()
        print(f"🔧 MongoDB使用配置:")
        print(f"   MongoDB URI: {config.mongodb_uri}")
        print(f"   MongoDB数据库名称: {config.mongodb_database_name}")
        print(f"   MongoDB集合名称: {config.mongodb_collection_name}")
        from database_service import MongoDBService
        service = MongoDBService()
        print("✅ MongoDB客户端创建成功")
        print(f"   数据库: {service.db.name}")
        print(f"   集合: {service.collection.name}")
        print("✅ MongoDB数据库连接测试成功!\n")

        # 打印 MySQL 配置信息
        mysql_config = config.get_mysql_config()
        print(f"🔧 MySQL使用配置:")
        print(f"   MySQL主机: {mysql_config['host']}")
        print(f"   MySQL端口: {mysql_config['port']}")
        print(f"   MySQL用户: {mysql_config['user']}")
        print(f"   MySQL数据库: {mysql_config['database']}")
        from database_service import MySQLService
        db = MySQLService()
        print("✅ MySQL数据库连接测试成功！")

        return True

    except Exception as e:
        print(f"\n❌ 连接测试失败: {e}")
        return False


if __name__ == "__main__":
    main()