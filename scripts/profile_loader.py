#!/usr/bin/env python3
"""
中央配置加载器 — 所有用户信息从此模块获取，禁止在各脚本中硬编码。

★ 设计原则:
  1. 唯一数据源: user_profile.json
  2. 所有脚本通过 from scripts.profile_loader import XXX 获取用户信息
  3. 本模块不依赖任何其他技能模块（无循环导入风险）
  4. WORK_DIR 通过环境变量 BOSS_WORK_DIR 或当前目录自动解析

用法:
    from scripts.profile_loader import (
        USER_NAME, CITY_CODE, TARGET_CITY, MIN_SALARY_K,
        DAILY_TARGET, SESSION_NAME, WORK_DIR, SKILL_DIR,
        SEARCH_KEYWORDS, USER_PROFILE, get_llm_prompt,
    )
"""

import os
import json
import shutil

# ═══════════════════════════════════════════════════════════
# 路径解析
# ═══════════════════════════════════════════════════════════

# 技能目录（自适应定位：脚本所在目录的父目录）
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 用户画像文件
USER_PROFILE_FILE = os.path.join(SKILL_DIR, "user_profile.json")

# 工作目录: 优先环境变量，否则使用当前目录
#   SKILL.md 指导用户 cd $workdir 后运行脚本，os.getcwd() 即为工作目录
#   多用户/多环境时可通过 BOSS_WORK_DIR 环境变量覆盖
WORK_DIR = os.environ.get("BOSS_WORK_DIR", os.getcwd())

# ═══════════════════════════════════════════════════════════
# 加载用户画像
# ═══════════════════════════════════════════════════════════


