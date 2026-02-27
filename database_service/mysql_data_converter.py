# mysql_data_converter.py
import re
from typing import Optional, Any, Dict, List
from database_service import mysql_db
import logging

class MySQLDataConverter:
    def __init__(self):
        pass

    def convert_and_insert(self, parsed_result: Dict[str, Any], final_file_path: str) -> None:
        """
        将解析后的结果转换为MySQL表格式并插入数据库
        :param parsed_result: 解析后的JSON结果
        :param final_file_path: 最终文件路径
        """
        # 加强输入验证
        if not isinstance(parsed_result, dict):
            raise ValueError(f"parsed_result必须是字典类型，当前类型: {type(parsed_result)}")

        category = parsed_result.get("category")
        sub_category = parsed_result.get("sub_category", "")

        # 验证必要字段
        if not category:
            raise ValueError("parsed_result中缺少必需的'category'字段")

        logging.info(f"开始MySQL数据插入 - 分类: {category}, 子分类: {sub_category}")

        try:
            # 根据分类插入不同的表
            if category == "装备型号":
                self._insert_equip_data(parsed_result, final_file_path, sub_category)
            elif category == "作战指挥":
                self._insert_combat_data(parsed_result, final_file_path, sub_category)
            elif category == "军事基地":
                self._insert_base_data(parsed_result, final_file_path)
            elif category == "体系运用":
                self._insert_system_data(parsed_result, final_file_path)
            elif category == "作战环境":
                self._insert_environment_data(parsed_result, final_file_path)
            else:
                logging.warning(f"未识别的分类类型: {category}，跳过MySQL插入")
                return

            logging.info(f"✅ MySQL数据插入成功完成 - 分类: {category}")

        except Exception as e:
            logging.error(f"❌ MySQL数据插入失败 - 分类: {category}, 错误: {str(e)}")
            raise  # 重新抛出异常以便上层处理

    def _insert_equip_data(self, parsed_result: Dict[str, Any], final_file_path: str, sub_category=None) -> None:
        """
        插入装备型号相关数据
        :param parsed_result: 解析后的JSON结果
        :param final_file_path: 最终文件路径
        """
        # 插入主表数据
        main_data = {
            "data_module": "装备",
            "data_type": "基础数据",
            "core_theme": parsed_result.get("extract", {}).get("basic_info", {}).get("model", ""),
            "keyword": "",  # 可以从parsed_result中提取关键词
            "country": parsed_result.get("extract", {}).get("basic_info", {}).get("country", ""),
            "military_service": parsed_result.get("extract", {}).get("basic_info", {}).get("military_branch", ""),
            "literature_source": final_file_path,
            "data_note": ""
        }
        main_id = mysql_db.insert_data("military_main", main_data)

        # 插入装备基础表数据
        equip_basic_data = {
            "main_id": main_id,
            "equip_cn_name": parsed_result.get("extract", {}).get("basic_info", {}).get("model", ""),
            "equip_en_name": parsed_result.get("extract", {}).get("basic_info", {}).get("model_en", ""),
            "equip_class": sub_category,
            "manufacturer": parsed_result.get("extract", {}).get("basic_info", {}).get("manufacturer", ""),
            "serve_status": parsed_result.get("extract", {}).get("basic_info", {}).get("status", ""),
            "serve_date": parsed_result.get("extract", {}).get("basic_info", {}).get("service_date", None),
            "first_flight": parsed_result.get("extract", {}).get("basic_info", {}).get("first_flight", None),
            "serve_quantity": self._validate_and_convert_int(parsed_result.get("extract", {}).get("basic_info", {}).get("quantity", None), "serve_quantity"),
                "cost": parsed_result.get("extract", {}).get("basic_info", {}).get("cost", ""),
            "performance_feature": parsed_result.get("extract", {}).get("basic_info", {}).get("features", "")
        }
        mysql_db.insert_data("equip_basic", equip_basic_data)

        # 插入装备战技指标表数据
        equip_tactical_data = {
            "main_id": main_id,
            "length": parsed_result.get("extract", {}).get("specifications", {}).get("length", None),
            "width": parsed_result.get("extract", {}).get("specifications", {}).get("width", None),
            "height": parsed_result.get("extract", {}).get("specifications", {}).get("height", None),
            "wingspan": parsed_result.get("extract", {}).get("specifications", {}).get("wingspan", None),
            "wing_aspect_ratio": parsed_result.get("extract", {}).get("specifications", {}).get("wing_aspect_ratio", None),
            "net_wing_area": parsed_result.get("extract", {}).get("specifications", {}).get("net_wing_area", None),
            "tail_span": parsed_result.get("extract", {}).get("specifications", {}).get("tail_span", None),
            "empty_weight": parsed_result.get("extract", {}).get("specifications", {}).get("empty_weight", None),
            "max_takeoff_weight": parsed_result.get("extract", {}).get("specifications", {}).get("max_takeoff_weight", None),
            "max_load_weight": parsed_result.get("extract", {}).get("specifications", {}).get("max_load_weight", None),
            "max_wing_load": parsed_result.get("extract", {}).get("specifications", {}).get("max_wing_load", None),
            "crew_num": parsed_result.get("extract", {}).get("specifications", {}).get("crew_num", None),
            "min_runway_length": parsed_result.get("extract", {}).get("specifications", {}).get("min_runway_length", None),
            "min_maintain_time": parsed_result.get("extract", {}).get("specifications", {}).get("min_maintain_time", None),
            "max_maintain_time": parsed_result.get("extract", {}).get("specifications", {}).get("max_maintain_time", None),
            "climb_rate": parsed_result.get("extract", {}).get("specifications", {}).get("climb_rate", ""),
            "service_ceiling": parsed_result.get("extract", {}).get("specifications", {}).get("service_ceiling", None),
            "max_mach": parsed_result.get("extract", {}).get("specifications", {}).get("max_mach", None),
            "max_speed": parsed_result.get("extract", {}).get("specifications", {}).get("max_speed", ""),
            "cruising_speed": parsed_result.get("extract", {}).get("specifications", {}).get("cruising_speed", ""),
            "flight_range": parsed_result.get("extract", {}).get("specifications", {}).get("flight_range", None),
            "operation_radius": parsed_result.get("extract", {}).get("specifications", {}).get("operation_radius", None),
            "g_limits": parsed_result.get("extract", {}).get("specifications", {}).get("g_limits", None),
            "endurance": parsed_result.get("extract", {}).get("specifications", {}).get("endurance", "")
        }
        mysql_db.insert_data("equip_tactical", equip_tactical_data)

        # 插入装备运用数据表数据
        equip_apply_data = {
            "main_id": main_id,
            "use_manual": parsed_result.get("extract", {}).get("operational_data", {}).get("use_manual", ""),
            "deploy_unit": parsed_result.get("extract", {}).get("operational_data", {}).get("deploy_unit", ""),
            "deploy_time": parsed_result.get("extract", {}).get("operational_data", {}).get("deploy_time", ""),
            "deploy_place": parsed_result.get("extract", {}).get("operational_data", {}).get("deploy_place", ""),
            "deploy_task": parsed_result.get("extract", {}).get("operational_data", {}).get("deploy_task", ""),
            "exercise_name": parsed_result.get("extract", {}).get("operational_data", {}).get("exercise_name", ""),
            "exercise_content": parsed_result.get("extract", {}).get("operational_data", {}).get("exercise_content", ""),
            "maintain_cycle": parsed_result.get("extract", {}).get("operational_data", {}).get("maintain_cycle", ""),
            "maintain_manual": parsed_result.get("extract", {}).get("operational_data", {}).get("maintain_manual", ""),
            "accident_time": parsed_result.get("extract", {}).get("operational_data", {}).get("accident_time", None),
            "accident_place": parsed_result.get("extract", {}).get("operational_data", {}).get("accident_place", ""),
            "accident_type": parsed_result.get("extract", {}).get("operational_data", {}).get("accident_type", ""),
            "accident_process": parsed_result.get("extract", {}).get("operational_data", {}).get("accident_process", ""),
            "accident_result": parsed_result.get("extract", {}).get("operational_data", {}).get("accident_result", "")
        }
        mysql_db.insert_data("equip_apply", equip_apply_data)

        # 插入装备效能模型目特声像表数据
        equip_eff_model_data = {
            "main_id": main_id,
            "material_type": parsed_result.get("extract", {}).get("effectiveness", {}).get("material_type", ""),
            "material_param": parsed_result.get("extract", {}).get("effectiveness", {}).get("material_param", ""),
            "damage_evaluation": parsed_result.get("extract", {}).get("effectiveness", {}).get("damage_evaluation", ""),
            "simulation_info": parsed_result.get("extract", {}).get("effectiveness", {}).get("simulation_info", ""),
            "model_structure": parsed_result.get("extract", {}).get("effectiveness", {}).get("model_structure", ""),
            "model_format": parsed_result.get("extract", {}).get("effectiveness", {}).get("model_format", ""),
            "rcs": parsed_result.get("extract", {}).get("signatures", {}).get("rcs", ""),
            "optical_cross_section": parsed_result.get("extract", {}).get("signatures", {}).get("optical_cross_section", ""),
            "ir_signature": parsed_result.get("extract", {}).get("signatures", {}).get("ir_signature", ""),
            "media_type": parsed_result.get("extract", {}).get("signatures", {}).get("media_type", ""),
            "media_view": parsed_result.get("extract", {}).get("signatures", {}).get("media_view", ""),
            "media_tag": parsed_result.get("extract", {}).get("signatures", {}).get("media_tag", ""),
            "media_desc": parsed_result.get("extract", {}).get("signatures", {}).get("media_desc", "")
        }
        mysql_db.insert_data("equip_eff_model", equip_eff_model_data)

    def _insert_combat_data(self, parsed_result: Dict[str, Any], final_file_path: str, sub_category=None) -> None:
        """
        插入作战指挥相关数据
        :param parsed_result: 解析后的JSON结果
        :param final_file_path: 最终文件路径
        """
        # 插入主表数据
        main_data = {
            "data_module": "作战指挥",
            "data_type": "条令条例" if sub_category == "条令条例" else "组织机构",
            "core_theme": parsed_result.get("extract", {}).get("doc_name", "") or parsed_result.get("extract", {}).get("org_name", ""),
            "keyword": "",  # 可以从parsed_result中提取关键词
            "country": parsed_result.get("extract", {}).get("country", ""),
            "military_service": parsed_result.get("extract", {}).get("military_branch", ""),
            "literature_source": final_file_path,
            "data_note": ""
        }
        main_id = mysql_db.insert_data("military_main", main_data)

        # 根据子分类插入不同的表
        if sub_category == "条令条例":
            regulation_data = {
                "main_id": main_id,
                "regulation_name": parsed_result.get("extract", {}).get("doc_name", ""),
                "regulation_type": parsed_result.get("extract", {}).get("regulation_type", ""),
                "issuer": parsed_result.get("extract", {}).get("issuing_authority", ""),
                "issue_date": parsed_result.get("extract", {}).get("issue_date", None),
                "version": parsed_result.get("extract", {}).get("version", ""),
                "apply_scope": parsed_result.get("extract", {}).get("scope", ""),
                "regulation_content": parsed_result.get("extract", {}).get("key_content", "")
            }
            mysql_db.insert_data("military_regulation", regulation_data)
        elif sub_category == "组织机构":
            org_data = {
                "main_id": main_id,
                "org_name": parsed_result.get("extract", {}).get("org_name", ""),
                "org_level": parsed_result.get("extract", {}).get("org_level", ""),
                "dept_setup": parsed_result.get("extract", {}).get("dept_setup", ""),
                "job_name": parsed_result.get("extract", {}).get("job_name", ""),
                "commander": parsed_result.get("extract", {}).get("commander", ""),
                "org_function": parsed_result.get("extract", {}).get("function", ""),
                "operation_mechanism": parsed_result.get("extract", {}).get("mechanism", "")
            }
            mysql_db.insert_data("org_structure", org_data)

    def _insert_base_data(self, parsed_result: Dict[str, Any], final_file_path: str) -> None:
        """
        插入军事基地相关数据
        :param parsed_result: 解析后的JSON结果
        :param final_file_path: 最终文件路径
        """
        # 插入主表数据
        main_data = {
            "data_module": "军事基地",
            "data_type": "基础数据",
            "core_theme": parsed_result.get("extract", {}).get("base_name", ""),
            "keyword": "",  # 可以从parsed_result中提取关键词
            "country": parsed_result.get("extract", {}).get("country", ""),
            "military_service": parsed_result.get("extract", {}).get("military_branch", ""),
            "literature_source": final_file_path,
            "data_note": ""
        }
        main_id = mysql_db.insert_data("military_main", main_data)

        # 插入军事基地表数据
        base_data = {
            "main_id": main_id,
            "base_name": parsed_result.get("extract", {}).get("base_name", ""),
            "location": parsed_result.get("extract", {}).get("location", ""),
            "facility_type": parsed_result.get("extract", {}).get("facility_type", ""),
            "function": parsed_result.get("extract", {}).get("function", ""),
            "capacity": parsed_result.get("extract", {}).get("capacity", ""),
            "status": parsed_result.get("extract", {}).get("status", "")
        }
        mysql_db.insert_data("military_base", base_data)

    def _insert_system_data(self, parsed_result: Dict[str, Any], final_file_path: str) -> None:
        """
        插入体系运用相关数据
        :param parsed_result: 解析后的JSON结果
        :param final_file_path: 最终文件路径
        """
        # 插入主表数据
        main_data = {
            "data_module": "体系运用",
            "data_type": "体系数据",
            "core_theme": parsed_result.get("extract", {}).get("system_type", ""),
            "keyword": "",  # 可以从parsed_result中提取关键词
            "country": parsed_result.get("extract", {}).get("country", ""),
            "military_service": parsed_result.get("extract", {}).get("military_branch", ""),
            "literature_source": final_file_path,
            "data_note": ""
        }
        main_id = mysql_db.insert_data("military_main", main_data)

        # 插入体系运用表数据
        system_data = {
            "main_id": main_id,
            "system_type": parsed_result.get("extract", {}).get("system_type", ""),
            "components": parsed_result.get("extract", {}).get("components", ""),
            "capabilities": parsed_result.get("extract", {}).get("capabilities", ""),
            "coordination_mode": parsed_result.get("extract", {}).get("coordination_mode", ""),
            "application_scenario": parsed_result.get("extract", {}).get("application_scenario", "")
        }
        mysql_db.insert_data("system_application", system_data)

    def _insert_environment_data(self, parsed_result: Dict[str, Any], final_file_path: str) -> None:
        """
        插入作战环境相关数据
        :param parsed_result: 解析后的JSON结果
        :param final_file_path: 最终文件路径
        """
        # 插入主表数据
        main_data = {
            "data_module": "作战环境",
            "data_type": "环境数据",
            "core_theme": parsed_result.get("extract", {}).get("region", ""),
            "keyword": "",  # 可以从parsed_result中提取关键词
            "country": parsed_result.get("extract", {}).get("country", ""),
            "military_service": parsed_result.get("extract", {}).get("military_branch", ""),
            "literature_source": final_file_path,
            "data_note": ""
        }
        main_id = mysql_db.insert_data("military_main", main_data)

        # 插入海洋环境表数据
        ocean_data = {
            "main_id": main_id,
            "ocean_region": parsed_result.get("extract", {}).get("region", ""),
            "ocean_current": parsed_result.get("extract", {}).get("current", ""),
            "wave": parsed_result.get("extract", {}).get("wave", ""),
            "wave_height": parsed_result.get("extract", {}).get("wave_height", None),
            "tide": parsed_result.get("extract", {}).get("tide", ""),
            "sea_temperature": parsed_result.get("extract", {}).get("temperature", None),
            "salinity": parsed_result.get("extract", {}).get("salinity", None),
            "ocean_note": parsed_result.get("extract", {}).get("note", "")
        }
        mysql_db.insert_data("ocean_environment", ocean_data)


    def _validate_and_convert_int(self, value: Any, field_name: str = "未知字段") -> Optional[int]:
        """
        验证并转换整数类型数据
        :param value: 原始值
        :param field_name: 字段名称（用于日志）
        :return: 转换后的整数或None
        """
        if value is None or value == "":
            return None

        # 如果已经是整数类型，直接返回
        if isinstance(value, int):
            return value

        # 如果是浮点数，转换为整数
        if isinstance(value, float):
            return int(value)

        # 如果是字符串，尝试提取数字
        if isinstance(value, str):
            # 移除前后空白
            value = value.strip()

            # 如果是纯数字字符串
            if value.isdigit():
                return int(value)

            # 尝试从复杂字符串中提取数字（如 "44 in service, 3 building"）
            # 匹配第一个出现的数字
            number_match = re.search(r'\d+', value)
            if number_match:
                extracted_number = int(number_match.group())
                logging.warning(f"字段 '{field_name}' 的值 '{value}' 包含非数字字符，已提取数字部分: {extracted_number}")
                return extracted_number

            # 如果无法提取数字
            logging.warning(f"字段 '{field_name}' 的值 '{value}' 无法转换为整数，已设置为NULL")
            return None

        # 其他类型尝试转换
        try:
            return int(value)
        except (ValueError, TypeError):
            logging.warning(f"字段 '{field_name}' 的值 '{value}' 无法转换为整数，已设置为NULL")
            return None
