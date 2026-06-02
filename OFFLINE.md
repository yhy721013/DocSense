# 从光盘复制文件

robocopy E:\ D:\离线环境备份 /E /Z

# 安装软件包

# 配置Conda环境

1. 将Conda环境（conda_env.zip/tar.gz）解压，放入Conda环境目录（安装目录/miniconda3/envs/）

   ```
   tar -zxvf filename.tar.gz -C D:\target_folder
   ```

2. 配置环境变量，将以下路径添加到 Windows 的系统环境变量Path：

   安装路径\miniconda3

   安装路径\miniconda3\Scripts

   安装路径\miniconda3\Library\bin

   安装路径\miniconda3\condabin

3. 执行命令以获得脚本执行权限：Set-ExecutionPolicy RemoteSigned -Scope CurrentUser

4. 执行conda init命令，重启命令行。

   

# Ollama

将Ollama模型文件放入 C:\Users\\\<用户名>\\\.ollama\models。

# ArgosTranslate

1. 将argos-translate.zip的内容复制到C:\Users\用户名\\.local\share\argos-translate

2. 修改代码：在Conda环境（miniconda3\envs\环境名\Lib\site-packages\argostranslate）中修改sbd.py，在第154行后再加一行：

   ```python
   def lazy_pipeline(self):
       if self.stanza_pipeline is None:
           self.stanza_pipeline = stanza.Pipeline(
               lang=self.stanza_lang_code,
               dir=str(self.pkg.package_path / "stanza"),
               processors="tokenize",
               use_gpu=settings.device == "cuda",
               logging_level="WARNING",
               download_method=None,
           )
   ```


# MinerU

1. 将模型文件拷贝到目标路径，如D:/MinerU_Models。

   默认路径是C:\Users\accl\\.cache\modelscope......

2. 将mineru.json放到C:\Users\<用户名>\mineru.json。

3. 修改mineru.json，将models-dir中的路径分别改为模型文件路径。

# 修改启动脚本中的软件路径