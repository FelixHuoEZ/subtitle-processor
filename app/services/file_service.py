"""File management service for the subtitle processing application."""

import errno
import os
import json
import logging
import tempfile
import time
from ..config.config_manager import get_config_value
from ..utils.file_utils import detect_file_encoding, sanitize_filename

logger = logging.getLogger(__name__)


class FileService:
    """文件管理服务"""
    
    def __init__(self, upload_folder=None, output_folder=None):
        """初始化文件服务
        
        Args:
            upload_folder: 上传文件目录
            output_folder: 输出文件目录
        """
        self.upload_folder = upload_folder or get_config_value('app.upload_folder', '/app/uploads')
        self.output_folder = output_folder or get_config_value('app.output_folder', '/app/outputs')
        self.files_info_path = os.path.join(self.upload_folder, 'files_info.json')
        
        # 确保目录存在
        self._ensure_directories()
        self._ensure_files_info_exists()
    
    def _ensure_directories(self):
        """确保必要的目录存在"""
        try:
            os.makedirs(self.upload_folder, exist_ok=True)
            os.makedirs(self.output_folder, exist_ok=True)
            logger.debug(f"确保目录存在: {self.upload_folder}, {self.output_folder}")
        except Exception as e:
            logger.error(f"创建目录失败: {str(e)}")
            raise
    
    def _ensure_files_info_exists(self):
        """确保files_info.json文件存在"""
        try:
            if not os.path.exists(self.files_info_path):
                self._atomic_write({}, ensure_dir=True)
                logger.info(f"创建文件信息存储文件: {self.files_info_path}")
        except Exception as e:
            logger.error(f"创建文件信息存储文件失败: {str(e)}")
    
    def load_files_info(self):
        """加载文件信息"""
        attempts = 3
        for attempt in range(attempts):
            try:
                with open(self.files_info_path, 'r', encoding='utf-8') as f:
                    files_info = json.load(f)
                if isinstance(files_info, list):
                    files_info = self._migrate_files_info()
                return files_info
            except OSError as e:
                if e.errno == errno.EDEADLK and attempt < attempts - 1:
                    logger.warning(f"加载文件信息时遇到文件锁冲突，重试({attempt + 1}/{attempts})")
                    time.sleep(0.05 * (attempt + 1))
                    continue
                logger.error(f"加载文件信息时出错: {str(e)}")
                return {}
            except Exception as e:
                logger.error(f"加载文件信息时出错: {str(e)}")
                return {}
        return {}
    
    def save_files_info(self, files_info):
        """保存文件信息"""
        try:
            self._atomic_write(files_info)
            logger.debug("文件信息已保存")
        except Exception as e:
            logger.error(f"保存文件信息时出错: {str(e)}")
    
    def _atomic_write(self, data, ensure_dir=False):
        """原子写入JSON，避免部分写入造成的锁冲突"""
        directory = os.path.dirname(self.files_info_path)
        if ensure_dir:
            os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=directory, prefix='files_info_', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as tmp_file:
                json.dump(data, tmp_file, ensure_ascii=False, indent=2)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(temp_path, self.files_info_path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
    
    def _migrate_files_info(self):
        """将文件信息从列表格式迁移到字典格式"""
        try:
            with open(self.files_info_path, 'r', encoding='utf-8') as f:
                old_files_info = json.load(f)
                
            if isinstance(old_files_info, list):
                new_files_info = {}
                for file_info in old_files_info:
                    if 'id' in file_info:
                        new_files_info[file_info['id']] = file_info
                
                with open(self.files_info_path, 'w', encoding='utf-8') as f:
                    json.dump(new_files_info, f, ensure_ascii=False, indent=2)
                    
                logger.info("成功将文件信息从列表格式迁移到字典格式")
                return new_files_info
        except Exception as e:
            logger.error(f"迁移文件信息时出错: {str(e)}")
            return {}
    
    def add_file_info(self, file_id, file_info):
        """添加文件信息
        
        Args:
            file_id: 文件ID
            file_info: 文件信息字典
        """
        try:
            files_info = self.load_files_info()
            files_info[file_id] = file_info
            self.save_files_info(files_info)
            logger.debug(f"添加文件信息: {file_id}")
        except Exception as e:
            logger.error(f"添加文件信息失败: {str(e)}")
    
    def get_file_info(self, file_id):
        """获取文件信息
        
        Args:
            file_id: 文件ID
            
        Returns:
            dict: 文件信息，如果不存在返回None
        """
        try:
            files_info = self.load_files_info()
            return files_info.get(file_id)
        except Exception as e:
            logger.error(f"获取文件信息失败: {str(e)}")
            return None
    
    def update_file_info(self, file_id, updates):
        """更新文件信息
        
        Args:
            file_id: 文件ID
            updates: 更新的字段字典
        """
        try:
            files_info = self.load_files_info()
            if file_id in files_info:
                files_info[file_id].update(updates)
                self.save_files_info(files_info)
                logger.debug(f"更新文件信息: {file_id}")
            else:
                logger.warning(f"尝试更新不存在的文件信息: {file_id}")
        except Exception as e:
            logger.error(f"更新文件信息失败: {str(e)}")
    
    def delete_file_info(self, file_id):
        """删除文件信息
        
        Args:
            file_id: 文件ID
        """
        try:
            files_info = self.load_files_info()
            if file_id in files_info:
                del files_info[file_id]
                self.save_files_info(files_info)
                logger.debug(f"删除文件信息: {file_id}")
            else:
                logger.warning(f"尝试删除不存在的文件信息: {file_id}")
        except Exception as e:
            logger.error(f"删除文件信息失败: {str(e)}")
    
    def list_files(self):
        """列出所有文件信息
        
        Returns:
            dict: 所有文件信息
        """
        return self.load_files_info()
    
    def save_file(self, file_content, filename, folder=None):
        """保存文件内容到指定目录
        
        Args:
            file_content: 文件内容
            filename: 文件名
            folder: 保存目录，默认为output_folder
            
        Returns:
            str: 保存的文件路径
        """
        try:
            # 清理文件名
            clean_filename = sanitize_filename(filename)
            
            # 确定保存目录
            save_folder = folder or self.output_folder
            os.makedirs(save_folder, exist_ok=True)
            
            # 构建文件路径
            file_path = os.path.join(save_folder, clean_filename)
            
            # 保存文件
            if isinstance(file_content, str):
                # 文本内容
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(file_content)
            else:
                # 二进制内容
                with open(file_path, 'wb') as f:
                    f.write(file_content)
            
            logger.info(f"文件已保存: {file_path}")
            return file_path
        except Exception as e:
            logger.error(f"保存文件失败: {str(e)}")
            raise
    
    def read_file(self, file_path, encoding=None):
        """读取文件内容
        
        Args:
            file_path: 文件路径
            encoding: 文件编码，None时自动检测
            
        Returns:
            str: 文件内容
        """
        try:
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"文件不存在: {file_path}")
            
            if encoding is None:
                # 自动检测编码
                with open(file_path, 'rb') as f:
                    raw_content = f.read()
                encoding = detect_file_encoding(raw_content)
            
            with open(file_path, 'r', encoding=encoding) as f:
                content = f.read()
            
            logger.debug(f"读取文件成功: {file_path}, 编码: {encoding}")
            return content
        except Exception as e:
            logger.error(f"读取文件失败: {str(e)}")
            raise
    
    def delete_file(self, file_path):
        """删除文件
        
        Args:
            file_path: 文件路径
        """
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"文件已删除: {file_path}")
            else:
                logger.warning(f"尝试删除不存在的文件: {file_path}")
        except Exception as e:
            logger.error(f"删除文件失败: {str(e)}")
    
    def get_file_size(self, file_path):
        """获取文件大小
        
        Args:
            file_path: 文件路径
            
        Returns:
            int: 文件大小（字节）
        """
        try:
            return os.path.getsize(file_path) if os.path.exists(file_path) else 0
        except Exception as e:
            logger.error(f"获取文件大小失败: {str(e)}")
            return 0
    
    def file_exists(self, file_path):
        """检查文件是否存在
        
        Args:
            file_path: 文件路径
            
        Returns:
            bool: 文件是否存在
        """
        return os.path.exists(file_path)
