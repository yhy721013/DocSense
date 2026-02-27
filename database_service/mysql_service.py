# mysql_service.py
import pymysql
import logging
from typing import Optional, Dict, Any, List
from database_service.database_config import DatabaseConfig
from database_service.mysql_table_definitions import CREATE_TABLE_SQL  # 引入建表 SQL 语句

class MySQLService:
    def __init__(self):
        self.config = DatabaseConfig().get_mysql_config()
        self.connection = None
        self._connect()
        self.switch_to_database(self.config["database"])

    def _connect(self):
        """建立数据库连接"""
        try:
            # 先连接到 MySQL 服务（不指定数据库）
            self.connection = pymysql.connect(
                host=self.config["host"],
                port=self.config["port"],
                user=self.config["user"],
                password=self.config["password"]
            )
            logging.info("✅ 成功连接到 MySQL 服务")
            print("hello")  # 输出简单回应
        except Exception as e:
            logging.error(f"❌ 连接 MySQL 服务失败: {e}")
            raise e

    def _close(self):
        """关闭数据库连接"""
        if self.connection:
            self.connection.close()
            logging.info("数据库连接已关闭")

    def switch_to_database(self, database_name: str):
        """切换或创建数据库"""
        try:
            with self.connection.cursor() as cursor:
                # 检查数据库是否存在
                cursor.execute("SHOW DATABASES LIKE %s", (database_name,))
                result = cursor.fetchone()
                if not result:
                    # 如果数据库不存在，则创建
                    cursor.execute(f"CREATE DATABASE {database_name}")
                    logging.info(f"✅ 数据库 {database_name} 已创建")
                else:
                    logging.info(f"✅ 数据库 {database_name} 已存在")
                # 切换到目标数据库
                self.connection.select_db(database_name)
                logging.info(f"✅ 已切换到数据库 {database_name}")
        except Exception as e:
            logging.error(f"❌ 切换或创建数据库失败: {e}")
            raise e

    def init_database(self):
        """初始化数据库：创建数据库+创建所有表（已存在的表不会重复创建）"""
        try:
            with self.connection.cursor() as cursor:
                # 获取当前数据库中已存在的表
                cursor.execute("SHOW TABLES")
                existing_tables = {row[0] for row in cursor.fetchall()}
                logging.info(f"当前数据库中存在的表: {existing_tables}")

                # 定义所有需要的表（包括军事基地表）
                all_tables = [
                    "military_main", "equip_basic", "equip_tactical", "equip_apply", "equip_eff_model",
                    "combat_system_scene", "combat_war_case", "org_structure", "military_regulation",
                    "ocean_environment", "military_base"  # 添加军事基地表
                ]

                # 按顺序创建缺失的表
                created_tables = []
                for table_name in all_tables:
                    if table_name not in existing_tables:
                        try:
                            cursor.execute(CREATE_TABLE_SQL[table_name])
                            created_tables.append(table_name)
                            logging.info(f"✅ 表{table_name}创建成功")
                        except Exception as create_error:
                            logging.error(f"❌ 表{table_name}创建失败: {create_error}")
                            # 不中断其他表的创建
                    else:
                        logging.info(f"ℹ️  表{table_name}已存在，跳过创建")

                self.connection.commit()

                if created_tables:
                    logging.info(f"✅ 新创建了 {len(created_tables)} 个表: {created_tables}")
                else:
                    logging.info("✅ 所有表均已存在，无需创建")

        except Exception as e:
            self.connection.rollback()
            logging.error(f"数据库初始化失败: {e}")
            raise e

    def insert_data(self, table_name: str, data: Dict[str, Any]) -> int:
        """
        单条数据插入
        :param table_name: 表名
        :param data: 插入数据，字典格式{字段名: 字段值}
        :return: 插入数据的主键id
        """
        try:
            with self.connection.cursor() as cursor:
                # 处理MySQL保留关键字 - 用反引号包围
                mysql_reserved_keywords = {
                    'function', 'order', 'group', 'select', 'insert', 'update', 'delete',
                    'from', 'where', 'join', 'left', 'right', 'inner', 'outer'
                }

                processed_fields = []
                for field in data.keys():
                    if field.lower() in mysql_reserved_keywords:
                        processed_fields.append(f"`{field}`")
                    else:
                        processed_fields.append(field)

                fields = ", ".join(processed_fields)
                values = ", ".join([f"%({k})s" for k in data.keys()])
                sql = f"INSERT INTO {table_name} ({fields}) VALUES ({values})"
                cursor.execute(sql, data)
                self.connection.commit()
                insert_id = cursor.lastrowid
                logging.info(f"表{table_name}插入数据成功，主键id：{insert_id}")
                return insert_id
        except Exception as e:
            self.connection.rollback()
            logging.error(f"表{table_name}插入数据失败: {e}")
            raise e

    def batch_insert(self, table_name: str, data_list: List[Dict[str, Any]]) -> None:
        """
        批量数据插入
        :param table_name: 表名
        :param data_list: 插入数据列表，元素为字典{字段名: 字段值}
        """
        if not data_list:
            logging.warning("批量插入数据为空，无需执行")
            return
        try:
            with self.connection.cursor() as cursor:
            # 处理MySQL保留关键字
                mysql_reserved_keywords = {
                    'function', 'order', 'group', 'select', 'insert', 'update', 'delete',
                    'from', 'where', 'join', 'left', 'right', 'inner', 'outer'
                }

                if data_list:
                    processed_fields = []
                    for field in data_list[0].keys():
                        if field.lower() in mysql_reserved_keywords:
                            processed_fields.append(f"`{field}`")
                        else:
                            processed_fields.append(field)

                    fields = ", ".join(processed_fields)
                    values = ", ".join([f"%({k})s" for k in data_list[0].keys()])
                    sql = f"INSERT INTO {table_name} ({fields}) VALUES ({values})"
                    cursor.executemany(sql, data_list)
                    self.connection.commit()
                    logging.info(f"表{table_name}批量插入{len(data_list)}条数据成功")
        except Exception as e:
            self.connection.rollback()
            logging.error(f"表{table_name}批量插入数据失败: {e}")
            raise e

    def update_data(self, table_name: str, update_data: Dict[str, Any], where_cond: Dict[str, Any]) -> int:
        """
        更新数据
        :param table_name: 表名
        :param update_data: 更新的字段和值，字典格式
        :param where_cond: 更新条件，字典格式
        :return: 受影响的行数
        """
        try:
            with self.connection.cursor() as cursor:
                update_str = ", ".join([f"{k}=%({k})s" for k in update_data.keys()])
                where_str = " AND ".join([f"{k}=%({k})s" for k in where_cond.keys()])
                data = {**update_data, **where_cond}
                sql = f"UPDATE {table_name} SET {update_str} WHERE {where_str}"
                affected_rows = cursor.execute(sql, data)
                self.connection.commit()
                logging.info(f"表{table_name}更新数据成功，受影响行数：{affected_rows}")
                return affected_rows
        except Exception as e:
            self.connection.rollback()
            logging.error(f"表{table_name}更新数据失败: {e}")
            raise e

    def delete_data(self, table_name: str, where_cond: Dict[str, Any]) -> int:
        """
        删除数据
        :param table_name: 表名
        :param where_cond: 删除条件，字典格式
        :return: 受影响的行数
        """
        if not where_cond:
            logging.error("删除条件不能为空，避免全表删除")
            raise ValueError("where_cond参数不能为空")
        try:
            with self.connection.cursor() as cursor:
                where_str = " AND ".join([f"{k}=%({k})s" for k in where_cond.keys()])
                sql = f"DELETE FROM {table_name} WHERE {where_str}"
                affected_rows = cursor.execute(sql, where_cond)
                self.connection.commit()
                logging.info(f"表{table_name}删除数据成功，受影响行数：{affected_rows}")
                return affected_rows
        except Exception as e:
            self.connection.rollback()
            logging.error(f"表{table_name}删除数据失败: {e}")
            raise e

    def query_data(self, sql: str, params: Optional[tuple] = None) -> List[Dict[str, Any]]:
        """
        通用查询方法（支持单表/多表联查）
        :param sql: 自定义查询SQL语句
        :param params: SQL参数，元组格式
        :return: 查询结果列表，元素为字典
        """
        try:
            with self.connection.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(sql, params)
                result = cursor.fetchall()
                logging.info(f"查询成功，返回{len(result)}条数据")
                return result
        except Exception as e:
            logging.error(f"查询失败: {e}")
            raise e

    def close(self):
        """关闭数据库连接"""
        self._close()



