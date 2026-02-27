# 建表SQL语句（1主表+9子表，完全匹配多表设计）
CREATE_TABLE_SQL = {
    # 主表：military_main
    "military_main": """
    CREATE TABLE IF NOT EXISTS military_main (
        id BIGINT NOT NULL AUTO_INCREMENT COMMENT '唯一数据标识，自增',
        data_module VARCHAR(50) NOT NULL COMMENT '数据所属大模块：装备/作战/组织/海洋环境',
        data_type VARCHAR(100) NOT NULL COMMENT '数据具体类型',
        core_theme VARCHAR(200) NOT NULL COMMENT '数据核心主题',
        keyword TEXT COMMENT '相关关键词，英文逗号分隔',
        country VARCHAR(50) COMMENT '所属国家/地区',
        military_service VARCHAR(50) COMMENT '所属军种',
        literature_source VARCHAR(255) COMMENT '文献来源/文件夹路径',
        data_note TEXT COMMENT '数据备注/特殊说明',
        create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据录入时间',
        update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '数据更新时间',
        PRIMARY KEY (id),
        INDEX idx_core_theme (core_theme),
        INDEX idx_data_module (data_module),
        INDEX idx_country (country)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT '军事数据主表';
    """,
    # 装备类-基础数据：equip_basic
    "equip_basic": """
    CREATE TABLE IF NOT EXISTS equip_basic (
        id BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增标识',
        main_id BIGINT NOT NULL COMMENT '关联主表id',
        equip_cn_name VARCHAR(100) COMMENT '装备中文型号',
        equip_en_name VARCHAR(100) COMMENT '装备英文型号',
        equip_class VARCHAR(50) COMMENT '装备分类',
        manufacturer VARCHAR(100) COMMENT '总装厂商',
        serve_status VARCHAR(50) COMMENT '服役状态',
        serve_date DATE COMMENT '正式服役时间',
        first_flight DATE COMMENT '首飞时间',
        serve_quantity INT COMMENT '服役数量（架）',
        cost VARCHAR(50) COMMENT '造价（亿美元@年代）',
        performance_feature TEXT COMMENT '性能特点/核心事实',
        PRIMARY KEY (id),
        INDEX idx_main_id (main_id),
        FOREIGN KEY (main_id) REFERENCES military_main(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT '装备基础数据表';
    """,
    # 装备类-战技指标：equip_tactical
    "equip_tactical": """
    CREATE TABLE IF NOT EXISTS equip_tactical (
        id BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增标识',
        main_id BIGINT NOT NULL COMMENT '关联主表id',
        length DECIMAL(8,2) COMMENT '长度（米）',
        width DECIMAL(8,2) COMMENT '宽度（米）',
        height DECIMAL(8,2) COMMENT '高度（米）',
        wingspan DECIMAL(8,2) COMMENT '翼展（米）',
        wing_aspect_ratio DECIMAL(6,2) COMMENT '机翼纵横比',
        net_wing_area DECIMAL(8,2) COMMENT '净机翼面积（平方米）',
        tail_span DECIMAL(8,2) COMMENT '尾翼翼展（米）',
        empty_weight DECIMAL(8,2) COMMENT '空载重量（吨）',
        max_takeoff_weight DECIMAL(8,2) COMMENT '最大起飞重量（吨）',
        max_load_weight DECIMAL(8,2) COMMENT '最大负载重量（吨）',
        max_wing_load DECIMAL(8,2) COMMENT '最大机翼载荷（kg/m²）',
        crew_num TINYINT COMMENT '最大飞行乘员数量（人）',
        min_runway_length DECIMAL(8,2) COMMENT '最短跑道长度（米）',
        min_maintain_time INT COMMENT '最短维护时间（秒）',
        max_maintain_time INT COMMENT '最长维护时间（秒）',
        climb_rate VARCHAR(50) COMMENT '爬升率',
        service_ceiling DECIMAL(8,2) COMMENT '实用升限（米）',
        max_mach DECIMAL(4,2) COMMENT '最大平飞马赫数',
        max_speed VARCHAR(50) COMMENT '最大飞行速度（含单位）',
        cruising_speed VARCHAR(50) COMMENT '巡航速度（含单位）',
        flight_range DECIMAL(10,2) COMMENT '航程（公里）',
        operation_radius DECIMAL(10,2) COMMENT '作战半径（公里）',
        g_limits DECIMAL(4,2) COMMENT '过载限制',
        endurance VARCHAR(50) COMMENT '续航时间（含单位）',
        PRIMARY KEY (id),
        INDEX idx_main_id (main_id),
        FOREIGN KEY (main_id) REFERENCES military_main(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT '装备战技指标表';
    """,
    # 装备类-运用数据：equip_apply
    "equip_apply": """
    CREATE TABLE IF NOT EXISTS equip_apply (
        id BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增标识',
        main_id BIGINT NOT NULL COMMENT '关联主表id',
        use_manual VARCHAR(255) COMMENT '使用/作战/训练手册名称/路径',
        deploy_unit VARCHAR(255) COMMENT '隶属编制（行政/作战）',
        deploy_time VARCHAR(50) COMMENT '部署时间（单日期/时间段）',
        deploy_place VARCHAR(255) COMMENT '部署地点',
        deploy_task TEXT COMMENT '执行主要任务',
        exercise_name VARCHAR(255) COMMENT '参演演习/演训/作战试验名称',
        exercise_content TEXT COMMENT '演习中的运用情况/试验结果',
        maintain_cycle VARCHAR(100) COMMENT '维修周期/间隔',
        maintain_manual VARCHAR(255) COMMENT '维修相关手册名称/路径',
        accident_time DATETIME COMMENT '事故发生时间',
        accident_place VARCHAR(255) COMMENT '事故地点',
        accident_type VARCHAR(50) COMMENT '事故类型：撞山/坠海/失控/起火等',
        accident_process TEXT COMMENT '事故发生过程',
        accident_result TEXT COMMENT '事故结果/影响/意义',
        PRIMARY KEY (id),
        INDEX idx_main_id (main_id),
        FOREIGN KEY (main_id) REFERENCES military_main(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT '装备运用数据表';
    """,
    # 装备类-效能+模型+目特+声像：equip_eff_model
    "equip_eff_model": """
    CREATE TABLE IF NOT EXISTS equip_eff_model (
        id BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增标识',
        main_id BIGINT NOT NULL COMMENT '关联主表id',
        material_type VARCHAR(100) COMMENT '材料型号/规格',
        material_param TEXT COMMENT '材料参数：屈服强度、泊松比等',
        damage_evaluation TEXT COMMENT '毁伤效能/易损性评估结果',
        simulation_info TEXT COMMENT '数值仿真信息：网格、欧拉模型等',
        model_structure TEXT COMMENT '三维结构/图纸信息：图层、筋、孔等',
        model_format VARCHAR(50) COMMENT '模型格式：3D/CAD/图纸等',
        rcs VARCHAR(100) COMMENT '雷达散射截面积（RCS）',
        optical_cross_section VARCHAR(100) COMMENT '光学截面积',
        ir_signature TEXT COMMENT '红外特征/参数',
        media_type VARCHAR(50) COMMENT '声像类型：照片/视频/图片等',
        media_view VARCHAR(100) COMMENT '拍摄/录制视角',
        media_tag VARCHAR(255) COMMENT '人工打标签内容',
        media_desc TEXT COMMENT '声像内容描述',
        PRIMARY KEY (id),
        INDEX idx_main_id (main_id),
        FOREIGN KEY (main_id) REFERENCES military_main(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT '装备效能模型目特声像表';
    """,
    # 作战类-体系+场景+概念：combat_system_scene
    "combat_system_scene": """
    CREATE TABLE IF NOT EXISTS combat_system_scene (
        id BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增标识',
        main_id BIGINT NOT NULL COMMENT '关联主表id',
        combat_system_type VARCHAR(100) COMMENT '作战体系类型：全域/航母编队/水面编队等',
        combat_scene_type VARCHAR(50) COMMENT '作战场景类型：推演想定/演习演训',
        wargame_content TEXT COMMENT '推演想定内容',
        wargame_debug TINYINT(1) COMMENT '是否需要本地驻场调试：0=否，1=是',
        combat_concept TEXT COMMENT '作战概念：背景/内涵/机理/战术战法',
        exercise_org VARCHAR(255) COMMENT '演习演训组织单位',
        exercise_forces TEXT COMMENT '参演兵力',
        exercise_region VARCHAR(255) COMMENT '演习地域范围',
        exercise_purpose TEXT COMMENT '演习背景/目的',
        exercise_subject VARCHAR(255) COMMENT '演习科目',
        exercise_effect TEXT COMMENT '演习行动/成效',
        PRIMARY KEY (id),
        INDEX idx_main_id (main_id),
        FOREIGN KEY (main_id) REFERENCES military_main(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT '作战体系场景概念表';
    """,
    # 作战类-战史战例：combat_war_case
    "combat_war_case": """
    CREATE TABLE IF NOT EXISTS combat_war_case (
        id BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增标识',
        main_id BIGINT NOT NULL COMMENT '关联主表id',
        war_case_name VARCHAR(255) COMMENT '战例名称',
        war_case_time VARCHAR(50) COMMENT '战例时间（时间段/具体日期）',
        war_place VARCHAR(255) COMMENT '作战地点',
        war_countries VARCHAR(255) COMMENT '参战国/方，英文逗号分隔',
        force_scale TEXT COMMENT '兵力规模',
        war_intention TEXT COMMENT '作战企图',
        war_cause TEXT COMMENT '直接动因',
        war_plan TEXT COMMENT '作战筹划/规划',
        war_operation TEXT COMMENT '作战行动过程',
        war_tactics TEXT COMMENT '战术战法',
        war_result TEXT COMMENT '作战结果',
        war_damage TEXT COMMENT '战损情况（人员/装备）',
        PRIMARY KEY (id),
        INDEX idx_main_id (main_id),
        FOREIGN KEY (main_id) REFERENCES military_main(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT '战史战例表';
    """,
    # 组织类-组织机构：org_structure
    "org_structure": """
    CREATE TABLE IF NOT EXISTS org_structure (
        id BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增标识',
        main_id BIGINT NOT NULL COMMENT '关联主表id',
        org_name VARCHAR(255) COMMENT '机构/司令部名称',
        org_level VARCHAR(50) COMMENT '机构层级：总部/战区/舰队等',
        dept_setup TEXT COMMENT '部门设置/架构',
        job_name VARCHAR(100) COMMENT '岗位名称',
        commander VARCHAR(100) COMMENT '岗位人员/指挥官姓名',
        org_function TEXT COMMENT '机构/岗位职能/职责',
        operation_mechanism TEXT COMMENT '运行机制',
        PRIMARY KEY (id),
        INDEX idx_main_id (main_id),
        FOREIGN KEY (main_id) REFERENCES military_main(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT '组织机构表';
    """,
    # 组织类-条令条例：military_regulation
    "military_regulation": """
    CREATE TABLE IF NOT EXISTS military_regulation (
        id BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增标识',
        main_id BIGINT NOT NULL COMMENT '关联主表id',
        regulation_name VARCHAR(255) COMMENT '条令条例名称/编号',
        regulation_type VARCHAR(50) COMMENT '类型：联合出版物/作战出版物等',
        issuer VARCHAR(255) COMMENT '发布机构',
        issue_date DATE COMMENT '发布日期',
        version VARCHAR(50) COMMENT '版本号',
        apply_scope TEXT COMMENT '适用范围/场景',
        regulation_content TEXT COMMENT '核心内容摘要',
        PRIMARY KEY (id),
        INDEX idx_main_id (main_id),
        FOREIGN KEY (main_id) REFERENCES military_main(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT '条令条例表';
    """,
    # 环境类-海洋环境：ocean_environment
    "ocean_environment": """
    CREATE TABLE IF NOT EXISTS ocean_environment (
        id BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增标识',
        main_id BIGINT NOT NULL COMMENT '关联主表id',
        ocean_region VARCHAR(255) COMMENT '海洋区域',
        ocean_current VARCHAR(100) COMMENT '海流类型/流速',
        wave VARCHAR(50) COMMENT '海浪情况',
        wave_height DECIMAL(6,2) COMMENT '浪高（米）',
        tide VARCHAR(50) COMMENT '潮汐情况',
        sea_temperature DECIMAL(6,2) COMMENT '海温（℃）',
        salinity DECIMAL(6,2) COMMENT '盐度（‰）',
        ocean_note TEXT COMMENT '海洋环境补充说明',
        PRIMARY KEY (id),
        INDEX idx_main_id (main_id),
        FOREIGN KEY (main_id) REFERENCES military_main(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT '海洋环境表';
    """,

    # 军事基地表：military_base
    "military_base": """
    CREATE TABLE IF NOT EXISTS military_base (
    id BIGINT NOT NULL AUTO_INCREMENT COMMENT '自增标识',
    main_id BIGINT NOT NULL COMMENT '关联主表id',
    base_name VARCHAR(255) COMMENT '基地名称',
    location VARCHAR(255) COMMENT '地理位置',
    facility_type VARCHAR(100) COMMENT '设施类型',
    `function` TEXT COMMENT '基地功能/作用',  -- 使用反引号包围保留关键字
    capacity VARCHAR(255) COMMENT '承载能力/规模',
    status VARCHAR(50) COMMENT '状态：现役/退役等',
    PRIMARY KEY (id),
    INDEX idx_main_id (main_id),
    FOREIGN KEY (main_id) REFERENCES military_main(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT '军事基地表';
    """,
}

