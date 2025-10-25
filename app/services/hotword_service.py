"""热词管理服务 - 负责加载、生成和管理FunASR转录热词"""

import os
import yaml
import logging
import re
import jieba
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Set, Any
from collections import Counter
from ..config.config_manager import get_config_value

logger = logging.getLogger(__name__)

GLOBAL_STOPWORDS: Set[str] = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有",
    "看", "好", "自己", "这", "教程", "视频", "内容", "分享", "介绍", "讲解",
    "分析", "展示", "说明", "大家", "我们", "他们", "这些", "那些", "东西",
    "时候", "技术", "可以", "如何", "因为", "如果", "然后", "感觉",
    "为什么", "怎么", "怎么样", "哪些", "多少", "那么", "这么", "还是", "以及"
}


@dataclass
class HotwordCandidate:
    word: str
    score: float = 0.0
    sources: Set[str] = None
    count: int = 0

    def __post_init__(self):
        if self.sources is None:
            self.sources = set()

    def add(self, weight: float, source: str):
        self.score += weight
        if source:
            self.sources.add(source)
        self.count += 1


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
                'min_keyword_length': 2,
                'thresholds': {
                    'curated': {
                        'min_score': 0.2,
                        'strict_score': 0.6
                    },
                    'experiment': {
                        'min_score': 0.1,
                        'strict_score': 0.25
                    }
                }
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
                          tags: Optional[List[str]] = None,
                          channel_name: str = None,
                          platform: str = None,
                          max_hotwords: Optional[int] = None,
                          mode: str = "curated") -> List[Dict[str, Any]]:
        """生成热词候选列表，附带评分和来源信息"""
        try:
            strategy_config = self.config.get('strategy', {})
            enabled_methods = strategy_config.get('enabled_methods', [])
            weights = self.config.get('weights', {})
            thresholds_cfg = strategy_config.get('thresholds', {})
            max_count = max_hotwords or strategy_config.get('max_hotwords', 20)
            min_length = strategy_config.get('min_keyword_length', 2)

            mode = (mode or "curated").lower()
            if mode not in {"curated", "experiment"}:
                mode = "curated"

            candidate_map: Dict[str, HotwordCandidate] = {}

            def add_candidate(word: str, weight: float, source: str):
                word = (word or "").strip()
                if not word:
                    return
                candidate = candidate_map.get(word)
                if not candidate:
                    candidate = HotwordCandidate(word=word)
                    candidate_map[word] = candidate
                candidate.add(weight, source)

            # 1. 分类热词
            if 'category_based' in enabled_methods and self.category_hotwords:
                category_words = self._get_category_based_hotwords(title, tags, channel_name)
                weight = weights.get('category_based', 0.4)
                for word in category_words:
                    add_candidate(word, weight, 'category')
            elif 'category_based' in enabled_methods:
                logger.debug("跳过分类热词生成：无可用的分类文件")

            # 2. 标题关键词
            if 'title_extraction' in enabled_methods and title:
                title_keywords = self._extract_keywords_from_title(title)
                if title_keywords:
                    keyword_counts = Counter(title_keywords)
                    base_weight = weights.get('title_extraction', 0.3)
                    for word, count in keyword_counts.items():
                        add_candidate(word, base_weight * count, 'title')

            # 3. 标签
            if 'tag_based' in enabled_methods and tags:
                tag_keywords = self._get_tag_based_hotwords(tags)
                if tag_keywords:
                    tag_counts = Counter(tag_keywords)
                    base_weight = weights.get('tag_based', 0.2)
                    for word, count in tag_counts.items():
                        add_candidate(word, base_weight * count, 'tag')

            # 4. 机器学习占位（可关闭）
            if 'learned' in enabled_methods:
                learned_words = self._get_learned_hotwords(title, tags, channel_name)
                if learned_words:
                    base_weight = weights.get('learned', 0.1)
                    for word in learned_words:
                        add_candidate(word, base_weight, 'learned')

            if not candidate_map:
                logger.info("自动热词候选为空")
                return []

            thresholds = thresholds_cfg.get(mode, thresholds_cfg.get('curated', {}))
            min_score = float(thresholds.get('min_score', 0.2))
            strict_score = float(thresholds.get('strict_score', max(min_score * 2, min_score)))

            filtered_candidates: List[Dict[str, Any]] = []
            for candidate in candidate_map.values():
                if len(candidate.word) < min_length:
                    continue
                if self._is_stopword(candidate.word):
                    continue
                if not self._is_valid_word(candidate.word):
                    continue

                adjusted_score = self._apply_scoring_adjustments(candidate)
                if adjusted_score < min_score:
                    continue

                filtered_candidates.append({
                    'word': candidate.word,
                    'score': round(adjusted_score, 4),
                    'sources': sorted(candidate.sources),
                    'strict': adjusted_score >= strict_score
                })

            filtered_candidates.sort(key=lambda item: item['score'], reverse=True)
            final_candidates = filtered_candidates[:max_count]

            logger.info(
                "热词候选生成完成(mode=%s): 候选总数=%d, 通过筛选=%d, 前5=%s",
                mode,
                len(candidate_map),
                len(final_candidates),
                [item['word'] for item in final_candidates[:5]]
            )
            return final_candidates

        except Exception as e:
            logger.error(f"生成热词失败: {str(e)}")
            return []

    def _is_stopword(self, word: str) -> bool:
        normalized = (word or "").strip().lower()
        if not normalized:
            return True
        if normalized in GLOBAL_STOPWORDS:
            return True
        # 过滤常见无意义重复字符
        if len(set(normalized)) == 1 and len(normalized) < 4:
            return True
        return False

    def _is_valid_word(self, word: str) -> bool:
        if not word:
            return False
        clean_word = word.strip()
        if len(clean_word) < 2:
            return False
        if clean_word.isdigit():
            return False
        if re.fullmatch(r'[\W_]+', clean_word):
            return False
        if re.search(r'[A-Za-z]', clean_word) and len(clean_word) == 1:
            return False
        return True

    def _apply_scoring_adjustments(self, candidate: HotwordCandidate) -> float:
        score = candidate.score
        word = candidate.word.strip()

        # 多来源加成
        if len(candidate.sources) > 1:
            score += 0.05 * (len(candidate.sources) - 1)

        # 英文或数字混合词略微加权
        if re.search(r'[A-Za-z]', word):
            score += 0.05
        if re.search(r'\d', word):
            score += 0.05

        # 单一来源但出现次数多也加分
        if candidate.count >= 3:
            score += 0.05

        # 过长的词减分
        if len(word) > 12:
            score -= 0.05

        return max(score, 0.0)

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
            
            words = jieba.cut(title)
            keywords: List[str] = []

            for word in words:
                word = word.strip()
                if not word:
                    continue
                if word.lower() in GLOBAL_STOPWORDS:
                    continue
                if len(word) < 2 and not re.search(r'[A-Za-z]', word):
                    continue
                if word.isdigit():
                    continue
                if not re.search(r'[\u4e00-\u9fffA-Za-z0-9]', word):
                    continue
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
            
            logger.debug("学习热词模块暂未启用，返回空列表")
            return []
            
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
