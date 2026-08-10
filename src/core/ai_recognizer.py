#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多平台 AI 识别模块
==================
支持的平台（统一 OpenAI 兼容接口，共 13 个）：
国内：智谱 / 硅基流动 / DeepSeek / 讯飞星火 / 阿里通义 / 百度文心 /
      Kimi（月之暗面）/ 豆包（火山引擎）/ 腾讯混元
国外：Groq / OpenAI / Google Gemini / Mistral

使用方式：
    from ai_recognizer import AIRecognizer
    rec = AIRecognizer(platform="zhipu", api_key="xxx")
    results = rec.batch_identify(["Discord", "Steam", "Skyrim"])
    # 返回 {"Discord": {"name": "Discord", "desc": "...", "type": "通讯软件"}, ...}
"""
import json
import os
import time
import threading
import urllib.request
import urllib.error
from pathlib import Path


# ========== 平台配置 ==========
PLATFORMS = {
    "zhipu": {
        "name": "智谱",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4.7-flash",
        "signup_url": "https://open.bigmodel.cn",
        "doc_url": "https://open.bigmodel.cn/dev/api",
        "need_proxy": False,
        "rate_limit_ms": 3000,  # 智谱限流较严，批次间隔 3 秒
        "timeout_sec": 60,
        "batch_size": 5,  # 小批量避免 429
    },
    "siliconflow": {
        "name": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V3",
        "signup_url": "https://cloud.siliconflow.cn",
        "doc_url": "https://siliconflow.cn/models",
        "need_proxy": False,
        "rate_limit_ms": 200,
        "timeout_sec": 60,
        "batch_size": 20,
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "signup_url": "https://platform.deepseek.com",
        "doc_url": "https://platform.deepseek.com/docs",
        "need_proxy": False,
        "rate_limit_ms": 200,
        "timeout_sec": 60,
        "batch_size": 20,
    },
    "xfyun": {
        "name": "讯飞星火",
        "base_url": "https://spark-api-open.xf-yun.com/v1",
        "model": "lite",
        "signup_url": "https://xinghuo.xfyun.cn",
        "doc_url": "https://xinghuo.xfyun.cn/sparkapi",
        "need_proxy": False,
        "rate_limit_ms": 500,  # QPS=2，至少500ms间隔
        "timeout_sec": 60,
        "batch_size": 20,
    },
    "qwen": {
        "name": "阿里通义",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-turbo",
        "signup_url": "https://bailian.console.aliyun.com",
        "doc_url": "https://help.aliyun.com/zh/dashscope",
        "need_proxy": False,
        "rate_limit_ms": 200,
        "timeout_sec": 60,
        "batch_size": 20,
    },
    "ernie": {
        "name": "百度文心",
        "base_url": "https://qianfan.baidubce.com/v2",
        "model": "ernie-speed-8k",
        "signup_url": "https://qianfan.cloud.baidu.com",
        "doc_url": "https://cloud.baidu.com/qianfan",
        "need_proxy": False,
        "rate_limit_ms": 100,
        "timeout_sec": 60,
        "batch_size": 20,
    },
    "groq": {
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "signup_url": "https://console.groq.com",
        "doc_url": "https://console.groq.com/docs",
        "need_proxy": True,
        "rate_limit_ms": 200,
        "timeout_sec": 60,
        "batch_size": 20,
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "signup_url": "https://platform.openai.com",
        "doc_url": "https://platform.openai.com/docs",
        "need_proxy": True,
        "rate_limit_ms": 200,
        "timeout_sec": 60,
        "batch_size": 20,
    },
    "moonshot": {
        "name": "Kimi（月之暗面）",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "signup_url": "https://platform.moonshot.cn",
        "doc_url": "https://platform.moonshot.cn/docs",
        "need_proxy": False,
        "rate_limit_ms": 200,
        "timeout_sec": 60,
        "batch_size": 20,
    },
    "doubao": {
        "name": "豆包（火山引擎）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-1.5-pro-32k-250115",
        "signup_url": "https://console.volcengine.com/ark",
        "doc_url": "https://www.volcengine.com/docs/82379",
        "need_proxy": False,
        "rate_limit_ms": 200,
        "timeout_sec": 60,
        "batch_size": 20,
    },
    "hunyuan": {
        "name": "腾讯混元",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "model": "hunyuan-turbo",
        "signup_url": "https://console.cloud.tencent.com/hunyuan",
        "doc_url": "https://cloud.tencent.com/document/product/1729",
        "need_proxy": False,
        "rate_limit_ms": 200,
        "timeout_sec": 60,
        "batch_size": 20,
    },
    "gemini": {
        "name": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-2.0-flash",
        "signup_url": "https://aistudio.google.com",
        "doc_url": "https://ai.google.dev/gemini-api/docs",
        "need_proxy": True,
        "rate_limit_ms": 200,
        "timeout_sec": 60,
        "batch_size": 20,
    },
    "mistral": {
        "name": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-small-latest",
        "signup_url": "https://console.mistral.ai",
        "doc_url": "https://docs.mistral.ai",
        "need_proxy": True,
        "rate_limit_ms": 200,
        "timeout_sec": 60,
        "batch_size": 20,
    },
}


# ========== 识别 Prompt ==========
SYSTEM_PROMPT = """你是Windows软件识别专家。用户给你一批Windows下的目录，每个目录包含：目录名、完整路径、该目录下的文件列表。请根据"目录名 + 完整路径 + 文件列表"三者综合判断：这个目录属于什么软件、存放什么内容。

