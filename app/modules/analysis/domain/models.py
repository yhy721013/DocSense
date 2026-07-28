"""文件分析领域的稳定常量与默认范围。

本文件只保存不依赖环境、网络或数据库的业务事实。运行时环境变量仍由兼容配置层读取，
但其可选值必须从这里导出，避免纯规则反向依赖旧服务配置模块。
"""

from __future__ import annotations


# 分类模式与保护模式是领域可识别的有限值；配置层只负责读取和校验环境变量。
ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE = "topk_two_stage"
ANALYSIS_CLASSIFICATION_MODE_TOPK_SINGLE = "topk_single"
ANALYSIS_CLASSIFICATION_MODE_LEGACY = "legacy"
ANALYSIS_CLASSIFICATION_MODES = frozenset(
    {
        ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
        ANALYSIS_CLASSIFICATION_MODE_TOPK_SINGLE,
        ANALYSIS_CLASSIFICATION_MODE_LEGACY,
    }
)

ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY = "legacy"
ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD = "scope_guard"
ANALYSIS_FILENAME_CONSTRAINT_MODES = frozenset(
    {
        ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY,
        ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
    }
)

ANALYSIS_DATA_STANDARD_MODE_LEGACY = "legacy"
ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD = "scope_guard"
ANALYSIS_DATA_STANDARD_MODES = frozenset(
    {
        ANALYSIS_DATA_STANDARD_MODE_LEGACY,
        ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    }
)

ANALYSIS_IDENTITY_RESELECT_MODE_OFF = "off"
ANALYSIS_IDENTITY_RESELECT_MODE_SHADOW = "shadow"
ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE = "enforce"
ANALYSIS_IDENTITY_RESELECT_MODES = frozenset(
    {
        ANALYSIS_IDENTITY_RESELECT_MODE_OFF,
        ANALYSIS_IDENTITY_RESELECT_MODE_SHADOW,
        ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
    }
)

# 这些上限是既有 analysis 协议内部的稳定保护值，迁移阶段不得调整。
MAX_ANALYSIS_PROMPT_CHARS = 32_000
MAX_ANALYSIS_MODEL_CALLS = 4
MAX_ANALYSIS_PHASE_CALLS = 2
MAX_ANALYSIS_PARAMS_PER_REQUEST = 32
MAX_ANALYSIS_REQUEST_BYTES = 64 * 1024 * 1024

DEFAULT_COUNTRY_OPTIONS = [
    {"key": "02", "value": "美国"},
    {"key": "03", "value": "俄罗斯"},
    {"key": "04", "value": "日本"},
    {"key": "05", "value": "英国"},
    {"key": "06", "value": "法国"},
]

DEFAULT_FORMAT_OPTIONS = [
    {"key": "01", "value": "音频类"},
    {"key": "03", "value": "文档类"},
    {"key": "04", "value": "图片类"},
]

DEFAULT_MATURITY_OPTIONS = [
    {"key": "01", "value": "概念研究"},
    {"key": "02", "value": "阶段成果"},
    {"key": "03", "value": "定型成果"},
]

DEFAULT_SECURITY_OPTIONS = [
    {"key": "02", "value": "公开"},
]

