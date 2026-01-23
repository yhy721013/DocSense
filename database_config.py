# database_config.py
import os
from typing import Optional
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure


class DatabaseConfig:
    def __init__(self):
        self.mongodb_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
        self.database_name = os.getenv("DATABASE_NAME", "document_classification")
        self.collection_name = os.getenv("COLLECTION_NAME", "document_results")

    def get_client(self) -> MongoClient:
        """创建MongoDB客户端连接"""
        try:
            client = MongoClient(self.mongodb_uri, serverSelectionTimeoutMS=5000)
            # 测试连接
            client.admin.command('ping')
            return client
        except ConnectionFailure:
            raise ConnectionError(f"无法连接到MongoDB: {self.mongodb_uri}")

    def get_database(self):
        """获取数据库实例"""
        client = self.get_client()
        return client[self.database_name]

    def get_collection(self):
        """获取集合实例"""
        db = self.get_database()
        return db[self.collection_name]


def main():
    """测试MongoDB连接"""
    try:
        config = DatabaseConfig()
        print(f"🔧 使用配置:")
        print(f"   URI: {config.mongodb_uri}")
        print(f"   数据库: {config.database_name}")
        print(f"   集合: {config.collection_name}")

        # 获取客户端
        client = config.get_client()
        print("✅ MongoDB客户端创建成功")

        # 测试基本操作
        db = config.get_database()
        collection = config.get_collection()

        # 尝试插入和删除测试文档
        test_doc = {"test": "connection", "timestamp": "now"}
        result = collection.insert_one(test_doc)
        print(f"✅ 测试写入成功，ID: {result.inserted_id}")

        # 删除测试文档
        collection.delete_one({"_id": result.inserted_id})
        print("✅ 测试清理完成")

        print("\n🎉 MongoDB连接测试成功!")
        return True

    except Exception as e:
        print(f"\n❌ 连接测试失败: {e}")
        return False


if __name__ == "__main__":
    main()