一、识别策略（按优先级）：
1. 路径上下文最可信：AppData\\Local\\xxx 通常是软件的数据/缓存目录；AppData\\Roaming\\xxx 是配置目录；Program Files\\xxx 是程序安装目录；ProgramData\\xxx 是共享数据目录；用户目录下的一级文件夹（下载/文档/图片等）按系统用途识别。
2. 文件列表是第二线索：与目录同名的 .exe → 主程序目录；cache/cache2/temp/logs → 缓存日志目录；installer/updater/update → 更新程序；package.json/pyproject.toml/requirements.txt → 开发项目；*.db/*.sqlite → 数据库文件；node_modules → Node依赖。
3. 目录名兜底：版本号目录（如 2.5.0、v1.0）通常是父级软件的子版本目录，识别为父级软件的子目录；短英文/拼音目录名优先按常见软件判断。
4. 隐藏目录按内容识别：.git=版本控制、.vscode=VSCode配置、.gradle=Gradle缓存、node_modules=Node依赖。

二、输出格式（严格遵守，否则解析失败）：
- 只输出一个JSON数组，禁止任何额外文字、解释、前后缀，禁止markdown代码块（不要```json```围栏）
- 每个元素恰好4个字段：
  dir：原始目录名，逐字返回，禁止改动（程序靠它对应条目）
  name：中文软件名（微信/钉钉/Steam）；国际知名英文品牌保留英文（Discord/Steam/VSCode）；识别不出软件时为空字符串""
  desc：一句中文说明，20字以内，必须具体说明"这个目录存什么"（如"聊天记录与图片缓存""程序主目录""安装包缓存"），禁止"相关数据""配置文件"这类含糊词
  type：从下方类型列表选一个，只能选一个，不能自造
- 无法确定/不认识：name="" desc="" type="未知"，禁止猜测编造

三、类型列表（只能从中选）：
浏览器/通讯软件/游戏平台/游戏/开发工具/IDE/代码编辑器/数据库/云盘同步/办公软件/办公套件/邮件客户端/安全软件/杀毒软件/防火墙/广告拦截/输入法/媒体播放器/视频剪辑/音频编辑/图像处理/3D建模/CAD/压缩工具/下载工具/翻墙工具/远程控制/虚拟机/容器/驱动程序/固件工具/系统工具/磁盘工具/卸载工具/截图工具/录屏/直播/笔记软件/思维导图/翻译工具/电子书/漫画/音乐/视频/股票/财务/记账/项目管理/时间管理/日历/聊天/会议/服务器/Web服务器/数据库服务器/编程语言/运行时/SDK/命令行工具/版本控制/监控/日志/卸载器/备份恢复/加密解密/字体管理/桌面美化/启动器/搜索工具/文件管理/同步工具/驱动/固件/系统组件/未知"""

USER_PROMPT_TEMPLATE = """目录列表（含路径和文件列表）：
{dirs}

请按规则识别每个目录，只返回JSON数组。"""


# ========== AI 识别器 ==========
class AIRecognizer:
    """多平台 AI 软件识别器"""

    def __init__(self, platform="zhipu", api_key="", config_dir=None):
        """
        :param platform: 平台标识（见 PLATFORMS）
        :param api_key: 用户 API Key
        :param config_dir: 缓存目录，默认与 config.json 同级
        """
        if platform not in PLATFORMS:
            raise ValueError(f"不支持的平台: {platform}，可选: {list(PLATFORMS.keys())}")
        self.platform = platform
        self.api_key = api_key
        self.config = PLATFORMS[platform]
        self.base_url = self.config["base_url"]
        self.model = self.config["model"]
        self.timeout = self.config["timeout_sec"]
        self.batch_size = self.config["batch_size"]
        self.rate_limit_ms = self.config["rate_limit_ms"]

        # 缓存路径（本文件位于 src/core/ai_recognizer.py；打包模式 = exe 同级）
        if config_dir is None:
            import sys as _sys
            if getattr(_sys, "frozen", False):
                config_dir = Path(_sys.executable).resolve().parent
            else:
                config_dir = Path(__file__).parent.parent.parent
        self.cache_file = Path(config_dir) / "ai_recognize_cache.json"
        self._cache_lock = threading.Lock()
        self._cache = self._load_cache()

    # ---------- 缓存管理 ----------
    def _load_cache(self):
        """加载本地缓存，避免重复调用"""
        try:
            if self.cache_file.exists():
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_cache(self):
        """保存缓存（带锁，防并发写）"""
        with self._cache_lock:
            try:
                self.cache_file.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.cache_file.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._cache, f, ensure_ascii=False, indent=2)
                tmp.replace(self.cache_file)
            except Exception:
                pass

    def clear_cache(self):
        """清空缓存"""
        with self._cache_lock:
            self._cache = {}
            try:
                self.cache_file.unlink(missing_ok=True)
            except Exception:
                pass

    def get_cache_stats(self):
        """返回缓存统计"""
        return {
            "total": len(self._cache),
            "identified": sum(1 for v in self._cache.values() if v.get("name")),
            "unknown": sum(1 for v in self._cache.values() if not v.get("name")),
        }

    # ---------- API 调用 ----------
    def _call_api(self, system_prompt, user_prompt, timeout=None):
        """调用 OpenAI 兼容接口（带 429 限流退避重试）"""
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,  # 低温度保证稳定
        }
        t = timeout or self.timeout

        # 429 限流退避重试：最多重试 3 次，间隔递增（2s/4s/8s）
        max_retries = 3
        for attempt in range(max_retries + 1):
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=t) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    content = data["choices"][0]["message"]["content"]
                    usage = data.get("usage", {})  # 官方统计的 token 消耗（计费依据，100% 准确）
                    return content, usage
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < max_retries:
                    # 限流：等待后重试，间隔递增
                    wait_sec = 2 ** (attempt + 1)  # 2s, 4s, 8s
                    time.sleep(wait_sec)
                    continue
                # 其他错误或重试次数用尽：抛出
                body = e.read().decode("utf-8", errors="replace")[:300]
                raise urllib.error.HTTPError(
                    url, e.code, f"{e.reason} | {body}", e.headers, None
                )
            except urllib.error.URLError as e:
                if attempt < max_retries:
                    time.sleep(2)
                    continue
                raise

    def _parse_json_response(self, content):
        """解析大模型返回的 JSON 数组"""
        clean = content.strip()
        # 去掉可能的 ```json``` 包裹
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        # 找到第一个 [ 和最后一个 ]
        start = clean.find("[")
        end = clean.rfind("]")
        if start == -1 or end == -1:
            raise ValueError(f"返回不是JSON数组: {content[:200]}")
        return json.loads(clean[start : end + 1])

    # ---------- 批量识别 ----------
    def _normalize_items(self, dir_items):
        """把输入归一化成 [(dir_name, full_path, files_list), ...]"""
        normalized = []
        for item in dir_items:
            if isinstance(item, (tuple, list)):
                if len(item) == 3:
                    d, p, files = item
                elif len(item) == 2:
                    d, p = item
                    files = []
                else:
                    d = item[0]
                    p = ""
                    files = []
            else:
                d = str(item)
                p = ""
                files = []
            d_clean = str(d).strip()
            if not d_clean:
                continue
            normalized.append((d_clean, p, files or []))
        return normalized

    def _split_cache(self, normalized):
        """把已缓存和未缓存分离
        :return: (todo_list, cached_results_dict)
        """
        todo = []
        cached = {}
        for d, p, files in normalized:
            if d in self._cache:
                cached[d] = self._cache[d]
            else:
                todo.append((d, p, files))
        return todo, cached

    def batch_identify(self, dir_items, progress_callback=None):
        """
        批量识别目录（带路径和文件列表）
        :param dir_items: 列表，每项是 (dir_name, full_path, files_list)
                         files_list 可为空列表
        :param progress_callback: 进度回调 (current, total, batch_result, usage_info)
        :return: (results_dict, total_usage)
                 results_dict: {dir_name: {"name":..., "desc":..., "type":...}}
                 total_usage: {"prompt_tokens":N, "completion_tokens":N, "total_tokens":N, "api_calls":N}
        """
        if not self.api_key:
            raise ValueError("API Key 未配置")

        # 兼容旧调用：如果传的是字符串列表，转成三元组
        normalized = []
        for item in dir_items:
            if isinstance(item, (tuple, list)):
                if len(item) == 3:
                    d, p, files = item
                elif len(item) == 2:
                    d, p = item
                    files = []
                else:
                    d = item[0]
                    p = ""
                    files = []
            else:
                d = str(item)
                p = ""
                files = []
            d_clean = str(d).strip()
            if not d_clean:
                continue
            normalized.append((d_clean, p, files or []))

        # 分离已缓存和未缓存
        todo = []
        results = {}
        for d, p, files in normalized:
            if d in self._cache:
                results[d] = self._cache[d]
            else:
                todo.append((d, p, files))

        # 累计 token 用量（官方返回，100% 准确）
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "api_calls": 0}

        if not todo:
            return results, total_usage

        # 分批调用
        total = len(todo)
        for i in range(0, total, self.batch_size):
            batch = todo[i : i + self.batch_size]
            batch_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            # 每批顶部显式初始化 batch_result，避免上一批成功值残留被 progress_callback 误推
            batch_result = {}
            try:
                batch_result, batch_usage = self._identify_batch(batch)
                results.update(batch_result)
                # 写入缓存
                with self._cache_lock:
                    self._cache.update(batch_result)
                self._save_cache()
            except Exception as e:
                # 整批失败，把这一批都标为未知
                err_msg = f"API调用失败: {type(e).__name__}: {str(e)[:100]}"
                for d, _, _ in batch:
                    results[d] = {"name": "", "desc": "", "type": "未知", "error": err_msg}
            # 累加 token
            total_usage["prompt_tokens"] += batch_usage.get("prompt_tokens", 0)
            total_usage["completion_tokens"] += batch_usage.get("completion_tokens", 0)
            total_usage["total_tokens"] += batch_usage.get("total_tokens", 0)
            total_usage["api_calls"] += 1
            # 进度回调（带 token 信息）
            if progress_callback:
                progress_callback(min(i + self.batch_size, total), total, batch_result if 'batch_result' in dir() else {}, total_usage)
            # 礼貌间隔（批次间至少 2 秒，避免触发 QPS 限制）
            if i + self.batch_size < total:
                time.sleep(max(2.0, self.rate_limit_ms / 1000.0))

        return results, total_usage

    def _identify_batch(self, batch):
        """识别一批目录（内部方法）
        :param batch: [(dir_name, full_path, files_list), ...]
        :return: (result_dict, usage_dict)
        """
        # 构造目录列表文本（含路径和文件列表）
        dirs_text = ""
        for d, p, files in batch:
            dirs_text += f"\n目录名: {d}"
            if p:
                dirs_text += f"\n路径: {p}"
            if files:
                # 最多发 10 个文件名，避免 prompt 过长
                files_str = ", ".join(files[:10])
                if len(files) > 10:
                    files_str += f"... (共{len(files)}个文件)"
                dirs_text += f"\n文件列表: {files_str}"
            dirs_text += "\n"

        user_prompt = USER_PROMPT_TEMPLATE.format(dirs=dirs_text)
        content, usage = self._call_api(SYSTEM_PROMPT, user_prompt)
        parsed = self._parse_json_response(content)

        # 转 dict
        result = {}
        for item in parsed:
            d = item.get("dir", "").strip()
            if not d:
                continue
            result[d] = {
                "name": item.get("name", "").strip(),
                "desc": item.get("desc", "").strip(),
                "type": item.get("type", "未知").strip(),
            }
        # 补齐未返回的
        for d, _, _ in batch:
            if d not in result:
                result[d] = {"name": "", "desc": "", "type": "未知"}
        return result, usage or {}

    # ---------- 单条识别（带缓存） ----------
    def identify(self, dir_name):
        """识别单个目录"""
        d = str(dir_name).strip()
        if d in self._cache:
            return self._cache[d]
        results, _ = self.batch_identify([d])
        return results.get(d, {"name": "", "desc": "", "type": "未知"})

    # ---------- 健康检查 ----------
    def test_connection(self):
        """测试 API Key 是否可用，返回 (success, message)"""
        if not self.api_key:
            return False, "API Key 未填"
        try:
            content, usage = self._call_api(
                "你是测试助手。",
                "请回答：1+1=?",
                timeout=15,
            )
            return True, f"连接成功，模型回复: {content[:50]}（消耗 {usage.get('total_tokens', '?')} tokens）"
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:200]
            return False, f"HTTP {e.code}: {body}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"


# ========== 模块级便捷函数 ==========
def get_platform_list():
    """返回平台列表，供 UI 下拉选择"""
    return [
        {
            "id": pid,
            "name": p["name"],
            "signup_url": p["signup_url"],
            "need_proxy": p["need_proxy"],
        }
        for pid, p in PLATFORMS.items()
    ]


def create_recognizer(platform, api_key, config_dir=None):
    """工厂函数：创建识别器"""
    return AIRecognizer(platform=platform, api_key=api_key, config_dir=config_dir)


if __name__ == "__main__":
    # 自测
    import sys

    if len(sys.argv) < 3:
        print("用法: python ai_recognizer.py <platform> <api_key>")
        print("可选平台:", ", ".join(PLATFORMS.keys()))
        sys.exit(1)

    platform = sys.argv[1]
    api_key = sys.argv[2]

    rec = AIRecognizer(platform=platform, api_key=api_key)
    print(f"平台: {rec.config['name']}")
    print(f"模型: {rec.model}")

    # 测试连接
    ok, msg = rec.test_connection()
    print(f"\n连接测试: {ok}, {msg}")
    if not ok:
        sys.exit(1)

    # 批量识别
    test_dirs = ["Discord", "Steam", "Skyrim", "WeChat", "nvidia.displayidfirmwareupdater"]
    print(f"\n批量识别 {len(test_dirs)} 个目录...")
    t0 = time.time()
    results = rec.batch_identify(test_dirs)
    elapsed = time.time() - t0
    print(f"耗时 {elapsed:.2f}s")
    print("\n结果:")
    for d, r in results.items():
        print(f"  {d:40s} → {r.get('name', ''):15s} | {r.get('desc', '')} ({r.get('type', '')})")

    # 缓存统计
    stats = rec.get_cache_stats()
    print(f"\n缓存: {stats}")
