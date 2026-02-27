import logging

from database_service import mysql_db


def init_database():
    """初始化数据库表结构"""
    try:
        logging.info("开始检查和初始化数据库表...")

        # 获取所有需要的表名
        required_tables = [
            "military_main", "equip_basic", "equip_tactical", "equip_apply",
            "equip_eff_model", "combat_system_scene", "combat_war_case",
            "org_structure", "military_regulation", "ocean_environment"
        ]

        # 初始化数据库（会自动创建缺失的表）
        mysql_db.init_database()
        logging.info("✅ 数据库表初始化完成")

    except Exception as e:
        logging.error(f"❌ 数据库初始化失败: {e}")
        # 不中断应用启动，但记录错误
