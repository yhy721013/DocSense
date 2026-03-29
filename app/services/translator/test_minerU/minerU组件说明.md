# minerU组件说明
详情见：https://github.com/opendatalab/MinerU.git

## 运行minerU

### 1、安装minerU依赖

    pip install -U "mineru[all]"
    
    pip install albumentations

### 2、下载模型

    python modeel_download.py

完成第2步骤后，脚本会自动生成用户目录下的magic-pdf.json文件，并自动配置默认模型路径。 
可在【用户目录】下找到magic-pdf.json文件。
windows的用户目录为 "C:\Users\用户名", linux用户目录为 "/home/用户名", macOS用户目录为 "/Users/用户名"

形如：

      "models-dir": {
        "pipeline": "C:\\Users\\13774\\.cache\\modelscope\\hub\\models\\OpenDataLab\\PDF-Extract-Kit-1___0",
        "vlm": "C:\\Users\\13774\\.cache\\modelscope\\hub\\models\\OpenDataLab\\MinerU2___5-2509-1___2B"
      },

运行前请先配置magic-pdf.json文件。（可使用默认配置。默认会自动检测cuda进行加速，若要加速ocr过程需安装paddlepaddle-gpu）

###  3、运行测试

    python demo_minerU.py

