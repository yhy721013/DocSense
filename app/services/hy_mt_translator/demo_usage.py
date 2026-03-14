from hy_mt_translator import HYMTTranslator, DocumentTranslator

# 1. 初始化模型
translator = HYMTTranslator()

# 2. 包装为文档处理器
doc_translator = DocumentTranslator(translator)

# 3. 测试不同文件格式转换为 HTML
test_files = [
    ("./test/20250919凯瑟琳·萨顿被任命为美国防部网络政策负责人.docx", "Word 文件"),
    ("./test/关岛阿普拉海军基地.docx", "Word 文件"),
    ("./test/test_english.txt", "TXT 文件"),
    ("./test/JFS_5701-JFS_-06-Mar-2024.pdf", "PDF 文件"),
    ("./test/测试.pdf", "PDF 文件"),
]

output_folder = "./output"

# 设置翻译段落（文本块）限制（0 表示翻译全文）
translate_all = 100  # 设置为 100 表示只翻译前100个段落，通常1页包括3~5个段落

for file_path, description in test_files:
    import os
    import time

    if os.path.exists(file_path):
        print(f"\n{'=' * 50}")
        print(f"开始处理 {description}: {file_path}")
        print('=' * 50)

        try:
            # 启动线程来监控进度
            import threading
            stop_progress = threading.Event()

            def show_progress():
                while not stop_progress.is_set():
                    progress = doc_translator.get_progress()
                    if progress['status'] == 'completed':
                        # 完成后继续等待新任务，不退出
                        time.sleep(0.5)
                        continue
                    if progress['status'] == 'processing':
                        current = progress['current']
                        total = progress['total']
                        percentage = progress['percentage']
                        bar_length = 30
                        filled_length = int(bar_length * percentage // 100)
                        bar = '█' * filled_length + '-' * (bar_length - filled_length)
                        print(f'\r进度：[{bar}] {percentage}% ({current}/{total})', end='', flush=True)
                    time.sleep(0.5)


            # 启动进度显示线程
            progress_thread = threading.Thread(target=show_progress, daemon=True)
            progress_thread.start()

            result_path = doc_translator.convert_to_html(
                file_path=file_path,
                output_dir=output_folder,
                target_lang="Chinese",
                show_bilingual=True,
                translate_all=translate_all  # 翻译前 N 段或翻译全文
            )

            # 等待当前文件处理完成后再短暂延迟
            time.sleep(0.5)
            stop_progress.set()  # 通知进度线程停止

            print(f"\n✓ {description} 翻译完成！HTML 文件已保存至：{result_path}")

            # 显示最终进度
            final_progress = doc_translator.get_progress()
            if final_progress['status'] == 'completed':
                print(f"完成进度：100% ({final_progress['current']}/{final_progress['total']})")

        except Exception as e:
            print(f"\n✗ {description} 翻译过程中出错：{e}")
    else:
        print(f"⚠ 跳过 {description}：文件不存在 - {file_path}")

print(f"\n{'=' * 50}")
print("所有文件处理完成！")
print('=' * 50)
