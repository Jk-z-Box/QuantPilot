"""User-facing AI failures from fixed messages; never expose provider bodies or keys."""

from __future__ import annotations

MESSAGES = {
    "codex_sandbox": (
        "服务器 AI 沙箱未就绪",
        "隔离运行组件缺失或启动失败。请更新服务器组件并检查 quantpilot-ai 服务；不会降低隔离权限继续调用。",
    ),
    "codex_protocol": (
        "服务器 AI 协议不兼容",
        "服务器提交了当前 Codex 版本不支持的请求配置。请更新 QuantPilot；重新登录或更换密钥不能解决此问题。",
    ),
    "codex_unavailable": (
        "服务器 AI 服务未就绪",
        "请检查 quantpilot-ai 服务状态，或重试连接。不会改用 API Key。",
    ),
    "codex_failed": (
        "Codex 请求未完成",
        "官方服务未返回可采用的完整结果，请稍后重试；已有仓位保护继续运行。",
    ),
    "codex_busy": ("服务器 AI 正在处理请求", "请等待当前复核或登录操作结束后重试。"),
    "codex_limit": (
        "Codex 用量受限",
        "当前 ChatGPT/Codex 用量已达限制，请在 ChatGPT 检查用量，恢复后重试。不会扣 API Key 余额。",
    ),
    "codex_permission": (
        "设备授权未完成",
        "请在 ChatGPT 设置 → 安全中开启 Codex 设备码授权，然后重新生成授权码；工作区账户可能需管理员允许。",
    ),
    "codex_expired": ("设备授权码已失效", "本次授权已结束，请重新生成授权码。"),
    "codex_connected": ("服务器已连接 ChatGPT", "如需更换账户，请先断开当前服务器连接。"),
    "balance": (
        "API 余额不足",
        "OpenAI API 可用额度已用完或尚未充值。ChatGPT Plus 订阅不抵扣本平台的 API 调用费用，请检查该密钥所属组织的余额与账单。",
    ),
    "billing": ("API 计费受限", "请检查 OpenAI API 的付款状态、组织或项目支出上限；恢复可用额度后再重试。"),
    "credentials": ("API 密钥无效", "OpenAI 拒绝了这个 API 密钥，请重新保存有效密钥。"),
    "permission": ("API 权限受限", "请检查密钥所属项目的模型权限，以及账户和地区可用性。"),
    "model": (
        "模型不可用",
        "该密钥无法调用所选模型，请确认模型 ID 和项目访问权限。模型出现在列表中不保证复核请求可执行。",
    ),
    "rate_limit": ("API 请求限流", "请求频率或用量超过当前速率限制，请稍后重试或调长自动复核间隔。"),
    "schema": (
        "AI 输出格式不受支持",
        "所选模型拒绝了复核所需的结构化输出格式，请检查模型兼容性或联系维护者。",
    ),
    "parameters": (
        "AI 请求参数被拒绝",
        "OpenAI 不接受当前请求参数，请检查模型兼容性；更换密钥不能解决格式问题。",
    ),
    "provider": ("OpenAI 服务暂不可用", "OpenAI 服务返回错误，请稍后重试；已有仓位保护继续运行。"),
    "timeout": ("AI 响应超时", "规定时间内未收到完整结果，请稍后重试；超时请求仍可能消耗所选连接的额度。"),
    "network": ("AI 网络连接失败", "服务器无法完成到 OpenAI 的连接，请检查网络后重试。"),
    "output_limit": (
        "AI 输出长度不足",
        "模型在输出完整复核结果前用完了本次输出额度，结果未被采用；请联系维护者调整请求预算。",
    ),
    "incomplete": ("AI 响应未完成", "OpenAI 没有返回完整结果，本次复核未被采用。"),
    "refused": ("AI 未提供复核结果", "模型拒绝了本次请求，系统保留原有风控，不采用不完整建议。"),
    "invalid_response": (
        "AI 结果未通过校验",
        "返回内容缺失、格式不符或超出允许的风险调整范围，本次建议未被采用。",
    ),
}
BALANCE_CODES = {"credit_balance_exhausted", "insufficient_quota"}
BILLING_CODES = {
    "billing_hard_limit_reached",
    "billing_not_active",
    "billing_limit_user_error",
    "usage_limit_reached",
}
PLAN_CODES = {
    "subscription_sharing_user_not_eligible": "plan_eligibility",
    "subscription_sharing_usage_limit_exceeded": "plan_limit",
    "subscription_sharing_usage_unavailable": "oauth_temporary",
    "subscription_sharing_user_unavailable": "oauth_temporary",
    "subscription_sharing_unsupported_capability": "plan_capability",
    "subscription_sharing_route_not_supported": "plan_capability",
    "subscription_sharing_invalid_user": "oauth_login",
    "chatpass_v2_scope_not_authorized": "plan_permission",
    "chatpass_v2_invalid_authorization_context": "plan_permission",
}
MESSAGES.update(
    {
        "oauth_login": (
            "请重新登录 ChatGPT",
            "授权已失效或尚未连接。请使用官方登录重新授权；不会自动切换到 API Key。",
        ),
        "oauth_validation": (
            "登录校验未通过",
            "授权返回的身份、签名或会话参数未通过验证，请重新开始官方登录。",
        ),
        "oauth_temporary": ("ChatGPT 授权暂不可用", "服务或续期暂时失败，凭据已保留；请稍后重试。"),
        "plan_permission": (
            "未获准使用 ChatGPT 计划",
            "请重新登录并允许 QuantPilot 使用 ChatGPT 计划。仅登录账户不代表已授予模型调用权限。",
        ),
        "plan_eligibility": (
            "当前账户暂不符合授权条件",
            "OpenAI 暂未向此账户开放计划共享。已暂停该连接的请求，请在 ChatGPT 检查资格。",
        ),
        "plan_limit": (
            "ChatGPT 共享额度已用完",
            "已暂停该连接的请求，请在 ChatGPT 用量页面检查限制；恢复后点击重试授权连接。不会自动使用 API Key 余额。",
        ),
        "plan_capability": (
            "计划授权不支持此能力",
            "当前模型或计划授权不支持本次请求所需能力，请选择其他可用模型或关闭联网补全。",
        ),
    }
)
KNOWN_CODES = (
    set(PLAN_CODES)
    | BALANCE_CODES
    | BILLING_CODES
    | {
        "rate_limit_exceeded",
        "invalid_api_key",
        "model_not_found",
        "permission_denied",
        "unsupported_country_region_territory",
        "invalid_json_schema",
        "unsupported_parameter",
        "invalid_parameter",
        "context_length_exceeded",
    }
)


