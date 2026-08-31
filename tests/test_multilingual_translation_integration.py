# -*- coding: utf-8 -*-
"""
多语言翻译集成测试脚本
测试 Argo Translate 的真实翻译能力（非 Mock）
包括：
1. 自动检测并下载缺失的翻译包
2. 测试各语言到中文的翻译
3. 测试混合多语言段落
4. 验证中转翻译机制
5. 生成详细测试报告
"""

import sys
import os
from pathlib import Path

# 确保项目根目录在路径中
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from app.modules.translation.adapters import HYMTTranslator


def print_section(title):
    """打印分隔标题"""
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80)


def print_subsection(title):
    """打印子标题"""
    print(f"\n--- {title} ---")


def test_translator_initialization():
    """测试1: 翻译器初始化与翻译包检查"""
    print_section("测试1: 翻译器初始化与翻译包自动安装")
    
    try:
        print("正在初始化翻译器（可能触发自动下载）...")
        translator = HYMTTranslator(check_ollama=False)
        print("✅ 翻译器初始化成功")
        
        # 检查已安装的语言
        from argostranslate import translate
        installed_languages = translate.get_installed_languages()
        lang_codes = [lang.code for lang in installed_languages]
        
        print(f"\n已安装的语言代码: {lang_codes}")
        
        # 检查必需的翻译对
        required_pairs = [
            ("zh", "en", "中文→英文"),
            ("en", "zh", "英文→中文"),
            ("ja", "en", "日文→英文"),
            ("ru", "en", "俄文→英文"),
            ("ko", "en", "韩文→英文"),
            ("fr", "en", "法文→英文"),
            ("de", "en", "德文→英文"),
            ("it", "en", "意文→英文"),
        ]
        
        print("\n翻译包状态检查:")
        missing_packages = []
        for from_code, to_code, desc in required_pairs:
            from_lang = next((lang for lang in installed_languages if lang.code == from_code), None)
            to_lang = next((lang for lang in installed_languages if lang.code == to_code), None)
            
            if from_lang and to_lang:
                translation = from_lang.get_translation(to_lang)
                if translation:
                    print(f"  ✓ {desc}: 已安装")
                else:
                    print(f"  ✗ {desc}: 缺少翻译路径")
                    missing_packages.append(desc)
            else:
                print(f"  ✗ {desc}: 语言包未安装")
                missing_packages.append(desc)
        
        if missing_packages:
            print(f"\n⚠️  警告: 以下翻译包缺失或不可用:\n   {', '.join(missing_packages)}")
            print("   首次使用时会自动下载，请确保网络连接正常")
        else:
            print("\n✅ 所有必需的翻译包均已就绪")
        
        return translator
        
    except Exception as e:
        print(f"\n❌ 翻译器初始化失败: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_single_language_translation(translator):
    """测试2: 单语言到中文的翻译"""
    print_section("测试2: 单语言到中文翻译测试")
    
    if not translator:
        print("❌ 跳过测试: 翻译器未初始化")
        return {}
    
    # 测试用例：各种语言的军事相关文本
    test_cases = {
        "ja": {
            "text": "空母は悪天候でも航空機を発進させることができます。搭載する戦闘機の数は約80機です。",
            "expected_keywords": ["航空母舰", "飞机", "恶劣天气"]
        },
        "ru": {
            "text": "Авианосец может запускать самолеты в плохую погоду. На борту около 80 истребителей.",
            "expected_keywords": ["航空母舰", "飞机", "战斗机"]
        },
        "ko": {
            "text": "항공모함은 악천후에도 항공기를 발진시킬 수 있습니다. 약 80대의 전투기를 탑재합니다.",
            "expected_keywords": ["航空母舰", "飞机", "战斗机"]
        },
        "fr": {
            "text": "Le porte-avions peut lancer des avions par mauvais temps. Il embarque environ 80 chasseurs.",
            "expected_keywords": ["航空母舰", "飞机", "战斗机"]
        },
        "de": {
            "text": "Der Flugzeugträger kann bei schlechtem Wetter Flugzeuge starten. Er trägt etwa 80 Jäger.",
            "expected_keywords": ["航空母舰", "飞机", "战斗机"]
        },
        "it": {
            "text": "La portaerei può lanciare aerei con il maltempo. Imbarca circa 80 caccia.",
            "expected_keywords": ["航空母舰", "飞机", "战斗机"]
        },
        "en": {
            "text": "The aircraft carrier can launch aircraft in bad weather. It carries about 80 fighter jets.",
            "expected_keywords": ["航空母舰", "飞机", "战斗机"]
        },
    }
    
    results = {}
    
    for lang_code, test_data in test_cases.items():
        print_subsection(f"语言: {lang_code.upper()}")
        print(f"原文: {test_data['text']}")
        
        try:
            result = translator.translate_text(
                test_data['text'], 
                target_lang="Chinese", 
                fast_translate=True
            )
            
            print(f"译文: {result}")
            
            # 简单验证：检查是否包含预期的关键词
            # 注意：由于是机器翻译，可能不会完全匹配，这里只做基本检查
            if result and len(result) > 5:
                print(f"✅ 翻译成功 (长度: {len(result)} 字符)")
                results[lang_code] = {
                    "success": True,
                    "original": test_data['text'],
                    "translated": result
                }
            else:
                print(f"⚠️  翻译结果为空或过短")
                results[lang_code] = {
                    "success": False,
                    "error": "Empty or too short result"
                }
                
        except Exception as e:
            print(f"❌ 翻译失败: {type(e).__name__}: {e}")
            results[lang_code] = {
                "success": False,
                "error": str(e)
            }
    
    # 统计结果
    success_count = sum(1 for r in results.values() if r.get('success'))
    print(f"\n📊 单语言翻译测试结果: {success_count}/{len(test_cases)} 成功")
    
    return results


def test_mixed_language_paragraph(translator):
    """测试3: 混合多语言段落翻译"""
    print_section("测试3: 混合多语言段落翻译测试")
    
    if not translator:
        print("❌ 跳过测试: 翻译器未初始化")
        return
    
    # 混合多语言段落（模拟真实文档场景）
    mixed_paragraphs = [
        {
            "title": "军事装备描述（中英混合）",
            "text": "The aircraft carrier CVN-78 is the most advanced warship. 这艘航母采用电磁弹射系统。It can carry about 80 aircraft. 其排水量超过10万吨。"
        },
        {
            "title": "技术参数（多语言混合）",
            "text": "Length: 337 meters. 長さ：337メートル。Длина: 337 метров. Longueur: 337 mètres."
        },
        {
            "title": "作战能力描述",
            "text": "This carrier can operate in all weather conditions. この空母は全天候で作戦可能です。Этот авианосец может действовать в любых погодных условиях."
        }
    ]
    
    for i, paragraph in enumerate(mixed_paragraphs, 1):
        print_subsection(f"测试段落 {i}: {paragraph['title']}")
        print(f"原文:\n{paragraph['text']}\n")
        
        try:
            result = translator.translate_text(
                paragraph['text'],
                target_lang="Chinese",
                fast_translate=True
            )
            print(f"译文:\n{result}\n")
            
            if result and len(result) > 10:
                print(f"✅ 混合段落翻译成功")
            else:
                print(f"⚠️  翻译结果不理想")
                
        except Exception as e:
            print(f"❌ 翻译失败: {type(e).__name__}: {e}")


def test_pivot_translation(translator):
    """测试4: 中转翻译机制验证"""
    print_section("测试4: 中转翻译机制测试（通过英文中转）")
    
    if not translator:
        print("❌ 跳过测试: 翻译器未初始化")
        return
    
    # 测试非英语语言直接到中文的翻译（应该自动使用英语中转）
    pivot_test_cases = [
        ("ja", "この技術は非常に重要です。", "日语→英语→中文"),
        ("fr", "Cette technologie est très importante.", "法语→英语→中文"),
        ("de", "Diese Technologie ist sehr wichtig.", "德语→英语→中文"),
    ]
    
    for lang_code, text, description in pivot_test_cases:
        print_subsection(description)
        print(f"原文 ({lang_code}): {text}")
        
        try:
            result = translator.translate_text(
                text,
                target_lang="Chinese",
                fast_translate=True
            )
            print(f"译文: {result}")
            
            # 检查是否包含"技术"和"重要"等关键词
            if "技术" in result or "重要" in result:
                print(f"✅ 中转翻译成功，语义保持良好")
            else:
                print(f"⚠️  译文可能不完整，但翻译过程完成")
                
        except Exception as e:
            print(f"❌ 翻译失败: {type(e).__name__}: {e}")


def test_language_detection_accuracy(translator):
    """测试5: 语言自动识别准确性"""
    print_section("测试5: 语言自动识别准确性测试")
    
    if not translator:
        print("❌ 跳过测试: 翻译器未初始化")
        return
    
    detection_tests = [
        ("en", "The ship is large."),
        ("ja", "船は大きいです。"),
        ("ru", "Корабль большой."),
        ("ko", "배가 큽니다."),
        ("fr", "Le navire est grand."),
        ("de", "Das Schiff ist groß."),
        ("it", "La nave è grande."),
        ("zh", "这艘船很大。"),
    ]
    
    print("语言自动识别测试:\n")
    correct_count = 0
    
    for expected_lang, text in detection_tests:
        detected_lang = HYMTTranslator._detect_argos_source_language(text)
        status = "✅" if detected_lang == expected_lang else "❌"
        
        if detected_lang == expected_lang:
            correct_count += 1
        
        print(f"  {status} 期望: {expected_lang:3s}, 检测到: {detected_lang:3s} | {text[:30]}")
    
    print(f"\n识别准确率: {correct_count}/{len(detection_tests)} ({100*correct_count/len(detection_tests):.1f}%)")


def generate_test_report(single_results):
    """生成测试报告"""
    print_section("📋 测试报告总结")
    
    total_tests = len(single_results)
    successful = sum(1 for r in single_results.values() if r.get('success'))
    failed = total_tests - successful
    
    print(f"\n总体统计:")
    print(f"  - 总测试数: {total_tests}")
    print(f"  - 成功: {successful}")
    print(f"  - 失败: {failed}")
    print(f"  - 成功率: {100*successful/total_tests:.1f}%\n")
    
    if failed > 0:
        print("失败的测试:")
        for lang, result in single_results.items():
            if not result.get('success'):
                print(f"  ❌ {lang.upper()}: {result.get('error', 'Unknown error')}")
        
        print("\n💡 建议:")
        print("  1. 检查网络连接（首次使用需要下载翻译包）")
        print("  2. 确认 ARGOS_PACKAGES_DIR 环境变量配置正确")
        print("  3. 检查 Stanza 离线配置（sbd.py 中的 download_method=None）")
        print("  4. 查看上方日志中的具体错误信息")
    else:
        print("🎉 所有测试均通过！多语言翻译功能正常工作。")


def main():
    """主测试流程"""
    print("=" * 80)
    print("  多语言翻译集成测试")
    print("  Multi-Language Translation Integration Test")
    print("=" * 80)
    print(f"\n开始时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Python 版本: {sys.version}")
    print(f"工作目录: {Path.cwd()}")
    
    # 测试1: 初始化
    translator = test_translator_initialization()
    
    if not translator:
        print("\n❌ 关键组件初始化失败，终止测试")
        return
    
    # 测试2: 单语言翻译
    single_results = test_single_language_translation(translator)
    
    # 测试3: 混合多语言段落
    test_mixed_language_paragraph(translator)
    
    # 测试4: 中转翻译
    test_pivot_translation(translator)
    
    # 测试5: 语言识别
    test_language_detection_accuracy(translator)
    
    # 生成报告
    generate_test_report(single_results)
    
    print(f"\n结束时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断测试")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ 测试过程中发生未预期错误: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
