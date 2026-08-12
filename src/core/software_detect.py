#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""软件识别 - 13层识别管线 + 位置感知兜底
识别顺序（2026-07-21 精简：合并冗余层）：
  1. COMBO_MAP组合匹配（学习缓存，手动精调的高质量映射）
  2. winget内置软件数据库匹配（19421 包，返回 name+desc+type 三元组）
     - winget DB 不含版本号，软件升级不影响匹配结果
     - 国内软件和国际软件处理方式完全一致
  3. KNOWN_SOFTWARE_DIRS关键字匹配（学习缓存，笼统说明触发子目录精确检测）
  4. -updater/-update 后缀目录识别
  5. 通用词匹配（updater/update/cache+具体软件名，无具体软件名不返回）
  6. 反向域名包名识别（匹配具体软件名才返回）
  7. 注册表卸载项 + WMI 查询（合并层：两者都是查已安装软件）
  8. 特征文件 + 关联目录 + 动态索引（合并层：三种都是通过已知软件反推）
  9. App Paths + 开始菜单 lnk（合并层：都是 exe 名匹配）
  10. PE版本信息（合并层：根目录→一级子目录→深层递归）
  11. lnk快捷方式 + 文件标识
  12. 厂商容器目录判定（通用启发式，识别失败返回"无法识别"+原因）
  附加：Steam游戏库扫描（appmanifest_*.acf，作为_build_installed_index的一部分）
  兜底：智能兜底（位置感知）- type × position 矩阵生成差异化描述
    type=浏览器 + Local → 浏览器网页缓存与 Cookie，清理后需重新登录
    type=浏览器 + Roaming → 浏览器书签、历史与密码配置，不要删
    type=浏览器 + Program Files → 浏览器主程序（64位）
    其他 type × position 组合：见 utils.py::_TYPE_POSITION_MATRIX
    无 type 或矩阵未命中 → 套通用位置模板（本地数据/主程序/32位/64位）