class AIError(ValueError):
    def __init__(self, kind, http_status=None, provider_code=None):
        self.kind = kind if kind in MESSAGES else "invalid_response"
        self.title, self.message = MESSAGES[self.kind]
        self.http_status = http_status
        self.provider_code = provider_code if provider_code in KNOWN_CODES else None
        super().__init__(self.title + "：" + self.message)

    def public(self):
        return {
            "kind": self.kind,
            "title": self.title,
            "message": self.message,
            "http_status": self.http_status,
            "provider_code": self.provider_code,
            "action_url": "https://chatgpt.com/settings/usage"
            if self.kind in ("plan_limit", "plan_eligibility", "plan_permission", "oauth_login")
            else "https://platform.openai.com/settings/organization/billing/overview"
            if self.kind in ("balance", "billing")
            else None,
            "action_label": "查看 ChatGPT 用量与授权"
            if self.kind in ("plan_limit", "plan_eligibility", "plan_permission", "oauth_login")
            else "检查 API 余额与账单"
            if self.kind in ("balance", "billing")
            else None,
        }


def http_error(response, oauth=False):
    try:
        error = response.json().get("error", {})
        error = error if isinstance(error, dict) else {}
    except Exception:
        error = {}
    code, category = error.get("code"), error.get("type")
    code = code if isinstance(code, str) else None
    # Provider text is inspected only to classify errors and is never returned or logged.
    message = str(error.get("message", "")).lower()
    status = response.status_code
    if code in PLAN_CODES:
        kind = PLAN_CODES[code]
    elif oauth and status == 401:
        kind = "oauth_login"
    elif oauth and status == 403:
        kind = "plan_permission"
    elif code in BALANCE_CODES or category == "insufficient_quota":
        kind = "balance"
    elif code in BILLING_CODES or (status in (400, 402, 403, 429) and "billing" in message):
        kind = "billing"
    elif status == 401:
        kind = "credentials"
    elif status == 403:
        kind = "permission"
    elif status == 404 or code == "model_not_found":
        kind = "model"
    elif status == 429:
        kind = "rate_limit"
    elif status >= 500:
        kind = "provider"
    elif code == "invalid_json_schema" or "schema" in message:
        kind = "schema"
    else:
        kind = "parameters"
    return AIError(kind, status, code)