DEFAULT_ARCHITECTURE_OPTIONS = [
    {"id": 101, "name": "军事基地", "parentId": None, "path": "101", "pathName": "军事基地", "remark": "军事设施、基地建设、基地布局、港口码头、机场跑道、后勤保障设施等。"},
    {"id": 102, "name": "体系运用", "parentId": None, "path": "102", "pathName": "体系运用", "remark": "作战体系、系统集成、联合作战、协同配合、多域作战、体系对抗等。"},
    {"id": 103, "name": "装备型号", "parentId": None, "path": "103", "pathName": "装备型号", "remark": "武器装备、装备参数、技术指标、装备性能及型号资料。"},
    {"id": 10301, "name": "空中装备", "parentId": 103, "path": "103/10301", "pathName": "装备型号/空中装备", "remark": "飞机、无人机、航空平台及相关空中装备。"},
    {"id": 10302, "name": "水面装备", "parentId": 103, "path": "103/10302", "pathName": "装备型号/水面装备", "remark": "水面舰艇、船舶平台及相关水面装备。"},
    {"id": 10303, "name": "水下装备", "parentId": 103, "path": "103/10303", "pathName": "装备型号/水下装备", "remark": "潜艇、水下无人平台、鱼雷及相关水下装备。"},
    {"id": 104, "name": "作战环境", "parentId": None, "path": "104", "pathName": "作战环境", "remark": "战场环境、地理条件、气象水文、电磁环境、海洋环境等。"},
    {"id": 105, "name": "作战指挥", "parentId": None, "path": "105", "pathName": "作战指挥", "remark": "指挥控制、决策流程、作战计划、战术战法等。"},
    {"id": 10501, "name": "条令条例", "parentId": 105, "path": "105/10501", "pathName": "作战指挥/条令条例", "remark": "发布机构、编号、版本、规范、条令、条例、制度等。"},
    {"id": 10502, "name": "组织机构", "parentId": 105, "path": "105/10502", "pathName": "作战指挥/组织机构", "remark": "机构编制、隶属关系、职责分工、司令部、部门设置、岗位任命等。"},
    {"id": 106, "name": "数据标准", "parentId": None, "path": "106", "pathName": "数据标准", "remark": "GJB、国军标、国家军用标准、技术标准、数据规范和标准化资料。"},
]

ARCHITECTURE_FALLBACK_ID = 1
WEAPONRY_DETAIL_CATEGORY_SUFFIXES = frozenset(
    {
        "基础数据",
        "战技指标",
        "运用数据",
        "效能数据",
    }
)
SOURCE_SCORE_VALUES = {95, 85, 75, 65, 55}
DATA_STANDARD_LEAF_NAMES = frozenset(
    {"建模与仿真", "军用软件", "目标特性", "术语与定义", "通用要求", "元数据"}
)
DATA_STANDARD_FIELD_ALIASES = {
    "militaryName": ("militaryName", "国军标名称", "标准名称"),
    "num": ("num", "编号", "标准编号", "国军标编号", "fileNo", "文件编号"),
    "startTime": ("startTime", "发布时间", "发布日期", "发布日"),
    "implTime": ("implTime", "实施时间", "实施日期", "实施日"),
    "approvalDept": (
        "approvalDept",
        "批准部门",
        "批准单位",
        "批准机关",
        "批准机构",
        "发布部门",
    ),
}


__all__ = (
    "ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE",
    "ANALYSIS_CLASSIFICATION_MODE_TOPK_SINGLE",
    "ANALYSIS_CLASSIFICATION_MODE_LEGACY",
    "ANALYSIS_CLASSIFICATION_MODES",
    "ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY",
    "ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD",
    "ANALYSIS_FILENAME_CONSTRAINT_MODES",
    "ANALYSIS_DATA_STANDARD_MODE_LEGACY",
    "ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD",
    "ANALYSIS_DATA_STANDARD_MODES",
    "ANALYSIS_IDENTITY_RESELECT_MODE_OFF",
    "ANALYSIS_IDENTITY_RESELECT_MODE_SHADOW",
    "ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE",
    "ANALYSIS_IDENTITY_RESELECT_MODES",
    "MAX_ANALYSIS_PROMPT_CHARS",
    "MAX_ANALYSIS_MODEL_CALLS",
    "MAX_ANALYSIS_PHASE_CALLS",
    "MAX_ANALYSIS_PARAMS_PER_REQUEST",
    "MAX_ANALYSIS_REQUEST_BYTES",
    "DEFAULT_COUNTRY_OPTIONS",
    "DEFAULT_FORMAT_OPTIONS",
    "DEFAULT_MATURITY_OPTIONS",
    "DEFAULT_SECURITY_OPTIONS",
    "DEFAULT_ARCHITECTURE_OPTIONS",
    "ARCHITECTURE_FALLBACK_ID",
    "WEAPONRY_DETAIL_CATEGORY_SUFFIXES",
    "SOURCE_SCORE_VALUES",
    "DATA_STANDARD_LEAF_NAMES",
    "DATA_STANDARD_FIELD_ALIASES",
)