联网搜索：搜索词加入路径上下文，避免歧义词返回无关结果
自动学习：已永久禁用（污染词典），识别结果只在当前会话内存缓存
"""

import os
import threading
from config import KNOWN_SOFTWARE_DIRS, COMBO_MAP, _record_recognition, SOFTWARE_DICT_FILE
from utils import (
    get_exe_version_info, _read_lnk_target, _match_registry_uninstall,
    _match_wmi_installed, _match_app_paths, _match_start_menu_lnk,
    _deep_scan_exe_pe, _scan_subdir_for_exe_pe, _match_installed_index,
    _match_winget_db, _detect_vendor_container,
    _lookup_winget_by_display_name
)

# 学习缓存写锁（避免多线程并发写JSON冲突）
_dict_write_lock = threading.Lock()

# ========== 20 层命中统计（供动态监控窗口查询） ==========
# 20 层名称（顺序固定，UI 按此顺序显示）
LAYER_NAMES = [
    "0. 缓存目录短路",
    "1. 组合匹配(combo_map)",
    "2. winget软件数据库",
    "3. KNOWN_SOFTWARE_DIRS关键字",
    "4. updater后缀目录",
    "5. 通用词匹配(updater/cache)",
    "6. 反向域名包名识别",
    "7. 注册表+WMI查询",
    "8. 特征文件+关联反推",
    "9. App Paths+开始菜单lnk",
    "10. PE版本信息",
    "11. lnk快捷方式+文件标识",
    "12. 厂商容器目录判定",
    "兜底. 智能兜底(位置感知)",
    "未识别(返回空)",
]

# 统计字典：层名 → {'attempts': int, 'hits': int, 'sample': str}
_LAYER_STATS = {name: {'attempts': 0, 'hits': 0, 'sample': ''} for name in LAYER_NAMES}
_STATS_LOCK = threading.Lock()

# method → 层名映射（用于从 _return_desc 的 method 参数反查层名）
_METHOD_TO_LAYER = {
    "缓存目录短路": "0. 缓存目录短路",
    "组合匹配(combo_map)": "1. 组合匹配(combo_map)",
    "winget软件数据库": "2. winget软件数据库",
    "Microsoft子产品检测": "3. KNOWN_SOFTWARE_DIRS关键字",
    "精确产品检测(子目录扫描)": "3. KNOWN_SOFTWARE_DIRS关键字",
    "关键字匹配(known_software_dirs)": "3. KNOWN_SOFTWARE_DIRS关键字",
    "updater后缀目录": "4. updater后缀目录",
    "通用词匹配(前缀+updater)": "5. 通用词匹配(updater/cache)",
    "通用词匹配(update)": "5. 通用词匹配(updater/cache)",
    "通用词匹配(cache)": "5. 通用词匹配(updater/cache)",
    "反向域名包名(winget)": "6. 反向域名包名识别",
    "反向域名包名识别": "6. 反向域名包名识别",
    "注册表卸载项匹配": "7. 注册表+WMI查询",
    "WMI查询(Win32_InstalledWin32Program)": "7. 注册表+WMI查询",
    "特征文件检测": "8. 特征文件+关联反推",
    "关联安装目录": "8. 特征文件+关联反推",
    "动态已安装软件索引": "8. 特征文件+关联反推",
    "App Paths注册表匹配": "9. App Paths+开始菜单lnk",
    "开始菜单lnk匹配": "9. App Paths+开始菜单lnk",
    "PE版本信息(根目录exe)": "10. PE版本信息",
    "PE版本信息(一级子目录exe)": "10. PE版本信息",
    "PE版本信息(深层扫描)": "10. PE版本信息",
    "lnk快捷方式PE信息": "11. lnk快捷方式+文件标识",
    "文件标识(package.json)": "11. lnk快捷方式+文件标识",
    "文件标识(config.ini)": "11. lnk快捷方式+文件标识",
    "厂商容器目录判定": "12. 厂商容器目录判定",
    "智能兜底(位置感知)": "兜底. 智能兜底(位置感知)",
}


def _enter_layer(layer_name, dir_path=""):
    """记录某一层的尝试（每层执行前调用）"""
    with _STATS_LOCK:
        if layer_name in _LAYER_STATS:
            _LAYER_STATS[layer_name]['attempts'] += 1


def _record_hit(method, sample=""):
    """记录某一层命中（_return_desc 中调用）"""
    layer = _METHOD_TO_LAYER.get(method, "")
    if not layer:
        return
    with _STATS_LOCK:
        if layer in _LAYER_STATS:
            _LAYER_STATS[layer]['hits'] += 1
            if sample and not _LAYER_STATS[layer]['sample']:
                _LAYER_STATS[layer]['sample'] = sample


def get_layer_stats():
    """获取当前 20 层统计快照（主窗口定时查询）"""
    with _STATS_LOCK:
        # 拷贝避免外部修改
        return {k: dict(v) for k, v in _LAYER_STATS.items()}


def reset_layer_stats():
    """重置统计（主窗口点击重置时调用）"""
    with _STATS_LOCK:
        for name in LAYER_NAMES:
            _LAYER_STATS[name] = {'attempts': 0, 'hits': 0, 'sample': ''}


def _record_unrecognized(dir_path):
    """记录未识别（get_dir_description 返回空时调用）"""
    with _STATS_LOCK:
        _LAYER_STATS["未识别(返回空)"]['attempts'] += 1
        _LAYER_STATS["未识别(返回空)"]['hits'] += 1
        if not _LAYER_STATS["未识别(返回空)"]['sample']:
            _LAYER_STATS["未识别(返回空)"]['sample'] = dir_path


def _save_to_learned_dict(dir_name, desc):
    """[已永久禁用] 自动学习是污染根源，清空字典后会重新写入错误数据
    保留函数定义仅为兼容性，调用处已全部注释禁用
    新方案：识别结果只在当前会话内存缓存，不写回 software_dict.json
    """
    return


def _is_vague_desc(desc):
    """判断说明是否笼统"""
    if not desc:
        return False
    return ("相关" in desc) or desc.endswith("软件")


def _judge_text_style(text):
    """文体判别法（§4.5）— 不依赖具体软件名/品牌名，通过文体特征判断文本类别

    判断内容的文体而非内容本身：
    - 句子平均长度：软件描述一般短于20字，百科简介一般长于40字
    - 是否含组织描述句式（如"成立于""是一家...的公司"）
    - 是否含元描述词汇（如"名词""动词""电影""角色"等词类标签）
    - 句子数量：百科一般≥3句完整句子，软件描述一般≤2句

    :param text: 待判别的文本
    :return: "software" / "encyclopedia" / "dictionary" / "unknown"
    """
    if not text or len(text) < 3:
        return "unknown"

    import re

    # 1. 句子切分（按句号、分号、换行）
    #    中文文本用原切分（不含英文句号——版本号/域名/e.g. 里的 "." 会被
    #    误切，导致句数/句长统计失真）；英文文本补英文句号（否则英文
    #    句长统计失真，长度启发式失效）
    is_zh = any('\u4e00' <= c <= '\u9fff' for c in text)
    if is_zh:
        sentences = re.split(r'[。；;\n]', text)
    else:
        sentences = re.split(r'[.。;；\n]', text)
    sentences = [s.strip() for s in sentences if s.strip() and len(s.strip()) >= 2]
    if not sentences:
        return "unknown"

    sent_count = len(sentences)
    avg_len = sum(len(s) for s in sentences) / sent_count

    # 2. 元描述词汇检测（词典/影视/歌词文体的标记词，非具体软件名）
    META_DESC_WORDS = [
        "名词", "动词", "形容词", "副词", "代词", "介词", "连词", "助词", "叹词", "量词",
        "电影", "角色", "演员", "导演", "主演", "上映", "票房",
        "演唱", "专辑", "歌词", "作曲", "作词", "歌手", "单曲",
        "英语中", "中文中", "拼音", "发音", "多义词", "同义词", "反义词",
    ]
    has_meta_desc = any(w in text for w in META_DESC_WORDS)
    # 英文版（\b 词边界 + IGNORECASE；强标记为主，弱标记可删）
    if not has_meta_desc:
        for r in META_DESC_WORDS_EN_RE:
            if r.search(text):
                has_meta_desc = True
                break

    # 3. 组织描述句式检测（百科/公司简介文体的标记句式）
    org_patterns = [
        r'是一(家|种|个).*的(公司|企业|机构|组织|集团|平台|网站|厂商|开发商)',
        r'成立(于|时间|日期)',
        r'注册(于|资本|地)',
        r'总部(位于|于)',
        r'股票代码',
        r'上市(于|公司)',
        r'创始(人|于)',
    ]
    has_org_desc = any(re.search(p, text) for p in org_patterns)
    # 英文版组织描述句式
    if not has_org_desc:
        for r in ORG_PATTERNS_EN_RE:
            if r.search(text):
                has_org_desc = True
                break

    # 4. 判断（按优先级：词典 > 百科 > 软件 > 未知）
    if has_meta_desc:
        return "dictionary"
    if has_org_desc:
        return "encyclopedia"
    # 长度/句数启发式仅对中文生效（阈值按中文字符设计；英文词长不同，
    # 英文多句软件描述会被误判百科——英文文本词表未命中即 unknown）
    if is_zh:
        if avg_len > 40 and sent_count >= 3:
            return "encyclopedia"
        if avg_len <= 20 and sent_count <= 2:
            return "software"
        if sent_count >= 3 and avg_len > 30:
            return "encyclopedia"
    return "unknown"


# ---- 英文版文体判断词表（与上方中文词表语义等价）----
# 设计要点：
# - 英文必须用 \b 词边界 + IGNORECASE（裸子串会灾难性误伤）
# - META 命中即判 dictionary（最高优先级）→ 剔除高频软件词：
#   character(字符集)/released(软件发布)/single(single file)/classifier(分类器)
#   分别用 protagonist/premiered/lead single/measure word 等强替代词
# - 标"弱"的词与中文版覆盖面等价，追求严格可删
import re as _re

META_DESC_WORDS_EN_RE = [
    # 词类标签（词典文体最强标记）
    _re.compile(r"\bnoun\b", _re.IGNORECASE),
    _re.compile(r"\bverb\b", _re.IGNORECASE),
    _re.compile(r"\badjective\b", _re.IGNORECASE),
    _re.compile(r"\badverb\b", _re.IGNORECASE),
    _re.compile(r"\bpronoun\b", _re.IGNORECASE),
    _re.compile(r"\bpreposition\b", _re.IGNORECASE),
    _re.compile(r"(?<!in )\bconjunction\b", _re.IGNORECASE),  # 词类标签；排除软件常见 "in conjunction with"
    _re.compile(r"\binterjection\b", _re.IGNORECASE),
    _re.compile(r"\bmeasure word\b", _re.IGNORECASE),  # 不用裸 classifier（ML 分类器常见）
    # 影视
    _re.compile(r"\b(?:the|a|an)\s+movie\b", _re.IGNORECASE),  # 限定冠词：防游戏描述 "3D movie" 误伤
    _re.compile(r"\b(?:the|a|an)\s+film\b", _re.IGNORECASE),  # 限定冠词：防视频编辑 "for film/TV" 误伤；\b 防 Filmora 品牌名
    _re.compile(r"\bprotagonist\b", _re.IGNORECASE),    # 替代 character（字符编码常见）
    _re.compile(r"\bmain character\b", _re.IGNORECASE),
    _re.compile(r"\bactor\b", _re.IGNORECASE),
    _re.compile(r"\bactress\b", _re.IGNORECASE),
    _re.compile(r"\bstarring\b", _re.IGNORECASE),
    _re.compile(r"\bpremiered\b", _re.IGNORECASE),      # 替代 released（软件发布常见）；不用裸 premiere（Adobe Premiere Pro 产品名）
    _re.compile(r"\bin (?:theaters|cinemas)\b", _re.IGNORECASE),
    _re.compile(r"\bbox[- ]office\b", _re.IGNORECASE),
    _re.compile(r"\bscreenplay\b", _re.IGNORECASE),
    _re.compile(r"\bdirector\b", _re.IGNORECASE),       # 弱：engineering director 会命中
    _re.compile(r"\bepisodes?\b", _re.IGNORECASE),
    # 音乐
    _re.compile(r"\bsinger\b", _re.IGNORECASE),
    _re.compile(r"\balbum\b", _re.IGNORECASE),
    _re.compile(r"\bthe\s+lyrics\b", _re.IGNORECASE),   # 音乐内容语境；不用裸 lyrics（歌词下载工具描述会命中）
    _re.compile(r"\bcomposed\s+by\b", _re.IGNORECASE),  # 作曲（署名语境）；不用裸 composer（PHP Composer 工具）
    _re.compile(r"\bthe\s+composer\b", _re.IGNORECASE),
    _re.compile(r"\bsongwriter\b", _re.IGNORECASE),
    _re.compile(r"\blead single\b", _re.IGNORECASE),    # 替代 single（single file 常见）
    _re.compile(r"\bvocals\b", _re.IGNORECASE),
    _re.compile(r"\bsung by\b", _re.IGNORECASE),
    _re.compile(r"\bperformed by\b", _re.IGNORECASE),   # 弱：程序操作语境会命中
    # 语言/词典
    _re.compile(r"\bpronunciation\b", _re.IGNORECASE),
    _re.compile(r"\bsynonym\b", _re.IGNORECASE),
    _re.compile(r"\bantonym\b", _re.IGNORECASE),
    _re.compile(r"\bpolysemous\b", _re.IGNORECASE),
    _re.compile(r"\bhomonym\b", _re.IGNORECASE),
    _re.compile(r"\betymology\b", _re.IGNORECASE),
    _re.compile(r"\b(?:plural|singular|past tense)\s+of\b", _re.IGNORECASE),
    _re.compile(r"\bpinyin\b", _re.IGNORECASE),         # 弱：输入法描述会命中
    _re.compile(r"\bin English\b", _re.IGNORECASE),     # 弱：本地化文案会命中
    _re.compile(r"\bin Chinese\b", _re.IGNORECASE),     # 弱：同上
]

ORG_PATTERNS_EN_RE = [
    # "is a ..." 后必须跟公司实体名词（≤3 修饰词，单数）——不包含 platform/website：
    # "X is a platform for developers" 是常见软件描述（中文版靠"的"字锚定免疫）；
    # 不含 enterprise（"enterprise messaging/software"里是形容词，误伤）；
    # 不含 group（"group-oriented/groupware/group chat"是常见软件词）
    _re.compile(
        r"\bis an?\s+(?:[\w-]+\s+){0,3}"
        r"(?:company|corporation|organization|organisation|institution|"
        r"studio|developer|publisher|firm|vendor)\b", _re.IGNORECASE),
    _re.compile(r"\b(?:was\s+)?founded\s+(?:in|on|by|as)\b", _re.IGNORECASE),
    _re.compile(r"\bfounding\s+(?:date|year)\b", _re.IGNORECASE),
    _re.compile(r"\bestablished\s+(?:in|on|by)\b", _re.IGNORECASE),
    _re.compile(r"\bheadquartered\s+(?:in|at)\b", _re.IGNORECASE),
    _re.compile(r"\bbased\s+in\b", _re.IGNORECASE),      # 中弱：开源项目 based in 偶见
    _re.compile(r"\bregistered\s+(?:in|at)\b", _re.IGNORECASE),
    _re.compile(r"\bregistered\s+(?:capital|office|address)\b", _re.IGNORECASE),
    _re.compile(r"\bstock\s+(?:code|ticker|symbol)\b", _re.IGNORECASE),
    _re.compile(r"\bticker\s+symbol\b", _re.IGNORECASE),
    _re.compile(r"\bpublicly\s+(?:traded|listed)\b", _re.IGNORECASE),
    _re.compile(r"\bwent\s+public\b", _re.IGNORECASE),
    _re.compile(r"\binitial\s+public\s+offering\b|\bIPOs?\b", _re.IGNORECASE),
    # listed on 限定交易所，防 "listed on the App Store/GitHub" 误伤
    _re.compile(
        r"\blisted\s+on\s+(?:the\s+)?(?:stock\s+exchange|NASDAQ|NYSE|LSE|HKEX|SSE|A-shares?|B-shares?)\b",
        _re.IGNORECASE),
    _re.compile(r"\bfounders?\s*:", _re.IGNORECASE),
]


def _detect_microsoft_product(dir_path):
    r"""Microsoft目录子产品分拆：扫描子目录名推断具体产品
    - AppData\Local\Microsoft\<子产品> → "Microsoft <子产品> 系统应用数据"
    - ProgramData\Microsoft\<子产品> → "Microsoft <子产品> 系统级共享数据"
    - Program Files (x86)\Microsoft\<子产品> → "Microsoft <子产品> 系统运行时组件"
    """
    try:
        subdirs = [e.lower() for e in os.listdir(dir_path)
                   if os.path.isdir(os.path.join(dir_path, e))]
    except Exception:
        return ''
    
    # [已注释-硬编码映射，待用§6.1规则驱动替代]
    # 原PRODUCT_MAP含50+条硬编码产品映射，原SECONDARY含13条
    PRODUCT_MAP = {}  # 原硬编码映射已禁用
    # for sd_name, product_desc in PRODUCT_MAP.items():
    #     for sub in subdirs:
    #         if sd_name in sub:
    #             return product_desc
    SECONDARY = {}  # 原硬编码映射已禁用
    # for sd_name, product_desc in SECONDARY.items():
    #     for sub in subdirs:
    #         if sd_name in sub:
    #             return product_desc
    
    # 无法细粒度识别 → 根据路径特征给分类
    path_lower = dir_path.lower().replace('/', '\\')
    if 'appdata' in path_lower:
        return 'Microsoft 系统应用数据'
    if 'programdata' in path_lower:
        return 'Microsoft 系统级共享数据'
    if 'program files' in path_lower:
        return 'Microsoft 系统运行时组件'
    return 


def _detect_precise_product(dir_path, vague_key):
    """对笼统公司名目录，进入内部扫描子目录名/文件名判断具体产品
    如 C:\\Program Files (x86)\\<厂商名> 下有 <产品名> 子目录 → "[product name]"
    返回具体产品说明，检测失败返回 None
    扫描策略：一级子目录名 → exe文件名 → 深层子目录exe+PE信息
    """
    try:
        entries = os.listdir(dir_path)
    except Exception:
        return None
    sorted_keys = sorted(KNOWN_SOFTWARE_DIRS.keys(), key=len, reverse=True)
    # 排除过于通用的关键字避免误匹配（common→Microsoft公共组件等）
    skip_keys = {vague_key, "common", "code", "apps", "data", "cache",
                 "temp", "logs", "backup", "update", "updater", "config",
                 "history", "logs", "installer"}
    # 1. 优先匹配一级子目录名（如具体产品名等）
    for entry in entries:
        entry_lower = entry.lower()
        try:
            is_dir = os.path.isdir(os.path.join(dir_path, entry))
        except Exception:
            is_dir = False
        if is_dir:
            for k in sorted_keys:
                if k in skip_keys:
                    continue
                if k in entry_lower:
                    return KNOWN_SOFTWARE_DIRS[k]
    # 2. 匹配根目录exe文件名
    for entry in entries:
        entry_lower = entry.lower()
        if entry_lower.endswith('.exe'):
            for k in sorted_keys:
                if k in skip_keys:
                    continue
                if k in entry_lower:
                    return KNOWN_SOFTWARE_DIRS[k]
    # 3. 深层扫描：一级子目录下的exe文件名 + PE版本信息（最多扫8个子目录）
    sub_count = 0
    for entry in entries:
        if sub_count >= 8:
            break
        sub_path = os.path.join(dir_path, entry)
        try:
            if not os.path.isdir(sub_path):
                continue
        except Exception:
            continue
        sub_count += 1
        try:
            for sub_entry in os.listdir(sub_path):
                sub_lower = sub_entry.lower()
                if sub_lower.endswith('.exe'):
                    # 3a. exe文件名匹配字典
                    for k in sorted_keys:
                        if k in skip_keys:
                            continue
                        if k in sub_lower:
                            return KNOWN_SOFTWARE_DIRS[k]
                    # 3b. PE版本信息兜底
                    try:
                        info = get_exe_version_info(os.path.join(sub_path, sub_entry))
                        if info:
                            return info
                    except Exception:
                        pass
        except Exception:
            pass
    # 4. 根目录exe的PE版本信息兜底
    for entry in entries:
        if entry.lower().endswith('.exe'):
            try:
                info = get_exe_version_info(os.path.join(dir_path, entry))
                if info:
                    return info
            except Exception:
                pass
    return None


def get_dir_description(dir_path):
    r"""获取目录的说明信息 - 13层识别管线 + 位置感知后处理（对所有软件统一生效）
    识别顺序（2026-07-21 精简：合并冗余层）：
    1. COMBO_MAP组合匹配（学习缓存，手动精调的高质量映射）
    2. winget软件数据库匹配（19421 包，返回三元组 name+desc+type，触发 type×position 矩阵）
       - 国内软件和国际软件处理方式完全一致
       - winget DB 不含版本号，软件升级不影响匹配结果
    3. KNOWN_SOFTWARE_DIRS关键字匹配（学习缓存，长关键字优先，笼统说明触发子目录精确检测）
    4. -updater/-update 后缀目录识别
    5. 通用词匹配（updater/update/cache+具体软件名）
    6. 反向域名包名识别
    7. 注册表卸载项 + WMI 查询（合并层：两者都是查已安装软件）
    8. 特征文件 + 关联目录 + 动态索引（合并层：三种都是通过已知软件反推）
    9. App Paths + 开始菜单 lnk（合并层：都是 exe 名匹配）
    10. PE版本信息（合并层：根目录→一级子目录→深层递归）
    11. lnk快捷方式 + 文件标识
    12. 厂商容器目录判定（识别失败返回"无法识别"+原因）
    兜底：智能兜底（位置感知）

    位置感知后处理（核心改进，对所有软件统一生效，不针对特定软件）：
    每一层识别成功后，调用 _enhance_with_location 叠加位置信息
    有 type 时优先用 type × position 矩阵生成差异化描述（如"浏览器网页缓存与 Cookie"），
    无 type 或矩阵未命中时退回通用位置模板（"本地数据/主程序/32位/64位"）
    """
    dir_name = os.path.basename(dir_path).lower()
    dir_name_raw = os.path.basename(dir_path)  # 保留原始大小写，供驼峰分词匹配使用
    full_path_lower = dir_path.lower().replace("/", "\\")
    parent_name = ""
    if os.path.dirname(dir_path):
        parent_name = os.path.basename(os.path.dirname(dir_path)).lower()

    # ========== 缓存类目录短路（零层命中，< 1ms）==========
    # 这些目录名本身就表明功能是缓存/临时数据，无需走 20 层识别
    # 命中后直接返回固定描述，跳过 PE 扫描/WMI/注册表查询
    # （npm-cache 这种目录深层 PE 扫描需 30 秒，是并行下最大瓶颈路径）
    _CACHE_DIR_SHORTCUT = {
        # 包管理器缓存（深层文件多，PE 扫描巨慢）
        "npm-cache": "npm 包下载缓存",
        "pip cache": "pip 包下载缓存",
        "yarn cache": "yarn 包下载缓存",
        "pnpm store": "pnpm 包存储缓存",
        "nuget cache": "NuGet 包缓存",
        "maven cache": "Maven 构建缓存",
        "gradle caches": "Gradle 构建缓存",
        # 浏览器/运行时缓存目录
        "gpucache": "GPU 着色器缓存",
        "code cache": "Electron 应用缓存",
        "shader cache": "GPU 着色器缓存",
        "disk cache": "磁盘缓存数据",
        "cache2": "浏览器缓存数据",
        # 通用缓存目录名（必须整个目录名就是这些词，不匹配含这些词的子串）
        ".cache": "应用缓存数据",
        "cache": "应用缓存数据",
        "cachedata": "应用缓存数据",
        # 依赖目录
        "node_modules": "Node.js 依赖模块目录",
        "bower_components": "Bower 前端依赖目录",
        "vendor": "第三方依赖库目录",
    }
    _hit = _CACHE_DIR_SHORTCUT.get(dir_name)
    if _hit:
        _record_hit("缓存目录短路", sample=dir_name)
        # i18n：缓存短路描述翻译（延迟导入避免循环依赖）
        try:
            from i18n import tr
            return tr(_hit)
        except Exception:
            return _hit

    # 位置感知后处理函数：对所有识别结果叠加位置信息（通用，不针对特定软件）
    from utils import _enhance_with_location, _smart_fallback_desc, _detect_by_file_features, _find_related_install



    def _strip_location_suffix(desc):
        """从描述中剥离位置后缀，还原纯软件名"""
        if not desc:
            return desc
        suffixes = [
            " 本地数据（缓存/配置）", " 用户配置（漫游数据）",
            " 主程序（64位）", " 主程序（32位）",
            " 公共数据（系统级）", " 程序安装目录",
            " 本地数据", " 主程序", " 用户配置",
            " 公共数据", " 漫游数据",
        ]
        for suf in suffixes:
            if desc.endswith(suf):
                return desc[:-len(suf)]
        return desc

    def _return_desc(desc, method, save_to_dict=True, type_val="", software_name=""):
        """统一返回函数：叠加位置信息 + 记录日志 + 自动学习

        保存策略（保守，防止污染词典）：
        - 纯软件名（如"<软件名>"）→ 保存
        - 带位置后缀（如"本地数据（缓存/配置）"）→ 先剥离再保存
        - 笼统描述（"缓存数据"/"日志文件"/"浏览器数据"）→ 不保存
        - 过于简短（<4字）→ 不保存

        :param type_val: 软件类型（78类之一，可选）。有 type 时 _enhance_with_location
                        会按 type × position 矩阵生成差异化描述（第5步子任务E）
        :param software_name: 真实软件名（可选）。矩阵模板使用此名称填充 {sw}；
                              未提供时矩阵使用 desc 填充（可能产生冗余功能描述）
        """
        if not desc:
            return ""
        enhanced = _enhance_with_location(desc, dir_path, dir_name,
                                          type_val=type_val,
                                          software_name=software_name)
        # i18n：识别结果统一翻译（矩阵/位置模板已在 utils 内翻译，
        # 此处兜底词典/兜底描述；英文模式未命中保持原文）
        try:
            from i18n import tr
            enhanced = tr(enhanced)
        except Exception:
            pass
        _record_recognition(dir_path, enhanced, method)
        # 记录命中（动态监控窗口用）
        _record_hit(method, sample=dir_name)
        if save_to_dict:
            pure = _strip_location_suffix(desc) or desc
            # 过滤笼统/无意义的描述，不存词典（防止下次误匹配）
            # [已注释-穷举正则拒绝规则，待用§4.5文体判别法替代]
            # import re
            # QUALITY_REJECT_PATTERNS = [
            #     r"是一[家种]", r"推出|上线", r"成立于", r"是以", r"简称",
            #     r"歌曲|演唱|词曲", r"电影|中的角色", r"游戏《|大型多",
            #     r"英语|词典|名词|动词|形容词|多义词|发音为", r"拼音",
            #     r"股票代码", r"航天活动|太空", r"又称|原名",
            #     r"是一家|撰写", r"引擎|UE引擎|MLIR",
            #     r"列表", r"缩写",
            #     r"^(Et|Network|IndexedDB|Updater|Update|Cache|Config|Data|Logs|Common)$",
            # ]
            # qs_reject = any(re.search(p, pure) for p in QUALITY_REJECT_PATTERNS)
            # GENERIC_FILTER = {
            #     '缓存数据', '日志文件', '临时文件', '备份文件', '配置数据',
            #     '浏览器数据', 'Electron应用数据', '日志数据', '崩溃报告数据',
            #     '自动更新程序', '更新程序', '更新数据',
            # }
            # skip_list = ['相关', '软件', '程序', '数据']
            # 文体判别（§4.5）：百科/词典/影视内容不写入词典
            style = _judge_text_style(pure)
            style_reject = style in ("encyclopedia", "dictionary")
            # 保留规则驱动的判断（不依赖具体字眼）
            too_generic = (
                (len(pure.strip()) < 4) or
                (pure.strip().endswith('软件') and len(pure.strip()) <= 6) or
                ('相关' in pure and len(pure) < 15) or style_reject
            )
            # [已禁用-自动学习是污染根源，清空字典后会重新写入错误数据]
            # if not too_generic:
            #     _save_to_learned_dict(dir_name, pure)
        return enhanced

    # ========== G7: winget 精确层前置（精确覆盖粗略）==========
    # 原 13 层中 combo/关键字等粗略层"第一个命中即 return"，会拦截
    # winget 精确结果（如目录名恰好命中关键字笼统说明时，winget 的功能
    # 描述永远到不了）。在粗略层之前先查 winget（内存字典，微秒级），
    # 命中即返回——精确层结果覆盖粗略层（缺口清单 G7）。
    # 缓存类目录短路保持优先（缓存目录名本身即准确结论）。
    _enter_layer("G7. winget精确层前置", dir_path)
    _winget_pre = _match_winget_db(dir_name_raw)
    if _winget_pre:
        _wn_pre, _wd_pre, _wt_pre = _winget_pre
        _display_pre = _wd_pre if _wd_pre else _wn_pre
        return _return_desc(_display_pre, "winget软件数据库",
                            type_val=_wt_pre, software_name=_wn_pre)

    # 0. 多段目录名组合匹配
    _enter_layer("1. 组合匹配(combo_map)", dir_path)
    for key, desc in COMBO_MAP.items():
        if full_path_lower.endswith(key):
            return _return_desc(desc, "组合匹配(combo_map)")

    # 1. winget 内置软件数据库匹配（2026-07-20 提到第2层）
    # 从 winget-pkgs 仓库提取的 10500+ 软件 PackageName，覆盖本机未安装但常见的软件
    # 优势：返回三元组 (name, desc, type)，desc 是中文功能描述、type 是 78 类之一
    #       winget DB 不含版本号，软件升级不影响匹配结果
    # 放在第2层原因：COMBO_MAP 是手动精调的组合匹配（如 "Google"+"Chrome"）应保留优先；
    #              抢在 KNOWN_SOFTWARE_DIRS 之前，避免被笼统关键字说明拦截；
    #              立即拿到 type 触发 type × position 矩阵生成差异化描述
    # 注：用 dir_name_raw（原始大小写）以支持驼峰分词（EpicGames → epic+games）
    _enter_layer("2. winget软件数据库", dir_path)
    winget_result = _match_winget_db(dir_name_raw)
    if winget_result:
        winget_name, winget_desc, winget_type = winget_result
        display = winget_desc if winget_desc else winget_name
        return _return_desc(display, "winget软件数据库",
                            type_val=winget_type, software_name=winget_name)

    # 3. 目录名精确匹配/包含匹配已知软件库（KNOWN_SOFTWARE_DIRS）
    # 学习缓存的关键字匹配，长关键字优先
    # 笼统说明会触发子目录精确检测
    _enter_layer("3. KNOWN_SOFTWARE_DIRS关键字", dir_path)
    is_pkg_name = (dir_name.startswith("com.") or dir_name.startswith("org.")
                   or dir_name.startswith("io.") or dir_name.startswith("cn.")
                   or dir_name.startswith("dev."))
    if not is_pkg_name:
        sorted_keys = sorted(KNOWN_SOFTWARE_DIRS.keys(), key=len, reverse=True)
        for key in sorted_keys:
            if key in dir_name:
                desc = KNOWN_SOFTWARE_DIRS[key]
                if _is_vague_desc(desc):
                    # Microsoft目录子产品分拆
                    if 'microsoft' in desc.lower() or 'microsoft' in key:
                        ms_product = _detect_microsoft_product(dir_path)
                        if ms_product:
                            return _return_desc(ms_product, "Microsoft子产品检测")
                    precise = _detect_precise_product(dir_path, key)
                    if precise:
                        return _return_desc(precise, "精确产品检测(子目录扫描)")
                    break  # 笼统说明检测失败，继续下一层
                return _return_desc(desc, "关键字匹配(known_software_dirs)")
    else:
        sorted_keys = sorted(KNOWN_SOFTWARE_DIRS.keys(), key=len, reverse=True)

    # 5. -updater/-update 后缀目录识别（winget/scoop/KNOWN_SOFTWARE_DIRS 都未命中时）
    # 例：123pan-updater → "<前缀> 自动更新程序"
    _enter_layer("4. updater后缀目录", dir_path)
    _dl_lower_for_updater = dir_name.lower()
    _updater_prefix = None
    if _dl_lower_for_updater.endswith("-updater"):
        _updater_prefix = dir_name[:-8]
    elif _dl_lower_for_updater.endswith("_updater"):
        _updater_prefix = dir_name[:-8]
    elif _dl_lower_for_updater.endswith("-update"):
        _updater_prefix = dir_name[:-7]
    elif _dl_lower_for_updater.endswith("_update"):
        _updater_prefix = dir_name[:-7]
    if _updater_prefix:
        # 用 prefix 反查 winget DB 拿软件真实名 + type（用于触发矩阵）
        _prefix_winget = _match_winget_db(_updater_prefix)
        if _prefix_winget:
            _pw_name, _pw_desc, _pw_type = _prefix_winget
            # 用软件名走矩阵（type 在矩阵中时会生成"Discord 通讯软件自动更新组件"等差异化描述）
            # 矩阵未命中时走位置模板（会生成"Discord 自动更新组件"）
            return _return_desc(_pw_desc or _pw_name, "updater后缀目录",
                                type_val=_pw_type, software_name=_pw_name)
        # winget 未命中，从 KNOWN_SOFTWARE_DIRS 反查
        _prefix_lower = _updater_prefix.lower()
        for _k in sorted_keys:
            if _k in _prefix_lower:
                _desc = KNOWN_SOFTWARE_DIRS[_k]
                if not _is_vague_desc(_desc):
                    return _return_desc(_desc, "updater后缀目录", software_name=_desc)
        # 都未命中，用 prefix 直接走位置模板（去掉 @ 等前缀符号）
        _prefix_clean = _updater_prefix.lstrip("@")
        if _prefix_clean and len(_prefix_clean) >= 2:
            return _return_desc(_prefix_clean, "updater后缀目录", software_name=_prefix_clean)
        return _return_desc("软件自动更新组件", "updater后缀目录")

    # 6. 通用词匹配（updater/update/cache+具体软件名）
    # 改造：不再用"X 自动更新程序""X 的更新数据"机翻模板
    # 用软件名走 type×position 矩阵或位置模板自然拼接（如"Discord 通讯软件自动更新组件"）
    _enter_layer("5. 通用词匹配(updater/cache)", dir_path)
    if dir_name.endswith("-updater") or dir_name.endswith("_updater") or dir_name == "updater":
        prefix = dir_name.replace("-updater", "").replace("_updater", "")
        if prefix:
            # 优先反查 winget DB 拿 type 走矩阵
            _upd_winget = _match_winget_db(prefix)
            if _upd_winget:
                return _return_desc(_upd_winget[1] or _upd_winget[0], "通用词匹配(前缀+updater)",
                                   type_val=_upd_winget[2], software_name=_upd_winget[0])
            # winget 未命中，用 KNOWN_SOFTWARE_DIRS 反查
            for k in sorted_keys:
                if k in prefix:
                    desc = KNOWN_SOFTWARE_DIRS[k]
                    if not _is_vague_desc(desc):
                        # 用软件名走位置模板（updater 目录通常是 local/roaming 位置）
                        return _return_desc(desc, "通用词匹配(前缀+updater)", software_name=desc)
            # 都未命中，用 prefix 直接走位置模板
            _prefix_clean = prefix.lstrip("@")
            if _prefix_clean and len(_prefix_clean) >= 2:
                return _return_desc(_prefix_clean, "通用词匹配(前缀+updater)", software_name=_prefix_clean)
    elif "update" in dir_name and dir_name not in ("updates", "updatedata", "updatecache"):
        # 含 update 但不是 updater 后缀的目录（如"GoogleUpdate"）
        for k in sorted_keys:
            if k in dir_name and k != "update":
                desc = KNOWN_SOFTWARE_DIRS[k]
                if not _is_vague_desc(desc):
                    return _return_desc(desc, "通用词匹配(update)", software_name=desc)
    elif "cache" in dir_name:
        for k in sorted_keys:
            if k in dir_name and k != "cache":
                desc = KNOWN_SOFTWARE_DIRS[k]
                if not _is_vague_desc(desc):
                    return _return_desc(desc, "通用词匹配(cache)", software_name=desc)

    # 7. 反向域名包名识别
    _enter_layer("6. 反向域名包名识别", dir_path)
    if (dir_name.startswith("com.") or dir_name.startswith("org.")
            or dir_name.startswith("io.") or dir_name.startswith("cn.")
            or dir_name.startswith("dev.")):
        parts = dir_name.split(".")
        if len(parts) >= 3:
            app_name = parts[-1]
            mid_name = parts[-2]
            # 7a. 优先用 app_name/mid_name 反查 winget DB（拿 type 触发矩阵）
            for _pkg_part in (app_name, mid_name):
                if not _pkg_part or len(_pkg_part) < 3:
                    continue
                _pkg_winget = _match_winget_db(_pkg_part)
                if _pkg_winget:
                    _pkgw_name, _pkgw_desc, _pkgw_type = _pkg_winget
                    _pkgw_display = _pkgw_desc if _pkgw_desc else _pkgw_name
                    return _return_desc(_pkgw_display, "反向域名包名(winget)",
                                        type_val=_pkgw_type, software_name=_pkgw_name)
            # 7b. KNOWN_SOFTWARE_DIRS 关键字匹配（兜底）
            # 改造：去掉"（包名: xxx）"技术信息，传 software_name 走矩阵自然拼接
            for k in sorted_keys:
                if k in app_name or k in mid_name:
                    desc = KNOWN_SOFTWARE_DIRS[k]
                    if not _is_vague_desc(desc):
                        return _return_desc(desc, "反向域名包名识别", software_name=desc)

    # 7. 注册表卸载项 + WMI 查询（合并层：两者都是查已安装软件）
    # 拿到 DisplayName 后反查 winget DB 拿 desc/type，走 type×position 矩阵生成自然描述
    # 未反查命中才用 DisplayName 走原位置模板（避免机翻"DisplayName 本地数据"）
    _enter_layer("7. 注册表+WMI查询", dir_path)
    reg_desc = _match_registry_uninstall(dir_path, dir_name)
    if reg_desc:
        _reg_winget = _lookup_winget_by_display_name(reg_desc)
        if _reg_winget:
            return _return_desc(_reg_winget[1] or _reg_winget[0], "注册表卸载项匹配",
                               type_val=_reg_winget[2], software_name=_reg_winget[0])
        return _return_desc(reg_desc, "注册表卸载项匹配", software_name=reg_desc)
    # WMI 作为注册表的补充（覆盖 MSI/UWP）
    wmi_desc = _match_wmi_installed(dir_path, dir_name)
    if wmi_desc:
        _wmi_winget = _lookup_winget_by_display_name(wmi_desc)
        if _wmi_winget:
            return _return_desc(_wmi_winget[1] or _wmi_winget[0], "WMI查询(Win32_InstalledWin32Program)",
                               type_val=_wmi_winget[2], software_name=_wmi_winget[0])
        return _return_desc(wmi_desc, "WMI查询(Win32_InstalledWin32Program)", software_name=wmi_desc)

    # 8. 特征文件 + 关联目录 + 动态索引（合并层：三种都是通过已知软件反推）
    _enter_layer("8. 特征文件+关联反推", dir_path)
    feature_desc = _detect_by_file_features(dir_path, dir_name)
    if feature_desc:
        return _return_desc(feature_desc, "特征文件检测")
    related_desc = _find_related_install(dir_path, dir_name, KNOWN_SOFTWARE_DIRS)
    if related_desc:
        return _return_desc(related_desc, "关联安装目录")
    installed_name = _match_installed_index(dir_name)
    if installed_name:
        return _return_desc(installed_name, "动态已安装软件索引", software_name=installed_name)

    # 9. App Paths + 开始菜单 lnk（合并层：都是 exe 名匹配）
    _enter_layer("9. App Paths+开始菜单lnk", dir_path)
    app_path_desc = _match_app_paths(dir_name)
    if app_path_desc:
        return _return_desc(app_path_desc, "App Paths注册表匹配", software_name=app_path_desc)
    start_menu_desc = _match_start_menu_lnk(dir_name)
    if start_menu_desc:
        return _return_desc(start_menu_desc, "开始菜单lnk匹配", software_name=start_menu_desc)

    # 15-18. PE版本信息 / 文件标识 / lnk快捷方式
    # 厂商容器目录（Adobe/Google/Mozilla/NCSOFT/Tencent 等）跳过这些层，
    # 因为这些目录下含多个子产品的 exe，读到的 PE 信息只是其中一个子产品（如 Adobe Creative Cloud），
    # 但父目录本身是容器，应该走第19层 _detect_vendor_container 识别为"厂商容器目录"或子产品分拆
    from utils import _is_vendor_container_dir
    _is_vendor_dir, _ = _is_vendor_container_dir(dir_path, dir_name)
    # 厂商容器目录（强黑名单）一定跳过 PE 扫描层
    # 即使 _is_vendor_container_dir 启发式未识别（如子目录<2），也按目录名黑名单强制跳过
    from utils import _VENDOR_CONTAINER_DIRS_FOR_FEATURE
    if (not _is_vendor_dir) and (dir_name.lower() in _VENDOR_CONTAINER_DIRS_FOR_FEATURE):
        _is_vendor_dir = True
    if not _is_vendor_dir:
        # 10. PE版本信息（合并层：根目录→一级子目录→深层递归，统一递归扫描）
        # PE ProductName 同样反查 winget DB，命中走矩阵，未命中用 ProductName
        _enter_layer("10. PE版本信息", dir_path)
        # 过滤通用/系统组件PE文本（如 "Microsoft® Windows® Operating System"、"Network" 等）
        from utils import _is_generic_pe_text
        # 10a. 根目录 exe
        try:
            for item in os.listdir(dir_path):
                if item.lower().endswith('.exe'):
                    exe_path = os.path.join(dir_path, item)
                    info = get_exe_version_info(exe_path)
                    if info and not _is_generic_pe_text(info):
                        _pe_winget = _lookup_winget_by_display_name(info)
                        if _pe_winget:
                            return _return_desc(_pe_winget[1] or _pe_winget[0], "PE版本信息(根目录exe)",
                                               type_val=_pe_winget[2], software_name=_pe_winget[0])
                        return _return_desc(info, "PE版本信息(根目录exe)", software_name=info)
                    break
        except Exception:
            pass
        # 10b. 一级子目录 exe
        subdir_pe = _scan_subdir_for_exe_pe(dir_path, max_subdirs=10)
        if subdir_pe:
            _sub_pe_winget = _lookup_winget_by_display_name(subdir_pe)
            if _sub_pe_winget:
                return _return_desc(_sub_pe_winget[1] or _sub_pe_winget[0], "PE版本信息(一级子目录exe)",
                                   type_val=_sub_pe_winget[2], software_name=_sub_pe_winget[0])
            return _return_desc(subdir_pe, "PE版本信息(一级子目录exe)", software_name=subdir_pe)
        # 10c. 深层递归扫描
        deep_pe = _deep_scan_exe_pe(dir_path, max_depth=3, max_count=15)
        if deep_pe:
            _deep_pe_winget = _lookup_winget_by_display_name(deep_pe)
            if _deep_pe_winget:
                return _return_desc(_deep_pe_winget[1] or _deep_pe_winget[0], "PE版本信息(深层扫描)",
                                   type_val=_deep_pe_winget[2], software_name=_deep_pe_winget[0])
            return _return_desc(deep_pe, "PE版本信息(深层扫描)", software_name=deep_pe)

        # 11. 文件标识 + lnk快捷方式
        _enter_layer("11. lnk快捷方式+文件标识", dir_path)
        try:
            files = os.listdir(dir_path)
            for f in files:
                fl = f.lower()
                if fl.endswith('.json'):
                    if fl == 'package.json':
                        return _return_desc("Node.js项目", "文件标识(package.json)")
                if fl == 'config.ini' or fl == 'settings.ini':
                    return _return_desc("配置/设置数据", "文件标识(config.ini)")
            for f in files:
                if f.lower().endswith('.lnk'):
                    try:
                        target_exe = _read_lnk_target(os.path.join(dir_path, f))
                        if target_exe:
                            info = get_exe_version_info(target_exe)
                            if info and not _is_generic_pe_text(info):
                                _lnk_pe_winget = _lookup_winget_by_display_name(info)
                                if _lnk_pe_winget:
                                    return _return_desc(_lnk_pe_winget[1] or _lnk_pe_winget[0], "lnk快捷方式PE信息",
                                                       type_val=_lnk_pe_winget[2], software_name=_lnk_pe_winget[0])
                                return _return_desc(info, "lnk快捷方式PE信息", software_name=info)
                    except Exception:
                        pass
                    break
        except Exception:
            pass

    # 19. 厂商容器目录判定（通用启发式，不针对特定厂商/软件）
    _enter_layer("12. 厂商容器目录判定", dir_path)
    vendor_desc = _detect_vendor_container(dir_path, dir_name_raw)
    if vendor_desc:
        return _return_desc(vendor_desc, "厂商容器目录判定", save_to_dict=False)

    # 20. 智能兜底（位置感知，兜底不写入字典缓存）
    _enter_layer("兜底. 智能兜底(位置感知)", dir_path)
    installed_name_for_fallback = _match_installed_index(dir_name)
    fallback = _smart_fallback_desc(dir_path, dir_name, installed_name_for_fallback)
    if fallback:
        return _return_desc(fallback, "智能兜底(位置感知)", save_to_dict=False)

    # 记录未识别
    _record_unrecognized(dir_path)
    return ""


def _preprocess_dir_name(dir_name):
    """预处理目录名用于搜索
    1. 驼峰命名拆分：GLMPC → GLM PC，MyApp → My App
    2. 去掉常见后缀：-updater/_updater/-desktop 等
    3. 去掉反向域名前缀：com.xxx/org.xxx
    4. 去掉版本号
    返回 (原始名, 清理后的搜索词)
    """
    import re
    if not dir_name or not dir_name.strip():
        return "", ""
    name = dir_name.strip()
    # 驼峰拆分：在大小写交界处插入空格
    # GLMPCSetup → GLM PC Setup, glmPc → glm Pc, MyApp → My App
    camel_split = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
    # 处理连续大写后跟小写的情况：GLM Pc → GLM Pc（已正确）
    # 但 GLMPC → 需要在最后一个大写字母处分开
    camel_split = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', camel_split)
    clean = camel_split
    # 去掉常见后缀
    lower = clean.lower()
    for suffix in ['-updater', '_updater', '-desktop', '_data', '-app', '-pc',
                   '-cache', '_cache', '-update', '_update', ' updater', ' update']:
        if lower.endswith(suffix):
            clean = clean[:-len(suffix)].strip()
            lower = clean.lower()
            break
    # 去掉反向域名前缀
    for prefix in ['com.', 'org.', 'io.', 'cn.', 'app.', 'dev.']:
        if lower.startswith(prefix):
            parts = clean.split('.')
            if len(parts) >= 2:
                clean = parts[-1]
            break
    # 去掉版本号、数字后缀
    clean = re.sub(r'[\d_]+$', '', clean).strip('-').strip('_').strip()
    if not clean:
        clean = name
    return name, clean


def search_online_description(dir_name, dir_path=""):
    """联网搜索软件说明 - 国内源优先 + 交叉验证 + 激进精简
    策略：
    1. 优先国内源（某某百科API最快）→ 必应 → 中文Wikipedia
    2. 交叉验证必须：至少2个源描述一致（关键词重合度>=20%）才写入
    3. 没有确切信源宁愿不填（返回空，不写入错误信息）
    4. 精简激进：只取第一句，去掉百科冗长描述，限制40字
    5. 联网成功后自动写回 software_dict.json，下次不联网
    所有繁体中文自动转为简体中文后返回
    注：dir_path 参数保留（兼容调用方），当前不参与搜索词构造。
    """
    import json
    import urllib.request
    import urllib.parse
    import re

    if not dir_name or not dir_name.strip():
        return ""

    name, clean = _preprocess_dir_name(dir_name)
    if not clean:
        return ""

    UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'}

    def _to_simplified(text):
        """繁体转简体"""
        if not text:
            return text
        try:
            import opencc
            converter = opencc.OpenCC('t2s')
            return converter.convert(text)
        except Exception:
            pass
        try:
            # 扩展繁简映射（覆盖常见百科返回的繁体字）
            t2s_map = str.maketrans(
                '個這麼說來對於與從為現還裡後過給讓問題發間業電腦軟體網路資訊系統臺灣'
                '麽門開關對萬長時氣機動會進實體種學樣能體應這兒點兒'
                '號稱東邊遠選運行連線係參華國圖際麼'
                '妳亞歐羅馬數據龍書畫寶衛裏麵'
                '簡曆歷種兒隨',
                '个这么说来对于与从为现还里后过给让问题发间业电脑软件网络资讯系统台湾'
                '么门开关对万长时气机动会进实体种学样能体应这儿点儿'
                '号称东边远选运行连线关系华国国际么'
                '你亚欧罗马数据龙书画宝卫里面'
                '简历历种儿随'
            )
            return text.translate(t2s_map)
        except Exception:
            return text

    def _simplify(text, max_len=40):
        """激进精简描述 - 只取核心概括，去掉百科冗长内容
        策略：
        1. 去掉引用标记[1]、HTML标签
        2. 只取第一句（第一个句号/分号前）
        3. 去掉"是一个/是一款/是由xxx开发"等开头冗余
        4. 去掉"该软件/该程序/该应用"等冗余主语
        5. 限制40字，超长在标点处截断
        """
        if not text:
            return ""
        # 去引用标记、HTML标签
        text = re.sub(r'\[\d+\]', '', text)
        text = re.sub(r'<[^>]+>', '', text)
        # 只取第一句（句号/分号/换行前）
        for sep in ['。', '；', '\n', '；', ';']:
            idx = text.find(sep)
            if idx > 10:
                text = text[:idx]
                break
        # 去掉开头冗余：是一个/是一款/是由xxx开发/xxx是一款
        text = re.sub(r'^[^，,。；;]{1,15}是一(个|款|款由|种|项)', '', text)
        text = re.sub(r'^[^，,。；;]{1,15}是', '', text)
        text = re.sub(r'^(它|这|该|本)(是|为|是一个|是一款)', '', text)
        text = re.sub(r'^(由|是)[^，,。；;]{1,20}(开发|推出|提供|发行|制作)', '', text)
        # 去掉冗余主语
        text = re.sub(r'^(该|本)(软件|程序|应用|工具|平台|系统)', '', text)
        text = text.strip('，,。.；; 、的')
        if not text:
            return ""
        # 限制长度，在标点处智能截断
        if len(text) > max_len:
            cut = text[:max_len]
            for sep in ['，', ',', '、', ' ']:
                idx = cut.rfind(sep)
                if idx > max_len // 2:
                    cut = cut[:idx]
                    break
            else:
                cut = cut.rstrip()
            text = cut
        return text.strip()

    def _bing_search(query, content_ctx="", context=""):
        """必应搜索 — 纯规则生成搜索词，不包含任何目录名

        根据目录名特征自动选策略：
        - 纯中文 → "是什么文件夹"
        - 含空格 → 原词 + " 是什么文件夹"
        - 短英文(<=3字符) → "是什么文件夹 Windows"
        - 含数字/版本号 → 剥掉版本号再搜
        - 反域名格式(cn.org.xxx) → 取最后两段
        - 默认 → "是什么文件夹"

        content_ctx/context 为可选增强词（空则不插入，保持原行为）
        """

        def _build_search_terms(q):
            """根据查询词特征自动构造搜索词列表，零硬编码"""
            terms = []
            is_chinese = bool(re.search(r'[一-鿿]', q))
            is_domain = bool(re.match(r'^[a-z]{2,}\.[a-z]+(?:\.[a-z]+)+$', q, re.I))
            has_digit = bool(re.search(r'\d', q))
            has_space = ' ' in q or '-' in q or '_' in q
            word_len = len(re.sub(r'[^a-zA-Z一-鿿]', '', q))

            if is_domain:
                # 反向域名：取最后一段（app名）
                parts = q.split('.')
                short = parts[-1] if len(parts) > 2 else q
                terms = [
                    f'{short} 是什么文件夹',
                    f'{q} 是什么文件夹',
                ]
            elif is_chinese:
                terms = [
                    f'{q} 是什么文件夹',
                    f'{q} 文件夹 用途',
                ]
            elif has_digit and has_space:
                # 可能是带版本号的软件名，保留原始搜索
                terms = [
                    f'{q} 是什么文件夹',
                    f'{q} 软件 文件夹',
                ]
            elif word_len <= 3:
                # 短词极容易歧义
                terms = [
                    f'{q} 是什么文件夹',
                    f'{q} 文件夹 电脑 用途',
                ]
            else:
                terms = [
                    f'{q} 是什么文件夹',
                    f'{q} 是什么软件',
                    f'{q} 文件夹 用途',
                ]
            return terms

        # 按搜索词串行搜索，首个有结果停止
        terms = _build_search_terms(query)
        # 将路径上下文 / 内容上下文加入搜索词（顶部 docstring 意图：
        # "搜索词加入路径上下文，避免歧义词返回无关结果"）
        # content_ctx 是特征文件推断的软件类别（如 python/游戏/node.js），
        #   优先级高，插到列表头部缩小搜索范围
        # context 是目录所在路径（如 AppData\Local/Program Files），
        #   作为兜底附加搜索词
        # 任一为空时对应增强词不插入，保持原行为
        if content_ctx:
            terms.insert(0, f'{query} {content_ctx} 是什么文件夹')
        if context:
            terms.append(f'{query} {context} 文件夹 用途')
        results = []
        for search_term in terms:
            try:
                search_url = "https://cn.bing.com/search?q=" + urllib.parse.quote(search_term)
                req = urllib.request.Request(search_url, headers=UA)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    html = resp.read().decode('utf-8', errors='ignore')
                    # 优先抓必应AI摘要（b_ans class）
                    for m in re.finditer(r'<div[^>]*class="b_ans[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL):
                        desc = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                        if desc and len(desc) > 15:
                            results.append(_to_simplified(desc))
                    # 其次抓搜索摘要（b_lineclamp class）
                    if not results:
                        for m in re.finditer(r'<p[^>]*class="b_lineclamp[^"]*"[^>]*>(.*?)</p>', html, re.DOTALL):
                            desc = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                            if desc and len(desc) > 15:
                                results.append(_to_simplified(desc))
                    # 备用：data-content 属性
                    if not results:
                        for m in re.finditer(r'data-content="([^"]+)"', html):
                            desc = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                            if desc and len(desc) > 15:
                                results.append(_to_simplified(desc))
                if results:
                    break
            except Exception:
                continue
        return results
    def _bing_knowledge(query):
        """必应知识图谱 - 直接获取实体摘要"""
        try:
            search_url = "https://cn.bing.com/search?q=" + urllib.parse.quote(query)
            req = urllib.request.Request(search_url, headers=UA)
            with urllib.request.urlopen(req, timeout=5) as resp:
                html = resp.read().decode('utf-8', errors='ignore')
                m = re.search(r'<div[^>]*class="b_factrow[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
                if m:
                    desc = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                    if desc and len(desc) > 15:
                        return _to_simplified(desc)
        except Exception:
            pass
        return ""

    def _baidu_baike_search(query):
        """某某百科 API（国内最快）"""
        try:
            search_url = "https://baike.baidu.com/api/openapi/BaikeLemmaCardApi"
            params = urllib.parse.urlencode({
                'scope': 103, 'format': 'json', 'appid': 379020,
                'bk_key': query, 'bk_length': 600
            })
            req = urllib.request.Request(f"{search_url}?{params}", headers={
                **UA, 'Referer': 'https://baike.baidu.com/'
            })
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                abstract = data.get('abstract', '') or ''
                if not abstract:
                    card = data.get('card', {})
                    abstract = card.get('abstract', '') or ''
                if abstract:
                    abstract = re.sub(r'<[^>]+>', '', abstract).strip()
                    if abstract and len(abstract) > 10:
                        return _to_simplified(abstract)
        except Exception:
            pass
        return ""

    def _wiki_summary(lang, title):
        """Wikipedia 摘要"""
        try:
            api_url = f"https://{lang}.wikipedia.org/w/api.php"
            params = urllib.parse.urlencode({
                'action': 'query', 'prop': 'extracts', 'titles': title,
                'exintro': 1, 'explaintext': 1, 'format': 'json', 'utf8': 1
            })
            req = urllib.request.Request(f"{api_url}?{params}", headers=UA)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                pages = data.get('query', {}).get('pages', {})
                for pid, page in pages.items():
                    extract = page.get('extract', '').strip()
                    if extract:
                        return _to_simplified(extract)
        except Exception:
            pass
        return ""

    def _wiki_search(lang, query):
        """Wikipedia 搜索"""
        try:
            search_url = f"https://{lang}.wikipedia.org/w/api.php"
            params = urllib.parse.urlencode({
                'action': 'query', 'list': 'search', 'srsearch': query,
                'srlimit': 1, 'format': 'json', 'utf8': 1
            })
            req = urllib.request.Request(f"{search_url}?{params}", headers=UA)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                results = data.get('query', {}).get('search', [])
                if results:
                    return results[0].get('title', '')
        except Exception:
            pass
        return ""

    # ========== 多源采集（4 个源并行调用，总耗时≈最慢单源而非累加） ==========
    sources = {}  # {source_name: 原始描述}

    # 4 个源并行调用（ThreadPoolExecutor，IO 密集型，单条最坏 4s 而非 13s）
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as _ex:
        _fut_baike = _ex.submit(lambda: _baidu_baike_search(name) or _baidu_baike_search(clean))
        _fut_bing = _ex.submit(lambda: _bing_search(clean, context=dir_path))
        _fut_bing_kg = _ex.submit(lambda: _bing_knowledge(clean))
        _fut_wiki = _ex.submit(lambda: (_wiki_search('zh', clean), _wiki_summary('zh', _wiki_search('zh', clean) or clean)))
        try:
            _baike = _fut_baike.result(timeout=4)
            if _baike:
                sources['baike'] = _baike
        except Exception:
            pass
        try:
            _bing_results = _fut_bing.result(timeout=4)
            if _bing_results:
                sources['bing'] = _bing_results[0]
            else:
                try:
                    _bk = _fut_bing_kg.result(timeout=4)
                    if _bk:
                        sources['bing'] = _bk
                except Exception:
                    pass
        except Exception:
            pass
        try:
            _wiki_pair = _fut_wiki.result(timeout=4)
            if _wiki_pair and _wiki_pair[1]:
                sources['wiki'] = _wiki_pair[1]
        except Exception:
            pass

    # ========== 交叉验证（降级策略，避免因过于严格导致全部失败） ==========
    if not sources:
        return ""

    # 多源交叉验证：检查关键词重合度
    def _extract_keywords(text):
        """提取关键词（去掉停用词）"""
        if not text:
            return set()
        words = re.split(r'[\s，,。.；;、的与和是了在等及以]', text)
        return set(w for w in words if len(w) >= 2)

    # 有2个以上来源时，标准验证流程
    if len(sources) >= 2:
        source_names = list(sources.keys())
        base_name = 'baike' if 'baike' in sources else source_names[0]
        base_text = sources[base_name]
        base_keywords = _extract_keywords(base_text)

        verified_count = 1
        best_text = base_text
        for sn in source_names:
            if sn == base_name:
                continue
            other_keywords = _extract_keywords(sources[sn])
            overlap = base_keywords & other_keywords
            if base_keywords:
                ratio = len(overlap) / len(base_keywords)
            else:
                ratio = 0
            if ratio >= 0.20:  # 降低到20%
                verified_count += 1
                if len(sources[sn]) > len(best_text):
                    best_text = sources[sn]

        if verified_count >= 2:
            result = _simplify(best_text)
            if result and len(result) >= 3:
                # 文体判别：百科/词典/影视内容不采纳
                style = _judge_text_style(result)
                if style in ("encyclopedia", "dictionary"):
                    return ""
                # §P2：联网结果不自动采纳，标"疑似"让用户确认
                return f"疑似：{result}"

    # 降级：单源但有某某百科（最权威），且长度合理 → 标"疑似"返回
    if len(sources) == 1 or ('baike' in sources):
        best_text = sources.get('baike', list(sources.values())[0])
        result = _simplify(best_text)
        if result and len(result) >= 3:
            # 文体判别：百科/词典/影视内容不采纳
            style = _judge_text_style(result)
            if style in ("encyclopedia", "dictionary"):
                return ""
            # §P2：联网结果不自动采纳，标"疑似"让用户确认
            return f"疑似：{result}"

    return ""

    # [已注释-不可达死代码 + 穷举正则拒绝规则，待用§4.5文体判别法替代]
    # # 验证通过，激进精简后返回
    # result = _simplify(best_text)
    # if not result or len(result) < 3:
    #     return ""
    # # 最终过滤：搜索结果明显不相关则丢弃
    # _REJECT_RESULTS = {
    #     'et': r'^(et|Et)$',  # Bing偶尔返回"Et"
    #     'empty_brand': r'^(某某公司|阿里巴巴|腾讯控股|网易公司)',
    # }
    # import re
    # for _tag, _pat in _REJECT_RESULTS.items():
    #     if re.match(_pat, result):
    #         return ""
    # # 自动学习：写回词典，下次不联网
    # _save_to_learned_dict(dir_name, result)
    # return result