# 测试程序
if __name__ == "__main__":
    # 初始化数据库实例
    db = MySQLService()
    # 1. 执行数据库初始化（建库+建表）
    db.init_database()

    # 2. 测试插入数据（主表+装备基础表）
    # 插入主表数据
    main_data = {
        "data_module": "装备",
        "data_type": "基础数据",
        "core_theme": "F/A-18E/F超级大黄蜂",
        "keyword": "US,Navy,super hornet,战斗机,舰载机",
        "country": "美国",
        "military_service": "海军",
        "literature_source": "F/A-18E/F文件夹"
    }
    main_id = db.insert_data("military_main", main_data)

    # 插入装备基础表数据
    equip_basic_data = {
        "main_id": main_id,
        "equip_cn_name": "F/A-18E/F超级大黄蜂",
        "equip_en_name": "F/A-18E/F Super Hornet",
        "equip_class": "舰载多用途战斗机",
        "manufacturer": "波音公司",
        "serve_status": "在役",
        "serve_quantity": 600,
        "cost": "1.2@2020"
    }
    db.insert_data("equip_basic", equip_basic_data)

    # 3. 测试多表联查
    query_sql = """
    SELECT m.core_theme, eb.equip_cn_name, eb.manufacturer, eb.serve_quantity
    FROM military_main m
    LEFT JOIN equip_basic eb ON m.id = eb.main_id
    WHERE m.core_theme LIKE '%F/A-18E/F%'
    """
    result = db.query_data(query_sql)
    print("多表联查结果：", result)