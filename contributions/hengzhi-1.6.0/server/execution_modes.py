"""User-selected reasoning speed, without reducing tools or validation."""
from urllib.parse import urlsplit

MODES = {
    'fast': ('快速', 'low', '优先处理用户本轮明确问题，简短交付结论、关键依据和缺口。避免与本轮无关的扩展分析；必要的取证、计算和核验必须照常完成。'),
    'balanced': ('均衡', 'high', '按问题复杂度分析，先给结论与证据；证据充分后交付，避免重复论证。'),
    'deep': ('深度', 'max', '对复杂问题充分审阅关键假设、反证、口径冲突和敏感性；围绕用户范围深入，不重复读取相同证据或无限润色。'),
}


def mode_name(value):
    return value if isinstance(value, str) and value in MODES else 'balanced'


def native_thinking_supported(base_url, model):
    # Only known official routes: third-party compatible APIs differ.
    url = urlsplit(base_url)
    return (url.scheme == 'https' and url.hostname == 'api.deepseek.com' and
            model in {'deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-pro'})


def mode_prompt(value):
    label, _, instruction = MODES[mode_name(value)]
    return f'本轮速度档位：{label}。{instruction}\n所有档位均须区分事实与推论，保留必要核验、引用和待复核事项；不能以提速为由编造或跳过核查。'
