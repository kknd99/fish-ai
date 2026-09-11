import os
import sys

from dotenv import load_dotenv

from src.infrastructure.external.ai_client_factory import build_async_openai_client

# --- AI & Notification Configuration ---
load_dotenv()

# --- File Paths & Directories ---
STATE_FILE = "xianyu_state.json"
IMAGE_SAVE_DIR = "images"
CONFIG_FILE = "config.json"
os.makedirs(IMAGE_SAVE_DIR, exist_ok=True)

# 任务隔离的临时图片目录前缀
TASK_IMAGE_DIR_PREFIX = "task_images_"

# --- API URL Patterns ---
API_URL_PATTERN = "h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search"
DETAIL_API_URL_PATTERN = "h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail"

# --- Environment Variables ---
API_KEY = os.getenv("OPENAI_API_KEY")
BASE_URL = os.getenv("OPENAI_BASE_URL")
MODEL_NAME = os.getenv("OPENAI_MODEL_NAME")
PROXY_URL = os.getenv("PROXY_URL")
# 通知渠道配置不在这里读取：唯一来源是 NotificationSettings
# （src/infrastructure/config/settings.py）。这里曾有一份重复的 os.getenv 拷贝，
# 结果是"两处都能配、行为却可能不一致"，已删除。
RUN_HEADLESS = os.getenv("RUN_HEADLESS", "true").lower() != "false"
LOGIN_IS_EDGE = os.getenv("LOGIN_IS_EDGE", "false").lower() == "true"
RUNNING_IN_DOCKER = os.getenv("RUNNING_IN_DOCKER", "false").lower() == "true"
AI_DEBUG_MODE = os.getenv("AI_DEBUG_MODE", "false").lower() == "true"
SKIP_AI_ANALYSIS = os.getenv("SKIP_AI_ANALYSIS", "false").lower() == "true"
ENABLE_THINKING = os.getenv("ENABLE_THINKING", "false").lower() == "true"
ENABLE_RESPONSE_FORMAT = os.getenv("ENABLE_RESPONSE_FORMAT", "true").lower() == "true"

# --- Headers ---
IMAGE_DOWNLOAD_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:139.0) Gecko/20100101 Firefox/139.0',
    'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}

# --- Client Initialization ---
# 客户端构造与代理环境准备收敛在 ai_client_factory：两条 AI 路径共用同一份逻辑，
# 避免"修好了一条、另一条还带着同一个 bug"（NO_PROXY 的 IPv6 CIDR 崩溃就是这样漏的）。
client = build_async_openai_client(
    api_key=API_KEY,
    base_url=BASE_URL if all([BASE_URL, MODEL_NAME]) else None,
    proxy_url=PROXY_URL,
)

# 检查AI客户端是否成功初始化
if not client:
    # 在 prompt_generator.py 中，如果 client 为 None，会直接报错退出
    # 在 spider_v2.py 中，AI分析会跳过
    # 为了保持一致性，这里只打印警告，具体逻辑由调用方处理
    pass

# 检查关键配置
if not all([BASE_URL, MODEL_NAME]) and 'prompt_generator.py' in sys.argv[0]:
    sys.exit("错误：请确保在 .env 文件中完整设置了 OPENAI_BASE_URL 和 OPENAI_MODEL_NAME。(OPENAI_API_KEY 对于某些服务是可选的)")

def get_ai_request_params(**kwargs):
    """
    构建AI请求参数，根据ENABLE_THINKING和ENABLE_RESPONSE_FORMAT环境变量决定是否添加相应参数
    """
    if ENABLE_THINKING:
        kwargs["extra_body"] = {"enable_thinking": False}
    
    # 如果禁用结构化输出，则移除 text.format 配置
    if not ENABLE_RESPONSE_FORMAT and "text" in kwargs:
        text_config = kwargs.get("text")
        if isinstance(text_config, dict):
            text_config = dict(text_config)
            text_config.pop("format", None)
            if text_config:
                kwargs["text"] = text_config
            else:
                del kwargs["text"]
    
    return kwargs
