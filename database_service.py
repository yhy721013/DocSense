# database_service.py
from datetime import datetime
from typing import Optional, Dict, Any, List
from pathlib import Path
import json
import logging
from bson import ObjectId
from gridfs import GridFS
import mimetypes

from database_config import DatabaseConfig


class DocumentDatabase:
    def __init__(self):
        self.config = DatabaseConfig()
        self.db = self.config.get_database()
        self.collection = self.db[self.config.collection_name]
        self.fs = GridFS(self.db)  # 添加GridFS实例
        self._create_indexes()

    def _create_indexes(self):
        """创建必要的索引"""
        # 按分类创建索引
        self.collection.create_index([("metadata.category", 1)])
        # 按处理时间创建索引
        self.collection.create_index([("created_at", -1)])
        # 按用户ID创建索引
        self.collection.create_index([("metadata.user_id", 1)])
        # 按文件名创建索引
        self.collection.create_index([("file_path", 1)])
        # 复合索引：分类+时间
        self.collection.create_index([("metadata.category", 1), ("created_at", -1)])

    def save_result(
            self,
            original_file_path: str,
            final_file_path: str,
            result: str,
            metadata: Dict[str, Any],
            store_original_file: bool = True  # 新增参数
    ) -> Optional[str]:
        """
        保存文档处理结果到MongoDB

        Args:
            original_file_path: 原始文件路径
            final_file_path: 最终分类后的文件路径
            result: JSON格式的处理结果
            metadata: 元数据信息（分类、用户ID、时间等）
            store_original_file: 是否同时存储原始文件

        Returns:
            插入文档的ID，失败返回None
        """
        try:
            # 解析JSON结果，用于分类提取
            if isinstance(result, str):
                parsed_result = json.loads(result)
            else:
                parsed_result = result

            document = {
                "original_file_path": str(original_file_path),
                "final_file_path": str(final_file_path),
                "original_file_name": Path(original_file_path).name,
                "final_file_name": Path(final_file_path).name,
                "original_file_size": Path(original_file_path).stat().st_size if Path(
                    original_file_path).exists() else 0,
                "final_file_size": Path(final_file_path).stat().st_size if Path(final_file_path).exists() else 0,
                "result_json": result,
                "parsed_result": parsed_result,
                "metadata": metadata,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow()
            }

            # 提取分类信息用于索引
            category = parsed_result.get("category", "")
            sub_category = parsed_result.get("sub_category", "")
            document["category"] = category
            document["sub_category"] = sub_category
            document["full_category"] = f"{category}/{sub_category}" if sub_category else category

            # 如果需要存储原始文件
            if store_original_file and Path(original_file_path).exists():
                try:
                    # 获取文件MIME类型
                    mime_type, _ = mimetypes.guess_type(original_file_path)
                    if mime_type is None:
                        mime_type = 'application/octet-stream'

                    # 将原始文件存储到GridFS
                    with open(original_file_path, 'rb') as f:
                        file_id = self.fs.put(
                            f,
                            filename=Path(original_file_path).name,
                            content_type=mime_type,
                            metadata={
                                "original_path": str(original_file_path),
                                "file_size": Path(original_file_path).stat().st_size,
                                "category": category,
                                "sub_category": sub_category
                            }
                        )
                    document["original_file_gridfs_id"] = file_id
                    logging.info(f"原始文件已存储到GridFS: {original_file_path}, ID: {file_id}")
                except Exception as e:
                    logging.error(f"存储原始文件到GridFS失败: {str(e)}")

            result = self.collection.insert_one(document)
            logging.info(
                f"成功保存文档结果到MongoDB: 原始路径={original_file_path}, 最终路径={final_file_path}, ID: {result.inserted_id}")
            return str(result.inserted_id)

        except Exception as e:
            logging.error(
                f"保存文档结果到MongoDB失败: 原始路径={original_file_path}, 最终路径={final_file_path}, 错误: {str(e)}")
            return None

    def get_original_file_by_id(self, file_id: str):
        """
        根据GridFS ID获取原始文件

        Args:
            file_id: GridFS中存储的文件ID

        Returns:
            文件对象，如果不存在则返回None
        """
        try:
            object_id = ObjectId(file_id)
            if self.fs.exists(object_id):
                return self.fs.get(object_id)
            return None
        except Exception as e:
            logging.error(f"获取原始文件失败: {file_id}, 错误: {str(e)}")
            return None

    def get_result_by_id(self, result_id: str) -> Optional[Dict[str, Any]]:
        """根据ID获取文档处理结果"""
        try:
            result = self.collection.find_one({"_id": ObjectId(result_id)})
            return result
        except Exception as e:
            logging.error(f"根据ID获取文档结果失败: {result_id}, 错误: {str(e)}")
            return None

    def get_result_by_file_path(self, file_path: str) -> Optional[Dict[str, Any]]:
        """根据文件路径获取文档处理结果"""
        try:
            result = self.collection.find_one({"file_path": str(file_path)})
            return result
        except Exception as e:
            logging.error(f"根据文件路径获取文档结果失败: {file_path}, 错误: {str(e)}")
            return None

    def get_results_by_category(self, category: str, limit: int = 100) -> List[Dict[str, Any]]:
        """按分类获取文档处理结果"""
        try:
            query = {"category": category}
            results = list(self.collection.find(query).limit(limit))
            return results
        except Exception as e:
            logging.error(f"按分类获取文档结果失败: {category}, 错误: {str(e)}")
            return []

    def get_results_by_user(self, user_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        """按用户ID获取文档处理结果"""
        try:
            query = {"metadata.user_id": user_id}
            results = list(self.collection.find(query).sort("created_at", -1).limit(limit))
            return results
        except Exception as e:
            logging.error(f"按用户ID获取文档结果失败: {user_id}, 错误: {str(e)}")
            return []

    def update_result(self, result_id: str, updates: Dict[str, Any]) -> bool:
        """更新文档处理结果"""
        try:
            updates["updated_at"] = datetime.utcnow()
            result = self.collection.update_one(
                {"_id": ObjectId(result_id)},
                {"$set": updates}
            )
            return result.modified_count > 0
        except Exception as e:
            logging.error(f"更新文档结果失败: {result_id}, 错误: {str(e)}")
            return False

    def delete_result(self, result_id: str) -> bool:
        """删除文档处理结果"""
        try:
            # 先查找文档，如果有原始文件的GridFS ID，则删除对应的文件
            doc = self.collection.find_one({"_id": ObjectId(result_id)})
            if doc and "original_file_gridfs_id" in doc:
                try:
                    self.fs.delete(ObjectId(doc["original_file_gridfs_id"]))
                    logging.info(f"已删除GridFS中的原始文件: {doc['original_file_gridfs_id']}")
                except Exception as e:
                    logging.error(f"删除GridFS文件失败: {e}")

            result = self.collection.delete_one({"_id": ObjectId(result_id)})
            return result.deleted_count > 0
        except Exception as e:
            logging.error(f"删除文档结果失败: {result_id}, 错误: {str(e)}")
            return False

    def search_results(self, filters: Dict[str, Any], limit: int = 100) -> List[Dict[str, Any]]:
        """搜索文档处理结果"""
        try:
            results = list(self.collection.find(filters).sort("created_at", -1).limit(limit))
            return results
        except Exception as e:
            logging.error(f"搜索文档结果失败: {filters}, 错误: {str(e)}")
            return []


# 全局数据库实例
document_db = DocumentDatabase()