def _load_profile():
    """加载用户画像 JSON，返回 dict

    首次使用：user_profile.json 不存在但存在 user_profile.example.json 时，
    自动复制生成模板，再抛异常提示填写——不静默沿用示例城市（北京），避免投错城市。
    """
    if not os.path.exists(USER_PROFILE_FILE):
        example = os.path.join(SKILL_DIR, "user_profile.example.json")
        if os.path.exists(example):
            shutil.copyfile(example, USER_PROFILE_FILE)
            raise FileNotFoundError(
                f"首次使用：已从模板生成 {USER_PROFILE_FILE}\n"
                f"请至少填写 target_city / city_code / search_keywords / min_salary_k，然后重新运行。"
            )
        raise FileNotFoundError(
            f"用户画像文件不存在: {USER_PROFILE_FILE}\n"
            f"请复制 user_profile.example.json 为 user_profile.json 并按自身情况填写（见 SKILL.md）。"
        )
    with open(USER_PROFILE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# 模块级加载 — import 时即读取
USER_PROFILE = _load_profile()

# ═══════════════════════════════════════════════════════════
# 从用户画像提取的配置变量
# ═══════════════════════════════════════════════════════════

# --- 用户身份 ---
USER_NAME = USER_PROFILE.get("user_name", "")
ROLE_SUMMARY = USER_PROFILE.get("role_summary", "")
EXPERIENCE = USER_PROFILE.get("experience", "")
DOMAIN_DESCRIPTION = USER_PROFILE.get("domain_description", "")
DIRECTION_PRIORITY = USER_PROFILE.get("direction_priority", "")

# --- 求职目标 ---
TARGET_CITY = USER_PROFILE.get("target_city", "")
CITY_CODE = USER_PROFILE.get("city_code", "")
MIN_SALARY_K = USER_PROFILE.get("min_salary_k", 0)

# --- 投递参数 ---
DAILY_TARGET = USER_PROFILE.get("daily_target", 50)
SESSION_NAME = USER_PROFILE.get("session_name", "boss-main")

# --- 偏好 ---
PREFER = USER_PROFILE.get("prefer", [])
AVOID = USER_PROFILE.get("avoid", [])
PREFER_NO_BIG_COMPANY = USER_PROFILE.get("prefer_no_big_company", False)
PREFER_NO_HEADHUNTER = USER_PROFILE.get("prefer_no_headhunter", False)

# --- 搜索关键词（纯关键词，不含城市后缀）---
SEARCH_KEYWORDS = USER_PROFILE.get("search_keywords", [])

# --- 可选自定义排除列表 ---
C_EXCLUDE = USER_PROFILE.get("c_exclude", None)       # None = 不额外排除公司
T_EXCLUDE = USER_PROFILE.get("t_exclude", None)       # None = 不额外排除岗位词


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════


def get_profile():
    """获取完整用户画像（副本）"""
    return USER_PROFILE.copy()


try:
    from scripts import rules_loader as _rules
except Exception:  # 独立运行/单测场景
    try:
        from scripts import rules_loader as _rules
    except Exception:
        try:
            import rules_loader as _rules  # 独立运行时兜底
        except Exception:
            _rules = None

# 提示词版本（与 rules.json prompt.version 同源，便于审计回溯）
PROMPT_VERSION = _rules.prompt_version() if _rules else "2026-11-19.1"


def get_prompt_meta():
    """提示词元信息，供审计与一致性检查读取。"""
    return {
        "prompt_version": PROMPT_VERSION,
        "salary_policy": (_rules.get("salary.policy", "max_basis") if _rules else "max_basis"),
        "salary_policy_text": (_rules.get("salary.policy_text", "") if _rules else ""),
        "evidence_min_chars": (_rules.evidence_min_chars() if _rules else 15),
        "required_sections": (_rules.get("prompt.required_sections", []) if _rules else []),
    }


def get_work_dir():
    """获取工作目录（运行时重新解析，支持环境变量动态切换）"""
    return os.environ.get("BOSS_WORK_DIR", os.getcwd())


def get_llm_prompt():
    """生成 LLM 审核提示词（v5.8 / 提示词版本号见 PROMPT_VERSION）

    与旧版（v1.x）的差别：
    1. 把「是否匹配」的模糊判断，拆成四项必须逐项回答的子判定：
       工作地点 / 岗位职能 / 薪资 / 方向；
    2. 每项必须附 JD 原文证据（连续原文片段，不得改写、不得推断），
       无原文可引时必须判 UNKNOWN 并说明，禁止用想象补全；
    3. 薪资口径唯一来自 rules.json（单一真源），提示词内不再单独表述；
    4. 输出带 prompt_version，decision.json 可回溯到具体提示词版本。

    返回:
        str: 完整的 LLM 审核提示词
    """
    prefer_str = ", ".join(PREFER[:8]) if PREFER else "无"
    avoid_str = ", ".join(AVOID[:10]) if AVOID else "无"
    big_company_note = "不要大厂/上市公司（已上市、2000人以上）" if PREFER_NO_BIG_COMPANY else "无限制"
    headhunter_note = "不要猎头岗位" if PREFER_NO_HEADHUNTER else "无限制"

    if DOMAIN_DESCRIPTION:
        domain_section = DOMAIN_DESCRIPTION
    else:
        prefer_lines = "目标领域（偏好岗位）：" + (", ".join(PREFER) if PREFER else "不限")
        avoid_lines = "排除领域：" + (", ".join(AVOID) if AVOID else "无特定排除")
        domain_section = prefer_lines + "\n" + avoid_lines

    direction_section = ("\n【方向权重】" + DIRECTION_PRIORITY) if DIRECTION_PRIORITY else ""

    # 薪资口径：唯一来源 rules.json，禁止在此另写一套说法
    salary_line = _rules.get("salary.policy_text", "") if _rules else ""
    if not salary_line:
        salary_line = "判薪资看区间上限：上限 >= 最低门槛即可谈；上限 < 门槛才算不达标。"
    # 阈值必须与取证门同源（rules.json evidence.min_chars_by_field），否则提示词要求
    # 15 字、取证门按 4/10/5 判，AI 被两套标准夹住
    if _rules:
        _by = _rules.get("evidence.min_chars_by_field", {}) or {}
        _loc = _by.get("location", _rules.evidence_min_chars("location"))
        _func = _by.get("function", _rules.evidence_min_chars("function"))
        _sal = _by.get("salary", _rules.evidence_min_chars("salary"))
        _dir = _by.get("direction", _rules.evidence_min_chars("direction"))
    else:
        _loc, _func, _sal, _dir = 4, 10, 5, 10
    min_ev = _loc  # 兼容旧引用

    return f"""你是一位严格的岗位匹配审核专家。请根据候选人画像和岗位信息，逐项判定后给出结论。

【候选人画像】
- 姓名：{USER_NAME}
- 经验：{EXPERIENCE}
- 目标城市：{TARGET_CITY}
- 最低薪资门槛：月薪 {MIN_SALARY_K}K（{salary_line}）
- 大厂偏好：{big_company_note}
- 猎头偏好：{headhunter_note}
- 偏重方向：{prefer_str}
- 排除方向：{avoid_str}

【目标岗位范围】
{domain_section}{direction_section}

【输入】
{{job_info}}

【必须逐项回答的四个子判定】
1. 工作地点 location：岗位实际工作地是否为目标城市（{TARGET_CITY}）或可接受的驻外地区。
   JD/标题未写明地点 → verdict 记 UNKNOWN，不得默认"本地"。
2. 岗位职能 function：是运营/策划/内容/投放/管理类专业岗，还是拍剪、剪辑、摄影、美工、设计、
   数据标注、内容审核等执行/技术执行岗。
3. 薪资 salary：按上面写明的薪资口径判定，区间上限达标即可 PASS；面议/无法解析 → UNKNOWN。
4. 方向 direction：业务方向（住宿/文旅/酒店/民宿/景区/OTA/电商/本地生活/信息流投放等）
   是否落在目标范围内。

【引证纪律（硬要求）】
- 每一项的 evidence 必须是 JD 原文中的连续片段，原样摘录，最短字数：地点 {_loc} 字、职能 {_func} 字、薪资 {_sal} 字、方向 {_dir} 字；
- 禁止改写、概括、拼接不同段落，禁止用常识或标题补全 JD 未写明的内容；
- 原文确实没写 → verdict 填 UNKNOWN，evidence 填空字符串，并在 reason 里写明"JD未写明X"；
- 编造证据或不足最短字数，判定会被取证门直接作废；
- **REJECT 同样必须举证**：判 REJECT 的那一项必须引 JD 原句（比如异地、执行岗、薪资上限不达标），
  拒绝也要能被审计——说不清依据的 REJECT 记为"拒绝依据不可追溯"，与乱投同样不合格。

【输出格式】严格按以下 JSON 输出，不要任何解释文字：
{{
  "decision": "APPROVE" | "REJECT" | "NEED_JD",
  "reason": "一句话理由；若某项 UNKNOWN 必须在此说明缺哪项信息",
  "confidence": "high" | "medium" | "low",
  "sub": {{
    "location":  {{"verdict": "PASS" | "REJECT" | "UNKNOWN", "evidence": "JD原文片段"}},
    "function":  {{"verdict": "PASS" | "REJECT" | "UNKNOWN", "evidence": "JD原文片段"}},
    "salary":    {{"verdict": "PASS" | "REJECT" | "UNKNOWN", "evidence": "JD原文片段"}},
    "direction": {{"verdict": "PASS" | "REJECT" | "UNKNOWN", "evidence": "JD原文片段"}}
  }},
  "prompt_version": "{PROMPT_VERSION}"
}}

判定规则：
- APPROVE：四项子判定全部 PASS（且都有原文证据），才可 APPROVE；任一项 REJECT → REJECT；
- NEED_JD：标题/摘要信息不足以判四项时使用（尤其 location/salary 未写明）；
- 不允许"整体感觉合适"式 APPROVE；四项证据不全的 APPROVE 会被直接作废；
- 不确定时宁可 NEED_JD / REJECT，不要勉强 APPROVE。
"""

# ═══════════════════════════════════════════════════════════
# 版本信息
# ═══════════════════════════════════════════════════════════

__version__ = "5.8.0-prompt-v2"
__updated__ = "2026-11-19"
