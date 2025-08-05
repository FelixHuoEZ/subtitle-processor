"""热词管理服务 - 负责加载、生成和管理FunASR转录热词"""

import os
import yaml
import logging
import re
import jieba
from typing import Dict, List, Optional, Tuple, Set
from collections import Counter
from ..config.config_manager import get_config_value

logger = logging.getLogger(__name__)


class HotwordService:
    """热词管理服务"""
    
    def __init__(self):
        """初始化热词服务"""
        self.config_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'config', 'hotwords')
        self.categories_dir = os.path.join(self.config_dir, 'categories')
        self.config = self._load_hotword_config()
        self.category_hotwords = self._load_category_hotwords()
        
        # 初始化jieba分词（用于关键词提取）
        jieba.initialize()
    
    def _load_hotword_config(self) -> Dict:
        """加载热词配置"""
        try:
            config_file = os.path.join(self.config_dir, 'hotwords_config.yml')
            if os.path.exists(config_file):
                with open(config_file, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f) or {}
                    logger.info(f"成功加载热词配置: {config_file}")
                    return config.get('hotwords', {})
            else:
                logger.info(f"热词配置文件不存在，使用默认配置: {config_file}")
                return self._get_default_config()
        except Exception as e:
            logger.error(f"加载热词配置失败，使用默认配置: {str(e)}")
            return self._get_default_config()
    
    def _get_default_config(self) -> Dict:
        """获取默认热词配置"""
        return {
            'strategy': {
                'enabled_methods': ['title_extraction', 'tag_based'],
                'max_hotwords': 15,
                'min_keyword_length': 2
            },
            'weights': {
                'category_based': 0.4,
                'title_extraction': 0.4,
                'tag_based': 0.2,
                'learned': 0.0
            },
            'category_mapping': {
                'keywords': {
                    'general': ['视频', '内容', '分享', '介绍', '教程', '讲解']
                },
                'channels': {
                    'general': ['频道', '博主', '主播']
                }
            }
        }
    
    def _load_category_hotwords(self) -> Dict[str, Dict]:
        """加载分类热词"""
        category_hotwords = {}
        
        try:
            if not os.path.exists(self.categories_dir):
                logger.info(f"热词分类目录不存在，跳过分类热词加载: {self.categories_dir}")
                return {}
            
            # 遍历分类文件
            for filename in os.listdir(self.categories_dir):
                if filename.endswith('.yml') or filename.endswith('.yaml'):
                    category_name = os.path.splitext(filename)[0]
                    file_path = os.path.join(self.categories_dir, filename)
                    
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            category_data = yaml.safe_load(f) or {}
                            category_hotwords[category_name] = category_data
                            logger.debug(f"加载分类热词: {category_name}")
                    except Exception as e:
                        logger.error(f"加载分类热词文件失败 {file_path}: {str(e)}")
            
            if category_hotwords:
                logger.info(f"成功加载 {len(category_hotwords)} 个热词分类")
            else:
                logger.info("未找到热词分类文件，将仅使用标题提取和标签生成热词")
            return category_hotwords
            
        except Exception as e:
            logger.error(f"加载分类热词失败: {str(e)}")
            return {}
    
    def generate_hotwords(self, 
                         title: str = None, 
                         tags: List[str] = None, 
                         channel_name: str = None, 
                         platform: str = None,
                         max_hotwords: int = None) -> List[str]:
        """生成热词列表
        
        Args:
            title: 视频标题
            tags: 用户标签
            channel_name: 频道名称
            platform: 平台名称
            max_hotwords: 最大热词数量
            
        Returns:
            生成的热词列表
        """
        try:
            # 获取配置
            strategy_config = self.config.get('strategy', {})
            enabled_methods = strategy_config.get('enabled_methods', [])
            weights = self.config.get('weights', {})
            max_count = max_hotwords or strategy_config.get('max_hotwords', 20)
            min_length = strategy_config.get('min_keyword_length', 2)
            
            # 收集候选热词
            candidate_hotwords = {}
            
            # 1. 基于分类的热词（仅在有分类文件时启用）
            if 'category_based' in enabled_methods and self.category_hotwords:
                category_words = self._get_category_based_hotwords(title, tags, channel_name)
                weight = weights.get('category_based', 0.4)
                for word in category_words:
                    candidate_hotwords[word] = candidate_hotwords.get(word, 0) + weight
            elif 'category_based' in enabled_methods:
                logger.debug("跳过分类热词生成：无可用的分类文件")
            
            # 2. 从标题提取关键词
            if 'title_extraction' in enabled_methods and title:
                title_words = self._extract_keywords_from_title(title)
                weight = weights.get('title_extraction', 0.3)
                for word in title_words:
                    if len(word) >= min_length:
                        candidate_hotwords[word] = candidate_hotwords.get(word, 0) + weight
            
            # 3. 基于用户标签的热词
            if 'tag_based' in enabled_methods and tags:
                tag_words = self._get_tag_based_hotwords(tags)
                weight = weights.get('tag_based', 0.2)
                for word in tag_words:
                    candidate_hotwords[word] = candidate_hotwords.get(word, 0) + weight
            
            # 4. 学习热词（预留扩展）
            if 'learned' in enabled_methods:
                learned_words = self._get_learned_hotwords(title, tags, channel_name)
                weight = weights.get('learned', 0.1)
                for word in learned_words:
                    candidate_hotwords[word] = candidate_hotwords.get(word, 0) + weight
            
            # 按权重排序并取前N个
            sorted_hotwords = sorted(candidate_hotwords.items(), key=lambda x: x[1], reverse=True)
            final_hotwords = [word for word, score in sorted_hotwords[:max_count]]
            
            logger.info(f"生成热词完成: {len(final_hotwords)} 个词汇")
            logger.debug(f"热词列表: {final_hotwords}")
            
            return final_hotwords
            
        except Exception as e:
            logger.error(f"生成热词失败: {str(e)}")
            return []
    
    def _get_category_based_hotwords(self, title: str, tags: List[str], channel_name: str) -> List[str]:
        """基于分类映射获取热词"""
        try:
            mapping_config = self.config.get('category_mapping', {})
            keywords_mapping = mapping_config.get('keywords', {})
            channels_mapping = mapping_config.get('channels', {})
            
            matched_categories = set()
            
            # 基于关键词匹配分类
            search_text = ' '.join(filter(None, [title, channel_name])).lower()
            for category, keywords in keywords_mapping.items():
                for keyword in keywords:
                    if keyword.lower() in search_text:
                        matched_categories.add(category)
                        logger.debug(f"通过关键词'{keyword}'匹配到分类: {category}")
            
            # 基于频道名匹配分类
            if channel_name:
                for category, channel_keywords in channels_mapping.items():
                    for channel_keyword in channel_keywords:
                        if channel_keyword.lower() in channel_name.lower():
                            matched_categories.add(category)
                            logger.debug(f"通过频道名'{channel_keyword}'匹配到分类: {category}")
            
            # 基于用户标签匹配分类
            if tags:
                for tag in tags:
                    tag_lower = tag.lower()
                    for category, keywords in keywords_mapping.items():
                        for keyword in keywords:
                            if keyword.lower() in tag_lower or tag_lower in keyword.lower():
                                matched_categories.add(category)
                                logger.debug(f"通过标签'{tag}'匹配到分类: {category}")
            
            # 收集匹配分类的热词
            category_hotwords = []
            for category in matched_categories:
                if category in self.category_hotwords:
                    category_data = self.category_hotwords[category].get(category, {})
                    category_weights = self.category_hotwords[category].get('weights', {})
                    
                    # 从各个子分类收集热词
                    for subcategory, words in category_data.items():
                        if isinstance(words, list):
                            subcategory_weight = category_weights.get(subcategory, 1.0)
                            # 根据权重决定取词数量
                            word_count = max(1, int(len(words) * subcategory_weight))
                            category_hotwords.extend(words[:word_count])
            
            logger.debug(f"分类热词匹配结果: {len(category_hotwords)} 个词汇")
            return category_hotwords
            
        except Exception as e:
            logger.error(f"获取分类热词失败: {str(e)}")
            return []
    
    def _extract_keywords_from_title(self, title: str) -> List[str]:
        """从标题提取关键词"""
        try:
            if not title:
                return []
            
            # 使用jieba分词
            words = jieba.cut(title)
            
            # 过滤关键词
            keywords = []
            stopwords = {'的', '了', '在', '是', '我', '有', '和', '就', '不', '人', '都', '一', '一个', '上', '也', '很', '到', '说', '要', '去', '你', '会', '着', '没有', '看', '好', '自己', '这'}
            
            for word in words:
                word = word.strip()
                # 过滤条件：长度>=2，不是停用词，不是纯数字/标点
                if (len(word) >= 2 and 
                    word not in stopwords and 
                    not word.isdigit() and 
                    re.search(r'[\u4e00-\u9fff\w]', word)):  # 包含中文或字母
                    keywords.append(word)
            
            logger.debug(f"从标题提取关键词: {keywords}")
            return keywords
            
        except Exception as e:
            logger.error(f"提取标题关键词失败: {str(e)}")
            return []
    
    def _get_tag_based_hotwords(self, tags: List[str]) -> List[str]:
        """基于用户标签获取相关热词"""
        try:
            if not tags:
                return []
            
            tag_hotwords = []
            
            # 直接使用用户标签作为热词
            for tag in tags:
                tag = tag.strip()
                if len(tag) >= 2:
                    tag_hotwords.append(tag)
            
            # 从标签中查找相关的分类热词
            for tag in tags:
                tag_lower = tag.lower()
                for category_name, category_data in self.category_hotwords.items():
                    main_category = category_data.get(category_name, {})
                    for subcategory, words in main_category.items():
                        if isinstance(words, list):
                            # 检查标签是否与这个子分类相关
                            if (subcategory.lower() in tag_lower or 
                                tag_lower in subcategory.lower() or
                                any(tag_lower in word.lower() or word.lower() in tag_lower for word in words[:3])):
                                # 取该子分类的前几个热词
                                tag_hotwords.extend(words[:3])
            
            logger.debug(f"基于标签生成热词: {tag_hotwords}")
            return tag_hotwords
            
        except Exception as e:
            logger.error(f"获取标签热词失败: {str(e)}")
            return []
    
    def _get_learned_hotwords(self, title: str, tags: List[str], channel_name: str) -> List[str]:
        """获取机器学习生成的热词（预留扩展）"""
        try:
            # 预留机器学习热词生成接口
            # 可以在此实现：
            # 1. 基于历史转录结果学习常见错误
            # 2. 基于用户行为学习个性化热词  
            # 3. 基于相似视频学习相关热词
            
            learned_hotwords = []
            
            # 目前返回一些通用的热词作为占位
            common_words = ["视频", "内容", "分享", "介绍", "教程", "讲解", "分析"]
            learned_hotwords.extend(common_words[:2])
            
            logger.debug(f"学习热词: {learned_hotwords}")
            return learned_hotwords
            
        except Exception as e:
            logger.error(f"获取学习热词失败: {str(e)}")
            return []
    
    def get_default_hotwords(self) -> List[str]:
        """获取默认热词列表"""
        try:
            # 从配置文件获取默认热词
            default_hotwords = get_config_value('transcription.hotwords', [])
            
            # 如果配置中没有，使用基础默认热词
            if not default_hotwords:
                default_hotwords = [
                    "视频", "内容", "分享", "介绍", "教程", "讲解", 
                    "分析", "演示", "展示", "说明"
                ]
            
            logger.debug(f"默认热词: {default_hotwords}")
            return default_hotwords
            
        except Exception as e:
            logger.error(f"获取默认热词失败，使用空列表: {str(e)}")
            return []
    
    def update_hotword_config(self, config_updates: Dict) -> bool:
        """更新热词配置"""
        try:
            # 确保配置目录存在
            os.makedirs(self.config_dir, exist_ok=True)
            config_file = os.path.join(self.config_dir, 'hotwords_config.yml')
            
            # 合并配置
            updated_config = {'hotwords': self.config.copy()}
            if 'hotwords' in config_updates:
                updated_config['hotwords'].update(config_updates['hotwords'])
            
            # 保存配置
            with open(config_file, 'w', encoding='utf-8') as f:
                yaml.dump(updated_config, f, default_flow_style=False, allow_unicode=True, indent=2)
            
            # 重新加载配置
            self.config = self._load_hotword_config()
            
            logger.info("热词配置更新成功")
            return True
            
        except Exception as e:
            logger.error(f"更新热词配置失败: {str(e)}")
            return False
    
    def add_custom_hotwords(self, category: str, subcategory: str, words: List[str]) -> bool:
        """添加自定义热词到指定分类"""
        try:
            # 确保分类目录存在
            os.makedirs(self.categories_dir, exist_ok=True)
            category_file = os.path.join(self.categories_dir, f"{category}.yml")
            
            # 如果分类文件不存在，创建新文件
            if not os.path.exists(category_file):
                category_data = {category: {subcategory: words}, 'weights': {subcategory: 1.0}}
            else:
                with open(category_file, 'r', encoding='utf-8') as f:
                    category_data = yaml.safe_load(f) or {}
                
                # 确保分类结构存在
                if category not in category_data:
                    category_data[category] = {}
                if 'weights' not in category_data:
                    category_data['weights'] = {}
                
                # 添加热词
                if subcategory in category_data[category]:
                    existing_words = set(category_data[category][subcategory])
                    new_words = [w for w in words if w not in existing_words]
                    category_data[category][subcategory].extend(new_words)
                else:
                    category_data[category][subcategory] = words
                    category_data['weights'][subcategory] = 1.0
            
            # 保存文件
            with open(category_file, 'w', encoding='utf-8') as f:
                yaml.dump(category_data, f, default_flow_style=False, allow_unicode=True, indent=2)
            
            # 重新加载分类热词
            self.category_hotwords = self._load_category_hotwords()
            
            logger.info(f"成功添加自定义热词到 {category}.{subcategory}: {words}")
            return True
            
        except Exception as e:
            logger.error(f"添加自定义热词失败: {str(e)}")
            return False
    
    def analyze_transcription_errors(self, original_text: str, corrected_text: str) -> List[str]:
        """分析转录错误，提取可能的热词（预留扩展）"""
        try:
            # 预留功能：分析转录错误，自动学习热词
            # 可以实现：
            # 1. 对比原文和修正文本
            # 2. 识别常见的音近字错误
            # 3. 提取专业术语错误
            # 4. 生成针对性热词
            
            potential_hotwords = []
            
            # 简单实现：提取修正文本中的关键词
            if corrected_text:
                keywords = self._extract_keywords_from_title(corrected_text)
                potential_hotwords.extend(keywords[:5])
            
            return potential_hotwords
            
        except Exception as e:
            logger.error(f"分析转录错误失败: {str(e)}")
            return []